"""
The R2-3 freeze (design 13.1 and 13.9): everything that decides the acceptance result is fixed, hashed and committed **before** the labelling reviewers see any acceptance ticket.

`create_freeze` refuses unless, at that moment:
  * the role record passes (`roles.require`: a labeler holds no other role; named holders; identities declared);
  * amendment A-1 has a standing approval in the owner's own words (`amendments.require`);
  * the calibration checks (reviewer agreement, blinding) have been recorded as passed (`calibration.require_passed`), on calibration tickets only;
  * the Release 2 configuration freeze is intact and its approval is confirmed (`release.problems()` is empty);
  * the 20 acceptance goldens parse under A-1 (per-ticket approval by the named golden approver, who is not a labeler; expected causes and evidence on each) and are
    separate from the calibration goldens;
  * the private draw verifies (every case is the ticket the selection recorded), holds 40 PYPL and 40 PFG real tickets, and with the goldens makes exactly 100 unique cases;
  * no label of any kind exists yet, and no earlier freeze exists.
It then hashes (CRLF read as LF) the gates and rubric (design sections 13.1-13.8), the draw script and seed record, the analysis code, the batch runner, the blinded review layout, the exclusion
matcher, this freeze code, the Release 2 configuration freeze, the roles and amendments records, the private goldens, the private selection and the private cases, and writes the record. Only
hashes and ids are committed; no ticket text is. `verify_freeze` recomputes every hash and reports each difference.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from planner_experiment import acceptance, amendments, golden, roles
from planner_experiment import select_cases as sc

ROOT = Path(__file__).resolve().parent.parent
FREEZE_VERSION = "1"
FREEZE_PATH = ROOT / "docs" / "evaluation" / "freeze_record.json"
IDS_PATH = ROOT / "docs" / "evaluation" / "acceptance_ids.json"
DESIGN_PATH = ROOT / "docs" / "RELEASE_2_DESIGN.md"
PUBLIC_ITEMS = {
    "protocol": "docs/evaluation/R2_3_PROTOCOL.md",
    "draw_script": "planner_experiment/acceptance.py",
    "seed_record": "docs/baselines/release2/seed_acceptance.json",
    "analysis_code": "planner_experiment/evaluate_release2.py",
    "batch_runner": "planner_experiment/batch_runner.py",
    "review_layout": "planner_experiment/review_layout.py",
    "run_once": "planner_experiment/acceptance_run.py",
    "live_executor": "planner_experiment/acceptance_executor.py",
    "calibration_code": "planner_experiment/calibration.py",
    "calibration_result": "docs/evaluation/calibration_result.json",
    "exclusion": "planner_experiment/exclusion.py",
    "freeze_code": "planner_experiment/acceptance_freeze.py",
    "release_freeze": "docs/baselines/release2/rules_only_release_freeze.json",
    "roles": "docs/evaluation/roles.json",
    "amendments": "docs/evaluation/amendments.json",
}
PRIVATE_ITEMS = {"golden_tickets": "acceptance_golden_tickets.json", "selection": "acceptance_selection.json", "cases": "acceptance_cases.json"}
LABEL_PATTERNS = ("labels_*.json", "label_*.json", "adjudicated_*.json", "review_*.json", "outputs_*.json")


class FreezeError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def design_sections_hash(start: str = "### 13.1 ", end: str = "### 13.9 ") -> str:
    """The text of the design's gates, rubric and scoring rules (13.1 up to 13.9), so later edits to other sections do not move the freeze but any edit to these does."""
    text = DESIGN_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")
    a, b = text.find(start), text.find(end)
    if a < 0 or b < 0 or b <= a:
        raise FreezeError("the design's evaluation sections (13.1 to 13.9) were not found")
    return hashlib.sha256(text[a:b].encode("utf-8")).hexdigest()


def _public_hashes(root: Path) -> Dict[str, Dict[str, str]]:
    out = {}
    for name, rel in PUBLIC_ITEMS.items():
        path = root / rel
        if not path.is_file():
            raise FreezeError(f"frozen item missing: {rel}")
        out[name] = {"path": rel, "sha256": sha256_file(path)}
    return out


def preflight(private_dir: Path, root: Path = ROOT) -> Dict[str, Any]:
    """Every precondition; raises FreezeError naming the first that fails. Returns the parsed goldens and the verified selection."""
    private = Path(private_dir)
    try:
        record = roles.require(root / "docs" / "evaluation" / "roles.json")
    except roles.RoleError as exc:
        raise FreezeError(str(exc)) from exc
    try:
        amendments.require("A-1", root / "docs" / "evaluation" / "amendments.json")
    except amendments.AmendmentError as exc:
        raise FreezeError(str(exc)) from exc
    from planner_experiment import calibration
    try:
        calibration.require_passed(root / "docs" / "evaluation" / "calibration_result.json")
    except calibration.CalibrationError as exc:
        raise FreezeError(str(exc)) from exc
    from agentruntime import release
    problems = release.problems()
    if problems:
        raise FreezeError("the Release 2 configuration freeze has problems: " + "; ".join(problems))
    labelers = [record["roles"]["labeler_1"], record["roles"]["labeler_2"]]
    for pattern in LABEL_PATTERNS:
        found = list(private.glob(pattern))
        if found:
            raise FreezeError(f"labels or reviewer outputs already exist ({found[0].name}): the freeze must come before any labelling")
    missing = [n for n in PRIVATE_ITEMS.values() if not (private / n).is_file()]
    if missing:
        raise FreezeError(f"private package incomplete: {missing}")
    try:
        goldens = golden.parse_acceptance_golden(json.loads((private / PRIVATE_ITEMS["golden_tickets"]).read_text(encoding="utf-8")), labelers,
                                                 [record["roles"]["golden_approver"]])
        calibration = golden.parse_golden(json.loads((root / "data" / "golden_tickets.json").read_text(encoding="utf-8")), ["Vijay", "Sara"])
        golden.check_separation(calibration, goldens)
        acceptance.verify_selection(private / PRIVATE_ITEMS["selection"], private / PRIVATE_ITEMS["cases"])
    except (golden.GoldenError, acceptance.AcceptanceError, OSError, ValueError) as exc:
        raise FreezeError(f"the acceptance package does not verify: {exc}") from exc
    if len(goldens) != 20:
        raise FreezeError(f"exactly 20 acceptance goldens are required, found {len(goldens)}")
    selection = json.loads((private / PRIVATE_ITEMS["selection"]).read_text(encoding="utf-8"))
    per_pool: Dict[str, int] = {}
    for e in selection["cases"]:
        per_pool[e["pool"]] = per_pool.get(e["pool"], 0) + 1
    if per_pool != {"client_c": 40, "client_a": 40}:
        raise FreezeError(f"the real tickets must be 40 PYPL (client_c) and 40 PFG (client_a), found {per_pool}")
    ids = [e["case_id"] for e in selection["cases"]] + [g["ticket_id"] for g in goldens]
    if len(ids) != 100 or len(set(ids)) != 100:
        raise FreezeError("the package must hold exactly 100 unique cases")
    return {"roles": record, "goldens": goldens, "selection": selection, "ids": ids}


def create_freeze(private_dir: Path, out_path: Optional[Path] = None, ids_path: Optional[Path] = None, root: Path = ROOT) -> Dict[str, Any]:
    out, ids_out = Path(out_path) if out_path else root / "docs" / "evaluation" / "freeze_record.json", Path(ids_path) if ids_path else root / "docs" / "evaluation" / "acceptance_ids.json"
    if out.exists() or ids_out.exists():
        raise FreezeError("an acceptance freeze already exists: it is created once; a changed item burns the set and needs a new set and a new freeze")
    checked = preflight(private_dir, root)
    private = Path(private_dir)
    items = _public_hashes(root)
    package = {name: {"file": fname, "sha256": sha256_file(private / fname)} for name, fname in PRIVATE_ITEMS.items()}
    record = {
        "freeze_version": FREEZE_VERSION, "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "items": items,
        "gates_and_rubric": {"source": "docs/RELEASE_2_DESIGN.md sections 13.1 to 13.8", "sha256": design_sections_hash()},
        "package": package, "package_sha256": hashlib.sha256("|".join(f"{n}:{package[n]['sha256']}" for n in sorted(package)).encode("utf-8")).hexdigest(),
        "cases": len(checked["ids"]), "real_tickets": 80, "goldens": 20,
        "roles": checked["roles"]["roles"], "amendments_approved": ["A-1"], "labels_existed_at_freeze": False,
        "note": "Only hashes and ids are committed. The ticket text stays in the private package, which is revealed to the labelling reviewers only after this record exists.",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    ids_out.write_text(json.dumps({"cases": sorted(checked["ids"]), "sha256_of_ids": hashlib.sha256("\n".join(sorted(checked["ids"])).encode("utf-8")).hexdigest()}, indent=1) + "\n", encoding="utf-8")
    return record


def verify_freeze(private_dir: Path, record_path: Optional[Path] = None, root: Path = ROOT) -> List[str]:
    """Every difference between the record and what is on disk now (empty when nothing changed)."""
    p = Path(record_path) if record_path else root / "docs" / "evaluation" / "freeze_record.json"
    try:
        record = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"the freeze record cannot be read: {exc}"]
    out: List[str] = []
    for name, entry in record.get("items", {}).items():
        path = root / entry["path"]
        if not path.is_file():
            out.append(f"{name}: file missing ({entry['path']})")
        elif sha256_file(path) != entry["sha256"]:
            out.append(f"{name}: changed ({entry['path']})")
    for name in sorted(set(PUBLIC_ITEMS) - set(record.get("items", {}))):
        out.append(f"{name}: not in the record")
    try:
        if design_sections_hash() != record["gates_and_rubric"]["sha256"]:
            out.append("gates_and_rubric: the design's evaluation sections changed")
    except FreezeError as exc:
        out.append(str(exc))
    for name, entry in record.get("package", {}).items():
        path = Path(private_dir) / entry["file"]
        if not path.is_file():
            out.append(f"package {name}: missing")
        elif sha256_file(path) != entry["sha256"]:
            out.append(f"package {name}: changed")
    return out
