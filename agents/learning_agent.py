"""
agents/learning_agent.py — Learning Agent (Phase 1)

Wraps: record_outcome, get_pattern_intelligence.
Called after resolution to update memory and signal pattern state.

Input:  LearningInput
Output: LearningOutput + TraceSpan
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from agents.a2a_protocol import AgentMessage, TraceSpan
from agents.models import LearningInput, LearningOutput

logger = logging.getLogger(__name__)


def _do_learning(inp: LearningInput) -> LearningOutput:
    from resolution_outcome import record_outcome
    from knowledge_manager import get_pattern_intelligence

    outcome_id = ""
    if inp.incident_id or inp.resolution_id:
        try:
            outcome_id = record_outcome(
                incident_id=inp.incident_id,
                classification=inp.classification,
                resolution_id=inp.resolution_id,
                playbook_id=inp.playbook_id,
                tenant_id=inp.tenant_id,
                execution_mode=inp.execution_mode,
                outcome_status=inp.outcome_status,
                resolution_minutes=inp.resolution_minutes,
            )
        except Exception as exc:
            logger.warning("learning_agent: record_outcome failed: %s", exc)

    pattern_signal: Dict[str, Any] = {}
    try:
        intel = get_pattern_intelligence(inp.classification, tenant_id=inp.tenant_id)
        if intel:
            pattern_signal = intel
    except Exception as exc:
        logger.warning("learning_agent: get_pattern_intelligence failed: %s", exc)

    return LearningOutput(
        outcome_id=outcome_id,
        pattern_signal=pattern_signal,
        memory_updated=bool(outcome_id),
    )


def learning_handler(message: AgentMessage) -> Dict[str, Any]:
    span = TraceSpan(
        agent="learning",
        intent=message.intent,
        input_summary=f"cls={message.payload.get('classification')} tenant={message.payload.get('tenant_id')}",
    )
    try:
        inp = LearningInput(**message.payload)
        out = _do_learning(inp)
        span.finish(
            output_summary=f"outcome_id={out.outcome_id or 'none'} memory_updated={out.memory_updated} pattern_confidence={out.pattern_signal.get('pattern_confidence', 'n/a')}",
            decision="memory_updated" if out.memory_updated else "pattern_read",
        )
        return {"output": out.model_dump(), "trace": span.to_dict()}
    except Exception as exc:
        logger.exception("learning_agent error: %s", exc)
        span.finish(output_summary=str(exc), status="error")
        return {"output": None, "trace": span.to_dict(), "error": str(exc)}
