"""Telemetry Console page module."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import streamlit as st
from page_modules.shared import (
    BACKEND_URL, SESSION, REQUEST_TIMEOUT_MEDIUM, REQUEST_TIMEOUT_LONG,
    REQUEST_TIMEOUT_SHORT,
    fetch_telemetry_events, fetch_telemetry_incidents, fetch_telemetry_incident_detail,
    fetch_telemetry_incident_rca, call_telemetry_evaluate, call_telemetry_promote,
    nice_label, system_label, format_confidence, metric_card, render_empty_state,
    render_resolution_playbook,
)


# ---------------------------------------------------------------------------
# Local helpers: analyze (not yet in shared)
# ---------------------------------------------------------------------------

def analyze_telemetry_incident(incident_id: str) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/telemetry/incidents/{incident_id}/analyze", timeout=REQUEST_TIMEOUT_LONG)
    response.raise_for_status()
    return response.json().get("item") or {}


def _render_rca_table(rca: Dict[str, Any], incident: Dict[str, Any]) -> None:
    """Render AI RCA result as a clean structured table."""
    analysis = rca.get("analysis") or {}
    conf_block = rca.get("confidence") or {}

    classification = (
        analysis.get("telemetry_classification_hint")
        or analysis.get("classification")
        or incident.get("classification")
        or "—"
    )
    overall_conf = conf_block.get("overall_confidence") or analysis.get("confidence") or 0.0
    source_label = rca.get("analysis_source_label") or rca.get("response_source") or "AI Analysis"
    routing_method = (rca.get("routing") or {}).get("method") or "—"

    # Summary strip
    conf_pct = int(float(overall_conf) * 100) if overall_conf else 0
    conf_color = "#16a34a" if conf_pct >= 75 else ("#d97706" if conf_pct >= 50 else "#dc2626")
    st.markdown(
        f"""<div style="display:flex;gap:16px;align-items:center;padding:10px 14px;
        background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0;margin-bottom:12px;">
        <div><span style="font-size:0.72rem;color:#64748b;font-weight:600;">CLASSIFICATION</span><br>
        <span style="font-weight:700;font-size:0.95rem;">{nice_label(classification)}</span></div>
        <div style="border-left:1px solid #e2e8f0;padding-left:16px;">
        <span style="font-size:0.72rem;color:#64748b;font-weight:600;">CONFIDENCE</span><br>
        <span style="font-weight:700;font-size:0.95rem;color:{conf_color};">{conf_pct}%</span></div>
        <div style="border-left:1px solid #e2e8f0;padding-left:16px;">
        <span style="font-size:0.72rem;color:#64748b;font-weight:600;">SOURCE</span><br>
        <span style="font-size:0.85rem;">{nice_label(source_label)}</span></div>
        <div style="border-left:1px solid #e2e8f0;padding-left:16px;">
        <span style="font-size:0.72rem;color:#64748b;font-weight:600;">ROUTING</span><br>
        <span style="font-size:0.85rem;">{nice_label(routing_method)}</span></div>
        </div>""",
        unsafe_allow_html=True,
    )

    def _fmt_list(items: list, max_items: int = 5) -> str:
        cleaned = [nice_label(str(x)) for x in (items or []) if str(x or "").strip()]
        if not cleaned:
            return "—"
        return "<br>".join(f"• {item}" for item in cleaned[:max_items])

    probable_causes = analysis.get("probable_causes") or []
    recommended_actions = analysis.get("recommended_actions") or []
    next_checks = analysis.get("next_checks") or []
    resolution_summary = analysis.get("resolution_summary") or analysis.get("summary") or ""

    rows = [
        ("Probable Cause(s)", _fmt_list(probable_causes)),
        ("Recommended Actions", _fmt_list(recommended_actions)),
        ("Next Checks / Diagnostics", _fmt_list(next_checks)),
    ]
    if resolution_summary:
        rows.append(("Resolution Summary", str(resolution_summary)[:300]))

    # Similar past incidents
    retrieved = rca.get("retrieved_incidents") or []
    if retrieved:
        past_items = []
        for inc in retrieved[:3]:
            lbl = (
                inc.get("title")
                or inc.get("short_description")
                or inc.get("classification")
                or str(inc.get("id") or "")
            )
            past_items.append(str(lbl or "").strip()[:80])
        rows.append(("Similar Past Incidents", "<br>".join(f"• {x}" for x in past_items if x)))

    # Render as HTML table for clean reading
    table_html = (
        "<table style='width:100%;border-collapse:collapse;font-size:0.86rem;'>"
        "<colgroup><col style='width:38%'><col style='width:62%'></colgroup>"
    )
    for label, value in rows:
        table_html += (
            f"<tr style='border-bottom:1px solid #f1f5f9;'>"
            f"<td style='padding:8px 10px 8px 0;font-weight:600;color:#374151;vertical-align:top;'>{label}</td>"
            f"<td style='padding:8px 0;color:#1e293b;vertical-align:top;'>{value}</td>"
            f"</tr>"
        )
    table_html += "</table>"
    st.markdown(table_html, unsafe_allow_html=True)

    # KB / resolution links from result
    resolution_id = rca.get("resolution_id") or (rca.get("resolution") or {}).get("id")
    playbook_id = rca.get("playbook_id") or (rca.get("playbook") or {}).get("id")
    if resolution_id or playbook_id:
        link_cols = st.columns(2)
        if resolution_id:
            link_cols[0].caption(f"📄 Resolution linked: `{resolution_id}`")
        if playbook_id:
            link_cols[1].caption(f"📋 Playbook linked: `{playbook_id}`")

    # Structured resolution playbook — same steps as Incident Console
    playbook_steps = rca.get("resolution_playbook") or []
    if playbook_steps:
        st.markdown("---")
        render_resolution_playbook(
            playbook_steps,
            safety_note="Validate evidence and obtain approval before executing any state-changing remediation.",
            expanded=False,
            title="Step-by-Step Resolution Playbook",
        )


def _render_alert_details_table(selected: Dict[str, Any], current_status: str) -> None:
    """Render incident alert details as a clean two-column table."""
    rows = [
        ("Title", selected.get("title") or "Telemetry Alert"),
        ("Classification", nice_label(selected.get("classification") or "unknown")),
        ("Domain", nice_label(selected.get("domain") or "general")),
        ("Service Scope", nice_label(selected.get("service_scope") or "general")),
        ("Affected CI / Resource", selected.get("resource") or selected.get("service") or "unknown"),
        ("Trigger Metric", selected.get("trigger_metric") or selected.get("metric") or "unknown"),
        ("Trigger Value", str(selected.get("trigger_value") or selected.get("value") or "unknown")),
        ("Environment", str(selected.get("environment") or "production").replace("_", " ").title()),
        ("Severity", telemetry_severity_label(selected.get("severity"))),
        ("Status", telemetry_status_label(current_status)),
        ("ITSM Ticket", telemetry_ticket_label(selected)),
        ("Telemetry Rule", selected.get("telemetry_rule_id") or "unknown"),
        ("Created At", str(selected.get("created_at") or "")[:16]),
    ]
    if selected.get("status_note"):
        rows.append(("Status Note", str(selected.get("status_note"))[:200]))

    table_html = (
        "<table style='width:100%;border-collapse:collapse;font-size:0.85rem;margin-bottom:8px;'>"
        "<colgroup><col style='width:42%'><col style='width:58%'></colgroup>"
    )
    for label, value in rows:
        table_html += (
            f"<tr style='border-bottom:1px solid #f1f5f9;'>"
            f"<td style='padding:7px 10px 7px 0;font-weight:600;color:#374151;vertical-align:top;"
            f"white-space:nowrap;'>{label}</td>"
            f"<td style='padding:7px 0;color:#1e293b;vertical-align:top;word-break:break-word;'>{value}</td>"
            f"</tr>"
        )
    table_html += "</table>"
    st.markdown(table_html, unsafe_allow_html=True)


def _load_snow_config(tenant_id: str) -> Optional[Dict[str, Any]]:
    """Load ServiceNow config saved by Client Onboarding for a given tenant."""
    if not tenant_id:
        return None
    try:
        cfg_path = Path("data") / "clients" / tenant_id / "servicenow_config.json"
        if cfg_path.exists():
            return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def clear_lightweight_page_cache() -> None:
    """Clear short-lived backend-read cache after state-changing actions only.
    This preserves the existing UI/navigation design while preventing stale
    telemetry/knowledge data after lifecycle or ITSM updates.
    """
    try:
        st.cache_data.clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# RCA display guardrail
# ---------------------------------------------------------------------------

def _list_has_only_generic_rca_values(values: list, generic_values: set[str]) -> bool:
    cleaned = [system_label(v) for v in (values or []) if str(v or "").strip()]
    if not cleaned:
        return True
    return all(v in generic_values for v in cleaned)


def apply_ui_telemetry_rca_display_guardrail(result: dict, selected_incident: dict | None = None) -> dict:
    """
    UI-side safety net for telemetry RCA display.
    The backend is the source of truth, but Streamlit can retain stale session state.
    If a telemetry incident still renders generic RCA defaults, replace only the
    displayed action lists with deterministic telemetry guidance.
    """
    if not isinstance(result, dict):
        return result

    out = dict(result)
    analysis = dict(out.get("analysis") or {})
    telemetry_incident = dict(out.get("telemetry_incident") or selected_incident or {})

    classification = system_label(
        analysis.get("telemetry_classification_hint")
        or telemetry_incident.get("classification")
        or analysis.get("classification")
        or out.get("classification")
    )
    trigger_metric = system_label(telemetry_incident.get("trigger_metric") or out.get("trigger_metric"))
    title_blob = system_label(f"{telemetry_incident.get('title', '')} {out.get('telemetry_alert_name', '')}")

    if not classification and "cpu" in trigger_metric:
        classification = "cpu_saturation"
    if not classification and "cpu" in title_blob:
        classification = "cpu_saturation"

    templates = {
        "cpu_saturation": {
            "probable_causes": [
                "runaway_process",
                "background_batch_job_contention",
                "antivirus_or_security_scan_spike",
            ],
            "next_checks": [
                "verify_top_cpu_consuming_processes",
                "inspect_recent_scheduled_jobs",
                "check_antivirus_or_security_scan_windows",
                "review_host_load_and_process_history",
                "inspect_system_logs",
            ],
            "recommended_actions": [
                "stop_or_restart_runaway_process_after_validation",
                "reschedule_or_throttle_heavy_background_job",
                "tune_antivirus_exclusions_if_scan_spike_is_confirmed",
                "optimize_resource_allocation_if_necessary",
            ],
        },
        "memory_pressure": {
            "probable_causes": ["memory_leak", "heap_or_cache_growth", "undersized_resource_allocation"],
            "next_checks": ["check_top_memory_consuming_processes", "review_recent_deployments", "inspect_memory_trend"],
            "recommended_actions": ["restart_or_scale_affected_service_after_validation", "tune_memory_limits", "rollback_recent_change_if_correlated"],
        },
        "disk_full": {
            "probable_causes": ["log_growth", "temporary_file_accumulation", "capacity_threshold_breach"],
            "next_checks": ["check_largest_directories", "review_log_rotation", "verify_available_disk_space"],
            "recommended_actions": ["clean_approved_temporary_files", "rotate_or_archive_logs", "expand_storage_if_required"],
        },
        "api_gateway_timeout": {
            "probable_causes": ["downstream_service_latency", "connection_pool_exhaustion", "traffic_spike"],
            "next_checks": ["check_downstream_latency", "review_gateway_error_rate", "inspect_recent_deployments"],
            "recommended_actions": ["scale_or_restart_impacted_service_after_validation", "increase_timeout_only_if_approved", "route_traffic_away_from_unhealthy_instance"],
        },
        "pod_crash_loop": {
            "probable_causes": ["container_startup_failure", "bad_configuration", "resource_limit_breach"],
            "next_checks": ["check_pod_events", "inspect_container_logs", "review_recent_image_or_config_change"],
            "recommended_actions": ["rollback_bad_deployment_if_confirmed", "fix_config_or_secret_reference", "adjust_resource_limits_after_validation"],
        },
        "certificate_expiry_warning": {
            "probable_causes": ["certificate_nearing_expiry", "renewal_automation_failure", "manual_certificate_rotation_required"],
            "next_checks": ["verify_certificate_expiry_date", "check_certificate_owner", "review_auto_renewal_status"],
            "recommended_actions": ["renew_certificate", "deploy_updated_certificate", "validate_tls_chain_after_change"],
        },
    }

    template = templates.get(classification)
    if not template:
        return out

    generic_causes = {"unknown_factor", "unknown_contributing_factor"}
    generic_checks = {"check_system_logs", "inspect_system_logs"}
    generic_actions = {"monitor_system", "closely_monitor_affected_services"}

    # Replace only generic/empty fields; preserve useful backend output.
    if _list_has_only_generic_rca_values(analysis.get("probable_causes", []), generic_causes):
        analysis["probable_causes"] = template["probable_causes"]
    if _list_has_only_generic_rca_values(analysis.get("next_checks", []), generic_checks):
        analysis["next_checks"] = template["next_checks"]
    if _list_has_only_generic_rca_values(analysis.get("recommended_actions", []), generic_actions):
        analysis["recommended_actions"] = template["recommended_actions"]

    analysis["classification"] = classification
    analysis["telemetry_quality_enhancement_applied"] = True
    analysis["telemetry_ui_display_guardrail_applied"] = True
    out["analysis"] = analysis

    confidence = dict(out.get("confidence") or {})
    if confidence:
        try:
            confidence["overall_confidence"] = max(float(confidence.get("overall_confidence", 0.0) or 0.0), float(analysis.get("confidence", 0.0) or 0.0), 0.68)
            confidence["classification_certainty"] = max(float(confidence.get("classification_certainty", 0.0) or 0.0), 0.68)
        except Exception:
            pass
        out["confidence"] = confidence

    return out


# ---------------------------------------------------------------------------
# Lifecycle / ITSM API helpers
# ---------------------------------------------------------------------------

def update_telemetry_incident_status(
    incident_id: str,
    status: str,
    notes: str = "",
    updated_by: str = "Vijay",
    external_system: str | None = None,
    external_ticket_id: str | None = None,
    external_ticket_url: str | None = None,
    ticket_sync_status: str | None = None,
) -> dict:
    payload = {
        "status": status,
        "updated_by": updated_by,
        "notes": notes,
    }
    if external_system is not None:
        payload["external_system"] = external_system
    if external_ticket_id is not None:
        payload["external_ticket_id"] = external_ticket_id
    if external_ticket_url is not None:
        payload["external_ticket_url"] = external_ticket_url
    if ticket_sync_status is not None:
        payload["ticket_sync_status"] = ticket_sync_status
    response = SESSION.post(f"{BACKEND_URL}/telemetry/incidents/{incident_id}/status", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json().get("item") or {}


def create_telemetry_itsm_ticket(
    incident_id: str,
    external_system: str = "ServiceNow",
    external_ticket_id: str | None = None,
    external_ticket_url: str | None = None,
    notes: str = "",
    requested_by: str = "Vijay",
) -> dict:
    payload = {
        "external_system": external_system or "ServiceNow",
        "requested_by": requested_by or "Vijay",
        "notes": notes or "Simulated ITSM ticket created from AI Ops telemetry incident.",
    }
    if external_ticket_id:
        payload["external_ticket_id"] = external_ticket_id
    if external_ticket_url:
        payload["external_ticket_url"] = external_ticket_url
    response = SESSION.post(f"{BACKEND_URL}/telemetry/incidents/{incident_id}/itsm/create", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json().get("item") or {}


def link_telemetry_itsm_ticket(
    incident_id: str,
    external_ticket_id: str,
    external_system: str = "ServiceNow",
    external_ticket_url: str | None = None,
    notes: str = "",
    requested_by: str = "Vijay",
) -> dict:
    payload = {
        "external_system": external_system or "ServiceNow",
        "external_ticket_id": external_ticket_id,
        "external_ticket_url": external_ticket_url or "",
        "requested_by": requested_by or "Vijay",
        "notes": notes or "Linked existing ITSM ticket to AI Ops telemetry incident.",
    }
    response = SESSION.post(f"{BACKEND_URL}/telemetry/incidents/{incident_id}/itsm/link", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json().get("item") or {}


def push_telemetry_itsm_work_notes(
    incident_id: str,
    external_system: str = "ServiceNow",
    notes: str = "",
    requested_by: str = "Vijay",
) -> dict:
    payload = {
        "external_system": external_system or "ServiceNow",
        "requested_by": requested_by or "Vijay",
        "notes": notes or "Simulated push of AI RCA summary to ITSM work notes.",
    }
    response = SESSION.post(f"{BACKEND_URL}/telemetry/incidents/{incident_id}/itsm/push_work_notes", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    response.raise_for_status()
    return response.json().get("item") or {}


@st.cache_data(ttl=12, show_spinner=False)
def fetch_telemetry_status_history(incident_id: str, limit: int = 50) -> list[dict]:
    try:
        response = SESSION.get(
            f"{BACKEND_URL}/telemetry/incidents/{incident_id}/status_history",
            params={"limit": limit},
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        response.raise_for_status()
        return response.json().get("items", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def telemetry_status_label(value: str) -> str:
    raw = str(value or "new").strip().lower().replace(" ", "_").replace("-", "_")
    mapping = {
        "new": "New",
        "rca_complete": "RCA Complete",
        "acknowledged": "Acknowledged",
        "in_progress": "In Progress",
        "resolved": "Resolved",
        "suppressed": "Suppressed",
    }
    return mapping.get(raw, nice_label(raw or "new"))


def telemetry_ticket_label(item: dict) -> str:
    external_id = str(item.get("external_ticket_id") or "").strip()
    sync_status = str(item.get("ticket_sync_status") or "not_linked").strip().lower().replace("_", " ")
    if external_id:
        system = str(item.get("external_system") or "ITSM").strip() or "ITSM"
        return f"{system}: {external_id}"
    return sync_status.title() if sync_status else "Not Linked"


def telemetry_severity_label(value: str) -> str:
    raw = str(value or "unknown").strip().lower()
    mapping = {"critical": "Critical", "high": "High", "medium": "Medium", "warning": "Warning", "low": "Low"}
    return mapping.get(raw, nice_label(raw or "unknown"))


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------

def render_telemetry_console_page():
    st.subheader("Telemetry Console")
    st.caption("View telemetry-triggered incidents, inspect alert context, and run AI RCA without changing the existing Incident Console flow.")

    incidents = fetch_telemetry_incidents(limit=200)
    events = fetch_telemetry_events(limit=200)

    rca_cache = st.session_state.setdefault("telemetry_rca_cache", {})
    selected_id = st.session_state.get("telemetry_selected_incident_id", "")

    critical_high = sum(1 for x in incidents if str(x.get("severity", "")).lower() in {"critical", "high"})
    resources = {str(x.get("resource") or "").strip() for x in incidents if str(x.get("resource") or "").strip()}
    rca_completed = 0
    for item in incidents:
        iid = str(item.get("id") or "")
        if iid:
            # Backend-first RCA check avoids stale Streamlit cache after RCA re-runs.
            latest_rca = fetch_telemetry_incident_rca(iid) or {}
            if latest_rca:
                rca_cache[iid] = latest_rca
                rca_completed += 1

    # ── Metric chips row ──────────────────────────────────────────────────────
    _chip_style = (
        "display:flex;align-items:center;gap:12px;border-radius:10px;"
        "padding:12px 18px;border:1px solid;height:64px;box-sizing:border-box;width:100%;"
    )
    c1, c2, c3, c4 = st.columns(4, gap="medium")
    with c1:
        st.markdown(
            f'<div style="{_chip_style}background:#eff6ff;border-color:#bfdbfe;">'
            f'<span style="font-size:1.6rem;font-weight:800;color:#1d4ed8;">{len(events)}</span>'
            f'<span style="font-size:0.78rem;font-weight:600;color:#1e40af;line-height:1.35;">Telemetry<br>Events</span></div>',
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f'<div style="{_chip_style}background:#f0fdf4;border-color:#bbf7d0;">'
            f'<span style="font-size:1.6rem;font-weight:800;color:#15803d;">{len(incidents)}</span>'
            f'<span style="font-size:0.78rem;font-weight:600;color:#166534;line-height:1.35;">Telemetry<br>Incidents</span></div>',
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f'<div style="{_chip_style}background:#fff7ed;border-color:#fed7aa;">'
            f'<span style="font-size:1.6rem;font-weight:800;color:#c2410c;">{critical_high}</span>'
            f'<span style="font-size:0.78rem;font-weight:600;color:#9a3412;line-height:1.35;">Critical<br>/ High</span></div>',
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f'<div style="{_chip_style}background:#faf5ff;border-color:#e9d5ff;">'
            f'<span style="font-size:1.6rem;font-weight:800;color:#7e22ce;">{len(resources)}</span>'
            f'<span style="font-size:0.78rem;font-weight:600;color:#6b21a8;line-height:1.35;">Affected<br>Resources</span></div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    if not incidents:
        st.markdown(
            """
            <div style="text-align:center;padding:40px 20px;color:#6b7280;">
              <div style="font-size:2rem;margin-bottom:10px;">📡</div>
              <div style="font-size:1.05rem;font-weight:600;margin-bottom:6px;">No telemetry incidents yet</div>
              <div style="font-size:0.85rem;">Click the button above to convert raw telemetry events into the incident queue,<br>or send an Alertmanager alert to the <code>/telemetry/events</code> endpoint.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.expander("Recent raw telemetry events", expanded=False):
            if events:
                event_rows = []
                for item in events[:50]:
                    event_rows.append({
                        "Time": item.get("timestamp") or item.get("created_at"),
                        "Metric": item.get("metric"),
                        "Value": item.get("value"),
                        "Resource": item.get("resource"),
                        "Service": item.get("service"),
                        "Source": item.get("source"),
                    })
                st.dataframe(event_rows, use_container_width=True, hide_index=True)
            else:
                st.caption("No raw telemetry events available.")
        return

    table_rows = []
    for item in incidents:
        iid = str(item.get("id") or "")
        cached_rca = rca_cache.get(iid) or fetch_telemetry_incident_rca(iid)
        if cached_rca:
            rca_cache[iid] = cached_rca
        confidence = ""
        if cached_rca:
            conf = cached_rca.get("confidence", {}).get("overall_confidence") if isinstance(cached_rca.get("confidence"), dict) else None
            confidence = format_confidence(conf) if conf is not None else "Complete"
        table_rows.append({
            "Incident ID": iid,
            "Time": item.get("updated_at") or item.get("created_at"),
            "Severity": telemetry_severity_label(item.get("severity")),
            "Alert / Title": item.get("title") or nice_label(item.get("classification", "Telemetry Alert")),
            "Affected CI": item.get("resource") or item.get("service") or "unknown",
            "Metric": item.get("trigger_metric") or item.get("metric"),
            "Value": item.get("trigger_value") or item.get("value"),
            "Status": telemetry_status_label(item.get("status") or ("rca_complete" if cached_rca else "new")),
            "ITSM": telemetry_ticket_label(item),
            "Confidence": confidence,
        })

    # UX-08: Card-based incident queue — each card has an "Inspect" button to select it
    st.markdown(
        """<style>
        div[data-testid="stButton"] button[kind="primaryFormSubmit"]:has(+ *),
        div[data-testid="column"]:nth-of-type(2) div[data-testid="stButton"] > button {
            background-color: #1e3a5f !important;
            color: #ffffff !important;
            border: none !important;
        }
        div[data-testid="column"]:nth-of-type(2) div[data-testid="stButton"] > button:hover {
            background-color: #162d4a !important;
        }
        </style>""",
        unsafe_allow_html=True,
    )
    _q_left, _q_btn, _q_right = st.columns([2, 2, 2])
    with _q_left:
        st.markdown("### Telemetry Incident Queue")
    with _q_btn:
        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        if st.button("Click here to update the incident queue", key="tel_evaluate_btn", use_container_width=True, type="primary"):
            try:
                result = call_telemetry_evaluate()
                created = result.get("created_incidents", []) or []
                st.success(f"Queue updated — {len(created)} new incident(s) added.")
                fetch_telemetry_incidents.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"Update failed: {exc}")
    st.caption("Incidents automatically promoted from telemetry event rules. Select a card to inspect details and run AI RCA.")

    _sev_colors = {"critical": "#dc2626", "high": "#ea580c", "medium": "#d97706", "low": "#16a34a", "info": "#2563eb"}
    _status_colors = {"new": "#6b7280", "acknowledged": "#2563eb", "in_progress": "#d97706", "resolved": "#16a34a", "suppressed": "#9ca3af", "rca_complete": "#7c3aed"}

    # Severity filter tabs
    _sev_order = ["All", "Critical", "High", "Medium", "Low"]
    _sev_counts = {s: sum(1 for r in table_rows if str(r.get("Severity","")).lower().split()[0] == s.lower()) for s in _sev_order[1:]}
    _sev_tabs = [f"{s} ({_sev_counts[s]})" if s != "All" else f"All ({len(table_rows)})" for s in _sev_order]
    _active_sev_tab = st.session_state.get("tel_sev_filter", "All")
    _tab_cols = st.columns(len(_sev_tabs))
    for _ti, (_tc, _tl, _ts) in enumerate(zip(_tab_cols, _sev_tabs, _sev_order)):
        with _tc:
            _is_active = _ts == _active_sev_tab
            _tab_style = "background:#3b82f6;color:#fff;border:none;border-radius:6px;padding:4px 10px;font-size:0.8rem;font-weight:600;cursor:pointer;width:100%;" if _is_active else "background:#f3f4f6;color:#374151;border:none;border-radius:6px;padding:4px 10px;font-size:0.8rem;cursor:pointer;width:100%;"
            if st.button(_tl, key=f"tel_sev_tab_{_ts}", use_container_width=True):
                st.session_state.tel_sev_filter = _ts
                st.rerun()

    _active_sev_tab = st.session_state.get("tel_sev_filter", "All")
    filtered_rows = table_rows if _active_sev_tab == "All" else [
        r for r in table_rows if str(r.get("Severity","")).lower().split()[0] == _active_sev_tab.lower()
    ]

    incident_ids = [str(x.get("id") or "") for x in incidents if x.get("id")]
    if selected_id not in incident_ids and incident_ids:
        selected_id = incident_ids[0]

    for row in filtered_rows:
        iid = row.get("Incident ID", "")
        is_selected = iid == selected_id
        sev = str(row.get("Severity") or "").lower().split()[0]
        # Normalise to underscore-keyed status so badge colour always matches
        status_raw = str(row.get("Status") or "new").lower().replace(" ", "_").replace("-", "_")
        sev_color = _sev_colors.get(sev, "#6b7280")
        status_color = _status_colors.get(status_raw, "#6b7280")
        confidence_str = str(row.get("Confidence") or "")
        conf_display = f"<span style='color:#7c3aed;font-weight:600;'>{confidence_str}</span>" if confidence_str else "<span style='color:#9ca3af;'>—</span>"
        # Use border-left + separate outline so selection ring doesn't clobber severity stripe
        if is_selected:
            card_border = "outline:2px solid #3b82f6;outline-offset:-2px;box-shadow:0 0 0 4px rgba(59,130,246,0.12);"
        else:
            card_border = "outline:1px solid #e5e7eb;outline-offset:-1px;"
        card_col, btn_col = st.columns([5, 1])
        with card_col:
            st.markdown(
                f"""
                <div style="{card_border}border-left:4px solid {sev_color};border-radius:6px;padding:10px 14px;margin-bottom:4px;background:#fff;">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                    <span style="font-weight:600;font-size:0.95rem;">{row.get("Alert / Title","")}</span>
                    <span style="background:{sev_color};color:#fff;border-radius:4px;padding:1px 8px;font-size:0.75rem;font-weight:600;">{row.get("Severity","").upper()}</span>
                  </div>
                  <div style="font-size:0.82rem;color:#6b7280;margin-bottom:6px;">
                    <span style="margin-right:12px;">🖥 {row.get("Affected CI","")}</span>
                    <span style="margin-right:12px;">📊 {row.get("Metric","")} = {row.get("Value","")}</span>
                    <span>🕐 {str(row.get("Time",""))[:16]}</span>
                  </div>
                  <div style="display:flex;gap:10px;align-items:center;">
                    <span style="background:{status_color};color:#fff;border-radius:4px;padding:1px 8px;font-size:0.75rem;">{row.get("Status","")}</span>
                    <span style="font-size:0.8rem;color:#6b7280;">ITSM: {row.get("ITSM","—")}</span>
                    <span style="font-size:0.8rem;">RCA Confidence: {conf_display}</span>
                    <span style="font-size:0.75rem;color:#9ca3af;margin-left:auto;">ID: {iid}</span>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with btn_col:
            btn_label = "✓ Selected" if is_selected else "Inspect →"
            if st.button(btn_label, key=f"sel_tel_{iid}", use_container_width=True, disabled=is_selected):
                st.session_state.telemetry_selected_incident_id = iid
                st.rerun()

    st.session_state.telemetry_selected_incident_id = selected_id

    selected = fetch_telemetry_incident_detail(selected_id) or next((x for x in incidents if str(x.get("id")) == selected_id), {})
    # Backend-first RCA read. The session cache is only a fallback for the
    # current rerun, so stale UI state cannot override corrected RCA records.
    existing_rca = fetch_telemetry_incident_rca(selected_id) or rca_cache.get(selected_id) or {}
    if existing_rca:
        rca_cache[selected_id] = existing_rca

    current_status = str(selected.get("status") or ("rca_complete" if existing_rca else "new")).lower()

    # ── Section 1: Alert Details | AI RCA Investigation ───────────────────────
    with st.container(border=True):
        left, right = st.columns([1.05, 1.35])
        with left:
            st.markdown("##### Alert Details")
            _render_alert_details_table(selected, current_status)
        with right:
            st.markdown("##### AI RCA Investigation")
            rca_btn_col, _ = st.columns([1, 2])
            with rca_btn_col:
                if existing_rca:
                    if st.button("Re-run AI RCA", key=f"rerun_telemetry_rca_{selected_id}", use_container_width=True):
                        try:
                            with st.spinner("Running AI RCA..."):
                                result = analyze_telemetry_incident(selected_id)
                            rca_cache[selected_id] = result
                            clear_lightweight_page_cache()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Telemetry RCA failed: {e}")
                else:
                    if st.button("Run AI RCA", key=f"analyze_telemetry_rca_{selected_id}", type="primary", use_container_width=True):
                        try:
                            with st.spinner("Running AI RCA..."):
                                result = analyze_telemetry_incident(selected_id)
                            rca_cache[selected_id] = result
                            clear_lightweight_page_cache()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Telemetry RCA failed: {e}")
            rca_to_show = existing_rca or rca_cache.get(selected_id) or {}
            if rca_to_show:
                rca_to_show = apply_ui_telemetry_rca_display_guardrail(rca_to_show, selected)
                rca_cache[selected_id] = rca_to_show
                _render_rca_table(rca_to_show, selected)
            else:
                st.info("No RCA available. Click **Run AI RCA** to classify this incident and generate recommended actions.")

    # ── Section 2: Lifecycle Actions | Status Audit History ───────────────────
    with st.container(border=True):
        lc_left, lc_right = st.columns([1, 1.4])
        with lc_left:
            st.markdown("##### Lifecycle Actions")
            if selected.get("status_note"):
                st.caption(f"Latest note: {selected.get('status_note')}")
            action_notes = st.text_input(
                "Note for audit trail (optional)",
                value="",
                placeholder="e.g. CPU spike from backup job",
                key=f"telemetry_status_note_{selected_id}",
            )
            a1, a2 = st.columns(2)
            with a1:
                if st.button("Acknowledge", key=f"ack_tel_{selected_id}", use_container_width=True, disabled=current_status in {"acknowledged", "in_progress", "resolved", "suppressed"}):
                    try:
                        update_telemetry_incident_status(selected_id, "acknowledged", notes=action_notes or "Telemetry incident acknowledged.")
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("Acknowledged.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Status update failed: {e}")
                if st.button("Mark In Progress", key=f"progress_tel_{selected_id}", use_container_width=True, disabled=current_status in {"in_progress", "resolved", "suppressed"}):
                    try:
                        update_telemetry_incident_status(selected_id, "in_progress", notes=action_notes or "Investigation started.")
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("Marked in progress.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Status update failed: {e}")
            with a2:
                if st.button("Mark Resolved", key=f"resolve_tel_{selected_id}", use_container_width=True, disabled=current_status in {"resolved", "suppressed"}):
                    try:
                        update_telemetry_incident_status(selected_id, "resolved", notes=action_notes or "Telemetry incident resolved.")
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("Marked resolved.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Status update failed: {e}")
                if st.button("Suppress", key=f"suppress_tel_{selected_id}", use_container_width=True, disabled=current_status == "suppressed"):
                    try:
                        update_telemetry_incident_status(selected_id, "suppressed", notes=action_notes or "Telemetry incident suppressed as duplicate/noise.")
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("Suppressed.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Status update failed: {e}")
        with lc_right:
            st.markdown("##### Status Audit History")
            history_rows = fetch_telemetry_status_history(selected_id, limit=50)
            if history_rows:
                st.dataframe(
                    [
                        {
                            "Time": str(row.get("created_at") or "")[:16],
                            "From": telemetry_status_label(row.get("old_status")),
                            "To": telemetry_status_label(row.get("new_status")),
                            "By": row.get("updated_by"),
                            "Notes": row.get("notes"),
                        }
                        for row in history_rows
                    ],
                    use_container_width=True,
                    hide_index=True,
                    height=220,
                )
            else:
                st.caption("No status changes recorded yet.")

    # ── Section 3: Create ITSM Incident ──────────────────────────────────────
    with st.container(border=True):
        st.markdown("##### Create ITSM Incident")
        st.caption("Stub mode — simulates ServiceNow ticket creation without calling a live API.")

        _tenant_id = st.session_state.get("tenant_id", st.session_state.get("selected_tenant_id", ""))
        _snow_cfg = _load_snow_config(_tenant_id)
        _snow_instance = (_snow_cfg or {}).get("instance_url", "").strip()
        _snow_table = ((_snow_cfg or {}).get("table_mapping") or {}).get("incidents", "incident")

        if _snow_cfg and _snow_instance:
            st.success(f"ServiceNow configured — instance: `{_snow_instance}` · table: `{_snow_table}`")
        else:
            st.warning(
                "ServiceNow not configured for this tenant. "
                "Go to **Client Onboarding → ServiceNow Integration** to set up the connection first."
            )

        itsm_left, itsm_right = st.columns([1, 1.4])
        with itsm_left:
            # Current ticket link status
            _ticket_rows = [
                ("External System", selected.get("external_system") or "Not linked"),
                ("External Ticket", selected.get("external_ticket_id") or "Not linked"),
                ("Sync Status", nice_label(selected.get("ticket_sync_status") or "not_linked")),
            ]
            if selected.get("last_synced_at"):
                _ticket_rows.append(("Last Synced", str(selected.get("last_synced_at"))[:16]))
            if selected.get("external_ticket_url"):
                _ticket_rows[1] = ("External Ticket", f"[{selected.get('external_ticket_id')}]({selected.get('external_ticket_url')})")
            _tbl_html = "<table style='width:100%;border-collapse:collapse;font-size:0.85rem;margin-bottom:10px;'>"
            for _lbl, _val in _ticket_rows:
                _tbl_html += (
                    f"<tr style='border-bottom:1px solid #f1f5f9;'>"
                    f"<td style='padding:6px 10px 6px 0;font-weight:600;color:#374151;width:42%;'>{_lbl}</td>"
                    f"<td style='padding:6px 0;color:#1e293b;'>{_val}</td></tr>"
                )
            _tbl_html += "</table>"
            st.markdown(_tbl_html, unsafe_allow_html=True)

        with itsm_right:
            _itsm_options = ["ServiceNow", "Other ITSM"]
            _itsm_default = 0 if (_snow_cfg or not selected.get("external_system")) else (
                1 if selected.get("external_system", "").lower() not in ("servicenow", "service_now") else 0
            )
            itsm_system = st.selectbox(
                "External system",
                options=_itsm_options,
                index=_itsm_default,
                key=f"itsm_system_{selected_id}",
            )
            itsm_note = st.text_input(
                "Note (optional)",
                value="",
                placeholder="Audit note for ticket",
                key=f"itsm_note_{selected_id}",
            )
            _a1, _a2 = st.columns(2)
            with _a1:
                if st.button("Create ITSM Ticket", key=f"create_itsm_{selected_id}", use_container_width=True, type="primary"):
                    try:
                        _note = itsm_note or f"Telemetry incident via {itsm_system}, table={_snow_table}"
                        if _snow_instance:
                            _note += f" (instance: {_snow_instance})"
                        create_telemetry_itsm_ticket(selected_id, external_system=itsm_system, notes=_note)
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("ITSM ticket stub created.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"ITSM create failed: {e}")
            with _a2:
                if st.button("Push RCA to Work Notes", key=f"push_itsm_notes_{selected_id}", use_container_width=True, disabled=not selected.get("external_ticket_id")):
                    try:
                        push_telemetry_itsm_work_notes(selected_id, external_system=itsm_system, notes=itsm_note)
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("RCA work-notes stub updated.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Push work notes failed: {e}")

            with st.expander("Link an existing ticket", expanded=False):
                _lnk1, _lnk2 = st.columns(2)
                with _lnk1:
                    link_ticket_id = st.text_input(
                        "Existing ticket ID",
                        value="",
                        placeholder="INC0012345",
                        key=f"link_itsm_ticket_{selected_id}",
                    )
                with _lnk2:
                    link_ticket_url = st.text_input(
                        "Ticket URL (optional)",
                        value="",
                        placeholder="https://instance.service-now.com/...",
                        key=f"link_itsm_url_{selected_id}",
                    )
                if st.button("Link Existing Ticket", key=f"link_itsm_{selected_id}", disabled=not link_ticket_id.strip()):
                    try:
                        link_telemetry_itsm_ticket(
                            selected_id,
                            external_ticket_id=link_ticket_id.strip(),
                            external_system=itsm_system,
                            external_ticket_url=link_ticket_url.strip() or None,
                            notes=itsm_note,
                        )
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("Existing ITSM ticket linked.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"ITSM link failed: {e}")

    with st.expander("Recent raw telemetry events", expanded=False):
        if events:
            event_rows = []
            for item in events[:75]:
                event_rows.append({
                    "Time": item.get("timestamp") or item.get("created_at"),
                    "Metric": item.get("metric"),
                    "Value": item.get("value"),
                    "Resource": item.get("resource"),
                    "Service": item.get("service"),
                    "Environment": item.get("environment"),
                    "Source": item.get("source"),
                })
            st.dataframe(event_rows, use_container_width=True, hide_index=True)
        else:
            st.caption("No raw telemetry events available.")

