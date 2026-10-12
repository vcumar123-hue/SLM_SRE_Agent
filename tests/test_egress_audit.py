"""
Wave 1b: an external LLM decision is persisted to an audit sink or the call is denied (fail closed).

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_egress_audit -v
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

GROQ_URL = "https://api.groq.com/openai/v1"
OPEN = {"external_llm_policy": {"allow_external_llm": True, "allowed_llm_providers": ["groq"],
                                "external_llm_redaction": "basic"}}


def loader(tenant_id):
    return OPEN if tenant_id == "open_tenant" else {}


class AuditBase(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.audit = self.dir / "egress_audit.jsonl"
        # A path whose parent is a regular file can never be created: the JSONL sink always fails.
        blocker = self.dir / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        self.unwritable = blocker / "egress_audit.jsonl"
        self._patches = [
            patch.object(egress_policy, "AUDIT_FILE", self.audit),
            patch.object(egress_policy, "_write_pg", lambda row: False),  # no PostgreSQL sink by default
            patch.object(egress_policy, "_load_tenant_config", loader),
            patch.dict(os.environ, {"GROQ_API_KEY": "k", "GROQ_BASE_URL": GROQ_URL,
                                    "AIOPS_ALLOW_UNAUDITED_EXTERNAL_LLM": "", "AIOPS_ENV": ""}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def rows(self):
        return [json.loads(l) for l in self.audit.read_text(encoding="utf-8").splitlines()] if self.audit.exists() else []


class DurableAuditTests(AuditBase):
    def test_allowed_decision_is_persisted_before_the_call_is_permitted(self):
        d = egress_policy.evaluate_external_llm("open_tenant", "rca_fallback", GROQ_URL)
        self.assertTrue(d.allowed)
        self.assertTrue(d.audit_persisted)
        self.assertEqual(d.audit_sink, "jsonl")
        row = self.rows()[0]
        for key in ("ts", "tenant_id", "purpose", "provider", "allowed", "reason", "redaction"):
            self.assertIn(key, row)
        self.assertEqual((row["tenant_id"], row["purpose"], row["provider"], row["allowed"]),
                         ("open_tenant", "rca_fallback", "groq", True))
        self.assertNotIn("audit_persisted", row)

    def test_denied_decision_is_persisted_too(self):
        d = egress_policy.evaluate_external_llm("someone_else", "playbook_generation", GROQ_URL)
        self.assertFalse(d.allowed)
        self.assertTrue(d.audit_persisted)
        self.assertFalse(self.rows()[0]["allowed"])

    def test_unpersistable_audit_denies_the_external_call_and_logs(self):
        with patch.object(egress_policy, "AUDIT_FILE", self.unwritable):
            with self.assertLogs("aiops.egress", level="ERROR") as logs:
                d = egress_policy.evaluate_external_llm("open_tenant", "rca_fallback", GROQ_URL)
        self.assertFalse(d.allowed)
        self.assertFalse(d.audit_persisted)
        self.assertIn("fail closed", d.reason)
        self.assertTrue(d.audit_error)
        self.assertIn("could not persist", "\n".join(logs.output))

    def test_require_raises_when_audit_cannot_be_persisted(self):
        with patch.object(egress_policy, "AUDIT_FILE", self.unwritable):
            with self.assertRaises(egress_policy.EgressDenied) as ctx:
                egress_policy.require_external_llm("open_tenant", "x", GROQ_URL)
        self.assertIn("fail closed", str(ctx.exception))

    def test_local_endpoint_is_never_blocked_by_audit_failure(self):
        with patch.object(egress_policy, "AUDIT_FILE", self.unwritable):
            with self.assertLogs("aiops.egress", level="ERROR"):
                d = egress_policy.evaluate_external_llm("open_tenant", "x", "http://localhost:1234/v1")
        self.assertTrue(d.allowed and d.local)
        self.assertFalse(d.audit_persisted)


class SinkPreferenceTests(AuditBase):
    def test_postgres_is_preferred_and_jsonl_untouched(self):
        sent = []
        with patch.object(egress_policy, "_write_pg", lambda row: sent.append(row) or True):
            d = egress_policy.evaluate_external_llm("open_tenant", "x", GROQ_URL)
        self.assertEqual((d.audit_sink, d.audit_persisted), ("postgres", True))
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.rows(), [])

    def test_postgres_failure_falls_back_to_jsonl_with_a_warning(self):
        def boom(row):
            raise ConnectionError("db down")

        with patch.object(egress_policy, "_write_pg", boom):
            with self.assertLogs("aiops.egress", level="WARNING"):
                d = egress_policy.evaluate_external_llm("open_tenant", "x", GROQ_URL)
        self.assertTrue(d.allowed)
        self.assertEqual(d.audit_sink, "jsonl")
        self.assertIn("db down", d.audit_error)
        self.assertEqual(len(self.rows()), 1)

    def test_both_sinks_failing_denies(self):
        def boom(row):
            raise ConnectionError("db down")

        with patch.object(egress_policy, "_write_pg", boom), patch.object(egress_policy, "AUDIT_FILE", self.unwritable):
            with self.assertLogs("aiops.egress", level="ERROR"):
                d = egress_policy.evaluate_external_llm("open_tenant", "x", GROQ_URL)
        self.assertFalse(d.allowed)
        self.assertIn("postgres", d.audit_error)
        self.assertIn("jsonl", d.audit_error)


class UnauditedBypassTests(AuditBase):
    def test_bypass_allows_in_dev_and_is_logged_loudly(self):
        with patch.dict(os.environ, {"AIOPS_ALLOW_UNAUDITED_EXTERNAL_LLM": "true"}), \
             patch.object(egress_policy, "AUDIT_FILE", self.unwritable):
            with self.assertLogs("aiops.egress", level="ERROR") as logs:
                d = egress_policy.evaluate_external_llm("open_tenant", "x", GROQ_URL)
        self.assertTrue(d.allowed)
        self.assertIn("UNAUDITED", d.reason)
        self.assertIn("UNAUDITED", "\n".join(logs.output))

    def test_bypass_is_ignored_in_production(self):
        with patch.dict(os.environ, {"AIOPS_ALLOW_UNAUDITED_EXTERNAL_LLM": "true", "AIOPS_ENV": "production"}), \
             patch.object(egress_policy, "AUDIT_FILE", self.unwritable):
            with self.assertLogs("aiops.egress", level="ERROR") as logs:
                d = egress_policy.evaluate_external_llm("open_tenant", "x", GROQ_URL)
        self.assertFalse(d.allowed)
        self.assertIn("forbidden when AIOPS_ENV=production", "\n".join(logs.output))

    def test_bypass_cannot_override_a_policy_denial(self):
        with patch.dict(os.environ, {"AIOPS_ALLOW_UNAUDITED_EXTERNAL_LLM": "true"}):
            d = egress_policy.evaluate_external_llm("someone_else", "x", GROQ_URL)
        self.assertFalse(d.allowed)


class CallSitesFailClosedTests(AuditBase):
    def test_rca_fallback_makes_no_request_when_audit_cannot_persist(self):
        with patch.object(egress_policy, "AUDIT_FILE", self.unwritable), \
             patch.object(groq_fallback_real.requests, "post") as post:
            with self.assertRaises(egress_policy.EgressDenied):
                groq_fallback_real.run_openai_fallback("cpu high", "ctx", tenant_id="open_tenant")
        post.assert_not_called()

    def test_maybe_run_fallback_reports_egress_denied_when_audit_cannot_persist(self):
        with patch.object(egress_policy, "AUDIT_FILE", self.unwritable), \
             patch.object(groq_fallback_real.requests, "post") as post, \
             patch.object(app_module, "FALLBACK_RUNTIME_ENABLED", True), \
             patch.object(app_module.settings, "FALLBACK_ENABLED", True), \
             patch.object(app_module.settings, "fallback_is_configured", lambda: True):
            used, analysis, _, _, _, _, reason = app_module.maybe_run_fallback(
                ticket="payment timeout", primary_confidence=0.1, request_id="r",
                retrieved_incidents=[{"classification": "x", "score": 0.5}], tenant_id="open_tenant")
        self.assertEqual((used, analysis, reason), (False, None, "egress_denied"))
        post.assert_not_called()

    def test_playbook_generation_makes_no_request_when_audit_cannot_persist(self):
        with patch.object(egress_policy, "AUDIT_FILE", self.unwritable), patch("requests.post") as post:
            self.assertIsNone(playbook_agent._call_llm("zzz_class", "t", "r", tenant_id="open_tenant"))
        post.assert_not_called()

    def test_allowed_and_audited_call_goes_out_once(self):
        fake = MagicMock()
        fake.raise_for_status = lambda: None
        fake.json.return_value = {"choices": [{"message": {"content": json.dumps({
            "classification": "cpu_saturation", "probable_causes": [], "next_checks": [],
            "recommended_actions": [], "confidence": 0.7, "escalate": False})}}]}
        with patch.object(groq_fallback_real.requests, "post", return_value=fake) as post:
            groq_fallback_real.run_openai_fallback("cpu high", "ctx", tenant_id="open_tenant")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(self.rows()), 1)


if __name__ == "__main__":
    unittest.main()
