# Backlog IDP Multicloud

Backlog estructurado por épicas para evolucionar el IDP Multicloud Control Plane.

---

## EPIC 1: Orquestación y Atomicidad

> Inspirado en componentes como "Little Monster" y Cluster API wrappers que mantienen atomicidad en operaciones multi-step.

| # | Story | Descripción | Prioridad |
|---|-------|-------------|-----------|
| 1.1 | **Saga Orchestrator** | Motor de workflows multi-step para provisioning: `validate → schedule → apply_claim → wait_ready → configure_networking → register_dns → notify`. Si falla un paso, ejecutar compensación (rollback). | Alta |
| 1.2 | **State Machine por recurso** | Cada recurso (mysql, webapp) debe tener estados: `PENDING → PROVISIONING → READY → DEGRADED → FAILED → DELETING`. Persistir estado y transiciones. | Alta |
| 1.3 | **Cluster API Wrapper** | Abstracción sobre Cluster API para lifecycle de clusters runtime (create, upgrade, scale, decommission). El IDP no solo consume clusters, también los gestiona. | Media |
| 1.4 | **Dependency Graph** | Resolver dependencias entre recursos. Ej: WebApp depende de MySQL + Redis. Provisionar en orden topológico, destruir en orden inverso. | Media |
| 1.5 | **Idempotent Operations** | Garantizar que re-ejecutar cualquier operación produce el mismo resultado. Idempotency keys en cada request. | Alta |
| 1.6 | **Reconciliation Loop** | Proceso background que compara estado deseado vs estado actual y corrige drift (similar a un controller de K8s). | Media |

---

## EPIC 2: Deploy & Traffic Management

> Blue/green, canary, safe deployment, y migration multi-vendor.

| # | Story | Descripción | Prioridad |
|---|-------|-------------|-----------|
| 2.1 | **Deployment Strategies** | Agregar campo `strategy` a los productos: `rolling` (default), `blue_green`, `canary`. Cada estrategia define cómo se promueven las versiones. | Alta |
| 2.2 | **Blue/Green para Stateless** | Para WebApp: crear versión "green", validar health, switch traffic 100%, destruir "blue". Rollback instantáneo si falla health check. | Alta |
| 2.3 | **Canary con Traffic Splitting** | Evolucionar el A/B engine actual → traffic splitting real: 5% → 25% → 50% → 100%. Integrar con métricas para auto-promote o auto-rollback. | Alta |
| 2.4 | **Safe Deployment Gates** | Pipeline de validación pre-promote: health checks, error rate < threshold, latency p99 < SLO, custom checks. Si no pasa → rollback automático. | Alta |
| 2.5 | **Multi-Vendor Migration** | Workflow para migrar un recurso de un cloud provider a otro sin downtime: provision en destino → sync data → switch traffic → decommission origen. | Media |
| 2.6 | **Version Tracking** | Cada recurso mantiene historial de versiones desplegadas con timestamps, strategy usada, y resultado (success/rollback). | Media |
| 2.7 | **Traffic Policies** | Definir reglas de traffic management: weighted routing, header-based routing, geo-based routing para canary segmentado. | Baja |

---

## EPIC 3: Operación GitOps + Cluster Lifecycle

> GitOps como base operativa y automatización del lifecycle de clusters.

| # | Story | Descripción | Prioridad |
|---|-------|-------------|-----------|
| 3.1 | **GitOps State Store** | Persistir el estado deseado de todos los recursos en un repo Git. Cada `POST /api/services/*` genera un commit. El reconciler lee de Git, no de la API. | Alta |
| 3.2 | **Declarative API** | Además de la API imperativa actual (`POST /api/mysql`), soportar manifiestos declarativos YAML que se aplican via Git push. | Alta |
| 3.3 | **ArgoCD/Flux Integration** | Integrar con ArgoCD o Flux para sync automático del estado deseado desde Git al cluster. El IDP genera los manifiestos, GitOps los aplica. | Media |
| 3.4 | **Drift Detection** | Comparar estado declarado en Git vs estado real en clusters. Alertar y auto-remediar si hay drift. | Media |
| 3.5 | **Cluster Lifecycle Automation** | Motor de gestión automática de clusters: auto-upgrade de K8s versions, auto-scale de node pools, auto-patch de seguridad, decommission de clusters vacíos. | Media |
| 3.6 | **Cluster Health Scoring** | Extender el scoring actual de providers → scoring de clusters individuales. Factores: utilización, versión K8s, certificados, estado de nodos. | Baja |
| 3.7 | **Change Audit Log** | Log inmutable de todos los cambios: quién, qué, cuándo, desde dónde, aprobado por quién. Base para compliance. | Alta |
| 3.8 | **Rollback por Git Revert** | Si un despliegue falla, hacer `git revert` del commit genera rollback automático via reconciler. | Media |

---

## EPIC 4: Patrón de Servicios Internos

> SDKs, API unificada, operación administrada, policies por criticidad.

| # | Story | Descripción | Prioridad |
|---|-------|-------------|-----------|
| 4.1 | **Python SDK** | SDK que encapsula la API REST: `idp.mysql.create(cell="cell-us", tier="critical", ...)`. Type-safe, con autocompletado, retry/backoff incluido. | Alta |
| 4.2 | **CLI Tool** | `idp create mysql --cell cell-us --tier medium --size large`. Wrapper CLI sobre la API para operadores y pipelines CI/CD. | Alta |
| 4.3 | **Unified Service Contract** | Estandarizar que TODO servicio interno del IDP sigue el mismo contrato: `create`, `get`, `update`, `delete`, `failover`, `migrate`, `status`. Ya tenemos la base con el product registry. | Alta |
| 4.4 | **Managed Operations** | Operaciones administradas por producto: backup, restore, scale, rotate-credentials, upgrade-version. El developer no ejecuta, solo solicita. | Media |
| 4.5 | **Policy Engine (OPA)** | Externalizar las policies (tiers, gates, quotas) a Open Policy Agent. Permite modificar reglas sin redesplegar el control plane. | Media |
| 4.6 | **Quotas & Cost Budgets** | Límites por cell/team/namespace: max instancias, max storage, max cost mensual. Rechazar requests que excedan quota. | Media |
| 4.7 | **Self-Service Catalog** | UI/Portal donde developers ven todos los productos disponibles, sus tiers, parámetros, y pueden provisionar directamente. Evolución del web/index.html actual. | Media |
| 4.8 | **Chargeback & Cost Attribution** | Asociar costo estimado a cada recurso. Reports por cell, team, tier. Integrar con pricing APIs de AWS/GCP/OCI. | Baja |
| 4.9 | **SLA Dashboard** | Dashboard que muestra cumplimiento de RTO/RPO por tier, uptime por provider, health score de la plataforma. | Baja |

---

## EPIC 5: Observabilidad y Persistencia

> Prerequisitos cross-cutting para producción.

| # | Story | Descripción | Prioridad |
|---|-------|-------------|-----------|
| 5.1 | **Persistent State Store** | Migrar estado in-memory → PostgreSQL/SQLite. Hoy experiments, analytics, health, circuit breakers se pierden al reiniciar. | Crítica |
| 5.2 | **OpenTelemetry Traces** | Instrumentar el scheduling pipeline completo: cada stage como span. Trace ID propagado al claim como annotation. | Alta |
| 5.3 | **Prometheus Metrics** | Exportar: `idp_placements_total`, `idp_gate_rejections_total`, `idp_scheduling_duration_seconds`, `idp_circuit_breaker_state` por provider. | Alta |
| 5.4 | **Structured Logging** | JSON logging con correlation IDs para trazabilidad end-to-end. | Media |
| 5.5 | **Auth & RBAC** | OIDC authentication + RBAC: developer (create en su namespace), platform-eng (health, experiments, flags), admin (todo). | Alta |

---

## Roadmap Sugerido

```
Fase 1 - Fundaciones              Fase 2 - Deploy & Ops            Fase 3 - Self-Service
───────────────────────            ───────────────────────           ───────────────────────
5.1 Persistent State               2.1 Deploy Strategies             4.7 Self-Service Catalog
5.5 Auth & RBAC                    2.2 Blue/Green                    4.6 Quotas & Cost
1.2 State Machine                  2.3 Canary Traffic                3.5 Cluster Lifecycle
1.1 Saga Orchestrator              2.4 Safe Deploy Gates             4.8 Chargeback
1.5 Idempotent Ops                 3.1 GitOps State Store            4.9 SLA Dashboard
4.1 Python SDK                     3.2 Declarative API               2.7 Traffic Policies
4.2 CLI Tool                       3.7 Audit Log                     3.6 Cluster Health
5.2 OTel Traces                    2.5 Multi-Vendor Migration        1.3 Cluster API Wrapper
5.3 Prometheus Metrics             1.4 Dependency Graph
4.3 Unified Contract               3.4 Drift Detection
```

---

## Mapeo a Conceptos de Referencia

| Concepto | Stories |
|----------|---------|
| **Atomicidad / Orquestación (Little Monster)** | 1.1 Saga, 1.2 State Machine, 1.4 Dependency Graph, 1.5 Idempotency |
| **Cluster API wrapper** | 1.3 Cluster API Wrapper, 3.5 Cluster Lifecycle, 3.6 Cluster Health |
| **Blue/green / Canary / Safe deployment** | 2.1–2.4 (Deploy strategies, B/G, Canary, Safe gates) |
| **Migration multi-vendor** | 2.5 Multi-Vendor Migration, 2.6 Version Tracking |
| **GitOps como base** | 3.1–3.4, 3.7–3.8 (GitOps store, declarative API, ArgoCD, drift, audit, revert) |
| **Lifecycle de clusters** | 3.5 Cluster Lifecycle, 1.3 Cluster API, 1.6 Reconciliation |
| **SDKs** | 4.1 Python SDK, 4.2 CLI |
| **API unificada** | 4.3 Unified Contract (parcialmente implementado con product registry) |
| **Operación administrada** | 4.4 Managed Operations, 1.2 State Machine |
| **Policies por criticidad** | 4.5 OPA Engine, 4.6 Quotas (parcialmente implementado con tiers) |

---

## Resumen

- **5 Épicas**, **37 Stories**
- **Fase 1** (Fundaciones): persistencia, auth, state machine, saga, SDK, CLI, observabilidad
- **Fase 2** (Deploy & Ops): blue/green, canary, safe deploy, GitOps, audit, migration
- **Fase 3** (Self-Service): catalog UI, quotas, chargeback, SLA dashboard, cluster health
