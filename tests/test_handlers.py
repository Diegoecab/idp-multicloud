"""Tests for the HTTP API handlers."""

import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest

# Import create_app from main — it needs PROJECT_ROOT on sys.path
sys.path.insert(0, os.path.join(PROJECT_ROOT, "cmd", "controlplane"))
from main import create_app

from internal.scheduler.scheduler import (
    _provider_health, _provider_circuit_breakers,
    set_provider_health,
)


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_health_state():
    """Reset provider health and circuit breakers between tests."""
    _provider_health.clear()
    _provider_circuit_breakers.clear()
    yield
    _provider_health.clear()
    _provider_circuit_breakers.clear()


# ── Health ────────────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# ── POST /api/mysql — Validation ─────────────────────────────────────────────

def test_create_mysql_missing_body(client):
    resp = client.post("/api/mysql", content_type="application/json")
    assert resp.status_code == 400


def test_create_mysql_invalid_json(client):
    resp = client.post("/api/mysql", data="not json", content_type="application/json")
    assert resp.status_code == 400


def test_create_mysql_missing_fields(client):
    resp = client.post("/api/mysql", json={})
    assert resp.status_code == 400
    data = resp.get_json()
    assert "details" in data


def test_create_mysql_invalid_tier(client):
    resp = client.post("/api/mysql", json={
        "name": "db1", "cell": "c1", "tier": "invalid",
        "environment": "dev", "size": "small", "storageGB": 50, "ha": False,
    })
    assert resp.status_code == 400
    assert "tier" in str(resp.get_json()["details"])


def test_create_mysql_invalid_environment(client):
    resp = client.post("/api/mysql", json={
        "name": "db1", "cell": "c1", "tier": "medium",
        "environment": "invalid", "size": "small", "storageGB": 50, "ha": False,
    })
    assert resp.status_code == 400


def test_create_mysql_invalid_size(client):
    resp = client.post("/api/mysql", json={
        "name": "db1", "cell": "c1", "tier": "medium",
        "environment": "dev", "size": "tiny", "storageGB": 50, "ha": False,
    })
    assert resp.status_code == 400


def test_create_mysql_storage_too_small(client):
    resp = client.post("/api/mysql", json={
        "name": "db1", "cell": "c1", "tier": "medium",
        "environment": "dev", "size": "small", "storageGB": 5, "ha": False,
    })
    assert resp.status_code == 400


# ── POST /api/mysql — Contract Enforcement ───────────────────────────────────

def test_create_mysql_rejects_provider_field(client):
    resp = client.post("/api/mysql", json={
        "name": "db1", "cell": "c1", "tier": "medium",
        "environment": "dev", "size": "small", "storageGB": 50, "ha": False,
        "provider": "aws",
    })
    assert resp.status_code == 400
    assert "contract violation" in resp.get_json()["error"].lower()


def test_create_mysql_rejects_region_field(client):
    resp = client.post("/api/mysql", json={
        "name": "db1", "cell": "c1", "tier": "medium",
        "environment": "dev", "size": "small", "storageGB": 50, "ha": False,
        "region": "us-east-1",
    })
    assert resp.status_code == 400


def test_create_mysql_rejects_network_field(client):
    resp = client.post("/api/mysql", json={
        "name": "db1", "cell": "c1", "tier": "medium",
        "environment": "dev", "size": "small", "storageGB": 50, "ha": False,
        "network": {"vpc": "x"},
    })
    assert resp.status_code == 400


# ── POST /api/mysql — Successful Creation ────────────────────────────────────

def test_create_mysql_success(client):
    resp = client.post("/api/mysql", json={
        "name": "mydb",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "size": "medium",
        "storageGB": 100,
        "ha": True,
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "created"
    assert data["sticky"] is False
    assert "placement" in data
    assert "reason" in data
    assert "claim" in data

    # Verify placement fields
    placement = data["placement"]
    assert placement["provider"] in ("aws", "gcp", "oci")
    assert placement["region"]
    assert placement["runtimeCluster"]
    assert placement["network"]

    # Verify reason has top-3
    reason = data["reason"]
    assert "top_3_candidates" in reason
    assert len(reason["top_3_candidates"]) >= 1

    # Verify HA enforcement is in reason
    assert "ha_enforced" in reason

    # Verify claim structure
    claim = data["claim"]
    assert claim["apiVersion"] == "db.platform.example.org/v1alpha1"
    assert claim["kind"] == "MySQLInstanceClaim"
    assert claim["metadata"]["name"] == "mydb"
    assert claim["spec"]["writeConnectionSecretToRef"]["name"] == "mydb-conn"


def test_create_mysql_with_namespace(client):
    resp = client.post("/api/mysql", json={
        "name": "mydb",
        "namespace": "team-beta",
        "cell": "cell-eu",
        "tier": "low",
        "environment": "staging",
        "size": "large",
        "storageGB": 200,
        "ha": True,
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["claim"]["metadata"]["namespace"] == "team-beta"


def test_create_mysql_all_tiers(client):
    """All tiers that can be scheduled should return 201."""
    for tier in ("low", "medium", "critical"):
        resp = client.post("/api/mysql", json={
            "name": f"db-{tier}",
            "cell": "cell-us",
            "tier": tier,
            "environment": "dev",
            "size": "small",
            "storageGB": 20,
            "ha": False,
        })
        assert resp.status_code == 201, f"Tier '{tier}' failed: {resp.get_json()}"


# ── POST /api/mysql — HA Enforcement ─────────────────────────────────────────

def test_create_mysql_ha_true_excludes_single_az(client):
    """When ha=True, candidates without multi_az should be rejected."""
    resp = client.post("/api/mysql", json={
        "name": "ha-db",
        "cell": "cell-us",
        "tier": "critical",  # critical only requires private_networking
        "environment": "dev",
        "size": "small",
        "storageGB": 20,
        "ha": True,
    })
    assert resp.status_code == 201
    data = resp.get_json()
    # OCI candidates lack multi_az, so they should not be selected when ha=True
    assert data["placement"]["provider"] in ("aws", "gcp")
    assert data["reason"]["ha_enforced"] is True


def test_create_mysql_ha_false_allows_oci(client):
    """When ha=False, OCI candidates (without multi_az) are available."""
    resp = client.post("/api/mysql", json={
        "name": "no-ha-db",
        "cell": "cell-us",
        "tier": "critical",
        "environment": "dev",
        "size": "small",
        "storageGB": 20,
        "ha": False,
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["reason"]["ha_enforced"] is False


# ── POST /api/mysql — Failover Included ──────────────────────────────────────

def test_create_mysql_low_tier_includes_failover(client):
    """Low tier response should include a failover in a different cloud."""
    resp = client.post("/api/mysql", json={
        "name": "lo-db",
        "cell": "cell-us",
        "tier": "low",
        "environment": "production",
        "size": "medium",
        "storageGB": 50,
        "ha": True,
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert "failover" in data
    assert data["failover"]["provider"] != data["placement"]["provider"]


# ── GET /api/status — Without K8s ────────────────────────────────────────────

def test_status_without_k8s(client):
    """Without a K8s cluster, status should return 503."""
    resp = client.get("/api/status/mysql/default/nonexistent")
    assert resp.status_code in (404, 503)


# ── Provider Health Endpoints ────────────────────────────────────────────────

def test_get_providers_health(client):
    resp = client.get("/api/providers/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "providers" in data
    assert "circuit_breakers" in data


def test_set_provider_unhealthy(client):
    resp = client.put("/api/providers/aws/health", json={"healthy": False})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["provider"] == "aws"
    assert data["healthy"] is False

    # Verify it shows up in health endpoint
    resp = client.get("/api/providers/health")
    assert resp.get_json()["providers"]["aws"] is False


def test_set_provider_healthy(client):
    set_provider_health("aws", False)
    resp = client.put("/api/providers/aws/health", json={"healthy": True})
    assert resp.status_code == 200
    assert resp.get_json()["healthy"] is True


def test_set_provider_health_missing_body(client):
    resp = client.put("/api/providers/aws/health", json={})
    assert resp.status_code == 400


def test_scheduling_skips_unhealthy_provider(client):
    """Mark a provider unhealthy, verify it's skipped in scheduling."""
    client.put("/api/providers/aws/health", json={"healthy": False})
    resp = client.post("/api/mysql", json={
        "name": "skip-aws-db",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "dev",
        "size": "small",
        "storageGB": 20,
        "ha": False,
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["placement"]["provider"] != "aws"
    assert "unhealthy_skipped" in data["reason"]


# ── Root ──────────────────────────────────────────────────────────────────────

def test_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["service"] == "idp-multicloud-controlplane"
    assert data["status"] == "running"
