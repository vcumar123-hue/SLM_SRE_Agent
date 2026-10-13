"""
agents/resolution_agent.py — Resolution Agent (Phase 1)

Wraps: resolve_linked_knowledge, maybe_create_fallback_drafts.
Looks up linked resolutions and playbooks for the final classification.

Input:  ResolutionInput
Output: ResolutionOutput + TraceSpan
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.a2a_protocol import AgentMessage, TraceSpan
from agents.models import ResolutionInput, ResolutionOutput

logger = logging.getLogger(__name__)


def _do_resolution(inp: ResolutionInput) -> ResolutionOutput:
    from app import resolve_linked_knowledge, maybe_create_fallback_drafts

    # Legacy links knowledge by the INITIAL classification; the shared post-RCA finalization re-links if the final one differs.

    linked_resolution, linked_playbook = resolve_linked_knowledge(inp.classification)

    if inp.fallback_used and inp.candidate_record:
        linked_resolution, linked_playbook = maybe_create_fallback_drafts(
            candidate_record=inp.candidate_record,
            classification=inp.final_classification,
            final_analysis=inp.analysis,
            linked_resolution=linked_resolution,
            linked_playbook=linked_playbook,
            request_id="",
        )

    # The decision label is owned by the RCA/finalization step; legacy never relabels it on link.
    decision = inp.decision

    return ResolutionOutput(
        linked_resolution=linked_resolution,
        linked_playbook=linked_playbook,
        candidate_record=inp.candidate_record,
        decision=decision,
    )


def resolution_handler(message: AgentMessage) -> Dict[str, Any]:
    span = TraceSpan(
        agent="resolution",
        intent=message.intent,
        input_summary=f"final_cls={message.payload.get('final_classification')} fallback={message.payload.get('fallback_used')}",
    )
    try:
        inp = ResolutionInput(**message.payload)
        out = _do_resolution(inp)
        span.finish(
            output_summary=f"resolution={'yes' if out.linked_resolution else 'no'} playbook={'yes' if out.linked_playbook else 'no'} decision={out.decision}",
            decision=out.decision,
        )
        return {"output": out.model_dump(), "trace": span.to_dict()}
    except Exception as exc:
        logger.exception("resolution_agent error: %s", exc)
        span.finish(output_summary=str(exc), status="error")
        return {"output": None, "trace": span.to_dict(), "error": str(exc)}
