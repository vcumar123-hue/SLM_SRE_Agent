"""
incident_kb_search: similar incidents from the knowledge layers (design 4.3).

A boundary wrapper, not a pass-through. The model supplies only `query` and `top_k`. The tenant comes from the trusted context; the
knowledge scope, search order and collection are fixed here and passed explicitly (retrieval's own default tenant is `client_a`, and
nothing may rely on it). Every returned row is checked: a tenant-private row for another tenant is a boundary violation.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agentruntime.context import TrustedContext
from agentruntime.contracts import EvidenceDraft, ToolSpec
from agentruntime.db_limits import statement_timeout
from agentruntime.executor import CancelToken
from agentruntime.registry import BoundaryViolation
from agentruntime.tools._common import check_cancel, clip, relevance

NAME = "incident_kb_search"
VERSION = "1"
MAX_ITEMS = 5

SPEC = ToolSpec(
    name=NAME, version=VERSION, max_items=MAX_ITEMS,
    description="Search the knowledge base for incidents similar to a description. Returns up to 5 incidents with their classification and a short excerpt.",
    input_schema={
        "type": "object", "additionalProperties": False, "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 3, "maxLength": 300},
            "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS},
        },
    },
)

_LEARNED_LAYERS = {"global_learned", "global_learned_patterns_index"}


def _provenance(row: Dict[str, Any], tenant: str) -> str:
    scope = str(row.get("knowledge_scope") or "").lower()
    layer = str(row.get("collection_name") or "").lower()
    if scope == "client_private" or layer == f"{tenant}_index":
        return "tenant_data"
    if scope == "global_learned" or layer in _LEARNED_LAYERS:
        return "learned"
    return "shared_base"


def make_tool(retrieve: Optional[Callable[..., List[Dict[str, Any]]]] = None) -> Callable[..., List[EvidenceDraft]]:
    def incident_kb_search(ctx: TrustedContext, query: str, top_k: int = 3, *, cancel: Optional[CancelToken] = None,
                           timeout_s: Optional[float] = None) -> List[EvidenceDraft]:
        check_cancel(cancel)
        fn = retrieve
        if fn is None:
            import retrieval_engine
            fn = retrieval_engine.retrieve_incidents
        limit = max(1, min(int(top_k), MAX_ITEMS))
        with statement_timeout(timeout_s):
            # every argument that selects what is searched is set here, on the server side
            rows = fn(ticket=query, limit=limit, domain=None, classification=None, service_scope=None,
                      tenant_id=ctx.tenant_id, knowledge_scope=ctx.knowledge_scopes[0], search_order=None, collection_name=None)
        check_cancel(cancel)
        drafts: List[EvidenceDraft] = []
        for row in list(rows or [])[:limit]:
            provenance = _provenance(row, ctx.tenant_id)
            row_tenant = str(row.get("tenant_id") or "")
            if provenance == "tenant_data" and row_tenant and row_tenant != ctx.tenant_id:
                raise BoundaryViolation("incident_kb_search returned a private record of another tenant")
            title = clip(row.get("title"), 120)
            body = clip(row.get("summary") or row.get("content"), 300)
            drafts.append(EvidenceDraft(
                tenant_id=ctx.tenant_id, source_type="incident", source_id=str(row.get("id") or ""), provenance=provenance,
                relevance=relevance(row.get("score")),
                excerpt=f"{title}. {body}".strip(". "),
                facts={"classification": clip(row.get("classification"), 80), "domain": clip(row.get("domain"), 60),
                       "severity": clip(row.get("severity"), 20), "retrieval_method": clip(row.get("retrieval_method"), 30)},
            ))
        return [d for d in drafts if d.source_id]

    return incident_kb_search
