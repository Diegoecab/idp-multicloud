"""Crossplane MySQLInstanceClaim builder.

Builds a complete Claim manifest from a developer request and a placement decision.
The manifest follows the custom CRD:
    apiVersion: db.platform.example.org/v1alpha1
    kind: MySQLInstanceClaim
"""

import json
from internal.models.types import MySQLRequest, PlacementDecision


API_VERSION = "db.platform.example.org/v1alpha1"
KIND = "MySQLInstanceClaim"


def build_claim(request: MySQLRequest, placement: PlacementDecision) -> dict:
    """Build a Crossplane MySQLInstanceClaim manifest.

    Args:
        request: The developer's MySQL provisioning request.
        placement: The scheduler's placement decision (provider, region, network, reason).

    Returns:
        A dict representing the full Kubernetes-style Claim manifest.
    """
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {
            "name": request.name,
            "namespace": request.namespace,
            "labels": {
                "platform.example.org/cell": request.cell,
                "platform.example.org/environment": request.environment,
                "platform.example.org/tier": request.tier,
            },
            "annotations": {
                "platform.example.org/placement-reason": json.dumps(
                    placement.reason, separators=(",", ":")
                ),
            },
        },
        "spec": {
            "parameters": {
                "cell": request.cell,
                "environment": request.environment,
                "tier": request.tier,
                "provider": placement.provider,
                "region": placement.region,
                "size": request.size,
                "storageGB": request.storage_gb,
                "ha": request.ha,
                "network": placement.network,
            },
            "compositionSelector": {
                "matchLabels": {
                    "db.platform.example.org/provider": placement.provider,
                    "db.platform.example.org/class": "mysql",
                },
            },
            "writeConnectionSecretToRef": {
                "name": f"{request.name}-conn",
            },
        },
    }
