"""
Thin public module for recording and reading incident resolution outcomes.

Call record_outcome() from:
  - create_resolution()           (manual / human-approved path)
  - /automation/execution_feedback (autonomous path — skip simulation runs)

Primary store: PostgreSQL resolution_outcomes table.
Fallback: data/resolution_outcomes.json (append-only JSONL).

Public read API:
  get_outcomes(classification, tenant_id, limit) — PG-first, JSON fallback.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FALLBACK_PATH = Path(os.getenv("RESOLUTION_OUTCOMES_FILE", "data/resolution_outcomes.json"))
_RECURRENCE_WINDOW_HOURS_DEFAULT = 72


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Recurrence detection ───────────────────────────────────────────────────────

def _check_recurrence(incident_id: str, classification: str, tenant_id: str, window_hours: int) -> bool:
    """Return True if this incident_id OR this classification+tenant already has a resolved
    outcome within window_hours. Uses get_outcomes() for PG-first/JSON-fallback coverage."""
    if not incident_id and not classification:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    def _within_window(row: Dict[str, Any]) -> bool:
        resolved_at = row.get("resolved_at")
        if not resolved_at:
            return False
        try:
            ts = datetime.fromisoformat(str(resolved_at).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts >= cutoff
        except Exception:
            return False

    try:
        prior = get_outcomes(classification, tenant_id=tenant_id, limit=100)
        for row in prior:
            # Same incident resolving again within the window
            if incident_id and row.get("incident_id") == incident_id and _within_window(row):
                return True
            # Same classification + tenant within the window (different ticket, same problem)
            if classification and _within_window(row):
                return True
    except Exception:
        pass
    return False


# ── Write path ────────────────────────────────────────────────────────────────

def record_outcome(
    *,
    incident_id: str,
    classification: str = "",
    resolution_id: str = "",
    playbook_id: str = "",
    tenant_id: str = "default",
    execution_mode: str = "manual",          # manual | human_approved | autonomous
    ai_recommendation_used: str = "no",      # yes | partial | no
    outcome_status: str = "resolved",        # resolved | partial | failed | recurred
    opened_at: Optional[str] = None,
    resolved_at: Optional[str] = None,
    resolution_minutes: Optional[float] = None,
    recurrence_window_hours: int = _RECURRENCE_WINDOW_HOURS_DEFAULT,
    recurrence_detected: bool = False,
    confirmed_by: str = "",
    confirmed_at: Optional[str] = None,
    notes: str = "",
    outcome_id: Optional[str] = None,
) -> str:
    """Record one resolution outcome. Returns the outcome_id used.

    Simulation runs (execution_mode='simulation') are skipped — they are not
    real outcomes and must not pollute future intelligence.
    """
    if execution_mode == "simulation":
        logger.debug("resolution_outcome: skipping simulation run for incident=%s", incident_id)
        return ""

    oid = outcome_id or f"ro_{uuid.uuid4().hex}"
    resolved_at = resolved_at or _now_iso()

    # Detect recurrence unless caller already set it.
    # Run regardless of whether incident_id is present — classification+tenant is sufficient.
    if not recurrence_detected:
        recurrence_detected = _check_recurrence(
            incident_id, classification, tenant_id, recurrence_window_hours
        )

    outcome: Dict[str, Any] = {
        "outcome_id":              oid,
        "incident_id":             incident_id,
        "classification":          classification,
        "resolution_id":           resolution_id,
        "playbook_id":             playbook_id,
        "tenant_id":               tenant_id,
        "execution_mode":          execution_mode,
        "ai_recommendation_used":  ai_recommendation_used,
        "outcome_status":          outcome_status,
        "opened_at":               opened_at,
        "resolved_at":             resolved_at,
        "resolution_minutes":      resolution_minutes,
        "recurrence_window_hours": recurrence_window_hours,
        "recurrence_detected":     recurrence_detected,
        "confirmed_by":            confirmed_by,
        "confirmed_at":            confirmed_at,
        "notes":                   notes,
    }

    _write_pg(outcome)
    _write_json(outcome)

    logger.debug(
        "resolution_outcome recorded: id=%s incident=%s mode=%s status=%s recurrence=%s",
        oid, incident_id, execution_mode, outcome_status, recurrence_detected,
    )
    return oid


def _write_pg(outcome: Dict[str, Any]) -> None:
    try:
        import postgres_store as pg
        pg.ensure_resolution_outcomes_schema()
        pg.insert_resolution_outcome(outcome)
    except Exception as exc:
        logger.warning("resolution_outcome: PostgreSQL write failed (%s) — fallback only", exc)


def _write_json(outcome: Dict[str, Any]) -> None:
    try:
        _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _FALLBACK_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(outcome, default=str) + "\n")
    except Exception as exc:
        logger.warning("resolution_outcome: JSON fallback write failed: %s", exc)


# ── Read path ─────────────────────────────────────────────────────────────────

def _read_json_outcomes(
    classification: str = "",
    tenant_id: str = "",
) -> List[Dict[str, Any]]:
    """Read all matching rows from the JSON fallback file."""
    if not _FALLBACK_PATH.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in _FALLBACK_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if classification and row.get("classification") != classification:
                continue
            if tenant_id and row.get("tenant_id") not in (tenant_id, "default"):
                continue
            rows.append(row)
    except Exception as exc:
        logger.warning("resolution_outcome: JSON read failed: %s", exc)
    return rows


def autonomous_gate(
    classification: str,
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """Return the autonomous execution gate decision for a classification+tenant pair.

    Tiers:
      auto_execute       — success_rate >= 0.85 AND total_outcomes >= 5
      auto_execute_watch — success_rate >= 0.60 AND total_outcomes >= 3
      hold_for_approval  — everything else, including no history

    Always returns a dict; never raises.
    """
    try:
        summary = summarize_outcomes(classification, tenant_id=tenant_id)
    except Exception:
        summary = None

    if summary is None:
        return {
            "tier":              "hold_for_approval",
            "success_rate":      None,
            "total_outcomes":    0,
            "confidence_signal": None,
            "reason":            "No outcome history for this classification - autonomous execution requires at least 3 confirmed resolved outcomes.",
        }

    total   = summary["total_outcomes"]
    success = summary["success_rate"]
    signal  = summary["confidence_signal"]

    if success >= 0.85 and total >= 5:
        tier   = "auto_execute"
        reason = f"Strong outcome history: {total} outcomes, {success:.0%} success rate."
    elif success >= 0.60 and total >= 3:
        tier   = "auto_execute_watch"
        reason = f"Sufficient but not fully reliable history: {total} outcomes, {success:.0%} success rate. Monitor closely."
    else:
        parts = []
        if total < 3:
            parts.append(f"only {total} outcome(s) recorded (minimum 3 required)")
        if success < 0.60:
            parts.append(f"success rate {success:.0%} below 60% threshold")
        reason = f"Hold for human approval - {'; '.join(parts)}."
        tier   = "hold_for_approval"

    return {
        "tier":              tier,
        "success_rate":      success,
        "total_outcomes":    total,
        "confidence_signal": signal,
        "reason":            reason,
    }


def summarize_outcomes(
    classification: str,
    tenant_id: str = "default",
) -> Optional[Dict[str, Any]]:
    """Return a structured intelligence summary for a classification+tenant pair.

    Excludes simulation runs defensively. Returns None when no usable outcomes exist.
    Rates are decimals (0.0–1.0); format as percentages in the UI layer.
    """
    rows = get_outcomes(classification, tenant_id=tenant_id, limit=200)
    # Defensive filter: exclude simulation runs even if any slipped through
    rows = [r for r in rows if r.get("execution_mode") != "simulation"]
    if not rows:
        return None

    total = len(rows)
    resolved_count = sum(1 for r in rows if r.get("outcome_status") == "resolved")
    recurred_count = sum(1 for r in rows if r.get("recurrence_detected"))
    ai_used_count  = sum(1 for r in rows if r.get("ai_recommendation_used") in ("yes", "partial"))

    minutes = [r["resolution_minutes"] for r in rows if r.get("resolution_minutes") is not None]
    avg_minutes = sum(minutes) / len(minutes) if minutes else None

    _mode_priority = {"autonomous": 3, "human_approved": 2, "manual": 1}
    mode_counts: Dict[str, int] = {}
    for r in rows:
        m = r.get("execution_mode") or "manual"
        mode_counts[m] = mode_counts.get(m, 0) + 1
    dominant_mode = max(
        mode_counts,
        key=lambda m: (mode_counts[m], _mode_priority.get(m, 0)),
    ) if mode_counts else "manual"

    resolved_at_vals = [r.get("resolved_at") for r in rows if r.get("resolved_at")]
    last_resolved_at = max(resolved_at_vals) if resolved_at_vals else None

    success_rate = resolved_count / total
    if total < 3:
        confidence_signal = "insufficient"
    elif success_rate >= 0.80:
        confidence_signal = "strong"
    else:
        confidence_signal = "mixed"

    return {
        "total_outcomes":          total,
        "success_rate":            round(success_rate, 4),
        "recurrence_rate":         round(recurred_count / total, 4),
        "ai_used_rate":            round(ai_used_count / total, 4),
        "avg_resolution_minutes":  round(avg_minutes, 1) if avg_minutes is not None else None,
        "dominant_execution_mode": dominant_mode,
        "last_resolved_at":        last_resolved_at,
        "confidence_signal":       confidence_signal,
    }


def get_outcomes(
    classification: str,
    tenant_id: str = "default",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """PG-first, JSON-fallback reader for Phase 2 inline intelligence.

    Returns up to `limit` outcomes for the classification, newest first.
    """
    try:
        import postgres_store as pg
        rows = pg.get_outcomes_by_classification(classification, tenant_id=tenant_id, limit=limit)
        if rows:
            return rows
    except Exception:
        pass
    # JSON fallback — sort newest-first by resolved_at
    rows = _read_json_outcomes(classification=classification, tenant_id=tenant_id)
    rows.sort(key=lambda r: r.get("resolved_at") or "", reverse=True)
    return rows[:limit]
