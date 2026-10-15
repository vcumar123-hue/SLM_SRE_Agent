"""
Bounded, cancellable execution of tool and model calls (design 7 and review finding "timed-out tools keep running").

A call that exceeds its timeout cannot be killed inside a thread, so the executor does three things instead:
  1. runs every call on one shared, fixed-size pool (never a new pool per call);
  2. hands the function a CancelToken, set the moment the call times out, so cooperative callers (and database statement timeouts)
     stop promptly;
  3. counts abandoned calls and refuses new work while too many are still running, so repeated timeouts cannot pile up threads,
     connections or model requests.
A call still queued when its timeout expires is cancelled before it ever starts.
"""
from __future__ import annotations

import concurrent.futures
import threading
from typing import Any, Callable


class CancelToken:
    """Set when a call has timed out or the run is being stopped. Long-running wrappers must check it between steps."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("the call was cancelled")


class Cancelled(Exception):
    pass


class CallTimeout(Exception):
    """The call did not finish inside its timeout (it may still be running; see BoundedExecutor)."""


class ExecutorBusy(Exception):
    """Too many earlier calls are still running after their timeout; new work is refused until they finish."""


class BoundedExecutor:
    def __init__(self, max_workers: int = 8, max_abandoned: int = 4, name: str = "agent") -> None:
        if max_abandoned >= max_workers:
            raise ValueError("max_abandoned must be smaller than max_workers so a worker is always free")
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)
        self._lock = threading.Lock()
        self._abandoned = 0
        self.max_workers = max_workers
        self.max_abandoned = max_abandoned

    @property
    def abandoned(self) -> int:
        with self._lock:
            return self._abandoned

    def run(self, fn: Callable[[CancelToken], Any], timeout_s: float) -> Any:
        with self._lock:
            if self._abandoned >= self.max_abandoned:
                raise ExecutorBusy(f"{self._abandoned} timed-out call(s) are still running")
        token = CancelToken()
        future = self._pool.submit(fn, token)
        try:
            return future.result(timeout=max(0.0, timeout_s))
        except concurrent.futures.TimeoutError as exc:
            token.cancel()
            if future.cancel():          # never started: nothing is left running
                raise CallTimeout(f"timed out after {timeout_s:.1f}s while queued") from exc
            with self._lock:
                self._abandoned += 1
            future.add_done_callback(self._release)
            raise CallTimeout(f"timed out after {timeout_s:.1f}s") from exc

    def _release(self, _future: Any) -> None:
        with self._lock:
            self._abandoned = max(0, self._abandoned - 1)


# one shared pool for tools and one for model requests, so their limits are global to the process
TOOL_EXECUTOR = BoundedExecutor(max_workers=8, max_abandoned=4, name="agent-tool")
MODEL_EXECUTOR = BoundedExecutor(max_workers=4, max_abandoned=2, name="agent-model")
