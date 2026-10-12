from __future__ import annotations
import json, os, re, traceback, uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

import settings_portable as settings
from agents.helpers import _automation_classification_key, _safe_limit, _normalize_api_row
from file_manager import append_debug_event, append_jsonl, read_json, utc_now_iso
from models import (
    AnalyzeRequest, FeedbackRequest, ReviewCandidateRequest,
    PromoteCandidateRequest, CreateResolutionRequest, CreatePlaybookRequest,
    FallbackControlRequest, TelemetryEventRequest, TelemetryPromoteRequest,
    AutomationPlanRequest, AutomationApproveRequest, AutomationExecuteRequest,
    AutomationPreExecutionCheckRequest, AutomationExecutionFeedbackRequest,
    AutomationConfidenceSummaryRequest, AutomationAutonomousEnableRequest,
    AutomationAutonomousEligibilityRequest, AutomationRefreshRequest,
    AutomationSuggestMappingRequest, AutomationLearnMappingRequest,
    AutomationCandidateCreateRequest, AutomationCandidateUpdateRequest,
    AutomationCandidateTestResultRequest, AutomationCandidateApproveRequest,
    AutomationCandidateEnableRequest, AutomationCandidateGeneratePlaybookRequest,
    AutomationCandidateSavePlaybookRequest, EvidenceSubmitRequest,
    _ANALYZE_MAX_TICKET_LEN,
)
from shared_utils import (
    SOURCE_PRIORITY,
    debug_enabled, pipeline_debug, retrieval_debug, confidence_debug,
    feedback_debug, fallback_debug, knowledge_debug,
    clean_text, dedupe_preserve_order, normalize_phrase,
    normalize_classification_token, source_priority_value,
    is_mainframe_batch_abend_signal, is_batch_delay_signal, is_batch_failure_signal,
    tokenize_signature, similarity_ratio,
    text_has_any, has_dns_negative_evidence, has_dns_positive_failure_evidence,
    has_vpn_negative_evidence, has_container_connectivity_signal,
    item_blob, item_is_container_relevant, item_contradicted_by_ticket,
    filter_contradicted_incidents,
    dedupe_semanticish, prioritize_and_dedupe_sections,
    classify_check_bucket, classify_action_bucket, bucket_dedupe, is_diagnostic_phrase,
)
from db_utils import (
    postgres_enabled, postgres_read_enabled, postgres_connect_readonly,
    postgres_read_rows, postgres_write_safe,
    load_taxonomy_from_postgres, load_feedback_corrections_from_postgres,
    load_incident_metadata_from_postgres,
    score_feedback_match, find_feedback_override,
)
try:
    from confidence_logger import log_confidence_event
except Exception:
    log_confidence_event = None
try:
    from fallback_logger import log_fallback_event
except Exception:
    log_fallback_event = None
try:
    from feedback_learning_enhanced import (
        record_feedback_event, search_feedback_learning, store_feedback_learning,
    )
except Exception:
    record_feedback_event = None
    search_feedback_learning = None
    store_feedback_learning = None
try:
    from groq_fallback_real import analyze_with_groq_fallback
except Exception:
    analyze_with_groq_fallback = None
try:
    from retrieval_engine import retrieve_incidents
except Exception:
    retrieve_incidents = None
try:
    from confidence_engine import compute_confidence_score
except Exception:
    compute_confidence_score = None
try:
    from knowledge_manager import (
        create_knowledge_candidate, review_knowledge_candidate,
        promote_candidate_to_incident, create_resolution, create_playbook,
        list_candidates, get_candidate_by_id,
        find_resolution_by_classification, find_playbook_by_classification,
    )
except Exception:
    create_knowledge_candidate = None
    review_knowledge_candidate = None
    promote_candidate_to_incident = None
    create_resolution = None
    create_playbook = None
    list_candidates = None
    get_candidate_by_id = None
    find_resolution_by_classification = None
    find_playbook_by_classification = None
try:
    from resolution_engine import get_resolution_engine
except Exception:
    get_resolution_engine = None
try:
    from postgres_store import insert_feedback_event as db_insert_feedback_event
except Exception:
    db_insert_feedback_event = None


router = APIRouter(tags=["analyze"])



@router.post("/analyze")
async def analyze(
    request: Request,
    ticket: Optional[str] = Form(default=None),
    image: Optional[UploadFile] = File(default=None),
    use_fallback: Optional[str] = Form(default=None),
    force_fallback: Optional[str] = Form(default=None),
    confirm_local_matches: Optional[str] = Form(default=None),
    tenant_id: Optional[str] = Form(default=None),
    knowledge_scope: Optional[str] = Form(default=None),
):
    from trace_context import ensure_trace_id, set_trace_id
    import uuid as _uuid
    _trace_id = request.headers.get("X-Trace-ID") or _uuid.uuid4().hex
    set_trace_id(_trace_id)

    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        try:
            from app import (
                analyze_core as _analyze_core,
                extract_text_from_image_bytes,
                ocr_text_has_operational_signal,
                apply_operational_ocr_override,
                get_tenant_defaults,
                parse_bool_form,
                DEFAULT_TENANT_ID,
            )
        except Exception as _imp_exc:
            raise HTTPException(status_code=503, detail=f"analyze_core not available: {_imp_exc}")
        base_ticket = clean_text(ticket or "")
        if len(base_ticket) > _ANALYZE_MAX_TICKET_LEN:
            raise HTTPException(status_code=400, detail=f"ticket field exceeds maximum length of {_ANALYZE_MAX_TICKET_LEN} characters.")
        image_bytes = b""
        image_meta = None
        ocr_text = ""

        if image is not None:
            image_bytes = await image.read()
            image_meta = {
                "name": image.filename,
                "mime": image.content_type,
                "size": len(image_bytes),
            }
            ocr_text = extract_text_from_image_bytes(image_bytes)

        # OCR priority rule:
        # If the uploaded image contains readable operational text, classify the incident
        # based on that text. The image is only the carrier, not the incident type.
        ocr_operational = bool(ocr_text and ocr_text_has_operational_signal(ocr_text))
        if ocr_operational:
            enriched_ticket = ocr_text if not base_ticket else f"{base_ticket}\n\nOCR incident text: {ocr_text}"
        else:
            enriched_ticket = base_ticket
            if ocr_text:
                enriched_ticket = f"{base_ticket}\n\nExtracted screenshot text: {ocr_text}" if base_ticket else ocr_text
            elif image_meta and not enriched_ticket:
                enriched_ticket = "Image attached without readable incident text"

        _effective_tenant = (tenant_id or "").strip() or DEFAULT_TENANT_ID
        tenant_defaults = get_tenant_defaults(_effective_tenant)
        req = AnalyzeRequest(
            ticket=enriched_ticket or "Image attached without readable incident text",
            use_fallback=parse_bool_form(use_fallback, True),
            force_fallback=parse_bool_form(force_fallback, False),
            confirm_local_matches=(
                parse_bool_form(confirm_local_matches, False) if confirm_local_matches is not None else None
            ),
            tenant_id=tenant_defaults["tenant_id"],
            knowledge_scope=(knowledge_scope or "").strip() or tenant_defaults["knowledge_scope"],
        )
        result = _analyze_core(req)

        if ocr_operational:
            result = apply_operational_ocr_override(result, ocr_text)

        # Always attach the real incident text used for analysis.
        # This prevents the UI feedback panel from showing
        # "Incident Description Unavailable" when the source was an uploaded screenshot.
        result["submitted_ticket"] = enriched_ticket or ""
        result["incident_description"] = enriched_ticket or ""
        result["display_ticket"] = enriched_ticket or ""

        result["ocr_attempted"] = image is not None
        result["ocr_text_extracted"] = bool(ocr_text)
        result["ocr_operational_text_detected"] = bool(ocr_operational)
        result["ocr_debug"] = {
            "image_received": image is not None,
            "ocr_text_length": len(ocr_text or ""),
            "ocr_operational_text_detected": bool(ocr_operational),
            "text_used_for_analysis": enriched_ticket or "",
        }
        if image_meta:
            result["uploaded_image_meta"] = image_meta
        if ocr_text:
            result["ocr_text"] = ocr_text
            result["submitted_ticket"] = ocr_text if ocr_operational else enriched_ticket
            result["incident_description"] = ocr_text if ocr_operational else enriched_ticket
            result["display_ticket"] = ocr_text if ocr_operational else enriched_ticket
        result["trace_id"] = _trace_id
        return JSONResponse(content=jsonable_encoder(result))

    try:
        payload = await request.json()
        req = AnalyzeRequest(**payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid request payload: {exc}")
    try:
        from app import analyze_core as _analyze_core
    except Exception:
        raise HTTPException(status_code=503, detail="analyze_core not available")
    try:
        result = _analyze_core(req)
    except HTTPException:
        raise
    except Exception as exc:
        import traceback as _tb
        raise HTTPException(status_code=500, detail=f"Analyze failed: {exc}") from exc
    try:
        result["trace_id"] = _trace_id
        return JSONResponse(content=jsonable_encoder(result))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Analyze serialization failed: {exc}") from exc


@router.post("/feedback")
def submit_feedback_route(request: FeedbackRequest) -> Dict[str, Any]:
    from app import load_taxonomy, infer_domain_from_taxonomy
    request_id = request.request_id or str(uuid.uuid4())
    taxonomy = load_taxonomy()
    stored_domain = infer_domain_from_taxonomy(request.corrected_classification, taxonomy)
    submitted_domain = request.submitted_domain.strip().lower() if request.submitted_domain else (request.original_domain or "general")
    feedback_payload = {
        "timestamp": utc_now_iso(),
        "request_id": request_id,
        "ticket": clean_text(request.ticket),
        "original_domain": request.original_domain or "general",
        "original_classification": request.original_classification or "unknown",
        "corrected_classification": request.corrected_classification,
        "submitted_domain": submitted_domain,
        "stored_domain": stored_domain,
        "is_correct": request.is_correct,
    }
    feedback_debug("feedback_received", feedback_payload, request_id=request_id)
    append_jsonl(settings.FEEDBACK_LOG_FILE, feedback_payload)
    if record_feedback_event is not None:
        try:
            record_feedback_event(feedback_payload)
        except Exception:
            feedback_debug("feedback_record_helper_error", {"error": traceback.format_exc()}, request_id=request_id)
    if store_feedback_learning is not None:
        try:
            store_feedback_learning(feedback_payload)
            feedback_debug("feedback_learning_stored", {"corrected_classification": request.corrected_classification, "stored_domain": stored_domain}, request_id=request_id)
        except Exception:
            feedback_debug("feedback_learning_store_error", {"error": traceback.format_exc()}, request_id=request_id)

    postgres_write_safe(
        "insert_feedback_event",
        db_insert_feedback_event,
        ticket=feedback_payload["ticket"],
        corrected_classification=feedback_payload["corrected_classification"],
        request_id=request_id,
        original_domain=feedback_payload["original_domain"],
        original_classification=feedback_payload["original_classification"],
        submitted_domain=feedback_payload["submitted_domain"],
        stored_domain=feedback_payload["stored_domain"],
        is_correct=bool(feedback_payload["is_correct"]),
    )

    return {
        "status": "ok",
        "request_id": request_id,
        "stored_domain": stored_domain,
        "corrected_classification": request.corrected_classification,
        "postgres_parallel_write": postgres_enabled(),
    }


@router.post("/review_candidate")
def review_candidate_route(request: ReviewCandidateRequest) -> Dict[str, Any]:
    if review_knowledge_candidate is None:
        raise HTTPException(status_code=500, detail="knowledge_manager not available")
    try:
        result = review_knowledge_candidate(
            candidate_id=request.candidate_id,
            reviewed_by=request.reviewed_by,
            approved=request.approved,
            review_notes=request.review_notes or "",
            updated_classification=request.updated_classification,
            updated_probable_causes=request.updated_probable_causes,
            updated_next_checks=request.updated_next_checks,
            updated_recommended_actions=request.updated_recommended_actions,
        )
        return {"status": "ok", "candidate": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/promote_candidate")
def promote_candidate_route(request: PromoteCandidateRequest) -> Dict[str, Any]:
    if promote_candidate_to_incident is None:
        raise HTTPException(status_code=500, detail="knowledge_manager not available")
    from agents.governance import check_export_governance
    operation = "global_promote" if request.promote_to_global else "promote"
    gate = check_export_governance(tenant_id=request.tenant_id, operation=operation)
    if not gate.get("allowed"):
        raise HTTPException(status_code=403, detail=gate.get("reason", "Blocked by governance policy."))
    try:
        result = promote_candidate_to_incident(
            request.candidate_id,
            tenant_id=request.tenant_id or "client_a",
            promote_to_global=request.promote_to_global,
            promoted_by=request.promoted_by or "engineer",
        )
        key = "pattern" if request.promote_to_global else "incident"
        return {"status": "ok", key: result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/run_global_pattern_analytics")
def run_global_pattern_analytics_route(dry_run: bool = False) -> Dict[str, Any]:
    """Run Problem Management analytics — detect recurring patterns and causal chains."""
    try:
        from global_pattern_analytics import run_analytics
        return run_analytics(dry_run=dry_run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/global_pattern_stats")
def get_global_pattern_stats_route() -> Dict[str, Any]:
    """Return Problem Management candidates with causal chains (read-only, no disk write)."""
    try:
        from global_pattern_analytics import compute_problem_management_candidates
        return {"candidates": compute_problem_management_candidates()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create_resolution")
def create_resolution_route(request: CreateResolutionRequest) -> Dict[str, Any]:
    if create_resolution is None:
        raise HTTPException(status_code=500, detail="knowledge_manager not available")
    from agents.governance import check_export_governance
    gate = check_export_governance(tenant_id=request.tenant_id, operation="local_write")
    if not gate.get("allowed"):
        raise HTTPException(status_code=403, detail=gate.get("reason", "Blocked by governance policy."))
    try:
        resolution = create_resolution(
            classification=_automation_classification_key(request.classification),
            root_cause=request.root_cause,
            validated_checks=request.validated_checks,
            validated_actions=request.validated_actions,
            confirmed_by=request.confirmed_by,
            success_notes=request.success_notes,
            linked_candidate_id=request.linked_candidate_id or "",
            automation_ready=bool(request.automation_ready),
            linked_playbook_id=request.linked_playbook_id or "",
            incident_id=request.incident_id or "",
        )
        return {"status": "ok", "resolution": resolution}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/autonomous_gate")
def autonomous_gate_route(
    classification: str = "",
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """Return the ledger-based autonomous execution gate tier for a classification.

    Always returns 200 with a gate dict. On any failure, returns hold_for_approval
    so callers degrade safely.
    """
    try:
        cls = _automation_classification_key(classification or "")
        tid = (tenant_id or "default").strip() or "default"
        if not cls:
            return {"tier": "hold_for_approval", "reason": "No classification provided.", "total_outcomes": 0, "success_rate": None, "confidence_signal": None}
        from resolution_outcome import autonomous_gate
        return autonomous_gate(cls, tenant_id=tid)
    except Exception:
        return {"tier": "hold_for_approval", "reason": "Gate check failed — defaulting to hold for approval.", "total_outcomes": 0, "success_rate": None, "confidence_signal": None}


@router.get("/pattern_intelligence")
def pattern_intelligence_route(
    classification: str = "",
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """Return classification pattern intelligence from resolutions and candidates.

    Always returns 200. Use `status` field to distinguish ok / no_history.
    Fail-safe: any internal error returns no_history so the UI degrades gracefully.
    """
    try:
        cls = _automation_classification_key(classification or "")
        tid = (tenant_id or "default").strip() or "default"
        if not cls:
            return {"status": "no_history"}
        from knowledge_manager import get_pattern_intelligence
        intel = get_pattern_intelligence(cls, tenant_id=tid)
        if intel is None:
            return {"status": "no_history"}
        return {"status": "ok", "classification": cls, "tenant_id": tid, "intelligence": intel}
    except Exception:
        return {"status": "no_history"}


@router.get("/resolution_intelligence")
def resolution_intelligence_route(
    classification: str = "",
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """Return historical outcome intelligence for a classification+tenant pair.

    Always returns 200. Use `status` field to distinguish ok / no_history.
    Fail-safe: any internal error returns no_history so the UI degrades gracefully.
    """
    try:
        cls = _automation_classification_key(classification or "")
        tid = (tenant_id or "default").strip() or "default"
        if not cls:
            return {"status": "no_history"}
        from resolution_outcome import summarize_outcomes
        summary = summarize_outcomes(cls, tenant_id=tid)
        if summary is None:
            return {"status": "no_history"}
        return {"status": "ok", "classification": cls, "tenant_id": tid, "summary": summary}
    except Exception:
        return {"status": "no_history"}


@router.post("/create_playbook")
def create_playbook_route(request: CreatePlaybookRequest) -> Dict[str, Any]:
    if create_playbook is None:
        raise HTTPException(status_code=500, detail="knowledge_manager not available")
    from agents.governance import check_export_governance
    gate = check_export_governance(tenant_id=request.tenant_id, operation="local_write")
    if not gate.get("allowed"):
        raise HTTPException(status_code=403, detail=gate.get("reason", "Blocked by governance policy."))
    try:
        playbook = create_playbook(
            name=request.name,
            classification=_automation_classification_key(request.classification),
            description=request.description,
            steps=request.steps,
            scripts=request.scripts,
            approval_mode=request.approval_mode or "manual_approval_required",
            rollback_available=bool(request.rollback_available),
            risk_tier=request.risk_tier or "medium",
        )
        return {"status": "ok", "playbook": playbook}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))




def _safe_limit(limit: int = 100, maximum: int = 1000) -> int:
    try:
        value = int(limit)
    except Exception:
        value = 100
    return max(1, min(value, maximum))


def _normalize_api_row(row: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in dict(row or {}).items():
        if hasattr(value, "isoformat"):
            clean[key] = value.isoformat()
        else:
            clean[key] = value
    return clean


def _list_telemetry_events(limit: int = 100) -> List[Dict[str, Any]]:
    limit = _safe_limit(limit)
    if db_get_telemetry_events is not None and postgres_enabled():
        try:
            rows = db_get_telemetry_events(limit=limit)
            return [_normalize_api_row(x) for x in rows]
        except Exception:
            pipeline_debug("telemetry_events_db_read_error", {"traceback": traceback.format_exc()})
    if json_list_telemetry_events is not None:
        try:
            return [_normalize_api_row(x) for x in json_list_telemetry_events(limit=limit)]
        except Exception:
            pipeline_debug("telemetry_events_json_read_error", {"traceback": traceback.format_exc()})
    return []


def _list_telemetry_incidents(limit: int = 100) -> List[Dict[str, Any]]:
    limit = _safe_limit(limit)
    if db_get_telemetry_incidents is not None and postgres_enabled():
        try:
            rows = db_get_telemetry_incidents(limit=limit)
            return [_normalize_api_row(x) for x in rows]
        except Exception:
            pipeline_debug("telemetry_incidents_db_read_error", {"traceback": traceback.format_exc()})
    if json_list_telemetry_incidents is not None:
        try:
            return [_normalize_api_row(x) for x in json_list_telemetry_incidents(limit=limit)]
        except Exception:
            pipeline_debug("telemetry_incidents_json_read_error", {"traceback": traceback.format_exc()})
    return []


def _get_telemetry_incident(incident_id: str) -> Optional[Dict[str, Any]]:
    incident_id = clean_text(incident_id)
    if not incident_id:
        return None
    if db_get_telemetry_incident_by_id is not None and postgres_enabled():
        try:
            row = db_get_telemetry_incident_by_id(incident_id)
            if row:
                return _normalize_api_row(row)
        except Exception:
            pipeline_debug("telemetry_incident_db_read_error", {"incident_id": incident_id, "traceback": traceback.format_exc()})
    if json_get_telemetry_incident_by_id is not None:
        try:
            row = json_get_telemetry_incident_by_id(incident_id)
            if row:
                return _normalize_api_row(row)
        except Exception:
            pipeline_debug("telemetry_incident_json_read_error", {"incident_id": incident_id, "traceback": traceback.format_exc()})
    return None


def _build_telemetry_ticket(incident: Dict[str, Any]) -> str:
    labels = incident.get("labels") if isinstance(incident.get("labels"), dict) else {}
    label_lines = ", ".join([f"{k}={v}" for k, v in labels.items()]) if labels else ""
    parts = [
        "Telemetry-triggered incident requiring AI RCA.",
        f"Alert/Title: {incident.get('title') or incident.get('classification') or 'Telemetry Alert'}",
        f"Severity: {incident.get('severity') or 'unknown'}",
        f"Domain: {incident.get('domain') or 'general'}",
        f"Classification hint: {incident.get('classification') or 'unknown'}",
        f"Affected resource: {incident.get('resource') or incident.get('service') or 'unknown'}",
        f"Environment: {incident.get('environment') or 'production'}",
        f"Trigger metric: {incident.get('trigger_metric') or incident.get('metric') or 'unknown'}",
        f"Trigger value: {incident.get('trigger_value') or incident.get('value') or 'unknown'}",
        f"Telemetry rule: {incident.get('telemetry_rule_id') or 'unknown'}",
        f"Details: {incident.get('content') or incident.get('description') or ''}",
    ]
    if label_lines:
        parts.append(f"Labels: {label_lines}")
    return "\n".join([p for p in parts if clean_text(p)])


def _extract_rca_payload(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    payload = row.get("rca_json")
    if isinstance(payload, dict):
        return _normalize_api_row(payload)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return _normalize_api_row(parsed)
        except Exception:
            pass
    return _normalize_api_row(row)

