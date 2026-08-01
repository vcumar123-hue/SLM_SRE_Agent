from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["maintenance"])

SLM_ROOT_DIR = Path(os.getenv("AIOPS_SLM_ROOT", ".")).resolve()


@router.post("/maintenance/cleanup_backups")
def maintenance_cleanup_backups_route() -> Dict[str, Any]:
    """CQ-05: Remove .before_* playbook backup files left by edit operations."""
    import glob as _glob
    removed = []
    for pattern in [".before_*", "*.before_bak"]:
        for path_str in _glob.glob(str(SLM_ROOT_DIR / "**" / pattern), recursive=True):
            try:
                Path(path_str).unlink()
                removed.append(path_str)
            except Exception:
                pass
    return {"status": "ok", "removed_count": len(removed), "removed": removed}


@router.post("/maintenance/deduplicate_candidates")
def maintenance_deduplicate_candidates_route() -> Dict[str, Any]:
    """CQ-06: Deduplicate automation candidates by classification+playbook key."""
    from automation_candidate_registry import (
        load_candidates,
        save_candidates,
    )
    try:
        candidates = load_candidates()
        seen: dict = {}
        deduped = []
        duplicates = 0
        for c in candidates:
            key = f"{str(c.get('classification','')).lower()}::{str(c.get('playbook','')).lower()}"
            if key not in seen:
                seen[key] = True
                deduped.append(c)
            else:
                duplicates += 1
        if duplicates > 0:
            save_candidates(deduped)
        return {"status": "ok", "total_before": len(candidates), "total_after": len(deduped), "removed_duplicates": duplicates}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
