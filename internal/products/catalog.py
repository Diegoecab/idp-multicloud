"""Product catalog: all registered cloud service products.

To add a new product:
  1. Define a ProductDefinition with its ParameterSpecs
  2. Call register_product()
  3. The generic /api/services/<product> endpoint handles the rest

The scheduler, tiers, experiments, and analytics are product-agnostic.
"""

from internal.products.registry import (
    ProductDefinition, ParameterSpec, register_product,
)


# ── MySQL ────────────────────────────────────────────────────────────────────

def _mysql_param_builder(body: dict) -> dict:
    """Map request body to MySQL-specific claim parameters."""
    return {
        "size": body.get("size", "medium"),
        "storageGB": body.get("storageGB", 50),
        "ha": body.get("ha", False),
    }


MYSQL = ProductDefinition(
    name="mysql",
    display_name="Managed MySQL",
    description="Managed MySQL database with automatic backups, replication, and failover.",
    api_version="db.platform.example.org/v1alpha1",
    kind="MySQLInstanceClaim",
    composition_class="mysql",
    composition_group="db.platform.example.org",
    parameters=[
        ParameterSpec(name="size", param_type="choice",
                      choices=("small", "medium", "large", "xlarge")),
        ParameterSpec(name="storageGB", param_type="int",
                      min_value=10, max_value=65536),
        ParameterSpec(name="ha", param_type="bool", required=False, default=False),
    ],
    connection_secret_suffix="-conn",
    param_builder=_mysql_param_builder,
)

register_product(MYSQL)


# ── WebApp (Compute) ────────────────────────────────────────────────────────

def _webapp_param_builder(body: dict) -> dict:
    """Map request body to WebApp-specific claim parameters."""
    return {
        "image": body.get("image", ""),
        "port": body.get("port", 8080),
        "cpu": body.get("cpu", "250m"),
        "memory": body.get("memory", "512Mi"),
        "replicas": body.get("replicas", 2),
        "ha": body.get("ha", False),
    }


WEBAPP = ProductDefinition(
    name="webapp",
    display_name="Web Application",
    description="Managed web application compute with auto-scaling, load balancing, and TLS.",
    api_version="compute.platform.example.org/v1alpha1",
    kind="WebAppClaim",
    composition_class="webapp",
    composition_group="compute.platform.example.org",
    parameters=[
        ParameterSpec(name="image", param_type="string", required=True),
        ParameterSpec(name="port", param_type="int",
                      min_value=1, max_value=65535, required=False, default=8080),
        ParameterSpec(name="cpu", param_type="choice",
                      choices=("125m", "250m", "500m", "1000m", "2000m", "4000m"),
                      required=False, default="250m"),
        ParameterSpec(name="memory", param_type="choice",
                      choices=("256Mi", "512Mi", "1Gi", "2Gi", "4Gi", "8Gi"),
                      required=False, default="512Mi"),
        ParameterSpec(name="replicas", param_type="int",
                      min_value=1, max_value=20, required=False, default=2),
        ParameterSpec(name="ha", param_type="bool", required=False, default=False),
    ],
    connection_secret_suffix="-conn",
    param_builder=_webapp_param_builder,
)

register_product(WEBAPP)
