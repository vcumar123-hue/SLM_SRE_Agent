"""
Bounded, cancellable execution of planner, model and tool calls.

A call that exceeds its timeout cannot be killed inside a thread, so the executor limits the damage instead:
  1. every call runs on a shared, fixed-size pool of **daemon** worker threads (never a new pool per call, and a stuck thread cannot
     block interpreter exit);
  2. the function receives a CancelToken that is set the moment the call times out, and callers pass the effective timeout down so
     HTTP clients and database statements can stop on their own;
  3. calls that timed out and are still running are counted as *abandoned*, and new work is refused while too many are (ExecutorBusy);
  4. an abandoned call that is still running after `leak_after_s` is declared *leaked*: its worker is retired (it exits when the call
     finally returns) and a replacement worker is started, so capacity recovers after a transient hang. Leaked calls are capped at
     `max_leaked`: at the cap the executor refuses all work (ExecutorFailed) until some of them return. Stuck threads (abandoned plus
     leaked) are therefore bounded by `max_abandoned + max_leaked`, and the live threads of one executor by `max_workers + max_leaked`
     (abandoned calls still occupy active workers; only leaked ones have been replaced);
  5. a call still queued when its timeout expires is cancelled before it starts.
Planner decisions, model requests and tool calls each have their own pool, so a planner waiting on a model request can never starve
the model pool (nested calls must never share an executor).
"""
from __future__ import annotations

import concurrent.futures
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional, Set


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
    """Too many earlier calls are still running after their timeout; new work is refused until they finish or are declared leaked."""


class ExecutorFailed(Exception):
    """The maximum number of leaked (stuck) calls is reached; all work is refused until some of them return."""


class _Job:
    __slots__ = ("fn", "token", "future", "worker", "abandoned_at")

    def __init__(self, fn: Callable[[CancelToken], Any]) -> None:
        self.fn = fn
        self.token = CancelToken()
        self.future: concurrent.futures.Future = concurrent.futures.Future()
        self.worker: Optional["_Worker"] = None
        self.abandoned_at: Optional[float] = None


class _Worker(threading.Thread):
    def __init__(self, owner: "BoundedExecutor", index: int) -> None:
        super().__init__(name=f"{owner.name}-{index}", daemon=True)
        self.owner = owner
        self.retired = False

    def run(self) -> None:
        while not self.retired:
            job = self.owner._queue.get()
            if job is None:
                return
            if not job.future.set_running_or_notify_cancel():
                continue                                    # cancelled while queued: never started
            job.worker = self
            try:
                job.future.set_result(job.fn(job.token))
            except BaseException as exc:  # noqa: BLE001 - delivered to the waiting caller
                job.future.set_exception(exc)
            finally:
                self.owner._finished(job)


class BoundedExecutor:
    def __init__(self, max_workers: int = 8, max_abandoned: int = 4, name: str = "agent", *, leak_after_s: float = 60.0,
                 max_leaked: int = 8, clock: Callable[[], float] = time.monotonic) -> None:
        if max_abandoned >= max_workers:
            raise ValueError("max_abandoned must be smaller than max_workers so a worker is always free")
        self.name = name
        self.max_workers = max_workers
        self.max_abandoned = max_abandoned
        self.leak_after_s = leak_after_s
        self.max_leaked = max_leaked
        self._clock = clock
        self._queue: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._lock = threading.Lock()
        self._workers: Set[_Worker] = set()
        self._abandoned: Dict[_Job, float] = {}
        self._leaked: Set[_Job] = set()
        self._started = 0

    # -- introspection (used by tests and monitoring) -------------------------------------------------------------------------

    @property
    def abandoned(self) -> int:
        with self._lock:
            return len(self._abandoned)

    @property
    def leaked(self) -> int:
        with self._lock:
            return len(self._leaked)

    @property
    def live_threads(self) -> int:
        with self._lock:
            return sum(1 for w in self._workers if w.is_alive())

    # -- internals ------------------------------------------------------------------------------------------------------------

    def _ensure_workers(self) -> None:
        """Called with the lock held: keep `max_workers` active (non-retired) workers."""
        self._workers = {w for w in self._workers if w.is_alive()}
        active = [w for w in self._workers if not w.retired]
        while len(active) < self.max_workers:
            self._started += 1
            worker = _Worker(self, self._started)
            self._workers.add(worker)
            active.append(worker)
            worker.start()

    def _reap(self) -> None:
        """Declare abandoned calls that have run too long as leaked, retiring their workers so capacity recovers."""
        now = self._clock()
        with self._lock:
            for job, since in list(self._abandoned.items()):
                if now - since >= self.leak_after_s and not job.future.done():
                    del self._abandoned[job]
                    self._leaked.add(job)
                    if job.worker is not None:
                        job.worker.retired = True
            self._ensure_workers()

    def _finished(self, job: _Job) -> None:
        with self._lock:
            self._abandoned.pop(job, None)
            self._leaked.discard(job)

    # -- the one public operation ---------------------------------------------------------------------------------------------

    def run(self, fn: Callable[[CancelToken], Any], timeout_s: float) -> Any:
        self._reap()
        with self._lock:
            if len(self._leaked) >= self.max_leaked:
                raise ExecutorFailed(f"{len(self._leaked)} stuck call(s) have not returned; refusing all work")
            if len(self._abandoned) >= self.max_abandoned:
                raise ExecutorBusy(f"{len(self._abandoned)} timed-out call(s) are still running")
            self._ensure_workers()
        job = _Job(fn)
        self._queue.put(job)
        try:
            return job.future.result(timeout=max(0.0, timeout_s))
        except concurrent.futures.TimeoutError as exc:
            job.token.cancel()
            if job.future.cancel():                         # never started: nothing is left running
                raise CallTimeout(f"timed out after {timeout_s:.1f}s while queued") from exc
            with self._lock:
                if not job.future.done():
                    self._abandoned[job] = self._clock()
                    raise CallTimeout(f"timed out after {timeout_s:.1f}s") from exc
            return job.future.result()                      # it finished while we were deciding


# one shared pool per kind of call, so nested calls never compete for the same workers
PLANNER_EXECUTOR = BoundedExecutor(max_workers=16, max_abandoned=8, name="agent-planner", max_leaked=8)
MODEL_EXECUTOR = BoundedExecutor(max_workers=8, max_abandoned=4, name="agent-model", max_leaked=8)
TOOL_EXECUTOR = BoundedExecutor(max_workers=8, max_abandoned=4, name="agent-tool", max_leaked=8)
