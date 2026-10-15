"""Shared fixtures for the agentruntime tests: a trusted context, fake tools and a scripted planner."""
import json
import os
import pathlib
import tempfile
from typing import Any, Dict, List, Optional

from agentruntime.context import build_context
from agentruntime.contracts import Action, AgentPolicy, Budgets, EvidenceDraft, ToolSpec
from agentruntime.registry import ToolRegistry
from agentruntime.runner import PlannerError, PlannerReply


class FakePrincipal:
    id = "tester"

    def __init__(self, tenants=None):
        self._tenants = tenants

    def allows_tenant(self, tenant: str) -> bool:
        return self._tenants is None or tenant in self._tenants


def tenant_dir(*tenants: str) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    for t in tenants:
        (root / f"{t}.json").write_text(json.dumps({"tenant_id": t, "display_name": t}), encoding="utf-8")
    return root


def make_ctx(tenant: str = "client_x", mode: str = "batch"):
    return build_context(tenant, "req-1", FakePrincipal(), mode, config_dir=tenant_dir(tenant, "client_x", "client_y", tenant))


def draft(tenant: str, source_id: str, excerpt: str = "evidence text", source_type: str = "incident", **kw) -> EvidenceDraft:
    return EvidenceDraft(tenant_id=tenant, source_type=source_type, source_id=source_id, provenance="tenant_data",
                         excerpt=excerpt, relevance=kw.pop("relevance", 0.8), **kw)


KB_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["query"],
             "properties": {"query": {"type": "string", "maxLength": 300}, "top_k": {"type": "integer", "minimum": 1, "maximum": 5}}}
LOOKUP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["host"],
                 "properties": {"host": {"type": "string", "maxLength": 80}}}


def make_registry(kb_fn=None, history_fn=None) -> ToolRegistry:
    reg = ToolRegistry()

    def default_kb(ctx, query, top_k=3):
        return [draft(ctx.tenant_id, f"inc-{i}", f"{query} similar incident {i}") for i in range(2)]

    def default_hist(ctx, host):
        return [draft(ctx.tenant_id, f"INC{host}", f"ticket for {host}", "ticket")]

    reg.register(ToolSpec(name="incident_kb_search", description="similar incidents", input_schema=KB_SCHEMA), kb_fn or default_kb)
    reg.register(ToolSpec(name="ticket_history_lookup", description="tenant tickets", input_schema=LOOKUP_SCHEMA), history_fn or default_hist)
    return reg


class ScriptedPlanner:
    prompt_version = "test-prompt"
    ruleset_version = "test-rules"
    model_id = None

    def __init__(self, script: List[Any]):
        self.script = list(script)
        self.calls = 0

    def next_action(self, state):
        self.calls += 1
        if not self.script:
            raise PlannerError("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, PlannerReply):
            return item
        return PlannerReply(action=item, model_calls=1, tokens_in=10, tokens_out=5, model="test-model")


def tool_action(tool: str, **args) -> Action:
    return Action(kind="tool", tool=tool, input=args)


def finish_action(*hyps: Dict[str, Any], conflict: bool = False, analysis: Optional[dict] = None) -> Action:
    return Action(kind="finish", hypotheses=list(hyps), conflict=conflict,
                  analysis=analysis if analysis is not None else {"probable_causes": [h["cause"] for h in hyps]})


def policy(allowed=("incident_kb_search", "ticket_history_lookup"), mode="batch", **budget) -> AgentPolicy:
    return AgentPolicy(allowed_tools=list(allowed), budgets=Budgets(**budget), mode=mode)


def batch_env():
    """The explicit opt-in the batch entry point requires."""
    from unittest.mock import patch
    return patch.dict(os.environ, {"AIOPS_AGENT_BATCH_ENABLED": "1"})


class FakeClock:
    """A controllable monotonic clock: tools and planners advance it explicitly."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


def write_waiver_file(directory=None, **override):
    """A recorded waiver file, as an owner would create it. Returns its path."""
    root = pathlib.Path(directory) if directory else pathlib.Path(tempfile.mkdtemp())
    data = {"approved_by": "owner", "reason": "client policy accepts infrastructure encryption alone", "reference": "POLICY-7",
            "approved_at": "2026-10-14", "threat_model_reviewed": True}
    data.update(override)
    path = root / "agent_data_encryption_waiver.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def real_waiver(**override):
    """A verified waiver, obtained the only way production does: by loading the recorded file."""
    from agentruntime import crypto
    return crypto.load_waiver(write_waiver_file(**override))
