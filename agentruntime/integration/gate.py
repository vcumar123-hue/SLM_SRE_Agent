"""
Mode resolution for the investigation loop: the single place that decides whether it may run for a tenant.

The mode is the most restrictive of:
  * the global setting (`AIOPS_AGENTIC_RCA_LOOP`; missing, empty or invalid is `off`; `shadow`/`on` also need a usable encryption setup, config.resolve_mode);
  * the tenant's recorded deployment decision (`docs/baselines/release2/deployment_decisions.json`): a tenant with no decision is `off`, so merging this code enables
    nothing for anyone (owner decision 5). An entry is valid only with a tenant, a mode of shadow or on, the name of who decided, a date and a reference;
  * for `on`, a recorded R2-3 acceptance result in which every gate G1-G8 passed: until it exists `on` resolves to `off` (not to `shadow`: nobody gets a weaker
    mode than they asked for by surprise);
  * the release freeze: if the frozen runtime, planner, tools, budgets, integration package, route hooks or confidence input no longer match their record, or the owner's
    named approval of the freeze has not been confirmed, the loop resolves to `off`;
  * the kill switch (rollback): the environment variable `AIOPS_AGENTIC_RCA_KILL`, or `rollback()` in this process, forces `off` immediately and cancels queued shadow runs.
Every failure to read or validate any of these is `off`; nothing here raises.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentruntime import config as runtime_config

ROOT = Path(__file__).resolve().parents[2]
RELEASE_DIR = ROOT / "docs" / "baselines" / "release2"
DECISIONS_PATH = RELEASE_DIR / "deployment_decisions.json"
ACCEPTANCE_PATH = RELEASE_DIR / "r2_3_acceptance_result.json"
KILL_ENV = "AIOPS_AGENTIC_RCA_KILL"
ORCHESTRATOR_ENV = "AIOPS_AGENTIC_ORCHESTRATOR_ENABLED"
ORDER = {"off": 0, "shadow": 1, "on": 2}
GATES = tuple(f"G{i}" for i in range(1, 9))
PLACEHOLDERS = {"", "owner", "admin", "test", "tbd", "todo", "none", "n/a", "name", "unknown", "x"}
FREEZE_CACHE_S = 30.0

_lock = threading.Lock()
_killed: Dict[str, Any] = {"on": False, "reason": ""}
_on_rollback: List[Callable[[], None]] = []
_freeze_cache: Dict[str, Any] = {"at": 0.0, "problems": None}


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def rollback(reason: str = "rollback requested") -> None:
    """The in-process rollback switch: the loop is `off` for every tenant from now on, and queued shadow runs are dropped."""
    with _lock:
        _killed.update(on=True, reason=reason)
        callbacks = list(_on_rollback)
    for callback in callbacks:
        try:
            callback()
        except Exception:  # noqa: BLE001 - a failing callback must not stop the rollback
            pass


def clear_rollback() -> None:
    """Lift an in-process rollback (an operator action; the environment kill switch is separate and must be removed by whoever set it)."""
    with _lock:
        _killed.update(on=False, reason="")


def on_rollback(callback: Callable[[], None]) -> None:
    with _lock:
        _on_rollback.append(callback)


def killed() -> Tuple[bool, str]:
    if _truthy(os.getenv(KILL_ENV)):
        return True, f"{KILL_ENV} is set"
    with _lock:
        return bool(_killed["on"]), str(_killed["reason"])


def read_decisions(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """{tenant_id: entry} for the valid entries; anything unreadable or invalid contributes nothing."""
    p = Path(path) if path else DECISIONS_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    items = raw.get("decisions", []) if isinstance(raw, dict) else raw
    out: Dict[str, Dict[str, Any]] = {}
    for e in items if isinstance(items, list) else []:
        if not isinstance(e, dict):
            continue
        tenant, mode = str(e.get("tenant_id") or "").strip().lower(), str(e.get("mode") or "").strip().lower()
        by, date, ref = str(e.get("decided_by") or "").strip(), str(e.get("date") or "").strip(), str(e.get("reference") or "").strip()
        if tenant and mode in ("shadow", "on") and by.lower() not in PLACEHOLDERS and date and ref:
            out[tenant] = {"tenant_id": tenant, "mode": mode, "decided_by": by, "date": date, "reference": ref}
    return out


def r2_3_passed(path: Optional[Path] = None) -> bool:
    """True only for a recorded acceptance result with every gate passed, an acceptance-set hash and a named decider."""
    p = Path(path) if path else ACCEPTANCE_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    gates = raw.get("gates") if isinstance(raw, dict) else None
    return (isinstance(raw, dict) and raw.get("passed") is True and isinstance(gates, dict) and all(gates.get(g) is True for g in GATES)
            and bool(str(raw.get("acceptance_set_hash") or "").strip()) and str(raw.get("decided_by") or "").strip().lower() not in PLACEHOLDERS)


def freeze_problems(force: bool = False) -> List[str]:
    """Differences between the code and the release freeze (cached briefly: hashing the runtime on every request would be wasteful)."""
    now = time.monotonic()
    with _lock:
        if not force and _freeze_cache["problems"] is not None and now - _freeze_cache["at"] < FREEZE_CACHE_S:
            return list(_freeze_cache["problems"])
    try:
        from agentruntime import release
        problems = release.problems()
    except Exception as exc:  # noqa: BLE001 - an unreadable record is a problem, not a pass
        problems = [f"the release freeze could not be verified: {exc}"]
    with _lock:
        _freeze_cache.update(at=now, problems=list(problems))
    return problems


def reset_cache() -> None:
    with _lock:
        _freeze_cache.update(at=0.0, problems=None)


def global_mode(env_value: Optional[str] = None) -> Tuple[str, str]:
    """The environment's mode without any tenant (used to decide cheaply whether anything needs doing at all)."""
    dead, why = killed()
    if dead:
        return "off", f"kill switch: {why}"
    try:
        return runtime_config.resolve_mode(env_value)
    except Exception as exc:  # noqa: BLE001
        return "off", f"mode could not be resolved: {exc}"


def effective_mode(tenant_id: Optional[str], *, env_value: Optional[str] = None, decisions_path: Optional[Path] = None,
                   acceptance_path: Optional[Path] = None, check_freeze: bool = True) -> Tuple[str, str]:
    """(mode, reason) for a tenant. Never raises; every doubt is `off`."""
    try:
        mode, why = global_mode(env_value)
        if mode == "off":
            return "off", why
        tenant = str(tenant_id or "").strip().lower()
        decision = read_decisions(decisions_path).get(tenant)
        if decision is None:
            return "off", f"no recorded deployment decision for tenant {tenant or '(none)'}"
        allowed = decision["mode"] if ORDER[decision["mode"]] <= ORDER[mode] else mode
        if allowed == "on" and not r2_3_passed(acceptance_path):
            return "off", "on is blocked until R2-3 has passed (no recorded acceptance result with every gate passed)"
        if allowed == "on" and _truthy(os.getenv(ORCHESTRATOR_ENV)):
            return "off", "the flag-driven agentic orchestrator path has its own fallback and is not integrated, so on stays off while it is enabled"
        if check_freeze:
            problems = freeze_problems()
            if problems:
                return "off", "the release freeze does not match the code: " + problems[0]
        return allowed, f"recorded decision by {decision['decided_by']} ({decision['reference']})"
    except Exception as exc:  # noqa: BLE001
        return "off", f"mode could not be resolved: {exc}"
