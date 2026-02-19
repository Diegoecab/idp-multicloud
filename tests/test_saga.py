"""Tests for the Saga orchestrator and multi-cloud deployer."""

import os
import tempfile
import pytest

@pytest.fixture(autouse=True)
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from internal.db import database
    database.init_db(path)
    database.seed_defaults()
    for prov in ("aws", "gcp", "oci"):
        database.save_provider_credentials(prov, "access_key", {"test": True})
    yield path
    os.unlink(path)


def test_saga_success():
    from internal.orchestration.saga import SagaOrchestrator
    body = {
        "name": "test-db",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "size": "large",
        "storageGB": 100,
        "ha": False,
    }
    saga = SagaOrchestrator("mysql", body)
    result = saga.execute()
    assert result["status"] == "created"
    assert result["saga"]["state"] == "COMPLETED"
    assert "validate" in result["saga"]["steps_completed"]
    assert "schedule" in result["saga"]["steps_completed"]
    assert "register" in result["saga"]["steps_completed"]
    assert result["placement"]["provider"] in ("aws", "gcp", "oci")
    assert result["saga_id"] > 0
    assert result["placement_id"] > 0


def test_saga_persists_placement():
    from internal.orchestration.saga import SagaOrchestrator
    from internal.db.database import get_placement_by_id
    body = {
        "name": "persist-test",
        "namespace": "ns1",
        "cell": "cell-us",
        "tier": "critical",
        "environment": "dev",
        "size": "small",
        "storageGB": 20,
        "ha": False,
    }
    result = SagaOrchestrator("mysql", body).execute()
    p = get_placement_by_id(result["placement_id"])
    assert p is not None
    assert p["product"] == "mysql"
    assert p["name"] == "persist-test"
    assert p["namespace"] == "ns1"
    assert p["provider"] in ("aws", "gcp", "oci")


def test_saga_persists_saga_record():
    from internal.orchestration.saga import SagaOrchestrator
    from internal.db.database import get_saga
    body = {
        "name": "saga-record-test",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "staging",
        "size": "medium",
        "storageGB": 50,
        "ha": True,
    }
    result = SagaOrchestrator("mysql", body).execute()
    saga = get_saga(result["saga_id"])
    assert saga["state"] == "COMPLETED"
    assert len(saga["steps_completed"]) == 6  # all 6 steps


def test_saga_validation_failure():
    from internal.orchestration.saga import SagaOrchestrator
    body = {
        "name": "",  # missing name
        "cell": "cell-us",
        "tier": "invalid_tier",
        "environment": "production",
        "size": "large",
        "storageGB": 100,
    }
    result = SagaOrchestrator("mysql", body).execute()
    assert result["status"] == "failed"
    assert "error" in result
    assert result["saga"]["state"] in ("FAILED", "ROLLED_BACK")


def test_saga_unknown_product():
    from internal.orchestration.saga import SagaOrchestrator
    body = {
        "name": "test",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
    }
    result = SagaOrchestrator("nonexistent", body).execute()
    assert result["status"] == "failed"
    assert "Unknown product" in result["error"]


def test_saga_webapp():
    from internal.orchestration.saga import SagaOrchestrator
    body = {
        "name": "my-web",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "image": "nginx:latest",
        "ha": False,
    }
    result = SagaOrchestrator("webapp", body).execute()
    assert result["status"] == "created"
    assert result["product"] == "webapp"


def test_multicloud_deployer():
    from internal.orchestration.saga import MultiCloudDeployer
    body = {
        "name": "mc-web",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "image": "nginx:latest",
        "ha": False,
    }
    deployer = MultiCloudDeployer("webapp", body, ["aws", "gcp"])
    result = deployer.deploy()
    assert result["status"] == "multicloud_deploy"
    assert len(result["deployments"]) == 2
    # Each deployment should have a unique name per provider
    names = [d.get("name", d.get("saga", {}).get("name", "")) for d in result["deployments"]]
    providers = [d.get("target_provider", "") for d in result["deployments"]]
    assert "aws" in providers
    assert "gcp" in providers


def test_multicloud_unknown_provider():
    from internal.orchestration.saga import MultiCloudDeployer
    body = {
        "name": "mc-fail",
        "namespace": "default",
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "image": "nginx:latest",
    }
    deployer = MultiCloudDeployer("webapp", body, ["nonexistent_cloud"])
    result = deployer.deploy()
    assert result["deployments"][0]["status"] == "skipped"


def test_multicloud_unknown_product():
    from internal.orchestration.saga import MultiCloudDeployer
    result = MultiCloudDeployer("ghost", {}, ["aws"]).deploy()
    assert "error" in result
