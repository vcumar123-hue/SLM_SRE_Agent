"""
retrieval_engine.py
Hybrid retrieval engine for AI Ops.

This version adds:
- tenant-aware search scope
- search order across:
    client_<tenant>_index
    global_learned_patterns_index
    global_base_index
- optional local topology corpus loading
- backward-compatible calls to vector_store
- backward-compatible public API

Environment variables (optional):
    AIOPS_INCIDENTS_FILE=data/incidents.json
    AIOPS_RAG_TICKETS_FILE=data/rag/tickets.json
    AIOPS_RAG_KB_FILE=data/rag/kb.json
    AIOPS_RAG_TOPOLOGY_FILE=data/rag/topology.json
    AIOPS_HYBRID_LOCAL_WEIGHT=0.45
    AIOPS_HYBRID_VECTOR_WEIGHT=0.55
    AIOPS_ENABLE_VECTOR=true
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from embedding_store import embed_text, ping_model
from vector_store import search_similar as qdrant_search_similar, ping as qdrant_ping, search_multi_scope as qdrant_search_multi_scope

LOGGER = logging.getLogger("retrieval_engine")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


INCIDENTS_FILE = Path(_env("AIOPS_INCIDENTS_FILE", "data/incidents.json"))
RAG_TICKETS_FILE = Path(_env("AIOPS_RAG_TICKETS_FILE", "data/rag/tickets.json"))
RAG_KB_FILE = Path(_env("AIOPS_RAG_KB_FILE", "data/rag/kb.json"))
RAG_TOPOLOGY_FILE = Path(_env("AIOPS_RAG_TOPOLOGY_FILE", "data/rag/topology.json"))
LOCAL_WEIGHT = float(_env("AIOPS_HYBRID_LOCAL_WEIGHT", "0.60"))
VECTOR_WEIGHT = float(_env("AIOPS_HYBRID_VECTOR_WEIGHT", "0.40"))
ENABLE_VECTOR = _env("AIOPS_ENABLE_VECTOR", "true").strip().lower() in {"1", "true", "yes", "on"}


def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOGGER.exception("Failed reading JSON from %s", path)
        return default


def _normalize_text(text: str) -> str:
    text = str(text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_token(text: str, default: str = "") -> str:
    text = str(text or "").strip().lower().replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or default


def _tokenize(text: str) -> List[str]:
    text = _normalize_text(text)
    if not text:
        return []
    return text.split()


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _combine_text(record: Dict[str, Any]) -> str:
    title = str(record.get("title") or "")
    content = str(record.get("content") or record.get("summary") or "")
    classification = str(record.get("classification") or "")
    domain = str(record.get("domain") or "")
    service_scope = str(record.get("service_scope") or "")
    knowledge_type = str(record.get("knowledge_type") or "")
    system = str(record.get("system") or "")
    return ". ".join([x for x in [title, content, classification, domain, service_scope, knowledge_type, system] if x]).strip()


def _source_boost(source_type: str) -> float:
    mapping = {
        "incident": 1.00,
        "canonical_incident": 1.00,
        "ticket": 0.92,
        "kb": 0.95,
        "topology": 0.97,
        "document": 0.94,
    }
    return mapping.get(str(source_type or "").strip().lower(), 0.90)


def normalize_scope_token(value: Optional[str], default: str = "client_private") -> str:
    token = _normalize_token(value, default=default)
    if token in {"client_private", "global_base", "global_learned"}:
        return token
    return default


def build_collection_name(tenant_id: Optional[str], knowledge_scope: Optional[str]) -> str:
    tenant = _normalize_token(tenant_id, default="shared")
    scope = normalize_scope_token(knowledge_scope)
    if scope == "global_base":
        return "global_base_index"
    if scope == "global_learned":
        return "global_learned_patterns_index"
    return f"{tenant}_index"


def build_search_order(
    tenant_id: Optional[str] = "client_a",
    knowledge_scope: Optional[str] = "client_private",
    search_order: Optional[List[str]] = None,
) -> List[str]:
    if search_order:
        cleaned = [str(x).strip() for x in search_order if str(x).strip()]
        if cleaned:
            return cleaned
    tenant = _normalize_token(tenant_id, default="shared")
    scope = normalize_scope_token(knowledge_scope)
    if scope == "global_base":
        return ["global_base_index", "aiops_knowledge"]
    if scope == "global_learned":
        return ["global_learned_patterns_index", "global_base_index", "aiops_knowledge"]
    # client_private: tenant-specific first, then learned patterns, then curated library
    return [f"{tenant}_index", "global_learned_patterns_index", "global_base_index", "aiops_knowledge"]


def _collection_matches(
    record: Dict[str, Any],
    tenant_id: Optional[str],
    knowledge_scope: Optional[str],
    search_order: Optional[List[str]],
) -> bool:
    desired = set(build_search_order(tenant_id=tenant_id, knowledge_scope=knowledge_scope, search_order=search_order))
    row_collection = str(record.get("collection_name") or "").strip()
    if row_collection:
        return row_collection in desired

    # Backward compatibility for older rows without collection_name metadata.
    row_tenant = _normalize_token(record.get("tenant_id"), default="")
    row_scope = normalize_scope_token(record.get("knowledge_scope"), default="client_private") if record.get("knowledge_scope") else ""
    source_type = str(record.get("source_type") or record.get("source") or "").strip().lower()

    if "global_base_index" in desired and row_scope == "global_base":
        return True
    if "global_learned_patterns_index" in desired and row_scope == "global_learned":
        return True
    if tenant_id and f"{_normalize_token(tenant_id, 'shared')}_index" in desired:
        if row_tenant and row_tenant == _normalize_token(tenant_id):
            return True
        # Allow legacy local RAG files with no tenant metadata to participate in client-private search.
        if not row_tenant and source_type in {"ticket", "kb", "topology", "document"}:
            return True
    # Legacy incidents.json without tenant metadata should be treated as global base
    if not row_collection and not row_tenant and source_type in {"incident", "canonical_incident"} and "global_base_index" in desired:
        return True
    return False


def _prepare_row(row: Dict[str, Any], fallback_source_type: str) -> Dict[str, Any]:
    row = dict(row)
    row.setdefault("source_type", fallback_source_type)
    row.setdefault("knowledge_type", "incident" if fallback_source_type in {"incident", "canonical_incident"} else fallback_source_type)
    if not row.get("collection_name"):
        tenant = row.get("tenant_id")
        scope = row.get("knowledge_scope")
        row["collection_name"] = build_collection_name(tenant, scope) if (tenant or scope) else ""
    return row


def load_incidents() -> List[Dict[str, Any]]:
    rows = _safe_read_json(INCIDENTS_FILE, default=[])
    if not isinstance(rows, list):
        return []
    return [_prepare_row(row, "incident") for row in rows if isinstance(row, dict)]


def load_rag_tickets() -> List[Dict[str, Any]]:
    rows = _safe_read_json(RAG_TICKETS_FILE, default=[])
    if not isinstance(rows, list):
        return []
    return [_prepare_row(row, "ticket") for row in rows if isinstance(row, dict)]


def load_rag_kb() -> List[Dict[str, Any]]:
    rows = _safe_read_json(RAG_KB_FILE, default=[])
    if not isinstance(rows, list):
        return []
    return [_prepare_row(row, "kb") for row in rows if isinstance(row, dict)]


def load_rag_topology() -> List[Dict[str, Any]]:
    rows = _safe_read_json(RAG_TOPOLOGY_FILE, default=[])
    if not isinstance(rows, list):
        return []
    return [_prepare_row(row, "topology") for row in rows if isinstance(row, dict)]


def load_all_sources(
    tenant_id: Optional[str] = "client_a",
    knowledge_scope: Optional[str] = "client_private",
    search_order: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    rows = load_incidents() + load_rag_tickets() + load_rag_kb() + load_rag_topology()
    return [row for row in rows if _collection_matches(row, tenant_id, knowledge_scope, search_order)]


def _lexical_score(query: str, record: Dict[str, Any]) -> float:
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0

    title = str(record.get("title") or "")
    classification = str(record.get("classification") or "")
    domain = str(record.get("domain") or "")
    service_scope = str(record.get("service_scope") or "")
    full_text = _combine_text(record)

    full_tokens = set(_tokenize(full_text))
    title_tokens = set(_tokenize(title))
    class_tokens = set(_tokenize(classification))
    scope_tokens = set(_tokenize(service_scope))
    domain_tokens = set(_tokenize(domain))

    q_set = set(q_tokens)
    if not full_tokens:
        return 0.0

    overlap = len(q_set & full_tokens) / max(len(q_set), 1)
    title_overlap = len(q_set & title_tokens) / max(len(q_set), 1)
    class_overlap = len(q_set & class_tokens) / max(len(q_set), 1)
    scope_overlap = len(q_set & scope_tokens) / max(len(q_set), 1)
    domain_overlap = len(q_set & domain_tokens) / max(len(q_set), 1)

    phrase_bonus = 0.0
    norm_q = _normalize_text(query)
    norm_text = _normalize_text(full_text)
    if norm_q and norm_q in norm_text:
        phrase_bonus = 0.20

    score = (
        0.45 * overlap
        + 0.20 * title_overlap
        + 0.25 * class_overlap
        + 0.10 * scope_overlap
        + 0.10 * domain_overlap
        + phrase_bonus
    )

    score *= _source_boost(str(record.get("source_type", "incident")))
    return _clamp(score, 0.0, 1.25)


def local_search(
    query: str,
    limit: int = 10,
    domain: Optional[str] = None,
    classification: Optional[str] = None,
    service_scope: Optional[str] = None,
    tenant_id: Optional[str] = "client_a",
    knowledge_scope: Optional[str] = "client_private",
    search_order: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    rows = load_all_sources(tenant_id=tenant_id, knowledge_scope=knowledge_scope, search_order=search_order)
    ranked: List[Dict[str, Any]] = []

    for row in rows:
        row_domain = str(row.get("domain") or "").strip().lower()
        row_class = str(row.get("classification") or "").strip().lower()
        row_scope = str(row.get("service_scope") or "").strip().lower()

        if domain and row_domain != str(domain).strip().lower():
            continue
        if classification and row_class != str(classification).strip().lower():
            continue
        if service_scope and row_scope != str(service_scope).strip().lower():
            continue

        score = _lexical_score(query, row)
        if score <= 0:
            continue

        ranked.append(
            {
                "id": str(row.get("id") or row.get("incident_id") or ""),
                "title": str(row.get("title") or ""),
                "summary": str(row.get("content") or row.get("summary") or ""),
                "content": str(row.get("content") or row.get("summary") or ""),
                "classification": str(row.get("classification") or "unknown"),
                "domain": str(row.get("domain") or "general"),
                "service_scope": str(row.get("service_scope") or ""),
                "environment": str(row.get("environment") or ""),
                "severity": str(row.get("severity") or ""),
                "source_type": str(row.get("source_type") or "incident"),
                "knowledge_type": str(row.get("knowledge_type") or row.get("source_type") or "incident"),
                "collection_name": str(row.get("collection_name") or ""),
                "tenant_id": str(row.get("tenant_id") or tenant_id or ""),
                "knowledge_scope": str(row.get("knowledge_scope") or knowledge_scope or ""),
                "score": float(score),
                "score_local": float(score),
                "score_vector": 0.0,
                "score_combined": float(score),
                "retrieval_method": "local_lexical",
            }
        )

    ranked.sort(key=lambda x: x["score_combined"], reverse=True)
    return ranked[:limit]


def vector_search(
    query: str,
    limit: int = 10,
    domain: Optional[str] = None,
    classification: Optional[str] = None,
    service_scope: Optional[str] = None,
    environment: Optional[str] = None,
    severity: Optional[str] = None,
    tenant_id: Optional[str] = "client_a",
    knowledge_scope: Optional[str] = "client_private",
    search_order: Optional[List[str]] = None,
    collection_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not ENABLE_VECTOR:
        return []
    if not qdrant_ping():
        return []
    if not ping_model():
        return []

    qvec = embed_text(query)
    scope_order = build_search_order(tenant_id=tenant_id, knowledge_scope=knowledge_scope, search_order=search_order)
    target_collection = collection_name or build_collection_name(tenant_id, knowledge_scope)

    # Use multi-scope search to cover all collections in the scope order (including aiops_knowledge).
    # Fall back to single-collection search if multi-scope is unavailable.
    try:
        rows = qdrant_search_multi_scope(
            query_vector=qvec,
            limit=limit,
            tenant_id=tenant_id or "client_a",
            knowledge_scope=knowledge_scope or "client_private",
            search_order=scope_order,
            domain=domain,
            classification=classification,
            service_scope=service_scope,
            environment=environment,
            severity=severity,
        )
    except Exception:
        try:
            rows = qdrant_search_similar(
                query_vector=qvec,
                limit=limit,
                collection_name=target_collection,
                domain=domain,
                classification=classification,
                service_scope=service_scope,
                environment=environment,
                severity=severity,
            )
        except Exception:
            rows = []

    out: List[Dict[str, Any]] = []
    for row in rows:
        raw_score = float(row.get("score") or 0.0)
        score = _clamp(raw_score, 0.0, 1.0)
        out.append(
            {
                "id": str(row.get("id") or row.get("incident_id") or ""),
                "title": str(row.get("title") or ""),
                "summary": str(row.get("content") or ""),
                "content": str(row.get("content") or ""),
                "classification": str(row.get("classification") or "unknown"),
                "domain": str(row.get("domain") or "general"),
                "service_scope": str(row.get("service_scope") or ""),
                "environment": str(row.get("environment") or ""),
                "severity": str(row.get("severity") or ""),
                "source_type": str(row.get("source_type") or row.get("source") or "incident"),
                "knowledge_type": str(row.get("knowledge_type") or row.get("source_type") or "incident"),
                "collection_name": str(row.get("collection_name") or target_collection),
                "tenant_id": str(row.get("tenant_id") or tenant_id or ""),
                "knowledge_scope": str(row.get("knowledge_scope") or knowledge_scope or ""),
                "score": score,
                "score_local": 0.0,
                "score_vector": score,
                "score_combined": score,
                "retrieval_method": "qdrant_vector",
            }
        )
    return out


def merge_hybrid_results(
    local_results: List[Dict[str, Any]],
    vector_results: List[Dict[str, Any]],
    limit: int = 10,
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    def ensure_row(row: Dict[str, Any]) -> Dict[str, Any]:
        rid = str(row.get("id") or "")
        if rid not in merged:
            merged[rid] = dict(row)
            merged[rid]["score_local"] = float(row.get("score_local") or 0.0)
            merged[rid]["score_vector"] = float(row.get("score_vector") or 0.0)
        return merged[rid]

    for row in local_results:
        base = ensure_row(row)
        base["score_local"] = max(base.get("score_local", 0.0), float(row.get("score_local") or row.get("score") or 0.0))

    for row in vector_results:
        base = ensure_row(row)
        base["score_vector"] = max(base.get("score_vector", 0.0), float(row.get("score_vector") or row.get("score") or 0.0))
        if not base.get("collection_name") and row.get("collection_name"):
            base["collection_name"] = row.get("collection_name")
        if not base.get("tenant_id") and row.get("tenant_id"):
            base["tenant_id"] = row.get("tenant_id")
        if not base.get("knowledge_scope") and row.get("knowledge_scope"):
            base["knowledge_scope"] = row.get("knowledge_scope")

    for _, row in merged.items():
        score_local = float(row.get("score_local") or 0.0)
        score_vector = float(row.get("score_vector") or 0.0)
        source_boost = _source_boost(str(row.get("source_type") or "incident"))
        combined = (LOCAL_WEIGHT * score_local) + (VECTOR_WEIGHT * score_vector)
        combined *= source_boost
        row["score_combined"] = _clamp(combined, 0.0, 1.5)
        row["score"] = row["score_combined"]

        if score_local > 0 and score_vector > 0:
            row["retrieval_method"] = "hybrid"
        elif score_vector > 0:
            row["retrieval_method"] = "qdrant_vector"
        else:
            row["retrieval_method"] = "local_lexical"

    ranked = list(merged.values())
    ranked.sort(key=lambda x: x["score_combined"], reverse=True)
    return ranked[:limit]


def hybrid_search(
    query: str,
    limit: int = 10,
    domain: Optional[str] = None,
    classification: Optional[str] = None,
    service_scope: Optional[str] = None,
    environment: Optional[str] = None,
    severity: Optional[str] = None,
    tenant_id: Optional[str] = "client_a",
    knowledge_scope: Optional[str] = "client_private",
    search_order: Optional[List[str]] = None,
    collection_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    local_results = local_search(
        query=query,
        limit=max(limit * 2, 10),
        domain=domain,
        classification=classification,
        service_scope=service_scope,
        tenant_id=tenant_id,
        knowledge_scope=knowledge_scope,
        search_order=search_order,
    )

    vector_results = vector_search(
        query=query,
        limit=max(limit * 2, 10),
        domain=domain,
        classification=classification,
        service_scope=service_scope,
        environment=environment,
        severity=severity,
        tenant_id=tenant_id,
        knowledge_scope=knowledge_scope,
        search_order=search_order,
        collection_name=collection_name,
    )

    return merge_hybrid_results(local_results, vector_results, limit=limit)


def retrieve_incidents(
    ticket: str,
    limit: int = 5,
    domain: Optional[str] = None,
    classification: Optional[str] = None,
    service_scope: Optional[str] = None,
    tenant_id: Optional[str] = "client_a",
    knowledge_scope: Optional[str] = "client_private",
    search_order: Optional[List[str]] = None,
    collection_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return hybrid_search(
        query=ticket,
        limit=limit,
        domain=domain,
        classification=classification,
        service_scope=service_scope,
        tenant_id=tenant_id,
        knowledge_scope=knowledge_scope,
        search_order=search_order,
        collection_name=collection_name,
    )


def retrieve_for_routing(ticket: str, limit: int = 5) -> List[Dict[str, Any]]:
    return retrieve_incidents(ticket=ticket, limit=limit)


def summarize_retrieval_quality(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {
            "match_count": 0,
            "top_score": 0.0,
            "avg_top3_score": 0.0,
            "hybrid_hits": 0,
            "vector_hits": 0,
            "local_hits": 0,
            "collections": [],
        }

    top_score = float(results[0].get("score_combined") or results[0].get("score") or 0.0)
    top3 = results[:3]
    avg_top3 = sum(float(x.get("score_combined") or x.get("score") or 0.0) for x in top3) / max(len(top3), 1)

    hybrid_hits = sum(1 for x in results if x.get("retrieval_method") == "hybrid")
    vector_hits = sum(1 for x in results if x.get("retrieval_method") == "qdrant_vector")
    local_hits = sum(1 for x in results if x.get("retrieval_method") == "local_lexical")
    collections = sorted({str(x.get("collection_name") or "").strip() for x in results if str(x.get("collection_name") or "").strip()})

    return {
        "match_count": len(results),
        "top_score": round(top_score, 6),
        "avg_top3_score": round(avg_top3, 6),
        "hybrid_hits": hybrid_hits,
        "vector_hits": vector_hits,
        "local_hits": local_hits,
        "collections": collections,
    }


if __name__ == "__main__":
    sample_query = "VPN client authentication failure after password reset"
    results = retrieve_incidents(sample_query, limit=5, tenant_id="client_a", knowledge_scope="client_private")
    print("Results:", len(results))
    for idx, row in enumerate(results, start=1):
        print(
            f"{idx}. {row.get('id')} | {row.get('classification')} | "
            f"score={row.get('score_combined'):.6f} | method={row.get('retrieval_method')} | "
            f"collection={row.get('collection_name')}"
        )
