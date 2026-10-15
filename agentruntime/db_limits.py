"""
Per-call database limits for runtime tools.

A tool sets the statement timeout for the current call here; `postgres_store.get_connection` applies it to every connection opened
while it is set, so retrieval, outcome and ticket queries made on the tool's behalf cannot run longer than the tool's own budget. The
value is a ContextVar, so it belongs to the worker thread running that one call and cannot leak into other requests.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Iterator, Optional

STATEMENT_TIMEOUT_MS: ContextVar[Optional[int]] = ContextVar("agent_statement_timeout_ms", default=None)
MARGIN_MS = 250          # leave room for the wrapper to return before the caller's own timeout fires
MARGIN_FRACTION = 0.25   # ...but never more than this share of a very short budget


class DeadlineExhausted(TimeoutError):
    """No time is left for a database statement."""


def timeout_ms_for(timeout_s: Optional[float]) -> Optional[int]:
    """The statement timeout, in milliseconds, for a call that has `timeout_s` seconds left (None means no limit was given).

    Always strictly less than the time left, and at least 1 ms (0 means "no limit" in PostgreSQL). With no time left it raises
    DeadlineExhausted rather than start a statement that cannot finish.
    """
    if timeout_s is None:
        return None
    total_ms = float(timeout_s) * 1000.0
    if total_ms < 2.0:
        raise DeadlineExhausted(f"{timeout_s:.3f}s left: too little time to run a statement")
    margin = min(MARGIN_MS, total_ms * MARGIN_FRACTION)
    return max(1, int(total_ms - margin))


@contextlib.contextmanager
def statement_timeout(timeout_s: Optional[float]) -> Iterator[Optional[int]]:
    """Apply a statement timeout to every database connection opened inside the block."""
    ms = timeout_ms_for(timeout_s)
    token = STATEMENT_TIMEOUT_MS.set(ms)
    try:
        yield ms
    finally:
        STATEMENT_TIMEOUT_MS.reset(token)
