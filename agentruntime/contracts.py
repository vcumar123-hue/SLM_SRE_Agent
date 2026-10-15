"""Data contracts for the investigation runtime (design section 6)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# -- budgets (design 7.1): defaults and non-overridable hard maxima ---------------------------------------------------------------

BUDGET_DEFAULTS: Dict[str, float] = {
    "max_steps": 8, "max_tool_calls": 5, "max_model_calls": 5,
    "model_call_timeout_s": 15, "tool_timeout_s": 6, "deadline_s": 40,
    "max_tokens_planner": 300, "max_tokens_synthesis": 900, "max_tokens_run": 4000,
    "max_result_chars": 4000, "max_evidence_items": 20, "max_reformulations": 1, "no_progress_limit": 2,
}
BUDGET_HARD_MAX: Dict[str, float] = {
    "max_steps": 10, "max_tool_calls": 6, "max_model_calls": 7,
    "model_call_timeout_s": 30, "tool_timeout_s": 10, "deadline_s": 45,
    "max_tokens_planner": 400, "max_tokens_synthesis": 1200, "max_tokens_run": 6000,
    "max_result_chars": 6000, "max_evidence_items": 30, "max_reformulations": 1, "no_progress_limit": 3,
}

RunStatus = Literal[
    "supported", "escalated_insufficient", "escalated_conflict", "denied_policy", "failed_model", "failed_tool",
    "failed_internal", "timeout_deadline", "budget_exhausted", "dropped", "cancelled",
]
Mode = Literal["off", "shadow", "on", "batch"]


class Budgets(BaseModel):
    """Run budgets. Every value is clamped into [1, hard maximum] on construction, so no caller can exceed a hard maximum."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int = 8
    max_tool_calls: int = 5
    max_model_calls: int = 5
    model_call_timeout_s: float = 15
    tool_timeout_s: float = 6
    deadline_s: float = 40
    max_tokens_planner: int = 300
    max_tokens_synthesis: int = 900
    max_tokens_run: int = 4000
    max_result_chars: int = 4000
    max_evidence_items: int = 20
    max_reformulations: int = 1
    no_progress_limit: int = 2

    def __init__(self, **data: Any) -> None:
        clamped: Dict[str, Any] = {}
        for key, value in data.items():
            if key in BUDGET_HARD_MAX:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    number = float(BUDGET_DEFAULTS[key])
                number = max(1.0, min(number, float(BUDGET_HARD_MAX[key])))
                clamped[key] = number if isinstance(BUDGET_DEFAULTS[key], float) or key.endswith("_s") else int(number)
            else:
                clamped[key] = value  # an unknown key is rejected by extra="forbid"
        super().__init__(**clamped)


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: Dict[str, Any]
    max_items: int = 5
    tier: Literal["read"] = "read"      # nothing above read exists in Release 2
    version: str = "1"


class Action(BaseModel):
    """One planner decision."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["tool", "finish", "escalate"]
    tool: Optional[str] = None
    input: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    hypotheses: List[Dict[str, Any]] = Field(default_factory=list)   # for finish: proposals {cause, status, evidence[], notes}
    analysis: Optional[Dict[str, Any]] = None                        # for finish: RCAOutput.analysis-compatible
    conflict: bool = False                                           # the planner reports unresolved conflict among leading hypotheses


class EvidenceDraft(BaseModel):
    """What a tool returns. The runtime assigns the id, run id and hash and sanitises the text."""
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    tool: str = ""
    tool_version: str = "1"
    source_type: Literal["incident", "ticket", "outcome", "pattern", "evidence_check"]
    source_id: str
    provenance: Literal["tenant_data", "shared_base", "learned", "computed"]
    data_timestamp: Optional[datetime] = None
    ttl_days: Optional[int] = Field(default=None, ge=0)
    relevance: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    excerpt: str = ""
    facts: Optional[Dict[str, Any]] = None


def evidence_hash(excerpt: str, facts: Optional[Dict[str, Any]]) -> str:
    payload = json.dumps({"excerpt": excerpt, "facts": facts}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvidenceItem(BaseModel):
    """An immutable, sanitised evidence object (design 5.1)."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    run_id: str
    tenant_id: str
    tool: str
    tool_version: str
    source_type: Literal["incident", "ticket", "outcome", "pattern", "evidence_check"]
    source_id: str
    provenance: Literal["tenant_data", "shared_base", "learned", "computed"]
    retrieved_at: datetime
    data_timestamp: Optional[datetime] = None
    ttl_days: Optional[int] = Field(default=None, ge=0)
    sanitization: Literal["clean", "redacted", "truncated"]
    relevance: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    excerpt: str
    facts: Optional[Dict[str, Any]] = None
    content_hash: str
    flags: List[str] = Field(default_factory=list)      # for example "instruction_like_text"


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: str
    status: Literal["supported", "weak", "rejected"]
    evidence: List[str] = Field(default_factory=list)
    verified: bool = False                # set by the runtime, never by the model
    notes: str = ""


class AgentStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int
    kind: Literal["plan", "tool", "verify", "synthesise"]
    tool: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    action: Optional[Dict[str, Any]] = None       # the planner decision that led here (needed for replay)
    result_summary: str = ""
    evidence_added: List[str] = Field(default_factory=list)
    duration_ms: int = 0
    model: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    error: Optional[str] = None
    skipped: bool = False


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    tenant_id: str
    status: RunStatus
    reason: str = ""
    hypotheses: List[Hypothesis] = Field(default_factory=list)
    evidence: List[EvidenceItem] = Field(default_factory=list)
    steps: List[AgentStep] = Field(default_factory=list)
    analysis: Optional[Dict[str, Any]] = None
    evidence_grounding: Optional[float] = None
    counters: Dict[str, Any] = Field(default_factory=dict)
    versions: Dict[str, Any] = Field(default_factory=dict)
    mode: str = "off"
    flags: List[str] = Field(default_factory=list)


class AgentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_tools: List[str]
    budgets: Budgets = Field(default_factory=Budgets)
    planner: Literal["rules_only", "rules_then_model", "model"] = "rules_then_model"
    mode: Mode = "off"            # off by default: a policy must opt in to shadow, on or batch

    def policy_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()[:16]
