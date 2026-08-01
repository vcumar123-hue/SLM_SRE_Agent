from __future__ import annotations
import os
import traceback
from typing import Any, Dict, List, Optional

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None  # type: ignore[assignment]
    RealDictCursor = None  # type: ignore[assignment]

try:
    from postgres_store import ping as postgres_ping
except ImportError:
    postgres_ping = None

# Imports from sibling modules (created alongside this file)
# shared_utils provides: normalize_phrase, similarity_ratio, pipeline_debug
from shared_utils import (
    normalize_phrase, similarity_ratio, pipeline_debug,
    clean_text, SOURCE_PRIORITY, normalize_classification_token, source_priority_value,
)



def postgres_read_enabled() -> bool:
    return psycopg2 is not None and postgres_enabled()


def postgres_connect_readonly():
    if not postgres_read_enabled():
        return None
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "aiops"),
        user=os.getenv("PGUSER", "aiops_user"),
        password=os.getenv("PGPASSWORD", ""),
    )


def postgres_read_rows(query: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
    if not postgres_read_enabled():
        return []
    conn = None
    try:
        conn = postgres_connect_readonly()
        if conn is None:
            return []
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]
    except Exception:
        pipeline_debug("postgres_read_error", {"query": query, "traceback": traceback.format_exc()})
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def choose_better_domain(primary: Optional[str], fallback: Optional[str]) -> str:
    primary = clean_text(primary).lower()
    fallback = clean_text(fallback).lower()
    if primary and primary != "general":
        return primary
    if fallback:
        return fallback
    return primary or "general"


def load_taxonomy_from_postgres() -> List[Dict[str, Any]]:
    rows = postgres_read_rows(
        "SELECT tower, category, classification, keywords, patterns FROM taxonomy ORDER BY tower, category, classification"
    )
    return rows


def load_feedback_corrections_from_postgres() -> List[Dict[str, Any]]:
    rows = postgres_read_rows(
        """
        SELECT ticket, original_domain, original_classification, corrected_classification, submitted_domain, stored_domain, is_correct, request_id
        FROM feedback_events
        ORDER BY COALESCE(request_id, ''), corrected_classification
        """
    )
    return rows


def load_incident_metadata_from_postgres(classification: Optional[str] = None) -> List[Dict[str, Any]]:
    if classification:
        return postgres_read_rows(
            "SELECT id, title, domain, classification, source, severity, service_scope, environment FROM incidents WHERE classification = %s",
            (classification,),
        )
    return postgres_read_rows(
        "SELECT id, title, domain, classification, source, severity, service_scope, environment FROM incidents"
    )


def score_feedback_match(ticket: str, row: Dict[str, Any]) -> float:
    ticket_norm = normalize_phrase(ticket)
    row_ticket = normalize_phrase(str(row.get("ticket", "") or ""))
    if not ticket_norm or not row_ticket:
        return 0.0
    if ticket_norm == row_ticket:
        return 1.0
    if ticket_norm in row_ticket or row_ticket in ticket_norm:
        return 0.92
    return similarity_ratio(ticket_norm, row_ticket)


def find_feedback_override(ticket: str) -> Optional[Dict[str, Any]]:
    best = None
    best_score = 0.0
    for row in load_feedback_corrections_from_postgres():
        score = score_feedback_match(ticket, row)
        if score > best_score:
            best = dict(row)
            best_score = score
    if best and best_score >= 0.78:
        best["match_score"] = round(best_score, 4)
        return best
    return None


def rerank_retrieved_incidents(
    ticket: str,
    incidents: List[Dict[str, Any]],
    taxonomy: Any,
    feedback_override: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    try:
        from app import extract_ticket_intents as _eti
        intents = _eti(ticket)
    except Exception:
        intents = {}
    preferred_cls = normalize_classification_token((feedback_override or {}).get("corrected_classification"))
    preferred_domain = clean_text((feedback_override or {}).get("stored_domain") or "").lower()

    ranked: List[Dict[str, Any]] = []
    for item in incidents:
        row = dict(item)
        source = clean_text(row.get("source", "incident")).lower()
        classification = normalize_classification_token(row.get("classification"))
        domain = clean_text(row.get("domain", "general")).lower()
        base_score = float(row.get("score", 0.0) or 0.0)
        bonus = source_priority_value(source) / 1000.0

        if preferred_cls and classification == preferred_cls:
            bonus += 0.30
        if preferred_domain and domain == preferred_domain:
            bonus += 0.12

        try:
            from app import infer_domain_from_taxonomy as _idt
            taxonomy_domain = _idt(classification, taxonomy)
        except Exception:
            taxonomy_domain = "general"
        if taxonomy_domain and taxonomy_domain == domain and domain != "general":
            bonus += 0.08
        elif taxonomy_domain and taxonomy_domain != domain and taxonomy_domain != "general":
            bonus -= 0.06

        if intents.get("auth_token") and "jwt" in classification:
            bonus += 0.16
        if intents.get("network_dns") and "dns" in classification:
            bonus += 0.16
        if intents.get("browser_auth") and "browser_sso" in classification:
            bonus += 0.10

        # Strongly bias file-transfer/integration incidents toward exact domain terms.
        try:
            from app import (
                ticket_has_strict_integration_terms as _thsi,
                ticket_has_broad_integration_terms as _thbi,
                count_matching_integration_terms as _cmi,
            )
        except Exception:
            _thsi = lambda t: False
            _thbi = lambda t: False
            _cmi = lambda t, r: 0
        if _thsi(ticket):
            matched_specific = _cmi(ticket, row)
            if matched_specific > 0:
                bonus += 0.22 + (0.04 * matched_specific)
            elif source == "incident" and domain == "general":
                bonus -= 0.30
            else:
                bonus -= 0.12
        elif _thbi(ticket):
            blob = normalize_phrase(f"{row.get('title', '')} {classification} {row.get('content', '')} {row.get('summary', '')}")
            if any(term in blob for term in ["file transfer", "integration", "payload"]):
                bonus += 0.10
            elif source == "incident" and domain == "general":
                bonus -= 0.12

        if source == "promoted_from_fallback" and preferred_cls and classification == preferred_cls:
            bonus -= 0.03

        row["score"] = round(base_score + bonus, 4)
        ranked.append(row)

    ranked.sort(key=lambda x: (float(x.get("score", 0.0) or 0.0), source_priority_value(x.get("source"))), reverse=True)
    return ranked


def postgres_enabled() -> bool:
    return postgres_ping is not None


def postgres_write_safe(op_name: str, fn, *args, **kwargs) -> None:
    if fn is None:
        return
    try:
        fn(*args, **kwargs)
    except Exception:
        pipeline_debug("postgres_parallel_write_error", {"operation": op_name, "traceback": traceback.format_exc()})

