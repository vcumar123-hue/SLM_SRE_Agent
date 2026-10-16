"""
rules_then_model: the rule table plans the evidence; the local model is consulted only once the rules have nothing more to ask (design 9.2).

  1. While the rules name a tool (host history, knowledge-base search, one reformulation, outcome and pattern lookups, the conflict check) their action is
     taken unchanged: the model never replaces the evidence plan.
  2. When the rules would conclude, the model reviews the evidence it can see (the ticket and the tool results, both framed as untrusted data) and either
     finishes with causes, asks for one more search, or escalates. At most two such consultations are made, and at most one search may be requested.
  3. A cause from the model is accepted only if every evidence id it cites exists in this run and the quote it gives is found word for word in a cited
     record; `supported` also needs two or more relevant cited records, the same corroboration the rules require. Anything else is discarded.
  4. **Any model failure falls back to the rules' own conclusion**: the model being unreachable, a timeout, a reply over its token limit, an invalid or
     ungrounded reply, or a used-up model or token budget. The fallback is deterministic (it is exactly what rules_only would have done) and is recorded in
     the action's reason and in `fallbacks`.

Local model only, through the run's ModelGateway (its call, token, timeout and deadline budgets apply). Prompt: `prompts.SYNTHESIS_*`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from agentruntime.contracts import Action
from agentruntime.executor import Cancelled
from agentruntime.planners import model_manifest, prompts
from agentruntime.planners.model_support import (
    LocalModelClient, ModelReplyError, consult, finish_action, ground_hypotheses, tool_action,
)
from agentruntime.planners.rules_only import RulesOnlyPlanner
from agentruntime.runner import PlannerReply

MAX_CONSULTS = 2
SEARCH_ACTIONS = ("incident_kb_search", "ticket_history_lookup")
EVIDENCE_CHARS = 2400      # two consultations must fit the run's 4,000-token budget, prompt tokens included
TICKET_CHARS = 400


def _merge(rule_hypotheses: List[Dict[str, Any]], model_hypotheses: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    """The rules' hypotheses first (they are deterministic), then the model's that say something new. When both name the same cause the stronger status
    stands and the evidence is pooled; supported ones are listed first."""
    merged = [dict(h) for h in rule_hypotheses]
    index = {" ".join(h["cause"].lower().split()): h for h in merged}
    for h in model_hypotheses:
        key = " ".join(h["cause"].lower().split())
        if key in index:
            known = index[key]
            if h["status"] == "supported":
                known["status"] = "supported"
            known["evidence"] = list(dict.fromkeys(list(known["evidence"]) + list(h["evidence"])))[:5]
        else:
            index[key] = dict(h)
            merged.append(index[key])
    return sorted(merged, key=lambda h: h["status"] != "supported")[:limit]


class RulesThenModelPlanner:
    prompt_version = prompts.SYNTHESIS_VERSION
    ruleset_version = RulesOnlyPlanner.ruleset_version
    model_id = model_manifest.MODEL["served_id"]

    def __init__(self, case: Optional[Dict[str, Any]] = None, client: Optional[LocalModelClient] = None) -> None:
        self.rules = RulesOnlyPlanner(case)
        self.client = client or LocalModelClient()
        self.consults = 0
        self.search_requests = 0
        self.fallbacks: List[str] = []

    def next_action(self, state) -> PlannerReply:
        rule = self.rules.decide(state)
        if rule.kind == "tool":
            return PlannerReply(rule)
        budgets = state.policy.budgets
        if (self.consults >= MAX_CONSULTS or int(state.counters.get("model_calls", 0)) >= int(budgets.max_model_calls)
                or int(state.counters.get("tokens", 0)) >= int(budgets.max_tokens_run)):
            return PlannerReply(rule)
        calls_left = int(budgets.max_tool_calls) - int(state.counters.get("tool_calls", 0))
        may_search = self.search_requests < 1 and calls_left > 0
        allowed = ["finish", "escalate"] + ([a for a in SEARCH_ACTIONS if a in state.policy.allowed_tools] if may_search else [])
        note = ("You may ask for one more search if it could find missing evidence." if may_search else "No more searches are allowed: finish or escalate.")
        note += "\nThe rule set's own conclusion: " + (
            "causes " + "; ".join(h["cause"] for h in rule.hypotheses) if rule.kind == "finish" and rule.hypotheses else "no cause is established.")
        self.consults += 1
        try:
            reply, response = consult(state, self.client, "synthesis", allowed, note, "synthesis",
                                      ticket_chars=TICKET_CHARS, evidence_chars=EVIDENCE_CHARS, desc_chars=90)
            if reply["action"] in SEARCH_ACTIONS:
                action = tool_action(reply, state, allowed)
                self.search_requests += 1
                return PlannerReply(action, model=response.model)
            if reply["action"] == "finish":
                grounded = ground_hypotheses(reply, state)
                if not grounded:
                    raise ModelReplyError("the reply contained no grounded cause")
                merged = _merge(list(rule.hypotheses) if rule.kind == "finish" else [], grounded)
                return PlannerReply(finish_action(merged, reply["reason"], conflict=bool(rule.conflict)), model=response.model)
            return PlannerReply(rule, model=response.model)          # the model also finds no cause: the rules' own conclusion stands
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - every model failure falls back to the deterministic conclusion
            self.fallbacks.append(type(exc).__name__)
            return PlannerReply(rule.model_copy(update={"reason": f"rules: fallback after {type(exc).__name__}"}))
