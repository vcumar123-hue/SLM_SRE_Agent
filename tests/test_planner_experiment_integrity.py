"""
Experiment-integrity tests for the planner harness (review of 2026-10-15): frozen inputs are the inputs used, missing inputs fail instead
of shrinking the experiment, executors are bounded and released, reviewer agreement is recomputed at the freeze, the seed is derived, and
golden tickets are approved and calibration-only. Synthetic data and fake planners only.
"""
import threading
import time
import unittest
from unittest.mock import patch

from agentruntime.contracts import Budgets
from agentruntime.executor import BoundedExecutor, CallTimeout, ExecutorFailed
from planner_experiment import annotations as ann
from planner_experiment import experiment as exp_mod
from planner_experiment import injection
from planner_experiment.experiment import ExperimentInputError, run_experiment_unfrozen
from planner_experiment.scripted_env import Recording
from tests.pe_fake_planners import GiveUpPlanner, GoodPlanner
from tests.test_planner_experiment import annotation, ctx_for, drafts_for

def pe_threads(prefix="pe-"):
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


def small_experiment(n=6):
    cases = [{"case_id": f"c{i}", "tenant_id": "t", "ticket_text": f"disk full on host{i}"} for i in range(n)]
    rec = Recording()
    for c in cases:
        rec.add(c["case_id"], "incident_kb_search", {"query": c["ticket_text"], "top_k": 5}, drafts_for("t", ("incident", "I1", "disk full on the database host")))
    notes = {c["case_id"]: annotation(c["case_id"]) for c in cases}
    return cases, notes, rec, injection.build_injection_cases([c["case_id"] for c in cases])


class MissingInputsFailTests(unittest.TestCase):
    """An incomplete experiment must fail before it starts, never run smaller."""

    def setUp(self):
        self.cases, self.notes, self.rec, self.inj = small_experiment()

    def run_all(self, factories=None, **kw):
        factories = factories or {"rules_only": GiveUpPlanner, "rules_then_model": GoodPlanner, "model": GoodPlanner}
        return run_experiment_unfrozen(self.cases, self.notes, self.rec, factories, ctx_for, self.inj, seed=1, **kw)

    def test_a_complete_set_runs(self):
        out = self.run_all()
        self.assertEqual(out["cases"], 6)
        for mode in exp_mod.MODES:
            self.assertEqual(len(out["modes"][mode]["runs"]), 6)                      # every case scored in every mode
            self.assertEqual(len(out["modes"][mode]["injections"]), 12)

    def test_a_case_without_an_annotation_fails(self):
        del self.notes["c3"]
        with self.assertRaisesRegex(ExperimentInputError, "no annotation"):
            self.run_all()

    def test_an_annotation_without_a_case_fails(self):
        self.notes["ghost"] = annotation("ghost")
        with self.assertRaisesRegex(ExperimentInputError, "no case"):
            self.run_all()

    def test_a_case_without_recorded_output_fails(self):
        del self.rec.data["c2"]
        with self.assertRaisesRegex(ExperimentInputError, "no recorded tool output"):
            self.run_all()

    def test_an_injection_case_with_a_missing_base_case_fails(self):
        self.inj = injection.build_injection_cases(["c0", "nope"])
        with self.assertRaisesRegex(ExperimentInputError, "missing base case"):
            self.run_all()

    def test_a_missing_planner_or_mode_fails(self):
        with self.assertRaises(ExperimentInputError):
            self.run_all({"rules_only": GiveUpPlanner})
        with self.assertRaises(ExperimentInputError):
            self.run_all({"model": GoodPlanner}, modes=("model",))

    def test_duplicate_or_incomplete_cases_fail(self):
        self.cases.append(dict(self.cases[0]))
        with self.assertRaisesRegex(ExperimentInputError, "duplicate"):
            self.run_all()
        self.cases.pop()
        self.cases[0] = {"case_id": "c0", "tenant_id": "t", "ticket_text": ""}
        with self.assertRaisesRegex(ExperimentInputError, "missing"):
            self.run_all()

    def test_strict_mode_requires_the_designed_size(self):
        with self.assertRaisesRegex(ExperimentInputError, "exactly 50 cases"):
            self.run_all(strict=True)


class ExecutorLifecycleTests(unittest.TestCase):
    def test_threads_do_not_grow_with_the_number_of_runs_and_are_released_at_the_end(self):
        cases, notes, rec, _ = small_experiment(40)
        inj = injection.build_injection_cases(["c0", "c1"])
        peak = {"n": 0}

        class Spy(GoodPlanner):
            def next_action(self, state):
                peak["n"] = max(peak["n"], len(pe_threads()))
                return super().next_action(state)

        before = threading.active_count()
        run_experiment_unfrozen(cases, notes, rec, {m: Spy for m in exp_mod.MODES}, ctx_for, inj, seed=1)
        self.assertLessEqual(peak["n"], 9)            # three executors x three workers, for 40 cases x 3 modes plus 36 injection runs
        deadline = time.time() + 5
        while pe_threads() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(pe_threads(), [])
        self.assertLessEqual(threading.active_count(), before + 1)

    def test_a_closed_executor_refuses_work_and_its_workers_exit(self):
        ex = BoundedExecutor(max_workers=3, max_abandoned=1, name="pe-close")
        self.assertEqual(ex.run(lambda token: 7, 2), 7)
        ex.close()
        with self.assertRaises(ExecutorFailed):
            ex.run(lambda token: 7, 2)
        deadline = time.time() + 5
        while pe_threads("pe-close") and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(pe_threads("pe-close"), [])

    def test_a_run_that_leaves_a_stuck_call_gets_fresh_executors(self):
        executors = exp_mod._Executors()
        gate = threading.Event()
        try:
            with self.assertRaises(CallTimeout):
                executors.tool.run(lambda token: gate.wait(10), 0.2)
            old = executors.tool
            self.assertTrue(executors.stuck())
            self.assertTrue(executors.replace_if_stuck())
            self.assertIsNot(executors.tool, old)
            self.assertTrue(old.closed)
            self.assertFalse(executors.stuck())
            self.assertEqual(executors.replacements, 1)
            self.assertFalse(executors.replace_if_stuck())
        finally:
            gate.set()
            executors.close()


class StuckCapTests(unittest.TestCase):
    """Replacing a stuck executor set must not reset the cap on stuck calls: the limit is for the whole experiment."""

    def setUp(self):
        self.gate = threading.Event()
        self.executors = exp_mod._Executors()
        self.addCleanup(self.executors.close)
        self.addCleanup(self.gate.set)

    def stick(self):
        with self.assertRaises(CallTimeout):
            self.executors.tool.run(lambda token: self.gate.wait(30), 0.1)

    def test_replacing_keeps_counting_the_stuck_calls_of_retired_executors(self):
        self.stick()
        self.assertEqual(self.executors.live_stuck(), 1)
        self.assertTrue(self.executors.replace_if_stuck())
        self.assertEqual(self.executors.live_stuck(), 1)              # still stuck in the retired executor: still counted
        self.assertFalse(self.executors.stuck())                      # the fresh set itself is clean
        self.gate.set()
        deadline = time.time() + 5
        while self.executors.live_stuck() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.executors.live_stuck(), 0)              # counted until the call really returns

    def test_too_many_replacements_abort_the_experiment(self):
        for _ in range(exp_mod.MAX_REPLACEMENTS):
            self.stick()
            self.assertTrue(self.executors.replace_if_stuck())
        self.stick()
        with self.assertRaisesRegex(exp_mod.ExperimentAborted, "replaced"):
            self.executors.replace_if_stuck()

    def test_too_many_live_stuck_calls_abort_even_with_replacements_left(self):
        with patch.object(exp_mod, "MAX_STUCK_CALLS", 2), patch.object(exp_mod, "MAX_REPLACEMENTS", 50):
            self.stick()
            self.executors.replace_if_stuck()
            self.stick()
            self.executors.replace_if_stuck()
            self.stick()
            with self.assertRaisesRegex(exp_mod.ExperimentAborted, "stuck calls"):
                self.executors.replace_if_stuck()

    def test_threads_stay_bounded_across_replacements(self):
        for _ in range(exp_mod.MAX_REPLACEMENTS):
            self.stick()
            self.executors.replace_if_stuck()
        # three live executors of three workers, plus only the stuck threads of the retired ones (idle workers exited when they closed)
        deadline = time.time() + 5
        while self.executors.live_threads() > 9 + exp_mod.MAX_REPLACEMENTS and time.time() < deadline:
            time.sleep(0.05)
        self.assertLessEqual(self.executors.live_threads(), 9 + exp_mod.MAX_REPLACEMENTS)
        self.assertLessEqual(len(pe_threads()), 9 + exp_mod.MAX_REPLACEMENTS)

    def test_an_abort_produces_no_result(self):
        """A run that aborts raises; there is no partial result to mistake for an experiment outcome."""

        class Hangs(GoodPlanner):
            def next_action(self, state):
                time.sleep(30)

        cases, notes, rec, inj = small_experiment(3)
        with patch.object(exp_mod, "MAX_REPLACEMENTS", 0), self.assertRaises(exp_mod.ExperimentAborted):
            run_experiment_unfrozen(cases, notes, rec, {m: Hangs for m in exp_mod.MODES}, ctx_for, inj, seed=1,
                                    budgets=Budgets(deadline_s=1, model_call_timeout_s=1))


class AnnotationRuleTests(unittest.TestCase):
    def test_a_supported_annotation_needs_required_evidence(self):
        with self.assertRaisesRegex(ValueError, "required_evidence"):
            annotation("c", required_evidence=[]).check()
        annotation("c", end_state="escalate", cause_keywords=[], required_evidence=[]).check()

    def test_a_different_expected_cause_is_a_disagreement(self):
        first = [annotation("c1", cause_keywords=[["disk"], ["full"]])]
        same = [annotation("c1", "r2", cause_keywords=[["FULL"], ["Disk"]])]
        other = [annotation("c1", "r2", cause_keywords=[["memory"], ["leak"]])]
        self.assertEqual(ann.agreement(first, same)["disagreements"], [])
        self.assertEqual(ann.agreement(first, other)["disagreements"], [{"case_id": "c1", "fields": ["cause_keywords"]}])


if __name__ == "__main__":
    unittest.main()
