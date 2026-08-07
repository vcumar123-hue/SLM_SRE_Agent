from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import settings_portable as settings
from file_manager import read_json, write_json, utc_now_iso


KNOWLEDGE_CANDIDATES_FILE = settings.DATA_DIR / "knowledge_candidates.json"
RESOLUTIONS_FILE = settings.DATA_DIR / "resolutions.json"
PLAYBOOKS_FILE = settings.DATA_DIR / "playbooks.json"
GLOBAL_LEARNED_PATTERNS_FILE = settings.DATA_DIR / "global_learned_patterns.json"


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
) -> Dict[str, Any]:
    records = _load_list(RESOLUTIONS_FILE)
    classification_clean = _clean_str(classification, "unknown")
    classification_key = classification_clean.lower()

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
        existing["updated_at"] = _now()
        _save_list(RESOLUTIONS_FILE, records)
        return existing

    resolution = {
        "resolution_id": f"res_{uuid.uuid4().hex[:12]}",
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
            return rec
    raise ValueError(f"Playbook not found: {playbook_id}")
