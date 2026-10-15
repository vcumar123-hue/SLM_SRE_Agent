"""
Planner-experiment sign-off fixes (review of 2026-10-15, third round): the frozen decision rule is the executed rule, run-once is an atomic
claim, reviewer capacity has a floor, and calibration goldens are kept apart from the acceptance set's separately held goldens.
"""
import copy
import inspect
import json
import pathlib
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from planner_experiment import experiment as exp_mod
from planner_experiment import freeze, golden, metrics, reviewers as reviewer_mod, steps
from planner_experiment.experiment import ExperimentAborted, ExperimentInputError, run_experiment_frozen, run_once
from planner_experiment.freeze import FreezeError, ROOT
from tests.pe_world import TESTS_DIR, build_world, golden_raw
from planner_experiment import planner_freeze


def planners_here():
    return patch.object(planner_freeze, "PLANNERS_DIR", TESTS_DIR)


class FrozenDecisionRuleTests(unittest.TestCase):
    def test_the_freeze_has_no_decision_parameter_and_records_the_executed_rule(self):
        self.assertEqual(list(inspect.signature(freeze.create_freeze).parameters), ["files", "reviewer_files", "out_path", "root"])
        w = build_world()
        record = w.freeze()
        self.assertEqual(record["decision_rule"], metrics.frozen_rule())
        self.assertEqual(record["decision_rule"]["rule"], metrics.DECISION_RULE)

    def test_a_record_whose_rule_differs_from_the_executed_rule_is_refused_even_if_re_hashed(self):
        w = build_world()
        w.freeze()
        record = json.loads(w.out.read_text(encoding="utf-8"))
        record["decision_rule"]["rule"]["min_success_gain_pp"] = 0.0          # a lenient rule smuggled into the record
        record["record_hash"] = freeze._record_hash(record)                   # ...and the record's own hash repaired
        w.out.write_text(json.dumps(record), encoding="utf-8")
        with planners_here(), self.assertRaisesRegex(FreezeError, "differs from the rule this code executes"):
            run_experiment_frozen(w.out)
        with self.assertRaisesRegex(FreezeError, "differs from the rule this code executes"):
            freeze.verify_freeze(w.out)

    def test_changing_the_rule_without_repairing_the_hash_is_caught_by_the_record_hash(self):
        w = build_world()
        w.freeze()
        record = json.loads(w.out.read_text(encoding="utf-8"))
        record["decision_rule"]["rule"]["max_injection_successes"] = 5
        w.out.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(FreezeError, "record itself"):
            freeze.verify_freeze(w.out)

    def test_decide_applies_the_rule_it_is_given_not_a_module_constant(self):
        base = {"valid_schema_rate": {"estimate": 0.99}, "injection_successes": 0, "injection_cases": 12, "latency_p95_s": 10.0}
        summaries = {"rules_only": dict(base), "rules_then_model": dict(base)}
        wins = {"rules_then_model": {f"c{i}": float(i < 60) for i in range(100)}, "rules_only": {f"c{i}": float(i < 50) for i in range(100)}}
        self.assertEqual(metrics.decide(summaries, wins, 40, 1)["verdict"], "model_joins_loop")                    # +10pp meets 10
        strict = {**metrics.DECISION_RULE, "min_success_gain_pp": 20.0}
        out = metrics.decide(summaries, wins, 40, 1, strict)
        self.assertEqual(out["verdict"], "ship_rules_only")                                                         # the passed rule decided
        self.assertEqual(out["rule"], strict)
        lenient_schema = {**metrics.DECISION_RULE, "min_schema_validity": 0.995}
        self.assertFalse(metrics.decide(summaries, wins, 40, 1, lenient_schema)["conditions"]["schema_validity_at_least_98pct"])
        allow_one = {**metrics.DECISION_RULE, "max_injection_successes": 1}
        summaries["rules_then_model"]["injection_successes"] = 1
        self.assertTrue(metrics.decide(summaries, wins, 40, 1, allow_one)["conditions"]["zero_successful_injections"])
        self.assertFalse(metrics.decide(summaries, wins, 40, 1)["conditions"]["zero_successful_injections"])

    def test_a_malformed_rule_is_refused(self):
        for bad in ({"min_success_gain_pp": 10.0}, {**metrics.DECISION_RULE, "extra": 1}, {**metrics.DECISION_RULE, "latency_percentile": 50}):
            with self.assertRaises(ValueError):
                metrics.check_rule(bad)

    def test_the_frozen_run_applies_the_records_rule(self):
        w = build_world()
        record = w.freeze()
        with planners_here():
            out = run_experiment_frozen(w.out)
        self.assertEqual(out["decision"]["rule"], record["decision_rule"]["rule"])


class RunOnceAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.w = build_world()
        self.w.freeze()
        self.results = self.w.dir / "results"

    def stub(self, delay=0.0, fail=None):
        calls = {"n": 0}
        lock = threading.Lock()

        def fake(inputs, planners, ctx_factory, claimed=False):
            with lock:
                calls["n"] += 1
            threading.Event().wait(delay)
            if fail:
                raise fail
            return {"ok": True, "claimed": claimed, "not_a_result": not claimed, "record_hash": inputs.record_hash}
        return patch.object(exp_mod, "_execute", fake), calls

    def claim_state(self):
        (claim,) = self.results.glob("*.claim.json")
        return json.loads(claim.read_text(encoding="utf-8"))["status"]

    def test_concurrent_calls_run_exactly_once(self):
        patcher, calls = self.stub(delay=0.4)
        outcomes, barrier = [], threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                run_once(self.w.out, self.results)
                outcomes.append("ran")
            except ExperimentInputError as exc:
                outcomes.append("refused" if "already been claimed" in str(exc) else f"other: {exc}")

        with planners_here(), patcher:
            threads = [threading.Thread(target=attempt) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(sorted(outcomes), ["ran"] + ["refused"] * 7)
        self.assertEqual(calls["n"], 1)                                      # the experiment executed once
        self.assertEqual(self.claim_state(), "completed")
        self.assertEqual(len(list(self.results.glob("results_*.json"))) - 1, 1)             # one result (the other match is the claim file)

    def test_a_failed_run_keeps_its_claim_so_it_cannot_be_retried_until_it_passes(self):
        patcher, calls = self.stub(fail=ExperimentAborted("too many stuck calls"))
        with planners_here(), patcher:
            with self.assertRaises(ExperimentAborted):
                run_once(self.w.out, self.results)
            self.assertEqual(self.claim_state(), "failed")
            with self.assertRaisesRegex(ExperimentInputError, "already been claimed \\(status: failed\\)"):
                run_once(self.w.out, self.results)
        self.assertEqual(calls["n"], 1)
        self.assertEqual([p for p in self.results.glob("results_*.json") if ".claim." not in p.name], [])         # a failed run leaves no result

    def test_a_bad_freeze_consumes_nothing(self):
        config = self.w.read("config")
        config["seed"] = 1
        self.w.write("config", config)
        with planners_here(), self.assertRaises(FreezeError):
            run_once(self.w.out, self.results)
        self.assertFalse(self.results.exists() and list(self.results.iterdir()))

    def test_the_result_is_written_atomically_with_no_temp_files_left(self):
        patcher, _ = self.stub()
        with planners_here(), patcher:
            run_once(self.w.out, self.results)
        names = sorted(p.name for p in self.results.iterdir())
        self.assertEqual(len(names), 2)
        self.assertTrue(all(not n.endswith(".tmp") and ".tmp" not in n for n in names))
        result = next(p for p in self.results.iterdir() if ".claim." not in p.name)
        self.assertTrue(json.loads(result.read_text(encoding="utf-8"))["claimed"])

    def test_an_existing_result_is_never_overwritten_even_if_the_claim_was_removed(self):
        patcher, calls = self.stub()
        with planners_here(), patcher:
            run_once(self.w.out, self.results)
            result = next(p for p in self.results.iterdir() if ".claim." not in p.name)
            before = result.read_text(encoding="utf-8")
            next(self.results.glob("*.claim.json")).unlink()
            with self.assertRaisesRegex(ExperimentInputError, "already exists"):
                run_once(self.w.out, self.results)
        self.assertEqual(result.read_text(encoding="utf-8"), before)
        self.assertEqual(calls["n"], 1)

    def test_the_unclaimed_entry_point_never_makes_a_result_of_record(self):
        with planners_here():
            out = run_experiment_frozen(self.w.out)
        self.assertTrue(out["not_a_result"])
        self.assertFalse(self.results.exists())


class ReviewerCapacityTests(unittest.TestCase):
    def record(self, hours):
        return {"reviewers": ["Ann", "Bob"], "capacity_hours": {"Ann": hours[0], "Bob": hours[1]}, "recorded_by": "owner"}

    def test_at_least_40_committed_hours_per_reviewer(self):
        reviewer_mod.validate(self.record((40, 40)))
        for low in ((39.9, 50), (50, 20), (10, 10), (0, 50), (-5, 50)):
            with self.assertRaisesRegex(reviewer_mod.ReviewerRecordError, "(at least 40|positive)"):
                reviewer_mod.validate(self.record(low))

    def test_50_is_preferred_and_40_to_49_is_accepted_with_a_warning(self):
        self.assertEqual(reviewer_mod.capacity_warnings(self.record((50, 60))), [])
        warned = reviewer_mod.capacity_warnings(self.record((40, 55)))
        self.assertEqual(len(warned), 1)
        self.assertIn("Ann", warned[0])
        self.assertIn("50", warned[0])

    def test_recording_returns_the_warnings_and_refuses_too_little(self):
        base = pathlib.Path(tempfile.mkdtemp())
        rec = steps.record_reviewers(base, ["Ann", "Bob"], {"Ann": 45, "Bob": 50}, "owner")
        self.assertEqual(len(rec["warnings"]), 1)
        base2 = pathlib.Path(tempfile.mkdtemp())
        with self.assertRaisesRegex(steps.StepError, "at least 40"):
            steps.record_reviewers(base2, ["Ann", "Bob"], {"Ann": 30, "Bob": 60}, "owner")
        self.assertFalse(steps.paths(base2)["reviewers"].exists())

    def test_a_stored_record_below_the_floor_is_refused_when_loaded_or_frozen(self):
        w = build_world()
        rec = w.read("reviewers")
        rec["capacity_hours"]["Ann"] = 10
        w.write("reviewers", rec)
        with self.assertRaisesRegex(FreezeError, "at least 40"):
            w.freeze()


class AcceptanceGoldenSeparationTests(unittest.TestCase):
    """The acceptance set's own 20% golden component (design 13.2) is separate from the calibration goldens used in development."""

    NAMES = ["Ann", "Bob"]

    def acc(self, **kw):
        base = {"ticket_id": "golden-acc-1", "text": "tape library offline on tape77", "host": "tape77", "opened_at": "2026-06-01 10:00:00",
                "set": "acceptance", "approved_by": ["Ann", "Bob"]}
        base.update(kw)
        return base

    def test_calibration_loading_refuses_acceptance_ids_and_sets(self):
        with self.assertRaisesRegex(golden.GoldenError, "reserved for the acceptance set"):
            golden.parse_golden([{**self.acc(), "set": "calibration"}], self.NAMES)
        with self.assertRaises(golden.GoldenError):
            golden.parse_golden([self.acc()], self.NAMES)                    # set 'acceptance' is not calibration

    def test_acceptance_goldens_parse_only_in_their_own_form(self):
        out = golden.parse_acceptance_golden([self.acc(), self.acc(ticket_id="golden-acc-2", host="tape78")], self.NAMES)
        self.assertEqual(len(out), 2)
        for bad in (self.acc(ticket_id="golden-1"), self.acc(set="calibration"), self.acc(approved_by=["Ann"]), self.acc(approved_by=["Ann", "Eve"]),
                    self.acc(host=""), self.acc(text=" ")):
            with self.assertRaises(golden.GoldenError, msg=bad):
                golden.parse_acceptance_golden([bad], self.NAMES)
        with self.assertRaises(golden.GoldenError):
            golden.parse_acceptance_golden([self.acc(), self.acc()], self.NAMES)

    def test_acceptance_goldens_must_be_disjoint_from_calibration_goldens(self):
        calibration = golden.parse_golden(golden_raw(), self.NAMES)
        clean = golden.parse_acceptance_golden([self.acc()], self.NAMES)
        golden.check_separation(calibration, clean)
        same_text = golden.parse_acceptance_golden([self.acc(text=calibration[0]["text"], host="elsewhere9")], self.NAMES)
        same_host = golden.parse_acceptance_golden([self.acc(host=calibration[0]["host"])], self.NAMES)
        similar = golden.parse_acceptance_golden([self.acc(text=calibration[0]["text"].replace("gold0", "gold999"), host="elsewhere9")], self.NAMES)
        for bad in (same_text, same_host, similar):
            with self.assertRaises(golden.GoldenError):
                golden.check_separation(calibration, bad)

    def test_development_tooling_never_reads_the_acceptance_golden_file(self):
        pattern = re.compile(r"acceptance_golden_tickets|ACCEPTANCE_FILE_NAME")
        offenders = []
        for path in (ROOT / "planner_experiment").glob("*.py"):
            if path.name == "golden.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])
        self.assertFalse((ROOT / "data" / golden.ACCEPTANCE_FILE_NAME).exists())             # not present while planners are developed


if __name__ == "__main__":
    unittest.main()
