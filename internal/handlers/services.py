"""Generic HTTP handlers for the multi-product provisioning API.

Endpoints:
  GET  /api/products                                    — List registered products
  POST /api/services/<product>                          — Create a service instance
  GET  /api/services/<product>/<ns>/<name>              — Query status
  POST /api/services/<product>/<ns>/<name>/failover     — Force failover
"""

import json
import logging

from flask import Blueprint, request, jsonify

from internal.models.types import ServiceRequest
from internal.products.registry import (
    get_product, list_products, validate_product_params, build_product_claim,
)
from internal.scheduler.scheduler import (
    schedule, get_circuit_breaker, CANDIDATES,
)
from internal.k8s import client as k8s

logger = logging.getLogger(__name__)

services_bp = Blueprint("services", __name__)

# Fields that developers must NOT provide (decided by the control plane)
_FORBIDDEN_FIELDS = {"provider", "region", "runtimeCluster", "runtime_cluster", "network"}


@services_bp.route("/api/products", methods=["GET"])
def get_products():
    """List all registered products and their parameters."""
    return jsonify({"products": list_products()}), 200


@services_bp.route("/api/services/<product_name>", methods=["POST"])
def create_service(product_name: str):
    """Create a service instance for any registered product.

    The developer provides cell, tier, environment, ha, and product-specific
    parameters. The control plane decides provider, region, and network.
    """
    product = get_product(product_name)
    if product is None:
        return jsonify({
            "error": f"Unknown product: '{product_name}'",
            "available": [p["name"] for p in list_products()],
        }), 404

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    # Enforce developer contract
    present = _FORBIDDEN_FIELDS & set(body.keys())
    if present:
        return jsonify({
            "error": (
                f"Developer contract violation: fields {sorted(present)} "
                "are decided by the control plane and must not be provided"
            ),
        }), 400

    # Validate common fields
    svc_req = ServiceRequest(
        product=product_name,
        cell=body.get("cell", ""),
        tier=body.get("tier", ""),
        environment=body.get("environment", ""),
        ha=body.get("ha", False),
        namespace=body.get("namespace", "default"),
        name=body.get("name", ""),
    )
    errors = svc_req.validate()

    # Validate product-specific parameters
    errors.extend(validate_product_params(product, body))

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    # Sticky placement check
    existing = None
    try:
        existing = k8s.get_claim_generic(
            product.api_version, product.kind,
            svc_req.namespace, svc_req.name,
        )
    except RuntimeError:
        pass  # K8s not available — skip sticky check
    except Exception as e:
        logger.error("Error during sticky check: %s", e)

    if existing is not None:
        annotations = existing.get("metadata", {}).get("annotations", {})
        reason_raw = annotations.get("platform.example.org/placement-reason", "{}")
        try:
            reason = json.loads(reason_raw)
        except json.JSONDecodeError:
            reason = {"raw": reason_raw}

        spec_params = existing.get("spec", {}).get("parameters", {})
        return jsonify({
            "status": "exists",
            "sticky": True,
            "product": product_name,
            "message": "Claim already exists. Returning existing placement (sticky).",
            "placement": {
                "provider": spec_params.get("provider", "unknown"),
                "region": spec_params.get("region", "unknown"),
                "runtimeCluster": reason.get("selected", {}).get("runtime_cluster", "unknown"),
                "network": spec_params.get("network", {}),
            },
            "reason": reason,
            "namespace": svc_req.namespace,
            "name": svc_req.name,
        }), 200

    # Run the scheduler
    try:
        placement = schedule(svc_req)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422

    # Record success on circuit breaker
    cb = get_circuit_breaker(placement.provider)
    cb.record_success()

    # Build claim via product registry
    claim = build_product_claim(product, body, placement)

    # Attempt to apply
    applied = False
    apply_error = None
    try:
        k8s.apply_claim_generic(product.api_version, product.kind, claim)
        applied = True
    except RuntimeError as e:
        apply_error = str(e)
        logger.warning("Claim built but not applied: %s", e)
    except Exception as e:
        apply_error = str(e)
        logger.error("Unexpected error applying claim: %s", e)
        cb.record_failure()

    response = {
        "status": "created",
        "sticky": False,
        "product": product_name,
        "placement": {
            "provider": placement.provider,
            "region": placement.region,
            "runtimeCluster": placement.runtime_cluster,
            "network": placement.network,
        },
        "reason": placement.reason,
        "claim": claim,
        "applied_to_cluster": applied,
        "namespace": svc_req.namespace,
        "name": svc_req.name,
    }
    if apply_error:
        response["apply_warning"] = apply_error
    if placement.failover:
        response["failover"] = placement.failover

    return jsonify(response), 201


@services_bp.route("/api/services/<product_name>/<namespace>/<name>", methods=["GET"])
def service_status(product_name: str, namespace: str, name: str):
    """Return the claim status for any product."""
    product = get_product(product_name)
    if product is None:
        return jsonify({"error": f"Unknown product: '{product_name}'"}), 404

    try:
        claim = k8s.get_claim_generic(
            product.api_version, product.kind, namespace, name,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"Failed to fetch claim: {e}"}), 500

    if claim is None:
        return jsonify({
            "error": "not_found",
            "message": f"{product.kind} '{namespace}/{name}' not found",
        }), 404

    secret_name = f"{name}{product.connection_secret_suffix}"
    secret_exists = k8s.get_secret_exists(namespace, secret_name)

    return jsonify({
        "product": product_name,
        "claim": claim,
        "connectionSecret": {
            "name": secret_name,
            "namespace": namespace,
            "exists": secret_exists,
        },
    }), 200


@services_bp.route("/api/services/<product_name>/<namespace>/<name>/failover", methods=["POST"])
def force_service_failover(product_name: str, namespace: str, name: str):
    """Force rescheduling of an existing claim (override sticky placement)."""
    product = get_product(product_name)
    if product is None:
        return jsonify({"error": f"Unknown product: '{product_name}'"}), 404

    body = request.get_json(silent=True) or {}
    exclude_providers = set(body.get("exclude_providers", []))

    # Fetch existing Claim
    existing = None
    try:
        existing = k8s.get_claim_generic(
            product.api_version, product.kind, namespace, name,
        )
    except RuntimeError:
        pass
    except Exception as e:
        logger.error("Error fetching claim for failover: %s", e)

    if existing is None:
        return jsonify({
            "error": "not_found",
            "message": f"{product.kind} '{namespace}/{name}' not found. Cannot failover.",
        }), 404

    spec_params = existing.get("spec", {}).get("parameters", {})
    current_provider = spec_params.get("provider", "unknown")

    svc_req = ServiceRequest(
        product=product_name,
        cell=spec_params.get("cell", ""),
        tier=spec_params.get("tier", "medium"),
        environment=spec_params.get("environment", ""),
        ha=spec_params.get("ha", False),
        namespace=namespace,
        name=name,
    )

    filtered_candidates = [
        c for c in CANDIDATES if c.provider not in exclude_providers
    ]

    if not filtered_candidates:
        return jsonify({
            "error": f"No candidates remain after excluding: {sorted(exclude_providers)}",
        }), 422

    try:
        placement = schedule(svc_req, candidates=filtered_candidates)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422

    # Delete existing Claim before applying the new one
    try:
        k8s.delete_claim_generic(
            product.api_version, product.kind, namespace, name,
        )
    except Exception as e:
        logger.warning("Could not delete existing claim during failover: %s", e)

    # Reconstruct body from existing params for claim building
    claim_body = {**spec_params, "name": name, "namespace": namespace}
    claim = build_product_claim(product, claim_body, placement)

    applied = False
    apply_error = None
    try:
        k8s.apply_claim_generic(product.api_version, product.kind, claim)
        applied = True
    except RuntimeError as e:
        apply_error = str(e)
    except Exception as e:
        apply_error = str(e)

    response = {
        "status": "failover_complete",
        "product": product_name,
        "previous_provider": current_provider,
        "placement": {
            "provider": placement.provider,
            "region": placement.region,
            "runtimeCluster": placement.runtime_cluster,
            "network": placement.network,
        },
        "reason": placement.reason,
        "claim": claim,
        "applied_to_cluster": applied,
        "namespace": namespace,
        "name": name,
    }
    if apply_error:
        response["apply_warning"] = apply_error
    if placement.failover:
        response["failover"] = placement.failover

    return jsonify(response), 200
