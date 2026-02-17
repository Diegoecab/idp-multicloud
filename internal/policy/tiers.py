"""Criticality framework: tier definitions with gates and scoring weights.

Each tier defines:
  - RTO/RPO targets (informational, used for documentation and auditing)
  - Required capabilities (hard gates — candidates without these are rejected)
  - Dimension weights (latency, dr, maturity, cost — must sum to 1.0)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TierDefinition:
    """Immutable criticality tier specification."""
    name: str
    rto_minutes: int
    rpo_minutes: int
    required_capabilities: tuple  # hard gates
    weights: dict  # {latency, dr, maturity, cost} -> float, must sum to 1.0
    description: str = ""


# ── Tier Registry ────────────────────────────────────────────────────────────

TIERS: dict = {
    "low": TierDefinition(
        name="low",
        rto_minutes=30,
        rpo_minutes=5,
        required_capabilities=("pitr", "multi_az", "private_networking"),
        weights={"latency": 0.30, "dr": 0.30, "maturity": 0.25, "cost": 0.15},
        description=(
            "Low tolerance for failure. Strictest SLA with full DR capabilities. "
            "Requires PITR, Multi-AZ, and private networking."
        ),
    ),
    "medium": TierDefinition(
        name="medium",
        rto_minutes=120,
        rpo_minutes=15,
        required_capabilities=("pitr", "private_networking"),
        weights={"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
        description=(
            "Balanced tier. PITR and private networking required. "
            "Equal weighting across all scoring dimensions."
        ),
    ),
    "critical": TierDefinition(
        name="critical",
        rto_minutes=480,
        rpo_minutes=60,
        required_capabilities=("private_networking",),
        weights={"latency": 0.15, "dr": 0.15, "maturity": 0.20, "cost": 0.50},
        description=(
            "Cost-sensitive tier. Only private networking required. "
            "Cost has the highest weight (0.50) to optimize for budget."
        ),
    ),
    "business_critical": TierDefinition(
        name="business_critical",
        rto_minutes=15,
        rpo_minutes=1,
        required_capabilities=("pitr", "multi_az", "private_networking", "cross_region_replication"),
        weights={"latency": 0.25, "dr": 0.40, "maturity": 0.25, "cost": 0.10},
        description=(
            "Highest criticality. Near-zero RPO with full DR and cross-region replication. "
            "DR has the highest weight (0.40) to maximize resilience."
        ),
    ),
}


def get_tier(name: str):
    """Return the TierDefinition for the given name, or None."""
    return TIERS.get(name)


def list_tiers() -> list:
    """Return all tier definitions."""
    return list(TIERS.values())
