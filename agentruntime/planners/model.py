"""
model: the local model chooses every step (design 9.2, "exists for experiments only").

Each turn the model sees the ticket and the evidence so far (untrusted-data blocks), the steps taken and the tools it may use, and returns one strict JSON
action: a tool call, `finish` with causes, or `escalate`. There is no rule table and **no fallback**: a model failure is the model's result, so the
comparison with rules_then_model shows what the model adds. Failures surface through the runner as it already handles them (an unusable reply raises
PlannerError; a timeout, an over-limit reply or an exhausted budget end the run as the gateway reports).

Causes are accepted only under the same grounding rule as rules_then_model: cited ids must exist in the run and the quote must appear word for word in a
cited record. A finish with no grounded cause becomes an escalation. Prompt: `prompts.PLANNER_*`. The prompt shows less evidence than the synthesis prompt
because the run's 4,000-token budget counts prompt tokens and this planner makes one request per step.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from agentruntime.contracts import Action
from agentruntime.planners import model_manifest, prompts
from agentruntime.planners.model_support import (
    LocalModelClient, ModelReplyError, TOOL_ACTIONS, consult, finish_action, ground_hypotheses, tool_action,
)
from agentruntime.runner import PlannerError, PlannerReply

EVIDENCE_CHARS = 1000
TICKET_CHARS = 300


class ModelPlanner:
    prompt_version = prompts.PLANNER_VERSION
    ruleset_version = "none"
    model_id = model_manifest.MODEL["served_id"]

    def __init__(self, case: Optional[Dict[str, Any]] = None, client: Optional[LocalModelClient] = None) -> None:
        self.client = client or LocalModelClient()

    def next_action(self, state) -> PlannerReply:
        calls_left = int(state.policy.budgets.max_tool_calls) - int(state.counters.get("tool_calls", 0))
        tools = [t for t in TOOL_ACTIONS if t in state.policy.allowed_tools] if calls_left > 0 else []
        allowed = tools + ["finish", "escalate"]
        note = "" if tools else "No tool calls are left: finish or escalate."
        try:
            reply, response = consult(state, self.client, "planner", allowed, note, "synthesis", ticket_chars=TICKET_CHARS,
                                      evidence_chars=EVIDENCE_CHARS, desc_chars=70)
            if reply["action"] == "finish":
                grounded = ground_hypotheses(reply, state)
                if not grounded:
                    return PlannerReply(Action(kind="escalate", reason="the model offered no cause grounded in the evidence"), model=response.model)
                return PlannerReply(finish_action(grounded, reply["reason"]), model=response.model)
            if reply["action"] == "escalate":
                return PlannerReply(Action(kind="escalate", reason=(reply["reason"] or "the model found the evidence insufficient")[:300]), model=response.model)
            return PlannerReply(tool_action(reply, state, allowed), model=response.model)
        except ModelReplyError as exc:
            raise PlannerError(f"unusable model reply: {exc}") from exc
