"""Configure Crossplane providers from IDP credentials and cloud provider config.

Reads both sources of truth from the IDP database:
  - provider_credentials  (aws_access_key_id / aws_secret_access_key, etc.)
  - provider_config       (which providers are enabled, their settings)

For each enabled provider that has credentials it creates:
  1. A Kubernetes Secret in crossplane-system holding the raw credentials
  2. A Crossplane Provider package resource (installs the provider controller)
  3. A Crossplane ProviderConfig that points the provider at the Secret

Supported providers:
  aws  — access_key  → provider-aws  (upbound)
  gcp  — service_account → provider-gcp (upbound)
  oci  — api_key     → provider-oci  (upbound)
"""

import logging

from internal.db.database import get_provider_configs, get_provider_credentials
from internal.k8s import client as k8s

logger = logging.getLogger(__name__)

# Crossplane provider packages (Upbound official)
_PROVIDER_PACKAGES = {
    "aws": "xpkg.upbound.io/upbound/provider-aws:v0.33.0",
    "gcp": "xpkg.upbound.io/upbound/provider-gcp:v0.33.0",
    "oci": "xpkg.upbound.io/upbound/provider-oci:v0.17.0",
}

# ProviderConfig apiVersion per cloud
_PROVIDER_CONFIG_API = {
    "aws": "aws.upbound.io/v1beta1",
    "gcp": "gcp.upbound.io/v1beta1",
    "oci": "oci.upbound.io/v1beta1",
}


def configure_crossplane_providers() -> list[dict]:
    """Read IDP credentials + provider config, then configure Crossplane.

    Returns a list of result dicts, one per enabled provider, describing
    what was applied or why it was skipped.
    """
    providers = get_provider_configs()
    results = []

    for provider_row in providers:
        name = provider_row["name"]
        enabled = provider_row.get("enabled", True)

        if not enabled:
            results.append({
                "provider": name,
                "status": "skipped",
                "reason": "provider is disabled",
            })
            continue

        cred = get_provider_credentials(name)
        if not cred or not cred.get("cred_data"):
            results.append({
                "provider": name,
                "status": "skipped",
                "reason": "no credentials configured",
            })
            continue

        try:
            result = _configure_provider(name, cred["cred_type"], cred["cred_data"])
            results.append(result)
        except Exception as exc:
            logger.error("Failed to configure Crossplane for provider %s: %s", name, exc)
            results.append({
                "provider": name,
                "status": "error",
                "reason": str(exc),
            })

    return results


# ── Per-provider configuration ───────────────────────────────────────────────

def _configure_provider(provider: str, cred_type: str, cred_data: dict) -> dict:
    if provider == "aws":
        return _configure_aws(cred_type, cred_data)
    if provider == "gcp":
        return _configure_gcp(cred_type, cred_data)
    if provider == "oci":
        return _configure_oci(cred_type, cred_data)
    return {
        "provider": provider,
        "status": "skipped",
        "reason": f"no Crossplane configuration handler for provider '{provider}'",
    }


def _configure_aws(cred_type: str, cred_data: dict) -> dict:
    """Configure the Crossplane AWS provider."""
    if cred_type == "irsa":
        # IRSA uses pod-level identity — no secret needed, ProviderConfig uses WebIdentity
        _apply_provider_package("aws")
        pending = _apply_provider_config_secret_ref(
            provider="aws",
            config_manifest={
                "apiVersion": _PROVIDER_CONFIG_API["aws"],
                "kind": "ProviderConfig",
                "metadata": {"name": "default"},
                "spec": {"credentials": {"source": "InjectedIdentity"}},
            },
        )
        if pending:
            return pending
        return {"provider": "aws", "status": "configured", "auth": "irsa"}

    if cred_type != "access_key":
        return {
            "provider": "aws",
            "status": "error",
            "reason": f"unsupported cred_type '{cred_type}' for AWS",
        }

    key_id = cred_data.get("aws_access_key_id", "")
    secret = cred_data.get("aws_secret_access_key", "")
    if not key_id or not secret:
        return {
            "provider": "aws",
            "status": "error",
            "reason": "aws_access_key_id or aws_secret_access_key is empty",
        }

    # 1. Kubernetes Secret with AWS credentials file format
    credentials_file = (
        "[default]\n"
        f"aws_access_key_id = {key_id}\n"
        f"aws_secret_access_key = {secret}\n"
    )
    k8s.apply_secret(
        namespace="crossplane-system",
        name="aws-creds",
        string_data={"credentials": credentials_file},
    )

    # 2. Crossplane Provider package
    _apply_provider_package("aws")

    # 3. ProviderConfig — only possible once the provider CRD is installed
    pending = _apply_provider_config_secret_ref(
        provider="aws",
        config_manifest={
            "apiVersion": _PROVIDER_CONFIG_API["aws"],
            "kind": "ProviderConfig",
            "metadata": {"name": "default"},
            "spec": {
                "credentials": {
                    "source": "Secret",
                    "secretRef": {
                        "namespace": "crossplane-system",
                        "name": "aws-creds",
                        "key": "credentials",
                    },
                },
            },
        },
    )
    if pending:
        return pending

    return {
        "provider": "aws",
        "status": "configured",
        "auth": "access_key",
        "secret": "crossplane-system/aws-creds",
        "provider_config": "default",
    }


def _configure_gcp(cred_type: str, cred_data: dict) -> dict:
    """Configure the Crossplane GCP provider."""
    if cred_type == "workload_identity":
        _apply_provider_package("gcp")
        pending = _apply_provider_config_secret_ref(
            provider="gcp",
            config_manifest={
                "apiVersion": _PROVIDER_CONFIG_API["gcp"],
                "kind": "ProviderConfig",
                "metadata": {"name": "default"},
                "spec": {"credentials": {"source": "InjectedIdentity"}},
            },
        )
        if pending:
            return pending
        return {"provider": "gcp", "status": "configured", "auth": "workload_identity"}

    if cred_type != "service_account":
        return {
            "provider": "gcp",
            "status": "error",
            "reason": f"unsupported cred_type '{cred_type}' for GCP",
        }

    import json as _json
    sa_json = _json.dumps(cred_data)

    k8s.apply_secret(
        namespace="crossplane-system",
        name="gcp-creds",
        string_data={"credentials": sa_json},
    )

    _apply_provider_package("gcp")

    pending = _apply_provider_config_secret_ref(
        provider="gcp",
        config_manifest={
            "apiVersion": _PROVIDER_CONFIG_API["gcp"],
            "kind": "ProviderConfig",
            "metadata": {"name": "default"},
            "spec": {
                "projectID": cred_data.get("project_id", ""),
                "credentials": {
                    "source": "Secret",
                    "secretRef": {
                        "namespace": "crossplane-system",
                        "name": "gcp-creds",
                        "key": "credentials",
                    },
                },
            },
        },
    )
    if pending:
        return pending

    return {
        "provider": "gcp",
        "status": "configured",
        "auth": "service_account",
        "secret": "crossplane-system/gcp-creds",
        "provider_config": "default",
    }


def _configure_oci(cred_type: str, cred_data: dict) -> dict:
    """Configure the Crossplane OCI provider."""
    if cred_type == "instance_principal":
        _apply_provider_package("oci")
        pending = _apply_provider_config_secret_ref(
            provider="oci",
            config_manifest={
                "apiVersion": _PROVIDER_CONFIG_API["oci"],
                "kind": "ProviderConfig",
                "metadata": {"name": "default"},
                "spec": {"credentials": {"source": "InjectedIdentity"}},
            },
        )
        if pending:
            return pending
        return {"provider": "oci", "status": "configured", "auth": "instance_principal"}

    if cred_type != "api_key":
        return {
            "provider": "oci",
            "status": "error",
            "reason": f"unsupported cred_type '{cred_type}' for OCI",
        }

    # OCI credentials file format
    private_key = cred_data.get("private_key", "")
    oci_config = (
        "[DEFAULT]\n"
        f"tenancy={cred_data.get('tenancy_ocid', '')}\n"
        f"user={cred_data.get('user_ocid', '')}\n"
        f"fingerprint={cred_data.get('fingerprint', '')}\n"
        "key_file=/tmp/oci_api_key.pem\n"
        f"region={cred_data.get('region', 'us-ashburn-1')}\n"
    )

    k8s.apply_secret(
        namespace="crossplane-system",
        name="oci-creds",
        string_data={
            "config": oci_config,
            "private_key": private_key,
        },
    )

    _apply_provider_package("oci")

    pending = _apply_provider_config_secret_ref(
        provider="oci",
        config_manifest={
            "apiVersion": _PROVIDER_CONFIG_API["oci"],
            "kind": "ProviderConfig",
            "metadata": {"name": "default"},
            "spec": {
                "credentials": {
                    "source": "Secret",
                    "secretRef": {
                        "namespace": "crossplane-system",
                        "name": "oci-creds",
                        "key": "config",
                    },
                },
            },
        },
    )
    if pending:
        return pending

    return {
        "provider": "oci",
        "status": "configured",
        "auth": "api_key",
        "secret": "crossplane-system/oci-creds",
        "provider_config": "default",
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_provider_package(provider: str) -> None:
    """Install the Crossplane provider package for the given cloud."""
    package = _PROVIDER_PACKAGES.get(provider)
    if not package:
        logger.warning("No provider package defined for '%s'", provider)
        return

    k8s.apply_manifest({
        "apiVersion": "pkg.crossplane.io/v1",
        "kind": "Provider",
        "metadata": {"name": f"upbound-provider-{provider}"},
        "spec": {
            "package": package,
            "packagePullPolicy": "IfNotPresent",
        },
    })
    logger.info("Applied Provider package for %s (%s)", provider, package)


def _apply_provider_config_secret_ref(provider: str, config_manifest: dict):
    """Apply a ProviderConfig manifest.

    Returns a 'pending' result dict if the ProviderConfig CRD is not yet
    installed (i.e. the provider package is still downloading), or None on
    success so callers can decide the final status.
    """
    try:
        k8s.apply_manifest(config_manifest)
        logger.info("Applied ProviderConfig for %s", provider)
        return None  # success — caller builds the final result
    except RuntimeError as exc:
        if "No matches found" in str(exc) or "not found in cluster" in str(exc):
            logger.info(
                "ProviderConfig CRD not available yet for %s — provider still installing",
                provider,
            )
            return {
                "provider": provider,
                "status": "pending",
                "reason": (
                    f"Provider package is downloading; ProviderConfig CRD not available yet. "
                    f"Re-call POST /api/admin/crossplane/configure once the provider is healthy. "
                    f"Check: kubectl get provider upbound-provider-{provider}"
                ),
            }
        raise
