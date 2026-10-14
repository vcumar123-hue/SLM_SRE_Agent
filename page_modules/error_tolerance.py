"""Error Tolerance page module."""
from __future__ import annotations
from default_tenant import default_tenant_id
import json
from typing import Any, Dict, List, Optional
import streamlit as st
import requests
from page_modules.shared import (
    BACKEND_URL, SESSION, REQUEST_TIMEOUT_MEDIUM, REQUEST_TIMEOUT_SHORT,
    nice_label, format_confidence, metric_card, section_title,
    render_empty_state, dedupe_lines, natural_label,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tier badge colours (bg, text, border)
_TIER_BADGE: Dict[str, tuple] = {
    "prod-standalone": ("#fee2e2", "#991b1b", "#dc2626"),
    "prod-redundant":  ("#ffedd5", "#9a3412", "#f97316"),
    "prod-ha":         ("#fef9c3", "#854d0e", "#ca8a04"),
    "non-prod":        ("#d1fae5", "#065f46", "#10b981"),
    "dr-standby":      ("#dbeafe", "#1e40af", "#3b82f6"),
    "unknown":         ("#f3f4f6", "#374151", "#9ca3af"),
}

_TIER_POLICIES: List[Dict[str, Any]] = [
    {"tier": "prod-standalone", "tolerance_mins": 15,  "auto_remediate": False, "escalation": "P1", "approval": True,  "max_retry": 1},
    {"tier": "prod-redundant",  "tolerance_mins": 30,  "auto_remediate": True,  "escalation": "P1", "approval": True,  "max_retry": 2},
    {"tier": "prod-ha",         "tolerance_mins": 60,  "auto_remediate": True,  "escalation": "P2", "approval": False, "max_retry": 3},
    {"tier": "non-prod",        "tolerance_mins": 240, "auto_remediate": True,  "escalation": "P3", "approval": False, "max_retry": 5},
    {"tier": "dr-standby",      "tolerance_mins": 120, "auto_remediate": False, "escalation": "P2", "approval": True,  "max_retry": 2},
    {"tier": "unknown",         "tolerance_mins": 30,  "auto_remediate": False, "escalation": "P1", "approval": True,  "max_retry": 1},
]

_INCIDENT_CLASSES = [
    "cpu_high", "memory_exhaustion", "disk_full", "network_latency",
    "service_down", "database_connection_failure", "ssl_cert_expiry",
    "application_crash", "load_balancer_failure", "dns_resolution_failure",
    "authentication_failure", "deployment_rollback", "config_drift",
    "backup_failure", "monitoring_gap",
]

_SEVERITIES = ["P1", "P2", "P3", "P4"]

_RECOMMENDATION_CONFIG: Dict[str, tuple] = {
    "auto_remediate": ("✅", "#065f46", "#d1fae5", "#10b981", "Auto-remediate approved"),
    "escalate":       ("⚠️", "#92400e", "#fef3c7", "#f59e0b", "Escalate to team"),
    "page_oncall":    ("🚨", "#991b1b", "#fee2e2", "#dc2626", "Page on-call engineer"),
    "monitor":        ("👁️", "#1e40af", "#dbeafe", "#3b82f6", "Monitor only"),
}

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30, show_spinner=False)
def _fetch_ci_list(query: str = "") -> List[dict]:
    try:
        params = {"query": query} if query else {}
        r = SESSION.get(f"{BACKEND_URL}/cmdb/ci", params=params, timeout=REQUEST_TIMEOUT_MEDIUM)
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception:
        return []


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_tolerance(ci_name: str) -> dict:
    try:
        r = SESSION.get(f"{BACKEND_URL}/tolerance/{ci_name}", timeout=REQUEST_TIMEOUT_SHORT)
        r.raise_for_status()
        return r.json().get("item") or r.json()
    except Exception:
        return {}


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_cmdb_stats() -> dict:
    try:
        r = SESSION.get(f"{BACKEND_URL}/cmdb/stats", timeout=REQUEST_TIMEOUT_SHORT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _tier_badge_html(tier: str, size: str = "normal") -> str:
    key = str(tier or "unknown").strip().lower()
    bg, text, border = _TIER_BADGE.get(key, _TIER_BADGE["unknown"])
    font = "0.72rem"
    pad = "2px 10px"
    label = tier.upper() if tier else "UNKNOWN"
    return (
        f'<span style="background:{bg};color:{text};border:1.5px solid {border};'
        f'border-radius:20px;padding:{pad};font-size:{font};font-weight:700;'
        f'letter-spacing:0.04em;">{label}</span>'
    )


def _risk_indicator(tier: str) -> str:
    tier_lower = str(tier or "").lower()
    if tier_lower in {"prod-standalone"}:
        return "🔴 High risk"
    if tier_lower in {"prod-redundant", "dr-standby", "unknown"}:
        return "🟡 Medium risk"
    return "🟢 Low risk"


def _stars(n: int) -> str:
    n = max(1, min(5, int(n or 1)))
    return "★" * n + "☆" * (5 - n)


# ---------------------------------------------------------------------------
# Tab 1: CI Browser
# ---------------------------------------------------------------------------

def _render_ci_browser() -> None:
    left_col, right_col = st.columns([2, 3])

    with left_col:
        section_title("Search & Sync")
        search = st.text_input(
            "Search CIs",
            placeholder="hostname, environment, tier…",
            key="et_ci_search",
        )
        sync_col, _ = st.columns([1, 2])
        with sync_col:
            if st.button("🔄 Sync from ServiceNow", key="et_sync_btn", use_container_width=True):
                try:
                    with st.spinner("Syncing from ServiceNow…"):
                        r = SESSION.post(f"{BACKEND_URL}/cmdb/sync", timeout=REQUEST_TIMEOUT_MEDIUM)
                        r.raise_for_status()
                        result = r.json()
                    synced = result.get("synced", result.get("count", "?"))
                    st.success(f"Sync complete — {synced} CIs updated.")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Sync failed: {exc}", icon="⚠️")

        ci_list = _fetch_ci_list(search.strip())
        section_title("CI Inventory")

        if not ci_list:
            render_empty_state(
                "🖥️",
                "No CIs Found",
                "No CIs matched your search. Try a different query or sync from ServiceNow.",
            )
        else:
            for ci in ci_list[:50]:
                ci_name = ci.get("ci_name", "")
                tier = ci.get("tier", "unknown")
                env = ci.get("environment", "")
                redundancy = ci.get("redundancy_type", "")
                badge_html = _tier_badge_html(tier)

                card_html = (
                    f'<div style="border:1px solid #e5e7eb;border-radius:8px;'
                    f'padding:10px 14px;margin-bottom:8px;background:#fafafa;cursor:pointer;">'
                    f'  <div style="font-weight:600;font-size:0.95rem;">{ci_name}</div>'
                    f'  <div style="margin-top:4px;">{badge_html} '
                    f'<span style="font-size:0.78rem;color:#6b7280;margin-left:6px;">'
                    f'{env} · {nice_label(redundancy)}</span></div>'
                    f'</div>'
                )
                st.markdown(card_html, unsafe_allow_html=True)
                if st.button(
                    f"Select  {ci_name}",
                    key=f"et_select_ci_{ci_name}",
                    use_container_width=True,
                ):
                    st.session_state["et_selected_ci"] = ci
                    st.rerun()

    with right_col:
        selected = st.session_state.get("et_selected_ci")
        if not selected:
            render_empty_state(
                "🖥️",
                "No CI Selected",
                "Select a CI from the list on the left to view its full profile and tolerance policy.",
            )
        else:
            ci_name = selected.get("ci_name", "")
            tier = selected.get("tier", "unknown")
            section_title(f"CI Profile — {ci_name}")

            badge_html = _tier_badge_html(tier)
            risk_label = _risk_indicator(tier)
            criticality = selected.get("business_criticality", 3)

            st.markdown(
                f'<div style="background:#f8faff;border:1px solid #dbeafe;border-radius:10px;'
                f'padding:16px 20px;margin-bottom:12px;">'
                f'  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 20px;">'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">CI Name</span><br>'
                f'      <strong>{ci_name}</strong></div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Environment</span><br>'
                f'      <strong>{selected.get("environment", "—")}</strong></div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Tier</span><br>'
                f'      {badge_html}</div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Redundancy</span><br>'
                f'      <strong>{nice_label(selected.get("redundancy_type", ""))}</strong></div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Business Criticality</span><br>'
                f'      <strong style="color:#f59e0b;">{_stars(criticality)}</strong> ({criticality}/5)</div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Risk Level</span><br>'
                f'      <strong>{risk_label}</strong></div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Assignment Group</span><br>'
                f'      <strong>{selected.get("assignment_group", "—")}</strong></div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Failover Target</span><br>'
                f'      <strong>{selected.get("failover_target", "—")}</strong></div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Last Synced</span><br>'
                f'      <strong>{str(selected.get("last_synced", "—"))[:16]}</strong></div>'
                f'    <div><span style="color:#6b7280;font-size:0.78rem;">Source</span><br>'
                f'      <strong>{nice_label(selected.get("source", "ServiceNow"))}</strong></div>'
                f'  </div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            section_title("Tolerance Policy")
            tol = _fetch_tolerance(ci_name)
            if tol:
                tc1, tc2, tc3, tc4, tc5 = st.columns(5)
                with tc1:
                    metric_card("Tolerance (min)", str(tol.get("tolerance_minutes", "—")), "dashboard")
                with tc2:
                    metric_card("Auto Remediate", "Yes" if tol.get("auto_remediate") else "No", "automation")
                with tc3:
                    metric_card("Escalation Level", str(tol.get("escalation_level", "—")), "knowledge")
                with tc4:
                    metric_card("Approval Required", "Yes" if tol.get("approval_required") else "No", "neutral")
                with tc5:
                    metric_card("Max Retry", str(tol.get("max_retry", "—")), "neutral")
            else:
                st.info("No tolerance policy configured for this CI. Using tier defaults.")

    # Register CI manually
    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("➕ Register CI Manually", expanded=False):
        with st.form("et_register_ci_form", clear_on_submit=True):
            f1, f2 = st.columns(2)
            with f1:
                reg_name = st.text_input("CI Name *", placeholder="web-prod-01")
                reg_env = st.selectbox("Environment", ["production", "staging", "development", "dr"])
                reg_tier = st.selectbox(
                    "Tier",
                    list(_TIER_BADGE.keys()),
                    format_func=nice_label,
                )
                reg_redundancy = st.selectbox(
                    "Redundancy Type",
                    ["standalone", "active-passive", "active-active", "clustered"],
                )
            with f2:
                reg_criticality = st.slider("Business Criticality", 1, 5, 3)
                reg_group = st.text_input("Assignment Group", placeholder="sre-platform")
                reg_failover = st.text_input("Failover Target", placeholder="web-prod-02")
                reg_source = st.selectbox("Source", ["manual", "servicenow", "cache"])

            if st.form_submit_button("Register CI", type="primary"):
                if not reg_name.strip():
                    st.warning("CI Name is required.", icon="⚠️")
                else:
                    payload = {
                        "ci_name": reg_name.strip(),
                        "environment": reg_env,
                        "tier": reg_tier,
                        "redundancy_type": reg_redundancy,
                        "business_criticality": reg_criticality,
                        "assignment_group": reg_group.strip(),
                        "failover_target": reg_failover.strip(),
                        "source": reg_source,
                    }
                    try:
                        r = SESSION.post(f"{BACKEND_URL}/cmdb/ci", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
                        r.raise_for_status()
                        st.success(f"CI `{reg_name}` registered successfully.")
                        st.cache_data.clear()
                    except Exception as exc:
                        st.error(f"Registration failed: {exc}", icon="⚠️")


# ---------------------------------------------------------------------------
# Tab 2: Tolerance Policies
# ---------------------------------------------------------------------------

def _render_tolerance_policies() -> None:
    section_title("Tier Tolerance Reference")

    rows = []
    for p in _TIER_POLICIES:
        rows.append({
            "Tier": p["tier"],
            "Tolerance (min)": p["tolerance_mins"],
            "Auto-Remediate": "Yes" if p["auto_remediate"] else "No",
            "Escalation": p["escalation"],
            "Approval Required": "Yes" if p["approval"] else "No",
            "Max Retry": p["max_retry"],
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    # CMDB stats
    st.markdown("<br>", unsafe_allow_html=True)
    section_title("Your CI Inventory")
    stats = _fetch_cmdb_stats()

    if not stats:
        render_empty_state(
            "📊",
            "No CMDB Stats Available",
            "Sync from ServiceNow to populate CI inventory statistics.",
        )
        return

    by_env = stats.get("by_environment", {})
    by_tier = stats.get("by_tier", {})
    by_redundancy = stats.get("by_redundancy", {})

    if by_env:
        section_title("By Environment")
        env_cols = st.columns(min(len(by_env), 5))
        for col, (env, count) in zip(env_cols, by_env.items()):
            with col:
                metric_card(nice_label(env), str(count), "dashboard")

    if by_tier:
        st.markdown("<br>", unsafe_allow_html=True)
        section_title("By Tier")
        tier_cols = st.columns(min(len(by_tier), 6))
        for col, (tier, count) in zip(tier_cols, by_tier.items()):
            with col:
                metric_card(nice_label(tier), str(count), "automation")

    if by_redundancy:
        st.markdown("<br>", unsafe_allow_html=True)
        section_title("By Redundancy Type")
        red_cols = st.columns(min(len(by_redundancy), 5))
        for col, (rtype, count) in zip(red_cols, by_redundancy.items()):
            with col:
                metric_card(nice_label(rtype), str(count), "knowledge")


# ---------------------------------------------------------------------------
# Tab 3: Evaluate Incident
# ---------------------------------------------------------------------------

def _render_evaluate_incident() -> None:
    section_title("Evaluate Incident Against Tolerance Policy")

    with st.form("et_evaluate_form"):
        ev1, ev2 = st.columns(2)
        with ev1:
            eval_ci = st.text_input(
                "CI Name *",
                placeholder="e.g. web-prod-01",
                key="et_eval_ci_name",
            )
            eval_class = st.selectbox(
                "Incident Classification",
                _INCIDENT_CLASSES,
                format_func=nice_label,
                key="et_eval_class",
            )
        with ev2:
            eval_sev = st.selectbox("Severity", _SEVERITIES, key="et_eval_sev")
            eval_elapsed = st.number_input(
                "Elapsed Time (minutes since incident started)",
                min_value=0,
                max_value=10000,
                value=0,
                step=1,
                key="et_eval_elapsed",
            )

        submitted = st.form_submit_button("⚖️ Evaluate Tolerance", type="primary", use_container_width=True)

    if submitted:
        if not eval_ci.strip():
            st.warning("CI Name is required.", icon="⚠️")
            return
        payload = {
            "ci_name": eval_ci.strip(),
            "classification": eval_class,
            "severity": eval_sev,
            "elapsed_minutes": int(eval_elapsed),
        }
        try:
            with st.spinner("Evaluating tolerance…"):
                r = SESSION.post(
                    f"{BACKEND_URL}/tolerance/evaluate",
                    json=payload,
                    timeout=REQUEST_TIMEOUT_MEDIUM,
                )
                r.raise_for_status()
                result = r.json()
            st.session_state["et_eval_result"] = result
        except Exception as exc:
            st.error(f"Evaluation failed: {exc}", icon="⚠️")
            st.session_state.pop("et_eval_result", None)

    result = st.session_state.get("et_eval_result")
    if not result:
        return

    st.markdown("<br>", unsafe_allow_html=True)

    # Tolerance status banner
    within_tolerance = result.get("within_tolerance", False)
    if within_tolerance:
        tolerance_bg, tolerance_text, tolerance_border = "#d1fae5", "#065f46", "#10b981"
        tolerance_label = "✅  WITHIN TOLERANCE"
    else:
        tolerance_bg, tolerance_text, tolerance_border = "#fee2e2", "#991b1b", "#dc2626"
        tolerance_label = "🚨  TOLERANCE EXCEEDED"

    st.markdown(
        f'<div style="background:{tolerance_bg};border:2px solid {tolerance_border};'
        f'border-radius:12px;padding:18px 24px;text-align:center;margin-bottom:16px;">'
        f'  <div style="font-size:1.4rem;font-weight:800;color:{tolerance_text};">{tolerance_label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Recommendation box
    recommendation = str(result.get("recommendation", "monitor")).lower().strip()
    rec_key = recommendation if recommendation in _RECOMMENDATION_CONFIG else "monitor"
    icon, text_col, bg_col, border_col, rec_label = _RECOMMENDATION_CONFIG[rec_key]

    st.markdown(
        f'<div style="background:{bg_col};border:1.5px solid {border_col};'
        f'border-radius:10px;padding:14px 20px;margin-bottom:14px;">'
        f'  <div style="font-size:0.78rem;font-weight:700;color:{text_col};'
        f'letter-spacing:0.06em;margin-bottom:6px;">RECOMMENDATION</div>'
        f'  <div style="font-size:1.1rem;font-weight:700;color:{text_col};">'
        f'{icon} {rec_label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Policy details + reasoning
    pc1, pc2 = st.columns(2)
    policy = result.get("policy", {})
    with pc1:
        section_title("Policy Details Used")
        if policy:
            pd1, pd2, pd3 = st.columns(3)
            with pd1:
                st.metric("Tolerance (min)", policy.get("tolerance_minutes", "—"))
            with pd2:
                st.metric("Auto Remediate", "Yes" if policy.get("auto_remediate") else "No")
            with pd3:
                st.metric("Escalation", policy.get("escalation_level", "—"))
            pd4, pd5 = st.columns(2)
            with pd4:
                st.metric("Approval Required", "Yes" if policy.get("approval_required") else "No")
            with pd5:
                st.metric("Max Retry", policy.get("max_retry", "—"))
        else:
            st.caption("No policy details returned.")

    with pc2:
        section_title("Reasoning")
        reasoning = result.get("reasoning", result.get("reason", ""))
        if reasoning:
            st.markdown(
                f'<div style="background:#f9fafb;border-left:4px solid #6b7280;'
                f'padding:12px 16px;border-radius:0 8px 8px 0;font-size:0.88rem;'
                f'color:#374151;line-height:1.6;">{reasoning}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("No reasoning provided by the backend.")

    # Add to Automation Queue CTA
    if rec_key == "auto_remediate":
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button(
            "➕ Add to Automation Queue",
            key="et_add_to_queue_btn",
            type="primary",
            use_container_width=False,
        ):
            queue_payload = {
                "ci_name": result.get("ci_name", ""),
                "classification": result.get("classification", ""),
                "severity": result.get("severity", ""),
                "elapsed_minutes": result.get("elapsed_minutes", 0),
                "recommendation": recommendation,
                "source": "error_tolerance_page",
                "requested_by": "operator",
                "tenant_id": st.session_state.get("tenant_id", default_tenant_id()),
            }
            try:
                r = SESSION.post(
                    f"{BACKEND_URL}/automation/queue",
                    json=queue_payload,
                    timeout=REQUEST_TIMEOUT_MEDIUM,
                )
                r.raise_for_status()
                res = r.json()
                st.success(
                    f"Added to Automation Queue: `{res.get('queue_id', 'ok')}`. "
                    "Navigate to the Automation Queue page to review and execute."
                )
            except Exception as exc:
                st.error(f"Failed to add to queue: {exc}", icon="⚠️")

    # Raw result for debugging
    with st.expander("Raw evaluation response", expanded=False):
        st.json(result)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def render_error_tolerance_page() -> None:
    st.subheader("Error Tolerance")
    st.caption(
        "Browse the CI inventory, review tier-based tolerance policies, "
        "and evaluate how far an active incident is from its tolerance threshold."
    )

    tab_ci, tab_policies, tab_evaluate = st.tabs([
        "🏢 CI Browser",
        "⚖️ Tolerance Policies",
        "🔍 Evaluate Incident",
    ])

    with tab_ci:
        _render_ci_browser()

    with tab_policies:
        _render_tolerance_policies()

    with tab_evaluate:
        _render_evaluate_incident()
