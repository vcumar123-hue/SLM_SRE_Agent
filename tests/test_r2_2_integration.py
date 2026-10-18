"""R2-2 integration: mode gating and rollback, engagement, the shadow queue, reconciliation, the hook, the confidence input, and `off` parity."""
import copy
import json
import pathlib
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agentruntime import config as runtime_config
from agentruntime import release
from agentruntime.contracts import AgentRunResult, EvidenceItem, Hypothesis
from agentruntime.integration import engagement, gate, hook, reconcile
from agentruntime.integration.shadow import IN_FLIGHT, ShadowJob, ShadowQueue, analysis_in_flight

KEY_OK = MagicMock()


def write_json(directory, name, value):
    p = pathlib.Path(directory) / name
    p.write_text(json.dumps(value), encoding="utf-8")
    return p


def valid_decision(tenant="client_x", mode="shadow", **kw):
    return {"tenant_id": tenant, "mode": mode, "decided_by": "Pat Owner", "date": "2026-11-01", "reference": "DEC-1", **kw}


def passing_acceptance(**kw):
    return {"passed": True, "gates": {f"G{i}": True for i in range(1, 9)}, "acceptance_set_hash": "abc123", "decided_by": "Pat Owner", **kw}


class Quiet(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="r22-"))
        gate.clear_rollback()
        gate.reset_cache()
        patcher = patch.object(runtime_config.crypto, "get_cipher", return_value=KEY_OK)             # a usable encryption setup
        patcher.start()
        self.addCleanup(patcher.stop)
        from tests.agentruntime_helpers import tenant_dir
        env = patch.dict("os.environ", {"AIOPS_TENANT_CONFIG_DIR": str(tenant_dir("client_x", "client_y"))})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(gate.clear_rollback)
        self.addCleanup(gate.reset_cache)

    def mode(self, tenant="client_x", env="shadow", decisions=None, acceptance=None, freeze=True):
        d = write_json(self.dir, "d.json", {"decisions": decisions if decisions is not None else [valid_decision(tenant)]})
        a = write_json(self.dir, "a.json", acceptance if acceptance is not None else {})
        with patch.object(gate, "freeze_problems", return_value=[] if freeze else ["agentruntime/runner.py: changed"]):
            return gate.effective_mode(tenant, env_value=env, decisions_path=d, acceptance_path=a)


class GateTests(Quiet):
    def test_everything_defaults_to_off(self):
        for env in (None, "", "   ", "off", "maybe", "ON ", "enabled"):
            mode, _ = self.mode(env=env) if env != "ON " else self.mode(env="ON ")
            if env == "ON ":
                continue                                               # 'on' is valid but blocked below; see its own test
            self.assertEqual(mode, "off", env)

    def test_a_tenant_with_no_recorded_decision_is_off_even_when_the_global_mode_is_shadow(self):
        self.assertEqual(self.mode(decisions=[])[0], "off")
        self.assertEqual(self.mode(tenant="client_y", decisions=[valid_decision("client_x")])[0], "off")
        self.assertIn("no recorded deployment decision", self.mode(decisions=[])[1])

    def test_a_recorded_decision_enables_shadow_for_that_tenant_only(self):
        self.assertEqual(self.mode()[0], "shadow")
        self.assertEqual(self.mode(tenant="client_y", decisions=[valid_decision("client_x"), valid_decision("client_y")])[0], "shadow")

    def test_invalid_decisions_enable_nothing(self):
        for bad in (valid_decision(decided_by=""), valid_decision(decided_by="owner"), valid_decision(decided_by="TBD"), valid_decision(date=""), valid_decision(reference=" "),
                    valid_decision(mode="off"), valid_decision(mode="batch"), valid_decision(tenant=""), "not a dict", None, 5):
            self.assertEqual(self.mode(decisions=[bad])[0], "off", str(bad))
        (self.dir / "d.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(gate.read_decisions(self.dir / "d.json"), {})
        self.assertEqual(gate.read_decisions(self.dir / "missing.json"), {})

    def test_the_decision_caps_the_global_mode_and_the_global_mode_caps_the_decision(self):
        self.assertEqual(self.mode(env="shadow", decisions=[valid_decision(mode="on")], acceptance=passing_acceptance())[0], "shadow")
        self.assertEqual(self.mode(env="on", decisions=[valid_decision(mode="shadow")], acceptance=passing_acceptance())[0], "shadow")

    def test_on_is_blocked_until_r2_3_has_passed(self):
        on = [valid_decision(mode="on")]
        mode, why = self.mode(env="on", decisions=on)
        self.assertEqual(mode, "off")
        self.assertIn("R2-3", why)
        self.assertEqual(self.mode(env="on", decisions=on, acceptance=passing_acceptance())[0], "on")
        for bad in (passing_acceptance(passed=False), passing_acceptance(gates={**{f"G{i}": True for i in range(1, 9)}, "G6": False}),
                    passing_acceptance(gates={f"G{i}": True for i in range(1, 8)}), passing_acceptance(acceptance_set_hash=""), passing_acceptance(decided_by="x"),
                    passing_acceptance(gates={f"G{i}": "yes" for i in range(1, 9)}), []):
            self.assertEqual(self.mode(env="on", decisions=on, acceptance=bad)[0], "off", str(bad)[:60])

    def test_a_frozen_core_that_no_longer_matches_turns_the_loop_off(self):
        mode, why = self.mode(freeze=False)
        self.assertEqual(mode, "off")
        self.assertIn("release freeze", why)

    def test_the_encryption_requirement_still_applies(self):
        with patch.object(runtime_config.crypto, "get_cipher", side_effect=runtime_config.crypto.KeyMissing("no key")):
            self.assertEqual(self.mode()[0], "off")

    def test_resolution_never_raises(self):
        with patch.object(runtime_config, "resolve_mode", side_effect=RuntimeError("boom")):
            self.assertEqual(gate.effective_mode("client_x", env_value="shadow")[0], "off")
        self.assertEqual(gate.effective_mode(None, env_value="shadow", decisions_path=self.dir / "nope.json")[0], "off")


class RollbackTests(Quiet):
    def test_the_environment_kill_switch_forces_off(self):
        self.assertEqual(self.mode()[0], "shadow")
        with patch.dict("os.environ", {gate.KILL_ENV: "1"}):
            mode, why = self.mode()
            self.assertEqual(mode, "off")
            self.assertIn("kill switch", why)
            self.assertEqual(gate.global_mode("shadow")[0], "off")

    def test_the_in_process_rollback_forces_off_for_every_tenant_and_drops_queued_runs(self):
        dropped = []
        gate.on_rollback(lambda: dropped.append(1))
        self.assertEqual(self.mode()[0], "shadow")
        gate.rollback("operator pulled the switch")
        self.assertEqual(self.mode()[0], "off")
        self.assertEqual(self.mode(tenant="client_y", decisions=[valid_decision("client_y")])[0], "off")
        self.assertEqual(dropped, [1])
        gate.clear_rollback()
        self.assertEqual(self.mode()[0], "shadow")

    def test_a_failing_rollback_callback_does_not_stop_the_rollback(self):
        def bad():
            raise RuntimeError("x")
        gate.on_rollback(bad)
        gate.rollback()
        self.assertEqual(self.mode()[0], "off")

    def test_rollback_empties_the_real_shadow_queue(self):
        gate_done = threading.Event()
        q = ShadowQueue(lambda job: gate_done.wait(5), requires_idle=False)
        gate.on_rollback(q.clear)
        q.submit(ShadowJob(None, None, {}, "r1"))
        time.sleep(0.2)                                                     # r1 is running
        for i in range(3):
            q.submit(ShadowJob(None, None, {}, f"q{i}"))
        gate.rollback()
        self.assertEqual(q.stats()["dropped_rollback"], 3)
        gate_done.set()
        q.wait_until_idle()
        q.stop()


def item(eid, classification="disk_full", relevance=0.8, run="run1", tenant="client_x"):
    return EvidenceItem(evidence_id=eid, run_id=run, tenant_id=tenant, tool="incident_kb_search", tool_version="1", source_type="incident", source_id=f"src-{eid}",
                        provenance="tenant_data", retrieved_at=datetime.now(timezone.utc), sanitization="clean", relevance=relevance, excerpt="Root cause: log growth",
                        facts={"classification": classification}, content_hash="h")


def run_result(status="supported", hyps=None, evidence=None, grounding=0.8):
    evidence = evidence if evidence is not None else [item("e1"), item("e2")]
    hyps = hyps if hyps is not None else [Hypothesis(cause="log growth on the data volume", status="supported", evidence=["e1", "e2"], verified=True)]
    return AgentRunResult(run_id="run1", tenant_id="client_x", status=status, reason="r", hypotheses=hyps, evidence=evidence, mode="on", evidence_grounding=grounding)


def response(**over):
    r = {"request_id": "req1", "ticket": "disk full on app01", "tenant_config": {"tenant_id": "client_x"},
         "analysis": {"classification": "disk_full", "probable_causes": ["disk quota too small", "log growth on the data volume"], "escalate": False},
         "confidence": {"overall_confidence": 0.4, "retrieval_quality": 0.2}, "retrieved_incidents": [], "linked_resolution": None, "linked_playbook": None}
    r.update(over)
    return r


class EngagementTests(unittest.TestCase):
    T = dict(confidence_threshold=0.65, min_retrieval_score=0.25)

    def test_each_reason_engages_on_its_own(self):
        solid = response(confidence={"overall_confidence": 0.9, "retrieval_quality": 0.9}, linked_resolution={"id": 1})
        self.assertEqual(engagement.should_engage(solid, **self.T), (False, []))
        for change, reason in ((dict(confidence={"overall_confidence": 0.5, "retrieval_quality": 0.9}), "confidence_below_threshold"),
                               (dict(confidence={"overall_confidence": 0.9, "retrieval_quality": 0.1}), "retrieval_quality_below_minimum"),
                               (dict(linked_resolution=None, linked_playbook=None), "no_reusable_knowledge"),
                               (dict(retrieved_incidents=[{"score": 0.8, "classification": "disk_full"}, {"score": 0.78, "classification": "memory_pressure"}]), "conflicting_evidence")):
            engaged, reasons = engagement.should_engage({**solid, **change}, **self.T)
            self.assertTrue(engaged and reason in reasons, reason)

    def test_agreeing_or_distant_or_low_leaders_are_not_a_conflict(self):
        f = engagement.conflicting_leaders
        self.assertFalse(f([{"score": 0.8, "classification": "a"}, {"score": 0.7, "classification": "b"}], 0.25))
        self.assertFalse(f([{"score": 0.8, "classification": "a"}, {"score": 0.79, "classification": "a"}], 0.25))
        self.assertFalse(f([{"score": 0.2, "classification": "a"}, {"score": 0.19, "classification": "b"}], 0.25))
        self.assertFalse(f([{"score": 0.8, "classification": "a"}, {"score": 0.79, "classification": "unknown"}], 0.25))
        self.assertFalse(f([], 0.25))

    def test_no_ticket_no_engagement(self):
        self.assertFalse(engagement.should_engage({"ticket": " "}, **self.T)[0])
        self.assertFalse(engagement.should_engage("nonsense", **self.T)[0])


class ReconcileTests(unittest.TestCase):
    def test_a_supported_run_with_the_same_classification_is_merged_loop_causes_first(self):
        original = response()
        before = copy.deepcopy(original)
        out, summary = reconcile.reconcile(original, run_result())
        self.assertEqual(original, before)                                           # the input is never changed
        self.assertEqual(summary["outcome"], "merged")
        self.assertEqual(out["analysis"]["probable_causes"], ["log growth on the data volume", "disk quota too small"])
        self.assertEqual(out["investigation"]["cause_evidence"], {"log growth on the data volume": ["e1", "e2"]})
        self.assertEqual(out["analysis"]["classification"], "disk_full")
        self.assertEqual(out["confidence"]["evidence_grounding"], 0.8)
        self.assertEqual(out["investigation"]["deterministic"]["probable_causes"], before["analysis"]["probable_causes"])

    def test_a_different_classification_is_a_conflict_the_deterministic_result_is_kept_and_an_operator_decides(self):
        run = run_result(evidence=[item("e1", "memory_pressure"), item("e2", "memory_pressure")])
        out, summary = reconcile.reconcile(response(), run)
        self.assertEqual(summary["outcome"], "conflict")
        self.assertEqual(out["analysis"]["classification"], "disk_full")                  # no automatic override
        self.assertEqual(out["analysis"]["probable_causes"], response()["analysis"]["probable_causes"])
        self.assertTrue(out["analysis"]["escalate"])
        conflict = out["investigation"]["conflict"]
        self.assertEqual((conflict["deterministic_classification"], conflict["loop_classification"], conflict["resolution"]), ("disk_full", "memory_pressure", "operator"))

    def test_a_run_that_is_not_supported_leaves_the_deterministic_result_as_it_was(self):
        for status in ("escalated_insufficient", "escalated_conflict", "failed_model", "failed_tool", "timeout_deadline", "budget_exhausted", "denied_policy"):
            out, summary = reconcile.reconcile(response(), run_result(status=status, hyps=[Hypothesis(cause="weak idea", status="weak", evidence=["e1"])]))
            self.assertEqual(summary["outcome"], "kept", status)
            self.assertEqual(out["analysis"], response()["analysis"], status)
            self.assertEqual(out["investigation"]["status"], status)

    def test_an_unverified_supported_hypothesis_is_not_enough(self):
        run = run_result(hyps=[Hypothesis(cause="log growth", status="supported", evidence=["e1"], verified=False)])
        self.assertEqual(reconcile.reconcile(response(), run)[1]["outcome"], "kept")

    def test_a_run_with_no_real_classification_merges_without_touching_the_classification(self):
        run = run_result(evidence=[item("e1", "unknown"), item("e2", "")])
        out, summary = reconcile.reconcile(response(), run)
        self.assertEqual((summary["outcome"], summary["loop_classification"]), ("merged", None))
        self.assertEqual(out["analysis"]["classification"], "disk_full")

    def test_a_rejected_cause_is_not_carried_over_and_the_cause_list_is_capped(self):
        det = ["old cause %d" % i for i in range(8)] + ["rejected cause"]
        hyps = [Hypothesis(cause="rejected cause", status="rejected", evidence=["e1"]), Hypothesis(cause="loop cause", status="supported", evidence=["e1", "e2"], verified=True)]
        out, _ = reconcile.reconcile(response(analysis={"classification": "disk_full", "probable_causes": det}), run_result(hyps=hyps))
        causes = out["analysis"]["probable_causes"]
        self.assertEqual(causes[0], "loop cause")
        self.assertNotIn("rejected cause", causes)
        self.assertLessEqual(len(causes), reconcile.MAX_CAUSES)

    def test_a_response_without_an_analysis_object_is_handled(self):
        out, summary = reconcile.reconcile({"ticket": "t"}, run_result())
        self.assertIn(summary["outcome"], ("merged", "kept", "conflict"))
        self.assertIn("investigation", out)

    def test_the_investigation_block_is_json_serialisable_and_carries_the_audit_trail(self):
        block = reconcile.reconcile(response(), run_result())[0]["investigation"]
        json.dumps(block)
        self.assertEqual({"run_id", "status", "reason", "steps", "evidence", "hypotheses", "counters", "versions", "evidence_grounding", "deterministic"} <= set(block), True)


class ShadowQueueTests(unittest.TestCase):
    def test_submit_returns_at_once_and_the_run_happens_later(self):
        ran = threading.Event()
        q = ShadowQueue(lambda job: (time.sleep(0.2), ran.set()), requires_idle=False)
        start = time.monotonic()
        self.assertTrue(q.submit(ShadowJob(None, None, {}, "r1")))
        self.assertLess(time.monotonic() - start, 0.1)
        self.assertTrue(ran.wait(3))
        q.wait_until_idle()
        self.assertEqual(q.stats()["completed"], 1)
        q.stop()

    def test_a_full_queue_drops_the_new_run_and_counts_it(self):
        release_run = threading.Event()
        q = ShadowQueue(lambda job: release_run.wait(5), max_queue=2, requires_idle=False)
        q.submit(ShadowJob(None, None, {}, "running"))
        time.sleep(0.2)
        self.assertTrue(q.submit(ShadowJob(None, None, {}, "a")))
        self.assertTrue(q.submit(ShadowJob(None, None, {}, "b")))
        self.assertFalse(q.submit(ShadowJob(None, None, {}, "c")))
        self.assertEqual(q.stats()["dropped_full"], 1)
        release_run.set()
        q.wait_until_idle()
        q.stop()

    def test_a_run_waits_for_user_analyses_and_is_dropped_after_the_maximum_wait(self):
        clock = {"t": 0.0}

        def tick():
            clock["t"] += 100.0
            return clock["t"]
        ran = []
        q = ShadowQueue(lambda job: ran.append(job), idle=lambda: False, max_wait_s=250, poll_s=0.01, clock=tick)
        q.submit(ShadowJob(None, None, {}, "r1"))
        self.assertTrue(q.wait_until_idle(5))
        self.assertEqual((ran, q.stats()["dropped_wait"]), ([], 1))
        q.stop()

    def test_a_run_starts_once_no_analysis_is_in_flight(self):
        done = threading.Event()
        q = ShadowQueue(lambda job: done.set(), poll_s=0.02)
        with analysis_in_flight():
            self.assertEqual(IN_FLIGHT.count, 1)
            q.submit(ShadowJob(None, None, {}, "r1"))
            self.assertFalse(done.wait(0.3))                                         # waits while an analysis is running
        self.assertTrue(done.wait(3))
        self.assertEqual(IN_FLIGHT.count, 0)
        q.stop()

    def test_a_failing_run_is_counted_and_the_worker_carries_on(self):
        calls = []

        def run(job):
            calls.append(job.request_id)
            if job.request_id == "bad":
                raise RuntimeError("tool exploded")
        q = ShadowQueue(run, requires_idle=False)
        q.submit(ShadowJob(None, None, {}, "bad"))
        q.submit(ShadowJob(None, None, {}, "good"))
        q.wait_until_idle()
        self.assertEqual((calls, q.stats()["failed"], q.stats()["completed"]), (["bad", "good"], 1, 1))
        q.stop()

    def test_shutdown_drops_the_queue_and_refuses_new_runs(self):
        hold = threading.Event()
        q = ShadowQueue(lambda job: hold.wait(5), requires_idle=False)
        q.submit(ShadowJob(None, None, {}, "running"))
        time.sleep(0.2)
        for i in range(3):
            q.submit(ShadowJob(None, None, {}, f"q{i}"))
        q.stop()
        self.assertEqual(q.stats()["dropped_shutdown"], 3)
        self.assertFalse(q.submit(ShadowJob(None, None, {}, "late")))
        hold.set()
        q.wait_until_idle()
        self.assertEqual(q.stats()["cancelled"], 1)                                  # the run in flight finished after shutdown began

    def test_submit_never_raises_into_the_request(self):
        q = ShadowQueue(lambda job: None)
        q._ensure_workers = MagicMock(side_effect=RuntimeError("no threads"))
        self.assertFalse(q.submit(ShadowJob(None, None, {}, "r1")))


class HookTests(Quiet):
    def setUp(self):
        super().setUp()
        self.req = MagicMock(tenant_id="client_x", ticket="disk full on app01")
        self.http = MagicMock()
        self.http.state.principal = MagicMock(id="svc", allows_tenant=lambda t: True)

    def enabled(self, mode="shadow", acceptance=None):
        d = write_json(self.dir, "d.json", {"decisions": [valid_decision("client_x", mode)]})
        a = write_json(self.dir, "a.json", acceptance if acceptance is not None else {})
        return patch.multiple(gate, DECISIONS_PATH=d, ACCEPTANCE_PATH=a, freeze_problems=lambda force=False: [])

    def test_off_returns_the_same_object_and_does_nothing_else(self):
        result = response()
        with patch.object(gate, "effective_mode") as eff, patch.object(engagement, "should_engage") as eng, patch.object(hook, "get_queue") as q:
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)
        eff.assert_not_called()
        eng.assert_not_called()
        q.assert_not_called()
        with hook.track_analysis():
            self.assertEqual(IN_FLIGHT.count, 0)                                     # off: not even counted

    def test_off_in_every_misconfigured_state(self):
        for env in ("", "garbage", "OFF", None):
            with patch.dict("os.environ", {runtime_config.MODE_ENV: env or ""}):
                result = response()
                self.assertIs(hook.after_analysis(self.http, self.req, result), result)

    def test_shadow_queues_a_run_and_returns_the_response_untouched(self):
        result = response()
        before = copy.deepcopy(result)
        queue = MagicMock()
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), self.enabled("shadow"), patch.object(hook, "get_queue", return_value=queue):
            out = hook.after_analysis(self.http, self.req, result)
        self.assertIs(out, result)
        self.assertEqual(result, before)
        (job,), _ = queue.submit.call_args
        self.assertEqual((job.ctx.tenant_id, job.ctx.mode, job.policy.mode, job.policy.planner, job.task), ("client_x", "shadow", "shadow", "rules_only", {"ticket_text": "disk full on app01"}))
        self.assertIn("confidence_below_threshold", job.reasons)

    def test_no_run_without_an_authenticated_principal_or_without_engagement(self):
        queue = MagicMock()
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), self.enabled("shadow"), patch.object(hook, "get_queue", return_value=queue):
            self.http.state.principal = None
            hook.after_analysis(self.http, self.req, response())
            self.http.state.principal = MagicMock(id="svc", allows_tenant=lambda t: True)
            confident = response(confidence={"overall_confidence": 0.95, "retrieval_quality": 0.9}, linked_resolution={"id": 1})
            self.assertIs(hook.after_analysis(self.http, self.req, confident), confident)
        queue.submit.assert_not_called()

    def test_a_principal_that_may_not_act_for_the_tenant_gets_no_run(self):
        queue = MagicMock()
        self.http.state.principal = MagicMock(id="svc", allows_tenant=lambda t: False)
        result = response()
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), self.enabled("shadow"), patch.object(hook, "get_queue", return_value=queue):
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)
        queue.submit.assert_not_called()

    def test_on_is_blocked_until_r2_3_passes_so_nothing_runs(self):
        result = response()
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "on"}), self.enabled("on"), patch.object(hook, "_execute") as ex:
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)
        ex.assert_not_called()

    def test_on_after_r2_3_runs_in_the_request_and_returns_a_reconciled_copy(self):
        result = response()
        before = copy.deepcopy(result)
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "on"}), self.enabled("on", passing_acceptance()), patch.object(hook, "_execute", return_value=run_result()) as ex:
            out = hook.after_analysis(self.http, self.req, result)
        self.assertEqual(result, before)
        self.assertIsNot(out, result)
        self.assertEqual(out["analysis"]["probable_causes"][0], "log growth on the data volume")
        self.assertEqual(out["investigation"]["status"], "supported")
        ctx, policy = ex.call_args.args[0], ex.call_args.args[1]
        self.assertEqual((ctx.mode, policy.mode), ("on", "on"))

    def test_any_failure_returns_the_deterministic_response(self):
        result = response()
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "on"}), self.enabled("on", passing_acceptance()), patch.object(hook, "_execute", side_effect=RuntimeError("db down")):
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), self.enabled("shadow"), patch.object(engagement, "should_engage", side_effect=RuntimeError("x")):
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)
        self.assertEqual(hook.after_analysis(self.http, self.req, "not a dict"), "not a dict")

    def test_the_rollback_switch_stops_everything_at_once(self):
        result = response()
        queue = MagicMock()
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), self.enabled("shadow"), patch.object(hook, "get_queue", return_value=queue):
            hook.after_analysis(self.http, self.req, result)
            self.assertEqual(queue.submit.call_count, 1)
            gate.rollback("test")
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)
            self.assertEqual(queue.submit.call_count, 1)

    def test_the_hook_only_ever_builds_the_frozen_planner_policy(self):
        with patch.object(release, "policy_for", wraps=release.policy_for) as pf, patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), self.enabled("shadow"), \
                patch.object(hook, "get_queue", return_value=MagicMock()):
            hook.after_analysis(self.http, self.req, response())
        self.assertEqual(pf.call_args.args[0], "shadow")
        self.assertNotIn("planner", pf.call_args.kwargs)


class ConfidenceInputTests(unittest.TestCase):
    def setUp(self):
        import confidence_engine as ce
        self.ce = ce
        self.base = dict(embedding_score=0.6, llm_score=0.5, retrieval_quality=0.5, classification_certainty=0.6, learned_feedback_score=0.1)

    def test_without_a_grounding_the_result_is_exactly_what_it_always_was(self):
        a = self.ce.compute_confidence_score(**self.base)
        self.assertNotIn("evidence_grounding", a)
        self.assertEqual(a, self.ce.compute_confidence_score(**self.base, evidence_grounding=None))

    def test_a_grounding_is_recorded_but_changes_no_confidence_until_calibrated(self):
        plain = self.ce.compute_confidence_score(**self.base)
        with_g = self.ce.compute_confidence_score(**self.base, evidence_grounding=0.9)
        self.assertEqual(with_g["evidence_grounding"], 0.9)
        self.assertEqual(with_g["primary_confidence"], plain["primary_confidence"])
        self.assertEqual(self.ce.EVIDENCE_GROUNDING_WEIGHT, 0.0)

    def test_a_calibrated_weight_would_move_the_confidence_toward_the_grounding_within_bounds(self):
        f = self.ce.apply_evidence_grounding
        self.assertAlmostEqual(f(0.4, 0.9, weight=0.5), 0.65)
        self.assertEqual(f(0.4, 0.9, weight=0.0), 0.4)
        for junk in (None, "x", -0.1, 1.5):
            self.assertEqual(f(0.4, junk, weight=0.5), 0.4)
        self.assertLessEqual(f(0.99, 1.0, weight=1.0), 1.0)


class OffParityThroughTheRouteTests(unittest.TestCase):
    """With the loop off the /analyze route returns exactly what it returned before R2-2."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            import app as application
            from agents import analyze as analyze_module
        except Exception as exc:                                                       # pragma: no cover - environment without the web stack
            raise unittest.SkipTest(f"the web stack is not importable here: {exc}")
        cls.application, cls.analyze_module = application, analyze_module
        web = FastAPI()
        web.include_router(analyze_module.router)
        cls.client = TestClient(web)

    CANNED = {"request_id": "r1", "ticket": "disk full on app01", "analysis": {"classification": "disk_full", "probable_causes": ["a"], "escalate": False},
              "confidence": {"overall_confidence": 0.3, "retrieval_quality": 0.1}, "tenant_config": {"tenant_id": "client_x"}, "retrieved_incidents": [],
              "linked_resolution": None, "linked_playbook": None}

    def call(self, hook_enabled=True):
        patches = [patch.object(self.application, "analyze_core", side_effect=lambda req: copy.deepcopy(self.CANNED))]
        if not hook_enabled:
            patches += [patch.object(self.analyze_module, "_loop_after", side_effect=lambda http, req, result: result),
                        patch.object(self.analyze_module, "_loop_track", side_effect=lambda: __import__("contextlib").nullcontext())]
        for p in patches:
            p.start()
        try:
            r = self.client.post("/analyze", json={"ticket": "disk full on app01", "tenant_id": "client_x"})
        finally:
            for p in patches:
                p.stop()
        return r

    def test_the_response_is_byte_identical_with_and_without_the_hook_when_off(self):
        with patch.dict("os.environ", {runtime_config.MODE_ENV: ""}, clear=False):
            with_hook, without_hook = self.call(True), self.call(False)
        self.assertEqual(with_hook.status_code, 200)
        a, b = with_hook.json(), without_hook.json()
        a.pop("trace_id", None)
        b.pop("trace_id", None)
        self.assertEqual(a, b)
        self.assertNotIn("investigation", a)
        self.assertEqual(set(a), set(self.CANNED) | set())

    def test_the_hook_is_called_but_stays_inert_when_off(self):
        with patch.dict("os.environ", {runtime_config.MODE_ENV: ""}), patch.object(gate, "effective_mode") as eff, patch.object(hook, "get_queue") as q:
            r = self.call(True)
        self.assertEqual(r.status_code, 200)
        eff.assert_not_called()
        q.assert_not_called()

    def test_a_crash_inside_the_hook_cannot_break_the_route(self):
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), patch.object(gate, "global_mode", side_effect=RuntimeError("boom")):
            r = self.call(True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("investigation", r.json())


if __name__ == "__main__":
    unittest.main()
