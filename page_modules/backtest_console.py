"""Backtest Console — multi-client incident pattern correlation."""
from __future__ import annotations
import streamlit as st
from page_modules.shared import BACKEND_URL, SESSION, REQUEST_TIMEOUT_LONG, REQUEST_TIMEOUT_MEDIUM, nice_label


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict:
    r = SESSION.get(f"{BACKEND_URL}{path}", params=params, timeout=REQUEST_TIMEOUT_MEDIUM)
    r.raise_for_status()
    return r.json()


def _post(path: str, params: dict | None = None) -> dict:
    r = SESSION.post(f"{BACKEND_URL}{path}", params=params, timeout=REQUEST_TIMEOUT_LONG)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

_CLIENT_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16"]

def _client_color(idx: int) -> str:
    return _CLIENT_COLORS[idx % len(_CLIENT_COLORS)]


def _priority_badge(p: str) -> str:
    colors = {"1": "#dc2626", "2": "#ea580c", "3": "#d97706", "4": "#16a34a", "5": "#6b7280"}
    c = colors.get(str(p), "#6b7280")
    label = {"1": "P1-Critical", "2": "P2-Major", "3": "P3-Moderate", "4": "P4-Low", "5": "P5-Planning"}.get(str(p), f"P{p}")
    return f'<span style="background:{c};color:#fff;border-radius:4px;padding:1px 7px;font-size:0.73rem;font-weight:600;">{label}</span>'


def _bar_html(value: float, max_val: float, color: str, label: str, width: int = 180) -> str:
    pct = min(100, (value / max_val * 100)) if max_val else 0
    bar_w = int(width * pct / 100)
    return (
        f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px;">'
        f'  <div style="width:{width}px;background:#f3f4f6;border-radius:4px;height:14px;overflow:hidden;">'
        f'    <div style="width:{bar_w}px;background:{color};height:14px;border-radius:4px;"></div>'
        f'  </div>'
        f'  <span style="font-size:0.78rem;color:#374151;">{label}</span>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------

def render_backtest_console_page():
    st.subheader("Backtest Console")
    st.caption("Cross-client incident pattern correlation — classify historical incidents and identify patterns shared across clients.")

    # --- Ingest status ---
    try:
        ingest = _get("/backtesting/client/ingest_status")
    except Exception as e:
        st.warning(
            f"**Backtest API unavailable** — {type(e).__name__}. "
            "The backend may still be starting up. Wait a moment and refresh."
        )
        if st.button("Retry", key="backtest_retry"):
            st.rerun()
        return

    if ingest.get("status") == "missing":
        st.warning("No client incident data loaded yet. Run `python3 tools/backtest_ingest.py` inside the container.")
        return

    by_client = ingest.get("by_client", {})
    total = ingest.get("total", 0)
    clients = sorted(by_client.keys())

    # Top metrics
    mc = st.columns(len(clients) + 1)
    with mc[0]:
        st.metric("Total Incidents", f"{total:,}")
    for idx, cid in enumerate(clients):
        with mc[idx + 1]:
            st.metric(cid, f"{by_client[cid]:,}")

    st.markdown("---")

    # --- Run controls ---
    col_run, col_filter, col_limit = st.columns([2, 3, 2])
    with col_filter:
        selected_clients = st.multiselect(
            "Filter clients",
            options=clients,
            default=clients,
            key="bt_client_filter",
        )
    with col_limit:
        use_limit = st.checkbox("Cap incidents", value=False, key="bt_use_limit")
        limit_val = st.number_input("Max incidents", min_value=1000, max_value=total, value=min(50000, total), step=5000, key="bt_limit_val") if use_limit else None
    with col_run:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        run_clicked = st.button("▶ Run Pattern Correlation", key="bt_run_btn", use_container_width=True, type="primary")

    if run_clicked:
        params: dict = {}
        if selected_clients and set(selected_clients) != set(clients):
            params["clients"] = ",".join(selected_clients)
        if limit_val:
            params["limit"] = int(limit_val)
        try:
            resp = _post("/backtesting/client/run", params=params)
            if resp.get("status") == "already_running":
                st.warning("Backtest is already running. Refresh to check progress.")
            else:
                st.success("Backtest started — this may take 30–120 seconds for large datasets. Results will appear below when complete.")
                st.rerun()
        except Exception as e:
            st.error(f"Failed to start backtest: {e}")
        return

    # --- Status ---
    try:
        status_resp = _get("/backtesting/client/status")
    except Exception as e:
        st.error(f"Status check failed: {e}")
        return

    run_info = status_resp.get("run", {})
    last_report = status_resp.get("last_report", {})

    if run_info.get("status") == "running":
        st.info(f"Backtest running… started {run_info.get('started_at','')[:19]}. Refresh to check progress.")
        return
    if run_info.get("status") == "failed":
        st.error(f"Last run failed: {run_info.get('error','unknown')}")

    if not last_report:
        st.info("No report yet — click **Run Pattern Correlation** to analyse the loaded incidents.")
        return

    # --- Report ---
    summary = last_report.get("summary", {})
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Incidents Analysed", f"{last_report.get('total_incidents', 0):,}")
    s2.metric("Classifications Found", summary.get("total_classifications", "—"))
    s3.metric("Patterns Shared 2+ Clients", summary.get("shared_across_2plus_clients", "—"))
    s4.metric("Unclassified", f"{summary.get('unclassified_count', 0):,}")

    st.markdown(f"*Report generated: {last_report.get('generated_at','')[:19]}*")

    # Fetch full report
    try:
        report_resp = _get("/backtesting/client/report", params={"top_n": 30, "min_clients": 1})
    except Exception as e:
        st.error(f"Failed to load report: {e}")
        return

    shared = report_resp.get("shared_patterns", [])
    by_client_full = report_resp.get("by_client", {})
    resolve_stats = report_resp.get("resolution_stats", {})
    reassign_stats = report_resp.get("reassignment_stats", {})
    report_clients = sorted(report_resp.get("clients", []))

    # --- Tabs ---
    tab_shared, tab_per_client, tab_resolution, tab_matrix = st.tabs([
        "Shared Patterns", "Per-Client Breakdown", "Resolution Times", "Pattern Matrix"
    ])

    # ---- Tab 1: Shared patterns ----
    with tab_shared:
        st.markdown("#### Patterns Shared Across Multiple Clients")
        st.caption("Classifications that appear in 2 or more clients indicate recurring, cross-environment problems that the AI has learned to recognise.")

        min_c = st.slider("Min clients sharing pattern", 1, max(2, len(report_clients)), 2, key="bt_min_clients")
        filtered_shared = [p for p in shared if p.get("client_count", 0) >= min_c]

        if not filtered_shared:
            st.info("No patterns match the filter. Try reducing min clients.")
        else:
            max_total = max(p["total_incidents"] for p in filtered_shared) or 1
            for pat in filtered_shared:
                cls = pat["classification"]
                n_clients = pat["client_count"]
                total_inc = pat["total_incidents"]
                c_data = pat.get("clients", {})
                rstat = pat.get("resolve_stats", {})
                reostat = pat.get("reopen_stats", {})

                with st.expander(
                    f"**{nice_label(cls)}** — {n_clients} clients, {total_inc:,} incidents",
                    expanded=n_clients >= 3,
                ):
                    pc1, pc2 = st.columns([3, 2])
                    with pc1:
                        st.markdown("**Client distribution:**")
                        for idx, (cid, cinfo) in enumerate(sorted(c_data.items())):
                            color = _client_color(report_clients.index(cid) if cid in report_clients else idx)
                            cnt = cinfo.get("count", 0)
                            share = cinfo.get("share_pct", 0)
                            st.markdown(
                                _bar_html(cnt, max(v.get("count", 0) for v in c_data.values()), color,
                                          f"{cid}: {cnt:,} ({share}% of that client)"),
                                unsafe_allow_html=True,
                            )
                    with pc2:
                        if rstat:
                            st.markdown("**Resolution time:**")
                            st.markdown(f"- Mean: **{rstat.get('mean_h','—')} h**")
                            st.markdown(f"- Median: **{rstat.get('median_h','—')} h**")
                            st.markdown(f"- P90: **{rstat.get('p90_h','—')} h**")
                        if reostat:
                            st.markdown("**Reassignment:**")
                            st.markdown(f"- Avg reassignments: **{reostat.get('mean_reassignments','—')}**")
                            st.markdown(f"- % reassigned: **{reostat.get('pct_reassigned','—')}%**")

    # ---- Tab 2: Per-client breakdown ----
    with tab_per_client:
        st.markdown("#### Top Classifications Per Client")
        for idx, cid in enumerate(report_clients):
            cinfo = by_client_full.get(cid, {})
            total_c = cinfo.get("total", 0)
            top_cls = cinfo.get("top_classifications", [])
            color = _client_color(idx)
            with st.expander(f"**{cid}** — {total_c:,} incidents", expanded=idx == 0):
                if not top_cls:
                    st.info("No classifications available.")
                    continue
                max_cnt = top_cls[0][1] if top_cls else 1
                for cls, cnt in top_cls:
                    pct = round(100 * cnt / total_c, 1) if total_c else 0
                    st.markdown(
                        _bar_html(cnt, max_cnt, color, f"{nice_label(cls)}: {cnt:,} ({pct}%)"),
                        unsafe_allow_html=True,
                    )

    # ---- Tab 3: Resolution times ----
    with tab_resolution:
        st.markdown("#### Resolution Time by Classification (hours)")
        st.caption("Based on created_at → resolved_at for incidents with both timestamps.")

        if not resolve_stats:
            st.info("No resolution time data available.")
        else:
            rows = sorted(resolve_stats.items(), key=lambda x: x[1].get("mean_h", 9999))
            tbl_data = []
            for cls, rs in rows:
                tbl_data.append({
                    "Classification": nice_label(cls),
                    "Count": rs.get("count", 0),
                    "Mean (h)": rs.get("mean_h", "—"),
                    "Median (h)": rs.get("median_h", "—"),
                    "P90 (h)": rs.get("p90_h", "—"),
                })
            st.dataframe(tbl_data, use_container_width=True, hide_index=True)

    # ---- Tab 4: Pattern matrix ----
    with tab_matrix:
        st.markdown("#### Cross-Client Pattern Matrix")
        st.caption("Each cell = number of incidents of that classification from that client. Darker = more incidents.")

        cross = report_resp.get("shared_patterns", [])
        if not cross:
            st.info("No shared pattern data.")
        else:
            # Build matrix HTML
            top_patterns = [p["classification"] for p in cross[:20]]
            header_cells = "".join(
                f'<th style="font-size:0.72rem;padding:4px 6px;text-align:center;white-space:nowrap;background:#1e293b;color:#fff;">{nice_label(c)[:20]}</th>'
                for c in top_patterns
            )
            max_count = max(
                p.get("clients", {}).get(cid, {}).get("count", 0)
                for p in cross[:20]
                for cid in report_clients
            ) or 1

            rows_html = ""
            for idx, cid in enumerate(report_clients):
                color = _client_color(idx)
                cells = ""
                for pat in cross[:20]:
                    cnt = pat.get("clients", {}).get(cid, {}).get("count", 0)
                    alpha = round(0.08 + 0.82 * cnt / max_count, 2) if cnt else 0
                    bg = f"rgba(59,130,246,{alpha})" if cnt else "#f9fafb"
                    txt_color = "#fff" if alpha > 0.5 else "#374151"
                    fw = "600" if cnt else "400"
                    cell_val = f"{cnt:,}" if cnt else "—"
                    cells += f'<td style="text-align:center;padding:5px 6px;background:{bg};color:{txt_color};font-size:0.78rem;font-weight:{fw};">{cell_val}</td>'
                rows_html += f'<tr><td style="padding:5px 10px;font-weight:600;font-size:0.8rem;color:{color};white-space:nowrap;">{cid}</td>{cells}</tr>'

            matrix_html = f"""
            <div style="overflow-x:auto;margin-top:8px;">
              <table style="border-collapse:collapse;font-family:sans-serif;min-width:100%;">
                <thead>
                  <tr>
                    <th style="background:#1e293b;color:#fff;padding:6px 10px;font-size:0.78rem;">Client</th>
                    {header_cells}
                  </tr>
                </thead>
                <tbody>
                  {rows_html}
                </tbody>
              </table>
            </div>
            """
            st.markdown(matrix_html, unsafe_allow_html=True)
