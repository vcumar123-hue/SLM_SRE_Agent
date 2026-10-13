"""Knowledge Center page module."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import traceback
import streamlit as st
from page_modules.shared import (
    BACKEND_URL, SESSION, REQUEST_TIMEOUT_MEDIUM,
    fetch_knowledge_sources, fetch_knowledge_candidates,
    fetch_resolution_by_classification, fetch_playbook_by_classification,
    nice_label, format_confidence, metric_card, section_title,
    render_empty_state, natural_label,
)
try:
    import settings_portable as _settings
    from file_manager import append_debug_event as _append_audit
    _AUDIT_FILE = _settings.PROMOTION_AUDIT_FILE
    _AUDIT_OK = True
except Exception:
    _AUDIT_OK = False


def _write_audit(event_type: str, payload: Dict[str, Any]) -> None:
    if not _AUDIT_OK:
        return
    try:
        _append_audit(_AUDIT_FILE, event_type=event_type, payload=payload, component="promotion")
    except Exception:
        pass  # audit write failure must never block the main flow


def _review_and_promote_candidate(candidate_id: str, reviewed_by: str, notes: str, tenant_id: str) -> tuple[bool, str]:
    """Four-step: review → promote → create resolution → create playbook. Mirrors Approve & Learn.
    Every step is a hard failure. All outcomes (success and failure) are written to promotion_audit.jsonl.
    """
    engineer = reviewed_by or "engineer"
    _ctx = {"candidate_id": candidate_id, "engineer": engineer, "tenant_id": tenant_id}

    # ── Step 1: Review / approve ──────────────────────────────────────────────
    try:
        rv = SESSION.post(
            f"{BACKEND_URL}/review_candidate",
            json={"candidate_id": candidate_id, "reviewed_by": engineer, "approved": True, "review_notes": notes},
            timeout=REQUEST_TIMEOUT_MEDIUM,
        )
        if not rv.ok:
            detail = rv.json().get("detail", rv.text)[:300]
            _write_audit("promotion_step1_review_failed", {**_ctx, "http_status": rv.status_code, "detail": detail})
            return False, f"Step 1 (Review) failed: {detail}"
        candidate = rv.json().get("candidate", {})
        _write_audit("promotion_step1_review_ok", {**_ctx, "classification": candidate.get("classification")})
    except Exception as exc:
        _write_audit("promotion_step1_review_exception", {**_ctx, "error": str(exc), "traceback": traceback.format_exc()})
        return False, f"Step 1 (Review) error: {exc}"

    # ── Step 2: Promote to the knowledge base ─────────────────────────────────────────────
    try:
        pv = SESSION.post(
            f"{BACKEND_URL}/promote_candidate",
            json={"candidate_id": candidate_id, "tenant_id": tenant_id or None,
                  "promote_to_global": False, "promoted_by": engineer},
            timeout=REQUEST_TIMEOUT_MEDIUM,
        )
        if not pv.ok:
            detail = pv.json().get("detail", pv.text)[:300]
            _write_audit("promotion_step2_promote_failed", {**_ctx, "http_status": pv.status_code, "detail": detail})
            return False, f"Step 2 (Promote) failed: {detail}"
        _write_audit("promotion_step2_promote_ok", {**_ctx, "incident_id": pv.json().get("incident", {}).get("id")})
    except Exception as exc:
        _write_audit("promotion_step2_promote_exception", {**_ctx, "error": str(exc), "traceback": traceback.format_exc()})
        return False, f"Step 2 (Promote) error: {exc}"

    # Build shared payload for steps 3 and 4
    classification = candidate.get("classification", "unknown")
    next_checks = candidate.get("next_checks", [])
    recommended_actions = candidate.get("recommended_actions", []) or next_checks[:3]
    probable_causes = candidate.get("probable_causes", [])
    root_cause = (
        f"{nice_label(classification)} is likely driven by "
        + ", ".join(c.replace("_", " ") for c in probable_causes[:3])
        if probable_causes else nice_label(classification)
    )

    # ── Step 3: Create resolution record ─────────────────────────────────────
    try:
        rr = SESSION.post(
            f"{BACKEND_URL}/create_resolution",
            json={
                "classification": classification,
                "root_cause": root_cause,
                "validated_checks": next_checks,
                "validated_actions": recommended_actions,
                "confirmed_by": engineer,
                "success_notes": notes or "Validated via Knowledge Center promotion.",
                "linked_candidate_id": candidate_id,
                "automation_ready": False,
                "linked_playbook_id": "",
                "tenant_id": tenant_id or "client_a",
            },
            timeout=REQUEST_TIMEOUT_MEDIUM,
        )
        if not rr.ok:
            detail = rr.json().get("detail", rr.text)[:300]
            _write_audit("promotion_step3_resolution_failed", {**_ctx, "http_status": rr.status_code, "detail": detail})
            return False, f"Step 3 (Resolution) failed: {detail}"
        resolution_id = rr.json().get("resolution", {}).get("resolution_id", "unknown")
        _write_audit("promotion_step3_resolution_ok", {**_ctx, "resolution_id": resolution_id})
    except Exception as exc:
        _write_audit("promotion_step3_resolution_exception", {**_ctx, "error": str(exc), "traceback": traceback.format_exc()})
        return False, f"Step 3 (Resolution) error: {exc}"

    # ── Step 4: Create playbook record ───────────────────────────────────────
    try:
        playbook_steps = list(dict.fromkeys(next_checks + recommended_actions))
        pr = SESSION.post(
            f"{BACKEND_URL}/create_playbook",
            json={
                "name": f"{nice_label(classification)} Runbook",
                "classification": classification,
                "description": f"Engineer-validated runbook for {nice_label(classification)}.",
                "steps": playbook_steps,
                "scripts": [],
                "approval_mode": "manual_approval_required",
                "rollback_available": True,
                "risk_tier": "medium",
                "tenant_id": tenant_id or "client_a",
            },
            timeout=REQUEST_TIMEOUT_MEDIUM,
        )
        if not pr.ok:
            detail = pr.json().get("detail", pr.text)[:300]
            _write_audit("promotion_step4_playbook_failed", {**_ctx, "http_status": pr.status_code, "detail": detail})
            return False, f"Step 4 (Playbook) failed: {detail}"
        playbook_id = pr.json().get("playbook", {}).get("playbook_id", "unknown")
        _write_audit("promotion_step4_playbook_ok", {**_ctx, "playbook_id": playbook_id})
    except Exception as exc:
        _write_audit("promotion_step4_playbook_exception", {**_ctx, "error": str(exc), "traceback": traceback.format_exc()})
        return False, f"Step 4 (Playbook) error: {exc}"

    _write_audit("promotion_completed", {**_ctx, "classification": classification,
                                         "resolution_id": resolution_id, "playbook_id": playbook_id})
    return True, f"OK|{classification}|{resolution_id}|{playbook_id}"


def _promote_approved_candidate(candidate_id: str, tenant_id: str, promoted_by: str = "engineer") -> tuple[bool, str]:
    """Promote an already-approved candidate to local knowledge."""
    try:
        pv = SESSION.post(
            f"{BACKEND_URL}/promote_candidate",
            json={"candidate_id": candidate_id, "tenant_id": tenant_id or None,
                  "promote_to_global": False, "promoted_by": promoted_by},
            timeout=REQUEST_TIMEOUT_MEDIUM,
        )
        if not pv.ok:
            return False, pv.json().get("detail", pv.text)[:200]
    except Exception as exc:
        return False, str(exc)
    return True, "Promoted to local knowledge."



def render_knowledge_center_page():
    st.subheader("Knowledge Center")
    st.caption("Review promoted operational knowledge, fallback candidates, reusable resolutions, and playbooks.")

    # UX-09: Source status cards
    sources = fetch_knowledge_sources()
    if sources:
        st.markdown("#### Knowledge Sources")
        src_cols = st.columns(len(sources))
        _src_status_colors = {"ok": "#16a34a", "missing": "#d97706", "error": "#dc2626"}
        for i, src in enumerate(sources):
            col = src_cols[i]
            with col:
                src_status = str(src.get("status") or "unknown")
                color = _src_status_colors.get(src_status, "#6b7280")
                st.markdown(
                    f"""
                    <div style="border:1px solid #e5e7eb;border-top:3px solid {color};border-radius:6px;padding:12px;text-align:center;">
                      <div style="font-size:0.75rem;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px;">{src.get("label","")}</div>
                      <div style="font-size:1.6rem;font-weight:700;color:#111827;">{src.get("count",0)}</div>
                      <div style="font-size:0.72rem;color:{color};margin-top:2px;">{"✓ Active" if src_status=="ok" else ("⚠ Missing" if src_status=="missing" else "✗ Error")}</div>
                      <div style="font-size:0.68rem;color:#9ca3af;margin-top:2px;">{src.get("size_kb",0)} KB</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        st.markdown("---")

    items = fetch_knowledge_candidates()
    if not items:
        render_empty_state("📚", "No knowledge yet", "Trigger fallback cases to populate this view.", "Run an incident analysis, then submit feedback to generate knowledge candidates.")
        return

    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "candidate_id": item.get("candidate_id", ""),
                "classification": str(item.get("classification", "") or "unknown"),
                "status": str(item.get("status", "candidate") or "candidate"),
                "ticket": str(item.get("ticket", "") or ""),
                "probable_causes": item.get("probable_causes", []) or [],
                "next_checks": item.get("next_checks", []) or [],
                "recommended_actions": item.get("recommended_actions", []) or [],
                "reviewed_by": item.get("reviewed_by"),
                "review_notes": item.get("review_notes"),
                "created_at": item.get("created_at") or item.get("updated_at") or "",
            }
        )

    classifications = sorted({nice_label(x["classification"]) for x in normalized})
    statuses = sorted({nice_label(x["status"]) for x in normalized})

    approved_count = sum(1 for x in normalized if x["status"].lower() in {"approved", "promoted", "promoted_global"})
    draft_count    = sum(1 for x in normalized if x["status"].lower() in {"candidate", "pending_review"})
    unique_classes = len({x["classification"] for x in normalized})

    _CARD_STYLE = (
        "padding:16px 18px;border-radius:14px;"
        "background:linear-gradient(135deg,rgba(255,255,255,0.04),rgba(255,255,255,0.01));"
        "border:1px solid rgba(120,120,120,0.15);min-height:90px;"
        "display:flex;flex-direction:column;justify-content:space-between;"
    )
    _LABEL_STYLE = "font-size:0.78rem;font-weight:500;letter-spacing:0.04em;text-transform:uppercase;opacity:0.72;"
    _NUM_STYLE   = "font-size:1.85rem;font-weight:800;line-height:1.1;margin-top:6px;"
    _SUB_STYLE   = "font-size:0.7rem;opacity:0.5;margin-top:4px;"

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(
            f'<div style="{_CARD_STYLE}border-left:4px solid #6366f1;">'
            f'<div style="{_LABEL_STYLE}">📂 Total Candidates</div>'
            f'<div style="{_NUM_STYLE}color:#6366f1;">{len(normalized)}</div>'
            f'<div style="{_SUB_STYLE}">all knowledge items</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f'''<a href="#kc-promoted" style="text-decoration:none;">
            <div style="{_CARD_STYLE}border-left:4px solid #10b981;cursor:pointer;">
              <div style="{_LABEL_STYLE}">✅ Approved / Promoted</div>
              <div style="{_NUM_STYLE}color:#10b981;">{approved_count}</div>
              <div style="{_SUB_STYLE}">↓ view promoted articles</div>
            </div></a>''',
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            f'<div style="{_CARD_STYLE}border-left:4px solid #64748b;">'
            f'<div style="{_LABEL_STYLE}">🏷️ Knowledge Classes</div>'
            f'<div style="{_NUM_STYLE}color:#64748b;">{unique_classes}</div>'
            f'<div style="{_SUB_STYLE}">unique classifications</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with c4:
        st.markdown(
            f'''<a href="#kc-draft" style="text-decoration:none;">
            <div style="{_CARD_STYLE}border-left:4px solid #f59e0b;cursor:pointer;">
              <div style="{_LABEL_STYLE}">📋 Draft / Review</div>
              <div style="{_NUM_STYLE}color:#f59e0b;">{draft_count}</div>
              <div style="{_SUB_STYLE}">↓ view pending candidates</div>
            </div></a>''',
            unsafe_allow_html=True,
        )

    class_counts: Dict[str, int] = {}
    for row in normalized:
        key = nice_label(row["classification"])
        class_counts[key] = class_counts.get(key, 0) + 1
    top_classes = sorted(class_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]

    if top_classes:
        max_count = top_classes[0][1] or 1
        mid = (len(top_classes) + 1) // 2
        left_items  = top_classes[:mid]
        right_items = top_classes[mid:]

        def _class_rows(items, offset=0):
            html = ""
            for i, (cls, count) in enumerate(items):
                rank = i + offset + 1
                bar_pct = int(count / max_count * 100)
                rank_color = ["#6366f1", "#8b5cf6", "#a78bfa", "#c4b5fd", "#ddd6fe"][min(rank - 1, 4)]
                html += (
                    f'<div style="display:grid;grid-template-columns:18px 1fr 36px;'
                    f'align-items:center;gap:10px;padding:5px 0;'
                    f'border-bottom:1px solid rgba(120,120,120,0.08);">'
                    f'<span style="font-size:0.7rem;font-weight:700;color:{rank_color};'
                    f'text-align:right;opacity:0.8;">{rank}</span>'
                    f'<div>'
                    f'<div style="font-size:0.8rem;font-weight:500;margin-bottom:3px;'
                    f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{cls}</div>'
                    f'<div style="height:4px;border-radius:2px;background:rgba(120,120,120,0.12);">'
                    f'<div style="height:4px;border-radius:2px;width:{bar_pct}%;'
                    f'background:{rank_color};opacity:0.7;"></div></div>'
                    f'</div>'
                    f'<span style="font-size:0.85rem;font-weight:700;text-align:right;'
                    f'color:{rank_color};">{count}</span>'
                    f'</div>'
                )
            return html

        col_l, col_r = st.columns(2, gap="large")
        label_html = (
            '<div style="font-size:0.72rem;font-weight:600;letter-spacing:0.06em;'
            'text-transform:uppercase;opacity:0.5;margin:10px 0 6px 0;">Top Knowledge Classes</div>'
        )
        with col_l:
            st.markdown(
                label_html +
                f'<div style="border:1px solid rgba(120,120,120,0.15);border-radius:10px;'
                f'padding:8px 14px;">{_class_rows(left_items, offset=0)}</div>',
                unsafe_allow_html=True,
            )
        with col_r:
            st.markdown(
                label_html +
                f'<div style="border:1px solid rgba(120,120,120,0.15);border-radius:10px;'
                f'padding:8px 14px;">{_class_rows(right_items, offset=mid)}</div>',
                unsafe_allow_html=True,
            )
    else:
        st.caption("No historical knowledge classes yet.")

    # ── Split by status ───────────────────────────────────────────────────
    _PROMOTED_STATUSES = {"approved", "promoted", "promoted_global", "active"}
    _DRAFT_STATUSES    = {"candidate", "pending_review"}

    promoted_items = [x for x in normalized if x["status"].lower() in _PROMOTED_STATUSES]
    draft_items    = [x for x in normalized if x["status"].lower() in _DRAFT_STATUSES]
    tenant_id      = st.session_state.get("selected_tenant_id", "")

    # ── Shared search bar ─────────────────────────────────────────────────
    st.markdown("---")
    kc_search = st.text_input("🔍 Search by classification or ticket text", key="kc_search_global", placeholder="e.g. jcl abend, authentication")

    def _apply_search(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        q = kc_search.strip().lower()
        if not q:
            return items
        return [x for x in items if q in x["classification"].lower() or q in x["ticket"].lower()]

    # ══════════════════════════════════════════════════════════════════════
    # TABLE 1 — Promoted Knowledge Articles
    # ══════════════════════════════════════════════════════════════════════
    st.markdown('<div id="kc-promoted"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="display:flex;align-items:center;gap:10px;margin:20px 0 6px 0;">'
        '<span style="font-size:1.05rem;font-weight:700;">✅ Promoted Knowledge Articles</span>'
        '<span style="font-size:0.75rem;background:#d1fae5;color:#065f46;padding:2px 8px;'
        'border-radius:10px;font-weight:600;">Validated & active in knowledge base</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    _promoted_filtered = _apply_search(
        sorted(promoted_items, key=lambda x: x.get("created_at", ""), reverse=True)
    )

    if _promoted_filtered:
        import pandas as pd

        _prows = []
        for x in _promoted_filtered:
            _status_label = {
                "promoted_global": "🌐 Global",
                "promoted": "✅ Local",
                "approved": "✅ Approved",
                "active": "✅ Active",
            }.get(x["status"].lower(), nice_label(x["status"]))
            _causes  = "; ".join(x["probable_causes"][:2]) if x["probable_causes"] else "—"
            _next_steps_src = x["recommended_actions"] or x["next_checks"]
            _next_steps = "; ".join(_next_steps_src[:2]) if _next_steps_src else "—"
            _prows.append({
                "Classification": nice_label(x["classification"]),
                "Status": _status_label,
                "Probable Causes": _causes[:120] + ("…" if len(_causes) > 120 else ""),
                "Next Steps": _next_steps[:120] + ("…" if len(_next_steps) > 120 else ""),
                "Reviewed By": x.get("reviewed_by") or "—",
                "Date": (x.get("created_at") or "")[:10],
            })

        _psel = st.dataframe(
            pd.DataFrame(_prows),
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="kc_promoted_table",
        )

        # Detail panel for selected promoted row
        _psel_rows = _psel.selection.rows if _psel.selection else []
        if _psel_rows:
            _pidx = _psel_rows[0]
            _pitem = _promoted_filtered[_pidx]
            _pcls  = _pitem["classification"]
            _pres  = fetch_resolution_by_classification(_pcls)
            _ppb   = fetch_playbook_by_classification(_pcls)
            _is_ungrounded = (
                _pitem.get("source", "") in {"llm_fallback", "fallback"}
                and not (_pitem.get("retrieved_incidents") or [])
                and not int(_pitem.get("grounding_incident_count") or 0)
            )
            with st.container():
                if _is_ungrounded:
                    st.warning(
                        "⚠️ **Ungrounded LLM content** — this article was generated by LLM with zero RAG hits "
                        "(no historical incidents matched). Content is generic and may not reflect your environment. "
                        "Consider deleting and re-ingesting real incident data from ServiceNow."
                    )
                st.markdown(
                    f'<div style="background:#f0fdf4;border-left:4px solid #10b981;'
                    f'border-radius:6px;padding:14px 18px;margin:8px 0;">'
                    f'<strong>{nice_label(_pcls)}</strong>'
                    f'<span style="font-size:0.75rem;color:#6b7280;margin-left:10px;">'
                    f'{_pitem.get("candidate_id","")}</span></div>',
                    unsafe_allow_html=True,
                )
                _dcols = st.columns(3)
                for _col, (_field, _label) in zip(_dcols, [
                    ("probable_causes", "Probable Causes"),
                    ("next_checks", "Recommended Checks"),
                    ("recommended_actions", "Recommended Actions"),
                ]):
                    with _col:
                        vals = _pitem.get(_field) or []
                        if vals:
                            st.markdown(f"**{_label}**")
                            for v in vals[:4]:
                                st.markdown(f"• {natural_label(v)}")

                if _pres:
                    st.markdown("**Resolution on file:**")
                    _rcols = st.columns(2)
                    with _rcols[0]:
                        if _pres.get("root_cause"):
                            st.caption(f"Root cause: {_pres['root_cause']}")
                        for v in (_pres.get("validated_checks") or [])[:3]:
                            st.markdown(f"• {v}")
                    with _rcols[1]:
                        for v in (_pres.get("validated_actions") or [])[:3]:
                            st.markdown(f"• {v}")

                if _ppb:
                    st.markdown(f"**Playbook:** {_ppb.get('name','—')} — {_ppb.get('description','')[:100]}")
    else:
        st.caption("No promoted knowledge articles yet." if not kc_search.strip() else "No promoted articles match your search.")

    # ══════════════════════════════════════════════════════════════════════
    # TABLE 2 — Candidates Pending Review
    # ══════════════════════════════════════════════════════════════════════
    st.markdown('<div id="kc-draft"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="display:flex;align-items:center;gap:10px;margin:28px 0 6px 0;">'
        '<span style="font-size:1.05rem;font-weight:700;">📋 Candidates Pending Review</span>'
        '<span style="font-size:0.75rem;background:#fef3c7;color:#92400e;padding:2px 8px;'
        'border-radius:10px;font-weight:600;">LLM-suggested — engineer validation required</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    _draft_filtered = _apply_search(
        sorted(draft_items, key=lambda x: x.get("created_at", ""), reverse=True)
    )

    if _draft_filtered:
        import pandas as pd

        _drows = []
        for x in _draft_filtered:
            _causes = "; ".join(x["probable_causes"][:2]) if x["probable_causes"] else "—"
            _checks = "; ".join(x["next_checks"][:2]) if x["next_checks"] else "—"
            _drows.append({
                "Classification": nice_label(x["classification"]),
                "Status": nice_label(x["status"]),
                "Ticket (excerpt)": (x["ticket"][:100] + "…" if len(x["ticket"]) > 100 else x["ticket"]) or "—",
                "Probable Causes": _causes[:100] + ("…" if len(_causes) > 100 else ""),
                "Checks": _checks[:100] + ("…" if len(_checks) > 100 else ""),
                "Created": (x.get("created_at") or "")[:10],
            })

        _dsel = st.dataframe(
            pd.DataFrame(_drows),
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="kc_draft_table",
        )

        # Detail + promote workflow for selected draft row
        # Persist selected candidate ID in session state so checkbox reruns don't collapse the panel
        _dsel_rows = _dsel.selection.rows if _dsel.selection else []
        if _dsel_rows:
            st.session_state["kc_draft_selected_id"] = _draft_filtered[_dsel_rows[0]]["candidate_id"]

        _selected_cid = st.session_state.get("kc_draft_selected_id")
        _ditem = next((x for x in _draft_filtered if x["candidate_id"] == _selected_cid), None)

        if _ditem:
            _dcls  = _ditem["classification"]
            _dcid  = _ditem["candidate_id"]
            _dres  = fetch_resolution_by_classification(_dcls)

            with st.container():
                st.markdown(
                    f'<div style="background:#fffbeb;border-left:4px solid #f59e0b;'
                    f'border-radius:6px;padding:14px 18px;margin:8px 0;">'
                    f'<strong>{nice_label(_dcls)}</strong>'
                    f'<span style="font-size:0.75rem;color:#6b7280;margin-left:10px;">{_dcid}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                if _ditem.get("ticket"):
                    st.markdown(f"**Ticket:** {_ditem['ticket']}")

                _d2cols = st.columns(3)
                for _col, (_field, _label) in zip(_d2cols, [
                    ("probable_causes", "Probable Causes"),
                    ("next_checks", "Recommended Checks"),
                    ("recommended_actions", "Recommended Actions"),
                ]):
                    with _col:
                        vals = _ditem.get(_field) or []
                        if vals:
                            st.markdown(f"**{_label}**")
                            for v in vals[:4]:
                                st.markdown(f"• {natural_label(v)}")

                if _dres:
                    st.info(f"**Resolution on file for this classification** — root cause: {_dres.get('root_cause','—')[:120]}")

                st.markdown("---")
                st.warning("**Engineer validation required** — test these steps in a real environment before promoting.")
                _eby  = st.text_input("Your name / engineer ID", key=f"kc_by_{_dcid}", placeholder="e.g. john.doe")
                _enotes = st.text_area("Confirmation notes (optional)", key=f"kc_notes_{_dcid}", height=70)
                _econfirm = st.checkbox("I have tested these steps and they worked", key=f"kc_confirm_{_dcid}")
                if st.button("Promote to Local Knowledge", key=f"kc_promote_{_dcid}", type="primary", disabled=not (_econfirm and _eby.strip())):
                    _ok, _msg = _review_and_promote_candidate(_dcid, _eby.strip(), _enotes, tenant_id)
                    if _ok:
                        _parts = _msg.split("|")
                        _cls_done   = nice_label(_parts[1]) if len(_parts) > 1 else "—"
                        _res_id     = _parts[2] if len(_parts) > 2 else "—"
                        _pb_id      = _parts[3] if len(_parts) > 3 else "—"
                        st.session_state.pop("kc_draft_selected_id", None)
                        st.success("✅ Promotion complete — all 4 steps succeeded.")
                        st.markdown(
                            f"| Step | Result |\n|---|---|\n"
                            f"| 1 · Candidate reviewed & approved | ✅ `{_dcid}` |\n"
                            f"| 2 · Promoted to the knowledge base | ✅ Classification: **{_cls_done}** |\n"
                            f"| 3 · Resolution created | ✅ `{_res_id}` |\n"
                            f"| 4 · Playbook created | ✅ `{_pb_id}` |"
                        )
                        st.rerun()
                    else:
                        st.error(f"Promotion failed: {_msg}")
                        st.caption("Check `data/logs/promotion_audit.jsonl` for the full error trace.")
    else:
        st.caption("No pending candidates." if not kc_search.strip() else "No candidates match your search.")

