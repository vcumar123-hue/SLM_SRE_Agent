"""
The reviewer record: the two named reviewers and the capacity they have committed. Supplied by the owner, recorded once, and required
before any golden ticket is approved, any experiment set is drawn, anything is annotated, or anything is frozen. Golden-ticket approvals and
the frozen reviewer annotation files are checked against exactly these names.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

from planner_experiment.freeze_names import PLACEHOLDER_NAMES


MIN_CAPACITY_HOURS = 40.0         # each reviewer handles ~150 labels plus calibration, blinding checks and corrections
PREFERRED_CAPACITY_HOURS = 50.0   # below this the record is accepted with a warning


class ReviewerRecordError(Exception):
    pass


def validate(record: Dict[str, Any]) -> List[str]:
    names = [str(n).strip() for n in record.get("reviewers", [])]
    if len(names) != 2 or len({n.lower() for n in names}) != 2 or any(n.lower() in PLACEHOLDER_NAMES for n in names):
        raise ReviewerRecordError("exactly two distinct, named reviewers are required")
    capacity = record.get("capacity_hours")
    if not isinstance(capacity, dict) or {k.strip().lower() for k in capacity} != {n.lower() for n in names}:
        raise ReviewerRecordError("committed capacity (hours) is required for each named reviewer")
    for name, hours in capacity.items():
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
            raise ReviewerRecordError(f"capacity for {name} must be a positive number of hours")
        if hours < MIN_CAPACITY_HOURS:
            raise ReviewerRecordError(f"capacity for {name} is {hours} hours: at least {MIN_CAPACITY_HOURS:g} committed hours per reviewer are required "
                                      f"({PREFERRED_CAPACITY_HOURS:g} preferred)")
    if not str(record.get("recorded_by") or "").strip():
        raise ReviewerRecordError("who recorded this (the owner) is required")
    return names


def write_record(path: Path, names: List[str], capacity_hours: Mapping[str, float], recorded_by: str, note: str = "") -> Dict[str, Any]:
    record = {"reviewers": [n.strip() for n in names], "capacity_hours": dict(capacity_hours), "recorded_by": recorded_by.strip(), "note": note}
    validate(record)
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists: the reviewer record is written once (change it by a recorded owner decision)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return record


def load_record(path: Path) -> Dict[str, Any]:
    if not Path(path).is_file():
        raise ReviewerRecordError("no reviewer record: two named reviewers and their committed capacity must be recorded first")
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(record)
    return record


def capacity_warnings(record: Dict[str, Any]) -> List[str]:
    return [f"{name}: {hours:g} hours committed; {PREFERRED_CAPACITY_HOURS:g} is preferred" for name, hours in record["capacity_hours"].items()
            if hours < PREFERRED_CAPACITY_HOURS]


def names_of(record: Dict[str, Any]) -> List[str]:
    return validate(record)
