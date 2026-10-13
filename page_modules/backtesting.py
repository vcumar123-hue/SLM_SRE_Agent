"""Backtesting page — two-panel design:
  LEFT/TOP: Your History — client's own past incidents matching this pattern
  RIGHT/BOTTOM: Peer Benchmark — anonymised cross-client patterns
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import streamlit as st
from page_modules.shared import (
    BACKEND_URL, SESSION, REQUEST_TIMEOUT_MEDIUM, REQUEST_TIMEOUT_SHORT,
    nice_label, format_confidence, metric_card, section_title,
    render_empty_state, dedupe_lines, natural_label,
)


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_backtest_results() -> dict:
    try:
        r = SESSION.get(f"{BACKEND_URL}/backtesting/results", timeout=REQUEST_TIMEOUT_MEDIUM)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _fetch_self_history(incident_text: str, client_id: str = "client_a") -> dict:
    try:
        r = SESSION.get(
            f"{BACKEND_URL}/backtesting/client/self_history",
            params={"incident_text": incident_text, "client_id": client_id},
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _fetch_peer_insights(incident_text: str, client_id: str = "client_a") -> dict:
    try:
        r = SESSION.get(
            f"{BACKEND_URL}/backtesting/client/peer_insights",
            params={"incident_text": incident_text, "client_id": client_id, "max_peers": 5},
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _fetch_temporal_patterns(incident_text: str, classification: str = "") -> dict:
    try:
        r = SESSION.get(
            f"{BACKEND_URL}/backtesting/client/temporal_patterns",
            params={"incident_text": incident_text, "classification": classification},
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Sub-renderers
# ---------------------------------------------------------------------------

def _mini_bar(value: float, max_val: float, color: str, width: int = 120) -> str:
    pct = min(100, (value / max_val * 100)) if max_val else 0
    fill = max(4, int(width * pct / 100))
    return (
        f'<div style="display:inline-block;width:{width}px;background:#f1f5f9;'
        f'border-radius:3px;height:9px;vertical-align:middle;">'
        f'<div style="width:{fill}px;background:{color};height:9px;border-radius:3px;"></div>'
        f'</div>'
    )


def _year_bars(by_year: dict, ref_year: int, color_normal: str = "#3b82f6", color_ref: str = "#ef4444") -> str:
    if not by_year:
        return ""
    max_cnt = max(by_year.values()) or 1
    bars = ""
    for yr, cnt in sorted(by_year.items()):
        h = max(3, int(48 * cnt / max_cnt))
        is_ref = int(yr) == ref_year
        bc = color_ref if is_ref else color_normal
        lbl = f"font-weight:700;color:{color_ref};" if is_ref else "color:#6b7280;"
        bars += (
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:1px;">'
            f'<span style="font-size:0.62rem;color:#6b7280;">{cnt:,}</span>'
            f'<div style="width:24px;height:{h}px;background:{bc};border-radius:2px 2px 0 0;"></div>'
            f'<span style="font-size:0.62rem;{lbl}">{yr}</span>'
            f'</div>'
        )
    return f'<div style="display:flex;gap:3px;align-items:flex-end;margin:6px 0;">{bars}</div>'


def _term_chips(terms: list) -> str:
    chips = "".join(
        f'<span style="display:inline-block;background:#dbeafe;color:#1e40af;'
        f'font-size:0.7rem;padding:1px 6px;border-radius:10px;margin:2px 2px 2px 0;">{t}</span>'
        for t in terms[:10]
    )
    return f'<div style="margin-bottom:6px;">{chips}</div>'


def _render_self_history_panel(incident_text: str, client_id: str = "client_a") -> None:
    """Render the YOUR HISTORY section — same client's own past incidents."""
    cache_key = f"self_hist_{hash(incident_text)}_{client_id}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = _fetch_self_history(incident_text, client_id)
    data = st.session_state[cache_key]

    status = data.get("status", "")
    if status == "index_missing":
        st.info(
            "**Your History index not built yet.**  \n"
            "Run `docker exec aiops-app python3 tools/build_self_index.py` "
            "to index your own historical incidents."
        )
        return
    if status == "no_data" or not data.get("matched_terms"):
        st.caption("No matching records found in your own incident history for this pattern.")
        return

    matched_terms = data.get("matched_terms", [])
    matched_count = data.get("matched_incident_count", 0)
    total_cases = data.get("total_own_cases", 0)
    avg_rh = data.get("avg_resolve_hours")
    avg_rh_lbl = f"{avg_rh:.1f}h" if avg_rh is not None else "—"
    top_cls = data.get("top_classifications", [])
    snippets = data.get("resolution_snippets", [])
    timeline = data.get("timeline", {})
    ref_year = timeline.get("ref_year", 2026)
    before = timeline.get("before_count", 0)
    during = timeline.get("during_count", 0)

    # Header metrics
    h1, h2, h3 = st.columns(3)
    with h1:
        metric_card("Pattern Hits", f"{matched_count:,}", "dashboard")
    with h2:
        metric_card("From Your History", f"{total_cases} incidents", "knowledge")
    with h3:
        metric_card("Avg Resolution", avg_rh_lbl, "neutral")

    # Matched terms
    if matched_terms:
        st.markdown(_term_chips(matched_terms), unsafe_allow_html=True)

    # Year timeline
    by_year = timeline.get("by_year", {})
    if by_year:
        st.markdown("**When this pattern occurred in your environment:**")
        st.markdown(_year_bars(by_year, ref_year, "#6366f1", "#ef4444"), unsafe_allow_html=True)
        cols = st.columns(2)
        with cols[0]:
            if before > 0:
                st.info(f"**Before {ref_year}:** {before:,} matching incidents — recurring pattern in your history.")
        with cols[1]:
            if during > 0:
                st.warning(f"**This year ({ref_year}):** {during:,} incidents — currently active pattern.")

    # Classification breakdown
    if top_cls:
        st.markdown("**How similar incidents were classified in your history:**")
        max_cnt = max(c["historical_count"] for c in top_cls) or 1
        for item in top_cls:
            cls_name = nice_label(item["classification"])
            cnt = item["historical_count"]
            cls_avg_rh = item.get("avg_resolve_hours")
            cls_rh_lbl = f"{cls_avg_rh:.1f}h" if cls_avg_rh is not None else "—"
            bar = _mini_bar(cnt, max_cnt, "#6366f1")
            titles = item.get("sample_titles", [])
            cls_snippets = item.get("resolution_snippets", [])

            st.markdown(
                f'<div style="margin:8px 0 4px 0;">'
                f'  <strong style="font-size:0.85rem;">{cls_name}</strong>'
                f'  <span style="font-size:0.75rem;color:#6b7280;margin-left:8px;">{cnt} incidents · avg {cls_rh_lbl}</span>'
                f'  <div style="margin-top:3px;">{bar}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if titles:
                st.markdown(
                    '<div style="font-size:0.73rem;color:#64748b;margin-left:4px;">'
                    + "".join(f'<div style="margin:1px 0;">↳ {t}</div>' for t in titles[:2])
                    + "</div>",
                    unsafe_allow_html=True,
                )
            if cls_snippets:
                st.markdown(
                    '<div style="font-size:0.72rem;color:#64748b;margin:3px 0 6px 4px;">'
                    '<em>Resolution approach used:</em><br>'
                    + "".join(
                        f'<div style="padding:2px 6px;background:#f8fafc;border-left:2px solid #a5b4fc;'
                        f'border-radius:2px;margin:2px 0;">"{s}"</div>'
                        for s in cls_snippets[:2]
                    )
                    + "</div>",
                    unsafe_allow_html=True,
                )

    # Resolution snippets (aggregate)
    if snippets and not top_cls:
        st.markdown("**How this was resolved in past incidents:**")
        for s in snippets[:3]:
            st.markdown(f"> {s}")


def _pattern_chip(label: str, count: int, gap_label: str, direction: str, confidence: str = "medium", same_ag_pct: int = 0) -> str:
    """Render a single before/after pattern chip with confidence styling."""
    if direction == "before":
        color  = "#fef3c7"
        border = "#f59e0b"
    else:
        color  = "#dcfce7"
        border = "#22c55e"
    arrow = "↑ precedes" if direction == "before" else "↓ follows"
    name = nice_label(label) if label != "unclassified" else None
    if not name:
        return ""
    conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "⚪")
    conf_label = confidence.upper()
    ag_note = f" · {same_ag_pct}% same team" if same_ag_pct >= 20 else ""
    return (
        f'<div style="background:{color};border:1px solid {border};border-radius:6px;'
        f'padding:6px 10px;margin:3px 0;">'
        f'  <div style="font-size:0.8rem;font-weight:600;color:#1e293b;">{name}</div>'
        f'  <div style="font-size:0.7rem;color:#64748b;margin-top:1px;">'
        f'    <span style="color:{border};font-weight:600;">{arrow}</span>'
        f'    &nbsp;·&nbsp; seen {count:,}×'
        f'    &nbsp;·&nbsp; avg {gap_label} gap'
        f'    &nbsp;·&nbsp; {conf_icon} {conf_label}{ag_note}'
        f'  </div>'
        f'</div>'
    )


def _render_peer_benchmark_panel(incident_text: str, client_id: str = "client_a", classification: str = "") -> None:
    """Render the PEER BENCHMARK section — temporal before/after correlation chains."""
    cache_key = f"temporal_{hash(incident_text)}_{classification}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = _fetch_temporal_patterns(incident_text, classification)
    data = st.session_state[cache_key]

    status = data.get("status", "")
    if status == "index_missing":
        st.info(
            "**Temporal index not built yet.**  \n"
            "Run `docker exec aiops-app python3 tools/build_temporal_index.py`."
        )
        return
    if status == "no_data":
        st.caption("No temporal correlation data found for this incident type.")
        return

    cls_key    = data.get("classification", "")
    total      = data.get("total_incidents", 0)
    all_before = [p for p in data.get("before_patterns", []) if p.get("classification") != "unclassified"]
    all_after  = [p for p in data.get("after_patterns",  []) if p.get("classification") != "unclassified"]
    before_cats = data.get("before_categories", [])
    after_cats  = data.get("after_categories", [])

    # Default: show HIGH + MEDIUM confidence only. LOW = likely spurious cross-infra noise.
    show_low_key = f"show_low_conf_{hash(incident_text)}"
    show_low = st.session_state.get(show_low_key, False)

    high_med_before = [p for p in all_before if p.get("confidence") in ("high", "medium")]
    high_med_after  = [p for p in all_after  if p.get("confidence") in ("high", "medium")]
    low_count = len([p for p in all_before + all_after if p.get("confidence") == "low"])

    before_pats = all_before if show_low else high_med_before
    after_pats  = all_after  if show_low else high_med_after

    st.markdown(
        f'<div style="font-size:0.78rem;color:#475569;margin-bottom:10px;">'
        f'Based on <strong>{total:,}</strong> peer incidents classified as '
        f'<strong>{nice_label(cls_key)}</strong> across 4 client environments'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Timeline flow: before → THIS INCIDENT → after ────────────────────
    st.markdown("**Incident correlation chain — what typically happens before and after:**")

    # Three-column flow layout
    col_before, col_center, col_after = st.columns([2, 1, 2])

    with col_before:
        st.markdown(
            '<div style="font-size:0.78rem;font-weight:700;color:#b45309;margin-bottom:6px;">'
            '⬆ BEFORE — Precursor Signals</div>',
            unsafe_allow_html=True,
        )
        if before_pats:
            chips = "".join(
                _pattern_chip(
                    p["classification"], p["co_occurrence_count"], p["avg_gap_label"], "before",
                    confidence=p.get("confidence", "medium"), same_ag_pct=p.get("same_ag_pct", 0)
                )
                for p in before_pats[:5]
            )
            st.markdown(chips, unsafe_allow_html=True)
        else:
            st.caption("No consistent precursor pattern identified.")

        if before_cats:
            cat_labels = ", ".join(f"{c} ({n})" for c, n in before_cats[:3])
            st.markdown(
                f'<div style="font-size:0.7rem;color:#92400e;margin-top:6px;">'
                f'📂 ITSM categories seen before: {cat_labels}</div>',
                unsafe_allow_html=True,
            )

    with col_center:
        st.markdown(
            f'<div style="text-align:center;padding:16px 4px;">'
            f'  <div style="font-size:0.65rem;color:#6b7280;margin-bottom:4px;">THIS INCIDENT</div>'
            f'  <div style="background:linear-gradient(135deg,#3b82f6,#1d4ed8);color:#fff;'
            f'    border-radius:8px;padding:10px 6px;font-size:0.72rem;font-weight:700;'
            f'    text-align:center;box-shadow:0 4px 12px rgba(59,130,246,0.35);">'
            f'    {nice_label(cls_key)}'
            f'  </div>'
            f'  <div style="font-size:0.62rem;color:#6b7280;margin-top:4px;">{total:,} peer cases</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with col_after:
        st.markdown(
            '<div style="font-size:0.78rem;font-weight:700;color:#166534;margin-bottom:6px;">'
            '⬇ AFTER — Cascading Effects</div>',
            unsafe_allow_html=True,
        )
        if after_pats:
            chips = "".join(
                _pattern_chip(
                    p["classification"], p["co_occurrence_count"], p["avg_gap_label"], "after",
                    confidence=p.get("confidence", "medium"), same_ag_pct=p.get("same_ag_pct", 0)
                )
                for p in after_pats[:5]
            )
            st.markdown(chips, unsafe_allow_html=True)
        else:
            st.caption("No consistent cascading effects identified.")

        if after_cats:
            cat_labels = ", ".join(f"{c} ({n})" for c, n in after_cats[:3])
            st.markdown(
                f'<div style="font-size:0.7rem;color:#14532d;margin-top:6px;">'
                f'📂 ITSM categories seen after: {cat_labels}</div>',
                unsafe_allow_html=True,
            )

    # ── Confidence toggle + reading guide ─────────────────────────────────
    if before_pats or after_pats or low_count > 0:
        st.markdown("---")
        guide_col, toggle_col = st.columns([3, 1])
        with guide_col:
            st.markdown(
                '<div style="font-size:0.72rem;color:#64748b;background:#f8fafc;'
                'border-radius:4px;padding:8px 12px;">'
                '📖 <strong>How to read this:</strong> Patterns show incident types that '
                'co-occurred within ±48h. '
                '🟢&nbsp;<strong>HIGH</strong> = same infrastructure domain. '
                '🟡&nbsp;<strong>MEDIUM</strong> = related infrastructure layers (e.g. network → DB). '
                '🔴&nbsp;<strong>LOW</strong> = different infrastructure stacks — likely noise. '
                '"% same team" = share of incidents from the same assignment group.'
                '</div>',
                unsafe_allow_html=True,
            )
        with toggle_col:
            if low_count > 0:
                toggle_label = f"Hide low confidence ({low_count})" if show_low else f"Show low confidence ({low_count})"
                if st.button(toggle_label, key=f"toggle_low_{hash(incident_text)}", use_container_width=True):
                    st.session_state[show_low_key] = not show_low
                    st.rerun()


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

def render_backtesting_page():
    from ui import collect_persisted_incidents, restore_last_incident_snapshot_if_needed

    st.subheader("Backtesting")
    st.caption(
        "Replay incidents against your own historical data and anonymised peer patterns. "
        "Select an incident below to see both your history and cross-client benchmarks side by side."
    )

    # ── Run Backtest button + metrics ────────────────────────────────────────
    bt_col1, bt_col2 = st.columns([5, 1])
    with bt_col2:
        _btn_disabled = st.session_state.get("_bt_running", False)
        _btn_label = "⏳ Running…" if _btn_disabled else "▶ Run Backtest"
        if st.button(_btn_label, key="run_backtest_btn", use_container_width=True, disabled=_btn_disabled):
            st.session_state["_bt_running"] = True
            st.session_state.pop("_bt_toast", None)
            try:
                resp = SESSION.post(f"{BACKEND_URL}/backtesting/run", timeout=120)
                resp.raise_for_status()
                d = resp.json()
                summary_data = d.get("summary", {}) or {}
                n = summary_data.get("case_count") or d.get("total_cases") or 0
                st.session_state["_bt_toast"] = ("ok", f"Backtest complete — {n} case(s) processed.")
            except Exception as exc:
                st.session_state["_bt_toast"] = ("err", f"Backtest failed: {exc}")
            finally:
                st.session_state["_bt_running"] = False
                st.rerun()

    _toast = st.session_state.pop("_bt_toast", None)
    if _toast:
        if _toast[0] == "ok":
            st.success(_toast[1])
        else:
            st.error(_toast[1])

    bt_data = _fetch_backtest_results()
    if bt_data.get("status") == "ok":
        bt_result = bt_data.get("data", {})
        summary = bt_result.get("summary", {}) or {}
        result_counts = summary.get("result_counts", {}) or {}
        total = summary.get("case_count") or 0
        passed = result_counts.get("pass", 0)
        partial = result_counts.get("partial", 0)
        failed = result_counts.get("fail", 0)
        avg_score = summary.get("average_score") or summary.get("avg_score") or 0.0
        run_at = (bt_result.get("generated_at") or "")[:16]

        st.markdown("#### Latest Backtest Run")
        r1, r2, r3, r4, r5 = st.columns(5)
        with r1:
            metric_card("Total Cases", str(total), "dashboard")
        with r2:
            metric_card("Passed", str(passed), "knowledge")
        with r3:
            metric_card("Partial", str(partial), "neutral")
        with r4:
            metric_card("Failed", str(failed), "automation")
        with r5:
            metric_card("Avg Score", f"{float(avg_score):.0%}" if avg_score else "—", "neutral")
        if run_at:
            st.caption(f"Last run: {run_at} · Mode: {bt_result.get('mode','offline')}")

        _RESULT_BADGE = {"pass": "✅ Pass", "partial": "🟡 Partial", "fail": "❌ Fail"}
        case_results = bt_result.get("results", []) or []
        if case_results:
            with st.expander(f"Case-level results ({len(case_results)})", expanded=False):
                rows = []
                for cr in case_results[:50]:
                    scores = cr.get("scores", {}) or {}
                    expected = cr.get("expected", {}) or {}
                    actual = cr.get("actual", {}) or {}
                    raw_result = scores.get("result") or scores.get("legacy_result") or ""
                    overall = scores.get("overall_score")
                    cls_exp = expected.get("classification", "")
                    cls_act = actual.get("classification", "")
                    cls_match = "✓" if cls_exp and cls_exp == cls_act else ("✗" if cls_exp else "")
                    rows.append({
                        "Case ID": cr.get("case_id", ""),
                        "Classification": nice_label(cls_exp),
                        "Cls Match": cls_match,
                        "Score": f"{overall:.0%}" if overall is not None else "—",
                        "Result": _RESULT_BADGE.get(raw_result.lower(), nice_label(raw_result) if raw_result else "—"),
                        "Mode": cr.get("mode", "offline"),
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True)

        # Trend chart
        try:
            hist_resp = SESSION.get(f"{BACKEND_URL}/backtesting/history", params={"limit": 30}, timeout=REQUEST_TIMEOUT_MEDIUM)
            hist_resp.raise_for_status()
            entries = hist_resp.json().get("entries", [])
            if len(entries) >= 2:
                import pandas as pd
                df = pd.DataFrame(entries)
                df["run_at"] = pd.to_datetime(df["run_at"], utc=True, errors="coerce")
                df = df.sort_values("run_at").dropna(subset=["run_at"])
                chart_cols = [c for c in ["average_score", "classification_accuracy", "rca_quality_average"] if c in df.columns]
                if chart_cols:
                    st.markdown("#### Accuracy Trend")
                    st.line_chart(df.set_index("run_at")[chart_cols].dropna(how="all"), use_container_width=True)
            elif len(entries) == 1:
                st.caption("Trend chart will appear after 2+ backtest runs.")
        except Exception:
            pass

    # ── Pattern Analysis: Recurring Incidents & Causal Chains ───────────────
    st.markdown("---")
    st.markdown("### 🔁 Pattern Analysis — Problem Management Intelligence")
    st.caption(
        "Historical analysis of your incident log. Identifies recurring patterns, "
        "frequency trends, and — without a topology map — automatically discovers which "
        "incidents reliably precede or follow each pattern. Use this to target root cause "
        "fixes rather than repeatedly resolving the same symptoms."
    )

    _run_col, _sp1, _sp2, _sp3, _sp4 = st.columns([1.6, 1, 1, 1, 1])
    with _run_col:
        if st.button("▶ Run Pattern Analysis", key="bt_run_pmc",
                     help="Scans incident history for recurring patterns and causal chains"):
            try:
                _resp = SESSION.post(
                    f"{BACKEND_URL}/run_global_pattern_analytics",
                    params={"dry_run": False},
                    timeout=120,
                )
                if _resp.ok:
                    _r = _resp.json()
                    st.session_state["_pmc_result"] = _r
                    st.success(
                        f"Done — {_r.get('total_candidates', 0)} recurring patterns identified, "
                        f"{_r.get('with_causal_chain', 0)} with causal chains, "
                        f"{_r.get('high_priority', 0)} high priority."
                    )
                    st.rerun()
                else:
                    st.error("Pattern analysis failed.")
            except Exception as _e:
                st.error(f"Pattern analysis error: {_e}")

    # Fetch candidates (live, read-only)
    _pmc_candidates: list = []
    try:
        _pr = SESSION.get(f"{BACKEND_URL}/global_pattern_stats", timeout=30)
        if _pr.ok:
            _pmc_candidates = _pr.json().get("candidates", [])
    except Exception:
        pass

    if _pmc_candidates:
        _high   = sum(1 for c in _pmc_candidates if c.get("problem_management_priority") == "high")
        _medium = sum(1 for c in _pmc_candidates if c.get("problem_management_priority") == "medium")
        _chains = sum(1 for c in _pmc_candidates if c.get("has_causal_chain"))
        _up     = sum(1 for c in _pmc_candidates if c.get("trend") == "increasing")

        with _sp1: metric_card("Patterns", str(len(_pmc_candidates)), "dashboard")
        with _sp2: metric_card("🔴 High", str(_high), "automation")
        with _sp3: metric_card("With Chain", str(_chains), "knowledge")
        with _sp4: metric_card("↑ Increasing", str(_up), "neutral")

        # ── Summary table ────────────────────────────────────────────────────
        import pandas as pd
        _rows = []
        for c in _pmc_candidates[:25]:
            _rows.append({
                "Priority": {"high": "🔴 High", "medium": "🟡 Medium", "low": "🟢 Low"}.get(
                    c.get("problem_management_priority", "low"), "—"),
                "Pattern": c["classification"].replace("_", " ").title(),
                "Occurrences": c["occurrence_count"],
                "Trend": f"{c.get('trend_arrow','→')} {c.get('trend','').title()}",
                "Variance": c.get("variance", "").title(),
                "Avg Confidence": round(c.get("avg_confidence", 0), 3),
                "Recurs every": f"{c['mean_time_between_hours']}h" if c.get("mean_time_between_hours") else "—",
                "Chain": "✅" if c.get("has_causal_chain") else "—",
            })
        _df = pd.DataFrame(_rows)
        st.dataframe(
            _df, use_container_width=True, hide_index=True,
            column_config={
                "Occurrences": st.column_config.ProgressColumn(
                    min_value=0, max_value=max(r["Occurrences"] for r in _rows) or 1, format="%d"),
                "Avg Confidence": st.column_config.ProgressColumn(
                    min_value=0.0, max_value=1.0, format="%.3f"),
            },
        )

        # ── Causal chain cards ───────────────────────────────────────────────
        _chain_cands = [c for c in _pmc_candidates if c.get("has_causal_chain")]
        if _chain_cands:
            st.markdown("#### Causal Chain Detail")
            st.caption(
                "Each card shows what reliably happens **before** (upstream root cause signal) "
                "and **after** (downstream blast radius) each recurring pattern. "
                "Fix the upstream cause — prevent both."
            )

            for _c in _chain_cands[:10]:
                _cls      = _c["classification"].replace("_", " ").title()
                _priority = _c.get("problem_management_priority", "low")
                _arrow    = _c.get("trend_arrow", "→")
                _count    = _c.get("occurrence_count", 0)
                _variance = _c.get("variance", "low")
                _upstream = _c.get("upstream_precursors", [])
                _dnstream = _c.get("downstream_impacts", [])
                _rec      = _c.get("recommendation", "")
                _mtbo     = _c.get("mean_time_between_hours")

                _pcol  = {"high": "#ef4444", "medium": "#f59e0b", "low": "#10b981"}.get(_priority, "#6b7280")
                _acol  = {"↑": "#ef4444", "↓": "#10b981", "→": "#94a3b8"}.get(_arrow, "#94a3b8")
                _vcol  = {"low": "#10b981", "medium": "#f59e0b", "high": "#ef4444"}.get(_variance, "#6b7280")
                _vdots = {"low": "●○○", "medium": "●●○", "high": "●●●"}.get(_variance, "●○○")

                # Build upstream HTML nodes
                def _node(text: str, sub: str, side: str) -> str:
                    bg   = "#fef3c7" if side == "up" else "#dcfce7"
                    bord = "#f59e0b" if side == "up" else "#22c55e"
                    align = "right" if side == "up" else "left"
                    return (
                        f'<div style="background:{bg};border:1.5px solid {bord};border-radius:10px;'
                        f'padding:10px 14px;text-align:{align};min-width:160px;max-width:200px;">'
                        f'<div style="font-size:0.78rem;font-weight:700;color:#1f2937;line-height:1.3;">{text}</div>'
                        f'<div style="font-size:0.69rem;color:#64748b;margin-top:3px;">{sub}</div>'
                        f'</div>'
                    )

                def _arrow_seg(label: str, direction: str) -> str:
                    col = "#f59e0b" if direction == "up" else "#22c55e"
                    sym = "──▶" if direction == "dn" else "◀──"
                    return (
                        f'<div style="display:flex;flex-direction:column;align-items:center;'
                        f'justify-content:center;padding:0 6px;min-width:80px;">'
                        f'<div style="font-size:1.1rem;color:{col};">{sym}</div>'
                        f'<div style="font-size:0.65rem;color:#94a3b8;text-align:center;">{label}</div>'
                        f'</div>'
                    )

                # Anchor node (center)
                _anchor_html = (
                    f'<div style="background:#1e293b;border:2px solid {_pcol};border-radius:12px;'
                    f'padding:14px 18px;text-align:center;min-width:180px;">'
                    f'<div style="font-size:0.65rem;color:{_pcol};font-weight:700;letter-spacing:0.08em;'
                    f'text-transform:uppercase;margin-bottom:4px;">{_priority} priority</div>'
                    f'<div style="font-size:0.9rem;font-weight:800;color:#f1f5f9;line-height:1.25;">{_cls}</div>'
                    f'<div style="margin-top:8px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap;">'
                    f'<span style="font-size:0.68rem;background:#334155;color:#cbd5e1;padding:2px 7px;'
                    f'border-radius:20px;">{_count} occurrences</span>'
                    f'<span style="font-size:0.68rem;color:{_acol};font-weight:700;">{_arrow} {_c.get("trend","").title()}</span>'
                    f'</div>'
                    f'<div style="margin-top:6px;font-size:0.65rem;color:{_vcol};">'
                    f'{_vdots} {_variance.title()} variance'
                    + (f' · recurs ~{_mtbo}h' if _mtbo else "")
                    + f'</div></div>'
                )

                # Upstream column
                _up_html = ""
                if _upstream:
                    for _u in _upstream[:2]:
                        _ut = _u["classification"].replace("_", " ").title()
                        _ul = f"{int(_u['hit_rate']*100)}% · avg {_u['avg_minutes_before']}m before"
                        _up_html += _node(_ut, _ul, "up")
                        _up_html += '<div style="height:8px;"></div>'
                else:
                    _up_html = '<div style="font-size:0.72rem;color:#94a3b8;padding:8px;">No upstream signal detected</div>'

                # Downstream column
                _dn_html = ""
                if _dnstream:
                    for _d in _dnstream[:2]:
                        _dt = _d["classification"].replace("_", " ").title()
                        _dl = f"{int(_d['hit_rate']*100)}% · avg {_d['avg_minutes_after']}m after"
                        _dn_html += _node(_dt, _dl, "dn")
                        _dn_html += '<div style="height:8px;"></div>'
                else:
                    _dn_html = '<div style="font-size:0.72rem;color:#94a3b8;padding:8px;">No downstream impact detected</div>'

                _up_arrow = _arrow_seg(
                    f"{_upstream[0]['avg_minutes_before']}m" if _upstream else "", "up"
                ) if _upstream else '<div style="width:60px;"></div>'
                _dn_arrow = _arrow_seg(
                    f"{_dnstream[0]['avg_minutes_after']}m" if _dnstream else "", "dn"
                ) if _dnstream else '<div style="width:60px;"></div>'

                _chain_html = (
                    f'<div style="display:flex;align-items:center;justify-content:center;'
                    f'gap:0;padding:16px 0;flex-wrap:wrap;">'
                    f'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:0;">'
                    f'{_up_html}</div>'
                    f'{_up_arrow}'
                    f'{_anchor_html}'
                    f'{_dn_arrow}'
                    f'<div style="display:flex;flex-direction:column;align-items:flex-start;gap:0;">'
                    f'{_dn_html}</div>'
                    f'</div>'
                )

                with st.expander(
                    f"{_cls}  ·  {_count} occurrences  ·  {_arrow} {_c.get('trend','').title()}  "
                    f"·  {'🔗 Causal chain' if (_upstream or _dnstream) else ''}",
                    expanded=False,
                ):
                    st.markdown(_chain_html, unsafe_allow_html=True)

                    if _rec:
                        st.markdown(
                            f'<div style="margin-top:12px;padding:12px 16px;background:#f0f9ff;'
                            f'border-left:4px solid #0ea5e9;border-radius:6px;font-size:0.82rem;color:#0c4a6e;">'
                            f'<strong>Problem Management Recommendation:</strong><br>{_rec}</div>',
                            unsafe_allow_html=True,
                        )
    else:
        st.caption("Click **▶ Run Pattern Analysis** to analyse your incident history for recurring patterns and causal chains.")

    # ── Incident selector ────────────────────────────────────────────────────
    st.markdown("---")
    incidents = collect_persisted_incidents()
    if not incidents:
        restore_last_incident_snapshot_if_needed()
        incidents = collect_persisted_incidents()

    if not incidents:
        render_empty_state(
            "🔁", "No incidents available",
            "Analyse an incident first, then return here to replay it against your history and peer benchmarks.",
            "Go to Incident Console → analyse an incident → return here."
        )
        return

    selected_label = st.selectbox("Select incident to replay", options=[x["label"] for x in incidents])
    selected = next((x for x in incidents if x["label"] == selected_label), incidents[0])
    result = selected["result"]
    analysis = result.get("analysis", {}) or {}
    selected_ticket = selected.get("ticket") or st.session_state.get("last_incident_ticket", "")

    if selected_ticket:
        with st.expander("Incident text used for replay", expanded=False):
            st.write(selected_ticket)

    classification = analysis.get("classification", "")
    confidence = result.get("confidence", {}).get("overall_confidence", 0.0)
    retrieved = result.get("retrieved_incidents", []) or []

    h1, h2, h3 = st.columns(3)
    with h1:
        metric_card("Classification", nice_label(classification) if classification else "—")
    with h2:
        metric_card("Confidence", format_confidence(confidence, 2))
    with h3:
        metric_card("Vector Matches", str(len(retrieved)))

    # Search text for both panels: prefer ticket text, fall back to classification
    search_text = selected_ticket or nice_label(classification)

    st.markdown("---")

    # ── Two-panel layout ─────────────────────────────────────────────────────
    left_col, right_col = st.columns(2, gap="large")

    with left_col:
        st.markdown(
            '<div style="background:#f0f4ff;border-left:4px solid #6366f1;padding:8px 12px;'
            'border-radius:4px;margin-bottom:12px;">'
            '<span style="font-size:0.9rem;font-weight:700;color:#3730a3;">🏢 Your History</span>'
            '<span style="font-size:0.75rem;color:#6b7280;margin-left:8px;">How this pattern appeared in your own past incidents</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        _render_self_history_panel(search_text, client_id="client_a")

    with right_col:
        st.markdown(
            '<div style="background:#f0fdf4;border-left:4px solid #22c55e;padding:8px 12px;'
            'border-radius:4px;margin-bottom:12px;">'
            '<span style="font-size:0.9rem;font-weight:700;color:#15803d;">🌐 Peer Benchmark</span>'
            '<span style="font-size:0.75rem;color:#6b7280;margin-left:8px;">Anonymised patterns from other client environments</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        _render_peer_benchmark_panel(search_text, client_id="client_a", classification=classification)

    # ── Pattern Replay (from vector search) ─────────────────────────────────────────
    if retrieved:
        st.markdown("---")
        section_title("Knowledge Base Matches (vector retrieval)")
        for row in retrieved[:8]:
            title = nice_label(row.get("classification", "unknown"))
            score = format_confidence(row.get("score"), 2)
            domain = str(row.get("domain", "general")).title()
            st.write(f"• **{title}** | Domain: {domain} | Score: {score}")

    # ── Symptoms ─────────────────────────────────────────────────────────────
    symptoms_before = analysis.get("probable_causes", []) or []
    symptoms_after = analysis.get("recommended_actions", []) or []
    if symptoms_before or symptoms_after:
        st.markdown("---")
        section_title("Symptoms & Recommended Actions")
        lc, rc = st.columns(2)
        with lc:
            st.write("**Probable Causes**")
            for item in symptoms_before[:5]:
                st.write(f"• {natural_label(item)}")
        with rc:
            st.write("**Recommended Actions**")
            for item in symptoms_after[:5]:
                st.write(f"• {natural_label(item)}")
