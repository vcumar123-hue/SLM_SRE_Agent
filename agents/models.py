from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

# Agentic orchestration contracts only. HTTP request models belong in root models.py.


class TriageInput(BaseModel):
    ticket: str
    tenant_id: str = "default"
    knowledge_scope: str = "client_private"
    request_id: str = ""


class TriageOutput(BaseModel):
    domain: str
    classification: str
    classification_certainty: float
    llm_score: float = 0.0
    retrieval_locked_classification: str = ""
    retrieval_locked_score: float = 0.0
    intents: Dict[str, Any] = Field(default_factory=dict)
    feedback_override: Optional[Dict[str, Any]] = None


class RetrievalInput(BaseModel):
    ticket: str
    tenant_id: str = "default"
    knowledge_scope: str = "client_private"
    top_k: int = 5
    request_id: str = ""


class RetrievalOutput(BaseModel):
    incidents: List[Dict[str, Any]] = Field(default_factory=list)
    embedding_score: float = 0.0
    learned_feedback_score: float = 0.0
    feedback_override: Optional[Dict[str, Any]] = None
    retrieval_quality: float = 0.0
    retrieval_scope: str = ""
    rag_context: str = ""


class SafetyInput(BaseModel):
    ticket: str
    classification: str
    primary_confidence: float
    incidents: List[Dict[str, Any]] = Field(default_factory=list)
    linked_resolution: Optional[Dict[str, Any]] = None
    linked_playbook: Optional[Dict[str, Any]] = None
    confirm_local_matches: Optional[bool] = None
    use_fallback: bool = True


class SafetyOutput(BaseModel):
    force_fallback: bool = False
    force_fallback_reason: Optional[str] = None
    confirmation_required: bool = False
    gate_decision: str = "proceed"   # proceed | hold | escalate


class RCAInput(BaseModel):
    ticket: str
    domain: str
    classification: str
    classification_certainty: float
    retrieval_locked_classification: str = ""
    retrieval_locked_score: float = 0.0
    incidents: List[Dict[str, Any]] = Field(default_factory=list)
    rag_context: str = ""
    primary_confidence: float = 0.0
    confidence_context: Dict[str, Any] = Field(default_factory=dict)
    linked_resolution: Optional[Dict[str, Any]] = None
    linked_playbook: Optional[Dict[str, Any]] = None
    use_fallback: bool = True
    force_fallback: bool = False
    force_fallback_reason: Optional[str] = None
    tenant_id: str = "default"
    knowledge_scope: str = "client_private"
    request_id: str = ""


class RCAOutput(BaseModel):
    final_classification: str
    domain: str
    analysis: Dict[str, Any] = Field(default_factory=dict)
    fallback_used: bool = False
    fallback_provider: Optional[str] = None
    fallback_model: Optional[str] = None
    fallback_source: Optional[str] = None
    confidence_info: Dict[str, Any] = Field(default_factory=dict)
    response_source: str = "local_primary"
    analysis_source_label: str = ""
    decision: str = "local_primary"
    candidate_record: Optional[Dict[str, Any]] = None
    routed_intents: Dict[str, Any] = Field(default_factory=dict)


class ResolutionInput(BaseModel):
    classification: str
    final_classification: str
    domain: str
    analysis: Dict[str, Any] = Field(default_factory=dict)
    fallback_used: bool = False
    fallback_provider: Optional[str] = None
    fallback_model: Optional[str] = None
    candidate_record: Optional[Dict[str, Any]] = None
    tenant_id: str = "default"
    knowledge_scope: str = "client_private"
    decision: str = "local_primary"


class ResolutionOutput(BaseModel):
    linked_resolution: Optional[Dict[str, Any]] = None
    linked_playbook: Optional[Dict[str, Any]] = None
    candidate_record: Optional[Dict[str, Any]] = None
    decision: str = "local_primary"


class LearningInput(BaseModel):
    classification: str
    tenant_id: str = "default"
    incident_id: str = ""
    resolution_id: str = ""
    playbook_id: str = ""
    execution_mode: str = "manual"
    outcome_status: str = "resolved"
    resolution_minutes: Optional[float] = None


class LearningOutput(BaseModel):
    outcome_id: str = ""
    pattern_signal: Dict[str, Any] = Field(default_factory=dict)
    memory_updated: bool = False


class OrchestratorState(BaseModel):
    """Full state carried through an orchestrated analysis request."""
    request_id: str = ""
    tenant_id: str = "default"
    ticket: str = ""
    triage: Optional[TriageOutput] = None
    retrieval: Optional[RetrievalOutput] = None
    safety: Optional[SafetyOutput] = None
    rca: Optional[RCAOutput] = None
    resolution: Optional[ResolutionOutput] = None
    learning: Optional[LearningOutput] = None
    confidence_info: Dict[str, Any] = Field(default_factory=dict)
    trace_spans: List[Dict[str, Any]] = Field(default_factory=list)
    completed: bool = False
    error: Optional[str] = None
