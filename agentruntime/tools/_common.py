"""Helpers shared by the read-only tools (design 4.2): cancellation, bounded text, safe LIKE patterns, timestamps."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from agentruntime.executor import CancelToken

EXCERPT_CHARS = 400


def check_cancel(cancel: Optional[CancelToken]) -> None:
    """Stop promptly when the call has timed out or the run is being stopped (call between every slow step)."""
    if cancel is not None:
        cancel.raise_if_cancelled()


def clip(value: Any, limit: int = EXCERPT_CHARS) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def like_pattern(term: str) -> str:
    """A `%term%` pattern with the LIKE wildcards in the term escaped (use with ESCAPE '\\')."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Best-effort, timezone-aware (UTC) parse of an ISO-like timestamp; None when it does not parse."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def relevance(score: Any) -> Optional[float]:
    """A retrieval score clamped into [0, 1] (hybrid scores can exceed 1)."""
    try:
        return max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return None
