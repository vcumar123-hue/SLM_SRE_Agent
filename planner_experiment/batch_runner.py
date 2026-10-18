"""
The R2-3 batch runner (design 8.4 and 13.5): sequential, `batch` mode, production budgets, the frozen `rules_only` planner, with every held-out ticket and its cluster removed from what can be retrieved.

Held-out exclusion (13.2): "every held-out ticket and its cluster are removed from the retrievable index for evaluation runs, so the loop cannot find the answer by finding the ticket itself". Two
boundaries are covered, both failing closed:
  * the five tools: `filtered_registry` wraps each real tool and drops, before anything is returned, every item the exclusion matcher recognises as a held-out ticket, a copy of one, or a member of its
    cluster; an item that cannot be classified aborts the run (never kept silently);
  * the deterministic pipeline's own retrieval: `held_out_retrieval` patches the retrieval engine's functions for the duration of a block with the same filter, so the baseline arm and the loop see the same corpus.

Infrastructure re-run rule (13.5), predeclared: a run may be re-run at most twice with the identical frozen configuration, only when it ended `failed_internal` for an infrastructure reason on the fixed
error-class list below; model-behaviour outcomes are never re-run; every re-run is logged; more than 5% of runs needing one is a protocol deviation to be reviewed before any gate is read.
"""
from __future__ import annotations

import contextlib
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from agentruntime.contracts import AgentPolicy, Budgets, EvidenceDraft
from agentruntime.executor import CancelToken, TOOL_EXECUTOR
from agentruntime.registry import ToolRegistry
from agentruntime.runner import BATCH_ENV, Runner
from agentruntime.tools import TOOL_MODULES
from planner_experiment.exclusion import Matcher, Undecidable

INFRA_ERROR_CLASSES = ("OperationalError", "InterfaceError", "ConnectionError", "ConnectionRefusedError", "TimeoutError_infra", "ModelUnavailable")
INFRA_REASON_MARKERS = ("database", "connection", "container", "model server", "unavailable")
MAX_RERUNS = 2
RERUN_DEVIATION_SHARE = 0.05


class BatchError(Exception):
    pass


class HeldOutFilter:
    """Decides, per item, whether it may be seen. Fails closed: an item that cannot be classified raises."""

    def __init__(self, held_out: Iterable[Dict[str, Any]]) -> None:
        self.matcher = Matcher(held_out)
        self.matcher.self_test()
        self.removed = 0

    def keep(self, tenant: str, source_id: str, excerpt: str) -> bool:
        try:
            why = self.matcher.reason(tenant, source_id, excerpt)
        except Undecidable as exc:
            raise BatchError(f"held-out exclusion could not be decided for an item ({exc}); the run is stopped") from exc
        if why:
            self.removed += 1
            return False
        return True


def filtered_registry(filt: HeldOutFilter, make_tools: Optional[Dict[str, Callable[..., Any]]] = None) -> ToolRegistry:
    """The five production tools (or the supplied callables, for tests) with held-out items removed from every result."""
    reg = ToolRegistry(executor=TOOL_EXECUTOR)
    for module in TOOL_MODULES:
        real = (make_tools or {}).get(module.NAME) or module.make_tool()

        def wrapped(ctx, *, cancel: Optional[CancelToken] = None, timeout_s: Optional[float] = None, _real=real, **args: Any) -> List[EvidenceDraft]:
            rows = _real(ctx, cancel=cancel, timeout_s=timeout_s, **args)
            return [d for d in rows if filt.keep(ctx.tenant_id, d.source_id, d.excerpt)]
        wrapped.__name__ = module.NAME
        reg.register(module.SPEC, wrapped)
    return reg


@contextlib.contextmanager
def held_out_retrieval(filt: HeldOutFilter, engine: Any, tenant_of: Optional[Callable[[Dict[str, Any]], str]] = None, names: Iterable[str] = ("retrieve_incidents", "hybrid_search")):
    """Patch the retrieval engine's search functions for the block so the deterministic pipeline sees the same corpus as the loop. Rows are dicts with an id and a title/summary/content."""
    originals = {}

    def make(fn):
        def filtered(*args: Any, **kwargs: Any):
            tenant = str(kwargs.get("tenant_id") or "")
            rows = fn(*args, **kwargs)
            kept = []
            for row in rows or []:
                text = " ".join(str(row.get(k) or "") for k in ("title", "summary", "content") if isinstance(row, dict))
                if filt.keep(tenant or (tenant_of(row) if tenant_of else str(row.get("tenant_id") or "")), str(row.get("id") or ""), text):
                    kept.append(row)
            return kept
        return filtered
    try:
        for name in names:
            if hasattr(engine, name):
                originals[name] = getattr(engine, name)
                setattr(engine, name, make(originals[name]))
        if not originals:
            raise BatchError("none of the retrieval functions to filter was found: refusing to run with an unfiltered corpus")
        yield
    finally:
        for name, fn in originals.items():
            setattr(engine, name, fn)


def is_infrastructure_failure(result: Any) -> bool:
    """A `failed_internal` run whose reason names an infrastructure cause (fixed list)."""
    if getattr(result, "status", "") != "failed_internal":
        return False
    reason = str(getattr(result, "reason", "")).lower()
    return any(m in reason for m in INFRA_REASON_MARKERS) or any(c.lower() in reason for c in INFRA_ERROR_CLASSES)


def run_batch(cases: List[Dict[str, Any]], registry: ToolRegistry, ctx_factory: Callable[[str, str], Any], planner_factory: Callable[[Dict[str, Any]], Any],
              policy: AgentPolicy, clock: Callable[[], float] = time.monotonic, runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Run every case once, in order, in batch mode. Returns {"records": [...], "reruns": [...], "deviation": bool}. Raises BatchError if any case cannot be completed."""
    if policy.mode != "batch":
        raise BatchError("the acceptance run uses mode batch")
    runner = runner or Runner(registry)
    previous = os.environ.get(BATCH_ENV)
    os.environ[BATCH_ENV] = "1"
    records: List[Dict[str, Any]] = []
    reruns: List[Dict[str, Any]] = []
    try:
        for case in cases:
            attempts = 0
            while True:
                attempts += 1
                start = clock()
                result = runner.run_batch(ctx_factory(case["tenant_id"], case["case_id"]), policy, {"ticket_text": case["ticket_text"]}, planner_factory(case))
                seconds = clock() - start
                if is_infrastructure_failure(result) and attempts <= MAX_RERUNS:
                    reruns.append({"case_id": case["case_id"], "attempt": attempts, "status": result.status, "reason": result.reason[:120]})
                    continue
                break
            records.append({"case_id": case["case_id"], "attempts": attempts, "seconds": round(seconds, 3), "result": result.model_dump(mode="json")})
    finally:
        if previous is None:
            os.environ.pop(BATCH_ENV, None)
        else:
            os.environ[BATCH_ENV] = previous
    return {"records": records, "reruns": reruns, "deviation": bool(cases) and len(reruns) / len(cases) > RERUN_DEVIATION_SHARE}
