"""
Read-only visibility into the Release 2 investigation loop's stored runs (design: operator-visible shadow inspection, section 22.6).

One endpoint, tenant-scoped and authenticated, correlated by the `/analyze` response's own `request_id`:

    GET /agentic/runs/by-request/{request_id}?tenant_id=<tenant>

There is deliberately no "list all runs" endpoint and no listing by tenant alone: a caller must already hold the specific request id
the `/analyze` response gave them. The response is always one of:

  * `{"state": "disabled"}`               no run is stored for this request AND the loop is currently off for this tenant
  * `{"state": "pending", "mode": ...}`    no run is stored yet, but the loop is on/shadow for this tenant: a background run may still
                                           be running (nothing distinguishes "about to finish" from "never engaged"; the caller polls
                                           for a bounded time and then stops, which is exactly what a run that was never queued looks
                                           like too)
  * `{"state": "complete", ...}`          a stored run was found (see `agentruntime.store.summarize_run`), REGARDLESS of the tenant's
                                           current mode: once an investigation ran and was stored, turning shadow off again must not
                                           make it disappear from the record an operator already saw
  * `{"state": "unavailable"}`            the lookup itself failed (database or store error); never raised as a 500 into the console

`response_applied` is never inferred from the tenant's current mode: it is only ever true once `on`-mode runs record their own
reconciliation outcome in the stored record, which they do not yet do (the integration has no field for it and `on` cannot run until
R2-3 passes). Until that exists this endpoint reports `response_applied: false` for every run, with `response_impact` saying why:
`"none_shadow"` for a shadow run, `"not_recorded"` for an `on` run (so a future one is never mistaken for "no impact"), `"unknown"`
otherwise.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from security import principal_from_request

logger = logging.getLogger("aiops.agentic_runs")

router = APIRouter(tags=["agentic_runs"])

_TENANT_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

_state: Dict[str, Any] = {"store": None}


def _get_store():
    """Lazily construct the one RunStore (encryption/connection setup happens once). A test may replace this function."""
    if _state["store"] is None:
        from agentruntime.store import RunStore
        _state["store"] = RunStore()
    return _state["store"]


def _response_impact(mode: str) -> str:
    if mode == "shadow":
        return "none_shadow"
    if mode == "on":
        return "not_recorded"            # the stored record carries no reconciliation outcome yet; never claim "no impact" for it
    return "unknown"


@router.get("/agentic/runs/by-request/{request_id}")
def get_agentic_run_by_request(request_id: str, tenant_id: str, request: Request) -> Dict[str, Any]:
    tenant = str(tenant_id or "").strip().lower()
    req_id = str(request_id or "").strip()
    if not _TENANT_RE.fullmatch(tenant):
        raise HTTPException(status_code=400, detail="tenant_id is required and must be a valid tenant identifier.")
    if not _REQUEST_ID_RE.fullmatch(req_id):
        raise HTTPException(status_code=400, detail="request_id is malformed.")

    principal = principal_from_request(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Valid Bearer token required.")
    allows = getattr(principal, "allows_tenant", None)
    if not callable(allows) or not allows(tenant):
        raise HTTPException(status_code=403, detail=f"Not authorized for tenant '{tenant}'.")

    try:
        from agentruntime.store import summarize_run
        store = _get_store()
        run = store.load_run_by_request(tenant, req_id)
    except Exception:  # noqa: BLE001 - a lookup failure is reported as unavailable, never a 500 into the console
        logger.exception("agentic run lookup failed for tenant=%s request_id=%s", tenant, req_id)
        return {"state": "unavailable"}

    if run is not None:
        summary = summarize_run(run)
        return {"state": "complete", **summary, "response_applied": False, "response_impact": _response_impact(run.mode)}

    try:
        from agentruntime.integration import gate
        mode, _why = gate.effective_mode(tenant)
    except Exception:  # noqa: BLE001 - if the mode cannot even be read, treat it as off (fail closed on visibility, not on safety)
        mode = "off"
    if mode == "off":
        return {"state": "disabled"}
    return {"state": "pending", "mode": mode}
