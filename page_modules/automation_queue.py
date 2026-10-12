"""Automation Queue page module."""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
import streamlit as st
import requests
from page_modules.shared import (
    BACKEND_URL, SESSION, REQUEST_TIMEOUT_MEDIUM,
    nice_label, format_confidence, metric_card,
    render_automation_pipeline_steps, render_compact_bullet_list,
    render_resolution_playbook,
    refresh_automation_queue_request, fetch_all_queue_entries,
)

def _import_ui_symbols():
    """Lazy import to break circular dependency: ui imports automation_queue, automation_queue imports ui."""
    from ui import (  # noqa: PLC0415
        get_latest_analysis_result,
        get_result_analysis,
        get_result_classification,
        build_draft_resolution_and_playbook,
        build_engineer_runbook,
        get_automation_runtime_key,
        build_candidate_payload_from_runbook,
        lines_to_list,
        create_automation_candidate,
        list_automation_candidates,
        update_automation_candidate,
        record_automation_candidate_test_result,
        approve_automation_candidate_request,
        enable_automation_candidate_request,
        generate_automation_candidate_playbook,
        save_automation_candidate_playbook,
        candidate_lifecycle_flags,
        lifecycle_badges,
        get_approved_automation_mapping,
        find_candidate_by_id,
        matching_candidate_for_incident,
        next_improvement_playbook_name,
        build_playbook_improvement_candidate_payload,
        queue_automation,
        approve_automation_request,
        execute_automation_request,
        run_pre_execution_safety_gate,
        submit_execution_feedback_request,
        get_automation_confidence_summary_request,
        render_execution_evidence,
        render_learning_autonomy_panel,
        _confidence_summary_payload,
        _json_safe,
        system_label,
        UI_RUNTIME_DIR,
        learn_automation_mapping,
    )
    return locals()


def _render_rollback_panel(execution_id: str, queue_id: str) -> None:
    """Inline rollback panel — shown when the page is opened via the Slack one-click rollback link."""
    st.warning(
        f"**Rollback requested** via alert link  \n"
        f"Execution ID: `{execution_id}`  |  Queue ID: `{queue_id or '—'}`",
        icon="⚠️",
    )
    st.markdown("This will record a rollback event against the execution and mark the queue entry as rolled back.")

    col_confirm, col_cancel = st.columns([1, 3])
    with col_confirm:
        if st.button("Confirm Rollback", type="primary", key="rollback_confirm_btn"):
            try:
                params: Dict[str, Any] = {"execution_id": execution_id}
                if queue_id:
                    params["queue_id"] = queue_id
                params["rolled_back_by"] = "engineer_via_slack_link"
                resp = SESSION.post(f"{BACKEND_URL}/automation/rollback", params=params, timeout=REQUEST_TIMEOUT_MEDIUM)
                resp.raise_for_status()
                result = resp.json()
                st.success(f"Rollback recorded at `{result.get('rolled_back_at', '?')}`. Status: `{result.get('rollback_status')}`")
                st.session_state["_rollback_done"] = execution_id
            except Exception as exc:
                st.error(f"Rollback failed: {exc}")
    with col_cancel:
        if st.button("Cancel", key="rollback_cancel_btn"):
            st.info("Rollback cancelled.")

    st.divider()


def render_automation_queue_page():
    # Resolve circular-import symbols at call time (ui imports this module at load time)
    _ui = _import_ui_symbols()
    get_latest_analysis_result = _ui["get_latest_analysis_result"]
    get_result_analysis = _ui["get_result_analysis"]
    get_result_classification = _ui["get_result_classification"]
    build_draft_resolution_and_playbook = _ui["build_draft_resolution_and_playbook"]
    build_engineer_runbook = _ui["build_engineer_runbook"]
    get_automation_runtime_key = _ui["get_automation_runtime_key"]
    build_candidate_payload_from_runbook = _ui["build_candidate_payload_from_runbook"]
    lines_to_list = _ui["lines_to_list"]
    create_automation_candidate = _ui["create_automation_candidate"]
    list_automation_candidates = _ui["list_automation_candidates"]
    update_automation_candidate = _ui["update_automation_candidate"]
    record_automation_candidate_test_result = _ui["record_automation_candidate_test_result"]
    approve_automation_candidate_request = _ui["approve_automation_candidate_request"]
    enable_automation_candidate_request = _ui["enable_automation_candidate_request"]
    generate_automation_candidate_playbook = _ui["generate_automation_candidate_playbook"]
    save_automation_candidate_playbook = _ui["save_automation_candidate_playbook"]
    candidate_lifecycle_flags = _ui["candidate_lifecycle_flags"]
    lifecycle_badges = _ui["lifecycle_badges"]
    get_approved_automation_mapping = _ui["get_approved_automation_mapping"]
    find_candidate_by_id = _ui["find_candidate_by_id"]
    matching_candidate_for_incident = _ui["matching_candidate_for_incident"]
    next_improvement_playbook_name = _ui["next_improvement_playbook_name"]
    build_playbook_improvement_candidate_payload = _ui["build_playbook_improvement_candidate_payload"]
    queue_automation = _ui["queue_automation"]
    approve_automation_request = _ui["approve_automation_request"]
    execute_automation_request = _ui["execute_automation_request"]
    run_pre_execution_safety_gate = _ui["run_pre_execution_safety_gate"]
    submit_execution_feedback_request = _ui["submit_execution_feedback_request"]
    get_automation_confidence_summary_request = _ui["get_automation_confidence_summary_request"]
    render_execution_evidence = _ui["render_execution_evidence"]
    render_learning_autonomy_panel = _ui["render_learning_autonomy_panel"]
    _confidence_summary_payload = _ui["_confidence_summary_payload"]
    _json_safe = _ui["_json_safe"]
    system_label = _ui["system_label"]
    UI_RUNTIME_DIR = _ui["UI_RUNTIME_DIR"]
    learn_automation_mapping = _ui["learn_automation_mapping"]

    # Handle one-click rollback deep-link from Slack/webhook alerts
    qp = st.query_params
    if qp.get("action") == "rollback" and qp.get("execution_id"):
        _render_rollback_panel(
            execution_id=str(qp.get("execution_id", "")),
            queue_id=str(qp.get("queue_id", "")),
        )

    st.subheader("Automation Queue")
    latest, latest_ticket = get_latest_analysis_result()
    if not latest:
        st.markdown(
            """
            <div style="text-align:center;padding:40px 20px;color:#6b7280;">
              <div style="font-size:2rem;margin-bottom:12px;">⚙️</div>
              <div style="font-size:1.1rem;font-weight:600;margin-bottom:6px;">No incident analysed yet</div>
              <div style="font-size:0.88rem;">Run an incident analysis in the Incident Console first, then return here to manage automation.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    analysis = get_result_analysis(latest)
    classification = get_result_classification(latest)
    linked_resolution, linked_playbook = build_draft_resolution_and_playbook(latest, latest_ticket or "")
    runbook = build_engineer_runbook(latest, latest_ticket or "")
    incident_id = str(latest.get("request_id") or linked_resolution.get("linked_candidate_id") or f"adhoc_{classification}")
    runtime_key = get_automation_runtime_key(latest, classification)
    runtime = st.session_state.automation_runtime.get(runtime_key, {})
    mapping_runtime = st.session_state.automation_mapping_runtime.get(runtime_key, {})

    queue_entry = runtime.get("queue_entry") or {}
    approval_result = runtime.get("approval_result") or {}
    execution_result = runtime.get("execution_result") or {}
    queue_id = queue_entry.get("queue_id")
    queue_status = str(queue_entry.get("status", "")).lower() or "draft"
    autonomous_eligible = False  # resolved properly later after eligibility check

    # Derive stepper status from the richest available signal so Executed and
    # Feedback steps light up green even when queue_entry.status lags behind.
    if runtime.get("backend_feedback_result") or runtime.get("execution_feedback"):
        _stepper_status = "feedback_submitted"
    elif execution_result:
        _stepper_status = "executed"
    else:
        _stepper_status = queue_status

    # Visual lifecycle pipeline
    render_automation_pipeline_steps(_stepper_status)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Incident Class", nice_label(classification), "automation", uniform_value_sizing=True)
    with c2:
        metric_card("Runbook", "Ready", "automation", uniform_value_sizing=True)
    with c3:
        metric_card("Queue Status", nice_label(queue_status) if queue_status != "draft" else "Not Queued", "automation", uniform_value_sizing=True)
    with c4:
        metric_card("Risk Tier", nice_label(linked_playbook.get("risk_tier", "medium")), "automation", uniform_value_sizing=True)

    # UX-06: Queue filter tabs with badge counts
    all_entries = fetch_all_queue_entries(limit=200)
    _active_statuses = {"queued", "pending_approval", "approved"}
    _needs_approval_statuses = {"pending_approval"}
    _done_statuses = {"executed", "closed_remediated", "execution_failed", "cancelled", "skipped"}
    n_active = sum(1 for e in all_entries if str(e.get("status") or "").lower() in _active_statuses)
    n_needs_approval = sum(1 for e in all_entries if str(e.get("status") or "").lower() in _needs_approval_statuses)
    n_done = sum(1 for e in all_entries if str(e.get("status") or "").lower() in _done_statuses)

    tab_labels = [
        f"All ({len(all_entries)})",
        f"Active ({n_active})",
        f"Needs Approval ({n_needs_approval})",
        f"Completed ({n_done})",
    ]
    queue_tabs = st.tabs(tab_labels)

    def _render_queue_table(entries: list) -> None:
        if not entries:
            st.markdown(
                '<div style="text-align:center;padding:20px;color:#6b7280;font-size:0.88rem;">No queue entries in this filter.</div>',
                unsafe_allow_html=True,
            )
            return
        rows = []
        for e in entries[:50]:
            drift_type = str(e.get("drift_type") or "").strip()
            drift_eligible = bool(drift_type and drift_type not in ("unknown_or_not_detected", "none", ""))
            drift_icon = "🛡️ Active" if drift_eligible else "—"
            rows.append({
                "Queue ID": e.get("queue_id", ""),
                "Incident ID": e.get("incident_id", ""),
                "Classification": nice_label(e.get("classification", "")),
                "Status": nice_label(e.get("status", "")),
                "Safety Gate": drift_icon,
                "Requested By": e.get("requested_by", ""),
                "Queued At": (e.get("queued_at") or "")[:16],
            })
        st.dataframe(rows, use_container_width=True, hide_index=True,
                     column_config={"Safety Gate": st.column_config.TextColumn(width="small")})

    with queue_tabs[0]:
        _render_queue_table(all_entries)
    with queue_tabs[1]:
        _render_queue_table([e for e in all_entries if str(e.get("status") or "").lower() in _active_statuses])
    with queue_tabs[2]:
        _render_queue_table([e for e in all_entries if str(e.get("status") or "").lower() in _needs_approval_statuses])
    with queue_tabs[3]:
        _render_queue_table([e for e in all_entries if str(e.get("status") or "").lower() in _done_statuses])

    st.markdown("### Current Incident Automation Package")
    st.caption("Draft package is generated from the latest Incident Console analysis. It can be queued even when no backend playbook is formally linked yet; execution remains approval-gated.")
    st.write(f"**Incident ID:** `{incident_id}`")
    st.write(f"**Classification:** {nice_label(classification)}")
    st.write(f"**Approval Mode:** {nice_label(linked_playbook.get('approval_mode', 'manual_approval_required'))}")
    st.write("**Execution Governance:** Human-in-the-loop")
    _auto_status = "Eligible" if autonomous_eligible else "Not eligible"
    st.write(f"**Autonomous Status:** {_auto_status}")
    st.write(f"**Playbook Source:** {linked_playbook.get('source', 'linked_playbook')}")

    playbook_steps = latest.get("resolution_playbook") or []
    safety = runbook.get("safety", "Validate evidence and obtain approval before executing any state-changing remediation.")
    if playbook_steps:
        render_resolution_playbook(
            playbook_steps,
            safety_note=safety,
            title="Review Resolution Playbook Before Queueing",
        )
    else:
        with st.expander("Review runbook before queueing", expanded=True):
            st.write(f"**Objective:** {runbook['objective']}")
            st.warning(f"Safety / Approval Guardrail: {safety}")
            st.info(
                "No structured playbook available yet for this classification. "
                "Generate one via **Candidate Registry → Generate Playbook**, or trigger "
                "an LLM-assisted playbook via the fallback path before queueing.",
                icon="ℹ️",
            )

    # ── Error Tolerance: CI criticality check before execution ──────────────
    try:
        _tol_resp = SESSION.get(f"{BACKEND_URL}/tolerance/{classification}", timeout=5)
        if _tol_resp.status_code == 200:
            _tol = _tol_resp.json()
            _policy = _tol.get("policy", {})
            _tier = _tol.get("tier", "unknown")
            _tier_colors = {
                "prod-standalone": "#dc2626", "prod-redundant": "#ea580c",
                "prod-ha": "#d97706", "non-prod": "#16a34a",
                "dr-standby": "#2563eb", "unknown": "#6b7280",
            }
            _col = _tier_colors.get(_tier, "#6b7280")
            _tol_min = _policy.get("tolerance_minutes", "—")
            _auto = "✅ Approved" if _policy.get("auto_remediate") else "❌ Manual only"
            _appr = "⚠️ Required" if _policy.get("approval_required") else "Not required"
            _esc = _policy.get("escalation", "—")
            st.markdown(
                f'<div style="background:#f8fafc;border:1px solid #e2e8f0;'
                f'border-left:4px solid {_col};border-radius:6px;padding:10px 14px;margin:8px 0;">'
                f'<div style="font-size:0.75rem;font-weight:700;color:{_col};margin-bottom:4px;">'
                f'🛡️ CI ERROR TOLERANCE — '
                f'<span style="background:{_col};color:#fff;padding:2px 8px;border-radius:4px;">{_tier.upper()}</span>'
                f'</div>'
                f'<div style="display:flex;gap:20px;flex-wrap:wrap;font-size:0.78rem;color:#475569;">'
                f'<span><strong>Tolerance:</strong> {_tol_min} min</span>'
                f'<span><strong>Auto-remediate:</strong> {_auto}</span>'
                f'<span><strong>Approval:</strong> {_appr}</span>'
                f'<span><strong>Escalation:</strong> {_esc}</span>'
                f'</div></div>',
                unsafe_allow_html=True,
            )
    except Exception:
        pass

    execution_mode = st.selectbox(
        "Execution Mode",
        ["simulation", "live"],
        index=0 if st.session_state.automation_execution_mode == "simulation" else 1,
        key="automation_execution_mode_select",
    )
    st.session_state.automation_execution_mode = execution_mode

    st.info(
        "Execution governance: Human-in-the-loop. "
        "An engineer must create/approve the queue before execution. "
        "Live execution additionally requires autonomous eligibility."
    )

    # Single guided automation state machine.  The Incident Console should only route
    # the current runbook here; Automation Queue owns candidate, registry, queue,
    # approval, and execution control.
    try:
        candidates_resp = list_automation_candidates(classification=classification)
        queue_candidates = candidates_resp.get("items", []) if isinstance(candidates_resp, dict) else []
    except Exception as e:
        queue_candidates = []
        st.warning(f"Candidate state unavailable: {e}")

    registry_mapping = get_approved_automation_mapping(classification)
    registry_candidate_id = str(registry_mapping.get("source_candidate_id") or "").strip() if registry_mapping else ""
    registry_candidate = find_candidate_by_id(queue_candidates, registry_candidate_id)
    candidate_for_incident = matching_candidate_for_incident(queue_candidates, incident_id)
    enabled_candidate = registry_candidate or next((c for c in queue_candidates if candidate_lifecycle_flags(c).get("enabled")), {})

    # Step 3E: close the feedback → playbook-update loop.  If feedback created
    # an improvement candidate, keep the currently approved registry mapping as
    # production-active for execution, but also surface the v2/v3 candidate as
    # the active pending-review item in Automation Queue until it is enabled.
    runtime_improvement_candidate = runtime.get("playbook_improvement_candidate", {}) if isinstance(runtime.get("playbook_improvement_candidate"), dict) else {}
    runtime_improvement_candidate_id = str(runtime_improvement_candidate.get("candidate_id") or runtime.get("active_improvement_candidate_id") or "").strip()
    improvement_candidate = find_candidate_by_id(queue_candidates, runtime_improvement_candidate_id) or runtime_improvement_candidate or {}
    if not improvement_candidate:
        improvement_candidate = next((
            c for c in queue_candidates
            if str(c.get("source") or "").lower() == "execution_feedback"
            and str(c.get("classification") or "").strip().lower() == system_label(classification).lower()
            and not candidate_lifecycle_flags(c).get("enabled")
        ), {})
    improvement_flags = candidate_lifecycle_flags(improvement_candidate) if improvement_candidate else {}
    pending_improvement_candidate = bool(improvement_candidate) and not improvement_flags.get("enabled")

    active_candidate = registry_candidate or candidate_for_incident or enabled_candidate or {}
    active_flags = candidate_lifecycle_flags(active_candidate) if active_candidate else {}
    if registry_mapping:
        active_flags["enabled"] = True

    queue_status = str(queue_entry.get("status", "") or "").lower()
    queue_created = bool(queue_id)
    queue_approved = bool(approval_result) or queue_status in {
        "approved",
        "completed",
        "completed_live",
        "completed_simulation",
        "closed_remediated",
        "executed",
    }
    execution_done = bool(execution_result)

    # Step 2C: safety gate is now backed by /automation/pre_execution_check.
    # The backend is still advisory-only in this step; approval is unlocked when
    # the gate response says it is safe to approve. Live execution enforcement
    # remains unchanged until the next backend enforcement step.
    safety_gate_result = runtime.get("safety_gate_result", {}) if isinstance(runtime.get("safety_gate_result"), dict) else {}
    safety_gate_status = str(safety_gate_result.get("gate_status") or "not_run").lower()
    safety_gate_allows_approval = bool(safety_gate_result.get("safe_to_approve")) or safety_gate_status in {"passed", "warning"}
    _blocking_checks = safety_gate_result.get("blocking_checks") or []
    # WARNING with no hard blocking checks + human approval already covers risk — allow execution.
    # PASSED always allows; BLOCKED never allows.
    safety_gate_passed = (safety_gate_status == "passed") or (
        safety_gate_status == "warning"
        and not _blocking_checks
        and bool(safety_gate_result.get("safe_to_approve"))
    )
    safety_gate_completed = safety_gate_status in {"passed", "warning", "blocked"}

    # Autonomous eligibility check — determines whether live execution is permitted
    _eligibility_cache_key = f"_autonomous_eligibility_{runtime_key}"
    if _eligibility_cache_key not in st.session_state or st.session_state.get(f"_eligibility_refresh_{runtime_key}"):
        try:
            _elig_resp = SESSION.post(
                f"{BACKEND_URL}/automation/autonomous/eligible_mapping",
                json={"runtime_key": runtime_key, "classification": classification},
                timeout=REQUEST_TIMEOUT_MEDIUM,
            )
            _elig_resp.raise_for_status()
            st.session_state[_eligibility_cache_key] = _elig_resp.json()
        except Exception:
            st.session_state[_eligibility_cache_key] = {"autonomous_eligible": False, "eligibility_reasons": []}
        st.session_state.pop(f"_eligibility_refresh_{runtime_key}", None)
    eligibility = st.session_state.get(_eligibility_cache_key, {})
    autonomous_eligible = bool(eligibility.get("autonomous_eligible", False))
    eligibility_reasons = eligibility.get("eligibility_reasons") or []

    create_candidate_disabled = bool(candidate_for_incident) or bool(registry_mapping) or bool(enabled_candidate)
    queue_disabled = (not active_flags.get("enabled")) or queue_created
    safety_gate_disabled = (not queue_created) or safety_gate_completed or execution_done
    approve_disabled = (not queue_created) or (not safety_gate_allows_approval) or queue_approved or execution_done
    # Live execution additionally requires autonomous eligibility;
    # simulation mode is always available after approval + passed safety gate.
    live_mode = execution_mode == "live"
    # autonomous_eligible gates fully-autonomous (no-human-in-loop) runs only;
    # human-approved execution is always permitted once queue is approved + gate passed.
    execute_disabled = (
        (not queue_created) or (not queue_approved) or (not safety_gate_passed) or execution_done
    )

    st.markdown("### Guided Automation Control")
    st.caption(
        "Use this single control lane for the current incident: create candidate only if needed, "
        "enable registry, create queue, run safety gate, approve queue, then execute. Duplicate candidates are blocked by default."
    )

    step_cols = st.columns(7)
    with step_cols[0]:
        st.markdown("✅ Candidate" if active_candidate else "⬜ Candidate")
    with step_cols[1]:
        st.markdown("✅ Registry Enabled" if active_flags.get("enabled") else "⬜ Registry Enabled")
    with step_cols[2]:
        st.markdown("✅ Queue Created" if queue_created else "⬜ Queue Created")
    with step_cols[3]:
        st.markdown("✅ Safety Gate" if safety_gate_allows_approval else "⬜ Safety Gate")
    with step_cols[4]:
        st.markdown("✅ Human Approval" if queue_approved else "⬜ Human Approval")
    with step_cols[5]:
        st.markdown("✅ Executed" if execution_done else "⬜ Executed")
    with step_cols[6]:
        if autonomous_eligible:
            st.markdown("✅ Autonomous")
        else:
            st.markdown("⬜ Autonomous")

    if live_mode and not autonomous_eligible:
        reasons_txt = "; ".join(str(r) for r in eligibility_reasons) if eligibility_reasons else "not yet eligible"
        st.warning(
            f"**Live execution blocked:** this classification is not yet autonomous-eligible. "
            f"Reason: {reasons_txt}. Switch to **simulation** mode or fulfil eligibility requirements first.",
            icon="🔒",
        )
    elif autonomous_eligible:
        st.success("Autonomous execution eligible for this classification.", icon="✅")

    st.markdown("#### Pre-Execution Safety Gate")
    st.caption(
        "Calls the backend safety-gate endpoint before human approval. "
        "Step 2C is advisory-only: it records drift/SLO/change/rollback/blast-radius status, "
        "but live execution blocking will be added in the next backend enforcement step."
    )
    safety_checks = safety_gate_result.get("checks", {}) if isinstance(safety_gate_result.get("checks"), dict) else {}

    def _gate_metric(name: str, fallback_key: str, default: str = "Pending") -> str:
        detail = safety_checks.get(name) if isinstance(safety_checks, dict) else None
        if isinstance(detail, dict):
            status = str(detail.get("status") or default).upper()
            if name == "blast_radius":
                severity = str(detail.get("severity") or "").strip()
                return f"{status} / {severity.upper()}" if severity else status
            return status
        return str(safety_gate_result.get(fallback_key, default))

    drift_status = _gate_metric("infrastructure_drift", "infrastructure_drift")
    error_budget_status = _gate_metric("error_budget_slo", "error_budget_slo")
    concurrent_status = _gate_metric("concurrent_activity", "concurrent_activity")
    change_status = _gate_metric("change_window", "change_window")
    rollback_status = _gate_metric("rollback_readiness", "rollback_readiness")
    blast_radius_status = _gate_metric("blast_radius", "blast_radius")
    gate_label = safety_gate_status.upper().replace("_", " ") if safety_gate_completed else "NOT RUN"
    gate_cols = st.columns(6)
    with gate_cols[0]:
        st.metric("Infrastructure Drift", drift_status)
    with gate_cols[1]:
        st.metric("Error Budget / SLO", error_budget_status)
    with gate_cols[2]:
        st.metric("Concurrent Activity", concurrent_status)
    with gate_cols[3]:
        st.metric("Change Window", change_status)
    with gate_cols[4]:
        st.metric("Rollback Readiness", rollback_status)
    with gate_cols[5]:
        st.metric("Blast Radius", blast_radius_status)
    if safety_gate_passed:
        st.success("Execution Gate: PASSED. Human approval is now enabled for this queue.")
    elif safety_gate_status == "warning":
        st.warning("Execution Gate: WARNING. Human approval is enabled, but review the warning details before proceeding.")
    elif safety_gate_status == "blocked":
        st.error("Execution Gate: BLOCKED. Human approval is disabled until the blocking condition is resolved.")
    else:
        st.info(f"Execution Gate: {gate_label}. Create a queue, then run the safety gate before human approval.")

    if safety_gate_result:
        with st.expander("Safety gate evidence", expanded=False):
            st.json(safety_gate_result)

    if registry_mapping:
        st.success(
            "Approved automation selected from registry: "
            f"{registry_mapping.get('action', 'approved_action')} → "
            f"{registry_mapping.get('playbook', 'approved_playbook')} "
            f"on {registry_mapping.get('target_host') or registry_mapping.get('target_group') or 'approved target'}"
        )
        if active_candidate:
            st.caption(
                f"Registry source candidate: {active_candidate.get('candidate_id', registry_candidate_id or 'existing')} | "
                f"Status: {active_candidate.get('status', 'enabled')}"
            )
    elif active_candidate:
        st.info(
            f"Active candidate: {active_candidate.get('candidate_id', 'existing')} | "
            f"Status: {active_candidate.get('status', 'draft')} | "
            f"Playbook: {active_candidate.get('suggested_playbook') or 'not linked yet'}"
        )
    else:
        st.warning("No reusable automation candidate exists yet for this incident. Create one before queueing execution.")

    if pending_improvement_candidate:
        lineage = improvement_candidate.get("draft_runbook", {}).get("lineage", {}) if isinstance(improvement_candidate.get("draft_runbook"), dict) else {}
        current_approved_playbook = lineage.get("current_playbook") or (registry_mapping or {}).get("playbook") or "unknown"
        proposed_playbook = improvement_candidate.get("suggested_playbook") or lineage.get("improvement_candidate_playbook") or "pending"
        st.markdown("#### Playbook Update Pending Review")
        st.warning(
            "A feedback-driven playbook improvement candidate is now active for review. "
            "The current approved playbook remains in production until this candidate is saved, tested, approved, and registry-enabled."
        )
        loop_cols = st.columns(4)
        with loop_cols[0]:
            st.write(f"**Existing automation:** `{improvement_candidate.get('suggested_action') or (registry_mapping or {}).get('action') or 'unknown'}`")
        with loop_cols[1]:
            st.write(f"**Current approved playbook:** `{current_approved_playbook}`")
        with loop_cols[2]:
            st.write(f"**Proposed updated playbook:** `{proposed_playbook}`")
        with loop_cols[3]:
            st.write("**Source:** `execution_feedback`")
        st.caption(
            "Next action: open Automation Development / Candidate Backlog below, generate/review the v2 playbook, save it, mark tested, approve candidate, then enable registry mapping. "
            "Only after Enable Registry Mapping will future executions switch to the updated playbook."
        )

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        if st.button(
            "Create Candidate",
            width="stretch",
            key=f"create_candidate_auto_{runtime_key}",
            disabled=create_candidate_disabled,
            help="Disabled once a candidate already exists for this incident or a registry mapping is already enabled for this classification.",
        ):
            try:
                resp = create_automation_candidate(build_candidate_payload_from_runbook(
                    incident_id,
                    classification,
                    runbook,
                    linked_resolution,
                    linked_playbook,
                    "Created from Automation Queue guided control lane.",
                ))
                candidate = resp.get("candidate", {})
                runtime["candidate_entry"] = candidate
                runtime["candidate_result"] = resp
                st.session_state.automation_runtime[runtime_key] = runtime
                st.success(f"Automation candidate created: {candidate.get('candidate_id', 'created')}")
                st.rerun()
            except Exception as e:
                st.error(f"Candidate creation failed: {e}")
    with col2:
        if st.button(
            "Create Queue",
            width="stretch",
            key=f"queue_auto_{runtime_key}",
            disabled=queue_disabled,
            help="Enabled only after the candidate is approved and registry mapping is enabled. Disabled after queue creation.",
        ):
            try:
                resp = queue_automation({
                    "incident_id": incident_id,
                    "classification": classification,
                    "linked_resolution": linked_resolution,
                    "linked_playbook": linked_playbook,
                    "requested_by": "Vijay",
                })
                runtime["queue_entry"] = resp.get("queue_entry", {})
                runtime["queue_result"] = resp
                runtime.pop("safety_gate_result", None)
                runtime.pop("approval_result", None)
                runtime.pop("execution_result", None)
                st.session_state.automation_runtime[runtime_key] = runtime
                st.success("Execution queue created.")
                st.rerun()
            except Exception as e:
                st.error(f"Queue failed: {e}")
    with col3:
        if st.button(
            "Run Safety Gate",
            width="stretch",
            key=f"safety_gate_auto_{runtime_key}",
            disabled=safety_gate_disabled,
            help="Enabled after queue creation. Calls the backend pre-execution safety-gate endpoint.",
        ):
            try:
                gate_payload = {
                    "queue_id": queue_id,
                    "incident_id": incident_id,
                    "classification": classification,
                    "playbook": (registry_mapping or {}).get("playbook")
                        or linked_playbook.get("playbook")
                        or linked_playbook.get("filename")
                        or active_candidate.get("suggested_playbook")
                        or "",
                    "target_host": (registry_mapping or {}).get("target_host")
                        or linked_playbook.get("target_host")
                        or active_candidate.get("target_host")
                        or "",
                    "risk_tier": (registry_mapping or {}).get("risk_tier")
                        or linked_playbook.get("risk_tier")
                        or active_candidate.get("risk_tier")
                        or "medium",
                    "execution_mode": execution_mode,
                    "requested_by": "Vijay",
                }
                resp = run_pre_execution_safety_gate(gate_payload)
                runtime["safety_gate_result"] = resp
                st.session_state.automation_runtime[runtime_key] = runtime
                gate_status_msg = str(resp.get("gate_status") or "completed").upper()
                # Show live refresh summary
                refresh = resp.get("live_refresh") or {}
                snap_step = next((s for s in (refresh.get("steps") or []) if s.get("step") == "snapshot"), None)
                snap_id = (snap_step or {}).get("snapshot_id") or ""
                if snap_id:
                    st.toast(f"📸 Live snapshot captured: {snap_id}", icon="📸")
                sim_step = next((s for s in (refresh.get("steps") or []) if s.get("step") == "simulation"), None)
                if sim_step and sim_step.get("status") == "ok":
                    st.toast("✅ Dry-run simulation completed", icon="✅")
                elif sim_step and sim_step.get("status") == "warn":
                    st.toast("⚠️ Dry-run simulation advisory warning", icon="⚠️")
                if resp.get("safe_to_approve"):
                    st.success(f"Pre-execution safety gate completed: {gate_status_msg}.")
                else:
                    st.error(f"Pre-execution safety gate completed: {gate_status_msg}. Approval remains blocked.")
                st.rerun()
            except Exception as e:
                st.error(f"Safety gate failed: {e}")
    with col4:
        override_reason_key = f"override_reason_{runtime_key}"
        override_reason = ""
        if safety_gate_status == "warning":
            override_reason = st.text_input(
                "Warning override reason",
                key=override_reason_key,
                placeholder="Why is it acceptable to proceed despite the Safety Gate warnings?",
                help="Required to approve while the Safety Gate reports warnings. Recorded with your identity and the warnings you accepted.",
            ).strip()
        if st.button(
            "Approve Queue",
            width="stretch",
            key=f"approve_auto_{runtime_key}",
            disabled=approve_disabled or (safety_gate_status == "warning" and not override_reason),
            help="Enabled only after queue exists and the safety gate has passed. Disabled after approval.",
        ):
            try:
                resp = approve_automation_request(
                    queue_id,
                    approved_by="Vijay",
                    override_gate_warnings=(safety_gate_status == "warning"),
                    override_reason=override_reason,
                )
                runtime["approval_result"] = resp
                runtime["queue_entry"] = resp.get("queue_entry", runtime.get("queue_entry", {}))
                st.session_state.automation_runtime[runtime_key] = runtime
                st.success("Automation queue approved by human-in-the-loop.")
                st.rerun()
            except Exception as e:
                st.error(f"Approve failed: {e}")
    with col5:
        if st.button(
            "Execute",
            width="stretch",
            key=f"execute_auto_{runtime_key}",
            disabled=execute_disabled,
            help="Enabled only after queue approval and a PASSED safety gate. Disabled after execution is submitted.",
        ):
            try:
                # ── Drift Management: capture snapshot before execution ──────
                _drift_ci = str(latest.get("cmdb_ci") or latest.get("ci_name") or classification or "unknown")
                _drift_mode = "lab" if _drift_ci in ("linux-lab-01", "linux-lab-02", "network-lab-01") else "dry"
                _snap_id = None
                try:
                    _snap_resp = SESSION.post(
                        f"{BACKEND_URL}/drift/snapshot",
                        json={"ci_name": _drift_ci, "mode": _drift_mode,
                              "container": _drift_ci if _drift_mode == "lab" else None,
                              "notes": f"Pre-execution snapshot for queue {queue_id}"},
                        timeout=15,
                    )
                    if _snap_resp.status_code == 200:
                        _snap_id = _snap_resp.json().get("snapshot_id")
                        st.info(f"📸 Snapshot captured: `{_snap_id}`")
                except Exception:
                    pass  # snapshot failure must not block execution

                # ── Execute the change ───────────────────────────────────────
                resp = execute_automation_request(
                    queue_id,
                    execution_mode=execution_mode,
                    executed_by="Vijay",
                    override_gate_warnings=(safety_gate_status == "warning"),
                    override_reason=(
                        st.session_state.get(f"override_reason_{runtime_key}", "").strip()
                        or str((((runtime.get("queue_entry") or {}).get("approval")) or {}).get("override_reason") or "")
                        or "Reconfirmed at execution from the Automation Queue page."
                    ),
                )
                runtime["execution_result"] = resp
                runtime["drift_snapshot_id"] = _snap_id
                st.session_state.automation_runtime[runtime_key] = runtime

                # ── Drift Management: verify + destroy snapshot after execution ─
                if _snap_id:
                    try:
                        _ver = SESSION.post(f"{BACKEND_URL}/drift/verify",
                                            json={"snapshot_id": _snap_id}, timeout=30)
                        _ver_result = _ver.json().get("verify_result", "unknown") if _ver.status_code == 200 else "unknown"
                        if _ver_result == "pass":
                            SESSION.post(f"{BACKEND_URL}/drift/destroy",
                                         json={"snapshot_id": _snap_id, "write_audit": True}, timeout=10)
                            st.success(f"✅ Post-execution verification passed — snapshot destroyed, audit written.")
                        else:
                            st.warning(f"⚠️ Post-execution verification: **{_ver_result}** — snapshot `{_snap_id}` retained for inspection.")
                    except Exception:
                        pass

                st.success("Execution submitted.")
                st.rerun()
            except Exception as e:
                st.error(f"Execution failed: {e}")

    if create_candidate_disabled and not queue_created and not active_flags.get("enabled"):
        st.caption("Candidate already exists but is not registry-enabled yet. Complete Generate → Save → Test → Approve → Enable inside Candidate Backlog, then Create Queue will unlock.")
    elif active_flags.get("enabled") and not queue_created:
        st.success("Registry mapping is enabled. Next valid action: Create Queue.")
    elif queue_created and not safety_gate_completed:
        st.success("Queue created. Next valid action: Run Safety Gate.")
    elif safety_gate_allows_approval and not queue_approved:
        st.success("Safety gate completed. Next valid action: Approve Queue with human-in-the-loop.")
    elif safety_gate_status == "blocked" and not queue_approved:
        st.error("Safety gate blocked this queue. Resolve the issue before human approval.")
    elif queue_approved and not safety_gate_passed and not execution_done:
        st.error("Queue is approved but the safety gate is BLOCKED. Resolve all blocking checks before executing.")
    elif queue_approved and safety_gate_passed and safety_gate_status == "warning" and not execution_done:
        st.warning("Queue approved. Safety gate has warnings — no hard blockers. Human approval covers the risk. Next valid action: Execute.")
    elif queue_approved and safety_gate_passed and not execution_done:
        st.success("Queue approved and safety gate passed. Next valid action: Execute.")


    with st.expander("Automation Development / Candidate Backlog", expanded=bool(pending_improvement_candidate)):
        st.caption("Use this stage to convert a draft runbook into a tested, approved, reusable automation registry mapping.")
        try:
            candidates_resp = list_automation_candidates(classification=classification)
            candidates = candidates_resp.get("items", []) if isinstance(candidates_resp, dict) else []
        except Exception as e:
            candidates = []
            st.warning(f"Candidate backlog unavailable: {e}")

        if not candidates:
            st.info("No automation candidates yet for this classification.")
        else:
            if pending_improvement_candidate:
                pending_id = str(improvement_candidate.get("candidate_id") or "").strip()
                candidates = sorted(
                    candidates,
                    key=lambda c: 0 if str(c.get("candidate_id") or "").strip() == pending_id else 1,
                )
            for item in candidates[:10]:
                cid = item.get("candidate_id", "")
                flags = candidate_lifecycle_flags(item)
                is_pending_update_candidate = bool(pending_improvement_candidate) and str(cid) == str(improvement_candidate.get("candidate_id") or "")
                source_label = str(item.get("source") or "ui_draft").strip()
                st.markdown(f"**{cid}** — {nice_label(item.get('classification', ''))} — `{item.get('status', 'draft')}`")
                st.caption(f"Playbook: {item.get('suggested_playbook') or 'not linked'} | Action: {item.get('suggested_action') or 'not set'} | Target: {item.get('target_host') or item.get('target_group') or 'not set'} | Source: {source_label}")
                if is_pending_update_candidate:
                    st.warning("Active playbook update candidate. Complete Save → Test → Approve → Enable to promote this version into the registry.")
                    lineage = item.get("draft_runbook", {}).get("lineage", {}) if isinstance(item.get("draft_runbook"), dict) else {}
                    if lineage:
                        st.caption(
                            f"Lineage: {lineage.get('existing_action', item.get('suggested_action', ''))} | "
                            f"current={lineage.get('current_playbook', '')} | "
                            f"proposed={lineage.get('improvement_candidate_playbook', item.get('suggested_playbook', ''))}"
                        )
                st.markdown(lifecycle_badges(flags))
                with st.expander(f"Manage candidate {cid}", expanded=is_pending_update_candidate):
                    if flags.get("enabled"):
                        st.success("Registry mapping is enabled. Create/promote a fresh queue to execute this automation.")
                    generated = item.get("generated_playbook") if isinstance(item.get("generated_playbook"), dict) else {}
                    action_default = item.get("suggested_action") or generated.get("action") or ""
                    playbook_default = item.get("suggested_playbook") or generated.get("playbook") or ""
                    target_default = item.get("target_host") or generated.get("target_host") or ""
                    if not action_default or not playbook_default or not target_default:
                        st.caption("Candidate metadata is incomplete. Generate Draft Playbook will auto-resolve action, playbook, and target if needed.")
                    action_value = st.text_input("Optional action override", value=action_default, key=f"cand_action_{cid}", help="Leave unchanged to use deterministic platform suggestion.")
                    playbook_value = st.text_input("Optional playbook override", value=playbook_default, key=f"cand_playbook_{cid}", help="Leave unchanged to use deterministic platform suggestion.")
                    target_value = st.text_input("Optional target host override", value=target_default, key=f"cand_target_{cid}", help="Leave unchanged to use deterministic platform suggestion.")
                    risk_value = st.selectbox(
                        "Risk tier",
                        ["low", "medium", "high"],
                        index=["low", "medium", "high"].index(item.get("risk_tier", "medium")) if item.get("risk_tier", "medium") in ["low", "medium", "high"] else 1,
                        key=f"cand_risk_{cid}",
                    )
                    existing_content = generated.get("content", "") or ""
                    if existing_content:
                        st.caption(f"Generated draft: {generated.get('playbook', playbook_value) or playbook_value} | Saved: {bool(generated.get('saved'))} | Path: {generated.get('saved_path', '') or 'not saved yet'}")
                        playbook_content = st.text_area(
                            "Review generated playbook YAML before saving",
                            value=existing_content,
                            height=360,
                            key=f"cand_yaml_{cid}",
                        )
                    else:
                        playbook_content = ""
                        st.info("No generated playbook draft yet. Click Generate Draft Playbook to auto-create reviewed YAML from the candidate steps.")

                    _fs = f"cand_fired_{cid}"  # session-state prefix for grey-out flags
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        _k = f"{_fs}_save_meta"
                        if st.button("Save Candidate Metadata", key=f"cand_save_{cid}", width="stretch", disabled=st.session_state.get(_k, False)):
                            st.session_state[_k] = True
                            try:
                                update_automation_candidate({
                                    "candidate_id": cid,
                                    "status": item.get("status", "draft"),
                                    "suggested_action": action_value,
                                    "suggested_playbook": playbook_value,
                                    "target_host": target_value,
                                    "risk_tier": risk_value,
                                    "approval_required": True,
                                    "updated_by": "Vijay",
                                })
                                st.success("Candidate metadata updated.")
                                st.rerun()
                            except Exception as e:
                                st.session_state[_k] = False
                                st.error(f"Update failed: {e}")
                    with c2:
                        _k = f"{_fs}_generate"
                        if st.button("Generate Draft Playbook", key=f"cand_generate_playbook_{cid}", width="stretch", disabled=(flags.get("enabled") or st.session_state.get(_k, False))):
                            st.session_state[_k] = True
                            try:
                                generate_automation_candidate_playbook({
                                    "candidate_id": cid,
                                    "generated_by": "Vijay",
                                    "action": action_value,
                                    "playbook": playbook_value,
                                    "target_host": target_value,
                                })
                                st.success("Draft playbook generated for review.")
                                st.rerun()
                            except Exception as e:
                                st.session_state[_k] = False
                                st.error(f"Generate failed: {e}")
                    with c3:
                        _k = f"{_fs}_save_playbook"
                        if st.button("Save Reviewed Playbook", key=f"cand_save_playbook_{cid}", width="stretch", disabled=(not bool(existing_content) or flags.get("enabled") or st.session_state.get(_k, False))):
                            st.session_state[_k] = True
                            try:
                                save_automation_candidate_playbook({
                                    "candidate_id": cid,
                                    "playbook_content": playbook_content,
                                    "reviewed_by": "Vijay",
                                    "review_notes": "Reviewed and saved from Automation Development UI.",
                                    "overwrite": True,
                                })
                                st.success("Reviewed playbook saved to /automation/playbooks.")
                                st.rerun()
                            except Exception as e:
                                st.session_state[_k] = False
                                st.error(f"Save playbook failed: {e}")

                    m1, m2, m3 = st.columns(3)
                    with m1:
                        _k = f"{_fs}_mark_tested"
                        if st.button("Mark Tested", key=f"cand_tested_{cid}", width="stretch", disabled=(not flags.get("saved") or flags.get("enabled") or st.session_state.get(_k, False))):
                            st.session_state[_k] = True
                            try:
                                record_automation_candidate_test_result({
                                    "candidate_id": cid,
                                    "test_status": "pass",
                                    "tested_by": "Vijay",
                                    "execution_mode": execution_mode,
                                    "returncode": 0,
                                    "notes": "Marked tested from UI after reviewed playbook validation.",
                                })
                                st.success("Test result recorded.")
                                st.rerun()
                            except Exception as e:
                                st.session_state[_k] = False
                                st.error(f"Test record failed: {e}")
                    with m2:
                        _k = f"{_fs}_approve"
                        if st.button("Approve Candidate", key=f"cand_approve_{cid}", width="stretch", disabled=(not flags.get("tested") or flags.get("approved") or flags.get("enabled") or st.session_state.get(_k, False))):
                            st.session_state[_k] = True
                            try:
                                approve_automation_candidate_request(cid, approved_by="Vijay", approval_notes="Approved from Automation Development stage after reviewed playbook save/test.")
                                st.success("Candidate approved.")
                                st.rerun()
                            except Exception as e:
                                st.session_state[_k] = False
                                st.error(f"Approval failed: {e}")
                    with m3:
                        _k = f"{_fs}_enable"
                        if st.button("Enable Registry Mapping", key=f"cand_enable_{cid}", width="stretch", disabled=(not flags.get("approved") or flags.get("enabled") or st.session_state.get(_k, False))):
                            st.session_state[_k] = True
                            try:
                                enable_automation_candidate_request(cid, enabled_by="Vijay")
                                if str(item.get("source") or "").lower() == "execution_feedback":
                                    st.success("Updated playbook candidate enabled into automation registry. Future executions will use the promoted v2/v3 playbook.")
                                else:
                                    st.success("Candidate enabled into automation registry.")
                                st.rerun()
                            except Exception as e:
                                st.session_state[_k] = False
                                st.error(f"Enable failed: {e}")

        st.markdown("---")
        st.caption("If the candidate was created with the wrong inputs, create a new candidate from the current runbook here. Keep this as an exception path, not the normal flow.")
        if st.button("Create New Candidate From Current Runbook", key=f"cand_create_new_from_backlog_{runtime_key}", width="stretch"):
            try:
                resp = create_automation_candidate(build_candidate_payload_from_runbook(
                    incident_id, classification, runbook, linked_resolution, linked_playbook,
                    "Created from Automation Development backlog after engineer requested a new candidate."
                ))
                candidate = resp.get("candidate", {})
                runtime["candidate_entry"] = candidate
                runtime["candidate_result"] = resp
                st.session_state.automation_runtime[runtime_key] = runtime
                st.success(f"New automation candidate created: {candidate.get('candidate_id', 'created')}")
                st.rerun()
            except Exception as e:
                st.error(f"Candidate creation failed: {e}")


    if queue_entry:
        st.markdown("---")
        st.subheader("Queued Automation Plan")
        st.write(f"**Queue ID:** {queue_entry.get('queue_id', '')}")
        st.write(f"**Queue Status:** {queue_entry.get('status', '')}")
        st.write("**Execution Governance:** Human-in-the-loop")
        st.write("**Human Approval Required:** Yes")
        plan = queue_entry.get("plan", {}) or {}
        st.write(f"**Planned Steps:** {plan.get('step_count', 0)}")
        st.write(f"**Mapping Source:** {plan.get('mapping_source', 'unknown')}")
        steps = plan.get("steps", []) or []
        if steps:
            for step in steps:
                label = step.get("playbook") or step.get("action") or step.get("name")
                st.write(f"• {label} ({step.get('connector', 'simulation')})")
        else:
            st.caption("The queue was created but no executable mapping exists yet. Use Learn Automation Mapping below to approve actions.")

        if int(plan.get("step_count", 0) or 0) == 0 and classification:
            st.markdown("---")
            st.subheader("Learn Automation Mapping")
            st.caption("No action mapping exists for this classification yet. Review candidate steps and save them as approved mapping once.")

            suggested_actions = runbook.get("automation_steps", []) or linked_playbook.get("steps", []) or []
            if f"learned_actions_{runtime_key}" not in st.session_state:
                st.session_state[f"learned_actions_{runtime_key}"] = "\n".join(suggested_actions)
            actions_text = st.text_area(
                "Approved Actions (one per line)",
                key=f"learned_actions_{runtime_key}",
                height=150,
            )

            if st.button("Approve & Save Mapping", width="stretch", key=f"learn_mapping_{runtime_key}"):
                try:
                    approved_actions = lines_to_list(actions_text)
                    resp = learn_automation_mapping({
                        "classification": classification,
                        "actions": approved_actions,
                        "learned_by": "Vijay",
                        "source": "manual_approval",
                        "source_ticket": latest_ticket or "",
                    })
                    mapping_runtime["learn_result"] = resp
                    learn_status = resp.get("status") or (resp.get("result") or {}).get("status")
                    if learn_status == "ok" and queue_id:
                        refreshed = refresh_automation_queue_request(queue_id)
                        runtime["queue_entry"] = refreshed.get("queue_entry", runtime.get("queue_entry", {}))
                        mapping_runtime["refresh_result"] = refreshed
                        st.session_state.automation_runtime[runtime_key] = runtime
                    st.session_state.automation_mapping_runtime[runtime_key] = mapping_runtime
                    if learn_status == "ok":
                        st.success(f"Saved learned mapping for {nice_label(classification)}. Queue plan refreshed.")
                        st.rerun()
                    else:
                        st.error(f"Learned Mapping Status: {learn_status or 'invalid'}")
                except Exception as e:
                    st.error(f"Learn mapping failed: {e}")

    if approval_result:
        st.success(f"Approved: {(approval_result.get('queue_entry') or {}).get('queue_id', queue_id)}")

    if execution_result:
        st.markdown("---")
        execution = execution_result.get("execution", {}) or {}
        st.write(f"**Execution Status:** {execution.get('status', execution_result.get('status', 'unknown'))}")
        if (runtime.get("queue_entry") or {}).get("status") == "closed_remediated":
            st.success("Queue auto-closed after successful live remediation.")
        st.write(f"**Executed By:** {execution.get('executed_by', 'n/a')}")
        st.write(f"**Execution Mode:** {execution.get('execution_mode', execution_mode)}")
        st.write("**Execution Governance:** Human-in-the-loop")
        st.write("**Human Approval:** Completed before execution")
        st.write(f"**Executed At:** {execution.get('executed_at', 'n/a')}")
        steps = execution.get("steps", []) or []
        if steps:
            with st.expander("Execution Details", expanded=False):
                for idx, step in enumerate(steps, start=1):
                    st.write(f"**Step {idx}:** {step.get('action', 'unknown')} → {step.get('status', 'unknown')}")
                    result = step.get('connector_result') or {}
                    if result:
                        render_execution_evidence(result)
                    evidence = step.get('evidence') or {}
                    if evidence and evidence is not result:
                        render_execution_evidence(evidence)

        # Step 3C: submit post-execution engineer feedback to backend and
        # immediately display the refreshed automation confidence summary.
        st.markdown("---")
        st.subheader("Post-Execution Engineer Feedback")
        st.caption(
            "Submit the execution outcome to the backend learning endpoint. "
            "The Automation Confidence and autonomous-readiness panel refreshes after submission."
        )

        feedback_state_key = f"execution_feedback_{runtime_key}"
        existing_feedback = runtime.get("execution_feedback", {}) if isinstance(runtime.get("execution_feedback"), dict) else {}
        existing_backend_feedback = runtime.get("backend_feedback_result", {}) if isinstance(runtime.get("backend_feedback_result"), dict) else {}
        existing_confidence_summary = runtime.get("automation_confidence_summary", {}) if isinstance(runtime.get("automation_confidence_summary"), dict) else {}
        if feedback_state_key not in st.session_state:
            st.session_state[feedback_state_key] = existing_feedback.get("engineer_notes") or existing_feedback.get("notes", "")

        # Determine the automation identity used for confidence roll-up.
        first_step = steps[0] if steps and isinstance(steps[0], dict) else {}
        plan = (runtime.get("queue_entry") or {}).get("plan", {}) if isinstance((runtime.get("queue_entry") or {}).get("plan", {}), dict) else {}
        plan_steps = plan.get("steps", []) if isinstance(plan.get("steps", []), list) else []
        first_plan_step = plan_steps[0] if plan_steps and isinstance(plan_steps[0], dict) else {}
        action_for_feedback = (
            first_step.get("action")
            or first_plan_step.get("action")
            or registry_mapping.get("action") if isinstance(registry_mapping, dict) else ""
        )
        playbook_for_feedback = (
            first_step.get("playbook")
            or first_plan_step.get("playbook")
            or registry_mapping.get("playbook") if isinstance(registry_mapping, dict) else ""
        )
        target_for_feedback = (
            first_step.get("target_host")
            or first_plan_step.get("target_host")
            or first_plan_step.get("target")
            or (runtime.get("queue_entry") or {}).get("target_host")
            or registry_mapping.get("target_host") if isinstance(registry_mapping, dict) else ""
        )
        risk_tier_for_feedback = (
            linked_playbook.get("risk_tier")
            or registry_mapping.get("risk_tier") if isinstance(registry_mapping, dict) else "medium"
        ) or "medium"
        safety_gate_for_feedback = runtime.get("safety_gate_result", {}) if isinstance(runtime.get("safety_gate_result"), dict) else {}
        safety_gate_status_for_feedback = str(safety_gate_for_feedback.get("gate_status") or safety_gate_status or "unknown").lower()

        f1, f2, f3 = st.columns(3)
        with f1:
            resolved_choice = st.selectbox(
                "Did automation resolve the incident?",
                ["Yes", "Partially", "No"],
                index={"yes": 0, "partially": 1, "no": 2}.get(str(existing_feedback.get("resolved", "yes")).lower(), 0),
                key=f"feedback_resolved_{runtime_key}",
            )
        with f2:
            extra_steps_choice = st.selectbox(
                "Were additional manual steps required?",
                ["No", "Yes"],
                index=1 if bool(existing_feedback.get("additional_steps_required")) else 0,
                key=f"feedback_extra_steps_{runtime_key}",
            )
        with f3:
            playbook_update_choice = st.selectbox(
                "Should playbook be reviewed?",
                ["No", "Yes"],
                index=1 if bool(existing_feedback.get("playbook_review_required")) else 0,
                key=f"feedback_playbook_review_{runtime_key}",
            )

        feedback_notes = st.text_area(
            "Engineer notes / additional steps observed",
            key=feedback_state_key,
            height=130,
            placeholder=(
                "Example: Also checked target group health-check timeout and found a mismatch. "
                "Add listener health-check validation before remediation."
            ),
        )

        submitted_feedback = bool(existing_feedback.get("submitted")) or bool(existing_backend_feedback)
        if submitted_feedback:
            st.success("Engineer feedback submitted to backend and included in Automation Confidence scoring.")
            with st.expander("Submitted feedback", expanded=False):
                st.json(existing_feedback)
            if existing_backend_feedback:
                with st.expander("Backend feedback response", expanded=False):
                    st.json(existing_backend_feedback)

        if st.button(
            "Submit Execution Feedback",
            key=f"submit_execution_feedback_{runtime_key}",
            width="stretch",
            disabled=submitted_feedback,
        ):
            resolved_by_automation = resolved_choice == "Yes"
            additional_steps_required = extra_steps_choice == "Yes"
            playbook_review_required = playbook_update_choice == "Yes" or additional_steps_required or resolved_choice != "Yes"
            execution_status_value = execution.get("status", execution_result.get("status", "unknown"))
            feedback_payload = {
                "feedback_id": f"ef_{uuid.uuid4().hex[:10]}",
                "queue_id": queue_id,
                "incident_id": incident_id,
                "classification": system_label(classification),
                "action": action_for_feedback or "",
                "playbook": playbook_for_feedback or "",
                "target_host": target_for_feedback or "",
                "execution_id": execution.get("execution_id") or execution.get("id") or "",
                "execution_status": execution_status_value,
                "execution_mode": execution.get("execution_mode", execution_mode),
                "resolved_by_automation": resolved_by_automation,
                "additional_steps_required": additional_steps_required,
                "playbook_review_required": playbook_review_required,
                "engineer_notes": feedback_notes.strip(),
                "submitted_by": "Vijay",
                "safety_gate_status": safety_gate_status_for_feedback,
                "rollback_available": True,
                "risk_tier": risk_tier_for_feedback,
            }

            try:
                backend_feedback = submit_execution_feedback_request(feedback_payload)
                confidence_payload = _confidence_summary_payload(
                    classification,
                    action_for_feedback or "",
                    playbook_for_feedback or "",
                    target_for_feedback or "",
                )
                try:
                    confidence_summary_response = get_automation_confidence_summary_request(confidence_payload)
                    confidence_summary = (
                        confidence_summary_response.get("confidence_summary", confidence_summary_response)
                        if isinstance(confidence_summary_response, dict)
                        else {}
                    )
                except Exception:
                    confidence_summary = backend_feedback.get("confidence_summary", {}) if isinstance(backend_feedback, dict) else {}

                feedback_payload["submitted"] = True
                feedback_payload["backend_persisted"] = True
                runtime["execution_feedback"] = feedback_payload
                runtime["backend_feedback_result"] = backend_feedback
                runtime["automation_confidence_summary"] = confidence_summary if isinstance(confidence_summary, dict) else {}
                st.session_state.automation_runtime[runtime_key] = runtime

                # Keep a small local audit copy for operator troubleshooting.
                try:
                    UI_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                    feedback_file = UI_RUNTIME_DIR / "execution_feedback_local.json"
                    existing_items = []
                    if feedback_file.exists():
                        loaded = json.loads(feedback_file.read_text(encoding="utf-8"))
                        existing_items = loaded if isinstance(loaded, list) else []
                    existing_items.append(_json_safe(feedback_payload))
                    feedback_file.write_text(json.dumps(existing_items[-100:], indent=2), encoding="utf-8")
                except Exception:
                    pass

                if playbook_review_required:
                    st.warning("Feedback submitted. Backend recorded pending playbook review, so autonomous readiness should remain disabled.")
                else:
                    st.success("Feedback submitted. Backend recorded a clean human-approved run for Automation Confidence scoring.")
                st.rerun()
            except Exception as e:
                st.error(f"Execution feedback submission failed: {e}")

        # Step 3D: Feedback that requires extra steps creates a distinct
        # playbook improvement candidate tied to the existing automation.
        current_feedback_for_improvement = runtime.get("execution_feedback", {}) if isinstance(runtime.get("execution_feedback"), dict) else existing_feedback
        improvement_required = bool(
            current_feedback_for_improvement.get("additional_steps_required")
            or current_feedback_for_improvement.get("playbook_review_required")
        )
        existing_improvement_candidate = runtime.get("playbook_improvement_candidate", {}) if isinstance(runtime.get("playbook_improvement_candidate"), dict) else {}

        if submitted_feedback and improvement_required:
            st.markdown("---")
            st.subheader("Playbook Improvement Required")
            st.warning(
                "Engineer feedback included extra steps or requested playbook review. "
                "Create a new candidate version tied to the existing approved automation before autonomous eligibility can proceed."
            )

            improvement_playbook_name = next_improvement_playbook_name(playbook_for_feedback or "automation_playbook.yml")
            lineage_cols = st.columns(4)
            with lineage_cols[0]:
                st.write(f"**Existing automation:** `{action_for_feedback or 'unknown'}`")
            with lineage_cols[1]:
                st.write(f"**Current playbook:** `{playbook_for_feedback or 'unknown'}`")
            with lineage_cols[2]:
                st.write(f"**Improvement candidate:** `{improvement_playbook_name}`")
            with lineage_cols[3]:
                st.write("**Source:** `execution_feedback`")

            if existing_improvement_candidate:
                st.success(
                    "Playbook improvement candidate created: "
                    f"{existing_improvement_candidate.get('candidate_id', 'created')} → "
                    f"{existing_improvement_candidate.get('suggested_playbook', improvement_playbook_name)}"
                )
                with st.expander("Improvement candidate details", expanded=False):
                    st.json(existing_improvement_candidate)
            else:
                if st.button(
                    "Create Playbook Improvement Candidate",
                    key=f"create_playbook_improvement_candidate_{runtime_key}",
                    width="stretch",
                ):
                    try:
                        payload = build_playbook_improvement_candidate_payload(
                            incident_id=incident_id,
                            classification=system_label(classification),
                            current_action=action_for_feedback or "",
                            current_playbook=playbook_for_feedback or "",
                            target_host=target_for_feedback or "",
                            risk_tier=risk_tier_for_feedback or "medium",
                            feedback=current_feedback_for_improvement,
                            source_candidate=active_candidate or registry_candidate or {},
                        )
                        resp = create_automation_candidate(payload)
                        candidate = resp.get("candidate", {}) if isinstance(resp, dict) else {}
                        runtime["playbook_improvement_candidate"] = candidate
                        runtime["playbook_improvement_candidate_result"] = resp
                        runtime["active_improvement_candidate_id"] = candidate.get("candidate_id", "")
                        runtime["candidate_entry"] = candidate
                        runtime["candidate_result"] = resp
                        st.session_state.automation_runtime[runtime_key] = runtime
                        st.success(
                            "Playbook improvement candidate created and moved to the pending-review backlog: "
                            f"{candidate.get('candidate_id', 'created')} → "
                            f"{candidate.get('suggested_playbook', improvement_playbook_name)}. "
                            "Open Automation Development / Candidate Backlog to generate, save, test, approve, and enable it."
                        )
                        st.rerun()
                    except Exception as e:
                        st.error(f"Playbook improvement candidate creation failed: {e}")

        # Display the current backend confidence summary after feedback submission.
        if existing_confidence_summary:
            st.markdown("---")
            render_learning_autonomy_panel(
                existing_confidence_summary,
                identity_payload={
                    "classification": system_label(classification),
                    "action": action_for_feedback or "",
                    "playbook": playbook_for_feedback or "",
                    "target_host": target_for_feedback or "",
                    "risk_tier": risk_tier_for_feedback or "medium",
                },
                runtime=runtime,
                runtime_key=runtime_key,
            )
        elif submitted_feedback:
            st.info("Feedback was submitted, but the confidence summary is not available in local UI state yet. Refresh the page or submit the next run feedback to recalculate.")

