"""Data types for the IDP Multicloud control plane."""

from dataclasses import dataclass, field
from typing import Optional
import time


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
    healthy: bool = True  # dynamic health status — unhealthy candidates are skipped


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
    failover: Optional[dict] = None  # secondary placement for DR (different cloud)


# ── Circuit Breaker ──────────────────────────────────────────────────────────

@dataclass
class ServiceRequest:
    """Generic service provisioning request.

    Contains only the fields the scheduler needs. Product-specific
    parameters (size, storageGB, cpu, memory, etc.) are validated
    by the product registry and passed directly to the claim builder.
    """
    product: str
    cell: str
    tier: str
    environment: str
    ha: bool
    namespace: str = "default"
    name: str = ""

    def validate(self) -> list:
        """Return a list of validation errors (empty if valid)."""
        errors = []
        if not self.product:
            errors.append("product is required")
        if not self.cell:
            errors.append("cell is required")
        if not self.name:
            errors.append("name is required")
        if self.tier not in VALID_TIERS:
            errors.append(f"tier must be one of {VALID_TIERS}")
        if self.environment not in VALID_ENVIRONMENTS:
            errors.append(f"environment must be one of {VALID_ENVIRONMENTS}")
        return errors


# ── Circuit Breaker ──────────────────────────────────────────────────────────

class CircuitBreaker:
    """Per-provider circuit breaker: tracks failures and opens to prevent cascading calls.

    States:
      CLOSED  — normal operation, requests pass through
      OPEN    — too many failures, requests are blocked for a cooldown period
      HALF_OPEN — after cooldown, one probe request is allowed to test recovery
    """
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 60):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failure_count: int = 0
        self._state: str = self.CLOSED
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if time.time() - self._last_failure_time >= self.cooldown_seconds:
                self._state = self.HALF_OPEN
        return self._state

    def record_success(self):
        self._failure_count = 0
        self._state = self.CLOSED

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            self._state = self.OPEN

    def allow_request(self) -> bool:
        s = self.state
        if s == self.CLOSED:
            return True
        if s == self.HALF_OPEN:
            return True  # allow one probe
        return False

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
        }
