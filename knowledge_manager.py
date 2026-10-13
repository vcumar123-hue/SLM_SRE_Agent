from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import settings_portable as settings
from file_manager import read_json, write_json, utc_now_iso

logger = logging.getLogger(__name__)

# PostgreSQL dual-write (graceful no-op if DB unreachable)
try:
    from postgres_store import (
        upsert_resolution,
        upsert_playbook,
        upsert_knowledge_candidate,
        append_resolution_version,
        append_playbook_version,
    )
    _PG_ENABLED = True
except Exception:
    upsert_resolution = None  # type: ignore[assignment]
    upsert_playbook = None  # type: ignore[assignment]
    upsert_knowledge_candidate = None  # type: ignore[assignment]
    append_resolution_version = None  # type: ignore[assignment]
    append_playbook_version = None  # type: ignore[assignment]
    _PG_ENABLED = False


def _pg_write(fn, *args, **kwargs) -> None:
    if fn is None:
        return
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("PostgreSQL dual-write failed: %s", exc)


def _collection_for(tenant: str, scope: str) -> str:
    """Logical layer name for a tenant and knowledge scope."""
    if scope == "global_learned":
        return "global_learned_patterns_index"
    if scope == "global_base":
        return "global_base_index"
    return f"{(tenant or 'shared').strip().lower().replace(' ', '_')}_index"


def _layer_for(collection_name: str, record: Dict[str, Any]) -> tuple:
    """Map a logical layer name (e.g. client_a_index) to (knowledge_scope, tenant_id)."""
    if collection_name == "global_learned_patterns_index":
        return "global_learned", None
    if collection_name == "global_base_index":
        return "global_base", None
    tenant = collection_name[: -len("_index")] if collection_name.endswith("_index") else _clean_str(record.get("tenant_id"))
    return "client_private", tenant or None


def _best_effort_vector_upsert(record: Dict[str, Any], collection_name: str, quiet: bool = False) -> bool:
    """Index learned knowledge in PostgreSQL (pgvector) without blocking promotion.
    Returns True on success, False on any failure.
    quiet=True downgrades internal failure logs to DEBUG (use at promotion call sites)."""
    _log = logger.debug if quiet else logger.warning
    try:
        from embedding_store import embed_text
        from pg_vector_store import embedding_text, upsert_knowledge
        import settings_portable

        point_id = _clean_str(record.get("id") or record.get("incident_id") or record.get("point_id"))
        title = _clean_str(record.get("title"))
        content = _clean_str(record.get("content") or record.get("summary"))
        classification = _clean_str(record.get("classification"), "unknown")
        domain = _clean_str(record.get("domain"), "general")
        if not point_id or not content:
            _log("Vector upsert skipped - missing id or content for record: %s", record.get("id"))
            return False

        vector = embed_text(embedding_text(title, content, classification, domain))
        scope, tenant = _layer_for(collection_name, record)
        upsert_knowledge(
            {**record, "id": point_id, "title": title, "content": content,
             "classification": classification, "domain": domain},
            vector,
            model=getattr(settings_portable, "EMBEDDING_MODEL", "unknown"),
            tenant_id=tenant,
            knowledge_scope=scope,
        )
        return True
    except Exception as exc:
        _log("Learned-knowledge vector upsert failed: %s", exc)
        return False


try:
    from tenant_context import tenant_data_file as _tdf
    def _data_file(name: str):
        return _tdf(name)
except Exception:
    def _data_file(name: str):  # type: ignore[misc]
        return settings.DATA_DIR / name

KNOWLEDGE_CANDIDATES_FILE = _data_file("knowledge_candidates.json")
RESOLUTIONS_FILE = _data_file("resolutions.json")
PLAYBOOKS_FILE = _data_file("playbooks.json")
GLOBAL_LEARNED_PATTERNS_FILE = _data_file("global_learned_patterns.json")


def _load_list(path) -> List[Dict[str, Any]]:
    data = read_json(path, default=[])
    return data if isinstance(data, list) else []


def _save_list(path, records: List[Dict[str, Any]]) -> None:
    write_json(path, records, indent=2)


def _clean_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _clean_list(values: Optional[List[Any]]) -> List[str]:
    if not values:
        return []
    out: List[str] = []
    seen = set()
    for item in values:
        text = _clean_str(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _now() -> str:
    return utc_now_iso()


def _classification_key(value: Any) -> str:
    return _clean_str(value).lower()


def _normalize_scope(value: Any, default: str = "client_private") -> str:
    token = _clean_str(value, default).lower().replace(" ", "_")
    if token in {"client_private", "global_learned", "global_base"}:
        return token
    return default


def _normalize_tenant(value: Any, default: str = "client_a") -> str:
    return _clean_str(value, default).lower().replace(" ", "_")


def _sanitize_generic_text(value: str) -> str:
    text = _clean_str(value)
    if not text:
        return ""
    replacements = {
        "client_a": "client",
        "pfg": "client",
        "prudential": "client",
        "okta": "identity_provider",
        "kubernetes": "container_platform",
    }
    lower = text.lower()
    for src, dst in replacements.items():
        lower = lower.replace(src, dst)
    return lower


def get_candidate_by_id(candidate_id: str) -> Optional[Dict[str, Any]]:
    candidate_id = _clean_str(candidate_id)
    if not candidate_id:
        return None
    records = _load_list(KNOWLEDGE_CANDIDATES_FILE)
    return next((r for r in records if _clean_str(r.get("candidate_id")) == candidate_id), None)


def list_candidates(status: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    records = _load_list(KNOWLEDGE_CANDIDATES_FILE)
    if tenant_id:
        tenant_key = _normalize_tenant(tenant_id)
        records = [r for r in records if _normalize_tenant(r.get("tenant_id"), "") == tenant_key]
    if status:
        status_key = _clean_str(status).lower()
        records = [r for r in records if _clean_str(r.get("status")).lower() == status_key]
    return records


def find_resolution_by_classification(classification: str) -> Optional[Dict[str, Any]]:
    classification_key = _classification_key(classification)
    if not classification_key:
        return None
    records = _load_list(RESOLUTIONS_FILE)
    matches = [r for r in records if _classification_key(r.get("classification")) == classification_key]
    if not matches:
        return None
    matches.sort(
        key=lambda r: (
            _clean_str(r.get("updated_at") or r.get("created_at")),
            _clean_str(r.get("resolution_id")),
        ),
        reverse=True,
    )
    return matches[0]


def find_playbook_by_classification(classification: str) -> Optional[Dict[str, Any]]:
    classification_key = _classification_key(classification)
    if not classification_key:
        return None
    records = _load_list(PLAYBOOKS_FILE)
    matches = [r for r in records if _classification_key(r.get("classification")) == classification_key]
    if not matches:
        return None
    matches.sort(
        key=lambda r: (
            _clean_str(r.get("updated_at") or r.get("created_at")),
            _clean_str(r.get("playbook_id")),
        ),
        reverse=True,
    )
    return matches[0]


def create_knowledge_candidate(
    *,
    ticket: str,
    domain: str,
    classification: str,
    probable_causes: List[str],
    next_checks: List[str],
    recommended_actions: List[str],
    confidence: float,
    fallback_provider: str,
    fallback_model: str,
    retrieved_incidents: Optional[List[str]] = None,
    tenant_id: str = "client_a",
    knowledge_scope: str = "client_private",
    source_layer: str = "fallback_candidate",
) -> Dict[str, Any]:
    records = _load_list(KNOWLEDGE_CANDIDATES_FILE)

    ticket_clean = _clean_str(ticket)
    domain_clean = _clean_str(domain, "general")
    classification_clean = _clean_str(classification, "unknown")
    tenant_clean = _normalize_tenant(tenant_id)
    scope_clean = _normalize_scope(knowledge_scope)

    for rec in records:
        if (
            _clean_str(rec.get("ticket")).lower() == ticket_clean.lower()
            and _clean_str(rec.get("classification")).lower() == classification_clean.lower()
            and _normalize_tenant(rec.get("tenant_id"), tenant_clean) == tenant_clean
            and _clean_str(rec.get("status")).lower() in {"candidate", "approved", "promoted", "promoted_global"}
        ):
            return rec

    candidate = {
        "candidate_id": f"kc_{uuid.uuid4().hex[:12]}",
        "created_at": _now(),
        "updated_at": _now(),
        "source": "llm_fallback",
        "source_layer": _clean_str(source_layer, "fallback_candidate"),
        "status": "candidate",
        "ticket": ticket_clean,
        "domain": domain_clean,
        "classification": classification_clean,
        "probable_causes": _clean_list(probable_causes),
        "next_checks": _clean_list(next_checks),
        "recommended_actions": _clean_list(recommended_actions) or _clean_list(next_checks)[:3],
        "confidence": _safe_float(confidence, 0.0),
        "fallback_provider": _clean_str(fallback_provider, "unknown"),
        "fallback_model": _clean_str(fallback_model, "unknown"),
        "retrieved_incidents": _clean_list(retrieved_incidents),
        "tenant_id": tenant_clean,
        "knowledge_scope": scope_clean,
        "promotion_target": "",
        "promoted_by": "",
        "promoted_at": "",
        "engineer_review": {
            "reviewed": False,
            "reviewed_by": "",
            "review_notes": "",
            "approved": False,
            "reviewed_at": "",
        },
    }

    records.append(candidate)
    _save_list(KNOWLEDGE_CANDIDATES_FILE, records)
    _pg_write(upsert_knowledge_candidate, candidate)
    return candidate


def review_knowledge_candidate(
    *,
    candidate_id: str,
    reviewed_by: str,
    approved: bool,
    review_notes: str = "",
    updated_classification: Optional[str] = None,
    updated_probable_causes: Optional[List[str]] = None,
    updated_next_checks: Optional[List[str]] = None,
    updated_recommended_actions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    records = _load_list(KNOWLEDGE_CANDIDATES_FILE)

    for rec in records:
        if _clean_str(rec.get("candidate_id")) == _clean_str(candidate_id):
            if updated_classification:
                rec["classification"] = _clean_str(updated_classification, rec.get("classification", "unknown"))
            if updated_probable_causes is not None:
                rec["probable_causes"] = _clean_list(updated_probable_causes)
            if updated_next_checks is not None:
                rec["next_checks"] = _clean_list(updated_next_checks)
            if updated_recommended_actions is not None:
                rec["recommended_actions"] = _clean_list(updated_recommended_actions)

            rec["engineer_review"] = {
                "reviewed": True,
                "reviewed_by": _clean_str(reviewed_by),
                "review_notes": _clean_str(review_notes),
                "approved": bool(approved),
                "reviewed_at": _now(),
            }
            rec["status"] = "approved" if approved else "rejected"
            rec["updated_at"] = _now()

            _save_list(KNOWLEDGE_CANDIDATES_FILE, records)
            _pg_write(upsert_knowledge_candidate, rec)
            return rec

    raise ValueError(f"Candidate not found: {candidate_id}")


def promote_candidate_to_incident(
    candidate_id: str,
    *,
    tenant_id: str = "client_a",
    knowledge_scope: str = "client_private",
    promote_to_global: bool = False,
    promoted_by: str = "system",
) -> Dict[str, Any]:
    candidates = _load_list(KNOWLEDGE_CANDIDATES_FILE)
    incidents = _load_list(settings.INCIDENTS_FILE)
    global_patterns = _load_list(GLOBAL_LEARNED_PATTERNS_FILE)

    candidate = next((x for x in candidates if _clean_str(x.get("candidate_id")) == _clean_str(candidate_id)), None)
    if not candidate:
        raise ValueError(f"Candidate not found: {candidate_id}")

    if _clean_str(candidate.get("status")).lower() != "approved":
        raise ValueError("Only approved candidates can be promoted.")

    # Block promotion of truly ungrounded LLM hallucinations.
    # "fallback_candidate"       = LLM ran with zero context (old path, no vague-ticket check) — block.
    # "fallback_candidate_no_kb" = LLM ran on a specific ticket with no KB coverage — allow (engineer reviewed).
    # "fallback_candidate_grounded" = LLM had nearest-neighbour context — always allow.
    _source = _clean_str(candidate.get("source", "")).lower()
    _source_layer = _clean_str(candidate.get("source_layer", "")).lower()
    _retrieved = candidate.get("retrieved_incidents") or []
    _grounding_count = int(candidate.get("grounding_incident_count") or 0)
    # grounding_incident_count=None means the field predates the grounding system —
    # treat those the same as no_kb (engineer-reviewed, allow through).
    _grounding_count_raw = candidate.get("grounding_incident_count")
    _is_ungrounded_fallback = (
        _source in {"llm_fallback", "fallback"}
        and _source_layer in {"fallback_candidate", ""}
        and len(_retrieved) == 0
        and _grounding_count == 0
        and _grounding_count_raw is not None  # None = pre-grounding candidate, allow
    )
    if _is_ungrounded_fallback:
        raise ValueError(
            "Cannot promote: this candidate was generated by LLM with zero RAG grounding "
            "(retrieved_incidents is empty and no nearest-neighbour context was used). "
            "Ingest real incident data for this classification first, then re-analyse the ticket."
        )

    tenant_clean = _normalize_tenant(tenant_id)
    scope_clean = _normalize_scope(knowledge_scope)

    if promote_to_global:
        existing = next(
            (row for row in global_patterns if _clean_str(row.get("linked_candidate_id")).lower() == _clean_str(candidate_id).lower()),
            None,
        )
        if existing:
            if not _best_effort_vector_upsert(existing, "global_learned_patterns_index", quiet=True):
                logger.debug("promote: re-index of existing global pattern %s failed (non-blocking)", existing.get("id"))
            return existing

        pattern_parts = []
        ticket = _sanitize_generic_text(candidate.get("ticket", ""))
        if ticket:
            pattern_parts.append(ticket)
        causes = _clean_list(candidate.get("probable_causes"))
        if causes:
            pattern_parts.append("Likely causes: " + ", ".join([_sanitize_generic_text(x) for x in causes]))
        checks = _clean_list(candidate.get("next_checks"))
        if checks:
            pattern_parts.append("Recommended checks: " + ", ".join([_sanitize_generic_text(x) for x in checks]))
        actions = _clean_list(candidate.get("recommended_actions"))
        if actions:
            pattern_parts.append("Recommended actions: " + ", ".join([_sanitize_generic_text(x) for x in actions]))

        pattern = {
            "id": f"glp_{uuid.uuid4().hex[:10]}",
            "linked_candidate_id": _clean_str(candidate_id),
            "tenant_id": tenant_clean,
            "knowledge_scope": "global_learned",
            "source_layer": "global_learned_patterns",
            "domain": _clean_str(candidate.get("domain"), "general"),
            "classification": _clean_str(candidate.get("classification"), "unknown"),
            "title": _clean_str(candidate.get("classification"), "unknown").replace("_", " ").title(),
            "content": ". ".join(pattern_parts).strip(),
            "confidence": _safe_float(candidate.get("confidence"), 0.0),
            "source": "promoted_global_learned",
            "created_at": _now(),
            "updated_at": _now(),
        }
        global_patterns.append(pattern)
        _save_list(GLOBAL_LEARNED_PATTERNS_FILE, global_patterns)
        if not _best_effort_vector_upsert(pattern, "global_learned_patterns_index", quiet=True):
            logger.debug("promote: new global pattern %s failed to index in the vector store (non-blocking)", pattern.get("id"))

        candidate["status"] = "promoted_global"
        candidate["promotion_target"] = "global_learned"
        candidate["knowledge_scope"] = "global_learned"
        candidate["promoted_by"] = _clean_str(promoted_by)
        candidate["promoted_at"] = _now()
        candidate["updated_at"] = _now()
        _save_list(KNOWLEDGE_CANDIDATES_FILE, candidates)
        return pattern

    existing = next(
        (inc for inc in incidents if _clean_str(inc.get("linked_candidate_id")).lower() == _clean_str(candidate_id).lower()),
        None,
    )
    if existing:
        _t = _normalize_tenant(existing.get("tenant_id"), tenant_clean)
        _s = _normalize_scope(existing.get("knowledge_scope"), scope_clean)
        _coll = _collection_for(_t, _s)
        if not _best_effort_vector_upsert(existing, _coll, quiet=True):
            logger.debug("promote: re-index of existing incident %s failed (non-blocking)", existing.get("id"))
        return existing

    title = _clean_str(candidate.get("classification"), "unknown").replace("_", " ").title()

    parts = []
    ticket = _clean_str(candidate.get("ticket"))
    if ticket:
        parts.append(ticket)
    causes = _clean_list(candidate.get("probable_causes"))
    if causes:
        parts.append("Likely causes: " + ", ".join(causes))
    checks = _clean_list(candidate.get("next_checks"))
    if checks:
        parts.append("Recommended checks: " + ", ".join(checks))
    actions = _clean_list(candidate.get("recommended_actions"))
    if actions:
        parts.append("Recommended actions: " + ", ".join(actions))

    incident = {
        "id": f"inc_{uuid.uuid4().hex[:10]}",
        "tenant_id": tenant_clean,
        "knowledge_scope": scope_clean,
        "source_layer": "client_private_knowledge",
        "domain": _clean_str(candidate.get("domain"), "general"),
        "classification": _clean_str(candidate.get("classification"), "unknown"),
        "title": title,
        "content": ". ".join(parts).strip(),
        "source": "promoted_from_fallback",
        "confidence": _safe_float(candidate.get("confidence"), 0.0),
        "linked_candidate_id": _clean_str(candidate_id),
        "created_at": _now(),
        "updated_at": _now(),
    }

    incidents.append(incident)
    write_json(settings.INCIDENTS_FILE, incidents, indent=2)
    collection_name = _collection_for(tenant_clean, scope_clean)
    if not _best_effort_vector_upsert(incident, collection_name, quiet=True):
        logger.debug("promote: new incident %s failed to index in the vector store (non-blocking)", incident.get("id"))

    candidate["status"] = "promoted"
    candidate["promotion_target"] = "client_private"
    candidate["tenant_id"] = tenant_clean
    candidate["knowledge_scope"] = scope_clean
    candidate["promoted_by"] = _clean_str(promoted_by)
    candidate["promoted_at"] = _now()
    candidate["updated_at"] = _now()
    _save_list(KNOWLEDGE_CANDIDATES_FILE, candidates)

    return incident


def create_resolution(
    *,
    classification: str,
    root_cause: str,
    validated_checks: List[str],
    validated_actions: List[str],
    confirmed_by: str,
    success_notes: str,
    linked_candidate_id: str = "",
    automation_ready: bool = False,
    linked_playbook_id: str = "",
    incident_id: str = "",
) -> Dict[str, Any]:
    records = _load_list(RESOLUTIONS_FILE)
    classification_clean = _clean_str(classification, "unknown")
    classification_key = classification_clean.lower()
    _inc_id = _clean_str(incident_id)

    existing = next((r for r in records if _classification_key(r.get("classification")) == classification_key), None)
    if existing:
        existing["classification"] = classification_clean
        existing["root_cause"] = _clean_str(root_cause)
        existing["validated_checks"] = _clean_list(validated_checks)
        existing["validated_actions"] = _clean_list(validated_actions)
        existing["engineer_confirmed"] = True
        existing["confirmed_by"] = _clean_str(confirmed_by)
        existing["success_notes"] = _clean_str(success_notes)
        existing["automation_ready"] = bool(automation_ready)
        existing["linked_candidate_id"] = _clean_str(linked_candidate_id)
        existing["linked_playbook_id"] = _clean_str(linked_playbook_id)
        if _inc_id:
            existing["incident_id"] = _inc_id
        existing["updated_at"] = _now()
        _save_list(RESOLUTIONS_FILE, records)
        _pg_write(upsert_resolution, existing)
        _pg_write(append_resolution_version, existing, _clean_str(confirmed_by), "updated")
        try:
            import resolution_outcome as _ro
            _ro.record_outcome(
                incident_id=_inc_id,
                classification=classification_clean,
                resolution_id=existing.get("resolution_id", ""),
                playbook_id=_clean_str(linked_playbook_id),
                execution_mode="manual",
                ai_recommendation_used="partial",
                outcome_status="resolved",
                confirmed_by=_clean_str(confirmed_by),
                notes=_clean_str(success_notes),
            )
        except Exception:
            pass
        return existing

    resolution = {
        "resolution_id": f"res_{uuid.uuid4().hex[:12]}",
        "incident_id": _inc_id,
        "created_at": _now(),
        "updated_at": _now(),
        "classification": classification_clean,
        "root_cause": _clean_str(root_cause),
        "validated_checks": _clean_list(validated_checks),
        "validated_actions": _clean_list(validated_actions),
        "engineer_confirmed": True,
        "confirmed_by": _clean_str(confirmed_by),
        "success_notes": _clean_str(success_notes),
        "automation_ready": bool(automation_ready),
        "linked_candidate_id": _clean_str(linked_candidate_id),
        "linked_playbook_id": _clean_str(linked_playbook_id),
    }
    records.append(resolution)
    _save_list(RESOLUTIONS_FILE, records)
    _pg_write(upsert_resolution, resolution)
    _pg_write(append_resolution_version, resolution, _clean_str(confirmed_by), "created")
    # Reinforce long-term memory
    try:
        from long_term_memory import record_outcome
        record_outcome(classification_clean, success=bool(automation_ready), resolved_by=_clean_str(confirmed_by))
    except Exception:
        pass
    try:
        from episodic_memory import record_episode
        record_episode(
            classification=classification_clean,
            incident_id=_inc_id,
            resolved_by=_clean_str(confirmed_by),
            outcome="resolved" if bool(automation_ready) else "manual",
        )
    except Exception:
        pass
    try:
        import resolution_outcome as _ro
        _ro.record_outcome(
            incident_id=_inc_id,
            classification=classification_clean,
            resolution_id=resolution["resolution_id"],
            playbook_id=_clean_str(linked_playbook_id),
            execution_mode="manual",
            ai_recommendation_used="partial",
            outcome_status="resolved",
            confirmed_by=_clean_str(confirmed_by),
            notes=_clean_str(success_notes),
        )
    except Exception:
        pass
    return resolution


def _normalize_playbook_status(value: Any, default: str = "draft") -> str:
    token = _clean_str(value, default).lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "new": "draft",
        "candidate": "draft",
        "review": "review_needed",
        "pending_review": "review_needed",
        "ready_for_review": "review_needed",
        "approve": "approved",
        "enabled": "approved",
        "automation_ready": "automation_ready",
        "reject": "rejected",
    }
    token = aliases.get(token, token)
    if token not in {"draft", "review_needed", "approved", "automation_ready", "rejected", "disabled"}:
        return default
    return token


def create_playbook(
    *,
    name: str,
    classification: str,
    description: str,
    steps: List[str],
    scripts: List[str],
    approval_mode: str = "manual_approval_required",
    rollback_available: bool = True,
    risk_tier: str = "medium",
    enabled: bool = True,
    status: str = "draft",
    source: str = "manual",
    linked_incident_id: str = "",
    linked_rca_id: str = "",
    created_by: str = "Vijay",
    review_notes: str = "",
) -> Dict[str, Any]:
    records = _load_list(PLAYBOOKS_FILE)
    classification_clean = _clean_str(classification, "unknown")
    classification_key = classification_clean.lower()
    status_clean = _normalize_playbook_status(status)

    existing = next((r for r in records if _classification_key(r.get("classification")) == classification_key), None)
    if existing:
        existing["name"] = _clean_str(name)
        existing["classification"] = classification_clean
        existing["description"] = _clean_str(description)
        existing["steps"] = _clean_list(steps)
        existing["scripts"] = _clean_list(scripts)
        existing["approval_mode"] = _clean_str(approval_mode, "manual_approval_required")
        existing["rollback_available"] = bool(rollback_available)
        existing["risk_tier"] = _clean_str(risk_tier, "medium")
        existing["enabled"] = bool(enabled)
        existing["status"] = status_clean or existing.get("status") or "draft"
        existing["source"] = _clean_str(source, existing.get("source", "manual"))
        existing["linked_incident_id"] = _clean_str(linked_incident_id, existing.get("linked_incident_id", ""))
        existing["linked_rca_id"] = _clean_str(linked_rca_id, existing.get("linked_rca_id", ""))
        existing["created_by"] = _clean_str(existing.get("created_by"), _clean_str(created_by, "Vijay"))
        existing.setdefault("created_at", _now())
        existing["updated_at"] = _now()
        existing.setdefault("review", {"reviewed": False, "reviewed_by": "", "reviewed_at": "", "review_notes": ""})
        if review_notes:
            existing["review"]["review_notes"] = _clean_str(review_notes)
        _save_list(PLAYBOOKS_FILE, records)
        _pg_write(upsert_playbook, existing)
        _pg_write(append_playbook_version, existing, _clean_str(created_by), "updated")
        return existing

    playbook = {
        "playbook_id": f"pb_{uuid.uuid4().hex[:12]}",
        "created_at": _now(),
        "updated_at": _now(),
        "name": _clean_str(name),
        "classification": classification_clean,
        "description": _clean_str(description),
        "steps": _clean_list(steps),
        "scripts": _clean_list(scripts),
        "approval_mode": _clean_str(approval_mode, "manual_approval_required"),
        "rollback_available": bool(rollback_available),
        "risk_tier": _clean_str(risk_tier, "medium"),
        "enabled": bool(enabled),
        "status": status_clean,
        "source": _clean_str(source, "manual"),
        "linked_incident_id": _clean_str(linked_incident_id),
        "linked_rca_id": _clean_str(linked_rca_id),
        "created_by": _clean_str(created_by, "Vijay"),
        "review": {
            "reviewed": False,
            "reviewed_by": "",
            "reviewed_at": "",
            "review_notes": _clean_str(review_notes),
        },
    }
    records.append(playbook)
    _save_list(PLAYBOOKS_FILE, records)
    _pg_write(upsert_playbook, playbook)
    _pg_write(append_playbook_version, playbook, _clean_str(created_by), "created")
    return playbook


def list_playbooks(status: Optional[str] = None, classification: Optional[str] = None) -> List[Dict[str, Any]]:
    records = _load_list(PLAYBOOKS_FILE)
    if status:
        status_key = _normalize_playbook_status(status, default="")
        records = [r for r in records if _normalize_playbook_status(r.get("status", "draft"), default="") == status_key]
    if classification:
        cls_key = _classification_key(classification)
        records = [r for r in records if _classification_key(r.get("classification")) == cls_key]
    records.sort(key=lambda r: (_clean_str(r.get("updated_at") or r.get("created_at")), _clean_str(r.get("playbook_id"))), reverse=True)
    return records


def get_playbook_by_id(playbook_id: str) -> Optional[Dict[str, Any]]:
    playbook_id = _clean_str(playbook_id)
    if not playbook_id:
        return None
    return next((r for r in _load_list(PLAYBOOKS_FILE) if _clean_str(r.get("playbook_id")) == playbook_id), None)


def backfill_promoted_vectors() -> Dict[str, Any]:
    """
    Index all previously promoted records that may be missing vectors.
    Reads incidents.json (source=promoted_from_fallback) and
    global_learned_patterns.json (source=promoted_global_learned) and upserts
    vectors for each. Safe to re-run: the upsert is idempotent.

    Returns counts: scanned, indexed, skipped, failed.
    """
    counts: Dict[str, int] = {"scanned": 0, "indexed": 0, "skipped": 0, "failed": 0}

    incidents = _load_list(settings.INCIDENTS_FILE)
    for inc in incidents:
        counts["scanned"] += 1
        source = _clean_str(inc.get("source")).lower()
        if source != "promoted_from_fallback":
            counts["skipped"] += 1
            continue
        t = _normalize_tenant(inc.get("tenant_id"), "client_a")
        s = _normalize_scope(inc.get("knowledge_scope"), "client_private")
        coll = _collection_for(t, s)
        if _best_effort_vector_upsert(inc, coll):
            counts["indexed"] += 1
        else:
            logger.warning("backfill_promoted_vectors: incident %s failed to index", inc.get("id"))
            counts["failed"] += 1

    global_patterns = _load_list(GLOBAL_LEARNED_PATTERNS_FILE)
    for pat in global_patterns:
        counts["scanned"] += 1
        source = _clean_str(pat.get("source")).lower()
        if source != "promoted_global_learned":
            counts["skipped"] += 1
            continue
        if _best_effort_vector_upsert(pat, "global_learned_patterns_index"):
            counts["indexed"] += 1
        else:
            logger.warning("backfill_promoted_vectors: pattern %s failed to index", pat.get("id"))
            counts["failed"] += 1

    logger.info("backfill_promoted_vectors complete: %s", counts)
    return counts


def review_playbook(
    *,
    playbook_id: str,
    reviewed_by: str,
    approved: bool,
    review_notes: str = "",
    updated_name: Optional[str] = None,
    updated_description: Optional[str] = None,
    updated_steps: Optional[List[str]] = None,
    updated_scripts: Optional[List[str]] = None,
    risk_tier: Optional[str] = None,
    approval_mode: Optional[str] = None,
    rollback_available: Optional[bool] = None,
    automation_ready: bool = False,
) -> Dict[str, Any]:
    records = _load_list(PLAYBOOKS_FILE)
    for rec in records:
        if _clean_str(rec.get("playbook_id")) == _clean_str(playbook_id):
            if updated_name is not None:
                rec["name"] = _clean_str(updated_name, rec.get("name", ""))
            if updated_description is not None:
                rec["description"] = _clean_str(updated_description, rec.get("description", ""))
            if updated_steps is not None:
                rec["steps"] = _clean_list(updated_steps)
            if updated_scripts is not None:
                rec["scripts"] = _clean_list(updated_scripts)
            if risk_tier is not None:
                rec["risk_tier"] = _clean_str(risk_tier, rec.get("risk_tier", "medium"))
            if approval_mode is not None:
                rec["approval_mode"] = _clean_str(approval_mode, rec.get("approval_mode", "manual_approval_required"))
            if rollback_available is not None:
                rec["rollback_available"] = bool(rollback_available)

            if approved:
                rec["status"] = "automation_ready" if automation_ready else "approved"
                rec["enabled"] = True
            else:
                rec["status"] = "rejected"
                rec["enabled"] = False

            rec["review"] = {
                "reviewed": True,
                "approved": bool(approved),
                "reviewed_by": _clean_str(reviewed_by, "Vijay"),
                "reviewed_at": _now(),
                "review_notes": _clean_str(review_notes),
                "automation_ready": bool(automation_ready and approved),
            }
            rec["updated_at"] = _now()
            _save_list(PLAYBOOKS_FILE, records)
            _pg_write(upsert_playbook, rec)
            return rec
    raise ValueError(f"Playbook not found: {playbook_id}")


def get_pattern_intelligence(classification: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
    """Aggregate pattern intelligence for a classification from resolutions and candidates.

    Returns a dict with dominant root causes, dominant actions, recurrence trend,
    pattern confidence, and a one-line insight. Returns None when no history exists.
    Read-only. No LLM call. No writes.
    """
    from collections import Counter
    cls_key = _classification_key(classification)
    if not cls_key:
        return None

    tid_key = _normalize_tenant(tenant_id) if tenant_id else None

    # ── Resolutions ───────────────────────────────────────────────────────────
    all_resolutions = _load_list(RESOLUTIONS_FILE)
    resolutions = [
        r for r in all_resolutions
        if _classification_key(r.get("classification")) == cls_key
        and (tid_key is None or not r.get("tenant_id") or _normalize_tenant(r.get("tenant_id")) == tid_key)
    ]

    # ── Candidates ────────────────────────────────────────────────────────────
    all_candidates = _load_list(KNOWLEDGE_CANDIDATES_FILE)
    candidates = [
        c for c in all_candidates
        if _classification_key(c.get("classification")) == cls_key
        and (tid_key is None or not c.get("tenant_id") or _normalize_tenant(c.get("tenant_id")) == tid_key)
    ]

    total_resolutions = len(resolutions)
    candidate_count = len(candidates)

    if total_resolutions == 0 and candidate_count == 0:
        return None

    # ── Root causes — tolerant of field variation ─────────────────────────────
    root_cause_tokens: List[str] = []
    for r in resolutions:
        rc = r.get("root_cause") or ""
        if isinstance(rc, str) and rc.strip():
            root_cause_tokens.append(rc.strip())
    for c in candidates:
        for field in ("probable_causes", "root_cause"):
            val = c.get(field)
            if isinstance(val, list):
                root_cause_tokens.extend([v for v in val if isinstance(v, str) and v.strip()])
            elif isinstance(val, str) and val.strip():
                root_cause_tokens.append(val.strip())

    # ── Actions — tolerant of field variation ─────────────────────────────────
    action_tokens: List[str] = []
    for r in resolutions:
        for field in ("validated_actions", "recommended_actions"):
            val = r.get(field)
            if isinstance(val, list):
                action_tokens.extend([v for v in val if isinstance(v, str) and v.strip()])
    for c in candidates:
        for field in ("validated_actions", "recommended_actions", "suggested_action"):
            val = c.get(field)
            if isinstance(val, list):
                action_tokens.extend([v for v in val if isinstance(v, str) and v.strip()])
            elif isinstance(val, str) and val.strip():
                action_tokens.append(val.strip())

    dominant_root_causes = [cause for cause, _ in Counter(root_cause_tokens).most_common(3)]
    dominant_actions = [action for action, _ in Counter(action_tokens).most_common(3)]

    # ── Pattern confidence ────────────────────────────────────────────────────
    if total_resolutions >= 5:
        pattern_confidence = "high"
    elif total_resolutions >= 2:
        pattern_confidence = "moderate"
    else:
        pattern_confidence = "low"

    # ── Recurrence trend — from outcome ledger timestamps ────────────────────
    recurrence_trend = "new"
    try:
        import resolution_outcome as _ro
        outcomes = _ro.get_outcomes(cls_key, tenant_id=tenant_id, limit=20)
        resolved = sorted(
            [o for o in outcomes if o.get("outcome_status") == "resolved" and o.get("resolved_at")],
            key=lambda o: o["resolved_at"],
        )
        if len(resolved) >= 3:
            gaps = []
            for i in range(1, len(resolved)):
                try:
                    from datetime import datetime, timezone
                    t1 = datetime.fromisoformat(resolved[i - 1]["resolved_at"].replace("Z", "+00:00"))
                    t2 = datetime.fromisoformat(resolved[i]["resolved_at"].replace("Z", "+00:00"))
                    gaps.append((t2 - t1).total_seconds())
                except Exception:
                    pass
            if gaps:
                first_half = sum(gaps[: len(gaps) // 2]) / max(len(gaps) // 2, 1)
                second_half = sum(gaps[len(gaps) // 2 :]) / max(len(gaps) - len(gaps) // 2, 1)
                recurrence_trend = "increasing" if second_half < first_half * 0.75 else "stable"
        # fewer than 3 resolved outcomes — not enough data to infer a trend
    except Exception:
        pass

    # ── Insight line ──────────────────────────────────────────────────────────
    parts = []
    if total_resolutions:
        parts.append(f"{total_resolutions} validated resolution(s)")
    if candidate_count:
        parts.append(f"{candidate_count} automation candidate(s)")
    if dominant_root_causes:
        parts.append(f"dominant cause: {dominant_root_causes[0]}")
    insight = "; ".join(parts) if parts else "No actionable pattern data yet."

    return {
        "total_resolutions":    total_resolutions,
        "candidate_count":      candidate_count,
        "dominant_root_causes": dominant_root_causes,
        "dominant_actions":     dominant_actions,
        "recurrence_trend":     recurrence_trend,
        "pattern_confidence":   pattern_confidence,
        "insight":              insight,
    }
