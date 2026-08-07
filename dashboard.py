from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st


def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
            except Exception:
                continue
    except Exception:
        return rows
    return rows


def _data_file(name: str) -> Path:
    return Path("data") / name


def _log_file(name: str) -> Path:
    return Path("logs") / name


def _metric_card(label: str, value: str, icon: str, accent: str, sublabel: str = "") -> None:
    st.markdown(
        f"""
        <div style="
            padding: 16px 18px;
            border-radius: 14px;
            background: linear-gradient(135deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.01) 100%);
            border: 1px solid rgba(120,120,120,0.15);
            border-left: 4px solid {accent};
            min-height: 90px;
            display: flex; flex-direction: column; justify-content: space-between;
        ">
            <div style="display:flex; align-items:center; gap:8px; opacity:0.72; font-size:0.78rem; font-weight:500; letter-spacing:0.04em; text-transform:uppercase;">
                <span style="font-size:1rem;">{icon}</span>{label}
            </div>
            <div style="font-size:1.85rem; font-weight:800; letter-spacing:-0.02em; color:{accent}; line-height:1.1; margin-top:6px;">{value}</div>
            {f'<div style="font-size:0.72rem; opacity:0.50; margin-top:4px;">{sublabel}</div>' if sublabel else ''}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _section_header(icon: str, title: str) -> None:
    st.markdown(
        f"""
        <div style="display:flex; align-items:center; gap:10px; margin:24px 0 12px 0;">
            <span style="font-size:1.2rem;">{icon}</span>
            <span style="font-size:1.05rem; font-weight:700; letter-spacing:-0.01em;">{title}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _confidence_color(conf: float) -> str:
    if conf >= 0.75:
        return "🟢"
    if conf >= 0.45:
        return "🟡"
    return "🔴"


def _status_badge(status: str) -> str:
    s = status.lower()
    if "complete" in s:
        return "✅"
    if "fail" in s or "error" in s:
        return "❌"
    if "running" in s or "progress" in s:
        return "⏳"
    return "⚪"


def render_dashboard(compact: bool = False) -> None:
    confidence_rows = _safe_read_jsonl(_log_file("confidence_log.jsonl"))
    fallback_rows   = _safe_read_jsonl(_log_file("fallback_log.jsonl"))
    automation_rows = _safe_read_json(_data_file("automation_executions.json"), default=[])
    telemetry_rows  = _safe_read_json(_data_file("telemetry_incidents.json"), default=[])

    if not isinstance(automation_rows, list):
        automation_rows = []
    if not isinstance(telemetry_rows, list):
        telemetry_rows = []

    total_analyses     = len(confidence_rows)
    fallback_used      = sum(1 for r in confidence_rows if bool(r.get("fallback_used", False)))
    avg_conf           = (
        round(sum(float(r.get("display_confidence", r.get("primary_confidence", 0.0)) or 0.0)
                  for r in confidence_rows) / total_analyses, 3)
        if total_analyses else 0.0
    )
    escalations        = sum(1 for r in confidence_rows if bool(r.get("escalation", False)))
    auto_exec          = len(automation_rows)
    telemetry_incidents = len(telemetry_rows)
    fallback_rate      = f"{fallback_used / total_analyses * 100:.0f}% of analyses" if total_analyses else ""

    # ── Metric cards ──────────────────────────────────────────────────
    cols = st.columns(4 if compact else 6)
    cards = [
        ("Analyses",           str(total_analyses),       "🔍", "#4F8EF7", ""),
        ("Avg Confidence",     f"{avg_conf:.2f}",          "📊", "#10B981" if avg_conf >= 0.6 else "#F59E0B" if avg_conf >= 0.4 else "#EF4444", ""),
        ("Fallback Used",      str(fallback_used),         "🔄", "#F59E0B", fallback_rate),
        ("Escalations",        str(escalations),           "⚠️", "#EF4444", ""),
        ("Telemetry Incidents", str(telemetry_incidents),  "📡", "#8B5CF6", ""),
        ("Automation Runs",    str(auto_exec),             "⚙️", "#06B6D4", ""),
    ]
    for col, (label, value, icon, accent, sublabel) in zip(cols, cards):
        with col:
            _metric_card(label, value, icon, accent, sublabel)

    if compact:
        return

    st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)

    # ── Main content — two-column layout ─────────────────────────────
    left, right = st.columns(2, gap="large")

    # LEFT — Classifications + Automation
    with left:
        _section_header("🏷️", "Top Classifications")
        class_counts = Counter()
        for row in confidence_rows:
            cls = str(row.get("classification") or "unknown")
            class_counts[cls] += 1

        if class_counts:
            top = class_counts.most_common(10)
            labels = [k.replace("_", " ").title() for k, _ in top]
            counts = [v for _, v in top]
            max_count = max(counts) if counts else 1

            st.markdown("<div style='display:flex; flex-direction:column; gap:5px;'>", unsafe_allow_html=True)
            for label_txt, count in zip(labels, counts):
                pct = count / max_count * 100
                st.markdown(
                    f"""
                    <div style="display:flex; align-items:center; gap:10px; font-size:0.82rem;">
                        <div style="width:160px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; opacity:0.85;">{label_txt}</div>
                        <div style="flex:1; background:rgba(79,142,247,0.12); border-radius:4px; height:10px;">
                            <div style="width:{pct:.0f}%; background:#4F8EF7; border-radius:4px; height:10px;"></div>
                        </div>
                        <div style="width:32px; text-align:right; font-weight:600; color:#4F8EF7;">{count}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.caption("No confidence history available yet.")

        _section_header("⚙️", "Automation Status")
        if automation_rows:
            status_counts = Counter(str(r.get("status") or "unknown") for r in automation_rows)
            rows_display = [
                {"": _status_badge(k), "Status": k.replace("_", " ").title(), "Count": v}
                for k, v in status_counts.most_common()
            ]
            st.dataframe(
                rows_display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "": st.column_config.TextColumn(width="small"),
                    "Count": st.column_config.ProgressColumn(
                        min_value=0, max_value=max(v for _, v in status_counts.most_common()),
                        format="%d",
                    ),
                },
            )
        else:
            st.caption("No automation executions logged yet.")

    # RIGHT — Telemetry + Confidence samples
    with right:
        _section_header("📡", "Telemetry Trigger Summary")
        if telemetry_rows:
            telem_counts = Counter(str(r.get("classification") or "unknown") for r in telemetry_rows)
            top_telem    = telem_counts.most_common(10)
            max_t        = max(v for _, v in top_telem) if top_telem else 1

            st.markdown("<div style='display:flex; flex-direction:column; gap:5px;'>", unsafe_allow_html=True)
            for cls, cnt in top_telem:
                pct = cnt / max_t * 100
                st.markdown(
                    f"""
                    <div style="display:flex; align-items:center; gap:10px; font-size:0.82rem;">
                        <div style="width:160px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; opacity:0.85;">{cls.replace("_"," ").title()}</div>
                        <div style="flex:1; background:rgba(139,92,246,0.12); border-radius:4px; height:10px;">
                            <div style="width:{pct:.0f}%; background:#8B5CF6; border-radius:4px; height:10px;"></div>
                        </div>
                        <div style="width:24px; text-align:right; font-weight:600; color:#8B5CF6;">{cnt}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.caption("No telemetry-triggered incidents yet.")

        _section_header("📈", "Recent Confidence Samples")
        if confidence_rows:
            recent = list(reversed(confidence_rows[-10:]))
            rows_display = []
            for r in recent:
                conf = round(float(r.get("display_confidence", r.get("primary_confidence", 0.0)) or 0.0), 3)
                rows_display.append({
                    "ID":             str(r.get("request_id") or "")[:8],
                    "Classification": str(r.get("classification") or "—").replace("_", " ").title(),
                    "Confidence":     conf,
                    "Fallback":       "✅" if bool(r.get("fallback_used", False)) else "—",
                    "Decision":       str(r.get("decision") or "—"),
                })
            st.dataframe(
                rows_display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Confidence": st.column_config.ProgressColumn(
                        min_value=0.0, max_value=1.0, format="%.3f",
                    ),
                    "Fallback": st.column_config.TextColumn(width="small"),
                },
            )
        else:
            st.caption("No confidence events logged yet.")
