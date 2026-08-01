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


def _metric_card(label: str, value: str) -> None:
    st.markdown(
        f"""
        <div style="
            padding: 12px 14px;
            border: 1px solid rgba(120,120,120,0.18);
            border-radius: 14px;
            background: rgba(255,255,255,0.02);
            min-height: 76px;
        ">
            <div style="font-size: 0.80rem; opacity: 0.72;">{label}</div>
            <div style="font-size: 1.20rem; font-weight: 700; margin-top: 6px;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard(compact: bool = False) -> None:
    confidence_rows = _safe_read_jsonl(_log_file("confidence_log.jsonl"))
    fallback_rows = _safe_read_jsonl(_log_file("fallback_log.jsonl"))
    automation_rows = _safe_read_json(_data_file("automation_executions.json"), default=[])
    telemetry_rows = _safe_read_json(_data_file("telemetry_incidents.json"), default=[])

    if not isinstance(automation_rows, list):
        automation_rows = []
    if not isinstance(telemetry_rows, list):
        telemetry_rows = []

    total_analyses = len(confidence_rows)
    fallback_used = sum(1 for r in confidence_rows if bool(r.get("fallback_used", False)))
    avg_conf = round(sum(float(r.get("display_confidence", r.get("primary_confidence", 0.0)) or 0.0) for r in confidence_rows) / total_analyses, 3) if total_analyses else 0.0
    escalations = sum(1 for r in confidence_rows if bool(r.get("escalation", False)))
    auto_exec = len(automation_rows)
    telemetry_incidents = len(telemetry_rows)

    cols = st.columns(4 if compact else 6)
    metrics = [
        ("Analyses", str(total_analyses)),
        ("Avg Confidence", f"{avg_conf:.2f}"),
        ("Fallback Used", str(fallback_used)),
        ("Escalations", str(escalations)),
        ("Telemetry Incidents", str(telemetry_incidents)),
        ("Automation Runs", str(auto_exec)),
    ]
    for col, (label, value) in zip(cols, metrics):
        with col:
            _metric_card(label, value)

    if compact:
        return

    st.markdown("---")
    left, right = st.columns(2)

    with left:
        st.subheader("Top Classifications")
        class_counts = Counter()
        for row in confidence_rows:
            cls = str(row.get("classification") or "unknown")
            class_counts[cls] += 1
        if class_counts:
            st.table([{"classification": k, "count": v} for k, v in class_counts.most_common(10)])
        else:
            st.caption("No confidence history available yet.")

        st.subheader("Automation Status")
        if automation_rows:
            status_counts = Counter(str(r.get("status") or "unknown") for r in automation_rows)
            st.table([{"status": k, "count": v} for k, v in status_counts.most_common()])
        else:
            st.caption("No automation executions logged yet.")

    with right:
        st.subheader("Telemetry Trigger Summary")
        if telemetry_rows:
            telem_counts = Counter(str(r.get("classification") or "unknown") for r in telemetry_rows)
            st.table([{"classification": k, "count": v} for k, v in telem_counts.most_common(10)])
        else:
            st.caption("No telemetry-triggered incidents yet.")

        st.subheader("Recent Confidence Samples")
        if confidence_rows:
            recent = confidence_rows[-10:]
            st.table(
                [
                    {
                        "request_id": str(r.get("request_id") or "")[:8],
                        "classification": r.get("classification"),
                        "confidence": round(float(r.get("display_confidence", r.get("primary_confidence", 0.0)) or 0.0), 3),
                        "fallback": bool(r.get("fallback_used", False)),
                        "decision": r.get("decision"),
                    }
                    for r in reversed(recent)
                ]
            )
        else:
            st.caption("No confidence events logged yet.")
