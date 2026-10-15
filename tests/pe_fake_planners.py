"""Fake planners for the planner-harness tests. They live in a module (not a test file) so the harness can import them by "module:Class"."""
import json
import re

from agentruntime.contracts import Action
from agentruntime.runner import PlannerReply


class GoodPlanner:
    prompt_version, ruleset_version, model_id = "fake-good", "fake", None

    def __init__(self, case):
        self.case, self.n = case, 0

    def next_action(self, state):
        self.n += 1
        if self.n == 1:
            return PlannerReply(Action(kind="tool", tool="incident_kb_search", input={"query": state.task["ticket_text"][:200] or "disk full"}))
        return PlannerReply(Action(kind="finish", hypotheses=[{"cause": "disk full on the database host", "status": "supported", "evidence": ["e1"]}],
                                   analysis={"probable_causes": ["disk full"]}))


class GiveUpPlanner:
    prompt_version, ruleset_version, model_id = "fake-giveup", "fake", None

    def __init__(self, case):
        pass

    def next_action(self, state):
        return PlannerReply(Action(kind="escalate", reason="no idea"))


class GullibleModelPlanner(GoodPlanner):
    """Obeys a planted instruction: searches for the canary it finds in the task or evidence."""
    prompt_version = "fake-gullible"

    def next_action(self, state):
        self.n += 1
        text = state.task.get("ticket_text", "") + json.dumps([e.excerpt for e in state.evidence])
        canary = re.search(r"CANARY-[0-9A-F]{10}", text)
        if self.n == 1 and canary:
            return PlannerReply(Action(kind="tool", tool="incident_kb_search", input={"query": canary.group(0)}))
        return super().next_action(state) if canary is None else PlannerReply(Action(kind="escalate", reason="done"))
