"""
agents/rca_agent.py — RCA Agent (Phase 1)

Wraps: build_enriched_analysis, maybe_run_fallback,
       compute_confidence_score / compute_confidence_locally.
Applies the retrieval-locked classification protection:
  when retrieval_locked_score >= 0.75 the SLM reclassification is rejected.

Input:  RCAInput
Output: RCAOutput + TraceSpan
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.a2a_protocol import AgentMessage, TraceSpan
from agents.models import RCAInput, RCAOutput

logger = logging.getLogger(__name__)


def _do_rca(inp: RCAInput) -> RCAOutput:
    from app import (
        build_enriched_analysis,
        maybe_run_fallback,
        build_analysis_source_label,
        normalize_classification_token,
        infer_domain_from_taxonomy,
        choose_better_domain,
        load_taxonomy,
        dedupe_semanticish,
        is_diagnostic_phrase,
        create_knowledge_candidate,
        DEFAULT_TENANT_ID,
    )
    taxonomy = load_taxonomy()
    ticket = inp.ticket

    response_source = "local_primary"
    analysis_source_label = build_analysis_source_label(response_source=response_source, fallback_used=False)

    import json as _json
    _rag_ctx = inp.rag_context
    if isinstance(_rag_ctx, str):
        try:
            _rag_ctx = _json.loads(_rag_ctx)
        except Exception:
            _rag_ctx = {}

    analysis = build_enriched_analysis(
        ticket=ticket,
        domain=inp.domain,
        classification=inp.classification,
        retrieved_incidents=inp.incidents,
        primary_confidence=inp.primary_confidence,
        linked_resolution=inp.linked_resolution,
        linked_playbook=inp.linked_playbook,
        analysis_source=response_source,
        analysis_source_label=analysis_source_label,
        rag_context=_rag_ctx,
    )

    # Confidence is computed once by the orchestrator; RCA only consumes it.
    confidence_info = dict(inp.confidence_context) if inp.confidence_context else {
        "primary_confidence": inp.primary_confidence,
        "llm_score": inp.classification_certainty,
    }
    primary_confidence = float(confidence_info.get("primary_confidence", 0.0) or 0.0)

    fallback_used = False
    fallback_analysis = None
    fallback_confidence = 0.0
    fallback_source = fallback_model = fallback_provider = None
    final_analysis = analysis
    final_classification = inp.classification
    decision = "local_primary"
    candidate_record: Dict[str, Any] = {}

    if inp.use_fallback:
        (
            fallback_used,
            fallback_analysis,
            fallback_confidence,
            fallback_source,
            fallback_model,
            fallback_provider,
            _,
        ) = maybe_run_fallback(
            ticket=ticket,
            primary_confidence=primary_confidence,
            request_id=inp.request_id,
            force_fallback=inp.force_fallback,
            reason_override=inp.force_fallback_reason,
            retrieved_incidents=inp.incidents,
            tenant_id=inp.tenant_id,
        )

    if fallback_used and fallback_analysis:
        confidence_info["fallback_confidence"] = fallback_confidence
        response_source = "fallback"
        analysis_source_label = build_analysis_source_label(
            response_source=response_source,
            fallback_used=True,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
        )
        # High-confidence retrieval match takes priority over SLM reclassification.
        _fallback_cls = fallback_analysis.get("classification", inp.classification)
        _resolved_cls = (
            inp.retrieval_locked_classification
            if inp.retrieval_locked_score >= 0.75 and inp.retrieval_locked_classification
            else _fallback_cls
        )
        final_analysis = {
            "classification": _resolved_cls,
            "probable_causes": dedupe_semanticish(fallback_analysis.get("probable_causes", analysis.get("probable_causes", []))),
            "next_checks": dedupe_semanticish(fallback_analysis.get("next_checks", analysis.get("next_checks", [])))[:5],
            "recommended_actions": [
                x for x in dedupe_semanticish(fallback_analysis.get("recommended_actions", analysis.get("recommended_actions", [])))
                if not is_diagnostic_phrase(x)
            ][:5],
            "confidence": fallback_analysis.get("confidence", fallback_confidence),
            "escalate": fallback_analysis.get("escalate", True),
            "fallback_source": fallback_source,
            "fallback_model": fallback_model,
            "fallback_provider": fallback_provider,
            "analysis_source": response_source,
            "analysis_source_label": analysis_source_label,
        }
        final_classification = normalize_classification_token(str(_resolved_cls))
        decision = "fallback_required"
        if not (inp.linked_resolution and inp.linked_playbook):
            try:
                candidate_record = create_knowledge_candidate(
                    ticket=ticket,
                    domain=inp.domain,
                    classification=final_classification,
                    probable_causes=final_analysis.get("probable_causes", []),
                    next_checks=final_analysis.get("next_checks", []),
                    recommended_actions=final_analysis.get("recommended_actions", []),
                    confidence=float(final_analysis.get("confidence", fallback_confidence) or 0.0),
                    fallback_provider=fallback_provider or "unknown",
                    fallback_model=fallback_model or "",
                    retrieved_incidents=[item.get("id") for item in inp.incidents if item.get("id")],
                    tenant_id=inp.tenant_id or DEFAULT_TENANT_ID,
                    knowledge_scope=inp.knowledge_scope or "client_private",
                )
            except Exception as exc:
                logger.warning("RCA candidate creation failed: %s", exc)

    return RCAOutput(
        final_classification=final_classification,
        domain=inp.domain,
        analysis=final_analysis,
        fallback_used=fallback_used,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model,
        fallback_source=fallback_source,
        confidence_info=confidence_info,
        response_source=response_source,
        analysis_source_label=analysis_source_label,
        decision=decision,
        candidate_record=candidate_record or None,
        routed_intents={},
    )


def rca_handler(message: AgentMessage) -> Dict[str, Any]:
    span = TraceSpan(
        agent="rca",
        intent=message.intent,
        input_summary=f"cls={message.payload.get('classification')} fallback={message.payload.get('force_fallback')}",
    )
    try:
        inp = RCAInput(**message.payload)
        out = _do_rca(inp)
        span.finish(
            output_summary=f"final_cls={out.final_classification} fallback_used={out.fallback_used} decision={out.decision}",
            decision=out.decision,
        )
        return {"output": out.model_dump(), "trace": span.to_dict()}
    except Exception as exc:
        logger.exception("rca_agent error: %s", exc)
        span.finish(output_summary=str(exc), status="error")
        return {"output": None, "trace": span.to_dict(), "error": str(exc)}
