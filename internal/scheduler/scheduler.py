import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from internal.policy.cells import CELL_CATALOG
from internal.policy.tiers import TIER_POLICIES
from internal.policy.types import Candidate, TierRequirements


class SchedulingError(Exception):
    pass


def _passes_gates(candidate: Candidate, tier: TierRequirements, ha: bool) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    caps = candidate.capabilities

    if tier.requires_pitr and not caps.get("pitr", False):
        reasons.append("Missing PITR support")
    if tier.requires_multiaz and not caps.get("multiaz", False):
        reasons.append("Missing MultiAZ support")
    if tier.requires_private_networking and not caps.get("privateNetworking", False):
        reasons.append("Missing private networking support")
    if ha and not caps.get("multiaz", False):
        reasons.append("HA requested but candidate is not MultiAZ")

    return len(reasons) == 0, reasons


def schedule_mysql(cell: str, tier_name: str, ha: bool) -> Dict[str, Any]:
    if cell not in CELL_CATALOG:
        raise SchedulingError(f"Unknown cell '{cell}'.")
    if tier_name not in TIER_POLICIES:
        raise SchedulingError(f"Unknown tier '{tier_name}'.")

    tier = TIER_POLICIES[tier_name]
    candidates = CELL_CATALOG[cell]

    excluded: List[Dict[str, Any]] = []
    scored: List[Dict[str, Any]] = []

    for candidate in candidates:
        ok, gate_reasons = _passes_gates(candidate, tier, ha)
        if not ok:
            excluded.append({"candidate": candidate.id, "gateFailures": gate_reasons})
            continue

        weighted_total = 0.0
        sub_scores: Dict[str, Dict[str, float]] = {}
        for dim, weight in tier.weights.items():
            dim_score = float(candidate.scores[dim])
            contribution = dim_score * weight
            weighted_total += contribution
            sub_scores[dim] = {"score": dim_score, "weight": weight, "contribution": round(contribution, 4)}

        scored.append(
            {
                "candidate": candidate,
                "provider": candidate.provider,
                "region": candidate.region,
                "runtimeCluster": candidate.runtime_cluster,
                "network": candidate.network,
                "score": round(weighted_total, 4),
                "subscores": sub_scores,
            }
        )

    if not scored:
        raise SchedulingError("No candidates satisfy hard gates for the selected cell/tier/ha combination.")

    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)
    winner = ranked[0]
    top_3 = [
        {
            "rank": idx + 1,
            "candidate": f"{entry['provider']}:{entry['region']}:{entry['runtimeCluster']}",
            "weightedScore": entry["score"],
            "subscores": entry["subscores"],
        }
        for idx, entry in enumerate(ranked[:3])
    ]

    reason = {
        "version": "v1",
        "decidedAt": datetime.now(timezone.utc).isoformat(),
        "tier": tier_name,
        "requirements": {
            "rtoMinutes": tier.rto_minutes,
            "rpoMinutes": tier.rpo_minutes,
            "requiresPITR": tier.requires_pitr,
            "requiresMultiAZ": tier.requires_multiaz,
            "requiresPrivateNetworking": tier.requires_private_networking,
            "weights": tier.weights,
        },
        "winner": {
            "provider": winner["provider"],
            "region": winner["region"],
            "runtimeCluster": winner["runtimeCluster"],
            "score": winner["score"],
        },
        "top3": top_3,
        "excluded": excluded,
    }

    return {
        "provider": winner["provider"],
        "region": winner["region"],
        "runtimeCluster": winner["runtimeCluster"],
        "network": winner["network"],
        "reason": reason,
        "reasonJSON": json.dumps(reason, separators=(",", ":"), sort_keys=True),
    }
