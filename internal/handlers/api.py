from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Dict, Optional, Tuple

from internal.k8s.claim_builder import (
    ClaimRequest,
    CrossplaneCRDNotInstalledError,
    K8sClientError,
    KubernetesClaimClient,
)
from internal.scheduler.scheduler import SchedulingError, schedule_mysql


VALID_TIERS = {"C0", "C1", "C2"}
VALID_ENVS = {"dev", "staging", "prod"}
VALID_SIZES = {"small", "medium", "large"}
K8S_NAME_REGEX = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


@dataclass
class APIResponse:
    status: int
    payload: Dict[str, Any]


class ControlPlaneAPI:
    def __init__(self, k8s_client: KubernetesClaimClient):
        self.k8s = k8s_client

    def health(self) -> APIResponse:
        return APIResponse(status=HTTPStatus.OK, payload={"status": "ok", "service": "idp-multicloud-control-plane"})

    def create_mysql(self, body: Dict[str, Any]) -> APIResponse:
        error = self._validate_create_input(body)
        if error:
            return APIResponse(status=HTTPStatus.BAD_REQUEST, payload={"error": error})

        namespace = body["namespace"]
        name = body["name"]

        try:
            existing = self.k8s.get_claim(namespace=namespace, name=name)
            if existing:
                placement_reason = (
                    existing.get("metadata", {})
                    .get("annotations", {})
                    .get("platform.example.org/placement-reason", "{}")
                )
                spec_params = existing.get("spec", {}).get("parameters", {})
                return APIResponse(
                    status=HTTPStatus.OK,
                    payload={
                        "sticky": True,
                        "message": "Claim already exists. Placement is sticky and was not rescheduled.",
                        "placement": {
                            "provider": spec_params.get("provider"),
                            "region": spec_params.get("region"),
                            "runtimeCluster": self._runtime_from_reason(placement_reason),
                        },
                        "placementReason": self._safe_json(placement_reason),
                    },
                )

            decision = schedule_mysql(cell=body["cell"], tier_name=body["tier"], ha=body["ha"])
            req = ClaimRequest(
                namespace=namespace,
                name=name,
                cell=body["cell"],
                environment=body["environment"],
                tier=body["tier"],
                size=body["size"],
                storage_gb=body["storageGB"],
                ha=body["ha"],
                provider=decision["provider"],
                region=decision["region"],
                network=decision["network"],
                placement_reason_json=decision["reasonJSON"],
            )
            self.k8s.apply_claim(req)
            return APIResponse(
                status=HTTPStatus.CREATED,
                payload={
                    "sticky": False,
                    "message": "Claim created.",
                    "placement": {
                        "provider": decision["provider"],
                        "region": decision["region"],
                        "runtimeCluster": decision["runtimeCluster"],
                    },
                    "placementReason": decision["reason"],
                },
            )
        except SchedulingError as exc:
            return APIResponse(status=HTTPStatus.BAD_REQUEST, payload={"error": str(exc)})
        except CrossplaneCRDNotInstalledError as exc:
            return APIResponse(status=HTTPStatus.FAILED_DEPENDENCY, payload={"error": str(exc)})
        except K8sClientError as exc:
            return APIResponse(status=HTTPStatus.BAD_GATEWAY, payload={"error": str(exc)})

    def mysql_status(self, namespace: str, name: str) -> APIResponse:
        try:
            claim = self.k8s.get_claim(namespace=namespace, name=name)
            if not claim:
                return APIResponse(status=HTTPStatus.NOT_FOUND, payload={"error": "Claim not found."})
            has_secret = self.k8s.connection_secret_exists(namespace=namespace, name=name, claim=claim)
            return APIResponse(
                status=HTTPStatus.OK,
                payload={
                    "claim": claim,
                    "connectionSecret": {"exists": has_secret},
                },
            )
        except CrossplaneCRDNotInstalledError as exc:
            return APIResponse(status=HTTPStatus.FAILED_DEPENDENCY, payload={"error": str(exc)})
        except K8sClientError as exc:
            return APIResponse(status=HTTPStatus.BAD_GATEWAY, payload={"error": str(exc)})

    def _validate_create_input(self, body: Dict[str, Any]) -> Optional[str]:
        required = ["namespace", "name", "cell", "tier", "environment", "size", "storageGB", "ha"]
        missing = [field for field in required if field not in body]
        if missing:
            return f"Missing required fields: {', '.join(missing)}"

        if any(k in body for k in ["provider", "region", "runtimeCluster", "network"]):
            return "Developer contract violation: provider, region, runtimeCluster, and network are control-plane managed fields."

        if body["tier"] not in VALID_TIERS:
            return "tier must be one of C0, C1, C2"
        if body["environment"] not in VALID_ENVS:
            return "environment must be one of dev, staging, prod"
        if body["size"] not in VALID_SIZES:
            return "size must be one of small, medium, large"
        if not isinstance(body["storageGB"], int) or body["storageGB"] < 10:
            return "storageGB must be an integer >= 10"
        if not isinstance(body["ha"], bool):
            return "ha must be a boolean"
        if not isinstance(body["name"], str) or not K8S_NAME_REGEX.match(body["name"]):
            return "name must be a valid Kubernetes resource name"
        if not isinstance(body["namespace"], str) or not K8S_NAME_REGEX.match(body["namespace"]):
            return "namespace must be a valid Kubernetes namespace"

        return None

    @staticmethod
    def _safe_json(raw: str) -> Dict[str, Any]:
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw}

    @staticmethod
    def _runtime_from_reason(placement_reason: str) -> Optional[str]:
        parsed = ControlPlaneAPI._safe_json(placement_reason)
        return parsed.get("winner", {}).get("runtimeCluster")
