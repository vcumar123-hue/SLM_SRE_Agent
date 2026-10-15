"""
Freeze record for the planner experiment (design 12, 13.1).

What is frozen, together, before the experiment runs:
  * the inputs the run reads: adjudicated annotations, case list, recording, injection cases, config (round, seed, modes, planner entry
    points, budgets), the partition and draw seed records, the experiment selection, the development selection, the planner freeze, the
    reviewer record, the approved golden tickets, and each reviewer's own annotation file;
  * all code under test: every `.py` file in `agentruntime/` (runtime, tools and the planners in `agentruntime/planners/`) and in
    `planner_experiment/` (harness, scoring and the decision rule). The set of files is frozen too, so an added or removed file fails.

One function, `validate_set`, checks that these inputs are consistent with each other. It runs when the freeze is created (reading the files)
and again when the experiment is loaded (reading the hash-verified bytes of the same files), so what was checked at the freeze is exactly
what is checked at run time:
  * reviewers: the two named reviewers of the reviewer record, each with an annotation per frozen case, kappa >= 0.6, rationale for every
    disagreement; golden tickets approved by exactly those two reviewers;
  * composition (composition.py): 20 PYPL / 20 PFG / 10 golden, one case per cluster, calibration buckets, type cap, content hashes tied
    to the draw, disjoint from the development subset;
  * seeds: the draw seed is derived from the planner-freeze hash, the partition seed from its own record; the draw selection records both;
  * planners: the config's planner entry points are the planner freeze's, and the planner and runtime code is unchanged since that freeze;
  * the decision rule: derived from the code that executes it (`metrics.frozen_rule()`), never supplied by a caller, and verified against the
    record when the experiment loads; the run applies the record's copy.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from planner_experiment.freeze_names import PLACEHOLDER_NAMES  # noqa: F401  (re-exported for existing callers)

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = ("annotations", "cases", "recording", "injection_cases", "config", "seed_record", "partition_seed_record",
                  "experiment_selection", "development_selection", "planner_freeze", "reviewers", "golden")
CODE_ROOTS = ("agentruntime", "planner_experiment")
PLANNERS_DIR = ROOT / "agentruntime" / "planners"
MIN_KAPPA = 0.6          # design 13.4: end-state kappa must be at least 0.6
MODES = ("rules_only", "rules_then_model", "model")


class FreezeError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    """SHA-256 of the bytes with CRLF read as LF, so the same files hash the same on a Windows (CRLF) and a Linux (LF) checkout; a freeze
    made on one machine verifies on another. Any other change to the content still changes the hash."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def code_files(root: Path = ROOT) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for top in CODE_ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            out["code/" + path.relative_to(root).as_posix()] = path
    return out


def _record_hash(record: Dict[str, Any]) -> str:
    body = {k: record[k] for k in ("reviewers", "files", "code", "agreement", "decision_rule")}
    return sha256_bytes(json.dumps(body, sort_keys=True).encode("utf-8"))


def _stored(path: Path) -> str:
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _resolve(stored: str) -> Path:
    p = Path(stored)
    return p if p.is_absolute() else ROOT / p


# -- readers: the same validation over files (at the freeze) and over verified bytes (at run time) ------------------------------------------

class PathReader:
    def __init__(self, files: Dict[str, Path]) -> None:
        self.files = {k: Path(v) for k, v in files.items()}

    def names(self) -> List[str]:
        return list(self.files)

    def bytes(self, name: str) -> bytes:
        if name not in self.files:
            raise FreezeError(f"input '{name}' is missing")
        return self.files[name].read_bytes()

    def json(self, name: str) -> Any:
        return json.loads(self.bytes(name).decode("utf-8"))


class RecordReader:
    """Reads the files named in a freeze record, each verified against its recorded hash at the moment it is read."""

    def __init__(self, record: Dict[str, Any]) -> None:
        self.record = record

    def names(self) -> List[str]:
        return list(self.record["files"])

    def bytes(self, name: str) -> bytes:
        return read_frozen(self.record, name)

    def json(self, name: str) -> Any:
        return json.loads(self.bytes(name).decode("utf-8"))


def validate_set(reader, code_hashes: Dict[str, str], root: Path = ROOT) -> Dict[str, Any]:
    """Check that the frozen inputs are consistent with each other. Returns {'reviewers', 'agreement', 'round', 'seed', 'partition_seed', ...}."""
    from planner_experiment import annotations as ann
    from planner_experiment import composition, golden, planner_freeze, reviewers as reviewer_mod
    from planner_experiment import seed as seed_mod

    present = set(reader.names())
    reviewer_keys = sorted(k for k in present if k.startswith("reviewer:"))
    missing = [k for k in REQUIRED_FILES if k not in present]
    unknown = sorted(present - set(REQUIRED_FILES) - set(reviewer_keys))
    if missing or unknown:
        raise FreezeError(f"frozen inputs must be exactly {REQUIRED_FILES} plus one reviewer file each; missing {missing}, unknown {unknown}")

    # reviewers: the recorded names, golden approvals, and the reviewers' own annotation files
    try:
        names = reviewer_mod.validate(reader.json("reviewers"))
        golden_items = golden.parse_golden(reader.json("golden"), names)
    except (reviewer_mod.ReviewerRecordError, golden.GoldenError) as exc:
        raise FreezeError(str(exc)) from exc
    if sorted(k.split(":", 1)[1].lower() for k in reviewer_keys) != sorted(n.lower() for n in names):
        raise FreezeError("the reviewer annotation files must be exactly those of the two reviewers in the reviewer record")
    cases = reader.json("cases")
    case_ids = {c["case_id"] for c in cases}
    loaded: Dict[str, List[Any]] = {}
    for key in reviewer_keys:
        name = key.split(":", 1)[1]
        items = ann.parse(reader.json(key))
        ids = [a.case_id for a in items]
        if len(ids) != len(set(ids)) or set(ids) != case_ids:
            raise FreezeError(f"reviewer '{name}' must annotate exactly the {len(case_ids)} frozen cases, once each")
        if any(a.reviewer.strip().lower() != name.strip().lower() for a in items):
            raise FreezeError(f"reviewer '{name}': every annotation must be recorded under that reviewer's name")
        loaded[name] = items
    first, second = (loaded[k.split(":", 1)[1]] for k in reviewer_keys)
    report = ann.agreement(first, second)
    if report["cases_compared"] != len(case_ids):
        raise FreezeError(f"agreement covers {report['cases_compared']} of {len(case_ids)} cases")
    kappa = report["end_state_kappa"]
    if kappa is None or kappa < MIN_KAPPA:
        raise FreezeError(f"end-state kappa {kappa} is below the required {MIN_KAPPA}")
    adjudicated = {a.case_id: a for a in ann.parse(reader.json("annotations"))}
    if set(adjudicated) != case_ids:
        raise FreezeError("the adjudicated annotations must cover exactly the frozen cases")
    for d in report["disagreements"]:
        if not adjudicated[d["case_id"]].notes.strip():
            raise FreezeError(f"{d['case_id']}: a disagreement ({', '.join(d['fields'])}) needs an adjudication rationale in notes")

    # seeds and planners
    config = reader.json("config")
    rnd = config.get("round")
    if not isinstance(rnd, int) or rnd < 1:
        raise FreezeError("config.round must be a positive integer")
    try:
        partition = seed_mod.check_record(reader.json("partition_seed_record"), "calibration-selection")["seed"]
        draw = seed_mod.check_record(reader.json("seed_record"), f"planner-experiment-{rnd}")
        pf = planner_freeze.check_record(reader.json("planner_freeze"))
    except (ValueError, KeyError, planner_freeze.PlannerFreezeError) as exc:
        raise FreezeError(f"seed or planner-freeze record invalid: {exc}") from exc
    if config.get("seed") != draw["seed"]:
        raise FreezeError("config.seed is not the derived draw seed")
    if draw["artifact"] != pf["record_hash"]:
        raise FreezeError("the draw seed must be derived from the planner-freeze hash (the planners were not frozen before the draw)")
    if sha256_bytes(reader.bytes("development_selection")) != pf["development_selection_hash"]:
        raise FreezeError("the development selection is not the one the planners were frozen against")
    if config.get("planners") != pf["entrypoints"]:
        raise FreezeError("config.planners must be exactly the planner freeze's entry points")
    if tuple(config.get("modes", ())) != MODES:
        raise FreezeError(f"config.modes must be {MODES}")
    runtime_now = {k: v for k, v in code_hashes.items() if k.startswith("code/agentruntime/")}
    if runtime_now != pf["runtime_code"]:
        changed = sorted({k for k in set(runtime_now) | set(pf["runtime_code"]) if runtime_now.get(k) != pf["runtime_code"].get(k)})
        raise FreezeError(f"planner or runtime code differs from the planner freeze: {changed[:5]}")

    # the drawn set
    selection = reader.json("experiment_selection")
    if (selection.get("round"), selection.get("planner_freeze_hash"), selection.get("draw_seed"), selection.get("partition_seed")) != (
            rnd, pf["record_hash"], draw["seed"], partition):
        raise FreezeError("the experiment selection was not drawn with this round, planner freeze and these seeds")
    try:
        from planner_experiment import secrecy
        composition.validate(cases, selection, partition, golden_items, reader.json("development_selection"), key=secrecy.load_key())
    except composition.CompositionError as exc:
        raise FreezeError(f"composition: {exc}") from exc
    return {"reviewers": names, "agreement": report, "round": rnd, "seed": draw["seed"], "partition_seed": partition,
            "planner_freeze_hash": pf["record_hash"]}


def check_decision_rule(record: Dict[str, Any]) -> Dict[str, Any]:
    """The rule in the record must be exactly the rule this code executes (metrics.frozen_rule()). Returns the rule to apply."""
    from planner_experiment import metrics
    if record.get("decision_rule") != metrics.frozen_rule():
        raise FreezeError("the frozen decision rule differs from the rule this code executes")
    return metrics.check_rule(record["decision_rule"]["rule"])


def create_freeze(files: Dict[str, Path], reviewer_files: Dict[str, Path], out_path: Path, root: Path = ROOT) -> Dict[str, Any]:
    """Create the freeze record. The decision rule is not a parameter: it is derived from the code that will execute it."""
    from planner_experiment import metrics
    every = {**files, **{f"reviewer:{n}": p for n, p in reviewer_files.items()}}
    for k, p in every.items():
        if not Path(p).is_file():
            raise FreezeError(f"input '{k}' does not exist: {p}")
    code = code_files(root)
    code_hashes = {k: sha256_bytes(p.read_bytes()) for k, p in code.items()}
    info = validate_set(PathReader(every), code_hashes, root)
    record = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reviewers": info["reviewers"],
        "files": {k: sha256_bytes(Path(p).read_bytes()) for k, p in sorted(every.items())},
        "paths": {k: _stored(p) for k, p in every.items()}, "code": code_hashes,
        "agreement": info["agreement"], "decision_rule": metrics.frozen_rule(),
    }
    record["record_hash"] = _record_hash(record)
    Path(out_path).write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return record


def load_record(record_path: Path) -> Dict[str, Any]:
    if not Path(record_path).is_file():
        raise FreezeError("no freeze record: the experiment may not run before the inputs are frozen")
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    if _record_hash(record) != record.get("record_hash"):
        raise FreezeError("the freeze record itself was modified")
    return record


def verify_code(record: Dict[str, Any], root: Path = ROOT) -> None:
    current = code_files(root)
    frozen = set(record["code"])
    if set(current) != frozen:
        added, removed = sorted(set(current) - frozen), sorted(frozen - set(current))
        raise FreezeError(f"the set of code files changed after the freeze: added {added}, removed {removed}")
    for key, path in current.items():
        if sha256_bytes(path.read_bytes()) != record["code"][key]:
            raise FreezeError(f"{key} changed after the freeze")


def read_frozen(record: Dict[str, Any], name: str) -> bytes:
    """The bytes of a frozen input, verified against the recorded hash at the moment they are read."""
    if name not in record["files"]:
        raise FreezeError(f"'{name}' is not part of the freeze record")
    path = _resolve(record["paths"][name])
    if not path.is_file():
        raise FreezeError(f"frozen input '{name}' is missing")
    data = path.read_bytes()
    if sha256_bytes(data) != record["files"][name]:
        raise FreezeError(f"{name} changed after the freeze")
    return data


def verify_freeze(record_path: Path, root: Path = ROOT) -> Dict[str, Any]:
    record = load_record(record_path)
    verify_code(record, root)
    check_decision_rule(record)
    validate_set(RecordReader(record), record["code"], root)
    return record
