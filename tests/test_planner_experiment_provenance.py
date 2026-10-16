"""
Provenance of the development draw and of the model (review of 2026-10-15, after the incident-only redraw):
  * the selection algorithm has a version that moves with the algorithm and is enforced wherever a selection is consumed;
  * voids of the development draw must be acknowledged by both reviewers before the planners are frozen;
  * the development draws and recordings stay out of git;
  * the model manifest records exactly which model, file, parameters and budgets the model planners use, and can be checked against the real server and file.
"""
import hashlib
import inspect
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agentruntime.contracts import Budgets
from agentruntime.planners import model_manifest as mm
from planner_experiment import cli, freeze, planner_freeze, steps
from planner_experiment import select_cases as sc
from planner_experiment.freeze import FreezeError, ROOT
from tests.pe_world import ENTRYPOINTS, TESTS_DIR, build_world, golden_raw, make_tickets, register_test_custody

ALGORITHM_FUNCTIONS = ["normalise", "cluster_id", "ticket_type", "type_cap", "bucket", "month_of", "content_hash", "keyed_cluster", "keyed_type",
                       "is_eligible", "select_calibration", "_tokens", "text_sketch", "sketch_similarity", "selection_entries", "case_id_for"]
# version -> fingerprint of the selection algorithm's source and constants. Changing the algorithm changes the fingerprint and fails the test below:
# advance sc.SELECTION_VERSION, add the new version to SELECTION_HISTORY and to this table, and void any draw made under the old one.
FINGERPRINTS = {"3": "578fcdc5186123ecae028b559ccf0993129f6c2bd3841e00b7f8f462515b4a8c"}


def algorithm_fingerprint() -> str:
    source = "".join(inspect.getsource(getattr(sc, n)).replace("\r\n", "\n") for n in ALGORITHM_FUNCTIONS)
    constants = repr((sc.CALIBRATION_BUCKETS, sc.ACCEPTANCE_BUCKETS, sc.EXPERIMENT_QUOTAS, sc.DEVELOPMENT_QUOTAS, sc.MAX_TYPE_SHARE, sc.SKETCH_K,
                      sc.INCIDENT_ID.pattern))
    return hashlib.sha256((source + constants).encode("utf-8")).hexdigest()


class SelectionVersionTests(unittest.TestCase):
    def test_the_version_is_advanced_and_documented(self):
        self.assertEqual(sc.SELECTION_VERSION, "3")
        self.assertIn(sc.SELECTION_VERSION, sc.SELECTION_HISTORY)
        self.assertIn("eligibility", sc.SELECTION_HISTORY["3"])
        self.assertIn("NO eligibility", sc.SELECTION_HISTORY["2"])                        # version 2 is the one that drew Change records

    def test_the_algorithm_cannot_change_without_a_version_bump(self):
        self.assertIn(sc.SELECTION_VERSION, FINGERPRINTS, "add this version's fingerprint to FINGERPRINTS when the version advances")
        self.assertEqual(algorithm_fingerprint(), FINGERPRINTS[sc.SELECTION_VERSION],
                         "the selection algorithm changed: advance SELECTION_VERSION (and SELECTION_HISTORY), record the new fingerprint, "
                         "and void any draw made under the old version")

    def test_every_new_draw_carries_the_current_version(self):
        w = build_world()
        for key in ("experiment_selection", "development_selection"):
            self.assertEqual(w.read(key)["selection_version"], sc.SELECTION_VERSION)

    def test_a_selection_recorded_under_another_version_is_refused_by_the_steps(self):
        w = build_world()
        sel_path = steps.round_paths(w.base, 1)["selection"]
        record = json.loads(sel_path.read_text(encoding="utf-8"))
        steps.check_selection_version(record, "x")
        record["selection_version"] = "2"
        sel_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(steps.StepError, "version '2'"):
            steps.build_cases(sel_path, w.tickets)
        with self.assertRaises(steps.StepError):
            steps.check_selection_version({}, "x")                                          # no version recorded at all

    def test_the_development_version_is_checked_before_planners_are_frozen_and_before_a_draw(self):
        base = pathlib.Path(tempfile.mkdtemp())
        self.setup_base(base)
        dev = steps.paths(base)["development"]
        record = json.loads(dev.read_text(encoding="utf-8"))
        record["selection_version"] = "2"
        dev.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(steps.StepError, "version '2'"):
            steps.freeze_planners(base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        record["selection_version"] = sc.SELECTION_VERSION
        dev.write_text(json.dumps(record), encoding="utf-8")
        steps.freeze_planners(base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        record["selection_version"] = "2"
        dev.write_text(json.dumps(record), encoding="utf-8")
        # once the planners are frozen the file's hash is checked first, so an edited file is refused for that reason (stronger than the label check)
        with self.assertRaisesRegex(steps.StepError, "(version '2'|not the one the planners were frozen against)"):
            steps.draw_experiment(base, 1, make_tickets())

    @staticmethod
    def setup_base(base):
        from planner_experiment import golden as golden_mod, seed as seed_mod
        from tests.pe_world import DEVELOPMENT_ARTIFACT, PARTITION_ARTIFACT
        steps.record_reviewers(base, ["Ann", "Bob"], {"Ann": 50, "Bob": 50}, "owner")
        register_test_custody(base)
        seed_mod.write_record(steps.paths(base)["partition_seed"], PARTITION_ARTIFACT, "calibration-selection")
        gpath = base / "g.json"
        gpath.write_text(json.dumps(golden_raw()), encoding="utf-8")
        steps.select_development(base, make_tickets() + golden_mod.load_golden(gpath, ["Ann", "Bob"]), DEVELOPMENT_ARTIFACT)

    def test_the_freeze_refuses_every_selection_recorded_under_another_version(self):
        for key in ("experiment_selection", "development_selection"):
            w = build_world()
            record = w.read(key)
            record["selection_version"] = "2"
            w.write(key, record)
            with self.assertRaises(FreezeError, msg=key):
                w.freeze()

    def test_a_prior_round_under_another_version_is_refused_at_the_freeze(self):
        w = build_world(rounds=2)
        prior = json.loads(w.files["prior_selection:1"].read_text(encoding="utf-8"))
        prior["selection_version"] = "2"
        w.files["prior_selection:1"].write_text(json.dumps(prior, sort_keys=True), encoding="utf-8")
        sel = w.read("experiment_selection")
        sel["prior_rounds"][0]["selection_hash"] = freeze.sha256_bytes(w.files["prior_selection:1"].read_bytes())
        w.write("experiment_selection", sel)
        with self.assertRaisesRegex(FreezeError, "version '2'"):
            w.freeze()

    def test_the_repository_development_draw_is_under_the_current_version_when_present(self):
        dev = ROOT / "docs" / "baselines" / "planner_experiment" / "development_selection.json"
        if not dev.exists():
            self.skipTest("no development draw on this checkout (it is not kept in git)")
        self.assertEqual(json.loads(dev.read_text(encoding="utf-8"))["selection_version"], sc.SELECTION_VERSION)


class VoidAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="pe-ack-"))
        SelectionVersionTests.setup_base(self.base)
        steps.void_development(self.base, "first problem", "project owner")
        from tests.pe_world import DEVELOPMENT_ARTIFACT
        from planner_experiment import golden as golden_mod
        gpath = self.base / "g.json"
        steps.select_development(self.base, make_tickets() + golden_mod.load_golden(gpath, ["Ann", "Bob"]), DEVELOPMENT_ARTIFACT)
        steps.void_development(self.base, "second problem", "project owner")
        steps.select_development(self.base, make_tickets() + golden_mod.load_golden(gpath, ["Ann", "Bob"]), DEVELOPMENT_ARTIFACT)

    def test_planners_cannot_be_frozen_until_both_reviewers_acknowledge_every_void(self):
        with self.assertRaisesRegex(steps.StepError, "acknowledge every void"):
            steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        steps.acknowledge_voids(self.base, [1, 2], "Ann", "received by e-mail")
        with self.assertRaisesRegex(steps.StepError, "void 1: Bob"):                          # names exactly what is still missing
            steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        steps.acknowledge_voids(self.base, [1], "Bob")
        with self.assertRaisesRegex(steps.StepError, "void 2: Bob"):
            steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        steps.acknowledge_voids(self.base, [2], "Bob")
        self.assertEqual(steps.unacknowledged_voids(self.base), [])
        steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)

    def test_the_acknowledgements_sit_beside_the_void_record(self):
        steps.acknowledge_voids(self.base, [1, 2], "ann", "read the notice")
        log = json.loads((self.base / "development_voids.json").read_text(encoding="utf-8"))
        for entry in log:
            (ack,) = entry["acknowledged_by"]
            self.assertEqual(ack["reviewer"], "Ann")                                          # recorded in the reviewer record's spelling
            self.assertEqual(ack["note"], "read the notice")
            self.assertRegex(ack["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertTrue(entry["reason"] and entry["authorised_by"] and entry["sha256"])

    def test_only_the_recorded_reviewers_can_acknowledge_and_only_once(self):
        for who in ("Eve", "", "owner", "Ann Bob"):
            with self.assertRaises(steps.StepError, msg=who):
                steps.acknowledge_voids(self.base, [1], who)
        steps.acknowledge_voids(self.base, [1], "Ann")
        with self.assertRaisesRegex(steps.StepError, "already acknowledged"):
            steps.acknowledge_voids(self.base, [1], "Ann")
        for voids in ([], [1, 1], [3], [0]):
            with self.assertRaises(steps.StepError, msg=voids):
                steps.acknowledge_voids(self.base, voids, "Bob")
        self.assertEqual(steps.unacknowledged_voids(self.base), ["void 1: Bob", "void 2: Ann", "void 2: Bob"])

    def test_the_planner_freeze_records_the_void_log_it_was_frozen_under(self):
        steps.acknowledge_voids(self.base, [1, 2], "Ann")
        steps.acknowledge_voids(self.base, [1, 2], "Bob")
        record = steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        self.assertEqual(record["void_log_hash"], freeze.sha256_bytes((self.base / "development_voids.json").read_bytes()))
        planner_freeze.check_record(record)
        record["void_log_hash"] = "0" * 64
        with self.assertRaises(planner_freeze.PlannerFreezeError):
            planner_freeze.check_record(record)                                                # the hash is part of the record's own hash

    def test_a_draw_that_was_never_voided_needs_no_acknowledgement(self):
        base = pathlib.Path(tempfile.mkdtemp())
        SelectionVersionTests.setup_base(base)
        self.assertEqual(steps.unacknowledged_voids(base), [])
        record = steps.freeze_planners(base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        self.assertIsNone(record["void_log_hash"])

    def test_the_command_line(self):
        with patch.object(steps, "BASE", self.base):
            self.assertEqual(cli.main(["acknowledge-void", "--voids", "1", "2", "--by", "Ann", "--note", "n"]), 0)
            self.assertEqual(cli.main(["acknowledge-void", "--voids", "1", "--by", "Ann"]), 2)       # twice
            self.assertEqual(cli.main(["acknowledge-void", "--voids", "1", "2", "--by", "Mallory"]), 2)
            self.assertEqual(cli.main(["acknowledge-void", "--voids", "1", "2", "--by", "Bob"]), 0)
        self.assertEqual(steps.unacknowledged_voids(self.base), [])


class KeepOutOfGitTests(unittest.TestCase):
    PATHS = ["docs/baselines/planner_experiment/development_selection.json", "docs/baselines/planner_experiment/development_selection_VOID_1.json",
             "docs/baselines/planner_experiment/development_selection_VOID_12.json", "data/planner_dev/recordings.json",
             "data/planner_dev/dev_cases.json", "data/planner_experiment.key"]
    COMMITTED = ["docs/baselines/planner_experiment/development_voids.json", "docs/baselines/planner_experiment/seed_development.json",
                 "docs/baselines/planner_experiment/seed_calibration.json"]

    def git(self, *args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        except OSError:
            self.skipTest("git is not available")

    def test_development_selections_recordings_and_the_key_are_ignored(self):
        if self.git("rev-parse", "--git-dir").returncode != 0:
            self.skipTest("not a git checkout")
        for path in self.PATHS:
            self.assertEqual(self.git("check-ignore", "-q", path).returncode, 0, path)

    def test_the_content_free_records_are_not_ignored(self):
        if self.git("rev-parse", "--git-dir").returncode != 0:
            self.skipTest("not a git checkout")
        for path in self.COMMITTED:
            self.assertNotEqual(self.git("check-ignore", "-q", path).returncode, 0, path)

    def test_none_of_them_has_ever_been_committed(self):
        if self.git("rev-parse", "--git-dir").returncode != 0:
            self.skipTest("not a git checkout")
        for path in ("docs/baselines/planner_experiment/development_selection.json", "docs/baselines/planner_experiment/development_selection_VOID_1.json",
                     "docs/baselines/planner_experiment/development_selection_VOID_2.json", "data/planner_experiment.key", "data/planner_dev/dev_cases.json"):
            self.assertEqual(self.git("log", "--all", "--oneline", "--", path).stdout.strip(), "", path)
            self.assertNotEqual(self.git("ls-files", "--error-unmatch", path).returncode, 0, path)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def good_opener(url, timeout_s):
    if url.endswith("/v1/models"):
        return FakeResponse({"data": [{"id": mm.MODEL["served_id"]}, {"id": "other"}]})
    return FakeResponse({"id": mm.MODEL["served_id"], "publisher": mm.MODEL["publisher"], "arch": mm.MODEL["architecture"],
                         "quantization": mm.MODEL["quantization"], "state": "loaded", "loaded_context_length": 8192, "max_context_length": 131072})


class VerbatimAcknowledgementTests(unittest.TestCase):
    """The reviewers' own words are audit evidence: kept exactly (typos, case, spacing), never paraphrased, with corrections preserved and visible."""

    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="pe-verbatim-"))
        SelectionVersionTests.setup_base(self.base)
        steps.void_development(self.base, "first problem", "project owner")
        from planner_experiment import golden as golden_mod
        from tests.pe_world import DEVELOPMENT_ARTIFACT
        steps.select_development(self.base, make_tickets() + golden_mod.load_golden(self.base / "g.json", ["Ann", "Bob"]), DEVELOPMENT_ARTIFACT)

    def entry(self, reviewer):
        log = json.loads((self.base / "development_voids.json").read_text(encoding="utf-8"))
        return next(a for a in log[0]["acknowledged_by"] if a["reviewer"] == reviewer)

    def test_the_words_are_stored_exactly_as_written(self):
        words = "Understand the challenge, I am ok with the current approch and I support it "         # a typo, a trailing space, mixed case
        overall = "NO additional comment"
        steps.acknowledge_voids(self.base, [1], "Bob", comments={1: words}, overall=overall, sheet="sheet_Bob.xlsx")
        ack = self.entry("Bob")
        self.assertEqual(ack["void_comment"], words)
        self.assertEqual(ack["overall_comment"], overall)
        self.assertEqual(ack["source_sheet"], "sheet_Bob.xlsx")
        self.assertEqual(ack["note"], "")                                                        # the recorder's own remark is never where the reviewer's words go

    def test_unicode_and_odd_spacing_survive(self):
        words = "  Prüfung: \u201cok\u201d \u2013 ja\t\n"
        steps.acknowledge_voids(self.base, [1], "Ann", comments={1: words})
        self.assertEqual(self.entry("Ann")["void_comment"], words)

    def test_a_correction_replaces_the_paraphrase_keeps_it_and_says_why(self):
        steps.acknowledge_voids(self.base, [1], "Ann", note="a paraphrase that added things the reviewer never wrote")
        ack = steps.correct_acknowledgement(self.base, 1, "ann", "exact words", "exact overall", "sheet_Ann.xlsx", "paraphrase replaced with the sheet text")
        self.assertEqual((ack["void_comment"], ack["overall_comment"], ack["source_sheet"], ack["note"]), ("exact words", "exact overall", "sheet_Ann.xlsx", ""))
        (correction,) = ack["corrections"]
        self.assertEqual(correction["previous_note"], "a paraphrase that added things the reviewer never wrote")
        self.assertEqual(correction["reason"], "paraphrase replaced with the sheet text")
        self.assertEqual(ack["reviewer"], "Ann")                                                 # who and when are untouched
        steps.correct_acknowledgement(self.base, 1, "Ann", "exact words v2", "exact overall", "sheet_Ann.xlsx", "second fix")
        self.assertEqual(len(self.entry("Ann")["corrections"]), 2)                                 # every correction stays visible

    def test_corrections_are_validated_and_refused_once_planners_are_frozen(self):
        steps.acknowledge_voids(self.base, [1], "Ann")
        for comment, reason in (("", "r"), ("c", ""), (" ", "r"), ("c", "  ")):
            with self.assertRaises(steps.StepError, msg=(comment, reason)):
                steps.correct_acknowledgement(self.base, 1, "Ann", comment, "o", "s", reason)
        with self.assertRaisesRegex(steps.StepError, "no recorded acknowledgement"):
            steps.correct_acknowledgement(self.base, 1, "Bob", "c", "o", "s", "r")                  # Bob has not acknowledged
        with self.assertRaises(steps.StepError):
            steps.correct_acknowledgement(self.base, 9, "Ann", "c", "o", "s", "r")
        steps.acknowledge_voids(self.base, [1], "Bob")
        steps.freeze_planners(self.base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        with self.assertRaisesRegex(steps.StepError, "can no longer be corrected"):
            steps.correct_acknowledgement(self.base, 1, "Ann", "c", "o", "s", "r")

    def test_the_command_line(self):
        with patch.object(steps, "BASE", self.base):
            code = cli.main(["acknowledge-void", "--voids", "1", "--by", "Ann", "--comment", "1=word, with = signs", "--overall", "fine", "--sheet", "a.xlsx"])
            self.assertEqual(code, 0)
            self.assertEqual(self.entry("Ann")["void_comment"], "word, with = signs")                # only the first '=' separates the void number
            self.assertEqual(cli.main(["correct-acknowledgement", "--void", "1", "--by", "Ann", "--comment", "exact", "--overall", "o", "--sheet", "a.xlsx",
                                       "--reason", "why"]), 0)
            self.assertEqual(self.entry("Ann")["void_comment"], "exact")

    def test_the_repository_log_matches_the_reviewers_sheets_word_for_word(self):
        log_path = ROOT / "docs" / "baselines" / "planner_experiment" / "development_voids.json"
        log = json.loads(log_path.read_text(encoding="utf-8"))
        for e in log:
            for a in e["acknowledged_by"]:
                for field in ("void_comment", "overall_comment", "source_sheet"):
                    self.assertIn(field, a, (e["void"], a["reviewer"], field))
                self.assertEqual(a["note"], "")                                                  # no recorder paraphrase stands in for their words
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl is not installed here, so the sheets cannot be read")
        for e in log:
            for a in e["acknowledged_by"]:
                sheet = ROOT / "docs" / "drafts" / a["source_sheet"]
                if not sheet.exists():
                    self.skipTest(f"{a['source_sheet']} is not on this checkout")
                wb = openpyxl.load_workbook(sheet, data_only=True)
                cells = {r[0]: r[6] for r in list(wb["Voids"].iter_rows(values_only=True))[1:]}
                self.assertEqual(a["void_comment"], cells[f"Void {e['void']}"], (e["void"], a["reviewer"]))
                self.assertEqual(a["overall_comment"], wb["Reviewer"]["B3"].value, a["reviewer"])


class KeyRetirementAndCustodyTests(unittest.TestCase):
    """A key that has been on a machine a development session can read is development-accessible. It is retired by its check value, and a draw,
    freeze or run uses only the replacement registered as being in custody (created on the key-holder machine, never shown to the development session)."""

    OTHER = bytes.fromhex("cd" * 32)

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="pe-custody-"))
        self.retired = self.dir / "retired_keys.json"
        self.custody = self.dir / "key_custody.json"

    def test_retiring_records_only_the_check_value_and_never_the_key(self):
        from planner_experiment import secrecy
        from tests.pe_world import TEST_KEY, TEST_KEY_HEX
        entry = secrecy.retire(TEST_KEY, "was on the development machine", "project owner", self.retired)
        self.assertEqual(entry["key_check"], secrecy.key_check(TEST_KEY))
        text = self.retired.read_text(encoding="utf-8")
        self.assertNotIn(TEST_KEY_HEX, text)
        self.assertNotRegex(text, r"[0-9a-f]{32,}")                                          # nothing key-sized is written
        self.assertTrue(secrecy.is_retired(entry["key_check"], self.retired))
        self.assertFalse(secrecy.is_retired(secrecy.key_check(self.OTHER), self.retired))

    def test_retiring_needs_a_reason_and_a_named_person_and_is_append_only(self):
        from planner_experiment import secrecy
        from tests.pe_world import TEST_KEY
        for reason, who in (("", "project owner"), ("why", ""), ("why", "TBD"), ("  ", "project owner")):
            with self.assertRaises(secrecy.KeyError_, msg=(reason, who)):
                secrecy.retire(TEST_KEY, reason, who, self.retired)
        secrecy.retire(TEST_KEY, "first", "project owner", self.retired)
        with self.assertRaisesRegex(secrecy.KeyError_, "already retired"):
            secrecy.retire(TEST_KEY, "again", "project owner", self.retired)
        secrecy.retire(self.OTHER, "second key", "project owner", self.retired)
        self.assertEqual(len(json.loads(self.retired.read_text(encoding="utf-8"))), 2)

    def test_custody_registration_is_validated(self):
        from planner_experiment import secrecy
        good = secrecy.key_check(self.OTHER)
        for check in ("", "abc", "g" * 16, good + "0", good.upper() + "x"):
            with self.assertRaises(secrecy.KeyError_, msg=check):
                secrecy.register_custody(check, "owner machine", self.custody, self.retired)
        for who in ("", "  ", "TBD", "owner"):
            with self.assertRaises(secrecy.KeyError_, msg=who):
                secrecy.register_custody(good, who, self.custody, self.retired)
        record = secrecy.register_custody(good, "Project owner, separate key-holder machine", self.custody, self.retired)
        self.assertEqual(record["key_check"], good)
        self.assertIn("never present on a machine a development session can read", record["statement"])

    def test_a_retired_key_cannot_be_registered_and_a_registration_is_replaced_only_after_retirement(self):
        from planner_experiment import secrecy
        from tests.pe_world import TEST_KEY
        secrecy.retire(TEST_KEY, "development-accessible", "project owner", self.retired)
        with self.assertRaisesRegex(secrecy.KeyError_, "retired"):
            secrecy.register_custody(secrecy.key_check(TEST_KEY), "owner machine", self.custody, self.retired)
        secrecy.register_custody(secrecy.key_check(self.OTHER), "owner machine", self.custody, self.retired)
        third = secrecy.key_check(bytes.fromhex("ef" * 32))
        with self.assertRaisesRegex(secrecy.KeyError_, "already registered"):
            secrecy.register_custody(third, "owner machine", self.custody, self.retired)
        secrecy.retire(self.OTHER, "replaced", "project owner", self.retired)
        secrecy.register_custody(third, "owner machine", self.custody, self.retired)                 # allowed once the registered key is retired
        self.assertEqual(secrecy.custody(self.custody)["key_check"], third)

    def test_the_active_key_must_be_present_not_retired_and_the_registered_one(self):
        from planner_experiment import secrecy
        from tests.pe_world import TEST_KEY, no_key
        with no_key():
            with self.assertRaisesRegex(secrecy.KeyError_, "experiment key is required"):
                secrecy.active_key(self.custody, self.retired)
        with self.assertRaisesRegex(secrecy.KeyError_, "no key is registered"):
            secrecy.active_key(self.custody, self.retired)                                            # the test key is loaded, nothing is registered
        secrecy.register_custody(secrecy.key_check(self.OTHER), "owner machine", self.custody, self.retired)
        with self.assertRaisesRegex(secrecy.KeyError_, "not the key registered"):
            secrecy.active_key(self.custody, self.retired)                                            # a different key is registered
        pathlib.Path(self.custody).unlink()
        secrecy.register_custody(secrecy.key_check(TEST_KEY), "owner machine", self.custody, self.retired)
        self.assertEqual(secrecy.active_key(self.custody, self.retired), TEST_KEY)                    # the registered key is accepted
        secrecy.retire(TEST_KEY, "now development-accessible", "project owner", self.retired)
        with self.assertRaisesRegex(secrecy.KeyError_, "retired"):
            secrecy.active_key(self.custody, self.retired)

    def draw_ready_base(self):
        base = pathlib.Path(tempfile.mkdtemp(prefix="pe-draw-"))
        SelectionVersionTests.setup_base(base)                                                        # registers the test key in custody
        from planner_experiment import golden as golden_mod
        steps.freeze_planners(base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
        return base, make_tickets() + golden_mod.load_golden(base / "g.json", ["Ann", "Bob"])

    def test_a_draw_is_refused_with_a_retired_unregistered_or_unknown_key(self):
        from planner_experiment import secrecy
        from tests.pe_world import TEST_KEY
        base, tickets = self.draw_ready_base()
        (base / "key_custody.json").unlink()
        with self.assertRaisesRegex(steps.StepError, "no key is registered"):
            steps.draw_experiment(base, 1, tickets)
        secrecy.register_custody(secrecy.key_check(self.OTHER), "owner machine", base / "key_custody.json", base / "retired_keys.json")
        with self.assertRaisesRegex(steps.StepError, "not the key registered"):
            steps.draw_experiment(base, 1, tickets)
        (base / "key_custody.json").unlink()
        secrecy.register_custody(secrecy.key_check(TEST_KEY), "owner machine", base / "key_custody.json", base / "retired_keys.json")
        secrecy.retire(TEST_KEY, "development-accessible", "project owner", base / "retired_keys.json")
        with self.assertRaisesRegex(steps.StepError, "retired"):
            steps.draw_experiment(base, 1, tickets)
        self.assertFalse(steps.round_paths(base, 1)["selection"].exists())                            # nothing recorded
        self.assertFalse(steps.round_paths(base, 1)["seed"].exists())

    def test_cases_cannot_be_built_with_a_retired_key(self):
        from planner_experiment import secrecy
        w = build_world()
        secrecy.retire(secrecy.load_key(), "development-accessible", "project owner", w.base / "retired_keys.json")
        with self.assertRaisesRegex(steps.StepError, "retired"):
            steps.build_cases(steps.round_paths(w.base, 1)["selection"], w.tickets)

    def test_the_freeze_refuses_a_set_not_drawn_with_the_registered_key_or_drawn_with_a_retired_one(self):
        from planner_experiment import secrecy
        w = build_world()
        custody = w.read("key_custody")
        custody["key_check"] = secrecy.key_check(self.OTHER)
        w.write("key_custody", custody)
        with self.assertRaisesRegex(FreezeError, "not drawn with the key registered"):
            w.freeze()
        w = build_world()
        w.write("retired_keys", [{"key_check": w.read("experiment_selection")["key_check"], "reason": "r", "retired_by": "p", "date": "2026-10-15"}])
        with self.assertRaisesRegex(FreezeError, "retired key"):
            w.freeze()

    def test_a_machine_holding_a_retired_or_unregistered_key_cannot_freeze_or_run(self):
        from planner_experiment import secrecy
        w = build_world()
        with patch.dict("os.environ", {secrecy.KEY_ENV: "cd" * 32}):                               # a different key is on this machine
            with self.assertRaises(FreezeError):
                w.freeze()
        w.freeze()                                                                                  # with the registered key it works
        w.write("retired_keys", [{"key_check": secrecy.key_check(secrecy.load_key()), "reason": "r", "retired_by": "p", "date": "2026-10-15"}])
        w2 = build_world()
        w2.write("retired_keys", [{"key_check": secrecy.key_check(secrecy.load_key()), "reason": "r", "retired_by": "p", "date": "2026-10-15"}])
        with self.assertRaises(FreezeError):
            w2.freeze()

    def test_the_assembled_freeze_carries_custody_and_an_empty_retired_list_when_nothing_is_retired(self):
        w = build_world()
        (w.base / "retired_keys.json").unlink()
        record = steps.create_freeze_for_round(w.base, 1, w.files["cases"], w.files["recording"], w.files["annotations"], dict(w.reviewer_files),
                                               w.extra["golden_path"], out_path=w.dir / "f.json")
        self.assertIn("key_custody", record["files"])
        self.assertIn("retired_keys", record["files"])
        self.assertEqual(json.loads((w.base / "retired_keys.json").read_text(encoding="utf-8")), [])

    def run_cli(self, argv, base):
        import contextlib
        import io
        out = io.StringIO()
        with patch.object(steps, "BASE", base), contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_the_commands_print_check_values_never_the_key(self):
        from planner_experiment import secrecy
        from tests.pe_world import TEST_KEY, TEST_KEY_HEX, no_key
        base = pathlib.Path(tempfile.mkdtemp())
        code, out = self.run_cli(["key-check"], base)
        self.assertEqual(code, 0)
        self.assertIn(secrecy.key_check(TEST_KEY), out)
        self.assertNotIn(TEST_KEY_HEX, out)
        with no_key():
            code, out = self.run_cli(["key-check"], base)
            self.assertIn("no key on this machine", out)
            self.assertEqual(self.run_cli(["retire-key", "--reason", "r", "--by", "project owner"], base)[0], 2)      # nothing to retire
        code, out = self.run_cli(["retire-key", "--reason", "was on the development machine", "--by", "project owner"], base)
        self.assertEqual(code, 0)
        self.assertNotIn(TEST_KEY_HEX, out)
        self.assertNotIn(TEST_KEY_HEX, (base / "retired_keys.json").read_text(encoding="utf-8"))
        self.assertEqual(self.run_cli(["retire-key", "--reason", "again", "--by", "project owner"], base)[0], 2)       # already retired
        check = secrecy.key_check(self.OTHER)
        self.assertEqual(self.run_cli(["register-custody", "--check", check, "--custodian", "Project owner, key-holder machine"], base)[0], 0)
        self.assertEqual(self.run_cli(["register-custody", "--check", "nonsense", "--custodian", "x y"], base)[0], 2)

    def test_the_repository_record_of_retired_keys_holds_only_check_values_when_present(self):
        path = ROOT / "docs" / "baselines" / "planner_experiment" / "retired_keys.json"
        if not path.exists():
            self.skipTest("no retired keys are recorded on this checkout")
        text = path.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"[0-9a-f]{32,}")
        for entry in json.loads(text):
            self.assertRegex(entry["key_check"], r"^[0-9a-f]{16}$")
            self.assertTrue(entry["reason"] and entry["retired_by"])


class VoidLogVerificationTests(unittest.TestCase):
    """The void-log hash recorded in the planner freeze is a claim until it is compared with the file. It is compared at the draw and at the freeze."""

    def frozen_world(self):
        return build_world(with_void=True)

    def test_a_genuine_freeze_with_an_acknowledged_void_log_verifies_and_carries_the_log(self):
        w = self.frozen_world()
        record = w.freeze()
        self.assertIn("development_voids", record["files"])
        pf = json.loads(w.files["planner_freeze"].read_text(encoding="utf-8"))
        self.assertEqual(pf["void_log_hash"], freeze.sha256_bytes(w.files["development_voids"].read_bytes()))
        freeze.verify_freeze(w.out)

    def test_the_log_changed_after_the_planner_freeze_is_refused(self):
        for change in (lambda log: log[0].update(reason="a different story"), lambda log: log[0]["acknowledged_by"][0].update(note="edited"),
                       lambda log: log.append({"void": 2, "file": "x", "sha256": "0" * 64, "reason": "r", "authorised_by": "a", "date": "2026-10-15"}),
                       lambda log: log.clear()):
            w = self.frozen_world()
            log = w.read("development_voids")
            change(log)
            w.write("development_voids", log)
            with self.assertRaisesRegex(FreezeError, "does not match the hash recorded in the planner freeze"):
                w.freeze()

    def test_a_plausible_but_incorrect_recorded_hash_is_refused(self):
        """Tested on the check itself (the rest of the freeze would fail first for other reasons if the planner freeze were edited)."""
        w = self.frozen_world()
        log_bytes = w.files["development_voids"].read_bytes()
        real = freeze.sha256_bytes(log_bytes)
        pf = {"void_log_hash": real}
        freeze.check_void_log(pf, log_bytes, ["Ann", "Bob"])                                       # the right hash passes
        for wrong in ("a" * 64, "0" * 64, real[::-1], real[:-1] + ("0" if real[-1] != "0" else "1"), real.upper(), "x"):
            with self.assertRaisesRegex(FreezeError, "does not match the hash recorded", msg=wrong):
                freeze.check_void_log({"void_log_hash": wrong}, log_bytes, ["Ann", "Bob"])
        for wrong in ("a" * 64, real[::-1]):
            record = {"development_selection_hash": freeze.sha256_bytes(w.files["development_selection"].read_bytes()), "void_log_hash": wrong}
            with self.assertRaisesRegex(planner_freeze.PlannerFreezeError, "does not match the hash recorded"):
                planner_freeze.verify_provenance(record, w.files["development_selection"], w.files["development_voids"])

    def test_the_log_is_required_when_the_planners_were_frozen_under_one(self):
        w = self.frozen_world()
        del w.files["development_voids"]
        with self.assertRaisesRegex(FreezeError, "must be a frozen input"):
            w.freeze()

    def test_a_log_may_not_be_supplied_when_the_planners_were_frozen_without_one(self):
        w = build_world()
        log = w.dir / "forged_voids.json"
        log.write_text("[]", encoding="utf-8")
        w.files["development_voids"] = log
        with self.assertRaisesRegex(FreezeError, "frozen without one"):
            w.freeze()

    def test_every_void_in_the_frozen_log_must_be_acknowledged_by_both_reviewers_at_the_freeze_too(self):
        w = self.frozen_world()
        log = w.read("development_voids")
        names = ["Ann", "Bob"]
        freeze.check_void_log({"void_log_hash": freeze.sha256_bytes(json.dumps(log).encode())}, json.dumps(log).encode(), names)
        for broken in (lambda e: e.update(acknowledged_by=e["acknowledged_by"][:1]), lambda e: e.update(acknowledged_by=[]), lambda e: e.pop("acknowledged_by"),
                       lambda e: e.update(acknowledged_by=[{"reviewer": "Ann"}, {"reviewer": "Mallory"}])):
            entries = json.loads(json.dumps(log))
            broken(entries[0])
            raw = json.dumps(entries).encode()
            with self.assertRaisesRegex(FreezeError, "not acknowledged by both reviewers"):
                freeze.check_void_log({"void_log_hash": freeze.sha256_bytes(raw)}, raw, names)

    def test_a_log_that_is_not_json_is_refused(self):
        raw = b"not json"
        with self.assertRaisesRegex(FreezeError, "not valid JSON"):
            freeze.check_void_log({"void_log_hash": freeze.sha256_bytes(raw)}, raw, ["Ann", "Bob"])

    def test_the_draw_step_rechecks_the_development_selection_and_the_log_against_the_planner_freeze(self):
        for target in ("development_voids.json", "development_selection.json"):
            base = pathlib.Path(tempfile.mkdtemp())
            SelectionVersionTests.setup_base(base)
            steps.void_development(base, "a defect", "project owner")
            from planner_experiment import golden as golden_mod
            from tests.pe_world import DEVELOPMENT_ARTIFACT
            tickets = make_tickets() + golden_mod.load_golden(base / "g.json", ["Ann", "Bob"])
            steps.select_development(base, tickets, DEVELOPMENT_ARTIFACT)
            for who in ("Ann", "Bob"):
                steps.acknowledge_voids(base, [1], who)
            steps.freeze_planners(base, 1, ENTRYPOINTS, planners_dir=TESTS_DIR)
            file = base / target
            file.write_text(file.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaises(steps.StepError, msg=target):
                steps.draw_experiment(base, 1, tickets)
            self.assertFalse(steps.round_paths(base, 1)["selection"].exists())

    def test_verify_provenance_covers_each_case(self):
        base = pathlib.Path(tempfile.mkdtemp())
        dev, log = base / "dev.json", base / "voids.json"
        dev.write_text("{}", encoding="utf-8")
        record = planner_freeze.create_planner_freeze(ENTRYPOINTS, dev, base / "pf.json", base, planners_dir=TESTS_DIR)
        planner_freeze.verify_provenance(record, dev, log)                                       # no void, no log: fine
        log.write_text("[]", encoding="utf-8")
        planner_freeze.verify_provenance(record, dev, log)                                       # an empty log is the same as none
        log.write_text('[{"void": 1}]', encoding="utf-8")
        with self.assertRaisesRegex(planner_freeze.PlannerFreezeError, "void log with entries now exists"):
            planner_freeze.verify_provenance(record, dev, log)
        dev.write_text("{ }", encoding="utf-8")
        with self.assertRaisesRegex(planner_freeze.PlannerFreezeError, "development selection on disk"):
            planner_freeze.verify_provenance(record, dev, log)

    def test_the_assembled_freeze_includes_the_log_automatically(self):
        w = self.frozen_world()
        out = w.dir / "assembled.json"
        record = steps.create_freeze_for_round(w.base, 1, w.files["cases"], w.files["recording"], w.files["annotations"], dict(w.reviewer_files),
                                               w.extra["golden_path"], out_path=out)
        self.assertIn("development_voids", record["files"])


class ModelManifestTests(unittest.TestCase):
    def test_it_records_the_exact_model_file_parameters_and_budgets(self):
        m = mm.manifest()
        self.assertEqual(m["model"]["served_id"], "google/gemma-3-4b")
        self.assertEqual(m["model"]["quantization"], "Q4_K_M")
        self.assertRegex(m["model"]["file_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(m["model"]["file_bytes"], 2489757856)
        self.assertEqual(m["inference"]["temperature"], 0.0)
        self.assertIn("seed", m["inference"])
        self.assertIn("json_schema", m["inference"]["response_format"])
        self.assertIn("local only", m["model"]["scope"])
        self.assertTrue(m["model"]["endpoint"].startswith("http://localhost"))

    def test_the_budgets_are_the_runtimes_own_not_a_copy(self):
        b = Budgets()
        for key, value in mm.budgets_in_force().items():
            self.assertEqual(value, getattr(b, key), key)
        self.assertEqual(mm.budgets_in_force()["model_call_timeout_s"], 15)
        self.assertEqual(mm.budgets_in_force()["max_tokens_planner"], 300)
        self.assertEqual(mm.budgets_in_force()["max_tokens_synthesis"], 900)

    def test_the_manifest_is_complete_with_versioned_prompts_and_incomplete_without_them(self):
        self.assertEqual(mm.incomplete(), [])                                                       # the prompts are written, versioned and hashed
        with patch.dict(mm.PROMPTS, {"planner": None}):
            problems = mm.incomplete()
        self.assertTrue(any("prompt 'planner'" in p and "no version" in p for p in problems))
        self.assertFalse([p for p in problems if "model." in p or "inference." in p])

    def test_any_missing_field_is_reported(self):
        with patch.dict(mm.MODEL, {"file_sha256": "abc"}):
            self.assertTrue(any("file_sha256" in p for p in mm.incomplete()))
        with patch.dict(mm.MODEL, {"served_id": " "}):
            self.assertTrue(any("served_id" in p for p in mm.incomplete()))
        with patch.dict(mm.MODEL, {"file_bytes": 0}):
            self.assertTrue(any("file_bytes" in p for p in mm.incomplete()))
        with patch.dict(mm.INFERENCE, {"temperature": None}):
            self.assertTrue(any("temperature" in p for p in mm.incomplete()))

    def test_the_manifest_hash_changes_with_any_setting(self):
        base = mm.manifest_hash()
        with patch.dict(mm.INFERENCE, {"temperature": 0.2}):
            self.assertNotEqual(mm.manifest_hash(), base)
        with patch.dict(mm.PROMPTS, {"planner": "x"}):
            self.assertNotEqual(mm.manifest_hash(), base)
        with patch.dict(mm.MODEL, {"quantization": "Q8_0"}):
            self.assertNotEqual(mm.manifest_hash(), base)
        self.assertEqual(mm.manifest_hash(), base)

    def test_it_is_part_of_the_hashed_planner_code(self):
        self.assertIn("code/agentruntime/planners/model_manifest.py", freeze.code_files())

    def test_verify_passes_when_the_server_and_file_match(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        target = directory / mm.MODEL["file"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not the real model")
        with patch.dict(mm.MODEL, {"file_bytes": 18, "file_sha256": hashlib.sha256(b"not the real model").hexdigest()}):
            report = mm.verify(opener=good_opener, models_dir=directory)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["observed"]["loaded_context_length"], 8192)

    def test_verify_reports_each_kind_of_mismatch(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        target = directory / mm.MODEL["file"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * 10)
        report = mm.verify(opener=good_opener, models_dir=directory)                              # file differs from the manifest
        self.assertFalse(report["ok"])
        failed = {c["check"] for c in report["checks"] if not c["ok"]}
        self.assertEqual(failed, {"model file size", "model file sha256"})

        def wrong_quantization(url, timeout_s):
            response = good_opener(url, timeout_s)
            payload = json.loads(response.read().decode("utf-8"))
            if "quantization" in payload:
                payload["quantization"] = "Q8_0"
            return FakeResponse(payload)
        report = mm.verify(opener=wrong_quantization, check_file=False)
        self.assertEqual({c["check"] for c in report["checks"] if not c["ok"]}, {"server reports quantization"})

        def not_served(url, timeout_s):
            return FakeResponse({"data": [{"id": "something-else"}]})
        self.assertFalse(mm.verify(opener=not_served, check_file=False)["ok"])

    def test_an_unreachable_server_or_missing_file_is_a_failed_check_not_an_exception(self):
        import urllib.error

        def down(url, timeout_s):
            raise urllib.error.URLError("connection refused")
        report = mm.verify(opener=down, models_dir=pathlib.Path(tempfile.mkdtemp()))
        self.assertFalse(report["ok"])
        self.assertTrue(any(c["check"] == "server reachable" and not c["ok"] for c in report["checks"]))
        self.assertTrue(any(c["check"] == "model file present" and not c["ok"] for c in report["checks"]))

    def test_the_manifest_matches_the_real_model_file_when_it_is_present(self):
        path = pathlib.Path.home() / ".lmstudio" / "models" / mm.MODEL["file"]
        if not path.is_file():
            self.skipTest("the model file is not on this machine")
        self.assertEqual(path.stat().st_size, mm.MODEL["file_bytes"])                              # size only: hashing 2.5 GB is left to `cli verify-model`

    def test_a_production_planner_freeze_is_refused_while_the_manifest_is_incomplete(self):
        base = pathlib.Path(tempfile.mkdtemp())
        SelectionVersionTests.setup_base(base)
        with patch.dict(mm.PROMPTS, {"synthesis": None}), self.assertRaisesRegex(steps.StepError, "model manifest must be complete"):
            steps.freeze_planners(base, 1, ENTRYPOINTS)                                              # planners_dir=None: the production path
        self.assertFalse(steps.round_paths(base, 1)["planner_freeze"].exists())

    def test_the_verify_model_command_reports_and_sets_the_exit_code(self):
        with patch.object(mm, "verify", return_value={"ok": True, "checks": [], "observed": {}, "manifest_hash": "h", "manifest_complete": False}):
            self.assertEqual(cli.main(["verify-model"]), 0)
        with patch.object(mm, "verify", return_value={"ok": False, "checks": [], "observed": {}, "manifest_hash": "h", "manifest_complete": False}):
            self.assertEqual(cli.main(["verify-model"]), 2)


if __name__ == "__main__":
    unittest.main()
