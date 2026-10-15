"""
Golden / synthetic calibration tickets.

They must be approved by the two named reviewers and are **calibration-only**: they are never part of the acceptance set. The file
(`data/golden_tickets.json`) is a list of {ticket_id, text, host, opened_at, set: "calibration", approved_by: [name, name], tenant_id?}
(`tenant_id` names the tenant whose knowledge the ticket is run against; default `poc_client`). Loading refuses any entry that is not
approved by **both reviewers named in the reviewer record** (and only them), that is not marked calibration, or whose id collides.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from planner_experiment.freeze_names import PLACEHOLDER_NAMES


class GoldenError(Exception):
    pass


def load_golden(path: Path, reviewers: List[str]) -> List[Dict[str, Any]]:
    return parse_golden(json.loads(Path(path).read_text(encoding="utf-8")), reviewers)


def parse_golden(raw: List[Dict[str, Any]], reviewers: List[str]) -> List[Dict[str, Any]]:
    """`reviewers` is required: every approval must come from the two reviewers named in the reviewer record, not from any two names."""
    if not reviewers or len({r.lower() for r in reviewers}) != 2:
        raise GoldenError("golden tickets can only be loaded against the two named reviewers of the reviewer record")
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in raw:
        tid = str(item.get("ticket_id") or "")
        if not tid.startswith("golden-"):
            raise GoldenError(f"{tid or '(no id)'}: golden ticket ids start with 'golden-'")
        if tid in seen:
            raise GoldenError(f"{tid}: duplicate id")
        seen.add(tid)
        if item.get("set") != "calibration":
            raise GoldenError(f"{tid}: golden tickets are calibration-only (set must be 'calibration')")
        approvers = [str(a).strip() for a in item.get("approved_by") or []]
        if len({a.lower() for a in approvers}) < 2 or any(a.lower() in PLACEHOLDER_NAMES for a in approvers):
            raise GoldenError(f"{tid}: approval by two distinct named reviewers is required")
        if {a.lower() for a in approvers} != {r.lower() for r in reviewers}:
            raise GoldenError(f"{tid}: must be approved by both named reviewers ({', '.join(reviewers)}), and only by them")
        for field in ("text", "host", "opened_at"):
            if not str(item.get(field) or "").strip():
                raise GoldenError(f"{tid}: '{field}' is required")
        out.append({**item, "pool": "golden", "tenant_id": str(item.get("tenant_id") or "poc_client")})
    return out
