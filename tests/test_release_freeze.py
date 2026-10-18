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
        integration = {n for n in names if n.startswith("agentruntime/integration/")}           # the R2-2 wrapper is inside the freeze (review 2026-10-17)
        self.assertEqual(integration, {f"agentruntime/integration/{n}" for n in ("__init__.py", "engagement.py", "gate.py", "hook.py", "reconcile.py", "shadow.py")})

    def test_the_route_hooks_the_pipeline_call_site_and_the_confidence_input_are_fingerprinted(self):
        prints = release.load()["source_fingerprints"]
        for key in ("agents/analyze.py::analyze", "agents/analyze.py::_loop_scope", "agents/analyze.py::_loop_after", "agents/analyze.py::_loop_background",
                    "agents/analyze.py::_loop_response", "app.py::_r2_before_fallback", "confidence_engine.py::apply_evidence_grounding",
                    "confidence_engine.py::compute_confidence_score", "app.py::lines-with::_r2_", "agents/analyze.py::lines-with::_loop_",
                    "confidence_engine.py::lines-with::EVIDENCE_GROUNDING_WEIGHT"):
            self.assertIn(key, prints)
            self.assertNotEqual(prints[key], "MISSING", key)

    def test_a_changed_hook_function_or_call_site_is_detected_but_unrelated_edits_are_not(self):
        record = release.load()
        for key in ("agents/analyze.py::analyze", "app.py::lines-with::_r2_", "confidence_engine.py::compute_confidence_score"):
            changed = json.loads(json.dumps(record))
            changed["source_fingerprints"][key] = "0" * 64
            self.assertEqual(release.differences(changed), [f"{key}: changed"])
        text = "x = 1\n\ndef unrelated():\n    return 1\n"
        self.assertEqual(release._norm_text(text.encode()), text)                          # fingerprints read source text, so other functions in the file are not part of them

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


BASE = """
def _analyze_core_legacy(request):
    a = 1
    if cond:
        x = 2
    else:
        _r2_loop = _r2_before_fallback(request)
        if _r2_loop is not None and _r2_loop.replaces_fallback:
            reason = "replaced"
        elif request.use_fallback:
            out = maybe_run_fallback(ticket=t, force=f)
        _post = finalize_post_rca_state(s)
    return a
"""


class FallbackPlacementTests(unittest.TestCase):
    """Moving the investigation relative to the one-shot fallback, or changing what guards the fallback, must change the fingerprint."""

    def fp(self, text):
        return release.fallback_placement(text)

    def test_the_real_pipeline_is_fingerprinted(self):
        record = release.load()["source_fingerprints"]
        self.assertIn("app.py::_analyze_core_legacy::fallback-placement", record)
        self.assertNotEqual(record["app.py::_analyze_core_legacy::fallback-placement"], "MISSING")

    def test_unrelated_edits_do_not_change_it(self):
        base = self.fp(BASE)
        self.assertEqual(self.fp(BASE.replace("    a = 1\n", "    a = 1  # a comment\n")), base)
        self.assertEqual(self.fp(BASE + "\n\ndef another():\n    return maybe_run_fallback()\n"), base)          # other functions are not part of it
        self.assertEqual(self.fp(BASE.replace("return a", "return a + 0")), base)

    def test_every_way_of_moving_or_re_guarding_the_fallback_is_detected(self):
        base = self.fp(BASE)
        variants = {
            "the fallback moved before the investigation": BASE.replace("        _r2_loop = _r2_before_fallback(request)\n", "        out0 = maybe_run_fallback(ticket=t, force=f)\n        _r2_loop = _r2_before_fallback(request)\n"),
            "the elif became an if (the fallback runs even when replaced)": BASE.replace("        elif request.use_fallback:", "        if request.use_fallback:"),
            "the guard no longer reads the investigation": BASE.replace("_r2_loop is not None and _r2_loop.replaces_fallback", "False"),
            "the fallback call changed": BASE.replace("maybe_run_fallback(ticket=t, force=f)", "maybe_run_fallback(ticket=t, force=True)"),
            "the fallback is called twice": BASE.replace("            out = maybe_run_fallback(ticket=t, force=f)\n", "            out = maybe_run_fallback(ticket=t, force=f)\n            out2 = maybe_run_fallback(ticket=t)\n"),
            "a statement was inserted between the hook and the branch": BASE.replace("        if _r2_loop is not None", "        extra = 1\n        if _r2_loop is not None"),
            "the hook moved out of its block": BASE.replace("        _r2_loop = _r2_before_fallback(request)\n", "").replace("    a = 1\n", "    a = 1\n    _r2_loop = _r2_before_fallback(request)\n"),
            "the finalisation moved ahead of the hook": BASE.replace("        _post = finalize_post_rca_state(s)\n", "").replace("        _r2_loop = _r2_before_fallback", "        _post = finalize_post_rca_state(s)\n        _r2_loop = _r2_before_fallback"),
        }
        for label, text in variants.items():
            self.assertNotEqual(self.fp(text), base, label)
        self.assertEqual(len({self.fp(t) for t in variants.values()}), len(variants))                       # and each differently

    def test_a_missing_function_or_hook_is_reported_as_missing(self):
        self.assertEqual(self.fp("def other():\n    pass\n"), "MISSING")
        self.assertEqual(self.fp(BASE.replace("_r2_loop", "_loop2")), "MISSING")


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.path = self.dir / "freeze.json"
        release.write_first("first", "(not yet named)", self.path)

    def test_a_new_freeze_is_unconfirmed_and_the_loop_must_stay_off(self):
        record = release.load(self.path)
        self.assertIs(record["approval_confirmed"], False)
        self.assertEqual(release.verify(self.path), [])                                     # the code matches...
        self.assertTrue(any("not confirmed" in p for p in release.problems(self.path)))     # ...but the approval is not there yet

    def test_confirming_needs_a_name_and_a_statement_and_is_recorded(self):
        with self.assertRaises(release.ReleaseError):
            release.confirm_approval("", "I approve the Release 2 owner amendment in section 21", self.path)
        with self.assertRaises(release.ReleaseError):
            release.confirm_approval("Ramanthan", "ok", self.path)
        record = release.confirm_approval("Ramanthan", "I approve the Release 2 owner amendment in section 21.", self.path)
        self.assertEqual((record["approved_by"], record["approval_confirmed"]), ("Ramanthan", True))
        self.assertEqual(record["approval_history"][0]["statement"], "I approve the Release 2 owner amendment in section 21.")
        self.assertEqual(release.problems(self.path), [])

    def test_a_refreeze_withdraws_the_confirmation_and_keeps_the_approval_history(self):
        release.confirm_approval("Ramanthan", "I approve the Release 2 owner amendment in section 21.", self.path)
        with patch.object(release, "CORE_FILES", release.CORE_FILES[:-1]):
            record = release.refreeze("verifier moved", "(not yet named)", self.path)
        self.assertIs(record["approval_confirmed"], False)
        self.assertEqual(len(record["approval_history"]), 1)

    def test_the_integration_gate_keeps_the_loop_off_until_the_approval_is_confirmed(self):
        from agentruntime.integration import gate
        gate.reset_cache()
        try:
            with patch.object(release, "FREEZE_PATH", self.path):
                self.assertTrue(any("not confirmed" in p for p in gate.freeze_problems(force=True)))
                release.confirm_approval("Ramanthan", "I approve the Release 2 owner amendment in section 21.", self.path)
                self.assertEqual(gate.freeze_problems(force=True), [])
        finally:
            gate.reset_cache()


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
