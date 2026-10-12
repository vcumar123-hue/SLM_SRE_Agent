"""
agents/orchestrator.py — Orchestrator Agent (Phase 2)

Replaces the fixed analyze_core() pipeline when
AIOPS_AGENTIC_ORCHESTRATOR_ENABLED=true.

Flow:
  triage → retrieval → [merge triage+retrieval] → safety
        → rca → resolution → learning (read-only, pattern signal only)

Feature flag: set AIOPS_AGENTIC_ORCHESTRATOR_ENABLED=true in environment.
Flag off (default): analyze_core() runs unchanged.
Flag on:            OrchestratorAgent.run() drives specialist agents via A2A.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Optional

from agents.a2a_protocol import get_dispatcher, AgentMessage, TraceSpan
from agents.models import (
    OrchestratorState,
    TriageInput, TriageOutput,
    RetrievalInput, RetrievalOutput,
    SafetyInput, SafetyOutput,
    RCAInput, RCAOutput,
    ResolutionInput, ResolutionOutput,
    LearningInput, LearningOutput,
)

logger = logging.getLogger(__name__)

AGENTIC_FLAG = "AIOPS_AGENTIC_ORCHESTRATOR_ENABLED"


def is_agentic_enabled() -> bool:
    return os.environ.get(AGENTIC_FLAG, "false").strip().lower() in {"1", "true", "yes"}


class OrchestratorAgent:
    """
    Drives specialist agents in sequence via the A2A dispatcher.
    Each step reads from OrchestratorState set by the previous step,
    so branches (e.g. skip triage classification when retrieval quality is high)
    are expressed as state conditions, not hardcoded pipeline order.
    """

    def __init__(self) -> None:
        self._dispatcher = get_dispatcher()

    def _send(self, sender: str, recipient: str, intent: str, payload: Dict[str, Any]) -> Any:
        """Synchronous A2A send. Returns the result dict or raises."""
        from agents.a2a_protocol import MessageStatus
        msg = AgentMessage(sender=sender, recipient=recipient, intent=intent, payload=payload)
        resp = self._dispatcher.send_sync(msg, timeout=60.0, retries=1)
        if resp.status not in {MessageStatus.DELIVERED}:
            raise RuntimeError(f"A2A {sender}→{recipient} failed: {resp.error}")
        return resp.result

    # ── Steps ─────────────────────────────────────────────────────────────────

    def _step_triage(self, state: OrchestratorState, request: Any) -> OrchestratorState:
        payload = TriageInput(
            ticket=state.ticket,
            tenant_id=state.tenant_id,
            knowledge_scope=getattr(request, "knowledge_scope", "client_private"),
            request_id=state.request_id,
        ).model_dump()
        result = self._send("orchestrator", "triage", "triage", payload)
        if result and result.get("output"):
            state.triage = TriageOutput(**result["output"])
        if result and result.get("trace"):
            state.trace_spans.append(result["trace"])
        return state

    def _step_retrieval(self, state: OrchestratorState, request: Any) -> OrchestratorState:
        payload = RetrievalInput(
            ticket=state.ticket,
            tenant_id=state.tenant_id,
            knowledge_scope=getattr(request, "knowledge_scope", "client_private"),
            top_k=getattr(request, "top_k", None) or 5,
            request_id=state.request_id,
        ).model_dump()
        result = self._send("orchestrator", "retrieval", "retrieve", payload)
        if result and result.get("output"):
            state.retrieval = RetrievalOutput(**result["output"])
        if result and result.get("trace"):
            state.trace_spans.append(result["trace"])
        return state

    def _merge_triage_retrieval(self, state: OrchestratorState) -> OrchestratorState:
        """
        Apply the retrieval vector match on top of the triage baseline,
        and save the locked classification before intent router effects.
        This mirrors the app.py logic that was previously embedded in analyze_core().
        """
        if not state.triage or not state.retrieval:
            return state

        from app import (
            normalize_classification_token,
            infer_domain_from_taxonomy,
            choose_better_domain,
            load_taxonomy,
            apply_intent_router,
            normalize_symptom_vs_cause_classification,
        )
        import re

        taxonomy = load_taxonomy()
        incidents = state.retrieval.incidents
        triage = state.triage

        domain = triage.domain
        classification = triage.classification
        certainty = triage.classification_certainty
        retrieval_locked_classification = ""
        retrieval_locked_score = 0.0

        # Apply feedback override if present
        feedback_override = state.retrieval.feedback_override
        if feedback_override:
            classification = normalize_classification_token(
                feedback_override.get("corrected_classification")
            ) or classification
            domain = choose_better_domain(
                feedback_override.get("stored_domain"),
                infer_domain_from_taxonomy(classification, taxonomy),
            )
            certainty = max(certainty, 0.92)

        # Apply vector match classification
        if incidents:
            top = sorted(incidents, key=lambda x: float(x.get("score", 0.0) or 0.0), reverse=True)[0]
            retrieved_cls = str(top.get("classification", "") or "").strip()
            top_score = float(top.get("score", 0.0) or 0.0)
            top_source = str(top.get("source", "") or "").lower()
            retrieved_domain = str(top.get("domain", "") or "").strip().lower()
            min_score = 0.25

            is_topology = re.match(r"^topology_\d+$", retrieved_cls, re.IGNORECASE) is not None
            is_junk = retrieved_cls.lower() in {"unclassified_issue", "unknown", "general_issue", ""}

            if retrieved_cls and not is_topology and not is_junk and top_score >= min_score:
                from app import extract_ticket_intents
                _intents = extract_ticket_intents(state.ticket)
                if not (top_source == "feedback_learning" and _intents.get("network_dns") and "browser_sso" in retrieved_cls.lower()):
                    classification = normalize_classification_token(retrieved_cls)
                    certainty = max(certainty, top_score)
                    domain = choose_better_domain(infer_domain_from_taxonomy(classification, taxonomy), domain)
                    # Lock before intent router can override
                    retrieval_locked_classification = classification
                    retrieval_locked_score = top_score

            if retrieved_domain:
                taxonomy_domain = infer_domain_from_taxonomy(classification, taxonomy)
                domain = choose_better_domain(taxonomy_domain, retrieved_domain)

        # Intent router (may override classification for surface-symptom tickets)
        domain, classification, certainty, intents = apply_intent_router(
            ticket=state.ticket,
            domain=domain,
            classification=classification,
            classification_certainty=certainty,
        )
        domain, classification, certainty, _ = normalize_symptom_vs_cause_classification(
            ticket=state.ticket,
            domain=domain,
            classification=classification,
            classification_certainty=certainty,
        )

        # Write back merged state into triage output
        state.triage = TriageOutput(
            domain=domain,
            classification=classification,
            classification_certainty=certainty,
            llm_score=triage.llm_score,
            retrieval_locked_classification=retrieval_locked_classification,
            retrieval_locked_score=retrieval_locked_score,
            intents=dict(intents) if intents else state.triage.intents,
            feedback_override=feedback_override,
        )

        span = TraceSpan(agent="merge", intent="triage_retrieval_merge")
        span.finish(
            output_summary=f"cls={classification} locked={retrieval_locked_classification} score={retrieval_locked_score:.2f}",
            decision=classification,
        )
        state.trace_spans.append(span.to_dict())
        return state

    def _compute_confidence(self, state: OrchestratorState) -> OrchestratorState:
        """Single source of truth for confidence; same function/fallback order as the legacy path."""
        triage = state.triage
        retrieval = state.retrieval
        if not triage:
            return state

        from app import compute_confidence_locally
        try:
            from confidence_engine import compute_confidence_score
        except Exception:
            compute_confidence_score = None

        kwargs = dict(
            embedding_score=retrieval.embedding_score if retrieval else 0.0,
            llm_score=triage.llm_score,
            retrieval_quality=retrieval.retrieval_quality if retrieval else 0.0,
            classification_certainty=triage.classification_certainty,
            learned_feedback_score=retrieval.learned_feedback_score if retrieval else 0.0,
        )
        try:
            if compute_confidence_score is None:
                raise RuntimeError("confidence_engine unavailable")
            state.confidence_info = compute_confidence_score(**kwargs)
        except Exception:
            state.confidence_info = compute_confidence_locally(**kwargs)
        return state

    def _step_safety(self, state: OrchestratorState, request: Any) -> OrchestratorState:
        triage = state.triage
        retrieval = state.retrieval
        if not triage:
            return state

        from app import resolve_linked_knowledge
        linked_res, linked_pb = resolve_linked_knowledge(triage.classification)

        primary_confidence = float(state.confidence_info.get("primary_confidence", 0.0) or 0.0)

        payload = SafetyInput(
            ticket=state.ticket,
            classification=triage.classification,
            primary_confidence=primary_confidence,
            incidents=retrieval.incidents if retrieval else [],
            linked_resolution=linked_res,
            linked_playbook=linked_pb,
            confirm_local_matches=getattr(request, "confirm_local_matches", None),
            use_fallback=getattr(request, "use_fallback", True),
        ).model_dump()
        result = self._send("orchestrator", "safety", "safety_check", payload)
        if result and result.get("output"):
            state.safety = SafetyOutput(**result["output"])
        if result and result.get("trace"):
            state.trace_spans.append(result["trace"])
        return state

    def _step_rca(self, state: OrchestratorState, request: Any) -> OrchestratorState:
        triage = state.triage
        retrieval = state.retrieval
        safety = state.safety
        if not triage:
            return state

        from app import resolve_linked_knowledge
        linked_res, linked_pb = resolve_linked_knowledge(triage.classification)
        primary_confidence = float(state.confidence_info.get("primary_confidence", 0.0) or 0.0)

        from app import infer_domain_from_taxonomy, choose_better_domain, load_taxonomy as _ltax
        _rca_domain = choose_better_domain(infer_domain_from_taxonomy(triage.classification, _ltax()), triage.domain)

        payload = RCAInput(
            ticket=state.ticket,
            domain=_rca_domain,
            classification=triage.classification,
            classification_certainty=triage.classification_certainty,
            retrieval_locked_classification=triage.retrieval_locked_classification,
            retrieval_locked_score=triage.retrieval_locked_score,
            incidents=retrieval.incidents if retrieval else [],
            rag_context=retrieval.rag_context if retrieval else "",
            primary_confidence=primary_confidence,
            confidence_context=dict(state.confidence_info),
            linked_resolution=linked_res,
            linked_playbook=linked_pb,
            use_fallback=getattr(request, "use_fallback", True),
            force_fallback=safety.force_fallback if safety else False,
            force_fallback_reason=safety.force_fallback_reason if safety else None,
            tenant_id=state.tenant_id,
            knowledge_scope=getattr(request, "knowledge_scope", "client_private"),
            request_id=state.request_id,
        ).model_dump()
        result = self._send("orchestrator", "rca", "rca", payload)
        if result and result.get("output"):
            state.rca = RCAOutput(**result["output"])
        if result and result.get("trace"):
            state.trace_spans.append(result["trace"])
        return state

    def _step_resolution(self, state: OrchestratorState, request: Any) -> OrchestratorState:
        rca = state.rca
        triage = state.triage
        if not rca:
            return state

        payload = ResolutionInput(
            classification=triage.classification if triage else rca.final_classification,
            final_classification=rca.final_classification,
            domain=rca.domain,
            analysis=rca.analysis,
            fallback_used=rca.fallback_used,
            fallback_provider=rca.fallback_provider,
            fallback_model=rca.fallback_model,
            candidate_record=rca.candidate_record,
            tenant_id=state.tenant_id,
            knowledge_scope=getattr(request, "knowledge_scope", "client_private"),
        ).model_dump()
        result = self._send("orchestrator", "resolution", "resolve", payload)
        if result and result.get("output"):
            state.resolution = ResolutionOutput(**result["output"])
        if result and result.get("trace"):
            state.trace_spans.append(result["trace"])
        return state

    def _step_learning(self, state: OrchestratorState) -> OrchestratorState:
        """Read-only learning step at analysis time — just fetches pattern signal.
        Outcome recording happens later when the incident is actually resolved."""
        rca = state.rca
        if not rca:
            return state

        payload = LearningInput(
            classification=rca.final_classification,
            tenant_id=state.tenant_id,
            incident_id="",
            resolution_id="",
            execution_mode="manual",
            outcome_status="pending",
        ).model_dump()
        result = self._send("orchestrator", "learning", "learn", payload)
        if result and result.get("output"):
            state.learning = LearningOutput(**result["output"])
        if result and result.get("trace"):
            state.trace_spans.append(result["trace"])
        return state

    # ── Main entry ─────────────────────────────────────────────────────────────

    def run(self, request: Any) -> OrchestratorState:
        """
        Orchestrate a full incident analysis.
        `request` is an AnalyzeRequest (or duck-typed equivalent).
        Returns OrchestratorState with all agent outputs and trace spans.
        """
        state = OrchestratorState(
            request_id=str(uuid.uuid4().hex[:12]),
            tenant_id=getattr(request, "tenant_id", "default") or "default",
            ticket=getattr(request, "ticket", ""),
        )

        entry_span = TraceSpan(agent="orchestrator", intent="analyze")
        entry_span.input_summary = f"tenant={state.tenant_id} ticket_len={len(state.ticket)}"

        try:
            state = self._step_triage(state, request)
            state = self._step_retrieval(state, request)
            state = self._merge_triage_retrieval(state)
            state = self._compute_confidence(state)
            state = self._step_safety(state, request)
            state = self._step_rca(state, request)
            state = self._step_resolution(state, request)
            state = self._step_learning(state)
            state.completed = True

            rca = state.rca
            entry_span.finish(
                output_summary=f"cls={rca.final_classification if rca else 'unknown'} spans={len(state.trace_spans)}",
                decision="completed",
            )
        except Exception as exc:
            logger.exception("orchestrator.run failed: %s", exc)
            state.error = str(exc)
            entry_span.finish(output_summary=str(exc), status="error", decision="error")

        state.trace_spans.insert(0, entry_span.to_dict())
        return state
