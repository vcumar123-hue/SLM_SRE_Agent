"""
agents/triage_agent.py — Triage Agent (Phase 1)

Wraps: classify_by_keywords, apply_intent_router,
       normalize_symptom_vs_cause_classification, extract_ticket_intents.

Input:  TriageInput
Output: TriageOutput + TraceSpan

Uses lazy imports from app.py to avoid circular imports.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.a2a_protocol import AgentMessage, TraceSpan
from agents.models import TriageInput, TriageOutput

logger = logging.getLogger(__name__)


def _do_triage(inp: TriageInput) -> TriageOutput:
    # Lazy imports — app.py imports agents routers, so top-level import would be circular.
    from app import (
        classify_by_keywords,
        apply_intent_router,
        normalize_symptom_vs_cause_classification,
        extract_ticket_intents,
        infer_ticket_domain,
        normalize_classification_token,
        choose_better_domain,
        infer_domain_from_taxonomy,
        load_taxonomy,
    )

    ticket = inp.ticket
    taxonomy = load_taxonomy()

    domain, classification, llm_score = classify_by_keywords(ticket, taxonomy)
    classification_certainty = llm_score
    raw_llm_score = llm_score

    inferred = infer_ticket_domain(ticket)
    if inferred in {"compute", "storage", "database", "network", "iam", "integration"}:
        domain = inferred

    # Vector match classification is resolved by the retrieval agent and
    # passed back; triage works with keyword baseline only.
    retrieval_locked_classification = ""
    retrieval_locked_score = 0.0

    domain, classification, classification_certainty, intents = apply_intent_router(
        ticket=ticket,
        domain=domain,
        classification=classification,
        classification_certainty=classification_certainty,
    )
    domain, classification, classification_certainty, _ = normalize_symptom_vs_cause_classification(
        ticket=ticket,
        domain=domain,
        classification=classification,
        classification_certainty=classification_certainty,
    )

    return TriageOutput(
        domain=domain,
        classification=classification,
        classification_certainty=classification_certainty,
        llm_score=raw_llm_score,
        retrieval_locked_classification=retrieval_locked_classification,
        retrieval_locked_score=retrieval_locked_score,
        intents=dict(intents) if intents else {},
        feedback_override=None,
    )


def triage_handler(message: AgentMessage) -> Dict[str, Any]:
    span = TraceSpan(
        agent="triage",
        intent=message.intent,
        input_summary=f"ticket[:{min(80, len(message.payload.get('ticket', '')))}]",
    )
    try:
        inp = TriageInput(**message.payload)
        out = _do_triage(inp)
        span.finish(
            output_summary=f"cls={out.classification} certainty={out.classification_certainty:.2f}",
            decision=out.classification,
        )
        return {"output": out.model_dump(), "trace": span.to_dict()}
    except Exception as exc:
        logger.exception("triage_agent error: %s", exc)
        span.finish(output_summary=str(exc), status="error")
        return {"output": None, "trace": span.to_dict(), "error": str(exc)}
