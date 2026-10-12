from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

# ── Agentic orchestration contracts (Phase 0) ────────────────────────────────
# Typed inputs and outputs for each specialist agent.
# Existing request/response models below are unchanged.


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

_ANALYZE_MAX_TICKET_LEN = 32_000


class AnalyzeRequest(BaseModel):
    ticket: str = Field(..., min_length=1, max_length=_ANALYZE_MAX_TICKET_LEN)
    use_fallback: bool = True
    top_k: Optional[int] = Field(default=None, ge=1, le=50)
    force_fallback: bool = False
    confirm_local_matches: Optional[bool] = None
    tenant_id: Optional[str] = Field(default="client_a", max_length=64)
    knowledge_scope: Optional[str] = Field(default="client_private", max_length=64)


class FeedbackRequest(BaseModel):
    ticket: str = Field(..., min_length=1)
    original_domain: Optional[str] = None
    original_classification: Optional[str] = None
    corrected_classification: str = Field(..., min_length=1)
    submitted_domain: Optional[str] = None
    is_correct: bool = True
    request_id: Optional[str] = None


class ReviewCandidateRequest(BaseModel):
    candidate_id: str
    reviewed_by: str
    approved: bool
    review_notes: Optional[str] = ""
    updated_classification: Optional[str] = None
    updated_probable_causes: Optional[List[str]] = None
    updated_next_checks: Optional[List[str]] = None
    updated_recommended_actions: Optional[List[str]] = None


class PromoteCandidateRequest(BaseModel):
    candidate_id: str


class CreateResolutionRequest(BaseModel):
    classification: str
    root_cause: str
    validated_checks: List[str]
    validated_actions: List[str]
    confirmed_by: str
    success_notes: str
    linked_candidate_id: Optional[str] = ""
    automation_ready: Optional[bool] = False
    linked_playbook_id: Optional[str] = ""
    incident_id: Optional[str] = ""
    tenant_id: Optional[str] = Field(default=None, max_length=64)


class CreatePlaybookRequest(BaseModel):
    name: str
    classification: str
    description: str
    steps: List[str]
    scripts: List[str]
    approval_mode: Optional[str] = "manual_approval_required"
    rollback_available: Optional[bool] = True
    risk_tier: Optional[str] = "medium"


class FallbackControlRequest(BaseModel):
    enabled: bool


class TelemetryEventRequest(BaseModel):
    metric: str = Field(..., min_length=1)
    value: float
    resource: Optional[str] = ""
    service: Optional[str] = ""
    environment: Optional[str] = "production"
    source: Optional[str] = "manual"
    labels: Optional[Dict[str, Any]] = None


class TelemetryPromoteRequest(BaseModel):
    limit: Optional[int] = None


class AutomationPlanRequest(BaseModel):
    incident_id: str = Field(..., min_length=1)
    classification: str = Field(..., min_length=1)
    linked_resolution: Optional[Dict[str, Any]] = None
    linked_playbook: Optional[Dict[str, Any]] = None
    requested_by: Optional[str] = "system"


class AutomationApproveRequest(BaseModel):
    queue_id: str = Field(..., min_length=1)
    approved_by: Optional[str] = "engineer"


class AutomationExecuteRequest(BaseModel):
    queue_id: str = Field(..., min_length=1)
    executed_by: Optional[str] = "automation_engine"
    execution_mode: Optional[str] = "simulation"
    # Deprecated and ignored for authorization: autonomous execution is determined server-side from
    # the service identity, the registry mapping and the queue entry's server-created intent.
    autonomous_requested: bool = False
    # Explicit human override of Safety Gate warnings for live execution (role l3+, reason required).
    override_gate_warnings: bool = False
    override_reason: Optional[str] = ""


class AutomationPreExecutionCheckRequest(BaseModel):
    queue_id: Optional[str] = ""
    incident_id: Optional[str] = ""
    classification: Optional[str] = ""
    playbook: Optional[str] = ""
    target_host: Optional[str] = ""
    risk_tier: Optional[str] = "medium"
    execution_mode: Optional[str] = "simulation"
    requested_by: Optional[str] = "engineer"


class AutomationExecutionFeedbackRequest(BaseModel):
    queue_id: Optional[str] = ""
    incident_id: Optional[str] = ""
    classification: Optional[str] = ""
    action: Optional[str] = ""
    playbook: Optional[str] = ""
    target_host: Optional[str] = ""
    execution_status: Optional[str] = "completed"
    execution_mode: Optional[str] = "simulation"
    resolved_by_automation: Optional[bool] = None
    additional_steps_required: Optional[bool] = False
    playbook_review_required: Optional[bool] = False
    engineer_notes: Optional[str] = ""
    submitted_by: Optional[str] = "engineer"
    safety_gate_status: Optional[str] = ""
    rollback_available: Optional[bool] = True
    risk_tier: Optional[str] = "medium"


class AutomationConfidenceSummaryRequest(BaseModel):
    classification: Optional[str] = ""
    playbook: Optional[str] = ""
    action: Optional[str] = ""


class AutomationAutonomousEnableRequest(BaseModel):
    classification: str = Field(..., min_length=1)
    action: Optional[str] = ""
    playbook: Optional[str] = ""
    target_host: Optional[str] = ""
    enabled: Optional[bool] = True
    requested_by: Optional[str] = "engineer"
    notes: Optional[str] = ""
    tenant_id: Optional[str] = "default"


class AutomationAutonomousEligibilityRequest(BaseModel):
    classification: str = Field(..., min_length=1)
    action: Optional[str] = ""
    playbook: Optional[str] = ""
    tenant_id: Optional[str] = "default"

class AutomationRefreshRequest(BaseModel):
    queue_id: str = Field(..., min_length=1)

class AutomationSuggestMappingRequest(BaseModel):
    classification: str = Field(..., min_length=1)
    linked_resolution: Optional[Dict[str, Any]] = None
    linked_playbook: Optional[Dict[str, Any]] = None


class AutomationLearnMappingRequest(BaseModel):
    classification: str = Field(..., min_length=1)
    actions: Optional[List[str]] = None
    approved_actions: Optional[List[str]] = None
    learned_by: Optional[str] = "engineer"
    approved_by: Optional[str] = None
    source: Optional[str] = "manual_approval"
    source_ticket: Optional[str] = None


class AutomationCandidateCreateRequest(BaseModel):
    incident_id: str = ""
    classification: str = Field(..., min_length=1)
    draft_runbook: Optional[Dict[str, Any]] = None
    draft_steps: Optional[List[Any]] = None
    suggested_action: Optional[str] = ""
    suggested_playbook: Optional[str] = ""
    target_host: Optional[str] = ""
    target_group: Optional[str] = ""
    risk_tier: Optional[str] = "medium"
    approval_required: Optional[bool] = True
    created_by: Optional[str] = "engineer"
    source: Optional[str] = "ui_draft_from_analysis"
    notes: Optional[str] = ""


class AutomationCandidateUpdateRequest(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    status: Optional[str] = None
    suggested_action: Optional[str] = None
    suggested_playbook: Optional[str] = None
    target_host: Optional[str] = None
    target_group: Optional[str] = None
    risk_tier: Optional[str] = None
    approval_required: Optional[bool] = None
    updated_by: Optional[str] = "engineer"
    notes: Optional[str] = None


class AutomationCandidateTestResultRequest(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    test_status: str = Field(..., min_length=1)
    tested_by: Optional[str] = "engineer"
    execution_mode: Optional[str] = "simulation"
    stdout: Optional[str] = ""
    stderr: Optional[str] = ""
    returncode: Optional[int] = None
    notes: Optional[str] = ""


class AutomationCandidateApproveRequest(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    approved_by: Optional[str] = "engineer"
    approval_notes: Optional[str] = ""


class AutomationCandidateEnableRequest(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    enabled_by: Optional[str] = "engineer"


class AutomationCandidateGeneratePlaybookRequest(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    generated_by: Optional[str] = "engineer"
    action: Optional[str] = None
    playbook: Optional[str] = None
    target_host: Optional[str] = None


class AutomationCandidateSavePlaybookRequest(BaseModel):
    candidate_id: str = Field(..., min_length=1)
    playbook_content: Optional[str] = None
    reviewed_by: Optional[str] = "engineer"
    review_notes: Optional[str] = ""
    overwrite: Optional[bool] = True


class EvidenceSubmitRequest(BaseModel):
    evidence_type: str
    service: Optional[str] = None
    environment: Optional[str] = None
    resource: Optional[str] = None
    status: str
    severity: Optional[str] = None
    source_system: Optional[str] = None
    summary: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    checked_at: Optional[str] = None
    confidence: Optional[float] = None
