"""
long_term_memory.py — Item #13
Outcome reinforcement loop: records per-classification resolution outcomes
so PlaybookAgent can bias step generation toward steps that worked before.

Storage: PostgreSQL `classification_outcomes` table (primary) + JSON fallback.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FALLBACK_PATH = Path("data/classification_outcomes.json")

# ── PostgreSQL backend ────────────────────────────────────────────────────────
try:
    from postgres_store import db_cursor, Json as _Json
    _PG = True
except Exception:
    db_cursor = None  # type: ignore[assignment]
    _Json = None      # type: ignore[assignment]
    _PG = False


def _pg(fn, *a, **kw):
    if not _PG:
        return None
    try:
        return fn(*a, **kw)
    except Exception as exc:
        logger.warning("long_term_memory pg error: %s", exc)
        return None


def _ensure_schema() -> None:
    def _run():
        with db_cursor(commit=True) as (_, cur):  # type: ignore[misc]
            cur.execute("""
                CREATE TABLE IF NOT EXISTS classification_outcomes (
                    classification TEXT PRIMARY KEY,
                    total_resolutions INT DEFAULT 0,
                    successful_resolutions INT DEFAULT 0,
                    failed_resolutions INT DEFAULT 0,
                    avg_resolution_mins NUMERIC DEFAULT 0,
                    step_success_rates JSONB DEFAULT '{}',
                    last_resolved_by TEXT,
                    last_resolved_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_co_classification ON classification_outcomes (classification)"
            )
    _pg(_run)


# ── JSON fallback ─────────────────────────────────────────────────────────────
def _load_fallback() -> Dict[str, Any]:
    try:
        if _FALLBACK_PATH.exists():
            return json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_fallback(data: Dict[str, Any]) -> None:
    try:
        _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FALLBACK_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("long_term_memory fallback write failed: %s", exc)


# ── Public API ────────────────────────────────────────────────────────────────
def record_outcome(
    classification: str,
    success: bool,
    resolution_mins: float = 0.0,
    resolved_by: str = "",
    step_outcomes: Optional[Dict[str, bool]] = None,
) -> None:
    """
    Call after each resolution is confirmed (Approve & Learn or automation execution).
    step_outcomes: {step_title: True/False} — which steps succeeded/were skipped
    """
    _ensure_schema()
    classification = classification.strip().lower()
    if not classification:
        return

    def _pg_write():
        with db_cursor(commit=True) as (_, cur):  # type: ignore[misc]
            cur.execute("""
                INSERT INTO classification_outcomes
                    (classification, total_resolutions, successful_resolutions, failed_resolutions,
                     avg_resolution_mins, step_success_rates, last_resolved_by, last_resolved_at, updated_at)
                VALUES (%s, 1, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (classification) DO UPDATE SET
                    total_resolutions = classification_outcomes.total_resolutions + 1,
                    successful_resolutions = classification_outcomes.successful_resolutions + %s,
                    failed_resolutions = classification_outcomes.failed_resolutions + %s,
                    avg_resolution_mins = (
                        classification_outcomes.avg_resolution_mins *
                        classification_outcomes.total_resolutions + %s
                    ) / (classification_outcomes.total_resolutions + 1),
                    step_success_rates = (
                        CASE WHEN %s::jsonb IS NOT NULL
                        THEN classification_outcomes.step_success_rates || %s::jsonb
                        ELSE classification_outcomes.step_success_rates END
                    ),
                    last_resolved_by = %s,
                    last_resolved_at = NOW(),
                    updated_at = NOW()
            """, (
                classification,
                1 if success else 0,
                0 if success else 1,
                resolution_mins,
                _Json(step_outcomes or {}),
                1 if success else 0,
                0 if success else 1,
                resolution_mins,
                json.dumps(step_outcomes) if step_outcomes else None,
                json.dumps(step_outcomes) if step_outcomes else None,
                resolved_by,
            ))
    _pg(_pg_write)

    # JSON fallback
    data = _load_fallback()
    rec = data.get(classification, {
        "total": 0, "success": 0, "fail": 0,
        "avg_mins": 0.0, "steps": {}, "last_by": "",
    })
    prev_total = rec["total"]
    rec["total"] += 1
    rec["success"] += 1 if success else 0
    rec["fail"] += 0 if success else 1
    rec["avg_mins"] = (rec["avg_mins"] * prev_total + resolution_mins) / rec["total"]
    rec["last_by"] = resolved_by
    if step_outcomes:
        for step, ok in step_outcomes.items():
            prev = rec["steps"].get(step, {"ok": 0, "total": 0})
            prev["total"] += 1
            prev["ok"] += 1 if ok else 0
            rec["steps"][step] = prev
    data[classification] = rec
    _save_fallback(data)


def get_outcome(classification: str) -> Optional[Dict[str, Any]]:
    """Return outcome stats for a classification, or None if no history."""
    _ensure_schema()
    classification = classification.strip().lower()

    def _pg_read():
        with db_cursor(dict_rows=True) as (_, cur):  # type: ignore[misc]
            cur.execute(
                "SELECT * FROM classification_outcomes WHERE classification = %s LIMIT 1",
                (classification,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    result = _pg(_pg_read)
    if result is not None:
        return result

    # JSON fallback
    data = _load_fallback()
    return data.get(classification)


def get_step_bias(classification: str) -> Dict[str, float]:
    """
    Return per-step success rates (0.0–1.0) for use by PlaybookAgent.
    High-success steps should appear first / be reinforced in generated playbooks.
    """
    outcome = get_outcome(classification)
    if not outcome:
        return {}
    steps = outcome.get("step_success_rates") or outcome.get("steps") or {}
    bias: Dict[str, float] = {}
    for step, data in steps.items():
        if isinstance(data, dict):
            total = int(data.get("total", 0) or 0)
            ok = int(data.get("ok", 0) or 0)
            bias[step] = round(ok / total, 3) if total else 0.5
        elif isinstance(data, bool):
            bias[step] = 1.0 if data else 0.0
    return bias


def get_resolution_summary(classification: str) -> str:
    """Return a human-readable one-liner for the episodic memory banner."""
    outcome = get_outcome(classification)
    if not outcome:
        return ""
    total = int(outcome.get("total_resolutions") or outcome.get("total") or 0)
    success = int(outcome.get("successful_resolutions") or outcome.get("success") or 0)
    avg = float(outcome.get("avg_resolution_mins") or outcome.get("avg_mins") or 0)
    last_by = str(outcome.get("last_resolved_by") or outcome.get("last_by") or "")
    if total == 0:
        return ""
    rate = round(success / total * 100)
    parts = [f"Seen {total}× · {rate}% success rate"]
    if avg:
        parts.append(f"avg {avg:.0f} min")
    if last_by:
        parts.append(f"last resolved by {last_by}")
    return " · ".join(parts)


def list_top_classifications(limit: int = 10) -> List[Dict[str, Any]]:
    """Return top classifications by resolution count — for dashboard."""
    _ensure_schema()

    def _pg_list():
        with db_cursor(dict_rows=True) as (_, cur):  # type: ignore[misc]
            cur.execute("""
                SELECT classification, total_resolutions, successful_resolutions,
                       avg_resolution_mins, last_resolved_by, last_resolved_at
                FROM classification_outcomes
                ORDER BY total_resolutions DESC
                LIMIT %s
            """, (limit,))
            return list(cur.fetchall())
    result = _pg(_pg_list)
    if result is not None:
        return result
    data = _load_fallback()
    rows = [{"classification": k, "total_resolutions": v.get("total", 0), **v}
            for k, v in data.items()]
    rows.sort(key=lambda r: r.get("total_resolutions", 0), reverse=True)
    return rows[:limit]
