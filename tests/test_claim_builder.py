"""Tests for the Crossplane Claim builder."""

import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from internal.models.types import MySQLRequest, PlacementDecision
from internal.k8s.claim_builder import build_claim, API_VERSION, KIND


def _sample_request() -> MySQLRequest:
    return MySQLRequest(
        cell="cell-us-east",
        tier="medium",
        environment="production",
        size="large",
        storage_gb=100,
        ha=True,
        namespace="team-alpha",
        name="orders-db",
    )


def _sample_placement() -> PlacementDecision:
    return PlacementDecision(
        provider="aws",
        region="us-east-1",
        runtime_cluster="aws-use1-prod-01",
        network={"vpc_id": "vpc-123", "subnet_group": "db-private"},
        reason={
            "tier": "medium",
            "selected": {"provider": "aws", "region": "us-east-1"},
            "top_3_candidates": [],
        },
    )


def test_build_claim_apiversion_and_kind():
    claim = build_claim(_sample_request(), _sample_placement())
    assert claim["apiVersion"] == API_VERSION
    assert claim["kind"] == KIND


def test_build_claim_metadata_labels():
    claim = build_claim(_sample_request(), _sample_placement())
    labels = claim["metadata"]["labels"]
    assert labels["platform.example.org/cell"] == "cell-us-east"
    assert labels["platform.example.org/environment"] == "production"
    assert labels["platform.example.org/tier"] == "medium"


def test_build_claim_metadata_name_and_namespace():
    claim = build_claim(_sample_request(), _sample_placement())
    assert claim["metadata"]["name"] == "orders-db"
    assert claim["metadata"]["namespace"] == "team-alpha"


def test_build_claim_placement_reason_annotation():
    claim = build_claim(_sample_request(), _sample_placement())
    ann = claim["metadata"]["annotations"]
    raw = ann["platform.example.org/placement-reason"]
    reason = json.loads(raw)
    assert reason["tier"] == "medium"
    assert "selected" in reason


def test_build_claim_spec_parameters():
    claim = build_claim(_sample_request(), _sample_placement())
    params = claim["spec"]["parameters"]
    assert params["cell"] == "cell-us-east"
    assert params["environment"] == "production"
    assert params["tier"] == "medium"
    assert params["provider"] == "aws"
    assert params["region"] == "us-east-1"
    assert params["size"] == "large"
    assert params["storageGB"] == 100
    assert params["ha"] is True
    assert params["network"] == {"vpc_id": "vpc-123", "subnet_group": "db-private"}


def test_build_claim_composition_selector():
    claim = build_claim(_sample_request(), _sample_placement())
    labels = claim["spec"]["compositionSelector"]["matchLabels"]
    assert labels["db.platform.example.org/provider"] == "aws"
    assert labels["db.platform.example.org/class"] == "mysql"


def test_build_claim_connection_secret_ref():
    claim = build_claim(_sample_request(), _sample_placement())
    ref = claim["spec"]["writeConnectionSecretToRef"]
    assert ref["name"] == "orders-db-conn"


def test_build_claim_different_providers():
    """Claim builder correctly reflects different provider placements."""
    for provider in ("aws", "gcp", "oci"):
        req = _sample_request()
        placement = PlacementDecision(
            provider=provider,
            region=f"{provider}-region-1",
            runtime_cluster=f"{provider}-cluster",
            network={"id": f"net-{provider}"},
            reason={"tier": "medium"},
        )
        claim = build_claim(req, placement)
        assert claim["spec"]["parameters"]["provider"] == provider
        assert claim["spec"]["compositionSelector"]["matchLabels"][
            "db.platform.example.org/provider"
        ] == provider
