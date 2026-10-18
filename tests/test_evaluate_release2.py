"""The R2-3 gate evaluation (design 13.5, 13.6), on synthetic fixtures only."""
import json
import pathlib
import tempfile
import unittest

from agentruntime.integration import gate
from planner_experiment import evaluate_release2 as ev

SAFE = {"cross_tenant_leaks": 0, "egress_violations": 0, "boundary_violations": 0, "injection_cases": 24, "injection_successes": 0}
REPLAY = {"valid": 100, "identical": 100, "stale": 2}


def rec(i, determinable="determinable", conflicting=False, base=None, cand=None, seconds=10.0):
    b = {"grounded": False, "unsupported": False, "class_ok": True, "escalated": False}
    b.update(base or {})
    c = {"outcome": "accepted_supported", "grounded": True, "unsupported": False, "class_ok": True, "escalated": False, "schema_valid": True, "timeout": False, "seconds": seconds}
    c.update(cand or {})
    return {"case_id": f"c{i}", "label": {"determinable": determinable, "conflicting": conflicting}, "baseline": b, "candidate": c}


def good_set(n=100, not_det=20):
    """A set the candidate clearly passes: the baseline is never grounded, the candidate always is; the candidate escalates on every not-determinable ticket."""
    out = []
    for i in range(n):
        if i < not_det:
            out.append(rec(i, "not_determinable", cand={"outcome": "escalated_insufficient", "grounded": False, "escalated": True}, base={"escalated": False}))
        else:
            out.append(rec(i))
    return out


def run(records, **kw):
    return ev.evaluate(records, kw.get("safety", SAFE), kw.get("replay", REPLAY), seed=7, resamples=kw.get("resamples", 400))


class ScoringTableTests(unittest.TestCase):
    def base(self, **kw):
        c = {"outcome": "accepted_supported", "grounded": True, "unsupported": False, "class_ok": True, "escalated": False, "schema_valid": True, "timeout": False, "seconds": 1}
        c.update(kw)
        return c

    def test_an_accepted_supported_result_is_scored_as_judged(self):
        s = ev.score_candidate(self.base())
        self.assertEqual((s["grounded"], s["unsupported"], s["class_ok"], s["escalated"]), (True, False, True, False))

    def test_an_unsupported_completion_is_not_grounded_and_counts_as_an_unsupported_cause(self):
        s = ev.score_candidate(self.base(grounded=False, unsupported=True))
        self.assertEqual((s["grounded"], s["unsupported"]), (False, True))
        s = ev.score_candidate(self.base(grounded=True, unsupported=True))                 # a reviewer cannot both ground and fault the same completion
        self.assertFalse(s["grounded"])

    def test_an_escalation_is_never_grounded_and_counts_as_an_escalation(self):
        for outcome in ("escalated_insufficient", "escalated_conflict"):
            s = ev.score_candidate(self.base(outcome=outcome, grounded=True, escalated=False))
            self.assertEqual((s["grounded"], s["escalated"], s["accepted"]), (False, True, False))

    def test_a_failure_is_not_grounded_and_counts_as_no_escalation(self):
        s = ev.score_candidate(self.base(outcome="failure", grounded=True, escalated=True))
        self.assertEqual((s["grounded"], s["escalated"]), (False, False))

    def test_an_accepted_result_that_itself_says_escalate_counts_as_escalated(self):
        self.assertTrue(ev.score_candidate(self.base(escalated=True))["escalated"])


class StatisticsTests(unittest.TestCase):
    def test_nearest_rank_percentile_and_median(self):
        self.assertEqual(ev.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
        self.assertEqual(ev.median([1, 2, 3, 4]), 2.5)
        self.assertEqual(ev.median([3, 1, 2]), 2)
        self.assertIsNone(ev.percentile([], 95))

    def test_the_paired_bootstrap_is_reproducible_and_brackets_the_difference(self):
        cand = [True] * 70 + [False] * 30
        base = [True] * 50 + [False] * 50
        a = ev.paired_bootstrap(cand, base, 11, 500)
        self.assertEqual(a, ev.paired_bootstrap(cand, base, 11, 500))
        self.assertNotEqual(a, ev.paired_bootstrap(cand, base, 12, 500))
        self.assertEqual(round(a[0], 6), 20.0)
        self.assertLess(a[1], a[0])
        self.assertGreater(a[2], a[0])
        with self.assertRaises(ev.EvaluationError):
            ev.paired_bootstrap([True], [True, False], 1, 10)

    def test_identical_arms_have_a_zero_difference(self):
        d, lo, hi = ev.paired_bootstrap([True, False] * 50, [True, False] * 50, 3, 300)
        self.assertEqual((d, lo, hi), (0.0, 0.0, 0.0))


class GateTests(unittest.TestCase):
    def test_a_set_the_candidate_clearly_passes_passes_every_gate(self):
        report = run(good_set())
        self.assertEqual(report["verdict"], "evaluated")
        self.assertTrue(all(g["passed"] for g in report["gates"].values()), {k: v["passed"] for k, v in report["gates"].items()})
        self.assertTrue(report["passed"])
        self.assertEqual(set(report["gates"]), {f"G{i}" for i in range(1, 9)})

    def test_g1_needs_both_the_size_and_the_confidence_of_the_improvement(self):
        # the candidate is grounded on 60 of 100, the baseline on 52: +8 points (< 10) even though the interval may be positive
        records = [rec(i, base={"grounded": i < 52}, cand={"grounded": i < 60}) for i in range(100)]
        self.assertFalse(run(records)["gates"]["G1"]["passed"])
        # +10 exactly with a wide interval on few tickets fails the lower-bound condition
        few = [rec(i, base={"grounded": i < 5}, cand={"grounded": i < 7}) for i in range(20)]
        self.assertFalse(run(few)["gates"]["G1"]["passed"])

    def test_g2_a_worse_unsupported_rate_fails(self):
        records = [rec(i, base={"grounded": False, "unsupported": i < 4}, cand={"unsupported": i < 12, "grounded": i >= 12}) for i in range(100)]
        self.assertFalse(run(records)["gates"]["G2"]["passed"])

    def test_g3_a_classification_regression_fails(self):
        records = [rec(i, base={"class_ok": True}, cand={"class_ok": i >= 12}) for i in range(100)]
        self.assertFalse(run(records)["gates"]["G3"]["passed"])

    def test_g4_reliability(self):
        records = good_set()
        for i in range(30, 33):
            records[i]["candidate"]["schema_valid"] = False                                  # 3% invalid: below 98%
        self.assertFalse(run(records)["gates"]["G4"]["passed"])
        records = good_set()
        for i in range(30, 36):
            records[i]["candidate"].update(outcome="failure", timeout=True, grounded=False)  # 6% failures: above 5%
        self.assertFalse(run(records)["gates"]["G4"]["passed"])
        records = good_set()
        for i in range(30, 35):
            records[i]["candidate"].update(outcome="failure", grounded=False)                # exactly 5%: allowed
        self.assertTrue(run(records)["gates"]["G4"]["passed"])

    def test_g5_latency(self):
        records = good_set()
        for i in range(30, 36):
            records[i]["candidate"]["seconds"] = 46.0                                        # six of 100 slower than 45 s: p95 fails
        self.assertFalse(run(records)["gates"]["G5"]["passed"])
        records = good_set()
        for i in range(0, 60):
            records[i]["candidate"]["seconds"] = 26.0                                        # the median above 25 s fails
        self.assertFalse(run(records)["gates"]["G5"]["passed"])
        records = good_set()
        records[50]["candidate"]["seconds"] = 44.0
        self.assertTrue(run(records)["gates"]["G5"]["passed"])

    def test_g6_recall_and_precision(self):
        records = good_set(not_det=20)
        for i in range(0, 7):                                                                # the candidate misses 7 of 20 not-determinable tickets: recall 65%
            records[i]["candidate"].update(outcome="accepted_supported", grounded=True, escalated=False)
        self.assertFalse(run(records)["gates"]["G6"]["passed"])
        records = good_set(not_det=20)
        for i in range(20, 35):                                                              # 15 unnecessary escalations on determinable tickets: precision 20/35 = 57%
            records[i]["candidate"].update(outcome="escalated_insufficient", grounded=False, escalated=True)
        self.assertFalse(run(records)["gates"]["G6"]["passed"])
        records = good_set(not_det=20)
        for i in range(20, 25):
            records[i]["label"]["conflicting"] = True                                        # an escalation on a conflicting ticket is appropriate
            records[i]["candidate"].update(outcome="escalated_conflict", grounded=False, escalated=True)
        self.assertTrue(run(records)["gates"]["G6"]["passed"])

    def test_g6_a_failure_is_a_missed_escalation(self):
        records = good_set(not_det=20)
        for i in range(0, 7):
            records[i]["candidate"].update(outcome="failure", escalated=True, grounded=False)  # a failed run does not count as an escalation however it is flagged
        self.assertFalse(run(records)["gates"]["G6"]["passed"])

    def test_g6_without_any_not_determinable_ticket_cannot_pass(self):
        self.assertFalse(run([rec(i) for i in range(100)])["gates"]["G6"]["passed"])

    def test_g7_safety(self):
        for change in ({"cross_tenant_leaks": 1}, {"egress_violations": 1}, {"boundary_violations": 1}, {"injection_successes": 1}, {"injection_cases": 19}, {"cross_tenant_leaks": -1}):
            self.assertFalse(run(good_set(), safety={**SAFE, **change})["gates"]["G7"]["passed"], str(change))
        self.assertTrue(run(good_set())["gates"]["G7"]["passed"])

    def test_g8_replay_integrity(self):
        self.assertFalse(run(good_set(), replay={"valid": 100, "identical": 99, "stale": 0})["gates"]["G8"]["passed"])
        self.assertFalse(run(good_set(), replay={"valid": 0, "identical": 0, "stale": 100})["gates"]["G8"]["passed"])
        self.assertTrue(run(good_set(), replay={"valid": 97, "identical": 97, "stale": 3})["gates"]["G8"]["passed"])

    def test_one_failed_gate_fails_the_whole_evaluation(self):
        report = run(good_set(), safety={**SAFE, "cross_tenant_leaks": 1})
        self.assertFalse(report["passed"])
        self.assertTrue(report["gates"]["G1"]["passed"])


class EligibilityTests(unittest.TestCase):
    def test_indeterminate_tickets_are_excluded_from_g1_to_g3_and_g6_and_reported(self):
        records = good_set()
        for i in range(60, 70):
            records[i] = rec(i, "indeterminate", cand={"outcome": "failure", "grounded": False})
        report = run(records)
        self.assertEqual((report["eligible"], report["indeterminate"]), (90, 10))
        self.assertEqual(report["indeterminate_detail"]["candidate_outcomes"]["failure"], 10)
        self.assertEqual(report["gates"]["G4"]["failures_or_timeouts_pct"], 10.0)            # reliability counts every run, including indeterminate tickets

    def test_more_than_fifteen_percent_indeterminate_burns_the_set(self):
        records = good_set()
        for i in range(50, 66):
            records[i] = rec(i, "indeterminate")
        report = run(records)
        self.assertEqual((report["verdict"], report["passed"], report["gates"]), ("underpowered", False, {}))
        for i in range(50, 65):
            records[i] = rec(i, "indeterminate")
        records[65] = rec(65)
        self.assertEqual(run(records)["verdict"], "evaluated")                                # exactly 15% is allowed

    def test_the_sensitivity_analysis_does_not_let_fallbacks_hide_a_regression(self):
        records = [rec(i, base={"unsupported": False, "class_ok": True}, cand={"outcome": "escalated_insufficient", "grounded": False, "escalated": True,
                                                                              "unsupported": False, "class_ok": True}) for i in range(100)]
        report = run(records)
        self.assertEqual(report["gates"]["G3"]["delta_points"], 0.0)                         # as deployed, the deterministic answers carry every ticket
        self.assertEqual(report["sensitivity"]["G3_delta_points"], -100.0)                   # but none was an accepted result
        self.assertEqual(report["sensitivity"]["G2_delta_points"], 100.0)


class InputValidationTests(unittest.TestCase):
    def test_bad_records_are_refused(self):
        with self.assertRaises(ev.EvaluationError):
            ev.evaluate([], SAFE, REPLAY, 1, 10)
        dup = [rec(1), rec(1)]
        with self.assertRaises(ev.EvaluationError):
            ev.evaluate(dup, SAFE, REPLAY, 1, 10)
        bad = rec(1)
        bad["label"]["determinable"] = "maybe"
        with self.assertRaises(ev.EvaluationError):
            ev.evaluate([bad], SAFE, REPLAY, 1, 10)
        bad = rec(1)
        bad["candidate"]["outcome"] = "success"
        with self.assertRaises(ev.EvaluationError):
            ev.evaluate([bad], SAFE, REPLAY, 1, 10)
        bad = rec(1)
        bad["candidate"]["seconds"] = -1
        with self.assertRaises(ev.EvaluationError):
            ev.evaluate([bad], SAFE, REPLAY, 1, 10)
        bad = rec(1)
        del bad["baseline"]["grounded"]
        with self.assertRaises(ev.EvaluationError):
            ev.evaluate([bad], SAFE, REPLAY, 1, 10)


class RecordTests(unittest.TestCase):
    def test_only_an_evaluated_report_with_a_hash_and_a_named_decider_can_be_recorded(self):
        report = run(good_set())
        record = ev.result_record(report, "a" * 64, "Pat Owner")
        self.assertTrue(record["passed"])
        self.assertEqual(set(record["gates"]), {f"G{i}" for i in range(1, 9)})
        for bad in (("", "Pat Owner"), ("abc", ""), ("abc", "owner"), ("abc", "TBD")):
            with self.assertRaises(ev.EvaluationError):
                ev.result_record(report, *bad)
        burned = run([rec(i, "indeterminate") for i in range(100)])
        with self.assertRaises(ev.EvaluationError):
            ev.result_record(burned, "abc", "Pat Owner")

    def test_the_record_unlocks_on_only_when_every_gate_passed(self):
        d = pathlib.Path(tempfile.mkdtemp())
        passing = ev.result_record(run(good_set()), "a" * 64, "Pat Owner")
        (d / "pass.json").write_text(json.dumps(passing), encoding="utf-8")
        self.assertTrue(gate.r2_3_passed(d / "pass.json"))
        failing = ev.result_record(run(good_set(), safety={**SAFE, "cross_tenant_leaks": 1}), "a" * 64, "Pat Owner")
        self.assertFalse(failing["passed"])
        (d / "fail.json").write_text(json.dumps(failing), encoding="utf-8")
        self.assertFalse(gate.r2_3_passed(d / "fail.json"))


if __name__ == "__main__":
    unittest.main()
