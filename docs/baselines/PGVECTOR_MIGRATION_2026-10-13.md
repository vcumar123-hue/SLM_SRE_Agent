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
