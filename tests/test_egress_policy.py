"""
Release 1 (W5): tenant policy governs every external LLM call (deny by default).

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_egress_policy -v

requests.post is replaced so a denied call is proven to make no network request.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import agents.playbook_agent as playbook_agent
import app as app_module
import egress_policy
import groq_fallback_real
from agents.models import OrchestratorState, RCAOutput, RetrievalOutput, TriageOutput
from agents.orchestrator import OrchestratorAgent
from models import AnalyzeRequest

GROQ_URL = "https://api.groq.com/openai/v1"

TENANTS = {
    "open_tenant": {"external_llm_policy": {"allow_external_llm": True, "allowed_llm_providers": ["groq"],
                                            "external_llm_redaction": "basic"}},
    "wrong_provider": {"external_llm_policy": {"allow_external_llm": True, "allowed_llm_providers": ["openai"]}},
    "explicit_off": {"external_llm_policy": {"allow_external_llm": False, "allowed_llm_providers": ["groq"]}},
    "no_policy": {},
}


def loader(tenant_id):
    return TENANTS.get(tenant_id, {})


class EgressBase(unittest.TestCase):
    def setUp(self):
        self.audit = pathlib.Path(tempfile.mkdtemp()) / "egress_audit.jsonl"
        self._patches = [
            patch.object(egress_policy, "AUDIT_FILE", self.audit),
            patch.object(egress_policy, "_load_tenant_config", loader),
            patch.dict(os.environ, {"GROQ_API_KEY": "test-key", "GROQ_BASE_URL": GROQ_URL}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def audit_rows(self):
        return [json.loads(l) for l in self.audit.read_text(encoding="utf-8").splitlines()] if self.audit.exists() else []


class PolicyDecisionTests(EgressBase):
    def test_absent_policy_is_denied_and_audited(self):
        d = egress_policy.evaluate_external_llm("no_policy", "rca_fallback", GROQ_URL)
        self.assertFalse(d.allowed)
        self.assertIn("deny by default", d.reason)
        rows = self.audit_rows()
        self.assertEqual((rows[0]["tenant_id"], rows[0]["allowed"], rows[0]["provider"]), ("no_policy", False, "groq"))

    def test_unknown_tenant_is_denied(self):
        self.assertFalse(egress_policy.evaluate_external_llm("never_heard_of_it", "x", GROQ_URL).allowed)

    def test_explicit_off_is_denied(self):
        self.assertFalse(egress_policy.evaluate_external_llm("explicit_off", "x", GROQ_URL).allowed)

    def test_provider_must_be_allowlisted(self):
        d = egress_policy.evaluate_external_llm("wrong_provider", "x", GROQ_URL)
        self.assertFalse(d.allowed)
        self.assertIn("allowlist", d.reason)

    def test_allowed_when_tenant_allows_and_provider_listed(self):
        d = egress_policy.evaluate_external_llm("open_tenant", "x", GROQ_URL)
        self.assertTrue(d.allowed)
        self.assertEqual(d.redaction, "basic")
        self.assertTrue(self.audit_rows()[0]["allowed"])

    def test_local_endpoints_are_not_egress_but_their_usage_is_recorded(self):
        urls = ("http://localhost:1234/v1", "http://127.0.0.1:11434/v1",
                "http://192.168.1.20:8000/v1", "http://host.docker.internal:1234/v1")
        for url in urls:
            d = egress_policy.evaluate_external_llm("no_policy", "x", url)
            self.assertTrue(d.allowed and d.local, url)
        rows = self.audit_rows()
        self.assertEqual(len(rows), len(urls))
        self.assertTrue(all(r["local"] and r["allowed"] for r in rows))

    def test_require_raises_when_denied(self):
        with self.assertRaises(egress_policy.EgressDenied):
            egress_policy.require_external_llm("no_policy", "x", GROQ_URL)

    def test_redaction(self):
        text = "Contact bob@example.com from 10.1.2.3 password=hunter2 token: abc"
        out = egress_policy.redact(text, "basic")
        for leaked in ("bob@example.com", "10.1.2.3", "hunter2"):
            self.assertNotIn(leaked, out)
        self.assertEqual(egress_policy.redact(text, "none"), text)


class RcaFallbackEgressTests(EgressBase):
    def test_denied_tenant_makes_no_network_request(self):
        with patch.object(groq_fallback_real.requests, "post") as post:
            with self.assertRaises(egress_policy.EgressDenied):
                groq_fallback_real.run_openai_fallback("cpu high on host", "ctx", tenant_id="no_policy")
        post.assert_not_called()

    def test_maybe_run_fallback_reports_egress_denied_and_does_not_use_fallback(self):
        with patch.object(groq_fallback_real.requests, "post") as post, \
             patch.object(app_module, "FALLBACK_RUNTIME_ENABLED", True), \
             patch.object(app_module.settings, "FALLBACK_ENABLED", True), \
             patch.object(app_module.settings, "fallback_is_configured", lambda: True):
            result = app_module.maybe_run_fallback(
                ticket="payment service timing out",
                primary_confidence=0.1,
                request_id="r1",
                retrieved_incidents=[{"classification": "x", "score": 0.5}],
                tenant_id="no_policy",
            )
        used, analysis, _, _, _, _, reason = result
        self.assertFalse(used)
        self.assertIsNone(analysis)
        self.assertEqual(reason, "egress_denied")
        post.assert_not_called()

    def test_allowed_tenant_calls_out_with_redacted_prompt(self):
        fake = MagicMock()
        fake.raise_for_status = lambda: None
        fake.json.return_value = {"choices": [{"message": {"content": json.dumps({
            "classification": "cpu_saturation", "probable_causes": ["x"], "next_checks": [],
            "recommended_actions": [], "confidence": 0.7, "escalate": False})}}]}
        with patch.object(groq_fallback_real.requests, "post", return_value=fake) as post:
            groq_fallback_real.run_openai_fallback("alert from bob@example.com host 10.9.8.7", "ctx",
                                                   tenant_id="open_tenant")
        self.assertEqual(post.call_count, 1)
        sent = json.dumps(post.call_args.kwargs["json"])
        self.assertNotIn("bob@example.com", sent)
        self.assertNotIn("10.9.8.7", sent)


class PlaybookEgressTests(EgressBase):
    def setUp(self):
        super().setUp()
        self._pb = [
            patch.object(playbook_agent, "get_cached_playbook", lambda c: None),
            patch.object(playbook_agent, "set_cached_playbook", lambda *a, **k: None),
        ]
        for p in self._pb:
            p.start()

    def tearDown(self):
        for p in reversed(self._pb):
            p.stop()
        super().tearDown()

    def test_call_llm_denied_makes_no_request(self):
        with patch("requests.post") as post:
            self.assertIsNone(playbook_agent._call_llm("zzz_class", "ticket", "rag", tenant_id="no_policy"))
        post.assert_not_called()

    def test_generate_playbook_with_llm_not_allowed_never_calls_out(self):
        with patch.object(playbook_agent, "_call_llm", side_effect=AssertionError("must not call")):
            self.assertEqual(playbook_agent.generate_playbook("zzz_class", "t", "r", allow_llm=False), [])

    def test_use_fallback_false_suppresses_playbook_generation_in_analyze(self):
        state = OrchestratorState(
            request_id="x", tenant_id="open_tenant", ticket="odd new problem", completed=True,
            triage=TriageOutput(domain="general", classification="zzz_unmapped_class", classification_certainty=0.5),
            retrieval=RetrievalOutput(),
            rca=RCAOutput(final_classification="zzz_unmapped_class", domain="general"),
        )
        with patch.object(OrchestratorAgent, "run", return_value=state), \
             patch.object(playbook_agent, "_call_llm", side_effect=AssertionError("external generation must not run")):
            out = app_module._analyze_core_agentic(AnalyzeRequest(ticket="odd new problem", use_fallback=False))
        self.assertEqual(out["resolution_playbook"], [])


if __name__ == "__main__":
    unittest.main()
