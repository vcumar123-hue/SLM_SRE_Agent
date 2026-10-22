"""
R2-0: run storage against a real PostgreSQL (design 11): encryption, tenant keys, no raw output or ticket text, retention, deletion, replay.

These tests need a PostgreSQL reachable through the usual PG* variables and run only when AIOPS_TEST_AGENT_PG=1 (and the
`cryptography` package for the encrypted cases). They create rows under throwaway tenant names and remove them afterwards.

    AIOPS_TEST_AGENT_PG=1 python -m unittest tests.test_agentruntime_store -v
"""
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agentruntime import crypto
from agentruntime.contracts import Action
from agentruntime.replay import replay
from agentruntime.runner import Runner, build_versions
from agentruntime.store import RunStore, StoreError
from tests.agentruntime_helpers import (
    FakeClock, FakePrincipal, ScriptedPlanner, batch_env, draft, finish_action, make_registry, policy, real_waiver, tenant_dir,
    tool_action, write_waiver_file,
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
    with batch_env():
        result = Runner(reg, clock=FakeClock()).run_batch(ctx, policy(), {"ticket_text": TICKET_TEXT}, planner, run_id=run_id)
    return reg, ctx, result


def current_manifest(reg, pol=None):
    return build_versions(reg, ScriptedPlanner([]), pol or policy())


@unittest.skipUnless(LIVE, "set AIOPS_TEST_AGENT_PG=1 with a reachable PostgreSQL to run")
class StoreBase(unittest.TestCase):
    encrypted = True

    @classmethod
    def setUpClass(cls):
        if cls.encrypted and crypto.AESGCM is None:
            raise unittest.SkipTest("the cryptography package is not installed")
        cls.cipher = crypto.Cipher(b"s" * 32) if cls.encrypted else None
        cls.store = (RunStore(cipher=cls.cipher) if cls.encrypted
                     else RunStore(waiver_loader=lambda: crypto.load_waiver(write_waiver_file())))
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
        report = replay(loaded, reg, ctx, policy(), current_manifest(reg))
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
        report = replay(loaded, reg, ctx, policy(), current_manifest(reg))
        self.assertFalse(report.valid)

    def test_retention_is_clamped_by_the_store_and_foreign_evidence_is_refused(self):
        t = self.tenant()
        _r, _c, result = make_run(t)
        self.store.save_run(result, retention_days=99999)
        days = self.raw("SELECT extract(epoch FROM (expires_at - created_at)) / 86400 FROM agent_runs WHERE tenant_id = %s", (t,))[0][0]
        self.assertLessEqual(float(days), 3650.5)
        t2 = self.tenant()
        _r, _c, other = make_run(t2)
        self.store.save_run(other, retention_days=0)
        days = self.raw("SELECT extract(epoch FROM (expires_at - created_at)) / 86400 FROM agent_runs WHERE tenant_id = %s", (t2,))[0][0]
        self.assertGreaterEqual(float(days), 0.99)
        t3 = self.tenant()
        _r, _c, mixed = make_run(t3)
        foreign = mixed.evidence[0].model_copy(update={"tenant_id": t})
        with self.assertRaises(StoreError):
            self.store.save_run(mixed.model_copy(update={"evidence": [foreign]}))
        self.assertEqual(self.store.count_runs(t3), 0)

    def test_the_stored_manifest_records_how_the_data_is_protected(self):
        t = self.tenant()
        _r, _c, result = make_run(t)
        self.store.save_run(result)
        versions = self.store.load_run(t, result.run_id).versions
        self.assertEqual(versions["encryption"]["mode"], "aes-256-gcm")
        self.assertNotIn("record_hash", versions["encryption"])
        for key in ("schema", "runtime", "tools", "prompt", "ruleset", "model", "policy_hash"):
            self.assertIn(key, versions)

    def test_the_schema_can_be_applied_repeatedly(self):
        self.store.ensure_schema()
        self.store.ensure_schema()

    def test_one_run_id_per_tenant(self):
        t = self.tenant()
        _r, _c, result = make_run(t, run_id="dup")
        self.store.save_run(result)
        with self.assertRaises(Exception):
            self.store.save_run(result)


class RequestLookupTests(StoreBase):
    """`load_run_by_request` (design: operator-visible shadow inspection, section 22.6): the Incident Console's correlation path from
    an /analyze response's own request_id back to the stored run, tenant-scoped like every other read here."""

    def test_lookup_succeeds_for_the_correct_tenant_and_request(self):
        t = self.tenant()
        _r, _c, result = make_run(t)
        self.store.save_run(result, ticket_ref="INC1", request_id="req-abc")
        found = self.store.load_run_by_request(t, "req-abc")
        self.assertIsNotNone(found)
        self.assertEqual(found.run_id, result.run_id)
        self.assertEqual(found.status, "supported")

    def test_another_tenant_cannot_retrieve_the_run(self):
        a, b = self.tenant(), self.tenant()
        _r, _c, result = make_run(a)
        self.store.save_run(result, request_id="req-shared-name")
        self.assertIsNotNone(self.store.load_run_by_request(a, "req-shared-name"))
        self.assertIsNone(self.store.load_run_by_request(b, "req-shared-name"))

    def test_an_unknown_request_id_is_none_not_an_error(self):
        t = self.tenant()
        self.assertIsNone(self.store.load_run_by_request(t, "never-stored"))

    def test_missing_identifiers_are_refused(self):
        t = self.tenant()
        with self.assertRaises(StoreError):
            self.store.load_run_by_request("", "req-1")
        with self.assertRaises(StoreError):
            self.store.load_run_by_request(t, "")
        with self.assertRaises(StoreError):
            self.store.load_run_by_request("", "")

    def test_multiple_runs_under_the_same_request_id_return_the_newest(self):
        t = self.tenant()
        _r, _c, older = make_run(t, run_id="run-older")
        _r, _c, newer = make_run(t, run_id="run-newer")
        self.store.save_run(older, request_id="req-rerun")
        self.store.save_run(newer, request_id="req-rerun")
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE agent_runs SET created_at = created_at - interval '1 minute' WHERE tenant_id = %s AND run_id = %s", (t, "run-older"))
        found = self.store.load_run_by_request(t, "req-rerun")
        self.assertEqual(found.run_id, "run-newer")

    def test_the_request_id_column_is_plaintext_but_the_run_is_still_decrypted_only_through_the_store(self):
        t = self.tenant()
        _r, _c, result = make_run(t, marker=SECRET_EXCERPT)
        self.store.save_run(result, request_id="req-plain-lookup")
        row = self.raw("SELECT request_id FROM agent_runs WHERE tenant_id = %s", (t,))[0][0]
        self.assertEqual(row, "req-plain-lookup")                           # the lookup key itself is never secret
        evidence_text = " ".join(str(r) for r in self.raw("SELECT item::text FROM agent_evidence WHERE tenant_id = %s", (t,)))
        self.assertNotIn("UNIQUE-EVIDENCE-MARKER", evidence_text)           # but the run content stored alongside it still is
        found = self.store.load_run_by_request(t, "req-plain-lookup")
        self.assertIn("UNIQUE-EVIDENCE-MARKER", found.evidence[0].excerpt)  # decrypted only by going through the store


class SummarizeRunTests(unittest.TestCase):
    """`summarize_run` is pure: no I/O, callable without a database. Bounds every free-text and list field."""

    def result(self, **over):
        from agentruntime.contracts import AgentRunResult, AgentStep, EvidenceItem, Hypothesis
        from datetime import datetime, timezone
        steps = over.pop("steps", [
            AgentStep(n=1, kind="tool", tool="ticket_history_lookup", result_summary="0 evidence item(s)", evidence_added=[], duration_ms=30),
            AgentStep(n=2, kind="tool", tool="incident_kb_search", result_summary="1 evidence item(s)", evidence_added=["e1"], duration_ms=90),
        ])
        hyps = over.pop("hypotheses", [Hypothesis(cause="disk full on the data volume", status="supported", evidence=["e1"], verified=True)])
        evidence = over.pop("evidence", [EvidenceItem(evidence_id="e1", run_id="r1", tenant_id="t1", tool="incident_kb_search", tool_version="1",
                                                      source_type="incident", source_id="inc-1", provenance="tenant_data", retrieved_at=datetime.now(timezone.utc),
                                                      sanitization="clean", relevance=0.8, excerpt="file system full", content_hash="h")])
        base = dict(run_id="r1", tenant_id="t1", status="supported", reason="a verified hypothesis is supported by evidence", hypotheses=hyps,
                    evidence=evidence, steps=steps, counters={"steps": 2, "tool_calls": 2, "model_calls": 0, "elapsed_s": 0.121, "secret_internal_counter": 999},
                    versions={"ruleset": "rules-1", "model": None, "tools": {"incident_kb_search": "1"}, "policy_hash": "abc123"}, mode="shadow", flags=[])
        base.update(over)
        return AgentRunResult(**base)

    def test_the_summary_has_no_encryption_metadata_ticket_text_or_the_full_counters_dict(self):
        from agentruntime.store import summarize_run
        summary = summarize_run(self.result())
        blob = json.dumps(summary)
        self.assertNotIn("encryption", summary)
        self.assertNotIn("policy_hash", blob)
        self.assertNotIn("secret_internal_counter", blob)
        self.assertEqual(summary["counters"], {"steps": 2, "tool_calls": 2, "model_calls": 0, "elapsed_s": 0.121})

    def test_tool_steps_carry_only_name_and_counts_never_inputs_or_raw_output(self):
        from agentruntime.store import summarize_run
        summary = summarize_run(self.result())
        self.assertEqual(summary["tool_count"], 2)
        self.assertEqual([t["name"] for t in summary["tools"]], ["ticket_history_lookup", "incident_kb_search"])
        for t in summary["tools"]:
            self.assertEqual(set(t), {"step", "name", "ok", "evidence_added", "duration_ms"})

    def test_hypotheses_and_reason_are_length_bounded(self):
        from agentruntime.store import summarize_run, SUMMARY_MAX_CAUSE_CHARS, SUMMARY_MAX_REASON_CHARS, SUMMARY_MAX_HYPOTHESES
        from agentruntime.contracts import Hypothesis
        long_cause = "x" * 5000
        many = [Hypothesis(cause=f"cause {i}", status="weak", evidence=[]) for i in range(20)]
        many[0] = Hypothesis(cause=long_cause, status="weak", evidence=[])
        summary = summarize_run(self.result(hypotheses=many, reason="y" * 5000))
        self.assertLessEqual(len(summary["hypotheses"]), SUMMARY_MAX_HYPOTHESES)
        self.assertLessEqual(len(summary["hypotheses"][0]["cause"]), SUMMARY_MAX_CAUSE_CHARS)
        self.assertLessEqual(len(summary["reason"]), SUMMARY_MAX_REASON_CHARS)

    def test_evidence_count_is_a_count_only(self):
        from agentruntime.store import summarize_run
        summary = summarize_run(self.result())
        self.assertEqual(summary["evidence_count"], 1)
        self.assertNotIn("file system full", json.dumps(summary))           # the excerpt itself is never in the summary

    def test_planner_is_the_frozen_release_planner(self):
        from agentruntime.store import summarize_run
        from agentruntime.release import RELEASE_PLANNER
        self.assertEqual(summarize_run(self.result())["planner"], RELEASE_PLANNER)


class PlaintextWaiverTests(StoreBase):
    encrypted = False

    def test_with_a_recorded_waiver_data_is_stored_in_plain_form_and_still_loads(self):
        t = self.tenant()
        _r, _c, result = make_run(t)
        self.store.save_run(result)
        text = " ".join(str(r) for r in self.raw("SELECT item::text FROM agent_evidence WHERE tenant_id = %s", (t,)))
        self.assertIn("plain", text)
        self.assertEqual(self.store.load_run(t, result.run_id).status, "supported")
        encryption = self.store.load_run(t, result.run_id).versions["encryption"]
        self.assertEqual(encryption["mode"], "waived")
        self.assertEqual(encryption["reference"], "POLICY-7")
        self.assertEqual(len(encryption["record_hash"]), 64)             # the waiver record travels with the run for audit


@unittest.skipUnless(LIVE, "set AIOPS_TEST_AGENT_PG=1 with a reachable PostgreSQL to run")
class RefusalTests(unittest.TestCase):
    def test_persistence_is_refused_when_encryption_is_required_and_no_key_exists(self):
        with patch.object(crypto, "_load_key", return_value=None), patch.object(crypto, "load_waiver", return_value=None):
            with self.assertRaises(crypto.KeyMissing):
                RunStore()


if __name__ == "__main__":
    unittest.main()
