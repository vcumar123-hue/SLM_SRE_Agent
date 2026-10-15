"""
The investigation runner (design 5.1, 7 and 10.1).

`Runner.run` always returns an AgentRunResult; it never raises into its caller. It enforces every budget, validates each planner action,
sanitises tool results into immutable evidence objects, verifies hypotheses deterministically and records every step so a run can be
replayed. The planner is injected (rules, model, or scripted for tests); this module contains no model or tool logic of its own.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from agentruntime import SCHEMA_VERSION
from agentruntime.context import TrustedContext
from agentruntime.contracts import (
    Action, AgentPolicy, AgentRunResult, AgentStep, EvidenceDraft, EvidenceItem, Hypothesis, evidence_hash,
)
from agentruntime.registry import (
    BoundaryViolation, ToolInputError, ToolRegistry, ToolTimeout, UnknownTool,
)
from agentruntime.sanitize import sanitize_text, wrap_untrusted
from agentruntime.verifier import compute_grounding, verify_hypotheses

EXCERPT_MAX = 400
FACTS_MAX_CHARS = 800
RUNTIME_VERSION = "r2-0"


class PlannerError(Exception):
    """The planner could not produce a valid action (model unreachable, malformed reply, ...)."""


@dataclass
class PlannerReply:
    action: Action
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    model: Optional[str] = None


class Planner(Protocol):
    prompt_version: str
    ruleset_version: str

    def next_action(self, state: "RunState") -> PlannerReply: ...


@dataclass
class RunState:
    """Read-only view handed to the planner."""
    ctx: TrustedContext
    policy: AgentPolicy
    task: Dict[str, Any]
    run_id: str
    evidence: List[EvidenceItem]
    steps: List[AgentStep]
    counters: Dict[str, Any]
    tools: List[Dict[str, Any]]
    elapsed_s: float = 0.0

    def evidence_block(self, max_chars: int = 3000) -> str:
        """Evidence the planner may read, delimited as untrusted data."""
        lines = [f"[{e.evidence_id}] {e.source_type}:{e.source_id} {e.excerpt}" for e in self.evidence]
        return wrap_untrusted("\n".join(lines)[:max_chars])


def _canonical(args: Dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))


def clean_obj(value: Any, max_chars: int = 1000) -> Any:
    """Sanitise every string in a JSON-like structure before it is recorded."""
    if isinstance(value, str):
        return sanitize_text(value, max_chars)[0]
    if isinstance(value, dict):
        return {str(k): clean_obj(v, max_chars) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_obj(v, max_chars) for v in value]
    return value


class Runner:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        clock: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.now_fn = now_fn

    # -- helpers ------------------------------------------------------------------------------------------------------

    def _make_items(self, drafts: List[EvidenceDraft], evidence: List[EvidenceItem], run_id: str, policy: AgentPolicy,
                    next_index: int) -> Tuple[List[EvidenceItem], List[str], bool]:
        """Sanitise drafts into immutable items, dropping duplicates and honouring the size caps."""
        budgets = policy.budgets
        existing = {(e.source_type, e.source_id) for e in evidence}
        items: List[EvidenceItem] = []
        flags: List[str] = []
        truncated = False
        chars = 0
        for draft in drafts:
            key = (draft.source_type, draft.source_id)
            if key in existing:
                continue
            if len(evidence) + len(items) >= budgets.max_evidence_items:
                truncated = True
                break
            excerpt, status, item_flags = sanitize_text(draft.excerpt, EXCERPT_MAX)
            if chars + len(excerpt) > budgets.max_result_chars:
                truncated = True
                break
            chars += len(excerpt)
            facts = draft.facts
            if facts is not None:
                facts = clean_obj(facts, 200)
                if len(json.dumps(facts, default=str)) > FACTS_MAX_CHARS:
                    facts = None
                    status = "truncated"
                    truncated = True
            existing.add(key)
            items.append(EvidenceItem(
                evidence_id=f"e{next_index + len(items)}",
                run_id=run_id,
                tenant_id=draft.tenant_id,
                tool=draft.tool,
                tool_version=draft.tool_version,
                source_type=draft.source_type,
                source_id=sanitize_text(draft.source_id, 200)[0],
                provenance=draft.provenance,
                retrieved_at=self.now_fn(),
                data_timestamp=draft.data_timestamp,
                ttl_days=draft.ttl_days,
                sanitization=status,
                relevance=draft.relevance,
                excerpt=excerpt,
                facts=facts,
                content_hash=evidence_hash(excerpt, facts),
                flags=item_flags,
            ))
        return items, flags, truncated

    # -- the loop ---------------------------------------------------------------------------------------------------------

    def run(self, ctx: TrustedContext, policy: AgentPolicy, task: Dict[str, Any], planner: Planner,
            run_id: Optional[str] = None) -> AgentRunResult:
        run_id = run_id or uuid.uuid4().hex[:16]
        started = self.clock()
        budgets = policy.budgets
        counters: Dict[str, Any] = {"steps": 0, "tool_calls": 0, "model_calls": 0, "tokens": 0, "no_progress": 0,
                                    "invalid_actions": 0, "tool_failures": 0, "skipped_duplicates": 0}
        evidence: List[EvidenceItem] = []
        steps: List[AgentStep] = []
        hypotheses: List[Hypothesis] = []
        analysis: Optional[Dict[str, Any]] = None
        flags: List[str] = []
        status: str = "failed_internal"
        reason = ""
        seen: set = set()
        consecutive_invalid = 0
        consecutive_tool_failures = 0

        def elapsed() -> float:
            return self.clock() - started

        try:
            unknown = [t for t in policy.allowed_tools if t not in self.registry.names()]
            if unknown:
                status, reason = "denied_policy", f"allowed tool(s) not registered: {sorted(unknown)}"
                raise _Stop()
            while True:
                if elapsed() >= budgets.deadline_s:
                    status, reason = "timeout_deadline", "deadline reached"
                    break
                if counters["steps"] >= budgets.max_steps:
                    status, reason = "budget_exhausted", "step budget used"
                    break
                if counters["model_calls"] >= budgets.max_model_calls or counters["tokens"] >= budgets.max_tokens_run:
                    status, reason = "budget_exhausted", "model call or token budget used"
                    break

                state = RunState(ctx=ctx, policy=policy, task=task, run_id=run_id, evidence=list(evidence), steps=list(steps),
                                 counters=dict(counters), tools=self.registry.describe(policy.allowed_tools), elapsed_s=elapsed())
                t0 = self.clock()
                n = counters["steps"] + 1
                try:
                    reply = planner.next_action(state)
                    if not isinstance(reply, PlannerReply) or not isinstance(reply.action, Action):
                        raise PlannerError("the planner returned something that is not an action")
                except Exception as exc:  # noqa: BLE001 - any planner failure is recorded, never raised
                    counters["steps"] += 1
                    counters["invalid_actions"] += 1
                    consecutive_invalid += 1
                    steps.append(AgentStep(n=n, kind="plan", error="planner_error: " + sanitize_text(str(exc), 200)[0],
                                           duration_ms=int((self.clock() - t0) * 1000)))
                    if consecutive_invalid >= 2:
                        status, reason = "failed_model", "the planner failed twice in a row"
                        break
                    continue

                counters["steps"] += 1
                counters["model_calls"] += max(0, int(reply.model_calls))
                counters["tokens"] += max(0, int(reply.tokens_in)) + max(0, int(reply.tokens_out))
                action = reply.action
                recorded_action = clean_obj(action.model_dump(mode="json"))
                duration = lambda: int((self.clock() - t0) * 1000)  # noqa: E731

                if action.kind == "escalate":
                    steps.append(AgentStep(n=n, kind="plan", action=recorded_action, result_summary="escalate",
                                           model=reply.model, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                                           duration_ms=duration()))
                    status = "escalated_conflict" if action.conflict else "escalated_insufficient"
                    reason = sanitize_text(action.reason or "the planner escalated", 200)[0]
                    break

                if action.kind == "finish":
                    proposals: List[Hypothesis] = []
                    for raw in action.hypotheses:
                        try:
                            proposals.append(Hypothesis(
                                cause=str(raw.get("cause", ""))[:500], status=raw.get("status", "weak"),
                                evidence=[str(e) for e in (raw.get("evidence") or [])][:10], verified=False,
                                notes=str(raw.get("notes", ""))[:300]))
                        except Exception:  # noqa: BLE001 - a malformed proposal is dropped
                            continue
                    proposals = [h.model_copy(update={"cause": sanitize_text(h.cause, 500)[0], "notes": sanitize_text(h.notes, 300)[0]})
                                 for h in proposals if h.cause]
                    hypotheses = verify_hypotheses(proposals, evidence, ctx, run_id, policy.allowed_tools, self.now_fn())
                    supported = [h for h in hypotheses if h.status == "supported" and h.verified]
                    steps.append(AgentStep(n=n, kind="synthesise", action=recorded_action, result_summary=f"{len(supported)} supported",
                                           model=reply.model, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                                           duration_ms=duration()))
                    if action.conflict:
                        status, reason = "escalated_conflict", "unresolved conflict among the leading hypotheses"
                    elif supported:
                        status, reason = "supported", "a verified hypothesis is supported by evidence"
                        analysis = clean_obj(action.analysis) if action.analysis is not None else None
                    else:
                        status, reason = "escalated_insufficient", "no hypothesis has verified evidence"
                    break

                # action.kind == "tool"
                tool = action.tool or ""
                args = dict(action.input or {})
                step_common = dict(n=n, kind="tool", tool=tool, tool_input=clean_obj(args), action=recorded_action,
                                   model=reply.model, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out)
                if tool not in policy.allowed_tools:
                    counters["invalid_actions"] += 1
                    consecutive_invalid += 1
                    steps.append(AgentStep(**step_common, error="tool_not_allowed", duration_ms=duration()))
                    if consecutive_invalid >= 2:
                        status, reason = "failed_model", "the planner chose tools that are not allowed"
                        break
                    continue
                if counters["tool_calls"] >= budgets.max_tool_calls:
                    steps.append(AgentStep(**step_common, error="budget:tool_calls", duration_ms=duration()))
                    status, reason = "budget_exhausted", "tool call budget used"
                    break
                key = (tool, _canonical(args))
                if key in seen:
                    counters["skipped_duplicates"] += 1
                    counters["no_progress"] += 1
                    steps.append(AgentStep(**step_common, skipped=True, result_summary="duplicate call skipped", duration_ms=duration()))
                    if counters["no_progress"] >= budgets.no_progress_limit:
                        status, reason = "escalated_insufficient", "no new evidence"
                        break
                    continue
                seen.add(key)
                counters["tool_calls"] += 1
                timeout = max(0.1, min(budgets.tool_timeout_s, budgets.deadline_s - elapsed()))
                try:
                    drafts = self.registry.call(ctx, tool, args, timeout)
                except ToolInputError as exc:
                    counters["invalid_actions"] += 1
                    consecutive_invalid += 1
                    steps.append(AgentStep(**step_common, error="tool_input: " + sanitize_text(str(exc), 200)[0], duration_ms=duration()))
                    if consecutive_invalid >= 2:
                        status, reason = "failed_model", "the planner sent invalid tool arguments twice in a row"
                        break
                    continue
                except BoundaryViolation as exc:
                    flags.append("boundary_violation")
                    steps.append(AgentStep(**step_common, error="boundary: " + sanitize_text(str(exc), 200)[0], duration_ms=duration()))
                    status, reason = "failed_tool", "a tool broke the tenant boundary; its result was discarded"
                    break
                except UnknownTool:
                    counters["invalid_actions"] += 1
                    steps.append(AgentStep(**step_common, error="unknown_tool", duration_ms=duration()))
                    status, reason = "failed_model", "the planner chose an unknown tool"
                    break
                except ToolTimeout as exc:
                    counters["tool_failures"] += 1
                    consecutive_tool_failures += 1
                    steps.append(AgentStep(**step_common, error="tool_timeout: " + sanitize_text(str(exc), 200)[0], duration_ms=duration()))
                    if consecutive_tool_failures >= 2:
                        status, reason = "failed_tool", "two tool failures in a row"
                        break
                    continue
                except Exception as exc:  # noqa: BLE001
                    counters["tool_failures"] += 1
                    consecutive_tool_failures += 1
                    steps.append(AgentStep(**step_common, error="tool_error: " + sanitize_text(str(exc), 200)[0], duration_ms=duration()))
                    if consecutive_tool_failures >= 2:
                        status, reason = "failed_tool", "two tool failures in a row"
                        break
                    continue
                consecutive_invalid = 0
                consecutive_tool_failures = 0
                items, item_flags, truncated = self._make_items(drafts, evidence, run_id, policy, len(evidence) + 1)
                evidence.extend(items)
                if any("instruction_like_text" in i.flags for i in items) and "instruction_like_text" not in flags:
                    flags.append("instruction_like_text")
                if truncated and "result_truncated" not in flags:
                    flags.append("result_truncated")
                if items:
                    counters["no_progress"] = 0
                else:
                    counters["no_progress"] += 1
                steps.append(AgentStep(**step_common, result_summary=f"{len(items)} evidence item(s)",
                                       evidence_added=[i.evidence_id for i in items], duration_ms=duration()))
                if counters["no_progress"] >= budgets.no_progress_limit:
                    status, reason = "escalated_insufficient", "no new evidence"
                    break
        except _Stop:
            pass
        except Exception as exc:  # noqa: BLE001 - the runner never raises into its caller
            status, reason = "failed_internal", "internal error: " + sanitize_text(type(exc).__name__ + ": " + str(exc), 200)[0]

        counters["elapsed_s"] = round(elapsed(), 3)
        result = AgentRunResult(
            run_id=run_id, tenant_id=ctx.tenant_id, status=status, reason=reason, hypotheses=hypotheses, evidence=evidence,
            steps=steps, analysis=analysis if status == "supported" else None, counters=counters,
            versions={
                "schema": SCHEMA_VERSION, "runtime": RUNTIME_VERSION, "tools": self.registry.versions(),
                "prompt": getattr(planner, "prompt_version", "none"), "ruleset": getattr(planner, "ruleset_version", "none"),
                "model": getattr(planner, "model_id", None), "policy_hash": policy.policy_hash(),
            },
            mode=policy.mode, flags=flags,
        )
        try:
            result = result.model_copy(update={"evidence_grounding": compute_grounding(result, self.now_fn())})
        except Exception:  # noqa: BLE001
            pass
        return result


class _Stop(Exception):
    """Internal: leave the loop with the status already set."""
