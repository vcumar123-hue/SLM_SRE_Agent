"""
agents/safety_agent.py — Safety Agent (Phase 1)

Wraps: should_force_fallback_from_intent, should_request_user_confirmation.
Evaluates whether the request should be held, escalated, or can proceed.

Input:  SafetyInput
Output: SafetyOutput + TraceSpan
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.a2a_protocol import AgentMessage, TraceSpan
from agents.models import SafetyInput, SafetyOutput

logger = logging.getLogger(__name__)


def _do_safety(inp: SafetyInput) -> SafetyOutput:
    from app import (
        should_force_fallback_from_intent,
        should_request_user_confirmation,
    )

    force_fallback, reason = should_force_fallback_from_intent(
        ticket=inp.ticket,
        classification=inp.classification,
        primary_confidence=inp.primary_confidence,
        retrieved_incidents=inp.incidents,
    )

    if inp.confirm_local_matches is False and not force_fallback:
        force_fallback = True
        reason = "user_rejected_local_matches"

    confirmation_required = should_request_user_confirmation(
        primary_confidence=inp.primary_confidence,
        retrieved_incidents=inp.incidents,
        linked_resolution=inp.linked_resolution,
        linked_playbook=inp.linked_playbook,
        classification=inp.classification,
    )

    gate = "proceed"
    if force_fallback:
        gate = "hold"

    return SafetyOutput(
        force_fallback=force_fallback,
        force_fallback_reason=reason,
        confirmation_required=confirmation_required,
        gate_decision=gate,
    )


def safety_handler(message: AgentMessage) -> Dict[str, Any]:
    span = TraceSpan(
        agent="safety",
        intent=message.intent,
        input_summary=f"cls={message.payload.get('classification')} conf={message.payload.get('primary_confidence')}",
    )
    try:
        inp = SafetyInput(**message.payload)
        out = _do_safety(inp)
        span.finish(
            output_summary=f"gate={out.gate_decision} force_fallback={out.force_fallback} confirm={out.confirmation_required}",
            decision=out.gate_decision,
        )
        return {"output": out.model_dump(), "trace": span.to_dict()}
    except Exception as exc:
        logger.exception("safety_agent error: %s", exc)
        span.finish(output_summary=str(exc), status="error")
        return {"output": None, "trace": span.to_dict(), "error": str(exc)}
