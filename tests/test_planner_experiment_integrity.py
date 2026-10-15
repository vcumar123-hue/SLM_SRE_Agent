"""
Experiment-integrity tests for the planner harness (review of 2026-10-15): frozen inputs are the inputs used, missing inputs fail instead
of shrinking the experiment, executors are bounded and released, reviewer agreement is recomputed at the freeze, the seed is derived, and
golden tickets are approved and calibration-only. Synthetic data and fake planners only.
"""
import inspect
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agentruntime.executor import BoundedExecutor, CallTimeout, ExecutorFailed
from planner_experiment import annotations as ann
from planner_experiment import experiment as exp_mod
from planner_experiment import freeze, golden, injection, metrics
from planner_experiment import seed as seed_mod
from planner_experiment.experiment import ExperimentInputError, run_experiment_frozen, run_experiment_unfrozen
from planner_experiment.scripted_env import Recording
from tests.test_planner_experiment import GiveUpPlanner, GoodPlanner, annotation, ctx_for, drafts_for

TESTS_DIR = pathlib.Path(__file__).resolve().parent


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


def synthetic_frozen_set(directory, n=50, reviewers=("Ann", "Bob"), same=True):
    """Write a complete, consistent set of frozen inputs; returns (files, reviewer_files, cases)."""
    seed = seed_mod.derive_seed("a" * 40, "calibration-selection")
    cases = [{"case_id": f"client_c:T{i}", "tenant_id": "client_c", "ticket_text": f"disk full on host{i}"} for i in range(n)]
    rec = Recording()
    for c in cases:
        rec.add(c["case_id"], "incident_kb_search", {"query": c["ticket_text"], "top_k": 5},
                drafts_for("client_c", ("incident", "I1", "disk full on the database host")))
    inj = injection.build_injection_cases([c["case_id"] for c in cases[:4]])

    def write(name, value):
        path = directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    files = {
        "annotations": write("annotations.json", [annotation(c["case_id"], "adjudicated").model_dump() for c in cases]),
        "cases": write("cases.json", cases), "recording": write("recording.json", rec.data),
        "injection_cases": write("injection_cases.json", [vars(i) for i in inj]),
        "config": write("config.json", {"seed": seed, "modes": list(exp_mod.MODES), "budgets": {}}),
        "seed_record": write("seed_record.json", {"purpose": "calibration-selection", "artifact": "a" * 40, "seed": seed}),
        "calibration_selection": write("selection.json", {"cases": [{"case_id": c["case_id"]} for c in cases]}),
    }
    reviewer_files = {}
    for i, name in enumerate(reviewers):
        mine = [annotation(c["case_id"], name).model_dump() for c in cases]
        if not same and i == 1:
            for m in mine[: n // 2]:
                m["end_state"], m["cause_keywords"], m["required_evidence"] = "escalate", [], []
        reviewer_files[name] = write(f"reviewer_{name}.json", mine)
    return files, reviewer_files, cases


def planners_here():
    return patch.object(exp_mod, "PLANNERS_DIR", TESTS_DIR)


class FrozenInputTests(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.files, self.reviewers, self.cases = synthetic_frozen_set(self.d)
        self.out = self.d / "freeze.json"

    def freeze(self):
        return freeze.create_freeze(self.files, self.reviewers, metrics.DECISION_RULE, self.out)

    def factories(self):
        return {m: GoodPlanner for m in exp_mod.MODES}

    def test_the_run_takes_no_experiment_data_from_the_caller(self):
        self.assertEqual(list(inspect.signature(run_experiment_frozen).parameters), ["record_path", "planner_factories"])

    def test_a_planner_outside_the_frozen_planners_folder_is_refused(self):
        self.freeze()
        with self.assertRaisesRegex(ExperimentInputError, "not defined in planners"):
            run_experiment_frozen(self.out, self.factories())

    def test_a_frozen_run_end_to_end_uses_only_frozen_inputs(self):
        record = self.freeze()
        with planners_here():
            out = run_experiment_frozen(self.out, {"rules_only": GiveUpPlanner, "rules_then_model": GoodPlanner, "model": GoodPlanner})
        self.assertTrue(out["frozen"])
        self.assertFalse(out["not_a_result"])
        self.assertEqual(out["record_hash"], record["record_hash"])
        self.assertEqual(out["cases"], 50)
        self.assertEqual(out["modes"]["rules_then_model"]["summary"]["trajectory_success_rate"]["estimate"], 1.0)

    def test_changing_any_frozen_input_after_the_freeze_stops_the_run(self):
        names = ["annotations", "cases", "recording", "injection_cases", "config", "seed_record", "calibration_selection"]
        for name in names + ["reviewer:Ann", "reviewer:Bob"]:
            d = pathlib.Path(tempfile.mkdtemp())
            files, reviewers, _ = synthetic_frozen_set(d)
            out = d / "freeze.json"
            freeze.create_freeze(files, reviewers, metrics.DECISION_RULE, out)
            target = files[name] if name in files else reviewers[name.split(":", 1)[1]]
            target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
            if name.startswith("reviewer:"):
                with self.assertRaises(freeze.FreezeError, msg=name):
                    freeze.verify_freeze(out)
            else:
                with planners_here(), self.assertRaises(freeze.FreezeError, msg=name):
                    run_experiment_frozen(out, self.factories())

    def test_a_later_read_can_never_silently_return_different_content(self):
        record = self.freeze()
        data = freeze.read_frozen(record, "cases")
        self.assertEqual(json.loads(data.decode("utf-8")), self.cases)
        self.files["cases"].write_text("[]", encoding="utf-8")
        with self.assertRaises(freeze.FreezeError):
            freeze.read_frozen(record, "cases")

    def test_adding_removing_or_changing_a_code_file_after_the_freeze_is_detected(self):
        root = pathlib.Path(tempfile.mkdtemp())
        for top in freeze.CODE_ROOTS:
            (root / top).mkdir()
            (root / top / "m.py").write_text("x = 1\n", encoding="utf-8")
        record = freeze.create_freeze(self.files, self.reviewers, metrics.DECISION_RULE, self.out, root=root)
        freeze.verify_code(record, root)
        (root / "agentruntime" / "new_planner.py").write_text("y = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(freeze.FreezeError, "set of code files changed"):
            freeze.verify_code(record, root)
        (root / "agentruntime" / "new_planner.py").unlink()
        (root / "agentruntime" / "m.py").write_text("x = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(freeze.FreezeError, "changed after the freeze"):
            freeze.verify_code(record, root)

    def test_the_planners_folder_and_harness_are_part_of_the_hashed_code(self):
        keys = set(freeze.code_files())
        self.assertIn("code/agentruntime/planners/__init__.py", keys)
        self.assertIn("code/planner_experiment/experiment.py", keys)
        self.assertIn("code/planner_experiment/metrics.py", keys)
        self.assertIn("code/agentruntime/tools/patterns.py", keys)

    def test_a_tampered_record_is_refused(self):
        self.freeze()
        record = json.loads(self.out.read_text(encoding="utf-8"))
        record["agreement"]["end_state_kappa"] = 1.0
        record["reviewers"] = ["Eve", "Mallory"]
        self.out.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(freeze.FreezeError, "record itself"):
            freeze.verify_freeze(self.out)

    def test_the_case_list_must_match_the_selection_and_the_seed_must_be_the_derived_one(self):
        sel = json.loads(self.files["calibration_selection"].read_text(encoding="utf-8"))
        sel["cases"].pop()
        self.files["calibration_selection"].write_text(json.dumps(sel), encoding="utf-8")
        self.freeze()
        with planners_here(), self.assertRaisesRegex(ExperimentInputError, "frozen calibration selection"):
            run_experiment_frozen(self.out, self.factories())
        d = pathlib.Path(tempfile.mkdtemp())
        files, reviewers, _ = synthetic_frozen_set(d)
        files["seed_record"].write_text(json.dumps({"purpose": "calibration-selection", "artifact": "a" * 40, "seed": 20261014}), encoding="utf-8")
        out = d / "freeze.json"
        freeze.create_freeze(files, reviewers, metrics.DECISION_RULE, out)
        with planners_here(), self.assertRaises(ValueError):
            run_experiment_frozen(out, self.factories())

    def test_a_frozen_set_of_the_wrong_size_does_not_run(self):
        d = pathlib.Path(tempfile.mkdtemp())
        files, reviewers, _ = synthetic_frozen_set(d, n=20)
        out = d / "freeze.json"
        freeze.create_freeze(files, reviewers, metrics.DECISION_RULE, out)
        with planners_here(), self.assertRaisesRegex(ExperimentInputError, "exactly 50 cases"):
            run_experiment_frozen(out, self.factories())


class ReviewerAgreementFreezeTests(unittest.TestCase):
    """The freeze recomputes agreement from the reviewers' own files: an empty or partial record cannot pass."""

    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.files, self.reviewers, self.cases = synthetic_frozen_set(self.d)
        self.out = self.d / "freeze.json"

    def make(self):
        return freeze.create_freeze(self.files, self.reviewers, metrics.DECISION_RULE, self.out)

    def rewrite(self, name, change):
        path = self.reviewers[name]
        items = json.loads(path.read_text(encoding="utf-8"))
        change(items)
        path.write_text(json.dumps(items), encoding="utf-8")

    def test_a_complete_agreeing_record_freezes_and_stores_the_agreement(self):
        record = self.make()
        self.assertEqual(record["agreement"]["cases_compared"], 50)
        self.assertEqual(record["agreement"]["end_state_kappa"], 1.0)
        self.assertEqual(sorted(record["reviewers"]), ["Ann", "Bob"])

    def test_empty_or_partial_reviewer_files_are_refused(self):
        self.rewrite("Bob", lambda items: items.clear())
        with self.assertRaisesRegex(freeze.FreezeError, "exactly the 50 frozen cases"):
            self.make()
        self.rewrite("Ann", lambda items: items.pop())
        with self.assertRaises(freeze.FreezeError):
            self.make()

    def test_duplicates_and_foreign_reviewer_names_are_refused(self):
        self.rewrite("Bob", lambda items: items.__setitem__(0, dict(items[1])))
        with self.assertRaises(freeze.FreezeError):
            self.make()
        d = pathlib.Path(tempfile.mkdtemp())
        files, reviewers, _ = synthetic_frozen_set(d)
        items = json.loads(reviewers["Bob"].read_text(encoding="utf-8"))
        items[0]["reviewer"] = "Ann"
        reviewers["Bob"].write_text(json.dumps(items), encoding="utf-8")
        with self.assertRaisesRegex(freeze.FreezeError, "under that reviewer"):
            freeze.create_freeze(files, reviewers, metrics.DECISION_RULE, d / "f.json")

    def test_names_must_be_two_distinct_and_real(self):
        for names in (("Ann",), ("Ann", "ann"), ("Ann", "TBD"), ("Ann", "Bob", "Cy")):
            d = pathlib.Path(tempfile.mkdtemp())
            files, reviewers, _ = synthetic_frozen_set(d, reviewers=names)
            with self.assertRaises(freeze.FreezeError, msg=names):
                freeze.create_freeze(files, reviewers, metrics.DECISION_RULE, d / "f.json")

    def test_low_agreement_blocks_the_freeze(self):
        d = pathlib.Path(tempfile.mkdtemp())
        files, reviewers, _ = synthetic_frozen_set(d, same=False)
        with self.assertRaisesRegex(freeze.FreezeError, "kappa"):
            freeze.create_freeze(files, reviewers, metrics.DECISION_RULE, d / "f.json")

    def test_every_disagreement_needs_a_written_adjudication(self):
        self.rewrite("Bob", lambda items: items[0].__setitem__("acceptable_first_tools", ["outcome_lookup"]))
        with self.assertRaisesRegex(freeze.FreezeError, "needs an adjudication rationale"):
            self.make()
        notes = json.loads(self.files["annotations"].read_text(encoding="utf-8"))
        notes[0]["notes"] = "owner: either tool is acceptable; kept the first"
        self.files["annotations"].write_text(json.dumps(notes), encoding="utf-8")
        record = self.make()
        self.assertEqual(record["agreement"]["disagreements"][0]["fields"], ["acceptable_first_tools"])

    def test_a_cause_disagreement_also_needs_adjudication(self):
        self.rewrite("Bob", lambda items: items[0].__setitem__("cause_keywords", [["memory"], ["leak"]]))
        with self.assertRaisesRegex(freeze.FreezeError, "cause_keywords"):
            self.make()

    def test_the_adjudicated_file_must_cover_exactly_the_cases(self):
        notes = json.loads(self.files["annotations"].read_text(encoding="utf-8"))
        self.files["annotations"].write_text(json.dumps(notes[:-1]), encoding="utf-8")
        with self.assertRaisesRegex(freeze.FreezeError, "cover exactly"):
            self.make()

    def test_every_required_input_is_needed(self):
        for name in freeze.REQUIRED_FILES:
            files = {k: v for k, v in self.files.items() if k != name}
            with self.assertRaises(freeze.FreezeError, msg=name):
                freeze.create_freeze(files, self.reviewers, metrics.DECISION_RULE, self.out)


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


class SeedTests(unittest.TestCase):
    def test_derivation_is_deterministic_and_artifact_bound(self):
        a, b = "5" * 40, "6" * 40
        self.assertEqual(seed_mod.derive_seed(a, "calibration-selection"), seed_mod.derive_seed(a, "calibration-selection"))
        self.assertNotEqual(seed_mod.derive_seed(a, "calibration-selection"), seed_mod.derive_seed(b, "calibration-selection"))
        for bad in ("5ca2980", "g" * 40, "5" * 39):
            with self.assertRaises(ValueError):
                seed_mod.derive_seed(bad, "calibration-selection")
        with self.assertRaises(ValueError):
            seed_mod.derive_seed(a, "acceptance")

    def test_the_recorded_seed_for_this_repository_is_the_derived_one_and_not_retired(self):
        path = TESTS_DIR.parent / "docs" / "baselines" / "planner_experiment" / "seed_calibration.json"
        record = seed_mod.load_record(path, "calibration-selection")
        self.assertEqual(record["artifact"], "5ca29805cd892bec6ba1a611b08274af26d663d5")
        self.assertNotIn(record["seed"], seed_mod.RETIRED_SEEDS)

    def test_a_record_is_written_once_and_tampering_is_detected(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "seed.json"
        record = seed_mod.write_record(path, "7" * 40, "calibration-selection")
        with self.assertRaises(FileExistsError):
            seed_mod.write_record(path, "8" * 40, "calibration-selection")
        record["seed"] = 20261014
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            seed_mod.load_record(path, "calibration-selection")

    def test_the_retired_hand_picked_seed_is_refused_even_if_recorded(self):
        with self.assertRaises(ValueError):
            seed_mod.check_record({"purpose": "calibration-selection", "artifact": "a" * 40, "seed": 20261014}, "calibration-selection")

    def test_the_cli_has_no_way_to_pass_a_seed(self):
        from planner_experiment import cli
        with self.assertRaises(SystemExit):
            cli.main(["select", "--seed", "20261014"])


class GoldenTests(unittest.TestCase):
    def entry(self, **kw):
        base = {"ticket_id": "golden-1", "text": "disk full on sapdb01", "host": "sapdb01", "opened_at": "2026-05-01 10:00:00",
                "set": "calibration", "approved_by": ["Ann", "Bob"]}
        base.update(kw)
        return base

    def load(self, *entries, reviewers=None):
        path = pathlib.Path(tempfile.mkdtemp()) / "g.json"
        path.write_text(json.dumps(list(entries)), encoding="utf-8")
        return golden.load_golden(path, reviewers)

    def test_approved_calibration_tickets_load_as_the_golden_pool(self):
        out = self.load(self.entry(), self.entry(ticket_id="golden-2"), reviewers=["Ann", "Bob"])
        self.assertEqual({o["pool"] for o in out}, {"golden"})

    def test_unapproved_acceptance_or_malformed_tickets_are_refused(self):
        for bad in (self.entry(approved_by=["Ann"]), self.entry(approved_by=["Ann", "ann"]), self.entry(approved_by=["Ann", "TBD"]),
                    self.entry(approved_by=[]), self.entry(set="acceptance"), self.entry(set=None), self.entry(ticket_id="T-1"),
                    self.entry(host=""), self.entry(text=" ")):
            with self.assertRaises(golden.GoldenError, msg=bad):
                self.load(bad)
        with self.assertRaises(golden.GoldenError):
            self.load(self.entry(), self.entry())
        with self.assertRaises(golden.GoldenError):
            self.load(self.entry(approved_by=["Ann", "Eve"]), reviewers=["Ann", "Bob"])


if __name__ == "__main__":
    unittest.main()
