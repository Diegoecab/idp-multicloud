"""Tests for the criticality framework (policy/tiers)."""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from internal.policy.tiers import get_tier, list_tiers, TIERS


def test_all_tiers_exist():
    """All four tiers must be defined."""
    assert set(TIERS.keys()) == {"low", "medium", "critical", "business_critical"}


def test_get_tier_returns_correct_tier():
    tier = get_tier("low")
    assert tier is not None
    assert tier.name == "low"
    assert tier.rto_minutes == 30
    assert tier.rpo_minutes == 5


def test_get_tier_unknown_returns_none():
    assert get_tier("nonexistent") is None


def test_tier_weights_sum_to_one():
    """Each tier's weights must sum to 1.0."""
    for name, tier in TIERS.items():
        total = sum(tier.weights.values())
        assert abs(total - 1.0) < 0.001, f"Tier '{name}' weights sum to {total}, expected 1.0"


def test_low_tier_gates():
    tier = get_tier("low")
    assert "pitr" in tier.required_capabilities
    assert "multi_az" in tier.required_capabilities
    assert "private_networking" in tier.required_capabilities


def test_medium_tier_gates():
    tier = get_tier("medium")
    assert "pitr" in tier.required_capabilities
    assert "private_networking" in tier.required_capabilities
    assert "multi_az" not in tier.required_capabilities


def test_critical_tier_gates():
    tier = get_tier("critical")
    assert "private_networking" in tier.required_capabilities
    assert len(tier.required_capabilities) == 1


def test_critical_tier_cost_weight_is_highest():
    """Critical tier must have cost as the dominant weight."""
    tier = get_tier("critical")
    assert tier.weights["cost"] > tier.weights["latency"]
    assert tier.weights["cost"] > tier.weights["dr"]
    assert tier.weights["cost"] > tier.weights["maturity"]


def test_business_critical_tier():
    tier = get_tier("business_critical")
    assert tier.rto_minutes == 15
    assert tier.rpo_minutes == 1
    assert "cross_region_replication" in tier.required_capabilities


def test_list_tiers():
    tiers = list_tiers()
    assert len(tiers) == 4
    names = [t.name for t in tiers]
    for expected in ["low", "medium", "critical", "business_critical"]:
        assert expected in names
