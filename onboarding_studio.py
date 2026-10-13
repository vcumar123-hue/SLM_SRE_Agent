"""
onboarding_studio.py

Client Onboarding Studio for AI Ops Copilot.
Uses HTML/CSS navigation cards instead of Streamlit buttons for stable equal sizing.

Updated:
- Sources page now defaults to Smart Bulk Upload.
- Bulk files are auto-profiled and auto-classified using filename / extension / lightweight content signals.
- Manual upload remains available as a secondary fallback.
- Review Overrides allows per-file correction before confirming the source inventory.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from typing import Any, Dict, Iterable, List, Tuple

import streamlit as st


SOURCE_TYPES = [
    "Ticket Dump",
    "KB / SOP Documents",
    "CMDB / Asset Export",
    "Topology",
    "Monitoring Export",
    "Other",
]

ONBOARDING_STEPS = [
    ("▦", "Overview", "", "overview"),
    ("🔐", "Sources", "", "sources"),
    ("🔒", "Taxonomy", "", "taxonomy"),
    ("🔒", "Knowledge", "Build", "knowledge_build"),
    ("🔒", "Playbook", "Factory", "playbook_factory"),
    ("🔒", "Readiness", "", "readiness"),
    ("🔒", "Go Live", "", "go_live"),
]


def _tenant_display_name(tenant_id: str) -> str:
    """Display name from tenant_config/<tenant_id>.json, or the tenant id when there is none."""
    try:
        from pathlib import Path
        cfg = json.loads((Path(__file__).parent / "tenant_config" / f"{tenant_id}.json").read_text(encoding="utf-8-sig"))
        return cfg.get("display_name") or tenant_id
    except Exception:
        return tenant_id


def init_onboarding_state() -> None:
    st.session_state.setdefault("onboarding_active_step", "overview")
    # The workspace starts on the tenant selected in the sidebar (still editable below).
    _active_tenant = st.session_state.get("tenant_id") or "client_a"
    st.session_state.setdefault("onboarding_client_name", _tenant_display_name(_active_tenant))
    st.session_state.setdefault("onboarding_tenant_id", _active_tenant)
    st.session_state.setdefault(
        "onboarding_status",
        {
            "sources": "not_started",
            "taxonomy": "locked",
            "knowledge_build": "locked",
            "playbook_factory": "locked",
            "readiness": "locked",
            "go_live": "locked",
        },
    )
    st.session_state.setdefault("onboarding_uploaded_sources", [])
    st.session_state.setdefault("onboarding_profile", {})
    st.session_state.setdefault("onboarding_taxonomy", [])
    st.session_state.setdefault("onboarding_build_logs", [])
    st.session_state.setdefault("onboarding_playbooks", [])
    st.session_state.setdefault("onboarding_readiness_score", 0)
    st.session_state.setdefault("onboarding_source_mode", "Smart Bulk Upload")
    st.session_state.setdefault("onboarding_source_inventory", [])
    st.session_state.setdefault("onboarding_source_overrides", {})
    st.session_state.setdefault("onboarding_external_knowledge_contribution", "Disabled")
    st.session_state.setdefault("onboarding_knowledge_governance", build_knowledge_governance_policy("Disabled"))


def is_step_enabled(step_key: str) -> bool:
    if step_key in {"overview", "sources"}:
        return True
    return st.session_state.onboarding_status.get(step_key) != "locked"


def display_icon(icon: str, key: str) -> str:
    if key in {"overview", "sources"}:
        return icon
    status = st.session_state.onboarding_status.get(key, "locked")
    if status == "complete":
        return "✅"
    if status == "ready":
        return "🔓"
    if status == "running":
        return "⏳"
    return "🔒"


def mark_step_complete(step_key: str) -> None:
    st.session_state.onboarding_status[step_key] = "complete"

    if step_key == "sources":
        st.session_state.onboarding_status["taxonomy"] = "ready"
        st.session_state.onboarding_status["knowledge_build"] = "ready"
    elif step_key == "taxonomy":
        if st.session_state.onboarding_status.get("knowledge_build") == "complete":
            st.session_state.onboarding_status["playbook_factory"] = "ready"
    elif step_key == "knowledge_build":
        if st.session_state.onboarding_status.get("taxonomy") == "complete":
            st.session_state.onboarding_status["playbook_factory"] = "ready"
    elif step_key == "playbook_factory":
        st.session_state.onboarding_status["readiness"] = "ready"
    elif step_key == "readiness":
        st.session_state.onboarding_status["go_live"] = "ready"


def render_onboarding_styles() -> None:
    st.markdown(
        """
        <style>
        .onboarding-hero {
            background: linear-gradient(180deg,#ffffff 0%,#f8fbff 100%);
            border: 1px solid rgba(120,120,120,0.14);
            border-radius: 22px;
            padding: 24px 28px;
            box-shadow: 0 6px 18px rgba(15,23,42,0.05);
            margin-bottom: 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 24px;
        }
        .onboarding-hero-title {
            font-size: 1.85rem;
            font-weight: 800;
            color: #111827;
            margin-bottom: 6px;
            letter-spacing: -0.02em;
        }
        .onboarding-hero-meta {
            font-size: 1rem;
            color: #4b5563;
            margin-bottom: 10px;
        }
        .onboarding-hero-subtitle {
            font-size: 0.92rem;
            color: #6b7280;
            line-height: 1.45;
        }
        .onboarding-ring-wrap {
            text-align: center;
            min-width: 140px;
        }
        .onboarding-ring {
            width: 108px;
            height: 108px;
            margin: auto;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .onboarding-ring-inner {
            width: 78px;
            height: 78px;
            border-radius: 50%;
            background: white;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            box-shadow: inset 0 0 0 1px rgba(120,120,120,0.08);
        }
        .onboarding-ring-score {
            font-size: 1.35rem;
            font-weight: 800;
            color: #111827;
        }
        .onboarding-ring-label {
            font-size: 0.72rem;
            color: #6b7280;
        }
        .onboarding-ring-caption {
            font-size: 0.78rem;
            color: #6b7280;
            margin-top: 8px;
        }
        .onboard-grid {
            display: grid;
            grid-template-columns: repeat(7, minmax(0, 1fr));
            gap: 18px;
            align-items: stretch;
            margin: 1.7rem 0 1.25rem 0;
        }
        .onboard-card {
            height: 82px;
            min-height: 82px;
            max-height: 82px;
            border-radius: 14px;
            border: 1px solid rgba(120,120,120,0.18);
            background: linear-gradient(180deg, #ffffff 0%, #f8faff 100%);
            color: #2f3747 !important;
            text-decoration: none !important;
            display: grid;
            grid-template-rows: 24px 22px 22px;
            align-content: center;
            justify-items: center;
            text-align: center;
            padding: 8px 10px;
            box-sizing: border-box;
            overflow: hidden;
        }
        .onboard-card.disabled {
            color: #a1a1aa !important;
            background: linear-gradient(180deg, #ffffff 0%, #fafafa 100%);
            pointer-events: none;
        }
        .onboard-card.active {
            background: linear-gradient(180deg, #eef4ff 0%, #f7faff 100%);
            border: 1px solid rgba(60,90,160,0.38);
        }
        .onboard-card-icon {
            font-size: 1.0rem;
            line-height: 1;
            height: 21px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .onboard-card-line {
            font-size: 0.92rem;
            font-weight: 600;
            line-height: 1.05;
            height: 21px;
            display: flex;
            align-items: center;
            justify-content: center;
            white-space: nowrap;
        }
        .onboard-underline {
            height: 4px;
            background: #2563eb;
            border-radius: 4px;
            margin-top: 8px;
        }
        .source-hero-card {
            border: 1px solid rgba(37,99,235,0.22);
            background: linear-gradient(180deg,#ffffff 0%,#f8fbff 100%);
            border-radius: 18px;
            padding: 20px 22px;
            box-shadow: 0 8px 20px rgba(15,23,42,0.05);
            margin: 10px 0 16px 0;
        }
        .source-drop-card {
            border: 1.5px dashed rgba(37,99,235,0.42);
            background: #f8fbff;
            border-radius: 16px;
            padding: 24px 22px;
            margin-top: 12px;
            margin-bottom: 16px;
        }
        .source-drop-title {
            font-size: 1.1rem;
            font-weight: 800;
            color: #111827;
            margin-bottom: 4px;
        }
        .source-drop-subtitle {
            font-size: 0.92rem;
            color: #4b5563;
        }
        .source-pipeline {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 12px;
            margin: 14px 0 16px 0;
        }
        .source-pipe-step {
            border: 1px solid rgba(120,120,120,0.14);
            border-radius: 14px;
            background: #ffffff;
            padding: 12px 10px;
            text-align: center;
            box-shadow: 0 3px 10px rgba(15,23,42,0.035);
        }
        .source-pipe-icon {
            font-size: 1.15rem;
            margin-bottom: 4px;
        }
        .source-pipe-label {
            font-size: 0.82rem;
            color: #374151;
            font-weight: 700;
        }
        .source-tip {
            border: 1px solid rgba(37,99,235,0.26);
            background: #f8fbff;
            border-radius: 14px;
            padding: 13px 15px;
            color: #1f3a8a;
            font-size: 0.9rem;
            margin-top: 12px;
        }
        .source-stat-card {
            border: 1px solid rgba(120,120,120,0.16);
            background: #ffffff;
            border-radius: 16px;
            padding: 17px 18px;
            box-shadow: 0 6px 16px rgba(15,23,42,0.04);
        }
        .source-stat-title {
            font-size: 1rem;
            font-weight: 800;
            color: #111827;
            margin-bottom: 12px;
        }
        .source-stat-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0;
            border-bottom: 1px solid rgba(229,231,235,0.82);
            color: #4b5563;
            font-size: 0.9rem;
        }
        .source-stat-row:last-child { border-bottom: 0; }
        .source-stat-value {
            font-weight: 800;
            color: #2563eb;
            font-size: 1.08rem;
        }
        .source-manual-note {
            border: 1px solid rgba(245,158,11,0.25);
            background: #fffbeb;
            border-radius: 14px;
            padding: 13px 15px;
            color: #92400e;
            font-size: 0.9rem;
            margin-bottom: 14px;
        }
        .governance-card {
            border: 1px solid rgba(120,120,120,0.16);
            background: #ffffff;
            border-radius: 16px;
            padding: 18px 20px;
            box-shadow: 0 4px 14px rgba(15,23,42,0.035);
            margin: 10px 0 18px 0;
        }
        .governance-title {
            font-size: 1.0rem;
            font-weight: 800;
            color: #111827;
            margin-bottom: 10px;
        }
        .governance-summary-row {
            display: grid;
            grid-template-columns: minmax(260px, 0.9fr) minmax(240px, 1.1fr);
            gap: 18px;
            padding: 8px 0;
            border-bottom: 1px solid rgba(229,231,235,0.82);
            color: #4b5563;
            font-size: 0.9rem;
        }
        .governance-summary-row:last-child { border-bottom: 0; }
        .governance-value-yes { color: #15803d; font-weight: 700; }
        .governance-value-no { color: #b91c1c; font-weight: 700; }
        .governance-note {
            color: #6b7280;
            font-size: 0.88rem;
            line-height: 1.35;
            margin-top: 10px;
        }
        @media (max-width: 1150px) {
            .onboard-grid { gap: 10px; }
            .onboard-card-line { font-size: 0.82rem; }
            .source-pipeline { grid-template-columns: 1fr; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def apply_step_from_query() -> None:
    try:
        step = st.query_params.get("onboarding_step")
        valid = {item[3] for item in ONBOARDING_STEPS}
        if step in valid and is_step_enabled(step):
            st.session_state.onboarding_active_step = step
    except Exception:
        pass


def render_onboarding_header() -> None:
    client = st.session_state.get("onboarding_client_name", "PFG")
    tenant = st.session_state.get("onboarding_tenant_id", "client_a")
    score = int(st.session_state.get("onboarding_readiness_score", 0) or 0)
    score = max(0, min(100, score))
    ring_color = "#16a34a" if score >= 75 else "#2563eb" if score >= 50 else "#f59e0b"
    ring_degrees = score * 3.6

    header_html = (
        '<div class="onboarding-hero">'
        '<div>'
        '<div class="onboarding-hero-title">Client Onboarding Studio</div>'
        f'<div class="onboarding-hero-meta">Client: <b>{client}</b> &nbsp;&nbsp;|&nbsp;&nbsp; Tenant: <b>{tenant}</b></div>'
        '<div class="onboarding-hero-subtitle">AI Ops Onboarding Workspace · Knowledge Build · Playbook Factory · Go-Live Readiness</div>'
        '</div>'
        '<div class="onboarding-ring-wrap">'
        f'<div class="onboarding-ring" style="background: conic-gradient({ring_color} {ring_degrees}deg, #e5e7eb 0deg);">'
        '<div class="onboarding-ring-inner">'
        f'<div class="onboarding-ring-score">{score}%</div>'
        '<div class="onboarding-ring-label">Ready</div>'
        '</div>'
        '</div>'
        '<div class="onboarding-ring-caption">Onboarding Score</div>'
        '</div>'
        '</div>'
    )
    st.markdown(header_html, unsafe_allow_html=True)


def render_step_nav() -> None:
    cards = []
    active = st.session_state.onboarding_active_step

    for icon, line1, line2, key in ONBOARDING_STEPS:
        enabled = is_step_enabled(key)
        shown_icon = display_icon(icon, key)
        active_cls = " active" if active == key else ""
        disabled_cls = " disabled" if not enabled else ""
        href = f"?page=Client%20Onboarding%20Studio&onboarding_step={key}"
        card_tag = "a" if enabled else "div"
        href_attr = f' href="{href}" target="_self"' if enabled else ""
        second_line = line2 if line2 else "&nbsp;"
        underline = '<div class="onboard-underline"></div>' if active == key else '<div style="height:12px;"></div>'
        card = (
            '<div>'
            f'<{card_tag} class="onboard-card{active_cls}{disabled_cls}"{href_attr}>'
            f'<div class="onboard-card-icon">{shown_icon}</div>'
            f'<div class="onboard-card-line">{line1}</div>'
            f'<div class="onboard-card-line">{second_line}</div>'
            f'</{card_tag}>'
            f'{underline}'
            '</div>'
        )
        cards.append(card)

    nav_html = '<div class="onboard-grid">' + ''.join(cards) + '</div>'
    st.markdown(nav_html, unsafe_allow_html=True)


def render_overview() -> None:
    st.subheader("Overview")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sources Uploaded", len(st.session_state.onboarding_uploaded_sources))
    c2.metric("Taxonomy", st.session_state.onboarding_status.get("taxonomy", "locked").title())
    c3.metric("Knowledge Build", st.session_state.onboarding_status.get("knowledge_build", "locked").title())
    c4.metric("Draft Playbooks", len(st.session_state.onboarding_playbooks))
    st.info(
        "Start with Sources. The recommended path is Smart Bulk Upload: drop a client export package, "
        "auto-detect source types, review exceptions, and confirm the source inventory."
    )
    rows = [
        {
            "Step": (line1 + (" " + line2 if line2 else "")).strip(),
            "Status": st.session_state.onboarding_status.get(key, "ready" if key in {"overview", "sources"} else "locked"),
            "Enabled": is_step_enabled(key),
        }
        for _, line1, line2, key in ONBOARDING_STEPS[1:]
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _safe_read_sample(file_obj: Any, max_bytes: int = 80_000) -> bytes:
    try:
        return file_obj.getvalue()[:max_bytes]
    except Exception:
        return b""


def _decode_sample(sample: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return sample.decode(enc, errors="ignore")
        except Exception:
            continue
    return ""


def _extract_zip_members(file_obj: Any) -> List[Dict[str, Any]]:
    """Return virtual file entries for zip uploads. Content is sampled only for classification."""
    entries: List[Dict[str, Any]] = []
    try:
        raw = file_obj.getvalue()
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    with zf.open(info, "r") as member:
                        sample = member.read(80_000)
                except Exception:
                    sample = b""
                entries.append(
                    {
                        "name": info.filename,
                        "size_kb": round(info.file_size / 1024, 1),
                        "sample_bytes": sample,
                        "source_container": getattr(file_obj, "name", "uploaded.zip"),
                    }
                )
    except Exception:
        entries.append(
            {
                "name": getattr(file_obj, "name", "uploaded.zip"),
                "size_kb": round(len(_safe_read_sample(file_obj, 10_000_000)) / 1024, 1),
                "sample_bytes": _safe_read_sample(file_obj),
                "source_container": "",
            }
        )
    return entries


def _normalize_uploaded_files(files: Iterable[Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for f in files or []:
        name = getattr(f, "name", "uploaded_file")
        if name.lower().endswith(".zip"):
            entries.extend(_extract_zip_members(f))
        else:
            raw = _safe_read_sample(f, 10_000_000)
            entries.append(
                {
                    "name": name,
                    "size_kb": round(len(raw) / 1024, 1),
                    "sample_bytes": _safe_read_sample(f),
                    "source_container": "",
                }
            )
    return entries


def _score_source_type(filename: str, sample_text: str) -> Tuple[str, int, List[str]]:
    """Lightweight heuristic classifier for onboarding source files.

    This is intentionally dependency-free so the UI can run in demo mode.
    In production, this should call the ingestion profiler / metadata classifier.
    """
    lower_name = filename.lower()
    text = sample_text.lower()
    combined = f"{lower_name} {text[:12000]}"
    ext = lower_name.rsplit(".", 1)[-1] if "." in lower_name else ""

    patterns = {
        "Ticket Dump": [
            r"ticket", r"incident", r"inc\d+", r"servicenow", r"remedy", r"jira",
            r"priority", r"assignment group", r"short description", r"resolution", r"resolved_at",
            r"opened_at", r"closed_at", r"caller", r"category", r"subcategory",
        ],
        "KB / SOP Documents": [
            r"\bkb\b", r"knowledge", r"sop", r"runbook", r"procedure", r"troubleshoot",
            r"remediation", r"how to", r"step 1", r"root cause", r"workaround",
            r"standard operating", r"operating procedure",
        ],
        "CMDB / Asset Export": [
            r"cmdb", r"asset", r"ci name", r"configuration item", r"hostname", r"serial",
            r"environment", r"owner", r"business service", r"support group", r"ip address",
            r"application owner", r"discovery source",
        ],
        "Topology": [
            r"topology", r"dependency", r"upstream", r"downstream", r"service map",
            r"node", r"edge", r"graph", r"relationship", r"depends_on", r"connected_to",
            r"network map", r"app map",
        ],
        "Monitoring Export": [
            r"prometheus", r"grafana", r"alertmanager", r"monitoring", r"metric",
            r"threshold", r"severity", r"alertname", r"cpu", r"memory", r"disk", r"latency",
            r"exporter", r"rule", r"labels", r"annotations",
        ],
    }

    ext_hints = {
        "Ticket Dump": {"xlsx", "xls", "csv", "json"},
        "KB / SOP Documents": {"pdf", "docx", "pptx", "md", "txt"},
        "CMDB / Asset Export": {"xlsx", "xls", "csv", "json"},
        "Topology": {"json", "yaml", "yml", "xml", "csv"},
        "Monitoring Export": {"yaml", "yml", "json", "csv", "txt"},
    }

    scores: Dict[str, int] = {}
    reasons: Dict[str, List[str]] = {}
    for source_type, pats in patterns.items():
        score = 0
        matched: List[str] = []
        for pat in pats:
            if re.search(pat, combined):
                score += 12 if pat in lower_name else 7
                matched.append(pat.replace("\\b", ""))
        if ext in ext_hints[source_type]:
            score += 8
            matched.append(f".{ext}")
        scores[source_type] = score
        reasons[source_type] = matched[:4]

    best_type, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score <= 11:
        return "Other", 52, ["low signal"]

    # Convert heuristic score to a bounded confidence.
    confidence = min(96, max(61, 58 + best_score))
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) > 1 and sorted_scores[0] - sorted_scores[1] < 8:
        confidence = min(confidence, 78)

    if confidence < 65:
        return "Other", confidence, reasons.get(best_type, ["low confidence"])
    return best_type, confidence, reasons.get(best_type, [])


def profile_uploaded_sources(files: Iterable[Any]) -> List[Dict[str, Any]]:
    entries = _normalize_uploaded_files(files)
    profiled: List[Dict[str, Any]] = []

    for entry in entries:
        filename = entry.get("name", "")
        sample_text = _decode_sample(entry.get("sample_bytes", b""))
        detected_type, confidence, reasons = _score_source_type(filename, sample_text)
        status = "Ready" if confidence >= 75 and detected_type != "Other" else "Review Needed"
        override = st.session_state.onboarding_source_overrides.get(filename, "Keep Detected")

        final_type = detected_type if override in {"Keep Detected", "", None} else override
        final_status = "Ready" if final_type != "Other" and (confidence >= 75 or override not in {"Keep Detected", "", None}) else status

        profiled.append(
            {
                "File": filename,
                "Detected Type": detected_type,
                "Final Type": final_type,
                "Confidence": confidence,
                "Status": final_status,
                "Override": override,
                "Size KB": entry.get("size_kb", 0),
                "Signals": ", ".join(reasons) if reasons else "filename/content profile",
                "Container": entry.get("source_container", ""),
            }
        )

    return profiled


def compute_source_summary(inventory: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(inventory)
    ready = sum(1 for row in inventory if row.get("Status") == "Ready")
    review = total - ready
    avg_conf = round(sum(int(row.get("Confidence", 0) or 0) for row in inventory) / total) if total else 0
    by_type: Dict[str, int] = {}
    for row in inventory:
        typ = row.get("Final Type") or row.get("Detected Type") or "Other"
        by_type[typ] = by_type.get(typ, 0) + 1
    return {
        "files": total,
        "auto_classified": ready,
        "needs_review": review,
        "avg_confidence": avg_conf,
        "by_type": by_type,
    }


def _inventory_for_display(inventory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for row in inventory:
        rows.append(
            {
                "File": row.get("File"),
                "Detected Type": row.get("Detected Type"),
                "Final Type": row.get("Final Type"),
                "Confidence %": row.get("Confidence"),
                "Status": row.get("Status"),
                "Override": row.get("Override", "Keep Detected"),
                "Signals": row.get("Signals"),
                "Size KB": row.get("Size KB"),
            }
        )
    return rows


def apply_inventory_overrides(edited_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    original_by_file = {row.get("File"): dict(row) for row in st.session_state.onboarding_source_inventory}
    updated: List[Dict[str, Any]] = []

    for edited in edited_rows:
        filename = edited.get("File")
        row = original_by_file.get(filename, dict(edited))
        override = edited.get("Override", "Keep Detected")
        detected = row.get("Detected Type", "Other")
        confidence = int(row.get("Confidence", edited.get("Confidence %", 0)) or 0)

        if override and override != "Keep Detected":
            final_type = override
        else:
            final_type = detected

        status = "Ready" if final_type != "Other" and (confidence >= 75 or override != "Keep Detected") else "Review Needed"

        row.update(
            {
                "Final Type": final_type,
                "Override": override,
                "Status": status,
            }
        )
        updated.append(row)

    return updated




def build_knowledge_governance_policy(external_contribution: str = "Disabled") -> Dict[str, Any]:
    """Return tenant governance defaults for onboarding-generated tenant config."""
    enabled = str(external_contribution or "Disabled").strip().lower() == "enabled"
    return {
        "external_knowledge_contribution": "enabled" if enabled else "disabled",
        "allow_base_release_import": True,
        "allow_global_pattern_usage": True,
        "allow_client_data_export": False,
        "allow_sanitized_pattern_export": bool(enabled),
        "allow_cross_client_learning_contribution": bool(enabled),
        "export_approval_required": True,
        "local_learning_enabled": True,
        "client_local_backtesting_enabled": True,
        "global_backtesting_enabled": bool(enabled),
    }


def yes_no(value: Any) -> str:
    return "Yes" if bool(value) else "No"


def render_knowledge_governance_panel() -> None:
    st.markdown(
        '''
        <div class="governance-card">
            <div class="governance-title">Knowledge Governance &amp; Data Boundary</div>
            <div style="color:#6b7280;font-size:0.90rem;margin-bottom:10px;">
                Define whether this client can contribute sanitized operational patterns outside the client environment.
            </div>
        </div>
        ''',
        unsafe_allow_html=True,
    )

    current = st.session_state.get("onboarding_external_knowledge_contribution", "Disabled")
    selected = st.radio(
        "External Knowledge Contribution",
        options=["Disabled", "Enabled"],
        index=0 if current != "Enabled" else 1,
        horizontal=True,
        key="onboarding_external_knowledge_contribution_radio",
        help=(
            "Disabled keeps client data and local learned patterns inside the client environment. "
            "Enabled allows only sanitized, reviewed pattern candidates to be exported; raw data remains blocked."
        ),
    )
    st.session_state.onboarding_external_knowledge_contribution = selected
    policy = build_knowledge_governance_policy(selected)
    st.session_state.onboarding_knowledge_governance = policy

    rows = [
        ("Use global enterprise patterns", yes_no(policy["allow_global_pattern_usage"]), True),
        ("Local learning inside client environment", yes_no(policy["local_learning_enabled"]), True),
        ("Export approval required", yes_no(policy["export_approval_required"]), True),
        ("Raw client data export", yes_no(policy["allow_client_data_export"]), False),
        ("Sanitized pattern export", yes_no(policy["allow_sanitized_pattern_export"]), policy["allow_sanitized_pattern_export"]),
        ("Cross-client learning contribution", yes_no(policy["allow_cross_client_learning_contribution"]), policy["allow_cross_client_learning_contribution"]),
        ("Client-local backtesting", yes_no(policy["client_local_backtesting_enabled"]), True),
        ("Global backtesting contribution", yes_no(policy["global_backtesting_enabled"]), policy["global_backtesting_enabled"]),
    ]
    html_rows = []
    for label, value, positive in rows:
        cls = "governance-value-yes" if positive else "governance-value-no"
        html_rows.append(
            f'<div class="governance-summary-row"><span>{label}</span><span class="{cls}">{value}</span></div>'
        )
    note = (
        "When disabled, client data and local learned patterns remain inside the client environment. "
        "The client can still consume imported global enterprise patterns and run client-local backtesting."
        if selected == "Disabled"
        else
        "When enabled, only sanitized and reviewed operational patterns may be exported. Raw tickets, documents, hostnames, IPs, URLs, user names, app names, and client identifiers remain excluded."
    )
    st.markdown(
        '<div class="governance-card">' + ''.join(html_rows) + f'<div class="governance-note">{note}</div></div>',
        unsafe_allow_html=True,
    )

def confirm_source_inventory() -> None:
    inventory = st.session_state.onboarding_source_inventory
    confirmed = []
    for row in inventory:
        confirmed.append(
            {
                "name": row.get("File"),
                "source_type": row.get("Final Type") or row.get("Detected Type"),
                "detected_type": row.get("Detected Type"),
                "confidence": row.get("Confidence"),
                "size_kb": row.get("Size KB"),
                "status": "profiled",
                "signals": row.get("Signals"),
                "container": row.get("Container", ""),
            }
        )

    st.session_state.onboarding_uploaded_sources = confirmed
    summary = compute_source_summary(inventory)
    st.session_state.onboarding_profile = {
        "files": summary["files"],
        "estimated_records": max(0, summary["files"] * 12500),
        "quality_score": summary["avg_confidence"],
        "auto_classified": summary["auto_classified"],
        "needs_review": summary["needs_review"],
        "source_mix": summary["by_type"],
        "knowledge_governance": st.session_state.get("onboarding_knowledge_governance") or build_knowledge_governance_policy(st.session_state.get("onboarding_external_knowledge_contribution", "Disabled")),
    }
    mark_step_complete("sources")


def render_source_pipeline() -> None:
    st.markdown(
        """
        <div class="source-pipeline">
            <div class="source-pipe-step"><div class="source-pipe-icon">🔎</div><div class="source-pipe-label">Scan Files</div></div>
            <div class="source-pipe-step"><div class="source-pipe-icon">🎯</div><div class="source-pipe-label">Detect Type</div></div>
            <div class="source-pipe-step"><div class="source-pipe-icon">📄</div><div class="source-pipe-label">Profile Content</div></div>
            <div class="source-pipe-step"><div class="source-pipe-icon">📊</div><div class="source-pipe-label">Assign Confidence</div></div>
            <div class="source-pipe-step"><div class="source-pipe-icon">📋</div><div class="source-pipe-label">Review Inventory</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_source_summary_card(summary: Dict[str, Any]) -> None:
    st.markdown(
        f"""
        <div class="source-stat-card">
            <div class="source-stat-title">Upload Summary</div>
            <div class="source-stat-row"><span>Files Scanned</span><span class="source-stat-value">{summary.get("files", 0)}</span></div>
            <div class="source-stat-row"><span>Auto-classified</span><span class="source-stat-value" style="color:#16a34a;">{summary.get("auto_classified", 0)}</span></div>
            <div class="source-stat-row"><span>Needs Review</span><span class="source-stat-value" style="color:#f97316;">{summary.get("needs_review", 0)}</span></div>
            <div class="source-stat-row"><span>Average Confidence</span><span class="source-stat-value">{summary.get("avg_confidence", 0)}%</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_smart_bulk_upload() -> None:
    st.markdown(
        """
        <div class="source-hero-card">
            <div style="font-size:1.05rem;font-weight:800;color:#111827;">Smart Bulk Upload</div>
            <div style="font-size:0.92rem;color:#4b5563;margin-top:4px;">
                Drop a client export package, zip file, or multiple files. The platform auto-detects source type,
                assigns confidence, and asks for manual review only where needed.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    files = st.file_uploader(
        "Drop folder export, zip package, or multiple files",
        accept_multiple_files=True,
        type=["zip", "xlsx", "xls", "csv", "json", "pdf", "docx", "pptx", "txt", "md", "png", "jpg", "jpeg", "yaml", "yml", "xml"],
        help="Recommended: ask the client for one folder export or zip package. Zip files will be scanned file-by-file.",
    )

    st.markdown(
        """
        <div class="source-drop-card">
            <div class="source-drop-title">📁 Upload once. Categorize automatically.</div>
            <div class="source-drop-subtitle">
                Supported source examples: ticket dumps, KB/SOP documents, CMDB exports, topology files, monitoring exports, and mixed operational folders.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    render_source_pipeline()

    col_a, col_b = st.columns([1, 1])
    with col_a:
        auto_profile_clicked = st.button(
            "✨ Auto Profile Sources",
            type="primary",
            use_container_width=True,
            disabled=not files,
        )
    with col_b:
        demo_clicked = st.button("Load Demo Smart Inventory", use_container_width=True)

    if auto_profile_clicked:
        st.session_state.onboarding_status["sources"] = "running"
        progress = st.progress(0)
        log = st.empty()
        stages = ["Scanning files", "Detecting source types", "Profiling content", "Assigning confidence", "Preparing review inventory"]
        for idx, stage in enumerate(stages):
            log.write(stage)
            progress.progress(int(((idx + 1) / len(stages)) * 100))
            time.sleep(0.18)
        st.session_state.onboarding_source_inventory = profile_uploaded_sources(files or [])
        st.session_state.onboarding_status["sources"] = "not_started"
        st.success("Sources profiled. Review the inventory and confirm source classification.")
        st.rerun()

    if demo_clicked:
        st.session_state.onboarding_source_inventory = [
            {"File": "Ticket Dump Q4.xlsx", "Detected Type": "Ticket Dump", "Final Type": "Ticket Dump", "Confidence": 96, "Status": "Ready", "Override": "Keep Detected", "Size KB": 5120, "Signals": "ticket, incident, priority, resolution", "Container": ""},
            {"File": "VPN_Runbook.docx", "Detected Type": "KB / SOP Documents", "Final Type": "KB / SOP Documents", "Confidence": 91, "Status": "Ready", "Override": "Keep Detected", "Size KB": 840, "Signals": "runbook, troubleshoot, remediation", "Container": ""},
            {"File": "cmdb_export.csv", "Detected Type": "CMDB / Asset Export", "Final Type": "CMDB / Asset Export", "Confidence": 94, "Status": "Ready", "Override": "Keep Detected", "Size KB": 1400, "Signals": "cmdb, ci name, hostname, owner", "Container": ""},
            {"File": "app_topology.json", "Detected Type": "Topology", "Final Type": "Topology", "Confidence": 89, "Status": "Ready", "Override": "Keep Detected", "Size KB": 760, "Signals": "topology, dependency, node, edge", "Container": ""},
            {"File": "alerts_export.yml", "Detected Type": "Monitoring Export", "Final Type": "Monitoring Export", "Confidence": 92, "Status": "Ready", "Override": "Keep Detected", "Size KB": 620, "Signals": "prometheus, alertmanager, threshold", "Container": ""},
            {"File": "misc_notes.pdf", "Detected Type": "Other", "Final Type": "Other", "Confidence": 52, "Status": "Review Needed", "Override": "Keep Detected", "Size KB": 210, "Signals": "low signal", "Container": ""},
        ]
        st.rerun()

    inventory = st.session_state.onboarding_source_inventory
    if inventory:
        st.markdown("### Review Source Inventory")
        st.caption("Override only the rows that need correction. Low-confidence rows should be reviewed before confirming.")

        left, right = st.columns([4, 1.3])
        with left:
            edited = st.data_editor(
                _inventory_for_display(inventory),
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                column_config={
                    "Override": st.column_config.SelectboxColumn(
                        "Override",
                        options=["Keep Detected"] + SOURCE_TYPES,
                        help="Use override only when auto-detection is wrong or low confidence.",
                    ),
                    "Confidence %": st.column_config.ProgressColumn(
                        "Confidence %",
                        min_value=0,
                        max_value=100,
                        format="%d%%",
                    ),
                    "Status": st.column_config.TextColumn("Status", disabled=True),
                    "Detected Type": st.column_config.TextColumn("Detected Type", disabled=True),
                    "Final Type": st.column_config.TextColumn("Final Type", disabled=True),
                    "Signals": st.column_config.TextColumn("Signals", disabled=True),
                    "Size KB": st.column_config.NumberColumn("Size KB", disabled=True),
                },
                key="smart_source_inventory_editor",
            )
            st.session_state.onboarding_source_inventory = apply_inventory_overrides(edited)
        with right:
            summary = compute_source_summary(st.session_state.onboarding_source_inventory)
            render_source_summary_card(summary)

        c1, c2, c3 = st.columns([1.2, 1.2, 1])
        with c1:
            if st.button("✅ Confirm Source Inventory", type="primary", use_container_width=True):
                summary = compute_source_summary(st.session_state.onboarding_source_inventory)
                if summary["needs_review"] > 0:
                    st.warning("Some files still need review. Override low-confidence rows or confirm manually after review.")
                else:
                    confirm_source_inventory()
                    st.success("Source inventory confirmed. Taxonomy and Knowledge Build are now enabled.")
                    st.rerun()
        with c2:
            if st.button("Confirm With Exceptions Noted", use_container_width=True):
                confirm_source_inventory()
                st.warning("Source inventory confirmed with exceptions noted. Review these during taxonomy and knowledge build.")
                st.rerun()
        with c3:
            if st.button("Clear Inventory", use_container_width=True):
                st.session_state.onboarding_source_inventory = []
                st.rerun()

        st.markdown(
            """
            <div class="source-tip">
                💡 Default experience: bulk upload first, manual categorization only for exceptions.
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_manual_upload() -> None:
    st.markdown(
        """
        <div class="source-manual-note">
            Manual Upload is retained for controlled category-specific loading. Use this only when the source type is already known.
        </div>
        """,
        unsafe_allow_html=True,
    )
    source_type = st.selectbox("Known Source Type", SOURCE_TYPES, key="manual_source_type")
    files = st.file_uploader(
        "Browse files or drag & drop",
        accept_multiple_files=True,
        type=["xlsx", "xls", "csv", "json", "pdf", "docx", "pptx", "txt", "md", "png", "jpg", "jpeg", "yaml", "yml", "xml", "zip"],
        key="manual_source_files",
    )

    if files:
        st.markdown("### Selected files")
        for f in files:
            st.write(f"• {f.name} · {round(len(f.getvalue()) / 1024, 1)} KB")

    if st.button("Upload as Selected Source Type", type="primary", use_container_width=True, disabled=not files):
        uploaded = []
        for file in files or []:
            uploaded.append(
                {
                    "name": file.name,
                    "source_type": source_type,
                    "detected_type": source_type,
                    "confidence": 100,
                    "size_kb": round(len(file.getvalue()) / 1024, 1),
                    "status": "profiled",
                    "signals": "manual source type selection",
                    "container": "",
                }
            )
        st.session_state.onboarding_uploaded_sources.extend(uploaded)
        st.session_state.onboarding_source_inventory.extend(
            [
                {
                    "File": row["name"],
                    "Detected Type": row["source_type"],
                    "Final Type": row["source_type"],
                    "Confidence": row["confidence"],
                    "Status": "Ready",
                    "Override": row["source_type"],
                    "Size KB": row["size_kb"],
                    "Signals": row["signals"],
                    "Container": "",
                }
                for row in uploaded
            ]
        )
        summary = compute_source_summary(st.session_state.onboarding_source_inventory)
        st.session_state.onboarding_profile = {
            "files": len(st.session_state.onboarding_uploaded_sources),
            "estimated_records": len(st.session_state.onboarding_uploaded_sources) * 12500,
            "quality_score": summary.get("avg_confidence", 100),
            "auto_classified": summary.get("auto_classified", 0),
            "needs_review": summary.get("needs_review", 0),
            "source_mix": summary.get("by_type", {}),
        }
        mark_step_complete("sources")
        st.success("Manual source upload completed. Taxonomy and Knowledge Build are now enabled.")
        st.rerun()


def render_review_overrides() -> None:
    inventory = st.session_state.onboarding_source_inventory
    if not inventory:
        st.info("No source inventory is available yet. Use Smart Bulk Upload first or load the demo inventory.")
        return

    st.markdown("### Review Overrides")
    st.caption("Use this screen to correct low-confidence or misclassified files before Knowledge Build.")

    edited = st.data_editor(
        _inventory_for_display(inventory),
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        column_config={
            "Override": st.column_config.SelectboxColumn(
                "Override",
                options=["Keep Detected"] + SOURCE_TYPES,
            ),
            "Confidence %": st.column_config.ProgressColumn(
                "Confidence %",
                min_value=0,
                max_value=100,
                format="%d%%",
            ),
        },
        key="source_override_editor",
    )
    st.session_state.onboarding_source_inventory = apply_inventory_overrides(edited)

    summary = compute_source_summary(st.session_state.onboarding_source_inventory)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Files", summary["files"])
    c2.metric("Ready", summary["auto_classified"])
    c3.metric("Needs Review", summary["needs_review"])
    c4.metric("Avg Confidence", f"{summary['avg_confidence']}%")

    if st.button("Confirm Reviewed Inventory", type="primary", use_container_width=True):
        confirm_source_inventory()
        st.success("Reviewed source inventory confirmed.")
        st.rerun()


def render_existing_source_inventory() -> None:
    if not st.session_state.onboarding_uploaded_sources:
        return

    st.markdown("### Confirmed Source Inventory")
    st.dataframe(st.session_state.onboarding_uploaded_sources, use_container_width=True, hide_index=True)

    profile = st.session_state.onboarding_profile
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Files", profile.get("files", 0))
    p2.metric("Estimated Records", f"{profile.get('estimated_records', 0):,}")
    p3.metric("Data Quality", f"{profile.get('quality_score', 0)}%")
    p4.metric("Needs Review", profile.get("needs_review", 0))

    governance = profile.get("knowledge_governance") or st.session_state.get("onboarding_knowledge_governance")
    if governance:
        st.markdown("#### Knowledge Governance")
        st.dataframe(
            [
                {"Policy": "External Knowledge Contribution", "Value": str(governance.get("external_knowledge_contribution", "disabled")).title()},
                {"Policy": "Use Global Enterprise Patterns", "Value": yes_no(governance.get("allow_global_pattern_usage", True))},
                {"Policy": "Local Learning Inside Client Environment", "Value": yes_no(governance.get("local_learning_enabled", True))},
                {"Policy": "Raw Client Data Export", "Value": yes_no(governance.get("allow_client_data_export", False))},
                {"Policy": "Sanitized Pattern Export", "Value": yes_no(governance.get("allow_sanitized_pattern_export", False))},
                {"Policy": "Global Backtesting Contribution", "Value": yes_no(governance.get("global_backtesting_enabled", False))},
            ],
            use_container_width=True,
            hide_index=True,
        )

    source_mix = profile.get("source_mix", {})
    if source_mix:
        st.markdown("#### Source Mix")
        st.dataframe(
            [{"Source Type": key, "Files": value} for key, value in source_mix.items()],
            use_container_width=True,
            hide_index=True,
        )


def render_sources() -> None:
    st.subheader("1. Sources")
    st.caption(
        "Recommended flow: upload one client export package or mixed set of files, let the platform auto-classify them, "
        "then manually review only exceptions."
    )

    st.session_state.onboarding_client_name = st.text_input("Client name", value=st.session_state.onboarding_client_name)
    st.session_state.onboarding_tenant_id = st.text_input("Tenant ID", value=st.session_state.onboarding_tenant_id)

    render_knowledge_governance_panel()

    tab1, tab2, tab3 = st.tabs(["Smart Bulk Upload", "Manual Upload", "Review Overrides"])
    with tab1:
        render_smart_bulk_upload()
    with tab2:
        render_manual_upload()
    with tab3:
        render_review_overrides()

    render_existing_source_inventory()


def render_taxonomy() -> None:
    st.subheader("2. Taxonomy")
    st.caption("Map client-specific terms to standard AI Ops classifications.")
    if not is_step_enabled("taxonomy"):
        st.warning("Complete Sources first.")
        return

    if not st.session_state.onboarding_taxonomy:
        st.session_state.onboarding_taxonomy = [
            {"Client Term": "DNS slow / name lookup delay", "Standard Classification": "dns_resolution_delay", "Domain": "network", "Approved": False},
            {"Client Term": "VPN user cannot connect", "Standard Classification": "vpn_client_failure", "Domain": "network", "Approved": False},
            {"Client Term": "Disk full / filesystem high", "Standard Classification": "disk_space_exhaustion", "Domain": "compute", "Approved": False},
            {"Client Term": "SSO login loop", "Standard Classification": "browser_sso_loop", "Domain": "iam", "Approved": False},
            {"Client Term": "MFT payload not sent", "Standard Classification": "file_transfer_integration_failure", "Domain": "integration", "Approved": False},
        ]

    edited = st.data_editor(st.session_state.onboarding_taxonomy, use_container_width=True, hide_index=True, num_rows="dynamic")
    if st.button("Approve Taxonomy Mapping", type="primary", use_container_width=True):
        st.session_state.onboarding_taxonomy = edited
        mark_step_complete("taxonomy")
        st.success("Taxonomy approved.")
        st.rerun()


def render_knowledge_build() -> None:
    st.subheader("3. Knowledge Build")
    st.caption("Run RAG ingestion, chunking, embedding, Qdrant upsert, and metadata registration.")
    if not is_step_enabled("knowledge_build"):
        st.warning("Complete Sources first.")
        return

    source_counts = st.session_state.onboarding_profile.get("source_mix", {})
    if source_counts:
        st.markdown("#### Confirmed Inputs for Knowledge Build")
        st.dataframe(
            [{"Source Type": key, "Files": value} for key, value in source_counts.items()],
            use_container_width=True,
            hide_index=True,
        )

    build_options = st.multiselect(
        "Build options",
        ["Parse ticket dumps", "Parse KB/SOP docs", "Parse topology / CMDB", "Generate chunks", "Create embeddings", "Upsert Qdrant", "Write Postgres metadata"],
        default=["Parse ticket dumps", "Parse KB/SOP docs", "Generate chunks", "Create embeddings", "Upsert Qdrant", "Write Postgres metadata"],
    )

    if st.button("Start Knowledge Build", type="primary", use_container_width=True):
        st.session_state.onboarding_status["knowledge_build"] = "running"
        progress = st.progress(0)
        logs = st.empty()
        build_logs = []
        for idx, step in enumerate(build_options or []):
            msg = f"{step} completed"
            build_logs.append(msg)
            logs.write("\n".join(build_logs))
            progress.progress(int(((idx + 1) / max(len(build_options), 1)) * 100))
            time.sleep(0.2)
        st.session_state.onboarding_build_logs = build_logs
        mark_step_complete("knowledge_build")
        st.success("Knowledge build completed.")
        st.rerun()

    if st.session_state.onboarding_build_logs:
        st.code("\n".join(st.session_state.onboarding_build_logs))


def render_playbook_factory() -> None:
    st.subheader("4. Playbook Factory")
    st.caption("Generate starter playbooks from ticket history and create reviewable engineer procedures.")
    if not is_step_enabled("playbook_factory"):
        st.warning("Complete Taxonomy and Knowledge Build first.")
        return

    top_n = st.slider("Generate top N playbooks", min_value=10, max_value=100, value=25, step=5)
    if st.button("Generate Draft Playbooks", type="primary", use_container_width=True):
        examples = [
            ("dns_resolution_delay", "Network", "DNS lookup latency and resolver timeout"),
            ("disk_space_exhaustion", "Compute", "Filesystem threshold breach"),
            ("vpn_client_failure", "Network", "VPN user unable to connect"),
            ("file_transfer_integration_failure", "Integration", "MFT payload missing or duplicate"),
            ("browser_sso_loop", "IAM", "SSO redirect loop"),
        ]
        st.session_state.onboarding_playbooks = [
            {
                "Playbook ID": f"PB-DRAFT-{idx+1:03d}",
                "Classification": examples[idx % len(examples)][0],
                "Domain": examples[idx % len(examples)][1],
                "Description": examples[idx % len(examples)][2],
                "Status": "Draft - Engineer Validation Required",
                "Automation Level": "Level 1 after QA",
            }
            for idx in range(top_n)
        ]
        mark_step_complete("playbook_factory")
        st.success(f"{top_n} draft playbooks generated.")
        st.rerun()

    if st.session_state.onboarding_playbooks:
        st.dataframe(st.session_state.onboarding_playbooks, use_container_width=True, hide_index=True)


def render_readiness() -> None:
    st.subheader("5. Readiness")
    if not is_step_enabled("readiness"):
        st.warning("Complete Playbook Factory first.")
        return

    source_score = 100 if st.session_state.onboarding_status.get("sources") == "complete" else 0
    taxonomy_score = 100 if st.session_state.onboarding_status.get("taxonomy") == "complete" else 0
    kb_score = 100 if st.session_state.onboarding_status.get("knowledge_build") == "complete" else 0
    playbook_score = min(100, len(st.session_state.onboarding_playbooks) * 4)
    review_penalty = min(20, int(st.session_state.onboarding_profile.get("needs_review", 0) or 0) * 5)
    readiness = int((source_score * 0.25) + (taxonomy_score * 0.20) + (kb_score * 0.30) + (playbook_score * 0.25) - review_penalty)
    readiness = max(0, min(100, readiness))
    st.session_state.onboarding_readiness_score = readiness

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sources", f"{source_score}%")
    c2.metric("Taxonomy", f"{taxonomy_score}%")
    c3.metric("Knowledge Build", f"{kb_score}%")
    c4.metric("Playbooks", f"{playbook_score}%")
    st.progress(readiness / 100)
    st.markdown(f"### Overall readiness: {readiness}%")

    if review_penalty:
        st.warning(f"Readiness includes a {review_penalty}% penalty because source exceptions were confirmed with review notes.")

    if readiness >= 75:
        st.success("Client onboarding is ready for controlled go-live.")
        if st.button("Approve Readiness", type="primary", use_container_width=True):
            mark_step_complete("readiness")
            st.rerun()
    else:
        st.warning("Readiness is below go-live threshold. Complete remaining steps.")


def render_go_live() -> None:
    st.subheader("6. Go Live")
    if not is_step_enabled("go_live"):
        st.warning("Approve readiness first.")
        return

    checks = {
        "Incident Console enabled": st.checkbox("Incident Console enabled", value=True),
        "RAG retrieval enabled": st.checkbox("RAG retrieval enabled", value=True),
        "Fallback LLM enabled": st.checkbox("Fallback LLM enabled", value=True),
        "Automation queue enabled": st.checkbox("Automation queue enabled", value=False),
        "Approved playbooks only for automation": st.checkbox("Approved playbooks only for automation", value=True),
    }

    if st.button("Complete Go Live Setup", type="primary", use_container_width=True):
        if all(checks.values()):
            mark_step_complete("go_live")
            st.success("Go-live checklist completed.")
        else:
            st.warning("All checks must be enabled before go-live.")


def render_client_onboarding_studio() -> None:
    init_onboarding_state()
    render_onboarding_styles()
    apply_step_from_query()
    render_onboarding_header()
    render_step_nav()
    st.markdown("---")

    step = st.session_state.onboarding_active_step
    if step == "overview":
        render_overview()
    elif step == "sources":
        render_sources()
    elif step == "taxonomy":
        render_taxonomy()
    elif step == "knowledge_build":
        render_knowledge_build()
    elif step == "playbook_factory":
        render_playbook_factory()
    elif step == "readiness":
        render_readiness()
    elif step == "go_live":
        render_go_live()
    else:
        render_overview()


if __name__ == "__main__":
    st.set_page_config(page_title="Client Onboarding Studio", layout="wide")
    render_client_onboarding_studio()
