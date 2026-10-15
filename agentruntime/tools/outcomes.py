"""
outcome_lookup: what the outcome ledger records for a classification, for this tenant only (design 4.3).

The ledger matches strictly by tenant (Wave 3), so another client's track record never appears. An empty ledger is itself evidence and is
returned as an explicit "no recorded outcomes" item rather than as nothing.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agentruntime.context import TrustedContext
from agentruntime.contracts import EvidenceDraft, ToolSpec
from agentruntime.db_limits import statement_timeout
from agentruntime.executor import CancelToken
from agentruntime.tools._common import check_cancel, clip, parse_timestamp

NAME = "outcome_lookup"
VERSION = "1"
TTL_DAYS = 30
CLASSIFICATION_PATTERN = r"[a-z0-9_]{2,80}"

SPEC = ToolSpec(
    name=NAME, version=VERSION, max_items=1,
    description="What the outcome ledger records for a classification: how many resolutions, the success rate and the recurrence rate. Only this client's outcomes are counted.",
    input_schema={
        "type": "object", "additionalProperties": False, "required": ["classification"],
        "properties": {"classification": {"type": "string", "minLength": 2, "maxLength": 80, "pattern": CLASSIFICATION_PATTERN}},
    },
)


def make_tool(summarize: Optional[Callable[..., Optional[Dict[str, Any]]]] = None) -> Callable[..., List[EvidenceDraft]]:
    def outcome_lookup(ctx: TrustedContext, classification: str, *, cancel: Optional[CancelToken] = None,
                       timeout_s: Optional[float] = None) -> List[EvidenceDraft]:
        check_cancel(cancel)
        fn = summarize
        if fn is None:
            import resolution_outcome
            fn = resolution_outcome.summarize_outcomes
        with statement_timeout(timeout_s):
            summary = fn(classification, tenant_id=ctx.tenant_id)
        check_cancel(cancel)
        if not summary:
            return [EvidenceDraft(
                tenant_id=ctx.tenant_id, source_type="outcome", source_id=classification, provenance="computed", ttl_days=TTL_DAYS,
                excerpt=f"No recorded outcomes for {classification} for this client.", facts={"total_outcomes": 0})]
        total = int(summary.get("total_outcomes") or 0)
        success, recurrence = summary.get("success_rate"), summary.get("recurrence_rate")
        return [EvidenceDraft(
            tenant_id=ctx.tenant_id, source_type="outcome", source_id=classification, provenance="tenant_data", ttl_days=TTL_DAYS,
            data_timestamp=parse_timestamp(summary.get("last_resolved_at")),
            excerpt=clip(f"{total} recorded outcome(s) for {classification}: success rate {success}, recurrence rate {recurrence}, "
                         f"signal {summary.get('confidence_signal')}."),
            facts={"total_outcomes": total, "success_rate": success, "recurrence_rate": recurrence,
                   "dominant_execution_mode": clip(summary.get("dominant_execution_mode"), 30),
                   "avg_resolution_minutes": summary.get("avg_resolution_minutes"),
                   "last_resolved_at": clip(summary.get("last_resolved_at"), 40)})]

    return outcome_lookup
