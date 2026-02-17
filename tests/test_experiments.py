"""Tests for the experimentation engine: A/B tests, feature flags, analytics."""

import os
import sys
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from internal.scheduler.experiments import (
    Experiment,
    create_experiment,
    get_experiment,
    list_experiments,
    delete_experiment,
    resolve_weights,
    set_feature_flag,
    get_feature_flag,
    list_feature_flags,
    delete_feature_flag,
    PlacementAnalytics,
    analytics,
    _experiments,
    _feature_flags,
)
from internal.models.types import MySQLRequest
from internal.scheduler.scheduler import (
    schedule, CANDIDATES,
    _provider_health, _provider_circuit_breakers,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset all experiments, flags, analytics, and health state."""
    _experiments.clear()
    _feature_flags.clear()
    _provider_health.clear()
    _provider_circuit_breakers.clear()
    analytics.reset()
    yield
    _experiments.clear()
    _feature_flags.clear()
    _provider_health.clear()
    _provider_circuit_breakers.clear()
    analytics.reset()


def _make_request(**overrides) -> MySQLRequest:
    defaults = {
        "cell": "cell-us", "tier": "medium", "environment": "production",
        "size": "medium", "storage_gb": 50, "ha": False,
        "namespace": "default", "name": "test-db",
    }
    defaults.update(overrides)
    return MySQLRequest(**defaults)


# ── Experiment CRUD ──────────────────────────────────────────────────────────

def test_create_experiment():
    exp = create_experiment(
        "exp-001", "Test cost boost",
        {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60},
        traffic_percentage=0.5, tier="critical",
    )
    assert exp.id == "exp-001"
    assert exp.traffic_percentage == 0.5
    assert exp.tier == "critical"
    assert exp.enabled is True


def test_create_experiment_invalid_weights():
    with pytest.raises(ValueError, match="sum to 1.0"):
        create_experiment(
            "bad", "Bad weights",
            {"latency": 0.50, "dr": 0.50, "maturity": 0.50, "cost": 0.50},
            traffic_percentage=0.5,
        )


def test_create_experiment_invalid_traffic():
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        create_experiment(
            "bad", "Bad traffic",
            {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
            traffic_percentage=1.5,
        )


def test_list_experiments():
    create_experiment(
        "exp-001", "Test", {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
        traffic_percentage=0.5,
    )
    exps = list_experiments()
    assert len(exps) == 1
    assert exps[0]["id"] == "exp-001"


def test_delete_experiment():
    create_experiment(
        "exp-001", "Test", {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
        traffic_percentage=0.5,
    )
    assert delete_experiment("exp-001") is True
    assert delete_experiment("nonexistent") is False
    assert list_experiments() == []


def test_get_experiment():
    create_experiment(
        "exp-001", "Test", {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
        traffic_percentage=0.5,
    )
    assert get_experiment("exp-001") is not None
    assert get_experiment("nonexistent") is None


# ── Experiment Deterministic Assignment ──────────────────────────────────────

def test_experiment_assignment_is_deterministic():
    exp = Experiment(
        id="exp-det", description="Det test",
        variant_weights={"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
        traffic_percentage=0.5, tier="*",
    )
    group1 = exp.assign_group("orders-db")
    group2 = exp.assign_group("orders-db")
    assert group1 == group2
    assert group1 in ("control", "variant")


def test_experiment_full_traffic_always_variant():
    exp = Experiment(
        id="full", description="Full traffic",
        variant_weights={"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
        traffic_percentage=1.0, tier="*",
    )
    # With 100% traffic, every request should be variant
    for name in ["db-1", "db-2", "db-3", "db-4", "db-5"]:
        assert exp.assign_group(name) == "variant"


def test_experiment_zero_traffic_always_control():
    exp = Experiment(
        id="none", description="Zero traffic",
        variant_weights={"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
        traffic_percentage=0.0, tier="*",
    )
    for name in ["db-1", "db-2", "db-3", "db-4", "db-5"]:
        assert exp.assign_group(name) == "control"


# ── Weight Resolution ────────────────────────────────────────────────────────

def test_resolve_weights_no_experiment():
    default_weights = {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25}
    weights, info = resolve_weights("medium", default_weights, "test-db")
    assert weights == default_weights
    assert info is None


def test_resolve_weights_with_experiment_variant():
    variant_w = {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60}
    create_experiment("exp-v", "Test variant", variant_w, traffic_percentage=1.0, tier="medium")
    default_w = {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25}
    weights, info = resolve_weights("medium", default_w, "any-db")
    assert weights == variant_w
    assert info["group"] == "variant"
    assert info["experiment_id"] == "exp-v"


def test_resolve_weights_with_experiment_control():
    variant_w = {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60}
    create_experiment("exp-c", "Test control", variant_w, traffic_percentage=0.0, tier="medium")
    default_w = {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25}
    weights, info = resolve_weights("medium", default_w, "any-db")
    assert weights == default_w
    assert info["group"] == "control"


def test_resolve_weights_experiment_tier_mismatch():
    """Experiment for 'critical' doesn't apply to 'medium' tier."""
    variant_w = {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60}
    create_experiment("exp-tier", "Critical only", variant_w, traffic_percentage=1.0, tier="critical")
    default_w = {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25}
    weights, info = resolve_weights("medium", default_w, "any-db")
    assert weights == default_w
    assert info is None


def test_resolve_weights_wildcard_tier():
    """Experiment with tier='*' applies to any tier."""
    variant_w = {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60}
    create_experiment("exp-all", "All tiers", variant_w, traffic_percentage=1.0, tier="*")
    default_w = {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25}
    for tier in ("low", "medium", "critical"):
        weights, info = resolve_weights(tier, default_w, "any-db")
        assert weights == variant_w
        assert info is not None


def test_disabled_experiment_ignored():
    variant_w = {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60}
    exp = create_experiment("exp-disabled", "Disabled", variant_w, traffic_percentage=1.0)
    exp.enabled = False
    default_w = {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25}
    weights, info = resolve_weights("medium", default_w, "any-db")
    assert weights == default_w
    assert info is None


# ── Experiment Integration with Scheduler ────────────────────────────────────

def test_scheduler_uses_experiment_weights():
    """When an experiment is active, the scheduler uses variant weights."""
    # Create experiment that heavily favors cost (should push OCI to top for critical)
    create_experiment(
        "exp-cost", "Cost experiment",
        {"latency": 0.05, "dr": 0.05, "maturity": 0.10, "cost": 0.80},
        traffic_percentage=1.0, tier="critical",
    )
    req = _make_request(tier="critical", name="exp-test-db")
    placement = schedule(req)
    # With 80% cost weight, OCI (cost=0.90) should win
    assert placement.provider == "oci"
    assert "experiment" in placement.reason
    assert placement.reason["experiment"]["group"] == "variant"


def test_scheduler_control_group_uses_default_weights():
    """Control group should use default tier weights."""
    create_experiment(
        "exp-ctrl", "Control test",
        {"latency": 0.05, "dr": 0.05, "maturity": 0.10, "cost": 0.80},
        traffic_percentage=0.0, tier="critical",
    )
    req = _make_request(tier="critical", name="any-db")
    placement = schedule(req)
    assert "experiment" in placement.reason
    assert placement.reason["experiment"]["group"] == "control"


def test_scheduler_records_analytics():
    """Scheduler should record placement in analytics."""
    req = _make_request(tier="medium")
    schedule(req)
    summary = analytics.get_summary()
    assert summary["total_placements"] == 1
    assert summary["total_requests"] == 1


# ── Feature Flags ────────────────────────────────────────────────────────────

def test_set_and_get_feature_flag():
    set_feature_flag("new_feature", True)
    assert get_feature_flag("new_feature") is True


def test_feature_flag_default():
    assert get_feature_flag("nonexistent") is False
    assert get_feature_flag("nonexistent", default=True) is True


def test_list_feature_flags_empty():
    assert list_feature_flags() == {}


def test_list_feature_flags():
    set_feature_flag("flag_a", True)
    set_feature_flag("flag_b", False)
    flags = list_feature_flags()
    assert flags == {"flag_a": True, "flag_b": False}


def test_delete_feature_flag():
    set_feature_flag("temp", True)
    assert delete_feature_flag("temp") is True
    assert delete_feature_flag("nonexistent") is False


def test_prefer_cost_optimization_flag():
    """Feature flag prefer_cost_optimization should boost cost weight."""
    set_feature_flag("prefer_cost_optimization", True)
    req = _make_request(tier="medium", name="cost-test")
    placement = schedule(req)
    # Cost weight should be boosted from 0.25 to 0.30 (25% * 1.2)
    assert placement.reason["weights"]["cost"] > 0.25


# ── Placement Analytics ──────────────────────────────────────────────────────

def test_analytics_empty_summary():
    summary = analytics.get_summary()
    assert summary["total_placements"] == 0
    assert summary["gate_rejection_rate"] == 0.0


def test_analytics_records_placements():
    analytics.record_placement({"provider": "aws", "region": "us-east-1", "tier": "medium", "total_score": 0.85})
    analytics.record_placement({"provider": "gcp", "region": "us-central1", "tier": "medium", "total_score": 0.80})
    analytics.record_placement({"provider": "aws", "region": "us-east-1", "tier": "low", "total_score": 0.90})

    summary = analytics.get_summary()
    assert summary["total_placements"] == 3
    assert summary["provider_distribution"]["aws"]["count"] == 2
    assert summary["provider_distribution"]["gcp"]["count"] == 1
    assert summary["tier_distribution"]["medium"]["count"] == 2
    assert summary["avg_score_by_provider"]["aws"] == 0.875


def test_analytics_gate_rejection_rate():
    analytics.record_placement({"provider": "aws", "region": "r1", "tier": "low", "total_score": 0.8})
    analytics.record_gate_rejection()
    analytics.record_gate_rejection()
    summary = analytics.get_summary()
    assert summary["total_requests"] == 3
    assert abs(summary["gate_rejection_rate"] - 0.6667) < 0.01


def test_analytics_experiment_tracking():
    analytics.record_placement({
        "provider": "aws", "region": "r1", "tier": "medium", "total_score": 0.8,
        "experiment": {"experiment_id": "exp-1", "group": "variant"},
    })
    analytics.record_placement({
        "provider": "gcp", "region": "r1", "tier": "medium", "total_score": 0.7,
        "experiment": {"experiment_id": "exp-1", "group": "control"},
    })
    summary = analytics.get_summary()
    assert summary["experiments"]["exp-1"]["variant"] == 1
    assert summary["experiments"]["exp-1"]["control"] == 1


def test_analytics_reset():
    analytics.record_placement({"provider": "aws", "region": "r1", "tier": "low", "total_score": 0.8})
    analytics.reset()
    summary = analytics.get_summary()
    assert summary["total_placements"] == 0
