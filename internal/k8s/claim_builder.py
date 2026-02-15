from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


GROUP = "db.platform.example.org"
VERSION = "v1alpha1"
KIND = "MySQLInstanceClaim"
PLURAL = "mysqlinstanceclaims"
FIELD_MANAGER = "idp-multicloud-controlplane"


class K8sClientError(Exception):
    pass


class CrossplaneCRDNotInstalledError(K8sClientError):
    pass


@dataclass
class ClaimRequest:
    namespace: str
    name: str
    cell: str
    environment: str
    tier: str
    size: str
    storage_gb: int
    ha: bool
    provider: str
    region: str
    network: Dict[str, Any]
    placement_reason_json: str


class KubernetesClaimClient:
    def __init__(self) -> None:
        try:
            from kubernetes import client, config
            from kubernetes.dynamic import DynamicClient
        except ImportError as exc:
            raise K8sClientError(
                "Missing python kubernetes client dependency. Install with: pip install kubernetes"
            ) from exc

        try:
            config.load_incluster_config()
        except Exception:
            try:
                config.load_kube_config()
            except Exception as exc:
                raise K8sClientError(f"Unable to load Kubernetes configuration: {exc}") from exc

        self._core = client.CoreV1Api()
        self._dynamic = DynamicClient(client.ApiClient())

    def _resource(self):
        try:
            return self._dynamic.resources.get(api_version=f"{GROUP}/{VERSION}", kind=KIND)
        except Exception as exc:
            if "could not find the requested resource" in str(exc).lower() or "404" in str(exc):
                raise CrossplaneCRDNotInstalledError(
                    f"CRD for {GROUP}/{VERSION}, kind {KIND} is not installed in the cluster."
                ) from exc
            raise K8sClientError(str(exc)) from exc

    def get_claim(self, namespace: str, name: str) -> Optional[Dict[str, Any]]:
        resource = self._resource()
        try:
            obj = resource.get(name=name, namespace=namespace)
            return obj.to_dict()
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "404" in msg:
                return None
            raise K8sClientError(str(exc)) from exc

    def apply_claim(self, req: ClaimRequest) -> Dict[str, Any]:
        resource = self._resource()

        manifest: Dict[str, Any] = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": KIND,
            "metadata": {
                "name": req.name,
                "namespace": req.namespace,
                "labels": {
                    "platform.example.org/cell": req.cell,
                    "platform.example.org/environment": req.environment,
                    "platform.example.org/tier": req.tier,
                },
                "annotations": {
                    "platform.example.org/placement-reason": req.placement_reason_json,
                },
            },
            "spec": {
                "parameters": {
                    "cell": req.cell,
                    "environment": req.environment,
                    "tier": req.tier,
                    "provider": req.provider,
                    "region": req.region,
                    "size": req.size,
                    "storageGB": req.storage_gb,
                    "ha": req.ha,
                    "network": req.network,
                },
                "compositionSelector": {
                    "matchLabels": {
                        "db.platform.example.org/provider": req.provider,
                        "db.platform.example.org/class": "mysql",
                    }
                },
                "writeConnectionSecretToRef": {"name": f"{req.name}-conn"},
            },
        }

        try:
            created = resource.patch(
                namespace=req.namespace,
                name=req.name,
                body=manifest,
                content_type="application/apply-patch+yaml",
                field_manager=FIELD_MANAGER,
                force=True,
            )
            return created.to_dict()
        except Exception as exc:
            raise K8sClientError(f"Failed to apply claim using server-side apply: {exc}") from exc

    def connection_secret_exists(self, namespace: str, name: str, claim: Optional[Dict[str, Any]] = None) -> bool:
        secret_name = f"{name}-conn"
        if claim:
            secret_name = (
                claim.get("spec", {})
                .get("writeConnectionSecretToRef", {})
                .get("name", secret_name)
            )
        try:
            self._core.read_namespaced_secret(namespace=namespace, name=secret_name)
            return True
        except Exception as exc:
            if "not found" in str(exc).lower() or "404" in str(exc):
                return False
            raise K8sClientError(f"Failed to check connection secret existence: {exc}") from exc
