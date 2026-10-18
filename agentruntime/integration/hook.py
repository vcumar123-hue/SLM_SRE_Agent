"""
The entry points the application calls (design 3.2 and 8; owner amendment, section 21).

Where each mode acts:

* `off` (the default, and what every doubt resolves to): every function here returns after one cheap check of the global setting. Nothing else is read, built or imported,
  the response is the very object it was, and no background task is attached.
* `shadow`: nothing happens until the HTTP response has been sent. The route attaches a Starlette background task (`shadow_background`) to the response; the task
  decides, builds the trusted context and queues the run only after the body has gone out. The queue then waits for idle (no user analysis in flight).
* `on` (blocked until R2-3 has passed, see gate): the investigation runs **before** the one-shot external fallback, inside the request, within the run's deadline
  (`investigate_before_fallback`, called from the legacy pipeline just ahead of `maybe_run_fallback`). A run that is `supported` and verified replaces the fallback call (merged
  or recorded as a conflict, never an override); anything else leaves the existing path, fallback included, exactly as it is. The run must be **stored for audit before its
  result is used**: if it cannot be stored the investigation is discarded and the response is the deterministic one. `after_analysis` then applies the stored run to the final
  response. The flag-driven agentic orchestrator path has its own fallback and is not integrated: `on` resolves to `off` while it is enabled (gate).
Whatever goes wrong here, the request gets the deterministic response: every failure is logged and the original object is returned.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from agentruntime.integration import gate

log = logging.getLogger(__name__)

_state: Dict[str, Any] = {"queue": None, "runner": None, "store_ready": False}
_principal: contextvars.ContextVar = contextvars.ContextVar("r2_principal", default=None)
_pending: contextvars.ContextVar = contextvars.ContextVar("r2_pending_run", default=None)


@dataclass
class LoopDecision:
    """What the investigation before the fallback decided: the stored run, how it reconciles, and whether it replaces the fallback call."""
    run: Any
    outcome: str
    replaces_fallback: bool


@contextlib.contextmanager
def request_scope(http_request: Any):
    """Wraps the analysis call in the route. With the loop off this does nothing at all. Otherwise it remembers who is asking (for the trusted context) and counts the
    analysis as in flight so a queued shadow run waits for it."""
    try:
        off = gate.global_mode()[0] == "off"
    except Exception:  # noqa: BLE001 - if the mode cannot even be read, the loop is off
        off = True
    if off:
        yield
        return
    from agentruntime.integration.shadow import analysis_in_flight
    principal = getattr(getattr(http_request, "state", None), "principal", None)
    token = _principal.set(principal)
    _pending.set(None)                                   # a fresh request starts with no investigation; `after_analysis` (called after this scope) consumes it
    try:
        with analysis_in_flight():
            yield
    finally:
        _principal.reset(token)


def _thresholds() -> Dict[str, float]:
    try:
        import settings_portable as s
        return {"confidence_threshold": float(getattr(s, "CONFIDENCE_THRESHOLD", 0.65)), "min_retrieval_score": float(getattr(s, "MIN_RETRIEVAL_SCORE", 0.25))}
    except Exception:  # noqa: BLE001
        return {"confidence_threshold": 0.65, "min_retrieval_score": 0.25}


def _runner():
    if _state["runner"] is None:
        from agentruntime.runner import Runner
        from agentruntime.tools import build_default_registry
        _state["runner"] = Runner(build_default_registry())
    return _state["runner"]


def _persist(result: Any, task: Dict[str, Any], request_id: str) -> bool:
    """Keep the run (sanitised, encrypted) for audit and replay. True only if it was stored."""
    try:
        from agentruntime.store import RunStore
        store = RunStore()
        if not _state["store_ready"]:
            store.ensure_schema()
            _state["store_ready"] = True
        store.save_run(result, ticket_ref=request_id, ticket_text=str(task.get("ticket_text") or ""), request_id=request_id)
        return True
    except Exception:  # noqa: BLE001
        log.warning("agent run could not be stored", exc_info=True)
        return False


def _execute(ctx: Any, policy: Any, task: Dict[str, Any], request_id: str) -> Tuple[Any, bool]:
    """Run the frozen planner once. Returns (result, stored)."""
    from agentruntime import release
    planner = release.planner_factory({"request_id": request_id})
    result = _runner().run(ctx, policy, task, planner)
    return result, _persist(result, task, request_id)


def get_queue():
    if _state["queue"] is None:
        from agentruntime.integration.shadow import ShadowQueue

        def run_job(job):
            return _execute(job.ctx, job.policy, job.task, job.request_id)
        queue = ShadowQueue(run_job)
        gate.on_rollback(queue.clear)
        _state["queue"] = queue
    return _state["queue"]


def _tenant_of(req: Any, result: Optional[Dict[str, Any]] = None) -> str:
    cfg = result.get("tenant_config") if isinstance(result, dict) else None
    return str((cfg or {}).get("tenant_id") or getattr(req, "tenant_id", "") or "").strip().lower()


# -- on: before the fallback -------------------------------------------------------------------------------------------------------

def investigate_before_fallback(req: Any, ticket: str, analysis: Dict[str, Any], *, primary_confidence: float, retrieval_quality: float,
                                retrieved_incidents: Any, linked_resolution: Any, linked_playbook: Any) -> Optional[LoopDecision]:
    """Called by the legacy pipeline just before the one-shot fallback. None means: carry on exactly as before."""
    try:
        if gate.global_mode()[0] != "on":
            return None
        tenant = _tenant_of(req)
        mode, _why = gate.effective_mode(tenant)
        if mode != "on":
            return None
        from agentruntime.integration import engagement, reconcile
        probe = {"ticket": ticket, "confidence": {"overall_confidence": primary_confidence, "retrieval_quality": retrieval_quality},
                 "retrieved_incidents": retrieved_incidents or [], "linked_resolution": linked_resolution, "linked_playbook": linked_playbook}
        engaged, _reasons = engagement.should_engage(probe, **_thresholds())
        principal = _principal.get()
        if not engaged or principal is None:
            return None
        from agentruntime import config as runtime_config
        from agentruntime import release
        from agentruntime.context import build_context
        request_id = uuid.uuid4().hex
        ctx = build_context(tenant, request_id, principal, "on")
        policy = release.policy_for("on", budgets=runtime_config.load_budgets())
        run, stored = _execute(ctx, policy, {"ticket_text": ticket}, request_id)
        if not stored:
            log.warning("investigation discarded: it could not be stored for audit, so the deterministic path continues unchanged")
            return None
        _merged, summary = reconcile.reconcile({"analysis": copy.deepcopy(analysis) if isinstance(analysis, dict) else {}}, run)
        decision = LoopDecision(run=run, outcome=summary["outcome"], replaces_fallback=summary["outcome"] in ("merged", "conflict"))
        _pending.set(decision)
        return decision
    except Exception:  # noqa: BLE001 - the existing path always continues
        log.exception("investigation before the fallback failed; the existing path continues unchanged")
        return None


def after_analysis(http_request: Any, req: Any, result: Any) -> Any:
    """`on`: apply the stored investigation to the final response (a reconciled copy). Anything else: `result` itself."""
    try:
        decision = _pending.get()
        _pending.set(None)
        if decision is None or not isinstance(result, dict):
            return result
        if gate.effective_mode(_tenant_of(req, result))[0] != "on":
            return result                                            # switched off while the request was running
        from agentruntime.integration import reconcile
        merged, _summary = reconcile.reconcile(result, decision.run)
        return merged
    except Exception:  # noqa: BLE001
        log.exception("applying the investigation failed; the deterministic response is returned unchanged")
        return result


# -- shadow: after the response has been sent ---------------------------------------------------------------------------------------

def _snapshot(result: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("request_id", "ticket", "tenant_config", "confidence", "retrieved_incidents", "linked_resolution", "linked_playbook")
    return copy.deepcopy({k: result.get(k) for k in keys})


def _shadow_submit(principal: Any, tenant: str, snapshot: Dict[str, Any]) -> None:
    """Runs as a background task, i.e. after the body has been sent. Queues the run; never raises."""
    try:
        mode, _why = gate.effective_mode(tenant)
        if mode != "shadow":
            return
        from agentruntime.integration import engagement
        engaged, reasons = engagement.should_engage(snapshot, **_thresholds())
        if not engaged or principal is None:
            return
        from agentruntime import config as runtime_config
        from agentruntime import release
        from agentruntime.context import build_context
        from agentruntime.integration.shadow import ShadowJob
        request_id = str(snapshot.get("request_id") or uuid.uuid4().hex)
        ctx = build_context(tenant, request_id, principal, "shadow")
        policy = release.policy_for("shadow", budgets=runtime_config.load_budgets())
        get_queue().submit(ShadowJob(ctx=ctx, policy=policy, task={"ticket_text": str(snapshot.get("ticket") or "")}, request_id=request_id, reasons=reasons))
    except Exception:  # noqa: BLE001
        log.exception("shadow run could not be queued")


def shadow_background(http_request: Any, req: Any, result: Any) -> Any:
    """A Starlette BackgroundTask for the route to attach to its response, or None. Cheap and side-effect free: the decision, the context and the queueing all
    happen in the task, after the response has been sent."""
    try:
        if gate.global_mode()[0] == "off" or not isinstance(result, dict):         # the task itself checks that the tenant's mode is shadow
            return None
        from starlette.background import BackgroundTask
        principal = getattr(getattr(http_request, "state", None), "principal", None)
        return BackgroundTask(_shadow_submit, principal, _tenant_of(req, result), _snapshot(result))
    except Exception:  # noqa: BLE001
        log.exception("shadow task could not be prepared")
        return None
