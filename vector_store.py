"""
vector_store.py
AI Ops Qdrant vector storage layer (foundation version, client-compatible)

Purpose:
- Introduce semantic vector search without breaking the current local retrieval flow.
- Store incident, ticket, and KB embeddings in Qdrant.
- Keep payload metadata rich enough for enterprise filtering.
- Handle Qdrant client API differences across versions.

Environment variables (optional):
    AIOPS_QDRANT_HOST=localhost
    AIOPS_QDRANT_PORT=6335
    AIOPS_QDRANT_COLLECTION=aiops_knowledge
    AIOPS_VECTOR_SIZE=384
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

LOGGER = logging.getLogger("vector_store")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


QDRANT_HOST = _env("AIOPS_QDRANT_HOST", _env("QDRANT_HOST", "localhost"))
QDRANT_PORT = int(_env("AIOPS_QDRANT_PORT", _env("QDRANT_PORT", "6335")))
QDRANT_COLLECTION = _env("AIOPS_QDRANT_COLLECTION", _env("QDRANT_COLLECTION", "aiops_knowledge"))
VECTOR_SIZE = int(_env("AIOPS_VECTOR_SIZE", "384"))


def get_client() -> QdrantClient:
    return QdrantClient(
        host=QDRANT_HOST,
        port=QDRANT_PORT,
        check_compatibility=False,
        timeout=15.0,
    )


def ping() -> bool:
    last_error = None
    for _ in range(3):
        try:
            client = get_client()
            client.get_collections()
            return True
        except Exception as exc:
            last_error = exc
    LOGGER.exception("Qdrant ping failed: %s", last_error)
    return False


def _to_qdrant_point_id(point_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aiops:{point_id}"))


def ensure_collection(
    collection_name: str = QDRANT_COLLECTION,
    vector_size: int = VECTOR_SIZE,
    distance: qm.Distance = qm.Distance.COSINE,
) -> None:
    client = get_client()
    existing = {c.name for c in client.get_collections().collections}
    if collection_name in existing:
        LOGGER.info("Qdrant collection already exists: %s", collection_name)
        return

    client.create_collection(
        collection_name=collection_name,
        vectors_config=qm.VectorParams(size=vector_size, distance=distance),
    )

    for field_name, schema in [
        ("source_type", qm.PayloadSchemaType.KEYWORD),
        ("incident_id", qm.PayloadSchemaType.KEYWORD),
        ("classification", qm.PayloadSchemaType.KEYWORD),
        ("domain", qm.PayloadSchemaType.KEYWORD),
        ("service_scope", qm.PayloadSchemaType.KEYWORD),
        ("environment", qm.PayloadSchemaType.KEYWORD),
        ("severity", qm.PayloadSchemaType.KEYWORD),
    ]:
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=schema,
            )
        except Exception:
            LOGGER.warning("Payload index creation failed or already exists for %s", field_name)

    LOGGER.info("Created Qdrant collection: %s", collection_name)


def delete_collection(collection_name: str = QDRANT_COLLECTION) -> None:
    client = get_client()
    client.delete_collection(collection_name=collection_name)
    LOGGER.info("Deleted Qdrant collection: %s", collection_name)


def _validate_vector(vector: List[float], expected_size: int = VECTOR_SIZE) -> None:
    if not isinstance(vector, list):
        raise TypeError("vector must be a list[float]")
    if len(vector) != expected_size:
        raise ValueError(f"vector size mismatch: expected {expected_size}, got {len(vector)}")


def _default_payload(
    point_id: str,
    source_type: str,
    title: str,
    content: str,
    classification: str,
    domain: str,
    service_scope: Optional[str] = None,
    environment: Optional[str] = None,
    severity: Optional[str] = None,
    extra_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "point_id": point_id,
        "incident_id": point_id,
        "source_type": source_type,
        "title": title,
        "content": content,
        "classification": classification,
        "domain": domain,
        "service_scope": service_scope or "",
        "environment": environment or "",
        "severity": severity or "",
    }
    if extra_payload:
        payload.update(extra_payload)
    return payload


def upsert_point(
    point_id: str,
    vector: List[float],
    source_type: str,
    title: str,
    content: str,
    classification: str,
    domain: str,
    service_scope: Optional[str] = None,
    environment: Optional[str] = None,
    severity: Optional[str] = None,
    extra_payload: Optional[Dict[str, Any]] = None,
    collection_name: str = QDRANT_COLLECTION,
) -> None:
    _validate_vector(vector)
    payload = _default_payload(
        point_id=point_id,
        source_type=source_type,
        title=title,
        content=content,
        classification=classification,
        domain=domain,
        service_scope=service_scope,
        environment=environment,
        severity=severity,
        extra_payload=extra_payload,
    )

    client = get_client()
    client.upsert(
        collection_name=collection_name,
        points=[
            qm.PointStruct(
                id=_to_qdrant_point_id(point_id),
                vector=vector,
                payload=payload,
            )
        ],
    )


def upsert_points(
    rows: Iterable[Dict[str, Any]],
    collection_name: str = QDRANT_COLLECTION,
    vector_size: int = VECTOR_SIZE,
) -> int:
    points: List[qm.PointStruct] = []

    for row in rows:
        point_id = str(row["id"])
        vector = row["vector"]
        _validate_vector(vector, expected_size=vector_size)
        payload = _default_payload(
            point_id=point_id,
            source_type=str(row.get("source_type", "incident")),
            title=str(row.get("title", "")),
            content=str(row.get("content", "")),
            classification=str(row.get("classification", "unknown")),
            domain=str(row.get("domain", "general")),
            service_scope=row.get("service_scope"),
            environment=row.get("environment"),
            severity=row.get("severity"),
            extra_payload=row.get("extra_payload"),
        )
        points.append(
            qm.PointStruct(
                id=_to_qdrant_point_id(point_id),
                vector=vector,
                payload=payload,
            )
        )

    if not points:
        return 0

    client = get_client()
    client.upsert(collection_name=collection_name, points=points)
    return len(points)


def _build_filter(
    domain: Optional[str] = None,
    classification: Optional[str] = None,
    source_type: Optional[str] = None,
    service_scope: Optional[str] = None,
    environment: Optional[str] = None,
    severity: Optional[str] = None,
) -> Optional[qm.Filter]:
    must = []

    def term(field: str, value: Optional[str]):
        if value:
            must.append(qm.FieldCondition(key=field, match=qm.MatchValue(value=value)))

    term("domain", domain)
    term("classification", classification)
    term("source_type", source_type)
    term("service_scope", service_scope)
    term("environment", environment)
    term("severity", severity)

    if not must:
        return None
    return qm.Filter(must=must)


def _extract_hits(result_obj: Any) -> List[Any]:
    """
    Support both:
    - client.search(...) -> list[ScoredPoint]
    - client.query_points(...) -> QueryResponse with .points
    """
    if isinstance(result_obj, list):
        return result_obj
    if hasattr(result_obj, "points"):
        return list(result_obj.points)
    return []


def search_similar(
    query_vector: List[float],
    limit: int = 5,
    collection_name: str = QDRANT_COLLECTION,
    domain: Optional[str] = None,
    classification: Optional[str] = None,
    source_type: Optional[str] = None,
    service_scope: Optional[str] = None,
    environment: Optional[str] = None,
    severity: Optional[str] = None,
    with_vectors: bool = False,
) -> List[Dict[str, Any]]:
    _validate_vector(query_vector)

    client = get_client()
    query_filter = _build_filter(
        domain=domain,
        classification=classification,
        source_type=source_type,
        service_scope=service_scope,
        environment=environment,
        severity=severity,
    )

    raw_results = None

    # Try classic search first. Different qdrant-client versions expect different
    # argument names, so we try both search_filter and query_filter safely.
    search_attempts = [
        {
            "collection_name": collection_name,
            "query_vector": query_vector,
            "limit": limit,
            "search_filter": query_filter,
            "with_payload": True,
            "with_vectors": with_vectors,
        },
        {
            "collection_name": collection_name,
            "query_vector": query_vector,
            "limit": limit,
            "query_filter": query_filter,
            "with_payload": True,
            "with_vectors": with_vectors,
        },
    ]

    # Prefer the classic search() API only when it is truly callable.
    # Newer qdrant-client versions may not expose search(), and some wrappers
    # may raise AttributeError during method access/call. Treat that as an
    # expected compatibility path and go directly to query_points() without
    # emitting repeated warnings on every /analyze request.
    search_fn = getattr(client, "search", None)

    if callable(search_fn):
        for kwargs in search_attempts:
            try:
                raw_results = search_fn(**kwargs)
                break
            except AttributeError as search_attr_err:
                LOGGER.debug(
                    "Qdrant search() unavailable; using query_points() fallback: %s",
                    search_attr_err,
                )
                raw_results = None
                break
            except Exception as search_err:
                # Most search() failures here are client-version argument mismatches.
                # Keep this at debug level so expected compatibility fallback does not
                # flood app logs or slow high-volume backtests.
                LOGGER.debug(
                    "Qdrant search() attempt failed; using query_points() fallback: %s",
                    search_err,
                )
                raw_results = None
    else:
        LOGGER.debug("Qdrant client.search() not available; using query_points() directly")
        raw_results = None

    if raw_results is None:
        query_attempts = [
            {
                "collection_name": collection_name,
                "query": query_vector,
                "limit": limit,
                "query_filter": query_filter,
                "with_payload": True,
                "with_vectors": with_vectors,
            },
            {
                "collection_name": collection_name,
                "query": query_vector,
                "limit": limit,
                "with_payload": True,
                "with_vectors": with_vectors,
            },
        ]
        for kwargs in query_attempts:
            try:
                raw_results = client.query_points(**kwargs)
                break
            except Exception as query_err:
                LOGGER.warning("Qdrant query_points() attempt failed: %s", query_err)

    if raw_results is None:
        raise RuntimeError("Qdrant search failed for all supported client/server compatibility paths")

    results = _extract_hits(raw_results)

    out: List[Dict[str, Any]] = []
    for hit in results:
        payload = getattr(hit, "payload", None) or {}
        score = getattr(hit, "score", None)
        point_id = getattr(hit, "id", None)

        out.append(
            {
                "qdrant_id": str(point_id),
                "id": payload.get("incident_id") or payload.get("point_id"),
                "score": float(score if score is not None else 0.0),
                "source_type": payload.get("source_type"),
                "incident_id": payload.get("incident_id"),
                "title": payload.get("title"),
                "content": payload.get("content"),
                "classification": payload.get("classification"),
                "domain": payload.get("domain"),
                "service_scope": payload.get("service_scope"),
                "environment": payload.get("environment"),
                "severity": payload.get("severity"),
                "payload": payload,
            }
        )
    return out

    
def delete_point(point_id: str, collection_name: str = QDRANT_COLLECTION) -> None:
    client = get_client()
    client.delete(
        collection_name=collection_name,
        points_selector=qm.PointIdsList(points=[_to_qdrant_point_id(point_id)]),
    )

def count_points(collection_name: str = QDRANT_COLLECTION) -> int:
    client = get_client()
    info = client.get_collection(collection_name=collection_name)
    return int(info.points_count or 0)


def load_json_file_rows_as_points(
    json_path: str,
    embedding_field: str = "vector",
    source_type: str = "incident",
) -> List[Dict[str, Any]]:
    path = Path(json_path)
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Expected a JSON list of objects")

    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        vector = row.get(embedding_field)
        if vector is None:
            continue
        out.append(
            {
                "id": str(row.get("id")),
                "vector": vector,
                "source_type": str(row.get("source_type", source_type)),
                "title": str(row.get("title", "")),
                "content": str(row.get("content") or row.get("summary") or ""),
                "classification": str(row.get("classification", "unknown")),
                "domain": str(row.get("domain", "general")),
                "service_scope": row.get("service_scope"),
                "environment": row.get("environment"),
                "severity": row.get("severity"),
                "extra_payload": {
                    "source": row.get("source"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                },
            }
        )
    return out




# =========================
# v2 Multi-tenant helpers
# =========================

def build_collection_name(tenant_id: str = "client_a", knowledge_scope: str = "client_private") -> str:
    tenant = (tenant_id or "shared").strip().lower().replace(" ", "_")
    scope = (knowledge_scope or "client_private").strip().lower()
    if scope == "client_private":
        return f"{tenant}_index"
    if scope == "global_learned":
        return "global_learned_patterns_index"
    if scope == "global_base":
        return "global_base_index"
    return QDRANT_COLLECTION

def resolve_search_order(tenant_id: str = "client_a", knowledge_scope: str = "client_private", search_order=None):
    if search_order:
        return list(search_order)
    scope=(knowledge_scope or "client_private").lower()
    tenant_coll=f"{tenant_id.lower()}_index"
    if scope=="client_private":
        return [tenant_coll, "global_learned_patterns_index", "global_base_index"]
    if scope=="global_learned":
        return ["global_learned_patterns_index", "global_base_index"]
    if scope=="global_base":
        return ["global_base_index"]
    return [tenant_coll, "global_learned_patterns_index", "global_base_index"]

def _safe_collection_exists(client, collection_name:str)->bool:
    try:
        existing = {c.name for c in client.get_collections().collections}
        return collection_name in existing
    except Exception:
        return False

def search_multi_scope(
    query_vector: List[float],
    limit: int = 5,
    tenant_id: str = "client_a",
    knowledge_scope: str = "client_private",
    search_order = None,
    **filters,
) -> List[Dict[str, Any]]:
    client = get_client()
    order = resolve_search_order(tenant_id, knowledge_scope, search_order)
    merged=[]
    seen=set()
    per_collection=max(limit,3)
    for coll in order:
        if not _safe_collection_exists(client, coll):
            continue
        try:
            rows = search_similar(
                query_vector=query_vector,
                limit=per_collection,
                collection_name=coll,
                domain=filters.get("domain"),
                classification=filters.get("classification"),
                source_type=filters.get("source_type"),
                service_scope=filters.get("service_scope"),
                environment=filters.get("environment"),
                severity=filters.get("severity"),
            )
        except Exception as exc:
            LOGGER.warning("search_multi_scope failed for %s: %s", coll, exc)
            continue
        for r in rows:
            rid=str(r.get("id") or r.get("qdrant_id"))
            if rid in seen:
                continue
            seen.add(rid)
            r["collection_name"]=coll
            r["source_layer"]="tenant" if coll.endswith("_index") and coll.startswith(tenant_id.lower()) else ("global_learned" if "learned" in coll else "global_base")
            merged.append(r)
    merged.sort(key=lambda x: float(x.get("score",0)), reverse=True)
    return merged[:limit]

if __name__ == "__main__":
    print("Qdrant reachable:", ping())
    if ping():
        ensure_collection()
        print("Collection ready:", QDRANT_COLLECTION)
        print("Point count:", count_points())
