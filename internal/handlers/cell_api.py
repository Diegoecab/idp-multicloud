import os
import time
from flask import Blueprint, jsonify, request, send_from_directory

from internal.policy.data import policy_store
from internal.scheduler.placement_engine import schedule
from internal.dr.orchestrator import failover, register_replication, failover_events
from internal.traffic.factory import get_traffic_provider

cell_bp = Blueprint("cell_api", __name__)

_STATE = {"mysql": {}, "apps": {}}


def _claim_name(prefix: str, cell: str, env: str):
    return f"{prefix}-{cell}-{env}".replace("_", "-")


@cell_bp.get("/health")
def health():
    return jsonify({"status": "ok"})


@cell_bp.get("/")
def root():
    web_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "web"))
    return send_from_directory(web_dir, "index.html")


@cell_bp.post("/api/mysql")
def create_mysql():
    body = request.get_json(force=True)
    cell = body["cell"]
    env = body["environment"]
    tier = body["tier"]
    sticky_key = f"mysql:{cell}:{env}:{body.get('name', 'default')}"
    plan = schedule(cell, env, tier, body.get("drProfile"), sticky_key)
    policy = policy_store.load()
    dr_profile = body.get("drProfile") or policy["tiers"][tier]["defaults"]["drProfile"]

    primary_name = _claim_name("mysql-primary", cell, env)
    secondary_name = _claim_name("mysql-secondary", cell, env)
    record = {
        "claims": {
            "primary": {
                "apiVersion": "db.platform.example.org/v1alpha1",
                "kind": "CellMySQLClaim",
                "metadata": {"name": primary_name, "annotations": {"placement-reason": plan.reason}},
                "spec": {
                    "cell": cell, "environment": env, "tier": tier, "drProfile": dr_profile,
                    "size": body["size"], "storageGB": body["storageGB"], "ha": body.get("ha", False),
                    "provider": plan.primary["provider"], "region": plan.primary["region"],
                },
            },
            "secondary": {
                "apiVersion": "db.platform.example.org/v1alpha1",
                "kind": "CellMySQLClaim",
                "metadata": {"name": secondary_name, "annotations": {"placement-reason": plan.reason}},
                "spec": {
                    "cell": cell, "environment": env, "tier": tier, "drProfile": dr_profile,
                    "size": body["size"], "storageGB": body["storageGB"], "ha": body.get("ha", False),
                    "provider": plan.secondary["provider"], "region": plan.secondary["region"],
                },
            },
        },
        "replication": {
            "apiVersion": "dr.platform.example.org/v1alpha1",
            "kind": "ReplicationClaim",
            "metadata": {"name": _claim_name("repl", cell, env)},
            "spec": {
                "tool": "oci-goldengate",
                "sourceSecretRef": f"{primary_name}-conn",
                "targetSecretRef": f"{secondary_name}-conn",
                "expectedRPO": policy["drProfiles"][dr_profile]["rpoSeconds"],
            },
        },
        "placementDecision": {
            "primary": plan.primary,
            "secondary": plan.secondary,
            "reason": plan.reason,
            "sticky": True,
        },
    }
    _STATE["mysql"][f"{cell}:{env}"] = record
    register_replication(f"{cell}:{env}", policy["drProfiles"][dr_profile]["rpoSeconds"])
    return jsonify(record), 201


@cell_bp.post("/api/app")
def create_app():
    body = request.get_json(force=True)
    cell, env, tier = body["cell"], body["environment"], body["tier"]
    sticky_key = f"app:{cell}:{env}:{body.get('name', 'default')}"
    plan = schedule(cell, env, tier, body.get("drProfile"), sticky_key)
    policy = policy_store.load()
    dr_profile = body.get("drProfile") or policy["tiers"][tier]["defaults"]["drProfile"]
    compute_mode = policy["drProfiles"][dr_profile]["computeMode"]
    sec_replicas = 1 if compute_mode == "warm" else 0
    if compute_mode == "cold":
        sec_replicas = 0

    host = body["traffic"]["publicHost"]
    provider = get_traffic_provider()
    traffic = provider.ensure_record(
        host,
        [f"{cell}-{env}-primary.svc.cluster.local"],
        [f"{cell}-{env}-secondary.svc.cluster.local"],
        [{"name": "http-200", "path": "/health"}],
        {"mode": body.get("traffic", {}).get("mode", "active-passive")},
    )

    result = {
        "argoApplications": {
            "primary": {"name": _claim_name("app-primary", cell, env), "cluster": plan.primary["runtimeClusterRef"], "replicas": body.get("app", {}).get("replicasHint", 2)},
            "secondary": {"name": _claim_name("app-secondary", cell, env), "cluster": plan.secondary["runtimeClusterRef"], "replicas": sec_replicas, "mode": compute_mode},
        },
        "traffic": traffic,
        "placementDecision": {"primary": plan.primary, "secondary": plan.secondary, "reason": plan.reason},
    }
    _STATE["apps"][f"{cell}:{env}"] = result
    return jsonify(result), 201


@cell_bp.get("/api/status/<cell>/<env>")
def status(cell, env):
    key = f"{cell}:{env}"
    app = _STATE["apps"].get(key, {})
    mysql = _STATE["mysql"].get(key, {})
    traffic_provider = get_traffic_provider()
    host = None
    if app:
        host = app.get("traffic", {}).get("policy", {}).get("host")
    return jsonify({
        "key": key,
        "mysql": mysql,
        "app": app,
        "traffic": traffic_provider.status(host) if host else {},
        "failovers": [e for e in failover_events() if e["cell"] == cell and e["env"] == env],
        "timestamp": int(time.time()),
    })


@cell_bp.post("/api/failover")
def run_failover():
    body = request.get_json(force=True)
    cell, env = body["cell"], body["environment"]
    key = f"{cell}:{env}"
    app = _STATE["apps"].get(key)
    mysql = _STATE["mysql"].get(key)
    if not app or not mysql:
        return jsonify({"error": "app and mysql must exist before failover"}), 409
    tier = mysql["claims"]["primary"]["spec"]["tier"]
    dr_profile = mysql["claims"]["primary"]["spec"]["drProfile"]
    host = body.get("host") or "unknown-host"
    try:
        result = failover(cell, env, tier, dr_profile, host)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    app["argoApplications"]["secondary"]["replicas"] = max(app["argoApplications"]["secondary"].get("replicas", 0), 2)
    return jsonify({"status": "ok", "result": result, "app": app}), 200
