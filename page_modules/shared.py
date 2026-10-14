"""Shared helpers and fetch utilities for all page modules."""
from __future__ import annotations
import datetime
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import streamlit as st
import requests

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8001")
REQUEST_TIMEOUT_SHORT = 8
REQUEST_TIMEOUT_MEDIUM = 15
REQUEST_TIMEOUT_LONG = 120
SESSION = requests.Session()
# Release 1: the backend authenticates every request. Set AIOPS_UI_API_KEY to the token of a
# principal configured in AIOPS_PRINCIPALS (see security.py). In open dev mode no key is needed.
if os.getenv("AIOPS_UI_API_KEY", "").strip():
    SESSION.headers["Authorization"] = "Bearer " + os.getenv("AIOPS_UI_API_KEY", "").strip()

import re


# ---------------------------------------------------------------------------
# Fetch utilities
# ---------------------------------------------------------------------------

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


@st.cache_data(ttl=12, show_spinner=False)
def fetch_telemetry_events(limit: int = 100) -> list[dict]:
    try:
        response = SESSION.get(f"{BACKEND_URL}/telemetry/events", params={"limit": limit}, timeout=REQUEST_TIMEOUT_MEDIUM)
        response.raise_for_status()
        return response.json().get("items", [])
    except Exception:
        return []


@st.cache_data(ttl=12, show_spinner=False)
def fetch_telemetry_incidents(limit: int = 100) -> list[dict]:
    try:
        response = SESSION.get(f"{BACKEND_URL}/telemetry/incidents", params={"limit": limit}, timeout=REQUEST_TIMEOUT_MEDIUM)
        response.raise_for_status()
        return response.json().get("items", [])
    except Exception:
        return []


@st.cache_data(ttl=12, show_spinner=False)
def fetch_telemetry_incident_detail(incident_id: str) -> dict:
    try:
        response = SESSION.get(f"{BACKEND_URL}/telemetry/incidents/{incident_id}", timeout=REQUEST_TIMEOUT_MEDIUM)
        response.raise_for_status()
        return response.json().get("item") or {}
    except Exception:
        return {}


@st.cache_data(ttl=12, show_spinner=False)
def fetch_telemetry_incident_rca(incident_id: str) -> dict:
    try:
        response = SESSION.get(f"{BACKEND_URL}/telemetry/incidents/{incident_id}/rca", timeout=REQUEST_TIMEOUT_MEDIUM)
        response.raise_for_status()
        data = response.json()
        return data.get("item") if data.get("found") else {}
    except Exception:
        return {}


def call_telemetry_evaluate() -> dict:
    response = SESSION.post(f"{BACKEND_URL}/telemetry/evaluate", timeout=REQUEST_TIMEOUT_LONG)
    response.raise_for_status()
    return response.json()


def call_telemetry_promote(limit: int = 50) -> dict:
    response = SESSION.post(f"{BACKEND_URL}/telemetry/promote", json={"limit": limit}, timeout=REQUEST_TIMEOUT_LONG)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=12, show_spinner=False)
@st.cache_data(ttl=30, show_spinner=False)
def fetch_knowledge_sources() -> list[dict]:
    try:
        r = SESSION.get(f"{BACKEND_URL}/knowledge/sources", timeout=REQUEST_TIMEOUT_SHORT)
        r.raise_for_status()
        return r.json().get("sources", [])
    except Exception:
        return []


def fetch_knowledge_candidates() -> list[dict]:
    try:
        response = SESSION.get(f"{BACKEND_URL}/knowledge_candidates", timeout=REQUEST_TIMEOUT_SHORT)
        response.raise_for_status()
        return response.json().get("items", [])
    except Exception:
        return []


@st.cache_data(ttl=12, show_spinner=False)
def fetch_resolution_by_classification(classification: str) -> dict:
    try:
        response = SESSION.get(
            f"{BACKEND_URL}/resolution_by_classification/{classification}",
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        response.raise_for_status()
        return response.json().get("item") or {}
    except Exception:
        return {}


@st.cache_data(ttl=12, show_spinner=False)
def fetch_playbook_by_classification(classification: str) -> dict:
    try:
        response = SESSION.get(
            f"{BACKEND_URL}/playbook_by_classification/{classification}",
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        response.raise_for_status()
        return response.json().get("item") or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Label / formatting helpers
# ---------------------------------------------------------------------------

def nice_label(text: str) -> str:
    label = str(text or "").replace("_", " ").strip().title()
    acronym_map = {
        "Cpu": "CPU",
        "Api": "API",
        "Db": "DB",
        "Dns": "DNS",
        "Ssl": "SSL",
        "Tls": "TLS",
        "Rca": "RCA",
        "Itsm": "ITSM",
        "Sso": "SSO",
        "Mfa": "MFA",
        "Kb": "KB",
        "Sop": "SOP",
        "Cmdb": "CMDB",
        "Ai": "AI",
        "Slm": "SLM",
        "Llm": "LLM",
    }
    for src, repl in acronym_map.items():
        label = re.sub(rf"\b{src}\b", repl, label)
    return label


def system_label(text: str) -> str:
    text = str(text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def natural_label(text: str) -> str:
    mapping = {
        "unknown_factor": "Unknown Contributing Factor",
        "user_feedback": "Collect User Feedback",
        "gather_more_info": "Gather Additional Details",
        "monitor_system": "Closely Monitor Affected Services",
        "review_change_logs": "Review Change Logs",
        "check_system_logs": "Check System Logs",
        "rollback_changes": "Rollback Recent Changes",
        "configuration_error": "Configuration Error",
        "recent_changes": "Recent Changes",
    }
    key = str(text or "").strip().lower()
    return mapping.get(key, nice_label(text))


def format_confidence(value, decimals: int = 2) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return str(value)


def dedupe_lines(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# Visual / UI helpers
# ---------------------------------------------------------------------------

def metric_card(label: str, value: str, variant: str = "neutral", uniform_value_sizing: bool = False):
    """
    Shared metric card with equal box size and center-aligned text.
    This version keeps the existing card layout intact while using a slightly
    smaller, more defensive font scale so longer labels fit cleanly inside
    narrow 4-column layouts such as the AI RCA summary cards.
    """
    value_text = str(value or "")
    value_len = len(value_text)
    word_count = len(value_text.split())

    # Use uniform sizing when a row of peer summary cards should look visually
    # consistent (for example the 4 AI RCA summary boxes in Telemetry Console).
    if uniform_value_sizing:
        value_font = "1.05rem"
        value_line_height = "1.10"
    else:
        # More conservative sizing so long values fit inside the same 150px card
        # without clipping or overflowing beyond the rounded border.
        if value_len >= 34 or word_count >= 5:
            value_font = "0.96rem"
            value_line_height = "1.06"
        elif value_len >= 26 or word_count >= 4:
            value_font = "1.08rem"
            value_line_height = "1.08"
        elif value_len >= 18 or word_count >= 3:
            value_font = "1.22rem"
            value_line_height = "1.10"
        elif value_len >= 12 or word_count >= 2:
            value_font = "1.48rem"
            value_line_height = "1.08"
        else:
            value_font = "1.70rem"
            value_line_height = "1.05"

    styles = {
        "neutral": {
            "border": "rgba(120,120,120,0.18)",
            "background": "linear-gradient(180deg, #ffffff 0%, #f8faff 100%)",
            "label_color": "#6b7280",
            "value_color": "#111827",
        },
        "knowledge": {
            "border": "rgba(214, 191, 107, 0.38)",
            "background": "linear-gradient(180deg, #FFFBEA 0%, #FFF8D6 100%)",
            "label_color": "#7c5f00",
            "value_color": "#111827",
        },
        "dashboard": {
            "border": "rgba(96, 165, 250, 0.35)",
            "background": "linear-gradient(180deg, #EFF6FF 0%, #EAF2FF 100%)",
            "label_color": "#1d4ed8",
            "value_color": "#111827",
        },
        "automation": {
            "border": "rgba(167, 139, 250, 0.35)",
            "background": "linear-gradient(180deg, #F5F3FF 0%, #F1EDFF 100%)",
            "label_color": "#5b21b6",
            "value_color": "#111827",
        },
    }
    s = styles.get(variant, styles["neutral"])

    st.markdown(
        f"""
        <div style="
            height: 150px;
            width: 100%;
            padding: 14px 14px;
            border: 1px solid {s['border']};
            border-radius: 18px;
            background: {s['background']};
            box-shadow: 0 2px 8px rgba(120,120,120,0.05);
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
            overflow: hidden;
            box-sizing: border-box;
        ">
            <div style="
                font-size: 0.82rem;
                color: {s['label_color']};
                line-height: 1.15;
                height: 28px;
                min-height: 28px;
                overflow: hidden;
                display: flex;
                align-items: center;
                justify-content: center;
                text-align: center;
                width: 100%;
                box-sizing: border-box;
            ">{label}</div>
            <div style="
                font-size: {value_font};
                font-weight: 750;
                margin-top: 8px;
                padding: 0 4px;
                color: {s['value_color']};
                line-height: {value_line_height};
                height: 88px;
                min-height: 88px;
                max-height: 88px;
                overflow: hidden;
                word-break: break-word;
                overflow-wrap: anywhere;
                white-space: normal;
                hyphens: auto;
                display: flex;
                align-items: center;
                justify-content: center;
                text-align: center;
                width: 100%;
                box-sizing: border-box;
            ">{value_text}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def section_title(title: str):
    st.markdown(
        f'''<div style="margin-top: 10px; margin-bottom: 8px; font-size: 1.00rem; font-weight: 700;">{title}</div>''',
        unsafe_allow_html=True,
    )


def render_empty_state(icon: str, title: str, body: str, action_hint: str = "") -> None:
    """UX-07: Consistent empty/error state card used across all pages."""
    hint_html = f'<div style="font-size:0.82rem;color:#2563eb;margin-top:6px;">{action_hint}</div>' if action_hint else ""
    st.markdown(
        f"""
        <div style="text-align:center;padding:40px 24px;color:#6b7280;border:1px dashed #d1d5db;border-radius:8px;margin:16px 0;">
          <div style="font-size:2.2rem;margin-bottom:10px;">{icon}</div>
          <div style="font-size:1.05rem;font-weight:600;color:#374151;margin-bottom:6px;">{title}</div>
          <div style="font-size:0.88rem;line-height:1.5;">{body}</div>
          {hint_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_api_error(error: Exception, context: str = "") -> None:
    """UX-07: Consistent API/backend error display."""
    ctx = f" ({context})" if context else ""
    st.error(f"Backend error{ctx}: {error}", icon="⚠️")

def render_confidence_meter(confidence_value: float, label: str = "RCA Confidence") -> None:
    """Render a colour-coded progress bar for a 0–1 confidence score."""
    pct = max(0.0, min(1.0, float(confidence_value or 0.0)))
    pct_int = int(pct * 100)
    if pct >= 0.75:
        bar_color = "#22c55e"   # green
        text_color = "#15803d"
        level = "High"
    elif pct >= 0.50:
        bar_color = "#f59e0b"   # amber
        text_color = "#b45309"
        level = "Medium"
    else:
        bar_color = "#ef4444"   # red
        text_color = "#b91c1c"
        level = "Low"
    st.markdown(
        f"""
        <div style="margin:6px 0 12px 0;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
            <span style="font-size:0.82rem;font-weight:600;color:#6b7280;">{label}</span>
            <span style="font-size:0.88rem;font-weight:700;color:{text_color};">{level} — {pct_int}%</span>
          </div>
          <div style="background:#e5e7eb;border-radius:6px;height:10px;overflow:hidden;">
            <div style="width:{pct_int}%;background:{bar_color};height:100%;border-radius:6px;transition:width 0.4s;"></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_status_badge(status: str, size: str = "normal") -> str:
    """Return HTML for a coloured status badge. status: pass/warn/fail/unknown."""
    _s = str(status or "").strip().lower()
    badge_map = {
        "pass": ("#dcfce7", "#15803d", "#16a34a", "PASS"),
        "warn": ("#fef9c3", "#854d0e", "#ca8a04", "WARN"),
        "warning": ("#fef9c3", "#854d0e", "#ca8a04", "WARN"),
        "fail": ("#fee2e2", "#991b1b", "#dc2626", "FAIL"),
        "failed": ("#fee2e2", "#991b1b", "#dc2626", "FAIL"),
        "unknown": ("#f3f4f6", "#374151", "#6b7280", "UNKNOWN"),
        "missing": ("#f3f4f6", "#374151", "#6b7280", "MISSING"),
    }
    bg, text, border, label = badge_map.get(_s, ("#f3f4f6", "#374151", "#6b7280", status.upper()))
    font = "0.95rem" if size == "large" else "0.72rem"
    pad = "5px 14px" if size == "large" else "2px 8px"
    return (
        f'<span style="background:{bg};color:{text};border:1.5px solid {border};'
        f'border-radius:20px;padding:{pad};font-size:{font};font-weight:700;'
        f'letter-spacing:0.04em;">{label}</span>'
    )


def render_automation_pipeline_steps(current_status: str) -> None:
    """Render a horizontal pipeline showing the automation lifecycle stage."""
    stages = [
        ("Queue", ["queued", "draft"]),
        ("Safety Gate", ["safety_checked", "gate_passed"]),
        ("Approved", ["approved"]),
        ("Executed", ["executed", "completed"]),
        ("Feedback", ["feedback_submitted"]),
    ]
    _s = str(current_status or "").strip().lower()

    # Find which stage is active (first stage whose statuses include current)
    active_idx = 0
    for i, (_, statuses) in enumerate(stages):
        if _s in statuses:
            active_idx = i
            break
    # Treat anything executed/completed as done (active=4)
    if _s in {"executed", "closed_remediated", "execution_failed"}:
        active_idx = 3

    html_parts = ['<div style="display:flex;align-items:center;gap:0;margin:12px 0 18px 0;overflow-x:auto;">']
    for i, (name, _) in enumerate(stages):
        if i < active_idx:
            bg, color, border = "#22c55e", "#fff", "#16a34a"
            icon = "✓"
        elif i == active_idx:
            bg, color, border = "#3b82f6", "#fff", "#2563eb"
            icon = "●"
        else:
            bg, color, border = "#f3f4f6", "#9ca3af", "#e5e7eb"
            icon = str(i + 1)

        html_parts.append(
            f'<div style="display:flex;flex-direction:column;align-items:center;min-width:90px;">'
            f'  <div style="width:32px;height:32px;border-radius:50%;background:{bg};border:2px solid {border};'
            f'       display:flex;align-items:center;justify-content:center;font-size:0.85rem;font-weight:700;color:{color};">{icon}</div>'
            f'  <div style="font-size:0.72rem;color:{"#111827" if i<=active_idx else "#9ca3af"};font-weight:{"600" if i==active_idx else "400"};'
            f'       margin-top:4px;text-align:center;">{name}</div>'
            f'</div>'
        )
        if i < len(stages) - 1:
            line_color = "#22c55e" if i < active_idx else "#e5e7eb"
            html_parts.append(
                f'<div style="flex:1;height:2px;background:{line_color};margin-bottom:18px;min-width:20px;"></div>'
            )

    html_parts.append('</div>')
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def render_evidence_ttl_warning(checked_at_str: str, evidence_type: str, warn_hours: int = 72) -> None:
    """Warn if evidence is older than warn_hours."""
    if not checked_at_str:
        return
    try:
        from datetime import datetime, timezone
        checked = datetime.fromisoformat(str(checked_at_str).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - checked).total_seconds() / 3600
        if age_hours > warn_hours:
            age_days = int(age_hours / 24)
            st.warning(
                f"Evidence is {age_days}d old (last checked {checked.strftime('%Y-%m-%d')}). "
                f"Refresh {evidence_type.replace('_', ' ')} evidence before live execution.",
                icon="⚠️",
            )
    except Exception:
        pass


def render_incident_action_banner(result: dict, approved_mapping: Optional[dict] = None) -> None:
    """Render a prominent CTA banner: Approved Automation Available vs Runbook Validation Required.

    `approved_mapping` is the registry lookup the rest of the Incident Console uses; passing it keeps this banner and
    the automation panel below it saying the same thing. Without it, the banner falls back to the analysis result.
    """
    approved_automations = [approved_mapping] if approved_mapping else (result.get("approved_automations", []) or [])
    classification = (result.get("analysis") or {}).get("classification", "")

    if approved_automations:
        auto = approved_automations[0] if isinstance(approved_automations[0], dict) else {}
        action = auto.get("action", "")
        playbook = auto.get("playbook", "")
        target = auto.get("target_host", "")
        st.markdown(
            f"""
            <div style="background:linear-gradient(135deg,#f0fdf4,#dcfce7);border:1.5px solid #16a34a;
                        border-radius:10px;padding:14px 18px;margin:10px 0 16px 0;">
              <div style="font-size:0.78rem;font-weight:700;color:#15803d;letter-spacing:0.06em;margin-bottom:6px;">
                ✅ APPROVED AUTOMATION AVAILABLE
              </div>
              <div style="font-size:1.05rem;font-weight:600;color:#111827;margin-bottom:4px;">{nice_label(action)}</div>
              <div style="font-size:0.82rem;color:#374151;">
                Playbook: <code>{playbook}</code> &nbsp;·&nbsp; Target: <code>{target}</code>
              </div>
              <div style="font-size:0.80rem;color:#6b7280;margin-top:6px;">
                Go to <strong>Automation Queue</strong> → Create Queue → Run Safety Gate → Approve → Execute
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif classification:
        st.markdown(
            """
            <div style="background:linear-gradient(135deg,#fffbeb,#fef9c3);border:1.5px solid #ca8a04;
                        border-radius:10px;padding:14px 18px;margin:10px 0 16px 0;">
              <div style="font-size:0.78rem;font-weight:700;color:#b45309;letter-spacing:0.06em;margin-bottom:6px;">
                ⚙️ RUNBOOK VALIDATION REQUIRED
              </div>
              <div style="font-size:0.90rem;color:#374151;">
                No approved automation exists for this incident type yet.
                Go to <strong>Automation Queue</strong> to create a new automation candidate.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_compact_bullet_list(items: List[Any], empty_message: str):
    cleaned = [natural_label(item) for item in (items or []) if str(item or "").strip()]
    if not cleaned:
        st.caption(empty_message)
        return
    st.markdown("\n".join(f"- {item}" for item in cleaned))


def render_resolution_playbook(
    steps: List[Dict[str, Any]],
    safety_note: str = "",
    expanded: bool = True,
    title: str = "Step-by-Step Resolution Playbook",
) -> None:
    """Shared renderer for structured resolution playbook steps.
    Used by Incident Console, Automation Queue, and Telemetry Console."""
    if not steps:
        return

    with st.expander(title, expanded=expanded):
        st.caption(
            "Follow these steps in order. Each step tells you exactly what to check, "
            "what you expect to see, and which path to take based on what you find."
        )
        if safety_note:
            st.warning(safety_note)

        for step in steps:
            step_num  = step.get("step", "")
            title_s   = step.get("title", "")
            context   = step.get("context", "")
            action    = step.get("action", "")
            commands  = step.get("commands") or []
            decisions = step.get("decision") or []
            caution   = step.get("caution", "")

            cmd_pills = "".join(
                f"<span style='display:inline-block;background:#dbeafe;color:#1e40af;"
                f"font-size:0.72rem;font-weight:700;padding:2px 8px;border-radius:12px;"
                f"margin:2px 4px 2px 0;font-family:monospace;'>{c}</span>"
                for c in commands
            )

            decision_rows = "".join(
                f"<tr>"
                f"<td style='padding:4px 10px 4px 0;font-size:0.8rem;color:#475569;"
                f"white-space:nowrap;vertical-align:top;'>If: <b>{cond}</b></td>"
                f"<td style='padding:4px 0;font-size:0.8rem;color:#1e293b;"
                f"vertical-align:top;'>{consequence}</td>"
                f"</tr>"
                for cond, consequence in (
                    (d[0], d[1]) if isinstance(d, (list, tuple)) and len(d) >= 2
                    else (str(d), "")
                    for d in decisions
                )
            )

            caution_block = (
                f"<div style='margin-top:8px;padding:6px 10px;background:#fef9c3;"
                f"border-left:3px solid #ca8a04;border-radius:4px;"
                f"font-size:0.78rem;color:#713f12;'>⚠ {caution}</div>"
            ) if caution else ""

            st.markdown(
                f"<div style='border:1px solid #e2e8f0;border-left:4px solid #3b82f6;"
                f"border-radius:8px;padding:14px 18px;margin-bottom:12px;background:#f8fafc;'>"

                f"<div style='display:flex;align-items:baseline;gap:10px;margin-bottom:6px;'>"
                f"<span style='background:#3b82f6;color:#fff;font-size:0.72rem;"
                f"font-weight:700;padding:2px 9px;border-radius:12px;'>STEP {step_num}</span>"
                f"<span style='font-size:0.95rem;font-weight:700;color:#1e293b;'>{title_s}</span>"
                f"</div>"

                f"<div style='font-size:0.8rem;color:#64748b;margin-bottom:8px;"
                f"font-style:italic;'>{context}</div>"

                f"<div style='font-size:0.83rem;color:#1e293b;margin-bottom:8px;'>"
                f"<b>Action:</b> {action}</div>"

                + (f"<div style='margin-bottom:8px;'>"
                   f"<b style='font-size:0.72rem;color:#64748b;text-transform:uppercase;"
                   f"letter-spacing:.05em;'>Tools / Commands</b><br>{cmd_pills}</div>"
                   if commands else "")

                + (f"<div style='margin-top:8px;'>"
                   f"<b style='font-size:0.72rem;color:#64748b;text-transform:uppercase;"
                   f"letter-spacing:.05em;'>Decision</b>"
                   f"<table style='margin-top:4px;border-collapse:collapse;width:100%;'>"
                   f"{decision_rows}</table></div>"
                   if decision_rows else "")

                + caution_block
                + "</div>",
                unsafe_allow_html=True,
            )


def routing_display_label(method: str) -> str:
    mapping = {
        "hybrid_embedding": "Hybrid Retrieval",
        "hybrid_embedding_consensus": "Hybrid Retrieval + Consensus",
        "retrieval_consensus": "Retrieval Consensus",
        "embedding": "Embedding Retrieval",
        "keyword": "Keyword Match",
        "fallback": "Fallback LLM",
        "unknown": "Unknown",
    }
    return mapping.get(str(method or "").strip().lower(), nice_label(method))


def decision_display_label(decision: str) -> str:
    mapping = {
        "local_primary": "Primary path selected",
        "fallback_required": "Fallback path selected",
        "error": "Error",
    }
    return mapping.get(str(decision or "").strip().lower(), nice_label(decision))


def shorten_text(text: str, max_chars: int = 145) -> tuple:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + "...", True


def lines_to_list(text: str) -> List[str]:
    return dedupe_lines([line.strip() for line in str(text or "").splitlines()])


def join_lines(values: List[str]) -> str:
    return "\n".join([str(v).strip() for v in values if str(v).strip()])


def clear_lightweight_page_cache() -> None:
    try:
        st.cache_data.clear()
    except Exception:
        pass
