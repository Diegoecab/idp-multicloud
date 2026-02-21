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

  GET  /api/admin/audit-log                        — List audit log entries
  GET  /api/admin/credentials                      — List provider credentials (masked)
  POST /api/admin/credentials                      — Save provider credentials
  DELETE /api/admin/credentials/<provider>         — Delete provider credentials
  POST /api/admin/credentials/<provider>/validate  — Validate provider credentials
"""

import logging
import re

from flask import Blueprint, request, jsonify

from internal.db.database import (
    get_all_config, get_config, set_config, delete_config,
    get_provider_configs, save_provider_config, delete_provider_config,
    get_provider_config_by_name,
    get_dr_policies, get_dr_policy, save_dr_policy, delete_dr_policy,
    list_sagas, get_saga, update_saga,
    list_placements, get_placement_by_id,
    list_audit_log,
    save_provider_credentials, get_provider_credentials,
    get_all_provider_credentials, delete_provider_credentials,
    mark_credentials_validated,
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


# ── Audit Log ───────────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/audit-log", methods=["GET"])
def get_audit_log():
    """Return the audit log with optional filters."""
    action = request.args.get("action")
    product = request.args.get("product")
    limit = int(request.args.get("limit", 100))
    return jsonify({
        "entries": list_audit_log(limit=limit, action=action, product=product),
    }), 200


# ── Provider Credentials ────────────────────────────────────────────────────

def _mask_value(value: str) -> str:
    """Mask a credential value, showing only last 4 chars."""
    if not value or len(value) <= 4:
        return "****"
    return "*" * (len(value) - 4) + value[-4:]


def _mask_credentials(cred_data: dict) -> dict:
    """Return a masked copy of credentials for safe display."""
    masked = {}
    for key, value in cred_data.items():
        if isinstance(value, str) and any(
            s in key.lower()
            for s in ("key", "secret", "password", "token", "credential")
        ):
            masked[key] = _mask_value(value)
        elif isinstance(value, str) and len(value) > 20:
            masked[key] = _mask_value(value)
        else:
            masked[key] = value
    return masked


@admin_bp.route("/api/admin/credentials", methods=["GET"])
def list_credentials():
    """List all provider credentials (summary only, no secrets)."""
    return jsonify({"credentials": get_all_provider_credentials()}), 200


@admin_bp.route("/api/admin/credentials/<provider>", methods=["GET"])
def get_credentials(provider: str):
    """Get credentials for a specific provider (masked)."""
    cred = get_provider_credentials(provider)
    if not cred:
        return jsonify({"error": f"No credentials for provider '{provider}'"}), 404
    cred["cred_data"] = _mask_credentials(cred["cred_data"])
    return jsonify({"credentials": cred}), 200


@admin_bp.route("/api/admin/credentials", methods=["POST"])
def save_credentials():
    """Save credentials for a cloud provider.

    Request body:
      {
        "provider": "aws",
        "cred_type": "access_key",
        "cred_data": {
          "aws_access_key_id": "AKIA...",
          "aws_secret_access_key": "wJal..."
        }
      }

    Supported cred_types:
      - access_key: AWS-style access key + secret key
      - service_account: GCP service account JSON
      - api_key: OCI API key
      - irsa: IAM Roles for Service Accounts (no stored secret)
      - workload_identity: GCP Workload Identity (no stored secret)
      - instance_principal: OCI Instance Principal (no stored secret)
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Request body must be valid JSON"}), 400
    if "provider" not in body:
        return jsonify({"error": "'provider' is required"}), 400
    if "cred_data" not in body:
        return jsonify({"error": "'cred_data' is required"}), 400

    provider = body["provider"]
    cred_type = body.get("cred_type", "access_key")
    cred_data = body["cred_data"]

    if not isinstance(cred_data, dict):
        return jsonify({"error": "'cred_data' must be a JSON object"}), 400

    save_provider_credentials(provider, cred_type, cred_data)
    logger.info("Credentials saved for provider '%s' (type: %s)", provider, cred_type)

    return jsonify({
        "status": "saved",
        "provider": provider,
        "cred_type": cred_type,
        "message": f"Credentials for '{provider}' saved. Run validation to verify.",
    }), 200


@admin_bp.route("/api/admin/credentials/<provider>", methods=["DELETE"])
def remove_credentials(provider: str):
    """Delete credentials for a provider."""
    if delete_provider_credentials(provider):
        return jsonify({"status": "deleted", "provider": provider}), 200
    return jsonify({"error": f"No credentials for provider '{provider}'"}), 404


# ── Crossplane Configuration ─────────────────────────────────────────────────

@admin_bp.route("/api/admin/crossplane/configure", methods=["POST"])
def configure_crossplane():
    """Configure Crossplane providers using IDP credentials and cloud provider settings.

    Reads every enabled provider from the Cloud Providers section and its
    corresponding credentials from the Credentials section, then applies:
      1. A Kubernetes Secret in crossplane-system with the raw credentials
      2. A Crossplane Provider package resource (installs the provider controller)
      3. A Crossplane ProviderConfig pointing at that Secret

    Returns a per-provider result list describing what was applied or skipped.
    """
    from internal.provisioning.crossplane import configure_crossplane_providers
    try:
        results = configure_crossplane_providers()
    except Exception as exc:
        logger.error("Crossplane configuration failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    any_error = any(r.get("status") == "error" for r in results)
    return jsonify({"results": results}), 207 if any_error else 200


@admin_bp.route("/api/admin/credentials/<provider>/validate", methods=["POST"])
def validate_credentials(provider: str):
    """Validate provider credentials by running a lightweight check.

    For now this checks structure (required fields present).
    In production this would call the actual cloud API (e.g., STS GetCallerIdentity).
    """
    cred = get_provider_credentials(provider)
    if not cred:
        return jsonify({
            "valid": False,
            "provider": provider,
            "error": "No credentials configured for this provider",
        }), 404

    cred_data = cred["cred_data"]
    cred_type = cred["cred_type"]
    errors = []

    if provider == "aws":
        if cred_type == "access_key":
            if not cred_data.get("aws_access_key_id"):
                errors.append("Missing 'aws_access_key_id'")
            elif not re.match(r"^AKI[A-Z0-9]{13,}$", cred_data["aws_access_key_id"]):
                errors.append("'aws_access_key_id' does not match expected format (AKIA...)")
            if not cred_data.get("aws_secret_access_key"):
                errors.append("Missing 'aws_secret_access_key'")
        elif cred_type not in ("irsa",):
            errors.append(f"Unsupported cred_type '{cred_type}' for AWS")

    elif provider == "gcp":
        if cred_type == "service_account":
            if not cred_data.get("project_id"):
                errors.append("Missing 'project_id'")
            if not cred_data.get("client_email"):
                errors.append("Missing 'client_email'")
            if not cred_data.get("private_key"):
                errors.append("Missing 'private_key'")
        elif cred_type not in ("workload_identity",):
            errors.append(f"Unsupported cred_type '{cred_type}' for GCP")

    elif provider == "oci":
        if cred_type == "api_key":
            if not cred_data.get("tenancy_ocid"):
                errors.append("Missing 'tenancy_ocid'")
            if not cred_data.get("user_ocid"):
                errors.append("Missing 'user_ocid'")
            if not cred_data.get("fingerprint"):
                errors.append("Missing 'fingerprint'")
            if not cred_data.get("private_key"):
                errors.append("Missing 'private_key'")
        elif cred_type not in ("instance_principal",):
            errors.append(f"Unsupported cred_type '{cred_type}' for OCI")

    is_valid = len(errors) == 0
    mark_credentials_validated(provider, is_valid)

    return jsonify({
        "valid": is_valid,
        "provider": provider,
        "cred_type": cred_type,
        "errors": errors if errors else None,
        "message": "Credentials validated successfully" if is_valid else "Validation failed",
    }), 200 if is_valid else 422
