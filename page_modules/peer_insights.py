"""Peer Insights panel — pattern-matched from peer historical incidents.

Passes the live incident's short_description directly to the backend,
which tokenises it and looks up matching terms in the inverted token index.
No keyword classification — pure text pattern matching.
"""
from __future__ import annotations
from typing import Any, Dict, Optional
import streamlit as st
import re as _re
from typing import List
from page_modules.shared import BACKEND_URL, SESSION, REQUEST_TIMEOUT_SHORT


def _fetch_peer_insights(
    incident_text: str,
    client_id: Optional[str] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"incident_text": incident_text, "max_peers": 5}
    if client_id:
        params["client_id"] = client_id
    try:
        r = SESSION.get(
            f"{BACKEND_URL}/backtesting/client/peer_insights",
            params=params,
            timeout=REQUEST_TIMEOUT_SHORT,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _mini_bar(value: float, max_val: float, color: str, width: int = 140) -> str:
    pct = min(100, (value / max_val * 100)) if max_val else 0
    fill = max(4, int(width * pct / 100))
    return (
        f'<div style="display:inline-block;width:{width}px;background:#f1f5f9;'
        f'border-radius:3px;height:10px;vertical-align:middle;">'
        f'<div style="width:{fill}px;background:{color};height:10px;border-radius:3px;"></div>'
        f'</div>'
    )


def _timeline_chart(timeline: Dict[str, Any]) -> str:
    by_year = timeline.get("by_year", {})
    if not by_year:
        return ""
    max_cnt = max(by_year.values()) or 1
    ref_year = str(timeline.get("ref_year", ""))
    bars = ""
    for yr, cnt in sorted(by_year.items()):
        h = max(4, int(60 * cnt / max_cnt))
        is_ref = yr == ref_year
        bar_color = "#ef4444" if is_ref else "#3b82f6"
        label_style = "font-weight:700;color:#ef4444;" if is_ref else "color:#6b7280;"
        bars += (
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:2px;">'
            f'  <span style="font-size:0.65rem;color:#6b7280;">{cnt:,}</span>'
            f'  <div style="width:28px;height:{h}px;background:{bar_color};border-radius:3px 3px 0 0;"></div>'
            f'  <span style="font-size:0.65rem;{label_style}">{yr}</span>'
            f'</div>'
        )
    before = timeline.get("before_count", 0)
    during = timeline.get("during_count", 0)
    return (
        f'<div style="display:flex;gap:4px;align-items:flex-end;margin:8px 0 4px 0;">{bars}</div>'
        f'<div style="font-size:0.75rem;color:#64748b;margin-top:2px;">'
        f'  <span style="margin-right:12px;">📅 Before: <strong>{before:,}</strong> incidents in peer environments</span>'
        f'  <span>📌 This year: <strong>{during:,}</strong></span>'
        f'</div>'
    )


def _render_client_history(rag_context: Dict[str, Any]) -> None:
    """Render this client's own past incidents from RAG alongside peer data."""
    incident_patterns = rag_context.get("incident_patterns") or []
    sources = rag_context.get("sources") or []
    _ts = _re.compile(r"^\d{4}-\d{2}-\d{2}")

    def _ok(v: str) -> bool:
        v = (v or "").strip()
        return len(v) >= 30 and not _ts.match(v) and not v.lower().startswith(
            ("work notes", "system administrator", "inc", "itm")
        )

    rca_keys = ["Root Cause (Work Notes)", "Root Cause", "Work Notes", "Details", "Executive Summary"]
    res_keys = ["Resolution", "Close Notes", "Details/Description"]
    inc_keys = ["INC#", "Number", "Incident Number"]
    sum_keys = ["Short Description", "Summary", "Description"]

    entries = []
    for i, pat in enumerate(incident_patterns[:4], 1):
        fields = pat.get("fields") or {}
        inc_num = next((fields[k] for k in inc_keys if k in fields), f"Ref {i}")
        summary = next((fields[k] for k in sum_keys if k in fields and _ok(fields[k])), "")
        rca     = next((fields[k] for k in rca_keys if k in fields and _ok(fields[k])), "")
        res     = next((fields[k] for k in res_keys if k in fields and _ok(fields[k])), "")
        if rca or res or summary:
            entries.append({"inc": inc_num, "summary": summary[:120],
                            "rca": rca[:250], "res": res[:250],
                            "src": pat.get("source_file", "")})

    if not entries:
        return

    src_label = ", ".join(sources) if sources else "client knowledge base"
    st.markdown(
        f"<div style='font-size:0.8rem;font-weight:700;color:#475569;"
        f"text-transform:uppercase;letter-spacing:.07em;margin:10px 0 6px;'>"
        f"This Client's Past Incidents"
        f"<span style='font-weight:400;font-size:0.72rem;color:#94a3b8;"
        f"margin-left:8px;text-transform:none;'>from {src_label}</span></div>",
        unsafe_allow_html=True,
    )
    for e in entries:
        body = ""
        if e["summary"]:
            body += f"<div style='font-size:0.78rem;color:#64748b;margin-bottom:4px;'>{e['summary']}</div>"
        if e["rca"]:
            body += (f"<div style='font-size:0.8rem;margin-bottom:3px;'>"
                     f"<b style='color:#b45309;'>Root Cause:</b> {e['rca']}</div>")
        if e["res"]:
            body += (f"<div style='font-size:0.8rem;'>"
                     f"<b style='color:#15803d;'>Resolution:</b> {e['res']}</div>")
        st.markdown(
            f"<div style='border:1px solid #e2e8f0;border-left:3px solid #0d9488;"
            f"border-radius:5px;padding:8px 12px;margin-bottom:6px;background:#f8fafc;'>"
            f"<div style='display:flex;justify-content:space-between;margin-bottom:4px;'>"
            f"<span style='font-size:0.8rem;font-weight:700;color:#0f766e;'>{e['inc']}</span>"
            f"<span style='font-size:0.68rem;color:#94a3b8;'>{e['src']}</span></div>"
            f"{body}</div>",
            unsafe_allow_html=True,
        )


def render_peer_insights_panel(
    incident_text: str,
    client_id: Optional[str] = "client_a",
    rag_context: Optional[Dict[str, Any]] = None,
) -> None:
    """Render the peer insights expander below an incident analysis result.

    Args:
        incident_text: The incident short_description (or any descriptive text).
                       Passed directly to the backend for token pattern matching.
        client_id:     The requesting client — excluded from the peer list.
        rag_context:   Client RAG context from the analysis result — used to show
                       this client's own past incidents alongside anonymous peer data.
    """
    if not incident_text or not incident_text.strip():
        return

    cache_key = f"peer_insights_{hash(incident_text)}_{client_id}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = None

    if st.session_state[cache_key] is None:
        data = _fetch_peer_insights(incident_text, client_id)
        st.session_state[cache_key] = data
    else:
        data = st.session_state[cache_key]

    status = data.get("status", "")
    if status in ("index_missing", "no_data", ""):
        return

    peer_count = data.get("peer_environments", 0)
    total_peer_inc = data.get("total_peer_incidents", 0)
    overall_avg_rh = data.get("overall_avg_resolve_hours")
    peers = data.get("peers", [])
    timeline = data.get("timeline", {})
    matched_terms = data.get("matched_terms", [])

    if not peers:
        return

    avg_rh_str = f"{overall_avg_rh:.1f}h" if overall_avg_rh is not None else "—"
    expander_title = (
        f"🌐 Peer Benchmark — **{peer_count}** other environment{'s' if peer_count != 1 else ''} "
        f"matched this pattern ({total_peer_inc:,} incidents, avg resolution {avg_rh_str})"
    )

    with st.expander(expander_title, expanded=False):
        st.caption(
            "Anonymised view: similar incidents found in peer environments by direct text pattern matching. "
            "No client identities are disclosed."
        )

        if matched_terms:
            tags = " ".join(
                f'<span style="display:inline-block;background:#dbeafe;color:#1e40af;'
                f'font-size:0.72rem;padding:1px 7px;border-radius:10px;margin:2px 3px 2px 0;">'
                f'{t}</span>'
                for t in matched_terms[:10]
            )
            st.markdown(
                f'<div style="margin-bottom:8px;">Matched on: {tags}</div>',
                unsafe_allow_html=True,
            )

        # Timeline
        tl_html = _timeline_chart(timeline)
        if tl_html:
            st.markdown("**Historical occurrence across peer environments:**")
            st.markdown(tl_html, unsafe_allow_html=True)
            st.markdown("")

        # Per-peer cards
        st.markdown("**Peer environment breakdown:**")
        max_count = max(p["count"] for p in peers) or 1

        for peer in peers:
            pid = peer["peer_id"]
            cnt = peer["count"]
            avg_rh = peer.get("avg_resolve_hours")
            pct_ra = peer.get("pct_with_reassignment")
            categories = peer.get("top_categories", [])
            snippets = peer.get("resolution_snippets", [])
            hits = peer.get("token_hits", 0)

            avg_rh_lbl = f"{avg_rh:.1f}h" if avg_rh is not None else "—"
            ra_lbl = f"{pct_ra:.0f}%" if pct_ra is not None else "—"
            cat_lbl = ", ".join(c for c, _ in categories[:2]) if categories else "—"

            bar_html = _mini_bar(cnt, max_count, "#3b82f6")
            hits_badge = (
                f'<span style="font-size:0.68rem;color:#6366f1;margin-left:8px;">'
                f'{hits} pattern{"s" if hits != 1 else ""} matched</span>'
            )

            col_bar, col_stats, col_res = st.columns([3, 2, 3])
            with col_bar:
                st.markdown(
                    f'<div style="margin:6px 0;">'
                    f'  <span style="font-size:0.82rem;font-weight:600;color:#1e293b;">{pid}</span>'
                    f'  {hits_badge}'
                    f'  <div style="margin-top:4px;">{bar_html} <span style="font-size:0.78rem;color:#475569;margin-left:6px;">{cnt:,} incidents</span></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with col_stats:
                st.markdown(
                    f'<div style="font-size:0.78rem;color:#475569;margin-top:6px;line-height:1.8;">'
                    f'  ⏱ Avg resolve: <strong>{avg_rh_lbl}</strong><br>'
                    f'  🔄 Reassigned: <strong>{ra_lbl}</strong><br>'
                    f'  📂 Category: <strong>{cat_lbl}</strong>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with col_res:
                if snippets:
                    st.markdown(
                        f'<div style="font-size:0.75rem;color:#64748b;margin-top:6px;">'
                        f'  <em>How peers resolved it:</em><br>'
                        + "".join(
                            f'  <div style="margin-top:3px;padding:3px 6px;background:#f8fafc;'
                            f'border-left:2px solid #cbd5e1;border-radius:2px;">"{s}"</div>'
                            for s in snippets[:2]
                        )
                        + "</div>",
                        unsafe_allow_html=True,
                    )

        # This client's own past incidents from RAG
        if rag_context:
            st.markdown("---")
            _render_client_history(rag_context)

        # Before/after callout
        before = timeline.get("before_count", 0)
        during = timeline.get("during_count", 0)
        ref_year = timeline.get("ref_year")
        if before or during:
            st.markdown("")
            if before > 0:
                st.info(
                    f"**Before {ref_year}:** {before:,} similar incidents recorded across peer environments — "
                    f"this is a known recurring pattern. Peer resolution data above can guide remediation."
                )
            if during > 0:
                st.warning(
                    f"**This year ({ref_year}):** {during:,} peer incidents of this type are active — "
                    f"this may be a widespread current issue, not an isolated occurrence."
                )
