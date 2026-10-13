"""
Wave 1b: warning overrides use stable identities (check id, warning code, evidence fingerprint),
are recorded at approval with a reason, and are re-validated against a fresh gate run at execution.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_override_workflow -v
"""
import unittest

import automation_engine as ae
import execution_authorization as exec_auth
from tests.test_execution_authorization import AuthzBase


def check(status="warn", severity="medium", source="blast_radius_estimator_live", summary="text", **extra):
    return {"status": status, "severity": severity, "source_system": source,
            "evidence_type": "rollback", "summary": summary, **extra}


def warning_gate(checks):
    names = sorted(checks)
    return {"gate_status": "warning", "safe_to_approve": True, "safe_to_execute_live": False,
            "blocking_checks": [], "warning_checks": names, "checks": checks}


class WarningIdentityTests(unittest.TestCase):
    def test_identity_ignores_wording_and_timestamps(self):
        a = exec_auth.warning_identities(warning_gate({"rollback_readiness": check(summary="Rollback may be slow",
                                                                                    checked_at="2026-01-01T00:00:00Z")}))
        b = exec_auth.warning_identities(warning_gate({"rollback_readiness": check(summary="Totally different wording",
                                                                                    checked_at="2027-05-05T10:00:00Z")}))
        self.assertEqual(a, b)
        self.assertEqual(a[0]["check_id"], "rollback_readiness")
        self.assertEqual(a[0]["warning_code"], "rollback_readiness:warn")

    def test_identity_changes_when_the_underlying_condition_changes(self):
        base = exec_auth.warning_identities(warning_gate({"rollback_readiness": check()}))[0]
        worse = exec_auth.warning_identities(warning_gate({"rollback_readiness": check(severity="high")}))[0]
        other_source = exec_auth.warning_identities(warning_gate({"rollback_readiness": check(source="client_api")}))[0]
        self.assertNotEqual(base["evidence_fingerprint"], worse["evidence_fingerprint"])
        self.assertNotEqual(base["evidence_fingerprint"], other_source["evidence_fingerprint"])

    def test_stale_evidence_has_its_own_warning_code(self):
        w = exec_auth.warning_identities(warning_gate({"infrastructure_drift": check(ttl_warning="Evidence is 90h old")}))[0]
        self.assertEqual(w["warning_code"], "infrastructure_drift:stale")

    def test_falls_back_to_warning_check_names_when_details_are_absent(self):
        w = exec_auth.warning_identities({"gate_status": "warning", "warning_checks": ["a", "b"]})
        self.assertEqual([x["check_id"] for x in w], ["a", "b"])

    def test_compare_distinguishes_new_warning_from_changed_evidence(self):
        ack = exec_auth.warning_identities(warning_gate({"rollback_readiness": check()}))
        same = exec_auth.warning_identities(warning_gate({"rollback_readiness": check(summary="reworded")}))
        changed = exec_auth.warning_identities(warning_gate({"rollback_readiness": check(severity="critical")}))
        added = exec_auth.warning_identities(warning_gate({"rollback_readiness": check(),
                                                           "error_budget_slo": check()}))
        self.assertEqual(exec_auth.compare_warnings(ack, same), [])
        self.assertIn("evidence for warning", exec_auth.compare_warnings(ack, changed)[0])
        self.assertIn("new warning 'error_budget_slo:warn'", exec_auth.compare_warnings(ack, added)[0])


class OverrideWorkflowTests(AuthzBase):
    REASON = "Accepted by on-call: rollback verified manually"

    def approved_with_warning(self, checks=None):
        checks = checks or {"rollback_readiness": check()}
        self.gate = warning_gate(checks)
        q = self.queue()
        self.run_gate(q["queue_id"])
        r = self.approve(q["queue_id"], override_gate_warnings=True, override_reason=self.REASON)
        self.assertEqual(r.status_code, 200, r.text)
        return q["queue_id"]

    def test_approval_records_override_reason_identity_and_warning_identities(self):
        qid = self.approved_with_warning()
        approval = ae.get_queue_entry(qid)["approval"]
        self.assertTrue(approval["override_requested"])
        self.assertEqual(approval["override_reason"], self.REASON)
        self.assertEqual(approval["approved_by"], "cara-l3")  # authenticated approver, not the request body
        self.assertTrue(approval["gate_run_id"])
        self.assertEqual(approval["plan_hash"], ae.compute_plan_hash(ae.get_queue_entry(qid)))
        self.assertEqual(approval["target_host"], "linux-lab-01")
        ack = approval["acknowledged_warnings"][0]
        self.assertEqual((ack["check_id"], ack["warning_code"]), ("rollback_readiness", "rollback_readiness:warn"))
        self.assertTrue(ack["evidence_fingerprint"])

    def test_approval_without_override_or_reason_is_refused_with_the_warnings_listed(self):
        self.gate = warning_gate({"rollback_readiness": check()})
        q = self.queue()
        self.run_gate(q["queue_id"])
        r = self.approve(q["queue_id"])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["status"], "override_required")
        self.assertEqual(r.json()["detail"]["warnings"][0]["check_id"], "rollback_readiness")

    def test_override_is_not_recorded_when_the_gate_passed(self):
        q = self.queue()
        self.run_gate(q["queue_id"])
        self.assertEqual(self.approve(q["queue_id"], override_gate_warnings=True, override_reason="x").status_code, 200)
        approval = ae.get_queue_entry(q["queue_id"])["approval"]
        self.assertFalse(approval["override_requested"])
        self.assertEqual(approval["acknowledged_warnings"], [])

    def test_same_warning_with_reworded_text_still_executes_with_reconfirmation(self):
        qid = self.approved_with_warning()
        self.gate = warning_gate({"rollback_readiness": check(summary="Completely reworded warning")})
        r = self.execute(qid, "live", override_gate_warnings=True, override_reason="Reconfirmed")
        self.assertEqual(r.status_code, 200, r.text)
        auth = r.json()["authorization"]
        self.assertTrue(auth["override"])
        self.assertEqual(auth["approval_override_reason"], self.REASON)
        self.assertEqual(auth["acknowledged_warnings"][0]["check_id"], "rollback_readiness")

    def test_new_warning_since_approval_is_rejected(self):
        qid = self.approved_with_warning()
        self.gate = warning_gate({"rollback_readiness": check(), "error_budget_slo": check()})
        r = self.execute(qid, "live", override_gate_warnings=True, override_reason="Reconfirmed")
        self.assertEqual(r.status_code, 403)
        self.assertIn("new warning 'error_budget_slo:warn'", r.text)
        self.assertEqual(self.connector_calls, [])

    def test_materially_changed_evidence_is_rejected(self):
        qid = self.approved_with_warning()
        self.gate = warning_gate({"rollback_readiness": check(severity="critical")})
        r = self.execute(qid, "live", override_gate_warnings=True, override_reason="Reconfirmed")
        self.assertEqual(r.status_code, 403)
        self.assertIn("changed since approval", r.text)
        self.assertEqual(self.connector_calls, [])

    def test_execution_needs_reconfirmation_even_when_warnings_match(self):
        qid = self.approved_with_warning()
        self.assertEqual(self.execute(qid, "live").status_code, 403)
        self.assertEqual(self.execute(qid, "live", override_gate_warnings=True).status_code, 403)
        self.assertEqual(self.connector_calls, [])

    def test_gate_passing_at_execution_needs_no_override(self):
        qid = self.approved_with_warning()
        self.gate = {"gate_status": "passed", "safe_to_approve": True, "safe_to_execute_live": True,
                     "blocking_checks": [], "warning_checks": [], "checks": {}}
        self.assertEqual(self.execute(qid, "live").status_code, 200)

    def test_warning_at_execution_when_approval_recorded_no_override_is_rejected(self):
        q = self.queue()
        self.run_gate(q["queue_id"])  # gate passed
        self.assertEqual(self.approve(q["queue_id"]).status_code, 200)
        self.gate = warning_gate({"rollback_readiness": check()})
        r = self.execute(q["queue_id"], "live", override_gate_warnings=True, override_reason="please")
        self.assertEqual(r.status_code, 403)
        self.assertIn("approval does not record a warning override", r.text)

    def test_blocked_at_execution_is_never_overridable(self):
        qid = self.approved_with_warning()
        self.gate = {**warning_gate({"rollback_readiness": check(status="fail")}), "gate_status": "blocked",
                     "blocking_checks": ["rollback_readiness"]}
        r = self.execute(qid, "live", override_gate_warnings=True, override_reason="please")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.connector_calls, [])

    def test_execute_gate_rerun_uses_the_approved_target_not_the_latest_check(self):
        qid = self.approved_with_warning()
        # Someone runs a later check against a different target.
        self.client.post("/automation/pre_execution_check", headers={"Authorization": "Bearer tok-l2"},
                         json={"queue_id": qid, "classification": "cpu_saturation", "playbook": "x.yml",
                               "target_host": "some-other-host", "execution_mode": "live"})
        seen = {}
        original = self.gate

        def spy(req):
            seen["target"] = req.target_host
            return dict(original)

        from unittest.mock import patch
        import agents.automation as aa
        with patch.object(aa, "_build_pre_execution_safety_gate", spy):
            self.execute(qid, "live", override_gate_warnings=True, override_reason="Reconfirmed")
        self.assertEqual(seen["target"], "linux-lab-01")

    def test_autonomous_entries_can_never_carry_an_override(self):
        q = self.queue("tok-svc")
        self.assertEqual((q.get("intent") or {}).get("kind"), "autonomous")
        self.gate = warning_gate({"rollback_readiness": check()})
        r = self.execute(q["queue_id"], "live", token="tok-svc", override_gate_warnings=True, override_reason="x")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.connector_calls, [])

    def test_target_change_between_approval_and_execution_is_rejected(self):
        qid = self.approved_with_warning()
        entry = ae.get_queue_entry(qid)
        approved_run = exec_auth.latest_gate_run(qid)
        moved = {**approved_run, "target_host": "different-host", "gate_run_id": "gr_x"}
        auth = exec_auth.authorize_execution(
            entry, "live", _Human(), run_gate=lambda e: moved,
            autonomy_decision=lambda e: None,  # human-approved entry: the autonomy decision is not consulted
            override=True, override_reason="Reconfirmed")
        self.assertFalse(auth.allowed)
        self.assertIn("target changed", " ".join(auth.reasons))


class _Human:
    id, kind, role = "cara-l3", "human", "l3"

    def has_role(self, minimum):
        return True

    def allows_tenant(self, tenant):
        return True


if __name__ == "__main__":
    unittest.main()
