"""Characterization tests for the legacy analysis contract before Wave 4 extraction."""

import os
import sys
import types
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import app
from models import AnalyzeRequest


class LegacyAnalyzeDispatchTests(unittest.TestCase):
    def test_agentic_disabled_dispatches_to_legacy(self):
        request = AnalyzeRequest(ticket="CPU high on host-01")
        expected = {"path": "legacy"}
        with patch.dict(os.environ, {"AIOPS_AGENTIC_ORCHESTRATOR_ENABLED": "false"}), \
             patch.object(app, "_analyze_core_legacy", return_value=expected) as legacy, \
             patch.object(app, "_analyze_core_agentic") as agentic:
            self.assertIs(app.analyze_core(request), expected)
        legacy.assert_called_once_with(request)
        agentic.assert_not_called()

    def test_agentic_failure_falls_back_to_legacy(self):
        request = AnalyzeRequest(ticket="CPU high on host-01")
        expected = {"path": "legacy"}
        with patch.dict(os.environ, {"AIOPS_AGENTIC_ORCHESTRATOR_ENABLED": "true"}), \
             patch.object(app, "_analyze_core_agentic", side_effect=RuntimeError("failed")) as agentic, \
             patch.object(app, "_analyze_core_legacy", return_value=expected) as legacy:
            self.assertIs(app.analyze_core(request), expected)
        agentic.assert_called_once_with(request)
        legacy.assert_called_once_with(request)

    def test_successful_dispatch_records_completion_once(self):
        request = AnalyzeRequest(ticket="CPU high on host-01")
        result = {
            "analysis": {"classification": "cpu_saturation"},
            "confidence": {"overall_confidence": 0.81},
            "fallback_used": False,
        }
        insert_trace = Mock()
        trace_store = types.SimpleNamespace(insert_agent_trace=insert_trace)
        with patch.dict(os.environ, {"AIOPS_AGENTIC_ORCHESTRATOR_ENABLED": "false"}), \
             patch.object(app, "_analyze_core_legacy", return_value=result), \
             patch.dict(sys.modules, {"postgres_store": trace_store}):
            self.assertIs(app.analyze_core(request), result)
        insert_trace.assert_called_once()
        self.assertEqual(insert_trace.call_args.kwargs["status"], "completed")
        self.assertIn("classification=cpu_saturation", insert_trace.call_args.kwargs["output_summary"])


class SharedFinalizationTests(unittest.TestCase):
    def test_final_incident_filter_prefers_domain_aligned_evidence(self):
        incidents = [
            {"id": "db", "domain": "database", "classification": "oracle_deadlock"},
            {"id": "cpu", "domain": "compute", "classification": "cpu_saturation"},
        ]
        filtered = app.filter_final_retrieved_incidents(
            ticket="CPU is high",
            classification="cpu_saturation",
            display_domain="compute",
            incidents=incidents,
            top_k=5,
        )
        self.assertEqual([item["id"] for item in filtered], ["cpu"])

    def test_post_rca_guards_run_in_stable_order(self):
        calls = []

        def symptom(**kwargs):
            calls.append("symptom")
            return kwargs["domain"], kwargs["final_classification"], kwargs["analysis"]

        def guard(name):
            def run(**kwargs):
                calls.append(name)
                return (
                    kwargs["domain"], kwargs["final_classification"],
                    kwargs["classification_certainty"], kwargs["analysis"],
                )
            return run

        with patch.object(app, "enforce_symptom_cause_classification_lock", side_effect=symptom), \
             patch.object(app, "apply_final_container_connectivity_guard", side_effect=guard("container")), \
             patch.object(app, "apply_final_batch_scheduler_guard", side_effect=guard("batch")), \
             patch.object(app, "apply_final_compact_context_normalization", side_effect=guard("compact")):
            app.apply_shared_post_rca_guards(
                ticket="ticket",
                analysis={"classification": "x"},
                domain="general",
                classification="x",
                classification_certainty=0.5,
            )
        self.assertEqual(calls, ["symptom", "container", "batch", "compact"])

    def test_fallback_confidence_is_hidden_when_display_is_disabled(self):
        with patch.object(app.settings, "ALLOW_FALLBACK_CONFIDENCE_DISPLAY", False):
            value = app.finalize_display_confidence(
                primary_confidence=0.42,
                fallback_confidence=0.91,
                fallback_used=True,
                has_evidence=True,
            )
        self.assertEqual(value, 0.42)

    def test_zero_confidence_uses_floor_only_when_evidence_exists(self):
        with patch.object(app.settings, "MIN_PRIMARY_CONFIDENCE_FLOOR", 0.2):
            with_evidence = app.finalize_display_confidence(
                primary_confidence=0.0,
                fallback_confidence=0.0,
                fallback_used=False,
                has_evidence=True,
            )
            without_evidence = app.finalize_display_confidence(
                primary_confidence=0.0,
                fallback_confidence=0.0,
                fallback_used=False,
                has_evidence=False,
            )
        self.assertEqual(with_evidence, 0.2)
        self.assertEqual(without_evidence, 0.0)

    def test_generic_domain_uses_retrieved_specific_domain(self):
        with patch.object(app, "infer_ticket_domain", return_value="general"), \
             patch.object(app, "canonical_domain_from_classification", return_value="general"):
            domain = app.resolve_final_display_domain(
                ticket="Something failed",
                classification="unclassified_issue",
                current_domain="general",
                taxonomy={},
                retrieved_incidents=[{"domain": "general"}, {"domain": "database"}],
            )
        self.assertEqual(domain, "database")


class LegacyAnalyzeResponseContractTests(unittest.TestCase):
    """Pin the public response shape produced by the deterministic local path."""

    def _run_local(self):
        request = AnalyzeRequest(
            ticket="CPU utilization on host-01 is at 98 percent",
            use_fallback=False,
            tenant_id="client_a",
            knowledge_scope="client_private",
        )
        incident = {
            "id": "inc-1",
            "domain": "compute",
            "classification": "cpu_saturation",
            "score": 0.91,
            "retrieval_method": "vector",
        }
        resolution = {"resolution_id": "res-1", "confirmed_by": "engineer"}
        playbook = {"playbook_id": "pb-1", "name": "CPU recovery"}
        analysis = {
            "classification": "cpu_saturation",
            "domain": "compute",
            "probable_causes": ["Runaway process"],
            "next_checks": ["Inspect process CPU"],
            "recommended_actions": ["Stop the runaway process"],
            "escalate": False,
        }
        confidence = {
            "primary_confidence": 0.84,
            "embedding_score": 0.91,
            "llm_score": 0.8,
            "retrieval_quality": 0.91,
            "classification_certainty": 0.91,
            "learned_feedback_score": 0.1,
        }

        passthrough_guard = lambda **kw: (
            kw["domain"], kw["final_classification"],
            kw["classification_certainty"], kw["analysis"]
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(app, "load_taxonomy", return_value={}))
            stack.enter_context(patch.object(app, "run_hybrid_retrieval", return_value=(
                [incident], 0.91, 0.1, None,
                {"tenant_id": "client_a", "display_name": "Client A", "knowledge_scope": "client_private"},
            )))
            stack.enter_context(patch.object(app, "summarize_retrieval_quality", return_value=0.91))
            stack.enter_context(patch.object(app, "fetch_rag_context", return_value="known CPU pattern"))
            stack.enter_context(patch.object(app, "classify_by_keywords", return_value=("compute", "cpu_saturation", 0.8)))
            stack.enter_context(patch.object(app, "infer_ticket_domain", return_value="compute"))
            stack.enter_context(patch.object(app, "apply_intent_router", return_value=("compute", "cpu_saturation", 0.91, {})))
            stack.enter_context(patch.object(app, "normalize_symptom_vs_cause_classification", return_value=("compute", "cpu_saturation", 0.91, {})))
            stack.enter_context(patch.object(app, "infer_domain_from_taxonomy", return_value="compute"))
            stack.enter_context(patch.object(app, "compute_confidence_score", return_value=confidence))
            stack.enter_context(patch.object(app, "should_force_fallback_from_intent", return_value=(False, None)))
            stack.enter_context(patch.object(app, "resolve_linked_knowledge", return_value=(resolution, playbook)))
            stack.enter_context(patch.object(app, "should_request_user_confirmation", return_value=False))
            stack.enter_context(patch.object(app, "build_enriched_analysis", return_value=analysis.copy()))
            stack.enter_context(patch.object(app, "enforce_symptom_cause_classification_lock", return_value=("compute", "cpu_saturation", analysis.copy())))
            stack.enter_context(patch.object(app, "apply_final_container_connectivity_guard", side_effect=passthrough_guard))
            stack.enter_context(patch.object(app, "apply_final_batch_scheduler_guard", side_effect=passthrough_guard))
            stack.enter_context(patch.object(app, "apply_final_compact_context_normalization", side_effect=passthrough_guard))
            stack.enter_context(patch.object(app, "canonical_domain_for_classification", return_value="compute"))
            stack.enter_context(patch.object(app, "canonical_domain_from_classification", return_value="compute"))
            stack.enter_context(patch.object(app, "build_workflow_timeline", return_value=[{"step": "complete"}]))
            stack.enter_context(patch.object(app, "build_automation_summary", return_value={"status": "ready"}))
            persist = stack.enter_context(patch.object(app, "persist_confidence_log"))
            stack.enter_context(patch.object(app, "get_resolution_playbook", return_value={"name": "CPU recovery"}))
            result = app._analyze_core_legacy(request)
        return result, persist

    def test_local_path_response_shape_and_linkage(self):
        result, persist = self._run_local()
        expected_keys = {
            "request_id", "ticket", "routing_input", "domain", "routing",
            "user_confirmation_prompt", "user_confirmation_options",
            "retrieved_incidents", "similar_incidents", "analysis", "confidence",
            "fallback_used", "fallback_runtime_enabled", "response_source",
            "analysis_source_label", "tenant_config", "knowledge_candidate",
            "knowledge_summary", "rag_context", "resolution_playbook",
            "feedback_override", "linked_resolution", "linked_playbook",
            "workflow_timeline", "automation_summary", "evidence_ui",
            "validation_recommendation",
        }
        self.assertEqual(set(result), expected_keys)
        self.assertEqual(result["routing"]["decision"], "local_primary")
        self.assertEqual(result["domain"], "compute")
        self.assertEqual(result["analysis"]["classification"], "cpu_saturation")
        self.assertEqual(result["confidence"]["overall_confidence"], 0.84)
        self.assertEqual(result["knowledge_summary"]["linked_resolution_id"], "res-1")
        self.assertEqual(result["knowledge_summary"]["linked_playbook_id"], "pb-1")
        self.assertEqual(result["retrieved_incidents"], result["similar_incidents"])
        persist.assert_called_once()

    def test_confidence_log_matches_response_decision(self):
        result, persist = self._run_local()
        record = persist.call_args.args[0]
        self.assertEqual(record["decision"], result["routing"]["decision"])
        self.assertEqual(record["display_confidence"], result["confidence"]["overall_confidence"])
        self.assertEqual(record["classification"], result["analysis"]["classification"])


if __name__ == "__main__":
    unittest.main()
