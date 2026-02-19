"""Generic HTTP handlers for the multi-product provisioning API.

Endpoints:
  GET  /api/products                                    — List registered products
  POST /api/services/<product>                          — Create a service instance (via saga)
  GET  /api/services/<product>/<ns>/<name>              — Query status
  POST /api/services/<product>/<ns>/<name>/failover     — Force failover
  POST /api/services/<product>/multicloud               — Deploy to multiple clouds
"""

import json
import logging
import time

from flask import Blueprint, request, jsonify

from internal.models.types import ServiceRequest
from internal.products.registry import (
    get_product, list_products, validate_product_params, build_product_claim,
)
from internal.scheduler.scheduler import (
    schedule, get_circuit_breaker, CANDIDATES,
)
from internal.k8s import client as k8s
from internal.orchestration.saga import SagaOrchestrator, MultiCloudDeployer
from internal.db.database import (
    get_config, get_placement, record_placement,
    get_saga_by_resource, get_dr_policy,
    append_audit_log, provider_has_credentials,
    get_replication_pair, list_replication_pairs,
    update_replication_pair, get_replication_pair_by_id,
)
from internal.replication.goldengate import (
    needs_replication, resolve_dr_strategy, build_gg_config, build_gg_resources,
    ReplicationPair, ReplicationState, FailoverPhase, FailoverOrchestrator,
    TIER_DR_DEFAULTS,
)

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

    Uses the Saga orchestrator for multi-step provisioning with
    automatic compensation (rollback) on failure. The saga tracks:
    validate -> schedule -> apply_claim -> wait_ready -> register -> notify
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

    # Sticky placement check (before starting saga)
    existing = None
    try:
        existing = k8s.get_claim_generic(
            product.api_version, product.kind,
            body.get("namespace", "default"), body.get("name", ""),
        )
    except RuntimeError:
        pass
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
            "namespace": body.get("namespace", "default"),
            "name": body.get("name", ""),
        }), 200

    # Execute via Saga orchestrator
    t0 = time.time()
    saga = SagaOrchestrator(product_name, body)
    result = saga.execute()
    duration_ms = (time.time() - t0) * 1000

    status_code = 422 if result.get("status") == "failed" else 201

    # Audit log
    append_audit_log(
        action="create_service",
        product=product_name,
        name=body.get("name", ""),
        namespace=body.get("namespace", "default"),
        source_ip=request.remote_addr,
        method="POST",
        path=request.path,
        request_body=body,
        response_status=status_code,
        response_summary={
            "status": result.get("status"),
            "saga_id": result.get("saga_id"),
            "provider": result.get("placement", {}).get("provider"),
            "region": result.get("placement", {}).get("region"),
        },
        provider=result.get("placement", {}).get("provider"),
        region=result.get("placement", {}).get("region"),
        error=result.get("error"),
        duration_ms=duration_ms,
    )

    return jsonify(result), status_code


@services_bp.route("/api/services/<product_name>/<namespace>/<name>", methods=["GET"])
def service_status(product_name: str, namespace: str, name: str):
    """Return the claim status for any product, enriched with DB state."""
    product = get_product(product_name)
    if product is None:
        return jsonify({"error": f"Unknown product: '{product_name}'"}), 404

    # Get DB state (placement + saga)
    db_placement = get_placement(namespace, name)
    db_saga = get_saga_by_resource(namespace, name)

    # Try K8s claim
    claim = None
    try:
        claim = k8s.get_claim_generic(
            product.api_version, product.kind, namespace, name,
        )
    except RuntimeError:
        pass
    except Exception as e:
        logger.error("Error fetching claim: %s", e)

    if claim is None and db_placement is None:
        return jsonify({
            "error": "not_found",
            "message": f"{product.kind} '{namespace}/{name}' not found",
        }), 404

    secret_name = f"{name}{product.connection_secret_suffix}"
    secret_exists = False
    try:
        secret_exists = k8s.get_secret_exists(namespace, secret_name)
    except Exception:
        pass

    response = {
        "product": product_name,
        "connectionSecret": {
            "name": secret_name,
            "namespace": namespace,
            "exists": secret_exists,
        },
    }
    if claim:
        response["claim"] = claim
    if db_placement:
        response["placement_record"] = db_placement
    if db_saga:
        response["saga"] = db_saga

    return jsonify(response), 200


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

    # Record failover in DB
    record_placement(
        product=product_name, name=name, namespace=namespace,
        cell=svc_req.cell, tier=svc_req.tier, environment=svc_req.environment,
        provider=placement.provider, region=placement.region,
        cluster=placement.runtime_cluster, ha=svc_req.ha,
        total_score=placement.reason.get("selected", {}).get("total_score", 0),
        reason=placement.reason, status="READY" if applied else "PROVISIONING",
        failover=placement.failover,
    )

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

    append_audit_log(
        action="failover",
        product=product_name,
        name=name,
        namespace=namespace,
        source_ip=request.remote_addr,
        method="POST",
        path=request.path,
        request_body=body,
        response_status=200,
        response_summary={
            "status": "failover_complete",
            "previous_provider": current_provider,
            "new_provider": placement.provider,
            "new_region": placement.region,
        },
        provider=placement.provider,
        region=placement.region,
    )

    return jsonify(response), 200


# ── Multi-Cloud Deployment ──────────────────────────────────────────────────

@services_bp.route("/api/services/<product_name>/multicloud", methods=["POST"])
def multicloud_deploy(product_name: str):
    """Deploy a service to multiple cloud providers simultaneously.

    Request body:
      {
        "name": "checkout-web",
        "namespace": "team-checkout",
        "cell": "cell-us-east",
        "tier": "business_critical",
        "environment": "production",
        "image": "registry.example.com/checkout:v2",
        "target_providers": ["aws", "gcp"]
      }

    Creates independent placements per provider for active-active DR.
    """
    if get_config("multicloud_deploy_enabled", "true") != "true":
        return jsonify({"error": "Multi-cloud deployment is disabled"}), 403

    product = get_product(product_name)
    if product is None:
        return jsonify({
            "error": f"Unknown product: '{product_name}'",
            "available": [p["name"] for p in list_products()],
        }), 404

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    target_providers = body.pop("target_providers", None)
    if not target_providers or not isinstance(target_providers, list):
        return jsonify({"error": "'target_providers' must be a non-empty list (e.g., ['aws', 'gcp'])"}), 400

    # Enforce developer contract
    present = _FORBIDDEN_FIELDS & set(body.keys())
    if present:
        return jsonify({
            "error": f"Developer contract violation: fields {sorted(present)} are decided by the control plane",
        }), 400

    deployer = MultiCloudDeployer(product_name, body, target_providers)
    result = deployer.deploy()

    if "error" in result:
        return jsonify(result), 422

    return jsonify(result), 201


# ── DR Replication ──────────────────────────────────────────────────────────

@services_bp.route("/api/dr/replication", methods=["GET"])
def list_replications():
    """List all replication pairs with optional filters."""
    cell = request.args.get("cell")
    state = request.args.get("state")
    limit = int(request.args.get("limit", 50))
    pairs = list_replication_pairs(limit=limit, cell=cell, state=state)
    return jsonify({"replication_pairs": pairs, "dr_strategies": TIER_DR_DEFAULTS}), 200


@services_bp.route("/api/dr/replication/<namespace>/<name>", methods=["GET"])
def get_replication_status(namespace: str, name: str):
    """Return the replication status for a service instance."""
    pair = get_replication_pair(namespace, name)
    if not pair:
        return jsonify({
            "error": "not_found",
            "message": f"No replication pair for '{namespace}/{name}'",
            "hint": "Replication is only set up for tiers that require DR (low, business_critical)",
        }), 404

    dr_strategy = resolve_dr_strategy(pair["tier"])
    return jsonify({
        "replication": pair,
        "dr_strategy": dr_strategy,
        "needs_replication": needs_replication(pair["tier"]),
    }), 200


@services_bp.route("/api/dr/replication/<namespace>/<name>/lag", methods=["PUT"])
def update_lag(namespace: str, name: str):
    """Update the replication lag for a pair (called by monitoring probes)."""
    pair = get_replication_pair(namespace, name)
    if not pair:
        return jsonify({"error": "not_found"}), 404

    body = request.get_json(silent=True) or {}
    lag_ms = body.get("lag_ms")
    if lag_ms is None or not isinstance(lag_ms, (int, float)):
        return jsonify({"error": "'lag_ms' is required (number)"}), 400

    rpo_ms = pair["rpo_target_minutes"] * 60 * 1000
    new_state = pair["state"]
    if lag_ms > rpo_ms * 0.8:
        new_state = "LAG_WARNING"
    elif pair["state"] == "LAG_WARNING":
        new_state = "REPLICATING"

    update_replication_pair(pair["id"], replication_lag_ms=lag_ms, state=new_state)
    return jsonify({
        "status": "updated",
        "lag_ms": lag_ms,
        "rpo_target_ms": rpo_ms,
        "within_rpo": lag_ms <= rpo_ms,
        "state": new_state,
    }), 200


@services_bp.route("/api/dr/replication/<namespace>/<name>/failover", methods=["POST"])
def dr_failover(namespace: str, name: str):
    """Execute a controlled DR failover for a replication pair.

    Steps:
      1. Freeze writes (write fence)
      2. Verify replication lag <= RPO
      3. Promote secondary as writer
      4. Update DNS/GSLB
      5. Scale compute if pilot-light
    """
    pair_dict = get_replication_pair(namespace, name)
    if not pair_dict:
        return jsonify({
            "error": "not_found",
            "message": f"No replication pair for '{namespace}/{name}'",
        }), 404

    if pair_dict["state"] == "FAILOVER_IN_PROGRESS":
        return jsonify({"error": "Failover already in progress"}), 409

    # Reconstruct the ReplicationPair object
    pair = ReplicationPair(
        id=pair_dict["id"],
        cell=pair_dict["cell"],
        name=pair_dict["name"],
        namespace=pair_dict["namespace"],
        product=pair_dict["product"],
        tier=pair_dict["tier"],
        primary_provider=pair_dict["primary"]["provider"],
        primary_region=pair_dict["primary"]["region"],
        primary_cluster=pair_dict["primary"]["cluster"],
        primary_placement_id=pair_dict["primary"]["placement_id"],
        secondary_provider=pair_dict["secondary"]["provider"],
        secondary_region=pair_dict["secondary"]["region"],
        secondary_cluster=pair_dict["secondary"]["cluster"],
        secondary_placement_id=pair_dict["secondary"]["placement_id"],
        state=ReplicationState(pair_dict["state"]),
        replication_lag_ms=pair_dict["replication_lag_ms"],
        rpo_target_minutes=pair_dict["rpo_target_minutes"],
        rto_target_minutes=pair_dict["rto_target_minutes"],
        dr_strategy=pair_dict["dr_strategy"],
    )

    update_replication_pair(pair.id,
                            state=ReplicationState.FAILOVER_IN_PROGRESS.value,
                            failover_phase=FailoverPhase.FREEZE_WRITES.value)

    orchestrator = FailoverOrchestrator(pair)
    t0 = time.time()
    result = orchestrator.execute()
    duration_ms = (time.time() - t0) * 1000

    # Persist final state
    if result["status"] == "completed":
        update_replication_pair(
            pair.id,
            state=ReplicationState.FAILED_OVER.value,
            failover_phase=FailoverPhase.COMPLETED.value,
            primary_provider=pair.primary_provider,
            primary_region=pair.primary_region,
            primary_cluster=pair.primary_cluster,
            secondary_provider=pair.secondary_provider,
            secondary_region=pair.secondary_region,
            secondary_cluster=pair.secondary_cluster,
        )
    else:
        update_replication_pair(
            pair.id,
            state=ReplicationState.ERROR.value,
            failover_phase=FailoverPhase.ABORTED.value,
        )

    result["duration_ms"] = duration_ms

    append_audit_log(
        action="dr_failover",
        product=pair_dict["product"],
        name=name,
        namespace=namespace,
        source_ip=request.remote_addr,
        method="POST",
        path=request.path,
        response_status=200 if result["status"] == "completed" else 422,
        response_summary=result,
        provider=pair.primary_provider,
        region=pair.primary_region,
        duration_ms=duration_ms,
    )

    status_code = 200 if result["status"] == "completed" else 422
    return jsonify(result), status_code


@services_bp.route("/api/dr/strategies", methods=["GET"])
def get_dr_strategies():
    """Return the DR strategy definitions per tier."""
    return jsonify({"strategies": TIER_DR_DEFAULTS}), 200
