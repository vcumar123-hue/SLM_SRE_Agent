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


class RecordedDecisionsTests(unittest.TestCase):
    def test_the_repository_has_no_active_deployment_decision_a_completed_test_decision_is_history_only(self):
        active = gate.read_decisions()
        self.assertEqual(active, {}, "a test decision must not stay active; move it to 'ended' with the reason")
        raw = json.loads(gate.DECISIONS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(raw["decisions"], [])
        for entry in raw.get("ended", []):
            self.assertTrue(entry.get("ended_on") and entry.get("ended_because"))

    def test_an_ended_entry_enables_nothing(self):
        d = pathlib.Path(tempfile.mkdtemp()) / "d.json"
        d.write_text(json.dumps({"decisions": [], "ended": [valid_decision("client_c")]}), encoding="utf-8")
        self.assertEqual(gate.read_decisions(d), {})


class NoSecretsInTheRepositoryScriptsTests(unittest.TestCase):
    def test_the_controlled_shadow_test_script_holds_no_default_password(self):
        text = (pathlib.Path(__file__).resolve().parent.parent / "scripts" / "controlled_shadow_test.py").read_text(encoding="utf-8")
        self.assertNotIn("StrongPassword", text)
        self.assertIn('_required("PG_TEST_PASSWORD")', text)


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

    def env(self, mode):
        return patch.dict("os.environ", {runtime_config.MODE_ENV: mode, gate.ORCHESTRATOR_ENV: ""})

    # -- off ---------------------------------------------------------------------------------------------------------------------

    def test_off_every_entry_point_is_inert(self):
        result = response()
        with patch.object(gate, "effective_mode") as eff, patch.object(engagement, "should_engage") as eng, patch.object(hook, "get_queue") as q, \
                patch.object(hook, "_execute") as ex:
            with hook.request_scope(self.http):
                self.assertEqual(IN_FLIGHT.count, 0)                                 # not even counted
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)
            self.assertIsNone(hook.shadow_background(self.http, self.req, result))
            self.assertIsNone(hook.investigate_before_fallback(self.req, "t", {}, primary_confidence=0.1, retrieval_quality=0.0, retrieved_incidents=[],
                                                               linked_resolution=None, linked_playbook=None))
        for spy in (eff, eng, q, ex):
            spy.assert_not_called()

    def test_off_in_every_misconfigured_state(self):
        for env in ("", "garbage", "OFF", None):
            with patch.dict("os.environ", {runtime_config.MODE_ENV: env or ""}):
                result = response()
                self.assertIs(hook.after_analysis(self.http, self.req, result), result)
                self.assertIsNone(hook.shadow_background(self.http, self.req, result))

    # -- shadow: nothing until the response has gone out ----------------------------------------------------------------------------

    def test_shadow_prepares_a_task_but_queues_nothing_until_that_task_is_run(self):
        result = response()
        before = copy.deepcopy(result)
        queue = MagicMock()
        with self.env("shadow"), self.enabled("shadow"), patch.object(hook, "get_queue", return_value=queue):
            task = hook.shadow_background(self.http, self.req, result)
            self.assertIsNotNone(task)
            queue.submit.assert_not_called()                                         # preparing the task queues nothing
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)
            queue.submit.assert_not_called()
            task.func(*task.args, **task.kwargs)                                     # what Starlette does after the body has been sent
        self.assertEqual(result, before)
        (job,), _ = queue.submit.call_args
        self.assertEqual((job.ctx.tenant_id, job.ctx.mode, job.policy.mode, job.policy.planner, job.task), ("client_x", "shadow", "shadow", "rules_only", {"ticket_text": "disk full on app01"}))
        self.assertIn("confidence_below_threshold", job.reasons)

    def test_the_task_decides_late_no_principal_no_engagement_or_a_changed_mode_means_no_run(self):
        queue = MagicMock()
        with self.env("shadow"), self.enabled("shadow"), patch.object(hook, "get_queue", return_value=queue):
            self.http.state.principal = None
            t = hook.shadow_background(self.http, self.req, response())
            t.func(*t.args, **t.kwargs)
            self.http.state.principal = MagicMock(id="svc", allows_tenant=lambda t: True)
            confident = response(confidence={"overall_confidence": 0.95, "retrieval_quality": 0.9}, linked_resolution={"id": 1})
            t = hook.shadow_background(self.http, self.req, confident)
            t.func(*t.args, **t.kwargs)
            t = hook.shadow_background(self.http, self.req, response())
            gate.rollback("between the response and the task")                      # the switch is pulled after the response
            t.func(*t.args, **t.kwargs)
        queue.submit.assert_not_called()

    def test_a_principal_that_may_not_act_for_the_tenant_gets_no_run(self):
        queue = MagicMock()
        self.http.state.principal = MagicMock(id="svc", allows_tenant=lambda t: False)
        with self.env("shadow"), self.enabled("shadow"), patch.object(hook, "get_queue", return_value=queue):
            t = hook.shadow_background(self.http, self.req, response())
            t.func(*t.args, **t.kwargs)
        queue.submit.assert_not_called()

    def test_a_failure_while_queueing_never_escapes_the_task(self):
        with self.env("shadow"), self.enabled("shadow"), patch.object(hook, "get_queue", side_effect=RuntimeError("no queue")):
            t = hook.shadow_background(self.http, self.req, response())
            t.func(*t.args, **t.kwargs)

    # -- on: before the fallback, stored before it is used ------------------------------------------------------------------------------

    def investigate(self, **kw):
        args = dict(primary_confidence=0.3, retrieval_quality=0.1, retrieved_incidents=[], linked_resolution=None, linked_playbook=None)
        args.update(kw)
        analysis = {"classification": "disk_full", "probable_causes": ["disk quota too small"], "escalate": False}
        with hook.request_scope(self.http):
            return hook.investigate_before_fallback(self.req, "disk full on app01", analysis, **args)

    def on(self, acceptance=True):
        return contextlib_stack(self.env("on"), self.enabled("on", passing_acceptance() if acceptance else None))

    def test_on_is_blocked_until_r2_3_passes_so_nothing_runs(self):
        with self.env("on"), self.enabled("on"), patch.object(hook, "_execute") as ex:
            self.assertIsNone(self.investigate())
        ex.assert_not_called()

    def test_a_supported_stored_run_replaces_the_fallback_and_is_applied_to_the_response(self):
        result = response()
        before = copy.deepcopy(result)
        with self.on(), patch.object(hook, "_execute", return_value=(run_result(), True)) as ex:
            with hook.request_scope(self.http):
                decision = hook.investigate_before_fallback(self.req, "disk full on app01", result["analysis"], primary_confidence=0.3, retrieval_quality=0.1,
                                                            retrieved_incidents=[], linked_resolution=None, linked_playbook=None)
                self.assertEqual((decision.outcome, decision.replaces_fallback), ("merged", True))
            out = hook.after_analysis(self.http, self.req, result)
        self.assertEqual(result, before)
        self.assertIsNot(out, result)
        self.assertEqual(out["analysis"]["probable_causes"][0], "log growth on the data volume")
        self.assertEqual(out["investigation"]["status"], "supported")
        ctx, policy = ex.call_args.args[0], ex.call_args.args[1]
        self.assertEqual((ctx.mode, policy.mode, ctx.tenant_id), ("on", "on", "client_x"))
        with self.on():
            self.assertIs(hook.after_analysis(self.http, self.req, result), result)           # consumed: applied once only

    def test_a_conflicting_run_also_replaces_the_fallback_but_overrides_nothing(self):
        run = run_result(evidence=[item("e1", "memory_pressure"), item("e2", "memory_pressure")])
        with self.on(), patch.object(hook, "_execute", return_value=(run, True)):
            with hook.request_scope(self.http):
                decision = hook.investigate_before_fallback(self.req, "t", {"classification": "disk_full", "probable_causes": []}, primary_confidence=0.3,
                                                            retrieval_quality=0.1, retrieved_incidents=[], linked_resolution=None, linked_playbook=None)
                self.assertEqual((decision.outcome, decision.replaces_fallback), ("conflict", True))
                out = hook.after_analysis(self.http, self.req, response())
        self.assertEqual(out["analysis"]["classification"], "disk_full")
        self.assertTrue(out["analysis"]["escalate"])

    def test_a_run_that_is_not_supported_leaves_the_fallback_in_place(self):
        run = run_result(status="escalated_insufficient", hyps=[Hypothesis(cause="weak", status="weak", evidence=["e1"])])
        with self.on(), patch.object(hook, "_execute", return_value=(run, True)):
            with hook.request_scope(self.http):
                decision = hook.investigate_before_fallback(self.req, "t", {"classification": "disk_full"}, primary_confidence=0.3, retrieval_quality=0.1,
                                                            retrieved_incidents=[], linked_resolution=None, linked_playbook=None)
                self.assertEqual((decision.outcome, decision.replaces_fallback), ("kept", False))
                out = hook.after_analysis(self.http, self.req, response())
        self.assertEqual(out["analysis"], response()["analysis"])                       # deterministic result as it was
        self.assertEqual(out["investigation"]["status"], "escalated_insufficient")      # attached for the operator

    def test_a_run_that_cannot_be_stored_is_discarded_and_changes_nothing(self):
        result = response()
        with self.on(), patch.object(hook, "_execute", return_value=(run_result(), False)):
            with hook.request_scope(self.http):
                decision = hook.investigate_before_fallback(self.req, "t", result["analysis"], primary_confidence=0.3, retrieval_quality=0.1,
                                                            retrieved_incidents=[], linked_resolution=None, linked_playbook=None)
                self.assertIsNone(decision)                                             # the fallback is not replaced
                self.assertIs(hook.after_analysis(self.http, self.req, result), result)   # and the response is untouched

    def test_persistence_failure_in_the_real_store_path_is_reported_as_not_stored(self):
        with patch("agentruntime.store.RunStore", side_effect=RuntimeError("database is down")):
            self.assertFalse(hook._persist(run_result(), {"ticket_text": "t"}, "req1"))

    def test_no_run_without_engagement_or_a_principal_or_when_the_run_raises(self):
        with self.on(), patch.object(hook, "_execute") as ex:
            self.assertIsNone(self.investigate(primary_confidence=0.95, retrieval_quality=0.9, linked_resolution={"id": 1}))
            self.http.state.principal = None
            self.assertIsNone(self.investigate())
        ex.assert_not_called()
        self.http.state.principal = MagicMock(id="svc", allows_tenant=lambda t: True)
        with self.on(), patch.object(hook, "_execute", side_effect=RuntimeError("db down")):
            self.assertIsNone(self.investigate())

    def test_the_mode_is_rechecked_when_the_response_is_finished(self):
        result = response()
        with self.on(), patch.object(hook, "_execute", return_value=(run_result(), True)):
            with hook.request_scope(self.http):
                self.assertIsNotNone(hook.investigate_before_fallback(self.req, "t", result["analysis"], primary_confidence=0.3, retrieval_quality=0.1,
                                                                      retrieved_incidents=[], linked_resolution=None, linked_playbook=None))
                gate.rollback("pulled mid-request")
                self.assertIs(hook.after_analysis(self.http, self.req, result), result)

    def test_on_stays_off_while_the_orchestrator_path_is_enabled(self):
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "on", gate.ORCHESTRATOR_ENV: "true"}), self.enabled("on", passing_acceptance()):
            mode, why = gate.effective_mode("client_x")
        self.assertEqual(mode, "off")
        self.assertIn("orchestrator", why)

    def test_the_hook_only_ever_builds_the_frozen_planner_policy(self):
        with patch.object(release, "policy_for", wraps=release.policy_for) as pf, self.env("shadow"), self.enabled("shadow"), patch.object(hook, "get_queue", return_value=MagicMock()):
            t = hook.shadow_background(self.http, self.req, response())
            t.func(*t.args, **t.kwargs)
        self.assertEqual(pf.call_args.args[0], "shadow")
        self.assertNotIn("planner", pf.call_args.kwargs)


import contextlib


def contextlib_stack(*managers):
    stack = contextlib.ExitStack()

    class _Ctx:
        def __enter__(self_inner):
            for m in managers:
                stack.enter_context(m)
            return self_inner

        def __exit__(self_inner, *exc):
            return stack.__exit__(*exc)
    return _Ctx()


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
    """With the loop off the /analyze route returns exactly what it returned before R2-2; with shadow, nothing starts until the body has been sent."""

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
        cls.web = FastAPI()
        cls.web.include_router(analyze_module.router)
        cls.client = TestClient(cls.web)

    CANNED = {"request_id": "r1", "ticket": "disk full on app01", "analysis": {"classification": "disk_full", "probable_causes": ["a"], "escalate": False},
              "confidence": {"overall_confidence": 0.3, "retrieval_quality": 0.1}, "tenant_config": {"tenant_id": "client_x"}, "retrieved_incidents": [],
              "linked_resolution": None, "linked_playbook": None}

    def call(self, hook_enabled=True):
        patches = [patch.object(self.application, "analyze_core", side_effect=lambda req: copy.deepcopy(self.CANNED))]
        if not hook_enabled:
            patches += [patch.object(self.analyze_module, "_loop_after", side_effect=lambda http, req, result: result),
                        patch.object(self.analyze_module, "_loop_scope", side_effect=lambda http: __import__("contextlib").nullcontext()),
                        patch.object(self.analyze_module, "_loop_background", side_effect=lambda http, req, result: None)]
        for p in patches:
            p.start()
        try:
            r = self.client.post("/analyze", json={"ticket": "disk full on app01", "tenant_id": "client_x"})
        finally:
            for p in patches:
                p.stop()
        return r

    def test_the_response_is_identical_with_and_without_the_hook_when_off(self):
        with patch.dict("os.environ", {runtime_config.MODE_ENV: ""}, clear=False):
            with_hook, without_hook = self.call(True), self.call(False)
        self.assertEqual(with_hook.status_code, 200)
        a, b = with_hook.json(), without_hook.json()
        a.pop("trace_id", None)
        b.pop("trace_id", None)
        self.assertEqual(a, b)
        self.assertNotIn("investigation", a)
        self.assertEqual(set(a), set(self.CANNED))

    def test_the_hook_is_called_but_stays_inert_when_off(self):
        with patch.dict("os.environ", {runtime_config.MODE_ENV: ""}), patch.object(gate, "effective_mode") as eff, patch.object(hook, "get_queue") as q:
            r = self.call(True)
        self.assertEqual(r.status_code, 200)
        eff.assert_not_called()
        q.assert_not_called()

    def test_off_attaches_no_background_task_to_the_response(self):
        with patch.dict("os.environ", {runtime_config.MODE_ENV: ""}):
            response = self.analyze_module._loop_response(MagicMock(), MagicMock(), copy.deepcopy(self.CANNED))
        self.assertIsNone(response.background)

    def test_a_crash_inside_the_hook_cannot_break_the_route(self):
        with patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), patch.object(gate, "global_mode", side_effect=RuntimeError("boom")):
            r = self.call(True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("investigation", r.json())

    def test_in_shadow_the_run_is_queued_only_after_the_response_body_has_been_sent(self):
        import asyncio
        events = []
        Quiet.setUp = Quiet.setUp                                                       # (the cipher patch below plays the part of a usable encryption setup)

        def fake_submit(principal, tenant, snapshot):
            events.append("queued")

        async def drive():
            body = json.dumps({"ticket": "disk full on app01", "tenant_id": "client_x"}).encode()
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/analyze", "raw_path": b"/analyze",
                     "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                                                      (b"host", b"test")], "client": ("127.0.0.1", 1), "server": ("test", 80)}
            sent = {"done": False}

            async def receive():
                if sent["done"]:
                    await asyncio.sleep(3600)
                sent["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    events.append("headers")
                elif message["type"] == "http.response.body" and not message.get("more_body"):
                    events.append("body_sent")
            await self.web(scope, receive, send)

        with patch.dict("os.environ", {runtime_config.MODE_ENV: "shadow"}), patch.object(runtime_config.crypto, "get_cipher", return_value=KEY_OK), \
                patch.object(self.application, "analyze_core", side_effect=lambda req: copy.deepcopy(self.CANNED)), patch.object(hook, "_shadow_submit", fake_submit):
            asyncio.run(drive())
        self.assertEqual(events, ["headers", "body_sent", "queued"])


class PipelineOrderTests(unittest.TestCase):
    """The investigation runs before the one-shot fallback, and the call sites are exactly where the freeze expects them."""

    def test_the_investigation_call_precedes_the_fallback_call_in_the_legacy_pipeline(self):
        import ast
        import pathlib
        text = (pathlib.Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_analyze_core_legacy")
        calls = [(c.lineno, c.func.id) for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        loop = [ln for ln, name in calls if name == "_r2_before_fallback"]
        fallback = [ln for ln, name in calls if name == "maybe_run_fallback"]
        self.assertEqual((len(loop), len(fallback)), (1, 1))
        self.assertLess(loop[0], fallback[0])

    def test_the_helper_returns_none_when_off_or_when_the_hook_raises(self):
        import app as application
        with patch.dict("os.environ", {runtime_config.MODE_ENV: ""}):
            self.assertIsNone(application._r2_before_fallback(MagicMock(tenant_id="client_x"), "t", {}, 0.1, 0.0, [], None, None))
        with patch("agentruntime.integration.hook.investigate_before_fallback", side_effect=RuntimeError("x")):
            self.assertIsNone(application._r2_before_fallback(MagicMock(), "t", {}, 0.1, 0.0, [], None, None))

    def test_a_replaced_fallback_is_not_called_and_an_unreplaced_one_is(self):
        import app as application
        from agentruntime.integration.hook import LoopDecision
        calls = []
        base = dict(ticket="Disk full on the db host", use_fallback=True, force_fallback=False, tenant_id="poc_client", top_k=3, confirm_local_matches=None, knowledge_scope="client_private")

        def run(decision):
            from models import AnalyzeRequest
            calls.clear()
            with patch.object(application, "_r2_before_fallback", return_value=decision), \
                    patch.object(application, "maybe_run_fallback", side_effect=lambda **kw: (calls.append("groq"), (False, None, 0.0, None, None, None, "no"))[1]), \
                    patch.object(application, "run_hybrid_retrieval", return_value=([], 0.0, 0.0, None, {"tenant_id": "poc_client", "display_name": "x", "knowledge_scope": "client_private"})), \
                    patch.object(application, "fetch_rag_context", return_value=""), patch.object(application, "persist_confidence_log"):
                try:
                    application._analyze_core_legacy(AnalyzeRequest(**base))
                except Exception:
                    pass                                                              # the stubbed pipeline need not complete; only whether the fallback was reached matters
            return list(calls)
        self.assertEqual(run(None), ["groq"])
        self.assertEqual(run(LoopDecision(run=None, outcome="kept", replaces_fallback=False)), ["groq"])
        self.assertEqual(run(LoopDecision(run=None, outcome="merged", replaces_fallback=True)), [])


if __name__ == "__main__":
    unittest.main()
