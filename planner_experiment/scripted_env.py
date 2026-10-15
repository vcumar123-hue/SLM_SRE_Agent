"""
Scripted environment (design 12): tool outputs are captured once from the live system, then every planner mode is run against the same
recorded outputs, so any difference between modes comes from the planner and not from the data.

The recording is keyed by case id (the run's request id) and then by tool and canonical arguments. A planner that asks for exactly a
recorded call gets it. A planner that asks the same tool with different arguments gets that tool's *default* recorded output for the case
(the capture of the ticket's own natural query), and the substitution is counted so it can be reported; a tool never captured for the
case returns an explicit "no recorded output" item. The same rule applies to every mode.

Capture uses the real tools through the real registry, so tenant isolation and bounds are the same as in production. Held-out tickets
(the whole calibration selection and their clusters, and later the acceptance clusters) are removed from what capture can record.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from agentruntime.context import TrustedContext
from agentruntime.contracts import EvidenceDraft
from agentruntime.executor import BoundedExecutor, CancelToken, TOOL_EXECUTOR
from agentruntime.registry import ToolRegistry
from agentruntime.tools import TOOL_MODULES

SOURCE_TYPE = {"incident_kb_search": "incident", "ticket_history_lookup": "ticket", "outcome_lookup": "outcome",
               "pattern_lookup": "pattern", "evidence_lookup": "evidence_check"}


def args_key(tool: str, args: Dict[str, Any]) -> str:
    return tool + "|" + json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


class Recording:
    """{case_id: {tool: {args_key: [draft dicts]}}} plus the first-captured key per tool (the default)."""

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self.data: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = data or {}

    def add(self, case_id: str, tool: str, args: Dict[str, Any], drafts: Iterable[EvidenceDraft]) -> None:
        self.data.setdefault(case_id, {}).setdefault(tool, {})[args_key(tool, args)] = [d.model_dump(mode="json") for d in drafts]

    def lookup(self, case_id: str, tool: str, args: Dict[str, Any]) -> Optional[tuple]:
        """(drafts, 'exact'|'default') or None when the tool was never captured for this case."""
        calls = self.data.get(case_id, {}).get(tool)
        if not calls:
            return None
        key = args_key(tool, args)
        if key in calls:
            return calls[key], "exact"
        return next(iter(calls.values())), "default"

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.data, sort_keys=True, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Recording":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def case_ids(self) -> List[str]:
        return sorted(self.data)


class ScriptedEnv:
    """Builds a ToolRegistry whose tools replay a Recording, and counts exact, default and missing lookups per case."""

    def __init__(self, recording: Recording) -> None:
        self.recording = recording
        self.stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"exact": 0, "default": 0, "missing": 0})

    def registry(self, executor: Optional[BoundedExecutor] = None) -> ToolRegistry:
        reg = ToolRegistry(executor=executor or TOOL_EXECUTOR)
        for module in TOOL_MODULES:
            reg.register(module.SPEC, self._make(module.NAME, module.VERSION))
        return reg

    def _make(self, name: str, version: str) -> Callable[..., List[EvidenceDraft]]:
        env = self

        def scripted(ctx: TrustedContext, *, cancel: Optional[CancelToken] = None, timeout_s: Optional[float] = None, **args):
            case_id = ctx.request_id
            found = env.recording.lookup(case_id, name, args)
            if found is None:
                env.stats[case_id]["missing"] += 1
                return [EvidenceDraft(tenant_id=ctx.tenant_id, source_type=SOURCE_TYPE[name], source_id=f"unscripted:{name}",
                                      provenance="computed", excerpt="No recorded output for this call.", facts={"recorded": False})]
            rows, how = found
            env.stats[case_id][how] += 1
            return [EvidenceDraft(**{**r, "tenant_id": ctx.tenant_id}) for r in rows]

        scripted.__name__ = name
        return scripted


# -- capture --------------------------------------------------------------------------------------------------------------------------

def candidate_calls(case: Dict[str, Any]) -> List[tuple]:
    """The natural calls for a case, derived deterministically from the ticket: (tool, args). The first call per tool is its default."""
    text = str(case.get("text") or "")[:300] or "incident"
    calls: List[tuple] = [("incident_kb_search", {"query": text, "top_k": 5})]
    host, service = case.get("host"), case.get("service")
    if host and len(str(host)) >= 2:
        calls.append(("ticket_history_lookup", {"host": str(host)[:80], "days": 400}))
    if service and len(str(service)) >= 2:
        calls.append(("ticket_history_lookup", {"service": str(service)[:80], "days": 400}))
    if not host and not service:
        calls.append(("ticket_history_lookup", {"text": text[:200], "days": 400}))
    classification = case.get("classification")
    if classification:
        calls.append(("outcome_lookup", {"classification": classification}))
        calls.append(("pattern_lookup", {"classification": classification}))
    calls.append(("evidence_lookup", {}))
    return calls


def capture_case(registry: ToolRegistry, ctx: TrustedContext, case: Dict[str, Any], recording: Recording, case_id: str,
                 excluded_source_ids: Set[str] = frozenset(), timeout_s: float = 10.0) -> Dict[str, str]:
    """Run the natural calls once against the real registry and record them. Returns {key: 'ok'|error text}.

    Held-out records are removed from what is recorded (`excluded_source_ids`), so a ticket can never be found by finding itself.
    """
    status: Dict[str, str] = {}
    for tool, args in candidate_calls(case):
        try:
            drafts = registry.call(ctx, tool, args, timeout_s)
            kept = [d for d in drafts if d.source_id not in excluded_source_ids]
            recording.add(case_id, tool, args, kept)
            status[args_key(tool, args)] = "ok"
        except Exception as exc:  # noqa: BLE001 - a failed capture is reported, not hidden
            status[args_key(tool, args)] = f"{type(exc).__name__}: {exc}"
    return status
