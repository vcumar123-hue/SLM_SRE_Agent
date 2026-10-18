"""
Reconciliation of an investigation with the deterministic result (design 10.2). Pure: it returns a new response and never changes its inputs.

| Investigation | Result |
|---|---|
| `supported`, and its classification is the same (or it names none) | merge: verified supported causes first (up to 3, each with its evidence ids), then the deterministic causes not contradicted |
| `supported`, a different classification | conservative rule: the deterministic classification is kept; the loop's alternative and its evidence are attached; `escalate` is set and the response is marked `conflict`; an operator resolves it. There is no automatic override |
| anything else (escalated, failed, timeout, budget, denied) | the deterministic result is kept unchanged (the investigation is attached for the operator) |

The original deterministic analysis is always kept in `investigation.deterministic`. The loop's classification is read from the evidence its supported hypotheses cite
(the rule planner states causes, not a classification), and is `None` when none of that evidence names a real one: then the causes are merged and the classification
is untouched.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from agentruntime.contracts import AgentRunResult

MAX_LOOP_CAUSES = 3
MAX_CAUSES = 5
EXCERPT_CHARS = 300
_NO_CLASS = {"", "unknown", "unclassified", "unclassified_issue", "general", "general_issue", "none", "other"}


def _fold(text: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def loop_classification(run: AgentRunResult) -> Optional[str]:
    """The most common real classification among the evidence cited by the verified supported hypotheses; None if there is none."""
    index = {e.evidence_id: e for e in run.evidence}
    votes: Counter = Counter()
    for h in run.hypotheses:
        if h.status == "supported" and h.verified:
            for eid in h.evidence:
                item = index.get(eid)
                name = _fold((item.facts or {}).get("classification") if item else "").replace(" ", "_")
                if name and name not in _NO_CLASS:
                    votes[name] += 1
    return votes.most_common(1)[0][0] if votes else None


def investigation_block(run: AgentRunResult) -> Dict[str, Any]:
    """The additive `investigation` object of the response (design 10.4): what an operator needs to audit the run, sanitised as stored."""
    return {
        "run_id": run.run_id, "status": run.status, "reason": run.reason, "mode": run.mode,
        "steps": [{"n": s.n, "kind": s.kind, "tool": s.tool, "tool_input": s.tool_input, "result_summary": s.result_summary, "evidence_added": s.evidence_added,
                   "error": s.error, "skipped": s.skipped, "duration_ms": s.duration_ms} for s in run.steps],
        "evidence": [{"evidence_id": e.evidence_id, "source": f"{e.source_type}:{e.source_id}", "tool": e.tool, "provenance": e.provenance, "relevance": e.relevance,
                      "excerpt": e.excerpt[:EXCERPT_CHARS], "flags": list(e.flags)} for e in run.evidence],
        "hypotheses": [h.model_dump(mode="json") for h in run.hypotheses], "counters": dict(run.counters), "versions": dict(run.versions),
        "evidence_grounding": run.evidence_grounding,
    }


def reconcile(response: Dict[str, Any], run: AgentRunResult) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(new response, summary). The summary says what happened: outcome in {merged, conflict, kept}, and the two classifications."""
    out = copy.deepcopy(response)
    if not isinstance(out.get("analysis"), dict):
        out["analysis"] = {}
    analysis = out["analysis"]
    det_class = str(analysis.get("classification") or "").strip().lower().replace(" ", "_")
    det_causes: List[str] = [str(c) for c in (analysis.get("probable_causes") or [])]
    block = investigation_block(run)
    block["deterministic"] = {"classification": analysis.get("classification"), "probable_causes": list(det_causes), "escalate": analysis.get("escalate")}
    supported = [h for h in run.hypotheses if h.status == "supported" and h.verified]
    summary: Dict[str, Any] = {"outcome": "kept", "deterministic_classification": det_class or None, "loop_classification": None}
    if run.evidence_grounding is not None:
        out.setdefault("confidence", {})["evidence_grounding"] = run.evidence_grounding

    if run.status == "supported" and supported:
        loop_class = loop_classification(run)
        summary["loop_classification"] = loop_class
        if loop_class and det_class and loop_class != det_class:
            analysis["escalate"] = True
            block["conflict"] = {"deterministic_classification": det_class, "loop_classification": loop_class, "resolution": "operator",
                                 "loop_causes": [{"cause": h.cause, "evidence": list(h.evidence)} for h in supported[:MAX_LOOP_CAUSES]]}
            summary["outcome"] = "conflict"
        else:
            rejected = {_fold(h.cause) for h in run.hypotheses if h.status == "rejected"}
            loop_causes = [h for h in supported[:MAX_LOOP_CAUSES]]
            merged = [h.cause for h in loop_causes]
            seen = {_fold(c) for c in merged}
            for cause in det_causes:
                key = _fold(cause)
                if key and key not in seen and key not in rejected:
                    seen.add(key)
                    merged.append(cause)
            analysis["probable_causes"] = merged[:MAX_CAUSES]
            block["cause_evidence"] = {h.cause: list(h.evidence) for h in loop_causes}
            summary["outcome"] = "merged"
    out["investigation"] = block
    return out, summary
