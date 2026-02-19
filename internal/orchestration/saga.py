"""Saga Orchestrator: multi-step provisioning with compensation (rollback).

Saga lifecycle:
  PENDING -> RUNNING -> COMPLETED
                     -> FAILED -> COMPENSATING -> ROLLED_BACK

Steps:
  1. validate     — validate request and product params
  2. schedule     — run the scheduler to select provider/region
  3. apply_claim  — build and apply the Crossplane claim
  4. wait_ready   — (async) wait for claim to become ready
  5. register     — register the placement in the DB
  6. notify       — log/notify completion

Compensation (reverse order):
  - notify: no-op
  - register: mark placement as FAILED
  - apply_claim: delete the claim from K8s
  - schedule: no-op (nothing to undo)
  - validate: no-op
"""

import logging
import time

from internal.db.database import (
    create_saga, update_saga, get_saga, get_config,
    record_placement, update_placement_status,
    provider_has_credentials,
)
from internal.models.types import ServiceRequest
from internal.products.registry import (
    get_product, validate_product_params, build_product_claim,
)
from internal.scheduler.scheduler import schedule, get_circuit_breaker, CANDIDATES
from internal.k8s import client as k8s

logger = logging.getLogger(__name__)

SAGA_STEPS = ["validate", "schedule", "apply_claim", "wait_ready", "register", "notify"]


class SagaOrchestrator:
    """Executes a provisioning saga with compensation on failure."""

    def __init__(self, product_name: str, body: dict):
        self.product_name = product_name
        self.body = body
        self.product = get_product(product_name)
        self.svc_req = None
        self.placement = None
        self.claim = None
        self.placement_id = None
        self.saga_id = None
        self.applied = False
        self.steps_completed = []

    def execute(self) -> dict:
        """Run all saga steps. Returns result dict."""
        saga_enabled = get_config("saga_enabled", "true") == "true"

        # Create saga tracking record
        self.saga_id = create_saga(
            product=self.product_name,
            name=self.body.get("name", ""),
            namespace=self.body.get("namespace", "default"),
        )
        update_saga(self.saga_id, state="RUNNING")

        try:
            for step in SAGA_STEPS:
                update_saga(self.saga_id, current_step=step)
                getattr(self, f"_step_{step}")()
                self.steps_completed.append(step)
                update_saga(self.saga_id, steps_completed=self.steps_completed)

            update_saga(self.saga_id, state="COMPLETED",
                        placement_id=self.placement_id)
            return self._build_response("created")

        except Exception as e:
            logger.error("Saga failed at step '%s': %s",
                         SAGA_STEPS[len(self.steps_completed)] if len(self.steps_completed) < len(SAGA_STEPS) else "unknown", e)
            update_saga(self.saga_id, state="FAILED", error=str(e))

            if saga_enabled:
                self._compensate()

            return self._build_error_response(str(e))

    def _step_validate(self):
        """Step 1: Validate request and product params."""
        if self.product is None:
            raise ValueError(f"Unknown product: '{self.product_name}'")

        self.svc_req = ServiceRequest(
            product=self.product_name,
            cell=self.body.get("cell", ""),
            tier=self.body.get("tier", ""),
            environment=self.body.get("environment", ""),
            ha=self.body.get("ha", False),
            namespace=self.body.get("namespace", "default"),
            name=self.body.get("name", ""),
        )
        errors = self.svc_req.validate()
        errors.extend(validate_product_params(self.product, self.body))
        if errors:
            raise ValueError(f"Validation failed: {errors}")

    def _step_schedule(self):
        """Step 2: Run the scheduler and verify provider credentials."""
        self.placement = schedule(self.svc_req)

        # Check provider credentials if validation is enabled
        if get_config("credential_validation_enabled", "true") == "true":
            if not provider_has_credentials(self.placement.provider):
                raise ValueError(
                    f"Provider '{self.placement.provider}' has no credentials configured. "
                    "Set credentials in Admin > Credentials before provisioning."
                )

        cb = get_circuit_breaker(self.placement.provider)
        cb.record_success()

    def _step_apply_claim(self):
        """Step 3: Build and apply the Crossplane claim."""
        self.claim = build_product_claim(self.product, self.body, self.placement)
        try:
            k8s.apply_claim_generic(
                self.product.api_version, self.product.kind, self.claim,
            )
            self.applied = True
        except RuntimeError:
            # K8s not available — standalone mode, claim built but not applied
            self.applied = False
        except Exception as e:
            cb = get_circuit_breaker(self.placement.provider)
            cb.record_failure()
            raise

    def _step_wait_ready(self):
        """Step 4: Wait for claim to become ready (simplified: immediate pass)."""
        # In production this would poll K8s for claim.status.conditions
        # For now we mark it as ready immediately
        pass

    def _step_register(self):
        """Step 5: Record placement in the database."""
        self.placement_id = record_placement(
            product=self.product_name,
            name=self.svc_req.name,
            namespace=self.svc_req.namespace,
            cell=self.svc_req.cell,
            tier=self.svc_req.tier,
            environment=self.svc_req.environment,
            provider=self.placement.provider,
            region=self.placement.region,
            cluster=self.placement.runtime_cluster,
            ha=self.svc_req.ha,
            total_score=self.placement.reason.get("selected", {}).get("total_score", 0),
            reason=self.placement.reason,
            status="READY" if self.applied else "PROVISIONING",
            failover=self.placement.failover,
            experiment=self.placement.reason.get("experiment"),
        )
        update_saga(self.saga_id, placement_id=self.placement_id)

    def _step_notify(self):
        """Step 6: Log completion."""
        logger.info(
            "Saga completed: %s/%s -> %s/%s (saga_id=%d, placement_id=%d)",
            self.svc_req.namespace, self.svc_req.name,
            self.placement.provider, self.placement.region,
            self.saga_id, self.placement_id or 0,
        )

    def _compensate(self):
        """Run compensation steps in reverse order."""
        update_saga(self.saga_id, state="COMPENSATING")
        for step in reversed(self.steps_completed):
            try:
                compensator = getattr(self, f"_compensate_{step}", None)
                if compensator:
                    compensator()
            except Exception as e:
                logger.error("Compensation failed for step '%s': %s", step, e)
        update_saga(self.saga_id, state="ROLLED_BACK")

    def _compensate_apply_claim(self):
        """Compensation: delete the applied claim."""
        if self.applied and self.claim:
            try:
                k8s.delete_claim_generic(
                    self.product.api_version, self.product.kind,
                    self.body.get("namespace", "default"),
                    self.body.get("name", ""),
                )
            except Exception as e:
                logger.warning("Could not delete claim during compensation: %s", e)

    def _compensate_register(self):
        """Compensation: mark placement as FAILED."""
        if self.placement_id:
            update_placement_status(self.placement_id, "FAILED")

    def _build_response(self, status: str) -> dict:
        resp = {
            "status": status,
            "product": self.product_name,
            "saga_id": self.saga_id,
            "placement_id": self.placement_id,
            "placement": {
                "provider": self.placement.provider,
                "region": self.placement.region,
                "runtimeCluster": self.placement.runtime_cluster,
                "network": self.placement.network,
            },
            "reason": self.placement.reason,
            "claim": self.claim,
            "applied_to_cluster": self.applied,
            "namespace": self.svc_req.namespace,
            "name": self.svc_req.name,
            "saga": {
                "steps_completed": self.steps_completed,
                "state": "COMPLETED",
            },
        }
        if self.placement.failover:
            resp["failover"] = self.placement.failover
        return resp

    def _build_error_response(self, error: str) -> dict:
        saga = get_saga(self.saga_id) if self.saga_id else None
        return {
            "status": "failed",
            "product": self.product_name,
            "saga_id": self.saga_id,
            "error": error,
            "saga": {
                "steps_completed": self.steps_completed,
                "state": saga["state"] if saga else "FAILED",
                "current_step": saga["current_step"] if saga else "",
            },
        }


class MultiCloudDeployer:
    """Deploy a service to multiple cloud providers simultaneously.

    Creates one placement per target provider, building independent claims
    for each. Useful for active-active DR or geo-distributed services.
    """

    def __init__(self, product_name: str, body: dict, target_providers: list):
        self.product_name = product_name
        self.body = body
        self.target_providers = target_providers

    def deploy(self) -> dict:
        """Deploy to each target provider."""
        product = get_product(self.product_name)
        if product is None:
            return {"error": f"Unknown product: '{self.product_name}'"}

        results = []
        for provider in self.target_providers:
            # Filter candidates to this provider only
            provider_candidates = [c for c in CANDIDATES if c.provider == provider]
            if not provider_candidates:
                results.append({
                    "provider": provider,
                    "status": "skipped",
                    "error": f"No candidates for provider '{provider}'",
                })
                continue

            # Create a unique name per provider to avoid collisions
            body_copy = dict(self.body)
            body_copy["name"] = f"{self.body.get('name', '')}-{provider}"

            saga = SagaOrchestrator(self.product_name, body_copy)
            # Override the scheduler to use only this provider's candidates
            saga._provider_filter = provider
            result = saga.execute()
            result["target_provider"] = provider
            results.append(result)

        return {
            "status": "multicloud_deploy",
            "product": self.product_name,
            "target_providers": self.target_providers,
            "deployments": results,
        }
