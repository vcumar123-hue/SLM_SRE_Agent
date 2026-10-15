"""
Deterministic evidence verifier and grounding (design 5.3 and 10.3).

The verifier decides only whether a cited evidence item is a legitimate item of this run. Whether it semantically supports the
claim is judged by reviewers offline (design 13.4), never by this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from agentruntime.context import TrustedContext
from agentruntime.contracts import AgentRunResult, EvidenceItem, Hypothesis, evidence_hash

MISSING_WEIGHT = 0.25     # per unanswered required check; tuned on the calibration set only


def is_stale(item: EvidenceItem, now: Optional[datetime] = None) -> bool:
    if item.ttl_days is None or item.data_timestamp is None:
        return False
    now = now or datetime.now(timezone.utc)
    stamp = item.data_timestamp if item.data_timestamp.tzinfo else item.data_timestamp.replace(tzinfo=timezone.utc)
    return stamp + timedelta(days=item.ttl_days) < now


def verify_evidence_id(
    evidence_id: str,
    index: Dict[str, EvidenceItem],
    ctx: TrustedContext,
    run_id: str,
    allowed_tools: Iterable[str],
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    item = index.get(evidence_id)
    if item is None:
        return False, "unknown evidence id"
    if item.run_id != run_id:
        return False, "evidence belongs to another run"
    if item.tenant_id != ctx.tenant_id:
        return False, "evidence belongs to another tenant"
    if item.tool not in set(allowed_tools):
        return False, "evidence was not returned by an allowed tool"
    if not item.sanitization:
        return False, "evidence was not sanitised"
    if is_stale(item, now):
        return False, "evidence is past its freshness horizon"
    if evidence_hash(item.excerpt, item.facts) != item.content_hash:
        return False, "evidence content does not match its hash"
    return True, ""


def verify_hypotheses(
    hypotheses: List[Hypothesis],
    evidence: List[EvidenceItem],
    ctx: TrustedContext,
    run_id: str,
    allowed_tools: Iterable[str],
    now: Optional[datetime] = None,
) -> List[Hypothesis]:
    """Set `verified` and demote (design 10.1): a supported hypothesis needs at least one verified evidence id."""
    index = {e.evidence_id: e for e in evidence}
    allowed = list(allowed_tools)
    out: List[Hypothesis] = []
    for h in hypotheses:
        results = [verify_evidence_id(eid, index, ctx, run_id, allowed, now) for eid in h.evidence]
        any_valid = any(ok for ok, _ in results)
        any_invalid = any(not ok for ok, _ in results)
        status = h.status
        verified = False
        notes = h.notes
        if h.status == "supported":
            if any_valid:
                verified = True
            else:
                status = "weak"
                notes = (notes + " " if notes else "") + "no verified evidence"
            if any_invalid and any_valid:
                notes = (notes + " " if notes else "") + "some cited evidence failed verification"
        out.append(h.model_copy(update={"status": status, "verified": verified, "notes": notes}))
    return out


def compute_grounding(result: AgentRunResult, now: Optional[datetime] = None) -> Optional[float]:
    """evidence_grounding in [0, 1] (design 10.3). None when there is no verified supported hypothesis."""
    supported = [h for h in result.hypotheses if h.status == "supported" and h.verified]
    if not supported:
        return None
    index = {e.evidence_id: e for e in result.evidence}

    def weight(item: EvidenceItem) -> float:
        if is_stale(item, now):
            return 0.0
        return float(item.relevance if item.relevance is not None else 0.5)

    cited = {eid for h in supported for eid in h.evidence if eid in index}
    positive = sum(weight(index[eid]) for eid in cited)
    contradicting = sum(
        weight(index[eid]) for h in result.hypotheses if h.status == "rejected" for eid in h.evidence if eid in index
    )
    denominator = positive + contradicting + MISSING_WEIGHT * 0  # no required-check list yet; constant applied when checks exist
    return round(positive / denominator, 4) if denominator > 0 else 0.0
