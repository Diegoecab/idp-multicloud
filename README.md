# IDP Multicloud

A cell-based Internal Developer Platform (IDP) control plane for provisioning managed cloud services (MySQL, WebApp, and more) across AWS, GCP, and OCI. Developers declare **what** they need; the control plane decides **where** it runs.

---

## Objective

Provide a **cell-based contract** for developers to request managed cloud services while the control plane autonomously decides provider, region, runtime cluster, and network placement across multiple clouds using a **tiered criticality framework**.

Developers specify only:

| Field | Description | Products |
|-------|-------------|----------|
| `cell` | Logical cell identifier | All |
| `tier` | Criticality tier (`low`, `medium`, `critical`, `business_critical`) | All |
| `environment` | Target environment (`dev`, `staging`, `production`) | All |
| `ha` | High availability (boolean) | All |
| `size` | Instance size (`small`, `medium`, `large`, `xlarge`) | MySQL |
| `storageGB` | Storage capacity (10–65536 GB) | MySQL |
| `image` | Container image to deploy | WebApp |
| `cpu` | CPU allocation (`125m`–`4000m`) | WebApp |
| `memory` | Memory allocation (`256Mi`–`8Gi`) | WebApp |
| `replicas` | Number of replicas (1–20) | WebApp |

The control plane decides: **provider**, **region**, **runtimeCluster**, and **network configuration**.

---

## High-Level Architecture

### System Layers

The platform is organized into three distinct layers with clear separation of concerns:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DEVELOPER LAYER                                   │
│                                                                             │
│  Developer only specifies: cell, tier, environment, ha + product params     │
│  Developer NEVER specifies: provider, region, runtimeCluster, network       │
│                                                                             │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                                │
│  │ Web UI   │   │ curl/CLI │   │ CI/CD    │                                 │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘                                │
│       └───────────────┴──────────────┘                                      │
│                       │ POST /api/services/<product>                        │
├───────────────────────┼─────────────────────────────────────────────────────┤
│                       ▼                                                     │
│                CONTROL PLANE LAYER  (Python/Flask)                          │
│                                                                             │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  ┌──────────────────┐   │
│  │ Validation  │─>│ Sticky Check │─>│ Scheduler  │─>│ Claim Builder    │   │
│  │ + Contract  │  │ (K8s lookup) │  │ Gates +    │  │ (Generic: any    │   │
│  │ + Product   │  │              │  │ Weighted   │  │  registered      │   │
│  │ Registry    │  │              │  │ Scoring    │  │  product CRD)    │   │
│  └─────────────┘  └──────────────┘  └────────────┘  └───────┬──────────┘   │
│                                                              │ SSA Apply    │
│  No cloud credentials here.                                 │              │
│  Only needs K8s access (kubeconfig or ServiceAccount).      │              │
├─────────────────────────────────────────────────────────────┼──────────────┤
│                       DATA PLANE LAYER  (Kubernetes + Crossplane)           │
│                                                              ▼              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                    Kubernetes Cluster                                │   │
│  │                                                                      │   │
│  │  ┌─────────────────┐    ┌───────────────────────────────────────┐   │   │
│  │  │  Crossplane      │    │  MySQLInstanceClaim (Custom Resource) │   │   │
│  │  │  Core Controller │    │  + placement-reason annotation        │   │   │
│  │  └────────┬─────────┘    └───────────────────────────────────────┘   │   │
│  │           │                                                          │   │
│  │           ▼                                                          │   │
│  │  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐         │   │
│  │  │ AWS Provider   │  │ GCP Provider   │  │ OCI Provider   │         │   │
│  │  │                │  │                │  │                │         │   │
│  │  │ ProviderConfig │  │ ProviderConfig │  │ ProviderConfig │         │   │
│  │  │ + Credentials  │  │ + Credentials  │  │ + Credentials  │         │   │
│  │  │   (K8s Secret) │  │   (K8s Secret) │  │   (K8s Secret) │         │   │
│  │  └───────┬────────┘  └───────┬────────┘  └───────┬────────┘         │   │
│  │          │                   │                    │                  │   │
│  └──────────┼───────────────────┼────────────────────┼──────────────────┘   │
│             │                   │                    │                      │
├─────────────┼───────────────────┼────────────────────┼──────────────────────┤
│             ▼                   ▼                    ▼                      │
│                        CLOUD PROVIDER LAYER                                 │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │   AWS             │  │   GCP             │  │   OCI             │         │
│  │   Amazon RDS      │  │   Cloud SQL       │  │   MySQL Database  │         │
│  │   (MySQL)         │  │   (MySQL)         │  │   Service (MDS)   │         │
│  │                   │  │                   │  │                   │         │
│  │   VPC, Subnets,   │  │   VPC, Subnets,   │  │   VCN, Subnets,   │         │
│  │   Security Groups │  │   Firewall Rules  │  │   Security Lists  │         │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘          │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Request Lifecycle

```
  Developer                Control Plane             Kubernetes/Crossplane        Cloud
     │                          │                            │                      │
     │  POST /api/mysql         │                            │                      │
     │  {cell, tier, env,       │                            │                      │
     │   size, storageGB, ha}   │                            │                      │
     │ ────────────────────────>│                            │                      │
     │                          │                            │                      │
     │                          │  1. Validate input         │                      │
     │                          │  2. Reject if developer    │                      │
     │                          │     sent provider/region   │                      │
     │                          │                            │                      │
     │                          │  3. Sticky check ─────────>│ GET Claim            │
     │                          │     (if exists, return it) │ {namespace, name}    │
     │                          │                            │                      │
     │                          │  4. Gate filter:           │                      │
     │                          │     Remove candidates      │                      │
     │                          │     missing tier caps      │                      │
     │                          │                            │                      │
     │                          │  5. Weighted scoring:      │                      │
     │                          │     latency * w_l +        │                      │
     │                          │     dr * w_d +             │                      │
     │                          │     maturity * w_m +       │                      │
     │                          │     cost * w_c             │                      │
     │                          │                            │                      │
     │                          │  6. Select winner          │                      │
     │                          │     (highest score)        │                      │
     │                          │                            │                      │
     │                          │  7. Build Claim ──────────>│ SSA Apply            │
     │                          │     MySQLInstanceClaim     │ MySQLInstanceClaim   │
     │                          │     + placement-reason     │         │            │
     │                          │       annotation           │         ▼            │
     │                          │                            │  Crossplane ────────>│ Provision
     │                          │                            │  reconciles          │ RDS / SQL
     │  201 Created             │                            │  the Claim           │ / MDS
     │  {placement, reason,     │                            │                      │
     │   claim, top-3}          │                            │                      │
     │ <────────────────────────│                            │                      │
     │                          │                            │                      │
```

### Credential Boundary

A critical design principle: **the control plane never handles cloud credentials**.

| Component | Has Cloud Credentials? | Has K8s Access? | Role |
|-----------|----------------------|-----------------|------|
| **Control Plane** (Python) | No | Yes (kubeconfig or ServiceAccount) | Decides placement, builds and applies Claims |
| **Crossplane Providers** | Yes (via K8s Secrets) | Yes (in-cluster) | Reconciles Claims into actual cloud resources |
| **K8s Secrets** | Yes (stores credentials) | N/A | Secure storage for provider credentials |
| **ProviderConfig** | References Secrets | N/A | Binds a Crossplane Provider to its credentials |

### Key Concepts

- **Cell-based contract**: Developers never choose a provider. The platform decides based on tier requirements, candidate scoring, and operational policy.
- **Sticky placement**: If a Claim already exists for `{namespace, name}`, the control plane returns the existing placement without rescheduling — preventing unnecessary migrations.
- **Auditable decisions**: Every placement stores a JSON reason annotation with the full top-3 candidate scoring breakdown, gates applied, and weights used.
- **Crossplane integration**: The control plane generates `MySQLInstanceClaim` custom resources and applies them to the cluster via server-side apply (SSA).
- **Separation of concerns**: The control plane handles scheduling logic only. Crossplane handles cloud API communication and credentials. Kubernetes Secrets provide secure credential storage.

---

## Criticality Framework

### Tier Definitions

| Tier | RTO | RPO | Required Capabilities | Weight Priority |
|------|-----|-----|----------------------|-----------------|
| `low` | 30 min | 5 min | PITR + Multi-AZ + Private networking | Latency (0.30), DR (0.30), Maturity (0.25), Cost (0.15) |
| `medium` | 120 min | 15 min | PITR + Private networking | Equal weights (0.25 each) |
| `critical` | 480 min | 60 min | Private networking only | **Cost (0.50)**, Maturity (0.20), Latency (0.15), DR (0.15) |
| `business_critical` | 15 min | 1 min | PITR + Multi-AZ + Private networking + Cross-region replication | **DR (0.40)**, Latency (0.25), Maturity (0.25), Cost (0.10) |

### Scheduling Algorithm

1. **Health filter**: Remove candidates whose provider is unhealthy or circuit breaker is open.
2. **Gate filtering**: Remove candidates that lack required capabilities for the tier.
3. **HA enforcement**: If `ha=true`, add `multi_az` as an additional hard gate (even for tiers that don't normally require it).
4. **Weighted scoring**: For each surviving candidate, compute `total = sum(raw_score[dim] * weight[dim])`.
5. **Ranking**: Sort candidates by total score descending. Select the winner.
6. **Failover selection**: For `low` and `business_critical` tiers, select the best candidate in a **different cloud provider** as a DR failover target.
7. **Audit trail**: Return a top-3 breakdown with scores and sub-scores in the placement reason.

### Candidate Pool

| Provider | Region | Capabilities |
|----------|--------|-------------|
| AWS | us-east-1 | PITR, Multi-AZ, Private networking, Cross-region replication |
| AWS | eu-west-1 | PITR, Multi-AZ, Private networking, Cross-region replication |
| AWS | us-west-2 | PITR, Multi-AZ, Private networking |
| GCP | us-central1 | PITR, Multi-AZ, Private networking |
| GCP | europe-west1 | PITR, Multi-AZ, Private networking |
| OCI | us-ashburn-1 | PITR, Private networking |
| OCI | eu-frankfurt-1 | PITR, Private networking |

---

## HA & DR Multicloud

The platform implements several best practices for high availability and disaster recovery across multiple cloud providers.

### HA Flag Enforcement

When the developer sets `ha: true`, the scheduler enforces `multi_az` as a **hard gate** — even for tiers that don't normally require it. This ensures HA workloads always land on candidates with multi-AZ support.

```
Developer: { "ha": true, "tier": "critical" }

Without HA enforcement:           With HA enforcement:
  critical tier gate:               critical tier gate:
    - private_networking              - private_networking
                                      - multi_az  ← added automatically
  Candidates: ALL 7 pass            Candidates: only AWS + GCP pass
  OCI is eligible                    OCI is rejected (no multi_az)
```

| `ha` value | Effect on scheduling |
|------------|---------------------|
| `true` | Adds `multi_az` as hard gate. OCI candidates (single-AZ) are rejected. |
| `false` | No extra gates. All candidates eligible based on tier alone. |

### Cross-Cloud Failover Placement

For `low` and `business_critical` tiers, the scheduler selects a **secondary failover target** in a different cloud provider. This enables active-passive DR across clouds.

```
Primary:   AWS us-east-1  (highest score)
Failover:  GCP us-central1 (best score in a DIFFERENT cloud)
                            ↑ anti-affinity: different_cloud_from_aws
```

The failover target is included in:
- The API response (`failover` field)
- The audit reason annotation (`reason.failover`)

This enables operators to pre-provision standby replicas or configure DNS failover.

| Tier | Failover included? | Rationale |
|------|-------------------|-----------|
| `low` | Yes | Strict SLA; needs cross-cloud DR standby |
| `medium` | No | Balanced tier; single-cloud acceptable |
| `critical` | No | Cost-driven; DR not prioritized |
| `business_critical` | Yes | Highest criticality; cross-cloud replication mandatory |

### Provider Health Management

Operators can mark providers as unhealthy via the API. Unhealthy providers are excluded from scheduling.

```bash
# Mark AWS as unhealthy (e.g., during an outage)
curl -X PUT http://localhost:8080/api/providers/aws/health \
  -H "Content-Type: application/json" \
  -d '{"healthy": false}'

# All subsequent scheduling skips AWS candidates
curl -X POST http://localhost:8080/api/mysql \
  -H "Content-Type: application/json" \
  -d '{"name":"db1","cell":"c1","tier":"medium","environment":"dev","size":"small","storageGB":20,"ha":false}'
# → placement.provider will be gcp or oci (never aws)

# View all provider health + circuit breaker status
curl http://localhost:8080/api/providers/health

# Restore AWS
curl -X PUT http://localhost:8080/api/providers/aws/health \
  -H "Content-Type: application/json" \
  -d '{"healthy": true}'
```

Health status is tracked at two levels:
- **Provider-level** (`set_provider_health`): operator-controlled, affects all candidates for that provider
- **Candidate-level** (`Candidate.healthy`): per-region health, for future integration with automated probes

### Circuit Breaker

Each provider has a circuit breaker that opens automatically after repeated failures. This prevents cascading failures when a cloud provider API is degraded.

```
State diagram:

   ┌────────┐  failures >= threshold  ┌────────┐  cooldown expired  ┌───────────┐
   │ CLOSED │ ──────────────────────> │  OPEN  │ ────────────────> │ HALF_OPEN │
   │        │                         │        │                    │           │
   │ normal │                         │ block  │                    │ allow one │
   │ traffic│ <────── success ─────── │  all   │ <── failure ───── │  probe    │
   └────────┘                         └────────┘                    └───────────┘
       ↑                                                                │
       └───────────────────── success ──────────────────────────────────┘
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `failure_threshold` | 5 | Number of consecutive failures to open the circuit |
| `cooldown_seconds` | 60 | Time to wait before allowing a probe request |

When a circuit is open, all candidates from that provider are skipped during scheduling — similar to marking the provider unhealthy.

### Forced Failover Endpoint

During disaster recovery, operators can force rescheduling of an existing Claim to override sticky placement.

```bash
# Force failover of a specific instance (reschedule to any available provider)
curl -X POST http://localhost:8080/api/mysql/default/orders-db/failover

# Force failover excluding a specific provider (e.g., during AWS outage)
curl -X POST http://localhost:8080/api/mysql/default/orders-db/failover \
  -H "Content-Type: application/json" \
  -d '{"exclude_providers": ["aws"]}'
```

The failover endpoint:
1. Fetches the existing Claim parameters
2. Deletes the existing Claim
3. Runs the scheduler with an optionally filtered candidate pool
4. Creates a new Claim at the newly selected provider/region

**Response:**
```json
{
  "status": "failover_complete",
  "previous_provider": "aws",
  "placement": {
    "provider": "gcp",
    "region": "us-central1",
    "runtimeCluster": "gcp-usc1-prod-01",
    "network": { ... }
  },
  "reason": { ... }
}
```

### DR Runbook (Example)

A typical disaster recovery sequence using these features:

```bash
# 1. Detect AWS outage → mark unhealthy
curl -X PUT http://localhost:8080/api/providers/aws/health \
  -d '{"healthy": false}' -H "Content-Type: application/json"

# 2. Failover all critical workloads away from AWS
for db in orders-db payments-db users-db; do
  curl -X POST "http://localhost:8080/api/mysql/production/$db/failover" \
    -d '{"exclude_providers": ["aws"]}' -H "Content-Type: application/json"
done

# 3. New requests automatically avoid AWS (health check + circuit breaker)
# 4. When AWS recovers:
curl -X PUT http://localhost:8080/api/providers/aws/health \
  -d '{"healthy": true}' -H "Content-Type: application/json"

# 5. Optionally failover back to AWS for cost optimization
```

---

## Experimentation & Data-Driven Optimization

Inspired by platform engineering practices at scale: continuous experimentation, data-driven decisions, fast iteration, and balancing stability with innovation.

### A/B Testing on Placement Strategies

Every placement decision can be part of an experiment. Test new scoring weights on a percentage of traffic before rolling them out.

```bash
# Create an experiment: test 60% cost weight on critical tier (20% of traffic)
curl -X POST http://localhost:8080/api/experiments \
  -H "Content-Type: application/json" \
  -d '{
    "id": "exp-cost-boost-001",
    "description": "Test higher cost weight for critical tier",
    "variant_weights": {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60},
    "traffic_percentage": 0.2,
    "tier": "critical"
  }'

# List active experiments
curl http://localhost:8080/api/experiments

# Delete experiment when done
curl -X DELETE http://localhost:8080/api/experiments/exp-cost-boost-001
```

**How it works:**
- Traffic is split deterministically using a hash of `(experiment_id, request_name)` — the same request always lands in the same group
- `control` group uses default tier weights, `variant` group uses experiment weights
- The experiment group is recorded in the placement reason annotation for auditability
- Set `traffic_percentage: 1.0` for full canary rollout, `0.1` for 10% canary

```
Request "orders-db"
  │
  ├── hash("exp-001:orders-db") → bucket 0.34
  │   traffic_percentage: 0.5
  │   0.34 < 0.50 → VARIANT (use experiment weights)
  │
  └── Placement scored with: {latency: 0.10, dr: 0.10, maturity: 0.20, cost: 0.60}
      instead of default:    {latency: 0.15, dr: 0.15, maturity: 0.20, cost: 0.50}
```

### Feature Flags

Toggle placement policies without redeploying. Iterate on behavior in production.

```bash
# Enable cost optimization mode (boosts cost weight by 20%)
curl -X PUT http://localhost:8080/api/flags/prefer_cost_optimization \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# List all flags
curl http://localhost:8080/api/flags

# Disable when done
curl -X DELETE http://localhost:8080/api/flags/prefer_cost_optimization
```

Built-in feature flags:

| Flag | Effect |
|------|--------|
| `prefer_cost_optimization` | Boost cost weight by 20% across all tiers (redistributing from other dimensions) |

Custom flags can be checked in the scheduler via `get_feature_flag("your_flag")`.

### Placement Analytics

Every placement is tracked for data-driven optimization. Understand where workloads land, which providers win, and how experiments perform.

```bash
# View analytics dashboard
curl http://localhost:8080/api/analytics
```

**Response:**
```json
{
  "total_placements": 150,
  "total_requests": 155,
  "gate_rejection_rate": 0.0323,
  "provider_distribution": {
    "aws": { "count": 85, "percentage": 56.7 },
    "gcp": { "count": 45, "percentage": 30.0 },
    "oci": { "count": 20, "percentage": 13.3 }
  },
  "region_distribution": {
    "aws/us-east-1": { "count": 50, "percentage": 33.3 },
    "gcp/us-central1": { "count": 30, "percentage": 20.0 }
  },
  "tier_distribution": {
    "medium": { "count": 80, "percentage": 53.3 },
    "critical": { "count": 40, "percentage": 26.7 },
    "low": { "count": 30, "percentage": 20.0 }
  },
  "avg_score_by_provider": {
    "aws": 0.8125,
    "gcp": 0.7950,
    "oci": 0.7200
  },
  "experiments": {
    "exp-cost-boost-001": { "control": 60, "variant": 15 }
  }
}
```

Use this data to:
- **Detect imbalances**: if one provider gets 90% of traffic, consider adjusting weights
- **Measure experiments**: compare avg scores between control and variant groups
- **Track gate rejections**: high rejection rates indicate the candidate pool needs expansion
- **Validate changes**: after adjusting weights or adding candidates, verify the impact

### Experimentation Workflow

A complete cycle for testing and rolling out a placement change:

```bash
# 1. Check current analytics baseline
curl http://localhost:8080/api/analytics

# 2. Create experiment with 10% traffic (small blast radius)
curl -X POST http://localhost:8080/api/experiments -H "Content-Type: application/json" \
  -d '{"id":"exp-dr-boost","description":"Boost DR weight for low tier",
       "variant_weights":{"latency":0.20,"dr":0.45,"maturity":0.20,"cost":0.15},
       "traffic_percentage":0.10,"tier":"low"}'

# 3. Let it run, monitor analytics
curl http://localhost:8080/api/analytics
# → Check experiments.exp-dr-boost control vs variant counts and avg scores

# 4. If results are positive, increase traffic to 50%
curl -X DELETE http://localhost:8080/api/experiments/exp-dr-boost
curl -X POST http://localhost:8080/api/experiments -H "Content-Type: application/json" \
  -d '{"id":"exp-dr-boost-v2","description":"Boost DR weight (50% canary)",
       "variant_weights":{"latency":0.20,"dr":0.45,"maturity":0.20,"cost":0.15},
       "traffic_percentage":0.50,"tier":"low"}'

# 5. If still good, go to 100% (full rollout)
# 6. Update the tier definition in code and remove the experiment
```

---

## Multi-Product Architecture

The platform supports **multiple cloud products** through an extensible product registry. Each product is a `ProductDefinition` that declares its CRD coordinates, parameters, and validation rules. The scheduler, tiers, experiments, and analytics are **product-agnostic** — they work identically for any registered product.

### Registered Products

| Product | Kind | API Version | Description |
|---------|------|-------------|-------------|
| `mysql` | `MySQLInstanceClaim` | `db.platform.example.org/v1alpha1` | Managed MySQL with backups, replication, failover |
| `webapp` | `WebAppClaim` | `compute.platform.example.org/v1alpha1` | Web application compute with auto-scaling, LB, TLS |

### How It Works

```
Developer Request
       │
       ▼
┌──────────────┐     ┌───────────────┐     ┌────────────┐     ┌──────────────┐
│ Product      │────>│ Parameter     │────>│ Scheduler  │────>│ Generic      │
│ Registry     │     │ Validation    │     │ (same for  │     │ Claim Builder│
│ (lookup by   │     │ (per-product  │     │  all       │     │ (from        │
│  product     │     │  specs)       │     │  products) │     │  ProductDef) │
│  name)       │     │               │     │            │     │              │
└──────────────┘     └───────────────┘     └────────────┘     └──────────────┘
```

### Adding a New Product

To add a new product (e.g., Redis, Load Balancer, Graph DB):

**1. Define the product in `internal/products/catalog.py`:**

```python
from internal.products.registry import ProductDefinition, ParameterSpec, register_product

REDIS = ProductDefinition(
    name="redis",
    display_name="Managed Redis Cache",
    description="High-performance in-memory data store",
    api_version="cache.platform.example.org/v1alpha1",
    kind="RedisClaim",
    composition_class="redis",
    composition_group="cache.platform.example.org",
    parameters=[
        ParameterSpec(name="memory", param_type="choice",
                      choices=("256Mi", "512Mi", "1Gi", "2Gi", "4Gi")),
        ParameterSpec(name="replicas", param_type="int", min_value=1, max_value=6),
        ParameterSpec(name="persistence", param_type="bool", required=False, default=False),
    ],
)
register_product(REDIS)
```

**2. Install the corresponding Crossplane Composition in your cluster.**

**3. Done.** The generic endpoint `POST /api/services/redis` is immediately available.

### WebApp Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `image` | string | Yes | — | Container image (e.g., `registry.example.com/app:v1`) |
| `port` | int | No | `8080` | Application listen port (1–65535) |
| `cpu` | choice | No | `250m` | CPU: `125m`, `250m`, `500m`, `1000m`, `2000m`, `4000m` |
| `memory` | choice | No | `512Mi` | Memory: `256Mi`, `512Mi`, `1Gi`, `2Gi`, `4Gi`, `8Gi` |
| `replicas` | int | No | `2` | Number of replicas (1–20) |
| `ha` | bool | No | `false` | Enable HA (enforces multi_az gate) |

### Developer Flow Example

A developer deploys a web application — they only specify what they need:

```bash
curl -s -X POST http://localhost:8080/api/services/webapp \
  -H "Content-Type: application/json" \
  -d '{
    "name": "checkout-frontend",
    "namespace": "team-checkout",
    "cell": "cell-us-east",
    "tier": "medium",
    "environment": "production",
    "image": "registry.example.com/checkout:v2.1.0",
    "cpu": "500m",
    "memory": "1Gi",
    "replicas": 3
  }'
```

Behind the scenes, the control plane:
1. Looks up `webapp` in the product registry
2. Validates common fields (cell, tier, environment) and product-specific params (image, cpu, memory, replicas)
3. Checks for sticky placement (existing claim in K8s)
4. Runs the scheduler: health filter → gate filter → HA enforcement → experiment weights → scoring → ranking
5. Selects the best provider/region (e.g., AWS us-east-1)
6. Builds a `WebAppClaim` manifest with the correct Crossplane composition selector
7. Applies the claim to the cluster (or returns it in standalone mode)

The developer gets back a placement decision without ever knowing which cloud provider was selected:

```json
{
  "status": "created",
  "product": "webapp",
  "placement": {
    "provider": "aws",
    "region": "us-east-1",
    "runtimeCluster": "eks-prod-use1"
  },
  "claim": {
    "apiVersion": "compute.platform.example.org/v1alpha1",
    "kind": "WebAppClaim",
    "spec": {
      "parameters": {
        "image": "registry.example.com/checkout:v2.1.0",
        "cpu": "500m",
        "memory": "1Gi",
        "replicas": 3,
        "provider": "aws",
        "region": "us-east-1"
      }
    }
  }
}
```

The same developer can also request a MySQL database using the **same contract pattern**:

```bash
curl -s -X POST http://localhost:8080/api/services/mysql \
  -H "Content-Type: application/json" \
  -d '{
    "name": "checkout-db",
    "namespace": "team-checkout",
    "cell": "cell-us-east",
    "tier": "medium",
    "environment": "production",
    "size": "large",
    "storageGB": 100,
    "ha": true
  }'
```

Both products go through the **same scheduling pipeline** with the same HA enforcement, experiments, and analytics.

---

## Project Structure

```
idp-multicloud/
├── cmd/
│   └── controlplane/
│       └── main.py                 # Entry point — Flask server
├── internal/
│   ├── models/
│   │   └── types.py                # Data types: MySQLRequest, ServiceRequest, Candidate
│   ├── policy/
│   │   └── tiers.py                # Criticality framework: tier gates and weights
│   ├── scheduler/
│   │   ├── scheduler.py            # Gate filter, weighted scoring, health, failover
│   │   └── experiments.py          # A/B testing, feature flags, placement analytics
│   ├── products/
│   │   ├── registry.py             # Product registry: ProductDefinition, validation, claim builder
│   │   └── catalog.py              # Product catalog: MySQL, WebApp (add new products here)
│   ├── k8s/
│   │   ├── client.py               # Kubernetes dynamic client (SSA, CRUD, generic + MySQL)
│   │   └── claim_builder.py        # Crossplane MySQLInstanceClaim builder (legacy)
│   └── handlers/
│       ├── mysql.py                # MySQL-specific API (backward compatible)
│       └── services.py             # Generic multi-product API: /api/services/<product>
├── web/
│   └── index.html                  # Minimal frontend (vanilla HTML + JS)
├── tests/
│   ├── test_models.py              # Model validation tests
│   ├── test_policy.py              # Tier framework tests
│   ├── test_scheduler.py           # Scheduler logic tests
│   ├── test_claim_builder.py       # Claim builder tests
│   ├── test_handlers.py            # API endpoint tests (MySQL legacy)
│   ├── test_experiments.py         # A/B testing and feature flag tests
│   ├── test_product_registry.py    # Product registry unit tests
│   └── test_services.py            # Generic services endpoint tests
├── requirements.txt
└── README.md
```

---

## API Endpoints

### Generic Multi-Product Endpoints

#### `GET /api/products`

List all registered products and their parameter specifications.

```json
{
  "products": [
    {
      "name": "mysql",
      "display_name": "Managed MySQL",
      "description": "Managed MySQL database with automatic backups, replication, and failover.",
      "kind": "MySQLInstanceClaim",
      "parameters": [
        {"name": "size", "type": "choice", "required": true, "choices": ["small","medium","large","xlarge"]},
        {"name": "storageGB", "type": "int", "required": true},
        {"name": "ha", "type": "bool", "required": false, "default": false}
      ]
    },
    {
      "name": "webapp",
      "display_name": "Web Application",
      "kind": "WebAppClaim",
      "parameters": [
        {"name": "image", "type": "string", "required": true},
        {"name": "cpu", "type": "choice", "required": false, "choices": ["125m","250m","500m","1000m","2000m","4000m"]},
        {"name": "replicas", "type": "int", "required": false, "default": 2}
      ]
    }
  ]
}
```

#### `POST /api/services/<product>`

Create a service instance for any registered product. Same scheduling pipeline for all products.

#### `GET /api/services/<product>/<namespace>/<name>`

Query the status of an existing claim for any product.

#### `POST /api/services/<product>/<namespace>/<name>/failover`

Force rescheduling of an existing claim (override sticky placement) for any product.

### Legacy MySQL Endpoint

### `GET /health`

Health check.

```json
{"status": "ok"}
```

### `POST /api/mysql`

Create a managed MySQL instance. The control plane validates input, enforces the developer contract, checks sticky placement, runs the scheduler, and builds a Crossplane Claim.

**Request body:**

```json
{
  "name": "orders-db",
  "namespace": "team-alpha",
  "cell": "cell-us-east",
  "tier": "medium",
  "environment": "production",
  "size": "large",
  "storageGB": 100,
  "ha": true
}
```

**Forbidden fields** (rejected with 400): `provider`, `region`, `runtimeCluster`, `network`.

**Success response (201):**

```json
{
  "status": "created",
  "sticky": false,
  "placement": {
    "provider": "aws",
    "region": "us-east-1",
    "runtimeCluster": "aws-use1-prod-01",
    "network": {"vpc_id": "vpc-aws-use1", "subnet_group": "db-private-use1"}
  },
  "reason": {
    "tier": "medium",
    "rto_minutes": 120,
    "rpo_minutes": 15,
    "gates": ["pitr", "private_networking", "multi_az"],
    "ha_enforced": true,
    "weights": {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
    "selected": { "provider": "aws", "region": "us-east-1", "total_score": 0.8125 },
    "top_3_candidates": [ ... ],
    "candidates_evaluated": 7,
    "candidates_healthy": 7,
    "candidates_passed_gates": 5
  },
  "claim": { ... },
  "applied_to_cluster": false,
  "failover": null,
  "namespace": "team-alpha",
  "name": "orders-db"
}
```

**Sticky response (200):** When a Claim already exists:

```json
{
  "status": "exists",
  "sticky": true,
  "message": "Claim already exists. Returning existing placement (sticky — no rescheduling).",
  "placement": { ... },
  "reason": { ... }
}
```

### `GET /api/status/mysql/{namespace}/{name}`

Returns the full Claim object and connection Secret status. Secret values are **never** returned.

```json
{
  "claim": { ... },
  "connectionSecret": {
    "name": "orders-db-conn",
    "namespace": "team-alpha",
    "exists": false
  }
}
```

### `POST /api/mysql/{namespace}/{name}/failover`

Force rescheduling of an existing Claim (override sticky placement for DR).

**Request body (optional):**

```json
{
  "exclude_providers": ["aws"]
}
```

**Response (200):**

```json
{
  "status": "failover_complete",
  "previous_provider": "aws",
  "placement": { "provider": "gcp", "region": "us-central1", ... },
  "reason": { ... },
  "claim": { ... }
}
```

### `GET /api/providers/health`

View health status and circuit breaker state for all providers.

```json
{
  "providers": { "aws": true, "gcp": true, "oci": false },
  "circuit_breakers": {
    "aws": { "state": "closed", "failure_count": 0, "failure_threshold": 5, "cooldown_seconds": 60 }
  }
}
```

### `PUT /api/providers/{provider}/health`

Set the health status of a specific provider.

**Request body:**

```json
{ "healthy": false }
```

**Response (200):**

```json
{
  "provider": "aws",
  "healthy": false,
  "message": "Provider 'aws' marked as unhealthy"
}
```

### `GET /api/analytics`

Placement analytics summary (provider distribution, scores, experiments).

### `POST /api/experiments`

Create an A/B experiment on placement scoring weights.

**Request body:**

```json
{
  "id": "exp-cost-boost-001",
  "description": "Test higher cost weight for critical tier",
  "variant_weights": {"latency": 0.10, "dr": 0.10, "maturity": 0.20, "cost": 0.60},
  "traffic_percentage": 0.2,
  "tier": "critical"
}
```

### `GET /api/experiments`

List all active experiments.

### `DELETE /api/experiments/{id}`

Delete an experiment.

### `GET /api/flags`

List all feature flags.

### `PUT /api/flags/{name}`

Set a feature flag. Body: `{ "enabled": true }`

### `DELETE /api/flags/{name}`

Delete a feature flag.

---

## How to Run Locally

### Prerequisites

- Python 3.10+
- pip

### Install dependencies

```bash
pip install -r requirements.txt
```

### Start the server

```bash
python cmd/controlplane/main.py
```

The server starts on `http://localhost:8080`. The web UI is at `http://localhost:8080/web/`.

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `IDP_HOST` | `0.0.0.0` | Listen address |
| `IDP_PORT` | `8080` | Listen port |
| `IDP_DEBUG` | `false` | Enable Flask debug mode |

### Run tests

```bash
python -m pytest tests/ -v
```

### Standalone mode

Without a Kubernetes cluster, the API runs in **standalone mode**: claims are generated and returned in the response but not applied to a cluster. This is the expected behavior for local development.

---

## Example curl Commands

### Create a MySQL instance

```bash
curl -s -X POST http://localhost:8080/api/mysql \
  -H "Content-Type: application/json" \
  -d '{
    "name": "orders-db",
    "namespace": "default",
    "cell": "cell-us-east",
    "tier": "medium",
    "environment": "production",
    "size": "large",
    "storageGB": 100,
    "ha": true
  }' | python -m json.tool
```

### Try the cost-optimized tier

```bash
curl -s -X POST http://localhost:8080/api/mysql \
  -H "Content-Type: application/json" \
  -d '{
    "name": "analytics-db",
    "cell": "cell-eu",
    "tier": "critical",
    "environment": "dev",
    "size": "small",
    "storageGB": 20,
    "ha": false
  }' | python -m json.tool
```

### Query status

```bash
curl -s http://localhost:8080/api/status/mysql/default/orders-db | python -m json.tool
```

### Health check

```bash
curl -s http://localhost:8080/health
```

### List available products

```bash
curl -s http://localhost:8080/api/products | python -m json.tool
```

### Create a WebApp (generic endpoint)

```bash
curl -s -X POST http://localhost:8080/api/services/webapp \
  -H "Content-Type: application/json" \
  -d '{
    "name": "checkout-frontend",
    "namespace": "team-checkout",
    "cell": "cell-us-east",
    "tier": "medium",
    "environment": "production",
    "image": "registry.example.com/checkout:v2.1.0",
    "cpu": "500m",
    "memory": "1Gi",
    "replicas": 3
  }' | python -m json.tool
```

### Create MySQL via generic endpoint

```bash
curl -s -X POST http://localhost:8080/api/services/mysql \
  -H "Content-Type: application/json" \
  -d '{
    "name": "orders-db",
    "namespace": "default",
    "cell": "cell-us-east",
    "tier": "medium",
    "environment": "production",
    "size": "large",
    "storageGB": 100,
    "ha": true
  }' | python -m json.tool
```

---

## Cloud Provider Credentials

Cloud credentials are managed **entirely by Crossplane** inside the Kubernetes cluster. The control plane Python process never touches them.

### How It Works

```
K8s Secret (credentials)  ──>  ProviderConfig  ──>  Crossplane Provider  ──>  Cloud API
```

1. Credentials are stored as **Kubernetes Secrets** in the `crossplane-system` namespace.
2. A **ProviderConfig** resource references the Secret and tells the Crossplane Provider how to authenticate.
3. When the control plane applies a `MySQLInstanceClaim`, Crossplane's reconciliation loop uses the ProviderConfig to call the appropriate cloud API.

### AWS Credentials

```yaml
# Secret: AWS access keys or IAM role credentials
apiVersion: v1
kind: Secret
metadata:
  name: aws-credentials
  namespace: crossplane-system
type: Opaque
stringData:
  credentials: |
    [default]
    aws_access_key_id = AKIAIOSFODNN7EXAMPLE
    aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY

---
# ProviderConfig: tells the AWS Provider how to authenticate
apiVersion: aws.upbound.io/v1beta1
kind: ProviderConfig
metadata:
  name: default
spec:
  credentials:
    source: Secret
    secretRef:
      namespace: crossplane-system
      name: aws-credentials
      key: credentials
```

**Production recommendation**: Use IRSA (IAM Roles for Service Accounts) instead of static keys:

```yaml
apiVersion: aws.upbound.io/v1beta1
kind: ProviderConfig
metadata:
  name: default
spec:
  credentials:
    source: IRSA
```

### GCP Credentials

```yaml
# Secret: GCP service account JSON key
apiVersion: v1
kind: Secret
metadata:
  name: gcp-credentials
  namespace: crossplane-system
type: Opaque
stringData:
  credentials: |
    {
      "type": "service_account",
      "project_id": "my-project-id",
      "private_key_id": "key-id-here",
      "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n",
      "client_email": "crossplane@my-project-id.iam.gserviceaccount.com",
      "client_id": "123456789",
      "auth_uri": "https://accounts.google.com/o/oauth2/auth",
      "token_uri": "https://oauth2.googleapis.com/token"
    }

---
# ProviderConfig: tells the GCP Provider how to authenticate
apiVersion: gcp.upbound.io/v1beta1
kind: ProviderConfig
metadata:
  name: default
spec:
  projectID: my-project-id
  credentials:
    source: Secret
    secretRef:
      namespace: crossplane-system
      name: gcp-credentials
      key: credentials
```

**Production recommendation**: Use Workload Identity Federation for keyless authentication.

### OCI Credentials

```yaml
# Secret: OCI API key configuration
apiVersion: v1
kind: Secret
metadata:
  name: oci-credentials
  namespace: crossplane-system
type: Opaque
stringData:
  credentials: |
    {
      "tenancy_ocid": "ocid1.tenancy.oc1..aaaaaaaaexample",
      "user_ocid": "ocid1.user.oc1..aaaaaaaaexample",
      "fingerprint": "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
      "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n",
      "region": "us-ashburn-1"
    }

---
# ProviderConfig: tells the OCI Provider how to authenticate
apiVersion: oci.upbound.io/v1beta1
kind: ProviderConfig
metadata:
  name: default
spec:
  credentials:
    source: Secret
    secretRef:
      namespace: crossplane-system
      name: oci-credentials
      key: credentials
```

**Production recommendation**: Use OCI Instance Principals when running on OCI compute.

### Security Best Practices

| Practice | Description |
|----------|-------------|
| **Never commit credentials** | Use `kubectl create secret` or a secrets manager, never YAML files in git |
| **Use cloud-native identity** | IRSA (AWS), Workload Identity (GCP), Instance Principals (OCI) |
| **Least privilege** | Grant only the permissions Crossplane needs (e.g., `rds:CreateDBInstance`) |
| **Rotate regularly** | Automate key rotation via Vault, AWS Secrets Manager, or similar |
| **Namespace isolation** | Keep credentials in `crossplane-system`, restrict RBAC access |
| **Audit access** | Enable CloudTrail (AWS), Cloud Audit Logs (GCP), Audit (OCI) |

---

## Deploying to a Cluster

### Prerequisites

- A Kubernetes cluster (v1.24+)
- Helm 3
- `kubectl` configured to access the cluster

### Step 1: Install Crossplane

```bash
helm repo add crossplane-stable https://charts.crossplane.io/stable
helm repo update

helm install crossplane crossplane-stable/crossplane \
  --namespace crossplane-system \
  --create-namespace \
  --wait
```

### Step 2: Install Cloud Providers

```bash
kubectl apply -f - <<EOF
apiVersion: pkg.crossplane.io/v1
kind: Provider
metadata:
  name: provider-aws-rds
spec:
  package: xpkg.upbound.io/upbound/provider-aws-rds:v1.5.0
---
apiVersion: pkg.crossplane.io/v1
kind: Provider
metadata:
  name: provider-gcp-sql
spec:
  package: xpkg.upbound.io/upbound/provider-gcp-sql:v1.5.0
---
apiVersion: pkg.crossplane.io/v1
kind: Provider
metadata:
  name: provider-oci-mysql
spec:
  package: xpkg.upbound.io/upbound/provider-oci-mysql:v0.5.0
EOF

# Wait for providers to become healthy
kubectl get providers -w
```

### Step 3: Configure Credentials

Create the Secrets and ProviderConfigs for each cloud provider (see [Cloud Provider Credentials](#cloud-provider-credentials) above).

```bash
# Example: create AWS credentials from a file
kubectl create secret generic aws-credentials \
  --namespace crossplane-system \
  --from-file=credentials=./aws-credentials.ini

# Apply the ProviderConfig
kubectl apply -f provider-config-aws.yaml
```

Repeat for GCP and OCI.

### Step 4: Install the MySQLInstanceClaim CRD

The Crossplane XRD (CompositeResourceDefinition) defines the claim schema:

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: CompositeResourceDefinition
metadata:
  name: mysqlinstanceclaims.db.platform.example.org
spec:
  group: db.platform.example.org
  names:
    kind: MySQLInstanceClaim
    plural: mysqlinstanceclaims
  claimNames:
    kind: MySQLInstanceClaim
    plural: mysqlinstanceclaims
  versions:
    - name: v1alpha1
      served: true
      referenceable: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                parameters:
                  type: object
                  properties:
                    cell: { type: string }
                    environment: { type: string }
                    tier: { type: string }
                    provider: { type: string }
                    region: { type: string }
                    size: { type: string }
                    storageGB: { type: integer }
                    ha: { type: boolean }
                    network: { type: object }
```

### Step 5: Create Compositions (one per provider)

Each Composition maps a `MySQLInstanceClaim` to provider-specific managed resources:

```yaml
# composition-mysql-aws.yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: mysql-aws
  labels:
    db.platform.example.org/provider: aws
    db.platform.example.org/class: mysql
spec:
  compositeTypeRef:
    apiVersion: db.platform.example.org/v1alpha1
    kind: MySQLInstanceClaim
  resources:
    - name: rds-instance
      base:
        apiVersion: rds.aws.upbound.io/v1beta1
        kind: Instance
        spec:
          forProvider:
            engine: mysql
            engineVersion: "8.0"
            instanceClass: db.t3.medium
            allocatedStorage: 50
            publiclyAccessible: false

---
# composition-mysql-gcp.yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: mysql-gcp
  labels:
    db.platform.example.org/provider: gcp
    db.platform.example.org/class: mysql
spec:
  compositeTypeRef:
    apiVersion: db.platform.example.org/v1alpha1
    kind: MySQLInstanceClaim
  resources:
    - name: cloudsql-instance
      base:
        apiVersion: sql.gcp.upbound.io/v1beta1
        kind: DatabaseInstance
        spec:
          forProvider:
            databaseVersion: MYSQL_8_0
            settings:
              - tier: db-f1-micro

---
# composition-mysql-oci.yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: mysql-oci
  labels:
    db.platform.example.org/provider: oci
    db.platform.example.org/class: mysql
spec:
  compositeTypeRef:
    apiVersion: db.platform.example.org/v1alpha1
    kind: MySQLInstanceClaim
  resources:
    - name: mds-instance
      base:
        apiVersion: mysql.oci.upbound.io/v1beta1
        kind: MysqlDbSystem
        spec:
          forProvider:
            shapeName: MySQL.VM.Standard.E3.1.8GB
```

The `compositionSelector.matchLabels` in the Claim (set automatically by the control plane) selects the correct Composition:
- `db.platform.example.org/provider: aws` → `mysql-aws`
- `db.platform.example.org/provider: gcp` → `mysql-gcp`
- `db.platform.example.org/provider: oci` → `mysql-oci`

### Step 6: Deploy the Control Plane

```bash
# Build container
docker build -t idp-controlplane:latest .

# Deploy to Kubernetes
kubectl create deployment idp-controlplane --image=idp-controlplane:latest
kubectl expose deployment idp-controlplane --port=8080

# The control plane needs RBAC to manage MySQLInstanceClaims
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: idp-controlplane
rules:
  - apiGroups: ["db.platform.example.org"]
    resources: ["mysqlinstanceclaims"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["compute.platform.example.org"]
    resources: ["webappclaims"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get"]  # only check existence, never read data
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: idp-controlplane
subjects:
  - kind: ServiceAccount
    name: default
    namespace: default
roleRef:
  kind: ClusterRole
  name: idp-controlplane
  apiGroup: rbac.authorization.k8s.io
EOF
```

### End-to-End Verification

```bash
# Verify Crossplane is healthy
kubectl get providers

# Verify XRD is installed
kubectl get xrd mysqlinstanceclaims.db.platform.example.org

# Create an instance via the API
curl -X POST http://<control-plane-ip>:8080/api/mysql \
  -H "Content-Type: application/json" \
  -d '{"name":"test-db","cell":"cell-us","tier":"medium","environment":"dev","size":"small","storageGB":20,"ha":false}'

# Watch the Claim being reconciled
kubectl get mysqlinstanceclaims -w

# Check the placement reason annotation
kubectl get mysqlinstanceclaim test-db \
  -o jsonpath='{.metadata.annotations.platform\.example\.org/placement-reason}' | python -m json.tool
```

---

## Next Steps

### More Products
- Add Redis, PostgreSQL, Load Balancer, Graph DB, Cache products to `catalog.py`.
- Each product only requires a `ProductDefinition` + Crossplane Composition.

### AuthN / AuthZ
- Integrate OIDC/OAuth2 for API authentication.
- Implement RBAC to restrict which teams can provision to which cells and tiers.
- Add namespace-level authorization checks.

### OPA / Policy-as-Code
- Use Open Policy Agent (OPA) to externalize placement policies.
- Allow operators to define custom gate rules and scoring overrides via Rego policies.
- Enforce cost budgets and quota limits per cell/team.

### Telemetry / Tracing / Metrics
- Add OpenTelemetry instrumentation for distributed tracing.
- Export Prometheus metrics: scheduling latency, placement distribution, gate rejection rates.
- Build Grafana dashboards for control plane observability.

### Additional Features
- Multi-cluster federation for global placement.
- Cost estimation and chargeback integration.
- GitOps workflow integration (ArgoCD, Flux).
