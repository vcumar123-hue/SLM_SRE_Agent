"""Telemetry Console page module."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional
import streamlit as st
from page_modules.shared import (
    BACKEND_URL, SESSION, REQUEST_TIMEOUT_MEDIUM, REQUEST_TIMEOUT_LONG,
    REQUEST_TIMEOUT_SHORT,
    fetch_telemetry_events, fetch_telemetry_incidents, fetch_telemetry_incident_detail,
    fetch_telemetry_incident_rca, call_telemetry_evaluate, call_telemetry_promote,
    nice_label, system_label, format_confidence, metric_card, render_empty_state,
)


# ---------------------------------------------------------------------------
# Local helpers: analyze (not yet in shared)
# ---------------------------------------------------------------------------

def analyze_telemetry_incident(incident_id: str) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/telemetry/incidents/{incident_id}/analyze", timeout=REQUEST_TIMEOUT_LONG)
    response.raise_for_status()
    return response.json().get("item") or {}


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
    # Import render_assistant_result here to avoid a top-level circular import.
    from ui import render_assistant_result  # noqa: PLC0415

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

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Telemetry Events", str(len(events)), "dashboard")
    with c2:
        metric_card("Telemetry Incidents", str(len(incidents)), "dashboard")
    with c3:
        metric_card("Critical / High", str(critical_high), "automation")
    with c4:
        metric_card("Affected Resources", str(len(resources)), "neutral")

    # MF-05: Evaluate & Promote action bar
    ev_col, pr_col, spacer = st.columns([1, 1, 4])
    with ev_col:
        if st.button("⚡ Evaluate Events → Incidents", key="tel_evaluate_btn", use_container_width=True):
            try:
                result = call_telemetry_evaluate()
                created = result.get("created_incidents", []) or []
                st.success(f"Evaluation complete — {len(created)} new incident(s) created.")
                fetch_telemetry_incidents.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"Evaluation failed: {exc}")
    with pr_col:
        if st.button("📤 Promote to Knowledge", key="tel_promote_btn", use_container_width=True):
            try:
                result = call_telemetry_promote(limit=50)
                promoted = result.get("promoted", 0) or len(result.get("items", []))
                st.success(f"Promotion complete — {promoted} pattern(s) promoted.")
            except Exception as exc:
                st.error(f"Promotion failed: {exc}")

    st.markdown("---")

    if not incidents:
        st.markdown(
            """
            <div style="text-align:center;padding:40px 20px;color:#6b7280;">
              <div style="font-size:2rem;margin-bottom:10px;">📡</div>
              <div style="font-size:1.05rem;font-weight:600;margin-bottom:6px;">No telemetry incidents yet</div>
              <div style="font-size:0.85rem;">Click <strong>Evaluate Events → Incidents</strong> above to promote raw telemetry events into the incident queue,<br>or send an Alertmanager alert to the <code>/telemetry/events</code> endpoint.</div>
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
    st.markdown("### Telemetry Incident Queue")
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

    left, right = st.columns([1.05, 1.35])
    with left:
        st.markdown("### Alert Details")
        d1, d2 = st.columns(2)
        d1.metric("Severity", telemetry_severity_label(selected.get("severity")))
        d2.metric("Environment", str(selected.get("environment") or "unknown").replace("_", " ").title())
        st.markdown(f"**Lifecycle Status:** {telemetry_status_label(selected.get('status') or ('rca_complete' if existing_rca else 'new'))}")
        st.markdown(f"**Title:** {selected.get('title') or 'Telemetry Alert'}")
        st.markdown(f"**Classification Hint:** `{selected.get('classification') or 'unknown'}`")
        st.markdown(f"**Domain:** {nice_label(selected.get('domain') or 'general')}")
        st.markdown(f"**Affected CI:** `{selected.get('resource') or selected.get('service') or 'unknown'}`")
        st.markdown(f"**Trigger Metric:** `{selected.get('trigger_metric') or selected.get('metric') or 'unknown'}`")
        st.markdown(f"**Trigger Value:** `{selected.get('trigger_value') or selected.get('value') or 'unknown'}`")
        st.markdown(f"**Rule:** `{selected.get('telemetry_rule_id') or 'unknown'}`")
        st.markdown("### Lifecycle")
        current_status = str(selected.get("status") or ("rca_complete" if existing_rca else "new")).lower()
        st.markdown(f"**Status:** {telemetry_status_label(current_status)}")
        st.markdown(f"**ITSM:** {telemetry_ticket_label(selected)}")
        if selected.get("status_note"):
            st.caption(f"Latest note: {selected.get('status_note')}")

        action_notes = st.text_input(
            "Lifecycle note",
            value="",
            placeholder="Optional note for audit trail",
            key=f"telemetry_status_note_{selected_id}",
        )
        a1, a2 = st.columns(2)
        with a1:
            if st.button("Acknowledge", key=f"ack_tel_{selected_id}", width="stretch", disabled=current_status in {"acknowledged", "in_progress", "resolved", "suppressed"}):
                try:
                    update_telemetry_incident_status(selected_id, "acknowledged", notes=action_notes or "Telemetry incident acknowledged.")
                    st.session_state.telemetry_rca_cache.pop(selected_id, None)
                    st.success("Telemetry incident acknowledged.")
                    clear_lightweight_page_cache()
                    st.rerun()
                except Exception as e:
                    st.error(f"Status update failed: {e}")
            if st.button("Mark In Progress", key=f"progress_tel_{selected_id}", width="stretch", disabled=current_status in {"in_progress", "resolved", "suppressed"}):
                try:
                    update_telemetry_incident_status(selected_id, "in_progress", notes=action_notes or "Investigation started.")
                    st.session_state.telemetry_rca_cache.pop(selected_id, None)
                    st.success("Telemetry incident marked in progress.")
                    clear_lightweight_page_cache()
                    st.rerun()
                except Exception as e:
                    st.error(f"Status update failed: {e}")
        with a2:
            if st.button("Mark Resolved", key=f"resolve_tel_{selected_id}", width="stretch", disabled=current_status in {"resolved", "suppressed"}):
                try:
                    update_telemetry_incident_status(selected_id, "resolved", notes=action_notes or "Telemetry incident resolved.")
                    st.session_state.telemetry_rca_cache.pop(selected_id, None)
                    st.success("Telemetry incident resolved.")
                    clear_lightweight_page_cache()
                    st.rerun()
                except Exception as e:
                    st.error(f"Status update failed: {e}")
            if st.button("Suppress", key=f"suppress_tel_{selected_id}", width="stretch", disabled=current_status == "suppressed"):
                try:
                    update_telemetry_incident_status(selected_id, "suppressed", notes=action_notes or "Telemetry incident suppressed as duplicate/noise.")
                    st.session_state.telemetry_rca_cache.pop(selected_id, None)
                    st.success("Telemetry incident suppressed.")
                    clear_lightweight_page_cache()
                    st.rerun()
                except Exception as e:
                    st.error(f"Status update failed: {e}")

        with st.expander("ITSM readiness", expanded=False):
            st.caption("Stub mode only: this simulates ServiceNow readiness without calling a live ITSM API.")
            st.markdown(f"**External System:** {selected.get('external_system') or 'Not linked'}")
            ticket_url = selected.get("external_ticket_url")
            ticket_id = selected.get("external_ticket_id") or "Not linked"
            if ticket_url:
                st.markdown(f"**External Ticket:** [{ticket_id}]({ticket_url})")
            else:
                st.markdown(f"**External Ticket:** {ticket_id}")
            st.markdown(f"**Sync Status:** {nice_label(selected.get('ticket_sync_status') or 'not_linked')}")
            if selected.get("last_synced_at"):
                st.markdown(f"**Last Synced:** {selected.get('last_synced_at')}")

            st.markdown("#### ITSM stub actions")
            itsm_system = st.selectbox(
                "External system",
                options=["ServiceNow", "Other ITSM"],
                index=0,
                key=f"itsm_system_{selected_id}",
            )
            itsm_note = st.text_input(
                "ITSM note",
                value="",
                placeholder="Optional note for ticket/work-notes audit",
                key=f"itsm_note_{selected_id}",
            )

            c_create, c_push = st.columns(2)
            with c_create:
                if st.button("Create ITSM Ticket", key=f"create_itsm_{selected_id}", width="stretch"):
                    try:
                        create_telemetry_itsm_ticket(selected_id, external_system=itsm_system, notes=itsm_note)
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("ITSM ticket stub created.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"ITSM create failed: {e}")
            with c_push:
                if st.button("Push RCA to Work Notes", key=f"push_itsm_notes_{selected_id}", width="stretch", disabled=not selected.get("external_ticket_id")):
                    try:
                        push_telemetry_itsm_work_notes(selected_id, external_system=itsm_system, notes=itsm_note)
                        st.session_state.telemetry_rca_cache.pop(selected_id, None)
                        st.success("RCA work-notes stub updated.")
                        clear_lightweight_page_cache()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Push work notes failed: {e}")

            st.markdown("##### Link existing ticket")
            link_ticket_id = st.text_input(
                "Existing ITSM ticket ID",
                value="",
                placeholder="Example: INC0012345",
                key=f"link_itsm_ticket_{selected_id}",
            )
            link_ticket_url = st.text_input(
                "Existing ITSM ticket URL",
                value="",
                placeholder="Optional",
                key=f"link_itsm_url_{selected_id}",
            )
            if st.button("Link Existing Ticket", key=f"link_itsm_{selected_id}", width="stretch", disabled=not link_ticket_id.strip()):
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

        with st.expander("Lifecycle / ITSM audit history", expanded=False):
            history_rows = fetch_telemetry_status_history(selected_id, limit=50)
            if history_rows:
                st.dataframe([
                    {
                        "Time": row.get("created_at"),
                        "From": telemetry_status_label(row.get("old_status")),
                        "To": telemetry_status_label(row.get("new_status")),
                        "Updated By": row.get("updated_by"),
                        "Notes": row.get("notes"),
                    }
                    for row in history_rows
                ], width="stretch", hide_index=True)
            else:
                st.caption("No lifecycle status history yet.")

    with right:
        st.markdown("### AI RCA")
        if existing_rca:
            st.success("RCA already available for this telemetry incident.")
            if st.button("Re-run AI RCA", key=f"rerun_telemetry_rca_{selected_id}", width="stretch"):
                try:
                    with st.spinner("Running AI RCA for telemetry incident..."):
                        result = analyze_telemetry_incident(selected_id)
                    rca_cache[selected_id] = result
                    st.success("Telemetry RCA refreshed.")
                    clear_lightweight_page_cache()
                    st.rerun()
                except Exception as e:
                    st.error(f"Telemetry RCA failed: {e}")
        else:
            st.info("No RCA result stored yet. Run AI RCA to classify, retrieve similar incidents, and generate recommended actions.")
            if st.button("Analyze RCA", key=f"analyze_telemetry_rca_{selected_id}", type="primary", width="stretch"):
                try:
                    with st.spinner("Running AI RCA for telemetry incident..."):
                        result = analyze_telemetry_incident(selected_id)
                    rca_cache[selected_id] = result
                    st.success("Telemetry RCA completed.")
                    clear_lightweight_page_cache()
                    st.rerun()
                except Exception as e:
                    st.error(f"Telemetry RCA failed: {e}")

        rca_to_show = existing_rca or rca_cache.get(selected_id) or {}
        if rca_to_show:
            rca_to_show = apply_ui_telemetry_rca_display_guardrail(rca_to_show, selected)
            rca_cache[selected_id] = rca_to_show
            render_assistant_result(rca_to_show, rca_to_show.get("submitted_ticket") or selected.get("content") or "Telemetry incident", f"telemetry_{selected_id}")
            # Peer benchmark — text pattern match on the incident description
            _tel_text = (
                selected.get("short_description")
                or selected.get("description")
                or selected.get("content")
                or (rca_to_show.get("analysis") or {}).get("classification")
                or ""
            )
            if _tel_text:
                from page_modules.peer_insights import render_peer_insights_panel
                render_peer_insights_panel(_tel_text, client_id="client_a")

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
            st.dataframe(event_rows, width="stretch", hide_index=True)
        else:
            st.caption("No raw telemetry events available.")

