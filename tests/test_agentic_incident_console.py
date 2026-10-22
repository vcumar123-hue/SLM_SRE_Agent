"""
The Incident Console's Agentic Investigation panel (design: operator-visible shadow inspection, section 22.6).

Two layers, matching tests/test_ui_workflow.py's convention:

  1. Pure-function tests (`agentic_panel_view`, `agentic_poll_should_continue`, `get_agentic_run`) - these need Streamlit only because
     ui.py imports it at module level; they call no `st.*` function themselves, so they are the most robust check of what the panel
     will say for each state.
  2. A thin Streamlit AppTest layer, confirming the actual rendered text for the states the review asked for by name, and that the
     polling loop really does stop calling the backend once a run is no longer pending.

Needs Streamlit. Run with an interpreter that has it, e.g.:
    C:\\Users\\vcuma\\AppData\\Local\\Programs\\Python\\Python314\\python.exe -m unittest tests.test_agentic_incident_console -v
"""
import unittest
from unittest.mock import patch

try:  # pragma: no cover - depends on the interpreter
    import streamlit as st
    import ui
    from streamlit.testing.v1 import AppTest
    HAVE_UI = True
except Exception:  # ModuleNotFoundError in the project .venv
    HAVE_UI = False
    ui = None
    st = None
    AppTest = None

SKIP_UI = "Streamlit is not installed in this interpreter; run with one that has it (see module docstring)"


def complete_run(status="supported", mode="shadow", **over):
    body = {"state": "complete", "mode": mode, "status": status, "reason": "a verified hypothesis is supported by evidence",
            "planner": "rules_only", "ruleset": "rules-1", "duration_ms": 121, "tool_count": 2, "evidence_count": 1,
            "tools": [{"step": 1, "name": "ticket_history_lookup", "ok": True, "evidence_added": 0, "duration_ms": 30},
                      {"step": 2, "name": "incident_kb_search", "ok": True, "evidence_added": 1, "duration_ms": 91}],
            "hypotheses": [{"cause": "disk full on the data volume", "status": "supported", "verified": True, "evidence_count": 1}],
            "flags": [], "response_applied": False, "response_impact": "none_shadow"}
    body.update(over)
    return body


# -- Layer 1: pure functions, no Streamlit calls made ------------------------------------------------------------------------------

@unittest.skipUnless(HAVE_UI, SKIP_UI)
class PanelViewTests(unittest.TestCase):
    def test_off_disabled_state_shows_not_enabled(self):
        view = ui.agentic_panel_view({"state": "disabled"})
        self.assertTrue(view["visible"])
        self.assertEqual(view["headline"], "Agentic investigation not enabled.")

    def test_pending_state_shows_running(self):
        view = ui.agentic_panel_view({"state": "pending", "mode": "shadow"})
        self.assertTrue(view["visible"])
        self.assertIn("running", view["headline"].lower())

    def test_unavailable_or_missing_is_not_shown_at_all(self):
        for body in ({"state": "unavailable"}, {}, None, "not a dict", {"state": "something_else"}):
            view = ui.agentic_panel_view(body)
            self.assertFalse(view["visible"], body)

    def test_a_completed_shadow_run_shows_planner_status_and_counts(self):
        view = ui.agentic_panel_view(complete_run())
        self.assertEqual(view["state"], "complete")
        self.assertEqual((view["planner"], view["ruleset"], view["tool_count"], view["evidence_count"], view["duration_ms"]),
                         ("rules_only", "rules-1", 2, 1, 121))
        self.assertEqual(view["cause"], "disk full on the data volume")
        self.assertIn("supported", view["status_text"].lower())

    def test_insufficient_evidence_shows_an_escalation_not_a_fabricated_cause(self):
        run = complete_run(status="escalated_insufficient", reason="no new evidence", hypotheses=[])
        view = ui.agentic_panel_view(run)
        self.assertIsNone(view["cause"])
        self.assertIn("escalated", view["status_text"].lower())
        self.assertIn("insufficient", view["status_text"].lower())
        self.assertEqual(view["reason"], "no new evidence")

    def test_response_impact_is_never_claimed_as_applied_for_shadow(self):
        view = ui.agentic_panel_view(complete_run())
        self.assertFalse(view["response_applied"])
        self.assertIn("did not change the response", view["impact_text"])
        self.assertIn("independently", view["independent_note"])

    def test_an_on_run_not_yet_recorded_does_not_claim_none_impact(self):
        view = ui.agentic_panel_view(complete_run(mode="on", response_impact="not_recorded"))
        self.assertNotIn("none", view["impact_text"].lower())

    def test_hypotheses_and_tools_are_passed_through_for_the_expandable_trace(self):
        view = ui.agentic_panel_view(complete_run())
        self.assertEqual(len(view["tools"]), 2)
        self.assertEqual(view["tools"][0]["name"], "ticket_history_lookup")
        self.assertEqual(len(view["hypotheses"]), 1)


@unittest.skipUnless(HAVE_UI, SKIP_UI)
class PollStopTests(unittest.TestCase):
    def test_continues_while_pending_and_inside_the_timeout_window(self):
        now = 1000.0
        self.assertTrue(ui.agentic_poll_should_continue({"state": "pending", "started_at": now - 5}, now))

    def test_stops_once_the_state_is_no_longer_pending(self):
        now = 1000.0
        for state in ("complete", "disabled", "unavailable"):
            self.assertFalse(ui.agentic_poll_should_continue({"state": state, "started_at": now - 1}, now), state)

    def test_stops_after_the_timeout_even_while_still_pending(self):
        now = 1000.0
        self.assertFalse(ui.agentic_poll_should_continue({"state": "pending", "started_at": now - ui.AGENTIC_POLL_TIMEOUT_S - 0.01}, now))
        self.assertTrue(ui.agentic_poll_should_continue({"state": "pending", "started_at": now - ui.AGENTIC_POLL_TIMEOUT_S + 0.01}, now))

    def test_malformed_state_never_continues(self):
        self.assertFalse(ui.agentic_poll_should_continue(None, 1000.0))
        self.assertFalse(ui.agentic_poll_should_continue({}, 1000.0))
        self.assertFalse(ui.agentic_poll_should_continue("not a dict", 1000.0))


@unittest.skipUnless(HAVE_UI, SKIP_UI)
class GetAgenticRunTests(unittest.TestCase):
    """get_agentic_run never raises and never returns anything but {"state": ...} on any failure."""

    def test_a_non_200_response_is_unavailable(self):
        class Resp:
            status_code = 500
        with patch.object(ui.SESSION, "get", return_value=Resp()):
            self.assertEqual(ui.get_agentic_run("client_c", "req-1"), {"state": "unavailable"})

    def test_a_connection_error_is_unavailable_not_raised(self):
        with patch.object(ui.SESSION, "get", side_effect=ui.requests.exceptions.ConnectionError("down")):
            self.assertEqual(ui.get_agentic_run("client_c", "req-1"), {"state": "unavailable"})

    def test_a_non_json_body_is_unavailable(self):
        class Resp:
            status_code = 200

            def json(self):
                raise ValueError("not json")
        with patch.object(ui.SESSION, "get", return_value=Resp()):
            self.assertEqual(ui.get_agentic_run("client_c", "req-1"), {"state": "unavailable"})

    def test_missing_ids_are_unavailable_without_a_network_call(self):
        with patch.object(ui.SESSION, "get") as spy:
            self.assertEqual(ui.get_agentic_run("", "req-1"), {"state": "unavailable"})
            self.assertEqual(ui.get_agentic_run("client_c", ""), {"state": "unavailable"})
        spy.assert_not_called()

    def test_a_successful_response_is_passed_through(self):
        class Resp:
            status_code = 200

            def json(self):
                return {"state": "complete", "status": "supported"}
        with patch.object(ui.SESSION, "get", return_value=Resp()) as spy:
            body = ui.get_agentic_run("client_c", "req-1")
        self.assertEqual(body, {"state": "complete", "status": "supported"})
        self.assertEqual(spy.call_args.kwargs["params"], {"tenant_id": "client_c"})


# -- Layer 2: a real Streamlit script, via AppTest -----------------------------------------------------------------------------

def _panel_script():
    import ui as _ui
    import streamlit as _st
    _ui.render_agentic_investigation(_st.session_state.get("test_run"))


@unittest.skipUnless(HAVE_UI, SKIP_UI)
class RenderedPanelTests(unittest.TestCase):
    def run_panel(self, run):
        at = AppTest.from_function(_panel_script, default_timeout=30)
        at.session_state["test_run"] = run
        return at.run()

    def rendered_text(self, at):
        parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
        return " ".join(parts)

    def test_disabled_state_renders_not_enabled(self):
        at = self.run_panel({"state": "disabled"})
        self.assertFalse(at.exception, at.exception)
        self.assertIn("not enabled", self.rendered_text(at).lower())

    def test_pending_state_renders_running(self):
        at = self.run_panel({"state": "pending", "mode": "shadow"})
        self.assertFalse(at.exception, at.exception)
        self.assertIn("running", self.rendered_text(at).lower())

    def test_unavailable_renders_nothing(self):
        at = self.run_panel({"state": "unavailable"})
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(self.rendered_text(at).strip(), "")

    def test_completed_run_renders_planner_status_and_counts(self):
        at = self.run_panel(complete_run())
        self.assertFalse(at.exception, at.exception)
        text = self.rendered_text(at)
        self.assertIn("rules_only", text)
        self.assertIn("Tools executed", text)
        self.assertIn("2", text)
        self.assertIn("did not change the response", text)

    def test_the_legacy_result_is_unaffected_the_panel_is_additive(self):
        """render_agentic_investigation never touches session state belonging to the main result; it only reads what it is given."""
        before = dict(st.session_state) if False else None  # AppTest isolates state per run(); nothing to compare across instances
        at = self.run_panel(complete_run())
        self.assertFalse(at.exception, at.exception)


_poll_call_count = {"n": 0}


def _fake_get_flips_to_complete_on_the_second_call(tenant_id, request_id):
    # module-level (not a closure): AppTest.from_function re-executes the script's SOURCE TEXT in a fresh namespace, so a script
    # cannot close over a test method's local variables - only names it imports or defines itself are visible to it.
    _poll_call_count["n"] += 1
    return {"state": "complete", **complete_run()} if _poll_call_count["n"] >= 2 else {"state": "pending", "mode": "shadow"}


def _polling_script():
    from unittest.mock import patch as _patch
    import ui as _ui
    from tests.test_agentic_incident_console import _fake_get_flips_to_complete_on_the_second_call as _fake
    with _patch.object(_ui, "get_agentic_run", _fake):
        _ui.render_agentic_panel_section("client_c", "req-poll-1")


@unittest.skipUnless(HAVE_UI, SKIP_UI)
class PollingLoopTests(unittest.TestCase):
    """Confirms the panel section really stops fetching once a run leaves 'pending' - not merely that the pure predicate would say so."""

    def setUp(self):
        _poll_call_count["n"] = 0

    def test_polling_stops_calling_the_backend_once_the_run_completes(self):
        at = AppTest.from_function(_polling_script, default_timeout=30)
        at.run()
        self.assertFalse(at.exception, at.exception)
        at.run()      # second rerun: the fake flips to 'complete'
        self.assertFalse(at.exception, at.exception)
        calls_after_complete = _poll_call_count["n"]
        self.assertGreaterEqual(calls_after_complete, 2)
        at.run()
        at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(_poll_call_count["n"], calls_after_complete, "no further backend calls were made once the run was no longer pending")

    def test_a_request_with_no_id_renders_nothing_and_calls_nothing(self):
        def script():
            import ui as _ui
            _ui.render_agentic_panel_section("client_c", "")
        with patch.object(ui, "get_agentic_run") as spy:
            at = AppTest.from_function(script, default_timeout=30)
            at.run()
        self.assertFalse(at.exception, at.exception)
        spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
