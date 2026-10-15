"""
Annotations (design 12): per ticket, the acceptable first tools, the evidence required to support the correct cause, and the correct end
state. Made independently by two reviewers; disagreements are adjudicated by the owner; the adjudicated file is what is frozen.

A cause is judged by keywords, not free text: `cause_keywords` is a list of groups, and a hypothesis matches when every group has at least
one of its words in the cause text. That keeps scoring mechanical and reproducible.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentruntime.tools import TOOL_NAMES

END_STATES = ("supported", "escalate")


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    reviewer: str
    acceptable_first_tools: List[str] = Field(min_length=1)
    required_evidence: List[str] = Field(default_factory=list)        # "source_type:source_id" references that must be gathered
    end_state: str
    cause_keywords: List[List[str]] = Field(default_factory=list)     # required when end_state == "supported"
    notes: str = ""

    @field_validator("acceptable_first_tools")
    @classmethod
    def _tools(cls, v: List[str]) -> List[str]:
        bad = [t for t in v if t not in TOOL_NAMES]
        if bad:
            raise ValueError(f"unknown tool(s): {bad}")
        return v

    @field_validator("end_state")
    @classmethod
    def _end(cls, v: str) -> str:
        if v not in END_STATES:
            raise ValueError(f"end_state must be one of {END_STATES}")
        return v

    def check(self) -> None:
        if self.end_state == "supported" and not any(self.cause_keywords):
            raise ValueError(f"{self.case_id}: a 'supported' end state needs cause_keywords")
        if self.end_state == "supported" and not self.required_evidence:
            raise ValueError(f"{self.case_id}: a 'supported' end state needs at least one required_evidence reference")
        if self.end_state == "escalate" and self.cause_keywords:
            raise ValueError(f"{self.case_id}: an 'escalate' end state has no cause")
        for ref in self.required_evidence:
            if ":" not in ref:
                raise ValueError(f"{self.case_id}: evidence reference '{ref}' must look like source_type:source_id")


def parse(raw: List[Dict[str, Any]]) -> List[Annotation]:
    items = [Annotation(**r) for r in raw]
    for a in items:
        a.check()
    return items


def load(path: Path) -> List[Annotation]:
    return parse(json.loads(Path(path).read_text(encoding="utf-8")))


def template_for(case_ids: Sequence[str], reviewer: str) -> List[Dict[str, Any]]:
    """An empty annotation sheet for a reviewer. It contains case ids only, never ticket text (reviewers get text through their own view)."""
    return [{"case_id": c, "reviewer": reviewer, "acceptable_first_tools": [], "required_evidence": [], "end_state": "",
             "cause_keywords": [], "notes": ""} for c in case_ids]


def cause_matches(cause: str, groups: List[List[str]]) -> bool:
    text = (cause or "").lower()
    return bool(groups) and all(any(w.lower() in text for w in group) for group in groups if group)


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> Optional[float]:
    if not a or len(a) != len(b):
        return None
    n = len(a)
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    return 1.0 if expected == 1.0 else (observed - expected) / (1.0 - expected)


def _cause_signature(a: Annotation) -> frozenset:
    """The cause an annotation expects, independent of group order, word order and case: a set of word-sets."""
    return frozenset(frozenset(w.strip().lower() for w in group if w.strip()) for group in a.cause_keywords if group)


def agreement(first: List[Annotation], second: List[Annotation]) -> Dict[str, Any]:
    """Agreement between two reviewers on the cases both annotated, and the list of disagreements for adjudication."""
    b = {a.case_id: a for a in second}
    shared = [a for a in first if a.case_id in b]
    disagreements: List[Dict[str, Any]] = []
    for a in shared:
        o = b[a.case_id]
        diffs = []
        if a.end_state != o.end_state:
            diffs.append("end_state")
        if set(a.acceptable_first_tools) != set(o.acceptable_first_tools):
            diffs.append("acceptable_first_tools")
        if set(a.required_evidence) != set(o.required_evidence):
            diffs.append("required_evidence")
        if _cause_signature(a) != _cause_signature(o):
            diffs.append("cause_keywords")
        if diffs:
            disagreements.append({"case_id": a.case_id, "fields": diffs})
    return {"cases_compared": len(shared),
            "end_state_kappa": cohen_kappa([a.end_state for a in shared], [b[a.case_id].end_state for a in shared]),
            "first_tool_exact_agreement": (sum(1 for a in shared if set(a.acceptable_first_tools) == set(b[a.case_id].acceptable_first_tools))
                                           / len(shared)) if shared else None,
            "disagreements": disagreements}
