# IDP Multicloud

A cell-based Internal Developer Platform (IDP) control plane for provisioning managed MySQL across AWS, GCP, and OCI. Developers declare **what** they need; the control plane decides **where** it runs.

---

## Objective

Provide a **cell-based contract** for developers to request managed MySQL instances while the control plane autonomously decides provider, region, runtime cluster, and network placement across multiple clouds using a **tiered criticality framework**.

Developers specify only:

| Field | Description |
|-------|-------------|
| `cell` | Logical cell identifier |
| `tier` | Criticality tier (`low`, `medium`, `critical`, `business_critical`) |
| `environment` | Target environment (`dev`, `staging`, `production`) |
| `size` | Instance size (`small`, `medium`, `large`, `xlarge`) |
| `storageGB` | Storage capacity (10–65536 GB) |
| `ha` | High availability (boolean) |

The control plane decides: **provider**, **region**, **runtimeCluster**, and **network configuration**.

---

## Architecture Overview

```
Developer Request                Control Plane                    Cloud Providers
  (cell contract)            ┌──────────────────┐
                             │                  │
  POST /api/mysql  ─────────>│  Validation      │
                             │       │          │
                             │  Sticky Check    │──> K8s: existing Claim?
                             │       │          │
                             │  Gate Filter     │──> Reject candidates missing
                             │       │          │    required capabilities
                             │  Weighted Score  │──> Rank by tier weights
                             │       │          │
                             │  Claim Builder   │──> Build MySQLInstanceClaim
                             │       │          │         ┌──────────┐
                             │  SSA Apply ──────│────────>│ AWS RDS  │
                             │                  │         │ GCP SQL  │
                             └──────────────────┘         │ OCI MDS  │
                                                          └──────────┘
```

### Key Concepts

- **Cell-based contract**: Developers never choose a provider. The platform decides based on tier requirements, candidate scoring, and operational policy.
- **Sticky placement**: If a Claim already exists for `{namespace, name}`, the control plane returns the existing placement without rescheduling — preventing unnecessary migrations.
- **Auditable decisions**: Every placement stores a JSON reason annotation with the full top-3 candidate scoring breakdown, gates applied, and weights used.
- **Crossplane integration**: The control plane generates `MySQLInstanceClaim` custom resources and applies them to the cluster via server-side apply (SSA).

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

1. **Gate filtering**: Remove candidates that lack required capabilities for the tier.
2. **Weighted scoring**: For each surviving candidate, compute `total = sum(raw_score[dim] * weight[dim])`.
3. **Ranking**: Sort candidates by total score descending. Select the winner.
4. **Audit trail**: Return a top-3 breakdown with scores and sub-scores in the placement reason.

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

## Project Structure

```
idp-multicloud/
├── cmd/
│   └── controlplane/
│       └── main.py                 # Entry point — Flask server
├── internal/
│   ├── models/
│   │   └── types.py                # Data types: MySQLRequest, Candidate, PlacementDecision
│   ├── policy/
│   │   └── tiers.py                # Criticality framework: tier gates and weights
│   ├── scheduler/
│   │   └── scheduler.py            # Gate filter, weighted scoring, candidate ranking
│   ├── k8s/
│   │   ├── client.py               # Kubernetes dynamic client (SSA, CRUD)
│   │   └── claim_builder.py        # Crossplane MySQLInstanceClaim builder
│   └── handlers/
│       └── mysql.py                # Flask API route handlers
├── web/
│   └── index.html                  # Minimal frontend (vanilla HTML + JS)
├── tests/
│   ├── test_models.py              # Model validation tests
│   ├── test_policy.py              # Tier framework tests
│   ├── test_scheduler.py           # Scheduler logic tests
│   ├── test_claim_builder.py       # Claim builder tests
│   └── test_handlers.py            # API endpoint tests
├── requirements.txt
└── README.md
```

---

## API Endpoints

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
    "gates": ["pitr", "private_networking"],
    "weights": {"latency": 0.25, "dr": 0.25, "maturity": 0.25, "cost": 0.25},
    "selected": { "provider": "aws", "region": "us-east-1", "total_score": 0.8125 },
    "top_3_candidates": [ ... ],
    "candidates_evaluated": 7,
    "candidates_passed_gates": 7
  },
  "claim": { ... },
  "applied_to_cluster": false,
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

---

## Deploying to a Cluster

### Expected Crossplane Objects

Before the control plane can apply claims to a cluster, the following must be installed:

1. **Crossplane** (v1.14+)
2. **Custom CRD** for `MySQLInstanceClaim`:

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

3. **Compositions** for each provider:

```yaml
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
            # ... provider-specific configuration
```

### Deployment

```bash
# Build container
docker build -t idp-controlplane:latest .

# Deploy to Kubernetes
kubectl create deployment idp-controlplane --image=idp-controlplane:latest
kubectl expose deployment idp-controlplane --port=8080
```

---

## Next Steps

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

### DR / Fallback Patterns
- Implement automatic provider fallback when a region is unavailable.
- Add circuit breakers for cloud provider API failures.
- Support cross-region failover for `business_critical` tier instances.
- Implement placement migration for disaster recovery scenarios.

### Additional Features
- Support for PostgreSQL, Redis, and other managed services.
- Multi-cluster federation for global placement.
- Cost estimation and chargeback integration.
- GitOps workflow integration (ArgoCD, Flux).
