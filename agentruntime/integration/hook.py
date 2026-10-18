"""
The one entry point the application calls (design 3.2): after a response has been built, decide whether to investigate, and in which mode.

* `off` (the default, and what every doubt resolves to): `after_analysis` returns the very object it was given after one cheap check of the global setting.
  It reads no file, builds nothing and imports nothing else of this package.
* `shadow`: if the engagement rule says so, queue a run and return the response untouched, at once. The run is executed later by the shadow queue (idle-aware, bounded).
* `on`: blocked until R2-3 has passed (gate). When allowed, the run is made inside the request within the run's deadline and reconciled with the deterministic result:
  a supported, verified investigation is merged or recorded as a conflict; anything else leaves the deterministic result as it was. The one-shot external fallback is not
  replaced in this release: it has already run when the hook is reached, and stays the safety net.
Whatever goes wrong here, the request still gets the deterministic response: every failure is logged and the original object is returned.
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Any, Dict, Optional

from agentruntime.integration import gate

log = logging.getLogger(__name__)

_state: Dict[str, Any] = {"queue": None, "runner": None, "store_ready": False}


def track_analysis():
    """Wrap the analysis call: counts it as in flight (so shadow runs wait) unless the loop is off, in which case it does nothing at all."""
    if gate.global_mode()[0] == "off":
        return contextlib.nullcontext()
    from agentruntime.integration.shadow import analysis_in_flight
    return analysis_in_flight()


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


def _persist(result: Any, job_task: Dict[str, Any], request_id: str) -> None:
    """Keep the run (sanitised, encrypted) for audit and replay. Failing to store never affects anything else."""
    try:
        from agentruntime.store import RunStore
        store = RunStore()
        if not _state["store_ready"]:
            store.ensure_schema()
            _state["store_ready"] = True
        store.save_run(result, ticket_ref=request_id, ticket_text=str(job_task.get("ticket_text") or ""), request_id=request_id)
    except Exception:  # noqa: BLE001
        log.warning("agent run could not be stored", exc_info=True)


def _execute(ctx: Any, policy: Any, task: Dict[str, Any], request_id: str):
    from agentruntime import release
    planner = release.planner_factory({"request_id": request_id})
    result = _runner().run(ctx, policy, task, planner)
    _persist(result, task, request_id)
    return result


def get_queue():
    if _state["queue"] is None:
        from agentruntime.integration.shadow import ShadowQueue

        def run_job(job):
            return _execute(job.ctx, job.policy, job.task, job.request_id)
        queue = ShadowQueue(run_job)
        gate.on_rollback(queue.clear)
        _state["queue"] = queue
    return _state["queue"]


def _tenant_of(req: Any, result: Dict[str, Any]) -> str:
    cfg = result.get("tenant_config") if isinstance(result, dict) else None
    return str((cfg or {}).get("tenant_id") or getattr(req, "tenant_id", "") or "").strip().lower()


def after_analysis(http_request: Any, req: Any, result: Any) -> Any:
    try:
        if gate.global_mode()[0] == "off":
            return result                                            # the default path: nothing else happens
        if not isinstance(result, dict):
            return result
        tenant = _tenant_of(req, result)
        mode, why = gate.effective_mode(tenant)
        if mode == "off":
            return result
        from agentruntime.integration import engagement
        engaged, reasons = engagement.should_engage(result, **_thresholds())
        if not engaged:
            return result
        principal = getattr(getattr(http_request, "state", None), "principal", None)
        if principal is None:
            return result                                            # no authenticated principal, no trusted context, no run
        from agentruntime import config as runtime_config
        from agentruntime import release
        from agentruntime.context import build_context
        request_id = str(result.get("request_id") or uuid.uuid4().hex)
        ctx = build_context(tenant, request_id, principal, mode)
        policy = release.policy_for(mode, budgets=runtime_config.load_budgets())
        task = {"ticket_text": str(result.get("ticket") or getattr(req, "ticket", "") or "")}
        if mode == "shadow":
            from agentruntime.integration.shadow import ShadowJob
            get_queue().submit(ShadowJob(ctx=ctx, policy=policy, task=task, request_id=request_id, reasons=reasons))
            return result
        if mode == "on":
            from agentruntime.integration import reconcile
            run = _execute(ctx, policy, task, request_id)
            merged, _summary = reconcile.reconcile(result, run)
            return merged
        return result
    except Exception:  # noqa: BLE001 - the deterministic response always wins
        log.exception("agentic loop hook failed; the deterministic response is returned unchanged")
        return result
