"""
The engagement rule (design 3.2): when is an investigation worth running for a response that has just been built?

Engage when any of: the confidence is below the application's threshold; the retrieval quality is below its minimum; there is no reusable knowledge (no linked
resolution and no linked playbook); or the leading retrieved incidents disagree about the classification. (The design's "high impact with an uncertain cause" needs an
impact field the response does not carry; it is not engaged on, and that is stated rather than guessed.) The loop never replaces the deterministic result, so
engaging is cheap to get wrong in the safe direction: a run that was not needed is only a wasted run.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

CONFLICT_GAP = 0.05


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def conflicting_leaders(incidents: List[Dict[str, Any]], min_score: float, gap: float = CONFLICT_GAP) -> bool:
    """True when the two best-scoring incidents that clear the retrieval minimum name different classifications within `gap` of each other."""
    ranked = sorted((i for i in incidents or [] if isinstance(i, dict) and _num(i.get("score")) >= min_score), key=lambda i: -_num(i.get("score")))
    named = [(_num(i.get("score")), str(i.get("classification") or "").strip().lower()) for i in ranked]
    named = [n for n in named if n[1] and n[1] not in ("unknown", "unclassified_issue", "general_issue")]
    if len(named) < 2:
        return False
    top_score, top_class = named[0]
    return any(cls != top_class and top_score - score < gap for score, cls in named[1:])


def should_engage(response: Dict[str, Any], *, confidence_threshold: float, min_retrieval_score: float) -> Tuple[bool, List[str]]:
    if not isinstance(response, dict) or not str(response.get("ticket") or "").strip():
        return False, ["no ticket text"]
    conf = response.get("confidence") if isinstance(response.get("confidence"), dict) else {}
    reasons: List[str] = []
    if _num(conf.get("overall_confidence")) < confidence_threshold:
        reasons.append("confidence_below_threshold")
    if _num(conf.get("retrieval_quality")) < min_retrieval_score:
        reasons.append("retrieval_quality_below_minimum")
    if not response.get("linked_resolution") and not response.get("linked_playbook"):
        reasons.append("no_reusable_knowledge")
    if conflicting_leaders(response.get("retrieved_incidents") or [], min_retrieval_score):
        reasons.append("conflicting_evidence")
    return bool(reasons), reasons
