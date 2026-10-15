"""
R2-0: run storage against a real PostgreSQL (design 11): encryption, tenant keys, no raw output or ticket text, retention, deletion, replay.

These tests need a PostgreSQL reachable through the usual PG* variables and run only when AIOPS_TEST_AGENT_PG=1 (and the
`cryptography` package for the encrypted cases). They create rows under throwaway tenant names and remove them afterwards.

    AIOPS_TEST_AGENT_PG=1 python -m unittest tests.test_agentruntime_store -v
"""
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agentruntime import crypto
from agentruntime.contracts import Action
from agentruntime.replay import replay
from agentruntime.runner import Runner
from agentruntime.store import RunStore, StoreError
from tests.agentruntime_helpers import (
    FakeClock, FakePrincipal, ScriptedPlanner, draft, finish_action, make_registry, policy, tenant_dir, tool_action,
)
from agentruntime.context import build_context

LIVE = os.getenv("AIOPS_TEST_AGENT_PG") == "1"
SECRET_EXCERPT = "UNIQUE-EVIDENCE-MARKER-4711 disk is full on host sapdb01"
TICKET_TEXT = "TICKET-BODY-MARKER-9001 users cannot log in"


def make_tenant():
    return "t_" + uuid.uuid4().hex[:10]


def make_run(tenant, run_id=None, marker=SECRET_EXCERPT):
    reg = make_registry(kb_fn=lambda ctx, query, top_k=3: [draft(ctx.tenant_id, "inc-1", marker)])
    ctx = build_context(tenant, "req", FakePrincipal(), "batch", config_dir=tenant_dir(tenant))
    planner = ScriptedPlanner([tool_action("incident_kb_search", query="disk"),
                               finish_action({"cause": "disk full", "status": "supported", "evidence": ["e1"]})])
    result = Runner(reg, clock=FakeClock()).run(ctx, policy(), {"ticket_text": TICKET_TEXT}, planner, run_id=run_id)
    return reg, ctx, result


@unittest.skipUnless(LIVE, "set AIOPS_TEST_AGENT_PG=1 with a reachable PostgreSQL to run")
class StoreBase(unittest.TestCase):
    encrypted = True

    @classmethod
    def setUpClass(cls):
        if cls.encrypted and crypto.AESGCM is None:
            raise unittest.SkipTest("the cryptography package is not installed")
        cls.cipher = crypto.Cipher(b"s" * 32) if cls.encrypted else None
        cls.store = RunStore(cipher=cls.cipher, encrypt=cls.encrypted)
        cls.store.ensure_schema()

    def setUp(self):
        self.tenants = []

    def tearDown(self):
        for t in self.tenants:
            self.store.delete_tenant(t)

    def tenant(self):
        t = make_tenant()
        self.tenants.append(t)
        return t

    def raw(self, sql, params=()):
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


class EncryptedStoreTests(StoreBase):
    def test_a_run_round_trips_and_replays_identically(self):
        t = self.tenant()
        reg, ctx, result = make_run(t)
        self.store.save_run(result, ticket_ref="INC1", ticket_text=TICKET_TEXT, request_id="req", retention_days=90)
        loaded = self.store.load_run(t, result.run_id)
        self.assertEqual(loaded.status, "supported")
        self.assertEqual([(h.cause, h.verified) for h in loaded.hypotheses], [("disk full", True)])
        self.assertEqual([e.content_hash for e in loaded.evidence], [e.content_hash for e in result.evidence])
        self.assertEqual(loaded.versions["policy_hash"], result.versions["policy_hash"])
        report = replay(loaded, reg, ctx, policy())
        self.assertTrue(report.valid and report.identical, report.stale_reasons + report.differences)

    def test_stored_evidence_and_events_are_ciphertext_not_plaintext(self):
        t = self.tenant()
        _reg, _ctx, result = make_run(t)
        self.store.save_run(result, ticket_ref="INC1")
        text = " ".join(str(r) for r in self.raw("SELECT item::text FROM agent_evidence WHERE tenant_id = %s", (t,)))
        text += " ".join(str(r) for r in self.raw("SELECT output_json::text FROM agent_run_events WHERE tenant_id = %s", (t,)))
        self.assertNotIn("UNIQUE-EVIDENCE-MARKER", text)
        self.assertNotIn("disk full", text)
        self.assertIn("enc", text)

    def test_a_ciphertext_moved_to_another_tenant_does_not_decrypt(self):
        a, b = self.tenant(), self.tenant()
        _r, _c, run_a = make_run(a, run_id="run-shared")
        _r, _c, run_b = make_run(b, run_id="run-shared")
        self.store.save_run(run_a)
        self.store.save_run(run_b)
        item_a = self.raw("SELECT item::text FROM agent_evidence WHERE tenant_id = %s AND evidence_id = 'e1'", (a,))[0][0]
        self.raw("UPDATE agent_evidence SET item = %s::jsonb WHERE tenant_id = %s AND evidence_id = 'e1'", (item_a, b)) if False else None
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE agent_evidence SET item = %s::jsonb WHERE tenant_id = %s AND evidence_id = 'e1'", (item_a, b))
        with self.assertRaises(Exception):
            self.store.load_run(b, "run-shared")


class SharedStoreTests(StoreBase):
    """These hold whether or not encryption is on; they run encrypted."""

    def test_a_tenant_never_reads_another_tenants_run(self):
        a, b = self.tenant(), self.tenant()
        _r, _c, run_a = make_run(a)
        self.store.save_run(run_a)
        self.assertIsNotNone(self.store.load_run(a, run_a.run_id))
        self.assertIsNone(self.store.load_run(b, run_a.run_id))
        with self.assertRaises(StoreError):
            self.store.load_run("", run_a.run_id)
        self.assertEqual((self.store.count_runs(a), self.store.count_runs(b)), (1, 0))

    def test_neither_the_ticket_text_nor_raw_output_is_persisted(self):
        t = self.tenant()
        _r, _c, result = make_run(t)
        self.store.save_run(result, ticket_ref="INC1", ticket_text=TICKET_TEXT)
        run_row = self.raw("SELECT row_to_json(agent_runs)::text FROM agent_runs WHERE tenant_id = %s", (t,))[0][0]
        self.assertNotIn("TICKET-BODY-MARKER", run_row)
        self.assertIn("INC1", run_row)                       # a reference
        self.assertTrue(self.raw("SELECT ticket_hash FROM agent_runs WHERE tenant_id = %s", (t,))[0][0])
        everything = " ".join(str(r) for r in self.raw("SELECT row_to_json(e)::text FROM agent_evidence e WHERE tenant_id = %s", (t,)))
        everything += " ".join(str(r) for r in self.raw("SELECT row_to_json(e)::text FROM agent_run_events e WHERE tenant_id = %s", (t,)))
        self.assertNotIn("TICKET-BODY-MARKER", everything)

    def test_secrets_never_reach_storage(self):
        t = self.tenant()
        _r, _c, result = make_run(t, marker="login failed password=hunter2 for user a.b@example.com")
        self.store.save_run(result)
        loaded = self.store.load_run(t, result.run_id)
        self.assertNotIn("hunter2", loaded.model_dump_json())
        self.assertNotIn("a.b@example.com", loaded.model_dump_json())

    def test_expired_runs_are_purged_with_their_evidence_and_events(self):
        t = self.tenant()
        _r, _c, result = make_run(t)
        self.store.save_run(result, retention_days=1)
        self.assertEqual(self.store.purge_expired(datetime.now(timezone.utc) + timedelta(hours=1)), 0)
        self.assertGreaterEqual(self.store.purge_expired(datetime.now(timezone.utc) + timedelta(days=2)), 1)
        self.assertEqual(self.store.count_runs(t), 0)
        self.assertEqual(self.raw("SELECT count(*) FROM agent_evidence WHERE tenant_id = %s", (t,))[0][0], 0)
        self.assertEqual(self.raw("SELECT count(*) FROM agent_run_events WHERE tenant_id = %s", (t,))[0][0], 0)

    def test_deleting_a_tenant_removes_only_that_tenants_runs(self):
        a, b = self.tenant(), self.tenant()
        for tenant in (a, b):
            _r, _c, result = make_run(tenant)
            self.store.save_run(result)
        self.assertEqual(self.store.delete_tenant(a), 1)
        self.assertEqual((self.store.count_runs(a), self.store.count_runs(b)), (0, 1))

    def test_a_record_whose_tool_version_changed_replays_as_stale(self):
        t = self.tenant()
        reg, ctx, result = make_run(t)
        self.store.save_run(result)
        loaded = self.store.load_run(t, result.run_id)
        reg._tools["incident_kb_search"][0].version = "2"
        report = replay(loaded, reg, ctx, policy())
        self.assertFalse(report.valid)

    def test_the_schema_can_be_applied_repeatedly(self):
        self.store.ensure_schema()
        self.store.ensure_schema()

    def test_one_run_id_per_tenant(self):
        t = self.tenant()
        _r, _c, result = make_run(t, run_id="dup")
        self.store.save_run(result)
        with self.assertRaises(Exception):
            self.store.save_run(result)


class PlaintextWaiverTests(StoreBase):
    encrypted = False

    def test_with_a_recorded_waiver_data_is_stored_in_plain_form_and_still_loads(self):
        t = self.tenant()
        _r, _c, result = make_run(t)
        self.store.save_run(result)
        text = " ".join(str(r) for r in self.raw("SELECT item::text FROM agent_evidence WHERE tenant_id = %s", (t,)))
        self.assertIn("plain", text)
        self.assertEqual(self.store.load_run(t, result.run_id).status, "supported")


@unittest.skipUnless(LIVE, "set AIOPS_TEST_AGENT_PG=1 with a reachable PostgreSQL to run")
class RefusalTests(unittest.TestCase):
    def test_persistence_is_refused_when_encryption_is_required_and_no_key_exists(self):
        with patch.dict(os.environ, {"AIOPS_AGENT_DATA_ENCRYPTION": "on"}), patch.object(crypto, "_load_key", return_value=None):
            with self.assertRaises(crypto.KeyMissing):
                RunStore()


if __name__ == "__main__":
    unittest.main()
