"""
The preparation workflow, as functions that take their directory and ticket source explicitly (so they are testable without a database).
The command line (cli.py) is a thin layer over these.

Order, enforced by the functions themselves:

  1. record_reviewers        two named reviewers and committed capacity (owner-supplied)      -> reviewers.json
  2. (partition seed)        recorded once from an immutable commit                           -> seed_calibration.json
  3. select_development      10-ticket development subset, golden approved by the reviewers   -> development_selection.json
  4. [build and tune the candidate planners on the development subset only]
  5. freeze_planners         exact planner entry points + runtime/planner code hashes         -> planner_freeze_round<N>.json
  6. draw_experiment         50-ticket set, seed derived from the planner-freeze hash         -> experiment_selection_round<N>.json
  7. build_cases             case texts for reviewers, checked against the draw's content hashes
  8. [annotate, adjudicate, record, freeze (freeze.create_freeze), run once (experiment.run_once)]

No tuning on the 50 after seeing results: a later round needs revised planners (a new freeze hash) and a fresh set that excludes the
development subset and every earlier round's clusters.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from planner_experiment import planner_freeze, reviewers as reviewer_mod
from planner_experiment import seed as seed_mod
from planner_experiment import select_cases as sc
from planner_experiment.freeze import ROOT

BASE = ROOT / "docs" / "baselines" / "planner_experiment"


class StepError(Exception):
    pass


def paths(base: Path = BASE) -> Dict[str, Path]:
    return {"reviewers": base / "reviewers.json", "partition_seed": base / "seed_calibration.json",
            "development_seed": base / "seed_development.json", "development": base / "development_selection.json"}


def round_paths(base: Path, rnd: int) -> Dict[str, Path]:
    return {"planner_freeze": base / f"planner_freeze_round{rnd}.json", "seed": base / f"seed_planner_experiment_{rnd}.json",
            "selection": base / f"experiment_selection_round{rnd}.json"}


def _reviewers(base: Path) -> List[str]:
    try:
        return reviewer_mod.names_of(reviewer_mod.load_record(paths(base)["reviewers"]))
    except reviewer_mod.ReviewerRecordError as exc:
        raise StepError(str(exc)) from exc


def reviewer_names(base: Path = BASE) -> List[str]:
    """The recorded reviewers; raises StepError if they have not been recorded (every draw needs them)."""
    return _reviewers(base)


def record_reviewers(base: Path, names: List[str], capacity_hours: Dict[str, float], recorded_by: str, note: str = "") -> Dict[str, Any]:
    try:
        record = reviewer_mod.write_record(paths(base)["reviewers"], names, capacity_hours, recorded_by, note)
        return {**record, "warnings": reviewer_mod.capacity_warnings(record)}
    except (reviewer_mod.ReviewerRecordError, FileExistsError) as exc:
        raise StepError(str(exc)) from exc


def _partition_seed(base: Path) -> int:
    p = paths(base)["partition_seed"]
    if not p.is_file():
        raise StepError("the partition seed has not been recorded (cli seed --artifact <commit>)")
    return seed_mod.load_record(p, "calibration-selection")["seed"]


def _used_clusters(base: Path) -> frozenset:
    used = set()
    for p in [paths(base)["development"], *sorted(base.glob("experiment_selection_round*.json"))]:
        if p.is_file():
            used.update(e["cluster_id"] for e in json.loads(p.read_text(encoding="utf-8"))["cases"])
    return frozenset(used)


def _write_selection(path: Path, kind: str, selection: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    record = {"kind": kind, "selection_version": selection["selection_version"], "partition_seed": selection["partition_seed"],
              "draw_seed": selection["draw_seed"], "type_counts": selection["type_counts"], "cases": sc.selection_entries(selection), **extra}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return record


def select_development(base: Path, tickets: List[Dict[str, Any]], artifact: str) -> Dict[str, Any]:
    """Draw the 10-ticket development subset (4 PYPL, 4 PFG, 2 golden). `tickets` must already include golden tickets approved by the reviewers."""
    _reviewers(base)
    p = paths(base)
    if p["development"].is_file():
        raise StepError("the development subset is drawn once")
    partition = _partition_seed(base)
    draw = seed_mod.derive_seed(artifact, "development-selection")
    selection = sc.select_calibration(tickets, partition, draw, sc.DEVELOPMENT_QUOTAS, exclude_clusters=frozenset())
    if selection["shortfall"]:
        raise StepError(f"not enough eligible tickets for the development subset: {selection['shortfall']} (nothing was recorded)")
    seed_mod.write_record(p["development_seed"], artifact, "development-selection")
    return _write_selection(p["development"], "development", selection, {})


def freeze_planners(base: Path, rnd: int, entrypoints: Dict[str, str], root: Path = ROOT, planners_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Freeze the exact candidate planners for round `rnd`. Only possible once the development subset exists, and only in order."""
    if rnd < 1:
        raise StepError("rounds start at 1")
    if rnd > 1 and not round_paths(base, rnd - 1)["selection"].is_file():
        raise StepError(f"round {rnd - 1} has not been drawn: rounds are consecutive")
    if round_paths(base, rnd)["selection"].is_file():
        raise StepError(f"round {rnd} was already drawn")
    try:
        return planner_freeze.create_planner_freeze(entrypoints, paths(base)["development"], round_paths(base, rnd)["planner_freeze"], base,
                                                    root, planners_dir)
    except planner_freeze.PlannerFreezeError as exc:
        raise StepError(str(exc)) from exc


def draw_experiment(base: Path, rnd: int, tickets: List[Dict[str, Any]], root: Path = ROOT) -> Dict[str, Any]:
    """Draw the 50-ticket set for round `rnd`. The seed is derived from the planner-freeze hash, so the draw cannot precede the freeze."""
    _reviewers(base)
    rp = round_paths(base, rnd)
    if rp["selection"].is_file():
        raise StepError(f"round {rnd} was already drawn: a set is drawn once")
    try:
        pf = planner_freeze.load_planner_freeze(rp["planner_freeze"])
        planner_freeze.verify_unchanged(pf, root)
    except planner_freeze.PlannerFreezeError as exc:
        raise StepError(str(exc)) from exc
    if not paths(base)["development"].is_file():
        raise StepError("the development subset has not been drawn")
    partition = _partition_seed(base)
    purpose = f"planner-experiment-{rnd}"
    draw = seed_mod.derive_seed(pf["record_hash"], purpose)
    selection = sc.select_calibration(tickets, partition, draw, sc.EXPERIMENT_QUOTAS, exclude_clusters=_used_clusters(base))
    if selection["shortfall"]:
        raise StepError(f"not enough unused eligible tickets for a fresh set: {selection['shortfall']} (nothing was recorded)")
    seed_mod.write_record(rp["seed"], pf["record_hash"], purpose)
    return _write_selection(rp["selection"], "experiment", selection, {"round": rnd, "planner_freeze_hash": pf["record_hash"]})


def build_cases(selection_path: Path, tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The case list for a drawn set, with ticket text for the reviewers. Every case is checked against the content hash recorded at the draw,
    so a source that changed since the draw is refused instead of silently producing different cases."""
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    by_key = {(str(t.get("pool") or t.get("tenant_id")), str(t["ticket_id"])): t for t in tickets}
    cases: List[Dict[str, Any]] = []
    for entry in selection["cases"]:
        ticket = by_key.get((entry["pool"], entry["ticket_id"]))
        if ticket is None:
            raise StepError(f"{entry['case_id']}: the ticket is no longer in the source")
        if sc.content_hash({**ticket, "pool": entry["pool"]}) != entry["content_hash"]:
            raise StepError(f"{entry['case_id']}: the ticket changed since it was drawn")
        cases.append({"case_id": entry["case_id"], "pool": entry["pool"], "tenant_id": ticket["tenant_id"], "ticket_id": entry["ticket_id"],
                      "ticket_text": ticket["text"], "host": ticket.get("host", ""), "opened_at": ticket.get("opened_at", "")})
    return cases
