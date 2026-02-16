"""Data types for the IDP Multicloud control plane."""

from dataclasses import dataclass, field
from typing import Optional


VALID_TIERS = ("low", "medium", "critical", "business_critical")
VALID_ENVIRONMENTS = ("dev", "staging", "production")
VALID_SIZES = ("small", "medium", "large", "xlarge")


@dataclass
class MySQLRequest:
    """Developer request for a managed MySQL instance.

    Developers specify ONLY cell, tier, environment, size, storageGB, and ha.
    The control plane decides provider, region, runtimeCluster, and network.
    """
    cell: str
    tier: str
    environment: str
    size: str
    storage_gb: int
    ha: bool
    namespace: str = "default"
    name: str = ""

    def validate(self) -> list:
        """Return a list of validation errors (empty if valid)."""
        errors = []
        if not self.cell:
            errors.append("cell is required")
        if not self.name:
            errors.append("name is required")
        if self.tier not in VALID_TIERS:
            errors.append(f"tier must be one of {VALID_TIERS}")
        if self.environment not in VALID_ENVIRONMENTS:
            errors.append(f"environment must be one of {VALID_ENVIRONMENTS}")
        if self.size not in VALID_SIZES:
            errors.append(f"size must be one of {VALID_SIZES}")
        if not isinstance(self.storage_gb, int) or self.storage_gb < 10 or self.storage_gb > 65536:
            errors.append("storageGB must be an integer between 10 and 65536")
        return errors


@dataclass
class Candidate:
    """A cloud provider/region candidate for placement."""
    provider: str
    region: str
    runtime_cluster: str
    network: dict
    capabilities: set
    scores: dict  # keys: latency, dr, maturity, cost — values: 0.0-1.0


@dataclass
class CandidateScore:
    """Score breakdown for a single candidate after gate and weight evaluation."""
    provider: str
    region: str
    runtime_cluster: str
    total_score: float
    subscores: dict
    passed_gates: bool
    gate_failures: list = field(default_factory=list)


@dataclass
class PlacementDecision:
    """Result of the scheduler's placement decision."""
    provider: str
    region: str
    runtime_cluster: str
    network: dict
    reason: dict  # Full JSON-serializable reason including top-3 breakdown
    sticky: bool = False
