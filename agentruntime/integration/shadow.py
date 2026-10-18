"""
The shadow queue (design 8): runs are queued after the response is built and are never awaited by the API.

* in-process and bounded (default 20): a full queue drops the new run and counts it; the request is unaffected;
* a small number of worker threads (default 1), separate from request threads;
* `requires_idle` (default on): a run starts only when no user analysis is in flight; otherwise it waits, and is dropped after `max_wait_s` (default 600). The local
  model server and the database are shared with live users, so a shadow run must not slow a real analysis;
* shutdown (and the rollback switch) drops everything still queued; a run already in flight finishes but is counted `cancelled` and its result is not kept;
* the queue is memory-only: queued runs are lost on restart and are not recreated.
Nothing in `submit` can raise into the request path.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterator, Optional

log = logging.getLogger(__name__)

DEFAULT_QUEUE = 20
DEFAULT_WORKERS = 1
DEFAULT_MAX_WAIT_S = 600.0


class _InFlight:
    """How many user analyses are running right now (the idle probe of the shadow queue)."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self._n += 1

    def leave(self) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)

    @property
    def count(self) -> int:
        with self._lock:
            return self._n


IN_FLIGHT = _InFlight()


@contextmanager
def analysis_in_flight() -> Iterator[None]:
    IN_FLIGHT.enter()
    try:
        yield
    finally:
        IN_FLIGHT.leave()


@dataclass
class ShadowJob:
    ctx: Any
    policy: Any
    task: Dict[str, Any]
    request_id: str
    reasons: list = field(default_factory=list)
    enqueued_at: float = 0.0


class ShadowQueue:
    def __init__(self, run_job: Callable[[ShadowJob], Any], *, max_queue: int = DEFAULT_QUEUE, workers: int = DEFAULT_WORKERS, requires_idle: bool = True,
                 max_wait_s: float = DEFAULT_MAX_WAIT_S, idle: Callable[[], bool] = lambda: IN_FLIGHT.count == 0, poll_s: float = 0.25,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._run_job, self._max_queue, self._workers_n = run_job, int(max_queue), max(1, int(workers))
        self._requires_idle, self._max_wait_s, self._idle, self._poll_s, self._clock = requires_idle, float(max_wait_s), idle, float(poll_s), clock
        self._queue: Deque[ShadowJob] = deque()
        self._cond = threading.Condition()
        self._threads: list = []
        self._stopping = False
        self._running = 0
        self.counters: Dict[str, int] = {"submitted": 0, "dropped_full": 0, "dropped_wait": 0, "dropped_shutdown": 0, "dropped_rollback": 0, "started": 0,
                                         "completed": 0, "failed": 0, "cancelled": 0}

    # -- producer side (the request path): never raises, never blocks ------------------------------------------------------------

    def submit(self, job: ShadowJob) -> bool:
        try:
            with self._cond:
                if self._stopping:
                    self.counters["dropped_shutdown"] += 1
                    return False
                if len(self._queue) >= self._max_queue:
                    self.counters["dropped_full"] += 1
                    return False
                job.enqueued_at = self._clock()
                self._queue.append(job)
                self.counters["submitted"] += 1
                self._ensure_workers()
                self._cond.notify()
                return True
        except Exception:  # noqa: BLE001 - the request must never see a shadow failure
            log.exception("shadow submit failed")
            return False

    def _ensure_workers(self) -> None:
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self._workers_n:
            thread = threading.Thread(target=self._work, name=f"shadow-worker-{len(self._threads) + 1}", daemon=True)
            self._threads.append(thread)
            thread.start()

    # -- consumer side -----------------------------------------------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait(timeout=1.0)
                if self._stopping:
                    return
                job = self._queue.popleft()
                self._running += 1
            try:
                if self._requires_idle and not self._wait_for_idle(job):
                    with self._cond:
                        self.counters["dropped_wait"] += 1
                    continue
                with self._cond:
                    if self._stopping:
                        self.counters["dropped_shutdown"] += 1
                        continue
                    self.counters["started"] += 1
                try:
                    self._run_job(job)
                    with self._cond:
                        self.counters["cancelled" if self._stopping else "completed"] += 1
                except Exception:  # noqa: BLE001 - a failed shadow run is counted, never raised
                    log.exception("shadow run failed")
                    with self._cond:
                        self.counters["failed"] += 1
            finally:
                with self._cond:
                    self._running -= 1
                    self._cond.notify_all()

    def _wait_for_idle(self, job: ShadowJob) -> bool:
        while not self._stopping:
            if self._idle():
                return True
            if self._clock() - job.enqueued_at >= self._max_wait_s:
                return False
            time.sleep(self._poll_s)
        return False

    # -- control -----------------------------------------------------------------------------------------------------------------

    def clear(self, counter: str = "dropped_rollback") -> int:
        """Drop everything queued (used by the rollback switch). A run already in flight is left to finish."""
        with self._cond:
            n = len(self._queue)
            self._queue.clear()
            self.counters[counter] = self.counters.get(counter, 0) + n
            return n

    def stop(self) -> None:
        """Shutdown: queued runs are dropped, workers exit, a run in flight finishes but counts as cancelled."""
        with self._cond:
            self._stopping = True
            self.counters["dropped_shutdown"] += len(self._queue)
            self._queue.clear()
            self._cond.notify_all()

    def idle_now(self) -> bool:
        with self._cond:
            return not self._queue and self._running == 0

    def wait_until_idle(self, timeout_s: float = 10.0) -> bool:
        """For tests and shutdown: wait until nothing is queued or running."""
        end = time.monotonic() + timeout_s
        with self._cond:
            while self._queue or self._running:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(timeout=min(left, 0.2))
            return True

    @property
    def depth(self) -> int:
        with self._cond:
            return len(self._queue)

    def stats(self) -> Dict[str, int]:
        with self._cond:
            return {**self.counters, "queued": len(self._queue), "running": self._running}
