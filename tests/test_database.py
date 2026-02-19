"""Tests for the SQLite persistent state store."""

import os
import tempfile
import pytest

# Use a temp DB for each test
@pytest.fixture(autouse=True)
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from internal.db import database
    database.init_db(path)
    yield path
    os.unlink(path)


def test_init_creates_tables():
    from internal.db.database import _get_conn
    conn = _get_conn()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    assert "config" in tables
    assert "provider_config" in tables
    assert "placements" in tables
    assert "experiments" in tables
    assert "feature_flags" in tables
    assert "saga_executions" in tables
    assert "dr_policies" in tables


def test_config_crud():
    from internal.db.database import set_config, get_config, get_all_config, delete_config
    set_config("saga_enabled", "true")
    assert get_config("saga_enabled") == "true"
    assert get_config("nonexistent", "fallback") == "fallback"
    set_config("key2", "val2")
    all_cfg = get_all_config()
    assert "saga_enabled" in all_cfg
    assert "key2" in all_cfg
    assert delete_config("key2")
    assert not delete_config("key2")


def test_provider_config_crud():
    from internal.db.database import (
        save_provider_config, get_provider_configs,
        get_provider_config_by_name, delete_provider_config,
    )
    save_provider_config("aws", "Amazon Web Services", True, "secret", "aws-creds",
                         ["us-east-1", "eu-west-1"])
    providers = get_provider_configs()
    assert len(providers) >= 1
    aws = get_provider_config_by_name("aws")
    assert aws is not None
    assert aws["display_name"] == "Amazon Web Services"
    assert aws["regions"] == ["us-east-1", "eu-west-1"]
    assert aws["enabled"] is True

    # Update
    save_provider_config("aws", "AWS", False)
    aws2 = get_provider_config_by_name("aws")
    assert aws2["display_name"] == "AWS"
    assert aws2["enabled"] is False

    assert delete_provider_config("aws")
    assert get_provider_config_by_name("aws") is None


def test_placement_crud():
    from internal.db.database import (
        record_placement, get_placement, get_placement_by_id,
        list_placements, update_placement_status,
    )
    pid = record_placement(
        product="mysql", name="orders-db", namespace="default",
        cell="cell-us", tier="medium", environment="production",
        provider="aws", region="us-east-1", cluster="aws-use1-prod-01",
        ha=True, total_score=0.85, reason={"test": True},
        status="PROVISIONING",
    )
    assert pid > 0

    p = get_placement_by_id(pid)
    assert p["product"] == "mysql"
    assert p["provider"] == "aws"
    assert p["ha"] is True
    assert p["status"] == "PROVISIONING"
    assert p["reason"]["test"] is True

    update_placement_status(pid, "READY")
    p2 = get_placement_by_id(pid)
    assert p2["status"] == "READY"

    p3 = get_placement("default", "orders-db")
    assert p3["id"] == pid

    records = list_placements(limit=10)
    assert len(records) == 1

    records_by_product = list_placements(product="mysql")
    assert len(records_by_product) == 1

    records_by_status = list_placements(status="READY")
    assert len(records_by_status) == 1


def test_experiment_persistence():
    from internal.db.database import save_experiment, load_experiments, delete_experiment_db
    save_experiment("exp-1", "Test experiment",
                    {"cost": 0.5, "latency": 0.2, "dr": 0.2, "maturity": 0.1},
                    0.5, "medium")
    exps = load_experiments()
    assert len(exps) == 1
    assert exps[0]["id"] == "exp-1"
    assert exps[0]["variant_weights"]["cost"] == 0.5

    assert delete_experiment_db("exp-1")
    assert len(load_experiments()) == 0


def test_feature_flag_persistence():
    from internal.db.database import save_feature_flag, load_feature_flags, delete_feature_flag_db
    save_feature_flag("prefer_cost", True)
    save_feature_flag("new_algo", False)
    flags = load_feature_flags()
    assert flags["prefer_cost"] is True
    assert flags["new_algo"] is False

    delete_feature_flag_db("new_algo")
    flags2 = load_feature_flags()
    assert "new_algo" not in flags2


def test_provider_health_persistence():
    from internal.db.database import save_provider_health, load_provider_health
    save_provider_health("aws", True)
    save_provider_health("gcp", False)
    h = load_provider_health()
    assert h["aws"] is True
    assert h["gcp"] is False


def test_saga_crud():
    from internal.db.database import (
        create_saga, update_saga, get_saga, get_saga_by_resource, list_sagas,
    )
    sid = create_saga("mysql", "orders-db", "default")
    assert sid > 0

    saga = get_saga(sid)
    assert saga["state"] == "PENDING"
    assert saga["current_step"] == "validate"

    update_saga(sid, state="RUNNING", current_step="schedule",
                steps_completed=["validate"])
    saga2 = get_saga(sid)
    assert saga2["state"] == "RUNNING"
    assert saga2["steps_completed"] == ["validate"]

    update_saga(sid, state="FAILED", error="No candidates")
    saga3 = get_saga(sid)
    assert saga3["state"] == "FAILED"
    assert saga3["error"] == "No candidates"

    found = get_saga_by_resource("default", "orders-db")
    assert found["id"] == sid

    sagas = list_sagas(state="FAILED")
    assert len(sagas) == 1


def test_dr_policy_crud():
    from internal.db.database import save_dr_policy, get_dr_policies, get_dr_policy, delete_dr_policy
    save_dr_policy("business_critical", "active_active", ["aws", "gcp"],
                   True, 15, 1)
    policies = get_dr_policies()
    assert len(policies) >= 1

    bc = get_dr_policy("business_critical")
    assert bc["strategy"] == "active_active"
    assert bc["auto_failover"] is True
    assert bc["failover_providers"] == ["aws", "gcp"]

    assert delete_dr_policy("business_critical")
    assert get_dr_policy("business_critical") is None


def test_seed_defaults():
    from internal.db.database import seed_defaults, get_provider_configs, get_dr_policies, get_config
    seed_defaults()
    providers = get_provider_configs()
    names = [p["name"] for p in providers]
    assert "aws" in names
    assert "gcp" in names
    assert "oci" in names

    policies = get_dr_policies()
    tiers = [p["tier"] for p in policies]
    assert "low" in tiers
    assert "business_critical" in tiers

    assert get_config("saga_enabled") == "true"
    assert get_config("multicloud_deploy_enabled") == "true"
