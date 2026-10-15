"""
pattern_lookup: dominant causes and actions recorded for a classification (design 4.3).

`get_pattern_intelligence` by default also counts records with **no tenant tag** (shared or legacy data), which could expose another
tenant's text. This tool therefore calls it with `strict_tenant=True`: only records tagged with exactly the trusted tenant are counted,
and untagged records are excluded. The tenant is never empty.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agentruntime.context import TrustedContext
from agentruntime.contracts import EvidenceDraft, ToolSpec
from agentruntime.executor import CancelToken
from agentruntime.tools._common import check_cancel, clip

NAME = "pattern_lookup"
VERSION = "1"
TTL_DAYS = 30
MAX_LISTED = 3

SPEC = ToolSpec(
    name=NAME, version=VERSION, max_items=1,
    description="The most common root causes and actions recorded for a classification, with a confidence level and a recurrence trend.",
    input_schema={
        "type": "object", "additionalProperties": False, "required": ["classification"],
        "properties": {"classification": {"type": "string", "minLength": 2, "maxLength": 80, "pattern": r"[a-z0-9_]{2,80}"}},
    },
)


def make_tool(intelligence: Optional[Callable[..., Optional[Dict[str, Any]]]] = None) -> Callable[..., List[EvidenceDraft]]:
    def pattern_lookup(ctx: TrustedContext, classification: str, *, cancel: Optional[CancelToken] = None,
                       timeout_s: Optional[float] = None) -> List[EvidenceDraft]:
        check_cancel(cancel)
        if not ctx.tenant_id:
            raise ValueError("pattern_lookup needs a tenant")            # never ask with an empty tenant: it would count every tenant
        fn = intelligence
        if fn is None:
            import knowledge_manager
            fn = knowledge_manager.get_pattern_intelligence
        from agentruntime.db_limits import statement_timeout
        with statement_timeout(timeout_s):
            result = fn(classification, tenant_id=ctx.tenant_id, strict_tenant=True)
        check_cancel(cancel)
        if not result:
            return [EvidenceDraft(
                tenant_id=ctx.tenant_id, source_type="pattern", source_id=classification, provenance="computed", ttl_days=TTL_DAYS,
                excerpt=f"No recorded resolution patterns for {classification}.", facts={"total_resolutions": 0, "candidate_count": 0})]
        causes = [clip(c, 120) for c in (result.get("dominant_root_causes") or [])[:MAX_LISTED]]
        actions = [clip(a, 120) for a in (result.get("dominant_actions") or [])[:MAX_LISTED]]
        return [EvidenceDraft(
            tenant_id=ctx.tenant_id, source_type="pattern", source_id=classification, provenance="tenant_data", ttl_days=TTL_DAYS,
            excerpt=clip(result.get("insight") or f"Patterns for {classification}"),
            facts={"total_resolutions": int(result.get("total_resolutions") or 0), "candidate_count": int(result.get("candidate_count") or 0),
                   "dominant_root_causes": causes, "dominant_actions": actions,
                   "pattern_confidence": clip(result.get("pattern_confidence"), 20), "recurrence_trend": clip(result.get("recurrence_trend"), 20)})]

    return pattern_lookup
