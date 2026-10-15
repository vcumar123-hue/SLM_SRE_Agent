"""
The golden-approval and create-freeze commands (library functions and command line), and the draft golden tickets in docs/drafts.

Approval of golden tickets is a human act: these tests check the tooling cannot approve for anyone else, and that the repository holds no
fabricated approval. The create-freeze tests build a real world through the workflow steps and check the assembled freeze is the one the
experiment accepts and runs.
"""
import json
import pathlib
import re
import tempfile
import unittest
from unittest.mock import patch

from planner_experiment import cli, freeze, golden, injection, planner_freeze, steps
from planner_experiment import select_cases as sc
from planner_experiment.experiment import run_once
from planner_experiment.freeze import ROOT
from tests.pe_world import TESTS_DIR, build_world

DRAFT = ROOT / "docs" / "drafts" / "golden_tickets_DRAFT.json"


def planners_here():
    return patch.object(planner_freeze, "PLANNERS_DIR", TESTS_DIR)


class DraftGoldenTicketTests(unittest.TestCase):
    def setUp(self):
        self.items = json.loads(DRAFT.read_text(encoding="utf-8"))

    def test_the_draft_has_enough_varied_synthetic_tickets(self):
        self.assertGreaterEqual(len(self.items), 15)
        self.assertLessEqual(len(self.items), 20)
        ids = [i["ticket_id"] for i in self.items]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(re.fullmatch(r"golden-[0-9]+", i) for i in ids))              # never golden-acc-*
        self.assertEqual(len({i["host"].lower() for i in self.items}), len(self.items))     # distinct hosts
        types = [sc.ticket_type({"text": i["text"]}) for i in self.items]
        self.assertEqual(len(set(types)), len(types))                                       # varied opening words: no type repeats
        for i in self.items:
            self.assertEqual(i["set"], "calibration")
            for field in ("text", "host", "opened_at"):
                self.assertTrue(str(i[field]).strip(), (i["ticket_id"], field))

    def test_nothing_is_approved_yet_and_no_approved_file_exists(self):
        self.assertTrue(all(i["approved_by"] == [] for i in self.items))                    # names are added only after genuine review
        self.assertFalse((ROOT / "data" / "golden_tickets.json").exists())

    def test_the_draft_cannot_be_loaded_as_approved(self):
        with self.assertRaises(golden.GoldenError):
            golden.parse_golden(self.items, ["Vijay", "Sara"])

    def test_once_approved_by_exactly_the_two_reviewers_it_would_load_and_serve_both_draws(self):
        approved = [{**i, "approved_by": ["Vijay", "Sara"]} for i in self.items]
        loaded = golden.parse_golden(approved, ["Vijay", "Sara"])
        self.assertEqual({g["pool"] for g in loaded}, {"golden"})
        self.assertTrue(all(g["tenant_id"] == "poc_client" for g in loaded))
        # enough eligible golden tickets for the development subset (2) and the experiment set (10), with room for rejections
        first = sc.select_calibration(loaded, 1, 2, {"golden": 2})
        used = frozenset(s["cluster_id"] for s in first["selected"])
        second = sc.select_calibration(loaded, 1, 3, {"golden": 10}, exclude_clusters=used)
        self.assertEqual((len(first["selected"]), len(second["selected"])), (2, 10))
        self.assertFalse(first["shortfall"] or second["shortfall"])


class ApproveGoldenTests(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp())
        steps.record_reviewers(self.base, ["Vijay", "Sara"], {"Vijay": 50, "Sara": 50}, "owner")
        self.out = self.base / "golden_tickets.json"

    def approve(self, ids, approvers=("Vijay", "Sara"), out=None):
        return steps.approve_golden(self.base, DRAFT, out or self.out, list(ids), list(approvers))

    def test_only_the_listed_tickets_are_written_with_exactly_the_recorded_reviewers(self):
        items = self.approve(["golden-1", "golden-5", "golden-9"])
        written = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual([i["ticket_id"] for i in written], ["golden-1", "golden-5", "golden-9"])
        self.assertEqual(items, written)
        for i in written:
            self.assertEqual(i["approved_by"], ["Vijay", "Sara"])
            self.assertNotIn("category", i)                                                   # draft-only descriptive fields are dropped
        golden.load_golden(self.out, ["Vijay", "Sara"])

    def test_approval_cannot_be_given_in_anyone_elses_name(self):
        for approvers in (("Vijay", "Eve"), ("Vijay", "Vijay"), ("Vijay",), ("Vijay", "Sara", "Eve"), ("Sara", "Bob")):
            with self.assertRaises(steps.StepError, msg=approvers):
                self.approve(["golden-1"], approvers)
        self.assertFalse(self.out.exists())

    def test_the_names_are_taken_in_the_recorded_spelling(self):
        self.approve(["golden-1"], ("sara", "VIJAY"))
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))[0]["approved_by"], ["Vijay", "Sara"])

    def test_ids_must_be_explicit_known_and_unique(self):
        for ids in ([], ["golden-1", "golden-1"], ["golden-999"], ["golden-acc-1"]):
            with self.assertRaises(steps.StepError, msg=ids):
                self.approve(ids)
        self.assertFalse(self.out.exists())

    def test_written_once(self):
        self.approve(["golden-1"])
        with self.assertRaisesRegex(steps.StepError, "already exists"):
            self.approve(["golden-2"])
        self.assertEqual([i["ticket_id"] for i in json.loads(self.out.read_text(encoding="utf-8"))], ["golden-1"])

    def test_it_needs_the_reviewer_record_first(self):
        fresh = pathlib.Path(tempfile.mkdtemp())
        with self.assertRaisesRegex(steps.StepError, "reviewer"):
            steps.approve_golden(fresh, DRAFT, fresh / "g.json", ["golden-1"], ["Vijay", "Sara"])

    def test_a_malformed_draft_entry_is_refused_not_written(self):
        draft = self.base / "bad.json"
        items = json.loads(DRAFT.read_text(encoding="utf-8"))
        items[0]["host"] = ""
        draft.write_text(json.dumps(items), encoding="utf-8")
        with self.assertRaises(steps.StepError):
            steps.approve_golden(self.base, draft, self.out, ["golden-1"], ["Vijay", "Sara"])
        self.assertFalse(self.out.exists())

    def test_the_command_line_works_and_refuses(self):
        with patch.object(steps, "BASE", self.base), patch.object(cli, "GOLDEN", self.out):
            self.assertEqual(cli.main(["approve-golden", "--draft", str(DRAFT), "--ids", "golden-2", "golden-3", "--approved-by", "Vijay", "Sara"]), 0)
            self.assertEqual(len(json.loads(self.out.read_text(encoding="utf-8"))), 2)
            self.assertEqual(cli.main(["approve-golden", "--draft", str(DRAFT), "--ids", "golden-4", "--approved-by", "Vijay", "Sara"]), 2)   # exists
        other = self.base / "other.json"
        with patch.object(steps, "BASE", self.base), patch.object(cli, "GOLDEN", other):
            self.assertEqual(cli.main(["approve-golden", "--draft", str(DRAFT), "--ids", "golden-4", "--approved-by", "Vijay", "Eve"]), 2)
        self.assertFalse(other.exists())


def inputs_for(w):
    return dict(cases_path=w.files["cases"], recording_path=w.files["recording"], annotations_path=w.files["annotations"],
                reviewer_files=dict(w.reviewer_files), golden_path=w.extra["golden_path"])


class CreateFreezeTests(unittest.TestCase):
    def test_the_assembled_freeze_verifies_and_is_the_one_the_experiment_runs(self):
        w = build_world()
        out = w.dir / "assembled" / "freeze_round1.json"
        out.parent.mkdir()
        record = steps.create_freeze_for_round(w.base, 1, out_path=out, **inputs_for(w))
        freeze.verify_freeze(out)
        self.assertEqual(sorted(record["reviewers"]), ["Ann", "Bob"])
        self.assertEqual(record["agreement"]["cases_compared"], 50)
        with planners_here():
            result = run_once(out, w.dir / "results")
        self.assertFalse(result["not_a_result"])
        self.assertTrue(result["claimed"])

    def test_config_and_injection_cases_are_generated_from_the_workflow_not_typed(self):
        w = build_world()
        out = w.dir / "f.json"
        steps.create_freeze_for_round(w.base, 1, out_path=out, **inputs_for(w))
        inputs_dir = w.base / "inputs_round1"
        config = json.loads((inputs_dir / "config.json").read_text(encoding="utf-8"))
        pf = planner_freeze.load_planner_freeze(steps.round_paths(w.base, 1)["planner_freeze"])
        draw = json.loads(steps.round_paths(w.base, 1)["seed"].read_text(encoding="utf-8"))
        self.assertEqual(config, {"round": 1, "seed": draw["seed"], "modes": list(freeze.MODES), "planners": pf["entrypoints"], "budgets": {}})
        generated = json.loads((inputs_dir / "injection_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(generated), 12)
        self.assertEqual({g["base_case_id"] for g in generated}, {c["case_id"] for c in w.cases[:4]})
        rebuilt = injection.build_injection_cases([c["case_id"] for c in w.cases[:4]])
        self.assertEqual([(g["case_id"], g["canary"], g["payload"]) for g in generated], [(r.case_id, r.canary, r.payload) for r in rebuilt])

    def test_the_default_output_is_beside_the_workflow_records_and_written_once(self):
        w = build_world()
        record = steps.create_freeze_for_round(w.base, 1, **inputs_for(w))
        default = w.base / "freeze_round1.json"
        self.assertTrue(default.is_file())
        self.assertEqual(json.loads(default.read_text(encoding="utf-8"))["record_hash"], record["record_hash"])
        before = default.read_text(encoding="utf-8")
        with self.assertRaisesRegex(steps.StepError, "already exists"):
            steps.create_freeze_for_round(w.base, 1, **inputs_for(w))
        self.assertEqual(default.read_text(encoding="utf-8"), before)

    def test_exactly_the_two_recorded_reviewers_must_each_supply_a_file(self):
        w = build_world()
        base_inputs = inputs_for(w)
        cases = (({"Ann": w.reviewer_files["Ann"]}, "exactly the two"),
                 ({**w.reviewer_files, "Eve": w.reviewer_files["Ann"]}, "exactly the two"),
                 ({"Ann": w.reviewer_files["Ann"], "Eve": w.reviewer_files["Bob"]}, "exactly the two"))
        for reviewer_files, pattern in cases:
            with self.assertRaisesRegex(steps.StepError, pattern):
                steps.create_freeze_for_round(w.base, 1, **{**base_inputs, "reviewer_files": reviewer_files}, out_path=w.dir / "x.json")
        self.assertFalse((w.dir / "x.json").exists())

    def test_missing_inputs_are_named_and_nothing_is_written(self):
        w = build_world()
        for key in ("cases_path", "recording_path", "annotations_path", "golden_path"):
            inputs = {**inputs_for(w), key: w.dir / "nope.json"}
            with self.assertRaisesRegex(steps.StepError, "missing"):
                steps.create_freeze_for_round(w.base, 1, **inputs, out_path=w.dir / "y.json")
        with self.assertRaisesRegex(steps.StepError, "missing"):
            steps.create_freeze_for_round(w.base, 2, **inputs_for(w), out_path=w.dir / "y.json")          # round 2 was never drawn
        self.assertFalse((w.dir / "y.json").exists())

    def test_every_consistency_failure_surfaces_as_a_refusal_and_writes_no_freeze(self):
        w = build_world()
        cases = json.loads(w.files["cases"].read_text(encoding="utf-8"))
        cases[5]["ticket_text"] += " (edited)"                                                      # composition
        edited = w.dir / "cases_edited.json"
        edited.write_text(json.dumps(cases), encoding="utf-8")
        with self.assertRaisesRegex(steps.StepError, "composition"):
            steps.create_freeze_for_round(w.base, 1, **{**inputs_for(w), "cases_path": edited}, out_path=w.dir / "z.json")
        short = w.dir / "ann_short.json"                                                              # coverage
        short.write_text(json.dumps(json.loads(w.files["annotations"].read_text(encoding="utf-8"))[:-1]), encoding="utf-8")
        with self.assertRaisesRegex(steps.StepError, "freeze was not created"):
            steps.create_freeze_for_round(w.base, 1, **{**inputs_for(w), "annotations_path": short}, out_path=w.dir / "z.json")
        self.assertFalse((w.dir / "z.json").exists())

    def test_a_golden_file_not_approved_by_the_recorded_reviewers_is_refused(self):
        w = build_world()
        bad = w.dir / "golden_other.json"
        items = json.loads(w.extra["golden_path"].read_text(encoding="utf-8"))
        for i in items:
            i["approved_by"] = ["Ann", "Eve"]
        bad.write_text(json.dumps(items), encoding="utf-8")
        with self.assertRaisesRegex(steps.StepError, "both named reviewers"):
            steps.create_freeze_for_round(w.base, 1, **{**inputs_for(w), "golden_path": bad}, out_path=w.dir / "g.json")

    def test_a_later_round_includes_the_earlier_rounds_selection_automatically(self):
        w = build_world(rounds=2)
        record = steps.create_freeze_for_round(w.base, 2, out_path=w.dir / "f2.json", root=w.code_root, **inputs_for(w))
        self.assertIn("prior_selection:1", record["files"])
        freeze.verify_freeze(w.dir / "f2.json", root=w.code_root)

    def test_the_command_line_creates_the_freeze_and_reports_refusals(self):
        w = build_world()
        out = w.dir / "cli_freeze.json"
        argv = ["create-freeze", "--round", "1", "--cases", str(w.files["cases"]), "--recording", str(w.files["recording"]),
                "--annotations", str(w.files["annotations"]), "--out", str(out)]
        for name, path in w.reviewer_files.items():
            argv += ["--reviewer-file", f"{name}={path}"]
        with patch.object(steps, "BASE", w.base), patch.object(cli, "GOLDEN", w.extra["golden_path"]):
            self.assertEqual(cli.main(argv), 0)
            freeze.verify_freeze(out)
            self.assertEqual(cli.main(argv), 2)                                                      # already exists
            self.assertEqual(cli.main(argv[:-4] + ["--out", str(w.dir / "o2.json"), "--reviewer-file", f"Ann={w.reviewer_files['Ann']}"]), 2)   # one reviewer only


if __name__ == "__main__":
    unittest.main()
