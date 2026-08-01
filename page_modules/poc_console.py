"""POC Console — scenario theater for live client demonstrations."""
from __future__ import annotations

import sys
import time
import threading
from pathlib import Path
from typing import Any

import requests
import streamlit as st

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from poc.scenarios import (
    ALL_SCENARIOS, SCENARIOS_BY_ID, CATEGORY_LABELS, DETECTION_LABELS,
    Scenario, TelemetryEvent,
)
from page_modules.shared import BACKEND_URL, SESSION, REQUEST_TIMEOUT_LONG, REQUEST_TIMEOUT_MEDIUM

# ---------------------------------------------------------------------------
# Constants / styling helpers
# ---------------------------------------------------------------------------

CATEGORY_COLORS = {
    "infra":            "#3b82f6",
    "end_user":         "#8b5cf6",
    "data_engineering": "#f59e0b",
    "application":      "#ef4444",
}

MODE_ICONS = {
    "reactive":  "📨",
    "proactive": "📡",
    "both":      "🔄",
}

PRIORITY_COLORS = {
    "P1": "#dc2626",
    "P2": "#ea580c",
    "P3": "#d97706",
    "P4": "#16a34a",
    "P5": "#6b7280",
}

STATUS_STYLES = {
    "pending":  ("⬜", "#6b7280"),
    "running":  ("🔄", "#3b82f6"),
    "success":  ("✅", "#22c55e"),
    "failed":   ("❌", "#ef4444"),
    "skipped":  ("⏭️", "#6b7280"),
}


# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------

def _init_state() -> None:
    if "poc_results" not in st.session_state:
        st.session_state.poc_results = {}          # {scenario_id: result_dict}
    if "poc_telemetry_log" not in st.session_state:
        st.session_state.poc_telemetry_log = []    # [(ts, host, metric, value, threshold)]
    if "poc_running" not in st.session_state:
        st.session_state.poc_running = False
    if "poc_selected" not in st.session_state:
        st.session_state.poc_selected = None


# ---------------------------------------------------------------------------
# Backend calls
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict, timeout: int = REQUEST_TIMEOUT_LONG) -> dict | None:
    try:
        r = SESSION.post(f"{BACKEND_URL}{path}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _get(path: str, timeout: int = REQUEST_TIMEOUT_MEDIUM) -> Any:
    try:
        r = SESSION.get(f"{BACKEND_URL}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return None


def _register_cis() -> int:
    cis = [
        {"ci_name": "linux-lab-01", "tier": "production", "criticality": "high",
         "redundancy_type": "active-standby", "services": ["customer-portal", "payment-api"]},
        {"ci_name": "linux-lab-02", "tier": "production", "criticality": "high",
         "redundancy_type": "active-passive", "services": ["postgres-replica", "etl-engine"]},
        {"ci_name": "network-lab-01", "tier": "production", "criticality": "critical",
         "redundancy_type": "standalone", "services": ["north-south-gateway"]},
    ]
    ok = 0
    for ci in cis:
        r = _post("/cmdb/ci", ci, timeout=10)
        if r and "error" not in r:
            ok += 1
    return ok


def run_reactive_analysis(scenario: Scenario) -> dict:
    result = _post(
        "/analyze",
        {
            "text":     scenario.ticket_text,
            "source":   "poc_console",
            "priority": scenario.priority,
            "ci":       scenario.ci_target,
        },
        timeout=90,
    )
    return result or {"error": "No response from backend"}


def post_telemetry_step(event: TelemetryEvent, value: float) -> bool:
    from datetime import datetime
    payload = {
        "metric":    event.metric,
        "value":     round(value, 3),
        "host":      event.host,
        "unit":      event.unit,
        "tags":      {**event.tags, "poc_simulation": "true"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    r = _post("/telemetry/event", payload, timeout=10)
    return r is not None and "error" not in r


def trigger_evaluate_and_promote() -> tuple[int, list[dict]]:
    eval_r = _post("/telemetry/evaluate", {}, timeout=20) or {}
    fired = eval_r.get("fired_count", eval_r.get("alerts_fired", 0))
    promote_r = _post("/telemetry/promote", {"limit": 5}, timeout=20) or {}
    promoted = promote_r.get("promoted", promote_r.get("incidents", []))
    if not isinstance(promoted, list):
        promoted = []
    return fired, promoted


# ---------------------------------------------------------------------------
# Scenario card renderer
# ---------------------------------------------------------------------------

def _scenario_card(scenario: Scenario, result: dict | None) -> None:
    sid = scenario.id
    cat_color = CATEGORY_COLORS.get(scenario.category, "#6b7280")
    pri_color = PRIORITY_COLORS.get(scenario.priority, "#6b7280")
    mode_icon = MODE_ICONS.get(scenario.detection_mode, "?")

    status = "pending"
    if result:
        status = result.get("status", "pending")

    status_icon, status_color = STATUS_STYLES.get(status, STATUS_STYLES["pending"])

    reactive_ok = result.get("classification") if result else None
    confidence = result.get("confidence", 0)
    conf_str = f"{confidence:.0%}" if isinstance(confidence, float) and confidence else ""
    proactive_count = len(result.get("proactive_incidents", [])) if result else 0

    with st.container(border=True):
        col_badge, col_title, col_mode, col_pri, col_status = st.columns([1.2, 5, 2.5, 1, 1.2])

        with col_badge:
            st.markdown(
                f'<span style="background:{cat_color}22;color:{cat_color};'
                f'border:1px solid {cat_color}55;border-radius:4px;'
                f'padding:2px 8px;font-size:0.7rem;font-weight:700;'
                f'white-space:nowrap;">S{sid:02d} {CATEGORY_LABELS.get(scenario.category, "")}</span>',
                unsafe_allow_html=True,
            )
        with col_title:
            st.markdown(f"**{scenario.title}**", help=scenario.ticket_text[:300])
        with col_mode:
            st.markdown(
                f'<span style="font-size:0.78rem;color:#71717a;">'
                f'{mode_icon} {DETECTION_LABELS.get(scenario.detection_mode, "")}</span>',
                unsafe_allow_html=True,
            )
        with col_pri:
            st.markdown(
                f'<span style="color:{pri_color};font-size:0.78rem;font-weight:700;">'
                f'{scenario.priority}</span>',
                unsafe_allow_html=True,
            )
        with col_status:
            st.markdown(
                f'<span style="color:{status_color};font-size:1.1rem;">{status_icon}</span>',
                unsafe_allow_html=True,
            )

        # Result row (only when run)
        if result and result.get("status") == "success":
            r1, r2, r3 = st.columns([3, 2, 2])
            with r1:
                cls = result.get("classification", "—")
                st.caption(f"🏷 **{cls}**")
            with r2:
                if conf_str:
                    st.caption(f"🎯 Confidence: **{conf_str}**")
            with r3:
                steps = result.get("runbook_steps", 0)
                if steps:
                    st.caption(f"📋 {steps} runbook steps")
                if proactive_count:
                    st.caption(f"📡 {proactive_count} proactive incident(s)")
        elif result and result.get("status") == "failed":
            st.caption(f"⚠️ {result.get('error', 'Analysis failed')}")


# ---------------------------------------------------------------------------
# Reactive tab
# ---------------------------------------------------------------------------

def _render_reactive_tab() -> None:
    st.markdown("### Run Reactive Scenarios")
    st.caption(
        "Select scenarios and click **Run**. Each ticket is submitted to the AI engine "
        "which performs RCA, generates a runbook, and routes via the safety gate."
    )

    reactive_scenarios = [s for s in ALL_SCENARIOS if s.detection_mode in ("reactive", "both")]

    sel = st.multiselect(
        "Scenarios to run",
        options=[s.id for s in reactive_scenarios],
        format_func=lambda sid: f"S{sid:02d} — {SCENARIOS_BY_ID[sid].title}",
        default=[s.id for s in reactive_scenarios],
        key="poc_reactive_sel",
    )

    col_run, col_reset = st.columns([2, 1])
    with col_run:
        run_btn = st.button("▶  Run Selected Reactive Scenarios", type="primary", use_container_width=True)
    with col_reset:
        if st.button("↺  Reset Results", use_container_width=True):
            for sid in sel:
                st.session_state.poc_results.pop(sid, None)
            st.rerun()

    if run_btn and sel:
        progress = st.progress(0, text="Starting...")
        results_placeholder = st.empty()

        for i, sid in enumerate(sel):
            scenario = SCENARIOS_BY_ID[sid]
            progress.progress((i) / len(sel), text=f"Analysing S{sid:02d}: {scenario.title}...")

            st.session_state.poc_results[sid] = {"status": "running"}
            result = run_reactive_analysis(scenario)

            if "error" in result and "classification" not in result:
                st.session_state.poc_results[sid] = {"status": "failed", "error": result["error"]}
            else:
                classification = result.get("classification", result.get("category", "unknown"))
                confidence = result.get("confidence", result.get("confidence_score", 0.0))
                steps = result.get("runbook_steps", result.get("playbook_steps", []))
                st.session_state.poc_results[sid] = {
                    "status": "success",
                    "classification": classification,
                    "confidence": confidence,
                    "runbook_steps": len(steps) if isinstance(steps, list) else 0,
                    "raw": result,
                }

            progress.progress((i + 1) / len(sel), text=f"Done S{sid:02d}.")

        progress.empty()
        st.success(f"Completed {len(sel)} scenario(s).")
        st.rerun()

    # Results display
    st.divider()
    for sid in sel:
        scenario = SCENARIOS_BY_ID.get(sid)
        if not scenario:
            continue
        result = st.session_state.poc_results.get(sid)
        _scenario_card(scenario, result)

        # Expandable detail
        if result and result.get("status") == "success" and result.get("raw"):
            with st.expander(f"S{sid:02d} — Full Analysis Output"):
                raw = result["raw"]
                cols = st.columns(2)
                with cols[0]:
                    st.markdown("**Ticket (input)**")
                    st.text_area("", value=scenario.ticket_text, height=200,
                                 disabled=True, key=f"ticket_{sid}", label_visibility="collapsed")
                with cols[1]:
                    st.markdown("**AI Analysis Output**")
                    for key in ("classification", "confidence", "summary", "rca_summary", "root_cause"):
                        if key in raw:
                            st.caption(f"**{key.replace('_', ' ').title()}:** {raw[key]}")
                    steps = raw.get("runbook_steps", raw.get("playbook_steps", []))
                    if steps:
                        st.markdown("**Runbook Steps**")
                        for j, step in enumerate(steps[:6], 1):
                            text = step if isinstance(step, str) else step.get("action", str(step))
                            st.markdown(f"{j}. {text}")


# ---------------------------------------------------------------------------
# Proactive tab
# ---------------------------------------------------------------------------

def _render_proactive_tab() -> None:
    st.markdown("### Proactive Detection via Telemetry")
    st.caption(
        "The simulator escalates real metric streams (CPU, memory, latency, replication lag) "
        "until they breach thresholds — triggering the telemetry engine to auto-create incidents "
        "**before** a user raises a ticket."
    )

    proactive_scenarios = [
        s for s in ALL_SCENARIOS
        if s.detection_mode in ("proactive", "both") and s.telemetry_events
    ]

    sel_ids = st.multiselect(
        "Proactive scenarios to simulate",
        options=[s.id for s in proactive_scenarios],
        format_func=lambda sid: f"S{sid:02d} — {SCENARIOS_BY_ID[sid].title}",
        default=[s.id for s in proactive_scenarios],
        key="poc_proactive_sel",
    )

    speed = st.select_slider(
        "Escalation speed",
        options=["slow (8s/step)", "demo (3s/step)", "fast (1s/step)", "turbo (0.2s/step)"],
        value="demo (3s/step)",
        key="poc_sim_speed",
    )
    speed_key = speed.split(" ")[0]  # extract "slow", "demo", etc.

    SPEED_DELAYS = {"slow": 8.0, "demo": 3.0, "fast": 1.0, "turbo": 0.2}
    step_delay = SPEED_DELAYS.get(speed_key, 3.0)

    if st.button("📡  Start Proactive Simulation", type="primary", use_container_width=True, key="poc_sim_btn"):
        if not sel_ids:
            st.warning("Select at least one scenario.")
            return

        telemetry_log = st.session_state.poc_telemetry_log
        log_placeholder = st.empty()
        threshold_placeholder = st.empty()
        status_placeholder = st.empty()

        for sid in sel_ids:
            scenario = SCENARIOS_BY_ID[sid]
            st.session_state.poc_results[sid] = {"status": "running"}
            status_placeholder.info(f"📡 Running S{sid:02d}: {scenario.title}...")

            threshold_breached = False

            for event in scenario.telemetry_events:
                for step_val in event.escalation_steps:
                    ok = post_telemetry_step(event, step_val)
                    breach = step_val >= event.threshold
                    if breach and not threshold_breached:
                        threshold_breached = True

                    telemetry_log.append({
                        "scenario": sid,
                        "host": event.host,
                        "metric": event.metric,
                        "value": step_val,
                        "threshold": event.threshold,
                        "breached": breach,
                        "ok": ok,
                    })
                    st.session_state.poc_telemetry_log = telemetry_log[-100:]

                    # Live log panel
                    recent = telemetry_log[-12:]
                    rows = "\n".join(
                        f"{'🔴' if r['breached'] else '🟢'} [{r['host']}] "
                        f"{r['metric']} = {r['value']} {' ← BREACH' if r['breached'] else ''}"
                        for r in reversed(recent)
                    )
                    log_placeholder.code(rows, language=None)
                    time.sleep(step_delay)

            # Evaluate + promote
            fired, promoted = trigger_evaluate_and_promote()
            threshold_placeholder.success(
                f"S{sid:02d}: {fired} alert(s) fired | {len(promoted)} incident(s) promoted"
            )

            prev = st.session_state.poc_results.get(sid, {})
            prev_proactive = prev.get("proactive_incidents", [])
            st.session_state.poc_results[sid] = {
                "status": "success" if (threshold_breached or promoted) else "skipped",
                "classification": scenario.classification,
                "proactive_incidents": prev_proactive + promoted,
                "threshold_breached": threshold_breached,
            }
            time.sleep(1)

        status_placeholder.success("✅ Proactive simulation complete.")
        st.rerun()

    # Live telemetry log
    if st.session_state.poc_telemetry_log:
        st.divider()
        st.markdown("**Recent telemetry events**")
        log = st.session_state.poc_telemetry_log[-20:]
        import pandas as pd
        df = pd.DataFrame([
            {
                "Scenario": f"S{r['scenario']:02d}",
                "Host": r["host"],
                "Metric": r["metric"],
                "Value": r["value"],
                "Threshold": r["threshold"],
                "Status": "🔴 BREACH" if r["breached"] else "🟢 OK",
            }
            for r in reversed(log)
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)

    # Scenario status cards
    if any(sid in st.session_state.poc_results for sid in sel_ids):
        st.divider()
        st.markdown("**Scenario outcomes**")
        for sid in sel_ids:
            scenario = SCENARIOS_BY_ID.get(sid)
            if scenario:
                result = st.session_state.poc_results.get(sid)
                _scenario_card(scenario, result)


# ---------------------------------------------------------------------------
# Upload tab
# ---------------------------------------------------------------------------

def _render_upload_tab() -> None:
    st.markdown("### Ingest Client Ticket Data")
    st.caption(
        "Upload a ticket export from your ITSM or Jira. The platform will normalise it, "
        "extract resolution patterns, build knowledge candidates, and prepare backtest cases "
        "so the AI is contextualised to your environment before the demo."
    )

    fmt = st.selectbox(
        "Source system",
        options=["auto-detect", "servicenow", "jira", "csv"],
        key="poc_upload_fmt",
    )

    uploaded = st.file_uploader(
        "Upload ticket export (CSV, JSON, or XLSX)",
        type=["csv", "json", "xlsx", "xlsb"],
        key="poc_upload_file",
    )

    push_api = st.toggle("Push knowledge candidates to API", value=True, key="poc_push_toggle")

    if uploaded and st.button("⚙️  Process Tickets", type="primary"):
        import tempfile
        from poc.ingest_client_tickets import ingest

        with tempfile.NamedTemporaryFile(
            suffix=Path(uploaded.name).suffix, delete=False
        ) as tmp:
            tmp.write(uploaded.getbuffer())
            tmp_path = Path(tmp.name)

        with st.spinner("Processing tickets..."):
            report = ingest(
                file_path=tmp_path,
                fmt="auto" if fmt == "auto-detect" else fmt,
                push_to_api=push_api,
            )

        st.success(f"✅ Processed {report['total_tickets']} tickets")

        cols = st.columns(4)
        cols[0].metric("Tickets", report["total_tickets"])
        cols[1].metric("Classifications", report["classifications_found"])
        cols[2].metric("Knowledge candidates", report["knowledge_candidates_built"])
        cols[3].metric("Backtest cases", report["backtest_cases_built"])

        if report.get("top_classifications"):
            st.markdown("**Top classifications found in your data**")
            import pandas as pd
            df = pd.DataFrame(report["top_classifications"], columns=["Classification", "Count"])
            st.bar_chart(df.set_index("Classification").head(12))


# ---------------------------------------------------------------------------
# Setup tab
# ---------------------------------------------------------------------------

def _render_setup_tab() -> None:
    st.markdown("### Platform Setup")
    st.caption("One-click bootstrap: registers lab CIs in CMDB and seeds the knowledge base.")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🗂  Register Lab CIs (CMDB)", use_container_width=True):
            with st.spinner("Registering CIs..."):
                n = _register_cis()
            st.success(f"✓ {n}/3 CIs registered")

    with c2:
        if st.button("📚  Seed Knowledge Base", use_container_width=True):
            from poc.scenarios import ALL_SCENARIOS
            seeds = []
            for s in ALL_SCENARIOS:
                seeds.append({
                    "classification": s.classification,
                    "title": f"[POC] {s.title}",
                    "description": s.ticket_text[:300].strip(),
                    "resolution": s.expected_resolution_summary,
                    "playbook_steps": [
                        step.strip()
                        for step in s.expected_resolution_summary.split(";")
                        if step.strip()
                    ],
                    "confidence": 0.85,
                    "source": "poc_seed",
                })

            ok = 0
            with st.spinner(f"Seeding {len(seeds)} knowledge entries..."):
                for seed in seeds:
                    r = _post("/knowledge_candidates", seed, timeout=10)
                    if r and "error" not in r:
                        ok += 1
                    time.sleep(0.05)
            st.success(f"✓ {ok}/{len(seeds)} knowledge entries seeded")

    st.divider()
    st.markdown("**Backend health**")
    health = _get("/health")
    if health:
        st.json(health)
    else:
        st.warning("Backend unreachable — check that the API is running on port 8001.")

    st.markdown("**Run complete POC from CLI**")
    st.code(
        "# Register CIs + seed knowledge + run all 10 scenarios\n"
        "python poc/seed_poc.py --all --mode both --speed demo\n\n"
        "# Ingest your ticket data first\n"
        "python poc/seed_poc.py --all --tickets path/to/tickets.xlsx\n\n"
        "# Run just proactive scenarios\n"
        "python poc/telemetry_simulator.py --all-proactive --speed fast",
        language="bash",
    )


# ---------------------------------------------------------------------------
# Timeline tab
# ---------------------------------------------------------------------------

def _render_timeline_tab() -> None:
    st.markdown("### POC Event Timeline")
    results = st.session_state.poc_results

    if not results:
        st.info("No scenarios have been run yet. Use the Reactive or Proactive tabs to start.")
        return

    events = []
    for sid, res in sorted(results.items()):
        scenario = SCENARIOS_BY_ID.get(sid)
        if not scenario:
            continue
        status = res.get("status", "pending")
        cat = CATEGORY_LABELS.get(scenario.category, scenario.category)
        mode = scenario.detection_mode
        events.append({
            "S#": f"S{sid:02d}",
            "Title": scenario.title,
            "Category": cat,
            "Mode": DETECTION_LABELS.get(mode, mode),
            "Priority": scenario.priority,
            "Status": status.upper(),
            "Classification": res.get("classification", "—"),
            "Confidence": f"{res.get('confidence', 0):.0%}" if isinstance(res.get("confidence"), float) else "—",
            "Proactive Events": len(res.get("proactive_incidents", [])),
        })

    if events:
        import pandas as pd
        df = pd.DataFrame(events)
        st.dataframe(df, use_container_width=True, hide_index=True)

        total = len(events)
        success = sum(1 for e in events if e["Status"] == "SUCCESS")
        st.metric("Scenarios completed", f"{success}/{total}")


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render_poc_console_page() -> None:
    _init_state()

    st.subheader("POC Scenario Console")
    st.caption(
        "End-to-end demonstration environment — 10 curated incidents across "
        "infra, end-user, data engineering, and application domains. "
        "Drive reactive RCA from ticket text and proactive detection from live telemetry streams."
    )

    # Header metrics
    results = st.session_state.poc_results
    total_run = len(results)
    total_success = sum(1 for r in results.values() if r.get("status") == "success")
    proactive_total = sum(len(r.get("proactive_incidents", [])) for r in results.values())
    correct_cls = sum(
        1 for sid, r in results.items()
        if r.get("classification") == SCENARIOS_BY_ID[sid].classification
        and sid in SCENARIOS_BY_ID
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Scenarios run", f"{total_run}/10")
    m2.metric("Analyses succeeded", f"{total_success}/{total_run}" if total_run else "—")
    m3.metric("Classification accuracy", f"{correct_cls}/{total_run}" if total_run else "—")
    m4.metric("Proactive incidents", proactive_total)

    st.divider()

    tab_setup, tab_upload, tab_reactive, tab_proactive, tab_timeline = st.tabs([
        "⚙️ Setup",
        "📥 Client Tickets",
        "📨 Reactive Scenarios",
        "📡 Proactive Detection",
        "📊 Timeline",
    ])

    with tab_setup:
        _render_setup_tab()
    with tab_upload:
        _render_upload_tab()
    with tab_reactive:
        _render_reactive_tab()
    with tab_proactive:
        _render_proactive_tab()
    with tab_timeline:
        _render_timeline_tab()
