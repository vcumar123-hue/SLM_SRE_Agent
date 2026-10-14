from __future__ import annotations
from default_tenant import default_tenant_id
import json, os, re, traceback, uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

import settings_portable as settings
from file_manager import append_debug_event, append_jsonl, read_json, utc_now_iso
from agents.helpers import _safe_limit, _normalize_api_row, _automation_classification_key
from models import (
    AnalyzeRequest, FeedbackRequest, ReviewCandidateRequest,
    PromoteCandidateRequest, CreateResolutionRequest, CreatePlaybookRequest,
    FallbackControlRequest, TelemetryEventRequest, TelemetryPromoteRequest,
    TelemetryStatusUpdateRequest,
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
    from telemetry_trigger import (
        append_telemetry_event,
        evaluate_telemetry_rules,
        promote_telemetry_incidents_to_main_knowledge,
        list_telemetry_events as json_list_telemetry_events,
        list_telemetry_incidents as json_list_telemetry_incidents,
        get_telemetry_incident_by_id as json_get_telemetry_incident_by_id,
        update_telemetry_incident_status as json_update_telemetry_incident_status,
        mark_telemetry_incident_rca_complete as json_mark_telemetry_incident_rca_complete,
        get_telemetry_incident_status_history as json_get_telemetry_incident_status_history,
    )
except Exception:
    append_telemetry_event = None
    evaluate_telemetry_rules = None
    promote_telemetry_incidents_to_main_knowledge = None
    json_list_telemetry_events = None
    json_list_telemetry_incidents = None
    json_get_telemetry_incident_by_id = None
    json_update_telemetry_incident_status = None
    json_mark_telemetry_incident_rca_complete = None
    json_get_telemetry_incident_status_history = None
try:
    from postgres_store import (
        insert_telemetry_event as db_insert_telemetry_event,
        insert_telemetry_incident as db_insert_telemetry_incident,
        get_telemetry_events as db_get_telemetry_events,
        get_telemetry_incidents as db_get_telemetry_incidents,
        get_telemetry_incident_by_id as db_get_telemetry_incident_by_id,
        upsert_telemetry_rca_result as db_upsert_telemetry_rca_result,
        get_telemetry_rca_result as db_get_telemetry_rca_result,
        update_telemetry_incident_status as db_update_telemetry_incident_status,
        mark_telemetry_incident_rca_complete as db_mark_telemetry_incident_rca_complete,
        get_telemetry_incident_status_history as db_get_telemetry_incident_status_history,
    )
except Exception:
    db_insert_telemetry_event = None
    db_insert_telemetry_incident = None
    db_get_telemetry_events = None
    db_get_telemetry_incidents = None
    db_get_telemetry_incident_by_id = None
    db_upsert_telemetry_rca_result = None
    db_get_telemetry_rca_result = None
    db_update_telemetry_incident_status = None
    db_mark_telemetry_incident_rca_complete = None
    db_get_telemetry_incident_status_history = None
try:
    from confidence_logger import log_confidence_event
except Exception:
    log_confidence_event = None
try:
    from retrieval_engine import retrieve_incidents
except Exception:
    retrieve_incidents = None
try:
    from confidence_engine import compute_confidence_score
except Exception:
    compute_confidence_score = None
try:
    from groq_fallback_real import analyze_with_groq_fallback
except Exception:
    analyze_with_groq_fallback = None


router = APIRouter(tags=["telemetry"])

DEFAULT_TENANT_ID = default_tenant_id()


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



@router.get("/telemetry/events")
def telemetry_events_route(limit: int = 100) -> Dict[str, Any]:
    items = _list_telemetry_events(limit=limit)
    return {"status": "ok", "count": len(items), "items": items}


@router.get("/telemetry/incidents")
def telemetry_incidents_route(limit: int = 100) -> Dict[str, Any]:
    items = _list_telemetry_incidents(limit=limit)
    return {"status": "ok", "count": len(items), "items": items}


@router.get("/telemetry/incidents/{incident_id}")
def telemetry_incident_detail_route(incident_id: str) -> Dict[str, Any]:
    incident = _get_telemetry_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Telemetry incident not found")
    return {"status": "ok", "item": incident}


@router.get("/telemetry/incidents/{incident_id}/rca")
def telemetry_incident_rca_route(incident_id: str) -> Dict[str, Any]:
    try:
        from agents.analyze import _extract_rca_payload  # noqa: PLC0415
    except Exception:
        _extract_rca_payload = None  # type: ignore[assignment]
    if db_get_telemetry_rca_result is not None and postgres_enabled() and _extract_rca_payload is not None:
        try:
            row = db_get_telemetry_rca_result(incident_id)
            payload = _extract_rca_payload(row)
            if payload:
                return {"status": "ok", "found": True, "item": payload, "record": _normalize_api_row(row or {})}
        except Exception:
            pipeline_debug("telemetry_rca_db_read_error", {"incident_id": incident_id, "traceback": traceback.format_exc()})
    return {"status": "ok", "found": False, "item": None}


@router.post("/telemetry/incidents/{incident_id}/analyze")
def telemetry_incident_analyze_route(incident_id: str) -> Dict[str, Any]:
    try:
        from app import analyze_core, get_tenant_defaults  # noqa: PLC0415
        from agents.analyze import _build_telemetry_ticket  # noqa: PLC0415
    except Exception:
        analyze_core = None  # type: ignore[assignment]
        get_tenant_defaults = None  # type: ignore[assignment]
        _build_telemetry_ticket = None  # type: ignore[assignment]
    if _build_telemetry_ticket is None or analyze_core is None:
        raise HTTPException(status_code=500, detail="analyze_core or _build_telemetry_ticket not available")
    incident = _get_telemetry_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Telemetry incident not found")
    ticket = _build_telemetry_ticket(incident)
    tenant_defaults = get_tenant_defaults(DEFAULT_TENANT_ID) if get_tenant_defaults else {}
    req = AnalyzeRequest(
        ticket=ticket,
        use_fallback=True,
        force_fallback=False,
        confirm_local_matches=None,
        tenant_id=DEFAULT_TENANT_ID,
        knowledge_scope=tenant_defaults.get("knowledge_scope", "client_private"),
    )
    result = analyze_core(req)
    result["telemetry_incident"] = incident
    result["telemetry_incident_id"] = incident_id
    result["telemetry_alert_name"] = incident.get("title") or incident.get("classification")
    result["submitted_ticket"] = ticket
    result["incident_description"] = ticket
    result["display_ticket"] = ticket
    if db_upsert_telemetry_rca_result is not None and postgres_enabled():
        try:
            db_upsert_telemetry_rca_result(incident_id, jsonable_encoder(result))
        except Exception:
            pipeline_debug("telemetry_rca_db_write_error", {"incident_id": incident_id, "traceback": traceback.format_exc()})
    return {"status": "ok", "incident_id": incident_id, "item": jsonable_encoder(result)}


@router.post("/telemetry/event")
def telemetry_event_route(request: TelemetryEventRequest) -> Dict[str, Any]:
    if append_telemetry_event is None or evaluate_telemetry_rules is None:
        raise HTTPException(status_code=500, detail="telemetry_trigger not available")
    try:
        event = append_telemetry_event(
            {
                "metric": request.metric,
                "value": request.value,
                "resource": request.resource or "",
                "service": request.service or "",
                "environment": request.environment or "production",
                "source": request.source or "manual",
                "labels": request.labels or {},
            }
        )
        postgres_write_safe(
            "insert_telemetry_event",
            db_insert_telemetry_event,
            event_id=event.get("event_id"),
            metric=event.get("metric"),
            value=float(event.get("value", 0.0) or 0.0),
            resource=event.get("resource"),
            service=event.get("service"),
            environment=event.get("environment"),
            source=event.get("source"),
            labels=event.get("labels") or {},
        )
        evaluation = evaluate_telemetry_rules()
        for item in evaluation.get("created_incidents", []) or []:
            postgres_write_safe("insert_telemetry_incident", db_insert_telemetry_incident, item)
            
        response_payload = {
            "status": "ok",
            "event": event,
            "evaluation": evaluation,
            "postgres_parallel_write": postgres_enabled(),
        }
        return jsonable_encoder(response_payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/telemetry/alertmanager")
def telemetry_alertmanager_route(payload: Dict[str, Any]) -> Dict[str, Any]:
    if append_telemetry_event is None or evaluate_telemetry_rules is None:
        raise HTTPException(status_code=500, detail="telemetry_trigger not available")
    try:
        alerts = payload.get("alerts") or []
        ingested: List[Dict[str, Any]] = []
        for alert in alerts:
            labels = alert.get("labels") or {}
            annotations = alert.get("annotations") or {}
            metric_name = labels.get("metric_name") or "alert_value"
            raw_value = alert.get("valueString") or labels.get("observed_value") or "0"
            match = re.search(r"-?\d+(?:\.\d+)?", str(raw_value))
            numeric_value = float(match.group(0)) if match else 0.0
            resource = labels.get("instance") or labels.get("pod") or labels.get("job") or "unknown_resource"
            event = append_telemetry_event(
                {
                    "metric": metric_name,
                    "value": numeric_value,
                    "resource": resource,
                    "service": labels.get("job") or "",
                    "environment": labels.get("environment") or "production",
                    "source": "alertmanager",
                    "labels": {
                        **labels,
                        "summary": annotations.get("summary", ""),
                        "description": annotations.get("description", ""),
                        "status": alert.get("status", "firing"),
                    },
                }
            )
            ingested.append(event)
        for event in ingested:
            postgres_write_safe(
                "insert_telemetry_event",
                db_insert_telemetry_event,
                event_id=event.get("event_id"),
                metric=event.get("metric"),
                value=float(event.get("value", 0.0) or 0.0),
                resource=event.get("resource"),
                service=event.get("service"),
                environment=event.get("environment"),
                source=event.get("source"),
                labels=event.get("labels") or {},
            )
        evaluation = evaluate_telemetry_rules()
        for item in evaluation.get("created_incidents", []) or []:
            postgres_write_safe("insert_telemetry_incident", db_insert_telemetry_incident, item)
        return jsonable_encoder({"status": "ok", "ingested_count": len(ingested), "evaluation": evaluation, "postgres_parallel_write": postgres_enabled()})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/telemetry/evaluate")
def telemetry_evaluate_route() -> Dict[str, Any]:
    if evaluate_telemetry_rules is None:
        raise HTTPException(status_code=500, detail="telemetry_trigger not available")
    try:
        evaluation = evaluate_telemetry_rules()
        for item in evaluation.get("created_incidents", []) or []:
            postgres_write_safe("insert_telemetry_incident", db_insert_telemetry_incident, item)
        return evaluation
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/telemetry/promote")
def telemetry_promote_route(request: TelemetryPromoteRequest) -> Dict[str, Any]:
    if promote_telemetry_incidents_to_main_knowledge is None:
        raise HTTPException(status_code=500, detail="telemetry_trigger not available")
    try:
        from agents.governance import check_export_governance  # noqa: PLC0415
    except Exception:
        check_export_governance = None  # type: ignore[assignment]
    if check_export_governance is None:
        raise HTTPException(status_code=500, detail="governance module not available")
    # MF-08: Enforce cross-tenant export governance before promoting to global knowledge
    tenant_id = getattr(request, "tenant_id", None) or DEFAULT_TENANT_ID
    gate = check_export_governance(tenant_id, operation="promote")
    if not gate.get("allowed"):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "governance_blocked",
                "message": gate.get("reason", "Export blocked by governance policy."),
                "blocked_by": gate.get("blocked_by"),
                "tenant_id": tenant_id,
            },
        )
    try:
        return promote_telemetry_incidents_to_main_knowledge(limit=request.limit)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Status update helpers
# ---------------------------------------------------------------------------

def _update_telemetry_incident_status(incident_id: str, request: TelemetryStatusUpdateRequest) -> Optional[Dict[str, Any]]:
    if db_update_telemetry_incident_status is not None and postgres_enabled():
        try:
            row = db_update_telemetry_incident_status(
                incident_id=incident_id,
                status=request.status,
                updated_by=request.updated_by,
                notes=request.notes,
                external_system=request.external_system,
                external_ticket_id=request.external_ticket_id,
                external_ticket_url=request.external_ticket_url,
                ticket_sync_status=request.ticket_sync_status,
            )
            if row:
                return _normalize_api_row(row)
        except Exception:
            pipeline_debug("telemetry_status_db_update_error", {"incident_id": incident_id, "traceback": traceback.format_exc()})
    if json_update_telemetry_incident_status is not None:
        try:
            row = json_update_telemetry_incident_status(
                incident_id=incident_id,
                status=request.status,
                updated_by=request.updated_by,
                notes=request.notes,
                external_system=request.external_system,
                external_ticket_id=request.external_ticket_id,
                external_ticket_url=request.external_ticket_url,
                ticket_sync_status=request.ticket_sync_status,
            )
            if row:
                return _normalize_api_row(row)
        except Exception:
            pipeline_debug("telemetry_status_json_update_error", {"incident_id": incident_id, "traceback": traceback.format_exc()})
    return None


def _get_telemetry_status_history(incident_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    limit = _safe_limit(limit)
    if db_get_telemetry_incident_status_history is not None and postgres_enabled():
        try:
            rows = db_get_telemetry_incident_status_history(incident_id, limit=limit)
            return [_normalize_api_row(x) for x in rows]
        except Exception:
            pipeline_debug("telemetry_status_history_db_error", {"incident_id": incident_id, "traceback": traceback.format_exc()})
    if json_get_telemetry_incident_status_history is not None:
        try:
            rows = json_get_telemetry_incident_status_history(incident_id, limit=limit)
            return [_normalize_api_row(x) for x in rows]
        except Exception:
            pipeline_debug("telemetry_status_history_json_error", {"incident_id": incident_id, "traceback": traceback.format_exc()})
    return []


# ---------------------------------------------------------------------------
# Status update routes
# ---------------------------------------------------------------------------

@router.post("/telemetry/incidents/{incident_id}/status")
def telemetry_incident_status_update_route(incident_id: str, request: TelemetryStatusUpdateRequest) -> Dict[str, Any]:
    incident = _get_telemetry_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Telemetry incident not found")
    updated = _update_telemetry_incident_status(incident_id, request)
    if not updated:
        raise HTTPException(status_code=500, detail="Unable to update telemetry incident status")
    history = _get_telemetry_status_history(incident_id, limit=25)
    return {"status": "ok", "item": updated, "history": history}


@router.get("/telemetry/incidents/{incident_id}/status_history")
def telemetry_incident_status_history_route(incident_id: str, limit: int = 100) -> Dict[str, Any]:
    incident = _get_telemetry_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Telemetry incident not found")
    items = _get_telemetry_status_history(incident_id, limit=limit)
    return {"status": "ok", "count": len(items), "items": items}



