from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st


# ── Data helpers ──────────────────────────────────────────────────────────────
def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _pg_read_resolutions() -> List[Dict[str, Any]]:
    """Read resolutions from PostgreSQL; returns [] if PG unavailable."""
    try:
        from db_utils import postgres_read_enabled, postgres_read_rows
        if not postgres_read_enabled():
            return []
        rows = postgres_read_rows("SELECT * FROM resolutions ORDER BY updated_at DESC LIMIT 500")
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


def _pg_read_playbooks() -> List[Dict[str, Any]]:
    """Read playbooks from PostgreSQL; returns [] if PG unavailable."""
    try:
        from db_utils import postgres_read_enabled, postgres_read_rows
        if not postgres_read_enabled():
            return []
        rows = postgres_read_rows("SELECT * FROM playbooks ORDER BY updated_at DESC LIMIT 500")
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


def _pg_read_automation_candidates() -> List[Dict[str, Any]]:
    """Read automation candidates from PostgreSQL; returns [] if PG unavailable."""
    try:
        from db_utils import postgres_read_enabled, postgres_read_rows
        if not postgres_read_enabled():
            return []
        rows = postgres_read_rows("SELECT * FROM automation_candidates ORDER BY updated_at DESC LIMIT 500")
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


def _safe_read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
    p = Path("logs") / name
    return p if p.exists() else Path(name)


# ── UI primitives ─────────────────────────────────────────────────────────────
def _metric_card(label: str, value: str, icon: str, accent: str,
                 sublabel: str = "", cta: str = "") -> None:
    sub_html = f"<div style='font-size:0.72rem;opacity:0.55;margin-top:4px;'>{sublabel}</div>" if sublabel else ""
    cta_html = (
        f"<div style='font-size:0.72rem;color:{accent};font-weight:600;"
        f"margin-top:6px;letter-spacing:.02em;'>{cta}</div>"
    ) if cta else ""
    st.markdown(
        f"""<div style="
            padding:16px 18px;border-radius:14px;
            background:linear-gradient(135deg,rgba(255,255,255,0.04) 0%,rgba(255,255,255,0.01) 100%);
            border:1px solid rgba(120,120,120,0.15);border-left:4px solid {accent};
            min-height:90px;display:flex;flex-direction:column;justify-content:space-between;">
            <div style="display:flex;align-items:center;gap:8px;opacity:.72;font-size:.78rem;
                 font-weight:500;letter-spacing:.04em;text-transform:uppercase;">
                <span style="font-size:1rem;">{icon}</span>{label}
            </div>
            <div style="font-size:1.85rem;font-weight:800;letter-spacing:-.02em;
                 color:{accent};line-height:1.1;margin-top:6px;">{value}</div>
            {sub_html}{cta_html}
        </div>""",
        unsafe_allow_html=True,
    )


def _section_header(icon: str, title: str, subtitle: str = "") -> None:
    sub = f"<div style='font-size:.78rem;opacity:.55;margin-top:2px;'>{subtitle}</div>" if subtitle else ""
    st.markdown(
        f"""<div style="display:flex;align-items:flex-start;gap:10px;margin:24px 0 12px;">
            <span style="font-size:1.2rem;margin-top:2px;">{icon}</span>
            <div>
              <div style="font-size:1.05rem;font-weight:700;letter-spacing:-.01em;">{title}</div>
              {sub}
            </div>
        </div>""",
        unsafe_allow_html=True,
    )


def _bar_row(label: str, count: int, max_count: int, accent: str, suffix: str = "") -> None:
    pct = count / max_count * 100 if max_count else 0
    st.markdown(
        f"""<div style="display:flex;align-items:center;gap:10px;font-size:.82rem;margin-bottom:5px;">
            <div style="width:150px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
                 opacity:.85;">{label}</div>
            <div style="flex:1;background:rgba({accent},0.12);border-radius:4px;height:10px;">
                <div style="width:{pct:.0f}%;background:rgb({accent});border-radius:4px;height:10px;"></div>
            </div>
            <div style="width:36px;text-align:right;font-weight:600;color:rgb({accent});">
                {count}{suffix}</div>
        </div>""",
        unsafe_allow_html=True,
    )


def _pending_item(label: str, count: int, accent: str, action: str) -> None:
    if count == 0:
        return
    st.markdown(
        f"""<div style="display:flex;align-items:center;justify-content:space-between;
            padding:8px 12px;border-radius:8px;border:1px solid rgba(120,120,120,.15);
            margin-bottom:6px;background:rgba(255,255,255,.02);">
            <div>
              <span style="font-size:.82rem;font-weight:600;">{label}</span>
              <span style="font-size:.75rem;opacity:.6;margin-left:6px;">{action}</span>
            </div>
            <span style="font-size:.9rem;font-weight:800;color:{accent};">{count}</span>
        </div>""",
        unsafe_allow_html=True,
    )


# ── SLA config — target resolution times per category keyword ─────────────────
# Keys are lowercase substrings matched against classification; first match wins.
_SLA_THRESHOLDS: Dict[str, int] = {
    "p1":             15,
    "critical":       15,
    "outage":         20,
    "sev1":           15,
    "sev2":           30,
    "login":          30,
    "auth":           30,
    "password":       30,
    "vpn":            45,
    "network":        45,
    "connectivity":   45,
    "disk":           60,
    "storage":        60,
    "cpu":            60,
    "memory":         60,
    "performance":    60,
    "sap":            45,
    "database":       60,
    "db":             60,
    "patch":         120,
    "update":        120,
    "deployment":    120,
}
_SLA_DEFAULT_MINS = 60   # default SLA target when no keyword matches


def _sla_target(classification: str) -> int:
    """Return SLA target minutes for a classification."""
    cls = classification.lower()
    for keyword, mins in _SLA_THRESHOLDS.items():
        if keyword in cls:
            return mins
    return _SLA_DEFAULT_MINS


# ── MTTR helper ───────────────────────────────────────────────────────────────
_AVG_MANUAL_MINS = 47   # baseline: manual resolution without AI

def _compute_mttr(executions: List[Dict]) -> Dict[str, Any]:
    durations = []
    for e in executions:
        start = e.get("queued_at") or e.get("created_at")
        end   = e.get("completed_at") or e.get("resolved_at")
        if start and end:
            try:
                t0 = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                mins = (t1 - t0).total_seconds() / 60
                if 0 < mins < 480:
                    durations.append(mins)
            except Exception:
                pass
    if durations:
        avg = sum(durations) / len(durations)
        return {"avg_mins": round(avg, 1), "count": len(durations)}
    return {"avg_mins": None, "count": 0}


# ── Main render ───────────────────────────────────────────────────────────────
def render_dashboard(compact: bool = False) -> None:
    # PG is primary; JSON is fallback so the dashboard works without a DB connection
    resolutions = _pg_read_resolutions() or _safe_read_json(_data_file("resolutions.json"), [])
    playbooks   = _pg_read_playbooks()   or _safe_read_json(_data_file("playbooks.json"), [])
    candidates  = _pg_read_automation_candidates() or _safe_read_json(_data_file("automation_candidates.json"), [])
    executions        = _safe_read_json(_data_file("automation_executions.json"), [])
    telemetry_rows    = _safe_read_json(_data_file("telemetry_incidents.json"), [])
    confidence_rows   = _safe_read_jsonl(_log_file("confidence_log.jsonl"))

    if not isinstance(resolutions,    list): resolutions    = []
    if not isinstance(playbooks,      list): playbooks      = []
    if not isinstance(candidates,     list): candidates     = []
    if not isinstance(executions,     list): executions     = []
    if not isinstance(telemetry_rows, list): telemetry_rows = []

    # ── Business KPIs ─────────────────────────────────────────────────
    total_resolved   = len(resolutions)
    total_playbooks  = len(playbooks)
    auto_executions  = len([e for e in executions if str(e.get("status") or "").lower() in
                            ("completed", "success", "done")])
    pending_approval = len([c for c in candidates if str(c.get("status") or "").lower()
                            in ("draft", "pending", "needs_approval")])
    enabled_auto     = len([c for c in candidates if str(c.get("status") or "").lower()
                            in ("enabled", "approved", "ready", "automation_ready")])
    telemetry_count  = len(telemetry_rows)

    mttr             = _compute_mttr(executions)
    mttr_display     = f"{mttr['avg_mins']:.0f} min" if mttr["avg_mins"] else "—"
    time_saved_mins  = int(auto_executions * max(0, _AVG_MANUAL_MINS - (mttr["avg_mins"] or _AVG_MANUAL_MINS)))
    time_saved_hrs   = f"{time_saved_mins // 60}h {time_saved_mins % 60}m" if time_saved_mins >= 60 else f"{time_saved_mins}m"

    # ── Top row: 6 business metric cards ─────────────────────────────
    cols = st.columns(4 if compact else 6)
    cards = [
        ("Incidents Resolved",   str(total_resolved),     "✅", "#10B981",
         f"{auto_executions} automated", "View Resolution Library →" if not compact else ""),
        ("Playbooks",            str(total_playbooks),    "📚", "#4F8EF7",
         "validated runbooks", ""),
        ("Automations Ready",    str(enabled_auto),       "⚙️", "#06B6D4",
         "approved for execution", ""),
        ("Pending Approval",     str(pending_approval),   "⏳", "#F59E0B",
         "needs engineer review", "Review in Resolution Queue →" if not compact else ""),
        ("Avg MTTR",             mttr_display,            "⏱️",
         "#10B981" if mttr["avg_mins"] and mttr["avg_mins"] < 15 else "#F59E0B",
         f"vs ~{_AVG_MANUAL_MINS}min manual", ""),
        ("Time Saved",           time_saved_hrs,          "🚀", "#8B5CF6",
         f"est. across {auto_executions} auto-runs", ""),
    ]
    for col, (label, value, icon, accent, sublabel, cta) in zip(cols, cards):
        with col:
            _metric_card(label, value, icon, accent, sublabel, cta)

    if compact:
        return

    st.markdown("<div style='margin-top:28px;'></div>", unsafe_allow_html=True)

    # ── Needs Attention ───────────────────────────────────────────────
    if pending_approval > 0 or telemetry_count > 0:
        _section_header("🔔", "Needs Attention", "Items requiring engineer action now")
        _pending_item("Automation candidates awaiting approval", pending_approval,
                      "#F59E0B", "→ Resolution Queue")
        _pending_item("Telemetry-triggered incidents", telemetry_count,
                      "#EF4444", "→ Telemetry Console")

    st.markdown("<div style='margin-top:4px;'></div>", unsafe_allow_html=True)

    # ── Two-column main body ──────────────────────────────────────────
    left, right = st.columns(2, gap="large")

    # LEFT — Top incident categories + resolution rate
    with left:
        _section_header("🏷️", "Top Incident Categories",
                         "By volume — resolutions in knowledge base")
        class_counts = Counter()
        for r in resolutions:
            cls = str(r.get("classification") or r.get("incident_type") or "unknown")
            class_counts[cls] += 1
        # Also add confidence log entries not yet in resolutions
        for r in confidence_rows:
            cls = str(r.get("classification") or "unknown")
            if cls not in class_counts:
                class_counts[cls] += 1
            else:
                class_counts[cls] += 0  # already counted

        if class_counts:
            top = class_counts.most_common(8)
            max_c = top[0][1] if top else 1
            for cls, cnt in top:
                label = cls.replace("_", " ").title()
                _bar_row(label, cnt, max_c, "79,142,247")
        else:
            st.caption("No resolution data yet. Submit and resolve incidents to build the library.")

        # ── Automation pipeline health ─────────────────────────────
        _section_header("⚙️", "Automation Health",
                         "Candidate lifecycle across the approval pipeline")
        pipeline_states = Counter(
            str(c.get("status") or "unknown").lower() for c in candidates
        )
        state_map = {
            "draft":            ("📝 Draft",        "#94A3B8"),
            "pending":          ("⏳ Pending",       "#F59E0B"),
            "needs_approval":   ("🔍 Review",        "#F59E0B"),
            "approved":         ("✅ Approved",       "#10B981"),
            "enabled":          ("🟢 Enabled",        "#10B981"),
            "automation_ready": ("🤖 Auto-Ready",     "#06B6D4"),
            "draft_automation": ("🔧 Draft Auto",     "#8B5CF6"),
        }
        if pipeline_states:
            for state, count in pipeline_states.most_common():
                label, color = state_map.get(state, (state.replace("_"," ").title(), "#94A3B8"))
                st.markdown(
                    f"<div style='display:flex;justify-content:space-between;align-items:center;"
                    f"padding:5px 8px;border-radius:6px;margin-bottom:4px;font-size:.82rem;"
                    f"background:rgba(255,255,255,.03);'>"
                    f"<span style='color:{color};font-weight:600;'>{label}</span>"
                    f"<span style='font-weight:700;color:{color};'>{count}</span></div>",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("No automation candidates yet.")

    # RIGHT — MTTR trend + recent resolutions
    with right:
        _section_header("📈", "Recent Resolutions",
                         "Last 8 validated resolutions added to the knowledge base")
        recent = resolutions[-8:] if len(resolutions) >= 8 else resolutions
        if recent:
            rows_display = []
            for r in reversed(recent):
                cls = str(r.get("classification") or "—").replace("_", " ").title()
                src = str(r.get("source") or r.get("resolution_source") or "—")
                created = str(r.get("created_at") or r.get("timestamp") or "")[:10]
                rows_display.append({
                    "Classification": cls,
                    "Source": src,
                    "Date": created,
                })
            st.dataframe(rows_display, use_container_width=True, hide_index=True)
        else:
            st.caption("No resolutions recorded yet.")

        # ── AI vs Manual breakdown ─────────────────────────────────
        _section_header("🤖", "AI Resolution Breakdown",
                         "How incidents were resolved")
        ai_auto   = auto_executions
        ai_assist = len([r for r in resolutions if str(r.get("source") or "").startswith("ui")])
        manual    = max(0, total_resolved - ai_auto - ai_assist)
        if total_resolved:
            breakdown = [
                ("Automated (no touch)", ai_auto,   "79,142,247"),
                ("AI-assisted",          ai_assist,  "16,185,129"),
                ("Manual",               manual,     "148,163,184"),
            ]
            max_b = max(v for _, v, _ in breakdown) or 1
            for label, val, acc in breakdown:
                _bar_row(label, val, max_b, acc)
            pct_ai = round((ai_auto + ai_assist) / total_resolved * 100)
            st.markdown(
                f"<div style='font-size:.78rem;opacity:.6;margin-top:8px;'>"
                f"AI involvement: <b>{pct_ai}%</b> of all resolved incidents</div>",
                unsafe_allow_html=True,
            )
        else:
            st.caption("No resolution data to break down yet.")

    # ── SLA & MTTR per incident category ─────────────────────────────────────
    _section_header("📊", "SLA & MTTR by Category",
                    "Average resolution time vs SLA target — from long-term memory")
    try:
        from long_term_memory import list_top_classifications
        cat_rows = list_top_classifications(limit=12)
    except Exception:
        cat_rows = []

    if not cat_rows:
        # Fall back to building from resolutions.json
        cls_data: Dict[str, Dict] = {}
        for r in resolutions:
            cls = str(r.get("classification") or "unknown")
            rec = cls_data.setdefault(cls, {"total": 0})
            rec["total"] += 1
        cat_rows = [{"classification": k, "total_resolutions": v["total"],
                     "avg_resolution_mins": None} for k, v in cls_data.items()]
        cat_rows.sort(key=lambda x: x["total_resolutions"], reverse=True)
        cat_rows = cat_rows[:12]

    if cat_rows:
        # Header row
        hcols = st.columns([3, 1, 1, 1, 1])
        for col, lbl in zip(hcols, ["Category", "Count", "Avg MTTR", "SLA Target", "Status"]):
            col.markdown(
                f"<div style='font-size:.72rem;font-weight:700;text-transform:uppercase;"
                f"letter-spacing:.05em;opacity:.55;'>{lbl}</div>",
                unsafe_allow_html=True,
            )
        st.markdown("<div style='margin-bottom:4px;'></div>", unsafe_allow_html=True)
        for row in cat_rows:
            cls      = str(row.get("classification") or "")
            total    = int(row.get("total_resolutions") or row.get("total") or 0)
            avg_mins = row.get("avg_resolution_mins") or row.get("avg_mins")
            sla      = _sla_target(cls)
            label    = cls.replace("_", " ").title()
            avg_str  = f"{float(avg_mins):.0f} min" if avg_mins else "—"
            if avg_mins:
                ratio = float(avg_mins) / sla
                if ratio <= 0.75:
                    status_icon, status_color = "✅ On Track", "#10B981"
                elif ratio <= 1.0:
                    status_icon, status_color = "⚠️ At Risk", "#F59E0B"
                else:
                    status_icon, status_color = "❌ Breached", "#EF4444"
            else:
                status_icon, status_color = "— No data", "#94A3B8"
            row_cols = st.columns([3, 1, 1, 1, 1])
            row_cols[0].markdown(
                f"<div style='font-size:.82rem;font-weight:600;padding:3px 0;'>{label}</div>",
                unsafe_allow_html=True,
            )
            row_cols[1].markdown(
                f"<div style='font-size:.82rem;padding:3px 0;'>{total}</div>",
                unsafe_allow_html=True,
            )
            row_cols[2].markdown(
                f"<div style='font-size:.82rem;padding:3px 0;'>{avg_str}</div>",
                unsafe_allow_html=True,
            )
            row_cols[3].markdown(
                f"<div style='font-size:.82rem;padding:3px 0;opacity:.7;'>{sla} min</div>",
                unsafe_allow_html=True,
            )
            row_cols[4].markdown(
                f"<div style='font-size:.78rem;font-weight:700;color:{status_color};"
                f"padding:3px 0;'>{status_icon}</div>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("No resolution history yet. Resolve incidents to populate SLA tracking.")
