"""Placement scheduler: gate filtering, weighted scoring, candidate ranking,
provider health checks, HA enforcement, and cross-cloud failover.

Flow:
  1. Load candidates from the registry.
  2. Filter unhealthy candidates (provider health status).
  3. Apply hard gates (reject candidates missing required capabilities for the tier).
  4. If ha=True, enforce multi_az as an additional gate.
  5. Compute a weighted score for each surviving candidate.
  6. Sort by score descending, select the winner.
  7. For tiers that require DR, select a failover candidate in a DIFFERENT cloud.
  8. Return a PlacementDecision with top-3 breakdown and optional failover.
"""

from internal.models.types import (
    Candidate, CandidateScore, PlacementDecision, MySQLRequest, CircuitBreaker,
)
from internal.policy.tiers import get_tier

# ── Provider Health Registry ────────────────────────────────────────────────
# Tracks health per provider. In production this would be fed by external probes.

_provider_health: dict[str, bool] = {}
_provider_circuit_breakers: dict[str, CircuitBreaker] = {}


def set_provider_health(provider: str, healthy: bool):
    """Mark a provider as healthy or unhealthy (operator action or probe result)."""
    _provider_health[provider] = healthy


def get_provider_health(provider: str) -> bool:
    """Return the health status of a provider (default: healthy)."""
    return _provider_health.get(provider, True)


def get_all_provider_health() -> dict:
    """Return all provider health statuses."""
    return dict(_provider_health)


def get_circuit_breaker(provider: str) -> CircuitBreaker:
    """Return (or create) the circuit breaker for a provider."""
    if provider not in _provider_circuit_breakers:
        _provider_circuit_breakers[provider] = CircuitBreaker()
    return _provider_circuit_breakers[provider]


def get_all_circuit_breakers() -> dict:
    """Return all circuit breaker states keyed by provider."""
    return {p: cb.to_dict() for p, cb in _provider_circuit_breakers.items()}


# ── Candidate Registry ───────────────────────────────────────────────────────
# In production this would come from a config file or database.

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

# Tiers that require a failover candidate in a different cloud provider
_FAILOVER_TIERS = {"low", "business_critical"}


def score_candidate(candidate: Candidate, tier, ha_override: bool = False) -> CandidateScore:
    """Evaluate a candidate against a tier: check gates, compute weighted score.

    Args:
        candidate: The provider/region candidate.
        tier: The tier definition.
        ha_override: If True, add multi_az as an additional hard gate.
    """
    gate_failures = []
    required = set(tier.required_capabilities)

    # HA enforcement: if the developer requests ha=True, demand multi_az
    if ha_override and "multi_az" not in required:
        required.add("multi_az")

    for cap in required:
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

    Pipeline stages:
      1. Health filter   — skip candidates whose provider is unhealthy or circuit-open
      2. Gate filter      — reject candidates missing tier capabilities
      3. HA enforcement   — if ha=True, add multi_az as an extra gate
      4. Weighted scoring — rank surviving candidates
      5. Failover select  — for critical tiers, pick a DR candidate in a different cloud

    Args:
        request: The developer's MySQL request.
        candidates: Optional override of the candidate pool (for testing).

    Returns:
        PlacementDecision with provider, region, network, reason, and optional failover.

    Raises:
        ValueError: If the tier is unknown or no candidates pass the gates.
    """
    tier = get_tier(request.tier)
    if tier is None:
        raise ValueError(f"Unknown tier: {request.tier}")

    pool = candidates if candidates is not None else CANDIDATES
    if not pool:
        raise ValueError("No candidates available in the registry")

    # Stage 1 — Health filter: remove unhealthy or circuit-open candidates
    healthy_pool = []
    unhealthy_skipped = []
    for c in pool:
        provider_ok = get_provider_health(c.provider) and c.healthy
        cb = get_circuit_breaker(c.provider)
        circuit_ok = cb.allow_request()
        if provider_ok and circuit_ok:
            healthy_pool.append(c)
        else:
            reason = []
            if not provider_ok:
                reason.append("provider_unhealthy")
            if not circuit_ok:
                reason.append("circuit_open")
            unhealthy_skipped.append({
                "provider": c.provider,
                "region": c.region,
                "reasons": reason,
            })

    if not healthy_pool:
        raise ValueError(
            f"No healthy candidates available. All candidates skipped: {unhealthy_skipped}"
        )

    # Stage 2+3 — Score with gate filtering (+ HA enforcement)
    ha_enforce = request.ha
    scored = [score_candidate(c, tier, ha_override=ha_enforce) for c in healthy_pool]

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

    # Stage 4 — Rank by total score (descending)
    passed.sort(key=lambda s: s.total_score, reverse=True)
    top3 = passed[:3]
    winner = top3[0]

    winner_candidate = next(
        c for c in healthy_pool
        if c.provider == winner.provider and c.region == winner.region
    )

    # Stage 5 — Failover: pick best candidate in a DIFFERENT cloud provider
    failover_info = None
    if request.tier in _FAILOVER_TIERS:
        failover_candidates = [
            s for s in passed if s.provider != winner.provider
        ]
        if failover_candidates:
            failover_winner = failover_candidates[0]
            failover_candidate_obj = next(
                c for c in healthy_pool
                if c.provider == failover_winner.provider and c.region == failover_winner.region
            )
            failover_info = {
                "provider": failover_winner.provider,
                "region": failover_winner.region,
                "runtime_cluster": failover_winner.runtime_cluster,
                "network": failover_candidate_obj.network,
                "total_score": failover_winner.total_score,
                "anti_affinity": f"different_cloud_from_{winner.provider}",
            }

    # Build gates list — include the HA-enforced gate if applicable
    effective_gates = list(tier.required_capabilities)
    if ha_enforce and "multi_az" not in effective_gates:
        effective_gates.append("multi_az")

    # Build auditable reason JSON
    reason = {
        "tier": request.tier,
        "rto_minutes": tier.rto_minutes,
        "rpo_minutes": tier.rpo_minutes,
        "gates": effective_gates,
        "ha_enforced": ha_enforce,
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
        "candidates_healthy": len(healthy_pool),
        "candidates_passed_gates": len(passed),
    }
    if unhealthy_skipped:
        reason["unhealthy_skipped"] = unhealthy_skipped
    if failover_info:
        reason["failover"] = failover_info

    return PlacementDecision(
        provider=winner.provider,
        region=winner.region,
        runtime_cluster=winner.runtime_cluster,
        network=winner_candidate.network,
        reason=reason,
        failover=failover_info,
    )
