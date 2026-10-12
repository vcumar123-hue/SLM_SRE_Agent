"""Drift Management page module."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone, timedelta
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
_LAB_CONTAINERS = ["linux-lab-01", "linux-lab-02", "network-lab-01"]
_MODES = ["Lab (Docker container)", "CMDB (ServiceNow read)", "Dry Run (simulation)"]
_MODE_VALUES = ["lab", "cmdb", "dry_run"]
_CHANGE_TYPES = ["restart_service", "update_config", "install_package", "run_command"]

# ---------------------------------------------------------------------------
# Data helpers (local file reads — no backend required)
# ---------------------------------------------------------------------------

def _read_automation_candidates() -> List[dict]:
    """Read automation_candidates.json from the data directory."""
    candidates_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "automation_candidates.json"
    )
    try:
        with open(candidates_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items", data.get("candidates", []))
        return []
    except Exception:
        return []


def _read_local_audit_log() -> List[dict]:
    """Read drift audit records from local data/drift_audit.json or logs/drift_audit.jsonl."""
    base = os.path.dirname(os.path.dirname(__file__))
    # Try JSON file first
    for rel in ("data/drift_audit.json", "logs/drift_audit.jsonl"):
        path = os.path.join(base, rel)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                continue
            if path.endswith(".jsonl"):
                return [json.loads(line) for line in content.splitlines() if line.strip()]
            data = json.loads(content)
            if isinstance(data, list):
                return data
            return data.get("items", [])
        except Exception:
            continue
    return []


# ---------------------------------------------------------------------------
# Public chip helper (importable by ui.py for sidebar nav label)
# ---------------------------------------------------------------------------

def _drift_activity_chip() -> str:
    """Return a small coloured HTML indicator showing aggregate drift status.

    Green  = no candidates have drift_type set.
    Amber  = 1 candidate has drift_type set.
    Red    = 2 or more candidates have drift_type set.
    """
    candidates = _read_automation_candidates()
    drift_count = sum(
        1 for c in candidates
        if str(c.get("drift_type") or "").strip()
        not in ("", "none", "unknown_or_not_detected", "null")
    )
    if drift_count == 0:
        color, label, title = "#10b981", "●", "No active drift"
    elif drift_count == 1:
        color, label, title = "#f59e0b", "●", "1 candidate with drift"
    else:
        color, label, title = "#dc2626", "●", f"{drift_count} candidates with drift"
    return (
        f'<span title="{title}" style="color:{color};font-size:0.65rem;'
        f'vertical-align:middle;margin-left:4px;">{label}</span>'
    )


# ---------------------------------------------------------------------------
# Summary banner (call at top of drift page)
# ---------------------------------------------------------------------------

def render_drift_summary_banner() -> None:
    """Render a compact drift-status banner above the main drift page content."""
    candidates = _read_automation_candidates()
    drift_candidates = [
        c for c in candidates
        if str(c.get("drift_type") or "").strip()
        not in ("", "none", "unknown_or_not_detected", "null")
    ]
    n_candidates = len(candidates)
    n_drift = len(drift_candidates)

    # Try to get snapshot count from backend; fall back to audit log length
    snapshots = _fetch_snapshots()
    audit_records = _read_local_audit_log()
    n_snapshots = len(snapshots)
    n_audit = len(audit_records)

    # Chip colour
    if n_drift == 0:
        gate_color, gate_bg, gate_label = "#10b981", "#f0fdf4", "No Drift Detected"
        gate_border = "#86efac"
    elif n_drift == 1:
        gate_color, gate_bg, gate_label = "#d97706", "#fffbeb", "Drift Detected"
        gate_border = "#fde68a"
    else:
        gate_color, gate_bg, gate_label = "#dc2626", "#fef2f2", "Multiple Drift Events"
        gate_border = "#fecaca"

    banner_html = f"""
<div style="background:{gate_bg};border:1px solid {gate_border};border-left:4px solid {gate_color};
     border-radius:8px;padding:12px 16px;margin-bottom:16px;display:flex;
     flex-wrap:wrap;gap:20px;align-items:center;">
  <div style="display:flex;align-items:center;gap:8px;">
    <span style="font-size:1.1rem;">🔍</span>
    <div>
      <div style="font-size:0.72rem;font-weight:700;color:{gate_color};
           text-transform:uppercase;letter-spacing:.06em;">Safety Gate · Drift Status</div>
      <div style="font-size:0.82rem;font-weight:600;color:#1e293b;">{gate_label}</div>
    </div>
  </div>
  <div style="display:flex;gap:20px;flex-wrap:wrap;">
    <div style="text-align:center;">
      <div style="font-size:1.25rem;font-weight:800;color:#1e293b;">{n_snapshots}</div>
      <div style="font-size:0.68rem;color:#64748b;">Active Snapshots</div>
    </div>
    <div style="text-align:center;">
      <div style="font-size:1.25rem;font-weight:800;color:#1e293b;">{n_audit}</div>
      <div style="font-size:0.68rem;color:#64748b;">Audit Records</div>
    </div>
    <div style="text-align:center;">
      <div style="font-size:1.25rem;font-weight:800;color:{gate_color};">{n_drift}</div>
      <div style="font-size:0.68rem;color:#64748b;">Drift Candidates</div>
    </div>
    <div style="text-align:center;">
      <div style="font-size:1.25rem;font-weight:800;color:#1e293b;">{n_candidates}</div>
      <div style="font-size:0.68rem;color:#64748b;">Total Candidates</div>
    </div>
  </div>
</div>
"""
    st.markdown(banner_html, unsafe_allow_html=True)

    # Drift candidate list (if any)
    if drift_candidates:
        drift_rows = "".join(
            f"<div style='padding:4px 0;border-bottom:1px solid #e5e7eb;font-size:0.8rem;'>"
            f"<span style='font-weight:600;color:#1e293b;'>{c.get('incident_id','—')}</span>"
            f"&nbsp;&nbsp;<span style='color:#64748b;'>{nice_label(str(c.get('drift_type','')))} drift</span>"
            f"&nbsp;&nbsp;<span style='background:#fef3c7;color:#92400e;border-radius:4px;"
            f"padding:1px 6px;font-size:0.7rem;font-weight:700;'>DRIFT</span>"
            f"</div>"
            for c in drift_candidates[:5]
        )
        st.markdown(
            f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:6px;'
            f'padding:10px 14px;margin-bottom:14px;">'
            f'<div style="font-size:0.72rem;font-weight:700;color:#374151;'
            f'text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Drift-Flagged Automation Candidates</div>'
            f'{drift_rows}</div>',
            unsafe_allow_html=True,
        )

    # Drift Audit Trail — last 5 records (local file)
    if audit_records:
        trail_rows = "".join(
            f"<div style='padding:4px 0;border-bottom:1px solid #e5e7eb;font-size:0.78rem;'>"
            f"<span style='color:#64748b;font-size:0.7rem;'>{str(r.get('captured_at') or r.get('created_at',''))[:16] or '—'}</span>"
            f"&nbsp; <span style='font-weight:600;color:#1e293b;'>{r.get('ci_name','—')}</span>"
            f"&nbsp; <span style='color:#64748b;'>{nice_label(str(r.get('mode','')))} · {str(r.get('result','—')).upper()}</span>"
            f"</div>"
            for r in audit_records[-5:][::-1]
        )
        st.markdown(
            f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:6px;'
            f'padding:10px 14px;margin-bottom:14px;">'
            f'<div style="font-size:0.72rem;font-weight:700;color:#374151;'
            f'text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Drift Audit Trail (last 5)</div>'
            f'{trail_rows}</div>',
            unsafe_allow_html=True,
        )
    elif n_snapshots == 0 and n_audit == 0:
        st.markdown(
            '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;'
            'padding:10px 14px;margin-bottom:14px;font-size:0.8rem;color:#64748b;">'
            'No drift activity recorded yet. Capture a snapshot to begin the drift management cycle.'
            '</div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Status badge helpers
# ---------------------------------------------------------------------------

def _snapshot_status_badge(status: str) -> str:
    _s = str(status or "").strip().lower()
    badge_map = {
        "active":      ("#dbeafe", "#1e40af", "#3b82f6"),
        "remediated":  ("#fef3c7", "#92400e", "#f59e0b"),
        "verified":    ("#d1fae5", "#065f46", "#10b981"),
        "failed":      ("#fee2e2", "#991b1b", "#dc2626"),
    }
    bg, text, border = badge_map.get(_s, ("#f3f4f6", "#374151", "#9ca3af"))
    label = _s.upper() if _s else "UNKNOWN"
    return (
        f'<span style="background:{bg};color:{text};border:1.5px solid {border};'
        f'border-radius:20px;padding:2px 10px;font-size:0.72rem;font-weight:700;'
        f'letter-spacing:0.04em;">{label}</span>'
    )


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30, show_spinner=False)
def _fetch_snapshots() -> List[dict]:
    try:
        r = SESSION.get(f"{BACKEND_URL}/drift/snapshots", timeout=REQUEST_TIMEOUT_MEDIUM)
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception:
        return []


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_audit_log() -> List[dict]:
    try:
        r = SESSION.get(f"{BACKEND_URL}/drift/audit", timeout=REQUEST_TIMEOUT_MEDIUM)
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception:
        return []


def _post(path: str, payload: dict) -> dict:
    r = SESSION.post(f"{BACKEND_URL}{path}", json=payload, timeout=REQUEST_TIMEOUT_MEDIUM)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Sub-renderers
# ---------------------------------------------------------------------------

def _render_new_snapshot_form() -> None:
    section_title("📸 New Snapshot")
    with st.container():
        ci_name = st.text_input(
            "CI Name",
            placeholder="e.g. linux-lab-01, web-prod-01",
            key="drift_new_ci_name",
        )
        mode_label = st.radio(
            "Mode",
            _MODES,
            horizontal=True,
            key="drift_new_mode",
        )
        mode_idx = _MODES.index(mode_label)
        mode_value = _MODE_VALUES[mode_idx]

        container_name: Optional[str] = None
        if mode_value == "lab":
            container_name = st.selectbox(
                "Container",
                _LAB_CONTAINERS,
                key="drift_new_container",
            )

        notes = st.text_area(
            "Notes (optional)",
            placeholder="Describe the purpose of this snapshot…",
            key="drift_new_notes",
            height=80,
        )

        if st.button("📸 Capture Snapshot", key="drift_capture_btn", type="primary"):
            if not ci_name.strip():
                st.warning("CI Name is required.", icon="⚠️")
                return
            payload: Dict[str, Any] = {
                "ci_name": ci_name.strip(),
                "mode": mode_value,
                "notes": notes.strip(),
            }
            if container_name:
                payload["container_name"] = container_name
            try:
                with st.spinner("Capturing snapshot…"):
                    result = _post("/drift/snapshot", payload)
                st.success(
                    f"Snapshot captured: `{result.get('snapshot_id', 'ok')}` "
                    f"for **{ci_name}** ({mode_value})"
                )
                st.cache_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"Snapshot failed: {exc}", icon="⚠️")


def _render_remediation_form(snapshot_id: str) -> None:
    """Inline remediation form for a specific snapshot."""
    changes_key = f"drift_remediation_changes_{snapshot_id}"
    if changes_key not in st.session_state:
        st.session_state[changes_key] = []

    st.markdown(
        '<div style="background:#f8faff;border:1px solid #dbeafe;border-radius:8px;padding:14px 16px;margin-top:8px;">',
        unsafe_allow_html=True,
    )
    st.markdown("**⚙️ Remediation Parameters**")

    playbook_name = st.text_input(
        "Playbook Name",
        placeholder="e.g. restart_nginx.yml",
        key=f"drift_rem_playbook_{snapshot_id}",
    )
    col_type, col_target = st.columns([1, 2])
    with col_type:
        change_type = st.selectbox(
            "Change Type",
            _CHANGE_TYPES,
            key=f"drift_rem_change_type_{snapshot_id}",
            format_func=nice_label,
        )
    with col_target:
        change_target = st.text_input(
            "Target (service / path / package / command)",
            placeholder="nginx, /etc/nginx/nginx.conf, curl, …",
            key=f"drift_rem_target_{snapshot_id}",
        )

    add_col, _ = st.columns([1, 3])
    with add_col:
        if st.button("➕ Add Change", key=f"drift_rem_add_{snapshot_id}"):
            if change_type and change_target.strip():
                st.session_state[changes_key].append(
                    {"change_type": change_type, "target": change_target.strip()}
                )
            else:
                st.warning("Specify both change type and target.", icon="⚠️")

    if st.session_state[changes_key]:
        st.markdown("**Changes queued:**")
        for i, ch in enumerate(st.session_state[changes_key]):
            st.markdown(
                f"&nbsp;&nbsp;`{i + 1}.` {nice_label(ch['change_type'])} → `{ch['target']}`",
                unsafe_allow_html=True,
            )
        if st.button(
            "✅ Submit Remediation",
            key=f"drift_rem_submit_{snapshot_id}",
            type="primary",
        ):
            if not playbook_name.strip():
                st.warning("Playbook name is required.", icon="⚠️")
            else:
                payload = {
                    "snapshot_id": snapshot_id,
                    "playbook_name": playbook_name.strip(),
                    "changes": st.session_state[changes_key],
                }
                try:
                    with st.spinner("Submitting remediation…"):
                        result = _post("/drift/remediate", payload)
                    st.success(
                        f"Remediation submitted: `{result.get('remediation_id', 'ok')}`"
                    )
                    st.session_state[changes_key] = []
                    st.session_state[f"drift_active_action_{snapshot_id}"] = None
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Remediation failed: {exc}", icon="⚠️")
    else:
        st.caption("Add at least one change before submitting.")

    st.markdown("</div>", unsafe_allow_html=True)


def _render_active_snapshots() -> None:
    section_title("🗂️ Active Snapshots")
    snapshots = _fetch_snapshots()

    if not snapshots:
        render_empty_state(
            "📷",
            "No Snapshots Yet",
            "Capture a snapshot above to begin the drift management cycle.",
            "Use the form above to capture your first snapshot.",
        )
        return

    for snap in snapshots:
        snap_id = snap.get("snapshot_id", "")
        ci = snap.get("ci_name", "unknown")
        mode = snap.get("mode", "")
        status = snap.get("status", "unknown")
        captured_at = str(snap.get("captured_at", ""))[:16]
        action_key = f"drift_active_action_{snap_id}"

        badge_html = _snapshot_status_badge(status)
        header_html = (
            f'<span style="font-weight:600;">{ci}</span> &nbsp;—&nbsp; '
            f'<span style="color:#6b7280;">{nice_label(mode)}</span> &nbsp;—&nbsp; '
            f'{badge_html} &nbsp;—&nbsp; '
            f'<span style="font-size:0.82rem;color:#9ca3af;">{captured_at}</span>'
        )

        with st.expander(f"{ci}  ·  {nice_label(mode)}  ·  {status.upper()}", expanded=False):
            st.markdown(header_html, unsafe_allow_html=True)
            st.markdown("**State details:**")
            state = snap.get("state", snap.get("state_details", {}))
            if isinstance(state, dict):
                st.code(json.dumps(state, indent=2), language="json")
            else:
                st.code(str(state or "No state captured."), language="text")

            # Action buttons row
            b1, b2, b3, b4 = st.columns(4)
            with b1:
                if st.button("⚙️ Remediate", key=f"drift_btn_rem_{snap_id}"):
                    current = st.session_state.get(action_key)
                    st.session_state[action_key] = None if current == "remediate" else "remediate"
            with b2:
                if st.button("✅ Verify", key=f"drift_btn_verify_{snap_id}"):
                    try:
                        with st.spinner("Verifying…"):
                            res = _post("/drift/verify", {"snapshot_id": snap_id})
                        verified_status = res.get("status", "ok")
                        st.success(f"Verify result: `{verified_status}`")
                        st.cache_data.clear()
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Verify failed: {exc}", icon="⚠️")
            with b3:
                if st.button("🗑️ Destroy + Audit", key=f"drift_btn_destroy_{snap_id}"):
                    st.session_state[action_key] = (
                        None if st.session_state.get(action_key) == "destroy" else "destroy"
                    )
            with b4:
                if st.button("🔄 Run Full Cycle", key=f"drift_btn_cycle_{snap_id}"):
                    try:
                        with st.spinner("Running full drift cycle…"):
                            res = _post("/drift/run_cycle", {"snapshot_id": snap_id})
                        st.success(f"Full cycle completed: `{res.get('status', 'ok')}`")
                        st.cache_data.clear()
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Cycle failed: {exc}", icon="⚠️")

            # Conditional action panels
            active_action = st.session_state.get(action_key)
            if active_action == "remediate":
                _render_remediation_form(snap_id)

            if active_action == "destroy":
                st.warning(
                    f"This will destroy snapshot `{snap_id}` and write a final audit record. "
                    "This cannot be undone.",
                    icon="⚠️",
                )
                confirm_col, cancel_col = st.columns([1, 3])
                with confirm_col:
                    if st.button("Confirm Destroy", key=f"drift_confirm_destroy_{snap_id}", type="primary"):
                        try:
                            with st.spinner("Destroying…"):
                                res = _post("/drift/destroy", {"snapshot_id": snap_id})
                            st.success(f"Snapshot destroyed and audited: `{res.get('audit_id', 'ok')}`")
                            st.session_state[action_key] = None
                            st.cache_data.clear()
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Destroy failed: {exc}", icon="⚠️")
                with cancel_col:
                    if st.button("Cancel", key=f"drift_cancel_destroy_{snap_id}"):
                        st.session_state[action_key] = None
                        st.rerun()


def _render_audit_tab() -> None:
    """Render Audit Log tab content."""
    records = _fetch_audit_log()

    # Summary metrics
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    total = len(records)
    passes = sum(1 for r in records if str(r.get("result", "")).lower() in {"pass", "passed", "success", "verified"})
    fails = total - passes
    recent = sum(
        1 for r in records
        if _parse_dt(str(r.get("captured_at") or r.get("created_at", ""))) >= cutoff
    )

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        metric_card("Total Audits", str(total), "dashboard")
    with m2:
        metric_card("Pass", str(passes), "automation")
    with m3:
        metric_card("Fail", str(fails), "neutral")
    with m4:
        metric_card("Last 7 Days", str(recent), "knowledge")

    st.markdown("<br>", unsafe_allow_html=True)

    if not records:
        render_empty_state(
            "📋",
            "No Audit Records",
            "Audit records are created when snapshots are destroyed or after drift cycles complete.",
        )
        return

    # Build display rows
    rows = []
    for r in records:
        result_raw = str(r.get("result", "")).lower()
        result_icon = "✅" if result_raw in {"pass", "passed", "success", "verified"} else "❌"
        rows.append({
            "CI Name": r.get("ci_name", ""),
            "Mode": nice_label(r.get("mode", "")),
            "Captured At": str(r.get("captured_at", ""))[:16],
            "Remediation": r.get("playbook_name") or r.get("remediation_id") or "—",
            "Result": result_icon,
            "Destroyed At": str(r.get("destroyed_at", ""))[:16] or "—",
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)

    # Detailed row expanders
    section_title("Audit Record Details")
    for r in records[:20]:
        snap_id = r.get("snapshot_id", r.get("audit_id", ""))
        ci = r.get("ci_name", "unknown")
        result_raw = str(r.get("result", "")).lower()
        result_icon = "✅" if result_raw in {"pass", "passed", "success", "verified"} else "❌"
        with st.expander(f"{result_icon}  {ci}  ·  {snap_id}", expanded=False):
            before = r.get("before_state", {})
            after = r.get("after_state", {})

            st.markdown("**Full Audit Record:**")
            safe_r = {k: v for k, v in r.items() if k not in {"before_state", "after_state"}}
            st.code(json.dumps(safe_r, indent=2, default=str), language="json")

            if before or after:
                st.markdown("**State Diff (before / after):**")
                diff_col1, diff_col2 = st.columns(2)
                with diff_col1:
                    st.markdown("*Before*")
                    st.code(json.dumps(before, indent=2, default=str), language="json")
                with diff_col2:
                    st.markdown("*After*")
                    st.code(json.dumps(after, indent=2, default=str), language="json")

    # Export
    st.markdown("<br>", unsafe_allow_html=True)
    export_col, _ = st.columns([1, 3])
    with export_col:
        st.download_button(
            label="⬇️ Export Audit Log",
            data=json.dumps(records, indent=2, default=str),
            file_name="drift_audit_log.json",
            mime="application/json",
            use_container_width=True,
        )


def _parse_dt(ts: str) -> datetime:
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def render_drift_page() -> None:
    st.subheader("Drift Management")
    st.caption(
        "Capture configuration snapshots, run remediation playbooks, verify state, "
        "and maintain a full audit trail across Lab, CMDB, and Dry-Run modes."
    )

    render_drift_summary_banner()

    tab_cycle, tab_audit = st.tabs(["🔄 Snapshot Cycle", "📋 Audit Log"])

    with tab_cycle:
        _render_new_snapshot_form()
        st.divider()
        _render_active_snapshots()

    with tab_audit:
        _render_audit_tab()
