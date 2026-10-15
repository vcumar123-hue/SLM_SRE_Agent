"""
Freeze record for the planner experiment (design 12, 13.1).

What is frozen, together, before the experiment runs:
  * the inputs the run actually reads: adjudicated annotations, case list, recording, injection cases, config (budgets, modes, seed,
    expected counts), the seed derivation record and the calibration selection;
  * all code under test: every `.py` file in `agentruntime/` (runtime, tools and the planners in `agentruntime/planners/`) and in
    `planner_experiment/` (harness, scoring and the decision rule). The set of files is frozen too, so an added or removed file fails.

`create_freeze` takes the two reviewers' own annotation files and recomputes their agreement itself (it never trusts a report): two
distinct named reviewers, one annotation per frozen case from each, every case compared, end-state kappa at least 0.6, and a written
rationale for every disagreement in the adjudicated file. The agreement result is stored in, and hashed with, the record. The experiment does not take its
inputs from the caller: `load_frozen_inputs` reads each input file once, checks that exact byte string against its recorded hash, and
parses it from those same bytes, so what was verified is what is used. A planner is accepted only if its code lives in the frozen
`agentruntime/planners/` folder (experiment.py checks this).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_NAMES = {"", "tbd", "todo", "unknown", "reviewer", "reviewer1", "reviewer2", "adjudicated"}
REQUIRED_FILES = ("annotations", "cases", "recording", "injection_cases", "config", "seed_record", "calibration_selection")
OPTIONAL_FILES = ("golden",)
CODE_ROOTS = ("agentruntime", "planner_experiment")
PLANNERS_DIR = ROOT / "agentruntime" / "planners"


class FreezeError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


MIN_KAPPA = 0.6          # design 13.4: end-state kappa must be at least 0.6


def check_reviewers(files: Dict[str, Path], reviewer_files: Dict[str, Path]) -> Dict[str, Any]:
    """Recompute reviewer agreement from the two reviewers' own annotation files; never trust a report. Returns the agreement report.

    Requires: exactly two distinct, named reviewers; each file holds one annotation per frozen case, all by that reviewer, no extras;
    every case compared; end-state kappa defined and at least MIN_KAPPA; and for every disagreement the adjudicated annotation carries a
    non-empty rationale in `notes`.
    """
    from planner_experiment import annotations as ann

    names = [str(n).strip() for n in reviewer_files]
    if len(names) != 2 or len({n.lower() for n in names}) != 2 or any(n.lower() in PLACEHOLDER_NAMES for n in names):
        raise FreezeError("exactly two distinct, named reviewers are required before the experiment inputs can be frozen")
    case_ids = {c["case_id"] for c in json.loads(Path(files["cases"]).read_text(encoding="utf-8"))}
    if not case_ids:
        raise FreezeError("the case list is empty")
    loaded: Dict[str, List[Any]] = {}
    for name, path in reviewer_files.items():
        items = ann.load(Path(path))
        ids = [a.case_id for a in items]
        if len(ids) != len(set(ids)) or set(ids) != case_ids:
            raise FreezeError(f"reviewer '{name}' must annotate exactly the {len(case_ids)} frozen cases, once each")
        if any(a.reviewer.strip().lower() != name.strip().lower() for a in items):
            raise FreezeError(f"reviewer '{name}': every annotation must be recorded under that reviewer's name")
        loaded[name] = items
    first, second = (loaded[n] for n in reviewer_files)
    report = ann.agreement(first, second)
    if report["cases_compared"] != len(case_ids):
        raise FreezeError(f"agreement covers {report['cases_compared']} of {len(case_ids)} cases")
    kappa = report["end_state_kappa"]
    if kappa is None or kappa < MIN_KAPPA:
        raise FreezeError(f"end-state kappa {kappa} is below the required {MIN_KAPPA}")
    adjudicated = {a.case_id: a for a in ann.load(Path(files["annotations"]))}
    if set(adjudicated) != case_ids:
        raise FreezeError("the adjudicated annotations must cover exactly the frozen cases")
    for d in report["disagreements"]:
        if not adjudicated[d["case_id"]].notes.strip():
            raise FreezeError(f"{d['case_id']}: a disagreement ({', '.join(d['fields'])}) needs an adjudication rationale in notes")
    return report


def create_freeze(files: Dict[str, Path], reviewer_files: Dict[str, Path], decision: Dict[str, Any], out_path: Path,
                  root: Path = ROOT) -> Dict[str, Any]:
    missing = [k for k in REQUIRED_FILES if k not in files]
    unknown = [k for k in files if k not in REQUIRED_FILES + OPTIONAL_FILES]
    if missing or unknown:
        raise FreezeError(f"frozen inputs must be exactly {REQUIRED_FILES} (optionally {OPTIONAL_FILES}); missing {missing}, unknown {unknown}")
    for k, p in {**files, **{f"reviewer:{n}": p for n, p in reviewer_files.items()}}.items():
        if not Path(p).is_file():
            raise FreezeError(f"input '{k}' does not exist: {p}")
    report = check_reviewers(files, reviewer_files)
    names = [str(n).strip() for n in reviewer_files]
    every = {**files, **{f"reviewer:{n}": p for n, p in reviewer_files.items()}}
    code = code_files(root)
    record = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reviewers": names,
        "files": {k: sha256_bytes(Path(p).read_bytes()) for k, p in sorted(every.items())},
        "paths": {k: _stored(p) for k, p in every.items()},
        "code": {k: sha256_bytes(p.read_bytes()) for k, p in code.items()},
        "agreement": report, "decision_rule": decision,
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
    for name in record["files"]:
        read_frozen(record, name)
    return record
