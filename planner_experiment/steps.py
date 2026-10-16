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
import time
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


def _active_key(base: Path) -> bytes:
    """The key for a draw: present, not retired, and the one registered as being in custody."""
    try:
        return secrecy.active_key(Path(base) / "key_custody.json", Path(base) / "retired_keys.json")
    except secrecy.KeyError_ as exc:
        raise StepError(str(exc)) from exc


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


def check_selection_version(record: Dict[str, Any], label: str) -> None:
    """A draw is only valid under the algorithm that made it: refuse a selection recorded under any other version."""
    found = record.get("selection_version")
    if found != sc.SELECTION_VERSION:
        raise StepError(f"{label} was recorded under selection algorithm version {found!r}, but this code is version {sc.SELECTION_VERSION!r} "
                        f"({sc.SELECTION_HISTORY.get(sc.SELECTION_VERSION, '')}); it must be voided and drawn again")


def _load_voids(base: Path) -> List[Dict[str, Any]]:
    log_path = Path(base) / "development_voids.json"
    return json.loads(log_path.read_text(encoding="utf-8")) if log_path.is_file() else []


def acknowledge_voids(base: Path, voids: List[int], reviewer: str, note: str = "", comments: Optional[Dict[int, str]] = None,
                      overall: Optional[str] = None, sheet: str = "") -> List[Dict[str, Any]]:
    """Record that a reviewer has received and acknowledged the notice of a void-and-redraw. A human act: this records it, it cannot make it.
    The reviewer must be one of the two in the reviewer record; acknowledging the same void twice is refused.

    The reviewer's own words are kept **verbatim** in their own fields: `void_comment` (what they wrote against that void), `overall_comment` (what they
    wrote overall) and `source_sheet` (the file they came from). `note` is only for the recorder's remark and is never used for the reviewer's wording."""
    names = _reviewers(base)
    canonical = next((n for n in names if n.lower() == str(reviewer).strip().lower()), None)
    if canonical is None:
        raise StepError(f"{reviewer!r} is not one of the recorded reviewers ({', '.join(names)})")
    log = _load_voids(base)
    by_number = {e["void"]: e for e in log}
    if not voids or len(set(voids)) != len(voids):
        raise StepError("list each void number once (at least one)")
    unknown = [v for v in voids if v not in by_number]
    if unknown:
        raise StepError(f"no such void: {unknown}")
    for v in voids:
        if any(a["reviewer"] == canonical for a in by_number[v].get("acknowledged_by", [])):
            raise StepError(f"{canonical} has already acknowledged void {v}")
    for v in voids:
        entry = {"reviewer": canonical, "date": time.strftime("%Y-%m-%d", time.gmtime()), "note": note.strip()}
        if comments is not None and v in comments:
            entry["void_comment"] = comments[v]                      # verbatim: not trimmed, corrected or rephrased
        if overall is not None:
            entry["overall_comment"] = overall
        if sheet:
            entry["source_sheet"] = sheet
        by_number[v].setdefault("acknowledged_by", []).append(entry)
    (Path(base) / "development_voids.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
    return [by_number[v] for v in voids]


def correct_acknowledgement(base: Path, void: int, reviewer: str, void_comment: str, overall: str, sheet: str, reason: str) -> Dict[str, Any]:
    """Correct an acknowledgement's wording to the reviewer's exact text. Only while no planner is frozen (the void log is hashed into the planner freeze).
    The earlier note is kept in `corrections` (never lost), and the correction says why. Does not change who acknowledged or when."""
    if list(Path(base).glob("planner_freeze_round*.json")):
        raise StepError("planners are already frozen under this void log: it can no longer be corrected")
    if not str(reason or "").strip() or not str(void_comment or "").strip():
        raise StepError("a correction needs the reviewer's exact comment and a reason")
    log = _load_voids(base)
    entry = next((e for e in log if e["void"] == void), None)
    ack = next((a for a in (entry or {}).get("acknowledged_by", []) if a["reviewer"].lower() == str(reviewer).strip().lower()), None)
    if ack is None:
        raise StepError(f"{reviewer!r} has no recorded acknowledgement of void {void}")
    ack.setdefault("corrections", []).append({"date": time.strftime("%Y-%m-%d", time.gmtime()), "reason": reason.strip(),
                                              "previous_note": ack.get("note", ""), "previous_void_comment": ack.get("void_comment")})
    ack["void_comment"], ack["overall_comment"], ack["source_sheet"], ack["note"] = void_comment, overall, sheet, ""
    (Path(base) / "development_voids.json").write_text(json.dumps(log, indent=1), encoding="utf-8")
    return ack


def unacknowledged_voids(base: Path) -> List[str]:
    """What is still missing: 'void N: <reviewer>' for each reviewer who has not acknowledged each void."""
    names = _reviewers(base)
    missing = []
    for e in _load_voids(base):
        done = {a["reviewer"] for a in e.get("acknowledged_by", [])}
        missing.extend(f"void {e['void']}: {n}" for n in names if n not in done)
    return missing


def _ensure_seed_record(path: Path, artifact: str, purpose: str) -> None:
    """Write the seed derivation, or accept an existing one only if it is exactly this derivation (so a void-and-redraw reuses the record)."""
    if path.exists():
        record = seed_mod.load_record(path, purpose)
        if record["artifact"] != artifact.strip().lower():
            raise StepError(f"{path.name} records a different artifact ({record['artifact']}); a seed derivation is recorded once")
        return
    seed_mod.write_record(path, artifact, purpose)


def void_development(base: Path, reason: str, authorised_by: str) -> Dict[str, Any]:
    """Void the development draw so it can be drawn again. A departure from "drawn once", so it is deliberate and recorded:
    a reason and who authorised it are required, the voided file is kept (renamed, never deleted) and every void is logged.
    Refused once any planner freeze exists, because planners are built on the draw and it can no longer be replaced."""
    for label, value in (("a reason", reason), ("who authorised it", authorised_by)):
        if not str(value or "").strip() or str(value).strip().lower() in freeze_mod.PLACEHOLDER_NAMES:
            raise StepError(f"voiding the development draw needs {label}")
    p = paths(base)
    if not p["development"].is_file():
        raise StepError("there is no development draw to void")
    if list(Path(base).glob("planner_freeze_round*.json")):
        raise StepError("planners are already frozen against this development draw: it can no longer be voided")
    n = 1
    while (Path(base) / f"development_selection_VOID_{n}.json").exists():
        n += 1
    voided = Path(base) / f"development_selection_VOID_{n}.json"
    raw = p["development"].read_bytes()
    p["development"].replace(voided)
    log_path = Path(base) / "development_voids.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.is_file() else []
    entry = {"void": n, "file": voided.name, "sha256": freeze_mod.sha256_bytes(raw), "reason": reason.strip(), "authorised_by": authorised_by.strip(),
             "date": time.strftime("%Y-%m-%d", time.gmtime())}
    log.append(entry)
    log_path.write_text(json.dumps(log, indent=1), encoding="utf-8")
    return entry


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
    _ensure_seed_record(p["development_seed"], artifact, "development-selection")
    return _write_selection(p["development"], "development", selection, {})


def freeze_planners(base: Path, rnd: int, entrypoints: Dict[str, str], root: Path = ROOT, planners_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Freeze the exact candidate planners for round `rnd`. Only possible once the development subset exists, and only in order."""
    if rnd < 1:
        raise StepError("rounds start at 1")
    if rnd > 1 and not round_paths(base, rnd - 1)["selection"].is_file():
        raise StepError(f"round {rnd - 1} has not been drawn: rounds are consecutive")
    if round_paths(base, rnd)["selection"].is_file():
        raise StepError(f"round {rnd} was already drawn")
    dev_path = paths(base)["development"]
    if dev_path.is_file():
        check_selection_version(json.loads(dev_path.read_text(encoding="utf-8")), "the development selection")
    missing = unacknowledged_voids(base)
    if missing:
        raise StepError("both reviewers must acknowledge every void-and-redraw of the development subset before the planners are frozen; "
                        f"still missing: {', '.join(missing)}")
    if planners_dir is None:                                   # a production freeze (the command line); tests pass their own planners folder
        from agentruntime.planners import model_manifest
        problems = model_manifest.incomplete()
        if problems:
            raise StepError("the model manifest must be complete before the planners are frozen: " + "; ".join(problems))
    void_log = Path(base) / "development_voids.json"
    try:
        return planner_freeze.create_planner_freeze(entrypoints, dev_path, round_paths(base, rnd)["planner_freeze"], base,
                                                    root, planners_dir, void_log_path=void_log if void_log.is_file() else None)
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
        planner_freeze.verify_provenance(pf, paths(base)["development"], Path(base) / "development_voids.json")
    except planner_freeze.PlannerFreezeError as exc:
        raise StepError(str(exc)) from exc
    if not paths(base)["development"].is_file():
        raise StepError("the development subset has not been drawn")
    check_selection_version(json.loads(paths(base)["development"].read_text(encoding="utf-8")), "the development selection")
    for earlier_round, _raw, earlier_record in _earlier_rounds(base, rnd):
        check_selection_version(earlier_record, f"round {earlier_round}'s selection")
    partition = _partition_seed(base)
    purpose = f"planner-experiment-{rnd}"
    draw = seed_mod.derive_seed(pf["record_hash"], purpose)
    key = _active_key(base)
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
    retired_file = Path(base) / "retired_keys.json"
    if not retired_file.exists():
        retired_file.write_text("[]", encoding="utf-8")             # no key retired: an empty list is still a frozen input
    for label, path in (("the planner freeze", rp["planner_freeze"]), ("the key custody record", Path(base) / "key_custody.json"), ("the draw's seed record", rp["seed"]), ("the draw's selection", rp["selection"]),
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
             "development_selection": p["development"], "planner_freeze": rp["planner_freeze"], "reviewers": p["reviewers"], "golden": golden_path,
             "key_custody": Path(base) / "key_custody.json", "retired_keys": retired_file}
    for n in range(1, rnd):
        files[f"prior_selection:{n}"] = round_paths(base, n)["selection"]
    if pf.get("void_log_hash") is not None:                       # the planners were frozen under a void log: it travels with the freeze, hash-checked
        files["development_voids"] = Path(base) / "development_voids.json"
    try:
        return freeze_mod.create_freeze(files, {n: Path(f) for n, f in reviewer_files.items()}, out, root)
    except freeze_mod.FreezeError as exc:
        raise StepError(f"the freeze was not created: {exc}") from exc


def build_cases(selection_path: Path, tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The case list for a drawn set, with ticket text for the reviewers. Every case is checked against the content hash recorded at the draw,
    so a source that changed since the draw is refused instead of silently producing different cases."""
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    check_selection_version(selection, "this selection")
    key = None
    if selection.get("keyed"):
        key = secrecy.load_key()
        if key is None or secrecy.key_check(key) != selection.get("key_check"):
            raise StepError("this set is keyed: the experiment key it was drawn with is needed to build its cases")
        _active_key(Path(selection_path).parent)                    # ...and it must be a non-retired key, registered as in custody
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
