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


ACCEPTANCE_ID_PREFIX = "golden-acc-"
ACCEPTANCE_FILE_NAME = "acceptance_golden_tickets.json"   # reserved; read only by the acceptance freeze (13.9), never by development tooling


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
        if tid.startswith(ACCEPTANCE_ID_PREFIX):
            raise GoldenError(f"{tid}: ids starting '{ACCEPTANCE_ID_PREFIX}' are reserved for the acceptance set and can never be calibration tickets")
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


def parse_acceptance_golden(raw: List[Dict[str, Any]], reviewers: List[str], approvers: List[str], excluded_people: List[str] = ()) -> List[Dict[str, Any]]:
    """The acceptance set's separately held golden/synthetic component (design 13.2: 20% of the 100). Used only at the acceptance freeze.

    Acceptance goldens are acceptance-only cases: ids start 'golden-acc-', set is 'acceptance', and they are prepared independently of the calibration goldens (see
    `check_separation`) and never visible to planner development. Their content is approved by one or two named content approvers who are **not** the labelling reviewers and not
    any of `excluded_people` (the adjudicator and the package custodian): the reviewers label the acceptance set blind, so they must not have seen any part of it, including its
    synthetic tickets, before the freeze (review 2026-10-18; amendment A-1). Approval is recorded **per ticket** (`approved_by`, `approved_on`), and the approver must have been
    shown, and the ticket must carry, the expected causes (1-3) and the evidence a correct answer needs, so the approver reviews them before the freeze.
    """
    if not reviewers or len({r.lower() for r in reviewers}) != 2:
        raise GoldenError("acceptance golden tickets can only be loaded against the two named reviewers")
    content_approvers = {str(a).strip().lower() for a in approvers if str(a).strip()}
    if not 1 <= len(content_approvers) <= 2:
        raise GoldenError("acceptance golden tickets need one or two named content approvers")
    barred = {r.lower() for r in reviewers} | {str(p).strip().lower() for p in excluded_people if str(p).strip()}
    if content_approvers & barred:
        raise GoldenError("the content approvers of the acceptance goldens must not be a labelling reviewer, the adjudicator or the package custodian (none of them may see the acceptance set before the freeze)")
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in raw:
        tid = str(item.get("ticket_id") or "")
        if not tid.startswith(ACCEPTANCE_ID_PREFIX) or tid in seen:
            raise GoldenError(f"{tid or '(no id)'}: acceptance golden ids start with '{ACCEPTANCE_ID_PREFIX}' and are unique")
        seen.add(tid)
        if item.get("set") != "acceptance":
            raise GoldenError(f"{tid}: set must be 'acceptance'")
        given = {str(a).strip().lower() for a in item.get("approved_by") or []}
        if given != content_approvers:
            raise GoldenError(f"{tid}: must be approved by the named content approver(s), and only by them, recorded on the ticket itself")
        if not str(item.get("approved_on") or "").strip():
            raise GoldenError(f"{tid}: the date of the approval is required on every ticket")
        causes = [str(c).strip() for c in item.get("expected_causes") or [] if str(c).strip()]
        if not 1 <= len(causes) <= 3:
            raise GoldenError(f"{tid}: 1 to 3 expected causes are required, so the approver reviews them before the freeze")
        if not [e for e in item.get("required_evidence") or [] if str(e).strip()]:
            raise GoldenError(f"{tid}: the evidence a correct answer needs must be stated")
        for field in ("text", "host", "opened_at"):
            if not str(item.get(field) or "").strip():
                raise GoldenError(f"{tid}: '{field}' is required")
        out.append({**item, "pool": "golden", "tenant_id": str(item.get("tenant_id") or "poc_client"), "source": "golden"})
    return out


def check_separation(calibration: List[Dict[str, Any]], acceptance: List[Dict[str, Any]]) -> None:
    """Acceptance goldens must be disjoint from calibration goldens: no shared id, no identical or near-identical text (same normalised
    text pattern), no shared host, so nothing tuned on a calibration golden can have seen an acceptance golden."""
    from planner_experiment import select_cases as sc

    cal_ids = {str(c["ticket_id"]) for c in calibration}
    cal_text = {sc.normalise(str(c["text"])) for c in calibration}
    cal_hosts = {str(c["host"]).strip().lower() for c in calibration}
    problems: List[str] = []
    for a in acceptance:
        if str(a["ticket_id"]) in cal_ids:
            problems.append(f"{a['ticket_id']}: id also used by a calibration golden")
        if sc.normalise(str(a["text"])) in cal_text:
            problems.append(f"{a['ticket_id']}: text pattern equals a calibration golden's")
        if str(a["host"]).strip().lower() in cal_hosts:
            problems.append(f"{a['ticket_id']}: host shared with a calibration golden")
    if problems:
        raise GoldenError("; ".join(problems))
