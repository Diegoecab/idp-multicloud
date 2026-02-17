"""Experimentation engine: A/B testing, feature flags, and canary rollouts
for placement strategies.

Inspired by Mercado Libre's culture of continuous experimentation:
  - Every placement decision can be part of an experiment
  - Small changes in scoring weights are tested on a % of traffic
  - Results are tracked to measure real impact before full rollout
  - Feature flags allow iterating on policies without redeploying

Key concepts:
  - Experiment: an A/B test comparing a control (current weights) vs variant (new weights)
  - Feature flag: a boolean toggle to enable/disable a placement policy
  - Canary: a gradual rollout where a % of traffic uses a new strategy
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Experiment:
    """An A/B experiment on placement scoring weights.

    Traffic is split deterministically using a hash of (experiment_id, request_name)
    so the same request always lands in the same group.
    """
    id: str
    description: str
    variant_weights: dict          # the new weights to test
    traffic_percentage: float      # 0.0–1.0 — fraction of traffic that gets the variant
    tier: str                      # which tier this experiment applies to ("*" = all)
    enabled: bool = True
    created_at: float = field(default_factory=time.time)

    def assign_group(self, request_name: str) -> str:
        """Deterministically assign a request to 'control' or 'variant'."""
        digest = hashlib.md5(f"{self.id}:{request_name}".encode()).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        if bucket < self.traffic_percentage:
            return "variant"
        return "control"


# ── Experiment Registry ──────────────────────────────────────────────────────

_experiments: dict[str, Experiment] = {}


def create_experiment(
    experiment_id: str,
    description: str,
    variant_weights: dict,
    traffic_percentage: float,
    tier: str = "*",
) -> Experiment:
    """Create and register a new A/B experiment."""
    if traffic_percentage < 0.0 or traffic_percentage > 1.0:
        raise ValueError("traffic_percentage must be between 0.0 and 1.0")
    weight_sum = sum(variant_weights.values())
    if abs(weight_sum - 1.0) > 0.01:
        raise ValueError(f"Variant weights must sum to 1.0 (got {weight_sum})")

    exp = Experiment(
        id=experiment_id,
        description=description,
        variant_weights=variant_weights,
        traffic_percentage=traffic_percentage,
        tier=tier,
    )
    _experiments[experiment_id] = exp
    return exp


def get_experiment(experiment_id: str) -> Optional[Experiment]:
    return _experiments.get(experiment_id)


def list_experiments() -> list[dict]:
    return [
        {
            "id": e.id,
            "description": e.description,
            "variant_weights": e.variant_weights,
            "traffic_percentage": e.traffic_percentage,
            "tier": e.tier,
            "enabled": e.enabled,
        }
        for e in _experiments.values()
    ]


def delete_experiment(experiment_id: str) -> bool:
    return _experiments.pop(experiment_id, None) is not None


def resolve_weights(tier_name: str, tier_weights: dict, request_name: str) -> tuple[dict, Optional[dict]]:
    """Resolve final weights for a request, applying any active experiment.

    Returns:
        (effective_weights, experiment_info) — experiment_info is None if no experiment matched.
    """
    for exp in _experiments.values():
        if not exp.enabled:
            continue
        if exp.tier != "*" and exp.tier != tier_name:
            continue
        group = exp.assign_group(request_name)
        experiment_info = {
            "experiment_id": exp.id,
            "group": group,
            "description": exp.description,
        }
        if group == "variant":
            return exp.variant_weights, experiment_info
        return tier_weights, experiment_info
    return tier_weights, None


# ── Feature Flags ────────────────────────────────────────────────────────────

_feature_flags: dict[str, bool] = {}


def set_feature_flag(name: str, enabled: bool):
    """Set a feature flag."""
    _feature_flags[name] = enabled


def get_feature_flag(name: str, default: bool = False) -> bool:
    """Get a feature flag value (defaults to False if unset)."""
    return _feature_flags.get(name, default)


def list_feature_flags() -> dict[str, bool]:
    """Return all feature flags."""
    return dict(_feature_flags)


def delete_feature_flag(name: str) -> bool:
    return _feature_flags.pop(name, None) is not None


# ── Placement Analytics ──────────────────────────────────────────────────────

class PlacementAnalytics:
    """Tracks placement decisions for data-driven optimization.

    Collects metrics on:
      - Provider/region win rates
      - Tier distribution
      - Gate rejection rates
      - Experiment group distribution
      - Average scores per provider
    """

    def __init__(self):
        self._placements: list[dict] = []
        self._gate_rejections: int = 0
        self._total_requests: int = 0

    def record_placement(self, placement_event: dict):
        """Record a placement decision."""
        self._placements.append({
            **placement_event,
            "timestamp": time.time(),
        })
        self._total_requests += 1

    def record_gate_rejection(self):
        """Record a gate rejection (no candidates passed)."""
        self._gate_rejections += 1
        self._total_requests += 1

    def get_summary(self) -> dict:
        """Return analytics summary for data-driven decisions."""
        if not self._placements:
            return {
                "total_placements": 0,
                "total_requests": self._total_requests,
                "gate_rejection_rate": 0.0,
            }

        # Provider distribution
        provider_counts: dict[str, int] = {}
        region_counts: dict[str, int] = {}
        tier_counts: dict[str, int] = {}
        experiment_groups: dict[str, dict[str, int]] = {}
        provider_scores: dict[str, list[float]] = {}

        for p in self._placements:
            provider = p.get("provider", "unknown")
            region = p.get("region", "unknown")
            tier = p.get("tier", "unknown")

            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            region_counts[f"{provider}/{region}"] = region_counts.get(f"{provider}/{region}", 0) + 1
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

            score = p.get("total_score", 0)
            if provider not in provider_scores:
                provider_scores[provider] = []
            provider_scores[provider].append(score)

            exp = p.get("experiment")
            if exp:
                exp_id = exp["experiment_id"]
                group = exp["group"]
                if exp_id not in experiment_groups:
                    experiment_groups[exp_id] = {"control": 0, "variant": 0}
                experiment_groups[exp_id][group] = experiment_groups[exp_id].get(group, 0) + 1

        total = len(self._placements)
        avg_scores = {
            p: round(sum(scores) / len(scores), 4)
            for p, scores in provider_scores.items()
        }

        return {
            "total_placements": total,
            "total_requests": self._total_requests,
            "gate_rejection_rate": round(self._gate_rejections / max(self._total_requests, 1), 4),
            "provider_distribution": {
                p: {"count": c, "percentage": round(c / total * 100, 1)}
                for p, c in sorted(provider_counts.items(), key=lambda x: -x[1])
            },
            "region_distribution": {
                r: {"count": c, "percentage": round(c / total * 100, 1)}
                for r, c in sorted(region_counts.items(), key=lambda x: -x[1])
            },
            "tier_distribution": {
                t: {"count": c, "percentage": round(c / total * 100, 1)}
                for t, c in sorted(tier_counts.items(), key=lambda x: -x[1])
            },
            "avg_score_by_provider": avg_scores,
            "experiments": experiment_groups,
        }

    def reset(self):
        """Reset all analytics counters."""
        self._placements.clear()
        self._gate_rejections = 0
        self._total_requests = 0


# Global analytics instance
analytics = PlacementAnalytics()
