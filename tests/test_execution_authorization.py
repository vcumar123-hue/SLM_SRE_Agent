"""
Release 1 (W1/W2): direct API calls must not bypass approval, the Safety Gate, autonomy policy or role checks.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_execution_authorization -v

State is isolated in a temp directory; connectors are stubbed so no playbook ever runs.
"""
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import agents.automation as aa
import app as app_module
import automation_engine as ae
import execution_authorization as exec_auth
import security

PRINCIPALS = {
    "tok-l1": {"id": "ann-l1", "role": "l1", "kind": "human"},
    "tok-l2": {"id": "bob-l2", "role": "l2", "kind": "human"},
    "tok-l3": {"id": "cara-l3", "role": "l3", "kind": "human"},
    "tok-l3b": {"id": "dan-l3", "role": "l3", "kind": "human"},
    "tok-admin": {"id": "eve-admin", "role": "admin", "kind": "human"},
    "tok-svc": {"id": "telemetry-svc", "role": "l2", "kind": "service"},
    "tok-tenant": {"id": "fay-l3-other", "role": "l3", "kind": "human", "tenants": ["some_other_tenant"]},
}


def bearer(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


class AuthzBase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._patches = []

        def start(p):
            p.start()
            self._patches.append(p)

        for mod in (ae, aa):
            start(patch.object(mod, "AUTOMATION_QUEUE_FILE", self.tmp / "queue.json"))
            start(patch.object(mod, "AUTOMATION_LOG_FILE", self.tmp / "log.json"))
        start(patch.object(ae, "AUTOMATION_QUEUE_ARCHIVE_FILE", self.tmp / "archive.json"))
        start(patch.object(exec_auth, "GATE_RUNS_FILE", self.tmp / "gate_runs.json"))

        self.connector_calls = []
        self.connector_delay = 0.0
        lock = threading.Lock()

        def fake(name):
            def run(**kwargs):
                if self.connector_delay:
                    time.sleep(self.connector_delay)
                with lock:
                    self.connector_calls.append(name)
                return {"ok": True}
            return run

        start(patch.object(ae, "_import_connectors", lambda: {
            "run_ansible_playbook": fake("ansible"),
            "run_kubernetes_action": fake("kubernetes"),
            "run_ssh_command": fake("ssh"),
            "run_powershell_command": fake("powershell"),
        }))
        start(patch.object(aa, "postgres_write_safe", lambda *a, **k: None))

        self.gate = {"gate_status": "passed", "safe_to_approve": True, "safe_to_execute_live": True,
                     "blocking_checks": [], "warning_checks": []}
        start(patch.object(aa, "_build_pre_execution_safety_gate", lambda req: dict(self.gate)))
        self.ledger_tier = "auto_execute"
        self.mapping_allowed = True
        start(patch.object(aa, "_ledger_gate_for", lambda cls, tenant: {"tier": self.ledger_tier}))
        start(patch.object(aa, "_autonomous_mapping_allowed", lambda cls: self.mapping_allowed))

        self._env = patch.dict(os.environ, {"AIOPS_PRINCIPALS": json.dumps(PRINCIPALS), "AIOPS_API_KEY": ""})
        self._env.start()
        security.reset_principals_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._env.stop()
        security.reset_principals_cache()

    # helpers
    def queue(self, token="tok-l2", classification="cpu_saturation"):
        r = self.client.post("/automation/queue", headers=bearer(token),
                             json={"incident_id": "inc-1", "classification": classification})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["queue_entry"]

    def run_gate(self, queue_id, token="tok-l2"):
        r = self.client.post("/automation/pre_execution_check", headers=bearer(token),
                             json={"queue_id": queue_id, "classification": "cpu_saturation",
                                   "playbook": "linux_restart_nginx.yml", "target_host": "linux-lab-01",
                                   "execution_mode": "live"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def approve(self, queue_id, token="tok-l3"):
        return self.client.post("/automation/approve", headers=bearer(token),
                                json={"queue_id": queue_id, "approved_by": "spoofed-name"})

    def execute(self, queue_id, mode="live", token="tok-l3", **extra):
        return self.client.post("/automation/execute", headers=bearer(token),
                                json={"queue_id": queue_id, "execution_mode": mode, **extra})

    def approved_entry(self):
        q = self.queue()
        self.run_gate(q["queue_id"])
        self.assertEqual(self.approve(q["queue_id"]).status_code, 200)
        return q["queue_id"]

    def step_count(self, queue_id):
        return len((ae.get_queue_entry(queue_id) or {}).get("plan", {}).get("steps", []))

    def queue_entry(self, queue_id):
        return ae.get_queue_entry(queue_id) or ae._find_queue_entry(
            ae._safe_read_json(ae.AUTOMATION_QUEUE_ARCHIVE_FILE, []), queue_id)


class LiveExecutionBypassTests(AuthzBase):
    def test_live_execute_of_unapproved_queued_plan_is_denied(self):
        q = self.queue()
        r = self.execute(q["queue_id"], "live")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("approved", json.dumps(r.json()))
        self.assertEqual(self.connector_calls, [])

    def test_autonomous_requested_flag_carries_no_authority(self):
        q = self.queue()
        r = self.execute(q["queue_id"], "live", autonomous_requested=True)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.connector_calls, [])

    def test_unknown_execution_mode_is_rejected(self):
        q = self.queue()
        r = self.execute(q["queue_id"], "dry-ish")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.connector_calls, [])

    def test_engine_refuses_live_without_authorization(self):
        q = self.queue()
        res = ae.execute_automation(q["queue_id"], executed_by="script", execution_mode="live")
        self.assertEqual(res["status"], "not_authorized")
        self.assertEqual(self.connector_calls, [])

    def test_engine_refuses_authorization_for_a_different_queue_entry(self):
        a = self.queue()["queue_id"]
        b = self.queue()["queue_id"]
        forged = {"allowed": True, "queue_id": a, "mode": "live",
                  "plan_hash": ae.compute_plan_hash(ae.get_queue_entry(a))}
        res = ae.execute_automation(b, executed_by="script", execution_mode="live", authorization=forged)
        self.assertEqual(res["status"], "not_authorized")
        self.assertEqual(self.connector_calls, [])

    def test_gate_blocked_at_execute_time_denies_live(self):
        qid = self.approved_entry()
        self.gate = {**self.gate, "gate_status": "blocked", "safe_to_execute_live": False, "blocking_checks": ["x"]}
        r = self.execute(qid, "live")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.connector_calls, [])

    def test_gate_evaluation_failure_denies_live(self):
        qid = self.approved_entry()

        def boom(req):
            raise RuntimeError("evidence backend down")

        with patch.object(aa, "_build_pre_execution_safety_gate", boom):
            r = self.execute(qid, "live")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.connector_calls, [])

    def test_plan_swap_after_approval_is_denied(self):
        qid = self.approved_entry()
        queue = ae._safe_read_json(ae.AUTOMATION_QUEUE_FILE, [])
        entry = ae._find_queue_entry(queue, qid)
        entry["plan"]["steps"][0]["playbook"] = "something_else.yml"
        ae._safe_write_json(ae.AUTOMATION_QUEUE_FILE, queue)
        r = self.execute(qid, "live")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("plan changed", json.dumps(r.json()))
        self.assertEqual(self.connector_calls, [])

    def test_refresh_after_approval_invalidates_changed_plan(self):
        qid = self.approved_entry()
        queue = ae._safe_read_json(ae.AUTOMATION_QUEUE_FILE, [])
        ae._find_queue_entry(queue, qid)["classification"] = "disk_full"  # refresh will rebuild a different plan
        ae._safe_write_json(ae.AUTOMATION_QUEUE_FILE, queue)
        r = self.client.post("/automation/refresh_queue", headers=bearer("tok-l2"), json={"queue_id": qid})
        self.assertEqual(r.status_code, 200, r.text)
        entry = ae.get_queue_entry(qid)
        self.assertEqual(entry["status"], "queued")
        self.assertNotIn("approval", entry)
        self.assertEqual(self.execute(qid, "live").status_code, 403)

    def test_closed_entry_cannot_be_reapproved_or_replayed(self):
        qid = self.approved_entry()
        self.assertEqual(self.execute(qid, "live").status_code, 200)
        calls = len(self.connector_calls)
        self.assertEqual(self.approve(qid).status_code, 409)
        self.assertEqual(self.execute(qid, "live").status_code, 403)
        self.assertEqual(len(self.connector_calls), calls)


class ApprovalTests(AuthzBase):
    def test_approve_requires_a_gate_run(self):
        q = self.queue()
        r = self.approve(q["queue_id"])
        self.assertEqual(r.status_code, 409, r.text)

    def test_approve_refused_when_gate_blocked(self):
        q = self.queue()
        self.gate = {**self.gate, "gate_status": "blocked", "safe_to_approve": False, "blocking_checks": ["x"]}
        self.run_gate(q["queue_id"])
        self.assertEqual(self.approve(q["queue_id"]).status_code, 409)

    def test_approval_identity_comes_from_the_credential_not_the_body(self):
        qid = self.approved_entry()
        entry = ae.get_queue_entry(qid)
        self.assertEqual(entry["approval"]["approved_by"], "cara-l3")
        self.assertNotEqual(entry["approval"]["approved_by"], "spoofed-name")
        self.assertEqual(entry["approval"]["plan_hash"], ae.compute_plan_hash(entry))

    def test_approval_after_plan_change_requires_a_fresh_gate_run(self):
        q = self.queue()
        self.run_gate(q["queue_id"])
        queue = ae._safe_read_json(ae.AUTOMATION_QUEUE_FILE, [])
        ae._find_queue_entry(queue, q["queue_id"])["plan"]["steps"][0]["playbook"] = "changed.yml"
        ae._safe_write_json(ae.AUTOMATION_QUEUE_FILE, queue)
        self.assertEqual(self.approve(q["queue_id"]).status_code, 409)

    def test_l2_cannot_approve_and_service_cannot_approve(self):
        q = self.queue()
        self.run_gate(q["queue_id"])
        self.assertEqual(self.approve(q["queue_id"], "tok-l2").status_code, 403)
        self.assertEqual(self.approve(q["queue_id"], "tok-svc").status_code, 403)

    def test_principal_restricted_to_other_tenant_cannot_approve(self):
        q = self.queue()
        self.run_gate(q["queue_id"])
        self.assertEqual(self.approve(q["queue_id"], "tok-tenant").status_code, 403)


class HumanLiveFlowTests(AuthzBase):
    def test_full_human_flow_executes_live_and_records_authorization(self):
        qid = self.approved_entry()
        steps = self.step_count(qid)
        self.assertGreaterEqual(steps, 1)
        r = self.execute(qid, "live")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["execution"]["status"], "completed_live")
        self.assertEqual(body["execution"]["executed_by"], "cara-l3")
        self.assertEqual(body["authorization"]["execution_class"], "human_approved")
        self.assertTrue(body["authorization"]["gate_run_id"])
        self.assertEqual(len(self.connector_calls), steps)

    def test_simulation_of_queued_plan_is_allowed_and_runs_nothing_live(self):
        q = self.queue()
        r = self.execute(q["queue_id"], "simulation", token="tok-l1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["execution"]["status"], "completed_simulation")
        self.assertEqual(self.connector_calls, [])

    def test_live_requires_l3_even_for_an_approved_entry(self):
        qid = self.approved_entry()
        self.assertEqual(self.execute(qid, "live", token="tok-l2").status_code, 403)
        self.assertEqual(self.connector_calls, [])

    def test_gate_warning_needs_explicit_override_with_reason(self):
        q = self.queue()
        self.gate = {**self.gate, "gate_status": "warning", "warning_checks": ["rollback_readiness"]}
        self.run_gate(q["queue_id"])
        self.assertEqual(self.approve(q["queue_id"]).status_code, 200)
        self.assertEqual(self.execute(q["queue_id"], "live").status_code, 403)
        self.assertEqual(self.execute(q["queue_id"], "live", override_gate_warnings=True).status_code, 403)
        r = self.execute(q["queue_id"], "live", override_gate_warnings=True, override_reason="Accepted by on-call")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["authorization"]["override"])

    def test_concurrent_live_executions_run_the_plan_only_once(self):
        qid = self.approved_entry()
        steps = self.step_count(qid)
        self.connector_delay = 0.25
        results = []

        def go():
            c = TestClient(app_module.app)
            results.append(c.post("/automation/execute", headers=bearer("tok-l3"),
                                  json={"queue_id": qid, "execution_mode": "live"}).status_code)

        threads = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results)[0], 200, results)
        self.assertEqual(len([c for c in results if c == 200]), 1, results)
        self.assertEqual(len(self.connector_calls), steps, self.connector_calls)  # exactly one run


class AutonomousFlowTests(AuthzBase):
    def autonomous_entry(self):
        q = self.queue("tok-svc")
        self.assertEqual((q.get("intent") or {}).get("kind"), "autonomous", q)
        return q["queue_id"]

    def test_service_identity_with_mapping_and_ledger_can_execute_autonomously(self):
        qid = self.autonomous_entry()
        steps = self.step_count(qid)
        r = self.execute(qid, "live", token="tok-svc")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["authorization"]["execution_class"], "autonomous")
        self.assertEqual(len(self.connector_calls), steps)

    def test_human_cannot_execute_an_autonomous_entry(self):
        qid = self.autonomous_entry()
        self.assertEqual(self.execute(qid, "live", token="tok-admin").status_code, 403)
        self.assertEqual(self.connector_calls, [])

    def test_service_cannot_execute_a_human_entry(self):
        qid = self.approved_entry()
        self.assertEqual(self.execute(qid, "live", token="tok-svc").status_code, 403)
        self.assertEqual(self.connector_calls, [])

    def test_no_autonomous_intent_without_registry_mapping(self):
        self.mapping_allowed = False
        q = self.queue("tok-svc")
        self.assertNotIn("intent", q)
        self.assertEqual(self.execute(q["queue_id"], "live", token="tok-svc").status_code, 403)

    def test_watch_tier_requires_a_human(self):
        qid = self.autonomous_entry()
        self.ledger_tier = "auto_execute_watch"
        r = self.execute(qid, "live", token="tok-svc")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.connector_calls, [])

    def test_hold_tier_blocks_and_gate_warning_blocks_autonomous(self):
        qid = self.autonomous_entry()
        self.ledger_tier = "hold_for_approval"
        self.assertEqual(self.execute(qid, "live", token="tok-svc").status_code, 403)
        self.ledger_tier = "auto_execute"
        self.gate = {**self.gate, "gate_status": "warning"}
        self.assertEqual(self.execute(qid, "live", token="tok-svc", override_gate_warnings=True,
                                      override_reason="x").status_code, 403)
        self.assertEqual(self.connector_calls, [])

    def test_human_queue_cannot_create_an_autonomous_intent(self):
        q = self.queue("tok-l3")
        self.assertNotIn("intent", q)


class AuthenticationTests(AuthzBase):
    def test_missing_or_wrong_token_is_401(self):
        self.assertEqual(self.client.get("/automation/queue/list").status_code, 401)
        self.assertEqual(self.client.get("/automation/queue/list", headers=bearer("nope")).status_code, 401)

    def test_x_user_role_header_is_not_trusted(self):
        q = self.queue()
        self.run_gate(q["queue_id"])
        r = self.client.post("/automation/approve", headers=bearer("tok-l2", **{"X-User-Role": "admin"}),
                             json={"queue_id": q["queue_id"]})
        self.assertEqual(r.status_code, 403, r.text)

    def test_role_table_covers_execution_critical_routes(self):
        self.assertEqual(self.client.post("/automation/queue", headers=bearer("tok-l1"),
                                          json={"incident_id": "i", "classification": "cpu_saturation"}).status_code, 403)
        self.assertEqual(self.client.post("/automation/rollback", headers=bearer("tok-l2"),
                                          params={"execution_id": "ax_x"}).status_code, 403)
        self.assertEqual(self.client.post("/automation/autonomous/enable", headers=bearer("tok-l2"),
                                          json={"classification": "cpu_saturation"}).status_code, 403)

    def test_health_is_exempt(self):
        self.assertNotEqual(self.client.get("/health").status_code, 401)


class SecurityModuleTests(unittest.TestCase):
    def test_load_principals_parses_and_validates(self):
        env = {"AIOPS_PRINCIPALS": json.dumps({
            "a": {"id": "x", "role": "l3", "kind": "human"},
            "b": {"id": "y", "role": "superuser"},
            "c": {"id": "z", "role": "l2", "kind": "robot"},
        })}
        p = security.load_principals(env)
        self.assertEqual(list(p), ["a"])

    def test_legacy_api_key_maps_to_l2_human(self):
        p = security.load_principals({"AIOPS_API_KEY": "legacy"})
        self.assertEqual((p["legacy"].id, p["legacy"].role, p["legacy"].kind), ("shared-key", "l2", "human"))

    def test_production_without_principals_refuses_to_start(self):
        with patch.dict(os.environ, {"AIOPS_ENV": "production", "AIOPS_PRINCIPALS": "", "AIOPS_API_KEY": ""}):
            security.reset_principals_cache()
            try:
                self.assertEqual(security.auth_mode(), "misconfigured")
                with self.assertRaises(RuntimeError):
                    security.install_security(object())
            finally:
                security.reset_principals_cache()

    def test_dev_mode_is_open_with_a_dev_principal(self):
        with patch.dict(os.environ, {"AIOPS_ENV": "", "AIOPS_PRINCIPALS": "", "AIOPS_API_KEY": ""}):
            security.reset_principals_cache()
            try:
                self.assertEqual(security.auth_mode(), "dev")
                self.assertEqual(security.authenticate_headers({}).id, "dev-open")
            finally:
                security.reset_principals_cache()


if __name__ == "__main__":
    unittest.main()
