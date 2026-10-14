# vast.ai deployment check (2026-10-14)

Run locally from the repository's own deployment files, on arm64, with an isolated compose project (`vastest`, different
container names and ports, removed afterwards). The live stack was only read from (the export step).

## What was exercised
- Images built from `infra/Dockerfile.vastai` (backend and frontend targets), with the model baked in.
- `infra/docker-compose.vastai.yml` services `postgres`, `app`, `ui` started from compose; `app` reported healthy.
- `scripts/export_postgres.sh` against the live database (400 MB plain-SQL dump including embeddings), then
  `scripts/import_postgres.sh` into the new compose PostgreSQL: 39,423 incidents, 135,506 chunks, all embedded, both HNSW
  indexes and the `vector` / `pg_prewarm` extensions present.
- App start-up: embedding model pre-warm OK, vector warm-up 2.5 s for all tenants; a `/analyze` call for `poc_client` returned
  `vpn_client_failure` with five pgvector matches; the UI container reached the backend and analysed an incident.

## Defects found and fixed
1. The compose file, `.env.vastai.example` and `setup_vastai.sh` set `EMBEDDING_MODEL` to a nomic name that is not a valid
   in-process model, so vector search would have silently failed. Now `sentence-transformers/all-MiniLM-L6-v2`.
2. The UI container set `AIOPS_BACKEND_URL` but the UI reads `BACKEND_URL` (default `127.0.0.1:8001`), so it could not reach the
   backend. `BACKEND_URL` is now set in compose and in the generated `.env`.
3. A fresh database, or one restored from an older dump, had no vector columns; the API now adds the schema at start-up.
4. The UI started on a hard-coded tenant (`client_c`) and showed a hard-coded "PYPL" badge; the vast.ai POC would have opened
   on the wrong client. The UI now uses the same default tenant as the API (`poc_client` in the vast.ai compose) and shows the
   active tenant's display name. `tests/test_default_tenant.py` guards both.
5. The Dockerfile pinned an x86-only CPU torch wheel; `TORCH_SPEC` / `TORCH_INDEX` build arguments now allow an arm64 build
   (defaults unchanged for vast.ai).

## Not verified
- The real x86_64 build with `torch==2.4.1+cpu` (this machine cannot run or build amd64 images).
- Groq as the model provider, the lab containers, the backtesting scheduler and `setup_vastai.sh` end to end.
- `export_postgres.sh` needs `python3` on the host for its manifest step (absent in the PostgreSQL client image used here).
- The import's JSON-knowledge step could not write into the read-only mount used for this test.
- Start-up logs `PostgreSQL schema bootstrap failed (non-fatal): relation "resolutions" does not exist`; the same happens on the
  live database and predates this work.
- The dump is large because embeddings are stored as text in plain SQL (400 MB compressed); transfer time on vast.ai matters.
