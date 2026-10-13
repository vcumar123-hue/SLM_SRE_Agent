from __future__ import annotations
import json, os, re, traceback, uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

import settings_portable as settings
from security import principal_from_request
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
    from postgres_store import insert_feedback_event as db_insert_feedback_event
except Exception:
    db_insert_feedback_event = None


router = APIRouter(tags=["evidence"])

EVIDENCE_STORE_DIR = Path("data") / "evidence"
EVIDENCE_UPLOAD_MANIFEST = Path("data") / "ui_runtime" / "client_evidence_upload_manifest.json"

_EVIDENCE_STATUS_NORM: dict = {
    "green": "green", "ok": "green", "pass": "green", "passing": "green", "healthy": "green", "clear": "green",
    "amber": "amber", "warn": "amber", "warning": "amber", "degraded": "amber",
    "red": "red", "fail": "red", "failing": "red", "error": "red", "critical": "red",
    "unknown": "unknown", "missing": "unknown",
}

_VALID_EVIDENCE_TYPES = {
    "drift_status", "change_conflict", "error_budget_status",
    "concurrent_activity", "rollback_readiness", "blast_radius",
}



@router.post("/evidence/submit")
def evidence_submit(request: EvidenceSubmitRequest, http_request: Request) -> Dict[str, Any]:
    """Accept API-Push evidence from external adapters or source systems.

    Requires an authenticated principal (role l2 or above, enforced by the route table). The submitter is
    recorded in the evidence; the Safety Gate treats evidence pushed by an authenticated principal as
    `verified`, so who may call this route is a trust decision.
    """
    principal = principal_from_request(http_request)
    if principal is None:
        raise HTTPException(status_code=401, detail="authentication required")
    ev_type = (request.evidence_type or "").strip().lower()
    if ev_type not in _VALID_EVIDENCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown evidence_type '{ev_type}'. Must be one of: {sorted(_VALID_EVIDENCE_TYPES)}",
        )

    raw_status = (request.status or "unknown").strip().lower()
    normalised_status = _EVIDENCE_STATUS_NORM.get(raw_status, raw_status)

    payload: Dict[str, Any] = {
        "evidence_type": ev_type,
        "service": request.service or "unknown",
        "environment": request.environment or "unknown",
        "resource": request.resource or "unknown",
        "status": normalised_status,
        "severity": request.severity or "low",
        "source_system": request.source_system or "api_push",
        "summary": request.summary or f"{ev_type} evidence submitted via API Push.",
        "details": request.details or {},
        "checked_at": request.checked_at or utc_now_iso(),
        "confidence": request.confidence if request.confidence is not None else 0.85,
        "_submitted_via": "api_push",
        "submitted_by": principal.id,
        "submitted_role": principal.role,
        "submitted_at": utc_now_iso(),
    }

    EVIDENCE_STORE_DIR.mkdir(parents=True, exist_ok=True)
    evidence_file = EVIDENCE_STORE_DIR / f"{ev_type}.json"
    try:
        with open(evidence_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write evidence file: {exc}")

    try:
        manifest: Dict[str, Any] = {}
        if EVIDENCE_UPLOAD_MANIFEST.exists():
            with open(EVIDENCE_UPLOAD_MANIFEST, encoding="utf-8") as fh:
                manifest = json.load(fh)
        manifest.setdefault("evidence", {})[ev_type] = {
            "client_supplied": True,
            "source": "api_push",
            "uploaded_at": utc_now_iso(),
        }
        EVIDENCE_UPLOAD_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with open(EVIDENCE_UPLOAD_MANIFEST, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
    except Exception:
        pass

    return {
        "status": "ok",
        "evidence_type": ev_type,
        "normalised_status": normalised_status,
        "evidence_file": str(evidence_file),
        "message": f"Evidence for '{ev_type}' accepted and stored. Status: {normalised_status}.",
    }


@router.get("/evidence/latest")
def evidence_latest(evidence_type: Optional[str] = None) -> Dict[str, Any]:
    """Return current evidence payload for one or all evidence types."""
    if evidence_type:
        ev_type = evidence_type.strip().lower()
        if ev_type not in _VALID_EVIDENCE_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown evidence_type '{ev_type}'. Must be one of: {sorted(_VALID_EVIDENCE_TYPES)}",
            )
        evidence_file = EVIDENCE_STORE_DIR / f"{ev_type}.json"
        if not evidence_file.exists():
            return {"status": "missing", "evidence_type": ev_type, "data": None}
        try:
            with open(evidence_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return {"status": "ok", "evidence_type": ev_type, "data": data}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read evidence file: {exc}")

    result: Dict[str, Any] = {}
    for ev_type in sorted(_VALID_EVIDENCE_TYPES):
        evidence_file = EVIDENCE_STORE_DIR / f"{ev_type}.json"
        if evidence_file.exists():
            try:
                with open(evidence_file, encoding="utf-8") as fh:
                    result[ev_type] = json.load(fh)
            except Exception:
                result[ev_type] = {"status": "error", "evidence_type": ev_type}
        else:
            result[ev_type] = {"status": "missing", "evidence_type": ev_type}

    return {"status": "ok", "evidence": result, "count": len(result)}


@router.post("/evidence/run_api_pull")
def evidence_run_api_pull(dry_run: bool = False) -> Dict[str, Any]:
    """Execute HTTP pulls from saved api_pull_specs/ and refresh evidence files (MF-04)."""
    try:
        from adapters.universal_api_adapter import run_from_pull_specs
        return run_from_pull_specs(dry_run=dry_run)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Production hardening notes:
# - Remove raw image bytes from responses
# - Add draft resolution/playbook generation after strong fallback candidates
# - Queue readiness when approved playbook exists

