"""
FastAPI router — Drift management.

Mounts under the prefix configured in app.py (e.g. "").
Endpoints live at /drift/...

Dependencies:
  tools.snapshot_engine  — SnapshotEngine
  adapters.servicenow_cmdb — get_adapter
"""
from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from tools.snapshot_engine import SnapshotEngine
from adapters.servicenow_cmdb import get_adapter  # noqa: F401  (kept for future CMDB calls)

router = APIRouter(tags=["drift"])

_VALID_MODES = {"lab", "cmdb", "dry"}
_LAB_CONTAINERS = {"linux-lab-01", "linux-lab-02", "network-lab-01"}

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class SnapshotRequest(BaseModel):
    ci_name: str
    mode: str = "dry"
    container: Optional[str] = None
    notes: str = ""

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in _VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got '{v}'")
        return v


class RemediateRequest(BaseModel):
    snapshot_id: str
    playbook_name: str
    changes: List[Dict[str, str]]


class VerifyRequest(BaseModel):
    snapshot_id: str


class DestroyRequest(BaseModel):
    snapshot_id: str
    write_audit: bool = True


class RunCycleRequest(BaseModel):
    ci_name: str
    mode: str = "dry"
    container: Optional[str] = None
    playbook_name: str
    auto_destroy: bool = True

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in _VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got '{v}'")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_container(mode: str, ci_name: str, container: Optional[str]) -> Optional[str]:
    """For lab mode, resolve the container name with validation."""
    if mode != "lab":
        return container
    resolved = container if container is not None else ci_name
    if resolved not in _LAB_CONTAINERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"container '{resolved}' is not a recognised lab container. "
                f"Valid options: {sorted(_LAB_CONTAINERS)}"
            ),
        )
    return resolved


def _wrap_subprocess_error(exc: Exception) -> HTTPException:
    msg = str(exc)
    if "docker" in msg.lower() or "subprocess" in msg.lower() or "FileNotFoundError" in type(exc).__name__:
        return HTTPException(status_code=503, detail=f"Docker/subprocess not available: {msg}")
    return HTTPException(status_code=500, detail=msg)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/drift/snapshots")
def list_snapshots() -> Dict[str, Any]:
    """Return all active (non-destroyed) snapshots."""
    try:
        engine = SnapshotEngine()
        snapshots = engine.list_snapshots()
    except Exception as exc:
        raise _wrap_subprocess_error(exc) from exc
    return {"status": "ok", "snapshots": snapshots, "count": len(snapshots)}


@router.get("/drift/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str) -> Dict[str, Any]:
    """Return a specific snapshot by ID."""
    try:
        engine = SnapshotEngine()
        snapshot = engine.get_snapshot(snapshot_id)
    except Exception as exc:
        raise _wrap_subprocess_error(exc) from exc
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Snapshot not found: {snapshot_id}")
    return snapshot


@router.post("/drift/snapshot")
def capture_snapshot(req: SnapshotRequest) -> Dict[str, Any]:
    """Capture a new drift snapshot for a CI."""
    container = _resolve_container(req.mode, req.ci_name, req.container)
    try:
        engine = SnapshotEngine()
        snapshot = engine.capture(
            ci_name=req.ci_name,
            mode=req.mode,
            container=container,
            notes=req.notes,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=503, detail=f"Docker/subprocess not available: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return snapshot


@router.post("/drift/remediate")
def remediate_snapshot(req: RemediateRequest) -> Dict[str, Any]:
    """Apply a remediation playbook to a snapshot."""
    _validate_changes(req.changes)
    try:
        engine = SnapshotEngine()
        updated = engine.apply_remediation(
            snapshot_id=req.snapshot_id,
            playbook_name=req.playbook_name,
            changes=req.changes,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=503, detail=f"Docker/subprocess not available: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Snapshot not found: {req.snapshot_id}")
    return updated


def _validate_changes(changes: List[Dict[str, str]]) -> None:
    valid_types = {"restart_service", "update_config", "install_package", "run_command"}
    for i, change in enumerate(changes):
        if "type" not in change:
            raise HTTPException(status_code=422, detail=f"changes[{i}] missing 'type' field")
        if change["type"] not in valid_types:
            raise HTTPException(
                status_code=422,
                detail=f"changes[{i}].type '{change['type']}' invalid; must be one of {sorted(valid_types)}",
            )
        if "target" not in change:
            raise HTTPException(status_code=422, detail=f"changes[{i}] missing 'target' field")


@router.post("/drift/verify")
def verify_snapshot(req: VerifyRequest) -> Dict[str, Any]:
    """Verify a snapshot post-remediation."""
    try:
        engine = SnapshotEngine()
        result = engine.verify(snapshot_id=req.snapshot_id)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=503, detail=f"Docker/subprocess not available: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"Snapshot not found: {req.snapshot_id}")
    # result is expected to be a dict with at least {"verify_result": "pass"|"fail", "snapshot": {...}}
    return {
        "status": "ok",
        "verify_result": result.get("verify_result", "fail"),
        "snapshot": result.get("snapshot", result),
    }


@router.post("/drift/destroy")
def destroy_snapshot(req: DestroyRequest) -> Dict[str, Any]:
    """Destroy a snapshot and optionally write an audit record."""
    try:
        engine = SnapshotEngine()
        audit = engine.destroy(snapshot_id=req.snapshot_id, write_audit=req.write_audit)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=503, detail=f"Docker/subprocess not available: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if audit is None:
        raise HTTPException(status_code=404, detail=f"Snapshot not found: {req.snapshot_id}")
    return audit


@router.get("/drift/audit")
def list_audit(
    limit: int = Query(50, ge=1, le=1000),
    ci_name: str = Query(""),
) -> Dict[str, Any]:
    """List audit records, optionally filtered by ci_name."""
    try:
        engine = SnapshotEngine()
        records = engine.list_audit(limit=limit, ci_name=ci_name if ci_name else None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "audit": records, "count": len(records)}


@router.get("/drift/audit/{snapshot_id}")
def get_audit_record(snapshot_id: str) -> Dict[str, Any]:
    """Retrieve a specific audit record by snapshot_id."""
    try:
        engine = SnapshotEngine()
        record = engine.get_audit_record(snapshot_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"Audit record not found for snapshot: {snapshot_id}")
    return record


@router.post("/drift/run_cycle")
def run_cycle(req: RunCycleRequest) -> Dict[str, Any]:
    """
    Run a full capture → remediate → verify → (optionally) destroy cycle.

    Returns a structured result with per-step outcomes so callers can inspect
    exactly where a cycle failed.
    """
    container = _resolve_container(req.mode, req.ci_name, req.container)
    steps: List[Dict[str, Any]] = []
    snapshot_id: Optional[str] = None
    audit: Optional[Dict[str, Any]] = None
    cycle_result = "fail"

    # ---- Step 1: capture ----
    try:
        engine = SnapshotEngine()
        snapshot = engine.capture(
            ci_name=req.ci_name,
            mode=req.mode,
            container=container,
            notes="run_cycle auto-capture",
        )
        snapshot_id = snapshot.get("snapshot_id") or snapshot.get("id")
        steps.append({"step": "capture", "status": "ok", "snapshot_id": snapshot_id})
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        steps.append({"step": "capture", "status": "error", "detail": f"Docker/subprocess not available: {exc}"})
        raise HTTPException(
            status_code=503,
            detail={"cycle_result": "fail", "steps": steps, "error": str(exc)},
        ) from exc
    except Exception as exc:
        steps.append({"step": "capture", "status": "error", "detail": str(exc)})
        raise HTTPException(
            status_code=500,
            detail={"cycle_result": "fail", "steps": steps, "error": str(exc)},
        ) from exc

    # ---- Step 2: remediate (no changes — playbook only) ----
    try:
        engine = SnapshotEngine()
        engine.apply_remediation(
            snapshot_id=snapshot_id,
            playbook_name=req.playbook_name,
            changes=[],
        )
        steps.append({"step": "remediate", "status": "ok", "playbook": req.playbook_name})
    except Exception as exc:
        steps.append({"step": "remediate", "status": "error", "detail": str(exc)})
        # Continue to verify even if remediation errors, to get partial results

    # ---- Step 3: verify ----
    verify_result = "fail"
    try:
        engine = SnapshotEngine()
        vr = engine.verify(snapshot_id=snapshot_id)
        verify_result = vr.get("verify_result", "fail") if vr else "fail"
        steps.append({"step": "verify", "status": "ok", "verify_result": verify_result})
        if verify_result == "pass":
            cycle_result = "pass"
    except Exception as exc:
        steps.append({"step": "verify", "status": "error", "detail": str(exc)})

    # ---- Step 4: destroy ----
    if req.auto_destroy:
        try:
            engine = SnapshotEngine()
            audit = engine.destroy(snapshot_id=snapshot_id, write_audit=True)
            steps.append({"step": "destroy", "status": "ok"})
        except Exception as exc:
            steps.append({"step": "destroy", "status": "error", "detail": str(exc)})

    return {
        "status": "ok",
        "cycle_result": cycle_result,
        "snapshot_id": snapshot_id,
        "audit": audit,
        "steps": steps,
    }
