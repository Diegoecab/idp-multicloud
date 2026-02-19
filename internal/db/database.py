"""SQLite persistent state store for the IDP Multicloud Control Plane.

Stores:
  - Platform configuration (cloud providers, saga settings, DR policies)
  - Placement history (every scheduling decision)
  - Experiments and feature flags
  - Provider health status
  - Saga execution state (resource lifecycle)

The database file defaults to 'idp.db' in the working directory.
Set IDP_DB_PATH env var to override.
"""

import json
import os
import sqlite3
import threading
import time
from typing import Optional

_DB_PATH = os.environ.get("IDP_DB_PATH", "idp.db")
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db(db_path: Optional[str] = None):
    """Initialize the database schema. Safe to call multiple times."""
    if db_path:
        global _DB_PATH
        _DB_PATH = db_path
        # Reset thread-local connection
        if hasattr(_local, "conn") and _local.conn:
            _local.conn.close()
            _local.conn = None

    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS config (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_config (
            name        TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            credentials_type TEXT NOT NULL DEFAULT 'secret',
            credentials_ref TEXT NOT NULL DEFAULT '',
            regions     TEXT NOT NULL DEFAULT '[]',
            settings    TEXT NOT NULL DEFAULT '{}',
            updated_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS placements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            product     TEXT NOT NULL,
            name        TEXT NOT NULL,
            namespace   TEXT NOT NULL DEFAULT 'default',
            cell        TEXT NOT NULL,
            tier        TEXT NOT NULL,
            environment TEXT NOT NULL,
            provider    TEXT NOT NULL,
            region      TEXT NOT NULL,
            cluster     TEXT NOT NULL,
            ha          INTEGER NOT NULL DEFAULT 0,
            total_score REAL NOT NULL DEFAULT 0,
            reason      TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'PROVISIONING',
            failover    TEXT,
            experiment  TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS experiments (
            id          TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            variant_weights TEXT NOT NULL,
            traffic_pct REAL NOT NULL,
            tier        TEXT NOT NULL DEFAULT '*',
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS feature_flags (
            name    TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_health (
            provider TEXT PRIMARY KEY,
            healthy  INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS saga_executions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            placement_id INTEGER,
            product     TEXT NOT NULL,
            name        TEXT NOT NULL,
            namespace   TEXT NOT NULL,
            state       TEXT NOT NULL DEFAULT 'PENDING',
            current_step TEXT NOT NULL DEFAULT 'validate',
            steps_completed TEXT NOT NULL DEFAULT '[]',
            error       TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL,
            FOREIGN KEY (placement_id) REFERENCES placements(id)
        );

        CREATE TABLE IF NOT EXISTS dr_policies (
            tier        TEXT PRIMARY KEY,
            strategy    TEXT NOT NULL DEFAULT 'active_passive',
            failover_providers TEXT NOT NULL DEFAULT '[]',
            auto_failover INTEGER NOT NULL DEFAULT 0,
            rto_target  INTEGER NOT NULL DEFAULT 60,
            rpo_target  INTEGER NOT NULL DEFAULT 5,
            settings    TEXT NOT NULL DEFAULT '{}',
            updated_at  REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_placements_name ON placements(namespace, name);
        CREATE INDEX IF NOT EXISTS idx_placements_product ON placements(product);
        CREATE INDEX IF NOT EXISTS idx_placements_status ON placements(status);
        CREATE INDEX IF NOT EXISTS idx_saga_state ON saga_executions(state);
    """)
    conn.commit()


# ── Config CRUD ──────────────────────────────────────────────────────────────

def set_config(key: str, value: str):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, time.time()),
    )
    conn.commit()


def get_config(key: str, default: str = "") -> str:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_all_config() -> dict:
    conn = _get_conn()
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def delete_config(key: str) -> bool:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM config WHERE key = ?", (key,))
    conn.commit()
    return cursor.rowcount > 0


# ── Provider Config CRUD ─────────────────────────────────────────────────────

def save_provider_config(name: str, display_name: str, enabled: bool = True,
                         credentials_type: str = "secret", credentials_ref: str = "",
                         regions: list = None, settings: dict = None):
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO provider_config
           (name, display_name, enabled, credentials_type, credentials_ref,
            regions, settings, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, display_name, int(enabled), credentials_type, credentials_ref,
         json.dumps(regions or []), json.dumps(settings or {}), time.time()),
    )
    conn.commit()


def get_provider_configs() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM provider_config ORDER BY name").fetchall()
    return [
        {
            "name": r["name"],
            "display_name": r["display_name"],
            "enabled": bool(r["enabled"]),
            "credentials_type": r["credentials_type"],
            "credentials_ref": r["credentials_ref"],
            "regions": json.loads(r["regions"]),
            "settings": json.loads(r["settings"]),
        }
        for r in rows
    ]


def get_provider_config_by_name(name: str) -> Optional[dict]:
    conn = _get_conn()
    r = conn.execute("SELECT * FROM provider_config WHERE name = ?", (name,)).fetchone()
    if not r:
        return None
    return {
        "name": r["name"],
        "display_name": r["display_name"],
        "enabled": bool(r["enabled"]),
        "credentials_type": r["credentials_type"],
        "credentials_ref": r["credentials_ref"],
        "regions": json.loads(r["regions"]),
        "settings": json.loads(r["settings"]),
    }


def delete_provider_config(name: str) -> bool:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM provider_config WHERE name = ?", (name,))
    conn.commit()
    return cursor.rowcount > 0


# ── Placement History ────────────────────────────────────────────────────────

def record_placement(product: str, name: str, namespace: str, cell: str,
                     tier: str, environment: str, provider: str, region: str,
                     cluster: str, ha: bool, total_score: float, reason: dict,
                     status: str = "PROVISIONING", failover: dict = None,
                     experiment: dict = None) -> int:
    conn = _get_conn()
    now = time.time()
    cursor = conn.execute(
        """INSERT INTO placements
           (product, name, namespace, cell, tier, environment, provider, region,
            cluster, ha, total_score, reason, status, failover, experiment,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (product, name, namespace, cell, tier, environment, provider, region,
         cluster, int(ha), total_score, json.dumps(reason),
         status, json.dumps(failover) if failover else None,
         json.dumps(experiment) if experiment else None, now, now),
    )
    conn.commit()
    return cursor.lastrowid


def update_placement_status(placement_id: int, status: str, error: str = None):
    conn = _get_conn()
    conn.execute(
        "UPDATE placements SET status = ?, updated_at = ? WHERE id = ?",
        (status, time.time(), placement_id),
    )
    conn.commit()


def get_placement(namespace: str, name: str) -> Optional[dict]:
    conn = _get_conn()
    r = conn.execute(
        "SELECT * FROM placements WHERE namespace = ? AND name = ? ORDER BY created_at DESC LIMIT 1",
        (namespace, name),
    ).fetchone()
    if not r:
        return None
    return _row_to_placement(r)


def get_placement_by_id(placement_id: int) -> Optional[dict]:
    conn = _get_conn()
    r = conn.execute("SELECT * FROM placements WHERE id = ?", (placement_id,)).fetchone()
    if not r:
        return None
    return _row_to_placement(r)


def list_placements(limit: int = 50, product: str = None, status: str = None) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM placements WHERE 1=1"
    params = []
    if product:
        query += " AND product = ?"
        params.append(product)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_placement(r) for r in rows]


def _row_to_placement(r) -> dict:
    return {
        "id": r["id"],
        "product": r["product"],
        "name": r["name"],
        "namespace": r["namespace"],
        "cell": r["cell"],
        "tier": r["tier"],
        "environment": r["environment"],
        "provider": r["provider"],
        "region": r["region"],
        "cluster": r["cluster"],
        "ha": bool(r["ha"]),
        "total_score": r["total_score"],
        "reason": json.loads(r["reason"]),
        "status": r["status"],
        "failover": json.loads(r["failover"]) if r["failover"] else None,
        "experiment": json.loads(r["experiment"]) if r["experiment"] else None,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


# ── Experiments (persisted) ──────────────────────────────────────────────────

def save_experiment(exp_id: str, description: str, variant_weights: dict,
                    traffic_pct: float, tier: str = "*", enabled: bool = True):
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO experiments
           (id, description, variant_weights, traffic_pct, tier, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (exp_id, description, json.dumps(variant_weights), traffic_pct,
         tier, int(enabled), time.time()),
    )
    conn.commit()


def load_experiments() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM experiments").fetchall()
    return [
        {
            "id": r["id"],
            "description": r["description"],
            "variant_weights": json.loads(r["variant_weights"]),
            "traffic_pct": r["traffic_pct"],
            "tier": r["tier"],
            "enabled": bool(r["enabled"]),
        }
        for r in rows
    ]


def delete_experiment_db(exp_id: str) -> bool:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
    conn.commit()
    return cursor.rowcount > 0


# ── Feature Flags (persisted) ────────────────────────────────────────────────

def save_feature_flag(name: str, enabled: bool):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO feature_flags (name, enabled, updated_at) VALUES (?, ?, ?)",
        (name, int(enabled), time.time()),
    )
    conn.commit()


def load_feature_flags() -> dict[str, bool]:
    conn = _get_conn()
    rows = conn.execute("SELECT name, enabled FROM feature_flags").fetchall()
    return {r["name"]: bool(r["enabled"]) for r in rows}


def delete_feature_flag_db(name: str) -> bool:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM feature_flags WHERE name = ?", (name,))
    conn.commit()
    return cursor.rowcount > 0


# ── Provider Health (persisted) ──────────────────────────────────────────────

def save_provider_health(provider: str, healthy: bool):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO provider_health (provider, healthy, updated_at) VALUES (?, ?, ?)",
        (provider, int(healthy), time.time()),
    )
    conn.commit()


def load_provider_health() -> dict[str, bool]:
    conn = _get_conn()
    rows = conn.execute("SELECT provider, healthy FROM provider_health").fetchall()
    return {r["provider"]: bool(r["healthy"]) for r in rows}


# ── Saga Executions ──────────────────────────────────────────────────────────

SAGA_STEPS = ["validate", "schedule", "apply_claim", "wait_ready", "register", "notify"]
SAGA_STATES = ["PENDING", "RUNNING", "COMPLETED", "FAILED", "COMPENSATING", "ROLLED_BACK"]

def create_saga(product: str, name: str, namespace: str,
                placement_id: int = None) -> int:
    conn = _get_conn()
    now = time.time()
    cursor = conn.execute(
        """INSERT INTO saga_executions
           (placement_id, product, name, namespace, state, current_step,
            steps_completed, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'PENDING', 'validate', '[]', ?, ?)""",
        (placement_id, product, name, namespace, now, now),
    )
    conn.commit()
    return cursor.lastrowid


def update_saga(saga_id: int, state: str = None, current_step: str = None,
                steps_completed: list = None, error: str = None,
                placement_id: int = None):
    conn = _get_conn()
    updates = ["updated_at = ?"]
    params = [time.time()]
    if state:
        updates.append("state = ?")
        params.append(state)
    if current_step:
        updates.append("current_step = ?")
        params.append(current_step)
    if steps_completed is not None:
        updates.append("steps_completed = ?")
        params.append(json.dumps(steps_completed))
    if error is not None:
        updates.append("error = ?")
        params.append(error)
    if placement_id is not None:
        updates.append("placement_id = ?")
        params.append(placement_id)
    params.append(saga_id)
    conn.execute(f"UPDATE saga_executions SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def get_saga(saga_id: int) -> Optional[dict]:
    conn = _get_conn()
    r = conn.execute("SELECT * FROM saga_executions WHERE id = ?", (saga_id,)).fetchone()
    if not r:
        return None
    return _row_to_saga(r)


def get_saga_by_resource(namespace: str, name: str) -> Optional[dict]:
    conn = _get_conn()
    r = conn.execute(
        "SELECT * FROM saga_executions WHERE namespace = ? AND name = ? ORDER BY created_at DESC LIMIT 1",
        (namespace, name),
    ).fetchone()
    if not r:
        return None
    return _row_to_saga(r)


def list_sagas(limit: int = 50, state: str = None) -> list[dict]:
    conn = _get_conn()
    query = "SELECT * FROM saga_executions WHERE 1=1"
    params = []
    if state:
        query += " AND state = ?"
        params.append(state)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_row_to_saga(r) for r in rows]


def _row_to_saga(r) -> dict:
    return {
        "id": r["id"],
        "placement_id": r["placement_id"],
        "product": r["product"],
        "name": r["name"],
        "namespace": r["namespace"],
        "state": r["state"],
        "current_step": r["current_step"],
        "steps_completed": json.loads(r["steps_completed"]),
        "error": r["error"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


# ── DR Policies ──────────────────────────────────────────────────────────────

def save_dr_policy(tier: str, strategy: str = "active_passive",
                   failover_providers: list = None, auto_failover: bool = False,
                   rto_target: int = 60, rpo_target: int = 5, settings: dict = None):
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO dr_policies
           (tier, strategy, failover_providers, auto_failover, rto_target,
            rpo_target, settings, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (tier, strategy, json.dumps(failover_providers or []),
         int(auto_failover), rto_target, rpo_target,
         json.dumps(settings or {}), time.time()),
    )
    conn.commit()


def get_dr_policies() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM dr_policies ORDER BY tier").fetchall()
    return [
        {
            "tier": r["tier"],
            "strategy": r["strategy"],
            "failover_providers": json.loads(r["failover_providers"]),
            "auto_failover": bool(r["auto_failover"]),
            "rto_target": r["rto_target"],
            "rpo_target": r["rpo_target"],
            "settings": json.loads(r["settings"]),
        }
        for r in rows
    ]


def get_dr_policy(tier: str) -> Optional[dict]:
    conn = _get_conn()
    r = conn.execute("SELECT * FROM dr_policies WHERE tier = ?", (tier,)).fetchone()
    if not r:
        return None
    return {
        "tier": r["tier"],
        "strategy": r["strategy"],
        "failover_providers": json.loads(r["failover_providers"]),
        "auto_failover": bool(r["auto_failover"]),
        "rto_target": r["rto_target"],
        "rpo_target": r["rpo_target"],
        "settings": json.loads(r["settings"]),
    }


def delete_dr_policy(tier: str) -> bool:
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM dr_policies WHERE tier = ?", (tier,))
    conn.commit()
    return cursor.rowcount > 0


# ── Seed defaults ────────────────────────────────────────────────────────────

def seed_defaults():
    """Seed default provider configs and DR policies if not present."""
    conn = _get_conn()

    # Default provider configs
    for name, display_name in [("aws", "Amazon Web Services"), ("gcp", "Google Cloud Platform"), ("oci", "Oracle Cloud Infrastructure")]:
        existing = conn.execute("SELECT 1 FROM provider_config WHERE name = ?", (name,)).fetchone()
        if not existing:
            save_provider_config(
                name=name,
                display_name=display_name,
                enabled=True,
                credentials_type="secret",
                credentials_ref=f"{name}-credentials",
            )

    # Default DR policies
    for tier, strategy, auto_fo, rto, rpo in [
        ("low", "active_passive", False, 30, 5),
        ("medium", "backup_restore", False, 120, 15),
        ("critical", "backup_restore", False, 480, 60),
        ("business_critical", "active_active", True, 15, 1),
    ]:
        existing = conn.execute("SELECT 1 FROM dr_policies WHERE tier = ?", (tier,)).fetchone()
        if not existing:
            save_dr_policy(tier=tier, strategy=strategy, auto_failover=auto_fo,
                           rto_target=rto, rpo_target=rpo)

    # Default saga config
    if not get_config("saga_enabled"):
        set_config("saga_enabled", "true")
    if not get_config("saga_timeout_seconds"):
        set_config("saga_timeout_seconds", "300")
    if not get_config("multicloud_deploy_enabled"):
        set_config("multicloud_deploy_enabled", "true")
