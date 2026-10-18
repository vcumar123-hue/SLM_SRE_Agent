"""
The R2-3 run-once command (design 13.1: "The acceptance set is run once").

`run_once` executes the frozen `rules_only` evaluation over the 100 frozen cases exactly once:
  1. the freeze must verify (`acceptance_freeze.verify_freeze`): any changed frozen item, or a changed private package, refuses the run;
  2. the adjudicated labels must be complete BEFORE any system output exists (13.4: "Labels are made before reviewers see any system output"): every case has both labelers' original labels and an
     adjudication, and the adjudication departs from the labelers only where they disagreed (the adjudicator never overwrites an agreement or changes an original label);
  3. the run is claimed atomically (O_CREAT|O_EXCL, keyed by the package hash). The claim stays whatever happens: a run that fails or aborts has still been attempted, and trying again until the result is
     acceptable is exactly what run-once forbids; another attempt needs a new set and a new freeze, or a recorded owner decision that removes the claim by hand;
  4. the executor (the batch runner) is called once with the 100 cases; its output is written privately and hashed.
Gate evaluation is a separate, later step (`evaluate_release2`), after the blinded semantic review.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from planner_experiment import acceptance_freeze as af

LABEL_FIELDS = ("determinable", "classification", "conflicting")


class RunError(Exception):
    pass


def load_cases(private_dir: Path) -> List[Dict[str, Any]]:
    private = Path(private_dir)
    real = json.loads((private / af.PRIVATE_ITEMS["cases"]).read_text(encoding="utf-8"))
    goldens = json.loads((private / af.PRIVATE_ITEMS["golden_tickets"]).read_text(encoding="utf-8"))
    cases = [{**c, "source": "real"} for c in real]
    cases += [{"case_id": g["ticket_id"], "pool": "golden", "tenant_id": g.get("tenant_id") or "poc_client", "ticket_id": g["ticket_id"], "ticket_text": g["text"], "host": g["host"],
               "opened_at": g["opened_at"], "source": "golden"} for g in goldens]
    return sorted(cases, key=lambda c: c["case_id"])


def check_labels(labels_path: Path, case_ids: List[str]) -> Dict[str, Any]:
    try:
        labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunError(f"the adjudicated labels cannot be read: {exc}") from exc
    if set(labels) != set(case_ids):
        raise RunError("the adjudicated labels must cover exactly the 100 frozen cases")
    for cid, entry in labels.items():
        for part in ("labeler_1", "labeler_2", "adjudicated"):
            if not isinstance(entry.get(part), dict) or entry[part].get("determinable") not in ("determinable", "not_determinable", "indeterminate"):
                raise RunError(f"{cid}: {part} label is missing or invalid")
        a, b, final = entry["labeler_1"], entry["labeler_2"], entry["adjudicated"]
        for field in LABEL_FIELDS:
            if a.get(field) == b.get(field) and final.get(field) != a.get(field):
                raise RunError(f"{cid}: the adjudication changed '{field}' where both labelers agreed; an adjudicator rules only on disagreements")
        if a.get("determinable") != b.get("determinable") and not str(final.get("rationale") or "").strip():
            raise RunError(f"{cid}: a disagreement needs the adjudicator's written rationale")
    return labels


def _write_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_once(private_dir: Path, labels_path: Path, results_dir: Path, executor: Callable[[List[Dict[str, Any]]], Dict[str, Any]], record_path: Optional[Path] = None,
             root: Path = af.ROOT) -> Dict[str, Any]:
    problems = af.verify_freeze(private_dir, record_path, root)
    if problems:
        raise RunError("the freeze does not verify, so nothing is run: " + "; ".join(problems[:5]))
    record = json.loads((Path(record_path) if record_path else root / "docs" / "evaluation" / "freeze_record.json").read_text(encoding="utf-8"))
    cases = load_cases(private_dir)
    if len(cases) != 100:
        raise RunError(f"expected 100 cases, found {len(cases)}")
    check_labels(labels_path, [c["case_id"] for c in cases])
    results = Path(results_dir)
    results.mkdir(parents=True, exist_ok=True)
    stem = f"acceptance_{record['package_sha256'][:16]}"
    claim, out = results / f"{stem}.claim.json", results / f"{stem}.outputs.json"
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            state = json.loads(claim.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            state = "unknown"
        raise RunError(f"the acceptance set has already been run (status: {state}); it is run once. Another attempt needs a new set and a new freeze, or a recorded owner decision to remove {claim.name}") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"status": "running", "package_sha256": record["package_sha256"], "pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, handle)
    try:
        outcome = executor(cases)
    except BaseException as exc:                                          # noqa: BLE001 - recorded, then re-raised; the claim stays
        _write_atomic(claim, {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "package_sha256": record["package_sha256"]})
        raise
    _write_atomic(out, outcome)
    _write_atomic(claim, {"status": "completed", "outputs": out.name, "outputs_sha256": af.sha256_file(out), "package_sha256": record["package_sha256"], "cases": len(cases),
                          "reruns": len(outcome.get("reruns", [])), "deviation": bool(outcome.get("deviation"))})
    return {"outputs": str(out), "outputs_sha256": af.sha256_file(out), "cases": len(cases), "reruns": len(outcome.get("reruns", [])), "deviation": bool(outcome.get("deviation"))}
