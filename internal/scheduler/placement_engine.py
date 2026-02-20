from dataclasses import dataclass

from internal.policy.data import policy_store


@dataclass
class PlacementPlan:
    primary: dict
    secondary: dict
    reason: dict


_STICKY: dict[str, PlacementPlan] = {}


def _score(candidate: dict, weights: dict, capabilities: dict) -> tuple[float, dict]:
    provider = candidate["provider"]
    cap = capabilities.get(provider, {})
    maturity = cap.get("maturity", 0.5)
    cost = cap.get("cost", 0.5)
    # deterministic heuristic for demo purposes
    latency = 0.9 if "us" in candidate["region"] else 0.7
    dr = 0.9 if provider in {"aws", "gcp"} else 0.7
    subs = {"latency": latency, "dr": dr, "maturity": maturity, "cost": cost}
    total = sum(subs[k] * weights.get(k, 0) for k in subs)
    return round(total, 4), subs


def schedule(cell: str, env: str, tier: str, dr_profile: str | None, sticky_key: str) -> PlacementPlan:
    if sticky_key in _STICKY:
        return _STICKY[sticky_key]

    policy = policy_store.load()
    tier_cfg = policy["tiers"][tier]
    candidates = policy["cells"][cell]["candidates"]
    capabilities = policy["capabilities"]
    weights = tier_cfg["weights"]

    scored = []
    for c in candidates:
        total, subs = _score(c, weights, capabilities)
        scored.append((total, subs, c))
    scored.sort(key=lambda x: x[0], reverse=True)

    primary = scored[0][2]
    secondary = None
    if tier_cfg["gates"].get("crossCloudSecondary"):
        for _, _, c in scored[1:]:
            if c["provider"] != primary["provider"]:
                secondary = c
                break
    if not secondary:
        secondary = scored[1][2] if len(scored) > 1 else primary

    reason = {
        "tier": tier,
        "drProfile": dr_profile or tier_cfg["defaults"]["drProfile"],
        "ranking": [
            {"provider": c["provider"], "region": c["region"], "score": s, "subscores": subs}
            for s, subs, c in scored[:3]
        ],
        "policyGates": tier_cfg["gates"],
        "sticky": False,
    }
    plan = PlacementPlan(primary=primary, secondary=secondary, reason=reason)
    _STICKY[sticky_key] = plan
    return plan
