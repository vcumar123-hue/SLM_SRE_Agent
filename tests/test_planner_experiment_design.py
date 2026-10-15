"""
Planner-experiment design tests (review of 2026-10-15, second round): planners come from the freeze record, golden approval is tied to the
recorded reviewers, the frozen composition is validated, and the held-out design (development subset -> planner freeze -> derived draw ->
run once) is enforced by the workflow. A synthetic world is built through the real steps (tests/pe_world.py); each test breaks one thing.
"""
import copy
import inspect
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from planner_experiment import composition, freeze, golden, metrics, planner_freeze, reviewers as reviewer_mod, seed as seed_mod, steps
from planner_experiment import select_cases as sc
from planner_experiment.experiment import ExperimentInputError, run_experiment_frozen, run_once
from planner_experiment.freeze import FreezeError, ROOT
from tests.pe_world import (DEVELOPMENT_ARTIFACT, ENTRYPOINTS, NAMES, PARTITION_ARTIFACT, TESTS_DIR, build_world, golden_raw, make_tickets)


def planners_here():
    return patch.object(planner_freeze, "PLANNERS_DIR", TESTS_DIR)


def fake_root():
    """A small stand-in code tree, so a test can revise 'planner code' without touching the real repository."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="pe-root-"))
    for top in freeze.CODE_ROOTS:
        (root / top).mkdir()
        (root / top / "m.py").write_text("x = 1\n", encoding="utf-8")
    (root / "agentruntime" / "planners").mkdir()
    (root / "agentruntime" / "planners" / "p.py").write_text("RULES = 1\n", encoding="utf-8")
    return root


# -- reviewers -------------------------------------------------------------------------------------------------------------------------

class ReviewerRecordTests(unittest.TestCase):
    def good(self, **kw):
        rec = {"reviewers": ["Ann", "Bob"], "capacity_hours": {"Ann": 50, "Bob": 45.5}, "recorded_by": "owner"}
        rec.update(kw)
        return rec

    def test_a_complete_record_validates(self):
        self.assertEqual(reviewer_mod.validate(self.good()), ["Ann", "Bob"])

    def test_names_capacity_and_recorder_are_all_required(self):
        for bad in (self.good(reviewers=["Ann"]), self.good(reviewers=["Ann", "ann"]), self.good(reviewers=["Ann", "TBD"]),
                    self.good(reviewers=["Ann", "Bob", "Cy"]), self.good(capacity_hours={"Ann": 40}), self.good(capacity_hours={}),
                    self.good(capacity_hours={"Ann": 40, "Bob": 0}), self.good(capacity_hours={"Ann": 40, "Bob": -1}),
                    self.good(capacity_hours={"Ann": 40, "Bob": True}), self.good(capacity_hours={"Ann": 40, "Cy": 10}),
                    self.good(recorded_by=""), {k: v for k, v in self.good().items() if k != "capacity_hours"}):
            with self.assertRaises(reviewer_mod.ReviewerRecordError, msg=bad):
                reviewer_mod.validate(bad)

    def test_written_once_and_required_before_use(self):
        base = pathlib.Path(tempfile.mkdtemp())
        with self.assertRaises(reviewer_mod.ReviewerRecordError):
            reviewer_mod.load_record(base / "reviewers.json")
        steps.record_reviewers(base, ["Ann", "Bob"], {"Ann": 40, "Bob": 40}, "owner")
        with self.assertRaises(steps.StepError):
            steps.record_reviewers(base, ["Ann", "Bob"], {"Ann": 1, "Bob": 1}, "owner")
        self.assertEqual(steps.reviewer_names(base), ["Ann", "Bob"])

    def test_no_draw_is_possible_before_the_reviewers_are_recorded(self):
        base = pathlib.Path(tempfile.mkdtemp())
        seed_mod.write_record(steps.paths(base)["partition_seed"], PARTITION_ARTIFACT, "calibration-selection")
        with self.assertRaisesRegex(steps.StepError, "reviewer"):
            steps.select_development(base, make_tickets(), DEVELOPMENT_ARTIFACT)
        with self.assertRaisesRegex(steps.StepError, "reviewer"):
            steps.draw_experiment(base, 1, make_tickets())


# -- golden tickets tied to the recorded reviewers ---------------------------------------------------------------------------------------

class GoldenApprovalTests(unittest.TestCase):
    def entry(self, **kw):
        base = {"ticket_id": "golden-1", "text": "disk full on sapdb01", "host": "sapdb01", "opened_at": "2026-05-01 10:00:00",
                "set": "calibration", "approved_by": ["Ann", "Bob"]}
        base.update(kw)
        return base

    def parse(self, *entries, reviewers=("Ann", "Bob")):
        return golden.parse_golden(list(entries), list(reviewers))

    def test_approval_by_exactly_the_recorded_reviewers_loads_as_the_golden_pool(self):
        out = self.parse(self.entry(), self.entry(ticket_id="golden-2", approved_by=["bob", "ANN"]))
        self.assertEqual({o["pool"] for o in out}, {"golden"})
        self.assertEqual(out[0]["tenant_id"], "poc_client")
        self.assertEqual(self.parse(self.entry(tenant_id="client_c"))[0]["tenant_id"], "client_c")

    def test_approval_by_anyone_else_is_refused(self):
        for bad in (self.entry(approved_by=["Ann"]), self.entry(approved_by=["Bob"]), self.entry(approved_by=["Ann", "Eve"]),
                    self.entry(approved_by=["Eve", "Mallory"]), self.entry(approved_by=["Ann", "Bob", "Eve"]),
                    self.entry(approved_by=["Ann", "ann"]), self.entry(approved_by=["Ann", "TBD"]), self.entry(approved_by=[])):
            with self.assertRaises(golden.GoldenError, msg=bad):
                self.parse(bad)

    def test_calibration_only_and_well_formed(self):
        for bad in (self.entry(set="acceptance"), self.entry(set=None), self.entry(ticket_id="T-1"), self.entry(host=""), self.entry(text=" ")):
            with self.assertRaises(golden.GoldenError, msg=bad):
                self.parse(bad)
        with self.assertRaises(golden.GoldenError):
            self.parse(self.entry(), self.entry())

    def test_it_cannot_be_loaded_without_the_reviewers(self):
        for reviewers in ([], None, ["Ann"], ["Ann", "ann"]):
            with self.assertRaises(golden.GoldenError):
                golden.parse_golden([self.entry()], reviewers)

    def test_golden_tickets_skip_the_bucket_partition(self):
        kinds = ["disk", "login", "service", "cert", "cpu", "queue", "memory", "network", "backup", "batch", "print", "mail"]
        items = [self.entry(ticket_id=f"golden-{i}", host=f"gold{i}", text=f"{kinds[i % 12]} issue on gold{i}") for i in range(30)]
        out = sc.select_calibration(self.parse(*items), 1, 2, {"golden": 12})
        self.assertEqual(len(out["selected"]), 12)                       # none lost to acceptance buckets

    def test_the_freeze_refuses_a_golden_file_not_approved_by_the_recorded_reviewers(self):
        w = build_world()
        w.write("golden", golden_raw(("Ann", "Eve")))
        with self.assertRaisesRegex(FreezeError, "both named reviewers"):
            w.freeze()

    def test_the_golden_file_cannot_load_against_other_reviewers_when_drawing(self):
        d = pathlib.Path(tempfile.mkdtemp())
        path = d / "g.json"
        path.write_text(json.dumps(golden_raw(("Ann", "Eve"))), encoding="utf-8")
        with self.assertRaises(golden.GoldenError):
            golden.load_golden(path, ["Ann", "Bob"])

    def test_the_freeze_needs_annotation_files_from_exactly_the_recorded_reviewers(self):
        w = build_world()
        rec = w.read("reviewers")
        rec["reviewers"] = ["Ann", "Cy"]
        rec["capacity_hours"] = {"Ann": 40, "Cy": 40}
        w.write("reviewers", rec)
        with self.assertRaisesRegex(FreezeError, "(exactly those|both named reviewers)"):
            w.freeze()


# -- held-out design: development -> planner freeze -> derived draw -> fresh sets ------------------------------------------------------------

class HeldOutDesignTests(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="pe-base-"))
        steps.record_reviewers(self.base, ["Ann", "Bob"], {"Ann": 40, "Bob": 40}, "owner")
        seed_mod.write_record(steps.paths(self.base)["partition_seed"], PARTITION_ARTIFACT, "calibration-selection")
        self.golden_path = self.base / "g.json"
        self.golden_path.write_text(json.dumps(golden_raw()), encoding="utf-8")
        self.tickets = make_tickets() + golden.load_golden(self.golden_path, ["Ann", "Bob"])

    def develop(self):
        return steps.select_development(self.base, self.tickets, DEVELOPMENT_ARTIFACT)

    def test_the_planners_cannot_be_frozen_before_the_development_subset_exists(self):
        with self.assertRaisesRegex(steps.StepError, "development subset"):
            steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)

    def test_the_experiment_set_cannot_be_drawn_before_the_planners_are_frozen(self):
        self.develop()
        with self.assertRaisesRegex(steps.StepError, "planner freeze"):
            steps.draw_experiment(self.base, 1, self.tickets)

    def test_the_development_subset_is_4_4_2_and_drawn_once(self):
        rec = self.develop()
        self.assertEqual({p: sum(1 for c in rec["cases"] if c["pool"] == p) for p in ("client_c", "client_a", "golden")},
                         {"client_c": 4, "client_a": 4, "golden": 2})
        with self.assertRaisesRegex(steps.StepError, "once"):
            self.develop()
        self.assertEqual(rec["draw_seed"], seed_mod.derive_seed(DEVELOPMENT_ARTIFACT, "development-selection"))

    def test_a_short_pool_records_nothing(self):
        with self.assertRaisesRegex(steps.StepError, "nothing was recorded"):
            steps.select_development(self.base, make_tickets(3), DEVELOPMENT_ARTIFACT)
        self.assertFalse(steps.paths(self.base)["development"].exists())
        self.assertFalse(steps.paths(self.base)["development_seed"].exists())

    def test_the_draw_seed_is_derived_from_the_planner_freeze_hash(self):
        self.develop()
        pf = steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        sel = steps.draw_experiment(self.base, 1, self.tickets)
        record = json.loads(steps.round_paths(self.base, 1)["seed"].read_text(encoding="utf-8"))
        self.assertEqual(record["artifact"], pf["record_hash"])
        self.assertEqual(sel["draw_seed"], seed_mod.derive_seed(pf["record_hash"], "planner-experiment-1"))
        self.assertEqual(sel["planner_freeze_hash"], pf["record_hash"])
        self.assertEqual(len(sel["cases"]), 50)

    def test_the_experiment_set_shares_nothing_with_the_development_subset(self):
        dev = self.develop()
        steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        sel = steps.draw_experiment(self.base, 1, self.tickets)
        self.assertFalse({c["cluster_id"] for c in dev["cases"]} & {c["cluster_id"] for c in sel["cases"]})
        self.assertFalse({c["case_id"] for c in dev["cases"]} & {c["case_id"] for c in sel["cases"]})

    def test_a_set_is_drawn_once_and_cannot_be_redrawn_after_it_has_been_seen(self):
        self.develop()
        steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        steps.draw_experiment(self.base, 1, self.tickets)
        with self.assertRaisesRegex(steps.StepError, "already drawn"):
            steps.draw_experiment(self.base, 1, self.tickets)
        with self.assertRaisesRegex(steps.StepError, "already drawn"):
            steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)

    def test_planner_code_changed_after_the_freeze_blocks_the_draw(self):
        root = fake_root()
        self.develop()
        steps.freeze_planners(self.base, 1, ENTRYPOINTS, root=root, planners_dir=TESTS_DIR)
        (root / "agentruntime" / "planners" / "p.py").write_text("RULES = 2\n", encoding="utf-8")       # tuned after the freeze
        with self.assertRaisesRegex(steps.StepError, "changed after the planner freeze"):
            steps.draw_experiment(self.base, 1, self.tickets, root=root)
        self.assertFalse(steps.round_paths(self.base, 1)["selection"].exists())

    def test_the_same_planners_cannot_be_re_run_on_a_new_draw(self):
        self.develop()
        steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        steps.draw_experiment(self.base, 1, self.tickets)
        with self.assertRaisesRegex(steps.StepError, "identical"):
            steps.freeze_planners(self.base, 2, ENTRYPOINTS, planners_dir=TESTS_DIR)

    def test_revised_planners_get_a_fresh_set_that_excludes_all_earlier_sets(self):
        root = fake_root()
        dev = self.develop()
        steps.freeze_planners(self.base, 1, ENTRYPOINTS, root=root, planners_dir=TESTS_DIR)
        first = steps.draw_experiment(self.base, 1, self.tickets, root=root)
        (root / "agentruntime" / "planners" / "p.py").write_text("RULES = 2\n", encoding="utf-8")         # revised on the development subset
        pf2 = steps.freeze_planners(self.base, 2, ENTRYPOINTS, root=root, planners_dir=TESTS_DIR)
        second = steps.draw_experiment(self.base, 2, self.tickets, root=root)
        used = {c["cluster_id"] for c in dev["cases"]} | {c["cluster_id"] for c in first["cases"]}
        self.assertFalse(used & {c["cluster_id"] for c in second["cases"]})
        self.assertNotEqual(first["draw_seed"], second["draw_seed"])
        self.assertEqual(second["draw_seed"], seed_mod.derive_seed(pf2["record_hash"], "planner-experiment-2"))

    def test_rounds_are_consecutive(self):
        self.develop()
        with self.assertRaisesRegex(steps.StepError, "consecutive"):
            steps.freeze_planners(self.base, 2, ENTRYPOINTS, planners_dir=TESTS_DIR)
        with self.assertRaises(steps.StepError):
            steps.freeze_planners(self.base, 0, ENTRYPOINTS, planners_dir=TESTS_DIR)

    def test_build_cases_refuses_a_source_that_changed_since_the_draw(self):
        self.develop()
        steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        steps.draw_experiment(self.base, 1, self.tickets)
        path = steps.round_paths(self.base, 1)["selection"]
        self.assertEqual(len(steps.build_cases(path, self.tickets)), 50)
        changed = copy.deepcopy(self.tickets)
        wanted = json.loads(path.read_text(encoding="utf-8"))["cases"][0]
        for t in changed:
            if t["ticket_id"] == wanted["ticket_id"]:
                t["text"] += " edited"
        with self.assertRaisesRegex(steps.StepError, "changed since it was drawn"):
            steps.build_cases(path, changed)
        with self.assertRaisesRegex(steps.StepError, "no longer in the source"):
            steps.build_cases(path, [t for t in self.tickets if t["ticket_id"] != wanted["ticket_id"]])


class PlannerFreezeRecordTests(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.dev = self.base / "dev.json"
        self.dev.write_text("{}", encoding="utf-8")

    def create(self, entry, name="pf.json"):
        return planner_freeze.create_planner_freeze(entry, self.dev, self.base / name, self.base, planners_dir=TESTS_DIR)

    def test_entry_points_must_be_classes_defined_in_the_planners_folder(self):
        for spec in ("json:JSONDecoder", "tests.pe_fake_planners:Action", "tests.pe_fake_planners:Nope", "tests.pe_fake_planners", "os:path"):
            entry = {**ENTRYPOINTS, "model": spec}
            with self.assertRaises(planner_freeze.PlannerFreezeError, msg=spec):
                self.create(entry)

    def test_every_mode_needs_an_entry_point(self):
        with self.assertRaises(planner_freeze.PlannerFreezeError):
            self.create({"rules_only": ENTRYPOINTS["rules_only"]})
        with self.assertRaises(planner_freeze.PlannerFreezeError):
            self.create({**ENTRYPOINTS, "extra": ENTRYPOINTS["model"]})

    def test_the_record_is_tamper_evident_and_written_once(self):
        record = self.create(ENTRYPOINTS)
        planner_freeze.check_record(record)
        record["entrypoints"]["model"] = "tests.pe_fake_planners:GiveUpPlanner"
        with self.assertRaises(planner_freeze.PlannerFreezeError):
            planner_freeze.check_record(record)
        with self.assertRaises(planner_freeze.PlannerFreezeError):
            self.create(ENTRYPOINTS)

    def test_the_record_covers_the_whole_runtime_and_the_development_selection(self):
        record = self.create(ENTRYPOINTS)
        self.assertIn("code/agentruntime/planners/__init__.py", record["runtime_code"])
        self.assertIn("code/agentruntime/runner.py", record["runtime_code"])
        self.assertTrue(all(k.startswith("code/agentruntime/") for k in record["runtime_code"]))
        self.assertEqual(record["development_selection_hash"], freeze.sha256_bytes(self.dev.read_bytes()))


# -- frozen composition ------------------------------------------------------------------------------------------------------------------

class CompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = build_world()

    def parts(self):
        w = self.w
        return (copy.deepcopy(w.cases), copy.deepcopy(w.read("experiment_selection")), w.read("partition_seed_record")["seed"],
                golden.parse_golden(w.read("golden"), list(NAMES)), copy.deepcopy(w.read("development_selection")))

    def problems(self, cases, selection, partition, golden_items, dev):
        return composition.problems(cases, selection, partition, golden_items, dev)

    def entries_for(self, cases):
        out = []
        for c in cases:
            t = composition._as_ticket(c)
            out.append({"case_id": c["case_id"], "pool": c["pool"], "ticket_id": c["ticket_id"], "cluster_id": sc.cluster_id(t),
                        "ticket_type": sc.ticket_type(t), "content_hash": sc.content_hash(t)})
        return out

    def test_a_correct_draw_has_no_problems(self):
        self.assertEqual(self.problems(*self.parts()), [])

    def test_the_drawn_set_has_exactly_20_20_10(self):
        cases, *_ = self.parts()
        self.assertEqual({p: sum(1 for c in cases if c["pool"] == p) for p in ("client_c", "client_a", "golden")},
                         {"client_c": 20, "client_a": 20, "golden": 10})

    def test_wrong_pool_counts_are_reported(self):
        cases, sel, part, gold, dev = self.parts()
        cases[0]["pool"] = "client_a" if cases[0]["pool"] == "client_c" else "client_c"
        self.assertTrue(any("must be exactly" in p for p in self.problems(cases, sel, part, gold, dev)))
        cases, sel, part, gold, dev = self.parts()
        self.assertTrue(any("must be exactly" in p or "does not match" in p for p in self.problems(cases[:-1], sel, part, gold, dev)))
        cases, sel, part, gold, dev = self.parts()
        self.assertTrue(any("duplicate" in p for p in self.problems(cases + [copy.deepcopy(cases[0])], sel, part, gold, dev)))

    def test_one_case_per_cluster(self):
        cases, sel, part, gold, dev = self.parts()
        same = next(c for c in cases[1:] if c["pool"] == cases[0]["pool"])
        for key in ("host", "ticket_text"):
            same[key] = cases[0][key]
        sel["cases"] = self.entries_for(cases)                                  # keep hashes consistent so only the duplication is wrong
        self.assertTrue(any("one case per cluster" in p for p in self.problems(cases, sel, part, gold, dev)))

    def test_a_cluster_that_differs_from_the_draw_is_reported(self):
        cases, sel, part, gold, dev = self.parts()
        victim = next(c for c in cases if c["pool"] == "client_c")
        victim["host"] = "somewhereelse9"
        found = self.problems(cases, sel, part, gold, dev)
        self.assertTrue(any("cluster differs from the draw" in p for p in found))
        self.assertTrue(any("content does not match" in p for p in found))

    def test_every_cluster_must_be_in_a_calibration_bucket(self):
        cases, sel, part, gold, dev = self.parts()
        found = self.problems(cases, sel, part + 1, gold, dev)                  # a different partition seed
        self.assertTrue(any("not in a calibration bucket" in p for p in found))
        self.assertFalse(any(p.startswith("golden:") and "calibration bucket" in p for p in found))     # golden is calibration-only by rule

    def test_no_ticket_type_may_exceed_20_percent(self):
        cases, sel, part, gold, dev = self.parts()
        for i, c in enumerate([c for c in cases if c["pool"] == "client_c"][:12]):
            c["ticket_text"] = f"disk problem reported on host{i}"
            c["host"] = f"host{i}"
        sel["cases"] = self.entries_for(cases)
        self.assertTrue(any("cap 10" in p for p in self.problems(cases, sel, part, gold, dev)))

    def test_content_must_be_the_drawn_ticket(self):
        cases, sel, part, gold, dev = self.parts()
        cases[3]["ticket_text"] += " (edited)"
        self.assertTrue(any("content does not match" in p for p in self.problems(cases, sel, part, gold, dev)))

    def test_cases_not_in_the_selection_or_missing_from_it_are_reported(self):
        cases, sel, part, gold, dev = self.parts()
        sel["cases"].pop()
        self.assertTrue(any("does not match the cases recorded" in p for p in self.problems(cases, sel, part, gold, dev)))

    def test_golden_cases_must_be_approved_golden_items_exactly(self):
        cases, sel, part, gold, dev = self.parts()
        self.assertTrue(any("not an item of the approved golden file" in p for p in self.problems(cases, sel, part, [], dev)))
        for g in gold:
            g["text"] += " changed"
        self.assertTrue(any("differs from the approved golden item" in p for p in self.problems(cases, sel, part, gold, dev)))

    def test_nothing_may_be_shared_with_the_development_subset(self):
        cases, sel, part, gold, dev = self.parts()
        dev["cases"].append({"case_id": "x", "cluster_id": sel["cases"][0]["cluster_id"]})
        self.assertTrue(any("development subset" in p for p in self.problems(cases, sel, part, gold, dev)))
        cases, sel, part, gold, dev = self.parts()
        dev["cases"].append({"case_id": cases[0]["case_id"], "cluster_id": "unrelated"})
        self.assertTrue(any("development subset" in p for p in self.problems(cases, sel, part, gold, dev)))

    def test_validate_raises_with_every_problem(self):
        cases, sel, part, gold, dev = self.parts()
        cases[0]["ticket_text"] += "x"
        with self.assertRaises(composition.CompositionError):
            composition.validate(cases, sel, part, gold, dev)

    def test_the_freeze_refuses_a_bad_composition(self):
        w = build_world()
        cases = w.read("cases")
        cases[5]["ticket_text"] += " (edited after the draw)"
        w.write("cases", cases)
        with self.assertRaisesRegex(FreezeError, "composition"):
            w.freeze()

    def test_the_freeze_refuses_an_experiment_selection_that_was_not_drawn_for_this_round_and_planner_freeze(self):
        for key, value in (("round", 2), ("planner_freeze_hash", "0" * 64), ("draw_seed", 1), ("partition_seed", 1)):
            w = build_world()
            sel = w.read("experiment_selection")
            sel[key] = value
            w.write("experiment_selection", sel)
            with self.assertRaisesRegex(FreezeError, "not drawn with this round"):
                w.freeze()


# -- planners come from the freeze record ------------------------------------------------------------------------------------------------

class FrozenPlannerTests(unittest.TestCase):
    def test_the_frozen_run_takes_no_planners_or_data_from_the_caller(self):
        self.assertEqual(list(inspect.signature(run_experiment_frozen).parameters), ["record_path"])
        w = build_world()
        w.freeze()
        with self.assertRaises(TypeError):
            run_experiment_frozen(w.out, {"rules_only": object})              # a caller cannot hand in planners

    def test_the_planners_run_are_the_ones_the_frozen_config_names(self):
        w = build_world()
        w.freeze()
        with planners_here():
            out = run_experiment_frozen(w.out)
        self.assertTrue(out["frozen"])
        self.assertTrue(out["not_a_result"])                        # a dry run on the frozen inputs is not claimed: only run_once makes a result
        self.assertFalse(out["claimed"])
        self.assertEqual(out["cases"], 50)
        # rules_only is GiveUpPlanner (escalates, never succeeds) and the others GoodPlanner, exactly as named in the frozen config
        self.assertEqual(out["modes"]["rules_only"]["summary"]["trajectory_success_rate"]["estimate"], 0.0)
        self.assertEqual(out["modes"]["rules_then_model"]["summary"]["trajectory_success_rate"]["estimate"], 1.0)
        self.assertEqual(out["modes"]["model"]["summary"]["trajectory_success_rate"]["estimate"], 1.0)

    def test_the_config_planners_must_be_the_planner_freezes(self):
        w = build_world()
        config = w.read("config")
        config["planners"] = {**config["planners"], "rules_only": "tests.pe_fake_planners:GoodPlanner"}
        w.write("config", config)
        with self.assertRaisesRegex(FreezeError, "config.planners"):
            w.freeze()

    def test_a_planner_outside_the_frozen_folder_is_refused_at_run_time_too(self):
        w = build_world()
        w.freeze()
        with patch.object(planner_freeze, "PLANNERS_DIR", TESTS_DIR / "subfolder_that_does_not_hold_them"):
            with self.assertRaises(planner_freeze.PlannerFreezeError):
                run_experiment_frozen(w.out)

    def test_planner_or_runtime_code_changed_between_the_planner_freeze_and_the_experiment_freeze_is_refused(self):
        root = fake_root()
        w = build_world(root=root)                                                 # planners frozen against this code
        (root / "agentruntime" / "planners" / "p.py").write_text("RULES = 99\n", encoding="utf-8")       # tuned afterwards
        with self.assertRaisesRegex(FreezeError, "differs from the planner freeze"):
            freeze.create_freeze(w.files, w.reviewer_files, w.out, root=root)

    def test_the_draw_seed_must_come_from_the_planner_freeze(self):
        w = build_world()
        w.write("seed_record", seed_mod.check_record({"purpose": "planner-experiment-1", "artifact": "c" * 40,
                                                      "seed": seed_mod.derive_seed("c" * 40, "planner-experiment-1")}, "planner-experiment-1"))
        config = w.read("config")
        config["seed"] = seed_mod.derive_seed("c" * 40, "planner-experiment-1")
        w.write("config", config)
        with self.assertRaisesRegex(FreezeError, "planner-freeze hash"):
            w.freeze()

    def test_the_config_seed_must_be_the_derived_seed(self):
        w = build_world()
        config = w.read("config")
        config["seed"] = 20261014
        w.write("config", config)
        with self.assertRaisesRegex(FreezeError, "config.seed"):
            w.freeze()

    def test_the_development_selection_must_be_the_one_the_planners_were_frozen_against(self):
        w = build_world()
        dev = w.read("development_selection")
        dev["cases"].pop()
        w.write("development_selection", dev)
        with self.assertRaisesRegex(FreezeError, "development selection"):
            w.freeze()

    def test_the_experiment_is_run_once_per_freeze_record(self):
        w = build_world()
        w.freeze()
        results = w.dir / "results"
        with planners_here():
            first = run_once(w.out, results)
            self.assertEqual(len([p for p in results.glob("results_*.json") if ".claim." not in p.name]), 1)
            self.assertEqual(len(list(results.glob("results_*.claim.json"))), 1)
            with self.assertRaisesRegex(ExperimentInputError, "already been claimed"):
                run_once(w.out, results)
        self.assertEqual(first["record_hash"], json.loads(w.out.read_text(encoding="utf-8"))["record_hash"])
        self.assertFalse(first["not_a_result"])
        self.assertTrue(first["claimed"])


class FrozenInputTamperTests(unittest.TestCase):
    def test_changing_any_frozen_file_after_the_freeze_stops_the_run(self):
        names = ["annotations", "cases", "recording", "injection_cases", "config", "seed_record", "partition_seed_record", "experiment_selection",
                 "development_selection", "planner_freeze", "reviewers", "golden"]
        for name in names + [f"reviewer:{n}" for n in NAMES]:
            w = build_world()
            w.freeze()
            target = w.files[name] if name in w.files else w.reviewer_files[name.split(":", 1)[1]]
            target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with planners_here(), self.assertRaises(FreezeError, msg=name):
                run_experiment_frozen(w.out)

    def test_a_later_read_can_never_silently_return_different_content(self):
        w = build_world()
        record = w.freeze()
        data = freeze.read_frozen(record, "cases")
        self.assertEqual(json.loads(data.decode("utf-8")), w.cases)
        w.write("cases", [])
        with self.assertRaises(FreezeError):
            freeze.read_frozen(record, "cases")

    def test_adding_removing_or_changing_a_code_file_after_the_freeze_is_detected(self):
        root = fake_root()
        w = build_world(root=root)
        record = freeze.create_freeze(w.files, w.reviewer_files, w.out, root=root)
        freeze.verify_code(record, root)
        (root / "agentruntime" / "planners" / "new_planner.py").write_text("y = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(FreezeError, "set of code files changed"):
            freeze.verify_code(record, root)
        (root / "agentruntime" / "planners" / "new_planner.py").unlink()
        (root / "planner_experiment" / "m.py").write_text("x = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(FreezeError, "changed after the freeze"):
            freeze.verify_code(record, root)

    def test_the_planners_folder_and_harness_are_part_of_the_hashed_code(self):
        keys = set(freeze.code_files())
        for expected in ("code/agentruntime/planners/__init__.py", "code/planner_experiment/experiment.py", "code/planner_experiment/metrics.py",
                         "code/planner_experiment/composition.py", "code/planner_experiment/planner_freeze.py", "code/agentruntime/tools/patterns.py"):
            self.assertIn(expected, keys)

    def test_a_tampered_record_is_refused(self):
        w = build_world()
        w.freeze()
        record = json.loads(w.out.read_text(encoding="utf-8"))
        record["agreement"]["end_state_kappa"] = 1.0
        record["reviewers"] = ["Eve", "Mallory"]
        w.out.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(FreezeError, "record itself"):
            freeze.verify_freeze(w.out)

    def test_every_required_input_is_needed_and_unknown_inputs_are_refused(self):
        w = build_world()
        for name in freeze.REQUIRED_FILES:
            with self.assertRaises(FreezeError, msg=name):
                freeze.create_freeze({k: v for k, v in w.files.items() if k != name}, w.reviewer_files, w.out)
        with self.assertRaises(FreezeError):
            freeze.create_freeze({**w.files, "extra": w.files["cases"]}, w.reviewer_files, w.out)


class ReviewerAgreementFreezeTests(unittest.TestCase):
    """The freeze recomputes agreement from the reviewers' own files: an empty or partial record cannot pass."""

    def rewrite(self, w, name, change):
        items = w.read(name)
        change(items)
        w.write(name, items)

    def test_a_complete_agreeing_record_freezes_and_stores_the_agreement(self):
        w = build_world()
        record = w.freeze()
        self.assertEqual(record["agreement"]["cases_compared"], 50)
        self.assertEqual(record["agreement"]["end_state_kappa"], 1.0)
        self.assertEqual(sorted(record["reviewers"]), ["Ann", "Bob"])

    def test_empty_or_partial_reviewer_files_are_refused(self):
        w = build_world()
        self.rewrite(w, "Bob", lambda items: items.clear())
        with self.assertRaisesRegex(FreezeError, "exactly the 50 frozen cases"):
            w.freeze()
        w = build_world()
        self.rewrite(w, "Ann", lambda items: items.pop())
        with self.assertRaises(FreezeError):
            w.freeze()

    def test_duplicates_and_foreign_reviewer_names_are_refused(self):
        w = build_world()
        self.rewrite(w, "Bob", lambda items: items.__setitem__(0, dict(items[1])))
        with self.assertRaises(FreezeError):
            w.freeze()
        w = build_world()
        self.rewrite(w, "Bob", lambda items: items[0].__setitem__("reviewer", "Ann"))
        with self.assertRaisesRegex(FreezeError, "under that reviewer"):
            w.freeze()

    def test_low_agreement_blocks_the_freeze(self):
        w = build_world()

        def split(items):
            for m in items[:25]:
                m["end_state"], m["cause_keywords"], m["required_evidence"] = "escalate", [], []
        self.rewrite(w, "Bob", split)
        with self.assertRaisesRegex(FreezeError, "kappa"):
            w.freeze()

    def test_every_disagreement_needs_a_written_adjudication(self):
        w = build_world()
        self.rewrite(w, "Bob", lambda items: items[0].__setitem__("acceptable_first_tools", ["outcome_lookup"]))
        with self.assertRaisesRegex(FreezeError, "needs an adjudication rationale"):
            w.freeze()
        notes = w.read("annotations")
        notes[0]["notes"] = "owner: either tool is acceptable; kept the first"
        w.write("annotations", notes)
        self.assertEqual(w.freeze()["agreement"]["disagreements"][0]["fields"], ["acceptable_first_tools"])

    def test_a_cause_disagreement_also_needs_adjudication(self):
        w = build_world()
        self.rewrite(w, "Bob", lambda items: items[0].__setitem__("cause_keywords", [["memory"], ["leak"]]))
        with self.assertRaisesRegex(FreezeError, "cause_keywords"):
            w.freeze()

    def test_the_adjudicated_file_must_cover_exactly_the_cases(self):
        w = build_world()
        notes = w.read("annotations")
        w.write("annotations", notes[:-1])
        with self.assertRaisesRegex(FreezeError, "cover exactly"):
            w.freeze()


class SeedTests(unittest.TestCase):
    def test_derivation_is_deterministic_and_artifact_bound(self):
        a, b = "5" * 40, "6" * 40
        self.assertEqual(seed_mod.derive_seed(a, "calibration-selection"), seed_mod.derive_seed(a, "calibration-selection"))
        self.assertNotEqual(seed_mod.derive_seed(a, "calibration-selection"), seed_mod.derive_seed(b, "calibration-selection"))
        self.assertNotEqual(seed_mod.derive_seed(a, "calibration-selection"), seed_mod.derive_seed(a, "development-selection"))
        self.assertIsInstance(seed_mod.derive_seed("f" * 64, "planner-experiment-3"), int)             # a record hash is an artifact too
        for bad in ("5ca2980", "g" * 40, "5" * 39, "5" * 41, ""):
            with self.assertRaises(ValueError):
                seed_mod.derive_seed(bad, "calibration-selection")
        for purpose in ("acceptance", "planner-experiment-0", "planner-experiment-", "planner-experiment-a", "planner-experiment"):
            with self.assertRaises(ValueError, msg=purpose):
                seed_mod.derive_seed(a, purpose)

    def test_the_recorded_partition_seed_for_this_repository_is_the_derived_one_and_not_retired(self):
        path = ROOT / "docs" / "baselines" / "planner_experiment" / "seed_calibration.json"
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
        for argv in (["draw-experiment", "--round", "1", "--seed", "20261014"], ["select-development", "--artifact", "a" * 40, "--seed", "1"]):
            with self.assertRaises(SystemExit):
                cli.main(argv)


if __name__ == "__main__":
    unittest.main()
