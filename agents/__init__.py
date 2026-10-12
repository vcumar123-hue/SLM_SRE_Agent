# Modular agent routers — CQ-01
# Each module defines an APIRouter registered in app.py via include_router()

# Agentic orchestration contracts (Phase 0)
from agents.models import (
    TriageInput, TriageOutput,
    RetrievalInput, RetrievalOutput,
    SafetyInput, SafetyOutput,
    RCAInput, RCAOutput,
    ResolutionInput, ResolutionOutput,
    LearningInput, LearningOutput,
    OrchestratorState,
)
from agents.a2a_protocol import TraceSpan

__all__ = [
    "TriageInput", "TriageOutput",
    "RetrievalInput", "RetrievalOutput",
    "SafetyInput", "SafetyOutput",
    "RCAInput", "RCAOutput",
    "ResolutionInput", "ResolutionOutput",
    "LearningInput", "LearningOutput",
    "OrchestratorState",
    "TraceSpan",
]
