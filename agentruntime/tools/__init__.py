"""
The Release 2 read-only tool set (design 4.3): incident_kb_search, ticket_history_lookup, outcome_lookup, pattern_lookup, evidence_lookup.

Each tool is a wrapper in its own module. Those five modules are the only code under agentruntime/ that may import retrieval, database,
outcome, knowledge or evidence code; a test enforces that, so model input can only ever reach them through the validated tool schemas.
"""
from __future__ import annotations

from typing import Optional

from agentruntime.executor import BoundedExecutor, TOOL_EXECUTOR
from agentruntime.registry import ToolRegistry
from agentruntime.tools import evidence, kb_search, outcomes, patterns, ticket_history

TOOL_MODULES = (kb_search, ticket_history, outcomes, patterns, evidence)
TOOL_NAMES = tuple(m.NAME for m in TOOL_MODULES)


def build_default_registry(executor: Optional[BoundedExecutor] = None, **overrides) -> ToolRegistry:
    """A registry with the five tools. `overrides` (tests) may replace a tool's backing callable: retrieve, connect, summarize,
    intelligence, root."""
    reg = ToolRegistry(executor=executor or TOOL_EXECUTOR)
    reg.register(kb_search.SPEC, kb_search.make_tool(overrides.get("retrieve")))
    reg.register(ticket_history.SPEC, ticket_history.make_tool(overrides.get("connect")))
    reg.register(outcomes.SPEC, outcomes.make_tool(overrides.get("summarize")))
    reg.register(patterns.SPEC, patterns.make_tool(overrides.get("intelligence")))
    reg.register(evidence.SPEC, evidence.make_tool(overrides.get("root")))
    return reg
