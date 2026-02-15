from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class TierRequirements:
    rto_minutes: int
    rpo_minutes: int
    requires_pitr: bool
    requires_multiaz: bool
    requires_private_networking: bool
    weights: Dict[str, float]


@dataclass(frozen=True)
class Candidate:
    provider: str
    region: str
    runtime_cluster: str
    network: Dict[str, Any]
    capabilities: Dict[str, bool]
    scores: Dict[str, float]

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.region}:{self.runtime_cluster}"
