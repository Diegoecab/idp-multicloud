"""HTTP handlers for the MySQL provisioning API.

Endpoints:
  GET  /health                              — Health check
  POST /api/mysql                           — Create a managed MySQL instance
  GET  /api/status/mysql/<ns>/<name>        — Query status of an existing claim
  POST /api/mysql/<ns>/<name>/failover      — Force failover (override sticky placement)
  GET  /api/providers/health                — View provider health + circuit breakers
  PUT  /api/providers/<provider>/health     — Set provider health status
  GET  /api/analytics                       — Placement analytics summary
  POST /api/experiments                     — Create an A/B experiment
  GET  /api/experiments                     — List all experiments
  DELETE /api/experiments/<id>              — Delete an experiment
  GET  /api/flags                           — List feature flags
  PUT  /api/flags/<name>                    — Set a feature flag
"""

import json
import logging

from flask import Blueprint, request, jsonify

from internal.models.types import MySQLRequest
from internal.scheduler.scheduler import (
    schedule,
    set_provider_health,
    get_all_provider_health,
    get_all_circuit_breakers,
    get_circuit_breaker,
)
from internal.scheduler.experiments import (
    create_experiment,
    list_experiments,
    delete_experiment,
    analytics,
    set_feature_flag,
    list_feature_flags,
    delete_feature_flag,
)
from internal.k8s.claim_builder import build_claim
from internal.k8s import client as k8s

logger = logging.getLogger(__name__)

mysql_bp = Blueprint("mysql", __name__)

# Fields that developers must NOT provide (decided by the control plane)
_FORBIDDEN_FIELDS = {"provider", "region", "runtimeCluster", "runtime_cluster", "network"}


@mysql_bp.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"}), 200


@mysql_bp.route("/api/mysql", methods=["POST"])
def create_mysql():
    """Create a managed MySQL instance via the cell-based contract.

    The developer provides cell, tier, environment, size, storageGB, ha.
    The control plane decides provider, region, runtimeCluster, and network.
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    # Enforce developer contract: reject provider/region/network overrides
    present = _FORBIDDEN_FIELDS & set(body.keys())
    if present:
        return jsonify({
            "error": (
                f"Developer contract violation: fields {sorted(present)} "
                "are decided by the control plane and must not be provided"
            ),
        }), 400

    # Build and validate request
    req = MySQLRequest(
        cell=body.get("cell", ""),
        tier=body.get("tier", ""),
        environment=body.get("environment", ""),
        size=body.get("size", ""),
        storage_gb=body.get("storageGB", 0),
        ha=body.get("ha", False),
        namespace=body.get("namespace", "default"),
        name=body.get("name", ""),
    )

    errors = req.validate()
    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    # Sticky placement check: if a Claim already exists, return it unchanged
    existing = None
    try:
        existing = k8s.get_claim(req.namespace, req.name)
    except RuntimeError:
        pass  # K8s not available — skip sticky check, proceed with scheduling
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
            "message": "Claim already exists. Returning existing placement (sticky — no rescheduling).",
            "placement": {
                "provider": spec_params.get("provider", "unknown"),
                "region": spec_params.get("region", "unknown"),
                "runtimeCluster": reason.get("selected", {}).get("runtime_cluster", "unknown"),
                "network": spec_params.get("network", {}),
            },
            "reason": reason,
            "namespace": req.namespace,
            "name": req.name,
        }), 200

    # Run the scheduler
    try:
        placement = schedule(req)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422

    # Record success on the provider's circuit breaker
    cb = get_circuit_breaker(placement.provider)
    cb.record_success()

    # Build Crossplane claim manifest
    claim = build_claim(req, placement)

    # Attempt to apply the claim to the cluster
    applied = False
    apply_error = None
    try:
        k8s.apply_claim(claim)
        applied = True
    except RuntimeError as e:
        apply_error = str(e)
        logger.warning("Claim built but not applied to cluster: %s", e)
    except Exception as e:
        apply_error = str(e)
        logger.error("Unexpected error applying claim: %s", e)
        # Record failure on the provider's circuit breaker
        cb.record_failure()

    response = {
        "status": "created",
        "sticky": False,
        "placement": {
            "provider": placement.provider,
            "region": placement.region,
            "runtimeCluster": placement.runtime_cluster,
            "network": placement.network,
        },
        "reason": placement.reason,
        "claim": claim,
        "applied_to_cluster": applied,
        "namespace": req.namespace,
        "name": req.name,
    }
    if apply_error:
        response["apply_warning"] = apply_error
    if placement.failover:
        response["failover"] = placement.failover

    return jsonify(response), 201


@mysql_bp.route("/api/status/mysql/<namespace>/<name>", methods=["GET"])
def mysql_status(namespace: str, name: str):
    """Return the full Claim object and connection Secret status.

    The Secret's existence is reported but its values are NEVER returned.
    """
    try:
        claim = k8s.get_claim(namespace, name)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        return jsonify({"error": f"Failed to fetch claim: {e}"}), 500

    if claim is None:
        return jsonify({
            "error": "not_found",
            "message": f"MySQLInstanceClaim '{namespace}/{name}' not found",
        }), 404

    secret_name = f"{name}-conn"
    secret_exists = k8s.get_secret_exists(namespace, secret_name)

    return jsonify({
        "claim": claim,
        "connectionSecret": {
            "name": secret_name,
            "namespace": namespace,
            "exists": secret_exists,
        },
    }), 200


# ── Failover Endpoint ────────────────────────────────────────────────────────

@mysql_bp.route("/api/mysql/<namespace>/<name>/failover", methods=["POST"])
def force_failover(namespace: str, name: str):
    """Force rescheduling of an existing Claim (override sticky placement).

    Use this during disaster recovery to migrate a workload to a different
    provider/region. The existing Claim is deleted and a new one is created
    with fresh scheduling.

    Request body (optional):
      { "exclude_providers": ["aws"] }  — exclude specific providers from scheduling
    """
    body = request.get_json(silent=True) or {}
    exclude_providers = set(body.get("exclude_providers", []))

    # Fetch existing Claim to extract the original request parameters
    existing = None
    try:
        existing = k8s.get_claim(namespace, name)
    except RuntimeError:
        pass
    except Exception as e:
        logger.error("Error fetching claim for failover: %s", e)

    if existing is None:
        return jsonify({
            "error": "not_found",
            "message": f"MySQLInstanceClaim '{namespace}/{name}' not found. Cannot failover.",
        }), 404

    spec_params = existing.get("spec", {}).get("parameters", {})
    current_provider = spec_params.get("provider", "unknown")

    # Reconstruct the request from the existing Claim parameters
    req = MySQLRequest(
        cell=spec_params.get("cell", ""),
        tier=spec_params.get("tier", "medium"),
        environment=spec_params.get("environment", ""),
        size=spec_params.get("size", "medium"),
        storage_gb=spec_params.get("storageGB", 50),
        ha=spec_params.get("ha", False),
        namespace=namespace,
        name=name,
    )

    # Import candidates and filter out excluded providers
    from internal.scheduler.scheduler import CANDIDATES
    filtered_candidates = [
        c for c in CANDIDATES
        if c.provider not in exclude_providers
    ]

    if not filtered_candidates:
        return jsonify({
            "error": f"No candidates remain after excluding providers: {sorted(exclude_providers)}",
        }), 422

    # Run the scheduler with filtered pool
    try:
        placement = schedule(req, candidates=filtered_candidates)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422

    # Delete existing Claim before applying the new one
    try:
        k8s.delete_claim(namespace, name)
    except Exception as e:
        logger.warning("Could not delete existing claim during failover: %s", e)

    # Build and apply the new Claim
    claim = build_claim(req, placement)
    applied = False
    apply_error = None
    try:
        k8s.apply_claim(claim)
        applied = True
    except RuntimeError as e:
        apply_error = str(e)
    except Exception as e:
        apply_error = str(e)

    response = {
        "status": "failover_complete",
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


# ── Provider Health Endpoints ────────────────────────────────────────────────

@mysql_bp.route("/api/providers/health", methods=["GET"])
def providers_health():
    """Return health status and circuit breaker state for all providers."""
    return jsonify({
        "providers": get_all_provider_health(),
        "circuit_breakers": get_all_circuit_breakers(),
    }), 200


@mysql_bp.route("/api/providers/<provider>/health", methods=["PUT"])
def update_provider_health(provider: str):
    """Set the health status of a specific provider.

    Request body:
      { "healthy": false }
    """
    body = request.get_json(silent=True)
    if body is None or "healthy" not in body:
        return jsonify({"error": "Request body must include 'healthy' (boolean)"}), 400

    healthy = bool(body["healthy"])
    set_provider_health(provider, healthy)

    return jsonify({
        "provider": provider,
        "healthy": healthy,
        "message": f"Provider '{provider}' marked as {'healthy' if healthy else 'unhealthy'}",
    }), 200


# ── Analytics Endpoint ───────────────────────────────────────────────────────

@mysql_bp.route("/api/analytics", methods=["GET"])
def get_analytics():
    """Return placement analytics summary for data-driven decisions.

    Includes provider distribution, tier distribution, gate rejection rates,
    average scores, and experiment group breakdowns.
    """
    return jsonify(analytics.get_summary()), 200


# ── Experiment Endpoints ─────────────────────────────────────────────────────

@mysql_bp.route("/api/experiments", methods=["GET"])
def get_experiments():
    """List all A/B experiments."""
    return jsonify({"experiments": list_experiments()}), 200


@mysql_bp.route("/api/experiments", methods=["POST"])
def create_experiment_endpoint():
    """Create a new A/B experiment on placement scoring weights.

    Request body:
      {
        "id": "exp-cost-boost-001",
        "description": "Test 10% higher cost weight for critical tier",
        "variant_weights": {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60},
        "traffic_percentage": 0.2,
        "tier": "critical"
      }
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    required = {"id", "description", "variant_weights", "traffic_percentage"}
    missing = required - set(body.keys())
    if missing:
        return jsonify({"error": f"Missing required fields: {sorted(missing)}"}), 400

    try:
        exp = create_experiment(
            experiment_id=body["id"],
            description=body["description"],
            variant_weights=body["variant_weights"],
            traffic_percentage=body["traffic_percentage"],
            tier=body.get("tier", "*"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({
        "status": "created",
        "experiment": {
            "id": exp.id,
            "description": exp.description,
            "variant_weights": exp.variant_weights,
            "traffic_percentage": exp.traffic_percentage,
            "tier": exp.tier,
        },
    }), 201


@mysql_bp.route("/api/experiments/<experiment_id>", methods=["DELETE"])
def delete_experiment_endpoint(experiment_id: str):
    """Delete an experiment by ID."""
    deleted = delete_experiment(experiment_id)
    if not deleted:
        return jsonify({"error": f"Experiment '{experiment_id}' not found"}), 404
    return jsonify({"status": "deleted", "id": experiment_id}), 200


# ── Feature Flag Endpoints ───────────────────────────────────────────────────

@mysql_bp.route("/api/flags", methods=["GET"])
def get_flags():
    """List all feature flags."""
    return jsonify({"flags": list_feature_flags()}), 200


@mysql_bp.route("/api/flags/<name>", methods=["PUT"])
def set_flag(name: str):
    """Set a feature flag.

    Request body:
      { "enabled": true }
    """
    body = request.get_json(silent=True)
    if body is None or "enabled" not in body:
        return jsonify({"error": "Request body must include 'enabled' (boolean)"}), 400

    enabled = bool(body["enabled"])
    set_feature_flag(name, enabled)
    return jsonify({"flag": name, "enabled": enabled}), 200


@mysql_bp.route("/api/flags/<name>", methods=["DELETE"])
def remove_flag(name: str):
    """Delete a feature flag."""
    deleted = delete_feature_flag(name)
    if not deleted:
        return jsonify({"error": f"Flag '{name}' not found"}), 404
    return jsonify({"status": "deleted", "flag": name}), 200
