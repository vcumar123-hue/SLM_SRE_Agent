"""
Planner freeze: the exact candidate planners are fixed BEFORE the 50-ticket experiment set is drawn, annotated or inspected.

Held-out design (review of 2026-10-15). The planners (rule table, prompts, constants) are built and tuned on the 10-ticket development
subset only. When they are final, `create_planner_freeze` records:
  * the planner entry points, one per mode, as "module:Class" in `agentruntime/planners/`;
  * the hash of every file under `agentruntime/` (so planners and the runtime they depend on cannot change afterwards);
  * the hash of the development selection they were developed against.
The 50-ticket set is then drawn with a seed derived from this record's hash, so the draw cannot precede the freeze and cannot be steered
by anything learned from the set. The experiment is the planner-selection test; the 100-ticket acceptance set stays the promotion test.

No tuning on the 50 after seeing results: a new round needs revised planners (a different freeze hash), developed on the development
subset only, and a fresh, previously unused set (earlier sets' clusters are excluded). A planner freeze identical to one already used by a
round is refused.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from planner_experiment.freeze import PLANNERS_DIR, ROOT, FreezeError, code_files, sha256_bytes

MODES = ("rules_only", "rules_then_model", "model")


class PlannerFreezeError(FreezeError):
    pass


def runtime_hashes(root: Path = ROOT) -> Dict[str, str]:
    return {k: sha256_bytes(p.read_bytes()) for k, p in code_files(root).items() if k.startswith("code/agentruntime/")}


def resolve_entrypoint(spec: str, planners_dir: Optional[Path] = None):
    """Import "module:Class" and return the class, only if it is defined in a file inside the frozen planners folder."""
    planners = Path(planners_dir or PLANNERS_DIR).resolve()
    if spec.count(":") != 1:
        raise PlannerFreezeError(f"planner entry point '{spec}' must look like module:Class")
    module_name, class_name = spec.split(":")
    module = importlib.import_module(module_name)
    source = Path(inspect.getsourcefile(module) or "").resolve()
    if planners not in source.parents:
        raise PlannerFreezeError(f"{module_name} is not a file in {planners.name}/: {source}")
    cls = getattr(module, class_name, None)
    if not inspect.isclass(cls) or cls.__module__ != module.__name__:
        raise PlannerFreezeError(f"{spec} is not a class defined in {module_name}")
    return cls


def _record_hash(record: Dict[str, Any]) -> str:
    body = {k: record[k] for k in ("entrypoints", "runtime_code", "development_selection_hash", "void_log_hash")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def used_planner_hashes(base: Path) -> List[str]:
    hashes = []
    for p in sorted(Path(base).glob("experiment_selection_round*.json")):
        h = json.loads(p.read_text(encoding="utf-8")).get("planner_freeze_hash")
        if h:
            hashes.append(h)
    return hashes


def create_planner_freeze(entrypoints: Dict[str, str], development_selection_path: Path, out_path: Path, base: Path,
                          root: Path = ROOT, planners_dir: Optional[Path] = None, void_log_path: Optional[Path] = None) -> Dict[str, Any]:
    if tuple(sorted(entrypoints)) != tuple(sorted(MODES)):
        raise PlannerFreezeError(f"entry points are needed for exactly {MODES}")
    for spec in entrypoints.values():
        resolve_entrypoint(spec, planners_dir)
    dev_path = Path(development_selection_path)
    if not dev_path.is_file():
        raise PlannerFreezeError("the development subset must be drawn before the planners are frozen (they are built and tuned on it)")
    out_path = Path(out_path)
    if out_path.exists():
        raise PlannerFreezeError(f"{out_path} already exists: a planner freeze is recorded once; revise the planners and write a new round")
    record = {"frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "entrypoints": dict(sorted(entrypoints.items())),
              "runtime_code": runtime_hashes(root), "development_selection_hash": sha256_bytes(dev_path.read_bytes()),
              # the void-and-redraw log (with the reviewers' acknowledgements) the planners were frozen under; None if the draw was never voided
              "void_log_hash": sha256_bytes(Path(void_log_path).read_bytes()) if void_log_path is not None else None}
    record["record_hash"] = _record_hash(record)
    if record["record_hash"] in used_planner_hashes(base):
        raise PlannerFreezeError("these planners are identical to a planner freeze already used by a round: revise them (on the development "
                                 "subset only) before drawing a fresh set; the same planners are never re-run on a new draw")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return record


def check_record(record: Dict[str, Any]) -> Dict[str, Any]:
    if _record_hash(record) != record.get("record_hash"):
        raise PlannerFreezeError("the planner freeze record was modified")
    return record


def load_planner_freeze(path: Path) -> Dict[str, Any]:
    if not Path(path).is_file():
        raise PlannerFreezeError("no planner freeze: the exact candidate planners must be frozen before the experiment set is drawn")
    return check_record(json.loads(Path(path).read_text(encoding="utf-8")))


def verify_unchanged(record: Dict[str, Any], root: Path = ROOT) -> None:
    """The planners and runtime on disk are exactly those frozen."""
    now = runtime_hashes(root)
    if now != record["runtime_code"]:
        changed = sorted({k for k in set(now) | set(record["runtime_code"]) if now.get(k) != record["runtime_code"].get(k)})
        raise PlannerFreezeError(f"planner or runtime code changed after the planner freeze: {changed[:5]}")
