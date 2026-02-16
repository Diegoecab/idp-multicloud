"""Placement scheduler: gate filtering, weighted scoring, and candidate ranking.

Flow:
  1. Load candidates from the registry.
  2. Apply hard gates (reject candidates missing required capabilities for the tier).
  3. Compute a weighted score for each surviving candidate.
  4. Sort by score descending, select the winner.
  5. Return a PlacementDecision with a top-3 candidate breakdown in the reason JSON.
"""

from internal.models.types import Candidate, CandidateScore, PlacementDecision, MySQLRequest
from internal.policy.tiers import get_tier


# ── Candidate Registry ───────────────────────────────────────────────────────
# In production this would come from a config file or database.
# Each candidate represents a provider/region combination with known capabilities.

CANDIDATES = [
    # AWS
    Candidate(
        provider="aws",
        region="us-east-1",
        runtime_cluster="aws-use1-prod-01",
        network={"vpc_id": "vpc-aws-use1", "subnet_group": "db-private-use1"},
        capabilities={"pitr", "multi_az", "private_networking", "cross_region_replication"},
        scores={"latency": 0.90, "dr": 0.95, "maturity": 0.95, "cost": 0.50},
    ),
    Candidate(
        provider="aws",
        region="eu-west-1",
        runtime_cluster="aws-euw1-prod-01",
        network={"vpc_id": "vpc-aws-euw1", "subnet_group": "db-private-euw1"},
        capabilities={"pitr", "multi_az", "private_networking", "cross_region_replication"},
        scores={"latency": 0.70, "dr": 0.90, "maturity": 0.90, "cost": 0.45},
    ),
    Candidate(
        provider="aws",
        region="us-west-2",
        runtime_cluster="aws-usw2-prod-01",
        network={"vpc_id": "vpc-aws-usw2", "subnet_group": "db-private-usw2"},
        capabilities={"pitr", "multi_az", "private_networking"},
        scores={"latency": 0.85, "dr": 0.90, "maturity": 0.90, "cost": 0.55},
    ),
    # GCP
    Candidate(
        provider="gcp",
        region="us-central1",
        runtime_cluster="gcp-usc1-prod-01",
        network={"vpc_name": "vpc-gcp-usc1", "subnet": "db-private-usc1"},
        capabilities={"pitr", "multi_az", "private_networking"},
        scores={"latency": 0.88, "dr": 0.85, "maturity": 0.88, "cost": 0.65},
    ),
    Candidate(
        provider="gcp",
        region="europe-west1",
        runtime_cluster="gcp-euw1-prod-01",
        network={"vpc_name": "vpc-gcp-euw1", "subnet": "db-private-euw1"},
        capabilities={"pitr", "multi_az", "private_networking"},
        scores={"latency": 0.72, "dr": 0.82, "maturity": 0.85, "cost": 0.60},
    ),
    # OCI
    Candidate(
        provider="oci",
        region="us-ashburn-1",
        runtime_cluster="oci-iad-prod-01",
        network={"vcn_id": "vcn-oci-iad", "subnet_id": "db-private-iad"},
        capabilities={"pitr", "private_networking"},
        scores={"latency": 0.80, "dr": 0.70, "maturity": 0.65, "cost": 0.85},
    ),
    Candidate(
        provider="oci",
        region="eu-frankfurt-1",
        runtime_cluster="oci-fra-prod-01",
        network={"vcn_id": "vcn-oci-fra", "subnet_id": "db-private-fra"},
        capabilities={"pitr", "private_networking"},
        scores={"latency": 0.68, "dr": 0.65, "maturity": 0.60, "cost": 0.90},
    ),
]


def score_candidate(candidate: Candidate, tier) -> CandidateScore:
    """Evaluate a candidate against a tier: check gates, compute weighted score."""
    gate_failures = []
    for cap in tier.required_capabilities:
        if cap not in candidate.capabilities:
            gate_failures.append(f"missing required capability: {cap}")

    passed = len(gate_failures) == 0

    subscores = {}
    total = 0.0
    for dimension, weight in tier.weights.items():
        raw = candidate.scores.get(dimension, 0.0)
        weighted = raw * weight
        subscores[dimension] = round(raw, 4)
        total += weighted

    return CandidateScore(
        provider=candidate.provider,
        region=candidate.region,
        runtime_cluster=candidate.runtime_cluster,
        total_score=round(total, 4),
        subscores=subscores,
        passed_gates=passed,
        gate_failures=gate_failures,
    )


def schedule(request: MySQLRequest, candidates=None) -> PlacementDecision:
    """Run the full scheduling pipeline and return a PlacementDecision.

    Args:
        request: The developer's MySQL request.
        candidates: Optional override of the candidate pool (for testing).

    Returns:
        PlacementDecision with provider, region, network, and a reason JSON
        containing the top-3 candidate scoring breakdown.

    Raises:
        ValueError: If the tier is unknown or no candidates pass the gates.
    """
    tier = get_tier(request.tier)
    if tier is None:
        raise ValueError(f"Unknown tier: {request.tier}")

    pool = candidates if candidates is not None else CANDIDATES
    if not pool:
        raise ValueError("No candidates available in the registry")

    # Score every candidate
    scored = [score_candidate(c, tier) for c in pool]

    # Filter by gates
    passed = [s for s in scored if s.passed_gates]
    if not passed:
        failures = {
            f"{s.provider}/{s.region}": s.gate_failures
            for s in scored
        }
        raise ValueError(
            f"No candidates pass the gate requirements for tier '{request.tier}'. "
            f"Gate failures: {failures}"
        )

    # Rank by total score (descending)
    passed.sort(key=lambda s: s.total_score, reverse=True)
    top3 = passed[:3]
    winner = top3[0]

    # Retrieve the full candidate object for the winner (to get network config)
    winner_candidate = next(
        c for c in pool
        if c.provider == winner.provider and c.region == winner.region
    )

    # Build auditable reason JSON
    reason = {
        "tier": request.tier,
        "rto_minutes": tier.rto_minutes,
        "rpo_minutes": tier.rpo_minutes,
        "gates": list(tier.required_capabilities),
        "weights": tier.weights,
        "selected": {
            "provider": winner.provider,
            "region": winner.region,
            "runtime_cluster": winner.runtime_cluster,
            "total_score": winner.total_score,
            "subscores": winner.subscores,
        },
        "top_3_candidates": [
            {
                "rank": i + 1,
                "provider": c.provider,
                "region": c.region,
                "runtime_cluster": c.runtime_cluster,
                "total_score": c.total_score,
                "subscores": c.subscores,
            }
            for i, c in enumerate(top3)
        ],
        "candidates_evaluated": len(pool),
        "candidates_passed_gates": len(passed),
    }

    return PlacementDecision(
        provider=winner.provider,
        region=winner.region,
        runtime_cluster=winner.runtime_cluster,
        network=winner_candidate.network,
        reason=reason,
    )
