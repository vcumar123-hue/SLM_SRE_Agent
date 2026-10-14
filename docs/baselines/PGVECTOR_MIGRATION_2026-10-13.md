# pgvector migration - validation on scratch services (2026-10-13)

Compared against `WAVE5_BASELINE_2026-10-13.md` (Qdrant, same six sample tickets, tenants `client_a` and `client_c`).
Nothing here has been applied to the live database yet.

## What changed
- Vector search moved from Qdrant to PostgreSQL (pgvector 0.8.7). Code: `pg_vector_store.py`, `retrieval_engine.py`, `knowledge_manager.py`.
- Layers: tenant (`client_knowledge_chunks` + tenant-promoted `incidents`), learned, base (`incidents`). `incidents` gained `tenant_id`, `knowledge_scope`, `embedding`; chunks gained `embedding`. A trigger clears an embedding when its text changes.
- Embeddings: `sentence-transformers/all-MiniLM-L6-v2`, 384-dim, in-process (the old default, nomic via LM Studio, produced 768-dim vectors that could not query the existing 384-dim data). Model is loaded once per process.
- All 39,423 incidents and 135,506 chunks embedded (about 2.5 hours on CPU). Qdrant client, loaders and every `aiops_knowledge` reference removed from the code; a test enforces it. The Qdrant collections themselves are untouched.

## Results (scratch pgvector server, full data)
| Ticket (both tenants) | Before: usable hits after filtering | After |
|---|---|---|
| cpu | 1 (`cpu_saturation`) | 5-6 (`cpu_saturation`) |
| vpn | 1 (`vpn_client_failure`) | 6 (`vpn_client_failure`) |
| sap_login | 0-1 (wrong class `password_reset_loop`) | 5 (`sap_instance_or_service_unavailable`) |
| erp_queue | 0 (wrong class `queue_backlog_growth`) | 5 (`erp_interface_queue_stuck`) |
| integration | 0 (wrong class) | 5 (`downstream_integration_failure`) |
| release | 0 (wrong class) | 5 (`deployment_or_release_failure`) |

- Index recall vs exact search: incidents 0.996, chunks 0.984 (`python pg_vector_store.py --check-recall`).
- Latency once warm: about 0.2-0.6 s per ticket end to end. The first query after start-up was slow (model load, cold index: 10-27 s).
- Tenant layer verified: `client_a` and `client_c` queries return only their own tenant's chunks.
- Tests: 275 in the Linux container against the scratch server (live pgvector tests ran separately against an empty-schema database: 28 passed including tenant isolation, ordering, stale-embedding trigger, idempotent writes and small-tenant starvation).

## Defects found during validation
1. An HNSW index built with too little `maintenance_work_mem` returned near-random neighbours (recall collapse, wrong classes). The schema now sets 1 GB and `ef_construction=128`; `--check-recall` guards it.
2. Filtered index scans can starve a small tenant behind a large one; searches now use pgvector iterative scans.
3. The sentence-transformers model was reloaded on every call; fixed.
4. Index builds in a default container fail on shared memory when parallel; the schema builds serially.

## Not yet done
- Live cutover: swap `aiops-postgres` to the pgvector image on its existing volume, run `--migrate` and `--backfill` (about 2.5 hours on CPU), build the indexes, run `--check-recall`, repeat the snapshot.
- Deployment files (compose, vast.ai image, export/import scripts) were edited but not exercised end to end.
- Stored chunk and incident embeddings use the text recipe `title. content. classification. domain`; the historical Qdrant vectors were built with mixed recipes.

## Live cutover (2026-10-13)
- Backups: `C:/Projects/AI/backups/2026-10-13_pre-pgvector-cutover/` (fresh live dump, and the scratch dump that already held the embeddings). Live data was identical to the scratch source (row counts and checksums of `incidents` and `client_knowledge_chunks` matched).
- The live `aiops-postgres` container was replaced by a pgvector one on a new volume (`infra_pgvector_data`) and the dump was restored into it, rather than reusing the old data directory: the old image is built on a newer Debian than the pgvector image, and reusing a data directory across C-library versions can corrupt text indexes. The previous container is kept stopped as `aiops-postgres-pre-pgvector` with its volume `infra_postgres_data` untouched (rollback).
- Result on the live database: 39,423 incidents and 135,506 chunks, all embedded; both HNSW indexes present; recall at ef_search=200 about 0.97-0.99 (tie-aware `--check-recall`); the six-ticket snapshot returns the correct curated class for all six tickets for both tenants (200-300 ms per ticket once warm; the first query after start-up is slow while the model and index load). `release` now returns `rollback_failure` first on the live index (`deployment_or_release_failure` on the scratch index): both are relevant and the difference is approximate-index ordering between near-identical classes.
- The Qdrant containers and the `aiops_knowledge` collection are untouched and no longer used by the code.
- Rollback: stop `aiops-postgres`, rename it, rename `aiops-postgres-pre-pgvector` back to `aiops-postgres` and start it (the previous code on `release-1` expects Qdrant).

## Follow-ups done the same day
- Start-up warm-up: the API now loads the embedding model, prewarms both HNSW indexes (`pg_prewarm`) and runs one search per configured tenant in a background thread (`AIOPS_WARMUP_VECTOR=false` disables it). Measured against the live database: 2.2 s for all four tenants, so the first user query no longer pays the 10-25 s cold start. This added the `pg_prewarm` extension to the live database.
- The model cache previously reloaded the embedding model on every call, which made the existing start-up pre-warm ineffective; fixed earlier in this migration.
- Removed the scratch containers (`aiops-pgvector-scratch`, `aiops-qdrant-scratch`, `aiops-ui-session`, the backfill container). The `aiops_scratch` database inside the old, stopped PostgreSQL container was left in place and goes away when that rollback container is retired (planned no sooner than a week after the cutover).

