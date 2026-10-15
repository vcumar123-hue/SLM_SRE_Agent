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

from planner_experiment import golden as golden_mod
from planner_experiment import injection as injection_mod
from planner_experiment import planner_freeze, reviewers as reviewer_mod, secrecy
from planner_experiment import seed as seed_mod
from planner_experiment import select_cases as sc
from planner_experiment import freeze as freeze_mod
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


def _used_clusters(base: Path):
    """(plain, keyed) cluster ids already used: the development subset is recorded plain, earlier experiment sets keyed."""
    plain, keyed = set(), set()
    for p in [paths(base)["development"], *sorted(base.glob("experiment_selection_round*.json"))]:
        if p.is_file():
            record = json.loads(p.read_text(encoding="utf-8"))
            (keyed if record.get("keyed") else plain).update(e["cluster_id"] for e in record["cases"])
    return frozenset(plain), frozenset(keyed)


def _earlier_rounds(base: Path, rnd: int):
    """[(round number, selection file bytes, record)] for every other recorded planner-experiment draw."""
    found = []
    for p in sorted(base.glob("experiment_selection_round*.json")):
        raw = p.read_bytes()
        record = json.loads(raw.decode("utf-8"))
        if record.get("round") != rnd:
            found.append((record.get("round"), raw, record))
    return found


def _write_selection(path: Path, kind: str, selection: Dict[str, Any], extra: Dict[str, Any], key: Optional[bytes] = None) -> Dict[str, Any]:
    counts = ({sc.keyed_type(key, k): v for k, v in selection["type_counts"].items()} if key is not None else selection["type_counts"])
    record = {"kind": kind, "selection_version": selection["selection_version"], "partition_seed": selection["partition_seed"],
              "draw_seed": selection["draw_seed"], "type_counts": counts, "cases": sc.selection_entries(selection, key), **extra}
    if key is not None:
        record.update({"keyed": True, "key_check": secrecy.key_check(key)})
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
    selection = sc.select_calibration(tickets, partition, draw, sc.DEVELOPMENT_QUOTAS)
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
    key = secrecy.load_key()
    if key is None:
        raise StepError("the experiment key is required before a planner-experiment set is drawn: everything recorded about the drawn tickets is keyed "
                        "(cli init-key creates it once, outside the repository)")
    # One key across ALL rounds. Earlier rounds' clusters are recorded as HMACs under the key they were drawn with; under a different key none of
    # them would match, so their clusters would silently be eligible again. A mismatching key is therefore refused, before anything is recorded.
    earlier = _earlier_rounds(base, rnd)
    wrong = sorted(r for r, _raw, rec in earlier if rec.get("keyed") and rec.get("key_check") != secrecy.key_check(key))
    if wrong:
        raise StepError(f"round {rnd} must be drawn with the same experiment key as round(s) {wrong}: every round uses one key, otherwise the "
                        f"clusters already used could not be excluded (nothing was recorded)")
    plain_used, keyed_used = _used_clusters(base)
    used_tickets = frozenset((e["pool"], str(e["ticket_id"])) for _r, _raw, rec in earlier for e in rec["cases"])
    selection = sc.select_calibration(tickets, partition, draw, sc.EXPERIMENT_QUOTAS, exclude_clusters=plain_used, exclude_keyed=keyed_used, key=key,
                                      exclude_tickets=used_tickets)
    if selection["shortfall"]:
        raise StepError(f"not enough unused eligible tickets for a fresh set: {selection['shortfall']} (nothing was recorded)")
    seed_mod.write_record(rp["seed"], pf["record_hash"], purpose)
    prior = [{"round": r, "selection_hash": freeze_mod.sha256_bytes(raw), "key_check": rec.get("key_check")} for r, raw, rec in sorted(earlier, key=lambda x: x[0])]
    return _write_selection(rp["selection"], "experiment", selection,
                            {"round": rnd, "planner_freeze_hash": pf["record_hash"], "prior_rounds": prior}, key)


def approve_golden(base: Path, draft_path: Path, out_path: Path, approved_ids: List[str], approvers: List[str]) -> List[Dict[str, Any]]:
    """Write the golden tickets the reviewers have approved. Approval is explicit and per ticket: only the ids listed are written, with
    `approved_by` set to the two reviewers of the reviewer record. `approvers` is what the caller asserts; it must be exactly those two names,
    so this command cannot approve on behalf of anyone else. It records an approval; the reviewing has to have happened (golden-ticket
    approval is a human act, and the names must be added only after it). Written once; the draft's descriptive fields are dropped."""
    names = _reviewers(base)
    if sorted(a.strip().lower() for a in approvers) != sorted(n.lower() for n in names):
        raise StepError(f"the approvers must be exactly the two recorded reviewers ({', '.join(names)}), no more and no fewer")
    if not approved_ids or len(set(approved_ids)) != len(approved_ids):
        raise StepError("list each approved ticket id once (at least one)")
    out_path = Path(out_path)
    if out_path.exists():
        raise StepError(f"{out_path} already exists: the approved golden file is written once")
    draft = {str(i.get("ticket_id")): i for i in json.loads(Path(draft_path).read_text(encoding="utf-8"))}
    unknown = [i for i in approved_ids if i not in draft]
    if unknown:
        raise StepError(f"not in the draft: {unknown}")
    items = []
    for ticket_id in approved_ids:
        item = {k: v for k, v in draft[ticket_id].items() if k in ("ticket_id", "text", "host", "opened_at", "set", "tenant_id")}
        item["approved_by"] = list(names)
        items.append(item)
    try:
        golden_mod.parse_golden(items, names)
    except golden_mod.GoldenError as exc:
        raise StepError(str(exc)) from exc
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(items, indent=1), encoding="utf-8")
    return items


def create_freeze_for_round(base: Path, rnd: int, cases_path: Path, recording_path: Path, annotations_path: Path, reviewer_files: Dict[str, Path],
                            golden_path: Path, out_path: Optional[Path] = None, root: Path = ROOT) -> Dict[str, Any]:
    """Assemble and create the complete experiment freeze for round `rnd`.

    Supplied by the caller (they come from people and from the live system): the case package, the scripted recording, the adjudicated annotations
    and each reviewer's own annotation file. Everything else is taken from the recorded workflow, never typed: the seed and selection records, the
    planner freeze, the reviewer record, the earlier rounds' selections, the golden file, and the two files generated here so they cannot disagree
    with the workflow: `config.json` (round, derived seed, modes, the planner freeze's entry points) and the injection cases (built from the first
    four cases). `freeze.create_freeze` then runs the whole consistency check; any failure is reported and nothing is written.
    """
    rp, p = round_paths(base, rnd), paths(base)
    for label, path in (("the planner freeze", rp["planner_freeze"]), ("the draw's seed record", rp["seed"]), ("the draw's selection", rp["selection"]),
                        ("the development selection", p["development"]), ("the partition seed record", p["partition_seed"]),
                        ("the golden file", golden_path), ("the case package", cases_path), ("the recording", recording_path),
                        ("the adjudicated annotations", annotations_path)):
        if not Path(path).is_file():
            raise StepError(f"{label} is missing: {path}")
    names = _reviewers(base)
    if sorted(n.lower() for n in reviewer_files) != sorted(n.lower() for n in names):
        raise StepError(f"supply one annotation file for each of exactly the two recorded reviewers ({', '.join(names)})")
    out = Path(out_path) if out_path is not None else Path(base) / f"freeze_round{rnd}.json"
    if out.exists():
        raise StepError(f"{out} already exists: a freeze is created once per round")
    try:
        pf = planner_freeze.load_planner_freeze(rp["planner_freeze"])
        draw = seed_mod.load_record(rp["seed"], f"planner-experiment-{rnd}")
    except (planner_freeze.PlannerFreezeError, ValueError) as exc:
        raise StepError(str(exc)) from exc
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    inputs_dir = Path(base) / f"inputs_round{rnd}"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    config_path, injection_path = inputs_dir / "config.json", inputs_dir / "injection_cases.json"
    config_path.write_text(json.dumps({"round": rnd, "seed": draw["seed"], "modes": list(freeze_mod.MODES), "planners": pf["entrypoints"], "budgets": {}},
                                      indent=1, sort_keys=True), encoding="utf-8")
    base_ids = [c["case_id"] for c in cases][:4]
    if len(base_ids) < 4:
        raise StepError("the case package has fewer than four cases")
    injection_path.write_text(json.dumps([vars(i) for i in injection_mod.build_injection_cases(base_ids)], indent=1), encoding="utf-8")
    files = {"annotations": annotations_path, "cases": cases_path, "recording": recording_path, "injection_cases": injection_path, "config": config_path,
             "seed_record": rp["seed"], "partition_seed_record": p["partition_seed"], "experiment_selection": rp["selection"],
             "development_selection": p["development"], "planner_freeze": rp["planner_freeze"], "reviewers": p["reviewers"], "golden": golden_path}
    for n in range(1, rnd):
        files[f"prior_selection:{n}"] = round_paths(base, n)["selection"]
    try:
        return freeze_mod.create_freeze(files, {n: Path(f) for n, f in reviewer_files.items()}, out, root)
    except freeze_mod.FreezeError as exc:
        raise StepError(f"the freeze was not created: {exc}") from exc


def build_cases(selection_path: Path, tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The case list for a drawn set, with ticket text for the reviewers. Every case is checked against the content hash recorded at the draw,
    so a source that changed since the draw is refused instead of silently producing different cases."""
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    key = None
    if selection.get("keyed"):
        key = secrecy.load_key()
        if key is None or secrecy.key_check(key) != selection.get("key_check"):
            raise StepError("this set is keyed: the experiment key it was drawn with is needed to build its cases")
    by_key = {(str(t.get("pool") or t.get("tenant_id")), str(t["ticket_id"])): t for t in tickets}
    cases: List[Dict[str, Any]] = []
    for entry in selection["cases"]:
        ticket = by_key.get((entry["pool"], entry["ticket_id"]))
        if ticket is None:
            raise StepError(f"{entry['case_id']}: the ticket is no longer in the source")
        if sc.content_hash({**ticket, "pool": entry["pool"]}, key) != entry["content_hash"]:
            raise StepError(f"{entry['case_id']}: the ticket changed since it was drawn")
        cases.append({"case_id": entry["case_id"], "pool": entry["pool"], "tenant_id": ticket["tenant_id"], "ticket_id": entry["ticket_id"],
                      "ticket_text": ticket["text"], "host": ticket.get("host", ""), "opened_at": ticket.get("opened_at", "")})
    return cases
