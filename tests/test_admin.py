"""Tests for the Admin API endpoints."""

import os
import sys
import tempfile
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from internal.db import database
    database.init_db(path)
    database.seed_defaults()
    yield path
    os.unlink(path)


@pytest.fixture
def client():
    from flask import Flask
    from internal.handlers.mysql import mysql_bp
    from internal.handlers.services import services_bp
    from internal.handlers.admin import admin_bp
    import internal.products.catalog  # noqa: F401

    app = Flask(__name__)
    app.register_blueprint(mysql_bp)
    app.register_blueprint(services_bp)
    app.register_blueprint(admin_bp)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ── Config endpoints ─────────────────────────────────────────────────────────

def test_list_config(client):
    resp = client.get("/api/admin/config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "config" in data


def test_set_config(client):
    resp = client.put("/api/admin/config/test_key",
                      json={"value": "test_value"})
    assert resp.status_code == 200
    assert resp.get_json()["key"] == "test_key"

    resp2 = client.get("/api/admin/config")
    assert "test_key" in resp2.get_json()["config"]


def test_set_config_missing_value(client):
    resp = client.put("/api/admin/config/k", json={})
    assert resp.status_code == 400


def test_delete_config(client):
    client.put("/api/admin/config/del_me", json={"value": "x"})
    resp = client.delete("/api/admin/config/del_me")
    assert resp.status_code == 200

    resp2 = client.delete("/api/admin/config/nonexistent")
    assert resp2.status_code == 404


# ── Provider config endpoints ────────────────────────────────────────────────

def test_list_provider_configs(client):
    resp = client.get("/api/admin/providers")
    assert resp.status_code == 200
    providers = resp.get_json()["providers"]
    names = [p["name"] for p in providers]
    assert "aws" in names


def test_create_provider(client):
    resp = client.post("/api/admin/providers", json={
        "name": "azure",
        "display_name": "Microsoft Azure",
        "credentials_type": "workload_identity",
        "credentials_ref": "azure-creds",
        "regions": ["eastus", "westeurope"],
    })
    assert resp.status_code == 200
    assert resp.get_json()["provider"]["name"] == "azure"


def test_create_provider_missing_name(client):
    resp = client.post("/api/admin/providers", json={"display_name": "Test"})
    assert resp.status_code == 400


def test_delete_provider(client):
    client.post("/api/admin/providers", json={"name": "del-test"})
    resp = client.delete("/api/admin/providers/del-test")
    assert resp.status_code == 200

    resp2 = client.delete("/api/admin/providers/nonexistent")
    assert resp2.status_code == 404


# ── DR policy endpoints ──────────────────────────────────────────────────────

def test_list_dr_policies(client):
    resp = client.get("/api/admin/dr-policies")
    assert resp.status_code == 200
    policies = resp.get_json()["policies"]
    tiers = [p["tier"] for p in policies]
    assert "low" in tiers
    assert "business_critical" in tiers


def test_create_dr_policy(client):
    resp = client.post("/api/admin/dr-policies", json={
        "tier": "low",
        "strategy": "active_active",
        "rto_target": 10,
        "rpo_target": 1,
        "auto_failover": True,
    })
    assert resp.status_code == 200
    policy = resp.get_json()["policy"]
    assert policy["strategy"] == "active_active"
    assert policy["auto_failover"] is True


def test_create_dr_invalid_strategy(client):
    resp = client.post("/api/admin/dr-policies", json={
        "tier": "low",
        "strategy": "invalid_strategy",
    })
    assert resp.status_code == 400


def test_delete_dr_policy(client):
    resp = client.delete("/api/admin/dr-policies/low")
    assert resp.status_code == 200

    resp2 = client.delete("/api/admin/dr-policies/nonexistent")
    assert resp2.status_code == 404


# ── Saga endpoints ───────────────────────────────────────────────────────────

def test_list_sagas_empty(client):
    resp = client.get("/api/admin/sagas")
    assert resp.status_code == 200
    assert resp.get_json()["sagas"] == []


def test_saga_after_create(client):
    # Create a service — triggers a saga
    client.post("/api/services/mysql", json={
        "name": "saga-test",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "size": "large",
        "storageGB": 100,
    })
    resp = client.get("/api/admin/sagas")
    sagas = resp.get_json()["sagas"]
    assert len(sagas) >= 1
    assert sagas[0]["state"] == "COMPLETED"


def test_saga_detail(client):
    client.post("/api/services/mysql", json={
        "name": "detail-test",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "size": "large",
        "storageGB": 50,
    })
    sagas = client.get("/api/admin/sagas").get_json()["sagas"]
    saga_id = sagas[0]["id"]
    resp = client.get(f"/api/admin/sagas/{saga_id}")
    assert resp.status_code == 200
    assert resp.get_json()["saga"]["state"] == "COMPLETED"


def test_saga_not_found(client):
    resp = client.get("/api/admin/sagas/9999")
    assert resp.status_code == 404


# ── Placement history endpoints ──────────────────────────────────────────────

def test_placement_history(client):
    client.post("/api/services/mysql", json={
        "name": "hist-test",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "size": "large",
        "storageGB": 50,
    })
    resp = client.get("/api/admin/placements")
    placements = resp.get_json()["placements"]
    assert len(placements) >= 1
    assert placements[0]["product"] == "mysql"


def test_placement_detail(client):
    client.post("/api/services/mysql", json={
        "name": "det-test",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "size": "large",
        "storageGB": 50,
    })
    placements = client.get("/api/admin/placements").get_json()["placements"]
    pid = placements[0]["id"]
    resp = client.get(f"/api/admin/placements/{pid}")
    assert resp.status_code == 200


def test_placement_not_found(client):
    resp = client.get("/api/admin/placements/9999")
    assert resp.status_code == 404


# ── Multicloud deploy endpoint ───────────────────────────────────────────────

def test_multicloud_deploy(client):
    resp = client.post("/api/services/webapp/multicloud", json={
        "name": "mc-web",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "image": "nginx:latest",
        "target_providers": ["aws", "gcp"],
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "multicloud_deploy"
    assert len(data["deployments"]) == 2


def test_multicloud_missing_providers(client):
    resp = client.post("/api/services/webapp/multicloud", json={
        "name": "mc-fail",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "image": "nginx:latest",
    })
    assert resp.status_code == 400


def test_multicloud_unknown_product(client):
    resp = client.post("/api/services/ghost/multicloud", json={
        "name": "mc-fail",
        "target_providers": ["aws"],
    })
    assert resp.status_code == 404
