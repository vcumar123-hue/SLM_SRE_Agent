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
from tests.pe_world import ENTRYPOINTS, TESTS_DIR, build_world, golden_raw, make_tickets

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
        with self.assertRaisesRegex(steps.StepError, "version '2'"):
            steps.draw_experiment(base, 1, make_tickets())

    @staticmethod
    def setup_base(base):
        from planner_experiment import golden as golden_mod, seed as seed_mod
        from tests.pe_world import DEVELOPMENT_ARTIFACT, PARTITION_ARTIFACT
        steps.record_reviewers(base, ["Ann", "Bob"], {"Ann": 50, "Bob": 50}, "owner")
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

    def test_the_manifest_is_incomplete_until_the_prompts_have_versions(self):
        problems = mm.incomplete()
        self.assertTrue(any("prompt 'planner'" in p for p in problems))
        self.assertTrue(any("prompt 'synthesis'" in p for p in problems))
        self.assertFalse([p for p in problems if "model." in p or "inference." in p])           # everything else is already filled in
        with patch.dict(mm.PROMPTS, {"planner": "planner-v1", "synthesis": "synthesis-v1"}):
            self.assertEqual(mm.incomplete(), [])

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
        with self.assertRaisesRegex(steps.StepError, "model manifest must be complete"):
            steps.freeze_planners(base, 1, ENTRYPOINTS)                                              # planners_dir=None: the production path
        self.assertFalse(steps.round_paths(base, 1)["planner_freeze"].exists())

    def test_the_verify_model_command_reports_and_sets_the_exit_code(self):
        with patch.object(mm, "verify", return_value={"ok": True, "checks": [], "observed": {}, "manifest_hash": "h", "manifest_complete": False}):
            self.assertEqual(cli.main(["verify-model"]), 0)
        with patch.object(mm, "verify", return_value={"ok": False, "checks": [], "observed": {}, "manifest_hash": "h", "manifest_complete": False}):
            self.assertEqual(cli.main(["verify-model"]), 2)


if __name__ == "__main__":
    unittest.main()
