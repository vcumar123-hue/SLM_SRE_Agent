"""Knowledge Center page module."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import streamlit as st
from page_modules.shared import (
    BACKEND_URL, SESSION, REQUEST_TIMEOUT_MEDIUM,
    fetch_knowledge_sources, fetch_knowledge_candidates,
    fetch_resolution_by_classification, fetch_playbook_by_classification,
    nice_label, format_confidence, metric_card, section_title,
    render_empty_state, natural_label,
)


def _review_and_promote_candidate(candidate_id: str, reviewed_by: str, notes: str, tenant_id: str) -> tuple[bool, str]:
    """Two-step: review (approve) then promote. Returns (success, message)."""
    try:
        rv = SESSION.post(
            f"{BACKEND_URL}/review_candidate",
            json={"candidate_id": candidate_id, "reviewed_by": reviewed_by or "engineer", "approved": True, "review_notes": notes},
            timeout=REQUEST_TIMEOUT_MEDIUM,
        )
        if not rv.ok:
            return False, rv.json().get("detail", rv.text)[:200]
    except Exception as exc:
        return False, str(exc)
    try:
        pv = SESSION.post(
            f"{BACKEND_URL}/promote_candidate",
            json={"candidate_id": candidate_id, "tenant_id": tenant_id or None},
            timeout=REQUEST_TIMEOUT_MEDIUM,
        )
        if not pv.ok:
            return False, pv.json().get("detail", pv.text)[:200]
    except Exception as exc:
        return False, str(exc)
    return True, "Promoted to local knowledge."


def _promote_approved_candidate(candidate_id: str, tenant_id: str) -> tuple[bool, str]:
    """Promote an already-approved candidate."""
    try:
        pv = SESSION.post(
            f"{BACKEND_URL}/promote_candidate",
            json={"candidate_id": candidate_id, "tenant_id": tenant_id or None},
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

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Candidates", str(len(normalized)), variant="knowledge")
    with c2:
        approved_count = sum(1 for x in normalized if x["status"].lower() in {"approved", "promoted"})
        metric_card("Approved / Promoted", str(approved_count), variant="knowledge")
    with c3:
        unique_classes = len({x["classification"] for x in normalized})
        metric_card("Knowledge Classes", str(unique_classes), variant="knowledge")
    with c4:
        draft_count = sum(1 for x in normalized if x["status"].lower() in {"candidate", "pending_review"})
        metric_card("Draft / Review", str(draft_count), variant="knowledge")

    st.markdown("### Top Historical Knowledge Classes")
    st.caption("System-wide historical reuse trend. These counts change over time based on platform usage, candidate promotion, and learned runs.")

    class_counts = {}
    for row in normalized:
        key = nice_label(row["classification"])
        class_counts[key] = class_counts.get(key, 0) + 1
    top_classes = sorted(class_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]

    if top_classes:
        dash_cols = st.columns(4)
        for idx, (cls, count) in enumerate(top_classes):
            col = dash_cols[idx % 4]
            with col:
                st.markdown(
                    f'''
                    <div style="
                        padding: 18px;
                        border: 1px solid rgba(120,120,120,0.18);
                        border-radius: 20px;
                        background: linear-gradient(180deg,#ffffff 0%,#f8faff 100%);
                        height: 220px;
                        margin-bottom: 14px;
                        display: flex;
                        flex-direction: column;
                        justify-content: space-between;
                    ">
                        <div>
                            <div style="
                                font-size: 0.72rem;
                                color: #6b7280;
                                text-transform: uppercase;
                                letter-spacing: 0.05em;
                            ">
                                Historical Class
                            </div>
                            <div style="
                                margin-top: 14px;
                                font-size: 0.98rem;
                                font-weight: 600;
                                color: #1f2937;
                                line-height: 1.35;
                                min-height: 54px;
                                max-height: 54px;
                                overflow: hidden;
                            ">
                                {cls}
                            </div>
                        </div>
                        <div style="
                            text-align: center;
                            font-size: 3rem;
                            font-weight: 700;
                            color: #111827;
                            line-height: 1;
                            margin-top: 8px;
                        ">
                            {count}
                        </div>
                    </div>
                    ''',
                    unsafe_allow_html=True,
                )
    else:
        st.caption("No historical knowledge classes yet.")

    st.markdown("---")
    left, right = st.columns([0.34, 0.66], gap="large")

    filtered = normalized

    with left:
        st.markdown("#### Filters")
        search_text = st.text_input("Search by ticket or classification", key="kc_search_text")
        selected_status = st.multiselect("Status", options=statuses, default=[], key="kc_status_filter")
        selected_classes = st.multiselect("Classification", options=classifications, default=[], key="kc_class_filter")
        sort_mode = st.selectbox(
            "Sort By",
            options=["Newest First", "Classification A-Z", "Status"],
            index=0,
            key="kc_sort_mode",
        )

        if search_text.strip():
            q = search_text.strip().lower()
            filtered = [
                x for x in filtered
                if q in x["classification"].lower() or q in x["ticket"].lower()
            ]
        if selected_status:
            selected_status_l = {x.lower() for x in selected_status}
            filtered = [x for x in filtered if nice_label(x["status"]).lower() in selected_status_l]
        if selected_classes:
            selected_classes_l = {x.lower() for x in selected_classes}
            filtered = [x for x in filtered if nice_label(x["classification"]).lower() in selected_classes_l]

        if sort_mode == "Classification A-Z":
            filtered = sorted(filtered, key=lambda x: nice_label(x["classification"]).lower())
        elif sort_mode == "Status":
            filtered = sorted(filtered, key=lambda x: nice_label(x["status"]).lower())
        else:
            filtered = sorted(filtered, key=lambda x: x.get("created_at", ""), reverse=True)

    with right:
        st.markdown("#### Candidate Knowledge")
        if not filtered:
            st.info("No candidates match the current filters.")
            return

        for item in filtered:
            classification = item["classification"]
            resolution = fetch_resolution_by_classification(classification)
            playbook = fetch_playbook_by_classification(classification)
            status_badge = nice_label(item["status"])
            title = f"{nice_label(classification)} · {status_badge}"

            with st.expander(title, expanded=False):
                if item["candidate_id"]:
                    st.caption(f"Candidate ID: {item['candidate_id']}")
                if item["ticket"]:
                    st.write(f"**Ticket:** {item['ticket']}")
                if item["created_at"]:
                    st.caption(f"Updated: {item['created_at']}")

                for field, title_text in [
                    ("probable_causes", "Probable Causes"),
                    ("next_checks", "Recommended Checks"),
                    ("recommended_actions", "Recommended Actions"),
                ]:
                    vals = item.get(field, []) or []
                    if vals:
                        st.write(f"**{title_text}**")
                        for v in vals:
                            st.write(f"• {natural_label(v)}")

                if item.get("review_notes"):
                    st.write(f"**Review Notes:** {item['review_notes']}")

                # Engineer validation workflow
                cid = item["candidate_id"]
                status_lower = item["status"].lower()
                tenant_id = st.session_state.get("selected_tenant_id", "")

                if status_lower in {"candidate", "pending_review"}:
                    st.markdown("---")
                    st.warning("**Awaiting engineer validation** — LLM-suggested steps. Test these in a real environment before promoting to local knowledge.")
                    confirm_key = f"kc_confirm_{cid}"
                    notes_key = f"kc_notes_{cid}"
                    by_key = f"kc_by_{cid}"
                    btn_key = f"kc_promote_btn_{cid}"
                    confirmed = st.checkbox("I have tested these steps and they worked", key=confirm_key)
                    if confirmed:
                        reviewed_by = st.text_input("Your name / engineer ID", key=by_key, placeholder="e.g. john.doe")
                        review_notes = st.text_area("Confirmation notes (optional)", key=notes_key, height=80, placeholder="What did you verify? Any deviations?")
                        if st.button("Promote to Local Knowledge", key=btn_key, type="primary", disabled=not reviewed_by.strip()):
                            ok, msg = _review_and_promote_candidate(cid, reviewed_by.strip(), review_notes, tenant_id)
                            if ok:
                                st.success(f"Promoted — this classification will now be served from local knowledge.")
                                st.rerun()
                            else:
                                st.error(f"Promotion failed: {msg}")
                elif status_lower == "approved":
                    st.markdown("---")
                    st.info("**Approved** — ready to promote to local knowledge.")
                    if st.button("Promote to Local Knowledge", key=f"kc_promote_approved_{cid}", type="primary"):
                        ok, msg = _promote_approved_candidate(cid, tenant_id)
                        if ok:
                            st.success("Promoted to local knowledge.")
                            st.rerun()
                        else:
                            st.error(f"Promotion failed: {msg}")
                elif status_lower in {"promoted", "active"}:
                    st.success("Already promoted to local knowledge.")

                if resolution:
                    st.markdown("---")
                    st.write("**Resolution Reuse**")
                    root_cause = resolution.get("root_cause") or ""
                    if root_cause:
                        st.write(f"Root Cause: {root_cause}")
                    checks = resolution.get("validated_checks") or []
                    actions = resolution.get("validated_actions") or []
                    if checks:
                        st.write("Validated Checks")
                        for v in checks[:5]:
                            st.write(f"• {v}")
                    if actions:
                        st.write("Validated Actions")
                        for v in actions[:5]:
                            st.write(f"• {v}")

                if playbook:
                    st.markdown("---")
                    st.write("**Playbook Reuse**")
                    if playbook.get("name"):
                        st.write(f"Name: {playbook.get('name')}")
                    if playbook.get("description"):
                        st.write(f"Description: {playbook.get('description')}")
                    steps = playbook.get("steps") or []
                    if steps:
                        st.write("Top Steps")
                        for step in steps[:5]:
                            st.write(f"• {step}")

