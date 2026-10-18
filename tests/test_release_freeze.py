"""The Release 2 configuration freeze: the shipped planner, runtime, tools and budgets are exactly what was recorded (design section 21)."""
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agentruntime import release
from agentruntime.contracts import Budgets


class FrozenCoreTests(unittest.TestCase):
    def test_the_code_on_disk_matches_the_release_freeze(self):
        self.assertEqual(release.verify(), [], "frozen code changed: if deliberate, run agentruntime.release.refreeze(reason, approved_by) and say why")

    def test_the_record_names_the_shipped_planner_and_the_off_default(self):
        record = release.load()
        self.assertEqual((record["planner"], record["default_mode"], record["model_id"], record["prompt_version"]), ("rules_only", "off", None, "none"))
        self.assertEqual(record["planner_entry"], "agentruntime.planners.rules_only:RulesOnlyPlanner")
        self.assertEqual(record["budgets"], Budgets().model_dump())
        self.assertTrue(record["approved_by"] and record["reason"])

    def test_the_frozen_files_are_the_runtime_the_tools_and_only_the_shipped_planner(self):
        names = set(release.load()["files"])
        self.assertIn("agentruntime/runner.py", names)
        self.assertIn("agentruntime/planners/rules_only.py", names)
        self.assertEqual({n for n in names if n.startswith("agentruntime/tools/")}, {f"agentruntime/tools/{n}" for n in
                                                                                   ("__init__.py", "_common.py", "evidence.py", "kb_search.py", "outcomes.py", "patterns.py", "ticket_history.py")})
        for experimental in ("rules_then_model", "model", "model_support", "prompts", "model_manifest"):
            self.assertNotIn(f"agentruntime/planners/{experimental}.py", names)              # experimental code does not move the freeze
        self.assertFalse([n for n in names if "integration" in n])                           # the R2-2 wrapper is outside the frozen core

    def test_a_changed_file_a_changed_budget_and_a_changed_constant_are_each_detected(self):
        record = release.load()
        changed = json.loads(json.dumps(record))
        changed["files"]["agentruntime/runner.py"] = "0" * 64
        self.assertEqual(release.differences(changed), ["agentruntime/runner.py: changed"])
        changed = json.loads(json.dumps(record))
        changed["files"]["agentruntime/extra.py"] = "1" * 64
        self.assertIn("agentruntime/extra.py: removed", release.differences(changed))        # recorded but gone
        changed = json.loads(json.dumps(record))
        del changed["files"]["agentruntime/verifier.py"]
        self.assertIn("agentruntime/verifier.py: added", release.differences(changed))        # on disk but not recorded
        changed = json.loads(json.dumps(record))
        changed["budgets"]["deadline_s"] = 99
        self.assertIn("budgets differs from the record", release.differences(changed))
        changed = json.loads(json.dumps(record))
        changed["rule_constants"]["MIN_RETRIEVAL_SCORE"] = 0.1
        self.assertIn("rule_constants differs from the record", release.differences(changed))
        changed = json.loads(json.dumps(record))
        changed["default_mode"] = "on"
        self.assertIn("default_mode differs from the record", release.differences(changed))


class PolicyTests(unittest.TestCase):
    def test_the_policy_defaults_to_off_with_the_frozen_planner_and_all_five_tools(self):
        policy = release.policy_for()
        self.assertEqual((policy.mode, policy.planner), ("off", "rules_only"))
        self.assertEqual(sorted(policy.allowed_tools), ["evidence_lookup", "incident_kb_search", "outcome_lookup", "pattern_lookup", "ticket_history_lookup"])

    def test_the_experimental_planners_cannot_be_deployed(self):
        for name in ("rules_then_model", "model", "anything_else"):
            with self.assertRaisesRegex(release.ReleaseError, "not approved for deployment"):
                release.policy_for(planner=name)

    def test_the_planner_factory_builds_the_frozen_planner(self):
        planner = release.planner_factory({"case_id": "x"})
        self.assertEqual((planner.ruleset_version, planner.model_id, planner.prompt_version), ("rules-1", None, "none"))


class RefreezeTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "freeze.json"

    def test_the_first_freeze_needs_a_reason_and_an_approver_and_is_written_once(self):
        with self.assertRaises(release.ReleaseError):
            release.write_first("", "owner", self.path)
        with self.assertRaises(release.ReleaseError):
            release.write_first("because", "  ", self.path)
        record = release.write_first("because", "owner", self.path)
        self.assertEqual(record["history"], [])
        with self.assertRaisesRegex(release.ReleaseError, "already exists"):
            release.write_first("again", "owner", self.path)
        self.assertEqual(release.verify(self.path), [])

    def test_a_deliberate_change_is_refrozen_with_the_old_record_kept(self):
        release.write_first("first", "owner", self.path)
        with self.assertRaisesRegex(release.ReleaseError, "nothing changed"):
            release.refreeze("no change", "owner", self.path)
        with patch.object(release, "CORE_FILES", release.CORE_FILES[:-1]):                    # the code set changes: verifier.py leaves the frozen core
            self.assertTrue(release.verify(self.path))
            with self.assertRaises(release.ReleaseError):
                release.refreeze("", "owner", self.path)
            record = release.refreeze("verifier moved", "owner", self.path)
            self.assertEqual(release.verify(self.path), [])
        (entry,) = record["history"]
        self.assertEqual((entry["reason"], entry["approved_by"], entry["superseded_because"], entry["superseded_by"]), ("first", "owner", "verifier moved", "owner"))
        self.assertIn("agentruntime/verifier.py: removed", entry["changes"])
        self.assertEqual(len(entry["record_sha256"]), 64)

    def test_an_unreadable_record_is_an_error_not_a_pass(self):
        with self.assertRaises(release.ReleaseError):
            release.verify(self.dir / "missing.json")
        (self.dir / "bad.json").write_text("{", encoding="utf-8")
        with self.assertRaises(release.ReleaseError):
            release.verify(self.dir / "bad.json")


if __name__ == "__main__":
    unittest.main()
