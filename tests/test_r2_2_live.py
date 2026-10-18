"""
R2-2 end-to-end rehearsal against a real PostgreSQL (rehearsal of the controlled shadow test, using a throwaway tenant and a temporary decision file).

It drives the real /analyze route, the real hook, shadow queue, runner, frozen planner and encrypted run store, with fake tools. It proves, before any real tenant is
involved: the response is unchanged; the run starts only after the body has been sent; the run is stored encrypted (and decrypts for the right tenant); the rollback
switch stops further runs at once. It runs only when AIOPS_TEST_AGENT_PG=1 and a PostgreSQL is reachable through the PG* variables.

    AIOPS_TEST_AGENT_PG=1 python -m unittest tests.test_r2_2_live -v
"""
import asyncio
import copy
import json
import os
import unittest
import uuid
from unittest.mock import patch

from agentruntime import config as runtime_config
from agentruntime import crypto
from agentruntime.contracts import EvidenceDraft, ToolSpec
from agentruntime.executor import BoundedExecutor
from agentruntime.integration import gate, hook
from agentruntime.integration.shadow import ShadowQueue
from agentruntime.runner import Runner
from agentruntime.store import RunStore
from agentruntime.tools import TOOL_MODULES
from tests.agentruntime_helpers import tenant_dir

LIVE = os.getenv("AIOPS_TEST_AGENT_PG") == "1"
MARKER = "stuck orchard sprinkler valve"


def fake_registry():
    """The five real tool specs over deterministic fakes: one relevant incident whose own text states a cause (two copies, so the cause is corroborated)."""
    from agentruntime.registry import ToolRegistry
    reg = ToolRegistry(executor=BoundedExecutor(4, 2, "live-tools"))

    def make(name):
        def fn(ctx, *, cancel=None, timeout_s=None, **args):
            if name == "incident_kb_search":
                return [EvidenceDraft(tenant_id=ctx.tenant_id, source_type="incident", source_id=f"kb-{i}", provenance="tenant_data", relevance=0.7,
                                      excerpt=f"File system full on the data host. Root Cause: {MARKER}; Resolution: purge", facts={"classification": "unknown"})
                        for i in range(2)]
            if name == "ticket_history_lookup":
                return []
            return [EvidenceDraft(tenant_id=ctx.tenant_id, source_type={"outcome_lookup": "outcome", "pattern_lookup": "pattern", "evidence_lookup": "evidence_check"}[name],
                                  source_id=name, provenance="computed", excerpt=f"No recorded data from {name}.", facts={"total_resolutions": 0})]
        fn.__name__ = name
        return fn
    for m in TOOL_MODULES:
        reg.register(m.SPEC, make(m.NAME))
    return reg


@unittest.skipUnless(LIVE, "set AIOPS_TEST_AGENT_PG=1 with a reachable PostgreSQL to run")
class ShadowRehearsalTests(unittest.TestCase):
    CANNED = {"request_id": "rehearsal-1", "ticket": "disk full on the data host", "tenant_config": {"tenant_id": ""},
              "analysis": {"classification": "disk_full", "probable_causes": ["quota too small"], "escalate": False},
              "confidence": {"overall_confidence": 0.3, "retrieval_quality": 0.1}, "retrieved_incidents": [], "linked_resolution": None, "linked_playbook": None}

    @classmethod
    def setUpClass(cls):
        if crypto.AESGCM is None:
            raise unittest.SkipTest("the cryptography package is not installed")
        try:
            from fastapi import FastAPI
            import app as application
            from agents import analyze as analyze_module
        except Exception as exc:                                                        # pragma: no cover
            raise unittest.SkipTest(f"the web stack is not importable here: {exc}")
        cls.application = application
        cls.web = FastAPI()
        cls.web.include_router(analyze_module.router)

        @cls.web.middleware("http")
        async def principal(request, call_next):                                       # stands in for the security middleware
            request.state.principal = type("P", (), {"id": "rehearsal-svc", "allows_tenant": lambda self, t: True})()
            return await call_next(request)

    def setUp(self):
        self.tenant = "rehearsal_" + uuid.uuid4().hex[:8]
        self.canned = copy.deepcopy(self.CANNED)
        self.canned["tenant_config"]["tenant_id"] = self.tenant
        key = crypto.generate_key()
        self.dir = tenant_dir(self.tenant)
        import pathlib
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="rehearsal-"))
        (tmp / "decisions.json").write_text(json.dumps({"decisions": [{"tenant_id": self.tenant, "mode": "shadow", "decided_by": "Rehearsal Owner", "date": "2026-10-17",
                                                                        "reference": "REHEARSAL-NOT-A-REAL-DECISION"}]}), encoding="utf-8")
        stack = [patch.dict(os.environ, {runtime_config.MODE_ENV: "shadow", "AIOPS_AGENT_DATA_KEY": key, "AIOPS_TENANT_CONFIG_DIR": str(self.dir),
                                         gate.ORCHESTRATOR_ENV: "", gate.KILL_ENV: ""}),
                 patch.object(gate, "DECISIONS_PATH", tmp / "decisions.json"),
                 patch.object(gate, "freeze_problems", lambda force=False: []),            # the rehearsal does not depend on the (unconfirmed) approval
                 patch.object(self.application, "analyze_core", side_effect=lambda req: copy.deepcopy(self.canned))]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        gate.clear_rollback()
        self.addCleanup(gate.clear_rollback)
        self.queue = ShadowQueue(lambda job: hook._execute(job.ctx, job.policy, job.task, job.request_id),
                                 requires_idle=False)
        stack2 = [patch.dict(hook._state, {"queue": self.queue, "runner": Runner(fake_registry())})]
        for p in stack2:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.queue.stop)
        self.store = RunStore()
        self.store.ensure_schema()
        self.addCleanup(lambda: self.store.delete_tenant(self.tenant))

    def post(self, events):
        async def drive():
            body = json.dumps({"ticket": "disk full on the data host", "tenant_id": self.tenant}).encode()
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/analyze", "raw_path": b"/analyze",
                     "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"host", b"test")],
                     "client": ("127.0.0.1", 1), "server": ("test", 80)}
            state = {"sent": False}
            chunks = []

            async def receive():
                if state["sent"]:
                    await asyncio.sleep(3600)
                state["sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                if message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body"):
                        events.append("body_sent")
            await self.web(scope, receive, send)
            return json.loads(b"".join(chunks).decode("utf-8"))
        return asyncio.run(drive())

    def test_the_whole_shadow_path_then_rollback(self):
        events = []
        real_submit = self.queue.submit

        def submit(job):
            events.append("queued")
            return real_submit(job)
        with patch.object(self.queue, "submit", submit):
            body = self.post(events)
        self.assertEqual(events[:2], ["body_sent", "queued"])                           # queued only after the response body went out
        expected = copy.deepcopy(self.canned)
        body.pop("trace_id", None)
        self.assertEqual(body, expected)                                                # the response is exactly the deterministic one
        self.assertNotIn("investigation", body)
        self.assertTrue(self.queue.wait_until_idle(60))
        self.assertEqual(self.queue.stats()["completed"], 1, self.queue.stats())

        runs = self.store.count_runs(self.tenant)
        self.assertEqual(runs, 1)
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT run_id, mode, status, ticket_hash FROM agent_runs WHERE tenant_id = %s", (self.tenant,))
            run_id, mode, status, ticket_hash = cur.fetchone()
            cur.execute("SELECT item::text FROM agent_evidence WHERE tenant_id = %s", (self.tenant,))
            evidence_text = " ".join(r[0] for r in cur.fetchall())
            cur.execute("SELECT output_json::text FROM agent_run_events WHERE tenant_id = %s", (self.tenant,))
            events_text = " ".join(r[0] for r in cur.fetchall())
        self.assertEqual(mode, "shadow")
        self.assertTrue(ticket_hash)
        self.assertNotIn(MARKER, evidence_text + events_text)                           # encrypted at rest
        self.assertNotIn("disk full on the data host", evidence_text + events_text)
        loaded = self.store.load_run(self.tenant, run_id)
        self.assertIn(MARKER.lower(), " ".join(h.cause for h in loaded.hypotheses).lower() + " ".join(e.excerpt.lower() for e in loaded.evidence))   # and decrypts for its tenant
        self.assertIsNone(self.store.load_run("another_tenant", run_id))                 # but not for another

        gate.rollback("rehearsal: pull the switch")
        more = []
        self.post(more)
        self.assertEqual(self.queue.stats()["submitted"], 1)                            # nothing further was queued
        self.assertEqual(self.store.count_runs(self.tenant), 1)
        self.assertNotIn("queued", more)


if __name__ == "__main__":
    unittest.main()
