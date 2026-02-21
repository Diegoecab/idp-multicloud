"""Kubernetes dynamic client for Crossplane custom resources.

Handles:
  - Cluster connectivity (in-cluster or kubeconfig)
  - MySQLInstanceClaim CRUD via the dynamic client
  - Server-side apply (SSA) with fallback to create/update
  - Connection Secret existence check (without exposing values)

The system runs gracefully even when Kubernetes is not reachable.
"""

import json
import logging

logger = logging.getLogger(__name__)

CRD_GROUP = "db.platform.example.org"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "mysqlinstanceclaims"

_k8s_available = False
_api_client = None
_dynamic_client = None

# Try to import kubernetes; if not installed, k8s features are disabled.
try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.dynamic import DynamicClient
    _k8s_available = True
except ImportError:
    _k8s_available = False


def init_client() -> bool:
    """Initialize the Kubernetes client. Returns True if successful."""
    global _api_client, _dynamic_client

    if not _k8s_available:
        logger.warning(
            "kubernetes Python package is not installed. "
            "K8s operations will be unavailable. Install with: pip install kubernetes"
        )
        return False

    try:
        k8s_config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration")
    except k8s_config.ConfigException:
        try:
            k8s_config.load_kube_config()
            logger.info("Loaded kubeconfig from default location")
        except k8s_config.ConfigException:
            logger.warning(
                "No Kubernetes configuration found (neither in-cluster nor kubeconfig). "
                "K8s operations will be unavailable."
            )
            return False

    try:
        _api_client = k8s_client.ApiClient()
        _dynamic_client = DynamicClient(_api_client)
    except Exception as e:
        logger.warning(
            "Kubernetes client initialized config but DynamicClient failed: %s. "
            "K8s operations will be unavailable.", e
        )
        return False

    return True


def _get_resource():
    """Get the dynamic resource handle for MySQLInstanceClaim."""
    if _dynamic_client is None:
        return None
    try:
        return _dynamic_client.resources.get(
            api_version=f"{CRD_GROUP}/{CRD_VERSION}",
            kind="MySQLInstanceClaim",
        )
    except Exception as e:
        logger.error("Failed to discover MySQLInstanceClaim CRD: %s", e)
        return None


def get_claim(namespace: str, name: str):
    """Fetch an existing MySQLInstanceClaim. Returns None if not found.

    Raises:
        RuntimeError: If Kubernetes is not configured.
    """
    resource = _get_resource()
    if resource is None:
        if not _k8s_available or _dynamic_client is None:
            raise RuntimeError(
                "Kubernetes client is not available. Cannot check for existing claims."
            )
        raise RuntimeError(
            "MySQLInstanceClaim CRD is not installed in the cluster."
        )

    try:
        obj = resource.get(name=name, namespace=namespace)
        return obj.to_dict()
    except Exception as e:
        if hasattr(e, "status") and e.status == 404:
            return None
        logger.error("Error fetching claim %s/%s: %s", namespace, name, e)
        raise


def apply_claim(manifest: dict) -> dict:
    """Apply a MySQLInstanceClaim using server-side apply (SSA).

    Falls back to create/update if SSA is not supported by the client version.

    Raises:
        RuntimeError: If the CRD is not installed or Kubernetes is unreachable.
    """
    resource = _get_resource()
    if resource is None:
        raise RuntimeError(
            "Cannot apply claim: Crossplane MySQLInstanceClaim CRD is not installed "
            "or Kubernetes is not reachable. Install the CRD and ensure cluster connectivity."
        )

    namespace = manifest["metadata"]["namespace"]

    # Attempt SSA first
    try:
        result = resource.server_side_apply(
            body=manifest,
            namespace=namespace,
            field_manager="idp-controlplane",
        )
        return result.to_dict()
    except AttributeError:
        # Older kubernetes client without server_side_apply — use create/update fallback
        pass

    name = manifest["metadata"]["name"]
    try:
        existing = resource.get(name=name, namespace=namespace)
        manifest["metadata"]["resourceVersion"] = existing.metadata.resourceVersion
        result = resource.replace(body=manifest, namespace=namespace)
    except Exception as e:
        if hasattr(e, "status") and e.status == 404:
            result = resource.create(body=manifest, namespace=namespace)
        else:
            raise

    return result.to_dict()


def delete_claim(namespace: str, name: str):
    """Delete an existing MySQLInstanceClaim (used during forced failover).

    Raises:
        RuntimeError: If Kubernetes is not configured or CRD is missing.
    """
    resource = _get_resource()
    if resource is None:
        raise RuntimeError(
            "Cannot delete claim: Crossplane MySQLInstanceClaim CRD is not installed "
            "or Kubernetes is not reachable."
        )

    try:
        resource.delete(name=name, namespace=namespace)
    except Exception as e:
        if hasattr(e, "status") and e.status == 404:
            return  # already gone
        raise


# ── Generic CRD operations (multi-product) ──────────────────────────────────


def _get_resource_for(api_version: str, kind: str):
    """Get a dynamic resource handle for any CRD by apiVersion and kind."""
    if _dynamic_client is None:
        return None
    try:
        return _dynamic_client.resources.get(api_version=api_version, kind=kind)
    except Exception as e:
        logger.error("Failed to discover CRD %s/%s: %s", api_version, kind, e)
        return None


def get_claim_generic(api_version: str, kind: str, namespace: str, name: str):
    """Fetch an existing claim of any product type. Returns None if not found."""
    resource = _get_resource_for(api_version, kind)
    if resource is None:
        if not _k8s_available or _dynamic_client is None:
            raise RuntimeError(
                "Kubernetes client is not available. Cannot check for existing claims."
            )
        raise RuntimeError(f"{kind} CRD is not installed in the cluster.")

    try:
        obj = resource.get(name=name, namespace=namespace)
        return obj.to_dict()
    except Exception as e:
        if hasattr(e, "status") and e.status == 404:
            return None
        logger.error("Error fetching claim %s/%s: %s", namespace, name, e)
        raise


def apply_claim_generic(api_version: str, kind: str, manifest: dict) -> dict:
    """Apply a claim of any product type using server-side apply (SSA)."""
    import logging
    logger = logging.getLogger(__name__)
    
    resource = _get_resource_for(api_version, kind)
    if resource is None:
        raise RuntimeError(
            f"Cannot apply claim: {kind} CRD is not installed "
            "or Kubernetes is not reachable."
        )

    namespace = manifest["metadata"]["namespace"]
    name = manifest["metadata"]["name"]
    
    logger.debug(
        "Applying claim %s/%s (kind=%s, api_version=%s)",
        namespace, name, kind, api_version
    )
    
    try:
        result = resource.server_side_apply(
            body=manifest, namespace=namespace, field_manager="idp-controlplane",
        )
        result_dict = result.to_dict()
        
        logger.info(
            "Claim applied via SSA: %s/%s, result_kind=%s, has_status=%s",
            namespace, name,
            result_dict.get("kind"), "status" in result_dict
        )
        logger.debug("Apply result: %s", result_dict)
        return result_dict
    except AttributeError:
        logger.debug("SSA not available, falling back to replace/create")
        pass

    name = manifest["metadata"]["name"]
    try:
        existing = resource.get(name=name, namespace=namespace)
        manifest["metadata"]["resourceVersion"] = existing.metadata.resourceVersion
        result = resource.replace(body=manifest, namespace=namespace)
        
        logger.info(
            "Claim replaced: %s/%s",
            namespace, name
        )
    except Exception as e:
        if hasattr(e, "status") and e.status == 404:
            result = resource.create(body=manifest, namespace=namespace)
            logger.info(
                "Claim created: %s/%s",
                namespace, name
            )
        else:
            logger.error(
                "Error applying claim %s/%s: %s",
                namespace, name, e
            )
            raise
    
    result_dict = result.to_dict()
    logger.debug("Apply result: %s", result_dict)
    return result_dict


def delete_claim_generic(api_version: str, kind: str, namespace: str, name: str):
    """Delete an existing claim of any product type."""
    resource = _get_resource_for(api_version, kind)
    if resource is None:
        raise RuntimeError(
            f"Cannot delete claim: {kind} CRD is not installed "
            "or Kubernetes is not reachable."
        )

    try:
        resource.delete(name=name, namespace=namespace)
    except Exception as e:
        if hasattr(e, "status") and e.status == 404:
            return
        raise


def get_secret_exists(namespace: str, name: str) -> bool:
    """Check whether a connection Secret exists (does NOT return secret data)."""
    if not _k8s_available or _api_client is None:
        return False

    v1 = k8s_client.CoreV1Api(_api_client)
    try:
        v1.read_namespaced_secret(name=name, namespace=namespace)
        return True
    except Exception:
        return False


def apply_secret(namespace: str, name: str, string_data: dict) -> dict:
    """Create or update a Kubernetes Secret with the given string data.

    Uses create with fallback to patch if the secret already exists.

    Raises:
        RuntimeError: If Kubernetes is not configured.
    """
    if not _k8s_available or _api_client is None:
        raise RuntimeError(
            "Kubernetes client is not available. Cannot apply Secret."
        )

    v1 = k8s_client.CoreV1Api(_api_client)
    body = k8s_client.V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
        string_data=string_data,
    )
    try:
        result = v1.create_namespaced_secret(namespace=namespace, body=body)
        logger.info("Created Secret %s/%s", namespace, name)
    except Exception as e:
        if hasattr(e, "status") and e.status == 409:
            result = v1.patch_namespaced_secret(name=name, namespace=namespace, body=body)
            logger.info("Updated Secret %s/%s", namespace, name)
        else:
            raise
    return result.to_dict()


def apply_manifest(manifest: dict) -> dict:
    """Apply any Kubernetes or Crossplane resource manifest via the dynamic client.

    Uses server-side apply (SSA) so that partial updates are merged cleanly.
    Falls back to create/replace if SSA is unavailable.

    Raises:
        RuntimeError: If Kubernetes is not configured or the CRD is missing.
    """
    if _dynamic_client is None:
        raise RuntimeError(
            "Kubernetes dynamic client is not available. Cannot apply manifest."
        )

    api_version = manifest.get("apiVersion", "")
    kind = manifest.get("kind", "")
    namespace = manifest.get("metadata", {}).get("namespace")
    name = manifest.get("metadata", {}).get("name", "")

    try:
        resource = _dynamic_client.resources.get(api_version=api_version, kind=kind)
    except Exception as e:
        raise RuntimeError(
            f"CRD for {kind} ({api_version}) not found in cluster: {e}"
        )

    try:
        result = resource.server_side_apply(
            body=manifest,
            namespace=namespace,
            field_manager="idp-controlplane",
        )
        logger.info("Applied %s %s via SSA", kind, name)
        return result.to_dict()
    except AttributeError:
        pass  # older client — fall through to create/replace

    try:
        if namespace:
            existing = resource.get(name=name, namespace=namespace)
        else:
            existing = resource.get(name=name)
        manifest["metadata"]["resourceVersion"] = existing.metadata.resourceVersion
        if namespace:
            result = resource.replace(body=manifest, namespace=namespace)
        else:
            result = resource.replace(body=manifest)
        logger.info("Replaced %s %s", kind, name)
    except Exception as e:
        if hasattr(e, "status") and e.status == 404:
            if namespace:
                result = resource.create(body=manifest, namespace=namespace)
            else:
                result = resource.create(body=manifest)
            logger.info("Created %s %s", kind, name)
        else:
            raise
    return result.to_dict()
