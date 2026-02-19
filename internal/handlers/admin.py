"""Admin HTTP handlers for platform configuration.

Endpoints:
  GET  /api/admin/config                           — List all platform config
  PUT  /api/admin/config/<key>                     — Set a config value
  DELETE /api/admin/config/<key>                   — Delete a config key

  GET  /api/admin/providers                        — List cloud provider configs
  POST /api/admin/providers                        — Add/update a provider config
  DELETE /api/admin/providers/<name>               — Delete a provider config

  GET  /api/admin/dr-policies                      — List DR policies
  POST /api/admin/dr-policies                      — Create/update a DR policy
  DELETE /api/admin/dr-policies/<tier>             — Delete a DR policy

  GET  /api/admin/sagas                            — List saga executions
  GET  /api/admin/sagas/<id>                       — Get saga detail
  POST /api/admin/sagas/<id>/retry                 — Retry a failed saga

  GET  /api/admin/placements                       — List placement history
  GET  /api/admin/placements/<id>                  — Get placement detail

  POST /api/services/<product>/multicloud          — Deploy to multiple clouds
"""

import logging

from flask import Blueprint, request, jsonify

from internal.db.database import (
    get_all_config, get_config, set_config, delete_config,
    get_provider_configs, save_provider_config, delete_provider_config,
    get_provider_config_by_name,
    get_dr_policies, get_dr_policy, save_dr_policy, delete_dr_policy,
    list_sagas, get_saga, update_saga,
    list_placements, get_placement_by_id,
)

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)


# ── Platform Config ──────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/config", methods=["GET"])
def list_config():
    return jsonify({"config": get_all_config()}), 200


@admin_bp.route("/api/admin/config/<key>", methods=["PUT"])
def update_config(key: str):
    body = request.get_json(silent=True)
    if body is None or "value" not in body:
        return jsonify({"error": "Request body must include 'value'"}), 400
    set_config(key, str(body["value"]))
    return jsonify({"key": key, "value": str(body["value"])}), 200


@admin_bp.route("/api/admin/config/<key>", methods=["DELETE"])
def remove_config(key: str):
    if delete_config(key):
        return jsonify({"status": "deleted", "key": key}), 200
    return jsonify({"error": f"Config key '{key}' not found"}), 404


# ── Cloud Provider Config ────────────────────────────────────────────────────

@admin_bp.route("/api/admin/providers", methods=["GET"])
def list_provider_configs():
    return jsonify({"providers": get_provider_configs()}), 200


@admin_bp.route("/api/admin/providers", methods=["POST"])
def create_or_update_provider():
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Request body must be valid JSON"}), 400
    if "name" not in body:
        return jsonify({"error": "'name' is required"}), 400

    save_provider_config(
        name=body["name"],
        display_name=body.get("display_name", body["name"].upper()),
        enabled=body.get("enabled", True),
        credentials_type=body.get("credentials_type", "secret"),
        credentials_ref=body.get("credentials_ref", ""),
        regions=body.get("regions", []),
        settings=body.get("settings", {}),
    )
    return jsonify({
        "status": "saved",
        "provider": get_provider_config_by_name(body["name"]),
    }), 200


@admin_bp.route("/api/admin/providers/<name>", methods=["DELETE"])
def remove_provider(name: str):
    if delete_provider_config(name):
        return jsonify({"status": "deleted", "provider": name}), 200
    return jsonify({"error": f"Provider '{name}' not found"}), 404


# ── DR Policies ──────────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/dr-policies", methods=["GET"])
def list_dr():
    return jsonify({"policies": get_dr_policies()}), 200


@admin_bp.route("/api/admin/dr-policies", methods=["POST"])
def create_or_update_dr():
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Request body must be valid JSON"}), 400
    if "tier" not in body:
        return jsonify({"error": "'tier' is required"}), 400

    valid_strategies = ["active_active", "active_passive", "backup_restore", "pilot_light"]
    strategy = body.get("strategy", "active_passive")
    if strategy not in valid_strategies:
        return jsonify({"error": f"strategy must be one of {valid_strategies}"}), 400

    save_dr_policy(
        tier=body["tier"],
        strategy=strategy,
        failover_providers=body.get("failover_providers", []),
        auto_failover=body.get("auto_failover", False),
        rto_target=body.get("rto_target", 60),
        rpo_target=body.get("rpo_target", 5),
        settings=body.get("settings", {}),
    )
    return jsonify({
        "status": "saved",
        "policy": get_dr_policy(body["tier"]),
    }), 200


@admin_bp.route("/api/admin/dr-policies/<tier>", methods=["DELETE"])
def remove_dr(tier: str):
    if delete_dr_policy(tier):
        return jsonify({"status": "deleted", "tier": tier}), 200
    return jsonify({"error": f"DR policy for tier '{tier}' not found"}), 404


# ── Saga Executions ──────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/sagas", methods=["GET"])
def get_sagas():
    state = request.args.get("state")
    limit = int(request.args.get("limit", 50))
    return jsonify({"sagas": list_sagas(limit=limit, state=state)}), 200


@admin_bp.route("/api/admin/sagas/<int:saga_id>", methods=["GET"])
def get_saga_detail(saga_id: int):
    saga = get_saga(saga_id)
    if not saga:
        return jsonify({"error": "Saga not found"}), 404
    return jsonify({"saga": saga}), 200


@admin_bp.route("/api/admin/sagas/<int:saga_id>/retry", methods=["POST"])
def retry_saga(saga_id: int):
    saga = get_saga(saga_id)
    if not saga:
        return jsonify({"error": "Saga not found"}), 404
    if saga["state"] not in ("FAILED", "ROLLED_BACK"):
        return jsonify({"error": f"Cannot retry saga in state '{saga['state']}'"}), 400
    update_saga(saga_id, state="PENDING", current_step="validate",
                steps_completed=[], error=None)
    return jsonify({"status": "retrying", "saga": get_saga(saga_id)}), 200


# ── Placement History ────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/placements", methods=["GET"])
def get_placements():
    product = request.args.get("product")
    status = request.args.get("status")
    limit = int(request.args.get("limit", 50))
    return jsonify({"placements": list_placements(limit=limit, product=product, status=status)}), 200


@admin_bp.route("/api/admin/placements/<int:placement_id>", methods=["GET"])
def get_placement_detail(placement_id: int):
    p = get_placement_by_id(placement_id)
    if not p:
        return jsonify({"error": "Placement not found"}), 404
    return jsonify({"placement": p}), 200
