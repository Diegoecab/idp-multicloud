"""OCI GoldenGate CDC replication orchestrator for multi-cloud DR.

Manages the lifecycle of replication pairs between primary and secondary
databases across different cloud providers.  One GoldenGate deployment
per "replication pair" isolates blast radius and allows per-cell tuning.

Architecture per cell:
  1. Primary DB (writer) on the scheduler-chosen cloud
  2. Secondary DB (standby) on a different cloud
  3. OCI GoldenGate deployment with:
     - Extract: reads binlog from primary (ROW/FULL)
     - Replicat: applies changes to secondary
  4. Traffic failover via DNS/GSLB per cell

Tier patterns:
  C0 (low)               — warm_standby + GG continuous, semi-auto failover
  C1 (medium)            — pilot_light  + GG active, manual failover
  C2 (critical)          — backup_restore, GG optional
  business_critical      — active_active + GG cross-region, auto failover

Failover steps (C0/C1):
  1. Freeze writes (write fence via routing + read-only flag)
  2. Verify replication lag <= RPO target
  3. Promote secondary as new writer
  4. Update DNS/GSLB to point to secondary
  5. Scale secondary compute if pilot-light
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ReplicationState(str, Enum):
    PENDING = "PENDING"
    PROVISIONING_SECONDARY = "PROVISIONING_SECONDARY"
    CONFIGURING_GG = "CONFIGURING_GG"
    REPLICATING = "REPLICATING"
    LAG_WARNING = "LAG_WARNING"
    FAILOVER_IN_PROGRESS = "FAILOVER_IN_PROGRESS"
    FAILED_OVER = "FAILED_OVER"
    SUSPENDED = "SUSPENDED"
    ERROR = "ERROR"


class FailoverPhase(str, Enum):
    IDLE = "IDLE"
    FREEZE_WRITES = "FREEZE_WRITES"
    VERIFY_LAG = "VERIFY_LAG"
    PROMOTE_SECONDARY = "PROMOTE_SECONDARY"
    UPDATE_DNS = "UPDATE_DNS"
    SCALE_COMPUTE = "SCALE_COMPUTE"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


@dataclass
class GoldenGateConfig:
    """Configuration for a GoldenGate deployment."""
    deployment_name: str
    source_connection: dict  # {provider, region, endpoint, db_type, binlog_mode}
    target_connection: dict  # {provider, region, endpoint, db_type}
    extract_name: str = ""
    replicat_name: str = ""
    trail_name: str = ""
    settings: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.extract_name:
            self.extract_name = f"ext_{self.deployment_name[:8]}"
        if not self.replicat_name:
            self.replicat_name = f"rep_{self.deployment_name[:8]}"
        if not self.trail_name:
            self.trail_name = f"tr_{self.deployment_name[:8]}"


@dataclass
class ReplicationPair:
    """Represents a primary-secondary DB pair with GoldenGate replication."""
    id: Optional[int] = None
    cell: str = ""
    name: str = ""
    namespace: str = "default"
    product: str = "mysql"
    tier: str = "medium"

    # Primary placement
    primary_provider: str = ""
    primary_region: str = ""
    primary_cluster: str = ""
    primary_placement_id: Optional[int] = None

    # Secondary placement
    secondary_provider: str = ""
    secondary_region: str = ""
    secondary_cluster: str = ""
    secondary_placement_id: Optional[int] = None

    # GoldenGate
    gg_deployment_name: str = ""
    gg_config: dict = field(default_factory=dict)

    # State
    state: ReplicationState = ReplicationState.PENDING
    replication_lag_ms: float = 0
    last_lag_check: float = 0
    rpo_target_minutes: int = 15
    rto_target_minutes: int = 120
    failover_phase: FailoverPhase = FailoverPhase.IDLE
    dr_strategy: str = "pilot_light"

    created_at: float = 0
    updated_at: float = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cell": self.cell,
            "name": self.name,
            "namespace": self.namespace,
            "product": self.product,
            "tier": self.tier,
            "primary": {
                "provider": self.primary_provider,
                "region": self.primary_region,
                "cluster": self.primary_cluster,
                "placement_id": self.primary_placement_id,
            },
            "secondary": {
                "provider": self.secondary_provider,
                "region": self.secondary_region,
                "cluster": self.secondary_cluster,
                "placement_id": self.secondary_placement_id,
            },
            "goldengate": {
                "deployment_name": self.gg_deployment_name,
                "config": self.gg_config,
            },
            "state": self.state.value if isinstance(self.state, ReplicationState) else self.state,
            "replication_lag_ms": self.replication_lag_ms,
            "last_lag_check": self.last_lag_check,
            "rpo_target_minutes": self.rpo_target_minutes,
            "rto_target_minutes": self.rto_target_minutes,
            "failover_phase": self.failover_phase.value if isinstance(self.failover_phase, FailoverPhase) else self.failover_phase,
            "dr_strategy": self.dr_strategy,
            "lag_within_rpo": self.lag_within_rpo,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @property
    def lag_within_rpo(self) -> bool:
        """Check if current lag is within RPO target."""
        return self.replication_lag_ms <= (self.rpo_target_minutes * 60 * 1000)


# ── DR Strategy Resolution ──────────────────────────────────────────────────

# Map tier -> default DR strategy + whether GG is needed
TIER_DR_DEFAULTS = {
    "low": {
        "strategy": "warm_standby",
        "gg_required": True,
        "secondary_compute": "full",
        "auto_failover": False,
        "description": "Warm standby with continuous GG replication. Semi-auto failover with guardrails.",
    },
    "medium": {
        "strategy": "pilot_light",
        "gg_required": True,
        "secondary_compute": "minimal",
        "auto_failover": False,
        "description": "Pilot light with GG active. Manual failover via runbook + automation.",
    },
    "critical": {
        "strategy": "backup_restore",
        "gg_required": False,
        "secondary_compute": "none",
        "auto_failover": False,
        "description": "Backup/restore only. GG optional. Cost-optimized.",
    },
    "business_critical": {
        "strategy": "active_active",
        "gg_required": True,
        "secondary_compute": "full",
        "auto_failover": True,
        "description": "Active-active with cross-region GG replication. Automatic failover.",
    },
}


def resolve_dr_strategy(tier: str) -> dict:
    """Return the DR strategy for a given tier."""
    return TIER_DR_DEFAULTS.get(tier, TIER_DR_DEFAULTS["medium"])


def needs_replication(tier: str) -> bool:
    """Check if the tier requires GoldenGate replication."""
    return resolve_dr_strategy(tier).get("gg_required", False)


# ── GoldenGate Deployment Builder ────────────────────────────────────────────

def build_gg_config(pair: ReplicationPair) -> GoldenGateConfig:
    """Build a GoldenGate configuration for a replication pair.

    Source: primary DB (binlog ROW/FULL)
    Target: secondary DB
    """
    deployment_name = f"gg-{pair.cell}-{pair.name}"[:32]

    source_conn = {
        "provider": pair.primary_provider,
        "region": pair.primary_region,
        "db_type": "mysql",
        "binlog_mode": "ROW",
        "binlog_row_image": "FULL",
        "connection_type": _connection_type(pair.primary_provider),
        "endpoint": f"{pair.name}-primary.{pair.primary_region}.{pair.primary_provider}.internal",
    }

    target_conn = {
        "provider": pair.secondary_provider,
        "region": pair.secondary_region,
        "db_type": "mysql",
        "connection_type": _connection_type(pair.secondary_provider),
        "endpoint": f"{pair.name}-secondary.{pair.secondary_region}.{pair.secondary_provider}.internal",
    }

    return GoldenGateConfig(
        deployment_name=deployment_name,
        source_connection=source_conn,
        target_connection=target_conn,
        settings={
            "rpo_target_minutes": pair.rpo_target_minutes,
            "lag_alert_threshold_ms": pair.rpo_target_minutes * 60 * 1000 * 0.5,
            "monitoring_interval_seconds": 30,
            "network": _network_config(pair.primary_provider, pair.secondary_provider),
        },
    )


def _connection_type(provider: str) -> str:
    """Return the network connectivity type for cross-cloud GG connections."""
    return {
        "aws": "vpn_ipsec",
        "gcp": "cloud_interconnect",
        "oci": "fastconnect",
    }.get(provider, "vpn_ipsec")


def _network_config(primary_provider: str, secondary_provider: str) -> dict:
    """Build network connectivity configuration for GG between two clouds."""
    return {
        "primary_to_gg": _connection_type(primary_provider),
        "gg_to_secondary": _connection_type(secondary_provider),
        "gg_host": "oci",
        "description": (
            f"GoldenGate runs on OCI. "
            f"Extract connects to {primary_provider} primary via {_connection_type(primary_provider)}. "
            f"Replicat connects to {secondary_provider} secondary via {_connection_type(secondary_provider)}."
        ),
    }


# ── GoldenGate Resource Manifests ────────────────────────────────────────────

def build_gg_resources(config: GoldenGateConfig) -> dict:
    """Build the set of GoldenGate CRD resources for a deployment.

    Returns a dict of resources that would be applied to the cluster:
    - deployment: the GG deployment itself
    - source_connection: connection to the primary DB
    - target_connection: connection to the secondary DB
    - extract: CDC extract process
    - replicat: apply process on the target
    - monitoring: alerts and dashboards
    """
    return {
        "deployment": {
            "apiVersion": "goldengate.oci.oracle.com/v1",
            "kind": "GoldenGateDeployment",
            "metadata": {
                "name": config.deployment_name,
                "labels": {
                    "platform.example.org/component": "goldengate",
                    "platform.example.org/type": "mysql-replication",
                },
            },
            "spec": {
                "technology": "MySQL",
                "license_model": "BYOL",
                "subnet_id": "gg-private-subnet",
                "display_name": config.deployment_name,
            },
        },
        "source_connection": {
            "apiVersion": "goldengate.oci.oracle.com/v1",
            "kind": "GoldenGateConnection",
            "metadata": {"name": f"{config.deployment_name}-source"},
            "spec": {
                "technology_type": "MYSQL",
                "connection_type": config.source_connection["connection_type"],
                "host": config.source_connection["endpoint"],
                "port": 3306,
                "database_name": "primary",
                "security_protocol": "MTLS",
            },
        },
        "target_connection": {
            "apiVersion": "goldengate.oci.oracle.com/v1",
            "kind": "GoldenGateConnection",
            "metadata": {"name": f"{config.deployment_name}-target"},
            "spec": {
                "technology_type": "MYSQL",
                "connection_type": config.target_connection["connection_type"],
                "host": config.target_connection["endpoint"],
                "port": 3306,
                "database_name": "secondary",
                "security_protocol": "MTLS",
            },
        },
        "extract": {
            "name": config.extract_name,
            "type": "INTEGRATED_EXTRACT",
            "source_connection": f"{config.deployment_name}-source",
            "trail": config.trail_name,
            "parameters": {
                "TRANLOGOPTIONS": "INTEGRATEDPARAMS (MAX_SGA_SIZE 256)",
                "TABLE": "*.*",
                "GETTRUNCATES": True,
            },
        },
        "replicat": {
            "name": config.replicat_name,
            "type": "INTEGRATED_REPLICAT",
            "target_connection": f"{config.deployment_name}-target",
            "trail": config.trail_name,
            "parameters": {
                "MAP": "*.*, TARGET *.*",
                "HANDLECOLLISIONS": True,
            },
        },
        "monitoring": {
            "lag_alert_threshold_ms": config.settings.get("lag_alert_threshold_ms", 450000),
            "monitoring_interval_seconds": config.settings.get("monitoring_interval_seconds", 30),
            "alerts": [
                {
                    "name": f"{config.deployment_name}-lag-warning",
                    "condition": "lag_ms > rpo_target * 0.5",
                    "severity": "WARNING",
                },
                {
                    "name": f"{config.deployment_name}-lag-critical",
                    "condition": "lag_ms > rpo_target * 0.8",
                    "severity": "CRITICAL",
                },
                {
                    "name": f"{config.deployment_name}-extract-stopped",
                    "condition": "extract_status != RUNNING",
                    "severity": "CRITICAL",
                },
            ],
        },
    }


# ── Failover Orchestrator ────────────────────────────────────────────────────

class FailoverOrchestrator:
    """Execute a controlled failover for a replication pair.

    Follows the steps:
      1. FREEZE_WRITES     — set write fence (routing + read-only)
      2. VERIFY_LAG        — check replication lag <= RPO target
      3. PROMOTE_SECONDARY — promote secondary DB as new writer
      4. UPDATE_DNS        — switch DNS/GSLB to secondary
      5. SCALE_COMPUTE     — scale secondary compute if pilot-light
    """

    def __init__(self, pair: ReplicationPair):
        self.pair = pair
        self.steps_completed = []
        self.errors = []

    def execute(self) -> dict:
        """Run failover steps. Returns result dict."""
        logger.info(
            "Starting failover for %s/%s: %s/%s -> %s/%s",
            self.pair.namespace, self.pair.name,
            self.pair.primary_provider, self.pair.primary_region,
            self.pair.secondary_provider, self.pair.secondary_region,
        )

        steps = [
            ("FREEZE_WRITES", self._freeze_writes),
            ("VERIFY_LAG", self._verify_lag),
            ("PROMOTE_SECONDARY", self._promote_secondary),
            ("UPDATE_DNS", self._update_dns),
            ("SCALE_COMPUTE", self._scale_compute),
        ]

        for step_name, step_fn in steps:
            self.pair.failover_phase = FailoverPhase(step_name)
            try:
                step_fn()
                self.steps_completed.append(step_name)
            except Exception as e:
                self.errors.append({"step": step_name, "error": str(e)})
                logger.error("Failover step '%s' failed: %s", step_name, e)
                self.pair.failover_phase = FailoverPhase.ABORTED
                return self._build_result("aborted")

        self.pair.failover_phase = FailoverPhase.COMPLETED
        self.pair.state = ReplicationState.FAILED_OVER

        # Swap primary <-> secondary
        old_primary = {
            "provider": self.pair.primary_provider,
            "region": self.pair.primary_region,
            "cluster": self.pair.primary_cluster,
        }
        self.pair.primary_provider = self.pair.secondary_provider
        self.pair.primary_region = self.pair.secondary_region
        self.pair.primary_cluster = self.pair.secondary_cluster
        self.pair.secondary_provider = old_primary["provider"]
        self.pair.secondary_region = old_primary["region"]
        self.pair.secondary_cluster = old_primary["cluster"]

        logger.info(
            "Failover completed for %s/%s. New primary: %s/%s",
            self.pair.namespace, self.pair.name,
            self.pair.primary_provider, self.pair.primary_region,
        )

        return self._build_result("completed")

    def _freeze_writes(self):
        """Step 1: Freeze writes on the primary DB."""
        logger.info("Freezing writes on primary %s/%s",
                     self.pair.primary_provider, self.pair.primary_region)
        # In production: set read-only flag, update routing to reject writes

    def _verify_lag(self):
        """Step 2: Verify replication lag is within RPO target."""
        rpo_ms = self.pair.rpo_target_minutes * 60 * 1000
        current_lag = self.pair.replication_lag_ms

        if current_lag > rpo_ms:
            raise ValueError(
                f"Replication lag ({current_lag}ms) exceeds RPO target "
                f"({rpo_ms}ms / {self.pair.rpo_target_minutes}min). "
                "Cannot safely failover. Wait for lag to decrease."
            )
        logger.info("Lag check passed: %dms <= %dms RPO", current_lag, rpo_ms)

    def _promote_secondary(self):
        """Step 3: Promote secondary DB as the new writer."""
        logger.info("Promoting secondary %s/%s as writer",
                     self.pair.secondary_provider, self.pair.secondary_region)
        # In production: ALTER INSTANCE SET PERSIST read_only=OFF on secondary
        # Also: stop GG replicat, enable writer credentials

    def _update_dns(self):
        """Step 4: Update DNS/GSLB to point to secondary."""
        logger.info("Updating DNS/GSLB: %s -> %s/%s",
                     self.pair.name,
                     self.pair.secondary_provider, self.pair.secondary_region)
        # In production: update Route53/CloudDNS/OCI DNS GSLB record

    def _scale_compute(self):
        """Step 5: Scale secondary compute if it was pilot-light."""
        if self.pair.dr_strategy == "pilot_light":
            logger.info("Scaling up compute on secondary (pilot-light -> full)")
            # In production: scale up K8s deployment replicas / instance class
        else:
            logger.info("No compute scaling needed (strategy: %s)", self.pair.dr_strategy)

    def _build_result(self, status: str) -> dict:
        return {
            "status": status,
            "name": self.pair.name,
            "namespace": self.pair.namespace,
            "cell": self.pair.cell,
            "steps_completed": self.steps_completed,
            "errors": self.errors if self.errors else None,
            "previous_primary": {
                "provider": self.pair.secondary_provider if status == "completed" else self.pair.primary_provider,
                "region": self.pair.secondary_region if status == "completed" else self.pair.primary_region,
            },
            "new_primary": {
                "provider": self.pair.primary_provider,
                "region": self.pair.primary_region,
            },
            "failover_phase": self.pair.failover_phase.value,
            "dr_strategy": self.pair.dr_strategy,
        }
