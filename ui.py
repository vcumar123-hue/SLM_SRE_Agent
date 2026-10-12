import json
import os
import re
import uuid
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from typing import Any, Dict, List

import requests
import streamlit as st
import streamlit.components.v1 as _st_components

from dashboard import render_dashboard
from onboarding_studio import render_client_onboarding_studio

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8001").rstrip("/")
REQUEST_TIMEOUT_SHORT = 30
REQUEST_TIMEOUT_MEDIUM = 60
REQUEST_TIMEOUT_LONG = 180


SESSION = requests.Session()
# Release 1: the backend authenticates every request. Set AIOPS_UI_API_KEY to the token of a
# principal configured in AIOPS_PRINCIPALS (see security.py). In open dev mode no key is needed.
if os.getenv("AIOPS_UI_API_KEY", "").strip():
    SESSION.headers["Authorization"] = "Bearer " + os.getenv("AIOPS_UI_API_KEY", "").strip()

# Disk-backed UI state keeps the latest incident available even when the
# top navigation href causes Streamlit to create a fresh browser session.
# This is local runtime UI state only; it does not modify backend records.
UI_RUNTIME_DIR = Path("data") / "ui_runtime"
UI_LAST_INCIDENT_FILE = UI_RUNTIME_DIR / "last_incident_session.json"
# Stable only for the lifetime of the current Streamlit server process.
# This lets top-nav href clicks restore the active incident when Streamlit
# creates a fresh browser session, while preventing stale incident restore
# after ui.py / Streamlit is restarted.
UI_RUNTIME_INSTANCE_ID = os.environ.setdefault("AIOPS_UI_RUNTIME_INSTANCE_ID", uuid.uuid4().hex)



st.markdown(
    """
    <style>
    .aiops-card-equal {
        height: 150px !important;
        min-height: 150px !important;
        width: 100% !important;
        display: flex !important;
        flex-direction: column !important;
        align-items: center !important;
        justify-content: center !important;
        text-align: center !important;
        overflow: hidden !important;
    }
    .aiops-card-label {
        font-size: clamp(0.70rem, 1vw, 0.86rem) !important;
        line-height: 1.20 !important;
        max-height: 42px !important;
        overflow: hidden !important;
        text-align: center !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        width: 100% !important;
    }
    .aiops-card-value {
        font-size: clamp(1.25rem, 2.1vw, 1.85rem) !important;
        font-weight: 750 !important;
        line-height: 1.05 !important;
        max-height: 88px !important;
        overflow: hidden !important;
        overflow-wrap: anywhere !important;
        text-align: center !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        width: 100% !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# CQ-02: Modular page imports — pages/ modules are loaded lazily to avoid circular imports
from page_modules.shared import (
    render_empty_state, render_api_error,
    render_confidence_meter, render_status_badge,
    render_automation_pipeline_steps, render_evidence_ttl_warning,
    render_incident_action_banner, render_compact_bullet_list,
    render_resolution_playbook,
    metric_card, nice_label, format_confidence,
    section_title, system_label, natural_label,
    routing_display_label, decision_display_label,
    shorten_text, dedupe_lines, lines_to_list, join_lines,
    clear_lightweight_page_cache,
)
from page_modules.telemetry_console import render_telemetry_console_page, apply_ui_telemetry_rca_display_guardrail
from page_modules.client_onboarding import render_execution_evidence
from page_modules.knowledge_center import render_knowledge_center_page
from page_modules.backtesting import render_backtesting_page
from page_modules.backtest_console import render_backtest_console_page
from page_modules.peer_insights import render_peer_insights_panel
from page_modules.automation_queue import render_automation_queue_page
from page_modules.client_onboarding import render_client_onboarding_page_with_submenus
try:
    from page_modules.drift import render_drift_page
    from page_modules.error_tolerance import render_error_tolerance_page
    _new_modules_loaded = True
except Exception:
    _new_modules_loaded = False


def _backend_post(url: str, *, json=None, data=None, files=None, timeout: int) -> "requests.Response":
    """POST to the backend with one automatic retry on connection-abort errors.

    uvicorn --reload can kill a worker mid-request (WSAECONNABORTED / RemoteDisconnected).
    A single retry after a short pause recovers transparently in almost every case.
    """
    import time as _time
    _RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        ConnectionAbortedError,
        ConnectionResetError,
    )
    for attempt in range(2):
        try:
            return SESSION.post(url, json=json, data=data, files=files, timeout=timeout)
        except _RETRYABLE as exc:
            if attempt == 0:
                _time.sleep(1.5)
                continue
            raise


def call_backend_with_text(
    user_input: str,
    use_fallback: bool = True,
    force_fallback: bool = False,
    confirm_local_matches: bool | None = None,
    upload_info: Dict[str, Any] | None = None,
    tenant_id: str | None = None,
    knowledge_scope: str | None = None,
) -> dict:
    ticket_text = str(user_input or "").strip()

    if upload_info:
        data = {
            "ticket": ticket_text,
            "use_fallback": str(use_fallback).lower(),
            "force_fallback": str(force_fallback).lower(),
            "tenant_id": tenant_id or st.session_state.get("tenant_id", "client_c"),
            "knowledge_scope": knowledge_scope or st.session_state.get("knowledge_scope", "client_private"),
        }
        if confirm_local_matches is not None:
            data["confirm_local_matches"] = str(confirm_local_matches).lower()

        files = {
            "image": (
                upload_info["name"],
                upload_info["bytes"],
                upload_info.get("mime") or "application/octet-stream",
            )
        }

        response = _backend_post(f"{BACKEND_URL}/analyze", data=data, files=files, timeout=REQUEST_TIMEOUT_LONG)
        if not response.ok:
            try:
                detail = response.json().get("detail", response.text[:500])
            except Exception:
                detail = response.text[:500]
            raise Exception(f"[{response.status_code}] {detail}")
        result = response.json()
        if upload_info:
            result["uploaded_image_meta"] = {
                "name": upload_info.get("name"),
                "mime": upload_info.get("mime"),
                "size": upload_info.get("size"),
            }
        return result

    payload = {
        "ticket": ticket_text,
        "use_fallback": use_fallback,
        "force_fallback": force_fallback,
        "tenant_id": tenant_id or st.session_state.get("tenant_id", "client_c"),
        "knowledge_scope": knowledge_scope or st.session_state.get("knowledge_scope", "client_private"),
    }
    if confirm_local_matches is not None:
        payload["confirm_local_matches"] = confirm_local_matches
    response = _backend_post(f"{BACKEND_URL}/analyze", json=payload, timeout=REQUEST_TIMEOUT_LONG)
    if not response.ok:
        try:
            detail = response.json().get("detail", response.text[:500])
        except Exception:
            detail = response.text[:500]
        raise Exception(f"[{response.status_code}] {detail}")
    return response.json()


def normalize_uploaded_file(uploaded_file) -> Dict[str, Any]:
    return {
        "name": uploaded_file.name,
        "mime": getattr(uploaded_file, "type", ""),
        "size": len(uploaded_file.getvalue()),
        "bytes": uploaded_file.getvalue(),
    }


def get_autonomous_gate(classification: str, tenant_id: str) -> dict:
    """Fetch the ledger-based autonomous gate tier for a classification. Never raises."""
    try:
        response = SESSION.get(
            f"{BACKEND_URL}/autonomous_gate",
            params={"classification": classification, "tenant_id": tenant_id},
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        return {"tier": "hold_for_approval", "reason": "Gate check unavailable.", "total_outcomes": 0, "success_rate": None, "confidence_signal": None}


def get_resolution_intelligence(classification: str, tenant_id: str) -> dict:
    """Fetch historical outcome intelligence for a classification. Never raises."""
    try:
        response = SESSION.get(
            f"{BACKEND_URL}/resolution_intelligence",
            params={"classification": classification, "tenant_id": tenant_id},
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        return {"status": "no_history"}


def get_pattern_intelligence(classification: str, tenant_id: str) -> dict:
    """Fetch classification pattern intelligence from resolutions and candidates. Never raises."""
    try:
        response = SESSION.get(
            f"{BACKEND_URL}/pattern_intelligence",
            params={"classification": classification, "tenant_id": tenant_id},
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        return {"status": "no_history"}


def get_fallback_control() -> dict:
    response = SESSION.get(f"{BACKEND_URL}/fallback_control", timeout=REQUEST_TIMEOUT_SHORT)
    response.raise_for_status()
    return response.json()


def set_fallback_control(enabled: bool) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/fallback_control", json={"enabled": enabled}, timeout=REQUEST_TIMEOUT_SHORT)
    response.raise_for_status()
    return response.json()


def submit_feedback(ticket: str, predicted: str, corrected: str, domain: str, request_id: str | None = None) -> dict:
    payload = {
        "ticket": ticket,
        "original_domain": domain,
        "original_classification": system_label(predicted),
        "corrected_classification": system_label(corrected),
        "submitted_domain": domain,
        "is_correct": system_label(corrected) == system_label(predicted),
        "request_id": request_id,
    }
    response = SESSION.post(f"{BACKEND_URL}/feedback", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def review_candidate(payload: dict) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/review_candidate", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def promote_candidate(candidate_id: str) -> dict:
    payload = {
        "candidate_id": candidate_id,
        "tenant_id": st.session_state.get("tenant_id", "client_c"),
    }
    response = SESSION.post(f"{BACKEND_URL}/promote_candidate", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def create_resolution_record(payload: dict) -> dict:
    payload.setdefault("tenant_id", st.session_state.get("tenant_id", "client_c"))
    response = SESSION.post(f"{BACKEND_URL}/create_resolution", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def create_playbook_record(payload: dict) -> dict:
    payload.setdefault("tenant_id", st.session_state.get("tenant_id", "client_c"))
    response = SESSION.post(f"{BACKEND_URL}/create_playbook", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()



def create_automation_candidate(payload: dict) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/automation/candidates", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def list_automation_candidates(status: str | None = None, classification: str | None = None) -> dict:
    params = {}
    if status:
        params["status"] = status
    if classification:
        params["classification"] = classification
    response = SESSION.get(f"{BACKEND_URL}/automation/candidates", params=params, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def update_automation_candidate(payload: dict) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/automation/candidates/update", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def record_automation_candidate_test_result(payload: dict) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/automation/candidates/test_result", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def approve_automation_candidate_request(candidate_id: str, approved_by: str = "Vijay", approval_notes: str = "") -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/candidates/approve",
        json={"candidate_id": candidate_id, "approved_by": approved_by, "approval_notes": approval_notes},
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def enable_automation_candidate_request(candidate_id: str, enabled_by: str = "Vijay") -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/candidates/enable",
        json={"candidate_id": candidate_id, "enabled_by": enabled_by},
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def get_automation_registry_request() -> dict:
    response = SESSION.get(f"{BACKEND_URL}/automation/registry", timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def generate_automation_candidate_playbook(payload: dict) -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/candidates/generate_playbook",
        json=payload,
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def save_automation_candidate_playbook(payload: dict) -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/candidates/save_playbook",
        json=payload,
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def candidate_is_tested(item: dict) -> bool:
    tests = item.get("test_results") if isinstance(item.get("test_results"), list) else []
    return any(str(t.get("test_status", "")).lower() in {"pass", "passed", "success", "completed_live", "completed_simulation"} for t in tests)


def candidate_lifecycle_flags(item: dict) -> dict:
    generated = item.get("generated_playbook") if isinstance(item.get("generated_playbook"), dict) else {}
    review = item.get("playbook_review") if isinstance(item.get("playbook_review"), dict) else {}
    approval = item.get("approval") if isinstance(item.get("approval"), dict) else {}
    status = str(item.get("status", "")).lower()
    return {
        "draft": bool(item.get("candidate_id")),
        "generated": bool(generated.get("generated") or generated.get("content")),
        "saved": bool(generated.get("saved") or review.get("reviewed")),
        "tested": candidate_is_tested(item) or status in {"tested", "approved", "enabled"},
        "approved": bool(approval.get("approved")) or status in {"approved", "enabled"},
        "enabled": bool(item.get("enabled")) or status == "enabled",
    }


def lifecycle_badges(flags: dict) -> str:
    labels = [
        ("draft", "Candidate"),
        ("generated", "Draft YAML"),
        ("saved", "Saved"),
        ("tested", "Tested"),
        ("approved", "Approved"),
        ("enabled", "Registry Enabled"),
    ]
    return "  ".join([f"{'✅' if flags.get(k) else '⬜'} {label}" for k, label in labels])


def get_approved_automation_mapping(classification: str) -> dict:
    """Return the enabled registry mapping for a classification, if available.

    UI-only helper: the backend registry remains the source of truth.
    When this returns a mapping, Incident Console should guide engineers to
    human-in-loop execution instead of creating another runbook/candidate.
    """
    class_key = system_label(classification or "")
    if not class_key:
        return {}
    try:
        resp = get_automation_registry_request()
        items = resp.get("items", {}) if isinstance(resp, dict) else {}
        mapping = items.get(class_key) if isinstance(items, dict) else None
        if isinstance(mapping, dict) and mapping.get("enabled", True):
            return mapping
    except Exception:
        return {}
    return {}


def find_candidate_by_id(candidates: list, candidate_id: str) -> dict:
    cid = str(candidate_id or "").strip()
    if not cid:
        return {}
    for item in candidates or []:
        if str(item.get("candidate_id") or "").strip() == cid:
            return item
    return {}


def matching_candidate_for_incident(candidates: list, incident_id: str) -> dict:
    incident_id = str(incident_id or "").strip()
    if not incident_id:
        return {}
    for item in candidates or []:
        if str(item.get("incident_id") or "").strip() == incident_id and str(item.get("status", "")).lower() != "rejected":
            return item
    return {}


def infer_candidate_metadata_defaults(classification: str, runbook: dict, linked_playbook: dict) -> dict:
    """Infer visible candidate metadata before the backend playbook-draft step runs.

    The backend still remains the source of truth during Generate Draft Playbook /
    Enable Registry Mapping, but the UI should not show blank action/playbook/target
    immediately after Create Candidate.
    """
    class_key = system_label(classification or "incident")
    steps = runbook.get("automation_steps", []) if isinstance(runbook, dict) else []
    text = " ".join([class_key] + [str(x or "") for x in steps]).lower()

    # Keep primary-domain checks before dependency terms such as DNS/VPN.
    if any(term in text for term in ["container", "ecs", "kubernetes", "target group", "port mapping"]):
        action = "validate_container_connectivity"
        playbook = "container_connectivity_check.yml"
        target = "linux-lab-01"
    elif any(term in text for term in ["disk", "temporary", "tmp", "log rotation", "space"]):
        action = "cleanup_linux_temp_files"
        playbook = "linux_disk_cleanup.yml"
        target = "linux-lab-01"
    elif any(term in text for term in ["vpn", "tunnel", "remote access"]):
        action = "recover_vpn_connectivity"
        playbook = "vpn_recover_client_connectivity.yml"
        target = "vpn-lab-01"
    elif any(term in text for term in ["dns", "resolver", "lookup", "coredns"]):
        action = "clear_cache"
        playbook = "clear_cache.yml"
        target = "network-lab-01"
    else:
        action = f"validate_{class_key or 'incident'}"
        playbook = f"{action}.yml"
        target = "linux-lab-01"

    if isinstance(linked_playbook, dict):
        playbook = linked_playbook.get("playbook") or linked_playbook.get("filename") or playbook
        target = linked_playbook.get("target_host") or target
        action = linked_playbook.get("action") or action

    return {"suggested_action": action, "suggested_playbook": playbook, "target_host": target}


def build_candidate_payload_from_runbook(incident_id: str, classification: str, runbook: dict, linked_resolution: dict, linked_playbook: dict, source_notes: str) -> dict:
    candidate_defaults = infer_candidate_metadata_defaults(classification, runbook, linked_playbook)
    return {
        "incident_id": incident_id,
        "classification": classification,
        "suggested_action": candidate_defaults.get("suggested_action", ""),
        "suggested_playbook": candidate_defaults.get("suggested_playbook", ""),
        "target_host": candidate_defaults.get("target_host", ""),
        "draft_runbook": {
            "objective": runbook.get("objective"),
            "safety": runbook.get("safety"),
            "probable_causes": runbook.get("probable_causes", []),
            "validation_checks": runbook.get("validation_checks", []),
            "remediation_steps": runbook.get("remediation_steps", []),
            "automation_steps": runbook.get("automation_steps", []),
            "linked_resolution": linked_resolution,
            "linked_playbook": linked_playbook,
        },
        "draft_steps": runbook.get("automation_steps", []),
        "risk_tier": linked_playbook.get("risk_tier", runbook.get("risk_tier", "medium")),
        "approval_required": True,
        "created_by": "Vijay",
        "source": "ui_draft_from_analysis",
        "notes": source_notes,
    }


def next_improvement_playbook_name(current_playbook: str) -> str:
    """Return a deterministic candidate playbook name for feedback-driven improvements.

    Example: container_connectivity_check.yml -> container_connectivity_check_v2_candidate.yml
    """
    raw = str(current_playbook or "automation_playbook.yml").strip() or "automation_playbook.yml"
    base = raw.rsplit("/", 1)[-1]
    if base.endswith(".yaml"):
        stem = base[:-5]
        ext = ".yml"
    elif base.endswith(".yml"):
        stem = base[:-4]
        ext = ".yml"
    else:
        stem = base
        ext = ".yml"

    match = re.search(r"_v(\d+)(?:_candidate)?$", stem)
    if match:
        version = int(match.group(1)) + 1
        root = re.sub(r"_v\d+(?:_candidate)?$", "", stem)
    else:
        version = 2
        root = stem
    return f"{root}_v{version}_candidate{ext}"


def build_playbook_improvement_candidate_payload(
    incident_id: str,
    classification: str,
    current_action: str,
    current_playbook: str,
    target_host: str,
    risk_tier: str,
    feedback: dict,
    source_candidate: dict | None = None,
) -> dict:
    """Create a draft candidate tied to an existing automation and execution feedback.

    This is intentionally separate from the normal incident-created candidate flow.
    It preserves the approved action/current playbook as lineage and proposes a
    new *_vN_candidate.yml playbook for engineer review.
    """
    feedback = feedback if isinstance(feedback, dict) else {}
    source_candidate = source_candidate if isinstance(source_candidate, dict) else {}
    engineer_notes = str(feedback.get("engineer_notes") or feedback.get("notes") or "").strip()
    improvement_playbook = next_improvement_playbook_name(current_playbook)
    current_action = current_action or source_candidate.get("suggested_action") or "review_existing_automation"

    draft_steps = [
        f"Review execution feedback for existing automation action: {current_action}.",
        f"Start from current approved playbook: {current_playbook or 'unknown'}.",
        f"Create revised candidate playbook: {improvement_playbook}.",
        "Add the engineer-observed validation/remediation steps before any destructive remediation.",
        "Retest in simulation mode before approval and registry enablement.",
    ]
    if engineer_notes:
        draft_steps.append(f"Engineer requested change: {engineer_notes}")

    return {
        "incident_id": incident_id,
        "classification": classification,
        "suggested_action": current_action,
        "suggested_playbook": improvement_playbook,
        "target_host": target_host or source_candidate.get("target_host", ""),
        "target_group": source_candidate.get("target_group", ""),
        "draft_runbook": {
            "objective": "Improve an existing approved automation playbook based on post-execution engineer feedback.",
            "source": "execution_feedback",
            "lineage": {
                "existing_action": current_action,
                "current_playbook": current_playbook,
                "improvement_candidate_playbook": improvement_playbook,
                "source_candidate_id": source_candidate.get("candidate_id", ""),
                "source_feedback_id": feedback.get("feedback_id", ""),
                "queue_id": feedback.get("queue_id", ""),
                "execution_id": feedback.get("execution_id", ""),
            },
            "engineer_feedback": feedback,
            "requested_changes": [engineer_notes] if engineer_notes else [],
            "validation_checks": [
                "Validate that the added engineer feedback is represented as explicit playbook steps.",
                "Confirm the revised playbook remains idempotent and safe for human-approved execution.",
                "Confirm rollback or safe-stop behavior is documented before approval.",
            ],
            "automation_steps": draft_steps,
        },
        "draft_steps": draft_steps,
        "risk_tier": risk_tier or source_candidate.get("risk_tier", "medium") or "medium",
        "approval_required": True,
        "created_by": "Vijay",
        "source": "execution_feedback",
        "notes": (
            f"Playbook improvement candidate created from execution feedback. "
            f"Existing automation: {current_action}; current playbook: {current_playbook}; "
            f"improvement candidate: {improvement_playbook}."
        ),
    }


def queue_automation(payload: dict) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/automation/queue", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def approve_automation_request(queue_id: str, approved_by: str = "Vijay") -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/approve",
        json={"queue_id": queue_id, "approved_by": approved_by},
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def execute_automation_request(
    queue_id: str,
    execution_mode: str = "simulation",
    executed_by: str = "Vijay",
    override_gate_warnings: bool = False,
    override_reason: str = "",
) -> dict:
    payload = {"queue_id": queue_id, "executed_by": executed_by, "execution_mode": execution_mode}
    if override_gate_warnings:
        payload["override_gate_warnings"] = True
        payload["override_reason"] = override_reason
    response = SESSION.post(
        f"{BACKEND_URL}/automation/execute",
        json=payload,
        timeout=REQUEST_TIMEOUT_LONG,
    )
    if response.status_code in (401, 403, 409):
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"Execution refused by the server ({response.status_code}): {detail}")
    response.raise_for_status()
    return response.json()


def run_pre_execution_safety_gate(payload: dict) -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/pre_execution_check",
        json=payload,
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def submit_execution_feedback_request(payload: dict) -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/execution_feedback",
        json=payload,
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def get_automation_confidence_summary_request(payload: dict) -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/confidence_summary",
        json=payload,
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def enable_autonomous_mapping_request(payload: dict) -> dict:
    response = SESSION.post(
        f"{BACKEND_URL}/automation/autonomous/enable",
        json=payload,
        timeout=REQUEST_TIMEOUT_MEDIUM,
    )
    response.raise_for_status()
    return response.json()


def _confidence_summary_payload(classification: str, action: str = "", playbook: str = "", target_host: str = "") -> dict:
    payload = {
        "classification": system_label(classification or ""),
        "action": action or "",
        "playbook": playbook or "",
    }
    if target_host:
        payload["target_host"] = target_host
    return payload


def render_learning_autonomy_panel(summary: dict, identity_payload: dict | None = None, runtime: dict | None = None, runtime_key: str = "") -> None:
    if isinstance(summary, dict) and isinstance(summary.get("confidence_summary"), dict):
        summary = summary.get("confidence_summary", {})
    if not isinstance(summary, dict) or not summary:
        st.info("Automation Confidence is not available yet. Submit execution feedback to calculate it.")
        return

    identity_payload = identity_payload or {}
    runtime = runtime if isinstance(runtime, dict) else {}

    confidence = summary.get("automation_confidence", summary.get("confidence", 0.0))
    try:
        confidence_float = float(confidence or 0.0)
    except Exception:
        confidence_float = 0.0

    ready_enabled = bool(
        summary.get("ready_for_autonomous_checkbox_enabled")
        or summary.get("autonomous_eligible")
        or summary.get("ready_for_autonomous_enabled")
    )
    autonomous_result = runtime.get("autonomous_enable_result", {}) if isinstance(runtime.get("autonomous_enable_result"), dict) else {}
    registry_entry = autonomous_result.get("registry_entry", {}) if isinstance(autonomous_result.get("registry_entry"), dict) else {}
    autonomous_enabled = bool(
        summary.get("autonomous_enabled")
        or summary.get("registry_autonomous_enabled")
        or registry_entry.get("autonomous_enabled")
    )

    if autonomous_enabled:
        maturity_mode = "Autonomous enabled"
    elif ready_enabled:
        maturity_mode = "Ready for autonomous review"
    elif int(summary.get("clean_successful_runs", 0) or 0) > 0:
        maturity_mode = "Learning under human approval"
    else:
        maturity_mode = "Human-in-the-loop"

    st.markdown("#### Learning & Autonomy")
    st.caption(
        "Automation confidence is separate from RCA confidence. "
        "This panel governs whether an approved playbook can move from human-in-the-loop to autonomous execution."
    )

    # Visual confidence meter
    render_confidence_meter(confidence_float, label="Automation Confidence")

    # Clean run tracker — visual progress toward 3 runs
    clean_runs = int(summary.get("clean_successful_runs", summary.get("successful_runs", 0)) or 0)
    clean_target = 3
    clean_pct = min(100, int(clean_runs / clean_target * 100))
    clean_bar_color = "#22c55e" if clean_runs >= clean_target else "#3b82f6"
    st.markdown(
        f"""
        <div style="margin:2px 0 12px 0;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
            <span style="font-size:0.82rem;font-weight:600;color:#6b7280;">Clean Human-Approved Runs</span>
            <span style="font-size:0.88rem;font-weight:700;color:{"#15803d" if clean_runs>=clean_target else "#1d4ed8"};">{clean_runs} of {clean_target}</span>
          </div>
          <div style="background:#e5e7eb;border-radius:6px;height:10px;overflow:hidden;">
            <div style="width:{clean_pct}%;background:{clean_bar_color};height:100%;border-radius:6px;"></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Eligibility checklist
    failed_last3 = int(summary.get("failed_runs_last_3", summary.get("failed_runs_in_last_3", 0)) or 0)
    pending_fb = int(summary.get("pending_feedback", 0) or 0)
    safety_gate_ok = str(summary.get("latest_safety_gate", "unknown")).lower() in {"passed", "pass"}
    rollback_ok = bool(summary.get("rollback_available", False))
    risk_ok = str(summary.get("risk_tier", "medium")).lower() in {"low", "medium"}

    criteria = [
        ("Automation confidence ≥ 0.70", confidence_float >= 0.70),
        ("≥ 3 clean human-approved runs", clean_runs >= clean_target),
        ("No failures in last 3 runs", failed_last3 == 0),
        ("No pending feedback", pending_fb == 0),
        ("Safety Gate passed", safety_gate_ok),
        ("Rollback available", rollback_ok),
        ("Acceptable risk tier", risk_ok),
    ]
    criteria_html = '<div style="display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 14px 0;">'
    for label, met in criteria:
        icon = "✅" if met else "❌"
        bg = "#f0fdf4" if met else "#fff1f2"
        border = "#86efac" if met else "#fca5a5"
        text = "#166534" if met else "#991b1b"
        criteria_html += (
            f'<div style="background:{bg};border:1px solid {border};border-radius:8px;'
            f'padding:5px 11px;font-size:0.78rem;color:{text};font-weight:500;">'
            f'{icon} {label}</div>'
        )
    criteria_html += "</div>"
    st.markdown(criteria_html, unsafe_allow_html=True)

    # Compact metadata row
    rollback_backup_dir = summary.get("rollback_backup_dir") or summary.get("backup_dir") or "./bak"
    s1, s2 = st.columns(2)
    with s1:
        st.write(f"**Mode:** {maturity_mode}")
        st.caption(f"Rollback storage: `{rollback_backup_dir}`")
    with s2:
        st.write(f"**Risk Tier:** {nice_label(str(summary.get('risk_tier', identity_payload.get('risk_tier', 'medium'))))}")
        sg_status = str(summary.get("latest_safety_gate", summary.get("latest_safety_gate_status", "unknown"))).upper()
        st.write(f"**Latest Safety Gate:** {sg_status}")

    if pending_fb > 0:
        st.warning(
            f"**{pending_fb} pending feedback item(s)** — run a clean execution after enabling the updated playbook to clear.",
            icon="⚠️",
        )

    p1, p2 = st.columns(2)
    with p1:
        st.write(f"**Current Approved Playbook:** `{identity_payload.get('playbook') or summary.get('playbook') or 'unknown'}`")
    with p2:
        st.write(f"**Automation Action:** `{identity_payload.get('action') or summary.get('action') or 'unknown'}`")

    check_key = f"ready_for_autonomous_{runtime_key or hash(json.dumps(_json_safe(summary), sort_keys=True))}"
    checked = st.checkbox(
        "Ready for Autonomous",
        value=autonomous_enabled,
        disabled=(not ready_enabled) or autonomous_enabled,
        key=check_key,
        help="Enabled only after confidence >= 0.70, at least 3 clean human-approved runs, no recent failures, no pending feedback, safety gate passed, rollback available, and allowed risk tier. For this lab build, rollback artifacts are stored under the SLM ./bak folder.",
    )

    reasons = summary.get("eligibility_reasons") or summary.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]

    if autonomous_enabled:
        st.success("Autonomous execution is enabled in the registry for this automation. Telemetry/autonomous orchestration may use this mapping, subject to safety checks.")
    elif ready_enabled:
        st.success("Autonomous eligibility criteria are met. Check Ready for Autonomous, then enable it in the registry.")
        if checked:
            if st.button("Enable Autonomous in Registry", key=f"enable_autonomous_registry_{runtime_key}", width="stretch"):
                payload = {
                    "classification": identity_payload.get("classification", ""),
                    "action": identity_payload.get("action", ""),
                    "playbook": identity_payload.get("playbook", ""),
                    "target_host": identity_payload.get("target_host", ""),
                    "enabled": True,
                    "requested_by": "Vijay",
                    "notes": "Enabled from Automation Queue after backend autonomous eligibility checks passed.",
                }
                try:
                    result = enable_autonomous_mapping_request(payload)
                    runtime["autonomous_enable_result"] = result
                    if runtime_key:
                        st.session_state.automation_runtime[runtime_key] = runtime
                    st.success("Autonomous status persisted in the registry. Future telemetry/autonomous execution can only use mappings with autonomous_enabled=true.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Autonomous enablement failed: {e}")
    else:
        if reasons:
            st.warning(
                "**Not eligible for autonomous promotion.** Address the checklist items above.\n\n"
                + "\n".join(f"- {r}" for r in reasons),
                icon="🔒",
            )
        else:
            st.warning("Autonomous is not yet enabled for this automation.")

    with st.expander("Autonomous guardrail note", expanded=False):
        st.info(
            "Telemetry-triggered autonomous execution calls the backend eligibility guard "
            "and is blocked unless the registry mapping has `autonomous_enabled=true`."
        )

    with st.expander("Automation confidence evidence", expanded=False):
        st.json(summary)
    if autonomous_result:
        with st.expander("Autonomous registry evidence", expanded=False):
            st.json(autonomous_result)


def suggest_automation_mapping(payload: dict) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/automation/suggest_mapping", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


def learn_automation_mapping(payload: dict) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/automation/learn_mapping", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()

def refresh_automation_queue_request(queue_id: str) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/automation/refresh_queue", json={"queue_id": queue_id}, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=15, show_spinner=False)
def fetch_all_queue_entries(limit: int = 100) -> list:
    try:
        r = SESSION.get(f"{BACKEND_URL}/automation/queue/list", params={"limit": limit}, timeout=REQUEST_TIMEOUT_MEDIUM)
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception:
        return []



def _json_safe(value: Any) -> Any:
    """Convert values to JSON-safe structures for local UI snapshot persistence."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _clear_stale_last_incident_snapshot() -> None:
    """Remove stale disk snapshot from an older Streamlit server process."""
    try:
        if UI_LAST_INCIDENT_FILE.exists():
            UI_LAST_INCIDENT_FILE.unlink()
    except Exception:
        # Snapshot cleanup must never break the UI.
        pass


def save_last_incident_snapshot() -> None:
    """Persist latest incident context for active top-nav navigation only.

    The snapshot is scoped to UI_RUNTIME_INSTANCE_ID. It is valid while the
    current Streamlit server process is alive, but it is ignored and deleted
    after ui.py / Streamlit is restarted.
    """
    result = st.session_state.get("last_analysis_result")
    if not isinstance(result, dict) or not result.get("analysis"):
        return
    # Keep only the last 10 messages; encode attachment bytes as base64 so image survives refresh
    import base64 as _b64
    raw_messages = st.session_state.get("messages", []) or []
    safe_messages = []
    for m in raw_messages[-10:]:
        if not isinstance(m, dict):
            continue
        sm = {k: v for k, v in m.items() if k != "attachment"}
        att = m.get("attachment")
        if isinstance(att, dict) and att.get("name"):
            safe_att = {k: v for k, v in att.items() if k != "bytes"}
            raw_bytes = att.get("bytes")
            if isinstance(raw_bytes, (bytes, bytearray)):
                safe_att["bytes_b64"] = _b64.b64encode(raw_bytes).decode("ascii")
            sm["attachment"] = safe_att
        safe_messages.append(sm)
    payload = {
        "runtime_instance_id": UI_RUNTIME_INSTANCE_ID,
        "last_analysis_result": result,
        "last_incident_ticket": st.session_state.get("last_incident_ticket", ""),
        "last_incident_context": st.session_state.get("last_incident_context", {}),
        "incident_history": st.session_state.get("incident_history", [])[:12],
        "messages": safe_messages,
    }
    try:
        UI_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        UI_LAST_INCIDENT_FILE.write_text(json.dumps(_json_safe(payload), indent=2), encoding="utf-8")
    except Exception:
        # Do not let UI persistence failure break incident analysis.
        pass


def load_last_incident_snapshot() -> Dict[str, Any]:
    """Load latest incident context only from this active Streamlit runtime.

    A snapshot from a previous Streamlit/ui.py restart is intentionally treated
    as stale so the Incident Console opens fresh after restart.
    """
    try:
        if not UI_LAST_INCIDENT_FILE.exists():
            return {}
        data = json.loads(UI_LAST_INCIDENT_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            _clear_stale_last_incident_snapshot()
            return {}
        if data.get("runtime_instance_id") != UI_RUNTIME_INSTANCE_ID:
            _clear_stale_last_incident_snapshot()
            return {}
        return data
    except Exception:
        return {}


def restore_last_incident_snapshot_if_needed() -> None:
    """Restore last analysis only for active top-nav session continuity."""
    if isinstance(st.session_state.get("last_analysis_result"), dict) and st.session_state.get("last_analysis_result", {}).get("analysis"):
        return
    snapshot = load_last_incident_snapshot()
    result = snapshot.get("last_analysis_result") if isinstance(snapshot, dict) else None
    if not isinstance(result, dict) or not result.get("analysis"):
        return
    st.session_state["last_analysis_result"] = result
    st.session_state["last_incident_ticket"] = snapshot.get("last_incident_ticket", "") or ""
    st.session_state["last_incident_context"] = snapshot.get("last_incident_context", {}) or {}
    history = snapshot.get("incident_history", []) or []
    if isinstance(history, list):
        st.session_state["incident_history"] = history[:12]
    saved_messages = snapshot.get("messages", []) or []
    if isinstance(saved_messages, list) and saved_messages and not st.session_state.get("messages"):
        import base64 as _b64
        for m in saved_messages:
            if not isinstance(m, dict):
                continue
            att = m.get("attachment")
            if isinstance(att, dict) and att.get("bytes_b64") and not att.get("bytes"):
                try:
                    att["bytes"] = _b64.b64decode(att["bytes_b64"])
                except Exception:
                    pass
        st.session_state["messages"] = saved_messages


def get_incident_console_greeting() -> str:
    """Return an engaging empty-state greeting for the Incident Console.

    Behavior:
    - First visit on this local UI runtime profile: "Ready when you are"
    - Second visit: "Welcome back"
    - Later visits: time-aware rotating greetings
    """
    state_file = UI_RUNTIME_DIR / "greeting_state.json"
    state = {}
    try:
        if state_file.exists():
            raw = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = raw
    except Exception:
        state = {}

    # Count only once per active Streamlit browser session/runtime so reruns
    # caused by widgets do not keep advancing the greeting.
    if not st.session_state.get("incident_console_greeting_counted"):
        visits = int(state.get("visits", 0) or 0) + 1
        state["visits"] = visits
        state["last_seen_at"] = datetime.now().isoformat()
        try:
            UI_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass
        st.session_state["incident_console_greeting_counted"] = True
    else:
        visits = int(state.get("visits", 1) or 1)

    if visits <= 1:
        return "Ready when you are"
    if visits == 2:
        return "Welcome back"

    # Lock greeting for this session so reruns (e.g. file-attach preview) don't
    # pick a different random string and show two different greetings on screen.
    if st.session_state.get("_incident_console_greeting_text"):
        return st.session_state["_incident_console_greeting_text"]

    hour = datetime.now().hour
    if 5 <= hour < 12:
        options = [
            "Good morning, I am ready",
            "Good morning, ready when you are",
            "Good morning, how can I help today?",
        ]
    elif 12 <= hour < 17:
        options = [
            "Good afternoon, let me know how I can help you",
            "Good afternoon, I am ready",
            "Good afternoon, send me the incident when you are ready",
        ]
    else:
        options = [
            "Good evening, wake me when you're ready",
            "Good evening, I am ready when you are",
            "Good evening, let me know how I can help",
        ]
    text = random.choice(options)
    st.session_state["_incident_console_greeting_text"] = text
    return text

def init_state():
    defaults = {
        "messages": [],
        "show_debug_trace": False,
        "fallback_runtime_enabled": True,
        "force_fallback_for_next_run": False,
        "workflow_candidate_reviews": {},
        "workflow_resolution_drafts": {},
        "workflow_playbook_drafts": {},
        "workflow_promotions": {},
        "workflow_saved_resolution_ids": {},
        "workflow_saved_playbook_ids": {},
        "workflow_review_status": {},
        "workflow_auto_learn_status": {},
        "incident_input": "",
        "active_page": "Incident Console",
        "automation_runtime": {},
        "automation_execution_mode": "simulation",
        "automation_mapping_runtime": {},
        "automation_candidate_runtime": {},
        "automation_learned_actions_text": "",
        "tenant_id": "client_c",
        "user_role": "l2",        # default: L2 for demo; resolved from DB on login
        "user_display_name": "Engineer",
        "knowledge_scope": "client_private",
        # Persistent incident context used across top-nav page switches.
        # Streamlit reruns the script on every menu click, so the latest
        # analysis must live in session_state instead of only in local vars.
        "last_analysis_result": None,
        "last_incident_ticket": "",
        "last_incident_context": {},
        "incident_history": [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # If top-nav href created a fresh Streamlit session, restore the latest
    # incident from disk so Incident Console and Backtesting remain populated.
    restore_last_incident_snapshot_if_needed()

    try:
        status = get_fallback_control()
        st.session_state.fallback_runtime_enabled = bool(status.get("runtime_enabled", True))
    except Exception:
        pass


def build_incident_context(ticket: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Create a stable cross-page incident context from an /analyze result."""
    result = result if isinstance(result, dict) else {}
    analysis = result.get("analysis", {}) or {}
    confidence = result.get("confidence", {}) or {}
    routing = result.get("routing", {}) or {}
    retrieved = result.get("retrieved_incidents", []) or result.get("similar_incidents", []) or []
    classification = str(analysis.get("classification") or "").strip()
    request_id = str(result.get("request_id") or "").strip()
    label = f"{nice_label(classification or 'Incident')} ({request_id[:8] or 'recent'})"
    return {
        "label": label,
        "ticket": str(ticket or "").strip(),
        "classification": classification,
        "domain": result.get("domain") or analysis.get("domain") or "general",
        "associated_scenario": analysis.get("associated_scenario") or analysis.get("scenario") or "",
        "request_id": request_id,
        "confidence": confidence.get("overall_confidence", analysis.get("confidence", 0.0)),
        "routing_method": routing.get("method", "unknown"),
        "retrieved_count": len(retrieved),
        "result": result,
    }


def persist_last_incident_result(result: Dict[str, Any], ticket: str) -> None:
    """Persist the latest Incident Console result for navigation, Backtesting, and Automation.

    This is intentionally UI-only state. It does not alter backend records; it only
    prevents the current analysis from disappearing when Streamlit reruns after a
    top-menu navigation click.
    """
    if not isinstance(result, dict):
        return
    analysis = result.get("analysis", {}) or {}
    if not analysis.get("classification"):
        return

    context = build_incident_context(ticket, result)
    st.session_state["last_analysis_result"] = result
    st.session_state["last_incident_ticket"] = str(ticket or "").strip()
    st.session_state["last_incident_context"] = context

    history = st.session_state.get("incident_history", []) or []
    request_id = context.get("request_id") or ""
    history = [x for x in history if isinstance(x, dict) and (request_id and x.get("request_id") != request_id or not request_id)]
    history.insert(0, context)
    st.session_state["incident_history"] = history[:12]
    save_last_incident_snapshot()


def collect_persisted_incidents() -> List[Dict[str, Any]]:
    """Return incident contexts from session history plus chat messages, de-duped."""
    incidents: List[Dict[str, Any]] = []
    seen = set()

    def add_context(ctx: Dict[str, Any]) -> None:
        if not isinstance(ctx, dict):
            return
        result = ctx.get("result") if isinstance(ctx.get("result"), dict) else {}
        analysis = result.get("analysis", {}) if isinstance(result, dict) else {}
        classification = str(ctx.get("classification") or analysis.get("classification") or "").strip()
        if not classification:
            return
        request_id = str(ctx.get("request_id") or result.get("request_id") or "").strip()
        key = request_id or f"{classification}:{str(ctx.get('ticket',''))[:80]}"
        if key in seen:
            return
        seen.add(key)
        label = ctx.get("label") or f"{nice_label(classification)} ({request_id[:8] or 'recent'})"
        incidents.append({
            "label": label,
            "classification": classification,
            "request_id": request_id,
            "ticket": ctx.get("ticket", ""),
            "result": result,
        })

    for ctx in st.session_state.get("incident_history", []) or []:
        add_context(ctx)

    last_result = st.session_state.get("last_analysis_result")
    if isinstance(last_result, dict):
        add_context(build_incident_context(st.session_state.get("last_incident_ticket", ""), last_result))

    for msg in reversed(st.session_state.get("messages", []) or []):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        result = msg.get("result", {}) or {}
        if isinstance(result, dict):
            add_context(build_incident_context(msg.get("ticket", ""), result))

    return incidents


def get_latest_analysis_result() -> tuple[Dict[str, Any] | None, str]:
    """Latest analysis for pages that should follow the current incident."""
    latest = st.session_state.get("last_analysis_result")
    latest_ticket = st.session_state.get("last_incident_ticket", "")
    if isinstance(latest, dict) and latest.get("analysis"):
        return latest, latest_ticket
    for msg in reversed(st.session_state.get("messages", []) or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            result = msg.get("result", {}) or {}
            if isinstance(result, dict) and result.get("analysis"):
                return result, msg.get("ticket", "")
    return None, ""


def _sync_active_page() -> str:
    """Read ?page= URL param, validate, update session state. Returns current page name."""
    _VALID = {
        "Incident Console", "Dashboard", "Knowledge Center",
        "Telemetry Console", "Client Onboarding Studio",
        "Automation Queue", "Backtesting", "Backtest Console", "Demo",
        "Drift Management",
    }
    requested = ""
    try:
        requested = st.query_params.get("page", "")
    except Exception:
        try:
            qp = st.experimental_get_query_params()
            requested = qp.get("page", [""])[0]
        except Exception:
            pass
    if isinstance(requested, list):
        requested = requested[0] if requested else ""
    if requested in _VALID:
        st.session_state.active_page = requested
    current = st.session_state.get("active_page", "Incident Console")
    if current not in _VALID:
        current = "Incident Console"
        st.session_state.active_page = current
    return current


def _load_available_tenants() -> List[Dict[str, Any]]:
    """Scan tenant_config/ and return list of {tenant_id, display_name} dicts."""
    config_dir = Path(__file__).parent / "tenant_config"
    tenants = []
    if not config_dir.exists():
        return [{"tenant_id": "client_a", "display_name": "client_a"}]
    for f in sorted(config_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
            tid = data.get("tenant_id", "")
            # Skip template entries
            if not tid or "{{" in tid:
                continue
            # Deduplicate by tenant_id (keep first seen)
            if any(t["tenant_id"] == tid for t in tenants):
                continue
            tenants.append({
                "tenant_id": tid,
                "display_name": data.get("display_name") or tid,
            })
        except Exception:
            pass
    return tenants or [{"tenant_id": "client_a", "display_name": "client_a"}]


def render_sidebar_nav(current_page: str) -> None:
    """Professional dark sidebar: brand, user profile, navigation, status."""
    NAV_ITEMS = [
        ("\U0001f4ca", "Dashboard",          "Dashboard"),
        ("\U0001f6a8", "Incident Console",   "Incident Console"),
        ("⚙️",  "Resolution Queue",  "Automation Queue"),
        ("🔄",  "Drift Management",  "Drift Management"),
        ("\U0001f4da", "Knowledge Library",  "Knowledge Center"),
        ("\U0001f4cb", "Backtesting",        "Backtesting"),
        ("\U0001f52c", "Resolution History", "Backtest Console"),
        ("\U0001f4e1", "Telemetry Console",  "Telemetry Console"),
        ("\U0001f3e2", "Tenant Setup",       "Client Onboarding Studio"),
    ]

    css = """
        <style>
        .main .block-container {
            padding-top: 0rem !important;
            padding-bottom: 1.2rem;
            max-width: 1540px;
        }
        div[data-testid="stChatInput"] {
            max-width: 1060px;
            margin-left: auto;
            margin-right: auto;
        }
        section[data-testid="stSidebar"] > div:first-child {
            background: #0d1b2a !important;
        }
        section[data-testid="stSidebar"] {
            min-width: 260px !important;
            max-width: 260px !important;
        }
        section[data-testid="stSidebar"] .block-container {
            padding-top: 0 !important;
            padding-left: 0 !important;
            padding-right: 0 !important;
            margin-top: 0 !important;
        }
        /* Kill every layer of top spacing Streamlit adds before sidebar content */
        section[data-testid="stSidebar"] > div,
        section[data-testid="stSidebar"] > div > div,
        [data-testid="stSidebarContent"],
        [data-testid="stSidebarContent"] > div,
        [data-testid="stSidebarContent"] > div > div,
        [data-testid="stSidebarUserContent"],
        [data-testid="stSidebarUserContent"] > div {
            padding-top: 0 !important;
            margin-top: 0 !important;
        }
        section[data-testid="stSidebar"] .stMarkdown p {
            color: #94a3b8; font-size: 0.82rem;
        }
        section[data-testid="stSidebar"] details summary {
            color: #94a3b8 !important; font-size: 0.82rem;
        }
        section[data-testid="stSidebar"] .stCheckbox label {
            color: #94a3b8 !important; font-size: 0.80rem;
        }
        section[data-testid="stSidebar"] .stCaption {
            color: #475569 !important; font-size: 0.70rem !important;
        }
        section[data-testid="stSidebar"] .stExpander {
            border-color: rgba(255,255,255,0.08) !important;
            background: transparent !important;
            margin: 4px 8px;
        }
        .sb-brand {
            display: flex; align-items: center; gap: 10px;
            padding: 12px 14px 10px 14px;
            border-bottom: 1px solid rgba(255,255,255,0.07);
        }
        .sb-logo {
            width: 34px; height: 34px; border-radius: 9px;
            background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
            color: #fff; font-weight: 800; font-size: 0.78rem;
            display: flex; align-items: center; justify-content: center;
            letter-spacing: -0.04em; flex-shrink: 0;
            box-shadow: 0 4px 12px rgba(59,130,246,0.35);
        }
        .sb-platform-name {
            color: #ffffff; font-weight: 700; font-size: 0.88rem;
            line-height: 1.15; letter-spacing: -0.01em;
        }
        .sb-platform-sub { color: #64748b; font-size: 0.67rem; margin-top: 2px; }
        .sb-profile {
            display: flex; align-items: center; gap: 10px;
            padding: 8px 14px;
            border-bottom: 1px solid rgba(255,255,255,0.07);
        }
        .sb-avatar {
            width: 30px; height: 30px; border-radius: 50%;
            background: linear-gradient(135deg, #10b981, #059669);
            color: #fff; font-weight: 700; font-size: 0.74rem;
            display: flex; align-items: center; justify-content: center;
            flex-shrink: 0;
        }
        .sb-user-name {
            color: #e2e8f0; font-size: 0.82rem; font-weight: 600; line-height: 1.2;
        }
        .sb-user-role { color: #64748b; font-size: 0.69rem; }
        .sb-section {
            color: #94a3b8; font-size: 0.63rem; font-weight: 700;
            letter-spacing: 0.10em; text-transform: uppercase;
            padding: 6px 16px 4px 16px;
        }
        .sb-nav-item {
            display: flex; align-items: center; gap: 10px;
            padding: 8px 12px; margin: 1px 8px;
            border-radius: 8px; border-left: 3px solid transparent;
            color: #cbd5e1 !important; text-decoration: none !important;
            font-size: 0.81rem; font-weight: 500;
        }
        .sb-nav-item:hover {
            background: rgba(255,255,255,0.05);
            color: #ffffff !important; text-decoration: none !important;
        }
        .sb-nav-item.active {
            background: rgba(59,130,246,0.14);
            color: #93c5fd !important; font-weight: 600;
            border-left-color: #3b82f6;
        }
        .sb-nav-icon { width: 18px; text-align: center; flex-shrink: 0; font-size: 0.90rem; }
        .sb-nav-text  { flex: 1; min-width: 0; }
        .sb-divider {
            border: none; border-top: 1px solid rgba(255,255,255,0.07); margin: 8px 0;
        }
        .sb-status { padding: 8px 16px 10px 16px; }
        .sb-status-row {
            display: flex; align-items: center; gap: 6px;
            font-size: 0.72rem; color: #64748b; margin: 3px 0;
        }
        .sb-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
        .sb-dot-green { background: #10b981; box-shadow: 0 0 4px rgba(16,185,129,0.6); }
        [data-testid="stSidebarContent"] {
            overflow-y: auto !important;
            overflow-x: hidden !important;
        }
        /* Nav buttons — styled to match the dark sidebar */
        section[data-testid="stSidebar"] .stButton > button {
            width: calc(100% - 16px) !important;
            background: transparent !important;
            border: none !important;
            border-left: 3px solid transparent !important;
            border-radius: 8px !important;
            color: #cbd5e1 !important;
            font-size: 0.81rem !important;
            font-weight: 500 !important;
            text-align: left !important;
            justify-content: flex-start !important;
            display: flex !important;
            align-items: center !important;
            padding: 8px 12px !important;
            margin: 1px 8px !important;
            box-shadow: none !important;
            transition: background 0.15s, color 0.15s !important;
        }
        section[data-testid="stSidebar"] .stButton > button *,
        section[data-testid="stSidebar"] .stButton > button p,
        section[data-testid="stSidebar"] .stButton > button span {
            text-align: left !important;
            justify-content: flex-start !important;
            margin: 0 !important;
            width: 100% !important;
        }
        section[data-testid="stSidebar"] .stButton > button:hover {
            background: rgba(255,255,255,0.05) !important;
            color: #ffffff !important;
        }
        section[data-testid="stSidebar"] .sb-btn-active .stButton > button {
            background: rgba(59,130,246,0.14) !important;
            color: #93c5fd !important;
            font-weight: 600 !important;
            border-left-color: #3b82f6 !important;
        }
        section[data-testid="stSidebar"] .stButton {
            margin: 0 !important;
            padding: 0 !important;
        }
        /* Collapse Streamlit's internal block/element wrappers inside sidebar nav */
        section[data-testid="stSidebar"] div[data-testid="stVerticalBlock"] {
            gap: 0 !important;
        }
        section[data-testid="stSidebar"] div[data-testid="stVerticalBlock"] > div {
            margin-bottom: 0 !important;
            padding-bottom: 0 !important;
        }
        section[data-testid="stSidebar"] div[data-testid="stButton"] {
            margin: 0 !important;
            padding: 0 !important;
        }
        section[data-testid="stSidebar"] div[data-testid="element-container"] {
            margin: 0 !important;
            padding: 0 !important;
        }
        /* ── Sidebar brand logo — full-width pill at top of sidebar ── */
        .app-sidebar-logo {
            display: block;
            width: 100%;
            box-sizing: border-box;
            background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
            color: #fff;
            font-weight: 800;
            font-size: 1rem;
            letter-spacing: 0.03em;
            text-align: center;
            padding: 10px 0;
            border-radius: 8px;
            margin-bottom: 8px;
            box-shadow: 0 2px 8px rgba(59,130,246,0.35);
            white-space: nowrap;
        }
        /* Hide Streamlit's built-in faded empty-chat placeholder that duplicates our greeting */
        div[data-testid="stChatInput"] ~ div[class*="empty"],
        div[data-testid="stChatInputContainer"] ~ div,
        .stChatFloatingInputContainer ~ div[class*="placeholder"],
        div[data-baseweb="textarea"] ~ div[class*="empty"] {
            display: none !important;
        }
        /* Streamlit 1.35+ empty-state ghost text above chat input */
        div[class*="stChatInput"] div[class*="empty-state"],
        div[class*="stChatInput"] div[class*="emptyState"],
        div[data-testid="stChatInputContainer"] div[class*="empty"] {
            display: none !important;
        }
        </style>"""

    header_html = (
        '<div class="sb-profile" style="margin-top:8px;">'
        '<div class="sb-avatar">VK</div>'
        "<div>"
        '<div class="sb-user-name">Vijay Kumar</div>'
        '<div class="sb-user-role">SRE Engineer &nbsp;&middot;&nbsp; PYPL</div>'
        "</div></div>"
        '<div class="sb-section">Navigation</div>'
    )
    footer_html = (
        '<hr class="sb-divider"/>'
        '<div class="sb-status">'
        '<div class="sb-status-row"><div class="sb-dot sb-dot-green"></div>Backend API</div>'
        '<div class="sb-status-row"><div class="sb-dot sb-dot-green"></div>Qdrant Vector DB</div>'
        "</div>"
        '<hr class="sb-divider"/>'
    )

    sidebar_logo = (
        '<div class="app-sidebar-logo">AI SRE Platform</div>'
        '<div style="text-align:center;font-size:0.70rem;color:#5a7fa8;'
        'letter-spacing:0.03em;padding:0 12px 10px 12px;line-height:1.4;">'
        'Resolve faster &nbsp;·&nbsp; Learn automatically &nbsp;·&nbsp; Audit everything'
        '</div>'
    )
    st.markdown(css + sidebar_logo + header_html, unsafe_allow_html=True)

    # Signed-in user badge (RBAC Item #16)
    from rbac import current_role as _current_role
    _role_label = {"l1": "L1", "l2": "L2", "l3": "L3", "admin": "Admin"}.get(_current_role(), "?")
    st.caption(f"👤 {st.session_state.get('user_display_name', 'Engineer')} · {_role_label}")

    # Render nav as Streamlit buttons — no href, no page reload, session preserved
    for icon, label, page_value in NAV_ITEMS:
        is_active = page_value == current_page
        btn_label = f"{icon}  {label}"
        # Wrap in a div we can target with CSS for the active state
        if is_active:
            st.markdown('<div class="sb-btn-active">', unsafe_allow_html=True)
        if st.button(btn_label, key=f"nav_{page_value}", use_container_width=True):
            st.session_state.active_page = page_value
            try:
                st.query_params["page"] = page_value
            except Exception:
                try:
                    st.experimental_set_query_params(page=page_value)
                except Exception:
                    pass
            st.rerun()
        if is_active:
            st.markdown('</div>', unsafe_allow_html=True)

    st.markdown(footer_html, unsafe_allow_html=True)

    # ── Tenant switcher ───────────────────────────────────────────────────────
    _tenants = _load_available_tenants()
    if len(_tenants) > 1:
        st.markdown(
            '<div style="padding:4px 8px 2px 8px;color:#64748b;font-size:0.63rem;'
            'font-weight:700;letter-spacing:0.10em;text-transform:uppercase;">Active Tenant</div>',
            unsafe_allow_html=True,
        )
        _tenant_labels = [f"{t['display_name']} ({t['tenant_id']})" for t in _tenants]
        _current_tid = st.session_state.get("tenant_id", "client_c")
        _current_idx = next((i for i, t in enumerate(_tenants) if t["tenant_id"] == _current_tid), 0)
        _selected_label = st.selectbox(
            "Active Tenant",
            options=_tenant_labels,
            index=_current_idx,
            key="sidebar_tenant_switcher",
            label_visibility="collapsed",
        )
        _selected_tid = _tenants[_tenant_labels.index(_selected_label)]["tenant_id"]
        if _selected_tid != st.session_state.get("tenant_id"):
            st.session_state["tenant_id"] = _selected_tid
            st.rerun()
    else:
        st.caption(f"Tenant: {_tenants[0]['display_name']} ({_tenants[0]['tenant_id']})")


def render_top_nav(current_page: str = "") -> None:
    """Slim professional page-header bar. Call after _sync_active_page()."""
    if not current_page:
        current_page = st.session_state.get("active_page", "Incident Console")
    _ICONS: Dict[str, str] = {
        "Incident Console":         "\U0001f6a8",
        "Dashboard":                "\U0001f4ca",
        "Knowledge Center":         "\U0001f4da",
        "Telemetry Console":        "\U0001f4e1",
        "Client Onboarding Studio": "\U0001f3e2",
        "Automation Queue":         "⚙️",
        "Backtesting":              "\U0001f4cb",
        "Backtest Console":         "🔬",
        "Demo":                     "🎬",
    }
    icon = _ICONS.get(current_page, "")
    hdr_css = """
        <style>
        .page-hdr {
            display: flex; align-items: center; justify-content: space-between;
            padding: 12px 0 14px 0;
            margin-top: -3.10rem; margin-bottom: 0.6rem;
            border-bottom: 1px solid #e2e8f0;
        }
        .page-hdr-left  { display: flex; align-items: center; gap: 10px; }
        .page-hdr-icon  { font-size: 1.22rem; line-height: 1; }
        .page-hdr-title {
            font-size: 1.32rem; font-weight: 700;
            color: #0f172a; letter-spacing: -0.025em;
        }
        .page-hdr-right { display: flex; align-items: center; gap: 8px; }
        .page-hdr-badge {
            background: #f1f5f9; color: #64748b; border-radius: 6px;
            font-size: 0.71rem; padding: 3px 10px; font-weight: 600;
            letter-spacing: 0.03em; border: 1px solid #e2e8f0;
        }
        .page-hdr-env {
            background: #dcfce7; color: #166534; border-radius: 6px;
            font-size: 0.71rem; padding: 3px 10px; font-weight: 600;
            letter-spacing: 0.03em; border: 1px solid #bbf7d0;
        }
        </style>"""
    hdr_body = (
        '<div class="page-hdr">'
        '<div class="page-hdr-left">'
        '<span class="page-hdr-icon">' + icon + "</span>"
        '<span class="page-hdr-title">' + current_page + "</span>"
        "</div>"
        '<div class="page-hdr-right">'
        '<span class="page-hdr-badge">PYPL</span>'
        '<span class="page-hdr-env">&#x25cf;&nbsp;Live</span>'
        "</div></div>"
    )
    st.markdown(hdr_css + hdr_body, unsafe_allow_html=True)


def run_analysis_from_submission(submission):
    if submission is None:
        return

    text = ""
    upload_info = None

    if hasattr(submission, "text"):
        text = str(submission.text or "")
        files = list(getattr(submission, "files", []) or [])
        if files:
            upload_info = normalize_uploaded_file(files[0])
    else:
        text = str(submission or "")

    if not text.strip() and not upload_info:
        return

    display_text = text.strip() if text.strip() else "[Image attachment only]"
    user_msg = {"role": "user", "content": display_text}
    if upload_info:
        user_msg["attachment"] = {
            "name": upload_info["name"],
            "mime": upload_info["mime"],
            "size": upload_info["size"],
            "bytes": upload_info["bytes"],
        }
    st.session_state.messages.append(user_msg)

    try:
        result = call_backend_with_text(
            text,
            use_fallback=True,
            force_fallback=bool(st.session_state.force_fallback_for_next_run),
            upload_info=upload_info,
            tenant_id=st.session_state.get("tenant_id", "client_c"),
            knowledge_scope=st.session_state.get("knowledge_scope", "client_private"),
        )
        st.session_state.messages.append({"role": "assistant", "result": result, "ticket": text.strip() or display_text})
        persist_last_incident_result(result, text.strip() or display_text)
    except Exception as e:
        st.session_state.messages.append({"role": "assistant_error", "error": str(e), "ticket": text.strip() or display_text})
def render_key_metrics(result: dict):
    routing = result.get("routing", {})
    analysis = result.get("analysis", {})
    confidence = result.get("confidence", {})
    display_domain = str(result.get("domain", "Unknown") or "Unknown")
    # Trust backend first. Use retrieved incidents only as a safe rendering fallback.
    if display_domain.strip().lower() == "general":
        for item in (result.get("similar_incidents") or result.get("retrieved_incidents") or []):
            item_domain = str(item.get("domain", "") or "").strip()
            if item_domain and item_domain.lower() != "general":
                display_domain = item_domain
                break
    if display_domain.strip().lower() == "general":
        cls = str((result.get("analysis") or {}).get("classification", "") or "").lower()
        if any(x in cls for x in ["vpn", "firewall", "dns", "network"]):
            display_domain = "network"
        elif any(x in cls for x in ["sso", "browser", "login", "identity", "auth", "mfa"]):
            display_domain = "iam"

    domain_label_map = {
        "iam": "IAM",
        "api": "API",
        "vpn": "VPN",
        "dns": "DNS",
        "db": "Database",
        "application_erp": "Application — ERP",
        "application_finance": "Application — Finance",
        "application_saas": "Application — SaaS",
        "application_integration": "Application — Integration",
        "application_iam": "Application — IAM",
        "application_cloud": "Application — Cloud",
        "application_data": "Application — Data",
        "application_network": "Application — Network",
        "application_general": "Application — General",
        "sap_batch_job_failure": "SAP Batch Job",
    }
    display_domain_clean = str(display_domain).strip().lower()
    shown_domain = domain_label_map.get(display_domain_clean, str(display_domain).title())

    _conf_raw = confidence.get("overall_confidence", analysis.get("confidence", 0.0)) if isinstance(confidence, dict) else (confidence if isinstance(confidence, (int, float)) else 0.0)
    _conf_val = format_confidence(_conf_raw, 2)
    _cls_val = nice_label(analysis.get("classification", "unknown"))
    _route_val = routing_display_label(routing.get("method", "unknown"))
    st.markdown(
        f"""<div style="display:flex;border:1px solid #bfdbfe;border-left:4px solid #3b82f6;border-radius:8px;overflow:hidden;margin:6px 0 12px;background:#f0f7ff;">
          <div style="flex:1;padding:8px 14px;border-right:1px solid #bfdbfe;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#93c5fd;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Domain</div>
            <div style="font-size:0.9rem;font-weight:700;color:#1e3a5f;">{shown_domain}</div>
          </div>
          <div style="flex:2;padding:8px 14px;border-right:1px solid #bfdbfe;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#93c5fd;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Classification</div>
            <div style="font-size:0.9rem;font-weight:700;color:#1e3a5f;">{_cls_val}</div>
          </div>
          <div style="flex:1;padding:8px 14px;border-right:1px solid #bfdbfe;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#93c5fd;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Routing</div>
            <div style="font-size:0.9rem;font-weight:700;color:#1e3a5f;">{_route_val}</div>
          </div>
          <div style="flex:1;padding:8px 14px;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#93c5fd;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Confidence</div>
            <div style="font-size:0.9rem;font-weight:700;color:#0f766e;">{_conf_val}</div>
          </div>
        </div>""",
        unsafe_allow_html=True,
    )


def render_status_banners(result: dict):
    analysis = result.get("analysis", {})
    routing = result.get("routing", {})
    fallback_used = bool(result.get("fallback_used", False))
    tenant_context = result.get("tenant_context", {}) or {}
    analysis_source_label = result.get("analysis_source_label", analysis.get("analysis_source_label", "Unknown"))

    # Map raw label to display text
    source_text = analysis_source_label
    if source_text in {"Local SLM", "RAG + SLM"}:
        source_text = "RAG + SLM"
    elif "LLM Fallback" in source_text:
        source_text = "LLM Fallback"

    # Two distinct confidence values:
    #   primary_conf  = RAG retrieval score (what triggered / didn't trigger fallback)
    #   fallback_conf = LLM's own self-reported confidence (only present when fallback ran)
    conf_block      = result.get("confidence", {})
    primary_conf    = float(conf_block.get("primary_confidence", conf_block.get("overall_confidence", 0.0)) or 0.0)
    fallback_conf   = float(conf_block.get("fallback_confidence", 0.0) or 0.0)
    overall_conf    = float(conf_block.get("overall_confidence", 0.0) or 0.0)

    fallback_reason = result.get("routing", {}).get("fallback_reason", "")
    grounding_count = int(result.get("grounding_incident_count", 0) or 0)

    if fallback_reason == "zero_retrieval_no_grounding":
        source_text = "No data — escalated (zero RAG hits, LLM not invoked)"
    elif not fallback_used and source_text == "RAG + SLM":
        if overall_conf >= 0.70:
            source_text += f" · high confidence ({overall_conf:.0%})"
        elif overall_conf >= 0.40:
            source_text += f" · moderate confidence ({overall_conf:.0%})"
        else:
            source_text += f" · low confidence ({overall_conf:.0%})"
    elif fallback_used:
        rag_pct = f"{primary_conf:.0%}" if primary_conf else "—"
        llm_pct = f"{fallback_conf:.0%}" if fallback_conf else f"{overall_conf:.0%}"
        if grounding_count > 0:
            source_text += f" · RAG {rag_pct} → LLM {llm_pct} (grounded on {grounding_count} incidents)"
        else:
            source_text += f" · RAG {rag_pct} → LLM {llm_pct}"

    # Inline meta strip — compact, single line
    decision = decision_display_label(routing.get("decision", "unknown"))
    tenant_id = (tenant_context.get("tenant_id") if tenant_context else None) or st.session_state.get("tenant_id", "client_c")
    if fallback_used:
        source_html = f'<span style="color:#2ecc71;font-weight:600;">🤖 Source: {source_text}</span>'
        st.markdown(
            f'<p style="font-size:0.8rem;color:var(--text-color,#888);">'
            f'{source_html}'
            f'&nbsp;&nbsp;·&nbsp;&nbsp;Decision: {decision}&nbsp;&nbsp;·&nbsp;&nbsp;Tenant: {tenant_id}'
            f'</p>',
            unsafe_allow_html=True,
        )
    else:
        st.caption(f"Source: {source_text}  ·  Decision: {decision}  ·  Tenant: {tenant_id}")

    if fallback_reason == "zero_retrieval_no_grounding":
        st.error("No historical incidents found for this pattern — LLM was not invoked. Escalate to engineer and ingest real incident data from ServiceNow first.")
    elif fallback_used and grounding_count > 0:
        st.warning(f"LLM fallback used — grounded on {grounding_count} nearest incidents from your environment (cross-classification). Results are approximate; require stricter engineer review before promoting.")
    elif fallback_used:
        st.warning("LLM fallback was used — RAG confidence was below threshold. Results are LLM-generated; review carefully before promoting.")
    if analysis.get("escalate"):
        st.info("This incident is flagged for escalation or manual review.")


def render_agent_timeline(result: dict) -> None:
    """Render the agent reasoning timeline when the agentic orchestrator was used.
    Silent no-op when trace_spans are absent (legacy pipeline path).
    """
    import html as _html
    spans = result.get("trace_spans") or []
    if not spans:
        return

    STATUS_COLOR = {"ok": "#16a34a", "error": "#dc2626", "skipped": "#94a3b8"}
    AGENT_ICON = {
        "orchestrator": "🎯",
        "triage":       "🔍",
        "merge":        "🔗",
        "retrieval":    "📡",
        "safety":       "🛡️",
        "rca":          "🧠",
        "resolution":   "📋",
        "learning":     "💾",
    }

    rows = ""
    for sp in spans:
        agent  = sp.get("agent", "")
        intent = sp.get("intent", "")
        dur    = sp.get("duration_ms", 0)
        out    = _html.escape(str(sp.get("output_summary", ""))[:120])
        dec    = _html.escape(str(sp.get("decision", ""))[:60])
        status = sp.get("status", "ok")
        icon   = AGENT_ICON.get(agent, "⚙️")
        color  = STATUS_COLOR.get(status, "#94a3b8")
        rows += (
            f"<tr>"
            f"<td style='padding:4px 8px;font-size:.72rem;white-space:nowrap'>{icon} <b>{_html.escape(agent)}</b></td>"
            f"<td style='padding:4px 8px;font-size:.68rem;color:#64748b'>{_html.escape(intent)}</td>"
            f"<td style='padding:4px 8px;font-size:.68rem;text-align:right;white-space:nowrap'>{dur:.0f} ms</td>"
            f"<td style='padding:4px 8px;font-size:.68rem'>{out}</td>"
            f"<td style='padding:4px 8px;font-size:.68rem;color:{color};white-space:nowrap'>{dec}</td>"
            f"</tr>"
        )

    card = (
        "<div style='border:1px solid #e2e8f0;border-left:4px solid #7c3aed;"
        "border-radius:8px;padding:10px 16px;margin:8px 0 12px;"
        "background:var(--background-color,#fff);'>"
        "<div style='font-size:.7rem;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.07em;color:#7c3aed;margin-bottom:8px;'>🤖 Agent Reasoning Timeline</div>"
        "<div style='overflow-x:auto'>"
        "<table style='width:100%;border-collapse:collapse'>"
        "<thead><tr style='border-bottom:1px solid #e2e8f0'>"
        "<th style='padding:3px 8px;font-size:.65rem;color:#64748b;text-align:left'>Agent</th>"
        "<th style='padding:3px 8px;font-size:.65rem;color:#64748b;text-align:left'>Intent</th>"
        "<th style='padding:3px 8px;font-size:.65rem;color:#64748b;text-align:right'>Duration</th>"
        "<th style='padding:3px 8px;font-size:.65rem;color:#64748b;text-align:left'>Output</th>"
        "<th style='padding:3px 8px;font-size:.65rem;color:#64748b;text-align:left'>Decision</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table></div></div>"
    )
    st.markdown(card, unsafe_allow_html=True)


def render_pattern_intelligence_card(classification: str, tenant_id: str) -> None:
    """Fetch and render the Classification Pattern Intelligence card inline.

    Shows what the system has learned about this classification from all resolutions
    and candidates — not just ledger outcomes. Silent no-op when there is no history
    or the backend call fails.
    """
    if not classification:
        return
    data = get_pattern_intelligence(classification, tenant_id)
    if data.get("status") != "ok":
        return
    intel = data.get("intelligence", {})

    total_res    = intel.get("total_resolutions", 0)
    cand_count   = intel.get("candidate_count", 0)
    causes       = intel.get("dominant_root_causes", [])
    actions      = intel.get("dominant_actions", [])
    trend        = intel.get("recurrence_trend", "new")
    confidence   = intel.get("pattern_confidence", "low")
    insight      = intel.get("insight", "")

    conf_color  = {"high": "#2563eb", "moderate": "#7c3aed", "low": "#64748b"}.get(confidence, "#64748b")
    conf_label  = {"high": "High confidence", "moderate": "Moderate confidence", "low": "Low confidence"}.get(confidence, confidence.title())
    trend_color = {"increasing": "#dc2626", "stable": "#16a34a", "new": "#64748b"}.get(trend, "#64748b")
    trend_label = {"increasing": "Recurring (increasing)", "stable": "Recurring (stable)", "new": "First occurrence"}.get(trend, trend.title())

    import html as _html

    def _pills(items: list) -> str:
        if not items:
            return '<span style="color:#94a3b8;font-size:0.72rem;">—</span>'
        return "".join(
            f'<span style="background:#dbeafe;color:#1e40af;border:1px solid #93c5fd;border-radius:999px;'
            f'padding:2px 10px;font-size:0.70rem;margin-right:4px;margin-bottom:4px;'
            f'display:inline-block;white-space:nowrap;font-weight:600;">'
            f'{_html.escape(item[:60])}</span>'
            for item in items
        )

    st.markdown(
        f"""<div style="border:1px solid #e2e8f0;border-left:4px solid {conf_color};border-radius:8px;
                        padding:10px 16px;margin:8px 0 12px;background:var(--background-color,#fff);">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
            <span style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748b;">
              Classification Pattern Intelligence
            </span>
            <div style="display:flex;gap:6px;align-items:center;">
              <span style="font-size:0.7rem;font-weight:700;color:{trend_color};background:{trend_color}18;
                           padding:2px 8px;border-radius:999px;">{trend_label}</span>
              <span style="font-size:0.7rem;font-weight:700;color:{conf_color};background:{conf_color}18;
                           padding:2px 8px;border-radius:999px;">{conf_label}</span>
            </div>
          </div>
          <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;">
            <div style="min-width:90px;">
              <div style="font-size:0.65rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;">Resolutions</div>
              <div style="font-size:1.1rem;font-weight:700;color:#1e293b;">{total_res}</div>
            </div>
            <div style="min-width:90px;">
              <div style="font-size:0.65rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;">Candidates</div>
              <div style="font-size:1.1rem;font-weight:700;color:#1e293b;">{cand_count}</div>
            </div>
          </div>
          <div style="margin-bottom:6px;">
            <div style="font-size:0.65rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px;">Dominant Root Causes</div>
            <div>{_pills(causes)}</div>
          </div>
          <div style="margin-bottom:6px;">
            <div style="font-size:0.65rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px;">Dominant Actions</div>
            <div>{_pills(actions)}</div>
          </div>
          <div style="font-size:0.72rem;color:#475569;border-top:1px solid #f1f5f9;padding-top:6px;margin-top:4px;">
            {_html.escape(insight)}
          </div>
        </div>""",
        unsafe_allow_html=True,
    )


def render_autonomous_gate_badge(classification: str, tenant_id: str) -> None:
    """Fetch and render the autonomous gate tier badge inline.

    Shown only when there is a linked resolution with automation_ready.
    Silent no-op on failure.
    """
    if not classification:
        return
    gate = get_autonomous_gate(classification, tenant_id)
    tier = gate.get("tier", "hold_for_approval")
    reason = gate.get("reason", "")
    total = gate.get("total_outcomes", 0)
    success = gate.get("success_rate")
    success_str = f"{success:.0%}" if success is not None else "—"

    _tier_cfg = {
        "auto_execute":       ("#16a34a", "#dcfce7", "Auto-execute eligible"),
        "auto_execute_watch": ("#d97706", "#fef3c7", "Watch mode"),
        "hold_for_approval":  ("#dc2626", "#fee2e2", "Hold for approval"),
    }
    color, bg, label = _tier_cfg.get(tier, ("#6b7280", "#f3f4f6", tier.replace("_", " ").title()))

    st.markdown(
        f"""<div style="display:flex;align-items:center;gap:10px;padding:7px 12px;
                        border:1px solid {color}30;border-radius:8px;background:{bg};margin:6px 0 10px;">
          <span style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748b;">
            Autonomous Gate
          </span>
          <span style="font-size:0.75rem;font-weight:700;color:{color};background:{color}18;
                       padding:2px 10px;border-radius:999px;border:1px solid {color}40;">
            {label}
          </span>
          <span style="font-size:0.72rem;color:#64748b;">
            {total} outcome(s) · {success_str} success · {reason}
          </span>
        </div>""",
        unsafe_allow_html=True,
    )


def render_historical_outcome_evidence(classification: str, tenant_id: str) -> None:
    """Fetch and render the Historical Outcome Evidence card inline.

    Shows for all confidence signals including 'insufficient' (1–2 outcomes).
    Silent no-op when there is no history or the backend call fails.
    """
    if not classification:
        return
    intel = get_resolution_intelligence(classification, tenant_id)
    if intel.get("status") != "ok":
        return
    s = intel.get("summary", {})
    total      = s.get("total_outcomes", 0)
    success    = s.get("success_rate", 0.0)
    recur      = s.get("recurrence_rate", 0.0)
    ai_used    = s.get("ai_used_rate", 0.0)
    avg_min    = s.get("avg_resolution_minutes")
    last_res   = s.get("last_resolved_at", "")
    signal     = s.get("confidence_signal", "insufficient")
    dom_mode   = s.get("dominant_execution_mode", "manual")

    signal_color = {"strong": "#16a34a", "mixed": "#d97706", "insufficient": "#6b7280"}.get(signal, "#6b7280")
    signal_label = {"strong": "Strong pattern", "mixed": "Mixed results", "insufficient": "Insufficient history"}.get(signal, signal.title())

    avg_min_str = f"{avg_min:.0f} min" if avg_min is not None else "—"
    last_str = last_res[:10] if last_res else "—"

    st.markdown(
        f"""<div style="border:1px solid #e2e8f0;border-left:4px solid {signal_color};border-radius:8px;
                        padding:10px 16px;margin:8px 0 12px;background:var(--background-color,#fff);">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
            <span style="font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748b;">
              Historical Outcome Evidence
            </span>
            <span style="font-size:0.7rem;font-weight:700;color:{signal_color};background:{signal_color}18;
                         padding:2px 8px;border-radius:999px;">
              {signal_label}
            </span>
          </div>
          <div style="display:flex;gap:24px;flex-wrap:wrap;">
            <div><span style="font-size:0.65rem;color:#94a3b8;">Resolutions</span><br>
                 <span style="font-size:0.95rem;font-weight:700;">{total}</span></div>
            <div><span style="font-size:0.65rem;color:#94a3b8;">Success rate</span><br>
                 <span style="font-size:0.95rem;font-weight:700;">{success:.0%}</span></div>
            <div><span style="font-size:0.65rem;color:#94a3b8;">Recurrence</span><br>
                 <span style="font-size:0.95rem;font-weight:700;">{recur:.0%}</span></div>
            <div><span style="font-size:0.65rem;color:#94a3b8;">AI recommendation used</span><br>
                 <span style="font-size:0.95rem;font-weight:700;">{ai_used:.0%}</span></div>
            <div><span style="font-size:0.65rem;color:#94a3b8;">Avg resolution time</span><br>
                 <span style="font-size:0.95rem;font-weight:700;">{avg_min_str}</span></div>
            <div><span style="font-size:0.65rem;color:#94a3b8;">Dominant mode</span><br>
                 <span style="font-size:0.95rem;font-weight:700;">{dom_mode.replace("_"," ").title()}</span></div>
            <div><span style="font-size:0.65rem;color:#94a3b8;">Last resolved</span><br>
                 <span style="font-size:0.95rem;font-weight:700;">{last_str}</span></div>
          </div>
        </div>""",
        unsafe_allow_html=True,
    )


def render_condensed_confidence_panel(result: dict):
    confidence = result.get("confidence", {})
    section_title("Confidence")
    if confidence:
        st.write(f"Overall Confidence: **{format_confidence(confidence.get('overall_confidence', 0.0), 2)}**")
        st.write(f"Retrieval Quality: {format_confidence(confidence.get('retrieval_quality', 0.0), 2)}")
        st.write(f"Classification Certainty: {format_confidence(confidence.get('classification_certainty', 0.0), 2)}")
    else:
        st.caption("No confidence details available.")
    with st.expander("Technical Confidence Breakdown", expanded=False):
        if confidence:
            st.write(f"Primary Confidence: {format_confidence(confidence.get('primary_confidence', 0.0), 4)}")
            st.write(f"Fallback Confidence: {format_confidence(confidence.get('fallback_confidence', 0.0), 4)}")
            st.write(f"Embedding Score: {format_confidence(confidence.get('embedding_score', 0.0), 4)}")
            st.write(f"LLM Score: {format_confidence(confidence.get('llm_score', 0.0), 4)}")
            st.write(f"Learned Feedback Score: {format_confidence(confidence.get('learned_feedback_score', 0.0), 4)}")



RUNBOOK_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "ad_account_lockout_spike": {
        "objective": "Identify the lockout source, remove stale credentials, and prevent recurring AD account lockouts.",
        "safety": "Do not unlock/reset broad service accounts until the lockout source and dependent process are identified.",
        "triage": [
            "Confirm impacted account(s), business service, and lockout frequency.",
            "Identify source host/device from domain controller security events.",
            "Check whether the issue started after password reset, policy change, or service migration.",
        ],
        "validation": [
            "Review AD lockout events on domain controllers around the incident window.",
            "Inspect saved credentials on the source host, service, scheduled task, mapped drive, mobile client, and application pool.",
            "Validate whether a legacy service account or scheduled task is still using the old password.",
            "Check recent password changes and dependent services using the same credential.",
        ],
        "remediation": [
            "Clear stale cached credentials on the source host/device.",
            "Update scheduled task, Windows service, application pool, or integration credential using the old password.",
            "Reset/unlock the account only after the lockout source is corrected.",
            "Monitor domain-controller lockout events for recurrence after remediation.",
        ],
        "automation_steps": [
            "Collect AD lockout event evidence for affected account.",
            "Correlate lockout source host with recent password/service changes.",
            "Check scheduled tasks and services on the source host for stale credentials.",
            "Prepare credential update/unlock actions for engineer approval.",
            "Monitor recurrence for a fixed validation window.",
        ],
        "risk_tier": "medium",
    },
    "api_gateway_timeout": {
        "objective": "Validate gateway-to-downstream connectivity and restore API availability safely.",
        "safety": "Validate downstream health and firewall/route changes before rollback or restart.",
        "triage": [
            "Confirm API route, impacted consumers, error code, and start time.",
            "Check whether downstream service health is green while gateway calls fail.",
            "Identify recent firewall, security-group, route, gateway, or deployment changes.",
        ],
        "validation": [
            "Test source-to-destination port reachability from gateway to downstream service.",
            "Review gateway logs for 504/timeout and upstream connection errors.",
            "Compare firewall/security-group deny logs against the incident window.",
            "Validate topology dependency path and downstream port configuration.",
        ],
        "remediation": [
            "Restore or correct downstream port access after approval.",
            "Correct gateway route or upstream target configuration if mismatched.",
            "Rollback the firewall/security-group rule only if deny evidence confirms causality.",
            "Validate recovery with synthetic API transaction and monitor error rate.",
        ],
        "automation_steps": [
            "Collect gateway timeout and upstream connection evidence.",
            "Run downstream port reachability validation.",
            "Compare firewall/security-group changes with deny logs.",
            "Prepare approved rule correction or gateway route remediation.",
            "Run synthetic transaction validation after change.",
        ],
        "risk_tier": "medium",
    },
    "batch_job_failure": {
        "objective": "Validate batch dependency chain, isolate failed/locked job, and safely rerun or resubmit.",
        "safety": "Do not rerun a batch job until upstream dependencies, duplicate run risk, and business cut-off windows are validated.",
        "triage": [
            "Confirm job name, scheduler stream, failure/abend code, and business window.",
            "Check predecessor job completion and upstream file/dataset availability.",
            "Identify whether a prior instance, lock, or dependency is still active.",
        ],
        "validation": [
            "Inspect scheduler logs, job history, and return/abend code.",
            "Validate dependency chain and expected file/dataset arrival time.",
            "Check lock table, stale job markers, and overlapping job instance state.",
            "Confirm application owner approval and rerun policy.",
        ],
        "remediation": [
            "Clear stale lock only after owner approval and duplicate-run validation.",
            "Rerun failed dependency or resubmit the impacted job after validation.",
            "Escalate to batch operations/application owner if dependency remains unresolved.",
            "Monitor downstream job completion and business SLA recovery.",
        ],
        "automation_steps": [
            "Collect scheduler and job failure evidence.",
            "Validate predecessor dependency and file/dataset availability.",
            "Check stale locks or duplicate running instances.",
            "Prepare rerun/resubmit action for approval.",
            "Monitor downstream completion and SLA recovery.",
        ],
        "risk_tier": "medium",
    },
    "cpu_saturation": {
        "objective": "Identify high-CPU process, validate operational cause, and remediate without blind restart.",
        "safety": "Restart/kill process only after confirming owner, impact, and approved remediation path.",
        "triage": [
            "Confirm affected host/service, CPU duration, and business impact.",
            "Identify top CPU consuming process during the incident window.",
            "Check whether scheduled batch, scan, deployment, or traffic spike coincided.",
        ],
        "validation": [
            "Review process history, host metrics, and application logs.",
            "Validate recent scheduled jobs, scans, or deployments.",
            "Check service health and capacity headroom.",
        ],
        "remediation": [
            "Throttle/reschedule heavy job or scan where confirmed.",
            "Restart affected service only after approval and dependency check.",
            "Scale capacity if saturation is workload-driven.",
            "Monitor CPU and service error rates after remediation.",
        ],
        "automation_steps": [
            "Collect top-process and CPU evidence.",
            "Correlate CPU spike with jobs/scans/deployments.",
            "Prepare approved restart/throttle/scale action.",
            "Validate service recovery and CPU normalization.",
        ],
        "risk_tier": "medium",
    },
}


def get_result_analysis(result: Dict[str, Any]) -> Dict[str, Any]:
    return result.get("analysis", {}) if isinstance(result, dict) else {}


def get_result_classification(result: Dict[str, Any]) -> str:
    analysis = get_result_analysis(result)
    return system_label(analysis.get("classification") or result.get("classification") or "incident")


def get_runbook_template(classification: str) -> Dict[str, Any]:
    return RUNBOOK_TEMPLATES.get(system_label(classification), {})


def normalize_runbook_items(values: List[Any], fallback_values: List[str] | None = None, limit: int = 8) -> List[str]:
    cleaned = dedupe_lines([natural_label(v) for v in (values or []) if str(v or "").strip()])
    if not cleaned and fallback_values:
        cleaned = dedupe_lines([natural_label(v) for v in fallback_values if str(v or "").strip()])
    return cleaned[:limit]


def build_engineer_runbook(result: Dict[str, Any], ticket: str = "") -> Dict[str, Any]:
    """Build an engineer-ready runbook from the current analysis, even before a formal playbook is saved."""
    analysis = get_result_analysis(result)
    classification = get_result_classification(result)
    template = get_runbook_template(classification)
    linked_playbook = result.get("linked_playbook") or {}
    linked_resolution = result.get("linked_resolution") or {}

    probable_causes = normalize_runbook_items(analysis.get("probable_causes", []), template.get("triage", []), 6)
    next_checks = normalize_runbook_items(analysis.get("next_checks", []), template.get("validation", []), 8)
    recommended_actions = normalize_runbook_items(analysis.get("recommended_actions", []), template.get("remediation", []), 8)
    automation_steps = normalize_runbook_items(
        linked_playbook.get("steps") or template.get("automation_steps") or (next_checks + recommended_actions),
        next_checks + recommended_actions,
        10,
    )

    objective = linked_playbook.get("description") or template.get("objective") or f"Validate, isolate, and remediate {nice_label(classification).lower()} safely."
    safety = template.get("safety") or "Validate evidence and obtain approval before executing any state-changing remediation."
    root_cause = linked_resolution.get("root_cause") or analysis.get("root_cause") or (probable_causes[0] if probable_causes else "Root cause requires validation from evidence.")

    return {
        "classification": classification,
        "domain": result.get("domain") or analysis.get("domain") or "general",
        "objective": objective,
        "safety": safety,
        "root_cause": root_cause,
        "probable_causes": probable_causes,
        "validation_checks": next_checks,
        "remediation_steps": recommended_actions,
        "automation_steps": automation_steps,
        "risk_tier": linked_playbook.get("risk_tier") or template.get("risk_tier") or "medium",
        "approval_mode": linked_playbook.get("approval_mode") or "manual_approval_required",
        "rollback_available": bool(linked_playbook.get("rollback_available", True)),
        "source": "linked_playbook" if linked_playbook else "ui_draft_from_analysis",
    }


def build_draft_resolution_and_playbook(result: Dict[str, Any], ticket: str = "") -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return backend-linked artifacts when present; otherwise draft UI artifacts from the current runbook."""
    analysis = get_result_analysis(result)
    classification = get_result_classification(result)
    runbook = build_engineer_runbook(result, ticket)
    request_id = str(result.get("request_id") or "recent").replace("-", "")[:12]

    linked_resolution = result.get("linked_resolution") or {}
    linked_playbook = result.get("linked_playbook") or {}

    if not linked_resolution:
        linked_resolution = {
            "resolution_id": f"draft_res_{system_label(classification)}_{request_id}",
            "classification": classification,
            "root_cause": runbook["root_cause"],
            "validated_checks": runbook["validation_checks"],
            "validated_actions": runbook["remediation_steps"],
            "confirmed_by": "engineer_review_pending",
            "success_notes": "Drafted from current incident analysis. Engineer approval required before operational use.",
            "linked_candidate_id": result.get("request_id") or f"adhoc_{classification}",
            "automation_ready": False,
            "source": "ui_draft_from_analysis",
        }

    if not linked_playbook:
        linked_playbook = {
            "playbook_id": f"draft_pb_{system_label(classification)}_{request_id}",
            "name": f"{nice_label(classification)} Investigation and Recovery",
            "classification": classification,
            "description": runbook["objective"],
            "steps": runbook["automation_steps"],
            "scripts": [],
            "approval_mode": runbook["approval_mode"],
            "rollback_available": runbook["rollback_available"],
            "risk_tier": runbook["risk_tier"],
            "source": "ui_draft_from_analysis",
            "engineer_review_required": True,
        }

    return linked_resolution, linked_playbook


def get_automation_runtime_key(result: Dict[str, Any], classification: str) -> str:
    return str(result.get("request_id") or f"adhoc_{system_label(classification or 'incident')}")


def queue_draft_automation_from_result(result: Dict[str, Any], ticket: str, requested_by: str = "Vijay") -> Dict[str, Any]:
    classification = get_result_classification(result)
    incident_id = str(result.get("request_id") or f"adhoc_{classification}")
    linked_resolution, linked_playbook = build_draft_resolution_and_playbook(result, ticket)
    return queue_automation({
        "incident_id": incident_id,
        "classification": classification,
        "linked_resolution": linked_resolution,
        "linked_playbook": linked_playbook,
        "requested_by": requested_by,
    })



def create_automation_candidate_from_result(result: Dict[str, Any], ticket: str, created_by: str = "Vijay") -> Dict[str, Any]:
    classification = get_result_classification(result)
    incident_id = str(result.get("request_id") or f"adhoc_{classification}")
    runbook = build_engineer_runbook(result, ticket)
    linked_resolution, linked_playbook = build_draft_resolution_and_playbook(result, ticket)
    _drift_type = str(result.get("drift_type") or "").strip() or None
    return create_automation_candidate({
        "incident_id": incident_id,
        "classification": classification,
        "drift_type": _drift_type,
        "draft_runbook": {
            "objective": runbook.get("objective"),
            "safety": runbook.get("safety"),
            "probable_causes": runbook.get("probable_causes", []),
            "validation_checks": runbook.get("validation_checks", []),
            "remediation_steps": runbook.get("remediation_steps", []),
            "automation_steps": runbook.get("automation_steps", []),
            "linked_resolution": linked_resolution,
            "linked_playbook": linked_playbook,
        },
        "draft_steps": runbook.get("automation_steps", []),
        "risk_tier": runbook.get("risk_tier", "medium"),
        "approval_required": True,
        "created_by": created_by,
        "source": "ui_draft_from_analysis",
        "notes": "Created from Incident Console draft automation package.",
    })


def render_engineer_runbook_panel(result: Dict[str, Any], ticket: str, key_suffix: str):
    analysis = get_result_analysis(result)
    if not analysis:
        return

    runbook = build_engineer_runbook(result, ticket)
    linked_resolution, linked_playbook = build_draft_resolution_and_playbook(result, ticket)
    classification = runbook["classification"]
    # Stable incident identifier used for duplicate-candidate detection.
    # This must be defined before matching_candidate_for_incident() is called.
    incident_id = str(result.get("request_id") or f"adhoc_{classification}")
    runtime_key = get_automation_runtime_key(result, classification)
    runtime = st.session_state.automation_runtime.get(runtime_key, {})
    queue_entry = runtime.get("queue_entry") or {}
    approved_mapping = get_approved_automation_mapping(classification)
    has_approved_automation = bool(approved_mapping)

    st.markdown("---")
    st.subheader("Engineer Runbook & Automation Readiness")
    if has_approved_automation:
        st.caption("Approved automation already exists for this classification. Use human-in-loop execution from Automation Queue instead of creating another runbook/candidate.")
    else:
        st.caption("Engineer-ready steps generated from the current incident analysis. Validate the runbook before sending it for automation review.")

    # ── Safety Gate — Drift Management badge ─────────────────────────────────
    _drift_type = str(result.get("drift_type") or "").strip()
    _drift_eligible = bool(_drift_type and _drift_type not in ("unknown_or_not_detected", "none", ""))
    if _drift_eligible:
        _drift_label = nice_label(_drift_type.replace("_drift", "").replace("_", " "))
        st.markdown(
            f"""<div style="display:flex;align-items:flex-start;gap:12px;padding:10px 14px;
                border:1px solid #fbbf24;border-left:4px solid #f59e0b;border-radius:8px;
                background:#fffbeb;margin:4px 0 12px;">
                <span style="font-size:1.1rem;margin-top:1px;">🛡️</span>
                <div>
                  <div style="font-size:0.78rem;font-weight:700;color:#92400e;
                       text-transform:uppercase;letter-spacing:.06em;margin-bottom:2px;">
                    Safety Gate Active &nbsp;·&nbsp; Drift Management ENABLED
                  </div>
                  <div style="font-size:0.8rem;color:#78350f;line-height:1.45;">
                    Detected drift type: <b>{_drift_label}</b>.
                    Before executing any remediation, the system will capture a pre-execution
                    environment snapshot (services, configs, ports) and verify no conflicting
                    changes are in progress. A before/after diff is written to the audit trail
                    on completion.
                  </div>
                  <div style="font-size:0.72rem;color:#a16207;margin-top:4px;">
                    Drift snapshots and verification run automatically when you promote to
                    Automation Queue and execute via human-in-loop approval.
                  </div>
                </div>
            </div>""",
            unsafe_allow_html=True,
        )

    _auto_status = "Approved" if has_approved_automation else ("Queued" if queue_entry else "Draft Ready")
    _auto_color = "#16a34a" if has_approved_automation else ("#0f766e" if queue_entry else "#1e293b")
    _risk_val = nice_label(runbook.get("risk_tier", "medium"))
    _approval_val = nice_label(runbook.get("approval_mode", "manual_approval_required"))
    st.markdown(
        f"""<div style="display:flex;border:1px solid #bbf7d0;border-left:4px solid #16a34a;border-radius:8px;overflow:hidden;margin:6px 0 12px;background:#f0fdf4;">
          <div style="flex:1;padding:8px 14px;border-right:1px solid #bbf7d0;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#86efac;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Runbook</div>
            <div style="font-size:0.9rem;font-weight:700;color:#16a34a;">Ready</div>
          </div>
          <div style="flex:1;padding:8px 14px;border-right:1px solid #bbf7d0;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#86efac;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Risk Tier</div>
            <div style="font-size:0.9rem;font-weight:700;color:#14532d;">{_risk_val}</div>
          </div>
          <div style="flex:1;padding:8px 14px;border-right:1px solid #bbf7d0;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#86efac;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Approval</div>
            <div style="font-size:0.9rem;font-weight:700;color:#14532d;">{_approval_val}</div>
          </div>
          <div style="flex:1;padding:8px 14px;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#86efac;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Automation</div>
            <div style="font-size:0.9rem;font-weight:700;color:{_auto_color};">{_auto_status}</div>
          </div>
        </div>""",
        unsafe_allow_html=True,
    )

    # ── Episodic recall banner ────────────────────────────────────────────────
    try:
        from episodic_memory import get_recall_html as _get_recall_html
        _recall_html = _get_recall_html(classification)
        if _recall_html:
            st.markdown(_recall_html, unsafe_allow_html=True)
    except Exception:
        pass

    # ── Structured Resolution Playbook ───────────────────────────────────────
    playbook_steps = result.get("resolution_playbook") or []
    safety = runbook.get("safety", "Validate evidence and obtain approval before executing any state-changing remediation.")

    if playbook_steps:
        render_resolution_playbook(playbook_steps, safety_note=safety)
    else:
        with st.expander("Resolution Steps", expanded=True):
            st.warning(safety)
            st.info(
                "No structured playbook available yet for this classification. "
                "Use **Automation Queue → Candidate Registry** to generate and approve a playbook, "
                "or trigger an LLM-assisted playbook via the fallback path.",
                icon="ℹ️",
            )

    # ── Promote to Automation ─────────────────────────────────────────────────
    st.markdown("---")
    promote_key = f"promote_confirm_{runtime_key}_{key_suffix}"
    if not st.session_state.get(promote_key):
        st.markdown("#### Validate & Promote to Automation")
        st.caption(
            "Once you have followed the resolution playbook and confirmed the steps are correct "
            "for this environment, promote it to the Automation Queue for human-in-loop execution. "
            "The system will register these steps as a validated playbook for this classification."
        )
        if st.button(
            "✓ I have validated the playbook — Promote to Automation",
            key=f"promote_btn_{runtime_key}_{key_suffix}",
            type="primary",
        ):
            st.session_state[promote_key] = True
            st.rerun()
    else:
        st.markdown("#### Confirm Promotion")
        st.warning(
            "**Before confirming:** You are certifying that you have executed or reviewed each "
            "step in the playbook above and the resolution steps are correct for this incident "
            "classification in this environment. This will be recorded as a validated playbook."
        )
        col_confirm, col_cancel = st.columns([1, 1])
        with col_confirm:
            if st.button(
                "Confirm & Send to Automation Queue",
                key=f"promote_confirm_btn_{runtime_key}_{key_suffix}",
                type="primary",
            ):
                st.session_state[promote_key] = False
                persist_last_incident_result(result, ticket)
                st.session_state.active_page = "Automation Queue"
                try:
                    st.query_params["page"] = "Automation Queue"
                except Exception:
                    pass
                save_last_incident_snapshot()
                st.rerun()
        with col_cancel:
            if st.button("Cancel", key=f"promote_cancel_{runtime_key}_{key_suffix}"):
                st.session_state[promote_key] = False
                st.rerun()

    if has_approved_automation:
        st.markdown("### Approved Automation Available")
        st.success("This incident classification already has an approved playbook. Route it to Automation Queue for human-in-loop execution.")
        a1, a2, a3 = st.columns(3)
        with a1:
            st.write(f"**Action:** `{approved_mapping.get('action', 'approved_action')}`")
        with a2:
            st.write(f"**Playbook:** `{approved_mapping.get('playbook', 'approved_playbook')}`")
        with a3:
            st.write(f"**Target:** `{approved_mapping.get('target_host') or approved_mapping.get('target_group') or 'approved target'}`")
        st.caption("Runbook validation is hidden for approved automation to avoid duplicate candidates. Queue approval is still required before execution.")
    else:
        st.markdown("### Runbook Validation Required")
        st.info("No approved automation is registered for this classification yet. The engineer should validate the runbook and continue automation review in Automation Queue.")

    try:
        existing_candidates_resp = list_automation_candidates(classification=classification)
        existing_candidates = existing_candidates_resp.get("items", []) if isinstance(existing_candidates_resp, dict) else []
    except Exception:
        existing_candidates = []
    existing_candidate = matching_candidate_for_incident(existing_candidates, incident_id)

    st.markdown("### Automation Control")
    st.caption("Incident Console only validates or selects the automation path. Candidate, registry, queue approval, and execution are controlled from Automation Queue.")
    candidate_entry = runtime.get("candidate_entry") or existing_candidate or {}
    if queue_entry:
        st.success(f"Queue already created: {queue_entry.get('queue_id', 'queued')}. Continue in Automation Queue.")
    elif has_approved_automation:
        st.info("Approved automation is available. Continue in Automation Queue to create/approve/execute a human-in-loop queue item.")
    elif candidate_entry:
        st.info(f"Candidate already exists: {candidate_entry.get('candidate_id', 'created')}. Continue in Automation Queue to complete registry/queue/execution.")
    else:
        st.info("No approved automation exists yet. Continue in Automation Queue to create and review a candidate from this validated runbook.")

    if st.button("Review Automation in Queue", key=f"review_automation_queue_{runtime_key}_{key_suffix}", width="stretch"):
        # Persist result so Automation Queue can read it regardless of which
        # page triggered this navigation (Incident Console or Telemetry Console).
        persist_last_incident_result(result, ticket)
        st.session_state.active_page = "Automation Queue"
        # Top navigation is URL-query-param driven. If the browser URL still has
        # page=Incident Console, render_top_nav() will overwrite active_page on
        # rerun. Update the query param as well so the button actually navigates.
        try:
            st.query_params["page"] = "Automation Queue"
        except Exception:
            try:
                st.experimental_set_query_params(page="Automation Queue")
            except Exception:
                pass
        save_last_incident_snapshot()
        st.rerun()


def render_compact_bullet_list(items: List[Any], empty_message: str):
    cleaned = [natural_label(item) for item in (items or []) if str(item or "").strip()]
    if not cleaned:
        st.caption(empty_message)
        return
    st.markdown("\n".join(f"- {item}" for item in cleaned))


def render_action_panels(result: dict):
    analysis = result.get("analysis", {})
    left, right = st.columns([1.25, 1.0], gap="large")
    with left:
        section_title("Likely Causes")
        causes = analysis.get("probable_causes", [])
        render_compact_bullet_list(causes, "No likely causes were returned.")
        section_title("Recommended Checks")
        checks = analysis.get("next_checks", [])
        render_compact_bullet_list(checks, "No recommended checks were returned.")
        section_title("Recommended Actions")
        actions = analysis.get("recommended_actions", [])
        render_compact_bullet_list(actions, "No recommended actions were returned.")
    with right:
        render_condensed_confidence_panel(result)


def render_evidence_panel(result: dict, key_suffix: str):
    retrieved = result.get("similar_incidents") or result.get("retrieved_incidents", []) or []
    evidence_ui = result.get("evidence_ui", {}) or {}
    details_label = evidence_ui.get("details_label", "Why this matched")
    section_hint = evidence_ui.get(
        "section_hint",
        "This recommendation is based on previously resolved incidents and validated engineering knowledge.",
    )

    section_title("Similar Incidents")
    st.caption(section_hint)

    with st.expander("View supporting incident matches", expanded=False):
        if retrieved:
            for i, row in enumerate(retrieved, start=1):
                summary = row.get("summary", "") or row.get("content", "") or ""
                short_summary, truncated = shorten_text(summary, max_chars=145)
                st.write(f"**{i}. {nice_label(row.get('classification', 'unknown'))}**")
                collection_name = row.get("collection_name") or ""
                source_name = row.get("source") or row.get("source_type") or ""
                domain_name = str(row.get("domain", "unknown") or "unknown")
                if domain_name.lower() == "iam":
                    domain_name = "IAM"
                else:
                    domain_name = domain_name.title()

                meta_parts = [
                    f"Reference: {row.get('id')}",
                    f"Domain: {domain_name}",
                    f"Score: {format_confidence(row.get('score'), 2)}",
                ]
                if source_name:
                    meta_parts.append(f"Source: {source_name}")
                if collection_name:
                    meta_parts.append(f"Collection: {collection_name}")
                st.caption(" | ".join(meta_parts))
                if short_summary:
                    st.caption(short_summary)
                if truncated:
                    with st.expander(details_label, expanded=False):
                        st.write(summary)
                elif summary:
                    with st.expander(details_label, expanded=False):
                        st.write(summary)
                st.markdown("---")
        else:
            st.caption("No similar incidents were retrieved.")


def render_knowledge_panels(result: dict):
    knowledge_summary = result.get("knowledge_summary", {}) or {}
    linked_resolution = result.get("linked_resolution") or {}
    linked_playbook = result.get("linked_playbook") or {}

    if not knowledge_summary and not linked_resolution and not linked_playbook:
        return

    st.markdown("---")
    section_title("Knowledge Reuse Summary")

    _retrieved_ids = knowledge_summary.get("retrieved_incident_ids", []) or []
    _ret_count = str(len(_retrieved_ids))
    _has_hits = len(_retrieved_ids) > 0
    # Only show resolution/playbook as linked when there are actual retrieved incidents backing them
    _res_linked = "Yes" if _has_hits and (knowledge_summary.get("linked_resolution_id") or linked_resolution) else ("Generic" if (knowledge_summary.get("linked_resolution_id") or linked_resolution) else "No")
    _pb_linked = "Yes" if _has_hits and (knowledge_summary.get("linked_playbook_id") or linked_playbook) else ("Generic" if (knowledge_summary.get("linked_playbook_id") or linked_playbook) else "No")
    _res_color = "#16a34a" if _res_linked == "Yes" else ("#d97706" if _res_linked == "Generic" else "#94a3b8")
    _pb_color = "#16a34a" if _pb_linked == "Yes" else ("#d97706" if _pb_linked == "Generic" else "#94a3b8")
    st.markdown(
        f"""<div style="display:flex;border:1px solid #e9d5ff;border-left:4px solid #7c3aed;border-radius:8px;overflow:hidden;margin:6px 0 12px;background:#faf5ff;">
          <div style="flex:1;padding:8px 14px;border-right:1px solid #e9d5ff;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#c4b5fd;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Retrieved Incidents</div>
            <div style="font-size:0.9rem;font-weight:700;color:#3b0764;">{_ret_count}</div>
          </div>
          <div style="flex:1;padding:8px 14px;border-right:1px solid #e9d5ff;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#c4b5fd;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Resolution Linked</div>
            <div style="font-size:0.9rem;font-weight:700;color:{_res_color};">{_res_linked}</div>
          </div>
          <div style="flex:1;padding:8px 14px;text-align:center;">
            <div style="font-size:0.65rem;font-weight:700;color:#c4b5fd;text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px;">Playbook Linked</div>
            <div style="font-size:0.9rem;font-weight:700;color:{_pb_color};">{_pb_linked}</div>
          </div>
        </div>""",
        unsafe_allow_html=True,
    )

    if linked_resolution:
        with st.expander("Linked Resolution", expanded=False):
            root_cause = linked_resolution.get("root_cause") or ""
            if root_cause:
                st.write(f"**Root Cause:** {root_cause}")

            validated_checks = linked_resolution.get("validated_checks") or []
            if validated_checks:
                st.write("**Validated Checks**")
                for item in validated_checks:
                    st.write(f"• {item}")

            validated_actions = linked_resolution.get("validated_actions") or []
            if validated_actions:
                st.write("**Validated Actions**")
                for item in validated_actions:
                    st.write(f"• {item}")

            success_notes = linked_resolution.get("success_notes") or ""
            if success_notes:
                st.write(f"**Success Notes:** {success_notes}")

    if linked_playbook:
        with st.expander("Linked Playbook", expanded=False):
            name = linked_playbook.get("name") or ""
            description = linked_playbook.get("description") or ""
            if name:
                st.write(f"**Name:** {name}")
            if description:
                st.write(f"**Description:** {description}")

            steps = linked_playbook.get("steps") or []
            if steps:
                st.write("**Steps**")
                for item in steps:
                    st.write(f"• {item}")

            scripts = linked_playbook.get("scripts") or []
            if scripts:
                st.write("**Scripts**")
                for item in scripts:
                    st.write(f"• {item}")


def render_confirmation_panel(result: dict, ticket: str, key_suffix: str):
    routing = result.get("routing", {})
    if routing.get("decision") != "user_confirmation_required":
        return

    st.markdown("---")
    st.subheader("Match Confirmation")
    prompt = result.get("user_confirmation_prompt") or "I found partially similar local incidents. Do any of these look relevant?"
    st.info(prompt)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Use local matches", key=f"use_local_{key_suffix}", width="stretch"):
            try:
                confirmed_result = call_backend_with_text(
                    ticket,
                    use_fallback=True,
                    force_fallback=False,
                    confirm_local_matches=True,
                    tenant_id=st.session_state.get("tenant_id", "client_c"),
                    knowledge_scope=st.session_state.get("knowledge_scope", "client_private"),
                )
                st.session_state.messages.append({"role": "assistant", "result": confirmed_result, "ticket": ticket})
                persist_last_incident_result(confirmed_result, ticket)
                st.rerun()
            except Exception as e:
                st.error(f"Local confirmation failed: {e}")
    with col2:
        if st.button("None relevant — use fallback", key=f"use_fallback_{key_suffix}", width="stretch"):
            try:
                fallback_result = call_backend_with_text(
                    ticket,
                    use_fallback=True,
                    force_fallback=True,
                    confirm_local_matches=False,
                    tenant_id=st.session_state.get("tenant_id", "client_c"),
                    knowledge_scope=st.session_state.get("knowledge_scope", "client_private"),
                )
                st.session_state.messages.append({"role": "assistant", "result": fallback_result, "ticket": ticket})
                persist_last_incident_result(fallback_result, ticket)
                st.rerun()
            except Exception as e:
                st.error(f"Fallback escalation failed: {e}")


def render_debug_trace(result: dict):
    compact = {
        "request_id": result.get("request_id"),
        "analysis_source": result.get("analysis_source_label", result.get("analysis", {}).get("analysis_source_label", "Unknown")),
        "domain": result.get("domain"),
        "routing": result.get("routing"),
        "analysis": {
            "classification": result.get("analysis", {}).get("classification"),
            "confidence": result.get("analysis", {}).get("confidence"),
            "escalate": result.get("analysis", {}).get("escalate"),
        },
        "confidence": {
            "overall_confidence": result.get("confidence", {}).get("overall_confidence"),
            "primary_confidence": result.get("confidence", {}).get("primary_confidence"),
            "fallback_confidence": result.get("confidence", {}).get("fallback_confidence"),
            "retrieval_quality": result.get("confidence", {}).get("retrieval_quality"),
            "classification_certainty": result.get("confidence", {}).get("classification_certainty"),
        },
        "retrieved_incident_count": len(result.get("retrieved_incidents", [])),
        "fallback_used": result.get("fallback_used"),
        "knowledge_summary": result.get("knowledge_summary"),
    }
    with st.expander("Developer Trace", expanded=False):
        st.json(compact)


def render_feedback(result: dict, ticket: str, key_suffix: str):
    analysis = result.get("analysis", {})
    predicted_display = nice_label(analysis.get("classification", "unknown"))
    domain = result.get("domain", "general")
    request_id = result.get("request_id")
    st.markdown("---")
    section_title("Feedback")

    feedback_key = f"feedback_{key_suffix}"
    if feedback_key not in st.session_state or st.session_state.get(feedback_key) != predicted_display:
        st.session_state[feedback_key] = predicted_display

    corrected_display = st.text_input(
        "Confirm or adjust the classification",
        key=feedback_key,
    )
    col1, _ = st.columns([0.30, 0.70])
    with col1:
        if st.button("Submit Feedback", key=f"submit_{key_suffix}", width="stretch"):
            try:
                submit_feedback(ticket=ticket, predicted=predicted_display, corrected=corrected_display, domain=domain, request_id=request_id)
                st.success("Feedback saved.")
            except Exception as e:
                st.error(f"Feedback failed: {e}")


def should_render_candidate_workflow(result: dict) -> bool:
    candidate = result.get("knowledge_candidate")
    if not candidate:
        return False

    candidate_id = str(candidate.get("candidate_id") or "").strip()
    linked_resolution = result.get("linked_resolution") or {}
    linked_playbook = result.get("linked_playbook") or {}
    auto_learn_done = bool(st.session_state.workflow_auto_learn_status.get(candidate_id) == "completed")
    saved_resolution_id = st.session_state.workflow_saved_resolution_ids.get(candidate_id)
    saved_playbook_id = st.session_state.workflow_saved_playbook_ids.get(candidate_id)

    # Suppress the full guided learning workflow when the same learned candidate
    # already has saved resolution + playbook and the result is linking them again.
    if auto_learn_done and linked_resolution and linked_playbook and saved_resolution_id and saved_playbook_id:
        return False

    return True


def candidate_to_resolution_root_cause(candidate: Dict[str, Any]) -> str:
    classification = nice_label(candidate.get("classification", "unknown issue"))
    causes = candidate.get("probable_causes", []) or []
    if causes:
        readable = ", ".join(natural_label(c) for c in causes[:3])
        return f"{classification} is likely driven by {readable}."
    return f"{classification} was validated from fallback analysis and requires engineering confirmation."


def candidate_to_playbook_name(candidate: Dict[str, Any]) -> str:
    return f"{nice_label(candidate.get('classification', 'Unknown Issue'))} Recovery"


def candidate_to_playbook_description(candidate: Dict[str, Any]) -> str:
    classification = nice_label(candidate.get("classification", "unknown issue"))
    return f"Validate, isolate, and remediate {classification.lower()} using approved operational steps."


def candidate_to_playbook_steps(candidate: Dict[str, Any]) -> List[str]:
    steps: List[str] = []
    for item in candidate.get("next_checks", []) or []:
        steps.append(natural_label(item))
    for item in candidate.get("recommended_actions", []) or []:
        steps.append(natural_label(item))
    return dedupe_lines(steps)


def _candidate_scripts_text(candidate: Dict[str, Any]) -> str:
    """Extract automation script lines from the candidate for playbook pre-fill."""
    draft_runbook = candidate.get("draft_runbook") or {}
    # Prefer linked_playbook scripts if available
    scripts = (candidate.get("linked_playbook") or {}).get("scripts") or []
    if not scripts:
        scripts = draft_runbook.get("automation_steps") or candidate.get("draft_steps") or []
    if not scripts:
        return ""
    return "\n".join(str(s) for s in scripts if str(s or "").strip())


def ensure_candidate_workflow_state(candidate: Dict[str, Any]):
    candidate_id = candidate["candidate_id"]
    draft_runbook = candidate.get("draft_runbook") or {}
    engineer_review = candidate.get("engineer_review") or {}
    created_by = candidate.get("created_by") or candidate.get("requested_by") or "Engineer"
    risk_tier = candidate.get("risk_tier") or draft_runbook.get("risk_tier") or "medium"
    approval_mode = (
        (candidate.get("linked_playbook") or {}).get("approval_mode")
        or draft_runbook.get("approval_mode")
        or ("auto_approved" if risk_tier == "low" else "manual_approval_required")
    )
    rollback_available = bool(
        candidate.get("rollback_available")
        if candidate.get("rollback_available") is not None
        else (candidate.get("linked_playbook") or {}).get("rollback_available", True)
    )
    objective = draft_runbook.get("objective") or ""
    root_cause = (
        draft_runbook.get("root_cause")
        or (draft_runbook.get("linked_resolution") or {}).get("root_cause")
        or candidate_to_resolution_root_cause(candidate)
    )

    if candidate_id not in st.session_state.workflow_candidate_reviews:
        st.session_state.workflow_candidate_reviews[candidate_id] = {
            "classification": candidate.get("classification", ""),
            "probable_causes_text": join_lines(
                candidate.get("probable_causes") or draft_runbook.get("probable_causes") or []
            ),
            "next_checks_text": join_lines(
                candidate.get("next_checks") or draft_runbook.get("validation_checks") or []
            ),
            "recommended_actions_text": join_lines(
                candidate.get("recommended_actions") or draft_runbook.get("remediation_steps") or []
            ),
            "review_notes": engineer_review.get("review_notes") or objective or "",
        }
    if candidate_id not in st.session_state.workflow_resolution_drafts:
        st.session_state.workflow_resolution_drafts[candidate_id] = {
            "classification": candidate.get("classification", ""),
            "root_cause": root_cause,
            "validated_checks_text": join_lines(
                candidate.get("next_checks") or draft_runbook.get("validation_checks") or []
            ),
            "validated_actions_text": join_lines(
                candidate.get("recommended_actions") or draft_runbook.get("remediation_steps") or []
            ),
            "confirmed_by": created_by,
            "success_notes": (
                f"Validated via UI-guided approval flow. Risk tier: {risk_tier}."
                + (f" Objective: {objective}" if objective else "")
            ),
            "automation_ready": risk_tier in ("low", "medium"),
        }
    if candidate_id not in st.session_state.workflow_playbook_drafts:
        st.session_state.workflow_playbook_drafts[candidate_id] = {
            "name": candidate_to_playbook_name(candidate),
            "classification": candidate.get("classification", ""),
            "description": (
                (candidate.get("linked_playbook") or {}).get("description")
                or draft_runbook.get("objective")
                or candidate_to_playbook_description(candidate)
            ),
            "steps_text": join_lines(candidate_to_playbook_steps(candidate)),
            "scripts_text": _candidate_scripts_text(candidate),
            "approval_mode": approval_mode,
            "rollback_available": rollback_available,
            "risk_tier": risk_tier,
        }


def auto_learn_candidate(candidate: Dict[str, Any]) -> Dict[str, str]:
    candidate_id = candidate["candidate_id"]
    ensure_candidate_workflow_state(candidate)

    review_state = st.session_state.workflow_candidate_reviews[candidate_id]
    resolution_state = st.session_state.workflow_resolution_drafts[candidate_id]
    playbook_state = st.session_state.workflow_playbook_drafts[candidate_id]

    review_resp = review_candidate({
        "candidate_id": candidate_id,
        "reviewed_by": "Vijay",
        "approved": True,
        "review_notes": review_state["review_notes"] or "Approved via one-click UI workflow.",
        "updated_classification": system_label(review_state["classification"]),
        "updated_probable_causes": lines_to_list(review_state["probable_causes_text"]),
        "updated_next_checks": lines_to_list(review_state["next_checks_text"]),
        "updated_recommended_actions": lines_to_list(review_state["recommended_actions_text"]),
    })
    approved_candidate = review_resp.get("candidate", {})
    st.session_state.workflow_review_status[candidate_id] = approved_candidate.get("status", "approved")

    resolution_state["classification"] = approved_candidate.get("classification", review_state["classification"])
    resolution_state["root_cause"] = resolution_state["root_cause"] or candidate_to_resolution_root_cause(approved_candidate)
    resolution_state["validated_checks_text"] = join_lines(approved_candidate.get("next_checks", [])) or resolution_state["validated_checks_text"]
    resolution_state["validated_actions_text"] = join_lines(approved_candidate.get("recommended_actions", [])) or resolution_state["validated_actions_text"]

    playbook_state["classification"] = approved_candidate.get("classification", review_state["classification"])
    playbook_state["name"] = playbook_state["name"] or candidate_to_playbook_name(approved_candidate)
    playbook_state["description"] = playbook_state["description"] or candidate_to_playbook_description(approved_candidate)
    playbook_state["steps_text"] = playbook_state["steps_text"] or join_lines(candidate_to_playbook_steps(approved_candidate))

    promote_resp = promote_candidate(candidate_id)
    incident_id = promote_resp.get("incident", {}).get("id", "promoted")
    st.session_state.workflow_promotions[candidate_id] = incident_id

    resolution_resp = create_resolution_record({
        "classification": system_label(resolution_state["classification"]),
        "root_cause": resolution_state["root_cause"],
        "validated_checks": lines_to_list(resolution_state["validated_checks_text"]),
        "validated_actions": lines_to_list(resolution_state["validated_actions_text"]),
        "confirmed_by": resolution_state["confirmed_by"],
        "success_notes": resolution_state["success_notes"],
        "linked_candidate_id": candidate_id,
        "automation_ready": bool(resolution_state["automation_ready"]),
        "linked_playbook_id": "",
    })
    resolution_id = resolution_resp.get("resolution", {}).get("resolution_id", "saved")
    st.session_state.workflow_saved_resolution_ids[candidate_id] = resolution_id

    playbook_resp = create_playbook_record({
        "name": playbook_state["name"],
        "classification": system_label(playbook_state["classification"]),
        "description": playbook_state["description"],
        "steps": lines_to_list(playbook_state["steps_text"]),
        "scripts": lines_to_list(playbook_state["scripts_text"]),
        "approval_mode": playbook_state["approval_mode"],
        "rollback_available": bool(playbook_state["rollback_available"]),
        "risk_tier": playbook_state["risk_tier"],
    })
    playbook_id = playbook_resp.get("playbook", {}).get("playbook_id", "saved")
    st.session_state.workflow_saved_playbook_ids[candidate_id] = playbook_id
    st.session_state.workflow_auto_learn_status[candidate_id] = "completed"

    # Reinforce long-term + episodic memory directly from the UI path (#13/#14)
    try:
        _cls = system_label(resolution_state.get("classification", ""))
        _by  = resolution_state.get("confirmed_by", "Engineer")
        _auto = bool(resolution_state.get("automation_ready"))
        from long_term_memory import record_outcome as _rec_outcome
        _rec_outcome(_cls, success=True, resolved_by=_by)
    except Exception:
        pass
    try:
        from episodic_memory import record_episode as _rec_episode
        _rec_episode(
            classification=_cls,
            incident_id=str(candidate.get("incident_id") or candidate_id),
            resolved_by=_by,
            outcome="automated" if _auto else "resolved",
        )
    except Exception:
        pass

    return {
        "incident_id": incident_id,
        "resolution_id": resolution_id,
        "playbook_id": playbook_id,
    }


@st.dialog("Approve & Learn", width="large")
def _approve_learn_dialog(candidate: Dict[str, Any], key_suffix: str):
    """Compact single-screen modal — review key fields, then execute Approve & Learn in one action."""
    candidate_id = candidate["candidate_id"]
    review_state = st.session_state.workflow_candidate_reviews[candidate_id]
    resolution_state = st.session_state.workflow_resolution_drafts[candidate_id]
    playbook_state = st.session_state.workflow_playbook_drafts[candidate_id]

    st.caption("Review the pre-filled fields, adjust if needed, then click **Confirm & Learn** to save everything.")

    col_a, col_b = st.columns(2)
    with col_a:
        review_state["classification"] = st.text_input(
            "Classification",
            value=review_state["classification"],
            key=f"dlg_cls_{candidate_id}",
        )
        review_state["review_notes"] = st.text_area(
            "Review Notes",
            value=review_state["review_notes"],
            key=f"dlg_notes_{candidate_id}",
            height=80,
        )
        resolution_state["root_cause"] = st.text_area(
            "Root Cause",
            value=resolution_state["root_cause"],
            key=f"dlg_rc_{candidate_id}",
            height=100,
        )
    with col_b:
        risk_opts = ["low", "medium", "high"]
        _risk_idx = risk_opts.index(playbook_state["risk_tier"]) if playbook_state["risk_tier"] in risk_opts else 1
        playbook_state["risk_tier"] = st.selectbox(
            "Risk Tier", options=risk_opts, index=_risk_idx,
            key=f"dlg_risk_{candidate_id}",
        )
        playbook_state["rollback_available"] = st.checkbox(
            "Rollback Available",
            value=bool(playbook_state["rollback_available"]),
            key=f"dlg_rollback_{candidate_id}",
        )
        resolution_state["automation_ready"] = st.checkbox(
            "Automation Ready",
            value=bool(resolution_state["automation_ready"]),
            key=f"dlg_auto_ready_{candidate_id}",
        )
        resolution_state["confirmed_by"] = st.text_input(
            "Confirmed By",
            value=resolution_state["confirmed_by"],
            key=f"dlg_confirmed_by_{candidate_id}",
        )

    # Show a compact read-only summary of what will be saved
    checks = [l for l in (review_state.get("next_checks_text") or "").splitlines() if l.strip()]
    actions = [l for l in (review_state.get("recommended_actions_text") or "").splitlines() if l.strip()]
    if checks or actions:
        with st.expander("📋 Checks & Actions summary", expanded=False):
            if checks:
                st.markdown("**Validated Checks:**\n" + "\n".join(f"- {c}" for c in checks[:5]))
            if actions:
                st.markdown("**Recommended Actions:**\n" + "\n".join(f"- {a}" for a in actions[:5]))

    st.markdown("---")
    left_btn, right_btn = st.columns(2)
    with left_btn:
        if st.button("Confirm & Learn", key=f"dlg_confirm_{candidate_id}", type="primary", use_container_width=True):
            # Sync any dialog edits back into session state before calling auto_learn_candidate
            resolution_state["classification"] = system_label(review_state["classification"])
            playbook_state["classification"]   = system_label(review_state["classification"])
            playbook_state["name"]             = playbook_state.get("name") or candidate_to_playbook_name(candidate)
            playbook_state["description"]      = playbook_state.get("description") or candidate_to_playbook_description(candidate)
            try:
                auto_learn_candidate(candidate)
                st.rerun()
            except Exception as e:
                st.error(f"Approve & Learn failed: {e}")
    with right_btn:
        if st.button("Cancel", key=f"dlg_cancel_{candidate_id}", use_container_width=True):
            st.rerun()


def render_candidate_workflow(result: dict, ticket: str, key_suffix: str):
    candidate = result.get("knowledge_candidate")
    if not candidate:
        return
    ensure_candidate_workflow_state(candidate)
    candidate_id = candidate["candidate_id"]
    review_state = st.session_state.workflow_candidate_reviews[candidate_id]
    resolution_state = st.session_state.workflow_resolution_drafts[candidate_id]
    playbook_state = st.session_state.workflow_playbook_drafts[candidate_id]
    review_status = st.session_state.workflow_review_status.get(candidate_id)
    promotion_info = st.session_state.workflow_promotions.get(candidate_id)
    saved_resolution_id = st.session_state.workflow_saved_resolution_ids.get(candidate_id)
    saved_playbook_id = st.session_state.workflow_saved_playbook_ids.get(candidate_id)
    auto_learn_done = st.session_state.workflow_auto_learn_status.get(candidate_id) == "completed"

    review_done = bool(review_status)
    promotion_done = bool(promotion_info)
    resolution_done = bool(saved_resolution_id)
    playbook_done = bool(saved_playbook_id)

    st.markdown("---")
    st.subheader("Approve & Learn")
    st.caption("One-click: review key fields, confirm — the system saves the resolution, playbook, and promotes the incident.")

    # ── Completion status strip ───────────────────────────────────────
    if auto_learn_done:
        st.success("✅ Approve & Learn completed. Knowledge base updated.")
    else:
        if review_status:
            st.success(f"✔ Candidate reviewed: {review_status}")
        if promotion_info:
            st.success(f"✔ Promoted as incident {promotion_info}")
        if saved_resolution_id:
            st.success(f"✔ Resolution saved: {saved_resolution_id}")
        if saved_playbook_id:
            st.success(f"✔ Playbook saved: {saved_playbook_id}")

    # ── Primary CTA ──────────────────────────────────────────────────
    cta_col, _ = st.columns([0.4, 0.6])
    with cta_col:
        if st.button(
            "⚡ Approve & Learn",
            key=f"approve_and_learn_{candidate_id}_{key_suffix}",
            type="primary",
            use_container_width=True,
            disabled=auto_learn_done,
            help="Opens a compact review dialog — adjust key fields then confirm in one action.",
        ):
            _approve_learn_dialog(candidate, key_suffix)

    # ── Advanced: sequential expanders ──────────────────────────────
    st.caption("Need to control each step individually? Use the manual flow below.")

    with st.expander("1) Review Candidate", expanded=not review_done and not auto_learn_done):
        _generic_labels = {"service_degradation", "application_error", "middleware_issue",
                           "network_issue", "general_issue", "ambiguous_post_change_service_issue",
                           "middleware_service_issue", "unknown"}
        _current_cls = (review_state["classification"] or "").strip().lower()
        _is_generic = _current_cls in _generic_labels
        if _is_generic and not review_done and not auto_learn_done:
            st.warning(
                f"⚠️ Classification **{nice_label(review_state['classification'])}** is generic. "
                "Refine it to something specific (e.g. `as400_server_unavailable`, `websphere_jvm_crash`) "
                "before approving so the knowledge base entry is useful.",
                icon=None,
            )
        review_state["classification"] = st.text_input(
            "Classification",
            value=review_state["classification"],
            key=f"classification_{candidate_id}_{key_suffix}",
            disabled=review_done or auto_learn_done,
        )
        review_state["review_notes"] = st.text_area(
            "Review Notes",
            value=review_state["review_notes"],
            key=f"review_notes_{candidate_id}_{key_suffix}",
            height=80,
            disabled=review_done or auto_learn_done,
        )
        review_state["probable_causes_text"] = st.text_area(
            "Probable Causes (one per line)",
            value=review_state["probable_causes_text"],
            key=f"probable_causes_{candidate_id}_{key_suffix}",
            height=120,
            disabled=review_done or auto_learn_done,
        )
        review_state["next_checks_text"] = st.text_area(
            "Recommended Checks (one per line)",
            value=review_state["next_checks_text"],
            key=f"next_checks_{candidate_id}_{key_suffix}",
            height=120,
            disabled=review_done or auto_learn_done,
        )
        review_state["recommended_actions_text"] = st.text_area(
            "Recommended Actions (one per line)",
            value=review_state["recommended_actions_text"],
            key=f"recommended_actions_{candidate_id}_{key_suffix}",
            height=120,
            disabled=review_done or auto_learn_done,
        )
        col1, col2 = st.columns(2)
        with col1:
            from rbac import can_approve_candidates
            if not can_approve_candidates():
                st.caption("🔒 L2 or higher required to approve candidates.")
            elif st.button("Approve Candidate", key=f"approve_candidate_{candidate_id}_{key_suffix}", width="stretch", disabled=review_done or auto_learn_done):
                try:
                    resp = review_candidate({
                        "candidate_id": candidate_id,
                        "reviewed_by": "Vijay",
                        "approved": True,
                        "review_notes": review_state["review_notes"],
                        "updated_classification": system_label(review_state["classification"]),
                        "updated_probable_causes": lines_to_list(review_state["probable_causes_text"]),
                        "updated_next_checks": lines_to_list(review_state["next_checks_text"]),
                        "updated_recommended_actions": lines_to_list(review_state["recommended_actions_text"]),
                    })
                    approved_candidate = resp.get("candidate", {})
                    st.session_state.workflow_review_status[candidate_id] = approved_candidate.get("status", "approved")
                    resolution_state["classification"] = approved_candidate.get("classification", review_state["classification"])
                    resolution_state["root_cause"] = candidate_to_resolution_root_cause(approved_candidate)
                    resolution_state["validated_checks_text"] = join_lines(approved_candidate.get("next_checks", []))
                    resolution_state["validated_actions_text"] = join_lines(approved_candidate.get("recommended_actions", []))
                    playbook_state["classification"] = approved_candidate.get("classification", review_state["classification"])
                    playbook_state["name"] = candidate_to_playbook_name(approved_candidate)
                    playbook_state["description"] = candidate_to_playbook_description(approved_candidate)
                    playbook_state["steps_text"] = join_lines(candidate_to_playbook_steps(approved_candidate))
                    st.rerun()
                except Exception as e:
                    st.error(f"Candidate review failed: {e}")
        with col2:
            if st.button("Reject Candidate", key=f"reject_candidate_{candidate_id}_{key_suffix}", width="stretch", disabled=review_done or auto_learn_done):
                try:
                    resp = review_candidate({
                        "candidate_id": candidate_id,
                        "reviewed_by": "Vijay",
                        "approved": False,
                        "review_notes": review_state["review_notes"] or "Rejected from UI workflow.",
                        "updated_classification": system_label(review_state["classification"]),
                        "updated_probable_causes": lines_to_list(review_state["probable_causes_text"]),
                        "updated_next_checks": lines_to_list(review_state["next_checks_text"]),
                        "updated_recommended_actions": lines_to_list(review_state["recommended_actions_text"]),
                    })
                    st.session_state.workflow_review_status[candidate_id] = resp.get("candidate", {}).get("status", "rejected")
                    st.rerun()
                except Exception as e:
                    st.error(f"Candidate rejection failed: {e}")

    with st.expander("2) Promote Candidate", expanded=review_done and not promotion_done and not auto_learn_done):
        st.caption("This writes the approved candidate into the incident knowledge base for future local retrieval.")
        if st.button("Promote Candidate to Incident", key=f"promote_candidate_{candidate_id}_{key_suffix}", width="stretch", disabled=(not review_done or promotion_done or auto_learn_done)):
            try:
                resp = promote_candidate(candidate_id)
                incident = resp.get("incident", {})
                st.session_state.workflow_promotions[candidate_id] = incident.get("id", "promoted")
                st.rerun()
            except Exception as e:
                st.error(f"Promotion failed: {e}")

    with st.expander("3) Resolution Draft", expanded=promotion_done and not resolution_done and not auto_learn_done):
        resolution_state["classification"] = st.text_input("Resolution Classification", value=resolution_state["classification"], key=f"resolution_classification_{candidate_id}_{key_suffix}", disabled=resolution_done or auto_learn_done)
        resolution_state["root_cause"] = st.text_area("Root Cause", value=resolution_state["root_cause"], key=f"resolution_root_cause_{candidate_id}_{key_suffix}", height=120, disabled=resolution_done or auto_learn_done)
        resolution_state["validated_checks_text"] = st.text_area("Validated Checks (one per line)", value=resolution_state["validated_checks_text"], key=f"resolution_checks_{candidate_id}_{key_suffix}", height=120, disabled=resolution_done or auto_learn_done)
        resolution_state["validated_actions_text"] = st.text_area("Validated Actions (one per line)", value=resolution_state["validated_actions_text"], key=f"resolution_actions_{candidate_id}_{key_suffix}", height=120, disabled=resolution_done or auto_learn_done)
        resolution_state["confirmed_by"] = st.text_input("Confirmed By", value=resolution_state["confirmed_by"], key=f"resolution_confirmed_by_{candidate_id}_{key_suffix}", disabled=resolution_done or auto_learn_done)
        resolution_state["success_notes"] = st.text_area("Success Notes", value=resolution_state["success_notes"], key=f"resolution_notes_{candidate_id}_{key_suffix}", height=80, disabled=resolution_done or auto_learn_done)
        resolution_state["automation_ready"] = st.checkbox("Automation Ready", value=bool(resolution_state["automation_ready"]), key=f"resolution_automation_ready_{candidate_id}_{key_suffix}", disabled=resolution_done or auto_learn_done)

        # ── Drift-gated resolution: 3-step flow ─────────────────────────────
        # State keys for this candidate's drift cycle
        _dkey_snap   = f"drift_res_snap_{candidate_id}"    # snapshot_id
        _dkey_ver    = f"drift_res_verify_{candidate_id}"  # "pass"|"fail"|None
        _dkey_live   = f"drift_res_live_{candidate_id}"    # live verify result
        _snap_id     = st.session_state.get(_dkey_snap)
        _snap_ver    = st.session_state.get(_dkey_ver)
        _live_ver    = st.session_state.get(_dkey_live)
        _res_ci      = str(resolution_state.get("classification") or candidate_id or "unknown")
        _res_mode    = "lab" if _res_ci in ("linux-lab-01", "linux-lab-02", "network-lab-01") else "dry"

        st.markdown("---")
        st.markdown("**🔄 Drift Validation — test the resolution before applying to live**")

        # Progress indicator
        _steps = [
            ("① Snapshot + Test", _snap_id is not None),
            ("② Verify snapshot", _snap_ver is not None),
            ("③ Apply to live + Audit", _live_ver is not None),
        ]
        _prog_html = '<div style="display:flex;gap:8px;margin:8px 0;">'
        for _s_label, _s_done in _steps:
            _s_color = "#16a34a" if _s_done else "#94a3b8"
            _prog_html += (
                f'<div style="background:{_s_color};color:#fff;border-radius:4px;'
                f'padding:4px 10px;font-size:0.72rem;font-weight:600;">{_s_label}</div>'
            )
        _prog_html += "</div>"
        st.markdown(_prog_html, unsafe_allow_html=True)

        # Step 1: Capture snapshot + apply resolution steps as remediation
        _step1_done = _snap_id is not None
        if st.button(
            "📸 Capture Snapshot & Test Resolution",
            key=f"drift_snap_test_{candidate_id}_{key_suffix}",
            disabled=(_step1_done or resolution_done or auto_learn_done or not promotion_done),
            help="Captures the current (broken) environment state, then applies your validated actions as a test on the snapshot.",
        ):
            with st.spinner("Capturing snapshot of current state…"):
                try:
                    _snap_r = SESSION.post(
                        f"{BACKEND_URL}/drift/snapshot",
                        json={"ci_name": _res_ci, "mode": _res_mode,
                              "container": _res_ci if _res_mode == "lab" else None,
                              "notes": f"Pre-resolution snapshot — {resolution_state.get('confirmed_by', 'engineer')} — {resolution_state.get('root_cause', '')[:80]}"},
                        timeout=15,
                    )
                    if _snap_r.status_code == 200:
                        _new_snap_id = _snap_r.json().get("snapshot_id")
                        st.session_state[_dkey_snap] = _new_snap_id
                        # Apply the validated actions as remediation changes on the snapshot
                        _actions = lines_to_list(resolution_state.get("validated_actions_text", ""))
                        _changes = [{"type": "run_command", "target": a, "value": a} for a in _actions if a]
                        SESSION.post(
                            f"{BACKEND_URL}/drift/remediate",
                            json={"snapshot_id": _new_snap_id, "playbook_name": "resolution_test",
                                  "changes": _changes or [{"type": "run_command", "target": "echo resolution-test", "value": ""}]},
                            timeout=20,
                        )
                        st.success(f"Snapshot captured and resolution steps applied: `{_new_snap_id}`")
                    else:
                        st.error(f"Snapshot failed: {_snap_r.text}")
                except Exception as _e:
                    st.error(f"Snapshot error: {_e}")
            st.rerun()

        # Step 2: Verify snapshot — confirm fix works without breakage
        _step2_done = _snap_ver is not None
        if st.button(
            "✅ Verify Snapshot",
            key=f"drift_verify_snap_{candidate_id}_{key_suffix}",
            disabled=(not _step1_done or _step2_done or resolution_done or auto_learn_done),
            help="Verifies the snapshot state after applying the resolution steps — confirms no breakage before touching live.",
        ):
            with st.spinner("Verifying snapshot…"):
                try:
                    _ver_r = SESSION.post(f"{BACKEND_URL}/drift/verify",
                                          json={"snapshot_id": _snap_id}, timeout=30)
                    if _ver_r.status_code == 200:
                        _ver_result = _ver_r.json().get("verify_result", "unknown")
                        st.session_state[_dkey_ver] = _ver_result
                    else:
                        st.error(f"Verification error: {_ver_r.text}")
                except Exception as _e:
                    st.error(f"Verify error: {_e}")
            st.rerun()

        # Show snapshot verification result
        if _snap_ver == "pass":
            st.success("✅ Snapshot verification **PASSED** — resolution is safe to apply on live system.")
        elif _snap_ver == "fail":
            st.error("❌ Snapshot verification **FAILED** — review the resolution steps before applying to live.")
            if st.button("🔄 Retry with different steps", key=f"drift_retry_{candidate_id}_{key_suffix}"):
                st.session_state.pop(_dkey_snap, None)
                st.session_state.pop(_dkey_ver, None)
                st.rerun()

        # Step 3: Save resolution + verify live + destroy snapshot + audit
        _snapshot_test_passed = (_snap_ver == "pass")
        if st.button(
            "💾 Apply to Live & Save Resolution",
            key=f"save_resolution_{candidate_id}_{key_suffix}",
            width="stretch",
            disabled=(not _snapshot_test_passed or resolution_done or auto_learn_done),
            help="Only enabled after snapshot verification passes. Saves the resolution, then verifies the live system and writes the drift audit.",
        ):
            try:
                # Save the resolution record (engineer now applies fix to live system)
                resp = create_resolution_record({
                    "classification": system_label(resolution_state["classification"]),
                    "root_cause": resolution_state["root_cause"],
                    "validated_checks": lines_to_list(resolution_state["validated_checks_text"]),
                    "validated_actions": lines_to_list(resolution_state["validated_actions_text"]),
                    "confirmed_by": resolution_state["confirmed_by"],
                    "success_notes": resolution_state["success_notes"],
                    "linked_candidate_id": candidate_id,
                    "automation_ready": bool(resolution_state["automation_ready"]),
                    "linked_playbook_id": "",
                })
                st.session_state.workflow_saved_resolution_ids[candidate_id] = resp.get("resolution", {}).get("resolution_id", "saved")

                # Verify live system stability (post live-fix)
                try:
                    _live_r = SESSION.post(f"{BACKEND_URL}/drift/verify",
                                           json={"snapshot_id": _snap_id}, timeout=30)
                    _live_result = _live_r.json().get("verify_result", "unknown") if _live_r.status_code == 200 else "unknown"
                    st.session_state[_dkey_live] = _live_result
                except Exception:
                    _live_result = "unknown"
                    st.session_state[_dkey_live] = _live_result

                # Destroy snapshot and write audit (before = broken state, after = resolved state)
                try:
                    SESSION.post(f"{BACKEND_URL}/drift/destroy",
                                 json={"snapshot_id": _snap_id, "write_audit": True}, timeout=10)
                except Exception:
                    pass

                st.rerun()
            except Exception as e:
                st.error(f"Resolution save failed: {e}")

        # Final live verification status
        if _live_ver:
            if _live_ver == "pass":
                st.success("🎉 Live system verification **PASSED** — incident resolved, environment stable. Drift audit written.")
            else:
                st.warning(f"⚠️ Live system verification: **{_live_ver.upper()}** — monitor closely. Snapshot retained in audit.")

    with st.expander("4) Playbook Draft", expanded=resolution_done and not playbook_done and not auto_learn_done):
        playbook_state["name"] = st.text_input("Playbook Name", value=playbook_state["name"], key=f"playbook_name_{candidate_id}_{key_suffix}", disabled=playbook_done or auto_learn_done)
        playbook_state["classification"] = st.text_input("Playbook Classification", value=playbook_state["classification"], key=f"playbook_classification_{candidate_id}_{key_suffix}", disabled=playbook_done or auto_learn_done)
        playbook_state["description"] = st.text_area("Description", value=playbook_state["description"], key=f"playbook_description_{candidate_id}_{key_suffix}", height=100, disabled=playbook_done or auto_learn_done)
        playbook_state["steps_text"] = st.text_area("Playbook Steps (one per line)", value=playbook_state["steps_text"], key=f"playbook_steps_{candidate_id}_{key_suffix}", height=140, disabled=playbook_done or auto_learn_done)
        playbook_state["scripts_text"] = st.text_area("Scripts / Commands (one per line)", value=playbook_state["scripts_text"], key=f"playbook_scripts_{candidate_id}_{key_suffix}", height=100, disabled=playbook_done or auto_learn_done)
        col1, col2, col3 = st.columns(3)
        with col1:
            playbook_state["approval_mode"] = st.selectbox("Approval Mode", options=["manual_approval_required", "manual", "auto_approval_required"], index=["manual_approval_required", "manual", "auto_approval_required"].index(playbook_state["approval_mode"]), key=f"playbook_approval_mode_{candidate_id}_{key_suffix}", disabled=playbook_done or auto_learn_done)
        with col2:
            playbook_state["rollback_available"] = st.checkbox("Rollback Available", value=bool(playbook_state["rollback_available"]), key=f"playbook_rollback_{candidate_id}_{key_suffix}", disabled=playbook_done or auto_learn_done)
        with col3:
            playbook_state["risk_tier"] = st.selectbox("Risk Tier", options=["low", "medium", "high"], index=["low", "medium", "high"].index(playbook_state["risk_tier"]), key=f"playbook_risk_tier_{candidate_id}_{key_suffix}", disabled=playbook_done or auto_learn_done)
        if st.button("Save Playbook", key=f"save_playbook_{candidate_id}_{key_suffix}", width="stretch", disabled=(not resolution_done or playbook_done or auto_learn_done)):
            try:
                resp = create_playbook_record({
                    "name": playbook_state["name"],
                    "classification": system_label(playbook_state["classification"]),
                    "description": playbook_state["description"],
                    "steps": lines_to_list(playbook_state["steps_text"]),
                    "scripts": lines_to_list(playbook_state["scripts_text"]),
                    "approval_mode": playbook_state["approval_mode"],
                    "rollback_available": bool(playbook_state["rollback_available"]),
                    "risk_tier": playbook_state["risk_tier"],
                })
                st.session_state.workflow_saved_playbook_ids[candidate_id] = resp.get("playbook", {}).get("playbook_id", "saved")
                st.rerun()
            except Exception as e:
                st.error(f"Playbook save failed: {e}")

    with st.expander("5) Re-test Learned Incident", expanded=(playbook_done or auto_learn_done)):
        if st.button("Re-test Incident", key=f"retest_incident_{candidate_id}_{key_suffix}", width="stretch", disabled=not (playbook_done or auto_learn_done)):
            try:
                rerun_result = call_backend_with_text(
                    ticket,
                    use_fallback=True,
                    force_fallback=True,
                    tenant_id=st.session_state.get("tenant_id", "client_c"),
                    knowledge_scope=st.session_state.get("knowledge_scope", "client_private"),
                )
                st.session_state.messages.append({"role": "assistant", "result": rerun_result, "ticket": ticket})
                persist_last_incident_result(rerun_result, ticket)
                st.success("Re-test complete. Scroll below to see the new result.")
            except Exception as e:
                st.error(f"Re-test failed: {e}")


def _render_error_tolerance_inline(ci_name: str, classification: str) -> None:
    """Inline error tolerance panel — shown in Incident Console after analysis."""
    try:
        lookup = ci_name or classification
        if not lookup:
            return
        resp = SESSION.get(
            f"{BACKEND_URL}/tolerance/{lookup}",
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        policy = data.get("policy", {})
        tier = data.get("tier", "unknown")
        tier_colors = {
            "prod-standalone": "#dc2626", "prod-redundant": "#ea580c",
            "prod-ha": "#d97706", "non-prod": "#16a34a",
            "dr-standby": "#2563eb", "unknown": "#6b7280",
        }
        color = tier_colors.get(tier, "#6b7280")
        tol_min = policy.get("tolerance_minutes", "—")
        auto_rem = "✅ Yes" if policy.get("auto_remediate") else "❌ No"
        approval = "Required" if policy.get("approval_required") else "Not required"
        escalation = policy.get("escalation", "—")
        st.markdown(
            f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid {color};'
            f'border-radius:6px;padding:10px 14px;margin:8px 0;">'
            f'<div style="font-size:0.75rem;font-weight:700;color:{color};margin-bottom:6px;">'
            f'🛡️ ERROR TOLERANCE — '
            f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:4px;">{tier.upper()}</span>'
            f'</div>'
            f'<div style="display:flex;gap:24px;flex-wrap:wrap;font-size:0.78rem;color:#475569;">'
            f'<span><strong>Tolerance window:</strong> {tol_min} min</span>'
            f'<span><strong>Auto-remediate:</strong> {auto_rem}</span>'
            f'<span><strong>Approval:</strong> {approval}</span>'
            f'<span><strong>Escalation:</strong> {escalation}</span>'
            f'</div></div>',
            unsafe_allow_html=True,
        )
    except Exception:
        pass


def render_assistant_result(result: dict, ticket: str, key_suffix: str):
    result = apply_ui_telemetry_rca_display_guardrail(result)
    # Normalize confidence to dict — older stored results may have a raw float
    _raw_conf = result.get("confidence")
    if not isinstance(_raw_conf, dict):
        _scalar = float(_raw_conf) if isinstance(_raw_conf, (int, float)) else 0.0
        result = dict(result, confidence={"overall_confidence": _scalar, "primary_confidence": _scalar, "fallback_confidence": 0.0})
    render_key_metrics(result)
    # Confidence meter — visual progress bar for overall confidence
    overall_conf = (result.get("confidence") or {}).get("overall_confidence") or \
                   (result.get("analysis") or {}).get("confidence") or 0.0
    render_confidence_meter(overall_conf)
    render_incident_action_banner(result)
    render_status_banners(result)
    st.markdown("---")
    render_action_panels(result)
    _intel_cls = (result.get("analysis") or {}).get("classification", "")
    _intel_tid = st.session_state.get("tenant_id", "client_c")
    render_agent_timeline(result)
    render_historical_outcome_evidence(_intel_cls, _intel_tid)
    render_pattern_intelligence_card(_intel_cls, _intel_tid)
    render_autonomous_gate_badge(_intel_cls, _intel_tid)
    render_engineer_runbook_panel(result, ticket, key_suffix)
    # ── Error Tolerance check — show CI criticality & tolerance policy ──────
    _ci_name = (
        (result.get("analysis") or {}).get("ci_name")
        or (result.get("analysis") or {}).get("cmdb_ci")
        or (result.get("analysis") or {}).get("configuration_item")
        or ""
    )
    _classification = (result.get("analysis") or {}).get("classification", "")
    if _ci_name or _classification:
        _render_error_tolerance_inline(_ci_name, _classification)

    render_knowledge_panels(result)
    st.markdown("---")
    render_evidence_panel(result, key_suffix)
    render_confirmation_panel(result, ticket, key_suffix)
    if st.session_state.get("show_debug_trace", False):
        st.markdown("---")
        render_debug_trace(result)
    if should_render_candidate_workflow(result):
        render_candidate_workflow(result, ticket, key_suffix)
    render_feedback(result, ticket, key_suffix)
    render_peer_insights_panel(
        incident_text=ticket,
        client_id=st.session_state.get("tenant_id", "client_c"),
        rag_context=result.get("rag_context"),
    )


def render_error_state(error_text: str):
    error_result = {
        "domain": "Unknown",
        "routing": {"method": "unknown", "decision": "error"},
        "analysis": {
            "classification": "error",
            "probable_causes": [],
            "next_checks": [],
            "recommended_actions": [],
            "confidence": 0.0,
            "escalate": True,
            "analysis_source_label": "Error",
        },
        "confidence": {},
        "fallback_used": False,
        "response_source": "error",
        "analysis_source_label": "Error",
        "retrieved_incidents": [],
    }
    render_key_metrics(error_result)
    render_status_banners(error_result)
    st.markdown("---")
    render_action_panels(error_result)
    st.error(error_text)


def run_analysis_from_ui(user_input: str):
    user_input = str(user_input or "").strip()
    if not user_input:
        return
    st.session_state.messages.append({"role": "user", "content": user_input})
    try:
        result = call_backend_with_text(
            user_input,
            use_fallback=True,
            force_fallback=bool(st.session_state.force_fallback_for_next_run),
            tenant_id=st.session_state.get("tenant_id", "client_c"),
            knowledge_scope=st.session_state.get("knowledge_scope", "client_private"),
        )
        st.session_state.messages.append({"role": "assistant", "result": result, "ticket": user_input})
        persist_last_incident_result(result, user_input)
    except Exception as e:
        st.session_state.messages.append({"role": "assistant_error", "error": str(e), "ticket": user_input})



def _demo_step_visual_html(step_idx: int, path: str, ci: str,  # noqa: C901
                           metric: str, mval: float) -> str:
    """Return a full visual illustration (HTML/SVG) for each demo step."""
    # Design system — clean light palette
    BG   = "#ffffff"   # card base
    BG2  = "#f8fafc"   # card inner / subtle sections
    BG3  = "#f1f5f9"   # alternate rows
    BD   = "#e2e8f0"   # border
    TXT  = "#1e293b"   # primary text
    DIM  = "#64748b"   # secondary text
    BLUE  = "#2563eb"
    GREEN = "#16a34a"
    RED   = "#dc2626"
    AMBER = "#d97706"
    # Path accent — amber for human-approval path, blue for auto path
    pc = AMBER if path == "A" else BLUE

    def screen(title_icon, title_text, body):
        return (
            f"<div style='background:{BG};border:1px solid {BD};border-radius:12px;"
            f"overflow:hidden;margin-bottom:4px;box-shadow:0 1px 4px rgba(0,0,0,.06)'>"
            f"<div style='background:{BG2};border-bottom:1px solid {BD};padding:8px 14px;"
            f"display:flex;align-items:center;gap:8px'>"
            f"<span style='display:flex;gap:5px'>"
            f"<span style='width:9px;height:9px;border-radius:50%;background:#d1d5db;display:inline-block'></span>"
            f"<span style='width:9px;height:9px;border-radius:50%;background:#d1d5db;display:inline-block'></span>"
            f"<span style='width:9px;height:9px;border-radius:50%;background:#d1d5db;display:inline-block'></span>"
            f"</span>"
            f"<span style='font-size:.68rem;color:{DIM};font-weight:600'>{title_icon} {title_text}</span>"
            f"</div>"
            f"<div style='padding:18px 20px'>{body}</div>"
            f"</div>"
        )

    def badge(text, color):
        palettes = {
            "red":   (RED,   "rgba(220,38,38,.08)",  "rgba(220,38,38,.2)"),
            "amber": (AMBER, "rgba(217,119,6,.08)",  "rgba(217,119,6,.2)"),
            "green": (GREEN, "rgba(22,163,74,.08)",  "rgba(22,163,74,.2)"),
            "blue":  (BLUE,  "rgba(37,99,235,.08)",  "rgba(37,99,235,.2)"),
            "gray":  ("#64748b", "rgba(100,116,139,.06)", "rgba(100,116,139,.18)"),
        }
        fg, bg, bd = palettes.get(color, palettes["gray"])
        return (f"<span style='display:inline-block;padding:2px 8px;border-radius:4px;"
                f"font-size:.63rem;font-weight:700;letter-spacing:.05em;text-transform:uppercase;"
                f"background:{bg};color:{fg};border:1px solid {bd}'>{text}</span>")

    def stat_card(value, label, color=TXT):
        return (
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:12px;text-align:center'>"
            f"<div style='font-size:.9rem;font-weight:800;color:{color}'>{value}</div>"
            f"<div style='font-size:.58rem;color:{DIM};text-transform:uppercase;letter-spacing:.06em;margin-top:3px'>{label}</div>"
            f"</div>"
        )

    def kv_row(k, v, v_color=TXT, border=True):
        border_style = f"border-bottom:1px solid {BD};" if border else ""
        return (
            f"<div style='display:flex;align-items:center;justify-content:space-between;"
            f"padding:7px 0;{border_style}'>"
            f"<span style='font-size:.7rem;color:{DIM}'>{k}</span>"
            f"<span style='font-size:.7rem;font-weight:600;color:{v_color}'>{v}</span></div>"
        )

    # ── Step 0: CMDB Seed ────────────────────────────────────────────────────
    if step_idx == 0:
        cards = [
            ("linux-lab-02", "App tier",     "Standalone",    RED,   "standalone",    "CRITICAL"),
            ("linux-lab-01", "Web tier",     "Active-Active", GREEN, "active-active", "REDUNDANT"),
            ("network-lab-01","Network tier","Active-Passive", BLUE,  "active-passive","MEDIUM"),
        ]
        html = "<div style='display:grid;grid-template-columns:repeat(3,1fr);gap:10px'>"
        for name, tier, redundancy, col, _, crit in cards:
            html += (
                f"<div style='background:{BG2};border:1px solid {BD};border-radius:10px;padding:16px;text-align:center'>"
                f"<div style='font-size:1.6rem;margin-bottom:8px'>🖥️</div>"
                f"<div style='font-size:.76rem;font-weight:700;color:{TXT};margin-bottom:3px'>{name}</div>"
                f"<div style='font-size:.6rem;color:{DIM};margin-bottom:10px'>{tier} · {redundancy}</div>"
                f"<div style='background:rgba(0,0,0,.3);border:1px solid {BD};border-radius:4px;"
                f"padding:3px 0;font-size:.6rem;font-weight:700;color:{col}'>{crit}</div>"
                f"</div>"
            )
        html += "</div>"
        footer = (
            f"<div style='margin-top:12px;padding:10px 14px;background:{BG2};border:1px solid {BD};"
            f"border-radius:8px;display:flex;align-items:center;gap:8px'>"
            f"<span style='font-size:.65rem;color:{GREEN};font-weight:700'>✓ 3 CIs registered</span>"
            f"<span style='font-size:.6rem;color:{DIM};margin-left:auto'>CMDB · production environment</span>"
            f"</div>"
        )
        return screen("🌱", "CMDB — Register Lab Machines", html + footer)

    # ── Step 1: Alert / Metric spike ─────────────────────────────────────────
    if step_idx == 1:
        is_cpu = metric == "cpu_percent"
        unit = "%" if is_cpu else "ms"
        metric_label = "CPU Usage" if is_cpu else "API Latency"
        threshold = 80 if is_cpu else 2000
        max_val = 100 if is_cpu else 4000
        def to_y(v): return int(100 - (v / max_val) * 100)
        if is_cpu:
            pts = [(0,42),(40,45),(80,43),(120,46),(160,44),(185,52),(200,72),(210,98),(220,mval),(240,85),(270,60),(300,50),(340,48)]
        else:
            pts = [(0,800),(40,820),(80,790),(120,810),(160,830),(185,900),(200,1400),(210,mval),(225,2800),(240,1800),(270,1200),(310,900),(340,850)]
        path_d = " ".join(f"{'M' if i==0 else 'L'}{x},{to_y(v)}" for i,(x,v) in enumerate(pts))
        fill_d = path_d + " L340,100 L0,100 Z"
        thresh_y = to_y(threshold)
        spike_x, spike_v = pts[10][0], pts[10][1]
        spike_y = to_y(spike_v)
        svg = (
            f"<svg viewBox='0 0 340 110' xmlns='http://www.w3.org/2000/svg' style='width:100%;height:110px'>"
            f"<defs><linearGradient id='lg' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0%' stop-color='{BLUE}' stop-opacity='.2'/>"
            f"<stop offset='100%' stop-color='{BLUE}' stop-opacity='.01'/></linearGradient></defs>"
            f"<path d='{fill_d}' fill='url(#lg)'/>"
            f"<line x1='0' y1='{thresh_y}' x2='340' y2='{thresh_y}' stroke='{RED}' stroke-width='1' stroke-dasharray='4,3' opacity='.5'/>"
            f"<text x='4' y='{thresh_y-3}' fill='{RED}' font-size='7' opacity='.7'>threshold {threshold}{unit}</text>"
            f"<path d='{path_d}' fill='none' stroke='{BLUE}' stroke-width='2'/>"
            f"<circle cx='{spike_x}' cy='{spike_y}' r='4' fill='{RED}' opacity='.9'/>"
            f"<rect x='{spike_x-26}' y='{spike_y-22}' width='52' height='16' rx='3' fill='{RED}' opacity='.85'/>"
            f"<text x='{spike_x}' y='{spike_y-11}' fill='#fff' font-size='8' font-weight='700' text-anchor='middle'>{mval}{unit}</text>"
            f"</svg>"
        )
        alert_row = (
            f"<div style='display:flex;align-items:center;justify-content:space-between;"
            f"margin-top:10px;padding:10px 14px;background:{BG2};border:1px solid {BD};"
            f"border-left:3px solid {RED};border-radius:0 6px 6px 0'>"
            f"<div><div style='font-size:.72rem;font-weight:700;color:{RED}'>⚡ ALERT FIRED</div>"
            f"<div style='font-size:.63rem;color:{DIM};margin-top:2px'>{ci} · {metric} · prometheus</div></div>"
            f"{badge('CRITICAL' if path == 'A' else 'HIGH', 'red' if path == 'A' else 'amber')}"
            f"</div>"
        )
        body = (
            f"<div style='font-size:.62rem;color:{DIM};margin-bottom:6px'>"
            f"prometheus → {metric_label} — {ci}</div>"
            f"{svg}{alert_row}"
        )
        return screen("🚨", "Prometheus — Threshold Breach", body)

    # ── Step 2: Incident Console ─────────────────────────────────────────────
    if step_idx == 2:
        sev_col = "red" if path == "A" else "amber"
        sev_text = "CRITICAL" if path == "A" else "HIGH"
        svc_name = "claims-api" if path == "A" else "web-frontend"
        munit = "%" if metric == "cpu_percent" else "ms"
        body = (
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:10px;padding:16px 18px;margin-bottom:12px'>"
            f"<div style='display:flex;align-items:flex-start;gap:12px'>"
            f"<span style='font-size:1.5rem;flex-shrink:0'>🚨</span>"
            f"<div style='flex:1'>"
            f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:8px'>"
            f"{badge(sev_text, sev_col)}"
            f"<span style='font-size:.68rem;color:{DIM}'>INC-0042</span>"
            f"<span style='font-size:.68rem;color:{DIM};margin-left:auto'>● Just now</span>"
            f"</div>"
            f"<div style='font-size:.88rem;font-weight:700;color:{TXT};margin-bottom:5px'>"
            f"{'CPU Saturation Detected' if path=='A' else 'API Latency Spike Detected'}</div>"
            f"<div style='font-size:.7rem;color:{DIM}'>Resource: {ci} · Service: {svc_name}</div>"
            f"<div style='font-size:.7rem;color:{DIM};margin-top:3px'>"
            f"Metric: {metric} = <b style='color:{RED}'>{mval}{munit}</b></div>"
            f"</div></div></div>"
            f"<div style='display:grid;grid-template-columns:repeat(3,1fr);gap:8px'>"
            + stat_card("OPEN", "Status", TXT)
            + stat_card("P1" if path=="A" else "P2", "Priority", RED if path=="A" else AMBER)
            + stat_card("0 min", "MTTR", DIM)
            + "</div>"
        )
        return screen("🔔", "Incident Console — New Incident", body)

    # ── Step 3: AI Analysis ──────────────────────────────────────────────────
    if step_idx == 3:
        conf_pct = 91 if path == "A" else 84
        root_cause = ("CPU runaway process — claims-api thread pool exhausted"
                      if path == "A" else
                      "API gateway connection pool saturation — nginx upstream timeout")
        body = (
            f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px'>"
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:14px'>"
            f"<div style='font-size:.6rem;color:{DIM};text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px'>Classification</div>"
            f"{badge('HIGH SEVERITY', 'red')}"
            f"</div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:14px'>"
            f"<div style='font-size:.6rem;color:{DIM};text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px'>Confidence</div>"
            f"<div style='font-size:1.2rem;font-weight:800;color:{BLUE}'>{conf_pct}%</div>"
            f"</div></div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:14px;margin-bottom:10px'>"
            f"<div style='display:flex;justify-content:space-between;margin-bottom:8px'>"
            f"<span style='font-size:.62rem;color:{DIM}'>AI Confidence Score</span>"
            f"<span style='font-size:.62rem;color:{BLUE};font-weight:700'>{conf_pct}%</span></div>"
            f"<div style='height:6px;background:{BG3};border-radius:3px'>"
            f"<div style='width:{conf_pct}%;height:100%;background:{BLUE};border-radius:3px;opacity:.85'></div></div></div>"
            f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px'>"
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:14px'>"
            f"<div style='font-size:.6rem;color:{DIM};text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px'>👥 Peer Matches</div>"
            f"<div style='font-size:1.1rem;font-weight:800;color:{TXT}'>3</div>"
            f"<div style='font-size:.63rem;color:{DIM};margin-top:2px'>similar incidents · avg MTTR 11 min</div>"
            f"</div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:14px'>"
            f"<div style='font-size:.6rem;color:{DIM};text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px'>📋 Runbook</div>"
            f"<div style='font-size:1.1rem;font-weight:800;color:{TXT}'>8 steps</div>"
            f"<div style='font-size:.63rem;color:{DIM};margin-top:2px'>Ansible playbook ready</div>"
            f"</div></div>"
            f"<div style='padding:10px 14px;background:{BG2};border-left:3px solid {BLUE};border-radius:0 6px 6px 0'>"
            f"<div style='font-size:.6rem;color:{DIM};margin-bottom:3px'>Root Cause</div>"
            f"<div style='font-size:.74rem;color:{TXT}'>{root_cause}</div>"
            f"</div>"
        )
        return screen("🧠", "AI Analysis — Classification & Insights", body)

    # ── Step 4: Error Tolerance ──────────────────────────────────────────────
    if step_idx == 4:
        tier = "prod-standalone" if path == "A" else "prod-ha"
        verdict = "HUMAN APPROVAL REQUIRED" if path == "A" else "AUTO-EXECUTION ELIGIBLE"
        v_icon = "👤" if path == "A" else "⚡"
        v_desc = ("Standalone critical system — engineer sign-off required before any live change."
                  if path == "A" else
                  "Redundant active-active system — policy permits autonomous execution.")
        policy_rows = [
            ("Tier",             tier,                             "amber" if path=="A" else "blue"),
            ("Auto-Remediate",   "No" if path=="A" else "Yes",    "amber" if path=="A" else "green"),
            ("Approval Required","Yes" if path=="A" else "No",    "amber" if path=="A" else "green"),
            ("Escalation",       "P1" if path=="A" else "P2",     "red"   if path=="A" else "gray"),
            ("Tolerance Window", "0 min" if path=="A" else "10 min", "gray"),
        ]
        rows_html = "".join(
            f"<div style='display:flex;align-items:center;justify-content:space-between;"
            f"padding:7px 0;border-bottom:1px solid {BD}'>"
            f"<span style='font-size:.7rem;color:{DIM}'>{k}</span>"
            f"{badge(v, c)}</div>"
            for k, v, c in policy_rows
        )
        verdict_box = (
            f"<div style='margin-top:12px;padding:14px;text-align:center;"
            f"background:{BG2};border:1px solid {pc};border-radius:10px'>"
            f"<div style='font-size:1.4rem'>{v_icon}</div>"
            f"<div style='font-size:.85rem;font-weight:800;color:{pc};margin-top:6px'>{verdict}</div>"
            f"<div style='font-size:.68rem;color:{DIM};margin-top:6px;line-height:1.5'>{v_desc}</div>"
            f"</div>"
        )
        body = (
            f"<div style='font-size:.68rem;color:{DIM};margin-bottom:10px'>"
            f"CMDB lookup → {ci} → tier: <b style='color:{TXT}'>{tier}</b></div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:12px 16px'>"
            f"{rows_html}</div>"
            f"{verdict_box}"
        )
        return screen("⚖️", "Error Tolerance — CMDB Policy Evaluation", body)

    # ── Step 5: Safety Gate ──────────────────────────────────────────────────
    if step_idx == 5:
        checks = [
            ("🔍", "Drift State",        "No conflicting changes"),
            ("📊", "SLO Budget",         "Error budget available"),
            ("🔄", "Concurrent Activity","No parallel remediations"),
            ("🕐", "Change Window",      "Within permitted hours"),
            ("↩️", "Rollback Plan",      "Verified and ready"),
            ("💥", "Blast Radius",       "Contained to single CI"),
        ]
        check_html = "<div style='display:grid;grid-template-columns:1fr 1fr;gap:8px'>"
        for icon, name, detail in checks:
            check_html += (
                f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:12px'>"
                f"<div style='display:flex;align-items:center;justify-content:space-between;margin-bottom:4px'>"
                f"<span style='font-size:.72rem;color:{TXT};font-weight:600'>{icon} {name}</span>"
                f"<span style='font-size:.6rem;color:{GREEN};font-weight:700'>PASS</span></div>"
                f"<div style='font-size:.61rem;color:{DIM}'>{detail}</div>"
                f"</div>"
            )
        check_html += "</div>"
        gate_banner = (
            f"<div style='margin-top:10px;padding:10px 16px;display:flex;align-items:center;gap:10px;"
            f"background:{BG2};border:1px solid {BD};border-left:3px solid {GREEN};border-radius:0 6px 6px 0'>"
            f"<span style='font-size:1.1rem'>✅</span>"
            f"<div><div style='font-size:.78rem;font-weight:700;color:{GREEN}'>Safety Gate PASSED</div>"
            f"<div style='font-size:.63rem;color:{DIM}'>6 of 6 checks passed · 0 blockers · Safe to proceed</div>"
            f"</div></div>"
        )
        body = check_html + gate_banner
        return screen("🛡️", "Safety Gate — Pre-Execution Checks", body)

    # ── Step 6: Approve / Auto-approve ───────────────────────────────────────
    if step_idx == 6:
        if path == "A":
            body = (
                f"<div style='background:{BG2};border:1px solid {BD};border-radius:10px;padding:16px;margin-bottom:12px'>"
                f"<div style='font-size:.6rem;color:{DIM};margin-bottom:8px;text-transform:uppercase;letter-spacing:.06em'>Pending Review</div>"
                f"<div style='font-size:.85rem;font-weight:700;color:{TXT};margin-bottom:10px'>INC-0042 — CPU Saturation</div>"
                f"<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px'>"
                + "".join(
                    f"<div style='text-align:center;background:{BG3};border-radius:6px;padding:8px'>"
                    f"<div style='font-size:.72rem;font-weight:700;color:{TXT}'>{v}</div>"
                    f"<div style='font-size:.58rem;color:{DIM};margin-top:2px'>{l}</div></div>"
                    for v, l in [("HIGH","Severity"),("6 / 6","Safety Gate"),("91%","AI Confidence")]
                )
                + "</div></div>"
                f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px'>"
                f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:12px'>"
                f"<div style='font-size:.62rem;color:{DIM};margin-bottom:8px;text-transform:uppercase;letter-spacing:.06em'>Evidence Package</div>"
                + "".join(
                    f"<div style='font-size:.68rem;color:{DIM};padding:4px 0;border-bottom:1px solid {BD}'>"
                    f"<span style='color:{GREEN};margin-right:5px'>✓</span>{e}</div>"
                    for e in ["AI Analysis","Safety Gate Report","Peer Insights","Runbook Preview","Drift State"]
                )
                + "</div>"
                f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:12px;display:flex;"
                f"flex-direction:column;align-items:center;justify-content:center;text-align:center'>"
                f"<div style='font-size:1.8rem'>👤</div>"
                f"<div style='font-size:.72rem;font-weight:700;color:{AMBER};margin-top:8px'>Awaiting Approval</div>"
                f"<div style='font-size:.61rem;color:{DIM};margin-top:4px'>Go to Automation Queue</div>"
                f"</div></div>"
                f"<div style='background:{BG2};border:1px solid {BD};border-left:3px solid {AMBER};"
                f"border-radius:0 6px 6px 0;padding:10px 16px'>"
                f"<div style='font-size:.72rem;color:{AMBER};font-weight:700'>🔐 Execution gated — human decision required</div>"
                f"<div style='font-size:.63rem;color:{DIM};margin-top:3px'>No live action until an engineer approves this change</div>"
                f"</div>"
            )
            return screen("👤", "Automation Queue — Awaiting Approval", body)
        else:
            body = (
                f"<div style='text-align:center;padding:14px 0 16px'>"
                f"<div style='font-size:2rem;margin-bottom:10px'>⚡</div>"
                f"<div style='font-size:.95rem;font-weight:800;color:{GREEN};margin-bottom:6px'>Policy Auto-Approved</div>"
                f"<div style='font-size:.72rem;color:{DIM};max-width:280px;margin:0 auto;line-height:1.6'>"
                f"{ci} → active-active redundant → error tolerance policy permits autonomous execution</div>"
                f"</div>"
                f"<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px'>"
                + stat_card("prod-ha", "Tier")
                + stat_card("0", "Human Actions", DIM)
                + stat_card("Logged", "Audit Trail", GREEN)
                + "</div>"
                f"<div style='background:{BG2};border:1px solid {BD};border-left:3px solid {GREEN};"
                f"border-radius:0 6px 6px 0;padding:10px 16px'>"
                f"<div style='font-size:.68rem;color:{GREEN};font-weight:700;margin-bottom:3px'>✓ Auto-approval recorded in audit log</div>"
                f"<div style='font-size:.63rem;color:{DIM}'>"
                f"Decision basis: error_tolerance_policy · tier: prod-ha · safety_gate: passed · blockers: 0</div>"
                f"</div>"
            )
            return screen("⚡", "Automation Queue — Policy Decision", body)

    # ── Step 7: Drift Snapshot ───────────────────────────────────────────────
    if step_idx == 7:
        metric_disp = f"{mval}{'%' if metric=='cpu_percent' else 'ms'}"
        svc = "claims-api" if path == "A" else "web-frontend"
        body = (
            f"<div style='display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;margin-bottom:12px'>"
            f"<div style='background:{BG2};border:1px solid {BD};border-left:3px solid {RED};border-radius:0 8px 8px 0;padding:14px'>"
            f"<div style='font-size:.6rem;color:{RED};font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px'>Current (Broken)</div>"
            + kv_row(metric.replace("_"," "), metric_disp, RED)
            + kv_row("service", svc)
            + kv_row("status", "DEGRADED", RED, border=False)
            + "</div>"
            f"<div style='text-align:center'>"
            f"<div style='font-size:1.4rem'>📸</div>"
            f"<div style='font-size:.55rem;color:{DIM};margin-top:4px;white-space:nowrap'>snapshot<br>captured</div>"
            f"</div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-left:3px solid {BLUE};border-radius:0 8px 8px 0;padding:14px'>"
            f"<div style='font-size:.6rem;color:{BLUE};font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px'>Clone (Test Env)</div>"
            + kv_row(metric.replace("_"," "), metric_disp, DIM)
            + kv_row("service", svc)
            + kv_row("status", "SNAPSHOT", BLUE, border=False)
            + "</div></div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:10px'>"
            f"<span style='font-size:1.1rem'>🔒</span>"
            f"<div><div style='font-size:.72rem;font-weight:700;color:{TXT}'>Baseline Locked</div>"
            f"<div style='font-size:.63rem;color:{DIM};margin-top:2px'>Fix applied to clone only. Production untouched until verification passes.</div>"
            f"</div></div>"
        )
        return screen("📸", "Drift Management — Environment Snapshot", body)

    # ── Step 8: Verify ───────────────────────────────────────────────────────
    if step_idx == 8:
        munit = "%" if metric == "cpu_percent" else "ms"
        fixed_val = f"{'42%' if metric=='cpu_percent' else '180ms'}"
        body = (
            f"<div style='display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;margin-bottom:12px'>"
            f"<div style='background:{BG2};border:1px solid {BD};border-left:3px solid {RED};border-radius:0 8px 8px 0;padding:14px'>"
            f"<div style='font-size:.6rem;color:{RED};font-weight:700;margin-bottom:8px;text-transform:uppercase'>Before Fix</div>"
            + kv_row(metric.replace("_"," "), f"{mval}{munit}", RED)
            + kv_row("status", "DEGRADED", RED, border=False)
            + "</div>"
            f"<div style='text-align:center'>"
            f"<div style='font-size:1.6rem'>→</div>"
            f"<div style='font-size:.65rem;font-weight:800;color:{GREEN};margin-top:4px'>PASS</div>"
            f"</div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-left:3px solid {GREEN};border-radius:0 8px 8px 0;padding:14px'>"
            f"<div style='font-size:.6rem;color:{GREEN};font-weight:700;margin-bottom:8px;text-transform:uppercase'>After Fix</div>"
            + kv_row(metric.replace("_"," "), fixed_val, GREEN)
            + kv_row("status", "HEALTHY", GREEN, border=False)
            + "</div></div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-left:3px solid {GREEN};"
            f"border-radius:0 8px 8px 0;padding:14px 18px'>"
            f"<div style='font-size:.82rem;font-weight:800;color:{GREEN};margin-bottom:4px'>🔓 Live Gate Unlocked</div>"
            f"<div style='font-size:.68rem;color:{DIM}'>Fix verified on snapshot clone. Safe to apply to the live {ci} system.</div>"
            f"</div>"
        )
        return screen("✅", "Drift Management — Snapshot Verification", body)

    # ── Step 9: Execute ──────────────────────────────────────────────────────
    if step_idx == 9:
        playbook = "remediate_app_dependency.yml" if path == "A" else "remediate_web_nginx.yml"
        tasks = [
            ("TASK [Identify runaway process]",   "ok"),
            ("TASK [Kill high-CPU threads]",       "ok"),
            ("TASK [Clear application cache]",     "ok"),
            ("TASK [Restart service]",             "ok"),
            ("TASK [Validate health endpoint]",    "running"),
        ]
        def _task_row(t, s):
            col = "#4ade80" if s == "ok" else "#fbbf24"
            sym = "✓" if s == "ok" else "▶"
            return (f"<div style='font-size:.68rem;font-family:monospace;padding:3px 0;color:{col}'>"
                    f"{sym} {t} [{s.upper()}]</div>")
        terminal_rows = "".join(_task_row(t, s) for t, s in tasks)
        body = (
            f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:12px'>"
            f"<span style='font-size:.7rem;color:{DIM}'>Playbook:</span>"
            f"<code style='font-size:.7rem;color:{TXT};background:{BG2};border:1px solid {BD};padding:2px 8px;border-radius:4px'>{playbook}</code>"
            f"<span style='margin-left:auto;font-size:.62rem;color:{DIM}'>target: {ci}</span>"
            f"</div>"
            f"<div style='background:#1e293b;border:1px solid #334155;border-radius:8px;padding:14px;font-family:monospace'>"
            f"<div style='font-size:.63rem;color:#64748b;margin-bottom:8px'>ansible-runner · live output</div>"
            f"{terminal_rows}"
            f"<div style='margin-top:10px;height:4px;background:#334155;border-radius:2px'>"
            f"<div style='width:80%;height:100%;background:{BLUE};border-radius:2px;opacity:.8'></div></div>"
            f"<div style='font-size:.61rem;color:#64748b;margin-top:4px'>4 / 5 tasks complete</div>"
            f"</div>"
        )
        return screen("⚙️", "Ansible Runner — Playbook Executing", body)

    # ── Step 10: Governance ──────────────────────────────────────────────────
    if step_idx == 10:
        body = (
            f"<div style='display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px'>"
            + stat_card("12 min", "MTTR", BLUE)
            + stat_card("SLA MET", "Compliance", GREEN)
            + stat_card("CLOSED", "Incident", GREEN)
            + stat_card("P1 → Resolved", "Escalation", TXT)
            + "</div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-radius:8px;padding:14px;margin-bottom:10px'>"
            f"<div style='font-size:.6rem;color:{DIM};text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px'>Evidence Package</div>"
            + "".join(
                f"<div style='display:flex;align-items:center;justify-content:space-between;"
                f"padding:6px 0;border-bottom:1px solid {BD}'>"
                f"<span style='font-size:.68rem;color:{DIM}'>{item}</span>"
                f"<span style='font-size:.63rem;color:{GREEN};font-weight:700'>✓ Recorded</span></div>"
                for item in ["Approval Event Log", "Ansible Execution Log", "Before/After Drift Diff",
                             "MTTR & SLA Report", "Peer Benchmark"]
            ) + "</div>"
            f"<div style='background:{BG2};border:1px solid {BD};border-left:3px solid {GREEN};"
            f"border-radius:0 6px 6px 0;padding:10px 16px'>"
            f"<span style='font-size:.72rem;color:{GREEN};font-weight:700'>🏛️ Audit-ready — full evidence chain recorded</span>"
            f"</div>"
        )
        return screen("🏛️", "Governance — Incident Closed", body)

    return ""


def _demo_result_html(step_idx: int, result: dict, path: str, ci: str,  # noqa: C901
                      metric: str, mval: float) -> str:
    """Return a rich HTML card for the result of a given demo step."""
    if not result:
        return ""
    ok = result.get("ok", False)
    det = result.get("details", {}) or {}

    def badge(text, color):  # color: green | red | amber | blue | purple
        cols = {
            "green": ("rgba(22,163,74,.08)",  "#16a34a", "rgba(22,163,74,.2)"),
            "red":   ("rgba(220,38,38,.08)",  "#dc2626", "rgba(220,38,38,.2)"),
            "amber": ("rgba(217,119,6,.08)",  "#d97706", "rgba(217,119,6,.2)"),
            "blue":  ("rgba(37,99,235,.08)",  "#2563eb", "rgba(37,99,235,.2)"),
            "purple":("rgba(124,58,237,.08)", "#7c3aed", "rgba(124,58,237,.2)"),
        }
        bg, fg, bd = cols.get(color, cols["blue"])
        return (f"<span style='display:inline-block;padding:3px 10px;border-radius:4px;"
                f"font-size:.68rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;"
                f"background:{bg};color:{fg};border:1px solid {bd}'>{text}</span>")

    def metric_card(val, lbl):
        return (f"<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 14px'>"
                f"<div style='font-size:.95rem;font-weight:700;color:#1e293b'>{val}</div>"
                f"<div style='font-size:.62rem;color:#64748b;margin-top:3px;text-transform:uppercase;"
                f"letter-spacing:.06em'>{lbl}</div></div>")

    def check_row(icon, label, sublabel=""):
        sub = f"<div style='font-size:.65rem;color:#64748b'>{sublabel}</div>" if sublabel else ""
        return (f"<div style='display:flex;align-items:flex-start;gap:10px;padding:8px 0;"
                f"border-bottom:1px solid #e2e8f0'>"
                f"<span style='font-size:1rem;flex-shrink:0;margin-top:1px'>{icon}</span>"
                f"<div><div style='font-size:.75rem;color:#1e293b'>{label}</div>{sub}</div></div>")

    def wrap(title, body):
        return (f"<div style='background:#fff;border:1px solid #e2e8f0;border-radius:10px;"
                f"padding:18px 20px;margin-top:14px;box-shadow:0 1px 4px rgba(0,0,0,.05)'>"
                f"<div style='font-size:.6rem;font-weight:700;letter-spacing:.12em;"
                f"text-transform:uppercase;color:#94a3b8;margin-bottom:14px'>{title}</div>"
                f"{body}</div>")

    def grid(*cards):
        inner = "".join(cards)
        return f"<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:10px'>{inner}</div>"

    def note(text):
        return (f"<div style='font-size:.78rem;color:#64748b;line-height:1.65;margin-top:10px;"
                f"border-left:3px solid #e2e8f0;padding-left:12px'>{text}</div>")

    if not ok:
        err = det.get("error") or det.get("detail") or result.get("summary", "Unknown error")
        return wrap("⚠ Step Result",
                    f"<div style='color:#dc2626;font-size:.82rem'>{err}</div>"
                    f"<div style='font-size:.7rem;color:#64748b;margin-top:6px'>Check that the backend is running on port 8001.</div>")

    # ── Per-step visual renderers ─────────────────────────────────────────────
    if step_idx == 0:   # Seed
        rows = "".join(
            check_row("✅" if "registered" in v or "exists" in v else "❌", k,
                      "registered" if "registered" in v else "already exists" if "exists" in v else v)
            for k, v in det.items()
        ) if det else check_row("✅", "CIs registered in CMDB")
        return wrap("CMDB Registration", f"<div style='border-bottom:1px solid #111d2e'></div>{rows}")

    if step_idx == 1:   # Alert
        unit = "%" if metric == "cpu_percent" else " ms"
        sev_color = "red" if path == "A" else "amber"
        return wrap("Telemetry Event Fired", grid(
            metric_card(f"<b style='font-size:1.3rem'>{mval}{unit}</b>", metric.replace("_", " ").title()),
            metric_card(f"<b>{ci}</b>", "Resource"),
            metric_card(badge("FIRED", sev_color), "Status"),
        ) + note("Event delivered to the telemetry pipeline. Alert rules will evaluate on next cycle."))

    if step_idx == 2:   # Incident
        iid = det.get("incident_id") or "—"
        created = det.get("incidents_created", "?")
        sev_color = "red" if path == "A" else "amber"
        sev_text = "CRITICAL" if path == "A" else "HIGH"
        return wrap("Incident Created", grid(
            metric_card(badge(sev_text, sev_color), "Severity"),
            metric_card(f"<span style='font-size:.85rem'>{iid}</span>", "Incident ID"),
            metric_card(f"<b>{created}</b>", "Created"),
        ) + note("Structured incident record is now live in the Incident Console. AI analysis begins automatically."))

    if step_idx == 3:   # Analyse
        clf = str(det.get("classification") or det.get("severity") or "HIGH").upper()
        conf_raw = det.get("confidence_score") or det.get("confidence") or 0.91
        try:
            conf = float(conf_raw)
        except Exception:
            conf = 0.91
        conf_pct = int(conf * 100) if conf <= 1 else int(conf)
        clf_color = "red" if "HIGH" in clf else "amber" if "MED" in clf else "blue"
        reasoning = str(det.get("reasoning") or det.get("summary") or
                        "Pattern matches known CPU saturation profile. Peer incidents confirm process runaway root cause.")[:200]
        bar = (f"<div style='display:flex;align-items:center;gap:10px;margin:10px 0'>"
               f"<div style='flex:1;height:8px;background:#e2e8f0;border-radius:4px'>"
               f"<div style='width:{conf_pct}%;height:100%;background:#2563eb;border-radius:4px'></div></div>"
               f"<span style='font-size:.75rem;color:#2563eb;font-weight:700'>{conf_pct}%</span></div>")
        return wrap("AI Classification Result", grid(
            metric_card(badge(clf, clf_color), "Classification"),
            metric_card(f"<b>{conf_pct}%</b>", "Confidence"),
            metric_card(badge("Runbook Ready", "green"), "Runbook"),
        ) + f"<div style='font-size:.65rem;color:#94a3b8;margin:10px 0 2px'>Confidence Score</div>" + bar
              + note(reasoning))

    if step_idx == 4:   # Tolerance
        tier = str(det.get("tier") or ("prod-standalone" if path == "A" else "prod-ha"))
        appr = det.get("approval_required", path == "A")
        esc = str(det.get("escalation_level") or ("P1" if path == "A" else "P2"))
        verdict = ("HUMAN REQUIRED" if appr else "AUTO-ELIGIBLE")
        v_color = "amber" if appr else "green"
        reasoning = str(det.get("reasoning") or (
            f"{ci} is a standalone critical system. No auto-remediation permitted."
            if appr else
            f"{ci} is redundant (active-active). Policy permits autonomous execution."))[:200]
        return wrap("Error Tolerance Decision", grid(
            metric_card(f"<b style='font-size:.85rem'>{tier}</b>", "Tier"),
            metric_card(badge(verdict, v_color), "Decision"),
            metric_card(f"<b>{esc}</b>", "Escalation"),
        ) + note(reasoning))

    if step_idx == 5:   # Safety gate
        status = str(det.get("safety_gate_status") or det.get("status") or "passed").lower()
        blockers = det.get("blocking_checks") or []
        s_color = "green" if status == "passed" else "amber" if status == "warning" else "red"
        checks_html = "".join([
            check_row("✅" if k not in blockers else "🚫", label, "passed")
            for k, label in [
                ("drift",       "Drift state — no conflicting changes in progress"),
                ("slo",         "SLO budget — error budget available"),
                ("concurrent",  "Concurrent activity — no parallel remediations"),
                ("window",      "Change window — within permitted hours"),
                ("rollback",    "Rollback plan — verified and ready"),
                ("blast_radius","Blast radius — impact contained to single CI"),
            ]
        ])
        return wrap("Safety Gate — 6 Pre-Execution Checks", grid(
            metric_card(badge(status.upper(), s_color), "Gate Status"),
            metric_card(f"<b>{len(blockers)}</b>", "Blockers"),
            metric_card("<b>6 / 6</b>", "Checks Run"),
        ) + f"<div style='border-top:1px solid #e2e8f0;margin-top:4px'>{checks_html}</div>")

    if step_idx == 6:   # Manual: approve or auto-approve
        if path == "A":
            return wrap("⏳ Awaiting Engineer Approval",
                        f"<div style='text-align:center;padding:20px 0'>"
                        f"<div style='font-size:2rem;margin-bottom:8px'>👤</div>"
                        f"<div style='font-size:1rem;font-weight:700;color:#d97706'>Waiting for approval</div>"
                        f"<div style='font-size:.78rem;color:#64748b;margin-top:8px;max-width:320px;margin-left:auto;margin-right:auto'>"
                        f"Navigate to the Automation Queue, review the AI analysis and safety gate evidence, then click Approve.</div></div>")
        else:
            return wrap("⚡ Policy Auto-Approved",
                        f"<div style='text-align:center;padding:20px 0'>"
                        f"<div style='font-size:2rem;margin-bottom:8px'>⚡</div>"
                        f"<div style='font-size:1rem;font-weight:700;color:#16a34a'>Auto-approved by policy</div>"
                        f"<div style='font-size:.78rem;color:#64748b;margin-top:8px;max-width:320px;margin-left:auto;margin-right:auto'>"
                        f"Error tolerance policy auto-approved because {ci} is redundant (active-active). "
                        f"Approval decision is fully logged in the audit trail.</div></div>")

    if step_idx == 7:   # Snapshot
        snap_id = str(det.get("snapshot_id") or det.get("id") or "SNAP-AUTO")
        snap_status = str(det.get("status") or "captured").upper()
        return wrap("Environment Snapshot", grid(
            metric_card(f"<span style='font-size:.82rem'>{snap_id}</span>", "Snapshot ID"),
            metric_card(badge(snap_status, "blue"), "Status"),
            metric_card(badge("PRE-FIX", "amber"), "Baseline"),
        ) + note("Broken state captured. The fix will be applied and tested against this clone. "
                 "Production is not touched until verification passes."))

    if step_idx == 8:   # Verify
        res_val = str(det.get("result") or det.get("status") or "PASS").upper()
        v_ok = "FAIL" not in res_val and "ERROR" not in res_val
        icon = "✅" if v_ok else "❌"
        color = "green" if v_ok else "red"
        gate_msg = "Live gate unlocked. Safe to apply to production." if v_ok else "Gate locked. Fix did not verify — do not apply to live."
        return wrap("Snapshot Verification",
                    f"<div style='text-align:center;padding:18px 0'>"
                    f"<div style='font-size:2.8rem'>{icon}</div>"
                    f"<div style='font-size:1.4rem;font-weight:800;color:{'#16a34a' if v_ok else '#dc2626'};margin-top:6px'>{res_val}</div>"
                    f"<div style='font-size:.78rem;color:#64748b;margin-top:8px'>{gate_msg}</div></div>")

    if step_idx == 9:   # Execute
        ex_status = str(det.get("status") or "started").upper()
        playbook = str(det.get("playbook") or ("remediate_app_dependency.yml" if path == "A" else "remediate_web_nginx.yml"))
        ex_id = str(det.get("execution_id") or "—")
        return wrap("Ansible Execution", grid(
            metric_card(badge(ex_status, "green"), "Status"),
            metric_card(f"<span style='font-size:.78rem'>{playbook}</span>", "Playbook"),
            metric_card(f"<span style='font-size:.82rem'>{ex_id}</span>", "Execution ID"),
        ) + note("Playbook executing against the live container via ansible-runner. "
                 "Task output is streamed in real time in the Automation Queue."))

    if step_idx == 10:  # Governance
        mttr = str(det.get("mttr_minutes") or det.get("mttr") or "—")
        sla = str(det.get("sla_status") or det.get("sla") or "met").upper()
        sla_color = "green" if "MET" in sla or "PASS" in sla or sla == "—" else "red"
        total_inc = str(det.get("total_incidents") or det.get("count") or "—")
        return wrap("Governance Report Generated", grid(
            metric_card(f"<b>{mttr}</b>", "MTTR (min)"),
            metric_card(badge(f"SLA {sla}", sla_color), "SLA"),
            metric_card(f"<b>{total_inc}</b>", "Incidents Tracked"),
            metric_card(badge("CLOSED", "green"), "Status"),
        ) + note("Full evidence package written: approval log, Ansible output, before/after drift diff, MTTR, SLA compliance. Ready for client audit."))

    # Fallback
    return wrap("Step Result",
                f"<div style='font-size:.82rem;color:#64748b'>{result.get('summary','')}</div>")


def _render_demo_page():  # noqa: C901
    # ── Initialise demo session state ────────────────────────────────────────
    defaults = {
        "demo_step": -1,       # -1 = path not yet chosen
        "demo_path": None,     # "A" or "B"
        "demo_results": {},    # step_index → {ok, summary, details}
        "demo_incident_id": None,
        "demo_snapshot_id": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # ── Header row ───────────────────────────────────────────────────────────
    hdr_col, rst_col = st.columns([6, 1])
    with hdr_col:
        st.markdown("## 🎬 AI SRE Platform — Live Demo")
    with rst_col:
        if st.button("↺ Reset", use_container_width=True):
            for k in defaults:
                st.session_state[k] = {} if k == "demo_results" else defaults[k]
            st.rerun()

    # ── Global demo CSS ──────────────────────────────────────────────────────
    st.markdown("""<style>
/* ── Pipeline strip ──────────────────────────────────────────── */
.dp-pipe{display:flex;align-items:flex-start;gap:0;overflow-x:auto;padding:14px 0 6px}
.dp-node{display:flex;flex-direction:column;align-items:center;flex:1;min-width:60px;position:relative}
.dp-node:not(:last-child)::after{content:'';position:absolute;top:17px;left:55%;width:90%;height:2px;background:#e2e8f0;z-index:0}
.dp-node.n-done::after{background:#16a34a}
.dp-node.n-cur::after{background:#e2e8f0}
.dp-bub{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.95rem;border:2px solid #e2e8f0;background:#fff;position:relative;z-index:1;transition:all .25s}
.dp-node.n-done .dp-bub{border-color:#16a34a;background:rgba(22,163,74,.08)}
.dp-node.n-cur .dp-bub{border-color:var(--pc,#2563eb);box-shadow:0 0 0 4px rgba(37,99,235,.1),0 0 12px var(--pc,#2563eb);animation:dp-glow 1.2s ease-in-out infinite alternate}
@keyframes dp-glow{from{box-shadow:0 0 4px var(--pc,#2563eb)}to{box-shadow:0 0 14px var(--pc,#2563eb),0 0 0 6px rgba(37,99,235,.08)}}
.dp-lbl{font-size:.52rem;font-weight:600;text-align:center;color:#94a3b8;margin-top:5px;max-width:60px;line-height:1.3}
.dp-node.n-done .dp-lbl{color:#16a34a}
.dp-node.n-cur .dp-lbl{color:#1e293b}

/* ── Step card ───────────────────────────────────────────────── */
.dc-card{border-radius:14px;overflow:hidden;margin-bottom:18px;border:1px solid var(--pc,#e2e8f0);box-shadow:0 2px 8px rgba(0,0,0,.06)}
.dc-hdr{padding:22px 26px 18px;background:#fff;border-bottom:1px solid #e2e8f0;border-left:4px solid var(--pc,#2563eb)}
.dc-eyebrow{font-size:.62rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:var(--pc,#64748b);margin-bottom:6px}
.dc-title{font-size:1.25rem;font-weight:800;color:#1e293b;margin:0 0 11px;display:flex;align-items:center;gap:10px}
.dc-ctx{font-size:.83rem;line-height:1.7;color:#64748b}
.dc-body{background:#f8fafc;padding:0}

/* ── Module badge ────────────────────────────────────────────── */
.dc-mod{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:4px;font-size:.62rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;background:#f1f5f9;border:1px solid #e2e8f0;color:#64748b;margin-bottom:10px}

/* ── Completed strip ─────────────────────────────────────────── */
.dc-done-row{display:flex;align-items:center;gap:10px;padding:9px 14px;border-radius:8px;background:#f8fafc;margin-bottom:5px;border:1px solid #e2e8f0}
.dc-done-step{font-size:.68rem;font-weight:700;color:#94a3b8;min-width:52px}
.dc-done-title{font-size:.78rem;font-weight:600;color:#1e293b;flex:1}
.dc-done-sum{font-size:.68rem;color:#64748b;text-align:right;white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis}
</style>""", unsafe_allow_html=True)

    # ════════════════════════════════════════════════════════════════════════
    # PATH SELECTION SCREEN
    # ════════════════════════════════════════════════════════════════════════
    if st.session_state.demo_path is None:
        st.markdown("""
<div style='text-align:center;padding:32px 0 24px'>
  <div style='display:inline-flex;align-items:center;gap:8px;padding:4px 14px;border-radius:20px;
    background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.2);
    font-size:.68rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#60a5fa;margin-bottom:18px'>
    <span style='width:6px;height:6px;border-radius:50%;background:#3b82f6;display:inline-block;animation:dp-glow 1.2s infinite alternate'></span>
    Live Platform Demo
  </div>
  <h2 style='font-size:1.8rem;font-weight:800;color:#1e293b;margin:0 0 10px'>Choose a scenario</h2>
  <p style='font-size:.85rem;color:#64748b;max-width:520px;margin:0 auto;line-height:1.65'>
    Both paths run the same AI intelligence pipeline. The difference is one field in the CMDB —
    <b style='color:#1e293b'>redundancy_type</b> — which determines whether a human must approve or the policy acts autonomously.
  </p>
</div>""", unsafe_allow_html=True)

        ca, cb = st.columns(2)
        with ca:
            st.markdown("""
<div style='background:#fff;border:2px solid #d97706;border-radius:14px;padding:26px;min-height:260px;box-shadow:0 2px 12px rgba(217,119,6,.12)'>
  <div style='font-size:1.6rem;margin-bottom:10px'>🔐</div>
  <h3 style='color:#d97706;margin:0 0 6px;font-size:1.05rem'>Path A — Human Approval</h3>
  <div style='display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap'>
    <span style='background:rgba(217,119,6,.08);border:1px solid rgba(217,119,6,.25);color:#d97706;
      padding:2px 8px;border-radius:3px;font-size:.62rem;font-weight:700'>standalone</span>
    <span style='background:rgba(220,38,38,.08);border:1px solid rgba(220,38,38,.25);color:#dc2626;
      padding:2px 8px;border-radius:3px;font-size:.62rem;font-weight:700'>prod-critical</span>
    <span style='background:#f1f5f9;border:1px solid #e2e8f0;color:#64748b;
      padding:2px 8px;border-radius:3px;font-size:.62rem;font-weight:700'>linux-lab-02</span>
  </div>
  <p style='color:#64748b;font-size:.8rem;line-height:1.65;margin:0'>
    Platform analyses, correlates peers, generates runbook, runs safety gate — but gates
    live execution on an explicit engineer sign-off.<br><br>
    <b style='color:#1e293b'>Demonstrates:</b> Human-in-the-loop governance, approval audit trail.
  </p>
</div>""", unsafe_allow_html=True)
            if st.button("▶  Start Path A", use_container_width=True, type="primary", key="start_a"):
                st.session_state.demo_path = "A"
                st.session_state.demo_step = 0
                st.rerun()

        with cb:
            st.markdown("""
<div style='background:#fff;border:2px solid #16a34a;border-radius:14px;padding:26px;min-height:260px;box-shadow:0 2px 12px rgba(22,163,74,.12)'>
  <div style='font-size:1.6rem;margin-bottom:10px'>⚡</div>
  <h3 style='color:#16a34a;margin:0 0 6px;font-size:1.05rem'>Path B — Automated</h3>
  <div style='display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap'>
    <span style='background:rgba(22,163,74,.08);border:1px solid rgba(22,163,74,.25);color:#16a34a;
      padding:2px 8px;border-radius:3px;font-size:.62rem;font-weight:700'>active-active</span>
    <span style='background:rgba(37,99,235,.08);border:1px solid rgba(37,99,235,.25);color:#2563eb;
      padding:2px 8px;border-radius:3px;font-size:.62rem;font-weight:700'>redundant</span>
    <span style='background:#f1f5f9;border:1px solid #e2e8f0;color:#64748b;
      padding:2px 8px;border-radius:3px;font-size:.62rem;font-weight:700'>linux-lab-01</span>
  </div>
  <p style='color:#64748b;font-size:.8rem;line-height:1.65;margin:0'>
    Same intelligence pipeline, same safety gate, same drift snapshot — but the error
    tolerance policy auto-approves with zero human touchpoints.<br><br>
    <b style='color:#1e293b'>Demonstrates:</b> Autonomous remediation, identical audit trail.
  </p>
</div>""", unsafe_allow_html=True)
            if st.button("▶  Start Path B", use_container_width=True, type="primary", key="start_b"):
                st.session_state.demo_path = "B"
                st.session_state.demo_step = 0
                st.rerun()

        st.divider()
        with st.expander("📖 Platform Overview — what each module does", expanded=False):
            _demo_html_path = Path(__file__).parent / "demo" / "slm_platform_demo.html"
            if _demo_html_path.exists():
                st.components.v1.html(
                    _demo_html_path.read_text(encoding="utf-8"), height=2400, scrolling=True
                )
        return

    # ════════════════════════════════════════════════════════════════════════
    # STEP DEFINITIONS
    # ════════════════════════════════════════════════════════════════════════
    path = st.session_state.demo_path
    if path == "A":
        ci, svc, metric, mval = "linux-lab-02", "claims-api", "cpu_percent", 97.2
        alert_name, severity = "CpuSaturation", "critical"
        classification_hint = "cpu_saturation"
    else:
        ci, svc, metric, mval = "linux-lab-01", "web-frontend", "api_latency_ms", 3200
        alert_name, severity = "ApiLatencySpike", "high"
        classification_hint = "api_latency_spike"

    path_color = "#f59e0b" if path == "A" else "#22c55e"
    path_label = (
        "🔐 Path A — Human Approval · linux-lab-02"
        if path == "A"
        else "⚡ Path B — Automated · linux-lab-01"
    )

    # ── API call functions (closures over path/ci/svc/metric/mval) ───────────

    def _call(method, url, **kw):
        try:
            r = SESSION.request(method, f"{BACKEND_URL}{url}", timeout=25, **kw)
            try:
                d = r.json()
            except Exception:
                d = {"raw": r.text[:300]}
            return r.status_code, d
        except Exception as exc:
            return 0, {"error": str(exc)}

    def fn_seed():
        cis = [
            {"ci_name": "linux-lab-02", "environment": "production", "redundancy_type": "standalone",
             "business_criticality": 2, "assignment_group": "Application Engineering",
             "notes": "App tier — standalone, critical"},
            {"ci_name": "linux-lab-01", "environment": "production", "redundancy_type": "active-active",
             "business_criticality": 3, "assignment_group": "Platform Engineering",
             "notes": "Web tier — redundant, auto-eligible"},
            {"ci_name": "network-lab-01", "environment": "production", "redundancy_type": "active-passive",
             "business_criticality": 3, "assignment_group": "Data Engineering",
             "notes": "DB/config tier"},
        ]
        rows = {}
        for c in cis:
            code, _ = _call("POST", "/cmdb/ci", json=c)
            rows[c["ci_name"]] = "✅ registered" if code in (200, 201) else "⚠️ already exists" if code == 409 else f"❌ {code}"
        return {"ok": True, "summary": "3 lab CIs registered in CMDB", "details": rows}

    def fn_alert():
        code, d = _call("POST", "/telemetry/event", json={
            "metric": metric, "value": mval, "resource": ci, "service": svc,
            "environment": "production", "source": "prometheus",
            "labels": {"alertname": alert_name, "severity": severity},
        })
        ok = code in (200, 201, 202)
        return {"ok": ok, "summary": f"{metric}={mval} posted for {ci}", "details": d}

    def fn_incident():
        code, d = _call("POST", "/telemetry/evaluate", json={"dry_run": False})
        # fetch incident list to grab ID for this CI
        _, inc_resp = _call("GET", "/telemetry/incidents")
        inc_list = inc_resp.get("incidents", []) if isinstance(inc_resp, dict) else []
        for inc in inc_list:
            if ci in str(inc.get("resource", "")) or ci in str(inc.get("title", "")):
                st.session_state.demo_incident_id = inc.get("incident_id") or inc.get("id")
                break
        iid = st.session_state.demo_incident_id
        created = d.get("incidents_created", d.get("created_count", "?")) if isinstance(d, dict) else "?"
        return {
            "ok": code in (200, 201, 202),
            "summary": f"Incident created — ID: {iid or '(see Incident Console)'}",
            "details": {"incidents_created": created, "incident_id": iid, **({k: v for k, v in d.items() if k not in ("incidents_created",)} if isinstance(d, dict) else {})},
        }

    def fn_analyze():
        iid = st.session_state.demo_incident_id
        payload = (
            {"incident_id": iid}
            if iid
            else {"resource": ci, "service": svc, "metric": metric, "severity": severity}
        )
        code, d = _call("POST", "/analyze", json=payload)
        ok = code < 300
        clf = d.get("classification") or d.get("severity") or "HIGH"
        conf = d.get("confidence_score") or d.get("confidence") or "—"
        keep = ("classification", "confidence_score", "confidence", "reasoning",
                "summary", "peer_insights", "recommended_action", "runbook_steps")
        return {
            "ok": ok,
            "summary": f"AI classified as {clf}  (confidence: {conf})",
            "details": {k: v for k, v in d.items() if k in keep and v} if ok else d,
        }

    def fn_tolerance():
        code, d = _call("POST", "/tolerance/evaluate", json={
            "ci_name": ci, "incident_classification": classification_hint,
            "severity": severity, "elapsed_minutes": 3.0,
        })
        ok = code < 300
        tier = d.get("tier", "?")
        appr = d.get("approval_required")
        verdict = "Human approval required" if appr else "Auto-remediation eligible"
        keep = ("tier", "within_tolerance", "should_auto_remediate",
                "escalation_level", "approval_required", "recommendation", "reasoning")
        return {
            "ok": ok,
            "summary": f"Tier: {tier} — {verdict}",
            "details": {k: v for k, v in d.items() if k in keep and v is not None} if ok else d,
        }

    def fn_safety():
        iid = st.session_state.demo_incident_id
        code, d = _call("POST", "/automation/pre_execution_check", json={
            "incident_id": iid or "demo-incident",
            "ci_name": ci, "action": "remediate",
        })
        ok = code < 300
        status = d.get("safety_gate_status") or d.get("status") or ("passed" if ok else "failed")
        keep = ("safety_gate_status", "status", "checks", "blocking_checks",
                "safe_to_approve", "notes", "passed", "warnings")
        return {
            "ok": ok,
            "summary": f"Safety gate: {str(status).upper()}",
            "details": {k: v for k, v in d.items() if k in keep and v is not None} if ok else d,
        }

    def fn_snapshot():
        iid = st.session_state.demo_incident_id
        code, d = _call("POST", "/drift/snapshot", json={
            "incident_id": iid or "demo-incident",
            "ci_name": ci, "reason": "pre-remediation snapshot",
        })
        ok = code < 300
        snap_id = d.get("snapshot_id") or d.get("id")
        if snap_id:
            st.session_state.demo_snapshot_id = snap_id
        keep = ("snapshot_id", "id", "ci_name", "state", "timestamp", "status")
        return {
            "ok": ok,
            "summary": f"Snapshot captured — ID: {snap_id or '?'}",
            "details": {k: v for k, v in d.items() if k in keep and v is not None} if ok else d,
        }

    def fn_verify():
        snap_id = st.session_state.demo_snapshot_id
        code, d = _call("POST", "/drift/verify", json={
            "snapshot_id": snap_id or "demo-snapshot", "ci_name": ci,
        })
        ok = code < 300
        result = d.get("result") or d.get("status") or ("PASS" if ok else "FAIL")
        keep = ("result", "status", "diff", "changes", "message", "verified")
        return {
            "ok": ok,
            "summary": f"Snapshot verification: {str(result).upper()}",
            "details": {k: v for k, v in d.items() if k in keep and v is not None} if ok else d,
        }

    def fn_execute():
        iid = st.session_state.demo_incident_id
        code, d = _call("POST", "/automation/execute", json={
            "incident_id": iid or "demo-incident",
            "ci_name": ci, "approved": True,
        })
        ok = code < 300
        keep = ("status", "execution_id", "playbook", "task_output", "message", "result")
        return {
            "ok": ok,
            "summary": "Ansible playbook execution started" if ok else "Execution failed",
            "details": {k: v for k, v in d.items() if k in keep and v is not None} if ok else d,
        }

    def fn_governance():
        code, d = _call("GET", "/governance/check")
        ok = code < 300
        return {
            "ok": ok,
            "summary": "Governance audit record generated",
            "details": d if isinstance(d, dict) else {"response": str(d)[:400]},
        }

    # ── Step table: (icon, title, context, btn_label, fn, nav_page, nav_label)
    # fn=None means manual step — user navigates themselves
    STEPS_A = [
        ("🌱", "Seed Demo Data",
         "Register the three lab machines in CMDB with their criticality and redundancy profile. This is the foundation the Error Tolerance module reads at decision time.",
         "Register CIs", fn_seed, None, None),

        ("🚨", "Fire the Alert",
         f"Post a <b>{metric}={mval}</b> telemetry event from <b>{ci}</b>, simulating Prometheus crossing a critical CPU threshold. This is the moment the incident lifecycle begins.",
         "Send Telemetry Event", fn_alert, "Telemetry Console", "View in Telemetry Console"),

        ("🔔", "Incident Detected",
         "Evaluate the telemetry stream. The platform applies alert rules and creates a structured incident record — severity, affected service, resource, timestamp.",
         "Create Incident", fn_incident, "Incident Console", "View in Incident Console"),

        ("🧠", "AI Analysis",
         "AI classifies the incident with a confidence score and full reasoning chain. It also pulls peer insights from similar incidents across client tenants and generates a step-by-step remediation runbook.",
         "Run AI Analysis", fn_analyze, "Incident Console", "View in Incident Console"),

        ("⚖️", "Error Tolerance Check",
         f"CMDB lookup for <b>{ci}</b> returns tier = <b>prod-standalone</b>. This is a critical standalone system — the policy mandates human approval before any live change.",
         "Check CMDB Tolerance", fn_tolerance, "Automation Queue", "View in Automation Queue"),

        ("🛡️", "Safety Gate",
         "Six pre-execution checks run automatically: drift state, SLO budget, concurrent change activity, change window, rollback plan readiness, blast radius. All must pass before execution is permitted.",
         "Run Safety Gate", fn_safety, "Automation Queue", "View in Automation Queue"),

        ("👤", "Engineer Approves",
         "The platform is waiting. An engineer reviews the AI analysis, runbook, safety gate evidence, and peer insights — then clicks Approve in the Automation Queue. No approval = no execution.",
         None, None, "Automation Queue", "Go to Automation Queue to Approve"),

        ("📸", "Drift Snapshot",
         "Before touching the live system, the platform captures a full snapshot of the current (broken) environment. This is the baseline for safe testing.",
         "Capture Snapshot", fn_snapshot, "Drift Management", "View in Drift Management"),

        ("✅", "Verify on Snapshot",
         "The fix is applied and tested against the snapshot clone in isolation. Only if verification returns PASS does the live gate unlock. The production system is never touched first.",
         "Verify Snapshot", fn_verify, "Drift Management", "View in Drift Management"),

        ("⚙️", "Ansible Executes",
         "The approved, gate-passed, snapshot-verified playbook executes against the live container via ansible-runner. Task output streams in real time.",
         "Execute Playbook", fn_execute, "Automation Queue", "View Execution Output"),

        ("🏛️", "Governance Report",
         "Incident closed. The platform writes the full evidence package: MTTR, SLA compliance verdict, approval event log, Ansible execution log, and before/after drift diff.",
         "Generate Report", fn_governance, "Governance", "View Governance Report"),
    ]

    STEPS_B = [
        ("🌱", "Seed Demo Data",
         "Register the three lab machines in CMDB with their criticality and redundancy profile. This is the foundation the Error Tolerance module reads at decision time.",
         "Register CIs", fn_seed, None, None),

        ("🚨", "Fire the Alert",
         f"Post a <b>{metric}={mval}</b> telemetry event from <b>{ci}</b>, simulating Alertmanager crossing a latency threshold. This is the moment the incident lifecycle begins.",
         "Send Telemetry Event", fn_alert, "Telemetry Console", "View in Telemetry Console"),

        ("🔔", "Incident Detected",
         "Evaluate the telemetry stream. The platform applies alert rules and creates a structured incident record — severity, affected service, resource, timestamp.",
         "Create Incident", fn_incident, "Incident Console", "View in Incident Console"),

        ("🧠", "AI Analysis",
         "AI classifies the incident with a confidence score and full reasoning chain. It also pulls peer insights from similar incidents across client tenants and generates a step-by-step remediation runbook.",
         "Run AI Analysis", fn_analyze, "Incident Console", "View in Incident Console"),

        ("⚖️", "Error Tolerance Check",
         f"CMDB lookup for <b>{ci}</b> returns tier = <b>prod-ha (active-active)</b>. Redundant system — the policy permits autonomous execution. No human touchpoint required.",
         "Check CMDB Tolerance", fn_tolerance, "Automation Queue", "View in Automation Queue"),

        ("🛡️", "Safety Gate",
         "Same six pre-execution checks as Path A. All pass for this redundant tier. The platform is cleared to proceed without waiting for a human.",
         "Run Safety Gate", fn_safety, "Automation Queue", "View in Automation Queue"),

        ("⚡", "Policy Auto-Approves",
         "No engineer needed. The error tolerance policy auto-approves based on tier and safety gate result. The approval decision is fully logged in the audit trail — identical evidence to a human approval.",
         None, None, "Automation Queue", "View Auto-Approval in Queue"),

        ("📸", "Drift Snapshot",
         "Same rigour as Path A. Before touching the live system, the platform captures a snapshot of the current (broken) environment as the test baseline.",
         "Capture Snapshot", fn_snapshot, "Drift Management", "View in Drift Management"),

        ("✅", "Verify on Snapshot",
         "Fix tested on the snapshot clone in isolation. Live gate unlocks only after a clean PASS. Safety is never skipped for automated paths.",
         "Verify Snapshot", fn_verify, "Drift Management", "View in Drift Management"),

        ("⚙️", "Ansible Executes",
         "The playbook executes autonomously against the live container via ansible-runner. Zero human touchpoints from alert to execution.",
         "Execute Playbook", fn_execute, "Automation Queue", "View Execution Output"),

        ("🏛️", "Governance Report",
         "Incident closed. Identical evidence package to Path A — automation doesn't skip evidence. MTTR, SLA compliance, auto-approval log, Ansible output, drift diff.",
         "Generate Report", fn_governance, "Governance", "View Governance Report"),
    ]

    steps = STEPS_A if path == "A" else STEPS_B
    current = st.session_state.demo_step

    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    # VISUAL PIPELINE STRIP
    # ════════════════════════════════════════════════════════════════════════
    total = len(steps)
    pc_css = path_color.replace("#", "%23")
    nodes_html = ""
    for i, (ico, ttl, *_) in enumerate(steps):
        cls = "n-done" if i < current else ("n-cur" if i == current else "n-pend")
        bub_ico = "✓" if i < current else ico
        nodes_html += (
            f"<div class='dp-node {cls}' style='--pc:{path_color}'>"
            f"<div class='dp-bub'>{bub_ico}</div>"
            f"<div class='dp-lbl'>{ttl}</div>"
            f"</div>"
        )
    st.markdown(
        f"<div style='background:#fff;border:1px solid #e2e8f0;border-radius:10px;"
        f"padding:10px 16px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.05)'>"
        f"<div style='font-size:.6rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;"
        f"color:{path_color};margin-bottom:8px'>{path_label} &nbsp;·&nbsp; Step {min(current+1,total)} of {total}</div>"
        f"<div class='dp-pipe'>{nodes_html}</div></div>",
        unsafe_allow_html=True,
    )

    # ════════════════════════════════════════════════════════════════════════
    # COMPLETED STEPS (collapsed)
    # ════════════════════════════════════════════════════════════════════════
    if current > 0:
        done_rows = ""
        for i in range(current):
            ico_d, ttl_d, *_ = steps[i]
            res_d = st.session_state.demo_results.get(i)
            smry_d = (res_d.get("summary", "done") if res_d else "done")[:60]
            ok_ico = "✅" if (res_d and res_d.get("ok")) else "⚠️"
            done_rows += (
                f"<div class='dc-done-row'>"
                f"<div class='dc-done-step'>{ok_ico} Step {i+1}</div>"
                f"<div class='dc-done-title'>{ico_d} {ttl_d}</div>"
                f"<div class='dc-done-sum'>{smry_d}</div>"
                f"</div>"
            )
        with st.expander(f"✅ Completed steps ({current} / {total})", expanded=False):
            st.markdown(done_rows, unsafe_allow_html=True)

    # ════════════════════════════════════════════════════════════════════════
    # DEMO COMPLETE
    # ════════════════════════════════════════════════════════════════════════
    if current >= total:
        st.markdown(f"""
<div style='border:2px solid #16a34a;border-radius:14px;padding:32px;text-align:center;
  background:#fff;margin-bottom:20px;box-shadow:0 2px 12px rgba(22,163,74,.12)'>
  <div style='font-size:2.5rem;margin-bottom:10px'>🎉</div>
  <h2 style='color:#16a34a;margin:0 0 10px;font-size:1.4rem'>Demo Complete</h2>
  <p style='color:#64748b;font-size:.85rem;max-width:480px;margin:0 auto;line-height:1.7'>
    Incident detected → AI analysed → safely remediated → closed with a full governance audit trail.<br>
    Every decision is backed by evidence. Every action is logged.
  </p>
</div>""", unsafe_allow_html=True)
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            if st.button("🔁 Run Same Path Again", use_container_width=True):
                st.session_state.demo_step = 0
                st.session_state.demo_results = {}
                st.session_state.demo_incident_id = None
                st.session_state.demo_snapshot_id = None
                st.rerun()
        with col_b:
            if st.button(f"↔ Try {'Path B' if path == 'A' else 'Path A'}", use_container_width=True):
                st.session_state.demo_path = "B" if path == "A" else "A"
                st.session_state.demo_step = 0
                st.session_state.demo_results = {}
                st.session_state.demo_incident_id = None
                st.session_state.demo_snapshot_id = None
                st.rerun()
        with col_c:
            if st.button("🏛️ View Governance Report", use_container_width=True, type="primary"):
                st.session_state.active_page = "Governance"
                st.rerun()
        return

    # ════════════════════════════════════════════════════════════════════════
    # CURRENT STEP CARD
    # ════════════════════════════════════════════════════════════════════════
    ico, title, context, btn_label, api_fn, nav_page, nav_label = steps[current]
    prev = st.session_state.demo_results.get(current)

    # Module tag for each step
    _module_tags = [
        "CMDB", "Telemetry Engine", "Incident Console",
        "AI Classification", "Error Tolerance", "Safety Gate",
        "Approval Gate", "Drift Management", "Drift Management",
        "Ansible Runner", "Governance",
    ]
    mod_tag = _module_tags[current] if current < len(_module_tags) else "Platform"

    pc_bg = "rgba(245,158,11,.06)" if path == "A" else "rgba(34,197,94,.06)"
    st.markdown(
        f"<div class='dc-card' style='--pc:{path_color};--pc-bg:{pc_bg}'>"
        f"<div class='dc-hdr'>"
        f"<div class='dc-mod'>{ico} {mod_tag}</div>"
        f"<div class='dc-eyebrow'>Step {current+1} of {total}</div>"
        f"<div class='dc-title' style='color:{path_color}'>{title}</div>"
        f"<div class='dc-ctx'>{context}</div>"
        f"</div></div>",
        unsafe_allow_html=True,
    )

    # ── Visual illustration ───────────────────────────────────────────────────
    visual_html = _demo_step_visual_html(current, path, ci, metric, mval)
    if visual_html:
        st.markdown(visual_html, unsafe_allow_html=True)

    # ── Live result card (shown after step runs) ──────────────────────────────
    if prev:
        result_html = _demo_result_html(current, prev, path, ci, metric, mval)
        if result_html:
            st.markdown(result_html, unsafe_allow_html=True)

    # ── Action buttons ───────────────────────────────────────────────────────
    b_run, b_nav, b_next = st.columns([2, 2, 2])

    with b_run:
        if api_fn is not None:
            lbl = btn_label if not prev else "↺ Re-run"
            if st.button(lbl, type="primary", use_container_width=True, key=f"run_{current}"):
                with st.spinner(f"{ico} {title}…"):
                    try:
                        result = api_fn()
                    except Exception as exc:
                        result = {"ok": False, "summary": str(exc), "details": {}}
                st.session_state.demo_results[current] = result
                st.rerun()
        else:
            pass  # Manual step — visual already shown above

    with b_nav:
        if nav_page and nav_label:
            if st.button(f"→ {nav_label}", use_container_width=True, key=f"nav_{current}"):
                st.session_state.active_page = nav_page
                st.rerun()

    with b_next:
        can_advance = (api_fn is None) or (prev is not None)
        if st.button(
            "Next Step ▶",
            disabled=not can_advance,
            use_container_width=True,
            type="primary" if can_advance else "secondary",
            key=f"next_{current}",
        ):
            st.session_state.demo_step += 1
            st.rerun()

    if api_fn is not None and not prev:
        st.caption(f"Run **{btn_label}** above to see the live result, then click Next Step.")

    if api_fn is not None and not prev:
        st.caption("Run the step above to see the live result, then click Next Step.")

    # ── Platform overview (always available at bottom) ────────────────────────
    st.divider()
    with st.expander("📖 Platform Overview", expanded=False):
        _demo_html_path = Path(__file__).parent / "demo" / "slm_platform_demo.html"
        if _demo_html_path.exists():
            st.components.v1.html(
                _demo_html_path.read_text(encoding="utf-8"), height=2400, scrolling=True
            )


def main():
    st.set_page_config(page_title="AI SRE Platform", layout="wide", page_icon="\U0001f6a8")
    init_state()

    current_page = _sync_active_page()

    with st.sidebar:
        render_sidebar_nav(current_page)

        with st.expander("⚙️  Engineer Controls", expanded=False):
            st.session_state.force_fallback_for_next_run = st.checkbox(
                "Force Fallback for Next Run",
                value=bool(st.session_state.get("force_fallback_for_next_run", False)),
            )
            st.session_state.show_debug_trace = st.checkbox(
                "Show Developer Trace",
                value=bool(st.session_state.get("show_debug_trace", False)),
            )
        st.caption(
            "Incident Console → AI analysis. "
            "Resolution Queue → approve & learn. "
            "Knowledge Library → resolutions & playbooks. "
            "Telemetry Console → proactive detection."
        )

    render_top_nav(current_page)

    if st.session_state.active_page != "Incident Console":
        if st.session_state.active_page == "Dashboard":
            render_dashboard(compact=False)
        elif st.session_state.active_page == "Knowledge Center":
            render_knowledge_center_page()
        elif st.session_state.active_page == "Telemetry Console":
            render_telemetry_console_page()
        elif st.session_state.active_page == "Client Onboarding Studio":
            render_client_onboarding_page_with_submenus()
        elif st.session_state.active_page == "Automation Queue":
            render_automation_queue_page()
        elif st.session_state.active_page == "Drift Management":
            if _new_modules_loaded:
                render_drift_page()
            else:
                st.warning("Drift Management module could not be loaded.", icon="⚠️")
        elif st.session_state.active_page == "Backtest Console":
            render_backtest_console_page()
        elif st.session_state.active_page == "Demo":
            _render_demo_page()
        elif st.session_state.active_page == "POC Console":
            from page_modules.poc_console import render_poc_console_page
            render_poc_console_page()
        else:
            render_backtesting_page()
        return

    display_messages = st.session_state.get("messages", []) or []
    if not display_messages and isinstance(st.session_state.get("last_analysis_result"), dict):
        display_messages = [
            {"role": "user", "content": st.session_state.get("last_incident_ticket", "") or "Previous incident"},
            {"role": "assistant", "result": st.session_state.get("last_analysis_result"), "ticket": st.session_state.get("last_incident_ticket", "")},
        ]

    if display_messages:
        for idx, msg in enumerate(display_messages):
            role = msg["role"]
            with st.chat_message("user" if role == "user" else "assistant"):
                if role == "user":
                    st.write(msg["content"])
                    attachment = msg.get("attachment")
                    if attachment and attachment.get("bytes"):
                        try:
                            import base64 as _b64
                            _mime = attachment.get("mime", "image/png")
                            _b64str = _b64.b64encode(attachment["bytes"]).decode()
                            st.markdown(
                                f'<img src="data:{_mime};base64,{_b64str}" '
                                f'style="width:100%;max-height:340px;object-fit:contain;'
                                f'border-radius:6px;display:block;margin:4px 0;" />',
                                unsafe_allow_html=True,
                            )
                            st.caption(attachment["name"])
                        except Exception:
                            st.caption(f"Attached: {attachment['name']}")
                    elif attachment and attachment.get("name"):
                        st.caption(f"Attached: {attachment['name']}")
                elif role == "assistant":
                    render_assistant_result(msg["result"], msg["ticket"], f"hist_{idx}")
                else:
                    render_error_state(msg["error"])
        st.markdown("---")
    else:
        _greeting_text = get_incident_console_greeting()
        st.markdown(
            "<div data-aiops-role='greeting' style='"
            "text-align:center;font-size:3.4rem;font-weight:500;"
            "letter-spacing:-0.03em;margin:5.8rem 0 2.4rem 0;color:#111827;"
            "'>" + _greeting_text + "</div>",
            unsafe_allow_html=True,
        )
        # Remove any ghost/duplicate greeting that Streamlit 1.57 may render
        # alongside our custom markdown when the chat-input is in file-preview mode.
        _st_components.html(
            """<script>
(function() {
  function dedupe() {
    try {
      var greet = parent.document.querySelector('[data-aiops-role="greeting"]');
      if (!greet) return;
      var txt = (greet.textContent || '').trim();
      parent.document.querySelectorAll('[data-testid="stMarkdownContainer"]').forEach(function(el) {
        if (el.contains(greet)) return;
        if ((el.textContent || '').trim() === txt) {
          var wrap = el.closest('[data-testid="element-container"]') || el.parentElement;
          if (wrap) wrap.style.setProperty('display', 'none', 'important');
        }
      });
    } catch(e) {}
  }
  setTimeout(dedupe, 100);
  setTimeout(dedupe, 600);
})();
</script>""",
            height=0,
        )

        # ── Orientation: stats bar ────────────────────────────────────────
        def _read_json_list(path: str) -> list:
            try:
                import json as _json
                p = Path(path)
                if p.exists():
                    data = _json.loads(p.read_text(encoding="utf-8"))
                    return data if isinstance(data, list) else []
            except Exception:
                pass
            return []

        _res_count  = len(_read_json_list("data/resolutions.json"))
        _pb_count   = len(_read_json_list("data/playbooks.json"))
        _aq_list    = _read_json_list("data/automation_candidates.json")
        _auto_ready = sum(1 for c in _aq_list if str(c.get("status", "")).lower() in ("enabled", "approved", "ready", "automation_ready"))

        st.markdown(
            f"""
            <div style="display:flex;justify-content:center;gap:28px;margin:0 0 28px 0;flex-wrap:wrap;">
                <div style="text-align:center;">
                    <div style="font-size:1.5rem;font-weight:700;color:#4F8EF7;">{_res_count}</div>
                    <div style="font-size:0.72rem;opacity:0.55;letter-spacing:0.04em;text-transform:uppercase;">Resolutions</div>
                </div>
                <div style="color:#e2e8f0;font-size:1.2rem;align-self:center;">·</div>
                <div style="text-align:center;">
                    <div style="font-size:1.5rem;font-weight:700;color:#10B981;">{_pb_count}</div>
                    <div style="font-size:0.72rem;opacity:0.55;letter-spacing:0.04em;text-transform:uppercase;">Playbooks</div>
                </div>
                <div style="color:#e2e8f0;font-size:1.2rem;align-self:center;">·</div>
                <div style="text-align:center;">
                    <div style="font-size:1.5rem;font-weight:700;color:#8B5CF6;">{_auto_ready}</div>
                    <div style="font-size:0.72rem;opacity:0.55;letter-spacing:0.04em;text-transform:uppercase;">Automations Ready</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ── Orientation: example chips ────────────────────────────────────
        st.markdown(
            "<div style='text-align:center;font-size:0.78rem;opacity:0.45;"
            "letter-spacing:0.05em;text-transform:uppercase;margin-bottom:10px;'>"
            "Try an example</div>",
            unsafe_allow_html=True,
        )
        _EXAMPLE_CHIPS = [
            ("🔐", "SAP login failure — users cannot authenticate after password policy change"),
            ("🌐", "VPN connectivity issue — Zscaler dropping connections intermittently for remote users"),
            ("💾", "File system full alert — /var/log mount at 98% causing application errors"),
        ]
        _chip_cols = st.columns(len(_EXAMPLE_CHIPS), gap="small")
        for _col, (_icon, _chip_text) in zip(_chip_cols, _EXAMPLE_CHIPS):
            with _col:
                if st.button(
                    f"{_icon}  {_chip_text[:46]}{'…' if len(_chip_text) > 46 else ''}",
                    key=f"chip_{hash(_chip_text)}",
                    use_container_width=True,
                ):
                    run_analysis_from_submission(_chip_text)
                    st.rerun()

        st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)

    submission = st.chat_input(
        "Describe the incident...",
        accept_file=True,
        file_type=["png", "jpg", "jpeg", "webp"],
        accept_audio=False,
        key="main_chat_input",
    )
    if submission is not None:
        run_analysis_from_submission(submission)
        st.rerun()


if __name__ == "__main__":
    main()


