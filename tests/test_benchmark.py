"""The model speed benchmark: statistics, the declared warm-up, the verdict against the frozen budgets, and that it never changes them."""
import json
import os
import pathlib
import tempfile
import unittest

from agentruntime.contracts import Budgets
from agentruntime.gateway import ModelResponse, ModelUnavailable
from planner_experiment import benchmark, capture
from planner_experiment.benchmark import percentile, run_benchmark, stats
from tests.test_planner_capture import CASE_X, CASE_Y, capture_to, ctx_factory, tmp


class Ticker:
    """A clock the fake model advances: every model request 'takes' `delta` seconds."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeModel:
    def __init__(self, ticker, delta, fail=False, fail_warmup=False):
        self.ticker, self.delta, self.fail, self.fail_warmup, self.n = ticker, delta, fail, fail_warmup, 0

    def complete(self, system, user, name, schema, max_tokens, timeout_s, cancel=None):
        self.n += 1
        if name == "warmup":
            self.ticker.now += 30                                         # the load time: reported, never counted
            if self.fail_warmup:
                raise ModelUnavailable("not loaded")
            return ModelResponse('{"action":"escalate"}', 10, 5, "google/gemma-3-4b")
        self.ticker.now += self.delta
        if self.fail:
            raise ModelUnavailable("down")
        return ModelResponse('{"action":"escalate"}', 900, 20, "google/gemma-3-4b")


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.out = tmp()
        capture_to(self.out)                                              # an official development recording of CASE_X and CASE_Y
        self.cases = [dict(CASE_X), dict(CASE_Y)]

    def run_with(self, delta, repetitions=2, **kw):
        ticker = Ticker()
        model = FakeModel(ticker, delta, **{k: v for k, v in kw.items() if k in ("fail", "fail_warmup")})
        report = run_benchmark(self.cases, self.out, repetitions, client=model, ctx_factory=ctx_factory, clock=ticker)
        return report, model

    def test_percentiles_use_the_nearest_rank(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
        self.assertEqual(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50), 5)
        self.assertEqual(percentile([7], 95), 7)
        self.assertIsNone(percentile([], 95))
        self.assertEqual(stats([])["n"], 0)

    def test_a_fast_model_fits_the_frozen_budgets(self):
        report, model = self.run_with(delta=3.0)
        self.assertTrue(report["fits_frozen_budgets"])
        self.assertEqual(report["model_requests"]["share_within_call_timeout"], 1.0)
        self.assertLessEqual(report["p95_run_seconds"], Budgets().deadline_s)
        self.assertIn("fits the frozen budgets", report["verdict"])
        self.assertEqual(report["runs"], len(self.cases) * 2)

    def test_a_slow_model_does_not_fit_and_the_verdict_says_what_follows(self):
        report, _ = self.run_with(delta=40.0)
        self.assertFalse(report["fits_frozen_budgets"])
        self.assertGreater(report["p95_call_seconds"], Budgets().model_call_timeout_s)
        self.assertIn("rules_only", report["verdict"])
        self.assertEqual(report["model_requests"]["share_within_call_timeout"], 0.0)

    def test_the_declared_warm_up_is_reported_and_kept_out_of_the_statistics(self):
        report, model = self.run_with(delta=2.0)
        self.assertTrue(report["warmup"]["declared"])
        self.assertEqual(report["warmup"]["seconds"], 30.0)
        self.assertEqual(report["model_requests"]["count"], model.n - 1)              # every request but the warm-up
        self.assertEqual(report["model_requests"]["seconds"]["max"], 2.0)             # the 30 s load is not in it

    def test_a_failed_warm_up_is_recorded_and_failed_requests_are_counted(self):
        report, _ = self.run_with(delta=1.0, fail=True, fail_warmup=True)
        self.assertIn("ModelUnavailable", report["warmup"]["error"])
        self.assertEqual(report["model_requests"]["failed"], report["model_requests"]["count"])
        self.assertTrue(all(r["fallbacks"] for r in report["run_detail"] if r["model_calls"]))        # the rules carried every run

    def test_the_environment_the_model_and_the_prompts_are_recorded(self):
        report, _ = self.run_with(delta=1.0, repetitions=1)
        self.assertTrue(report["environment"]["platform"] and report["environment"]["cpu_count"])
        self.assertEqual(report["model"]["file_sha256"], "be49949e48422e4547b00af14179a193d3777eea7fbbd7d6e1b0861304628a01")
        self.assertTrue(report["manifest_complete"])
        self.assertEqual(set(report["prompt_hashes"]), {"planner", "synthesis"})
        self.assertEqual(report["recording_sha256"], capture.verify_capture(self.out)["recording_sha256"])
        json.dumps(report)                                                                       # serialisable

    def test_the_frozen_budgets_and_the_environment_are_left_unchanged(self):
        before = (Budgets().model_dump(), os.environ.get(benchmark.BENCH_ENV))
        report, _ = self.run_with(delta=1.0, repetitions=1)
        self.assertEqual((Budgets().model_dump(), os.environ.get(benchmark.BENCH_ENV)), before)
        self.assertEqual(report["frozen_budgets"]["model_call_timeout_s"], 15)
        self.assertEqual(report["frozen_budgets"]["deadline_s"], 40)

    def test_only_an_official_development_recording_for_exactly_these_cases_is_accepted(self):
        with self.assertRaises(capture.CaptureError):
            run_benchmark([dict(CASE_X)], self.out, 1, client=FakeModel(Ticker(), 1), ctx_factory=ctx_factory)          # a different case set
        other = tmp()
        capture_to(other, mode="experiment", key=b"k" * 32, key_check="c")
        with self.assertRaises(capture.CaptureError):
            run_benchmark(self.cases, other, 1, client=FakeModel(Ticker(), 1), ctx_factory=ctx_factory)                 # a hidden-set capture is never benchmarked
        with self.assertRaises(ValueError):
            run_benchmark(self.cases, self.out, 0, client=FakeModel(Ticker(), 1), ctx_factory=ctx_factory)

    def test_the_command_exists_and_reports_a_nonzero_exit_when_the_model_does_not_fit(self):
        from unittest.mock import patch
        from planner_experiment import cli
        cases_file = pathlib.Path(tempfile.mkdtemp()) / "cases.json"
        cases_file.write_text(json.dumps(self.cases), encoding="utf-8")
        report_file = cases_file.with_name("report.json")
        slow = {"fits_frozen_budgets": False, "verdict": "no", "environment": {}, "warmup": {}, "runs": 0, "p95_call_seconds": 99, "p95_run_seconds": 99}
        with patch.object(benchmark, "run_benchmark", return_value=slow):
            code = cli.main(["benchmark-model", "--cases", str(cases_file), "--recording", str(self.out), "--out", str(report_file)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(report_file.read_text(encoding="utf-8"))["fits_frozen_budgets"], False)


if __name__ == "__main__":
    unittest.main()
