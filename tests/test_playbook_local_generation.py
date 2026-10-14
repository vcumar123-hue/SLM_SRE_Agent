"""
A classification with no stored playbook must still get step-by-step guidance: when the external provider is not configured or not
permitted, the local model writes it (keeping ticket text on the machine). Before, the agent only knew Groq, so tickets such as
"SAP login failure after a password policy change" showed no playbook at all.

Run from the project root:  python -m unittest tests.test_playbook_local_generation -v
"""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import agents.playbook_agent as pa
import settings_portable as settings

STEPS = [
    {"step": 1, "title": "Confirm the policy change", "context": "Scope the failure", "action": "Check the policy change record",
     "commands": ["SU01"], "decision": [["policy changed", "go to step 2"]]},
    {"step": 2, "title": "Reset the affected users", "context": "Restore access", "action": "Reset passwords", "commands": ["SU01"],
     "decision": []},
]


def _denied():
    from egress_policy import EgressDenied
    return EgressDenied(MagicMock(reason="tenant has no external LLM policy"))


def reply(content):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return response


class Base(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"GROQ_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)
        for p in (
            patch.object(pa, "get_cached_playbook", return_value=None),
            patch.object(pa, "set_cached_playbook"),
            patch.object(settings, "MODEL_BASE_URL", "http://localhost:1234/v1"),
            patch.object(settings, "PRIMARY_CHAT_MODEL", "google/gemma-3-4b"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.cache_write = pa.set_cached_playbook


class LocalGenerationTests(Base):
    def test_local_model_writes_the_playbook_when_no_external_key_exists(self):
        with patch("requests.post", return_value=reply(json.dumps(STEPS))) as post:
            steps = pa.generate_playbook("authentication_or_authorization_failure", ticket="users cannot log in")
        self.assertEqual([s["title"] for s in steps], ["Confirm the policy change", "Reset the affected users"])
        self.assertTrue(all(s["generated_by"] == "google/gemma-3-4b" for s in steps))
        self.assertIn("localhost:1234", post.call_args.args[0])
        self.cache_write.assert_called_once()

    def test_a_request_that_forbids_external_models_still_gets_the_local_one(self):
        with patch("requests.post", return_value=reply(json.dumps(STEPS))):
            steps = pa.generate_playbook("x", ticket="t", allow_llm=False)
        self.assertEqual(len(steps), 2)

    def test_the_external_provider_is_tried_first_when_allowed(self):
        calls = []

        def fake_post(url, **kw):
            calls.append(url)
            return reply(json.dumps(STEPS))

        with patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
                patch("egress_policy.require_external_llm", return_value=MagicMock(redaction="none")), \
                patch("requests.post", side_effect=fake_post):
            pa.generate_playbook("x", ticket="t", allow_llm=True)
        self.assertIn("groq.com", calls[0])

    def test_a_denied_external_call_falls_back_to_the_local_model(self):
        calls = []

        def fake_post(url, **kw):
            calls.append(url)
            return reply(json.dumps(STEPS))

        with patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
                patch("egress_policy.require_external_llm", side_effect=_denied()), \
                patch("requests.post", side_effect=fake_post):
            steps = pa.generate_playbook("x", ticket="t", allow_llm=True)
        self.assertEqual(len(steps), 2)
        self.assertTrue(all("groq.com" not in c for c in calls))

    def test_markdown_fenced_json_is_accepted(self):
        with patch("requests.post", return_value=reply("```json\n" + json.dumps(STEPS) + "\n```")):
            self.assertEqual(len(pa.generate_playbook("x", ticket="t")), 2)

    def test_garbage_or_a_failed_call_returns_no_steps_and_caches_nothing(self):
        with patch("requests.post", return_value=reply("I cannot help with that")):
            self.assertEqual(pa.generate_playbook("x", ticket="t"), [])
        with patch("requests.post", side_effect=RuntimeError("LM Studio is down")):
            self.assertEqual(pa.generate_playbook("x", ticket="t"), [])
        self.cache_write.assert_not_called()

    def test_steps_missing_required_fields_are_dropped(self):
        steps = STEPS + [{"step": 3, "title": "No action here"}]
        with patch("requests.post", return_value=reply(json.dumps(steps))):
            self.assertEqual(len(pa.generate_playbook("x", ticket="t")), 2)

    def test_a_cached_playbook_is_returned_without_any_model_call(self):
        with patch.object(pa, "get_cached_playbook", return_value=STEPS), patch("requests.post") as post:
            self.assertEqual(pa.generate_playbook("x", ticket="t"), STEPS)
        post.assert_not_called()


class RemoteEndpointTests(Base):
    def test_a_non_local_model_endpoint_is_subject_to_the_egress_policy(self):
        with patch.object(settings, "MODEL_BASE_URL", "https://models.example.com/v1"), \
                patch("egress_policy.require_external_llm", side_effect=_denied()), \
                patch("requests.post") as post:
            self.assertEqual(pa.generate_playbook("x", ticket="t"), [])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
