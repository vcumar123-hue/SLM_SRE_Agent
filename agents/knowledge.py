from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException
from models import FallbackControlRequest

# ---------------------------------------------------------------------------
# Module-level imports with graceful fallback
# ---------------------------------------------------------------------------

try:
    from knowledge_manager import (  # type: ignore[attr-defined]
        create_knowledge_candidate,
        list_candidates,
        get_candidate_by_id,
        find_resolution_by_classification,
        find_playbook_by_classification,
    )
except Exception:
    create_knowledge_candidate = None
    list_candidates = None
    get_candidate_by_id = None
    find_resolution_by_classification = None
    find_playbook_by_classification = None

try:
    from resolution_engine import get_resolution_engine  # type: ignore[attr-defined]
except Exception:
    get_resolution_engine = None

# ---------------------------------------------------------------------------
# Runtime toggle — mirrored from app.py module-level global
# ---------------------------------------------------------------------------

FALLBACK_RUNTIME_ENABLED: bool = True

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _automation_classification_key(value: str) -> str:
    """Normalize UI display labels into executable automation mapping keys."""
    token = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    aliases = {
        "vpn_client_failure": "vpn_client_failure",
        "vpn_client_failures": "vpn_client_failure",
        "vpn_failure": "vpn_client_failure",
        "vpn_connectivity_failure": "vpn_client_failure",
        "vpn_connection_failure": "vpn_client_failure",
        "vpn_user_connectivity_failure": "vpn_client_failure",
    }
    return aliases.get(token, token)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/knowledge/sources")
def knowledge_sources_route() -> Dict[str, Any]:
    """UX-09: Return knowledge source file status for the Knowledge Center dashboard."""
    def _file_status(path: Path, label: str, key: str) -> Dict[str, Any]:
        if not path.exists():
            return {"label": label, "key": key, "status": "missing", "count": 0, "size_kb": 0, "path": str(path)}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
            count = len(items)
            size_kb = round(path.stat().st_size / 1024, 1)
            return {"label": label, "key": key, "status": "ok", "count": count, "size_kb": size_kb, "path": str(path)}
        except Exception:
            return {"label": label, "key": key, "status": "error", "count": 0, "size_kb": 0, "path": str(path)}

    from knowledge_manager import (  # type: ignore[attr-defined]
        KNOWLEDGE_CANDIDATES_FILE,
        GLOBAL_LEARNED_PATTERNS_FILE,
        PLAYBOOKS_FILE,
        RESOLUTIONS_FILE,
    )

    sources = [
        _file_status(Path(str(KNOWLEDGE_CANDIDATES_FILE)), "Knowledge Candidates", "candidates"),
        _file_status(Path(str(GLOBAL_LEARNED_PATTERNS_FILE)), "Global Learned Patterns", "global_patterns"),
        _file_status(Path(str(PLAYBOOKS_FILE)), "Playbooks", "playbooks"),
        _file_status(Path(str(RESOLUTIONS_FILE)), "Resolutions", "resolutions"),
    ]
    return {"status": "ok", "sources": sources}


@router.get("/knowledge_candidates")
def knowledge_candidates_view(status: Optional[str] = None) -> Dict[str, Any]:
    if list_candidates is None:
        raise HTTPException(status_code=500, detail="knowledge_manager not available")
    return {"items": list_candidates(status=status)}


@router.get("/knowledge_candidates/{candidate_id}")
def knowledge_candidate_detail(candidate_id: str) -> Dict[str, Any]:
    if get_candidate_by_id is None:
        raise HTTPException(status_code=500, detail="knowledge_manager not available")
    item = get_candidate_by_id(candidate_id)
    if not item:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return item


@router.get("/resolution_by_classification/{classification}")
def resolution_by_classification(classification: str) -> Dict[str, Any]:
    item = None
    if get_resolution_engine is not None:
        try:
            item = get_resolution_engine().get_resolution_by_classification(classification)
        except Exception:
            item = None
    if item is None and find_resolution_by_classification is not None:
        item = find_resolution_by_classification(classification)
    return {"item": item}


@router.get("/playbook_by_classification/{classification}")
def playbook_by_classification(classification: str) -> Dict[str, Any]:
    item = None
    if get_resolution_engine is not None:
        try:
            item = get_resolution_engine().get_playbook_by_classification(classification)
        except Exception:
            item = None
    if item is None and find_playbook_by_classification is not None:
        item = find_playbook_by_classification(classification)
    return {"item": item}


@router.get("/fallback_control")
def fallback_control_view() -> Dict[str, Any]:
    return {
        "runtime_enabled": FALLBACK_RUNTIME_ENABLED,
    }


@router.post("/fallback_control")
def fallback_control_update(request: FallbackControlRequest) -> Dict[str, Any]:
    global FALLBACK_RUNTIME_ENABLED
    FALLBACK_RUNTIME_ENABLED = bool(request.enabled)
    return {"status": "ok", "runtime_enabled": FALLBACK_RUNTIME_ENABLED}
