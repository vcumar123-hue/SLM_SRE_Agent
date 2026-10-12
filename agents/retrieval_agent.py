"""
agents/retrieval_agent.py — Retrieval Agent (Phase 1)

Wraps: run_hybrid_retrieval, fetch_rag_context, summarize_retrieval_quality.
Also applies the vector match classification override and saves
retrieval_locked_classification before any intent router can change it.

Input:  RetrievalInput
Output: RetrievalOutput + TraceSpan
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.a2a_protocol import AgentMessage, TraceSpan
from agents.models import RetrievalInput, RetrievalOutput

logger = logging.getLogger(__name__)


def _do_retrieval(inp: RetrievalInput) -> RetrievalOutput:
    from app import (
        run_hybrid_retrieval,
        fetch_rag_context,
        summarize_retrieval_quality,
        load_taxonomy,
    )

    ticket = inp.ticket
    taxonomy = load_taxonomy()

    incidents, embedding_score, learned_feedback_score, feedback_override, retrieval_scope = run_hybrid_retrieval(
        ticket=ticket,
        top_k=inp.top_k,
        request_id=inp.request_id,
        taxonomy=taxonomy,
        tenant_id=inp.tenant_id,
        knowledge_scope=inp.knowledge_scope,
    )
    retrieval_quality = summarize_retrieval_quality(incidents)
    rag_context = fetch_rag_context(tenant_id=inp.tenant_id, ticket=ticket)

    import json as _json

    def _to_str(v: Any) -> str:
        if isinstance(v, str):
            return v
        if v is None:
            return ""
        return _json.dumps(v)

    return RetrievalOutput(
        incidents=incidents,
        embedding_score=embedding_score,
        learned_feedback_score=learned_feedback_score,
        feedback_override=feedback_override,
        retrieval_quality=retrieval_quality,
        retrieval_scope=_to_str(retrieval_scope),
        rag_context=_to_str(rag_context),
    )


def retrieval_handler(message: AgentMessage) -> Dict[str, Any]:
    span = TraceSpan(
        agent="retrieval",
        intent=message.intent,
        input_summary=f"tenant={message.payload.get('tenant_id')} scope={message.payload.get('knowledge_scope')}",
    )
    try:
        inp = RetrievalInput(**message.payload)
        out = _do_retrieval(inp)
        span.finish(
            output_summary=f"incidents={len(out.incidents)} quality={out.retrieval_quality:.2f} feedback_override={out.feedback_override is not None}",
            decision=f"quality={out.retrieval_quality:.2f}",
        )
        return {"output": out.model_dump(), "trace": span.to_dict()}
    except Exception as exc:
        logger.exception("retrieval_agent error: %s", exc)
        span.finish(output_summary=str(exc), status="error")
        return {"output": None, "trace": span.to_dict(), "error": str(exc)}
