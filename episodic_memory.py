"""
episodic_memory.py — Item #14
Per-incident episodic recall: stores and retrieves the resolution episode
for each classification so engineers see "seen 6 times, last solved in 14 min"
above the playbook in the Incident Console.

Storage: PostgreSQL `incident_episodes` (primary) + JSON fallback.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FALLBACK_PATH = Path("data/incident_episodes.json")

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
        logger.warning("episodic_memory pg error: %s", exc)
        return None


def _ensure_schema() -> None:
    def _run():
        with db_cursor(commit=True) as (_, cur):  # type: ignore[misc]
            cur.execute("""
                CREATE TABLE IF NOT EXISTS incident_episodes (
                    id SERIAL PRIMARY KEY,
                    classification TEXT NOT NULL,
                    incident_id TEXT,
                    ticket_summary TEXT,
                    resolved_by TEXT,
                    resolution_mins NUMERIC,
                    key_step TEXT,
                    outcome TEXT DEFAULT 'resolved',
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodes_classification ON incident_episodes (classification)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodes_created ON incident_episodes (created_at DESC)"
            )
    _pg(_run)


def _load_fallback() -> List[Dict[str, Any]]:
    try:
        if _FALLBACK_PATH.exists():
            data = json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save_fallback(records: List[Dict[str, Any]]) -> None:
    try:
        _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FALLBACK_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        logger.warning("episodic_memory fallback write failed: %s", exc)


def record_episode(
    classification: str,
    incident_id: str = "",
    ticket_summary: str = "",
    resolved_by: str = "",
    resolution_mins: float = 0.0,
    key_step: str = "",
    outcome: str = "resolved",
    notes: str = "",
) -> None:
    """Call after each confirmed resolution to store the episode."""
    _ensure_schema()
    classification = classification.strip().lower()
    if not classification:
        return

    def _pg_write():
        with db_cursor(commit=True) as (_, cur):  # type: ignore[misc]
            cur.execute("""
                INSERT INTO incident_episodes
                    (classification, incident_id, ticket_summary, resolved_by,
                     resolution_mins, key_step, outcome, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (classification, incident_id, ticket_summary[:300], resolved_by,
                  resolution_mins, key_step[:200], outcome, notes[:500]))
    _pg(_pg_write)

    records = _load_fallback()
    records.append({
        "classification": classification,
        "incident_id": incident_id,
        "ticket_summary": ticket_summary[:300],
        "resolved_by": resolved_by,
        "resolution_mins": resolution_mins,
        "key_step": key_step,
        "outcome": outcome,
        "notes": notes,
    })
    _save_fallback(records[-500:])  # keep last 500 episodes in JSON


def get_episodes(classification: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Return the most recent episodes for a classification."""
    _ensure_schema()
    classification = classification.strip().lower()

    def _pg_read():
        with db_cursor(dict_rows=True) as (_, cur):  # type: ignore[misc]
            cur.execute("""
                SELECT * FROM incident_episodes
                WHERE classification = %s
                ORDER BY created_at DESC LIMIT %s
            """, (classification, limit))
            return list(cur.fetchall())
    result = _pg(_pg_read)
    if result is not None:
        return result

    records = _load_fallback()
    matches = [r for r in records if r.get("classification") == classification]
    return list(reversed(matches[-limit:]))


def get_recall_banner(classification: str) -> Optional[str]:
    """
    Returns a one-line HTML badge suitable for display above the runbook.
    Example: '🔁 Seen 6× — avg 14 min — last solved via "Restart JVM Pool" by Priya'
    Returns None if no prior episodes exist.
    """
    episodes = get_episodes(classification, limit=20)
    if not episodes:
        return None

    total = len(episodes)
    resolved = [e for e in episodes if str(e.get("outcome") or "resolved") == "resolved"]
    mins = [float(e.get("resolution_mins") or 0) for e in resolved if e.get("resolution_mins")]
    avg_mins = round(sum(mins) / len(mins), 0) if mins else None
    last = episodes[0]
    last_by = str(last.get("resolved_by") or "").strip()
    last_step = str(last.get("key_step") or "").strip()

    parts = [f"Seen {total}×"]
    if avg_mins:
        parts.append(f"avg {avg_mins:.0f} min")
    if last_step:
        parts.append(f'last resolved via <em>"{last_step}"</em>')
    if last_by:
        parts.append(f"by {last_by}")

    inner = " · ".join(parts)
    return (
        f'<div style="display:flex;align-items:center;gap:8px;padding:7px 12px;'
        f'border-radius:8px;background:rgba(79,142,247,0.08);'
        f'border:1px solid rgba(79,142,247,0.25);font-size:0.78rem;margin-bottom:10px;">'
        f'<span style="font-size:1rem;">🔁</span>'
        f'<span style="color:#4F8EF7;font-weight:600;">{inner}</span>'
        f'</div>'
    )


def get_recall_html(classification: str) -> str:
    """Returns the recall banner HTML or empty string — safe to use with st.markdown."""
    return get_recall_banner(classification) or ""
