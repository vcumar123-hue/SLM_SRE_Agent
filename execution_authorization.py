"""
Server-side execution authorization (Release 1, W1).

One decision function that every execution request must pass. Nothing here trusts the request body:
the execution class is derived from the authenticated principal, the queue entry's server-created
intent/approval, the current plan hash, a fresh Safety Gate run, and (for autonomous) the registry
mapping and outcome-ledger tier.

Execution classes
  simulation      queued or approved plans; no side effects
  human_approved  live; entry approved by a human (role >= l3), executor human role >= l3
  autonomous      live; entry carries a server-created autonomous intent, executor is a service identity,
                  registry mapping is enabled, ledger tier is auto_execute (watch tier requires a human)
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

import automation_engine as ae

GATE_RUNS_FILE = ae.DATA_DIR / "automation_gate_runs.json"
_GATE_RUNS_KEEP = 1000

LIVE_EXECUTE_MIN_ROLE = "l3"


# ── Stable warning identities ────────────────────────────────────────────────

# Only fields that describe the underlying condition, never human-readable text or timestamps.
_STABLE_EVIDENCE_KEYS = ("status", "severity", "source_system", "evidence_type", "adapter_mode",
                         "scope", "blast_radius_level", "simulation_status")


def _normalized_status(value: Any) -> str:
    token = str(value or "").strip().lower()
    return "unknown" if token in {"unknown", "missing", "not_available"} else "warn"


def warning_identities(gate_result: Dict[str, Any]) -> List[Dict[str, str]]:
    """Stable identities of the warnings in a gate result: check id, warning code, evidence fingerprint.

    Wording can change without the condition changing, so identity never depends on summary text.
    """
    checks = (gate_result or {}).get("checks") or {}
    names = list((gate_result or {}).get("warning_checks") or [])
    if not names:
        names = [n for n, d in checks.items()
                 if str((d or {}).get("status", "")).lower() in {"warn", "warning", "unknown"}]
    out: List[Dict[str, str]] = []
    for name in sorted(set(str(n) for n in names)):
        detail = checks.get(name) or {}
        code = f"{name}:stale" if detail.get("ttl_warning") else f"{name}:{_normalized_status(detail.get('status'))}"
        material = {k: detail.get(k) for k in _STABLE_EVIDENCE_KEYS}
        material["stale"] = bool(detail.get("ttl_warning"))
        fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        out.append({"check_id": name, "warning_code": code, "evidence_fingerprint": fingerprint})
    return out


def compare_warnings(acknowledged: List[Dict[str, Any]], current: List[Dict[str, Any]]) -> List[str]:
    """Return human-readable problems for warnings that were not acknowledged at approval (empty = all covered)."""
    ack_exact = {(w.get("check_id"), w.get("warning_code"), w.get("evidence_fingerprint")) for w in acknowledged or []}
    ack_code = {(w.get("check_id"), w.get("warning_code")) for w in acknowledged or []}
    problems: List[str] = []
    for w in current or []:
        key = (w.get("check_id"), w.get("warning_code"), w.get("evidence_fingerprint"))
        if key in ack_exact:
            continue
        if (w.get("check_id"), w.get("warning_code")) in ack_code:
            problems.append(f"evidence for warning '{w.get('warning_code')}' changed since approval")
        else:
            problems.append(f"new warning '{w.get('warning_code')}' since approval")
    return problems


# ── Gate-run store ───────────────────────────────────────────────────────────

def record_gate_run(
    entry: Dict[str, Any],
    gate_result: Dict[str, Any],
    params: Dict[str, Any],
    principal_id: str = "",
) -> Dict[str, Any]:
    """Persist a Safety Gate run bound to the queue entry, plan hash, target and time."""
    run = {
        "gate_run_id": f"gr_{uuid.uuid4().hex[:12]}",
        "queue_id": (entry or {}).get("queue_id", ""),
        "plan_hash": ae.compute_plan_hash(entry) if entry else "",
        "classification": params.get("classification", ""),
        "playbook": params.get("playbook", ""),
        "target_host": params.get("target_host", ""),
        "risk_tier": params.get("risk_tier", ""),
        "execution_mode": params.get("execution_mode", ""),
        "gate_status": str(gate_result.get("gate_status", "error")).lower(),
        "safe_to_approve": bool(gate_result.get("safe_to_approve", False)),
        "safe_to_execute_live": bool(gate_result.get("safe_to_execute_live", False)),
        "blocking_checks": list(gate_result.get("blocking_checks", []) or []),
        "warning_checks": list(gate_result.get("warning_checks", []) or []),
        "warnings": warning_identities(gate_result),
        "checked_at": gate_result.get("checked_at") or ae._utc_now_iso(),
        "principal": principal_id,
    }
    with ae._queue_lock("gate_run", run["queue_id"]):
        runs = ae._safe_read_json(GATE_RUNS_FILE, default=[])
        if not isinstance(runs, list):
            runs = []
        runs.append(run)
        ae._safe_write_json(GATE_RUNS_FILE, runs[-_GATE_RUNS_KEEP:])
    return run


def latest_gate_run(queue_id: str) -> Optional[Dict[str, Any]]:
    runs = ae._safe_read_json(GATE_RUNS_FILE, default=[])
    if not isinstance(runs, list):
        return None
    for run in reversed(runs):
        if isinstance(run, dict) and run.get("queue_id") == queue_id:
            return run
    return None


# ── Decision ─────────────────────────────────────────────────────────────────

@dataclass
class ExecutionAuthorization:
    allowed: bool
    queue_id: str
    mode: str
    execution_class: str = ""
    principal_id: str = ""
    plan_hash: str = ""
    gate_run_id: str = ""
    gate_status: str = ""
    override: bool = False
    override_reason: str = ""
    approval_override_reason: str = ""
    acknowledged_warnings: List[Dict[str, Any]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    decided_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _deny(auth: ExecutionAuthorization, reason: str) -> ExecutionAuthorization:
    auth.allowed = False
    auth.reasons.append(reason)
    return auth


def authorize_execution(
    entry: Dict[str, Any],
    requested_mode: str,
    principal: Any,
    *,
    run_gate: Callable[[Dict[str, Any]], Dict[str, Any]],
    ledger_gate: Callable[[str, str], Dict[str, Any]],
    autonomous_mapping_allowed: Callable[[str], bool],
    override: bool = False,
    override_reason: str = "",
) -> ExecutionAuthorization:
    mode = str(requested_mode or "").strip().lower()
    queue_id = str((entry or {}).get("queue_id", ""))
    auth = ExecutionAuthorization(
        allowed=True,
        queue_id=queue_id,
        mode=mode,
        principal_id=getattr(principal, "id", "") or "",
        plan_hash=ae.compute_plan_hash(entry) if entry else "",
        decided_at=ae._utc_now_iso(),
    )

    if mode not in ae._VALID_EXECUTION_MODES:
        return _deny(auth, f"unsupported execution_mode '{mode}'")
    if principal is None:
        return _deny(auth, "unauthenticated request")
    if not principal.allows_tenant((entry or {}).get("tenant_id", "")):
        return _deny(auth, "principal is not permitted for this tenant")

    status = (entry or {}).get("status")

    if mode == "simulation":
        auth.execution_class = "simulation"
        if status not in {"queued", "approved"}:
            return _deny(auth, f"entry status '{status}' is not executable")
        return auth

    # ── live ────────────────────────────────────────────────────────────────
    intent = (entry or {}).get("intent") or {}
    autonomous_entry = intent.get("kind") == "autonomous"
    auth.execution_class = "autonomous" if autonomous_entry else "human_approved"
    cls = (entry or {}).get("classification", "")
    tenant = (entry or {}).get("tenant_id", "") or "default"

    if autonomous_entry:
        if getattr(principal, "kind", "") != "service":
            return _deny(auth, "autonomous plans may be executed only by a service identity")
        if status not in {"queued", "approved"}:
            return _deny(auth, f"entry status '{status}' is not executable")
        if intent.get("plan_hash") != auth.plan_hash:
            return _deny(auth, "plan changed since the autonomous intent was created")
        if not autonomous_mapping_allowed(cls):
            return _deny(auth, "classification is not enabled for autonomous execution in the registry")
        try:
            tier = str((ledger_gate(cls, tenant) or {}).get("tier", "hold_for_approval"))
        except Exception:
            tier = "hold_for_approval"
        if tier != "auto_execute":
            return _deny(auth, f"outcome ledger tier '{tier}' does not permit autonomous execution")
    else:
        if getattr(principal, "kind", "") != "human":
            return _deny(auth, "human-approved plans may be executed only by a human principal")
        if not principal.has_role(LIVE_EXECUTE_MIN_ROLE):
            return _deny(auth, f"live execution requires role {LIVE_EXECUTE_MIN_ROLE} or above")
        if status != "approved":
            return _deny(auth, "live execution requires an approved queue entry")
        approval = (entry or {}).get("approval") or {}
        if not approval.get("approved_by"):
            return _deny(auth, "no approval record on the queue entry")
        if approval.get("plan_hash") != auth.plan_hash:
            return _deny(auth, "plan changed since approval")

    # Fresh Safety Gate run immediately before live execution.
    try:
        run = run_gate(entry) or {}
    except ae.QueueBusyError:
        raise  # infrastructure contention is a 503, not a policy denial
    except Exception as exc:
        return _deny(auth, f"safety gate could not be evaluated: {type(exc).__name__}")
    auth.gate_run_id = str(run.get("gate_run_id", ""))
    auth.gate_status = str(run.get("gate_status", "error")).lower()

    if auth.gate_status == "passed":
        pass
    elif auth.gate_status == "warning" and not autonomous_entry:
        approval = (entry or {}).get("approval") or {}
        approval_reason = str(approval.get("override_reason") or "").strip()
        if not approval.get("override_requested") or not approval_reason:
            return _deny(auth, "safety gate returned warnings but the approval does not record a warning override with a reason")
        if not override or not str(override_reason or "").strip():
            return _deny(auth, "safety gate returned warnings; execution requires an explicit override confirmation with a reason")
        gate_params = approval.get("gate_params") or {}
        if str(run.get("target_host", "")) != str(gate_params.get("target_host", run.get("target_host", ""))):
            return _deny(auth, "target changed since approval")
        if run.get("plan_hash") and run.get("plan_hash") != approval.get("plan_hash"):
            return _deny(auth, "plan changed since approval")
        problems = compare_warnings(approval.get("acknowledged_warnings") or [], run.get("warnings") or [])
        if problems:
            return _deny(auth, "; ".join(problems))
        auth.override = True
        auth.override_reason = str(override_reason).strip()
        auth.approval_override_reason = approval_reason
        auth.acknowledged_warnings = list(approval.get("acknowledged_warnings") or [])
    else:
        return _deny(auth, f"safety gate status '{auth.gate_status}' does not permit live execution")

    return auth
