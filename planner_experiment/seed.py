"""
Deterministic seed derivation for the experiment's random draws.

A seed chosen by hand can be chosen after seeing what it draws (the first dry run used 20261014 and its counts were observed, so that
value is retired). A seed here is derived: sha256(f"{purpose}:{artifact}") from an immutable artifact (a full git commit id) fixed
**before** the draw, so nobody can pick it. The derivation is recorded in a small file; the selection command reads the seed from that
file only and recomputes it before use.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

ALGORITHM = "int(sha256(f'{purpose}:{artifact}').hexdigest()[:8], 16)"
RETIRED_SEEDS = {20261014: "chosen by hand; its calibration draw counts were observed before review"}
PURPOSES = ("calibration-selection",)


def derive_seed(artifact: str, purpose: str) -> int:
    artifact = str(artifact).strip().lower()
    if len(artifact) != 40 or any(c not in "0123456789abcdef" for c in artifact):
        raise ValueError("the artifact must be a full 40-character git commit id")
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose '{purpose}'")
    return int(hashlib.sha256(f"{purpose}:{artifact}".encode("utf-8")).hexdigest()[:8], 16)


def write_record(path: Path, artifact: str, purpose: str, note: str = "") -> Dict[str, Any]:
    record = {"purpose": purpose, "artifact": artifact.strip().lower(), "algorithm": ALGORITHM, "seed": derive_seed(artifact, purpose),
              "retired_seeds": {str(k): v for k, v in RETIRED_SEEDS.items()}, "note": note}
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} already exists: a seed derivation is recorded once")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return record


def load_record(path: Path, purpose: str) -> Dict[str, Any]:
    """Load and re-verify a record: the stored seed must equal the derivation, and must not be a retired seed."""
    return check_record(json.loads(Path(path).read_text(encoding="utf-8")), purpose)


def check_record(record: Dict[str, Any], purpose: str) -> Dict[str, Any]:
    if record.get("purpose") != purpose:
        raise ValueError(f"the seed record is for '{record.get('purpose')}', not '{purpose}'")
    if record["seed"] != derive_seed(record["artifact"], purpose):
        raise ValueError("the recorded seed does not match its derivation")
    if record["seed"] in RETIRED_SEEDS:
        raise ValueError("this seed is retired")
    return record
