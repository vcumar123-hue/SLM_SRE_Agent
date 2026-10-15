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
MARGIN_S = 0.25          # leave room for the wrapper to return before the caller's own timeout fires
MIN_MS = 100


def timeout_ms_for(timeout_s: Optional[float]) -> Optional[int]:
    """The statement timeout, in milliseconds, for a call that has `timeout_s` seconds left (None means no limit was given)."""
    if timeout_s is None:
        return None
    return max(MIN_MS, int((float(timeout_s) - MARGIN_S) * 1000))


@contextlib.contextmanager
def statement_timeout(timeout_s: Optional[float]) -> Iterator[Optional[int]]:
    """Apply a statement timeout to every database connection opened inside the block."""
    ms = timeout_ms_for(timeout_s)
    token = STATEMENT_TIMEOUT_MS.set(ms)
    try:
        yield ms
    finally:
        STATEMENT_TIMEOUT_MS.reset(token)
