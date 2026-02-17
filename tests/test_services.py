"""Tests for the generic multi-product services handler."""

import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest

sys.path.insert(0, os.path.join(PROJECT_ROOT, "cmd", "controlplane"))
from main import create_app

from internal.scheduler.scheduler import (
    _provider_health, _provider_circuit_breakers,
    set_provider_health,
)
from internal.scheduler.experiments import (
    _experiments, _feature_flags, analytics,
)


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_all_state():
    """Reset all mutable state between tests."""
    _provider_health.clear()
    _provider_circuit_breakers.clear()
    _experiments.clear()
    _feature_flags.clear()
    analytics.reset()
    yield
    _provider_health.clear()
    _provider_circuit_breakers.clear()
    _experiments.clear()
    _feature_flags.clear()
    analytics.reset()


# ── Product Listing ──────────────────────────────────────────────────────────

def test_list_products(client):
    resp = client.get("/api/products")
    assert resp.status_code == 200
    data = resp.get_json()
    names = [p["name"] for p in data["products"]]
    assert "mysql" in names
    assert "webapp" in names


def test_list_products_shows_parameters(client):
    resp = client.get("/api/products")
    data = resp.get_json()
    webapp = [p for p in data["products"] if p["name"] == "webapp"][0]
    param_names = [p["name"] for p in webapp["parameters"]]
    assert "image" in param_names
    assert "cpu" in param_names
    assert "memory" in param_names
    assert "replicas" in param_names


# ── WebApp Creation ──────────────────────────────────────────────────────────

def _valid_webapp_body(**overrides):
    body = {
        "name": "frontend-app",
        "namespace": "team-web",
        "cell": "cell-us-east",
        "tier": "medium",
        "environment": "production",
        "image": "registry.example.com/frontend:v1.2.3",
    }
    body.update(overrides)
    return body


def test_create_webapp_success(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(),
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "created"
    assert data["product"] == "webapp"
    assert data["placement"]["provider"] in ("aws", "gcp", "oci")
    assert data["namespace"] == "team-web"
    assert data["name"] == "frontend-app"


def test_create_webapp_claim_structure(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(),
    )
    data = resp.get_json()
    claim = data["claim"]
    assert claim["apiVersion"] == "compute.platform.example.org/v1alpha1"
    assert claim["kind"] == "WebAppClaim"
    assert claim["metadata"]["labels"]["platform.example.org/product"] == "webapp"
    params = claim["spec"]["parameters"]
    assert params["image"] == "registry.example.com/frontend:v1.2.3"
    assert params["port"] == 8080  # default
    assert params["cpu"] == "250m"  # default
    assert params["memory"] == "512Mi"  # default
    assert params["replicas"] == 2  # default


def test_create_webapp_custom_params(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(
            cpu="1000m", memory="2Gi", replicas=5, port=3000,
        ),
    )
    assert resp.status_code == 201
    params = resp.get_json()["claim"]["spec"]["parameters"]
    assert params["cpu"] == "1000m"
    assert params["memory"] == "2Gi"
    assert params["replicas"] == 5
    assert params["port"] == 3000


def test_create_webapp_missing_image(client):
    body = _valid_webapp_body()
    del body["image"]
    resp = client.post("/api/services/webapp", json=body)
    assert resp.status_code == 400
    data = resp.get_json()
    assert any("image" in d for d in data["details"])


def test_create_webapp_invalid_cpu(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(cpu="999m"),
    )
    assert resp.status_code == 400


def test_create_webapp_invalid_replicas_too_high(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(replicas=100),
    )
    assert resp.status_code == 400


def test_create_webapp_invalid_replicas_too_low(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(replicas=0),
    )
    assert resp.status_code == 400


# ── MySQL via Generic Endpoint ───────────────────────────────────────────────

def _valid_mysql_body(**overrides):
    body = {
        "name": "orders-db",
        "namespace": "default",
        "cell": "cell-us-east",
        "tier": "medium",
        "environment": "production",
        "size": "large",
        "storageGB": 100,
    }
    body.update(overrides)
    return body


def test_create_mysql_via_services(client):
    resp = client.post(
        "/api/services/mysql",
        json=_valid_mysql_body(),
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["status"] == "created"
    assert data["product"] == "mysql"
    claim = data["claim"]
    assert claim["apiVersion"] == "db.platform.example.org/v1alpha1"
    assert claim["kind"] == "MySQLInstanceClaim"


def test_mysql_generic_claim_params(client):
    resp = client.post(
        "/api/services/mysql",
        json=_valid_mysql_body(),
    )
    data = resp.get_json()
    params = data["claim"]["spec"]["parameters"]
    assert params["size"] == "large"
    assert params["storageGB"] == 100


# ── Contract Enforcement ─────────────────────────────────────────────────────

def test_services_rejects_provider_field(client):
    body = _valid_webapp_body(provider="aws")
    resp = client.post("/api/services/webapp", json=body)
    assert resp.status_code == 400
    assert "contract violation" in resp.get_json()["error"].lower()


def test_services_rejects_region_field(client):
    body = _valid_webapp_body(region="us-east-1")
    resp = client.post("/api/services/webapp", json=body)
    assert resp.status_code == 400


def test_services_rejects_network_field(client):
    body = _valid_webapp_body(network={"vpcId": "vpc-123"})
    resp = client.post("/api/services/webapp", json=body)
    assert resp.status_code == 400


# ── Unknown Product ──────────────────────────────────────────────────────────

def test_unknown_product_returns_404(client):
    resp = client.post(
        "/api/services/redis",
        json={"name": "cache", "namespace": "default", "cell": "c1", "tier": "low", "environment": "dev"},
    )
    assert resp.status_code == 404
    data = resp.get_json()
    assert "redis" in data["error"]
    assert "available" in data


def test_unknown_product_status_returns_404(client):
    resp = client.get("/api/services/redis/default/my-cache")
    assert resp.status_code == 404


# ── Validation ───────────────────────────────────────────────────────────────

def test_services_missing_body(client):
    resp = client.post(
        "/api/services/webapp",
        content_type="application/json",
        data="not json",
    )
    assert resp.status_code == 400


def test_services_missing_common_fields(client):
    resp = client.post(
        "/api/services/webapp",
        json={"image": "myapp:latest"},
    )
    assert resp.status_code == 400
    details = resp.get_json()["details"]
    error_text = " ".join(details)
    assert "cell" in error_text
    assert "name" in error_text


# ── HA Enforcement ───────────────────────────────────────────────────────────

def test_webapp_ha_enforcement(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(ha=True, tier="critical"),
    )
    assert resp.status_code == 201
    data = resp.get_json()
    # HA enforcement should still produce a valid placement
    assert data["placement"]["provider"] in ("aws", "gcp", "oci")


# ── Tier Failover ────────────────────────────────────────────────────────────

def test_webapp_low_tier_gets_failover(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(tier="low"),
    )
    assert resp.status_code == 201
    data = resp.get_json()
    # Low tier should include a failover target in a different cloud
    assert "failover" in data
    if data["failover"]:
        assert data["failover"]["provider"] != data["placement"]["provider"]


def test_webapp_business_critical_tier_schedules(client):
    """business_critical tier schedules successfully (failover depends on gate survivors)."""
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(tier="business_critical", ha=True),
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["placement"]["provider"] in ("aws", "gcp", "oci")


# ── Provider Health Affects Generic Endpoint ─────────────────────────────────

def test_unhealthy_provider_excluded_from_webapp(client):
    set_provider_health("aws", False)
    set_provider_health("oci", False)
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(tier="critical"),
    )
    assert resp.status_code == 201
    # With AWS and OCI unhealthy, only GCP should be selected
    assert resp.get_json()["placement"]["provider"] == "gcp"


# ── Composition Selector ────────────────────────────────────────────────────

def test_webapp_composition_selector(client):
    resp = client.post(
        "/api/services/webapp",
        json=_valid_webapp_body(),
    )
    data = resp.get_json()
    selector = data["claim"]["spec"]["compositionSelector"]["matchLabels"]
    assert selector["compute.platform.example.org/class"] == "webapp"
    provider = data["placement"]["provider"]
    assert selector["compute.platform.example.org/provider"] == provider


def test_mysql_generic_composition_selector(client):
    resp = client.post(
        "/api/services/mysql",
        json=_valid_mysql_body(),
    )
    data = resp.get_json()
    selector = data["claim"]["spec"]["compositionSelector"]["matchLabels"]
    assert selector["db.platform.example.org/class"] == "mysql"
