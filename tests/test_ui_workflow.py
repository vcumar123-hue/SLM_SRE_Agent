"""
Wave 1b: the UI uses the hardened queue / gate / approval / execution contracts.

Two separate layers (server-side tests remain authoritative):

  1. API workflow tests - queue -> gate -> approval/override -> SIMULATION execution, with every live
     connector disabled. They run in any interpreter that can run the backend.
  2. UI-layer tests - the real helper functions in ui.py and the demo page (Streamlit AppTest) drive the
     real FastAPI app through an in-process adapter. They need Streamlit, which the project .venv does not
     contain, so they SKIP there. Run them with an interpreter that has Streamlit, e.g.:

         C:\\Users\\vcuma\\AppData\\Local\\Programs\\Python\\Python314\\python.exe -m unittest tests.test_ui_workflow -v

No test here can execute a live connector.
"""
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import automation_engine as ae
from tests.test_execution_authorization import AuthzBase, bearer

try:  # pragma: no cover - depends on the interpreter
    import streamlit  # noqa: F401
    import ui
    from streamlit.testing.v1 import AppTest
    HAVE_UI = True
except Exception:  # ModuleNotFoundError in the project .venv
    HAVE_UI = False
    ui = None
    AppTest = None

SKIP_UI = "Streamlit is not installed in this interpreter; run with one that has it (see module docstring)"


class NoLiveExecution(AuthzBase):
    """Base that makes any attempt to run a live step an immediate test failure."""

    def setUp(self):
        super().setUp()
        guard = patch.object(ae, "_execute_step_live", side_effect=AssertionError("live execution attempted"))
        guard.start()
        self.addCleanup(guard.stop)


# -- Layer 1: API workflow (authoritative) --------------------------------------------------------------

class DemoApiWorkflowTests(NoLiveExecution):
    def demo_gate_payload(self, qid):
        # exactly what the rewritten demo and the queue page send
        return {"queue_id": qid, "incident_id": "demo-incident", "classification": "cpu_saturation",
                "target_host": "linux-lab-02", "risk_tier": "medium", "execution_mode": "simulation",
                "requested_by": "demo"}

    def test_queue_gate_execute_in_simulation_runs_end_to_end(self):
        q = self.client.post("/automation/queue", headers=bearer("tok-l3"),
                             json={"incident_id": "demo-incident", "classification": "cpu_saturation",
                                   "requested_by": "demo"})
        self.assertEqual(q.status_code, 200, q.text)
        qid = q.json()["queue_entry"]["queue_id"]
        g = self.client.post("/automation/pre_execution_check", headers=bearer("tok-l3"),
                             json=self.demo_gate_payload(qid))
        self.assertEqual(g.status_code, 200, g.text)
        self.assertTrue(g.json()["gate_run_id"])
        e = self.client.post("/automation/execute", headers=bearer("tok-l3"),
                             json={"queue_id": qid, "execution_mode": "simulation", "executed_by": "demo"})
        self.assertEqual(e.status_code, 200, e.text)
        self.assertEqual(e.json()["execution"]["status"], "completed_simulation")
        self.assertEqual(e.json()["authorization"]["execution_class"], "simulation")
        self.assertEqual(self.connector_calls, [])

    def test_obsolete_demo_payload_is_rejected(self):
        # The old demo posted {incident_id, ci_name, approved}; the contract has always required queue_id.
        r = self.client.post("/automation/execute", headers=bearer("tok-l3"),
                             json={"incident_id": "demo-incident", "ci_name": "linux-lab-02", "approved": True})
        self.assertEqual(r.status_code, 422)

    def test_live_after_the_demo_flow_still_needs_approval(self):
        q = self.queue("tok-l3")
        self.run_gate(q["queue_id"], "tok-l3")
        self.assertEqual(self.execute(q["queue_id"], "live").status_code, 403)
        self.assertEqual(self.connector_calls, [])


# -- Layer 2a: the real ui.py helpers against the real app ---------------------------------------------

class InProcessSession:
    """requests.Session look-alike that sends ui.py's HTTP calls to the FastAPI TestClient."""

    def __init__(self, client, token=None):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.calls = []

    def request(self, method, url, **kw):
        kw.pop("timeout", None)
        self.calls.append((method.upper(), urlparse(url).path, kw.get("json")))
        return self.client.request(method, urlparse(url).path, headers=self.headers,
                                   json=kw.get("json"), params=kw.get("params"))

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


@unittest.skipUnless(HAVE_UI, SKIP_UI)
class UiHelperTests(AuthzBase):
    def setUp(self):
        super().setUp()
        self.session = InProcessSession(self.client, "tok-l3")
        p = patch.object(ui, "SESSION", self.session)
        p.start()
        self.addCleanup(p.stop)

    def queue_via_ui(self):
        return ui.queue_automation({"incident_id": "inc-ui", "classification": "cpu_saturation",
                                    "requested_by": "Vijay"})["queue_entry"]["queue_id"]

    def gate_via_ui(self, qid, **extra):
        return ui.run_pre_execution_safety_gate({
            "queue_id": qid, "incident_id": "inc-ui", "classification": "cpu_saturation",
            "playbook": "linux_restart_nginx.yml", "target_host": "linux-lab-01", "risk_tier": "medium",
            "execution_mode": "live", "requested_by": "Vijay", **extra})

    def test_queue_gate_approve_execute_through_the_ui_helpers(self):
        qid = self.queue_via_ui()
        self.assertTrue(self.gate_via_ui(qid)["gate_run_id"])
        approved = ui.approve_automation_request(qid, approved_by="Vijay")
        self.assertEqual(approved["queue_entry"]["approval"]["approved_by"], "cara-l3")
        result = ui.execute_automation_request(qid, execution_mode="live", executed_by="Vijay")
        self.assertEqual(result["execution"]["status"], "completed_live")
        self.assertEqual(len(self.connector_calls), 1)  # the stub connector, never a real one

    def test_simulation_helper_runs_a_queued_plan(self):
        qid = self.queue_via_ui()
        with patch.object(ae, "_execute_step_live", side_effect=AssertionError("live execution attempted")):
            result = ui.execute_automation_request(qid, execution_mode="simulation")
        self.assertEqual(result["execution"]["status"], "completed_simulation")
        self.assertEqual(self.connector_calls, [])

    def test_live_refusal_reason_is_surfaced_to_the_user(self):
        qid = self.queue_via_ui()
        with self.assertRaises(RuntimeError) as ctx:
            ui.execute_automation_request(qid, execution_mode="live")
        message = str(ctx.exception)
        self.assertIn("refused by the server (403)", message)
        self.assertIn("approved", message)

    def test_warning_gate_approval_refusal_then_override_flow(self):
        self.gate = {"gate_status": "warning", "safe_to_approve": True, "safe_to_execute_live": False,
                     "blocking_checks": [], "warning_checks": ["rollback_readiness"],
                     "checks": {"rollback_readiness": {"status": "warn", "severity": "medium",
                                                        "source_system": "blast_radius_estimator_live",
                                                        "evidence_type": "rollback"}}}
        qid = self.queue_via_ui()
        self.gate_via_ui(qid)
        with self.assertRaises(RuntimeError) as ctx:
            ui.approve_automation_request(qid, approved_by="Vijay")
        self.assertIn("Approval refused by the server (409)", str(ctx.exception))
        self.assertIn("override", str(ctx.exception))
        ui.approve_automation_request(qid, approved_by="Vijay", override_gate_warnings=True,
                                      override_reason="Accepted by on-call")
        # without reconfirmation the server still refuses
        with self.assertRaises(RuntimeError):
            ui.execute_automation_request(qid, execution_mode="live")
        result = ui.execute_automation_request(qid, execution_mode="live", override_gate_warnings=True,
                                               override_reason="Accepted by on-call")
        self.assertEqual(result["execution"]["status"], "completed_live")

    def test_override_fields_are_sent_only_when_requested(self):
        qid = self.queue_via_ui()
        ui.execute_automation_request(qid, execution_mode="simulation")
        _, path, body = self.session.calls[-1]
        self.assertEqual(path, "/automation/execute")
        self.assertNotIn("override_gate_warnings", body)
        self.assertNotIn("autonomous_requested", body)
        self.assertEqual(body["queue_id"], qid)

    def test_authentication_failure_is_visible(self):
        ui.SESSION.headers = {"Authorization": "Bearer wrong"}
        with self.assertRaises(Exception) as ctx:
            ui.queue_automation({"incident_id": "i", "classification": "cpu_saturation"})
        self.assertIn("401", str(ctx.exception))

    def test_queue_busy_503_is_surfaced(self):
        qid = self.queue_via_ui()
        busy = ae.QueueBusyError("approve", qid, 0.5, "another process holds the lock")
        self.gate_via_ui(qid)
        with patch.object(ae, "_queue_lock", side_effect=busy):
            with self.assertRaises(RuntimeError) as ctx:
                ui.approve_automation_request(qid, approved_by="Vijay")
        self.assertIn("(503)", str(ctx.exception))


# -- Layer 2b: the demo page, driven by Streamlit AppTest ------------------------------------------------

def _demo_page_script():
    import ui as _ui
    _ui._render_demo_page()


@unittest.skipUnless(HAVE_UI, SKIP_UI)
class DemoPageAppTests(NoLiveExecution):
    def setUp(self):
        super().setUp()
        self.session = InProcessSession(self.client, "tok-l3")
        p = patch.object(ui, "SESSION", self.session)
        p.start()
        self.addCleanup(p.stop)

    def run_step(self, path, step_index):
        at = AppTest.from_function(_demo_page_script, default_timeout=60)
        at.session_state["demo_path"] = path
        at.session_state["demo_step"] = step_index
        at.session_state["demo_results"] = {}
        at.session_state["demo_incident_id"] = None
        at.session_state["demo_snapshot_id"] = None
        at.session_state["demo_queue_id"] = None
        at.session_state["demo_gate_status"] = None
        return at.run()

    def assert_step(self, at, title):
        cards = " ".join(m.value for m in at.markdown)
        self.assertIn(title, cards, f"expected the demo to be on the '{title}' step")

    def click_run(self, at, step_index):
        buttons = [b for b in at.button if b.key == f"run_{step_index}"]
        self.assertTrue(buttons, f"no run button for step {step_index}")
        buttons[0].click()
        return at.run()

    def test_safety_gate_step_creates_a_queue_entry_and_a_gate_run(self):
        at = self.run_step("A", 5)
        self.assertFalse(at.exception, at.exception)
        self.assert_step(at, "Safety Gate")
        at = self.click_run(at, 5)
        self.assertFalse(at.exception, at.exception)
        result = at.session_state["demo_results"][5]
        self.assertTrue(result["ok"], result)
        self.assertIn("Safety gate: PASSED", result["summary"])
        qid = at.session_state["demo_queue_id"]
        self.assertTrue(qid)
        self.assertIn(("POST", "/automation/pre_execution_check"), [(m, p) for m, p, _ in self.session.calls])
        gate_body = [b for m, p, b in self.session.calls if p == "/automation/pre_execution_check"][0]
        self.assertEqual(gate_body["queue_id"], qid)
        self.assertEqual(gate_body["execution_mode"], "simulation")

    def test_execute_step_sends_a_valid_queue_payload_and_runs_in_simulation(self):
        at = self.run_step("A", 9)
        self.assert_step(at, "Ansible Executes")
        at = self.click_run(at, 9)
        self.assertFalse(at.exception, at.exception)
        result = at.session_state["demo_results"][9]
        self.assertTrue(result["ok"], result)
        self.assertIn("SIMULATION", result["summary"])
        body = [b for m, p, b in self.session.calls if p == "/automation/execute"][0]
        self.assertEqual(set(body), {"queue_id", "execution_mode", "executed_by"})
        self.assertEqual(body["execution_mode"], "simulation")
        self.assertEqual(self.connector_calls, [])

    def test_path_b_execute_step_also_runs_in_simulation(self):
        at = self.run_step("B", 9)
        self.assert_step(at, "Ansible Executes")
        at = self.click_run(at, 9)
        self.assertFalse(at.exception, at.exception)
        result = at.session_state["demo_results"][9]
        self.assertTrue(result["ok"], result)
        self.assertIn("SIMULATION", result["summary"])

    def test_server_refusal_is_displayed_not_hidden(self):
        at = self.run_step("A", 9)
        qid = at.session_state["demo_queue_id"]
        self.assertIsNone(qid)
        with patch.object(ae, "get_queue_entry", return_value=None):
            at = self.click_run(at, 9)
        result = at.session_state["demo_results"][9]
        self.assertFalse(result["ok"])
        self.assertIn("refused by the server", result["summary"])


if __name__ == "__main__":
    unittest.main()
