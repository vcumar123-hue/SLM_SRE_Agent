"""
Wave 4b: legacy and agentic analysis must give the same answer for the same ticket.

Both paths run side by side with external dependencies stubbed (no Qdrant, PostgreSQL, LLM or file writes),
and the fields an operator acts on are compared across representative ticket families.

Run from the project root:  python -m unittest tests.test_legacy_agentic_parity -v
"""
import logging
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import app
from agents.a2a_protocol import register_agent
from agents.learning_agent import learning_handler
from agents.rca_agent import rca_handler
from agents.resolution_agent import resolution_handler
from agents.retrieval_agent import retrieval_handler
from agents.safety_agent import safety_handler
from agents.triage_agent import triage_handler
from models import AnalyzeRequest

GOLDEN_TICKETS = {
    "sap_login": "Users cannot log in to SAP since the overnight reboot of hana-db-01",
    "cpu": "CPU utilization on linux-lab-01 is at 98% for the last 20 minutes, application is slow",
    "disk": "Disk usage on /var is above 90 percent on linux-lab-02, temp files growing",
    "vpn": "VPN client cannot connect, tunnel drops after authentication",
    "container": "Application container cannot connect to the database, connection refused",
    "batch": "Nightly batch job failed with exit code 1 on scheduler node",
}

COMPARED = ("domain", "classification", "probable_causes", "next_checks", "recommended_actions", "escalate")


def _fallback_stub(used: bool):
    analysis = {
        "classification": "cpu_saturation", "domain": "compute",
        "probable_causes": ["fallback cause"], "next_checks": ["fallback check"],
        "recommended_actions": ["fallback action"], "escalate": False,
    }

    def run(ticket, primary_confidence, request_id, force_fallback=False, reason_override=None,
            retrieved_incidents=None, tenant_id=None):
        if used:
            return (True, analysis, 0.7, "groq", "stub-model", "groq", "stubbed fallback")
        return (False, None, 0.0, None, None, None, "stubbed")

    return run


def view(result):
    a = result.get("analysis") or {}
    c = result.get("confidence") or {}
    return {
        "domain": result.get("domain"),
        "classification": a.get("classification"),
        "probable_causes": a.get("probable_causes"),
        "next_checks": a.get("next_checks"),
        "recommended_actions": a.get("recommended_actions"),
        "escalate": a.get("escalate"),
        "fallback_used": result.get("fallback_used"),
        "response_source": result.get("response_source"),
        "decision": (result.get("routing") or {}).get("decision"),
        "primary_confidence": c.get("primary_confidence"),
        "overall_confidence": c.get("overall_confidence"),
        "incident_ids": sorted(str(i.get("id")) for i in (result.get("retrieved_incidents") or [])),
        "linked_resolution_id": (result.get("linked_resolution") or {}).get("resolution_id"),
        "linked_playbook_id": (result.get("linked_playbook") or {}).get("playbook_id"),
    }


class SideBySideParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name, handler in (("triage", triage_handler), ("retrieval", retrieval_handler),
                              ("safety", safety_handler), ("rca", rca_handler),
                              ("resolution", resolution_handler), ("learning", learning_handler)):
            register_agent(name, handler)

    def run_both(self, ticket, *, fallback_used=False):
        request = AnalyzeRequest(ticket=ticket, use_fallback=True)
        out = {}
        for name, fn in (("legacy", app._analyze_core_legacy), ("agentic", app._analyze_core_agentic)):
            with ExitStack() as stack:
                stack.enter_context(patch.object(app, "get_resolution_playbook", return_value=[]))
                stack.enter_context(patch.object(app, "maybe_run_fallback", side_effect=_fallback_stub(fallback_used)))
                stack.enter_context(patch.object(app, "create_knowledge_candidate", None))
                stack.enter_context(patch.object(app, "persist_confidence_log"))
                stack.enter_context(patch.object(app, "fetch_rag_context", return_value={
                    "app_profile": [], "incident_patterns": [], "resolution_hints": [], "sources": []}))
                stack.enter_context(patch.object(app, "run_hybrid_retrieval", return_value=(
                    [], 0.0, 0.0, None,
                    {"tenant_id": "default", "display_name": "default", "knowledge_scope": "client_private"})))
                logging.disable(logging.CRITICAL)
                try:
                    out[name] = view(fn(request))
                finally:
                    logging.disable(logging.NOTSET)
        return out["legacy"], out["agentic"]

    def test_golden_tickets_give_the_same_operator_facing_answer(self):
        for family, ticket in GOLDEN_TICKETS.items():
            with self.subTest(family=family):
                legacy, agentic = self.run_both(ticket)
                for key in legacy:
                    self.assertEqual(legacy[key], agentic[key], f"{family}: '{key}' differs between legacy and agentic")

    def test_cpu_and_vpn_causes_match_exactly(self):
        """The two tickets that diverged before Wave 4b: extra/unknown_factor causes appeared only on the agentic path."""
        for family in ("cpu", "vpn"):
            with self.subTest(family=family):
                legacy, agentic = self.run_both(GOLDEN_TICKETS[family])
                self.assertEqual(legacy["probable_causes"], agentic["probable_causes"])
                self.assertNotIn("unknown_factor", agentic["probable_causes"] or [])

    def test_no_agentic_only_decision_label(self):
        for family, ticket in GOLDEN_TICKETS.items():
            with self.subTest(family=family):
                _, agentic = self.run_both(ticket)
                self.assertNotEqual(agentic["decision"], "knowledge_reused")

    def test_fallback_used_path_matches(self):
        for family in ("cpu", "vpn", "sap_login"):
            with self.subTest(family=family):
                legacy, agentic = self.run_both(GOLDEN_TICKETS[family], fallback_used=True)
                for key in legacy:
                    self.assertEqual(legacy[key], agentic[key], f"{family} (fallback): '{key}' differs")


if __name__ == "__main__":
    unittest.main()
