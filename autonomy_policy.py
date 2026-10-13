"""
Autonomy policy (Release 1, Wave 3): the thresholds behind the outcome-ledger tiers and the recurrence cap.

Thresholds live in config (config/autonomy_policy.json, or the file named by AIOPS_AUTONOMY_POLICY_FILE), never
in agent-controlled code paths, and are changed only by humans. Invalid values fall back to the defaults.

  success_tiers   what the outcome history must show for each tier (unchanged from the original ledger gate)
  recurrence_cap  a SEPARATE cap on the tier. A remediation can succeed and still recur within the window, so
                  recurrence is not counted as an ordinary failure:
                    recurrence rate >= moderate_rate  ->  tier is at most auto_execute_watch
                    recurrence rate >= high_rate      ->  hold_for_approval
                  The cap applies only once there are at least `min_outcomes` outcomes, so a rate is meaningful.

In Release 1 the watch tier never permits autonomous execution; it requires a human (see autonomy_decision.py).
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("aiops.autonomy")

DEFAULT_AUTONOMY_POLICY: Dict[str, Any] = {
    "schema_version": "autonomy_policy_v1",
    "success_tiers": {
        "auto_execute": {"min_success_rate": 0.85, "min_outcomes": 5},
        "auto_execute_watch": {"min_success_rate": 0.60, "min_outcomes": 3},
    },
    "recurrence_cap": {"min_outcomes": 3, "moderate_rate": 0.20, "high_rate": 0.40},
}


def policy_file() -> Path:
    return Path(os.getenv("AIOPS_AUTONOMY_POLICY_FILE", "config/autonomy_policy.json"))


def _rate(value: Any, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if 0.0 <= v <= 1.0 else default


def _count(value: Any, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if v >= 1 else default


def resolve_policy(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge a raw policy over the defaults, ignoring invalid or inconsistent values."""
    policy = copy.deepcopy(DEFAULT_AUTONOMY_POLICY)
    raw = raw if isinstance(raw, dict) else {}

    tiers = raw.get("success_tiers") if isinstance(raw.get("success_tiers"), dict) else {}
    for name in ("auto_execute", "auto_execute_watch"):
        spec = tiers.get(name) if isinstance(tiers.get(name), dict) else {}
        policy["success_tiers"][name]["min_success_rate"] = _rate(
            spec.get("min_success_rate"), policy["success_tiers"][name]["min_success_rate"])
        policy["success_tiers"][name]["min_outcomes"] = _count(
            spec.get("min_outcomes"), policy["success_tiers"][name]["min_outcomes"])
    auto, watch = policy["success_tiers"]["auto_execute"], policy["success_tiers"]["auto_execute_watch"]
    if auto["min_success_rate"] < watch["min_success_rate"] or auto["min_outcomes"] < watch["min_outcomes"]:
        logger.warning("autonomy policy: auto_execute must be at least as strict as auto_execute_watch; using defaults")
        policy["success_tiers"] = copy.deepcopy(DEFAULT_AUTONOMY_POLICY["success_tiers"])

    rc_raw = raw.get("recurrence_cap") if isinstance(raw.get("recurrence_cap"), dict) else {}
    rc = policy["recurrence_cap"]
    rc["min_outcomes"] = _count(rc_raw.get("min_outcomes"), rc["min_outcomes"])
    rc["moderate_rate"] = _rate(rc_raw.get("moderate_rate"), rc["moderate_rate"])
    rc["high_rate"] = _rate(rc_raw.get("high_rate"), rc["high_rate"])
    if rc["moderate_rate"] <= 0 or rc["moderate_rate"] > rc["high_rate"]:
        logger.warning("autonomy policy: recurrence thresholds must satisfy 0 < moderate_rate <= high_rate; using defaults")
        policy["recurrence_cap"] = copy.deepcopy(DEFAULT_AUTONOMY_POLICY["recurrence_cap"])
    return policy


def load_policy(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or policy_file()
    try:
        if path.exists():
            return resolve_policy(json.loads(path.read_text(encoding="utf-8-sig")))
    except Exception as exc:
        logger.error("autonomy policy: could not read %s (%s); using defaults", path, exc)
    return resolve_policy(None)
