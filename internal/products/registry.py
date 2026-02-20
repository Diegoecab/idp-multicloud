"""Product registry: extensible catalog of cloud services.

Each product defines:
  - Its Crossplane CRD (apiVersion, kind, composition class)
  - Its developer-facing parameters (with validation rules)
  - Its forbidden fields (decided by the control plane)

To add a new product (e.g., redis, loadbalancer, graph-db):
  1. Define a ProductDefinition
  2. Call register_product()
  3. The generic /api/services/<product> endpoint handles the rest

The scheduler, health checks, experiments, and analytics are
product-agnostic — they work for any registered product.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ParameterSpec:
    """Validation spec for a single product parameter."""
    name: str
    required: bool = True
    param_type: str = "string"   # string, int, bool, choice
    choices: tuple = ()          # valid values for choice type
    min_value: int = 0           # for int type
    max_value: int = 0           # for int type
    default: object = None       # default if not provided


@dataclass
class ProductDefinition:
    """Definition of a cloud service product.

    This is the extension point: add a new product by creating a
    ProductDefinition and registering it.
    """
    name: str                       # e.g. "mysql", "webapp", "redis"
    display_name: str               # e.g. "Managed MySQL", "Web Application"
    description: str
    api_version: str                # CRD apiVersion
    kind: str                       # CRD kind
    composition_class: str          # compositionSelector matchLabel class value
    composition_group: str          # compositionSelector matchLabel group prefix
    parameters: list                # list of ParameterSpec
    connection_secret_suffix: str = "-conn"
    # Optional custom parameter builder (maps raw body → spec.parameters dict)
    param_builder: Optional[Callable] = field(default=None, repr=False)


# ── Product Registry ─────────────────────────────────────────────────────────

_products: dict[str, ProductDefinition] = {}


def register_product(product: ProductDefinition):
    """Register a product in the catalog."""
    _products[product.name] = product


def get_product(name: str) -> Optional[ProductDefinition]:
    """Get a product definition by name."""
    return _products.get(name)


def list_products() -> list[dict]:
    """List all registered products."""
    return [
        {
            "name": p.name,
            "display_name": p.display_name,
            "description": p.description,
            "kind": p.kind,
            "parameters": [
                {
                    "name": ps.name,
                    "type": ps.param_type,
                    "required": ps.required,
                    "choices": list(ps.choices) if ps.choices else None,
                    "default": ps.default,
                }
                for ps in p.parameters
            ],
        }
        for p in _products.values()
    ]


def validate_product_params(product: ProductDefinition, body: dict) -> list[str]:
    """Validate request body against a product's parameter specs.

    Returns list of error strings (empty = valid).
    """
    errors = []
    for spec in product.parameters:
        value = body.get(spec.name)

        if value is None and spec.required and spec.default is None:
            errors.append(f"{spec.name} is required")
            continue

        if value is None:
            continue  # optional or has default

        if spec.param_type == "int":
            if not isinstance(value, int):
                errors.append(f"{spec.name} must be an integer")
            elif spec.min_value and value < spec.min_value:
                errors.append(f"{spec.name} must be >= {spec.min_value}")
            elif spec.max_value and value > spec.max_value:
                errors.append(f"{spec.name} must be <= {spec.max_value}")

        elif spec.param_type == "bool":
            if not isinstance(value, bool):
                errors.append(f"{spec.name} must be a boolean")

        elif spec.param_type == "choice":
            if value not in spec.choices:
                errors.append(f"{spec.name} must be one of {spec.choices}")

    return errors


def build_product_claim(
    product: ProductDefinition,
    body: dict,
    placement,
) -> dict:
    """Build a generic Crossplane Claim for any registered product."""
    import json

    name = body.get("name", "")
    namespace = body.get("namespace", "default")
    cell = body.get("cell", "")
    tier = body.get("tier", "")
    environment = body.get("environment", "")

    # Build parameters: common + product-specific
    params = {
        "cell": cell,
        "environment": environment,
        "tier": tier,
        "provider": placement.provider,
        "region": placement.region,
        "network": placement.network,
    }

    # Add product-specific params
    if product.param_builder:
        params.update(product.param_builder(body))
    else:
        for spec in product.parameters:
            if spec.name in ("name", "namespace", "cell", "tier", "environment"):
                continue  # already handled above
            value = body.get(spec.name, spec.default)
            if value is not None:
                params[spec.name] = value

    return {
        "apiVersion": product.api_version,
        "kind": product.kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "platform.example.org/cell": cell,
                "platform.example.org/environment": environment,
                "platform.example.org/tier": tier,
                "platform.example.org/product": product.name,
            },
            "annotations": {
                "platform.example.org/placement-reason": json.dumps(
                    placement.reason, separators=(",", ":")
                ),
            },
        },
        "spec": {
            "parameters": params,
            "compositionSelector": {
                "matchLabels": {
                    f"{product.composition_group}/provider": placement.provider,
                    f"{product.composition_group}/class": product.composition_class,
                },
            },
            "writeConnectionSecretToRef": {
                "name": f"{name}{product.connection_secret_suffix}",
            },
        },
    }
