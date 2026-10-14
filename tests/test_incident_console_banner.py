"""
The Incident Console must tell one story about automation: the top banner and the panel below it use the same
approved-automation lookup (they used to disagree, showing "validation required" above "approved automation available").

Run from the project root (needs Streamlit):  python -m unittest tests.test_incident_console_banner -v
"""
import importlib.util
import unittest
from unittest.mock import patch

HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None


@unittest.skipUnless(HAS_STREAMLIT, "Streamlit is not installed in this interpreter")
class ActionBannerTests(unittest.TestCase):
    def render(self, result, **kwargs):
        from page_modules import shared
        captured = []
        with patch.object(shared.st, "markdown", side_effect=lambda body, **kw: captured.append(str(body))):
            shared.render_incident_action_banner(result, **kwargs)
        return " ".join(captured)

    RESULT = {"analysis": {"classification": "vpn_client_failure"}}
    MAPPING = {"action": "recover_vpn_connectivity", "playbook": "vpn_recover_client_connectivity.yml", "target_host": "vpn-lab-01"}

    def test_registry_mapping_drives_the_approved_banner(self):
        text = self.render(self.RESULT, approved_mapping=self.MAPPING)
        self.assertIn("APPROVED AUTOMATION AVAILABLE", text)
        self.assertIn("vpn_recover_client_connectivity.yml", text)
        self.assertNotIn("RUNBOOK VALIDATION REQUIRED", text)

    def test_no_mapping_asks_for_runbook_validation(self):
        text = self.render(self.RESULT, approved_mapping=None)
        self.assertIn("RUNBOOK VALIDATION REQUIRED", text)
        self.assertNotIn("APPROVED AUTOMATION AVAILABLE", text)

    def test_the_mapping_wins_over_an_empty_result_list(self):
        text = self.render({**self.RESULT, "approved_automations": []}, approved_mapping=self.MAPPING)
        self.assertIn("APPROVED AUTOMATION AVAILABLE", text)

    def test_falls_back_to_the_analysis_result_when_no_mapping_is_given(self):
        text = self.render({**self.RESULT, "approved_automations": [self.MAPPING]})
        self.assertIn("APPROVED AUTOMATION AVAILABLE", text)

    def test_the_promote_block_is_skipped_when_approved_automation_exists(self):
        source = open("ui.py", encoding="utf-8").read()
        marker = "    if not has_approved_automation:\n        promote_key = "
        self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
