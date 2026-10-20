"""The live acceptance executor on fake pipelines and tools: identical held-out inputs for both arms, the hook's own decision and reconcile code, sanitised outputs, fail-closed exclusion."""
import copy
import contextlib
import json
import sys
import types
import unittest
from unittest.mock import patch

from agentruntime import release
from agentruntime.contracts import EvidenceDraft
from agentruntime.integration import hook
from agentruntime.integration.hook import LoopDecision
from planner_experiment import acceptance_executor as ax
from planner_experiment import batch_runner, evaluate_release2 as ev, review_layout
from planner_experiment.batch_runner import HeldOutFilter
from tests.agentruntime_helpers import make_ctx

HELD = [{"pool": "client_x", "tenant_id": "client_x", "ticket_id": "INC100", "text": "sapdb01 error 5 occurred", "host": "sapdb01"},
        {"pool": "client_x", "tenant_id": "client_x", "ticket_id": "INC200", "text": "ordersdb02 pool exhausted for the portal", "host": "ordersdb02"}]
CASES = [{"case_id": "acc:client_x:INC100", "tenant_id": "client_x", "ticket_text": "sapdb01 error 5 occurred", "source": "real"},
         {"case_id": "acc:client_x:INC200", "tenant_id": "client_x", "ticket_text": "ordersdb02 pool exhausted for the portal", "source": "real"}]


def response(causes, escalate=False, primary=0.3, rq=0.1, classification="disk_full", incidents=None, **extra):
    r = {"request_id": "r", "ticket": "t", "analysis": {"classification": classification, "probable_causes": causes, "escalate": escalate},
         "confidence": {"primary_confidence": primary, "overall_confidence": primary, "retrieval_quality": rq},
         "retrieved_incidents": incidents if incidents is not None else [{"id": "chk_ok", "title": "Tape job failed", "summary": "media error", "score": 0.5}],
         "linked_resolution": None, "linked_playbook": None}
    r.update(extra)
    return r


def tools(cause_text):
    def kb(ctx, *, cancel=None, timeout_s=None, query="", top_k=5):
        return [EvidenceDraft(tenant_id=ctx.tenant_id, source_type="incident", source_id=f"kb-{i}", provenance="tenant_data", relevance=0.7,
                              excerpt=f"File system full on the data host. Root Cause: {cause_text}; Resolution: purge", facts={"classification": "disk_full"}) for i in range(2)]
    return {"incident_kb_search": kb, "ticket_history_lookup": lambda ctx, *, cancel=None, timeout_s=None, **a: []}


class Fixture:
    def __init__(self, pipeline, cause_text="unrotated application logs"):
        self.filter = HeldOutFilter(HELD)
        self.calls = []
        self.pipeline_fn = pipeline
        self.registry = batch_runner.filtered_registry(self.filter, tools(cause_text))
        self.policy = release.policy_for("batch")
        self.clock_value = [0.0]

    def pipeline(self, case, use_fallback):
        self.calls.append((case["case_id"], use_fallback))
        self.clock_value[0] += 2.0
        return copy.deepcopy(self.pipeline_fn(case, use_fallback))

    def executor(self, **kw):
        return ax.LiveAcceptanceExecutor(self.pipeline, self.registry, lambda t, c: make_ctx(t), release.planner_factory, self.policy, clock=lambda: self.clock_value[0],
                                         held_out_filter=self.filter, **kw)


def weak(case, use_fallback):
    return response(["generic cause from the fallback"] if use_fallback else ["quota too small"], primary=0.3)


def confident(case, use_fallback):
    return response(["a well known cause"], primary=0.95, rq=0.9, linked_resolution={"id": 1})


class ArmsTests(unittest.TestCase):
    def test_a_confident_ticket_is_not_engaged_and_both_arms_are_the_same_answer(self):
        fx = Fixture(confident)
        out = fx.executor()(CASES[:1])
        rec = out["records"][0]
        self.assertEqual((rec["engaged"], rec["candidate_outcome"], rec["reconcile_outcome"]), (False, "not_engaged", "none"))
        self.assertEqual(rec["baseline"], rec["candidate"])
        self.assertEqual(fx.calls, [(CASES[0]["case_id"], False), (CASES[0]["case_id"], True)])

    def test_a_supported_run_replaces_the_fallback_and_is_reconciled_with_the_fallback_free_answer(self):
        fx = Fixture(weak)
        rec = fx.executor()(CASES[:1])["records"][0]
        self.assertTrue(rec["engaged"])
        self.assertEqual(rec["candidate_outcome"], "accepted_supported")
        self.assertEqual(rec["reconcile_outcome"], "merged")
        self.assertEqual(fx.calls, [(CASES[0]["case_id"], False), (CASES[0]["case_id"], True)])      # candidate pre-fallback state is fixed before baseline fallback
        self.assertIn("unrotated application logs", rec["candidate"]["causes"][0].lower() + " ".join(rec["candidate"]["causes"]).lower())
        self.assertIn("quota too small", " ".join(rec["candidate"]["causes"]).lower())         # the fallback-free deterministic cause survives
        self.assertNotIn("generic cause from the fallback", " ".join(rec["candidate"]["causes"]))
        self.assertEqual(rec["baseline"]["causes"], ["generic cause from the fallback"])

    def test_an_unsupported_run_leaves_the_baseline_answer_standing(self):
        fx = Fixture(weak, cause_text="")
        fx.registry = batch_runner.filtered_registry(fx.filter, {"incident_kb_search": lambda ctx, *, cancel=None, timeout_s=None, **a: [],
                                                                 "ticket_history_lookup": lambda ctx, *, cancel=None, timeout_s=None, **a: []})
        rec = fx.executor()(CASES[:1])["records"][0]
        self.assertEqual(rec["candidate_outcome"], "escalated_insufficient")
        self.assertEqual(rec["candidate"], rec["baseline"])
        self.assertEqual(fx.calls, [(CASES[0]["case_id"], False), (CASES[0]["case_id"], True)])

    def test_engagement_is_decided_from_the_pre_fallback_state(self):
        def changes_after_fallback(case, use_fallback):
            return confident(case, use_fallback) if use_fallback else weak(case, use_fallback)
        fx = Fixture(changes_after_fallback)
        rec = fx.executor()(CASES[:1])["records"][0]
        self.assertTrue(rec["engaged"])
        self.assertEqual(rec["candidate_outcome"], "accepted_supported")
        self.assertEqual(fx.calls, [(CASES[0]["case_id"], False), (CASES[0]["case_id"], True)])

    def test_a_conflicting_supported_run_is_recorded_as_a_conflict_and_still_replaces_the_fallback(self):
        def conflicting(case, use_fallback):
            return response(["quota too small"], classification="memory_pressure")
        fx = Fixture(conflicting)
        rec = fx.executor()(CASES[:1])["records"][0]
        self.assertEqual((rec["reconcile_outcome"], rec["candidate_outcome"]), ("conflict", "accepted_supported"))
        self.assertTrue(rec["candidate"]["escalate"])
        self.assertEqual(rec["candidate"]["classification"], "memory_pressure")           # no override of the deterministic classification

    def test_the_composition_equals_the_hooks_own_decision_and_reconcile(self):
        fx = Fixture(weak)
        rec = fx.executor()(CASES[:1])["records"][0]
        from agentruntime.contracts import AgentRunResult
        from agentruntime.integration import reconcile
        run = AgentRunResult(**rec["run"])
        merged, summary = reconcile.reconcile(weak(CASES[0], False), run)
        self.assertEqual(rec["reconcile_outcome"], summary["outcome"])
        self.assertEqual(rec["candidate"]["causes"], ax.summarise(merged)["causes"])
        self.assertEqual(LoopDecision(run=run, outcome=summary["outcome"], replaces_fallback=summary["outcome"] in ("merged", "conflict")).replaces_fallback, rec["candidate_outcome"] == "accepted_supported")
        self.assertTrue(hasattr(hook, "investigate_before_fallback"))


class ExclusionTests(unittest.TestCase):
    def test_the_agents_tools_never_see_the_held_out_tickets_and_the_count_is_reported(self):
        def kb(ctx, *, cancel=None, timeout_s=None, query="", top_k=5):
            return [EvidenceDraft(tenant_id=ctx.tenant_id, source_type="incident", source_id="chk_self", provenance="tenant_data", excerpt="sapdb01 error 5 occurred. the ticket itself", relevance=0.9),
                    EvidenceDraft(tenant_id=ctx.tenant_id, source_type="incident", source_id="chk_ok", provenance="tenant_data",
                                  excerpt="Tape job failed. Root Cause: media error; Resolution: replace", relevance=0.6, facts={"classification": "backup_failure"})]
        fx = Fixture(weak)
        fx.registry = batch_runner.filtered_registry(fx.filter, {"incident_kb_search": kb, "ticket_history_lookup": lambda ctx, *, cancel=None, timeout_s=None, **a: []})
        out = fx.executor()(CASES[:1])
        run = out["records"][0]["run"]
        sources = {e["source_id"] for e in run["evidence"]}
        self.assertNotIn("chk_self", sources)
        self.assertIn("chk_ok", sources)
        self.assertGreaterEqual(out["held_out_items_removed"], 1)

    def test_an_undecidable_item_stops_the_whole_run(self):
        fx = Fixture(weak)
        fx.registry = batch_runner.filtered_registry(fx.filter, {"incident_kb_search": lambda ctx, *, cancel=None, timeout_s=None, **a: [
            EvidenceDraft(tenant_id=ctx.tenant_id, source_type="incident", source_id="x", provenance="tenant_data", excerpt="   ")],
            "ticket_history_lookup": lambda ctx, *, cancel=None, timeout_s=None, **a: []})
        with self.assertRaisesRegex(ax.ExecutorError, "exclusion is not proven"):
            fx.executor()(CASES[:1])
        self.assertGreaterEqual(fx.filter.undecided, 1)

    def test_a_corpus_that_changes_during_the_run_invalidates_it(self):
        fx = Fixture(weak)
        counts = iter([{"chunks": 100}, {"chunks": 101}])
        with self.assertRaisesRegex(ax.ExecutorError, "corpus changed"):
            fx.executor(fingerprint=lambda: next(counts))(CASES[:1])
        stable = Fixture(weak).executor(fingerprint=lambda: {"chunks": 100})(CASES[:1])
        self.assertEqual(stable["corpus_fingerprint"], {"chunks": 100})

    def test_the_live_fingerprint_changes_when_content_changes_without_a_row_count_change(self):
        class Cursor:
            def __init__(self, worlds):
                self.worlds, self.rows, self.n = worlds, [], 0
            def execute(self, sql):
                table = "knowledge" if "client_knowledge_chunks" in sql else "tickets"
                self.rows = [{"row_json": text} for text in self.worlds[self.n][table]]
                self.n += 1
            def __iter__(self):
                return iter(self.rows)

        worlds = [{"knowledge": ['{"id":1,"text":"before"}'], "tickets": ['{"id":2}']},
                  {"knowledge": ['{"id":1,"text":"after"}'], "tickets": ['{"id":2}']}]

        def fingerprint(world):
            @contextlib.contextmanager
            def db_cursor(**_kw):
                yield None, Cursor([world, world])
            fake = types.SimpleNamespace(db_cursor=db_cursor)
            with patch.dict(sys.modules, {"postgres_store": fake}):
                return ax.corpus_fingerprint()

        before, after = fingerprint(worlds[0]), fingerprint(worlds[1])
        self.assertEqual(before["knowledge_chunks"]["rows"], after["knowledge_chunks"]["rows"])
        self.assertNotEqual(before["knowledge_chunks"]["sha256"], after["knowledge_chunks"]["sha256"])


class PipelineLeakGuardTests(unittest.TestCase):
    def test_a_rag_context_that_contains_a_held_out_ticket_invalidates_the_run(self):
        fx = Fixture(lambda case, fb: response(["x"], rag_context="Earlier note: sapdb01 error 5 occurred and was fixed by a reboot"))
        with self.assertRaisesRegex(ax.ExecutorError, "RAG context"):
            fx.executor()(CASES[:1])

    def test_a_stored_feedback_correction_for_the_ticket_invalidates_the_run(self):
        fx = Fixture(lambda case, fb: response(["x"], feedback_override={"corrected_classification": "disk_full"}))
        with self.assertRaisesRegex(ax.ExecutorError, "feedback correction"):
            fx.executor()(CASES[:1])

    def test_an_unrelated_rag_context_is_fine(self):
        fx = Fixture(lambda case, fb: response(["x"], primary=0.95, rq=0.9, linked_resolution={"id": 1}, rag_context="Runbook: how to rotate logs on the data hosts"))
        self.assertEqual(len(fx.executor()(CASES[:1])["records"]), 1)

    def test_app_py_binds_its_own_copy_of_retrieve_incidents_and_the_live_scope_patches_it(self):
        import app
        import retrieval_engine
        self.assertTrue(hasattr(app, "retrieve_incidents"))                             # imported by name at load: patching only retrieval_engine would not reach it
        filt = HeldOutFilter(HELD)
        before = (app.retrieve_incidents, retrieval_engine.retrieve_incidents)
        with ax.live_scope(filt):
            self.assertIsNot(app.retrieve_incidents, before[0])
            self.assertIsNot(retrieval_engine.retrieve_incidents, before[1])
        self.assertEqual((app.retrieve_incidents, retrieval_engine.retrieve_incidents), before)


class OutputTests(unittest.TestCase):
    def test_outputs_are_sanitised_and_bounded(self):
        resp = response(["Contact admin at john.doe@example.com about the Bearer abcdefghijklmnopqrstuvwxyz0123456789 token"], incidents=[
            {"id": str(i), "title": f"Incident {i} " + "x" * 600, "summary": "s"} for i in range(9)])
        s = ax.summarise(resp)
        self.assertNotIn("john.doe@example.com", json.dumps(s))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", json.dumps(s))
        self.assertLessEqual(len(s["evidence"]), 10)
        self.assertTrue(all(len(e) <= 301 for e in s["evidence"]))

    def test_the_review_items_are_deterministic_and_feed_the_blinded_layout(self):
        fx = Fixture(weak)
        out = fx.executor()(CASES)
        items = ax.review_items(out["records"], CASES)
        again = ax.review_items(list(reversed(out["records"])), CASES)
        self.assertEqual(items, again)
        packets, key = review_layout.build_packets(items, seed=4)
        self.assertEqual(len(packets), 2)
        self.assertTrue(review_layout.layout_is_identical(packets))
        self.assertIn(CASES[0]["ticket_text"], json.dumps(packets[0]["evidence_packet"] + packets[1]["evidence_packet"]))

    def test_the_records_feed_the_analysis_code(self):
        fx = Fixture(weak)
        out = fx.executor()(CASES)
        for rec in out["records"]:
            self.assertIn(rec["candidate_outcome"], ev.OUTCOMES)
        scored = ev.score_candidate({"outcome": "not_engaged", "grounded": True, "unsupported": False, "class_ok": True, "escalated": True, "schema_valid": True, "timeout": False, "seconds": 1})
        self.assertEqual((scored["grounded"], scored["escalated"]), (False, True))      # not engaged: never grounded by the loop; escalates only if the deterministic answer did
        self.assertEqual(ax.candidate_outcome(None, "none", False), "not_engaged")

    def test_latency_is_the_loop_plus_the_pipeline_that_produced_the_answer(self):
        fx = Fixture(weak)
        rec = fx.executor()(CASES[:1])["records"][0]
        self.assertEqual(rec["baseline_seconds"], 2.0)
        self.assertGreaterEqual(rec["candidate_seconds"], rec["loop_seconds"])
        conf = Fixture(confident).executor()(CASES[:1])["records"][0]
        self.assertEqual(conf["candidate_seconds"], conf["baseline_seconds"])


if __name__ == "__main__":
    unittest.main()
