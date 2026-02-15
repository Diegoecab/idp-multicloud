from internal.policy.types import TierRequirements


TIER_POLICIES = {
    "C0": TierRequirements(
        rto_minutes=30,
        rpo_minutes=5,
        requires_pitr=True,
        requires_multiaz=True,
        requires_private_networking=True,
        weights={"latency": 0.30, "dr": 0.30, "maturity": 0.25, "cost": 0.15},
    ),
    "C1": TierRequirements(
        rto_minutes=120,
        rpo_minutes=15,
        requires_pitr=True,
        requires_multiaz=False,
        requires_private_networking=True,
        weights={"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
    ),
    "C2": TierRequirements(
        rto_minutes=480,
        rpo_minutes=60,
        requires_pitr=False,
        requires_multiaz=False,
        requires_private_networking=True,
        # C2 explicitly prioritizes cost over all other dimensions.
        weights={"latency": 0.20, "dr": 0.15, "maturity": 0.15, "cost": 0.50},
    ),
}
