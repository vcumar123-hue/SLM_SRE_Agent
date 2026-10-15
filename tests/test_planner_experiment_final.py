"""
Planner-experiment sign-off fixes (review of 2026-10-15, third round): the frozen decision rule is the executed rule, run-once is an atomic
claim, reviewer capacity has a floor, and calibration goldens are kept apart from the acceptance set's separately held goldens.
"""
import copy
import inspect
import json
import os
import pathlib
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from planner_experiment import experiment as exp_mod
from planner_experiment import freeze, golden, metrics, reviewers as reviewer_mod, steps
from planner_experiment.experiment import ExperimentAborted, ExperimentInputError, run_once
from planner_experiment.freeze import FreezeError, ROOT
from tests.pe_world import TEST_KEY, TEST_KEY_HEX, TESTS_DIR, build_world, golden_raw
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
            run_once(w.out, w.dir / "results")
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
            out = run_once(w.out, w.dir / "results")
        self.assertEqual(out["decision"]["rule"], record["decision_rule"]["rule"])


class RunOnceAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.w = build_world()
        self.w.freeze()
        self.results = self.w.dir / "results"

    def stub(self, delay=0.0, fail=None):
        calls = {"n": 0}
        lock = threading.Lock()

        def fake(inputs, planners, ctx_factory, claim=None):
            with lock:
                calls["n"] += 1
            threading.Event().wait(delay)
            if fail:
                raise fail
            return {"ok": True, "claimed": claim is not None, "not_a_result": claim is None, "record_hash": inputs.record_hash}
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


class NoUnclaimedPathTests(unittest.TestCase):
    """There is no callable path that executes the frozen cases without consuming the run claim."""

    def setUp(self):
        self.w = build_world()
        self.w.freeze()
        self.results = self.w.dir / "results"
        point = patch.object(exp_mod, "_DRAWN_BASE", self.w.base)          # tests point the (module-private) guard at their own draws
        point.start()
        self.addCleanup(point.stop)

    def spy_runs(self):
        """Patch Runner.run_batch to record, at each executed case, whether the claim file already existed."""
        seen = []
        original = exp_mod.Runner.run_batch
        results = self.results

        def spy(runner, *args, **kwargs):
            seen.append(any(results.glob("*.claim.json")))
            return original(runner, *args, **kwargs)

        return patch.object(exp_mod.Runner, "run_batch", spy), seen

    def unfrozen_args(self, cases, recording=None):
        from planner_experiment import annotations as ann_mod
        from planner_experiment import injection
        from planner_experiment.scripted_env import Recording
        from tests.pe_fake_planners import GiveUpPlanner, GoodPlanner
        from tests.test_planner_experiment import ctx_for

        notes = {a.case_id: a for a in ann_mod.parse(self.w.read("annotations"))}
        return (cases, notes, recording or Recording(self.w.read("recording")),
                {"rules_only": GiveUpPlanner, "rules_then_model": GoodPlanner, "model": GoodPlanner}, ctx_for,
                injection.build_injection_cases([c["case_id"] for c in cases[:4]]))

    def test_the_public_surface_has_exactly_two_ways_to_run(self):
        public = sorted(n for n, v in vars(exp_mod).items() if not n.startswith("_") and callable(v) and getattr(v, "__module__", "") == exp_mod.__name__
                        and n.startswith(("run", "execute", "load_frozen")))
        self.assertEqual(public, ["run_experiment_unfrozen", "run_once"])
        self.assertFalse(hasattr(exp_mod, "run_experiment_frozen"))
        self.assertFalse(hasattr(exp_mod, "load_frozen_inputs"))           # nothing public hands out the frozen cases either

    def test_every_executed_frozen_case_ran_after_the_claim_existed(self):
        patcher, seen = self.spy_runs()
        with planners_here(), patcher:
            run_once(self.w.out, self.results)
        self.assertGreater(len(seen), 50 * 3)                              # all cases in all modes, plus the injection runs
        self.assertTrue(all(seen))                                         # none ran before the claim file existed

    def test_the_engine_refuses_frozen_inputs_without_the_claim_object(self):
        inputs = exp_mod._load_frozen_inputs(self.w.out)
        planners = {m: object for m in exp_mod.MODES}
        patcher, seen = self.spy_runs()
        with patcher:
            with self.assertRaisesRegex(ExperimentInputError, "under the run claim"):
                exp_mod._execute(inputs, planners, None)
            with self.assertRaisesRegex(ExperimentInputError, "under the run claim"):
                exp_mod._execute(inputs, planners, None, claim=object())
            other = exp_mod._RunClaim(exp_mod._CLAIM_KEY, "0" * 64)               # a claim for a different freeze record
            with self.assertRaisesRegex(ExperimentInputError, "own freeze record"):
                exp_mod._execute(inputs, planners, None, claim=other)
        self.assertEqual(seen, [])
        self.assertFalse(self.results.exists() and list(self.results.iterdir()))   # and none of it created a claim

    def test_a_run_claim_cannot_be_built_outside_run_once(self):
        with self.assertRaises(PermissionError):
            exp_mod._RunClaim(object(), "x")
        with self.assertRaises(PermissionError):
            exp_mod._RunClaim(None, "x")

    def test_the_unfrozen_route_refuses_the_drawn_cases_by_id(self):
        patcher, seen = self.spy_runs()
        with patcher, self.assertRaisesRegex(ExperimentInputError, "drawn into a planner-experiment set"):
            exp_mod.run_experiment_unfrozen(*self.unfrozen_args(self.w.cases), seed=1)
        self.assertEqual(seen, [])

    def test_renaming_the_cases_does_not_get_around_it_the_content_hash_matches(self):
        renamed = [{**c, "case_id": f"preview-{i}"} for i, c in enumerate(self.w.cases)]
        from planner_experiment.scripted_env import Recording
        rec = Recording({c["case_id"]: self.w.read("recording")[old["case_id"]] for c, old in zip(renamed, self.w.cases)})
        patcher, seen = self.spy_runs()
        with patcher, self.assertRaisesRegex(ExperimentInputError, "drawn into a planner-experiment set"):
            from planner_experiment import annotations as ann_mod
            notes = {f"preview-{i}": ann_mod.parse([{**a.model_dump(), "case_id": f"preview-{i}"}])[0]
                     for i, a in enumerate(ann_mod.parse(self.w.read("annotations")))}
            args = list(self.unfrozen_args(renamed, rec))
            args[1] = notes
            exp_mod.run_experiment_unfrozen(*args, seed=1)
        self.assertEqual(seen, [])

    def test_calling_the_engine_directly_on_unfrozen_inputs_holding_drawn_cases_is_refused_too(self):
        from planner_experiment import annotations as ann_mod
        from planner_experiment.scripted_env import Recording
        inputs = exp_mod.ExperimentInputs(
            cases=self.w.cases, annotations={a.case_id: a for a in ann_mod.parse(self.w.read("annotations"))},
            recording=Recording(self.w.read("recording")), injection_cases=[], budgets=exp_mod.Budgets(), seed=1, strict=False, frozen=False)
        with self.assertRaisesRegex(ExperimentInputError, "drawn into a planner-experiment set"):
            exp_mod._execute(inputs, {m: object for m in exp_mod.MODES}, None, None)

    def test_a_claim_cannot_be_given_to_unfrozen_inputs(self):
        cases, notes, rec, _f, _c, _i = self.unfrozen_args([{"case_id": "x1", "tenant_id": "t", "ticket_text": "disk full"}])
        inputs = exp_mod.ExperimentInputs(cases=cases, annotations={}, recording=rec, injection_cases=[], budgets=exp_mod.Budgets(), seed=1,
                                          strict=False, frozen=False)
        with self.assertRaisesRegex(ExperimentInputError, "belongs to frozen inputs"):
            exp_mod._execute(inputs, {}, None, exp_mod._RunClaim(exp_mod._CLAIM_KEY, "a"))

    def test_cases_that_were_not_drawn_still_run_unfrozen_for_the_harness_self_tests(self):
        from tests.test_planner_experiment import annotation, drafts_for
        from planner_experiment import injection
        from planner_experiment.scripted_env import Recording
        from tests.pe_fake_planners import GiveUpPlanner, GoodPlanner
        from tests.test_planner_experiment import ctx_for

        cases = [{"case_id": f"synthetic-{i}", "tenant_id": "t", "ticket_text": f"disk full on host{i}"} for i in range(4)]
        rec = Recording()
        for c in cases:
            rec.add(c["case_id"], "incident_kb_search", {"query": c["ticket_text"], "top_k": 5}, drafts_for("t", ("incident", "I1", "disk full")))
        out = exp_mod.run_experiment_unfrozen(cases, {c["case_id"]: annotation(c["case_id"]) for c in cases}, rec,
                                              {m: GoodPlanner for m in exp_mod.MODES}, ctx_for,
                                              injection.build_injection_cases([c["case_id"] for c in cases]), seed=1)
        self.assertTrue(out["not_a_result"])
        self.assertFalse(out["claimed"])

    def test_the_cli_can_only_reach_the_claimed_route(self):
        text = (ROOT / "planner_experiment" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("run_once", text)
        for forbidden in ("run_experiment_unfrozen", "_execute", "_load_frozen_inputs"):
            self.assertNotIn(forbidden, text)


class DrawnCaseProtectionBypassTests(unittest.TestCase):
    """The self-test route must not be able to execute a drawn experiment ticket: not by moving the guard, and not by disguising the case."""

    def setUp(self):
        self.w = build_world()
        point = patch.object(exp_mod, "_DRAWN_BASE", self.w.base)
        point.start()
        self.addCleanup(point.stop)
        self.drawn = self.w.cases[0]

    def run_unfrozen(self, cases, **kw):
        from planner_experiment import injection
        from planner_experiment.scripted_env import Recording
        from tests.pe_fake_planners import GoodPlanner
        from tests.test_planner_experiment import annotation, ctx_for, drafts_for

        rec = Recording()
        for c in cases:
            rec.add(c["case_id"], "incident_kb_search", {"query": c["ticket_text"], "top_k": 5}, drafts_for(c["tenant_id"], ("incident", "I1", "disk full")))
        patcher_calls = []
        original = exp_mod.Runner.run_batch

        def spy(runner, *a, **k):
            patcher_calls.append(1)
            return original(runner, *a, **k)

        with patch.object(exp_mod.Runner, "run_batch", spy):
            try:
                exp_mod.run_experiment_unfrozen(cases, {c["case_id"]: annotation(c["case_id"]) for c in cases}, rec, {m: GoodPlanner for m in exp_mod.MODES},
                                                ctx_for, injection.build_injection_cases([c["case_id"] for c in cases]), seed=1, **kw)
            except ExperimentInputError as exc:
                return "refused", len(patcher_calls), str(exc)
        return "ran", len(patcher_calls), ""

    def minimal(self, text, case_id="x1"):
        return {"case_id": case_id, "tenant_id": self.drawn["tenant_id"], "ticket_text": text}

    # variant 1: moving the guard
    def test_the_guard_location_is_not_a_parameter(self):
        self.assertNotIn("drawn_base", inspect.signature(exp_mod.run_experiment_unfrozen).parameters)
        self.assertNotIn("drawn_base", inspect.signature(exp_mod._execute).parameters)
        with self.assertRaises(TypeError):
            exp_mod.run_experiment_unfrozen([], {}, None, {}, None, [], 1, drawn_base=pathlib.Path(tempfile.mkdtemp()))

    def test_an_empty_or_foreign_folder_cannot_be_substituted_by_the_caller(self):
        empty = pathlib.Path(tempfile.mkdtemp())
        outcome = self.run_unfrozen([dict(self.drawn)])
        self.assertEqual(outcome[:2], ("refused", 0))
        self.assertEqual(sorted(empty.iterdir()), [])                       # nothing a caller creates changes where the guard looks

    def test_the_default_location_is_the_repositorys_recorded_draws(self):
        with patch.object(exp_mod, "_DRAWN_BASE", None):
            self.assertEqual(exp_mod._drawn_base(), steps.BASE)

    # variant 2: disguising the case
    def test_dropping_every_identifying_field_does_not_hide_a_drawn_ticket(self):
        self.assertEqual(self.run_unfrozen([self.minimal(self.drawn["ticket_text"])])[:2], ("refused", 0))

    def test_re_casing_spacing_punctuation_and_small_edits_do_not_hide_it(self):
        text = self.drawn["ticket_text"]
        words = text.split()
        disguises = [text.upper(), "  " + text.replace(" ", "   ") + "  ", text + " .", text + "!!", text.replace(" ", ", "),
                     text + " urgent", "urgent " + text, " ".join(words[:-1])]            # +/- one word on a five-word ticket
        for variant in disguises:
            outcome = self.run_unfrozen([self.minimal(variant)])
            self.assertEqual(outcome[:2], ("refused", 0), variant)

    def test_development_and_other_templated_tickets_are_not_wrongly_refused(self):
        dev = json.loads(steps.paths(self.w.base)["development"].read_text(encoding="utf-8"))["cases"]
        by_key = {(t["pool"], t["ticket_id"]): t for t in self.w.tickets}
        self.assertEqual(len(dev), 10)
        for d in dev:
            ticket = by_key[(d["pool"], d["ticket_id"])]
            outcome = self.run_unfrozen([{"case_id": "dev-" + d["ticket_id"], "tenant_id": ticket["tenant_id"], "ticket_text": ticket["text"]}])
            self.assertEqual(outcome[0], "ran", (d["case_id"], outcome))
        drawn_ids = {c["ticket_id"] for c in self.w.cases}
        drawn_texts = {c["ticket_text"] for c in self.w.cases}              # (the synthetic pools repeat texts: only genuinely different tickets)
        others = [t for t in self.w.tickets if t["pool"] == "client_c" and t["ticket_id"] not in drawn_ids and t["text"] not in drawn_texts][:30]
        self.assertGreater(len(others), 10)
        for t in others:
            self.assertEqual(self.run_unfrozen([{"case_id": "o-" + t["ticket_id"], "tenant_id": t["tenant_id"], "ticket_text": t["text"]}])[0], "ran", t["text"])

    def test_a_few_added_words_on_a_longer_ticket_do_not_hide_it(self):
        long_text = "disk space on the database host sapdb01 dropped below five percent overnight and the nightly backup job failed twice"
        from planner_experiment import select_cases as sc
        path = steps.round_paths(self.w.base, 1)["selection"]
        selection = json.loads(path.read_text(encoding="utf-8"))
        selection["cases"][0]["text_sketch"] = sc.text_sketch(long_text, key=TEST_KEY)           # a longer ticket in the recorded (keyed) draw
        path.write_text(json.dumps(selection), encoding="utf-8")
        for variant in (long_text + " please check", "URGENT: " + long_text, long_text.replace("overnight", "overnight!"),
                        " ".join(long_text.split()[:-2])):
            self.assertEqual(self.run_unfrozen([self.minimal(variant)])[:2], ("refused", 0), variant)
        self.assertEqual(self.run_unfrozen([self.minimal("tape library offline after the firmware update on the backup server")])[0], "ran")

    def test_the_id_pair_and_hash_routes_still_match_on_their_own(self):
        keep = {k: self.drawn[k] for k in ("pool", "ticket_id")}
        self.assertEqual(self.run_unfrozen([{**self.minimal("completely different words about printers", "y1"), **keep}])[:2], ("refused", 0))
        self.assertEqual(self.run_unfrozen([{**self.drawn, "case_id": "renamed"}])[:2], ("refused", 0))

    def test_each_of_the_fifty_drawn_tickets_is_recognised_from_its_text_alone(self):
        for c in self.w.cases:
            self.assertEqual(self.run_unfrozen([self.minimal(c["ticket_text"])])[:2], ("refused", 0), c["case_id"])

    def test_unrelated_tickets_still_run(self):
        outcome = self.run_unfrozen([self.minimal("tape library offline after firmware update on the backup server", "z1")])
        self.assertEqual(outcome[0], "ran")
        self.assertGreater(outcome[1], 0)

    def test_the_recorded_draw_stores_hashes_and_sketches_never_text(self):
        selection = json.loads(steps.round_paths(self.w.base, 1)["selection"].read_text(encoding="utf-8"))
        texts = [c["ticket_text"] for c in self.w.cases]
        raw = json.dumps(selection)
        for text in texts:
            self.assertNotIn(text, raw)
        for entry in selection["cases"]:
            self.assertTrue(entry["text_sketch"] and all(isinstance(h, int) for h in entry["text_sketch"]))
            self.assertLessEqual(len(entry["text_sketch"]), 32)

    def test_sketch_similarity_behaves(self):
        from planner_experiment import select_cases as sc
        a = sc.text_sketch("disk full on the database host at midnight")
        self.assertEqual(sc.sketch_similarity(a, a), 1.0)
        self.assertEqual(sc.sketch_similarity(a, sc.text_sketch("DISK FULL on the database HOST at midnight!")), 1.0)
        self.assertLess(sc.sketch_similarity(sc.text_sketch("disk full on sapdb01"), sc.text_sketch("disk full on sapdb02")), 0.7)   # other host: distinct
        self.assertLess(sc.sketch_similarity(a, sc.text_sketch("tape library offline after firmware update")), 0.2)
        self.assertEqual(sc.sketch_similarity([], a), 0.0)


class LineEndingTests(unittest.TestCase):
    """A freeze made on a CRLF checkout must verify on an LF checkout and the other way round (git converts line endings)."""

    def test_hash_ignores_line_endings_but_not_content(self):
        self.assertEqual(freeze.sha256_bytes(b"a\r\nb\r\n"), freeze.sha256_bytes(b"a\nb\n"))
        self.assertNotEqual(freeze.sha256_bytes(b"a\nb\n"), freeze.sha256_bytes(b"a\nc\n"))
        self.assertNotEqual(freeze.sha256_bytes(b"a\n"), freeze.sha256_bytes(b"a \n"))

    def test_a_frozen_file_rewritten_with_the_other_line_endings_still_verifies(self):
        w = build_world()
        record = w.freeze()
        target = w.files["config"]
        raw = target.read_bytes()
        target.write_bytes(raw.replace(b"\n", b"\r\n") if b"\r\n" not in raw else raw.replace(b"\r\n", b"\n"))
        freeze.read_frozen(record, "config")                                   # no error
        target.write_bytes(raw + b" ")
        with self.assertRaises(FreezeError):
            freeze.read_frozen(record, "config")                               # a real change is still caught


class KeyedDrawTests(unittest.TestCase):
    """The committed draw must reveal nothing about the drawn tickets: everything derived from content is keyed, and a machine without the
    key fails closed. These tests attack the recorded values the way a reader of the repository would."""

    @classmethod
    def setUpClass(cls):
        cls.w = build_world()
        cls.path = steps.round_paths(cls.w.base, 1)["selection"]
        cls.text = cls.path.read_text(encoding="utf-8")
        cls.sel = json.loads(cls.text)

    def vocabulary(self):
        from planner_experiment import select_cases as sc
        vocab = set()
        for c in self.w.cases:
            vocab |= sc._tokens(c["ticket_text"])                          # the attacker's best case: the exact vocabulary of the drawn tickets
        vocab |= {"disk", "login", "service", "cert", "cpu", "queue", "memory", "network", "backup", "batch", "print", "mail", "problem",
                  "reported", "on", "fault", "noted", "against", "golden", "incident", "issue", "error", "failed", "the", "server", "host"}
        return vocab

    def test_the_selection_is_marked_keyed_and_carries_only_a_key_check(self):
        from planner_experiment import secrecy
        self.assertTrue(self.sel["keyed"])
        self.assertEqual(self.sel["key_check"], secrecy.key_check(TEST_KEY))
        self.assertNotIn(TEST_KEY_HEX, self.text)
        self.assertNotIn(self.sel["key_check"], TEST_KEY_HEX)             # the check value is not the key or a slice of it

    def test_an_unkeyed_dictionary_attack_on_the_sketches_finds_nothing(self):
        import hashlib
        stored = {h for e in self.sel["cases"] for h in e["text_sketch"]}
        unkeyed = {int(hashlib.sha256(t.encode("utf-8")).hexdigest()[:12], 16) for t in self.vocabulary()}
        self.assertTrue(stored)
        self.assertEqual(stored & unkeyed, set())                          # the previous (unkeyed) scheme would have matched every one

    def test_nothing_in_the_file_is_a_plain_hash_cluster_type_or_word_of_a_drawn_ticket(self):
        from planner_experiment import select_cases as sc
        for c in self.w.cases:
            ticket = {"pool": c["pool"], "tenant_id": c["tenant_id"], "ticket_id": c["ticket_id"], "text": c["ticket_text"], "host": c["host"],
                      "opened_at": c["opened_at"]}
            for plain in (sc.content_hash(ticket), sc.cluster_id(ticket), sc.ticket_type(ticket)):
                self.assertNotIn(plain, self.text, plain)
        words = {w for w in re.findall(r"[a-z]{4,}", " ".join(c["ticket_text"].lower() for c in self.w.cases))} - {"client", "golden"}
        lowered = self.text.lower()
        for word in words:
            self.assertIsNone(re.search(rf"\b{re.escape(word)}\b", lowered), word)       # no word of any drawn ticket appears in the committed file

    def test_the_type_counts_are_keyed_too(self):
        self.assertTrue(self.sel["type_counts"])
        for name in self.sel["type_counts"]:
            self.assertRegex(name, r"^[0-9a-f]{16}$")
        self.assertEqual(sum(self.sel["type_counts"].values()), 50)

    def test_a_different_key_shares_nothing_with_the_recorded_values(self):
        from planner_experiment import select_cases as sc
        other = bytes.fromhex("cd" * 32)
        stored = {h for e in self.sel["cases"] for h in e["text_sketch"]}
        theirs = {h for c in self.w.cases for h in sc.text_sketch(c["ticket_text"], key=other)}
        self.assertEqual(stored & theirs, set())
        mine = {h for c in self.w.cases for h in sc.text_sketch(c["ticket_text"], key=TEST_KEY)}
        self.assertTrue(stored <= mine)                                     # the right key reproduces them exactly

    def test_sketch_length_is_bounded_and_values_are_integers(self):
        for e in self.sel["cases"]:
            self.assertLessEqual(len(e["text_sketch"]), 32)
            self.assertTrue(all(isinstance(h, int) and 0 <= h < 2 ** 48 for h in e["text_sketch"]))

    def test_the_development_subset_stays_plain_because_developers_see_those_tickets(self):
        dev = json.loads(steps.paths(self.w.base)["development"].read_text(encoding="utf-8"))
        self.assertNotIn("keyed", dev)
        for e in dev["cases"]:
            self.assertNotIn("text_sketch", e)
            self.assertRegex(e["content_hash"], r"^[0-9a-f]{64}$")

    def test_the_key_appears_in_no_recorded_file(self):
        hits = [p.name for p in list(self.w.base.glob("*.json")) + list(self.w.dir.glob("*.json")) if TEST_KEY_HEX in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, [])

    # -- fail closed without the key --------------------------------------------------------------------------------------------------------
    def test_without_the_key_the_freeze_and_the_case_builder_refuse(self):
        w = build_world()
        with patch.dict(os.environ, {"AIOPS_PE_KEY": ""}):
            with self.assertRaisesRegex(FreezeError, "does not hold the experiment key"):
                w.freeze()
            with self.assertRaisesRegex(steps.StepError, "experiment key"):
                steps.build_cases(steps.round_paths(w.base, 1)["selection"], w.tickets)

    def test_a_wrong_key_is_detected(self):
        w = build_world()
        with patch.dict(os.environ, {"AIOPS_PE_KEY": "cd" * 32}):
            with self.assertRaisesRegex(FreezeError, "does not match"):
                w.freeze()
            with self.assertRaisesRegex(steps.StepError, "experiment key"):
                steps.build_cases(steps.round_paths(w.base, 1)["selection"], w.tickets)

    def test_no_experiment_set_can_be_drawn_without_the_key(self):
        base = pathlib.Path(tempfile.mkdtemp())
        w = build_world(directory=base)
        # a second round would need a revised freeze; the key check comes first on a fresh base instead
        fresh = pathlib.Path(tempfile.mkdtemp(prefix="pe-nokey-"))
        steps.record_reviewers(fresh, ["Ann", "Bob"], {"Ann": 40, "Bob": 40}, "owner")
        from planner_experiment import seed as seed_mod
        seed_mod.write_record(steps.paths(fresh)["partition_seed"], "a" * 40, "calibration-selection")
        from tests.pe_world import DEVELOPMENT_ARTIFACT, ENTRYPOINTS
        steps.select_development(fresh, w.tickets, DEVELOPMENT_ARTIFACT)
        steps.freeze_planners(fresh, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        with patch.dict(os.environ, {"AIOPS_PE_KEY": ""}):
            with self.assertRaisesRegex(steps.StepError, "experiment key is required"):
                steps.draw_experiment(fresh, 1, w.tickets)
        self.assertFalse(steps.round_paths(fresh, 1)["selection"].exists())
        self.assertFalse(steps.round_paths(fresh, 1)["seed"].exists())

    def run_guard(self, cases):
        with patch.object(exp_mod, "_DRAWN_BASE", self.w.base):
            try:
                exp_mod._refuse_unclaimed_drawn_cases(cases)
            except ExperimentInputError as exc:
                return str(exc)
        return None

    def test_a_machine_without_the_key_may_run_only_the_development_subset(self):
        dev = json.loads(steps.paths(self.w.base)["development"].read_text(encoding="utf-8"))["cases"]
        by_key = {(t["pool"], t["ticket_id"]): t for t in self.w.tickets}
        dev_cases = []
        for d in dev:
            t = by_key[(d["pool"], d["ticket_id"])]
            dev_cases.append({"case_id": d["case_id"], "pool": d["pool"], "tenant_id": t["tenant_id"], "ticket_id": d["ticket_id"],
                              "ticket_text": t["text"], "host": t["host"], "opened_at": t["opened_at"]})
        with patch.dict(os.environ, {"AIOPS_PE_KEY": ""}):
            self.assertIsNone(self.run_guard(dev_cases))                                       # the development subset runs
            self.assertIn("does not hold the experiment key", self.run_guard([dict(self.w.cases[0])]))       # a drawn ticket does not
            unrelated = {"case_id": "z1", "tenant_id": "t", "ticket_text": "tape library offline after the firmware update"}
            self.assertIn("does not hold the experiment key", self.run_guard([unrelated]))     # nor anything unverifiable: fail closed
            edited = {**dev_cases[0], "ticket_text": dev_cases[0]["ticket_text"] + " extra"}
            self.assertIn("does not hold the experiment key", self.run_guard([edited]))        # nor an edited development ticket

    def test_a_machine_with_the_wrong_key_is_treated_as_keyless(self):
        with patch.dict(os.environ, {"AIOPS_PE_KEY": "cd" * 32}):
            self.assertIn("does not hold the experiment key", self.run_guard([{"case_id": "z1", "tenant_id": "t", "ticket_text": "anything at all"}]))

    def test_with_the_key_the_guard_recognises_every_drawn_ticket_from_its_text_alone(self):
        for c in self.w.cases:
            self.assertIn("drawn into a planner-experiment set", self.run_guard([{"case_id": "p1", "tenant_id": c["tenant_id"], "ticket_text": c["ticket_text"]}]) or "", c["case_id"])
        self.assertIsNone(self.run_guard([{"case_id": "p2", "tenant_id": "t", "ticket_text": "tape library offline after the firmware update on the backup server"}]))


class KeyCustodyTests(unittest.TestCase):
    def test_the_key_is_created_once_never_replaced_and_never_returned(self):
        from planner_experiment import secrecy
        path = pathlib.Path(tempfile.mkdtemp()) / "k" / "planner_experiment.key"
        check = secrecy.create_key(path)
        raw = path.read_text(encoding="utf-8")
        self.assertRegex(raw, r"^[0-9a-f]{64}$")
        self.assertNotIn(raw, check)
        self.assertEqual(len(check), 16)
        with self.assertRaises(secrecy.KeyError_):
            secrecy.create_key(path)
        self.assertEqual(path.read_text(encoding="utf-8"), raw)                    # the first key survived
        with patch.dict(os.environ, {"AIOPS_PE_KEY": ""}):                           # (the tests set a key in the environment, which takes priority)
            self.assertEqual(secrecy.key_check(secrecy.load_key(path)), check)

    def test_the_environment_takes_priority_and_bad_keys_are_refused(self):
        from planner_experiment import secrecy
        path = pathlib.Path(tempfile.mkdtemp()) / "k.key"
        secrecy.create_key(path)
        with patch.dict(os.environ, {"AIOPS_PE_KEY": "ef" * 32}):
            self.assertEqual(secrecy.load_key(path), bytes.fromhex("ef" * 32))
        with patch.dict(os.environ, {"AIOPS_PE_KEY": ""}):
            self.assertEqual(secrecy.load_key(path), bytes.fromhex(path.read_text(encoding="utf-8")))
            self.assertIsNone(secrecy.load_key(path.with_name("missing.key")))
        for bad in ("zz" * 32, "ab" * 31, "ab" * 33, "not hex"):
            with patch.dict(os.environ, {"AIOPS_PE_KEY": bad}), self.assertRaises(secrecy.KeyError_):
                secrecy.load_key(path)

    def test_the_key_file_is_git_ignored_and_labels_separate_the_uses(self):
        from planner_experiment import secrecy
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/planner_experiment.key", ignore)
        self.assertEqual(secrecy.KEY_FILE, ROOT / "data" / "planner_experiment.key")
        key = bytes.fromhex("ab" * 32)
        self.assertNotEqual(secrecy.keyed(key, "content", "x"), secrecy.keyed(key, "token", "x"))        # domain separation
        self.assertNotEqual(secrecy.keyed(key, "content", "x"), secrecy.keyed(bytes.fromhex("cd" * 32), "content", "x"))

    def test_the_repository_holds_no_key_file(self):
        self.assertFalse((ROOT / "data" / "planner_experiment.key").exists())


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
