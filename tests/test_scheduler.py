"""Tests for the placement scheduler."""

import os
import sys
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from internal.models.types import MySQLRequest, Candidate
from internal.scheduler.scheduler import (
    schedule, score_candidate, CANDIDATES,
    set_provider_health, get_provider_health, get_circuit_breaker,
    _provider_health, _provider_circuit_breakers,
)
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


@pytest.fixture(autouse=True)
def _reset_health_state():
    """Reset provider health and circuit breakers between tests."""
    _provider_health.clear()
    _provider_circuit_breakers.clear()
    yield
    _provider_health.clear()
    _provider_circuit_breakers.clear()


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


# ── HA Enforcement Tests ─────────────────────────────────────────────────────

def test_ha_override_adds_multi_az_gate():
    """When ha_override=True, multi_az becomes a hard gate even if tier doesn't require it."""
    tier = get_tier("critical")  # critical only needs private_networking
    c = _make_candidate(capabilities={"private_networking"})  # no multi_az
    result = score_candidate(c, tier, ha_override=True)
    assert result.passed_gates is False
    assert any("multi_az" in f for f in result.gate_failures)


def test_ha_override_passes_when_candidate_has_multi_az():
    tier = get_tier("critical")
    c = _make_candidate(capabilities={"private_networking", "multi_az"})
    result = score_candidate(c, tier, ha_override=True)
    assert result.passed_gates is True


def test_ha_false_does_not_enforce_multi_az():
    """ha=False should not add extra gates."""
    req = _make_request(tier="critical", ha=False)
    oci = _make_candidate(
        provider="oci", region="r1", runtime_cluster="c1",
        capabilities={"private_networking"},  # no multi_az
    )
    placement = schedule(req, candidates=[oci])
    assert placement.provider == "oci"


def test_ha_true_rejects_candidates_without_multi_az():
    """ha=True should reject candidates that lack multi_az, even for tiers that don't require it."""
    req = _make_request(tier="critical", ha=True)
    oci = _make_candidate(
        provider="oci", region="r1", runtime_cluster="c1",
        capabilities={"private_networking"},  # no multi_az
    )
    with pytest.raises(ValueError, match="No candidates pass"):
        schedule(req, candidates=[oci])


def test_ha_true_selects_multi_az_candidate():
    req = _make_request(tier="medium", ha=True)
    no_az = _make_candidate(
        provider="oci", region="r1", runtime_cluster="c1",
        capabilities={"pitr", "private_networking"},
        scores={"latency": 0.9, "dr": 0.9, "maturity": 0.9, "cost": 0.9},
    )
    with_az = _make_candidate(
        provider="aws", region="r2", runtime_cluster="c2",
        capabilities={"pitr", "private_networking", "multi_az"},
        scores={"latency": 0.5, "dr": 0.5, "maturity": 0.5, "cost": 0.5},
    )
    placement = schedule(req, candidates=[no_az, with_az])
    assert placement.provider == "aws"


def test_schedule_reason_includes_ha_enforced():
    req = _make_request(tier="medium", ha=True)
    placement = schedule(req)
    assert placement.reason["ha_enforced"] is True


def test_schedule_reason_ha_enforced_false():
    req = _make_request(tier="medium", ha=False)
    placement = schedule(req)
    assert placement.reason["ha_enforced"] is False


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
    req = _make_request(tier="critical", ha=False)
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
    assert "candidates_healthy" in reason


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
    """Ensure all four tiers that can schedule against default pool do succeed."""
    for tier_name in ("low", "medium", "critical"):
        req = _make_request(tier=tier_name)
        placement = schedule(req)
        assert placement.provider is not None


def test_schedule_empty_pool_raises():
    req = _make_request(tier="medium")
    with pytest.raises(ValueError, match="No candidates available"):
        schedule(req, candidates=[])


# ── Provider Health Tests ────────────────────────────────────────────────────

def test_unhealthy_provider_skipped():
    """Candidates from an unhealthy provider are excluded from scheduling."""
    set_provider_health("aws", False)
    req = _make_request(tier="medium", ha=False)
    placement = schedule(req)
    assert placement.provider != "aws"


def test_unhealthy_provider_appears_in_reason():
    set_provider_health("aws", False)
    req = _make_request(tier="medium", ha=False)
    placement = schedule(req)
    assert "unhealthy_skipped" in placement.reason
    skipped_providers = [s["provider"] for s in placement.reason["unhealthy_skipped"]]
    assert "aws" in skipped_providers


def test_all_providers_unhealthy_raises():
    set_provider_health("aws", False)
    set_provider_health("gcp", False)
    set_provider_health("oci", False)
    req = _make_request(tier="medium", ha=False)
    with pytest.raises(ValueError, match="No healthy candidates"):
        schedule(req)


def test_candidate_level_healthy_false():
    """A candidate marked as unhealthy individually is skipped."""
    unhealthy_candidate = _make_candidate(
        provider="bad", region="r1", runtime_cluster="c1",
        capabilities={"pitr", "private_networking"},
        scores={"latency": 1.0, "dr": 1.0, "maturity": 1.0, "cost": 1.0},
        healthy=False,
    )
    healthy_candidate = _make_candidate(
        provider="good", region="r2", runtime_cluster="c2",
        capabilities={"pitr", "private_networking"},
        scores={"latency": 0.5, "dr": 0.5, "maturity": 0.5, "cost": 0.5},
    )
    req = _make_request(tier="medium", ha=False)
    placement = schedule(req, candidates=[unhealthy_candidate, healthy_candidate])
    assert placement.provider == "good"


def test_set_and_get_provider_health():
    set_provider_health("aws", False)
    assert get_provider_health("aws") is False
    set_provider_health("aws", True)
    assert get_provider_health("aws") is True


def test_unknown_provider_defaults_healthy():
    assert get_provider_health("unknown_provider") is True


# ── Circuit Breaker Tests ────────────────────────────────────────────────────

def test_circuit_breaker_starts_closed():
    cb = get_circuit_breaker("test-provider")
    assert cb.state == "closed"
    assert cb.allow_request() is True


def test_circuit_breaker_opens_after_threshold():
    cb = get_circuit_breaker("test-provider")
    for _ in range(cb.failure_threshold):
        cb.record_failure()
    assert cb.state == "open"
    assert cb.allow_request() is False


def test_circuit_breaker_resets_on_success():
    cb = get_circuit_breaker("test-provider")
    for _ in range(3):
        cb.record_failure()
    cb.record_success()
    assert cb.state == "closed"
    assert cb.allow_request() is True


def test_circuit_breaker_half_open_after_cooldown():
    cb = get_circuit_breaker("test-provider")
    cb.cooldown_seconds = 0  # instant cooldown for testing
    for _ in range(cb.failure_threshold):
        cb.record_failure()
    assert cb.state == "half_open"  # cooldown expired immediately
    assert cb.allow_request() is True  # half-open allows one probe


def test_circuit_open_skips_candidates():
    """Candidates whose provider's circuit breaker is open get skipped."""
    cb = get_circuit_breaker("bad-provider")
    for _ in range(cb.failure_threshold):
        cb.record_failure()

    bad = _make_candidate(
        provider="bad-provider", region="r1", runtime_cluster="c1",
        capabilities={"pitr", "private_networking"},
        scores={"latency": 1.0, "dr": 1.0, "maturity": 1.0, "cost": 1.0},
    )
    good = _make_candidate(
        provider="good-provider", region="r2", runtime_cluster="c2",
        capabilities={"pitr", "private_networking"},
        scores={"latency": 0.5, "dr": 0.5, "maturity": 0.5, "cost": 0.5},
    )
    req = _make_request(tier="medium", ha=False)
    placement = schedule(req, candidates=[bad, good])
    assert placement.provider == "good-provider"


def test_circuit_breaker_to_dict():
    cb = get_circuit_breaker("test-provider")
    d = cb.to_dict()
    assert d["state"] == "closed"
    assert d["failure_count"] == 0
    assert "failure_threshold" in d
    assert "cooldown_seconds" in d


# ── Failover Tests ───────────────────────────────────────────────────────────

def test_low_tier_includes_failover():
    """Low tier should include a failover candidate in a different cloud."""
    req = _make_request(tier="low")
    placement = schedule(req)
    assert placement.failover is not None
    assert placement.failover["provider"] != placement.provider
    assert "anti_affinity" in placement.failover


def test_failover_in_reason():
    req = _make_request(tier="low")
    placement = schedule(req)
    assert "failover" in placement.reason
    assert placement.reason["failover"]["provider"] != placement.provider


def test_medium_tier_no_failover():
    """Medium tier should NOT include a failover candidate."""
    req = _make_request(tier="medium", ha=False)
    placement = schedule(req)
    assert placement.failover is None


def test_critical_tier_no_failover():
    """Critical tier should NOT include a failover candidate."""
    req = _make_request(tier="critical", ha=False)
    placement = schedule(req)
    assert placement.failover is None


def test_failover_none_when_only_one_provider():
    """If all candidates are from the same provider, failover should be None."""
    req = _make_request(tier="low")
    aws_only = [
        _make_candidate(
            provider="aws", region="us-east-1", runtime_cluster="c1",
            capabilities={"pitr", "multi_az", "private_networking"},
        ),
        _make_candidate(
            provider="aws", region="us-west-2", runtime_cluster="c2",
            capabilities={"pitr", "multi_az", "private_networking"},
        ),
    ]
    placement = schedule(req, candidates=aws_only)
    assert placement.failover is None


def test_failover_different_cloud_from_primary():
    """Failover must be in a different cloud provider than the primary."""
    req = _make_request(tier="low")
    candidates = [
        _make_candidate(
            provider="aws", region="us-east-1", runtime_cluster="c1",
            capabilities={"pitr", "multi_az", "private_networking"},
            scores={"latency": 0.95, "dr": 0.95, "maturity": 0.95, "cost": 0.50},
        ),
        _make_candidate(
            provider="gcp", region="us-central1", runtime_cluster="c2",
            capabilities={"pitr", "multi_az", "private_networking"},
            scores={"latency": 0.80, "dr": 0.80, "maturity": 0.80, "cost": 0.70},
        ),
    ]
    placement = schedule(req, candidates=candidates)
    assert placement.provider == "aws"
    assert placement.failover["provider"] == "gcp"
