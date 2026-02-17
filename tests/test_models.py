"""Tests for data model validation."""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from internal.models.types import MySQLRequest


def _valid_request(**overrides) -> MySQLRequest:
    defaults = {
        "cell": "cell-us",
        "tier": "medium",
        "environment": "production",
        "size": "medium",
        "storage_gb": 50,
        "ha": True,
        "namespace": "default",
        "name": "test-db",
    }
    defaults.update(overrides)
    return MySQLRequest(**defaults)


def test_valid_request_has_no_errors():
    req = _valid_request()
    assert req.validate() == []


def test_missing_cell():
    req = _valid_request(cell="")
    errors = req.validate()
    assert any("cell" in e for e in errors)


def test_missing_name():
    req = _valid_request(name="")
    errors = req.validate()
    assert any("name" in e for e in errors)


def test_invalid_tier():
    req = _valid_request(tier="gold")
    errors = req.validate()
    assert any("tier" in e for e in errors)


def test_invalid_environment():
    req = _valid_request(environment="qa")
    errors = req.validate()
    assert any("environment" in e for e in errors)


def test_invalid_size():
    req = _valid_request(size="micro")
    errors = req.validate()
    assert any("size" in e for e in errors)


def test_storage_too_small():
    req = _valid_request(storage_gb=5)
    errors = req.validate()
    assert any("storageGB" in e for e in errors)


def test_storage_too_large():
    req = _valid_request(storage_gb=999999)
    errors = req.validate()
    assert any("storageGB" in e for e in errors)


def test_all_valid_tiers():
    for tier in ("low", "medium", "critical", "business_critical"):
        req = _valid_request(tier=tier)
        assert req.validate() == [], f"Tier '{tier}' should be valid"


def test_all_valid_environments():
    for env in ("dev", "staging", "production"):
        req = _valid_request(environment=env)
        assert req.validate() == [], f"Environment '{env}' should be valid"


def test_all_valid_sizes():
    for size in ("small", "medium", "large", "xlarge"):
        req = _valid_request(size=size)
        assert req.validate() == [], f"Size '{size}' should be valid"


def test_storage_boundary_min():
    req = _valid_request(storage_gb=10)
    assert req.validate() == []


def test_storage_boundary_max():
    req = _valid_request(storage_gb=65536)
    assert req.validate() == []
