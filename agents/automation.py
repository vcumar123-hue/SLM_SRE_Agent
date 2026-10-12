from __future__ import annotations
import json, os, re, threading, traceback, uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_CONFIDENCE_LOCK = threading.Lock()
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

import settings_portable as settings
import automation_engine as _engine
import execution_authorization as exec_auth
from security import Principal, principal_from_request
from agents.helpers import _automation_classification_key
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
    from automation_engine import (
        build_automation_plan, queue_automation_plan,
        approve_automation, execute_automation,
        suggest_action_mapping, learn_action_mapping, get_learned_action_mappings,
        refresh_automation_queue, close_queue_if_remediated, list_automation_queue,
        AUTOMATION_LOG_FILE, AUTOMATION_QUEUE_FILE,
    )
except Exception:
    build_automation_plan = None
    queue_automation_plan = None
    approve_automation = None
    execute_automation = None
    suggest_action_mapping = None
    learn_action_mapping = None
    get_learned_action_mappings = None
    refresh_automation_queue = None
    list_automation_queue = None
    close_queue_if_remediated = None
    AUTOMATION_LOG_FILE = None  # type: ignore[assignment]
    AUTOMATION_QUEUE_FILE = None  # type: ignore[assignment]
try:
    from automation_candidate_registry import (
        create_automation_candidate, list_automation_candidates, get_automation_candidate,
        update_automation_candidate, record_candidate_test_result,
        approve_automation_candidate, enable_automation_candidate,
        generate_candidate_playbook_draft, save_reviewed_candidate_playbook,
        get_automation_registry, set_autonomous_registry_status,
        is_autonomous_registry_mapping_allowed,
    )
except Exception:
    create_automation_candidate = None
    list_automation_candidates = None
    get_automation_candidate = None
    update_automation_candidate = None
    record_candidate_test_result = None
    approve_automation_candidate = None
    enable_automation_candidate = None
    generate_candidate_playbook_draft = None
    save_reviewed_candidate_playbook = None
    get_automation_registry = None
    set_autonomous_registry_status = None
    is_autonomous_registry_mapping_allowed = None
try:
    from postgres_store import (
        insert_automation_queue as db_insert_automation_queue,
        update_automation_queue_status as db_update_automation_queue_status,
        insert_automation_execution as db_insert_automation_execution,
    )
except Exception:
    db_insert_automation_queue = None
    db_update_automation_queue_status = None
    db_insert_automation_execution = None


router = APIRouter(tags=["automation"])


# -- Release 1: identity and authorization helpers ----------------------------

def _require_principal(http_request: Request, minimum: str = "l1") -> Principal:
    principal = principal_from_request(http_request)
    if principal is None:
        raise HTTPException(status_code=401, detail="authentication required")
    if not principal.has_role(minimum):
        raise HTTPException(status_code=403, detail=f"role {minimum} or above required")
    return principal


def _busy_http(exc: "_engine.QueueBusyError") -> HTTPException:
    """Queue lock contention is a temporary infrastructure condition: 503, never a policy denial."""
    return HTTPException(
        status_code=503,
        detail={"status": "busy", "message": str(exc), "operation": exc.operation, "queue_id": exc.queue_id},
        headers={"Retry-After": "2"},
    )


def _ledger_gate_for(classification: str, tenant_id: str) -> Dict[str, Any]:
    from resolution_outcome import autonomous_gate
    return autonomous_gate(classification, tenant_id=tenant_id or "default")


def _autonomous_mapping_allowed(classification: str) -> bool:
    if is_autonomous_registry_mapping_allowed is None:
        return False
    try:
        result = is_autonomous_registry_mapping_allowed(classification=classification, action="", playbook="")
        return bool((result or {}).get("allowed"))
    except Exception:
        return False


def _first_playbook(entry: Dict[str, Any]) -> str:
    for step in ((entry.get("plan") or {}).get("steps") or []):
        if step.get("playbook"):
            return str(step["playbook"])
    return ""


def _run_gate_for_entry(entry: Dict[str, Any], principal: Principal, mode: str = "live") -> Dict[str, Any]:
    """Evaluate the Safety Gate for a queue entry using the parameters of its last recorded run."""
    # Re-run with the parameters that were approved, not whatever check happened to run last.
    approved = (entry.get("approval") or {}).get("gate_params") or {}
    last = approved or exec_auth.latest_gate_run(entry.get("queue_id", "")) or {}
    params = {
        "classification": entry.get("classification", ""),
        "playbook": last.get("playbook") or _first_playbook(entry),
        "target_host": last.get("target_host", ""),
        "risk_tier": last.get("risk_tier") or (entry.get("plan") or {}).get("risk_tier") or "medium",
        "execution_mode": mode,
    }
    gate_request = AutomationPreExecutionCheckRequest(
        queue_id=entry.get("queue_id", ""),
        incident_id=entry.get("incident_id", ""),
        requested_by=principal.id,
        **params,
    )
    result = _build_pre_execution_safety_gate(gate_request)
    return exec_auth.record_gate_run(entry, result, params, principal.id)


@router.post("/automation/plan")
def automation_plan_route(request: AutomationPlanRequest) -> Dict[str, Any]:
    if build_automation_plan is None:
        raise HTTPException(status_code=500, detail="automation_engine not available")
    try:
        plan = build_automation_plan(
            classification=_automation_classification_key(request.classification),
            linked_resolution=request.linked_resolution,
            linked_playbook=request.linked_playbook,
        )
        return {"status": "ok", "incident_id": request.incident_id, "plan": plan}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _automation_safety_level(value: Optional[str], default: str = "medium") -> str:
    token = clean_text(value or default).lower().replace(" ", "_").replace("-", "_")
    if token in {"low", "medium", "high", "critical"}:
        return token
    return default


def _estimate_blast_radius(classification: Optional[str], playbook: Optional[str], risk_tier: Optional[str]) -> Dict[str, Any]:
    cls = normalize_classification_token(classification)
    pb = clean_text(playbook).lower()
    risk = _automation_safety_level(risk_tier)

    # Read-only validation playbooks are low/medium by default. Shared routing,
    # identity, DNS, firewall, database, or load-balancer changes have broader
    # impact and should require stricter approval in later enforcement steps.
    high_terms = ["dns", "firewall", "iam", "identity", "gateway", "load_balancer", "load balancer", "database", "listener", "security_group"]
    medium_terms = ["container", "ecs", "kubernetes", "disk", "cpu", "vpn"]

    scope = "single_service_or_lab_target"
    level = risk

    if any(term in cls or term in pb for term in high_terms):
        level = "high" if risk != "critical" else "critical"
        scope = "shared_or_route_affecting_infrastructure"
    elif any(term in cls or term in pb for term in medium_terms):
        level = "medium" if risk not in {"high", "critical"} else risk
        scope = "service_or_host_level"
    elif risk == "low":
        level = "low"
        scope = "read_only_or_single_target"

    return {
        "level": level,
        "scope": scope,
        "reason": "Estimated from classification, playbook name, and risk tier.",
    }



# -----------------------------------------------------------------------------
# Step 5A - Client-onboarding Safety Gate Configuration
# -----------------------------------------------------------------------------
# The Safety Gate should not be hardwired to ServiceNow/Prometheus/CMDB/etc.
# It consumes a client onboarding configuration + normalized evidence files first.
# Later, file adapters can be replaced with API/push adapters without changing the
# Safety Gate contract or Automation Queue UI.

SAFETY_GATE_CONFIG_FILE = Path(os.getenv("SAFETY_GATE_CONFIG_FILE", "config/client_onboarding/safety_gate_config.json"))

DEFAULT_SAFETY_GATE_CONFIG: Dict[str, Any] = {
    "schema_version": "safety_gate_config_v1",
    "client_id": "client_a",
    "client_name": "Client A",
    "rollout_model": "universal_evidence_docking_layer",
    "adapter_strategy": "file_first_api_later",
    "evidence_store_dir": "data/evidence",
    "rollback": {
        "backup_dir": "./bak",
        "storage_mode": "local_slm_bak_folder",
        "default_available_for_read_only_playbooks": True,
    },
    "pilot_defaults": {
        "missing_evidence_status": "pass",
        "missing_evidence_severity": "low",
        "missing_evidence_note": "Pilot mode: no external integration evidence provided yet; treated as PASS for human-in-loop validation.",
    },
    "policy": {
        "simulation": {"missing_evidence": "warn", "fail_blocks": False, "warn_blocks": False},
        "live_human_approved": {"missing_evidence": "warn", "fail_blocks": True, "warn_blocks": False},
        "autonomous": {"missing_evidence": "fail", "fail_blocks": True, "warn_blocks": True},
    },
    "checks": {
        "infrastructure_drift": {
            "enabled": True,
            "evidence_type": "drift_status",
            "adapter_mode": "file",
            "source_file": "drift_status.json",
            "integration_owner": "Infrastructure / Cloud Platform",
            "system_examples": ["Terraform state", "AWS Config", "Azure Resource Graph", "CMDB", "GitOps"],
            "onboarding_required": False,
        },
        "error_budget_slo": {
            "enabled": True,
            "evidence_type": "error_budget_status",
            "adapter_mode": "file",
            "source_file": "error_budget_status.json",
            "integration_owner": "SRE / Observability",
            "system_examples": ["Prometheus", "Grafana", "Datadog", "Dynatrace", "New Relic"],
            "onboarding_required": False,
        },
        "concurrent_activity": {
            "enabled": True,
            "evidence_type": "concurrent_activity",
            "adapter_mode": "file",
            "source_file": "concurrent_activity.json",
            "integration_owner": "Operations / Command Center",
            "system_examples": ["Open incidents", "Active deployments", "AWX/Ansible jobs", "PagerDuty"],
            "onboarding_required": False,
        },
        "change_window": {
            "enabled": True,
            "evidence_type": "change_conflict",
            "adapter_mode": "file",
            "source_file": "change_conflict.json",
            "integration_owner": "ITSM / Change Management",
            "system_examples": ["ServiceNow Change", "Jira Service Management", "Remedy", "Release calendar"],
            "onboarding_required": False,
        },
        "rollback_readiness": {
            "enabled": True,
            "evidence_type": "rollback_readiness",
            "adapter_mode": "file",
            "source_file": "rollback_readiness.json",
            "integration_owner": "Automation Engineering",
            "system_examples": ["Playbook metadata", "Rollback manifest", "./bak metadata"],
            "onboarding_required": False,
        },
        "blast_radius": {
            "enabled": True,
            "evidence_type": "blast_radius",
            "adapter_mode": "derived_then_file",
            "source_file": "blast_radius.json",
            "integration_owner": "Service Owner / CMDB Owner",
            "system_examples": ["ServiceNow CMDB", "Backstage", "Kubernetes", "Service graph"],
            "onboarding_required": False,
        },
    },
}


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _ensure_safety_gate_config_file() -> None:
    try:
        SAFETY_GATE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not SAFETY_GATE_CONFIG_FILE.exists():
            SAFETY_GATE_CONFIG_FILE.write_text(json.dumps(DEFAULT_SAFETY_GATE_CONFIG, indent=2), encoding="utf-8")
    except Exception:
        pipeline_debug("safety_gate_config_create_error", {"path": str(SAFETY_GATE_CONFIG_FILE), "traceback": traceback.format_exc()})


def _load_safety_gate_config() -> Dict[str, Any]:
    _ensure_safety_gate_config_file()
    try:
        if SAFETY_GATE_CONFIG_FILE.exists():
            loaded = json.loads(SAFETY_GATE_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return _deep_merge_dict(DEFAULT_SAFETY_GATE_CONFIG, loaded)
    except Exception:
        pipeline_debug("safety_gate_config_load_error", {"path": str(SAFETY_GATE_CONFIG_FILE), "traceback": traceback.format_exc()})
    return dict(DEFAULT_SAFETY_GATE_CONFIG)


def _evidence_store_dir(config: Dict[str, Any]) -> Path:
    raw = clean_text(config.get("evidence_store_dir") or "data/evidence")
    return Path(raw)


def _status_from_evidence(value: Any, default: str = "pass") -> str:
    token = clean_text(str(value or default)).lower().replace(" ", "_").replace("-", "_")
    if token in {"pass", "passed", "ok", "healthy", "clear"}:
        return "pass"
    if token in {"warn", "warning", "degraded"}:
        return "warn"
    if token in {"fail", "failed", "block", "blocked", "critical"}:
        return "fail"
    if token in {"unknown", "missing", "not_available"}:
        return "unknown"
    return default


def _request_match_score(item: Dict[str, Any], request: AutomationPreExecutionCheckRequest) -> int:
    score = 0
    request_fields = {
        "classification": normalize_classification_token(request.classification or ""),
        "playbook": clean_text(request.playbook or "").lower(),
        "resource": clean_text(request.target_host or "").lower(),
        "target_host": clean_text(request.target_host or "").lower(),
        "environment": "production",  # can be added to request later without changing evidence schema
    }
    for field, req_val in request_fields.items():
        item_val = clean_text(str(item.get(field, ""))).lower()
        if req_val and item_val and item_val == req_val:
            score += 3
        elif item_val in {"*", "all", "any"}:
            score += 1
    return score


def _load_evidence_from_api(check_name: str, check_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """MF-04: API Pull runner — fetch evidence from a configured external URL."""
    api_url = clean_text(check_cfg.get("api_url") or "")
    if not api_url:
        return None
    timeout = int(check_cfg.get("api_timeout_seconds") or 10)
    headers_cfg = check_cfg.get("api_headers") or {}
    headers = {str(k): str(v) for k, v in headers_cfg.items()} if isinstance(headers_cfg, dict) else {}
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(api_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
        elif isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = [payload]
        else:
            return None
        candidates = [i for i in items if isinstance(i, dict)]
        if candidates:
            # Tag the record with the pull source so the UI can show it
            result = candidates[0]
            result.setdefault("source_system", f"api_pull:{api_url}")
            result.setdefault("adapter_mode", "api")
            return result
    except Exception:
        pipeline_debug("safety_gate_api_pull_error", {"check": check_name, "url": api_url, "traceback": traceback.format_exc()})
    return None


def _load_universal_evidence(check_name: str, check_cfg: Dict[str, Any], config: Dict[str, Any], request: AutomationPreExecutionCheckRequest) -> Optional[Dict[str, Any]]:
    adapter_mode = clean_text(check_cfg.get("adapter_mode") or "file").lower()

    # MF-04: Try API pull first when configured as "api" or "api_first"
    if adapter_mode in {"api", "api_first"}:
        api_result = _load_evidence_from_api(check_name, check_cfg)
        if api_result:
            return api_result
        # Fall through to file if API pull fails and mode allows it
        if adapter_mode == "api":
            return None

    source_file = clean_text(check_cfg.get("source_file") or f"{check_cfg.get('evidence_type', check_name)}.json")
    path = _evidence_store_dir(config) / source_file
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload.get("items", [])
        elif isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = [payload]
        else:
            return None
        candidates = [item for item in items if isinstance(item, dict)]
        if not candidates:
            return None
        scored = sorted(((_request_match_score(item, request), item) for item in candidates), key=lambda x: x[0], reverse=True)
        best_score, best_item = scored[0]
        # If no specific match exists, use the first/default item only when it is explicitly generic.
        if best_score <= 0:
            generic = [item for item in candidates if clean_text(str(item.get("resource", ""))).lower() in {"*", "all", "any", ""}]
            if not generic:
                return None
            best_item = generic[0]
        return best_item
    except Exception:
        pipeline_debug("safety_gate_evidence_load_error", {"check": check_name, "path": str(path), "traceback": traceback.format_exc()})
        return None


def _missing_evidence_detail(check_name: str, check_cfg: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    pilot = config.get("pilot_defaults", {}) if isinstance(config.get("pilot_defaults"), dict) else {}
    status = _status_from_evidence(pilot.get("missing_evidence_status", "pass"), "pass")
    severity = clean_text(pilot.get("missing_evidence_severity", "low") or "low")
    note = clean_text(pilot.get("missing_evidence_note", "No evidence file found; using onboarding default."))
    return {
        "status": status,
        "severity": severity,
        "summary": f"{check_name.replace('_', ' ').title()} using onboarding default.",
        "evidence": note,
        "source_system": "client_onboarding_config",
        "adapter_mode": clean_text(check_cfg.get("adapter_mode", "file")),
        "evidence_type": clean_text(check_cfg.get("evidence_type", check_name)),
        "source_file": str(_evidence_store_dir(config) / clean_text(check_cfg.get("source_file") or f"{check_name}.json")),
        "integration_owner": clean_text(check_cfg.get("integration_owner", "client_owner")),
        "integration_required_now": bool(check_cfg.get("onboarding_required", False)),
    }


_EVIDENCE_TTL_HOURS_DEFAULT = 72


def _check_evidence_ttl(evidence: Dict[str, Any], check_cfg: Dict[str, Any]) -> Optional[str]:
    """Return a staleness warning string if evidence exceeds TTL, else None.

    Evidence with no timestamp is treated as stale — it was likely written before
    TTL enforcement was added and has an unknown age.
    """
    ttl_hours = int(check_cfg.get("ttl_hours") or _EVIDENCE_TTL_HOURS_DEFAULT)
    checked_at_str = evidence.get("checked_at") or evidence.get("generated_at") or ""
    if not checked_at_str:
        return f"Evidence has no timestamp — age unknown (TTL: {ttl_hours}h). Refresh required."
    try:
        from datetime import datetime, timezone
        checked_at = datetime.fromisoformat(str(checked_at_str).replace("Z", "+00:00"))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - checked_at).total_seconds() / 3600
        if age_hours > ttl_hours:
            return f"Evidence is {age_hours:.0f}h old (TTL: {ttl_hours}h). Refresh required."
    except Exception:
        return f"Evidence timestamp could not be parsed — treating as stale (TTL: {ttl_hours}h)."
    return None


def _evidence_to_check_detail(check_name: str, evidence: Dict[str, Any], check_cfg: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    status = _status_from_evidence(evidence.get("status", evidence.get("gate_status", "pass")), "pass")
    severity = clean_text(evidence.get("severity", "low") or "low").lower()

    # MF-03: TTL enforcement — stale evidence blocks autonomous mode, warns others
    ttl_warning = _check_evidence_ttl(evidence, check_cfg)
    if ttl_warning and status == "pass":
        execution_mode = clean_text(check_cfg.get("_execution_mode", "")).lower()
        status = "fail" if execution_mode == "autonomous" else "warn"

    detail = {
        "status": status,
        "severity": severity,
        "summary": clean_text(evidence.get("summary") or evidence.get("message") or f"{check_name.replace('_', ' ').title()} evidence received."),
        "evidence": clean_text(evidence.get("evidence") or evidence.get("details") or evidence.get("reason") or "Evidence provided by Universal Evidence Adapter."),
        "source_system": clean_text(evidence.get("source_system") or "file_adapter"),
        "source_record_id": clean_text(evidence.get("source_record_id") or ""),
        "checked_at": clean_text(evidence.get("checked_at") or utc_now_iso()),
        "confidence": evidence.get("confidence", None),
        "adapter_mode": clean_text(check_cfg.get("adapter_mode", "file")),
        "evidence_type": clean_text(check_cfg.get("evidence_type", check_name)),
        "source_file": str(_evidence_store_dir(config) / clean_text(check_cfg.get("source_file") or f"{check_name}.json")),
        "raw_evidence": evidence,
    }
    if ttl_warning:
        detail["ttl_warning"] = ttl_warning
    return detail


def _configured_safety_check(check_name: str, config: Dict[str, Any], request: AutomationPreExecutionCheckRequest) -> Dict[str, Any]:
    checks_cfg = config.get("checks", {}) if isinstance(config.get("checks"), dict) else {}
    check_cfg = checks_cfg.get(check_name, {}) if isinstance(checks_cfg.get(check_name), dict) else {}
    if check_cfg and check_cfg.get("enabled") is False:
        return {
            "status": "pass",
            "severity": "low",
            "summary": f"{check_name.replace('_', ' ').title()} disabled by client onboarding configuration.",
            "evidence": "Check disabled in safety_gate_config.json.",
            "source_system": "client_onboarding_config",
            "disabled": True,
        }
    evidence = _load_universal_evidence(check_name, check_cfg, config, request)
    if evidence:
        check_cfg_with_mode = {**check_cfg, "_execution_mode": clean_text(request.execution_mode or "")}
        return _evidence_to_check_detail(check_name, evidence, check_cfg_with_mode, config)
    return _missing_evidence_detail(check_name, check_cfg, config)


def _refresh_live_evidence(request: AutomationPreExecutionCheckRequest, config: Dict[str, Any]) -> Dict[str, Any]:
    """Run a live dry-mode snapshot + dry-run simulation and write fresh evidence files.

    This is called at the start of every Safety Gate evaluation so the gate
    never reads stale static files.  All operations use mode='dry' — no
    infrastructure changes occur.  Returns a summary of what was written.
    """
    from datetime import datetime, timezone
    evidence_dir = _evidence_store_dir(config)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    ci_name = clean_text(request.target_host or request.classification or "unknown-ci")
    now_iso = utc_now_iso()

    # Select snapshot mode based on what is actually available in this environment.
    # "dry" is the fallback when neither Docker nor CMDB is reachable — it returns
    # synthetic state and must not be treated as a real infrastructure signal.
    # In production: CMDB_ENABLED=true → cmdb; LAB containers reachable → lab.
    _cmdb_enabled = os.getenv("CMDB_ENABLED", "").lower() in {"1", "true", "yes"}
    _lab_enabled = os.getenv("LAB_SNAPSHOT_ENABLED", "").lower() in {"1", "true", "yes"}
    if _cmdb_enabled:
        _snapshot_mode = "cmdb"
    elif _lab_enabled:
        _snapshot_mode = "lab"
    else:
        _snapshot_mode = "dry"  # POC/dev only — returns synthetic state

    refresh_summary: Dict[str, Any] = {
        "ci_name": ci_name,
        "refreshed_at": now_iso,
        "snapshot_mode": _snapshot_mode,
        "snapshot_mode_note": (
            "CMDB integration active" if _snapshot_mode == "cmdb"
            else "Lab Docker containers active" if _snapshot_mode == "lab"
            else "Synthetic dry-mode (no live infrastructure access) — set CMDB_ENABLED=true or LAB_SNAPSHOT_ENABLED=true for real state"
        ),
        "steps": [],
    }

    # ── Step 1: Infrastructure snapshot ───────────────────────────────────────
    snapshot: Dict[str, Any] = {}
    drift_detected = False
    # Default to warn if snapshot fails — unknown state is never a pass
    snapshot_status = "warn"
    snapshot_summary = "Snapshot capture failed; infrastructure state unknown."
    snapshot_evidence = f"SnapshotEngine could not complete {_snapshot_mode}-mode capture at safety gate evaluation time."
    try:
        from tools.snapshot_engine import SnapshotEngine
        engine = SnapshotEngine()
        snapshot = engine.capture(ci_name=ci_name, mode=_snapshot_mode, notes=f"safety_gate pre-check for {request.queue_id or 'unknown'}")
        state = snapshot.get("state", {})
        # Upgrade to pass once capture succeeds; check for anomalies
        snapshot_status = "pass"
        _mode_label = {
            "lab": "live Docker container",
            "cmdb": "CMDB read-only",
            "dry": "synthetic dry-mode (simulated state only — not live host data)",
        }.get(_snapshot_mode, _snapshot_mode)
        snapshot_summary = f"No infrastructure drift detected ({_mode_label} snapshot)."
        snapshot_evidence = f"Snapshot captured via SnapshotEngine mode={_snapshot_mode}."
        if _snapshot_mode == "dry":
            snapshot_evidence += " Set CMDB_ENABLED=true or LAB_SNAPSHOT_ENABLED=true to capture real infrastructure state."
        if isinstance(state, dict):
            anomalies = state.get("anomalies") or state.get("drift_items") or []
            if anomalies:
                drift_detected = True
                snapshot_status = "warn"
                snapshot_summary = f"Dry-mode snapshot reported {len(anomalies)} potential drift item(s)."
                snapshot_evidence = f"Drift items: {', '.join(str(a) for a in anomalies[:5])}"
        refresh_summary["steps"].append({"step": "snapshot", "status": "ok", "snapshot_id": snapshot.get("snapshot_id")})
    except Exception:
        refresh_summary["steps"].append({"step": "snapshot", "status": "error", "detail": traceback.format_exc(limit=3)})

    # Write fresh drift_status.json so _configured_safety_check reads current data
    drift_evidence = {
        "status": snapshot_status,
        "severity": "medium" if snapshot_status != "pass" else "low",
        "summary": snapshot_summary,
        "evidence": snapshot_evidence,
        "source_system": f"snapshot_engine_{_snapshot_mode}",
        "snapshot_id": snapshot.get("snapshot_id", ""),
        "checked_at": now_iso,
    }
    try:
        (evidence_dir / "drift_status.json").write_text(json.dumps(drift_evidence, indent=2), encoding="utf-8")
        refresh_summary["drift_status_written"] = True
    except Exception:
        refresh_summary["drift_status_write_error"] = traceback.format_exc(limit=2)

    # ── Step 2: Simulation of the playbook (always dry — NEVER on the lab/cmdb snapshot) ──
    # Safety Check must never execute anything. Capture (Step 1) may use lab or cmdb mode
    # to read real state, but simulation always creates and uses a *separate* dry snapshot
    # so apply_remediation() cannot trigger real Ansible execution on any live target.
    sim_status = "pass"
    sim_summary = "Dry-run simulation completed — no execution errors."
    sim_evidence = "Playbook simulated against a separate dry-mode snapshot; no infrastructure changes applied."
    try:
        playbook_name = clean_text(request.playbook or request.classification or "")
        if playbook_name:
            from tools.snapshot_engine import SnapshotEngine as _SE
            _sim_engine = _SE()
            # Always capture a new dry snapshot for simulation — isolated from the
            # infrastructure-state snapshot above, so lab mode cannot reach this path.
            _sim_snap = _sim_engine.capture(
                ci_name=ci_name,
                mode="dry",  # forced — simulation is never lab or cmdb
                notes=f"safety_gate simulation-only for {request.queue_id or 'unknown'}",
            )
            _sim_snap_id = _sim_snap.get("snapshot_id", "")
            if _sim_snap_id:
                sim_result = _sim_engine.apply_remediation(
                    snapshot_id=_sim_snap_id,
                    playbook_name=playbook_name,
                    changes=[{"action": "dry_run", "target": ci_name, "description": f"Simulate {playbook_name}"}],
                )
                # apply_remediation returns the updated snapshot dict;
                # remediation outcome is at snapshot["remediation"]["result"]["status"]
                rem_status = ""
                if isinstance(sim_result, dict):
                    rem = sim_result.get("remediation") or {}
                    rem_result = rem.get("result") or {}
                    rem_status = str(rem_result.get("status", "")).lower()
                if rem_status == "failed":
                    sim_status = "warn"
                    sim_summary = f"Dry-run simulation reported issues for playbook '{playbook_name}'."
                    sim_evidence = str(rem_result)
                # "simulated" is the expected dry-mode result — not a failure
        refresh_summary["steps"].append({"step": "simulation", "status": "ok", "playbook": playbook_name, "mode": "dry"})
    except Exception:
        # Simulation failure is advisory — the playbook may not exist in the registry yet
        sim_status = "warn"
        sim_summary = "Dry-run simulation encountered an error; treat as advisory warning."
        sim_evidence = traceback.format_exc(limit=3)
        refresh_summary["steps"].append({"step": "simulation", "status": "warn", "detail": sim_evidence[:200]})

    # ── Step 3: Error tolerance / rollback readiness ──────────────────────────
    # Derive from blast radius estimate and simulation result
    blast = _estimate_blast_radius(request.classification, request.playbook, request.risk_tier)
    rollback_status = "pass"
    rollback_summary = "Rollback artifacts available; error tolerance within acceptable range."
    rollback_evidence = f"Blast radius: {blast.get('level', 'unknown')}. Scope: {blast.get('scope', 'unknown')}."
    if blast.get("level") in {"high", "critical"}:
        rollback_status = "warn"
        rollback_summary = f"High blast radius ({blast.get('level')}) — verify rollback plan before executing."
        rollback_evidence = blast.get("reason", rollback_evidence)

    rollback_evidence_payload = {
        "status": rollback_status,
        "severity": blast.get("level", "medium"),
        "summary": rollback_summary,
        "evidence": rollback_evidence,
        "source_system": "blast_radius_estimator_live",
        "blast_radius_level": blast.get("level"),
        "blast_radius_scope": blast.get("scope"),
        "simulation_status": sim_status,
        "simulation_summary": sim_summary,
        "checked_at": now_iso,
    }
    try:
        (evidence_dir / "rollback_readiness.json").write_text(json.dumps(rollback_evidence_payload, indent=2), encoding="utf-8")
        refresh_summary["rollback_readiness_written"] = True
    except Exception:
        refresh_summary["rollback_readiness_write_error"] = traceback.format_exc(limit=2)

    # Write error_budget_status.json — combines simulation + blast radius signals
    error_budget_payload = {
        "status": sim_status,
        "severity": "medium" if sim_status != "pass" else "low",
        "summary": sim_summary,
        "evidence": sim_evidence[:500] if sim_evidence else "Simulation clean.",
        "source_system": "dry_run_simulation",
        "blast_radius": blast.get("level"),
        "checked_at": now_iso,
    }
    try:
        (evidence_dir / "error_budget_status.json").write_text(json.dumps(error_budget_payload, indent=2), encoding="utf-8")
        refresh_summary["error_budget_written"] = True
    except Exception:
        refresh_summary["error_budget_write_error"] = traceback.format_exc(limit=2)

    return refresh_summary


def _build_pre_execution_safety_gate(request: AutomationPreExecutionCheckRequest) -> Dict[str, Any]:
    config = _load_safety_gate_config()

    # Trigger live snapshot + dry-run simulation + error tolerance before reading evidence.
    # This ensures the Safety Gate never evaluates stale static files.
    live_refresh = _refresh_live_evidence(request, config)

    blast_radius = _estimate_blast_radius(request.classification, request.playbook, request.risk_tier)
    now = utc_now_iso()

    # Step 5A changes the gate from hardcoded simulation to a client-onboarding
    # evidence-docking model. It still works on Day 1 with local JSON files, and
    # the same contract can later be fed by ServiceNow/Prometheus/CMDB/etc.
    checks = {
        "infrastructure_drift": _configured_safety_check("infrastructure_drift", config, request),
        "error_budget_slo": _configured_safety_check("error_budget_slo", config, request),
        "concurrent_activity": _configured_safety_check("concurrent_activity", config, request),
        "change_window": _configured_safety_check("change_window", config, request),
        "rollback_readiness": _configured_safety_check("rollback_readiness", config, request),
    }

    # Blast radius can come from client evidence if available; otherwise derive it
    # from classification/playbook/risk so rollout does not wait for CMDB maturity.
    blast_evidence = _load_universal_evidence(
        "blast_radius",
        (config.get("checks", {}) or {}).get("blast_radius", {}),
        config,
        request,
    )
    if blast_evidence:
        br_cfg = {**(config.get("checks", {}) or {}).get("blast_radius", {}), "_execution_mode": clean_text(request.execution_mode or "")}
        checks["blast_radius"] = _evidence_to_check_detail("blast_radius", blast_evidence, br_cfg, config)
    else:
        checks["blast_radius"] = {
            "status": "pass" if blast_radius.get("level") in {"low", "medium"} else "warn",
            "severity": blast_radius.get("level", "medium"),
            "summary": f"Estimated blast radius is {blast_radius.get('level', 'medium')}.",
            "evidence": blast_radius.get("reason", "Derived locally from classification, playbook and risk tier."),
            "scope": blast_radius.get("scope", ""),
            "source_system": "derived_local_estimator",
            "adapter_mode": "derived_then_file",
        }

    blocking_checks = [
        name for name, detail in checks.items()
        if str(detail.get("status", "")).lower() in {"fail", "blocked"}
    ]
    warning_checks = [
        name for name, detail in checks.items()
        if str(detail.get("status", "")).lower() in {"warn", "warning", "unknown"}
    ]

    gate_status = "blocked" if blocking_checks else "warning" if warning_checks else "passed"

    return {
        "status": "ok",
        "schema_version": "pre_execution_safety_gate_v2_configured",
        "queue_id": request.queue_id,
        "incident_id": request.incident_id,
        "classification": _automation_classification_key(request.classification or ""),
        "playbook": clean_text(request.playbook or ""),
        "target_host": clean_text(request.target_host or ""),
        "risk_tier": _automation_safety_level(request.risk_tier),
        "execution_mode": clean_text(request.execution_mode or "simulation").lower(),
        "requested_by": clean_text(request.requested_by or "engineer"),
        "checked_at": now,
        "gate_status": gate_status,
        "safe_to_approve": gate_status in {"passed", "warning"},
        "safe_to_execute_live": gate_status == "passed",
        "enforcement_mode": "configuration_backed_advisory",
        "human_override_required": gate_status == "warning",
        "safety_gate_config_file": str(SAFETY_GATE_CONFIG_FILE),
        "evidence_store_dir": str(_evidence_store_dir(config)),
        "adapter_strategy": clean_text(config.get("adapter_strategy", "file_first_api_later")),
        "rollout_model": clean_text(config.get("rollout_model", "universal_evidence_docking_layer")),
        "checks": checks,
        **_rollback_backup_dir_payload(),
        "blocking_checks": blocking_checks,
        "warning_checks": warning_checks,
        "message": "Pre-execution safety gate completed using client onboarding safety_gate_config.json and Universal Evidence Adapter files.",
        "live_refresh": live_refresh,
    }


@router.post("/automation/pre_execution_check")
def automation_pre_execution_check_route(request: AutomationPreExecutionCheckRequest, http_request: Request) -> Dict[str, Any]:
    principal = _require_principal(http_request, "l1")
    try:
        result = _build_pre_execution_safety_gate(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Bind the run to the queue entry, plan hash and target so approval/execution can rely on it.
    entry = _engine.get_queue_entry(request.queue_id) if request.queue_id else None
    if entry:
        params = {
            "classification": request.classification or entry.get("classification", ""),
            "playbook": request.playbook or _first_playbook(entry),
            "target_host": request.target_host or "",
            "risk_tier": request.risk_tier or "medium",
            "execution_mode": request.execution_mode or "simulation",
        }
        try:
            run = exec_auth.record_gate_run(entry, result, params, principal.id)
        except _engine.QueueBusyError as busy:
            raise _busy_http(busy)
        result["gate_run_id"] = run["gate_run_id"]
        result["plan_hash"] = run["plan_hash"]
    return result


@router.get("/automation/safety_gate/config")
def automation_safety_gate_config_route() -> Dict[str, Any]:
    try:
        config = _load_safety_gate_config()
        return {
            "status": "ok",
            "config_file": str(SAFETY_GATE_CONFIG_FILE),
            "evidence_store_dir": str(_evidence_store_dir(config)),
            "config": config,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


AUTOMATION_FEEDBACK_LOG_FILE = Path(os.getenv("AUTOMATION_FEEDBACK_LOG_FILE", "data/automation_execution_feedback.jsonl"))
AUTOMATION_CONFIDENCE_FILE = Path(os.getenv("AUTOMATION_CONFIDENCE_FILE", "data/automation_confidence_summary.json"))
# Local rollback artifacts for the lab/dev SLM project. In Docker this normally
# resolves to /app/bak; on local Windows/Mac/Linux it resolves to <slm>/bak.
SLM_ROOT_DIR = Path(os.getenv("AIOPS_SLM_ROOT", ".")).resolve()
ROLLBACK_BACKUP_DIR = Path(os.getenv("AIOPS_ROLLBACK_BACKUP_DIR", str(SLM_ROOT_DIR / "bak"))).resolve()


def _rollback_backup_dir_payload() -> Dict[str, Any]:
    try:
        ROLLBACK_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pipeline_debug("rollback_backup_dir_create_error", {"path": str(ROLLBACK_BACKUP_DIR), "traceback": traceback.format_exc()})
    return {
        "rollback_backup_dir": str(ROLLBACK_BACKUP_DIR),
        "rollback_storage_mode": "local_slm_bak_folder",
        "rollback_storage_note": "For this lab build, rollback artifacts/backups are stored physically under the SLM bak folder.",
    }


def _automation_learning_key(classification: Optional[str], playbook: Optional[str], action: Optional[str] = "") -> str:
    cls = _automation_classification_key(classification or "")
    pb = clean_text(playbook or "").strip().lower()
    act = clean_text(action or "").strip().lower()
    token = cls or pb or act or "unknown_automation"
    token = re.sub(r"[^a-z0-9_.-]+", "_", token)
    return token.strip("_") or "unknown_automation"


def _improvement_candidate_enabled_for_key(key: str) -> bool:
    """Return True if an improvement candidate (source=execution_feedback) is enabled for this key.

    When an engineer reviews, approves, and enables an improvement candidate they
    have acknowledged the pending feedback.  The next clean execution will confirm
    the fix, but the eligibility gate should not stay permanently blocked waiting
    for that run.
    """
    try:
        from automation_candidate_registry import list_automation_candidates
        if list_automation_candidates is None:
            return False
        result = list_automation_candidates() if callable(list_automation_candidates) else {}
        candidates = result.get("candidates", []) if isinstance(result, dict) else []
        norm_key = clean_text(key).lower().replace(" ", "_")
        for c in candidates:
            if not isinstance(c, dict):
                continue
            if clean_text(str(c.get("source", ""))).lower() != "execution_feedback":
                continue
            c_class = clean_text(str(c.get("classification", ""))).lower().replace(" ", "_")
            if c_class == norm_key and clean_text(str(c.get("status", ""))).lower() in {"enabled", "approved"}:
                return True
    except Exception:
        pass
    return False


def _read_automation_feedback_records() -> List[Dict[str, Any]]:
    path = AUTOMATION_FEEDBACK_LOG_FILE
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except Exception:
                continue
    except Exception:
        return []
    return records


def _calculate_automation_confidence(records: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    scoped = [r for r in records if r.get("automation_key") == key]
    total_runs = len(scoped)
    successful_runs = 0
    failed_runs = 0
    clean_successful_runs = 0
    pending_feedback = 0
    review_required_count = 0

    for r in scoped:
        status = clean_text(r.get("execution_status", "")).lower()
        resolved = bool(r.get("resolved_by_automation"))
        additional = bool(r.get("additional_steps_required"))
        review_required = bool(r.get("playbook_review_required"))
        if status in {"failed", "completed_with_failures", "error"} or not resolved:
            failed_runs += 1
        else:
            successful_runs += 1
            if not additional and not review_required:
                clean_successful_runs += 1
        if additional or review_required:
            pending_feedback += 1
            review_required_count += 1

    last_three = scoped[-3:]
    failed_runs_last_3 = 0
    for r in last_three:
        status = clean_text(r.get("execution_status", "")).lower()
        resolved = bool(r.get("resolved_by_automation"))
        if status in {"failed", "completed_with_failures", "error"} or not resolved:
            failed_runs_last_3 += 1

    # Conservative maturity score: starts at 0.40, rewards clean human-approved runs,
    # slows growth when engineers add manual steps, and penalizes failed runs.
    confidence = 0.40
    confidence += min(clean_successful_runs, 5) * 0.11
    confidence += max(successful_runs - clean_successful_runs, 0) * 0.03
    confidence -= failed_runs * 0.12
    confidence -= pending_feedback * 0.06
    confidence = max(0.05, min(0.95, round(confidence, 2)))

    latest = scoped[-1] if scoped else {}
    latest_safety_gate = clean_text(latest.get("safety_gate_status", "")).lower() or "unknown"
    risk_tier = _automation_safety_level(latest.get("risk_tier", "medium"))
    rollback_available = bool(latest.get("rollback_available", True))

    # Feedback is resolved if either:
    # (a) the last execution was clean — improvement is confirmed working, OR
    # (b) an engineer has approved+enabled an improvement candidate for this key —
    #     they have explicitly reviewed the feedback; the next clean run will confirm.
    last_record = scoped[-1] if scoped else {}
    last_run_clean = (
        bool(last_record.get("resolved_by_automation"))
        and not bool(last_record.get("additional_steps_required"))
        and not bool(last_record.get("playbook_review_required"))
    )
    feedback_resolved = last_run_clean or _improvement_candidate_enabled_for_key(key)
    effective_pending_feedback = 0 if feedback_resolved else pending_feedback

    autonomous_eligible = (
        confidence >= 0.70
        and clean_successful_runs >= 3
        and failed_runs_last_3 == 0
        and effective_pending_feedback == 0
        and latest_safety_gate in {"passed", "pass"}
        and rollback_available
        and risk_tier in {"low", "medium"}
    )

    reasons: List[str] = []
    if confidence < 0.70:
        reasons.append("Automation confidence below 0.70")
    if clean_successful_runs < 3:
        reasons.append("Needs at least 3 clean human-approved successful runs")
    if failed_runs_last_3 > 0:
        reasons.append("Failed run exists in last 3 executions")
    if effective_pending_feedback > 0:
        reasons.append("Pending engineer feedback or playbook review exists — approve and enable an improvement candidate, or run a clean execution after the updated playbook is live")
    if latest_safety_gate not in {"passed", "pass"}:
        reasons.append("Latest safety gate has not passed")
    if not rollback_available:
        reasons.append("Rollback or safe-stop is not confirmed")
    if risk_tier not in {"low", "medium"}:
        reasons.append("Risk tier is too high for autonomous promotion")

    return {
        "automation_key": key,
        "automation_confidence": confidence,
        "mode": "autonomous_ready" if autonomous_eligible else "human_in_the_loop",
        "total_runs": total_runs,
        "successful_runs": successful_runs,
        "clean_successful_runs": clean_successful_runs,
        "failed_runs": failed_runs,
        "failed_runs_last_3": failed_runs_last_3,
        "pending_feedback": pending_feedback,
        "playbook_review_required_count": review_required_count,
        "latest_safety_gate": latest_safety_gate,
        "rollback_available": rollback_available,
        **_rollback_backup_dir_payload(),
        "risk_tier": risk_tier,
        "autonomous_eligible": autonomous_eligible,
        "ready_for_autonomous_checkbox_enabled": autonomous_eligible,
        "eligibility_reasons": reasons,
        "updated_at": utc_now_iso(),
    }


def _write_automation_confidence_summary(summary: Dict[str, Any]) -> None:
    try:
        with _CONFIDENCE_LOCK:
            AUTOMATION_CONFIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
            existing: Dict[str, Any] = {}
            if AUTOMATION_CONFIDENCE_FILE.exists():
                existing_obj = json.loads(AUTOMATION_CONFIDENCE_FILE.read_text(encoding="utf-8", errors="ignore") or "{}")
                if isinstance(existing_obj, dict):
                    existing = existing_obj
            existing[summary["automation_key"]] = summary
            tmp = AUTOMATION_CONFIDENCE_FILE.with_suffix(AUTOMATION_CONFIDENCE_FILE.suffix + ".tmp")
            tmp.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(AUTOMATION_CONFIDENCE_FILE)
    except Exception:
        pipeline_debug("automation_confidence_write_error", {"traceback": traceback.format_exc()})


@router.post("/automation/execution_feedback")
def automation_execution_feedback_route(request: AutomationExecutionFeedbackRequest) -> Dict[str, Any]:
    try:
        key = _automation_learning_key(request.classification, request.playbook, request.action)
        feedback_id = f"aef_{uuid.uuid4().hex[:12]}"
        record = {
            "feedback_id": feedback_id,
            "automation_key": key,
            "queue_id": clean_text(request.queue_id or ""),
            "incident_id": clean_text(request.incident_id or ""),
            "classification": _automation_classification_key(request.classification or ""),
            "action": clean_text(request.action or ""),
            "playbook": clean_text(request.playbook or ""),
            "target_host": clean_text(request.target_host or ""),
            "execution_status": clean_text(request.execution_status or "completed"),
            "execution_mode": clean_text(request.execution_mode or "simulation").lower(),
            "resolved_by_automation": bool(request.resolved_by_automation),
            "additional_steps_required": bool(request.additional_steps_required),
            "playbook_review_required": bool(request.playbook_review_required),
            "engineer_notes": clean_text(request.engineer_notes or ""),
            "submitted_by": clean_text(request.submitted_by or "engineer"),
            "submitted_at": utc_now_iso(),
            "safety_gate_status": clean_text(request.safety_gate_status or "").lower(),
            "rollback_available": bool(request.rollback_available),
            **_rollback_backup_dir_payload(),
            "risk_tier": _automation_safety_level(request.risk_tier),
        }
        AUTOMATION_FEEDBACK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(str(AUTOMATION_FEEDBACK_LOG_FILE), record)
        try:
            import resolution_outcome as _ro
            _exec_mode = record["execution_mode"]
            # Simulation runs are not real outcomes — skip them.
            if _exec_mode != "simulation":
                _valid_modes = {"manual", "human_approved", "autonomous"}
                _exec_mode = _exec_mode if _exec_mode in _valid_modes else "manual"
                # execution_status dominates: a failed/errored execution is always "failed"
                _exec_status = record.get("execution_status", "").lower()
                if _exec_status in ("failed", "error", "errored"):
                    _outcome_status = "failed"
                elif record["resolved_by_automation"]:
                    _outcome_status = "resolved"
                elif record["additional_steps_required"]:
                    _outcome_status = "partial"
                else:
                    _outcome_status = "failed"
                _ro.record_outcome(
                    incident_id=record["incident_id"],
                    classification=record["classification"],
                    resolution_id="",
                    playbook_id=record["playbook"],
                    execution_mode=_exec_mode,
                    ai_recommendation_used="yes",
                    outcome_status=_outcome_status,
                    confirmed_by=record["submitted_by"],
                    notes=record["engineer_notes"],
                )
        except Exception:
            pass
        records = _read_automation_feedback_records()
        confidence_summary = _calculate_automation_confidence(records, key)
        _write_automation_confidence_summary(confidence_summary)
        return {
            "status": "ok",
            "feedback_id": feedback_id,
            "message": "Execution feedback captured and automation confidence recalculated.",
            "feedback": record,
            "confidence_summary": confidence_summary,
            "next_action": "playbook_review" if record["playbook_review_required"] or record["additional_steps_required"] else "continue_human_in_loop_validation",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/automation/confidence_summary")
def automation_confidence_summary_route(request: AutomationConfidenceSummaryRequest) -> Dict[str, Any]:
    try:
        key = _automation_learning_key(request.classification, request.playbook, request.action)
        records = _read_automation_feedback_records()
        summary = _calculate_automation_confidence(records, key)
        return {"status": "ok", "confidence_summary": summary}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))



@router.post("/automation/autonomous/enable")
def automation_autonomous_enable_route(request: AutomationAutonomousEnableRequest) -> Dict[str, Any]:
    """Persist Ready-for-Autonomous state on the approved registry mapping.

    Enabling is allowed only when the same backend confidence rules mark the
    automation as autonomous-eligible. Disabling is always allowed.
    """
    if set_autonomous_registry_status is None:
        raise HTTPException(status_code=500, detail="automation candidate registry autonomous controls not available")
    try:
        classification_key = _automation_classification_key(request.classification)
        summary = _calculate_automation_confidence(
            _read_automation_feedback_records(),
            _automation_learning_key(classification_key, request.playbook, request.action),
        )
        if bool(request.enabled) and not bool(summary.get("autonomous_eligible")):
            return {
                "status": "blocked",
                "message": "Automation is not ready for autonomous enablement.",
                "confidence_summary": summary,
                "eligibility_reasons": summary.get("eligibility_reasons", []),
            }
        # Ledger gate: require sufficient real outcome history before enabling autonomous mode.
        # Fail CLOSED on exception — a safety gate must not fail open.
        if bool(request.enabled):
            _ledger_blocked = True
            _gate = {"tier": "hold_for_approval", "reason": "Ledger gate check failed — defaulting to hold for approval."}
            try:
                from resolution_outcome import autonomous_gate as _ledger_gate
                _gate = _ledger_gate(classification_key, tenant_id=request.tenant_id or "default")
                _ledger_blocked = _gate["tier"] == "hold_for_approval"
            except Exception:
                pass  # _ledger_blocked stays True, _gate has the safe default
            if _ledger_blocked:
                return {
                    "status": "blocked",
                    "message": "Autonomous enablement blocked by outcome ledger gate.",
                    "ledger_gate": _gate,
                    "confidence_summary": summary,
                    "eligibility_reasons": [_gate["reason"]],
                }
        result = set_autonomous_registry_status(
            classification=classification_key,
            action=request.action or "",
            playbook=request.playbook or "",
            enabled=bool(request.enabled),
            requested_by=request.requested_by or "engineer",
            confidence_summary=summary,
            notes=request.notes or "",
        )
        if result.get("status") != "ok":
            raise HTTPException(status_code=400, detail=result)
        return {
            "status": "ok",
            "autonomous_enabled": bool(request.enabled),
            "confidence_summary": summary,
            "registry_entry": result.get("registry_entry", {}),
            "message": "Autonomous registry status updated. Telemetry autonomous orchestration may only use mappings with autonomous_enabled=true.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/automation/autonomous/eligible_mapping")
def automation_autonomous_eligible_mapping_route(request: AutomationAutonomousEligibilityRequest) -> Dict[str, Any]:
    """Guardrail endpoint for telemetry/autonomous orchestration.

    A telemetry trigger should call this before any autonomous execution. Human-in-loop
    mappings return allowed=false even if they are approved for manual execution.
    """
    if is_autonomous_registry_mapping_allowed is None:
        raise HTTPException(status_code=500, detail="automation candidate registry autonomous controls not available")
    try:
        result = is_autonomous_registry_mapping_allowed(
            classification=_automation_classification_key(request.classification),
            action=request.action or "",
            playbook=request.playbook or "",
        )
        # Enforce ledger gate: if outcome history says hold, override allowed to False.
        # This ensures callers checking only the top-level `allowed` field get the correct answer.
        try:
            from resolution_outcome import autonomous_gate as _ledger_gate
            _cls = _automation_classification_key(request.classification)
            _tid = request.tenant_id or "default"
            _gate = _ledger_gate(_cls, tenant_id=_tid)
            result["ledger_gate"] = _gate
            if _gate["tier"] == "hold_for_approval":
                result["allowed"] = False
                result["reason"] = result.get("reason", "") + f" [Ledger gate: {_gate['reason']}]"
        except Exception:
            # Fail closed: if ledger gate check fails, mark not allowed
            result["allowed"] = False
            result["ledger_gate"] = {"tier": "hold_for_approval", "reason": "Ledger gate check failed — defaulting to hold for approval."}
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/automation/queue")
def automation_queue_route(request: AutomationPlanRequest, http_request: Request) -> Dict[str, Any]:
    if queue_automation_plan is None:
        raise HTTPException(status_code=500, detail="automation_engine not available")
    principal = _require_principal(http_request, "l2")
    try:
        classification_key = _automation_classification_key(request.classification)
        # An autonomous intent is created only by the server, for a service identity, when the registry
        # mapping is enabled and the outcome ledger permits autonomous execution.
        intent = None
        if principal.kind == "service" and _autonomous_mapping_allowed(classification_key):
            try:
                tier = str(_ledger_gate_for(classification_key, _engine.DEFAULT_TENANT_ID).get("tier", ""))
            except Exception:
                tier = ""
            if tier == "auto_execute":
                intent = {"kind": "autonomous", "created_by": principal.id, "created_at": utc_now_iso(), "ledger_tier": tier}
        result = queue_automation_plan(
            incident_id=request.incident_id,
            classification=classification_key,
            linked_resolution=request.linked_resolution,
            linked_playbook=request.linked_playbook,
            requested_by=principal.id if principal.kind == "service" else (request.requested_by or "system"),
            intent=intent,
        )
        queue_entry = result.get("queue_entry", {}) if isinstance(result, dict) else {}
        plan = queue_entry.get("plan", {}) if isinstance(queue_entry, dict) else {}
        postgres_write_safe(
            "insert_automation_queue",
            db_insert_automation_queue,
            queue_id=queue_entry.get("queue_id"),
            incident_id=queue_entry.get("incident_id"),
            classification=queue_entry.get("classification") or request.classification,
            requested_by=queue_entry.get("requested_by") or request.requested_by or "system",
            status=queue_entry.get("status") or "queued",
            plan=plan or {},
        )
        if isinstance(result, dict):
            result["postgres_parallel_write"] = postgres_enabled()
        return result
    except _engine.QueueBusyError as busy:
        raise _busy_http(busy)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/automation/queue/list")
def automation_queue_list_route(limit: int = 100, status: Optional[str] = None) -> Dict[str, Any]:
    if list_automation_queue is None:
        raise HTTPException(status_code=500, detail="automation_engine not available")
    try:
        return list_automation_queue(limit=limit, status_filter=status)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/automation/refresh_queue")
def automation_refresh_queue_route(request: AutomationRefreshRequest) -> Dict[str, Any]:
    if refresh_automation_queue is None:
        raise HTTPException(status_code=500, detail="automation_engine refresh not available")
    try:
        return refresh_automation_queue(request.queue_id)
    except _engine.QueueBusyError as busy:
        raise _busy_http(busy)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/automation/approve")
def automation_approve_route(request: AutomationApproveRequest, http_request: Request) -> Dict[str, Any]:
    if approve_automation is None:
        raise HTTPException(status_code=500, detail="automation_engine not available")
    principal = _require_principal(http_request, "l3")
    if principal.kind != "human":
        raise HTTPException(status_code=403, detail="service identities cannot approve plans")
    entry = _engine.get_queue_entry(request.queue_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"queue entry '{request.queue_id}' not found")
    if not principal.allows_tenant(entry.get("tenant_id", "")):
        raise HTTPException(status_code=403, detail="principal is not permitted for this tenant")
    run = exec_auth.latest_gate_run(request.queue_id)
    if not run:
        raise HTTPException(status_code=409, detail="run the pre-execution safety gate for this queue entry before approving")
    if run.get("plan_hash") != _engine.compute_plan_hash(entry):
        raise HTTPException(status_code=409, detail="the plan changed after the safety gate ran; re-run the gate")
    if run.get("gate_status") not in {"passed", "warning"}:
        raise HTTPException(status_code=409, detail=f"safety gate status '{run.get('gate_status')}' blocks approval")
    override_reason = str(request.override_reason or "").strip()
    needs_override = run.get("gate_status") == "warning"
    if needs_override and not (request.override_gate_warnings and override_reason):
        raise HTTPException(
            status_code=409,
            detail={"status": "override_required",
                    "message": "the safety gate returned warnings: approval requires override_gate_warnings=true and an override_reason",
                    "warnings": run.get("warnings") or []},
        )
    try:
        result = approve_automation(
            queue_id=request.queue_id,
            approved_by=principal.id,
            approver_role=principal.role,
            gate_run=run,
            override_requested=needs_override,
            override_reason=override_reason if needs_override else "",
        )
        if isinstance(result, dict) and result.get("status") == "not_approvable":
            raise HTTPException(status_code=409, detail=result)
        postgres_write_safe(
            "update_automation_queue_status",
            db_update_automation_queue_status,
            queue_id=request.queue_id,
            status=(result.get("queue_entry") or {}).get("status") or "approved",
            approved_by=principal.id,
        )
        if isinstance(result, dict):
            result["postgres_parallel_write"] = postgres_enabled()
        return result
    except HTTPException:
        raise
    except _engine.QueueBusyError as busy:
        raise _busy_http(busy)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/automation/execute")
def automation_execute_route(request: AutomationExecuteRequest, http_request: Request) -> Dict[str, Any]:
    if execute_automation is None:
        raise HTTPException(status_code=500, detail="automation_engine not available")
    principal = principal_from_request(http_request)
    entry = _engine.get_queue_entry(request.queue_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"queue entry '{request.queue_id}' not found")

    # One server-side decision. The request body supplies only the mode and an optional override;
    # `autonomous_requested` carries no authority.
    try:
        auth = exec_auth.authorize_execution(
            entry,
            request.execution_mode or "simulation",
            principal,
            run_gate=lambda e: _run_gate_for_entry(e, principal),
            ledger_gate=_ledger_gate_for,
            autonomous_mapping_allowed=_autonomous_mapping_allowed,
            override=bool(request.override_gate_warnings),
            override_reason=request.override_reason or "",
        )
    except _engine.QueueBusyError as busy:
        raise _busy_http(busy)
    if not auth.allowed:
        code = 401 if principal is None else (400 if any("unsupported execution_mode" in r for r in auth.reasons) else 403)
        raise HTTPException(status_code=code, detail={"status": "denied", "authorization": auth.to_dict()})

    try:
        result = execute_automation(
            queue_id=request.queue_id,
            executed_by=principal.id,
            execution_mode=auth.mode,
            authorization=auth.to_dict(),
        )
        status = (result or {}).get("status") if isinstance(result, dict) else None
        if status in {"not_found", "not_executable", "not_authorized", "invalid_mode"}:
            raise HTTPException(status_code=404 if status == "not_found" else 409, detail=result)
        execution = result.get("execution", {}) if isinstance(result, dict) else {}
        if execution:
            postgres_write_safe(
                "insert_automation_execution",
                db_insert_automation_execution,
                execution_id=execution.get("execution_id"),
                queue_id=execution.get("queue_id"),
                incident_id=execution.get("incident_id"),
                classification=execution.get("classification") or "unknown",
                executed_by=execution.get("executed_by") or principal.id,
                status=execution.get("status") or "completed_simulation",
                rollback_available=bool(execution.get("rollback_available", False)),
                steps={
                    "steps": execution.get("steps", []),
                    "execution_mode": execution.get("execution_mode", auth.mode),
                },
            )
            postgres_write_safe(
                "update_automation_queue_status",
                db_update_automation_queue_status,
                queue_id=request.queue_id,
                status="executed",
                execution_id=execution.get("execution_id"),
            )
        if isinstance(result, dict):
            result["postgres_parallel_write"] = postgres_enabled()
            result["authorization"] = auth.to_dict()
        # Fire webhook/Slack alert for live executions (non-blocking)
        if execution and execution.get("execution_mode") in ("autonomous", "auto_execute", "live"):
            try:
                from webhook_notifier import notify_execution
                webhook_result = notify_execution(execution)
                if isinstance(result, dict):
                    result["webhook_notification"] = webhook_result
            except Exception:
                pass
        return result
    except HTTPException:
        raise
    except _engine.QueueBusyError as busy:
        raise _busy_http(busy)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/automation/candidates")
def automation_create_candidate_route(request: AutomationCandidateCreateRequest) -> Dict[str, Any]:
    if create_automation_candidate is None:
        raise HTTPException(status_code=500, detail="automation candidate registry not available")
    result = create_automation_candidate(
        incident_id=request.incident_id,
        classification=_automation_classification_key(request.classification),
        draft_runbook=request.draft_runbook,
        draft_steps=request.draft_steps,
        suggested_action=request.suggested_action,
        suggested_playbook=request.suggested_playbook,
        target_host=request.target_host,
        target_group=request.target_group,
        risk_tier=request.risk_tier or "medium",
        approval_required=bool(request.approval_required),
        created_by=request.created_by or "engineer",
        source=request.source or "ui_draft_from_analysis",
        notes=request.notes or "",
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/automation/candidates")
def automation_list_candidates_route(status: Optional[str] = None, classification: Optional[str] = None) -> Dict[str, Any]:
    if list_automation_candidates is None:
        raise HTTPException(status_code=500, detail="automation candidate registry not available")
    return {
        "status": "ok",
        "items": list_automation_candidates(
            status=status,
            classification=_automation_classification_key(classification) if classification else None,
        ),
    }


@router.get("/automation/candidates/{candidate_id}")
def automation_get_candidate_route(candidate_id: str) -> Dict[str, Any]:
    if get_automation_candidate is None:
        raise HTTPException(status_code=500, detail="automation candidate registry not available")
    item = get_automation_candidate(candidate_id)
    if not item:
        raise HTTPException(status_code=404, detail="candidate not found")
    return {"status": "ok", "candidate": item}


@router.post("/automation/candidates/update")
def automation_update_candidate_route(request: AutomationCandidateUpdateRequest) -> Dict[str, Any]:
    if update_automation_candidate is None:
        raise HTTPException(status_code=500, detail="automation candidate registry not available")
    result = update_automation_candidate(
        candidate_id=request.candidate_id,
        updated_by=request.updated_by or "engineer",
        status=request.status,
        suggested_action=request.suggested_action,
        suggested_playbook=request.suggested_playbook,
        target_host=request.target_host,
        target_group=request.target_group,
        risk_tier=request.risk_tier,
        approval_required=request.approval_required,
        notes=request.notes,
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/automation/candidates/test_result")
def automation_candidate_test_result_route(request: AutomationCandidateTestResultRequest) -> Dict[str, Any]:
    if record_candidate_test_result is None:
        raise HTTPException(status_code=500, detail="automation candidate registry not available")
    result = record_candidate_test_result(
        candidate_id=request.candidate_id,
        test_status=request.test_status,
        tested_by=request.tested_by or "engineer",
        execution_mode=request.execution_mode or "simulation",
        stdout=request.stdout or "",
        stderr=request.stderr or "",
        returncode=request.returncode,
        notes=request.notes or "",
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/automation/candidates/approve")
def automation_candidate_approve_route(request: AutomationCandidateApproveRequest) -> Dict[str, Any]:
    if approve_automation_candidate is None:
        raise HTTPException(status_code=500, detail="automation candidate registry not available")
    result = approve_automation_candidate(
        candidate_id=request.candidate_id,
        approved_by=request.approved_by or "engineer",
        approval_notes=request.approval_notes or "",
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/automation/candidates/enable")
def automation_candidate_enable_route(request: AutomationCandidateEnableRequest) -> Dict[str, Any]:
    if enable_automation_candidate is None:
        raise HTTPException(status_code=500, detail="automation candidate registry not available")
    result = enable_automation_candidate(candidate_id=request.candidate_id, enabled_by=request.enabled_by or "engineer")
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/automation/candidates/generate_playbook")
def automation_candidate_generate_playbook_route(request: AutomationCandidateGeneratePlaybookRequest) -> Dict[str, Any]:
    if generate_candidate_playbook_draft is None:
        raise HTTPException(status_code=500, detail="automation playbook generator not available")
    result = generate_candidate_playbook_draft(
        candidate_id=request.candidate_id,
        generated_by=request.generated_by or "engineer",
        action=request.action,
        playbook=request.playbook,
        target_host=request.target_host,
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/automation/candidates/save_playbook")
def automation_candidate_save_playbook_route(request: AutomationCandidateSavePlaybookRequest) -> Dict[str, Any]:
    if save_reviewed_candidate_playbook is None:
        raise HTTPException(status_code=500, detail="automation playbook save function not available")
    result = save_reviewed_candidate_playbook(
        candidate_id=request.candidate_id,
        playbook_content=request.playbook_content,
        reviewed_by=request.reviewed_by or "engineer",
        review_notes=request.review_notes or "",
        overwrite=bool(request.overwrite),
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/automation/registry")
def automation_registry_route() -> Dict[str, Any]:
    try:
        from automation_candidate_registry import get_automation_registry as _get_automation_registry
        return {"status": "ok", "items": _get_automation_registry()}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"automation registry not available: {type(e).__name__}: {e}",
        )



@router.post("/automation/suggest_mapping")
def automation_suggest_mapping_route(request: AutomationSuggestMappingRequest) -> Dict[str, Any]:
    if suggest_action_mapping is None:
        raise HTTPException(status_code=500, detail="automation_engine self-learning not available")
    try:
        suggestion = suggest_action_mapping(
            classification=_automation_classification_key(request.classification),
            linked_resolution=request.linked_resolution,
            linked_playbook=request.linked_playbook,
        )
        return {"status": "ok", "suggestion": suggestion}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/automation/mappings")
def automation_mappings_route() -> Dict[str, Any]:
    if get_learned_action_mappings is None:
        raise HTTPException(status_code=500, detail="automation_engine self-learning not available")
    try:
        items = get_learned_action_mappings()
        return {"status": "ok", "items": items}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/automation/learn_mapping")
def automation_learn_mapping_route(request: AutomationLearnMappingRequest) -> Dict[str, Any]:
    if learn_action_mapping is None:
        raise HTTPException(status_code=500, detail="automation_engine self-learning not available")
    try:
        actions = request.actions if request.actions is not None else request.approved_actions
        result = learn_action_mapping(
            classification=_automation_classification_key(request.classification),
            actions=actions or [],
            learned_by=request.learned_by or request.approved_by or "engineer",
            source=request.source or "manual_approval",
        )
        return {"status": result.get("status", "ok"), "result": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Universal Evidence Docking Layer — API Push and API Pull endpoints
# -----------------------------------------------------------------------------------------------------------------------------

EVIDENCE_STORE_DIR = Path("data") / "evidence"
EVIDENCE_UPLOAD_MANIFEST = Path("data") / "ui_runtime" / "client_evidence_upload_manifest.json"

_EVIDENCE_STATUS_NORM = {
    "pass": "pass", "passed": "pass", "ok": "pass", "ready": "pass",
    "success": "pass", "healthy": "pass",
    "warn": "warn", "warning": "warn", "degraded": "warn",
    "fail": "fail", "failed": "fail", "blocked": "fail", "error": "fail",
    "critical": "fail",
}


# ---------------------------------------------------------------------------
# Rollback endpoint — triggered by Slack one-click rollback link
# ---------------------------------------------------------------------------

def _safe_read_json(path, default=None):
    try:
        if path and Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        pass
    return default if default is not None else {}


def _safe_write_json(path, data) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


@router.post("/automation/rollback")
def automation_rollback_route(
    http_request: Request,
    execution_id: str,
    queue_id: Optional[str] = None,
    rolled_back_by: Optional[str] = "engineer",
) -> Dict[str, Any]:
    """Roll back a completed autonomous execution.

    Finds the execution record by execution_id, verifies rollback_available,
    then records a rollback event in the execution log.  In simulation mode
    (default for this lab) rollback steps are simulated; in live mode they
    would be dispatched to Ansible.
    """
    principal = _require_principal(http_request, "l3")
    rolled_back_by = principal.id
    if AUTOMATION_LOG_FILE is None:
        raise HTTPException(status_code=500, detail="automation_engine not available")

    log: List[Dict[str, Any]] = _safe_read_json(AUTOMATION_LOG_FILE, default=[])
    if not isinstance(log, list):
        log = []

    execution = next((e for e in log if e.get("execution_id") == execution_id), None)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found in log.")

    if not execution.get("rollback_available"):
        raise HTTPException(
            status_code=409,
            detail="Rollback is not available for this execution (rollback_available=false).",
        )

    if execution.get("rollback_status") in {"rolled_back", "rollback_in_progress"}:
        return {
            "status": "already_rolled_back",
            "execution_id": execution_id,
            "rollback_status": execution.get("rollback_status"),
            "rolled_back_at": execution.get("rolled_back_at"),
        }

    now = utc_now_iso()
    rollback_steps = [
        {"action": f"undo_{s.get('action', 'step')}", "status": "simulated_rollback", "rolled_back_at": now}
        for s in reversed(execution.get("steps") or [])
    ]

    execution["rollback_status"] = "rolled_back"
    execution["rolled_back_at"] = now
    execution["rolled_back_by"] = rolled_back_by or "engineer"
    execution["rollback_steps"] = rollback_steps

    _safe_write_json(AUTOMATION_LOG_FILE, log)

    # Also mark the queue entry as rolled_back if queue_id provided
    if queue_id and AUTOMATION_QUEUE_FILE is not None:
        queue: List[Dict[str, Any]] = _safe_read_json(AUTOMATION_QUEUE_FILE, default=[])
        if isinstance(queue, list):
            for entry in queue:
                if entry.get("queue_id") == queue_id:
                    entry["status"] = "rolled_back"
                    entry["rolled_back_at"] = now
                    break
            _safe_write_json(AUTOMATION_QUEUE_FILE, queue)

    return {
        "status": "ok",
        "execution_id": execution_id,
        "queue_id": queue_id,
        "rollback_status": "rolled_back",
        "rolled_back_at": now,
        "rolled_back_by": rolled_back_by,
        "rollback_steps": rollback_steps,
    }

