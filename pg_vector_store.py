"""
PostgreSQL + pgvector vector store.

PostgreSQL is the single store for historical knowledge, and this module is its vector search. Knowledge lives in two
tables, searched in layers:

  tenant layer   client_knowledge_chunks (and incidents promoted by a tenant) for the requesting tenant
  learned layer  incidents with knowledge_scope = 'global_learned' (patterns learned across tenants)
  base layer     incidents with knowledge_scope = 'global_base' (the curated incidents every tenant starts from)

`search_multi_scope` returns the same row shape the earlier Qdrant version did, so retrieval code is unchanged apart from
naming layers instead of collections.

Run as a script to add the schema and embed rows that do not have a vector yet:
    python pg_vector_store.py --migrate           # add columns, indexes
    python pg_vector_store.py --backfill          # embed rows with no embedding (resumable)
    python pg_vector_store.py --status            # counts
    python pg_vector_store.py --check-recall      # HNSW index versus exact search
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

VECTOR_DIM = int(os.getenv("AIOPS_VECTOR_DIM", "384"))
# How many candidates the HNSW index examines per search: higher is more accurate and a little slower.
EF_SEARCH = int(os.getenv("AIOPS_HNSW_EF_SEARCH", "200"))
LAYERS = ("tenant", "global_learned", "global_base")
SCOPE_LAYERS = {
    "client_private": ("tenant", "global_learned", "global_base"),
    "global_learned": ("global_learned", "global_base"),
    "global_base": ("global_base",),
}

SCHEMA_SQL = f"""
-- parallel index builds need more shared memory than a default container has; build serially
SET max_parallel_maintenance_workers = 0;
-- an HNSW graph that does not fit in maintenance_work_mem is built badly (recall collapses), so give the build room
SET maintenance_work_mem = '1GB';
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE incidents ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS knowledge_scope TEXT NOT NULL DEFAULT 'global_base';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS embedding vector({VECTOR_DIM});
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;

ALTER TABLE client_knowledge_chunks ADD COLUMN IF NOT EXISTS embedding vector({VECTOR_DIM});
ALTER TABLE client_knowledge_chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE client_knowledge_chunks ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_incidents_scope ON incidents (knowledge_scope, tenant_id);
CREATE INDEX IF NOT EXISTS idx_chunks_scope ON client_knowledge_chunks (tenant_id, knowledge_scope);
CREATE OR REPLACE FUNCTION aiops_clear_stale_embedding() RETURNS trigger AS $$
BEGIN
    -- an update that changes the embedded text but not the vector leaves a stale vector; clear it so backfill re-embeds
    IF NEW.embedding IS NOT DISTINCT FROM OLD.embedding
       AND (NEW.title, NEW.content, NEW.classification, NEW.domain)
           IS DISTINCT FROM (OLD.title, OLD.content, OLD.classification, OLD.domain) THEN
        NEW.embedding := NULL;
        NEW.embedding_model := NULL;
        NEW.embedded_at := NULL;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_incidents_stale_embedding ON incidents;
CREATE TRIGGER trg_incidents_stale_embedding BEFORE UPDATE ON incidents
    FOR EACH ROW EXECUTE FUNCTION aiops_clear_stale_embedding();
DROP TRIGGER IF EXISTS trg_chunks_stale_embedding ON client_knowledge_chunks;
CREATE TRIGGER trg_chunks_stale_embedding BEFORE UPDATE ON client_knowledge_chunks
    FOR EACH ROW EXECUTE FUNCTION aiops_clear_stale_embedding();

CREATE INDEX IF NOT EXISTS idx_incidents_embedding ON incidents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON client_knowledge_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
"""


def _connect():
    import postgres_store
    return postgres_store.get_connection()


def ping() -> bool:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("pgvector not available: %s", exc)
        return False


def ensure_schema() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)


def vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"


def embedding_text(title: Any, content: Any, classification: Any, domain: Any) -> str:
    """The text a row is embedded from (same recipe the learned-knowledge indexer has always used)."""
    parts = [str(x).strip() for x in (title, content, classification, domain) if x and str(x).strip()]
    return ". ".join(parts)[:4000]


# -- search ---------------------------------------------------------------------------------------------------------

def _layer_queries(layer: str, tenant_id: str, filters: Dict[str, Optional[str]]) -> List[Tuple[str, List[Any]]]:
    """(sql, params) pairs for one layer; each yields rows ordered by cosine distance to the query vector (%s::vector)."""
    queries: List[Tuple[str, List[Any]]] = []

    def build(table: str, where: List[str], params: List[Any], cols: Dict[str, str], source_type: str) -> Tuple[str, List[Any]]:
        for column, value in filters.items():
            if value and column in cols and cols[column] != "''":
                where.append(f"{cols[column]} = %s")
                params.append(value)
        select = ", ".join(f"{expr} AS {name}" for name, expr in cols.items())
        sql = (
            f"SELECT {select}, '{source_type}' AS source_type, 1 - (embedding <=> %s::vector) AS score "
            f"FROM {table} WHERE embedding IS NOT NULL AND {' AND '.join(where)} "
            f"ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        return sql, params

    incident_cols = {"id": "id::text", "title": "title", "content": "content", "classification": "classification",
                     "domain": "domain", "service_scope": "COALESCE(service_scope, '')",
                     "environment": "COALESCE(environment, '')", "severity": "COALESCE(severity, '')",
                     "tenant_id": "COALESCE(tenant_id, '')", "knowledge_scope": "knowledge_scope"}
    chunk_cols = {"id": "chunk_id", "title": "title", "content": "content", "classification": "COALESCE(classification, '')",
                  "domain": "COALESCE(domain, '')", "service_scope": "COALESCE(service_scope, '')",
                  "environment": "COALESCE(environment, '')", "severity": "''",
                  "tenant_id": "tenant_id", "knowledge_scope": "knowledge_scope"}

    if layer == "tenant":
        queries.append(build("client_knowledge_chunks", ["tenant_id = %s", "knowledge_scope = 'client_private'"],
                             [tenant_id], chunk_cols, "document"))
        queries.append(build("incidents", ["tenant_id = %s", "knowledge_scope = 'client_private'"],
                             [tenant_id], incident_cols, "incident"))
    elif layer == "global_learned":
        queries.append(build("incidents", ["knowledge_scope = 'global_learned'"], [], incident_cols, "incident"))
    elif layer == "global_base":
        queries.append(build("incidents", ["knowledge_scope = 'global_base'"], [], incident_cols, "incident"))
    return queries


def search_multi_scope(
    query_vector: List[float],
    limit: int = 5,
    tenant_id: str = "client_a",
    knowledge_scope: str = "client_private",
    search_order: Optional[Iterable[str]] = None,
    **filters: Optional[str],
) -> List[Dict[str, Any]]:
    """Search the layers for a tenant/scope, merge by similarity and return the best `limit` rows."""
    layers = tuple(search_order) if search_order else SCOPE_LAYERS.get((knowledge_scope or "client_private").lower(),
                                                                       SCOPE_LAYERS["client_private"])
    layers = tuple(layer for layer in layers if layer in LAYERS)
    tenant = (tenant_id or "").strip().lower().replace(" ", "_")
    wanted = {k: filters.get(k) for k in ("domain", "classification", "service_scope", "environment", "severity")}
    qv = vector_literal(query_vector)
    per_layer = max(limit, 3)
    merged: List[Dict[str, Any]] = []
    seen = set()
    with _connect() as conn, conn.cursor() as cur:
        # Tenant and scope filters are applied after the index scan; iterative scans keep scanning until `limit`
        # matching rows are found, so a small tenant is not starved by a large neighbour.
        cur.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
        cur.execute(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}")
        for layer in layers:
            for sql, params in _layer_queries(layer, tenant, wanted):
                cur.execute(sql, [qv, *params, qv, per_layer])
                names = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    r = dict(zip(names, row))
                    key = (r["source_type"], r["id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    r["score"] = float(r["score"])
                    r["source_layer"] = layer
                    r["collection_name"] = layer
                    r["knowledge_type"] = r["source_type"]
                    merged.append(r)
    merged.sort(key=lambda r: r["score"], reverse=True)
    return merged[:limit]


# -- write path (used when knowledge is promoted) ---------------------------------------------------------------------

def upsert_knowledge(
    record: Dict[str, Any],
    vector: Sequence[float],
    *,
    model: str,
    tenant_id: Optional[str],
    knowledge_scope: str,
) -> None:
    """Insert or update one learned/promoted incident with its embedding.

    knowledge_scope is 'client_private' (needs tenant_id), 'global_learned' or 'global_base'.
    """
    scope = knowledge_scope if knowledge_scope in SCOPE_LAYERS else "client_private"
    if scope == "client_private" and not tenant_id:
        raise ValueError("client_private knowledge needs a tenant_id")
    if len(vector) != VECTOR_DIM:
        raise ValueError(f"embedding dimension {len(vector)} does not match vector({VECTOR_DIM})")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO incidents (id, domain, classification, title, content, source, confidence, linked_candidate_id,
                                   service_scope, environment, severity, tenant_id, knowledge_scope,
                                   embedding, embedding_model, embedded_at)
            VALUES (%(id)s, %(domain)s, %(classification)s, %(title)s, %(content)s, %(source)s, %(confidence)s,
                    %(linked_candidate_id)s, %(service_scope)s, %(environment)s, %(severity)s, %(tenant_id)s,
                    %(knowledge_scope)s, %(embedding)s::vector, %(model)s, now())
            ON CONFLICT (id) DO UPDATE SET
                domain = EXCLUDED.domain, classification = EXCLUDED.classification, title = EXCLUDED.title,
                content = EXCLUDED.content, source = EXCLUDED.source, confidence = EXCLUDED.confidence,
                linked_candidate_id = EXCLUDED.linked_candidate_id, service_scope = EXCLUDED.service_scope,
                environment = EXCLUDED.environment, severity = EXCLUDED.severity, tenant_id = EXCLUDED.tenant_id,
                knowledge_scope = EXCLUDED.knowledge_scope, embedding = EXCLUDED.embedding,
                embedding_model = EXCLUDED.embedding_model, embedded_at = now(), updated_at = now()
            """,
            {
                "id": str(record.get("id") or record.get("incident_id") or record.get("point_id")),
                "domain": record.get("domain") or "general",
                "classification": record.get("classification") or "unknown",
                "title": record.get("title") or "",
                "content": record.get("content") or record.get("summary") or "",
                "source": record.get("source"),
                "confidence": record.get("confidence"),
                "linked_candidate_id": record.get("linked_candidate_id"),
                "service_scope": record.get("service_scope"),
                "environment": record.get("environment"),
                "severity": record.get("severity"),
                "tenant_id": tenant_id if scope == "client_private" else None,
                "knowledge_scope": scope,
                "embedding": vector_literal(vector),
                "model": model,
            },
        )


# -- migration / backfill ---------------------------------------------------------------------------------------------

TABLES = {
    "incidents": ("id", "id::text", "title, content, classification, domain"),
    "client_knowledge_chunks": ("chunk_id", "chunk_id", "title, content, classification, domain"),
}


def check_recall(samples: int = 100, k: int = 10) -> Dict[str, float]:
    """Compare index results with exact results for sample rows; a low value means the HNSW index must be rebuilt.

    A returned row counts as correct when it is at least as close as the exact k-th neighbour, so ties between
    near-identical rows (common with templated incident text) do not count against the index.
    """
    result: Dict[str, float] = {}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}")
        for table, (key, key_expr, _cols) in TABLES.items():
            cur.execute(f"SELECT embedding::text FROM {table} WHERE embedding IS NOT NULL ORDER BY random() LIMIT %s", (samples,))
            queries = [r[0] for r in cur.fetchall()]
            if not queries:
                continue
            good = total = 0
            sql = f"SELECT embedding <=> %s::vector FROM {table} WHERE embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s"
            for q in queries:
                cur.execute("SET LOCAL enable_indexscan = off")
                cur.execute("SET LOCAL enable_bitmapscan = off")
                cur.execute(sql, (q, q, k))
                exact = [r[0] for r in cur.fetchall()]
                cur.execute("SET LOCAL enable_indexscan = on")
                cur.execute("SET LOCAL enable_bitmapscan = on")
                cur.execute(sql, (q, q, k))
                found = [r[0] for r in cur.fetchall()]
                limit = exact[-1] + 1e-6
                good += sum(1 for d in found if d <= limit)
                total += k
            result[table] = round(good / total, 3)
    return result


def status() -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    with _connect() as conn, conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"SELECT count(*), count(embedding) FROM {table}")
            total, embedded = cur.fetchone()
            out[table] = {"rows": total, "embedded": embedded, "missing": total - embedded}
    return out


def backfill(batch_size: int = 256, limit: Optional[int] = None) -> Dict[str, int]:
    """Embed rows that have no vector yet. Resumable: only rows with a NULL embedding are touched."""
    from embedding_store import embed_texts
    import settings_portable as settings

    model = getattr(settings, "EMBEDDING_MODEL", "unknown")
    done = {t: 0 for t in TABLES}
    started = time.monotonic()
    for table, (key, key_expr, cols) in TABLES.items():
        while limit is None or done[table] < limit:
            with _connect() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT {key_expr}, {cols} FROM {table} WHERE embedding IS NULL ORDER BY {key} LIMIT %s",
                            (batch_size,))
                rows = cur.fetchall()
                if not rows:
                    break
                texts = [embedding_text(*r[1:]) or "empty" for r in rows]
                vectors = embed_texts(texts)
                for vec in vectors:
                    if len(vec) != VECTOR_DIM:
                        raise RuntimeError(f"embedding dimension {len(vec)} does not match vector({VECTOR_DIM}); "
                                           f"check EMBEDDING_MODEL ({model})")
                from psycopg2.extras import execute_values
                execute_values(
                    cur,
                    f"UPDATE {table} AS t SET embedding = v.emb::vector, embedding_model = v.model, embedded_at = now() "
                    f"FROM (VALUES %s) AS v(k, emb, model) WHERE {key_expr.replace(key, 't.' + key, 1)} = v.k",
                    [(r[0], vector_literal(vec), model) for r, vec in zip(rows, vectors)],
                    template="(%s, %s, %s)",
                    page_size=batch_size,
                )
            done[table] += len(rows)
            if done[table] % (batch_size * 10) == 0:
                LOGGER.info("%s: %d embedded (%.0fs)", table, done[table], time.monotonic() - started)
    return done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--check-recall", action="store_true", help="compare HNSW results with exact results")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    if args.migrate:
        ensure_schema()
        print("schema ready")
    if args.backfill:
        print(backfill(limit=args.limit))
    if args.check_recall:
        print("recall@10 (1.0 is exact):", check_recall())
    if args.status or not (args.migrate or args.backfill or args.check_recall):
        print(status())
