"""
Recorded methodology amendments for R2-3 (docs/evaluation/amendments.json).

An amendment takes effect only through an approval entry that carries the owner's name and their own statement. An approval can be withdrawn (it stays on record, marked, with the reason);
an amendment is approved only while at least one of its approvals is not withdrawn. Nothing infers an approval: this module records exactly what it is given and refuses an empty statement.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

AMENDMENTS_PATH = Path(__file__).resolve().parent.parent / "docs" / "evaluation" / "amendments.json"


class AmendmentError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else AMENDMENTS_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AmendmentError(f"the amendments record cannot be read: {exc}") from exc


def _save(data: Dict[str, Any], path: Optional[Path]) -> None:
    p = Path(path) if path else AMENDMENTS_PATH
    p.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def _find(data: Dict[str, Any], amendment_id: str) -> Dict[str, Any]:
    for a in data.get("amendments", []):
        if a.get("id") == amendment_id:
            return a
    raise AmendmentError(f"no amendment {amendment_id!r}")


def is_approved(amendment_id: str, path: Optional[Path] = None) -> bool:
    try:
        a = _find(load(path), amendment_id)
    except AmendmentError:
        return False
    return any(not ap.get("withdrawn") and str(ap.get("approver") or "").strip() and len(str(ap.get("statement") or "").strip()) >= 20 for ap in a.get("approvals", []))


def approve(amendment_id: str, approver: str, statement: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Record an approval exactly as given. This records an approval; it cannot make one."""
    if not str(approver).strip() or len(str(statement).strip()) < 20:
        raise AmendmentError("an approval needs the approver's name and their own statement (at least a sentence)")
    data = load(path)
    a = _find(data, amendment_id)
    a.setdefault("approvals", []).append({"approver": approver.strip(), "statement": statement.strip(), "recorded_at": _now()})
    _save(data, path)
    return a


def withdraw(amendment_id: str, reason: str, path: Optional[Path] = None) -> Dict[str, Any]:
    if not str(reason).strip():
        raise AmendmentError("withdrawing an approval needs a reason")
    data = load(path)
    a = _find(data, amendment_id)
    for ap in a.get("approvals", []):
        if not ap.get("withdrawn"):
            ap["withdrawn"] = {"reason": reason.strip(), "at": _now()}
    _save(data, path)
    return a


def require(amendment_id: str, path: Optional[Path] = None) -> None:
    if not is_approved(amendment_id, path):
        raise AmendmentError(f"amendment {amendment_id} has no standing approval from the owner (an approval is a statement in the owner's own words; none was inferred)")
