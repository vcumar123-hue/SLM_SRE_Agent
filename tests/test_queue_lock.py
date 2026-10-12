"""
Wave 1b: the queue lock is an OS-level lock with bounded waits (no delete-based lock file).

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_queue_lock -v
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import agents.automation as aa
import app as app_module
import automation_engine as ae
import security

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
PY = sys.executable


def spawn(*args, env=None):
    return subprocess.Popen([PY, "-m", "tests._lock_helpers", *args], cwd=ROOT, env=env or os.environ.copy(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(proc, token, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if token in line:
            return
        if proc.poll() is not None:
            break
    raise AssertionError(f"helper did not report '{token}': {proc.stderr.read()[:400]}")


class LockBase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.queue_file = self.tmp / "queue.json"
        self._patches = [
            patch.object(ae, "AUTOMATION_QUEUE_FILE", self.queue_file),
            patch.object(ae, "AUTOMATION_LOG_FILE", self.tmp / "log.json"),
            patch.object(ae, "AUTOMATION_QUEUE_ARCHIVE_FILE", self.tmp / "arch.json"),
            patch.object(ae, "QUEUE_LOCK_TIMEOUT_SECONDS", 2.0),
        ]
        for p in self._patches:
            p.start()
        self.lock_path = self.queue_file.with_suffix(".lock")

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()


class LockSemanticsTests(LockBase):
    def test_lock_file_is_persistent_and_never_deleted(self):
        with ae._queue_lock("t"):
            pass
        self.assertTrue(self.lock_path.exists())
        with ae._queue_lock("t"):
            pass
        self.assertTrue(self.lock_path.exists())

    def test_failure_to_delete_the_lock_file_cannot_stall_anything(self):
        real_unlink = pathlib.Path.unlink

        def refuse_lock_unlink(self_, *a, **k):
            if str(self_).endswith(".lock"):
                raise PermissionError("simulated: file in use")
            return real_unlink(self_, *a, **k)

        t0 = time.monotonic()
        with patch.object(pathlib.Path, "unlink", refuse_lock_unlink):
            for _ in range(10):
                with ae._queue_lock("t"):
                    pass
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_reentrant_in_one_thread(self):
        with ae._queue_lock("outer"):
            with ae._queue_lock("inner"):
                pass

    def test_in_process_contention_times_out_with_diagnostics(self):
        held, release = threading.Event(), threading.Event()

        def holder():
            with ae._queue_lock("holder"):
                held.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        held.wait(2)
        try:
            t0 = time.monotonic()
            with self.assertRaises(ae.QueueBusyError) as ctx:
                with ae._queue_lock("approve", "aq_123", timeout=0.4):
                    pass
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertEqual((ctx.exception.operation, ctx.exception.queue_id), ("approve", "aq_123"))
            self.assertIn("aq_123", str(ctx.exception))
        finally:
            release.set()
            t.join()

    def test_cross_process_contention_is_bounded_and_clears_when_holder_exits(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        proc = spawn("hold", str(self.lock_path), "3")
        try:
            wait_for(proc, "locked")
            t0 = time.monotonic()
            with self.assertRaises(ae.QueueBusyError):
                with ae._queue_lock("execute_claim", "aq_x", timeout=0.5):
                    pass
            self.assertLess(time.monotonic() - t0, 2.0)
        finally:
            proc.wait(10)
        with ae._queue_lock("after_holder", timeout=2.0):  # holder has exited: lock is free again
            pass

    def test_lock_is_released_when_the_holding_process_is_killed(self):
        proc = spawn("hold", str(self.lock_path), "60")
        try:
            wait_for(proc, "locked")
            proc.kill()
            proc.wait(10)
            t0 = time.monotonic()
            with ae._queue_lock("after_kill", timeout=5.0):
                pass
            self.assertLess(time.monotonic() - t0, 5.0)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_unusable_lock_file_fails_fast_with_a_clear_error(self):
        # Make the lock path a directory so it can never be opened as a file.
        self.lock_path.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        with self.assertRaises(ae.QueueBusyError) as ctx:
            with ae._queue_lock("queue"):
                pass
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertIn("unusable", str(ctx.exception))


class DoubleExecutionAcrossProcessesTests(LockBase):
    def test_two_processes_executing_the_same_entry_run_it_once(self):
        entry = ae.queue_automation_plan("inc-1", "cpu_saturation")["queue_entry"]
        qid = entry["queue_id"]
        ae.approve_automation(qid, approved_by="cara", approver_role="l3",
                              gate_run={"gate_run_id": "gr_test", "gate_status": "passed"})
        stored = ae.get_queue_entry(qid)
        steps = len(stored["plan"]["steps"])
        auth = {"allowed": True, "queue_id": qid, "mode": "live", "plan_hash": ae.compute_plan_hash(stored)}
        call_log = self.tmp / "calls.log"
        env = {**os.environ, "AIOPS_DATA_DIR": str(self.tmp)}
        # The helper process builds its engine paths from AIOPS_DATA_DIR (queue.json is named differently there).
        (self.tmp / "automation_queue.json").write_text(self.queue_file.read_text(encoding="utf-8"), encoding="utf-8")

        procs = [spawn("execute", qid, json.dumps(auth), str(call_log), env=env) for _ in range(2)]
        outputs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err[:400])
            outputs.append(json.loads(out.strip().splitlines()[-1])["status"])
        self.assertEqual(sorted(outputs), ["not_executable", "ok"], outputs)
        calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
        ansible_steps = len([s for s in stored["plan"]["steps"] if s.get("connector") == "ansible"])
        self.assertEqual(len(calls), ansible_steps, f"plan ran more than once: {calls}")
        self.assertGreaterEqual(steps, 1)


class ApiReturns503WhenLockHeldTests(LockBase):
    def setUp(self):
        super().setUp()
        self._more = [
            patch.object(aa, "AUTOMATION_QUEUE_FILE", self.queue_file),
            patch.object(aa, "AUTOMATION_LOG_FILE", self.tmp / "log.json"),
            patch.object(ae, "QUEUE_LOCK_TIMEOUT_SECONDS", 0.5),
            patch.object(aa, "postgres_write_safe", lambda *a, **k: None),
            patch.dict(os.environ, {"AIOPS_PRINCIPALS": json.dumps(
                {"tok": {"id": "bob", "role": "l3", "kind": "human"}}), "AIOPS_API_KEY": ""}),
        ]
        for p in self._more:
            p.start()
        security.reset_principals_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in reversed(self._more):
            p.stop()
        security.reset_principals_cache()
        super().tearDown()

    def test_queue_endpoint_returns_503_with_retry_after_and_diagnostics(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        proc = spawn("hold", str(self.lock_path), "4")
        try:
            wait_for(proc, "locked")
            t0 = time.monotonic()
            r = self.client.post("/automation/queue", headers={"Authorization": "Bearer tok"},
                                 json={"incident_id": "i", "classification": "cpu_saturation"})
            self.assertEqual(r.status_code, 503, r.text)
            self.assertEqual(r.headers.get("retry-after"), "2")
            detail = r.json()["detail"]
            self.assertEqual((detail["status"], detail["operation"]), ("busy", "queue"))
            self.assertLess(time.monotonic() - t0, 3.0)
        finally:
            proc.wait(10)


if __name__ == "__main__":
    unittest.main()
