# IDP Multicloud Control Plane (Python)

## Objective
This repository implements a **cell-based Internal Developer Platform control plane** for managed MySQL requests across AWS, GCP, and OCI.

Developers only provide:
- `cell`
- `tier` (`C0`, `C1`, `C2`)
- `environment`
- `size`
- `storageGB`
- `ha`

The control plane decides:
- cloud `provider`
- `region`
- `runtimeCluster`
- network configuration

## Architecture Overview

### 1) Cell contract (not provider contract)
The scheduler receives a cell and tier and evaluates only candidates allowed by that cell catalog. This keeps application teams cloud-agnostic.

### 2) Tiered criticality policy
Hard gates are applied first, then weighted scoring across:
- Latency
- DR
- Maturity
- Cost

### 3) Sticky placement
If a claim already exists for the same `{namespace, name}`:
- The request is not rescheduled.
- Existing placement and reason are returned.

### 4) Auditable decisions
Each created claim stores an annotation:
- `platform.example.org/placement-reason`

The value is JSON including:
- requirements and weights
- winner
- top-3 candidates with scoring breakdown
- excluded candidates with gate failure reasons

### 5) Real Kubernetes claim builder with SSA
The control plane uses Python Kubernetes Dynamic Client and server-side apply against:
- `apiVersion: db.platform.example.org/v1alpha1`
- `kind: MySQLInstanceClaim`
- `plural: mysqlinstanceclaims`

If the CRD is not installed, API returns a clear `424 Failed Dependency` message.

## Criticality Framework

### C0
- RTO: 30m
- RPO: 5m
- Requires: PITR + MultiAZ + private networking
- Weights:
  - Latency: `0.30`
  - DR: `0.30`
  - Maturity: `0.25`
  - Cost: `0.15`

### C1
- RTO: 120m
- RPO: 15m
- Requires: PITR + private networking
- Weights:
  - Latency: `0.25`
  - DR: `0.25`
  - Maturity: `0.25`
  - Cost: `0.25`

### C2
- RTO: 480m
- RPO: 60m
- Requires: private networking
- Weights (cost-prioritized):
  - Latency: `0.20`
  - DR: `0.15`
  - Maturity: `0.15`
  - Cost: `0.50`

## Cell Catalog
Current cell:
- `payments`
  - AWS `us-east-1`
  - GCP `us-central1`
  - OCI `us-ashburn-1`

Each candidate contains:
- provider
- region
- runtimeCluster
- network map (for Crossplane parameters)
- capabilities (PITR, MultiAZ, private networking)
- per-dimension baseline scores

To add new cells, edit `internal/policy/cells.py`.


## Multi-cloud Capability

The scheduler evaluates candidates across **AWS, GCP, and OCI** for each cell and picks the highest-scoring candidate after hard-gate filtering.

Current `payments` cell candidates:
- AWS `us-east-1`
- GCP `us-central1`
- OCI `us-ashburn-1`

The placement reason includes a top-3 breakdown to prove the multi-cloud decision path.

## Where Crossplane is in this repository

Crossplane integration is implemented in two places:

1. Runtime claim apply client:
   - `internal/k8s/claim_builder.py`
   - Uses Kubernetes Dynamic Client + server-side apply to create/update `MySQLInstanceClaim`.
2. Example Crossplane manifests:
   - `crossplane/mysqlinstanceclaim-crd.yaml`
   - `crossplane/composition-aws.yaml`
   - `crossplane/composition-gcp.yaml`
   - `crossplane/composition-oci.yaml`

These manifests are starter artifacts you can adapt to your provider resources.

## API

### `GET /health`
Readiness/liveness endpoint.

### `POST /api/mysql`
Creates or reuses a sticky MySQL claim.

Request body:
```json
{
  "namespace": "default",
  "name": "payments-db",
  "cell": "payments",
  "tier": "C1",
  "environment": "prod",
  "size": "medium",
  "storageGB": 50,
  "ha": true
}
```

Rules:
- Developer **must not** send `provider`, `region`, `runtimeCluster`, `network`.
- Sticky claim check runs first.
- If not existing, scheduler selects placement, then claim is applied via SSA.

### `GET /api/status/mysql/{namespace}/{name}`
Returns:
- full claim object
- `connectionSecret.exists` boolean

Secret values are never returned.

## Project Structure

- `cmd/controlplane/main.py`
- `internal/policy/`
- `internal/scheduler/`
- `internal/k8s/`
- `internal/handlers/`
- `web/`
- `README.md`
- `go.mod` (compatibility marker)

## Run Locally

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure Kubernetes access (either in-cluster config or kubeconfig).

3. Run:
```bash
python cmd/controlplane/main.py
```

4. Open UI:
- `http://localhost:8080/web/`

## Example cURL

Create or stick to existing claim:
```bash
curl -sS -X POST http://localhost:8080/api/mysql \
  -H 'content-type: application/json' \
  -d '{
    "namespace":"default",
    "name":"payments-db",
    "cell":"payments",
    "tier":"C1",
    "environment":"prod",
    "size":"medium",
    "storageGB":50,
    "ha":true
  }' | jq
```

Query status:
```bash
curl -sS http://localhost:8080/api/status/mysql/default/payments-db | jq
```

## Cluster Deployment Notes

Containerize the app and provide RBAC permissions for:
- `mysqlinstanceclaims.db.platform.example.org` (`get`, `patch`, `create` via SSA semantics)
- `secrets` (`get`)

Expose HTTP service internally or externally as desired.

## Expected Crossplane Objects

The platform expects:
1. CRD for `MySQLInstanceClaim` in group `db.platform.example.org/v1alpha1`.
2. Compositions or CompositionRevisions selected by:
   - `db.platform.example.org/provider: <aws|gcp|oci>`
   - `db.platform.example.org/class: mysql`

Without these, provisioning cannot complete and the API returns clear error messages.

## Next Steps
- Authentication and authorization for API callers.
- OPA/policy-as-code integration for organizational rules.
- Full telemetry stack: traces, metrics, and structured logs.
- DR and fallback patterns (provider outage handling, tier-aware failover automation).
