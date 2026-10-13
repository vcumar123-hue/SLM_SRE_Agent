"""
Regression tests: agentic confidence must match the legacy computation and drive fallback correctly.

Bug guarded against: rca_agent recomputed confidence with retrieval_quality=0.0, which capped
primary_confidence at 0.35 and forced the Groq fallback on every ticket.

Run from the project root:  .venv\\Scripts\\python.exe -m unittest tests.test_agentic_confidence_parity -v
"""
import unittest
from unittest.mock import patch

import app
from agents.models import OrchestratorState, RCAInput, RetrievalOutput, TriageOutput
from agents.orchestrator import OrchestratorAgent
from agents.rca_agent import _do_rca
from confidence_engine import compute_confidence_score
from models import AnalyzeRequest


def _state(retrieval_quality: float, embedding_score: float, certainty: float, feedback: float = 0.0,
           llm_score: float = None):
    return OrchestratorState(
        ticket="SAP login failing after reboot",
        triage=TriageOutput(
            domain="iam",
            classification="sap_login_failure",
            classification_certainty=certainty,
            llm_score=certainty if llm_score is None else llm_score,
        ),
        retrieval=RetrievalOutput(
            embedding_score=embedding_score,
            retrieval_quality=retrieval_quality,
            learned_feedback_score=feedback,
        ),
    )


def _rca_input(context: dict) -> RCAInput:
    return RCAInput(
        ticket="SAP login failing after reboot",
        domain="iam",
        classification="sap_login_failure",
        classification_certainty=0.8,
        primary_confidence=float(context.get("primary_confidence", 0.0)),
        confidence_context=context,
    )


class OrchestratorConfidenceTests(unittest.TestCase):
    def test_matches_legacy_engine_for_same_inputs(self):
        state = OrchestratorAgent()._compute_confidence(_state(0.8, 0.7, 0.8, 0.05))
        expected = compute_confidence_score(
            embedding_score=0.7,
            llm_score=0.8,
            retrieval_quality=0.8,
            classification_certainty=0.8,
            learned_feedback_score=0.05,
        )
        self.assertEqual(state.confidence_info["primary_confidence"], expected["primary_confidence"])

    def test_uses_raw_keyword_score_as_llm_score_like_legacy(self):
        # Legacy passes the raw keyword score as llm_score and the post-retrieval certainty separately.
        state = OrchestratorAgent()._compute_confidence(_state(0.8, 0.7, certainty=0.9, llm_score=0.4))
        expected = compute_confidence_score(
            embedding_score=0.7,
            llm_score=0.4,
            retrieval_quality=0.8,
            classification_certainty=0.9,
            learned_feedback_score=0.0,
        )
        self.assertEqual(state.confidence_info["llm_score"], 0.4)
        self.assertEqual(state.confidence_info["primary_confidence"], expected["primary_confidence"])

    def test_strong_retrieval_is_not_capped_at_035(self):
        state = OrchestratorAgent()._compute_confidence(_state(0.8, 0.7, 0.8))
        self.assertGreater(state.confidence_info["primary_confidence"], 0.35)
        self.assertEqual(state.confidence_info["retrieval_quality"], 0.8)

    def test_zero_retrieval_is_still_capped(self):
        state = OrchestratorAgent()._compute_confidence(_state(0.0, 0.0, 0.9))
        self.assertLessEqual(state.confidence_info["primary_confidence"], 0.35)


class OrchestratorRCADomainTests(unittest.TestCase):
    """The RCA domain must follow legacy's choose_better_domain, not collapse to 'general'."""

    def _rca_payload_domain(self, taxonomy_domain: str, triage_domain: str) -> str:
        state = _state(0.8, 0.7, 0.8)
        state.triage.domain = triage_domain
        state.confidence_info = {"primary_confidence": 0.8}
        captured = {}

        def fake_send(self, sender, recipient, intent, payload):
            captured.update(payload)
            return {"output": None}

        with patch.object(app, "infer_domain_from_taxonomy", return_value=taxonomy_domain), \
             patch.object(app, "resolve_linked_knowledge", return_value=(None, None)), \
             patch.object(OrchestratorAgent, "_send", fake_send):
            OrchestratorAgent()._step_rca(state, request=type("R", (), {})())
        return captured["domain"]

    def test_general_taxonomy_domain_keeps_triage_domain(self):
        self.assertEqual(self._rca_payload_domain("general", "compute"), "compute")

    def test_specific_taxonomy_domain_wins(self):
        self.assertEqual(self._rca_payload_domain("storage", "compute"), "storage")


class AgenticFinalDomainTests(unittest.TestCase):
    """_analyze_core_agentic must apply legacy's canonical display-domain step."""

    def _domain(self, ticket, classification, rca_domain, incidents=None):
        from agents.models import RCAOutput
        state = OrchestratorState(
            request_id="x", tenant_id="t", ticket=ticket, completed=True,
            triage=TriageOutput(domain=rca_domain, classification=classification, classification_certainty=0.5),
            retrieval=RetrievalOutput(incidents=incidents or []),
            rca=RCAOutput(final_classification=classification, domain=rca_domain),
        )
        with patch.object(OrchestratorAgent, "run", return_value=state), \
             patch.object(app, "get_resolution_playbook", return_value={}):
            out = app._analyze_core_agentic(AnalyzeRequest(ticket=ticket))
        self.assertEqual(out["domain"], out["analysis"]["domain"])
        return out["domain"]

    def test_sap_ticket_gets_application_erp(self):
        d = self._domain("Users cannot log in to SAP since the overnight reboot of hana-db-01",
                         "unclassified_issue", "general")
        self.assertEqual(d, "application_erp")

    def test_taxonomy_domain_is_kept(self):
        d = self._domain("CPU utilization on linux-lab-01 is at 98% for 20 minutes",
                         "cpu_saturation", "compute")
        self.assertEqual(d, "compute")

    def test_general_domain_falls_back_to_retrieved_incident_domain(self):
        d = self._domain("Something odd happened", "unclassified_issue", "general",
                         incidents=[{"domain": "general"}, {"domain": "database"}])
        self.assertEqual(d, "database")


class RCAAgentConfidenceTests(unittest.TestCase):
    def _run(self, context: dict, fallback_result):
        with patch.object(app, "build_enriched_analysis", return_value={"classification": "sap_login_failure"}), \
             patch.object(app, "maybe_run_fallback", return_value=fallback_result) as fb, \
             patch.object(app, "create_knowledge_candidate", return_value={"candidate_id": "kc-1"}) as candidate:
            out = _do_rca(_rca_input(context))
        return out, fb, candidate

    def test_fallback_decision_uses_context_confidence_not_zeros(self):
        context = {"primary_confidence": 0.82, "retrieval_quality": 0.8, "embedding_score": 0.7}
        no_fallback = (False, None, 0.0, None, None, None, "threshold_not_met")
        out, fb, candidate = self._run(context, no_fallback)
        self.assertEqual(fb.call_args.kwargs["primary_confidence"], 0.82)
        self.assertFalse(out.fallback_used)
        self.assertEqual(out.confidence_info["primary_confidence"], 0.82)
        self.assertEqual(out.confidence_info["embedding_score"], 0.7)
        candidate.assert_not_called()

    def test_fallback_confidence_is_persisted(self):
        context = {"primary_confidence": 0.30, "retrieval_quality": 0.1}
        analysis = {"classification": "sap_login_failure", "probable_causes": [], "next_checks": [],
                    "recommended_actions": [], "confidence": 0.71}
        fallback = (True, analysis, 0.71, "groq", "model-x", "groq", None)
        out, _, candidate = self._run(context, fallback)
        self.assertTrue(out.fallback_used)
        self.assertEqual(out.confidence_info["fallback_confidence"], 0.71)
        self.assertEqual(out.confidence_info["primary_confidence"], 0.30)
        self.assertEqual(out.candidate_record, {"candidate_id": "kc-1"})
        candidate.assert_called_once()

    def test_missing_context_falls_back_to_passed_primary_confidence(self):
        no_fallback = (False, None, 0.0, None, None, None, "threshold_not_met")
        with patch.object(app, "build_enriched_analysis", return_value={}), \
             patch.object(app, "maybe_run_fallback", return_value=no_fallback) as fb:
            inp = RCAInput(ticket="t", domain="iam", classification="c",
                           classification_certainty=0.8, primary_confidence=0.9)
            _do_rca(inp)
        self.assertEqual(fb.call_args.kwargs["primary_confidence"], 0.9)


if __name__ == "__main__":
    unittest.main()
