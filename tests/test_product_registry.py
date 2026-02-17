"""Tests for the product registry module."""

import pytest

from internal.products.registry import (
    ProductDefinition, ParameterSpec, register_product,
    get_product, list_products, validate_product_params, build_product_claim,
    _products,
)
from internal.models.types import PlacementDecision


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sample_placement():
    return PlacementDecision(
        provider="aws",
        region="us-east-1",
        runtime_cluster="eks-prod-use1",
        network={"vpcId": "vpc-abc123", "subnetIds": ["subnet-1"]},
        reason={"selected": {"provider": "aws", "region": "us-east-1"}},
    )


def _test_product(name="testprod", **overrides):
    defaults = dict(
        name=name,
        display_name="Test Product",
        description="A test product",
        api_version="test.example.org/v1alpha1",
        kind="TestClaim",
        composition_class="test",
        composition_group="test.example.org",
        parameters=[
            ParameterSpec(name="size", param_type="choice", choices=("small", "large")),
            ParameterSpec(name="count", param_type="int", min_value=1, max_value=10),
            ParameterSpec(name="enabled", param_type="bool", required=False, default=True),
            ParameterSpec(name="label", param_type="string", required=False, default="default"),
        ],
        connection_secret_suffix="-creds",
    )
    defaults.update(overrides)
    return ProductDefinition(**defaults)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Save and restore the product registry between tests."""
    saved = dict(_products)
    yield
    _products.clear()
    _products.update(saved)


# ── Registration ─────────────────────────────────────────────────────────────

def test_register_and_get_product():
    prod = _test_product("mytest")
    register_product(prod)
    assert get_product("mytest") is prod


def test_get_unknown_product():
    assert get_product("nonexistent") is None


def test_list_products_includes_registered():
    prod = _test_product("listed")
    register_product(prod)
    names = [p["name"] for p in list_products()]
    assert "listed" in names


def test_list_products_shows_parameters():
    prod = _test_product("paramcheck")
    register_product(prod)
    items = [p for p in list_products() if p["name"] == "paramcheck"]
    assert len(items) == 1
    param_names = [p["name"] for p in items[0]["parameters"]]
    assert "size" in param_names
    assert "count" in param_names


# ── Parameter Validation ─────────────────────────────────────────────────────

def test_validate_required_field_missing():
    prod = _test_product()
    errors = validate_product_params(prod, {})  # missing 'size' and 'count'
    assert any("size" in e for e in errors)
    assert any("count" in e for e in errors)


def test_validate_int_below_min():
    prod = _test_product()
    errors = validate_product_params(prod, {"size": "small", "count": 0})
    assert any("count" in e and ">=" in e for e in errors)


def test_validate_int_above_max():
    prod = _test_product()
    errors = validate_product_params(prod, {"size": "small", "count": 99})
    assert any("count" in e and "<=" in e for e in errors)


def test_validate_int_wrong_type():
    prod = _test_product()
    errors = validate_product_params(prod, {"size": "small", "count": "five"})
    assert any("count" in e and "integer" in e for e in errors)


def test_validate_choice_invalid():
    prod = _test_product()
    errors = validate_product_params(prod, {"size": "huge", "count": 5})
    assert any("size" in e for e in errors)


def test_validate_bool_wrong_type():
    prod = _test_product()
    errors = validate_product_params(prod, {"size": "small", "count": 5, "enabled": "yes"})
    assert any("enabled" in e and "boolean" in e for e in errors)


def test_validate_all_valid():
    prod = _test_product()
    errors = validate_product_params(prod, {"size": "small", "count": 5})
    assert errors == []


def test_validate_optional_uses_default():
    prod = _test_product()
    # 'enabled' and 'label' are optional with defaults — should pass without them
    errors = validate_product_params(prod, {"size": "large", "count": 1})
    assert errors == []


# ── Claim Building ───────────────────────────────────────────────────────────

def test_build_claim_structure():
    prod = _test_product()
    body = {
        "name": "my-svc",
        "namespace": "team-a",
        "cell": "cell-us-east",
        "tier": "medium",
        "environment": "production",
        "size": "small",
        "count": 3,
    }
    claim = build_product_claim(prod, body, _sample_placement())

    assert claim["apiVersion"] == "test.example.org/v1alpha1"
    assert claim["kind"] == "TestClaim"
    assert claim["metadata"]["name"] == "my-svc"
    assert claim["metadata"]["namespace"] == "team-a"
    assert claim["metadata"]["labels"]["platform.example.org/product"] == "testprod"
    assert claim["spec"]["compositionSelector"]["matchLabels"]["test.example.org/class"] == "test"
    assert claim["spec"]["writeConnectionSecretToRef"]["name"] == "my-svc-creds"


def test_build_claim_uses_param_builder():
    def custom_builder(body):
        return {"flavor": body.get("size", "medium") + "-custom"}

    prod = _test_product(param_builder=custom_builder)
    body = {
        "name": "built",
        "namespace": "default",
        "cell": "cell-1",
        "tier": "low",
        "environment": "dev",
        "size": "large",
    }
    claim = build_product_claim(prod, body, _sample_placement())
    assert claim["spec"]["parameters"]["flavor"] == "large-custom"


def test_build_claim_includes_placement():
    prod = _test_product()
    body = {
        "name": "placed",
        "namespace": "default",
        "cell": "cell-1",
        "tier": "low",
        "environment": "dev",
        "size": "small",
        "count": 1,
    }
    placement = _sample_placement()
    claim = build_product_claim(prod, body, placement)
    params = claim["spec"]["parameters"]
    assert params["provider"] == "aws"
    assert params["region"] == "us-east-1"
    assert params["network"]["vpcId"] == "vpc-abc123"


def test_build_claim_placement_reason_annotation():
    prod = _test_product()
    body = {
        "name": "ann",
        "namespace": "default",
        "cell": "c1",
        "tier": "low",
        "environment": "dev",
        "size": "small",
        "count": 1,
    }
    claim = build_product_claim(prod, body, _sample_placement())
    annotations = claim["metadata"]["annotations"]
    assert "platform.example.org/placement-reason" in annotations
