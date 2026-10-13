# Wave 5 baseline - 2026-10-13

Recorded before the pgvector migration so that migration can be compared against it.

## Environment
- Code: `agentic-v2` at the commit recorded in the plan doc (tests ran from a `git archive` export).
- Test environment: Linux arm64 container `aiops-baseline-env` (`python:3.12-slim` + `tesseract-ocr`), built from `requirements.txt`; installed versions in `wave5_env_freeze_2026-10-13.txt`.
- Why a container: `psycopg2-binary==2.9.11` has no Windows-ARM64 wheel and fails to build without `pg_config`; this is also why the Windows project `.venv` never had a PostgreSQL driver.
- Services: scratch database `aiops_scratch` (restored from a `pg_dump` taken 2026-10-13) on `aiops-postgres` (PostgreSQL 16.13), and scratch Qdrant `aiops-qdrant-scratch` (restored from snapshots). The live `aiops` database and live Qdrant were not written to.
- Backups: `C:\Projects\AI\backups\2026-10-13_pre-wave5\` (dump, 5 Qdrant snapshots, SHA256SUMS), outside the repo.

## Data under test
| Store | Content |
|---|---|
| PostgreSQL `incidents` (no tenant column) | 39,423 rows, 176 classifications |
| PostgreSQL `client_knowledge_chunks` | `client_a` 135,130; `client_c` 376 |
| PostgreSQL `client_ticket_records` | `client_a` 4,029 |
| Qdrant `aiops_knowledge` | 5,000 vectors (all ids present in `incidents`) covering 80 of 176 classifications |
| Qdrant `client_a_index` / `client_c_index` | 60,785 / 376 |
| Qdrant `global_base_index` / `global_learned_patterns_index` | 0 / 0 |
| Rows under tenant `default` | 0 in every table with `tenant_id`; `resolution_outcomes` table does not exist yet |

## Results
- Full suite in the container: **241 tests, all passed, 0 skipped** (the 11 Streamlit tests ran here; they skip in the Windows `.venv`).
- Test-isolation defect found and fixed: three egress-policy tests assumed no PostgreSQL driver; with a reachable database the audit correctly goes to PostgreSQL and the tests read an empty JSONL file.
- Manual browser session of the Streamlit UI done (see below).

## Findings that matter for the migration
1. **Embedding mismatch.** Default config embeds with LM Studio `nomic-embed-text-v1.5` (768-dim); all Qdrant collections are 384-dim (`sentence-transformers/all-MiniLM-L6-v2`). With default settings every vector query fails with "vector size mismatch" and retrieval silently falls back to lexical results. Baseline numbers below use `EMBEDDING_BACKEND=sentence_transformers EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2`.
2. **Curated coverage gap.** 96 of 176 classifications (32,811 of 39,423 incidents) have no vector in `aiops_knowledge`, so they cannot be found by similarity search.
3. **Post-filter attrition.** On six sample tickets x two tenants, raw retrieval returns 5 hits each, but after the application's relevance/contradiction filtering only 0-1 remain (`wave5_retrieval_2026-10-13.json`). Sample tickets whose curated class has no vectors (ERP queue, integration, release) return a nearby but different class.
4. Lexical search reads `data/incidents.json`, which is absent in this tree.

## How to use this baseline
After migration, re-run the same six tickets for both tenants and the same suite; retrieval must be at least as good, and findings 1-2 should be resolved.

## Manual UI session (2026-10-13, scratch services, dev-open API)
- All nine pages load without exceptions; all four tenants (PFG, Demo Corp, PYPL, POC Client) appear in the selector.
- Incident Console analysis, Resolution Queue create -> Safety Gate -> approve (blocked until an override reason is typed when the gate has warnings) -> simulated execute all worked. The approval record carried the principal, reason, plan hash and acknowledged warnings.
- Not exercised: live execution (no connectors started), real authentication (API ran open), screenshots (pane window hidden; the DOM was inspected instead).

### Defects found and fixed (same day)
1. Queue entries were saved under a fixed default tenant (`client_a`) regardless of the active tenant. Fix: `tenant_id` on the queue request, validated against known tenants and the principal's allowed tenants; the UI sends the active tenant; the autonomy intent is judged for that tenant. Tests: `tests/test_queue_tenant.py`. Re-verified in the UI: a PYPL session now queues under `client_c`.
2. The Safety Gate banner said "PASSED" for a gate with warnings. It now says WARNING.
3. The onboarding header always defaulted to "PFG / client_a"; it now starts on the active tenant.

### Still open
- The two default-tenant settings differ (`app.py` defaults to `client_c`, `automation_engine.py` to `client_a`); callers that omit a tenant still get the engine default.
- Contradictory "no approved automation" / "approved automation exists" notices in the Incident Console.
