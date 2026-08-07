"""Client Onboarding Studio page module."""
from __future__ import annotations
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import streamlit as st

from page_modules.shared import (
    BACKEND_URL,
    SESSION,
    REQUEST_TIMEOUT_SHORT,
    REQUEST_TIMEOUT_MEDIUM,
    metric_card,
    section_title,
    nice_label,
    render_confidence_meter,
    render_status_badge,
    render_evidence_ttl_warning,
    format_confidence,
)

try:
    from ui import (
        get_latest_analysis_result,
        get_automation_runtime_key,
        enable_autonomous_mapping_request,
        _json_safe,
    )
except ImportError:
    def get_latest_analysis_result():  # type: ignore
        return None, ""

    def get_automation_runtime_key(result, classification):  # type: ignore
        return ""

    def enable_autonomous_mapping_request(payload):  # type: ignore
        return {}

    def _json_safe(value: Any) -> Any:  # type: ignore
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_json_safe(v) for v in value]
        if isinstance(value, tuple):
            return [_json_safe(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)


try:
    from onboarding_studio import render_client_onboarding_studio
except ImportError:
    def render_client_onboarding_studio():  # type: ignore
        st.info("Onboarding Studio module not available.")


# ---------------------------------------------------------------------------
# Local helpers (not in shared.py)
# ---------------------------------------------------------------------------

def system_label(text: str) -> str:
    text = str(text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


# ---------------------------------------------------------------------------
# render_learning_autonomy_panel
# ---------------------------------------------------------------------------

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
                icon="\U0001f512",
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


# ---------------------------------------------------------------------------
# render_execution_evidence
# ---------------------------------------------------------------------------

def render_execution_evidence(result: Dict[str, Any]):
    if not result:
        return

    display_pairs = [
        ("Executor", result.get("executor")),
        ("Playbook", result.get("playbook")),
        ("Script", result.get("script")),
        ("Service", result.get("service")),
        ("Target Host", result.get("target_host") or result.get("host")),
        ("Return Code", result.get("returncode")),
        ("Working Directory", result.get("working_directory")),
        ("Compose File", result.get("compose_file")),
    ]

    meta_lines = [f"**{label}:** {value}" for label, value in display_pairs if value not in (None, "", [])]
    for line in meta_lines:
        st.markdown(line)

    stdout_text = result.get("stdout") or result.get("stdout_preview")
    stderr_text = result.get("stderr") or result.get("stderr_preview")
    summary_text = result.get("summary")

    if summary_text:
        st.caption(str(summary_text))

    if stdout_text:
        with st.expander("stdout", expanded=False):
            st.code(str(stdout_text)[:5000])

    if stderr_text:
        with st.expander("stderr", expanded=False):
            st.code(str(stderr_text)[:5000])

    raw_result = result.get("raw_result")
    if raw_result:
        with st.expander("Raw Result", expanded=False):
            st.json(raw_result)


# ---------------------------------------------------------------------------
# Onboarding helper functions
# ---------------------------------------------------------------------------

def _onboarding_safe_read_json_file(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {"_missing": True, "_path": str(path)}
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("_path", str(path))
            return data
        return {"_raw": data, "_path": str(path)}
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}", "_path": str(path)}


def _onboarding_evidence_definitions() -> List[Dict[str, str]]:
    """Business-friendly evidence definitions for the Client Onboarding UI."""
    return [
        {
            "key": "drift_status",
            "title": "Infrastructure Drift",
            "filename": "drift_status.json",
            "check_question": "Has the actual infrastructure changed from the approved baseline?",
            "business_purpose": "Prevents automation from running against a server, cloud resource, Kubernetes object, or config state that no longer matches the approved design.",
            "recommended_sources": "Terraform state, CMDB, AWS Config, Azure Resource Graph, Kubernetes API, Ansible inventory, GitOps repo",
            "default_connector": "Terraform / Cloud / CMDB",
        },
        {
            "key": "change_conflict",
            "title": "Change Conflict / Change Window",
            "filename": "change_conflict.json",
            "check_question": "Is there an active change, freeze window, maintenance window, or release conflict?",
            "business_purpose": "Avoids running automation while another team or release process is already changing the same service or infrastructure.",
            "recommended_sources": "ServiceNow Change, Jira Service Management, Remedy, release calendar, deployment calendar",
            "default_connector": "ITSM",
        },
        {
            "key": "error_budget_status",
            "title": "Error Budget / SLO",
            "filename": "error_budget_status.json",
            "check_question": "Is the service already under reliability pressure or burning error budget?",
            "business_purpose": "Stops risky automation when customer impact is already high or the service is close to breaching reliability commitments.",
            "recommended_sources": "Prometheus, Grafana, Dynatrace, Datadog, New Relic, Splunk Observability",
            "default_connector": "Observability",
        },
        {
            "key": "concurrent_activity",
            "title": "Concurrent Activity",
            "filename": "concurrent_activity.json",
            "check_question": "Is another engineer, bridge, deployment, or automation job active on this resource?",
            "business_purpose": "Prevents duplicate or conflicting work across SRE, change, deployment, and automation teams.",
            "recommended_sources": "ServiceNow incidents/problems, PagerDuty, AWX jobs, Jenkins/GitHub Actions/Azure DevOps, Kubernetes events",
            "default_connector": "ITSM / AWX / CI-CD",
        },
        {
            "key": "rollback_readiness",
            "title": "Rollback Readiness",
            "filename": "rollback_readiness.json",
            "check_question": "Can the automation restore the previous file, config, or state if something fails?",
            "business_purpose": "Ensures live and autonomous automation has a safe stop, restore, or backup path before any state-changing action.",
            "recommended_sources": "Playbook metadata, ./bak rollback folder, backup artifacts, config snapshots, restore tasks",
            "default_connector": "Playbook / Backup Metadata",
        },
        {
            "key": "blast_radius",
            "title": "Blast Radius",
            "filename": "blast_radius.json",
            "check_question": "How much damage can this automation cause if it executes incorrectly?",
            "business_purpose": "Classifies whether the automation is read-only/single-host, service-impacting, shared-platform impacting, or critical-enterprise impacting.",
            "recommended_sources": "CMDB relationships, service topology, cloud topology, Kubernetes/service mesh, load balancer target groups",
            "default_connector": "CMDB / Topology",
        },
    ]


def _onboarding_integration_mode_file() -> Path:
    return Path("data") / "ui_runtime" / "safety_gate_evidence_modes.json"


def _onboarding_load_evidence_modes() -> Dict[str, Any]:
    path = _onboarding_integration_mode_file()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _onboarding_save_evidence_modes(modes: Dict[str, Any]) -> None:
    path = _onboarding_integration_mode_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_json_safe(modes), indent=2), encoding="utf-8")
    except Exception as exc:
        st.warning(f"Could not save evidence mode settings: {exc}")


def _onboarding_evidence_manifest_file() -> Path:
    return Path("data") / "ui_runtime" / "client_evidence_upload_manifest.json"


def _onboarding_load_evidence_manifest() -> Dict[str, Any]:
    path = _onboarding_evidence_manifest_file()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"evidence": {}}


def _onboarding_save_evidence_manifest(manifest: Dict[str, Any]) -> None:
    path = _onboarding_evidence_manifest_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_json_safe(manifest), indent=2), encoding="utf-8")
    except Exception as exc:
        st.warning(f"Could not save evidence upload manifest: {exc}")


def _onboarding_mark_client_evidence_supplied(evidence_type: str, target_path: Path, mode: str, source: str = "ui") -> None:
    manifest = _onboarding_load_evidence_manifest()
    evidence = manifest.get("evidence") if isinstance(manifest.get("evidence"), dict) else {}
    record = {
        "evidence_type": evidence_type,
        "path": str(target_path),
        "filename": target_path.name,
        "mode": mode,
        "source": source,
        "client_supplied": True,
        "updated_at": datetime.now().isoformat(),
    }
    evidence[evidence_type] = record
    manifest["evidence"] = evidence
    _onboarding_save_evidence_manifest(manifest)


def _onboarding_manifest_marks_client_supplied(evidence_type: str | None, path: Path | None) -> bool:
    manifest = _onboarding_load_evidence_manifest()
    evidence = manifest.get("evidence") if isinstance(manifest.get("evidence"), dict) else {}
    # Primary lookup by evidence_type only (triple-key bug fixed — writer now stores only evidence_type key)
    if evidence_type:
        rec = evidence.get(str(evidence_type))
        if isinstance(rec, dict) and bool(rec.get("client_supplied")):
            return True
    # Fallback: tolerate legacy manifests written with the old filename/path keys
    if path is not None:
        for key in [path.name, str(path)]:
            rec = evidence.get(key)
            if isinstance(rec, dict) and bool(rec.get("client_supplied")):
                return True
    return False


def _onboarding_evidence_is_client_supplied(data: Dict[str, Any] | None, path: Path | None = None) -> bool:
    """Return True only when evidence looks supplied during onboarding or by an adapter.

    The onboarding pack includes sample seed files so the Safety Gate can run
    immediately. Those files are useful defaults, but the onboarding screen
    should not show PASS until a user uploads/saves client evidence or an API
    adapter/push endpoint supplies it.
    """
    if not isinstance(data, dict):
        return False

    # First trust the explicit local onboarding manifest. This prevents packaged
    # seed/sample evidence files from being treated as client evidence merely
    # because a JSON file exists under data/evidence.
    evidence_type = str(data.get("evidence_type") or "").strip()
    if not evidence_type and isinstance(data.get("items"), list) and data.get("items"):
        first_item = data.get("items")[0]
        if isinstance(first_item, dict):
            evidence_type = str(first_item.get("evidence_type") or "").strip()
    if _onboarding_manifest_marks_client_supplied(evidence_type, path):
        return True

    accepted_markers = {
        "client_onboarding_safety_gate_ui",
        "ui_json_upload",
        "manual_entry",
        "api_pull_adapter",
        "api_push_adapter",
        "evidence_submit_api",
        "universal_api_adapter",
    }
    seed_sources = {
        "client_file_adapter",
        "file_onboarding",
        "sample",
        "sample_onboarding",
        "seed",
        "seed_onboarding_pack",
    }

    def _obj_has_marker(obj: Dict[str, Any]) -> bool:
        uploaded_via = str(obj.get("uploaded_via") or obj.get("submitted_via") or obj.get("created_via") or "").strip().lower()
        source_system = str(obj.get("source_system") or obj.get("source") or "").strip().lower()
        if source_system in seed_sources:
            return False
        if uploaded_via in accepted_markers or source_system in accepted_markers:
            return True
        if obj.get("uploaded_at") or obj.get("submitted_at") or obj.get("adapter_run_id") or obj.get("received_at"):
            return True
        return False

    if _obj_has_marker(data):
        return True
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return any(isinstance(item, dict) and _obj_has_marker(item) for item in items)


def _onboarding_raw_status_from_evidence(data: Dict[str, Any] | str | None) -> str:
    """Extract the raw status from either a direct object or an items[] wrapper.

    This helper intentionally ignores whether evidence is client-supplied. It is
    used only while validating a freshly uploaded/manual payload before the UI
    stamps it as client-provided.
    """
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return ""
    items = data.get("items") if isinstance(data.get("items"), list) else []
    if items:
        statuses = []
        for item in items:
            if isinstance(item, dict):
                statuses.append(str(
                    item.get("status")
                    or item.get("gate_status")
                    or item.get("result")
                    or item.get("decision")
                    or item.get("check_status")
                    or ""
                ))
        nested = [x.strip().lower().replace(" ", "_").replace("-", "_") for x in statuses if x and x.strip()]
        if any(x in {"fail", "failed", "blocked", "block", "error", "critical", "red", "false", "deny", "denied"} for x in nested):
            return "fail"
        if any(x in {"warn", "warning", "degraded", "partial", "amber", "yellow"} for x in nested):
            return "warn"
        if nested and all(x in {"pass", "passed", "ok", "ready", "success", "successful", "healthy", "true", "allow", "allowed", "green"} for x in nested):
            return "pass"
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    if checks:
        nested_statuses = []
        for value in checks.values():
            if isinstance(value, dict):
                nested_statuses.append(str(value.get("status") or value.get("result") or ""))
            else:
                nested_statuses.append(str(value or ""))
        nested = [x.strip().lower().replace(" ", "_").replace("-", "_") for x in nested_statuses if x and x.strip()]
        if any(x in {"fail", "failed", "blocked", "block", "error", "critical", "red", "false", "deny", "denied"} for x in nested):
            return "fail"
        if any(x in {"warn", "warning", "degraded", "partial", "amber", "yellow"} for x in nested):
            return "warn"
        if nested and all(x in {"pass", "passed", "ok", "ready", "success", "successful", "healthy", "true", "allow", "allowed", "green"} for x in nested):
            return "pass"
    return str(
        data.get("status")
        or data.get("gate_status")
        or data.get("result")
        or data.get("decision")
        or data.get("check_status")
        or ""
    )


def _onboarding_normalize_raw_status_value(raw: str) -> str:
    value = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if value in {"pass", "passed", "ok", "ready", "success", "successful", "healthy", "true", "allow", "allowed", "green"}:
        return "PASS"
    if value in {"warn", "warning", "degraded", "partial", "amber", "yellow"}:
        return "WARN"
    if value in {"fail", "failed", "blocked", "block", "error", "critical", "red", "false", "deny", "denied"}:
        return "FAIL"
    if value in {"missing", "not_found", "not_configured", "absent"}:
        return "MISSING"
    return "UNKNOWN"


def _onboarding_normalize_evidence_status(data: Dict[str, Any] | str | None, path: Path | None = None) -> str:
    """Return a UI-safe PASS/WARN/FAIL/MISSING/UNKNOWN status for evidence files.

    Seed evidence files from the onboarding pack should render as UNKNOWN in
    the onboarding screen until the user uploads/saves client-provided evidence
    or an API adapter supplies it. This makes it obvious what has been onboarded.
    """
    if isinstance(data, str):
        raw = data
    elif isinstance(data, dict):
        if data.get("_missing") or (path is not None and not path.exists()):
            return "MISSING"
        if data.get("_error"):
            return "FAIL"
        if not _onboarding_evidence_is_client_supplied(data, path):
            return "UNKNOWN"
        # Evidence files may be either a single evidence object:
        #   {"evidence_type": "drift_status", "status": "pass", ...}
        # or a wrapper produced by onboarding/API adapters:
        #   {"items": [{"evidence_type": "drift_status", "status": "pass", ...}]}
        # Normalize wrapper shapes first so cards do not show UNKNOWN when
        # the real status is nested under items[0].
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if items:
            item_statuses = []
            for item in items:
                if isinstance(item, dict):
                    item_statuses.append(str(
                        item.get("status")
                        or item.get("gate_status")
                        or item.get("result")
                        or item.get("decision")
                        or item.get("check_status")
                        or ""
                    ))
            nested_items = [x.strip().lower().replace(" ", "_").replace("-", "_") for x in item_statuses if x and x.strip()]
            if any(x in {"fail", "failed", "blocked", "block", "error", "critical", "red", "false", "deny", "denied"} for x in nested_items):
                return "FAIL"
            if any(x in {"warn", "warning", "degraded", "partial", "amber", "yellow"} for x in nested_items):
                return "WARN"
            if nested_items and all(x in {"pass", "passed", "ok", "ready", "success", "successful", "healthy", "true", "allow", "allowed", "green"} for x in nested_items):
                return "PASS"

        candidates = [
            data.get("status"),
            data.get("gate_status"),
            data.get("result"),
            data.get("decision"),
            data.get("check_status"),
        ]
        checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
        if checks:
            nested_statuses = []
            for value in checks.values():
                if isinstance(value, dict):
                    nested_statuses.append(str(value.get("status") or value.get("result") or ""))
                else:
                    nested_statuses.append(str(value or ""))
            nested = [x.strip().lower() for x in nested_statuses if x and x.strip()]
            if any(x in {"fail", "failed", "blocked", "block", "error", "critical"} for x in nested):
                return "FAIL"
            if any(x in {"warn", "warning", "degraded"} for x in nested):
                return "WARN"
            if nested and all(x in {"pass", "passed", "ok", "ready", "success", "healthy"} for x in nested):
                return "PASS"
        raw = next((str(x) for x in candidates if str(x or "").strip()), "")
    else:
        raw = ""

    value = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if value in {"pass", "passed", "ok", "ready", "success", "successful", "healthy", "true", "allow", "allowed", "green"}:
        return "PASS"
    if value in {"warn", "warning", "degraded", "partial", "amber", "yellow"}:
        return "WARN"
    if value in {"fail", "failed", "blocked", "block", "error", "critical", "red", "false", "deny", "denied"}:
        return "FAIL"
    if value in {"missing", "not_found", "not_configured", "absent"}:
        return "MISSING"
    return "UNKNOWN"


def _onboarding_status_badge(status: str | Dict[str, Any], path: Path | None = None) -> tuple[str, str]:
    status_upper = _onboarding_normalize_evidence_status(status, path)
    if status_upper == "PASS":
        return "✅", "PASS"
    if status_upper == "WARN":
        return "⚠️", "WARN"
    if status_upper == "FAIL":
        return "⛔", "FAIL"
    if status_upper == "MISSING":
        return "⬜", "MISSING"
    return "❔", "UNKNOWN"


def _onboarding_write_uploaded_evidence(evidence_type: str, target_path: Path, uploaded_file) -> tuple[bool, str, Dict[str, Any]]:
    try:
        raw = uploaded_file.getvalue()
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            return False, "Uploaded JSON must be a JSON object.", {}
        # Accept either direct evidence object or {"items": [evidence, ...]} wrappers.
        evidence_obj = parsed
        if isinstance(parsed.get("items"), list) and parsed.get("items"):
            first_item = parsed.get("items")[0]
            if isinstance(first_item, dict):
                evidence_obj = first_item

        found_type = str(evidence_obj.get("evidence_type") or parsed.get("evidence_type") or evidence_type).strip()
        if system_label(found_type) != system_label(evidence_type):
            return False, f"evidence_type mismatch. Expected {evidence_type}, found {found_type}.", parsed
        normalized_status = _onboarding_normalize_raw_status_value(_onboarding_raw_status_from_evidence(parsed))
        if normalized_status == "UNKNOWN":
            return False, "status must resolve to pass, warn, fail, or blocked. Accepted examples: pass, passed, ok, warn, fail, blocked.", parsed
        # Preserve wrapper shape if uploaded as items[], but stamp metadata so future reads are deterministic.
        if evidence_obj is parsed:
            parsed["status"] = normalized_status.lower() if normalized_status != "MISSING" else "unknown"
            parsed["source_system"] = parsed.get("source_system") or "ui_json_upload"
            parsed["uploaded_via"] = "client_onboarding_safety_gate_ui"
            parsed["uploaded_at"] = datetime.now().isoformat()
        else:
            evidence_obj["status"] = normalized_status.lower() if normalized_status != "MISSING" else "unknown"
            evidence_obj["source_system"] = evidence_obj.get("source_system") or parsed.get("source_system") or "ui_json_upload"
            evidence_obj["uploaded_via"] = "client_onboarding_safety_gate_ui"
            evidence_obj["uploaded_at"] = datetime.now().isoformat()
            parsed["status"] = normalized_status.lower()
            parsed["evidence_type"] = evidence_type
            parsed["source_system"] = parsed.get("source_system") or "ui_json_upload"
            parsed["uploaded_via"] = "client_onboarding_safety_gate_ui"
            parsed["uploaded_at"] = datetime.now().isoformat()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
        _onboarding_mark_client_evidence_supplied(evidence_type, target_path, "Offline JSON Upload", "ui_json_upload")
        return True, f"Saved to {target_path}", parsed
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


def _onboarding_evidence_file_health(evidence_dir: Path) -> List[Dict[str, Any]]:
    expected = [
        ("drift_status", "Infrastructure Drift", "drift_status.json"),
        ("change_conflict", "Change Conflict / Window", "change_conflict.json"),
        ("error_budget_status", "Error Budget / SLO", "error_budget_status.json"),
        ("concurrent_activity", "Concurrent Activity", "concurrent_activity.json"),
        ("rollback_readiness", "Rollback Readiness", "rollback_readiness.json"),
        ("blast_radius", "Blast Radius", "blast_radius.json"),
    ]
    rows: List[Dict[str, Any]] = []
    for evidence_type, label, filename in expected:
        path = evidence_dir / filename
        data = _onboarding_safe_read_json_file(path)
        rows.append({
            "Evidence Type": evidence_type,
            "Safety Check": label,
            "File": str(path),
            "Present": "Yes" if path.exists() else "No",
            "Status": _onboarding_normalize_evidence_status(data, path),
            "Severity": str(data.get("severity", "")),
            "Source System": str(data.get("source_system", "")),
            "Summary": str(data.get("summary", data.get("_error", "")))[:180],
        })
    return rows


def _onboarding_api_connector_rows(api_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    connectors = api_config.get("connectors", {}) if isinstance(api_config, dict) else {}
    if not isinstance(connectors, dict):
        connectors = {}
    expected = [
        ("itsm", "ITSM", "ServiceNow / Jira / Remedy"),
        ("observability", "Observability", "Prometheus / Grafana / Datadog / Dynatrace / New Relic / Splunk"),
        ("ansible_awx", "Ansible / AWX", "AWX / Ansible Automation Platform"),
        ("terraform", "Terraform", "Terraform Cloud / local state"),
        ("aws", "AWS", "AWS Config / CloudWatch / ECS / ELB"),
        ("azure", "Azure", "Azure Resource Graph / Azure Monitor"),
        ("gcp", "GCP", "Cloud Asset Inventory / Cloud Monitoring"),
        ("kubernetes", "Kubernetes", "Kubernetes API"),
        ("git_repo", "GitHub / GitLab", "GitHub / GitLab repo and pipeline metadata"),
    ]
    rows: List[Dict[str, Any]] = []
    for key, label, examples in expected:
        cfg = connectors.get(key, {}) if isinstance(connectors.get(key, {}), dict) else {}
        enabled = bool(cfg.get("enabled", False))
        rows.append({
            "Connector": label,
            "Key": key,
            "Status": "ENABLED" if enabled else "DISABLED",
            "Adapter Type": str(cfg.get("adapter_type", cfg.get("type", "not_configured"))),
            "Evidence Output": str(cfg.get("evidence_output", cfg.get("writes", ""))),
            "Examples": examples,
        })
    return rows


def _onboarding_try_backend_safety_config() -> Dict[str, Any]:
    try:
        resp = SESSION.get(f"{BACKEND_URL}/automation/safety_gate/config", timeout=REQUEST_TIMEOUT_SHORT)
        if resp.ok:
            data = resp.json()
            if isinstance(data, dict):
                return data
        return {"status": "unavailable", "detail": f"HTTP {resp.status_code}: {resp.text[:250]}"}
    except Exception as exc:
        return {"status": "unavailable", "detail": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# render_safety_gate_evidence_setup_submenu
# ---------------------------------------------------------------------------

def render_safety_gate_evidence_setup_submenu() -> None:
    """Guided, card-based safety-gate evidence setup for client onboarding.

    This replaces the dense default table with business-friendly evidence cards.
    The table remains available under Advanced View.
    """
    st.markdown("### Safety Gate Evidence Setup")
    st.caption(
        "Configure how the client will provide evidence for safe automation execution. "
        "Each check can start with Offline JSON Upload and later move to API Pull or API Push."
    )

    with st.expander("Onboarding evidence status behavior", expanded=False):
        st.write(
            "Packaged/sample JSON files are treated as UNKNOWN until the client uploads, manually saves, "
            "or an adapter/API submits evidence. This makes it clear what has actually been onboarded."
        )
        if st.button("Reset client-supplied evidence markers", key="reset_client_evidence_manifest", width="stretch"):
            _onboarding_save_evidence_manifest({"evidence": {}})
            st.success("Client-supplied evidence markers were reset. Existing seed files will show UNKNOWN until uploaded/saved again.")
            st.rerun()

    config_dir = Path("config") / "client_onboarding"
    evidence_dir = Path("data") / "evidence"
    safety_config_path = config_dir / "safety_gate_config.json"
    evidence_defs = _onboarding_evidence_definitions()
    evidence_rows = _onboarding_evidence_file_health(evidence_dir)
    row_by_type = {str(r.get("Evidence Type")): r for r in evidence_rows}
    backend_cfg = _onboarding_try_backend_safety_config()
    modes = _onboarding_load_evidence_modes()
    # Ensure all 6 evidence types have a mode entry — prevents safety_gate_evidence_modes.json
    # from only containing the one type that was last changed (BF-05 fix).
    _ALL_EVIDENCE_KEYS = [
        "drift_status", "change_conflict", "error_budget_status",
        "concurrent_activity", "rollback_readiness", "blast_radius",
    ]
    _modes_changed = False
    for _ek in _ALL_EVIDENCE_KEYS:
        if _ek not in modes:
            modes[_ek] = {"mode": "Offline JSON Upload", "updated_at": datetime.now().isoformat()}
            _modes_changed = True
    if _modes_changed:
        _onboarding_save_evidence_modes(modes)

    evidence_found = sum(1 for r in evidence_rows if r.get("Present") == "Yes")
    evidence_failures = sum(1 for r in evidence_rows if str(r.get("Status", "")).upper() in {"FAIL", "FAILED", "BLOCKED"})
    evidence_warnings = sum(1 for r in evidence_rows if str(r.get("Status", "")).upper() in {"WARN", "WARNING"})

    ready_human = evidence_found == 6 and evidence_failures == 0
    autonomous_ready = False

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Evidence Files", f"{evidence_found}/6")
    c2.metric("Rollout Mode", "File-first active")
    c3.metric("Human Approved Ready", "Yes" if ready_human else "No")
    c4.metric("Autonomous Ready", "Yes" if autonomous_ready else "No")

    if evidence_failures:
        st.error(f"{evidence_failures} evidence item(s) are failing. Human approval/live execution may be blocked by policy.")
    elif evidence_warnings:
        st.warning(f"{evidence_warnings} evidence item(s) are warning. Human approval may be allowed with review depending on policy.")
    elif ready_human:
        st.success("All required evidence files are present and currently passing. Ready for controlled human-approved automation.")
    else:
        st.warning("Evidence setup is incomplete. Upload or configure the missing evidence before live execution.")

    st.markdown("#### Evidence setup cards")
    st.caption("Open a card to choose the integration mode, upload JSON evidence, preview the current evidence, or review API setup instructions.")

    mode_options = ["Offline JSON Upload", "API Pull", "API Push", "Manual Entry", "Disabled / Not Required for Pilot"]

    for idx, item in enumerate(evidence_defs, start=1):
        key = item["key"]
        row = row_by_type.get(key, {})
        filename = item["filename"]
        path = evidence_dir / filename
        data = _onboarding_safe_read_json_file(path)
        status_icon, status_label = _onboarding_status_badge(data, path)
        mode_state = modes.get(key, {}) if isinstance(modes.get(key), dict) else {}
        current_mode = mode_state.get("mode") or "Offline JSON Upload"
        last_updated = "Not available"
        try:
            if path.exists():
                last_updated = datetime.fromtimestamp(path.stat().st_mtime).strftime("%b %d, %Y %I:%M %p")
        except Exception:
            pass

        with st.expander(f"{idx}. {item['title']} — {status_icon} {status_label}", expanded=False):
            st.markdown(f"**What this checks:** {item['check_question']}")
            st.caption(item["business_purpose"])

            # Large status badge + TTL warning
            badge_html = render_status_badge(status_label, size="large")
            st.markdown(badge_html, unsafe_allow_html=True)
            if data and isinstance(data, dict):
                render_evidence_ttl_warning(
                    data.get("checked_at") or data.get("uploaded_at", ""),
                    item["title"],
                    warn_hours=72,
                )
            if status_label == "UNKNOWN" and path.exists():
                st.caption("Sample/default file exists. Upload or submit client evidence to show a real status.")

            a, b, c = st.columns([1.2, 1.2, 1.6])
            with a:
                st.write(f"**Evidence File:** `{path}`")
                st.write(f"**Last Updated:** {last_updated}")
            with b:
                st.write(f"**Current Mode:** {current_mode}")
            with c:
                st.write(f"**Recommended Sources:** {item['recommended_sources']}")

            selected_mode = st.radio(
                "How will the client provide this evidence?",
                mode_options,
                index=mode_options.index(current_mode) if current_mode in mode_options else 0,
                horizontal=True,
                key=f"evidence_mode_{key}",
            )
            if selected_mode != current_mode:
                mode_state["mode"] = selected_mode
                mode_state["updated_at"] = datetime.now().isoformat()
                modes[key] = mode_state
                _onboarding_save_evidence_modes(modes)
                st.info(f"Mode updated to {selected_mode}. This is saved in `{_onboarding_integration_mode_file()}`.")

            if selected_mode == "Offline JSON Upload":
                st.markdown("##### Upload evidence JSON")
                st.caption(
                    f"Upload a `{filename}` file with `evidence_type = {key}` and a status such as `pass`, `passed`, `ok`, `warn`, `fail`, or `blocked`. "
                    "The file will be saved into `data/evidence/`."
                )
                uploaded = st.file_uploader(
                    f"Upload {filename}",
                    type=["json"],
                    key=f"upload_evidence_{key}",
                )
                if uploaded is not None:
                    try:
                        preview = json.loads(uploaded.getvalue().decode("utf-8"))
                        st.success("Valid JSON parsed. Review the preview, then save.")
                        st.json(preview)
                    except Exception as exc:
                        preview = None
                        st.error(f"Invalid JSON: {exc}")
                    if preview is not None and st.button(f"Save uploaded {filename}", key=f"save_uploaded_evidence_{key}", width="stretch"):
                        ok, message, _ = _onboarding_write_uploaded_evidence(key, path, uploaded)
                        if ok:
                            st.success(message)
                            st.caption("The uploaded evidence was normalized and the card will refresh with PASS/WARN/FAIL after rerun.")
                            st.rerun()
                        else:
                            st.error(message)

            elif selected_mode == "API Pull":
                st.markdown("##### Configure API Pull")
                st.caption(
                    "Use this when AIOps, an adapter job, or a scheduled integration runner is allowed to call the client/source system API, "
                    "normalize the response, and write the Safety Gate evidence file. This generator creates copy-ready pull-adapter code."
                )

                pull_spec_dir = Path("config") / "client_onboarding" / "api_pull_specs"
                pull_spec_path = pull_spec_dir / f"{key}_api_pull_spec.json"
                existing_pull_spec = _onboarding_safe_read_json_file(pull_spec_path)
                if not isinstance(existing_pull_spec, dict) or existing_pull_spec.get("_error"):
                    existing_pull_spec = {}

                default_provider = {
                    "change_conflict": "ServiceNow Change",
                    "error_budget_status": "Prometheus / Grafana",
                    "drift_status": "Terraform / Cloud Config / CMDB",
                    "concurrent_activity": "AWX / CI-CD / ITSM",
                    "rollback_readiness": "Playbook Registry / Backup Metadata",
                    "blast_radius": "CMDB / Service Topology",
                }.get(key, item.get("default_connector") or "Client Source System")

                pull_endpoint_default = {
                    "change_conflict": "https://<servicenow-instance>/api/now/table/change_request?sysparm_query=active=true",
                    "error_budget_status": "https://<observability-host>/api/v1/query?query=<slo_or_burn_rate_query>",
                    "drift_status": "https://<config-source>/api/drift/status?resource=<resource>",
                    "concurrent_activity": "https://<automation-or-itsm>/api/activity?resource=<resource>",
                    "rollback_readiness": "https://<playbook-registry>/api/rollback/readiness?playbook=<playbook>",
                    "blast_radius": "https://<cmdb-or-topology>/api/service-map?service=<service>",
                }.get(key, "https://<client-system>/api/evidence-source")

                p1, p2 = st.columns(2)
                with p1:
                    pull_source_system_name = st.text_input(
                        "Source System Name",
                        value=str(existing_pull_spec.get("source_system_name") or default_provider),
                        key=f"pull_source_name_{key}",
                    )
                    pull_source_api_url = st.text_input(
                        "Source System API URL",
                        value=str(existing_pull_spec.get("source_api_url") or pull_endpoint_default),
                        key=f"pull_source_api_url_{key}",
                        help="The source API that the AIOps adapter or scheduled runner will call.",
                    )
                    pull_auth_header_name = st.text_input(
                        "Auth Header Name",
                        value=str(existing_pull_spec.get("auth_header_name") or "Authorization"),
                        key=f"pull_auth_header_{key}",
                    )
                    pull_auth_header_value_hint = st.text_input(
                        "Auth Header Value Pattern",
                        value=str(existing_pull_spec.get("auth_header_value_hint") or "Bearer <token>"),
                        key=f"pull_auth_header_value_hint_{key}",
                        help="Do not store secrets here. Use a placeholder or secret reference.",
                    )
                with p2:
                    pull_service_name = st.text_input(
                        "Service Name",
                        value=str(existing_pull_spec.get("service") or data.get("service") or "claims-api"),
                        key=f"pull_service_{key}",
                    )
                    pull_environment_name = st.text_input(
                        "Environment",
                        value=str(existing_pull_spec.get("environment") or data.get("environment") or "production"),
                        key=f"pull_environment_{key}",
                    )
                    pull_resource_name = st.text_input(
                        "Resource / CI / Host",
                        value=str(existing_pull_spec.get("resource") or data.get("resource") or "linux-lab-01"),
                        key=f"pull_resource_{key}",
                    )
                    st.text_input(
                        "Normalized Evidence Output",
                        value=str(path),
                        key=f"pull_output_file_{key}",
                        disabled=True,
                    )

                st.markdown("###### What should the pull adapter extract from the source response?")
                pull_required_fields_text = st.text_area(
                    "Required source fields / response mapping",
                    value=str(existing_pull_spec.get("required_fields_text") or item.get("api_push_fields") or "status\nseverity\nsummary\nsource_record_id\nchecked_at"),
                    key=f"pull_required_fields_{key}",
                    height=120,
                )
                q1, q2 = st.columns(2)
                with q1:
                    pull_query_params_text = st.text_area(
                        "Query parameters / filter logic",
                        value=str(existing_pull_spec.get("query_params_text") or "service=<service>\nenvironment=<environment>\nresource=<resource>"),
                        key=f"pull_query_params_{key}",
                        height=105,
                    )
                with q2:
                    pull_refresh_frequency = st.text_input(
                        "Refresh Frequency",
                        value=str(existing_pull_spec.get("refresh_frequency") or "15 minutes"),
                        key=f"pull_refresh_frequency_{key}",
                    )
                    pull_storage_mode = st.selectbox(
                        "Evidence Write Mode",
                        ["Write local evidence file", "POST to /evidence/submit"],
                        index=0 if str(existing_pull_spec.get("write_mode") or "Write local evidence file") == "Write local evidence file" else 1,
                        key=f"pull_write_mode_{key}",
                    )

                r1, r2, r3 = st.columns(3)
                with r1:
                    pull_pass_rule = st.text_area(
                        "PASS rule",
                        value=str(existing_pull_spec.get("pass_rule") or "Source API returns no conflicting condition; status is pass/ok/healthy."),
                        key=f"pull_pass_rule_{key}",
                        height=90,
                    )
                with r2:
                    pull_warn_rule = st.text_area(
                        "WARN rule",
                        value=str(existing_pull_spec.get("warn_rule") or "Source API returns a potential risk that requires human review."),
                        key=f"pull_warn_rule_{key}",
                        height=90,
                    )
                with r3:
                    pull_fail_rule = st.text_area(
                        "FAIL/BLOCK rule",
                        value=str(existing_pull_spec.get("fail_rule") or "Source API returns active conflict, policy violation, missing rollback, high drift, or critical risk."),
                        key=f"pull_fail_rule_{key}",
                        height=90,
                    )

                pull_sample_details = {
                    "source_api_url": pull_source_api_url,
                    "query_params": [line.strip() for line in pull_query_params_text.splitlines() if line.strip()],
                    "required_fields": [line.strip() for line in pull_required_fields_text.splitlines() if line.strip()],
                    "pass_rule": pull_pass_rule,
                    "warn_rule": pull_warn_rule,
                    "fail_rule": pull_fail_rule,
                }
                pull_sample_payload = {
                    "evidence_type": key,
                    "service": pull_service_name,
                    "environment": pull_environment_name,
                    "resource": pull_resource_name,
                    "status": "pass",
                    "severity": "low",
                    "source_system": pull_source_system_name,
                    "source_system_url": pull_source_api_url,
                    "source_record_id": "<source-record-id-from-api>",
                    "summary": f"{item['title']} evidence passed based on API Pull adapter evaluation.",
                    "details": pull_sample_details,
                    "checked_at": datetime.utcnow().isoformat() + "Z",
                    "confidence": 0.90,
                }

                with st.expander("Generated normalized evidence payload", expanded=True):
                    st.json(pull_sample_payload)

                pull_payload_json = json.dumps(pull_sample_payload, indent=2)
                pull_output_path = str(path).replace("\\", "/")
                pull_python_code = (
                    "import json\n"
                    "from pathlib import Path\n"
                    "from datetime import datetime, timezone\n"
                    "import requests\n\n"
                    f'SOURCE_API_URL = "{pull_source_api_url}"\n'
                    f'AUTH_HEADER_NAME = "{pull_auth_header_name}"\n'
                    f'AUTH_HEADER_VALUE = "{pull_auth_header_value_hint}"  # replace with secret lookup\n'
                    f'OUTPUT_FILE = Path(r"{pull_output_path}")\n\n'
                    "def derive_status(source_data):\n"
                    "    # TODO: map source_data to pass/warn/fail using onboarding rules.\n"
                    f"    # PASS: {pull_pass_rule}\n"
                    f"    # WARN: {pull_warn_rule}\n"
                    f"    # FAIL: {pull_fail_rule}\n"
                    "    return \"pass\", \"low\", \"Evidence passed based on source API response.\"\n\n"
                    "resp = requests.get(\n"
                    "    SOURCE_API_URL,\n"
                    "    headers={AUTH_HEADER_NAME: AUTH_HEADER_VALUE},\n"
                    "    timeout=30,\n"
                    ")\n"
                    "resp.raise_for_status()\n"
                    "source_data = resp.json()\n"
                    "status, severity, summary = derive_status(source_data)\n\n"
                    "normalized_evidence = {\n"
                    f'    "evidence_type": "{key}",\n'
                    f'    "service": "{pull_service_name}",\n'
                    f'    "environment": "{pull_environment_name}",\n'
                    f'    "resource": "{pull_resource_name}",\n'
                    "    \"status\": status,\n"
                    "    \"severity\": severity,\n"
                    f'    "source_system": "{pull_source_system_name}",\n'
                    f'    "source_system_url": SOURCE_API_URL,\n'
                    "    \"source_record_id\": source_data.get(\"id\") or source_data.get(\"sys_id\") or \"api_pull\",\n"
                    "    \"summary\": summary,\n"
                    "    \"details\": source_data,\n"
                    "    \"checked_at\": datetime.now(timezone.utc).isoformat(),\n"
                    "    \"confidence\": 0.90,\n"
                    "}\n\n"
                    "OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)\n"
                    "OUTPUT_FILE.write_text(json.dumps(normalized_evidence, indent=2), encoding=\"utf-8\")\n"
                    "print(json.dumps({\"status\": \"ok\", \"written\": str(OUTPUT_FILE), \"evidence\": normalized_evidence}, indent=2))\n"
                )
                pull_powershell_code = (
                    f'$SourceApiUrl = "{pull_source_api_url}"\n'
                    f'$OutputFile = "{pull_output_path}"\n'
                    f'$Headers = @{{ "{pull_auth_header_name}" = "{pull_auth_header_value_hint}" }}\n\n'
                    "$SourceData = Invoke-RestMethod -Uri $SourceApiUrl -Method Get -Headers $Headers\n\n"
                    "# TODO: Map $SourceData to pass/warn/fail using onboarding rules.\n"
                    f"# PASS: {pull_pass_rule}\n"
                    f"# WARN: {pull_warn_rule}\n"
                    f"# FAIL: {pull_fail_rule}\n\n"
                    "$Evidence = @{\n"
                    f'  evidence_type = "{key}"\n'
                    f'  service = "{pull_service_name}"\n'
                    f'  environment = "{pull_environment_name}"\n'
                    f'  resource = "{pull_resource_name}"\n'
                    '  status = "pass"\n'
                    '  severity = "low"\n'
                    f'  source_system = "{pull_source_system_name}"\n'
                    "  source_system_url = $SourceApiUrl\n"
                    '  source_record_id = "api_pull"\n'
                    f'  summary = "{item["title"]} evidence passed based on API Pull adapter evaluation."\n'
                    "  details = $SourceData\n"
                    "  checked_at = (Get-Date).ToUniversalTime().ToString('o')\n"
                    "  confidence = 0.90\n"
                    "}\n\n"
                    "$EvidenceJson = $Evidence | ConvertTo-Json -Depth 30\n"
                    "New-Item -ItemType Directory -Path (Split-Path $OutputFile) -Force | Out-Null\n"
                    "Set-Content -Path $OutputFile -Value $EvidenceJson -Encoding UTF8\n"
                    "$EvidenceJson\n"
                )
                pull_curl_code = (
                    f'curl -X GET "{pull_source_api_url}" \\\n'
                    f'  -H "{pull_auth_header_name}: {pull_auth_header_value_hint}" \\\n'
                    '  -H "Accept: application/json"\n\n'
                    "# Pipe the response into the generated Python or PowerShell adapter to normalize and write the evidence file."
                )
                pull_adapter_pseudocode = (
                    f"# AIOps-side pull adapter pattern for {key}\n"
                    f"# 1. Call source API: {pull_source_system_name}\n"
                    "# 2. Map source response fields to PASS/WARN/FAIL\n"
                    "# 3. Write normalized evidence file consumed by Safety Gate\n\n"
                    "source_data = call_source_api()\n"
                    "status, severity, summary = evaluate_policy(source_data)\n"
                    "write_normalized_evidence({\n"
                    f'    "evidence_type": "{key}",\n'
                    f'    "service": "{pull_service_name}",\n'
                    f'    "environment": "{pull_environment_name}",\n'
                    f'    "resource": "{pull_resource_name}",\n'
                    "    \"status\": status,\n"
                    "    \"severity\": severity,\n"
                    "    \"summary\": summary,\n"
                    "    \"details\": source_data,\n"
                    f'    "output_file": "{pull_output_path}",\n'
                    "})"
                )

                st.markdown("###### Generated AIOps-side pull adapter code")
                pull_tab1, pull_tab2, pull_tab3, pull_tab4 = st.tabs(["PowerShell", "Python", "cURL Source Test", "Adapter Pseudocode"])
                with pull_tab1:
                    st.code(pull_powershell_code, language="powershell")
                with pull_tab2:
                    st.code(pull_python_code, language="python")
                with pull_tab3:
                    st.code(pull_curl_code, language="bash")
                with pull_tab4:
                    st.code(pull_adapter_pseudocode, language="python")

                pull_save_spec_payload = {
                    "evidence_type": key,
                    "title": item["title"],
                    "mode": "API Pull",
                    "source_system_name": pull_source_system_name,
                    "source_api_url": pull_source_api_url,
                    "auth_header_name": pull_auth_header_name,
                    "auth_header_value_hint": pull_auth_header_value_hint,
                    "service": pull_service_name,
                    "environment": pull_environment_name,
                    "resource": pull_resource_name,
                    "output_file": str(path),
                    "required_fields_text": pull_required_fields_text,
                    "query_params_text": pull_query_params_text,
                    "refresh_frequency": pull_refresh_frequency,
                    "write_mode": pull_storage_mode,
                    "pass_rule": pull_pass_rule,
                    "warn_rule": pull_warn_rule,
                    "fail_rule": pull_fail_rule,
                    "sample_payload": pull_sample_payload,
                    "generated_code": {
                        "powershell": pull_powershell_code,
                        "python": pull_python_code,
                        "curl_source_test": pull_curl_code,
                        "adapter_pseudocode": pull_adapter_pseudocode,
                    },
                    "updated_at": datetime.now().isoformat(),
                }

                ps1, ps2 = st.columns([1, 1])
                with ps1:
                    if st.button(f"Save API Pull spec for {item['title']}", key=f"save_api_pull_spec_{key}", width="stretch"):
                        pull_spec_dir.mkdir(parents=True, exist_ok=True)
                        pull_spec_path.write_text(json.dumps(pull_save_spec_payload, indent=2), encoding="utf-8")
                        mode_state["mode"] = "API Pull"
                        mode_state["api_pull_spec"] = str(pull_spec_path)
                        mode_state["updated_at"] = datetime.now().isoformat()
                        modes[key] = mode_state
                        _onboarding_save_evidence_modes(modes)
                        st.success(f"API Pull specification saved to {pull_spec_path}")
                with ps2:
                    st.download_button(
                        "Download API Pull spec JSON",
                        data=json.dumps(pull_save_spec_payload, indent=2),
                        file_name=f"{key}_api_pull_spec.json",
                        mime="application/json",
                        key=f"download_api_pull_spec_{key}",
                        width="stretch",
                    )

                st.info(
                    "This generates the AIOps-side pull contract and starter code. The adapter calls the source system, normalizes the response, "
                    "and writes the same `data/evidence/` file that Safety Gate already consumes."
                )

            elif selected_mode == "API Push":
                st.markdown("##### Configure API Push")
                st.caption(
                    "Use this when the client system, webhook, export job, scheduled export, or lightweight adapter will push normalized evidence into AIOps. "
                    "This generator creates a copy-ready payload contract and starter code for the source-system team."
                )

                push_endpoint_default = f"{BACKEND_URL}/evidence/submit"
                spec_dir = Path("config") / "client_onboarding" / "api_push_specs"
                spec_path = spec_dir / f"{key}_api_push_spec.json"
                existing_spec = _onboarding_safe_read_json_file(spec_path)
                if not isinstance(existing_spec, dict) or existing_spec.get("_error"):
                    existing_spec = {}

                g1, g2 = st.columns(2)
                with g1:
                    source_system_name = st.text_input(
                        "Source System Name",
                        value=str(existing_spec.get("source_system_name") or item.get("default_connector") or "Client Source System"),
                        key=f"push_source_name_{key}",
                    )
                    source_system_url = st.text_input(
                        "Source System URL / Record URL",
                        value=str(existing_spec.get("source_system_url") or "https://<client-system>/"),
                        key=f"push_source_url_{key}",
                        help="Used for traceability in evidence. The push call goes to the AIOps endpoint below.",
                    )
                    push_endpoint = st.text_input(
                        "AIOps Push Endpoint",
                        value=str(existing_spec.get("push_endpoint") or push_endpoint_default),
                        key=f"push_endpoint_{key}",
                    )
                    auth_header_name = st.text_input(
                        "Auth Header Name",
                        value=str(existing_spec.get("auth_header_name") or "X-AIOps-Evidence-Key"),
                        key=f"push_auth_header_{key}",
                    )
                with g2:
                    service_name = st.text_input(
                        "Service Name",
                        value=str(existing_spec.get("service") or data.get("service") or "claims-api"),
                        key=f"push_service_{key}",
                    )
                    environment_name = st.text_input(
                        "Environment",
                        value=str(existing_spec.get("environment") or data.get("environment") or "production"),
                        key=f"push_environment_{key}",
                    )
                    resource_name = st.text_input(
                        "Resource / CI / Host",
                        value=str(existing_spec.get("resource") or data.get("resource") or "linux-lab-01"),
                        key=f"push_resource_{key}",
                    )
                    st.text_input(
                        "Normalized Evidence Output",
                        value=str(path),
                        key=f"push_output_file_{key}",
                        disabled=True,
                    )

                st.markdown("###### What information will the source system send?")
                st.caption(
                    "Capture the minimum source fields needed to decide PASS/WARN/FAIL. "
                    "Examples: active change count, maintenance window flag, burn rate, drift count, rollback flag, blast radius tier."
                )
                required_fields_text = st.text_area(
                    "Required source fields / evidence details",
                    value=str(existing_spec.get("required_fields_text") or item.get("api_push_fields") or "status\nseverity\nsummary\nsource_record_id\nchecked_at"),
                    key=f"push_required_fields_{key}",
                    height=120,
                )

                m1, m2, m3 = st.columns(3)
                with m1:
                    pass_rule = st.text_area(
                        "PASS rule",
                        value=str(existing_spec.get("pass_rule") or "No conflicting condition found; source status is pass/ok/healthy."),
                        key=f"push_pass_rule_{key}",
                        height=90,
                    )
                with m2:
                    warn_rule = st.text_area(
                        "WARN rule",
                        value=str(existing_spec.get("warn_rule") or "Potential risk exists but human-approved execution may continue after review."),
                        key=f"push_warn_rule_{key}",
                        height=90,
                    )
                with m3:
                    fail_rule = st.text_area(
                        "FAIL/BLOCK rule",
                        value=str(existing_spec.get("fail_rule") or "Active conflict, high risk, missing rollback, or policy violation detected."),
                        key=f"push_fail_rule_{key}",
                        height=90,
                    )

                sample_details = {
                    "source_system_url": source_system_url,
                    "required_fields": [line.strip() for line in required_fields_text.splitlines() if line.strip()],
                    "pass_rule": pass_rule,
                    "warn_rule": warn_rule,
                    "fail_rule": fail_rule,
                }
                sample_payload = {
                    "evidence_type": key,
                    "service": service_name,
                    "environment": environment_name,
                    "resource": resource_name,
                    "status": "pass",
                    "severity": "low",
                    "source_system": source_system_name,
                    "source_system_url": source_system_url,
                    "source_record_id": "<source-record-id>",
                    "summary": f"{item['title']} evidence passed based on source-system evaluation.",
                    "details": sample_details,
                    "checked_at": datetime.utcnow().isoformat() + "Z",
                    "confidence": 0.90,
                }

                with st.expander("Generated evidence payload contract", expanded=True):
                    st.json(sample_payload)

                payload_json = json.dumps(sample_payload, indent=2)
                curl_payload = payload_json.replace('"', '\\"').replace("\n", " ")
                curl_code = (
                    f'curl -X POST "{push_endpoint}" ^\n'
                    f'  -H "Content-Type: application/json" ^\n'
                    f'  -H "{auth_header_name}: <token>" ^\n'
                    f'  -d "{curl_payload}"'
                )
                powershell_code = (
                    "$payload = @'\n"
                    f"{payload_json}\n"
                    "'@\n\n"
                    f'Invoke-RestMethod -Uri "{push_endpoint}" `\n'
                    "  -Method Post `\n"
                    f'  -Headers @{{ "{auth_header_name}" = "<token>" }} `\n'
                    "  -Body $payload `\n"
                    '  -ContentType "application/json" | ConvertTo-Json -Depth 20'
                )
                python_code = (
                    "import requests\n"
                    "from datetime import datetime, timezone\n\n"
                    f"payload = {payload_json}\n"
                    'payload["checked_at"] = datetime.now(timezone.utc).isoformat()\n\n'
                    "response = requests.post(\n"
                    f'    "{push_endpoint}",\n'
                    "    headers={\n"
                    '        "Content-Type": "application/json",\n'
                    f'        "{auth_header_name}": "<token>",\n'
                    "    },\n"
                    "    json=payload,\n"
                    "    timeout=30,\n"
                    ")\n"
                    "response.raise_for_status()\n"
                    "print(response.json())"
                )
                adapter_code = (
                    f"# Lightweight source-side adapter pattern for {key}\n"
                    f"# 1. Read source-system data from {source_system_name}\n"
                    "# 2. Apply PASS/WARN/FAIL rules\n"
                    "# 3. POST normalized evidence to AIOps\n\n"
                    "source_data = fetch_from_source_system()  # client-owned function\n\n"
                    'status = "pass"\n'
                    'severity = "low"\n'
                    f'summary = "{item["title"]} evidence passed."\n\n'
                    "# TODO: Map source fields using the rules captured in onboarding:\n"
                    f"# PASS: {pass_rule}\n"
                    f"# WARN: {warn_rule}\n"
                    f"# FAIL: {fail_rule}\n\n"
                    "normalized_evidence = {\n"
                    f'    "evidence_type": "{key}",\n'
                    f'    "service": "{service_name}",\n'
                    f'    "environment": "{environment_name}",\n'
                    f'    "resource": "{resource_name}",\n'
                    '    "status": status,\n'
                    '    "severity": severity,\n'
                    f'    "source_system": "{source_system_name}",\n'
                    f'    "source_system_url": "{source_system_url}",\n'
                    '    "summary": summary,\n'
                    '    "details": source_data,\n'
                    '    "checked_at": datetime.now(timezone.utc).isoformat(),\n'
                    '    "confidence": 0.90,\n'
                    "}\n\n"
                    f'requests.post("{push_endpoint}", json=normalized_evidence, timeout=30).raise_for_status()'
                )

                st.markdown("###### Generated source-system code")
                code_tab1, code_tab2, code_tab3, code_tab4 = st.tabs(["PowerShell", "Python", "cURL", "Adapter Pseudocode"])
                with code_tab1:
                    st.code(powershell_code, language="powershell")
                with code_tab2:
                    st.code(python_code, language="python")
                with code_tab3:
                    st.code(curl_code, language="bash")
                with code_tab4:
                    st.code(adapter_code, language="python")

                save_spec_payload = {
                    "evidence_type": key,
                    "title": item["title"],
                    "mode": "API Push",
                    "source_system_name": source_system_name,
                    "source_system_url": source_system_url,
                    "push_endpoint": push_endpoint,
                    "auth_header_name": auth_header_name,
                    "service": service_name,
                    "environment": environment_name,
                    "resource": resource_name,
                    "output_file": str(path),
                    "required_fields_text": required_fields_text,
                    "pass_rule": pass_rule,
                    "warn_rule": warn_rule,
                    "fail_rule": fail_rule,
                    "sample_payload": sample_payload,
                    "generated_code": {
                        "powershell": powershell_code,
                        "python": python_code,
                        "curl": curl_code,
                        "adapter_pseudocode": adapter_code,
                    },
                    "updated_at": datetime.now().isoformat(),
                }

                s1, s2 = st.columns([1, 1])
                with s1:
                    if st.button(f"Save API Push spec for {item['title']}", key=f"save_api_push_spec_{key}", width="stretch"):
                        spec_dir.mkdir(parents=True, exist_ok=True)
                        spec_path.write_text(json.dumps(save_spec_payload, indent=2), encoding="utf-8")
                        mode_state["mode"] = "API Push"
                        mode_state["api_push_spec"] = str(spec_path)
                        mode_state["updated_at"] = datetime.now().isoformat()
                        modes[key] = mode_state
                        _onboarding_save_evidence_modes(modes)
                        st.success(f"API Push specification saved to {spec_path}")
                with s2:
                    st.download_button(
                        "Download API Push spec JSON",
                        data=json.dumps(save_spec_payload, indent=2),
                        file_name=f"{key}_api_push_spec.json",
                        mime="application/json",
                        key=f"download_api_push_spec_{key}",
                        width="stretch",
                    )

                st.info(
                    "This generates the source-system push contract and starter code. Once the backend `/evidence/submit` endpoint is enabled, "
                    "the same payload can update `data/evidence/` automatically and Safety Gate will consume it without changing core logic."
                )

            elif selected_mode == "Manual Entry":
                st.markdown("##### Manual evidence entry")
                manual_status = st.selectbox("Status", ["pass", "warn", "fail", "unknown"], key=f"manual_status_{key}")
                manual_severity = st.selectbox("Severity", ["low", "medium", "high", "critical"], key=f"manual_severity_{key}")
                manual_summary = st.text_area(
                    "Summary",
                    value=str(data.get("summary") or f"Manual evidence for {item['title']}"),
                    key=f"manual_summary_{key}",
                    height=90,
                )
                if st.button(f"Save manual evidence for {item['title']}", key=f"save_manual_{key}", width="stretch"):
                    payload = {
                        "evidence_type": key,
                        "service": data.get("service", "claims-api"),
                        "environment": data.get("environment", "production"),
                        "resource": data.get("resource", "linux-lab-01"),
                        "status": manual_status,
                        "severity": manual_severity,
                        "source_system": "manual_entry",
                        "summary": manual_summary,
                        "details": data.get("details", {}) if isinstance(data.get("details"), dict) else {},
                        "updated_at": datetime.now().isoformat(),
                    }
                    path.parent.mkdir(parents=True, exist_ok=True)
                    payload["uploaded_via"] = "client_onboarding_safety_gate_ui"
                    payload["uploaded_at"] = datetime.now().isoformat()
                    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    _onboarding_mark_client_evidence_supplied(key, path, "Manual Entry", "manual_entry")
                    st.success(f"Manual evidence saved to {path}")
                    st.rerun()

            else:
                st.warning("This evidence check is disabled for the pilot. Use this only for simulation/non-production onboarding.")

            with st.expander("Preview current evidence", expanded=False):
                st.json(data)

    with st.expander("Advanced Evidence Matrix", expanded=False):
        st.caption("Technical view for platform engineers. The card view above is the recommended client-facing onboarding flow.")
        try:
            import pandas as pd  # type: ignore
            st.dataframe(pd.DataFrame(evidence_rows), width="stretch", hide_index=True)
        except Exception:
            st.json(evidence_rows)

    with st.expander("Backend Safety Gate Config", expanded=False):
        st.json(backend_cfg)


# ---------------------------------------------------------------------------
# render_universal_api_adapter_setup_submenu
# ---------------------------------------------------------------------------

def render_universal_api_adapter_setup_submenu() -> None:
    st.markdown("### Universal API Adapter Setup")
    st.caption(
        "Adapters are disabled by default. Enable only the integrations the client can support now; "
        "the Safety Gate continues to read the same normalized evidence contract."
    )

    config_dir = Path("config") / "client_onboarding"
    api_config_path = config_dir / "api_adapter_config.json"
    api_section_path = config_dir / "api_onboarding_section.json"
    api_cfg = _onboarding_safe_read_json_file(api_config_path)
    api_section = _onboarding_safe_read_json_file(api_section_path)
    connector_rows = _onboarding_api_connector_rows(api_cfg)
    enabled_connectors = sum(1 for r in connector_rows if r.get("Status") == "ENABLED")

    c1, c2, c3 = st.columns(3)
    c1.metric("API Adapter Config", "Found" if api_config_path.exists() else "Missing")
    c2.metric("Enabled Connectors", str(enabled_connectors))
    c3.metric("Rollout API Mode", "Hybrid" if enabled_connectors else "File-first")

    try:
        import pandas as pd  # type: ignore
        st.dataframe(pd.DataFrame(connector_rows), width="stretch", hide_index=True)
    except Exception:
        st.json(connector_rows)

    st.markdown("#### Adapter Runner Test")
    st.code(
        "python .\\adapters\\universal_api_adapter.py run `\n"
        "  --service claims-api `\n"
        "  --environment production `\n"
        "  --resource linux-lab-01",
        language="powershell",
    )

    with st.expander("API Adapter Config", expanded=False):
        st.json(api_cfg)
    with st.expander("API Onboarding Section", expanded=False):
        st.json(api_section)


# ---------------------------------------------------------------------------
# render_evidence_contract_submenu
# ---------------------------------------------------------------------------

def render_evidence_contract_submenu() -> None:
    st.markdown("### Evidence Contract")
    st.caption("Every source system docks through this contract. The Safety Gate consumes normalized evidence instead of tool-specific APIs.")
    contract_path = Path("config") / "client_onboarding" / "universal_evidence_contract.json"
    contract = _onboarding_safe_read_json_file(contract_path)

    c1, c2 = st.columns(2)
    c1.metric("Evidence Contract", "Found" if contract_path.exists() else "Missing")
    c2.metric("Docking Model", "Universal Evidence")

    st.json(contract)
    st.markdown("#### Standard evidence types")
    st.code(
        "drift_status\n"
        "change_conflict\n"
        "error_budget_status\n"
        "concurrent_activity\n"
        "rollback_readiness\n"
        "blast_radius",
        language="text",
    )


# ---------------------------------------------------------------------------
# render_rollout_readiness_submenu
# ---------------------------------------------------------------------------

def render_rollout_readiness_submenu() -> None:
    st.markdown("### Rollout Readiness")
    config_dir = Path("config") / "client_onboarding"
    evidence_dir = Path("data") / "evidence"
    safety_config_path = config_dir / "safety_gate_config.json"
    api_config_path = config_dir / "api_adapter_config.json"
    contract_path = config_dir / "universal_evidence_contract.json"
    evidence_rows = _onboarding_evidence_file_health(evidence_dir)
    api_cfg = _onboarding_safe_read_json_file(api_config_path)
    connector_rows = _onboarding_api_connector_rows(api_cfg)
    backend_cfg = _onboarding_try_backend_safety_config()

    evidence_found = sum(1 for r in evidence_rows if r.get("Present") == "Yes")
    evidence_failures = sum(1 for r in evidence_rows if str(r.get("Status", "")).upper() in {"FAIL", "FAILED", "BLOCKED"})
    enabled_connectors = sum(1 for r in connector_rows if r.get("Status") == "ENABLED")

    readiness = [
        ("Safety Gate config file", safety_config_path.exists(), str(safety_config_path)),
        ("Evidence folder", evidence_dir.exists(), str(evidence_dir)),
        ("All six evidence files", evidence_found == 6, f"{evidence_found}/6 files found"),
        ("API adapter config", api_config_path.exists(), str(api_config_path)),
        ("Universal evidence contract", contract_path.exists(), str(contract_path)),
        ("Backend config endpoint", backend_cfg.get("status") != "unavailable", backend_cfg.get("detail", "available")),
        ("No failing evidence", evidence_failures == 0, f"{evidence_failures} failing item(s)"),
    ]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Evidence Files", f"{evidence_found}/6")
    c2.metric("API Connectors Enabled", str(enabled_connectors))
    c3.metric("Evidence Failures", str(evidence_failures))
    c4.metric("Rollout Mode", "Hybrid file + API" if enabled_connectors else "File-first active")

    for label, ok, detail in readiness:
        if ok:
            st.success(f"{label}: {detail}")
        else:
            st.error(f"{label}: {detail}")

    # MF-07: Live gate computation
    st.markdown("---")
    st.markdown("#### Live Safety Gate Check")
    st.caption("Run a live pre-execution safety gate check against the current evidence files to verify rollout readiness.")
    if st.button("Run Live Gate Check", key="rollout_live_gate_btn", use_container_width=False):
        try:
            payload = {
                "incident_id": "rollout_readiness_probe",
                "classification": "rollout_readiness_check",
                "playbook": "rollout_readiness_probe",
                "target_host": "probe",
                "risk_tier": "low",
                "execution_mode": "simulation",
                "requested_by": "rollout_readiness_ui",
            }
            resp = SESSION.post(f"{BACKEND_URL}/automation/pre_execution_check", json=payload, timeout=15)
            resp.raise_for_status()
            gate = resp.json()
            gate_status = gate.get("gate_status", "unknown")
            checks = gate.get("checks", {})

            status_color = {"passed": "success", "warning": "warning", "blocked": "error"}.get(gate_status, "info")
            getattr(st, status_color)(f"Live gate status: **{gate_status.upper()}**")

            for check_name, detail in checks.items():
                chk_status = str(detail.get("status") or "unknown").lower()
                icon = "✅" if chk_status == "pass" else ("⚠️" if chk_status in {"warn", "warning"} else "❌")
                ttl_warn = detail.get("ttl_warning", "")
                ttl_note = f" _(stale: {ttl_warn})_" if ttl_warn else ""
                st.markdown(f"{icon} **{nice_label(check_name)}**: {detail.get('summary', chk_status)}{ttl_note}")
        except Exception as exc:
            st.error(f"Live gate check failed: {exc}")

    st.markdown("#### Recommended committed rollout model")
    st.markdown(
        """
        **Day 1:** File-first onboarding evidence is active. No enterprise API integration is required.
        **Day 2+:** Enable push or scheduled export for one evidence type at a time.
        **Priority APIs:** Start with ITSM change conflict and observability SLO/error budget.
        **Later:** Add Terraform, cloud, Kubernetes, Git, and AWX adapters progressively.
        """
    )


# ---------------------------------------------------------------------------
# render_client_onboarding_page_with_submenus
# ---------------------------------------------------------------------------

def render_servicenow_integration() -> None:
    """ServiceNow integration configuration for the selected tenant."""
    import json, os

    tenant_id = st.session_state.get("tenant_id", st.session_state.get("selected_tenant_id", ""))
    if not tenant_id:
        st.info("Select a tenant from the sidebar first.")
        return

    cfg_path = os.path.join("data", "clients", tenant_id, "servicenow_config.json")

    def _load() -> Dict[str, Any]:
        try:
            with open(cfg_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(cfg: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)

    cfg = _load()

    st.markdown("#### ServiceNow Integration")
    st.caption(f"Tenant: **{tenant_id}** — settings saved to `{cfg_path}`")

    # ── Connection ────────────────────────────────────────────────────────
    st.markdown("**Connection**")
    c1, c2 = st.columns(2)
    with c1:
        instance_url = st.text_input(
            "Instance URL",
            value=cfg.get("instance_url", ""),
            placeholder="https://your-instance.service-now.com",
            key="snow_instance_url",
        )
        auth_type = st.selectbox(
            "Authentication",
            ["Basic (username / password)", "OAuth 2.0 (client credentials)"],
            index=0 if cfg.get("auth_type", "basic") == "basic" else 1,
            key="snow_auth_type",
        )
    with c2:
        if "Basic" in auth_type:
            username = st.text_input("Username", value=cfg.get("username", ""), key="snow_username")
            password = st.text_input("Password", value=cfg.get("password", ""), type="password", key="snow_password")
            client_id = client_secret = ""
        else:
            client_id     = st.text_input("Client ID",     value=cfg.get("client_id", ""),     key="snow_client_id")
            client_secret = st.text_input("Client Secret", value=cfg.get("client_secret", ""), type="password", key="snow_client_secret")
            username = password = ""

    st.markdown("---")

    # ── Table mapping ─────────────────────────────────────────────────────
    st.markdown("**Table Mapping**")
    st.caption("Map ServiceNow tables to SLM data types. Leave blank to skip that type.")
    tm = cfg.get("table_mapping", {})
    t1, t2, t3 = st.columns(3)
    with t1:
        tbl_incident = st.text_input("Incidents table",  value=tm.get("incidents", "incident"),  key="snow_tbl_inc")
        tbl_change   = st.text_input("Changes table",    value=tm.get("changes",   "change_request"), key="snow_tbl_chg")
    with t2:
        tbl_problem  = st.text_input("Problems table",   value=tm.get("problems",  "problem"),   key="snow_tbl_prb")
        tbl_ci       = st.text_input("CMDB CI table",    value=tm.get("cmdb_ci",   "cmdb_ci"),   key="snow_tbl_ci")
    with t3:
        st.markdown("**Pull filters**")
        state_filter = st.text_input(
            "Active incident states (comma-separated)",
            value=cfg.get("state_filter", "1,2,3"),
            key="snow_state_filter",
            help="ServiceNow state values: 1=New, 2=In Progress, 3=On Hold",
        )
        days_back = st.number_input(
            "Historical pull window (days)",
            min_value=1, max_value=365,
            value=cfg.get("days_back", 90),
            key="snow_days_back",
        )

    st.markdown("---")

    # ── Field mapping ─────────────────────────────────────────────────────
    st.markdown("**Field Mapping** — ServiceNow field → SLM field")
    st.caption("Customise if your instance uses non-standard field names.")
    fm = cfg.get("field_mapping", {})
    f1, f2 = st.columns(2)
    with f1:
        fm_number      = st.text_input("Ticket number",       value=fm.get("number",      "number"),       key="snow_fm_num")
        fm_short_desc  = st.text_input("Short description",   value=fm.get("short_desc",  "short_description"), key="snow_fm_sd")
        fm_description = st.text_input("Full description",    value=fm.get("description", "description"),  key="snow_fm_desc")
        fm_ci          = st.text_input("Configuration item",  value=fm.get("ci",          "cmdb_ci"),      key="snow_fm_ci")
    with f2:
        fm_state       = st.text_input("State",               value=fm.get("state",       "state"),        key="snow_fm_state")
        fm_priority    = st.text_input("Priority",            value=fm.get("priority",    "priority"),     key="snow_fm_prio")
        fm_opened      = st.text_input("Opened at",           value=fm.get("opened_at",   "opened_at"),    key="snow_fm_open")
        fm_resolved    = st.text_input("Resolved at",         value=fm.get("resolved_at", "resolved_at"),  key="snow_fm_res")

    st.markdown("---")

    # ── Ingestion settings ────────────────────────────────────────────────
    st.markdown("**Ingestion Settings**")
    i1, i2 = st.columns(2)
    with i1:
        batch_size   = st.number_input("Batch size (records per call)", min_value=10, max_value=1000, value=cfg.get("batch_size", 100), key="snow_batch")
        ingest_mode  = st.selectbox("Ingest mode", ["Full refresh", "Incremental (last modified)"], index=0 if cfg.get("ingest_mode","full")=="full" else 1, key="snow_ingest_mode")
    with i2:
        auto_classify = st.checkbox("Auto-classify ingested tickets with LLM", value=cfg.get("auto_classify", True), key="snow_auto_classify")
        promote_kb    = st.checkbox("Auto-promote resolved tickets to Knowledge base", value=cfg.get("promote_kb", False), key="snow_promote_kb")

    # ── Save / Test ───────────────────────────────────────────────────────
    st.markdown("---")
    b1, b2, _ = st.columns([1, 1, 3])
    with b1:
        if st.button("💾 Save Configuration", type="primary", key="snow_save"):
            new_cfg = {
                "instance_url": instance_url,
                "auth_type": "basic" if "Basic" in auth_type else "oauth2",
                "username": username,
                "password": password,
                "client_id": client_id,
                "client_secret": client_secret,
                "table_mapping": {
                    "incidents": tbl_incident,
                    "changes": tbl_change,
                    "problems": tbl_problem,
                    "cmdb_ci": tbl_ci,
                },
                "state_filter": state_filter,
                "days_back": int(days_back),
                "field_mapping": {
                    "number": fm_number, "short_desc": fm_short_desc,
                    "description": fm_description, "ci": fm_ci,
                    "state": fm_state, "priority": fm_priority,
                    "opened_at": fm_opened, "resolved_at": fm_resolved,
                },
                "batch_size": int(batch_size),
                "ingest_mode": "full" if "Full" in ingest_mode else "incremental",
                "auto_classify": auto_classify,
                "promote_kb": promote_kb,
            }
            _save(new_cfg)
            st.success("Configuration saved.")
    with b2:
        if st.button("🔌 Test Connection", key="snow_test"):
            if not instance_url:
                st.error("Enter an instance URL first.")
            else:
                import requests
                try:
                    resp = requests.get(
                        f"{instance_url.rstrip('/')}/api/now/table/incident?sysparm_limit=1",
                        auth=(username, password) if username else None,
                        timeout=8,
                    )
                    if resp.status_code == 200:
                        st.success(f"Connected — HTTP {resp.status_code}. ServiceNow API is reachable.")
                    elif resp.status_code == 401:
                        st.error("Authentication failed (401). Check username / password.")
                    else:
                        st.warning(f"Received HTTP {resp.status_code}. Check the instance URL and credentials.")
                except Exception as e:
                    st.error(f"Connection failed: {e}")

    # ── Current config summary ────────────────────────────────────────────
    if cfg:
        with st.expander("Current saved configuration", expanded=False):
            safe = {k: ("***" if k in {"password", "client_secret"} else v) for k, v in cfg.items()}
            st.json(safe)


def render_client_onboarding_page_with_submenus() -> None:
    """Preserve the existing Client Onboarding Studio and add new submenus below it."""
    st.subheader("Client Onboarding")
    tabs = st.tabs([
        "Client Onboarding Studio",
        "Safety Gate Evidence Setup",
        "Evidence Contract",
        "Rollout Readiness",
        "ServiceNow Integration",
    ])

    with tabs[0]:
        render_client_onboarding_studio()

    with tabs[1]:
        render_safety_gate_evidence_setup_submenu()

    with tabs[2]:
        render_evidence_contract_submenu()

    with tabs[3]:
        render_rollout_readiness_submenu()

    with tabs[4]:
        render_servicenow_integration()

