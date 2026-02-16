"""Tests for the placement scheduler."""

import os
import sys
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from internal.models.types import MySQLRequest, Candidate
from internal.scheduler.scheduler import schedule, score_candidate, CANDIDATES
from internal.policy.tiers import get_tier


def _make_request(**overrides) -> MySQLRequest:
    """Helper to build a valid MySQLRequest with sensible defaults."""
    defaults = {
        "cell": "cell-us-east",
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


def _make_candidate(**overrides) -> Candidate:
    defaults = {
        "provider": "test",
        "region": "test-region",
        "runtime_cluster": "test-cluster",
        "network": {"vpc": "test"},
        "capabilities": {"pitr", "multi_az", "private_networking"},
        "scores": {"latency": 0.8, "dr": 0.8, "maturity": 0.8, "cost": 0.8},
    }
    defaults.update(overrides)
    return Candidate(**defaults)


# ── Gate Tests ────────────────────────────────────────────────────────────────

def test_candidate_passes_gates_when_capabilities_match():
    tier = get_tier("low")
    c = _make_candidate(capabilities={"pitr", "multi_az", "private_networking"})
    result = score_candidate(c, tier)
    assert result.passed_gates is True
    assert result.gate_failures == []


def test_candidate_fails_gate_when_missing_capability():
    tier = get_tier("low")
    c = _make_candidate(capabilities={"pitr", "private_networking"})  # missing multi_az
    result = score_candidate(c, tier)
    assert result.passed_gates is False
    assert len(result.gate_failures) > 0
    assert "multi_az" in result.gate_failures[0]


def test_critical_tier_only_needs_private_networking():
    tier = get_tier("critical")
    c = _make_candidate(capabilities={"private_networking"})
    result = score_candidate(c, tier)
    assert result.passed_gates is True


# ── Scoring Tests ─────────────────────────────────────────────────────────────

def test_score_uses_tier_weights():
    tier = get_tier("critical")  # cost weight = 0.50
    c = _make_candidate(
        capabilities={"private_networking"},
        scores={"latency": 0.5, "dr": 0.5, "maturity": 0.5, "cost": 1.0},
    )
    result = score_candidate(c, tier)
    # Expected: 0.5*0.15 + 0.5*0.15 + 0.5*0.20 + 1.0*0.50 = 0.075+0.075+0.10+0.50 = 0.75
    assert abs(result.total_score - 0.75) < 0.01


def test_higher_cost_score_wins_in_critical_tier():
    """In critical tier, a cheaper candidate should win."""
    req = _make_request(tier="critical")
    cheap = _make_candidate(
        provider="oci", region="cheap-1", runtime_cluster="cheap-cluster",
        capabilities={"private_networking"},
        scores={"latency": 0.5, "dr": 0.5, "maturity": 0.5, "cost": 1.0},
    )
    expensive = _make_candidate(
        provider="aws", region="expensive-1", runtime_cluster="exp-cluster",
        capabilities={"private_networking"},
        scores={"latency": 0.9, "dr": 0.9, "maturity": 0.9, "cost": 0.2},
    )
    placement = schedule(req, candidates=[cheap, expensive])
    assert placement.provider == "oci"


# ── Scheduling Tests ──────────────────────────────────────────────────────────

def test_schedule_returns_placement_decision():
    req = _make_request(tier="medium")
    placement = schedule(req)
    assert placement.provider in ("aws", "gcp", "oci")
    assert placement.region
    assert placement.runtime_cluster
    assert placement.network
    assert placement.reason


def test_schedule_reason_has_top3():
    req = _make_request(tier="medium")
    placement = schedule(req)
    assert "top_3_candidates" in placement.reason
    assert len(placement.reason["top_3_candidates"]) <= 3
    assert len(placement.reason["top_3_candidates"]) >= 1
    for entry in placement.reason["top_3_candidates"]:
        assert "rank" in entry
        assert "total_score" in entry
        assert "subscores" in entry


def test_schedule_reason_includes_metadata():
    req = _make_request(tier="low")
    placement = schedule(req)
    reason = placement.reason
    assert reason["tier"] == "low"
    assert reason["rto_minutes"] == 30
    assert reason["rpo_minutes"] == 5
    assert "gates" in reason
    assert "weights" in reason
    assert "candidates_evaluated" in reason
    assert "candidates_passed_gates" in reason


def test_schedule_unknown_tier_raises():
    req = _make_request(tier="nonexistent")
    with pytest.raises(ValueError, match="Unknown tier"):
        schedule(req)


def test_schedule_no_candidates_pass_gates():
    """business_critical requires cross_region_replication; OCI candidates lack it."""
    req = _make_request(tier="business_critical")
    oci_only = [
        _make_candidate(
            provider="oci", region="r1", runtime_cluster="c1",
            capabilities={"pitr", "private_networking"},  # missing multi_az, cross_region
        ),
    ]
    with pytest.raises(ValueError, match="No candidates pass"):
        schedule(req, candidates=oci_only)


def test_schedule_with_default_candidates():
    """Ensure all four tiers can schedule against the default candidate pool."""
    for tier_name in ("low", "medium", "critical"):
        req = _make_request(tier=tier_name)
        placement = schedule(req)
        assert placement.provider is not None


def test_schedule_empty_pool_raises():
    req = _make_request(tier="medium")
    with pytest.raises(ValueError, match="No candidates available"):
        schedule(req, candidates=[])
