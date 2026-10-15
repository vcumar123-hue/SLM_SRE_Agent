"""
Freeze record for the planner experiment (design 12, 13.1).

The inputs are hashed together before the experiment runs: the adjudicated annotations, the case list, the recording, the injection cases,
the decision rule and scoring code, the planner implementations and prompts under test, and the policy. `create_freeze` refuses unless two
named, distinct reviewers are recorded and their agreement report is included; `verify_freeze` is called by the runner and refuses to run
if any hashed file has changed.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PLACEHOLDER_NAMES = {"", "tbd", "todo", "unknown", "reviewer", "reviewer1", "reviewer2", "adjudicated"}
HARNESS_FILES = ("metrics.py", "injection.py", "annotations.py", "scripted_env.py", "experiment.py", "select_cases.py")


class FreezeError(Exception):
    pass


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def harness_paths() -> List[Path]:
    root = Path(__file__).resolve().parent
    return [root / name for name in HARNESS_FILES]


def create_freeze(files: Dict[str, Path], reviewers: Iterable[str], agreement_report: Path, decision: Dict[str, Any],
                  out_path: Path) -> Dict[str, Any]:
    names = [str(r).strip() for r in reviewers]
    if len({n.lower() for n in names}) < 2 or any(n.lower() in PLACEHOLDER_NAMES for n in names):
        raise FreezeError("two distinct, named reviewers are required before the experiment inputs can be frozen")
    if not Path(agreement_report).is_file():
        raise FreezeError("the reviewer agreement report is required")
    hashed = {name: file_hash(p) for name, p in sorted(files.items())}
    hashed.update({f"harness/{p.name}": file_hash(p) for p in harness_paths()})
    hashed["agreement_report"] = file_hash(agreement_report)
    record = {"frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reviewers": names, "files": hashed,
              "paths": {k: str(v) for k, v in files.items()}, "decision_rule": decision}
    record["record_hash"] = hashlib.sha256(json.dumps({k: record[k] for k in ("reviewers", "files", "decision_rule")}, sort_keys=True).encode()).hexdigest()
    Path(out_path).write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return record


def verify_freeze(record_path: Path, files: Optional[Dict[str, Path]] = None) -> Dict[str, Any]:
    if not Path(record_path).is_file():
        raise FreezeError("no freeze record: the experiment may not run before the inputs are frozen")
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    expect = hashlib.sha256(json.dumps({k: record[k] for k in ("reviewers", "files", "decision_rule")}, sort_keys=True).encode()).hexdigest()
    if expect != record.get("record_hash"):
        raise FreezeError("the freeze record itself was modified")
    paths = {**{k: Path(v) for k, v in record["paths"].items()}, **(files or {})}
    for name, digest in record["files"].items():
        if name.startswith("harness/"):
            path = Path(__file__).resolve().parent / name.split("/", 1)[1]
        elif name == "agreement_report":
            continue                                         # hashed at creation; its content is not an input to a run
        else:
            path = paths[name]
        if not path.is_file() or file_hash(path) != digest:
            raise FreezeError(f"{name} changed after the freeze")
    return record
