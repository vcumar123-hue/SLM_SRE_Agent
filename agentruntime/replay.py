"""
Record/replay (design 5.3 and 11.2).

Replay re-executes the runner with the recorded planner actions and the recorded evidence substituted for tool results, so it needs
neither a model nor a database and never makes an external call. A record is `valid` for comparison only if every recorded version
matches the current one and every evidence hash verifies; otherwise it is `stale` and may be inspected but not used for acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agentruntime.context import TrustedContext
from agentruntime.contracts import Action, AgentPolicy, AgentRunResult, EvidenceDraft, evidence_hash
from agentruntime.registry import BoundaryViolation, ToolInputError, ToolRegistry, ToolTimeout
from agentruntime.runner import REQUIRED_VERSION_KEYS, PlannerError, PlannerReply, RunState, Runner


@dataclass
class ReplayReport:
    valid: bool                       # versions and hashes match: usable for comparison
    identical: bool                   # the replayed result equals the recorded one
    stale_reasons: List[str] = field(default_factory=list)
    differences: List[str] = field(default_factory=list)
    result: Optional[AgentRunResult] = None


def _strip(message: Optional[str], prefix: str) -> str:
    """The recorded error text without the prefix the runner adds, so the replayed step records the identical string."""
    message = message or ""
    return message[len(prefix):] if message.startswith(prefix) else message


class RecordedPlanner:
    """Returns the recorded decisions in order; recorded planner failures are raised again."""

    def __init__(self, recorded: AgentRunResult) -> None:
        self._steps = list(recorded.steps)
        self._i = 0
        self.prompt_version = str(recorded.versions.get("prompt", "none"))
        self.ruleset_version = str(recorded.versions.get("ruleset", "none"))
        self.model_id = recorded.versions.get("model")

    def next_action(self, state: RunState) -> PlannerReply:
        if self._i >= len(self._steps):
            raise PlannerError("replay ran past the recorded steps")
        step = self._steps[self._i]
        self._i += 1
        if step.action is None:
            raise PlannerError(_strip(step.error, "planner_error: "))
        return PlannerReply(action=Action(**step.action), model_calls=1 if step.model else 0,
                            tokens_in=step.tokens_in, tokens_out=step.tokens_out, model=step.model)


class RecordedRegistry(ToolRegistry):
    """A registry that answers tool calls from recorded steps instead of running tools."""

    def __init__(self, source: ToolRegistry, recorded: AgentRunResult) -> None:
        super().__init__()
        self._source = source
        by_id = {e.evidence_id: e for e in recorded.evidence}
        self._calls: List[Dict[str, Any]] = []
        for step in recorded.steps:
            if step.kind != "tool" or step.skipped or step.action is None:
                continue
            if step.error and step.error.startswith("tool_input"):
                self._calls.append({"raise": ToolInputError(_strip(step.error, "tool_input: "))})
            elif step.error and step.error.startswith("boundary"):
                self._calls.append({"raise": BoundaryViolation(_strip(step.error, "boundary: "))})
            elif step.error and step.error.startswith("tool_timeout"):
                self._calls.append({"raise": ToolTimeout(_strip(step.error, "tool_timeout: "))})
            elif step.error and step.error.startswith("tool_error"):
                self._calls.append({"raise": RuntimeError(_strip(step.error, "tool_error: "))})
            elif step.error:
                continue
            else:
                drafts = [EvidenceDraft(**{k: v for k, v in by_id[eid].model_dump().items()
                                           if k in EvidenceDraft.model_fields}) for eid in step.evidence_added if eid in by_id]
                self._calls.append({"drafts": drafts})
        self._i = 0

    def names(self) -> List[str]:
        return self._source.names()

    def versions(self) -> Dict[str, str]:
        return self._source.versions()

    def describe(self, allowed: List[str]) -> List[Dict[str, Any]]:
        return self._source.describe(allowed)

    def spec(self, name: str):
        return self._source.spec(name)

    def call(self, ctx: TrustedContext, name: str, args: Dict[str, Any], timeout_s: float) -> List[EvidenceDraft]:
        if self._i >= len(self._calls):
            return []
        recorded = self._calls[self._i]
        self._i += 1
        if "raise" in recorded:
            raise recorded["raise"]
        return [d.model_copy(update={"tool": name, "tool_version": self._source.spec(name).version}) for d in recorded["drafts"]]


def _shape(r: AgentRunResult) -> Dict[str, Any]:
    """The parts of a result that must be identical on replay (no timings, timestamps or durations)."""
    return {
        "status": r.status,
        "hypotheses": [(h.cause, h.status, tuple(h.evidence), h.verified) for h in r.hypotheses],
        "evidence": [(e.evidence_id, e.tool, e.source_type, e.source_id, e.content_hash, e.sanitization) for e in r.evidence],
        "steps": [(s.n, s.kind, s.tool, repr(s.tool_input), tuple(s.evidence_added), s.error, s.skipped) for s in r.steps],
        "analysis": r.analysis,
        "counters": {k: r.counters.get(k) for k in ("steps", "tool_calls", "model_calls", "tokens", "invalid_actions", "skipped_duplicates")},
        "flags": sorted(r.flags),
    }


def replay(recorded: AgentRunResult, registry: ToolRegistry, ctx: TrustedContext, policy: AgentPolicy,
           current_versions: Dict[str, Any]) -> ReplayReport:
    """Replay a stored run.

    `current_versions` is the **complete** current manifest (build `runner.build_versions(registry, current_planner, policy)`):
    schema, runtime, tools, prompt, model, ruleset and policy hash. Replay fails closed: a manifest or recorded run that lacks any
    required key, or any value that differs, makes the record `stale` (inspectable, never usable for acceptance comparisons).
    """
    stale: List[str] = []
    for key in REQUIRED_VERSION_KEYS:
        if key not in current_versions:
            stale.append(f"the current version manifest is missing '{key}'")
        elif key not in recorded.versions:
            stale.append(f"the recorded run has no '{key}' version")
        elif recorded.versions[key] != current_versions[key]:
            stale.append(f"{key} version differs (recorded {recorded.versions[key]!r}, current {current_versions[key]!r})")
    if current_versions.get("policy_hash") != policy.policy_hash():
        stale.append("the supplied policy does not match the current manifest")
    for item in recorded.evidence:
        if evidence_hash(item.excerpt, item.facts) != item.content_hash:
            stale.append(f"evidence {item.evidence_id} does not match its hash")
    if ctx.tenant_id != recorded.tenant_id:
        stale.append("the replay context is for another tenant")
    if stale:
        return ReplayReport(valid=False, identical=False, stale_reasons=stale)

    replay_time = max((e.retrieved_at for e in recorded.evidence), default=datetime.now(timezone.utc))
    runner = Runner(RecordedRegistry(registry, recorded), now_fn=lambda: replay_time)
    replayed = runner.replay_run(ctx, policy, {"replay": True}, RecordedPlanner(recorded), run_id=recorded.run_id)
    a, b = _shape(recorded), _shape(replayed)
    diffs = [f"{k}: recorded {a[k]!r} != replayed {b[k]!r}" for k in a if a[k] != b[k]]
    return ReplayReport(valid=True, identical=not diffs, differences=diffs, result=replayed)
