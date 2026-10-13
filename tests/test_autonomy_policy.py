"""
Wave 3: the autonomy policy, the recurrence cap, the composed decision and the routes that use it.

Run from the project root:
    python -m unittest tests.test_autonomy_policy -v
"""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import agents.automation as aa
import autonomy_decision
import autonomy_policy
import resolution_outcome as ro
import safety_evidence as ev
from tests.test_execution_authorization import AuthzBase, bearer

REAL_EVALUATE_AUTONOMY = aa.evaluate_autonomy


def outcome(resolved=True, recurred=False, mode="human_approved", tenant="client_a"):
    return {"classification": "cpu_saturation", "tenant_id": tenant, "execution_mode": mode,
            "outcome_status": "resolved" if resolved else "failed", "recurrence_detected": recurred,
            "resolved_at": "2026-10-01T00:00:00Z"}


def gate_for(rows, tenant="client_a"):
    with patch.object(ro, "get_outcomes", return_value=rows):
        return ro.autonomous_gate("cpu_saturation", tenant)


class RecurrenceCapMatrixTests(unittest.TestCase):
    """success rate x recurrence rate -> tier (default policy: 0.85/5, 0.60/3; recurrence 3 / 0.20 / 0.40)."""

    def rows(self, total, resolved, recurred):
        return [outcome(resolved=i < resolved, recurred=i < recurred) for i in range(total)]

    def test_strong_history_without_recurrence_auto_executes(self):
        g = gate_for(self.rows(10, 10, 0))
        self.assertEqual((g["tier"], g["recurrence_cap"], g["recurrence_cap_applied"]), ("auto_execute", "none", False))

    def test_moderate_recurrence_caps_a_strong_history_at_watch(self):
        g = gate_for(self.rows(10, 10, 2))  # 20%
        self.assertEqual((g["base_tier"], g["tier"], g["recurrence_cap"]), ("auto_execute", "auto_execute_watch", "watch"))
        self.assertTrue(g["recurrence_cap_applied"])
        self.assertIn("capped at auto_execute_watch", g["reason"])

    def test_high_recurrence_forces_hold_even_with_perfect_success(self):
        g = gate_for(self.rows(10, 10, 4))  # 40%
        self.assertEqual((g["base_tier"], g["tier"], g["recurrence_cap"]), ("auto_execute", "hold_for_approval", "hold"))

    def test_just_below_the_moderate_threshold_is_not_capped(self):
        g = gate_for(self.rows(10, 10, 1))  # 10%
        self.assertEqual(g["tier"], "auto_execute")

    def test_cap_never_raises_a_weak_tier(self):
        g = gate_for(self.rows(10, 4, 2))  # 40% success -> hold regardless of a watch-level cap
        self.assertEqual(g["tier"], "hold_for_approval")
        self.assertFalse(g["recurrence_cap_applied"])

    def test_watch_history_with_moderate_recurrence_stays_watch(self):
        g = gate_for(self.rows(5, 4, 1))  # 80% success (watch), 20% recurrence (watch cap)
        self.assertEqual((g["base_tier"], g["tier"]), ("auto_execute_watch", "auto_execute_watch"))
        self.assertFalse(g["recurrence_cap_applied"])

    def test_recurrence_below_min_outcomes_is_not_a_signal(self):
        g = gate_for(self.rows(2, 2, 2))
        self.assertEqual(g["recurrence_cap"], "none")
        self.assertEqual(g["tier"], "hold_for_approval")  # too little history for any autonomy

    def test_recurrence_is_not_counted_as_a_failure(self):
        g = gate_for(self.rows(10, 10, 3))
        self.assertEqual(g["success_rate"], 1.0)

    def test_no_history_holds(self):
        g = gate_for([])
        self.assertEqual((g["tier"], g["total_outcomes"]), ("hold_for_approval", 0))

    def test_simulation_runs_never_count(self):
        g = gate_for([outcome(mode="simulation") for _ in range(10)])
        self.assertEqual(g["tier"], "hold_for_approval")

    def test_ledger_failure_fails_closed(self):
        with patch.object(ro, "summarize_outcomes", side_effect=RuntimeError("db down")):
            self.assertEqual(ro.autonomous_gate("cpu_saturation", "client_a")["tier"], "hold_for_approval")


class TenantSeparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.file = self.tmp / "outcomes.json"
        rows = ([outcome(tenant="default") for _ in range(10)]
                + [outcome(tenant="client_b") for _ in range(10)]
                + [outcome(tenant="client_a") for _ in range(2)])
        self.file.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        patcher = patch.object(ro, "_FALLBACK_PATH", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        # PostgreSQL is unavailable here (psycopg2 absent / no server), so reads use the JSON fallback.
        pg = patch.dict("sys.modules", {"postgres_store": None})
        pg.start()
        self.addCleanup(pg.stop)

    def test_other_tenants_and_default_outcomes_do_not_raise_a_tenants_tier(self):
        g = ro.autonomous_gate("cpu_saturation", "client_a")
        self.assertEqual(g["total_outcomes"], 2)
        self.assertEqual(g["tier"], "hold_for_approval")

    def test_a_tenant_with_its_own_history_is_judged_on_it(self):
        g = ro.autonomous_gate("cpu_saturation", "client_b")
        self.assertEqual((g["total_outcomes"], g["tier"]), (10, "auto_execute"))

    def test_default_tenant_sees_only_default_rows(self):
        self.assertEqual(ro.autonomous_gate("cpu_saturation", "default")["total_outcomes"], 10)

    def test_postgres_query_no_longer_pools_default_rows(self):
        source = pathlib.Path("postgres_store.py").read_text(encoding="utf-8")
        start = source.index("def get_outcomes_by_classification")
        self.assertNotIn("tenant_id = 'default'", source[start:start + 700])


class PolicyValidationTests(unittest.TestCase):
    def test_defaults_when_missing_or_malformed(self):
        for raw in (None, {}, [], "x", {"success_tiers": "bad", "recurrence_cap": 5}):
            p = autonomy_policy.resolve_policy(raw)
            self.assertEqual(p["success_tiers"], autonomy_policy.DEFAULT_AUTONOMY_POLICY["success_tiers"])
            self.assertEqual(p["recurrence_cap"], autonomy_policy.DEFAULT_AUTONOMY_POLICY["recurrence_cap"])

    def test_out_of_range_values_fall_back_individually(self):
        p = autonomy_policy.resolve_policy({"success_tiers": {"auto_execute": {"min_success_rate": 1.7, "min_outcomes": 0}}})
        self.assertEqual(p["success_tiers"]["auto_execute"], {"min_success_rate": 0.85, "min_outcomes": 5})

    def test_auto_execute_cannot_be_looser_than_watch(self):
        p = autonomy_policy.resolve_policy({"success_tiers": {
            "auto_execute": {"min_success_rate": 0.5, "min_outcomes": 2},
            "auto_execute_watch": {"min_success_rate": 0.6, "min_outcomes": 3}}})
        self.assertEqual(p["success_tiers"], autonomy_policy.DEFAULT_AUTONOMY_POLICY["success_tiers"])

    def test_recurrence_thresholds_must_be_ordered_and_positive(self):
        for bad in ({"moderate_rate": 0.5, "high_rate": 0.3}, {"moderate_rate": 0.0, "high_rate": 0.4}):
            p = autonomy_policy.resolve_policy({"recurrence_cap": bad})
            self.assertEqual(p["recurrence_cap"], autonomy_policy.DEFAULT_AUTONOMY_POLICY["recurrence_cap"])

    def test_valid_overrides_are_honoured(self):
        p = autonomy_policy.resolve_policy({"recurrence_cap": {"min_outcomes": 4, "moderate_rate": 0.1, "high_rate": 0.3}})
        self.assertEqual(p["recurrence_cap"], {"min_outcomes": 4, "moderate_rate": 0.1, "high_rate": 0.3})

    def test_unreadable_policy_file_uses_defaults(self):
        bad = pathlib.Path(tempfile.mkdtemp()) / "p.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(autonomy_policy.load_policy(bad), autonomy_policy.resolve_policy(None))
        self.assertEqual(autonomy_policy.load_policy(bad.parent / "missing.json"), autonomy_policy.resolve_policy(None))

    def test_custom_policy_changes_the_gate(self):
        strict = autonomy_policy.resolve_policy({"recurrence_cap": {"min_outcomes": 3, "moderate_rate": 0.05, "high_rate": 0.4}})
        rows = [outcome(recurred=(i == 0)) for i in range(10)]  # 10% recurrence
        with patch.object(ro, "get_outcomes", return_value=rows):
            self.assertEqual(ro.autonomous_gate("cpu_saturation", "client_a")["tier"], "auto_execute")
            self.assertEqual(ro.autonomous_gate("cpu_saturation", "client_a", policy=strict)["tier"], "auto_execute_watch")

    def test_shipped_policy_file_is_valid_and_equals_the_defaults(self):
        shipped = json.loads(pathlib.Path("config/autonomy_policy.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(autonomy_policy.resolve_policy(shipped), autonomy_policy.DEFAULT_AUTONOMY_POLICY)


def components(tier="auto_execute", mapping=True, eligible=True, disabled=(), **ledger_extra):
    return dict(
        registry_check=lambda c, a, p: {"allowed": mapping, "status": "ok", "reason": "not enabled"},
        ledger_check=lambda c, t: {"tier": tier, "reason": "ledger", **ledger_extra},
        confidence_check=lambda c, a, p: {"autonomous_eligible": eligible, "eligibility_reasons": ["not eligible"]},
        config_check=lambda: list(disabled),
    )


class ComposerTests(unittest.TestCase):
    def decide(self, purpose="execute", **kw):
        return autonomy_decision.decide("cpu_saturation", "client_a", purpose, **components(**kw))

    def test_everything_green_allows_without_a_human(self):
        d = self.decide()
        self.assertTrue(d.allowed)
        self.assertFalse(d.requires_human_approval)

    def test_any_single_deny_wins(self):
        for kw in ({"tier": "hold_for_approval"}, {"mapping": False}, {"eligible": False}, {"disabled": ["change_window"]}):
            with self.subTest(kw):
                d = self.decide(**kw)
                self.assertFalse(d.allowed)
                self.assertTrue(d.requires_human_approval)

    def test_watch_tier_requires_a_human_in_release_1(self):
        d = self.decide(tier="auto_execute_watch")
        self.assertFalse(d.allowed)
        self.assertTrue(d.requires_human_approval)
        self.assertIn("requires human approval", " ".join(d.reasons))

    def test_recurrence_capped_watch_says_so(self):
        d = self.decide(tier="auto_execute_watch", recurrence_cap_applied=True)
        self.assertIn("capped by recurrence", " ".join(d.reasons))

    def test_enable_purpose_skips_the_registry_requirement(self):
        self.assertTrue(self.decide("enable", mapping=False).allowed)
        self.assertFalse(self.decide("execute", mapping=False).allowed)

    def test_unknown_purpose_is_treated_as_execute(self):
        d = self.decide("bogus", mapping=False)
        self.assertEqual(d.purpose, "execute")
        self.assertFalse(d.allowed)

    def test_each_component_exception_denies(self):
        def boom(*a, **k):
            raise RuntimeError("down")
        for name in ("registry_check", "ledger_check", "confidence_check", "config_check"):
            with self.subTest(name):
                comps = components()
                comps[name] = boom
                d = autonomy_decision.decide("cpu_saturation", "client_a", "execute", **comps)
                self.assertFalse(d.allowed)
                self.assertTrue(d.requires_human_approval)
                self.assertIn("fail closed", " ".join(d.reasons))

    def test_ledger_exception_forces_hold_tier(self):
        def boom(c, t):
            raise RuntimeError("x")
        comps = components()
        comps["ledger_check"] = boom
        self.assertEqual(autonomy_decision.decide("c", "t", "execute", **comps).tier, "hold_for_approval")


class DisabledChecksTests(unittest.TestCase):
    def config(self, **policy):
        cfg = {"checks": {"change_window": {"enabled": False}, "rollback_readiness": {"enabled": True}}}
        if policy:
            cfg["evidence_policy"] = policy
        return cfg

    def test_disabled_checks_are_listed(self):
        self.assertEqual(aa._disabled_checks(self.config()), ["change_window"])

    def test_policy_can_waive_the_requirement(self):
        self.assertEqual(aa._disabled_checks(self.config(autonomous_requires_all_checks_enabled=False)), [])

    def test_enabled_or_malformed_checks_are_not_disabled(self):
        self.assertEqual(aa._disabled_checks({"checks": {"a": {"enabled": True}, "b": "x", "c": {}}}), [])
        self.assertEqual(aa._disabled_checks({}), [])

    def test_pilot_tenants_treat_uploaded_files_as_verified_others_do_not(self):
        pilot = ev.resolve_policy({}, {"deployment_stage": "pilot"})
        strict = ev.resolve_policy({}, {})
        self.assertTrue(pilot["treat_uploaded_files_as_verified"])
        self.assertFalse(strict["treat_uploaded_files_as_verified"])

    def test_shipped_pilot_tenants_declare_the_stage(self):
        for name in ("client_a", "client_b", "poc_client", "tenant_config_template"):
            cfg = json.loads(pathlib.Path("tenant_config/%s.json" % name).read_text(encoding="utf-8-sig"))
            self.assertEqual(cfg.get("deployment_stage"), "pilot", name)


class AutonomyRouteTests(AuthzBase):
    """Enable and eligibility routes use the REAL composer; only its inputs are injected."""

    def setUp(self):
        super().setUp()
        self.registry_calls = []
        self.registry = {"allowed": True, "status": "ok", "reason": "mapping is not enabled"}
        self.reset_state()
        for p in (
            patch.object(aa, "evaluate_autonomy", REAL_EVALUATE_AUTONOMY),
            patch.object(aa, "_autonomy_registry_check", lambda c, a="", p="": dict(self.registry)),
            patch.object(aa, "_ledger_gate_for", lambda c, t: dict(self.ledger)),
            patch.object(aa, "_calculate_automation_confidence",
                         lambda *a, **k: {"autonomous_eligible": self.eligible, "eligibility_reasons": ["not eligible"]}),
            patch.object(aa, "_load_safety_gate_config",
                         lambda: {"checks": {"change_window": {"enabled": not self.disabled}}}),
            patch.object(aa, "set_autonomous_registry_status",
                         lambda **kw: self.registry_calls.append(kw) or {"status": "ok", "registry_entry": {}}),
        ):
            p.start()
            self._patches.append(p)

    def reset_state(self):
        self.ledger = {"tier": "auto_execute", "reason": "ledger"}
        self.eligible, self.disabled = True, []

    def enable(self, enabled=True):
        return self.client.post("/automation/autonomous/enable", headers=bearer("tok-l3"),
                                json={"classification": "cpu_saturation", "tenant_id": "client_a",
                                      "enabled": enabled}).json()

    def eligibility(self):
        r = self.client.post("/automation/autonomous/eligible_mapping", headers=bearer("tok-l3"),
                             json={"classification": "cpu_saturation", "tenant_id": "client_a"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_enable_succeeds_when_every_component_agrees(self):
        self.assertEqual(self.enable()["status"], "ok")
        self.assertEqual(len(self.registry_calls), 1)

    def test_enable_is_blocked_by_each_component_and_never_writes(self):
        cases = {"ledger": lambda: self.ledger.update(tier="hold_for_approval"),
                 "watch": lambda: self.ledger.update(tier="auto_execute_watch"),
                 "confidence": lambda: setattr(self, "eligible", False),
                 "disabled check": lambda: setattr(self, "disabled", ["change_window"])}
        for name, arrange in cases.items():
            with self.subTest(name):
                self.reset_state()
                arrange()
                body = self.enable()
                self.assertEqual(body["status"], "blocked")
                self.assertTrue(body["autonomy_decision"]["reasons"])
                self.assertEqual(self.registry_calls, [])

    def test_disabling_is_always_allowed(self):
        self.ledger["tier"] = "hold_for_approval"
        self.assertEqual(self.enable(enabled=False)["status"], "ok")

    def test_eligibility_reports_the_composed_decision(self):
        body = self.eligibility()
        self.assertTrue(body["allowed"] and body["autonomous_eligible"])
        self.assertFalse(body["requires_human_approval"])

    def test_eligibility_denies_on_a_disabled_check_or_recurrence_capped_watch(self):
        self.disabled = ["change_window"]
        self.assertFalse(self.eligibility()["allowed"])
        self.reset_state()
        self.ledger.update(tier="auto_execute_watch", recurrence_cap_applied=True)
        body = self.eligibility()
        self.assertFalse(body["allowed"])
        self.assertTrue(body["requires_human_approval"])
        self.assertEqual(body["ledger_gate"]["tier"], "auto_execute_watch")

    def test_eligibility_denies_when_the_registry_mapping_is_not_enabled(self):
        self.registry["allowed"] = False
        body = self.eligibility()
        self.assertFalse(body["allowed"])
        self.assertIn("registry", body["reason"])

    def test_a_component_failure_denies_through_the_route(self):
        with patch.object(aa, "_ledger_gate_for", side_effect=RuntimeError("db down")):
            body = self.eligibility()
        self.assertFalse(body["allowed"])
        self.assertIn("fail closed", body["reason"])


if __name__ == "__main__":
    unittest.main()
