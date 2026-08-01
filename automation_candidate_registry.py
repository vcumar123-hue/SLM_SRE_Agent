from __future__ import annotations

import json
import os
import threading
import uuid
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Per-file locks for read-modify-write safety under FastAPI's thread-pool.
_CANDIDATES_LOCK = threading.Lock()
_REGISTRY_LOCK = threading.Lock()


DATA_DIR = Path(os.getenv("AIOPS_DATA_DIR", "data"))
AUTOMATION_CANDIDATES_FILE = DATA_DIR / "automation_candidates.json"
AUTOMATION_REGISTRY_FILE = DATA_DIR / "automation_registry.json"
SLM_ROOT_DIR = Path(os.getenv("AIOPS_SLM_ROOT", ".")).resolve()
BAK_DIR = Path(os.getenv("AIOPS_ROLLBACK_BACKUP_DIR", str(SLM_ROOT_DIR / "bak"))).resolve()
PLAYBOOK_DIR = Path(os.getenv("AIOPS_AUTOMATION_PLAYBOOK_DIR", "/automation/playbooks"))
if not PLAYBOOK_DIR.parent.exists():
    PLAYBOOK_DIR = Path(os.getenv("AIOPS_AUTOMATION_PLAYBOOK_DIR_FALLBACK", "automation/playbooks"))


CANDIDATE_STATUSES = {
    "draft",
    "needs_playbook",
    "playbook_draft_generated",
    "playbook_created",
    "tested",
    "approved",
    "enabled",
    "rejected",
}


# Deterministic guardrails for known classifications. These must override
# keyword inference from draft text so dependency language such as
# "DNS/VPN are healthy" cannot accidentally select DNS/VPN automation.
FORCED_CLASSIFICATION_MAPPINGS: Dict[str, Dict[str, str]] = {
    "container_connection_issue": {
        "action": "validate_container_connectivity",
        "playbook": "container_connectivity_check.yml",
        "target_host": "linux-lab-01",
    },
    "disk_usage_exceeding_threshold": {
        "action": "cleanup_linux_temp_files",
        "playbook": "linux_disk_cleanup.yml",
        "target_host": "linux-lab-01",
    },
    "disk_usage_exceeds_threshold_due_to_temporary_files": {
        "action": "cleanup_linux_temp_files",
        "playbook": "linux_disk_cleanup.yml",
        "target_host": "linux-lab-01",
    },
    "vpn_client_failure": {
        "action": "recover_vpn_connectivity",
        "playbook": "vpn_recover_client_connectivity.yml",
        "target_host": "vpn-lab-01",
    },
}

PROTECTED_PLAYBOOK_CLASSIFICATIONS: Dict[str, set] = {
    "vpn_recover_client_connectivity.yml": {"vpn_client_failure"},
    "container_connectivity_check.yml": {"container_connection_issue"},
    "linux_disk_cleanup.yml": {
        "disk_usage_exceeding_threshold",
        "disk_usage_exceeds_threshold_due_to_temporary_files",
    },
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")
    text = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in text)
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _backup_file_if_exists(path: Path, suffix: str = ".bak") -> None:
    """Backup mutable registry/candidate files under <slm>/bak.

    This keeps lab rollback artifacts in a visible physical folder instead of
    creating sidecar .bak files next to the JSON data files.
    """
    try:
        if path.exists():
            BAK_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe_name = path.name.replace(os.sep, "_")
            backup = BAK_DIR / f"{safe_name}.{stamp}{suffix}"
            shutil.copy2(path, backup)
    except Exception:
        # Backup is a safety convenience; do not fail the main workflow.
        pass


def _forced_mapping(classification: Any) -> Dict[str, str]:
    return dict(FORCED_CLASSIFICATION_MAPPINGS.get(_normalize_key(classification), {}))


def _enforce_forced_mapping_on_candidate(candidate: Dict[str, Any]) -> None:
    # Normal incident-created candidates must respect protected/forced mappings so
    # container, VPN, disk, CPU, and API-gateway automations do not drift into the
    # wrong playbook.  Execution-feedback improvement candidates are different:
    # they intentionally create a new *_vN_candidate.yml draft tied to the existing
    # approved automation.  Preserve the requested v2/v3 candidate playbook there.
    if str(candidate.get("source") or "").strip().lower() == "execution_feedback":
        forced = _forced_mapping(candidate.get("classification"))
        if forced and not candidate.get("suggested_action"):
            candidate["suggested_action"] = forced["action"]
        if forced and not candidate.get("target_host"):
            candidate["target_host"] = forced["target_host"]
        return

    forced = _forced_mapping(candidate.get("classification"))
    if not forced:
        return
    candidate["suggested_action"] = forced["action"]
    candidate["suggested_playbook"] = forced["playbook"]
    candidate["target_host"] = forced["target_host"]


def _is_mismatched_protected_playbook(classification: str, playbook_name: str) -> bool:
    allowed = PROTECTED_PLAYBOOK_CLASSIFICATIONS.get(_safe_playbook_name(playbook_name), set())
    return bool(allowed and _normalize_key(classification) not in allowed)


def _safe_write_json(path: Path, payload: Any) -> None:
    """Atomically write JSON to avoid partial/empty files during reloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2)
    # Validate before touching the target file.
    json.loads(encoded)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(path))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _clean_list(values: Optional[List[Any]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _load_candidates() -> List[Dict[str, Any]]:
    data = _safe_read_json(AUTOMATION_CANDIDATES_FILE, default=[])
    return data if isinstance(data, list) else []


def _save_candidates(items: List[Dict[str, Any]]) -> None:
    with _CANDIDATES_LOCK:
        _safe_write_json(AUTOMATION_CANDIDATES_FILE, items)


# CQ-06: Public aliases for maintenance endpoint
load_candidates = _load_candidates
save_candidates = _save_candidates


def _load_registry() -> Dict[str, Any]:
    data = _safe_read_json(AUTOMATION_REGISTRY_FILE, default={})
    return data if isinstance(data, dict) else {}


def _save_registry(payload: Dict[str, Any]) -> None:
    """Defensively persist the automation registry.

    This function prevents a single candidate enablement from wiping the full
    registry because of an empty in-memory payload or a reload/interruption.
    """
    if not isinstance(payload, dict):
        raise ValueError("automation registry payload must be a dictionary")

    with _REGISTRY_LOCK:
        existing = _safe_read_json(AUTOMATION_REGISTRY_FILE, default={})
        existing_count = len(existing) if isinstance(existing, dict) else 0

        if existing_count > 0 and len(payload) == 0:
            raise ValueError("refusing to overwrite non-empty automation registry with empty payload")

        # Each enabled mapping must be minimally executable.
        for key, value in payload.items():
            if not isinstance(value, dict):
                raise ValueError(f"registry entry {key} must be an object")
            if value.get("enabled", True):
                for required in ("classification", "action", "playbook"):
                    if not str(value.get(required) or "").strip():
                        raise ValueError(f"registry entry {key} missing required field: {required}")

        _backup_file_if_exists(AUTOMATION_REGISTRY_FILE, suffix=".autosave_before_write")
        _safe_write_json(AUTOMATION_REGISTRY_FILE, payload)


def list_automation_candidates(
    status: Optional[str] = None,
    classification: Optional[str] = None,
) -> List[Dict[str, Any]]:
    items = _load_candidates()
    status_norm = _normalize_key(status)
    class_norm = _normalize_key(classification)
    if status_norm:
        items = [x for x in items if _normalize_key(x.get("status", "")) == status_norm]
    if class_norm:
        items = [x for x in items if _normalize_key(x.get("classification", "")) == class_norm]
    return items


def get_automation_candidate(candidate_id: str) -> Optional[Dict[str, Any]]:
    cid = str(candidate_id or "").strip()
    for item in _load_candidates():
        if str(item.get("candidate_id")) == cid:
            return item
    return None


def create_automation_candidate(
    incident_id: str,
    classification: str,
    draft_runbook: Optional[Dict[str, Any]] = None,
    draft_steps: Optional[List[Any]] = None,
    suggested_action: Optional[str] = None,
    suggested_playbook: Optional[str] = None,
    target_host: Optional[str] = None,
    target_group: Optional[str] = None,
    risk_tier: str = "medium",
    approval_required: bool = True,
    created_by: str = "engineer",
    source: str = "ui_draft_from_analysis",
    notes: str = "",
) -> Dict[str, Any]:
    classification_norm = _normalize_key(classification)
    if not classification_norm:
        return {"status": "invalid", "detail": "classification is required"}

    candidate = {
        "candidate_id": f"ac_{uuid.uuid4().hex[:10]}",
        "incident_id": str(incident_id or ""),
        "classification": classification_norm,
        "source": source or "ui_draft_from_analysis",
        "status": "draft",
        "lifecycle_stage": "automation_candidate",
        "created_by": created_by or "engineer",
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
        "risk_tier": str(risk_tier or "medium").strip().lower(),
        "approval_required": bool(approval_required),
        "suggested_action": _normalize_key(suggested_action),
        "suggested_playbook": str(suggested_playbook or "").strip(),
        "target_host": str(target_host or "").strip(),
        "target_group": str(target_group or "").strip(),
        "draft_runbook": draft_runbook if isinstance(draft_runbook, dict) else {},
        "draft_steps": _clean_list(draft_steps),
        "test_results": [],
        "generated_playbook": {},
        "playbook_review": {},
        "approval": {},
        "enabled": False,
        "notes": str(notes or ""),
    }

    _enforce_forced_mapping_on_candidate(candidate)

    items = _load_candidates()
    items.append(candidate)
    _save_candidates(items)
    return {"status": "ok", "candidate": candidate}


def update_automation_candidate(
    candidate_id: str,
    updated_by: str = "engineer",
    status: Optional[str] = None,
    suggested_action: Optional[str] = None,
    suggested_playbook: Optional[str] = None,
    target_host: Optional[str] = None,
    target_group: Optional[str] = None,
    risk_tier: Optional[str] = None,
    approval_required: Optional[bool] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    items = _load_candidates()
    found = None
    for item in items:
        if str(item.get("candidate_id")) == str(candidate_id or "").strip():
            found = item
            break

    if found is None:
        return {"status": "not_found", "candidate_id": candidate_id}

    if status is not None:
        status_norm = _normalize_key(status)
        if status_norm not in CANDIDATE_STATUSES:
            return {"status": "invalid", "detail": f"Invalid status: {status}"}
        found["status"] = status_norm

    if suggested_action is not None:
        found["suggested_action"] = _normalize_key(suggested_action)
    if suggested_playbook is not None:
        found["suggested_playbook"] = str(suggested_playbook or "").strip()
    if target_host is not None:
        found["target_host"] = str(target_host or "").strip()
    if target_group is not None:
        found["target_group"] = str(target_group or "").strip()
    if risk_tier is not None:
        found["risk_tier"] = str(risk_tier or "medium").strip().lower()
    if approval_required is not None:
        found["approval_required"] = bool(approval_required)
    if notes is not None:
        found["notes"] = str(notes or "")

    found["updated_by"] = updated_by or "engineer"
    found["updated_at"] = _utc_now_iso()
    _save_candidates(items)
    return {"status": "ok", "candidate": found}


def record_candidate_test_result(
    candidate_id: str,
    test_status: str,
    tested_by: str = "engineer",
    execution_mode: str = "simulation",
    stdout: str = "",
    stderr: str = "",
    returncode: Optional[int] = None,
    notes: str = "",
) -> Dict[str, Any]:
    items = _load_candidates()
    found = None
    for item in items:
        if str(item.get("candidate_id")) == str(candidate_id or "").strip():
            found = item
            break

    if found is None:
        return {"status": "not_found", "candidate_id": candidate_id}

    result = {
        "test_id": f"act_{uuid.uuid4().hex[:10]}",
        "tested_at": _utc_now_iso(),
        "tested_by": tested_by or "engineer",
        "execution_mode": execution_mode or "simulation",
        "test_status": _normalize_key(test_status or "unknown"),
        "returncode": returncode,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "notes": notes or "",
    }

    found.setdefault("test_results", []).append(result)
    if result["test_status"] in {"pass", "passed", "success", "completed_live", "completed_simulation"}:
        found["status"] = "tested"
    found["updated_at"] = _utc_now_iso()
    found["updated_by"] = tested_by or "engineer"
    _save_candidates(items)
    return {"status": "ok", "candidate": found, "test_result": result}


def approve_automation_candidate(
    candidate_id: str,
    approved_by: str = "engineer",
    approval_notes: str = "",
) -> Dict[str, Any]:
    items = _load_candidates()
    found = None
    for item in items:
        if str(item.get("candidate_id")) == str(candidate_id or "").strip():
            found = item
            break

    if found is None:
        return {"status": "not_found", "candidate_id": candidate_id}

    if not str(found.get("suggested_action") or "").strip():
        return {"status": "invalid", "detail": "suggested_action is required before approval"}
    if not str(found.get("suggested_playbook") or "").strip():
        return {"status": "invalid", "detail": "suggested_playbook is required before approval"}
    generated = found.get("generated_playbook") if isinstance(found.get("generated_playbook"), dict) else {}
    if not generated.get("saved"):
        return {"status": "invalid", "detail": "reviewed playbook must be saved before candidate approval"}

    found["status"] = "approved"
    found["approved"] = True
    found["approved_by"] = approved_by or "engineer"
    found["approved_at"] = _utc_now_iso()
    found["approval"] = {
        "approved": True,
        "approved_by": approved_by or "engineer",
        "approved_at": found["approved_at"],
        "approval_notes": approval_notes or "",
    }
    found["updated_at"] = _utc_now_iso()
    found["updated_by"] = approved_by or "engineer"
    _save_candidates(items)
    return {"status": "ok", "candidate": found}



def _candidate_to_registry_entry(candidate: Dict[str, Any], enabled_by: str = "engineer") -> Dict[str, Any]:
    classification = _normalize_key(candidate.get("classification", ""))
    forced = _forced_mapping(classification)
    source = str(candidate.get("source") or "").strip().lower()

    # Normal candidates remain pinned to deterministic protected mappings.
    # Feedback-driven improvement candidates are intentionally allowed to promote
    # a reviewed *_vN_candidate.yml playbook for the SAME existing action.  The
    # original playbook remains active until this candidate is approved/enabled.
    if forced and source != "execution_feedback":
        action = forced["action"]
        playbook = forced["playbook"]
        target_host = forced["target_host"]
    else:
        action = _normalize_key(candidate.get("suggested_action", "")) or (forced.get("action") if forced else "")
        playbook = _safe_playbook_name(candidate.get("suggested_playbook", ""))
        target_host = str(candidate.get("target_host") or "").strip() or (forced.get("target_host") if forced else "")

    if not classification or not action or not playbook:
        raise ValueError("classification, action, and playbook are required")
    if _is_mismatched_protected_playbook(classification, playbook):
        playbook = f"{classification}_candidate.yml"

    return {
        "classification": classification,
        "action": action,
        "actions": [action],
        "connector": "ansible",
        "playbook": playbook,
        "target_host": target_host,
        "target_group": str(candidate.get("target_group") or "").strip(),
        "risk_tier": str(candidate.get("risk_tier") or "medium").strip().lower(),
        "approval_required": bool(candidate.get("approval_required", True)),
        "enabled": True,
        "source": "automation_candidate_registry",
        "source_candidate_id": candidate.get("candidate_id"),
        "enabled_by": enabled_by or "engineer",
        "enabled_at": _utc_now_iso(),
        # Autonomous execution is a separate maturity state. A registry mapping
        # can be approved/enabled for human-in-the-loop execution while still
        # being blocked from telemetry/autonomous execution.
        "autonomous_enabled": bool(candidate.get("autonomous_enabled", False)),
        "autonomy_mode": "autonomous_enabled" if bool(candidate.get("autonomous_enabled", False)) else "human_in_the_loop",
        "telemetry_autonomous_allowed": bool(candidate.get("autonomous_enabled", False)),
    }


def rebuild_automation_registry_from_enabled_candidates(rebuilt_by: str = "system_rebuild") -> Dict[str, Any]:
    """Rebuild the registry from enabled candidates.

    This is a recovery helper for cases where data/automation_registry.json is
    accidentally emptied while data/automation_candidates.json still contains
    enabled candidates. Forced classification mappings are applied during rebuild.
    """
    registry: Dict[str, Any] = {}
    for candidate in _load_candidates():
        if _normalize_key(candidate.get("status", "")) != "enabled" and not bool(candidate.get("enabled")):
            continue
        try:
            entry = _candidate_to_registry_entry(candidate, enabled_by=rebuilt_by)
        except Exception:
            continue
        registry[entry["classification"]] = entry

    if registry:
        _save_registry(registry)
    return registry

def enable_automation_candidate(
    candidate_id: str,
    enabled_by: str = "engineer",
) -> Dict[str, Any]:
    items = _load_candidates()
    found = None
    for item in items:
        if str(item.get("candidate_id")) == str(candidate_id or "").strip():
            found = item
            break

    if found is None:
        return {"status": "not_found", "candidate_id": candidate_id}

    classification = _normalize_key(found.get("classification", ""))

    # Idempotent enablement: a second UI click after successful enable should
    # not return 400. Rebuild/repair the registry entry if needed and return ok.
    if _normalize_key(found.get("status", "")) == "enabled" or bool(found.get("enabled")):
        forced = _forced_mapping(classification)
        if forced and str(found.get("source") or "").strip().lower() != "execution_feedback":
            found["suggested_action"] = forced["action"]
            found["suggested_playbook"] = forced["playbook"]
            found["target_host"] = forced["target_host"]
        elif forced:
            found["suggested_action"] = found.get("suggested_action") or forced["action"]
            found["target_host"] = found.get("target_host") or forced["target_host"]
        try:
            entry = _candidate_to_registry_entry(found, enabled_by=enabled_by or found.get("enabled_by") or "engineer")
        except ValueError as exc:
            return {"status": "invalid", "detail": str(exc)}
        registry = _load_registry()
        existing_entry = registry.get(entry["classification"], {}) if isinstance(registry, dict) else {}
        if existing_entry != entry:
            registry[entry["classification"]] = entry
            _save_registry(registry)
        found["status"] = "enabled"
        found["enabled"] = True
        found.setdefault("enabled_at", entry.get("enabled_at"))
        found.setdefault("enabled_by", enabled_by or "engineer")
        if not found.get("approved"):
            found["approved"] = True
            found.setdefault("approved_by", enabled_by or "engineer")
            found.setdefault("approved_at", found.get("enabled_at", _utc_now_iso()))
        _save_candidates(items)
        return {"status": "ok", "already_enabled": True, "candidate": found, "registry_entry": registry.get(entry["classification"], entry)}

    if _normalize_key(found.get("status", "")) != "approved":
        return {"status": "invalid", "detail": "candidate must be approved before enablement"}

    forced = _forced_mapping(classification)
    if forced and str(found.get("source") or "").strip().lower() != "execution_feedback":
        # Enforce deterministic approved mapping for normal candidates at
        # enable-time too, even if an older UI draft saved a bad suggestion.
        found["suggested_action"] = forced["action"]
        found["suggested_playbook"] = forced["playbook"]
        found["target_host"] = forced["target_host"]
    elif forced:
        # Improvement candidates preserve the reviewed v2/v3 playbook while
        # staying tied to the existing approved automation action/target.
        found["suggested_action"] = found.get("suggested_action") or forced["action"]
        found["target_host"] = found.get("target_host") or forced["target_host"]

    try:
        entry = _candidate_to_registry_entry(found, enabled_by=enabled_by)
    except ValueError as exc:
        return {"status": "invalid", "detail": str(exc)}

    registry = _load_registry()
    registry[entry["classification"]] = entry
    _save_registry(registry)

    found["status"] = "enabled"
    found["enabled"] = True
    found["enabled_at"] = entry["enabled_at"]
    found["enabled_by"] = enabled_by or "engineer"
    # Enabling implies approval; stamp approved=True if not already set.
    if not found.get("approved"):
        found["approved"] = True
        found.setdefault("approved_by", enabled_by or "engineer")
        found.setdefault("approved_at", found["enabled_at"])
    found["updated_at"] = _utc_now_iso()
    found["updated_by"] = enabled_by or "engineer"
    _save_candidates(items)

    return {"status": "ok", "candidate": found, "registry_entry": entry}



def _find_registry_entry(registry: Dict[str, Any], classification: str = "", action: str = "", playbook: str = "") -> Optional[tuple]:
    class_key = _normalize_key(classification)
    action_key = _normalize_key(action)
    playbook_name = _safe_playbook_name(playbook)
    if class_key and class_key in registry:
        return class_key, registry[class_key]
    for key, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        if action_key and _normalize_key(entry.get("action")) == action_key:
            if not playbook_name or _safe_playbook_name(entry.get("playbook")) == playbook_name:
                return key, entry
        if playbook_name and _safe_playbook_name(entry.get("playbook")) == playbook_name:
            return key, entry
    return None


def set_autonomous_registry_status(
    classification: str,
    action: str = "",
    playbook: str = "",
    enabled: bool = True,
    requested_by: str = "engineer",
    confidence_summary: Optional[Dict[str, Any]] = None,
    notes: str = "",
) -> Dict[str, Any]:
    """Persist autonomous readiness on the approved registry mapping only.

    This intentionally does not create a new mapping and does not execute anything.
    It only marks an already-approved human-in-loop mapping as eligible for future
    telemetry/autonomous orchestration after backend eligibility checks pass.
    """
    registry = _load_registry()
    match = _find_registry_entry(registry, classification=classification, action=action, playbook=playbook)
    if not match:
        return {"status": "not_found", "detail": "approved registry mapping not found"}
    key, entry = match
    if not isinstance(entry, dict):
        return {"status": "invalid", "detail": "registry entry is not an object"}

    now = _utc_now_iso()
    entry = dict(entry)
    if enabled:
        entry["autonomous_enabled"] = True
        entry["autonomy_mode"] = "autonomous_enabled"
        entry["telemetry_autonomous_allowed"] = True
        entry["autonomous_enabled_by"] = requested_by or "engineer"
        entry["autonomous_enabled_at"] = now
        entry["autonomous_disabled_by"] = ""
        entry["autonomous_disabled_at"] = ""
        entry["autonomous_disable_reason"] = ""
    else:
        entry["autonomous_enabled"] = False
        entry["autonomy_mode"] = "human_in_the_loop"
        entry["telemetry_autonomous_allowed"] = False
        entry["autonomous_disabled_by"] = requested_by or "engineer"
        entry["autonomous_disabled_at"] = now
        entry["autonomous_disable_reason"] = notes or "disabled_by_engineer"

    entry["last_autonomous_readiness"] = confidence_summary or {}
    entry["autonomous_notes"] = notes or ""
    entry["autonomous_updated_at"] = now
    registry[key] = entry
    _save_registry(registry)

    # Mirror state on the source candidate where possible so registry rebuilds
    # preserve the autonomous flag.
    source_candidate_id = str(entry.get("source_candidate_id") or "").strip()
    if source_candidate_id:
        items = _load_candidates()
        changed = False
        for item in items:
            if str(item.get("candidate_id") or "") == source_candidate_id:
                item["autonomous_enabled"] = bool(enabled)
                item["autonomy_mode"] = entry["autonomy_mode"]
                item["telemetry_autonomous_allowed"] = bool(enabled)
                item["last_autonomous_readiness"] = confidence_summary or {}
                item["autonomous_updated_at"] = now
                item["autonomous_updated_by"] = requested_by or "engineer"
                changed = True
                break
        if changed:
            _save_candidates(items)

    return {"status": "ok", "registry_key": key, "registry_entry": entry}


def is_autonomous_registry_mapping_allowed(classification: str, action: str = "", playbook: str = "") -> Dict[str, Any]:
    registry = _load_registry()
    match = _find_registry_entry(registry, classification=classification, action=action, playbook=playbook)
    if not match:
        return {"status": "not_found", "allowed": False, "reason": "approved registry mapping not found"}
    key, entry = match
    allowed = bool(entry.get("enabled", True)) and bool(entry.get("autonomous_enabled")) and bool(entry.get("telemetry_autonomous_allowed"))
    return {
        "status": "ok",
        "registry_key": key,
        "allowed": allowed,
        "reason": "autonomous_enabled" if allowed else "mapping is human-in-the-loop only",
        "registry_entry": entry,
    }

def _safe_playbook_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        return ""
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isalnum() or ch in {"_", "-", "."})
    if not name.endswith((".yml", ".yaml")):
        name = f"{name}.yml"
    return name


def _infer_default_action(classification: str, draft_steps: Optional[List[Any]] = None) -> str:
    forced = _forced_mapping(classification)
    if forced:
        return forced["action"]

    key = _normalize_key(classification)
    text = " ".join([key] + [str(x or "") for x in (draft_steps or [])]).lower()

    # Primary-domain checks come before dependency words. A container ticket may
    # mention DNS/VPN only as healthy dependencies; it must still map to container.
    if any(term in text for term in ["container", "ecs", "kubernetes", "target group", "port mapping"]):
        return "validate_container_connectivity"
    if any(term in text for term in ["disk", "temporary", "tmp", "log rotation", "space"]):
        return "cleanup_linux_temp_files"
    if any(term in text for term in ["vpn", "tunnel", "remote access"]):
        return "recover_vpn_connectivity"
    if any(term in text for term in ["dns", "resolver", "lookup", "coredns"]):
        return "clear_cache"
    return f"validate_{key or 'incident'}"


def _infer_default_playbook(classification: str, action: str, draft_steps: Optional[List[Any]] = None) -> str:
    forced = _forced_mapping(classification)
    if forced:
        return forced["playbook"]

    action_key = _normalize_key(action)
    class_key = _normalize_key(classification)
    text = " ".join([class_key, action_key] + [str(x or "") for x in (draft_steps or [])]).lower()
    if action_key == "validate_container_connectivity" or any(term in text for term in ["container", "ecs", "kubernetes"]):
        return "container_connectivity_check.yml"
    if action_key == "cleanup_linux_temp_files" or any(term in text for term in ["disk", "temporary", "tmp", "log rotation", "space"]):
        return "linux_disk_cleanup.yml"
    if action_key == "recover_vpn_connectivity" or "vpn" in text:
        return "vpn_recover_client_connectivity.yml"
    safe = action_key or f"validate_{class_key or 'incident'}"
    return f"{safe}.yml"


def _default_target_host(classification: str, action: str, target_host: str = "") -> str:
    forced = _forced_mapping(classification)
    if forced:
        return forced["target_host"]
    if str(target_host or "").strip():
        return str(target_host or "").strip()
    text = f"{classification} {action}".lower()
    if any(term in text for term in ["container", "ecs", "kubernetes"]):
        return "linux-lab-01"
    if any(term in text for term in ["disk", "linux", "tmp", "temporary"]):
        return "linux-lab-01"
    if "vpn" in text:
        return "vpn-lab-01"
    if any(term in text for term in ["dns", "network", "gateway"]):
        return "network-lab-01"
    return "linux-lab-01"


def _build_disk_cleanup_playbook(target_host: str) -> str:
    return f"""---
- name: Validate and clean approved temporary files for Linux disk pressure
  hosts: {target_host}
  gather_facts: false
  become: false

  vars:
    cleanup_path: /tmp
    min_file_age: 2d

  tasks:
    - name: Show current root filesystem usage
      ansible.builtin.shell: |
        df -h /
      register: disk_before
      changed_when: false

    - name: Display current disk usage
      ansible.builtin.debug:
        var: disk_before.stdout_lines

    - name: Show largest entries under cleanup path
      ansible.builtin.shell: |
        du -ah {{{{ cleanup_path }}}} 2>/dev/null | sort -rh | head -20
      register: largest_temp_entries
      changed_when: false
      failed_when: false

    - name: Display largest temp entries
      ansible.builtin.debug:
        var: largest_temp_entries.stdout_lines

    - name: Find old temporary files eligible for cleanup
      ansible.builtin.find:
        paths: "{{{{ cleanup_path }}}}"
        age: "{{{{ min_file_age }}}}"
        file_type: file
      register: old_temp_files

    - name: Display cleanup candidate count
      ansible.builtin.debug:
        msg: "Found {{{{ old_temp_files.matched }}}} files older than {{{{ min_file_age }}}} under {{{{ cleanup_path }}}}."

    - name: Remove old temporary files only
      ansible.builtin.file:
        path: "{{{{ item.path }}}}"
        state: absent
      loop: "{{{{ old_temp_files.files }}}}"
      when: old_temp_files.matched | int > 0

    - name: Show root filesystem usage after cleanup
      ansible.builtin.shell: |
        df -h /
      register: disk_after
      changed_when: false

    - name: Display final disk usage
      ansible.builtin.debug:
        var: disk_after.stdout_lines
"""


def _build_generic_diagnostic_playbook(target_host: str, classification: str, draft_steps: Optional[List[Any]] = None) -> str:
    steps = _clean_list(draft_steps)
    rendered_steps = "\\n".join([f"        echo '- {s}'" for s in steps[:12]]) or "        echo '- Review incident evidence and validate affected service health.'"
    return f"""---
- name: Governed diagnostic playbook for {classification}
  hosts: {target_host}
  gather_facts: false
  become: false

  tasks:
    - name: Show target host
      ansible.builtin.debug:
        msg: "Running governed diagnostic automation for {classification} on {{{{ inventory_hostname }}}}."

    - name: Validate host reachability
      ansible.builtin.ping:

    - name: Print reviewed candidate steps
      ansible.builtin.shell: |
{rendered_steps}
      register: candidate_steps
      changed_when: false
      failed_when: false

    - name: Display reviewed candidate steps
      ansible.builtin.debug:
        var: candidate_steps.stdout_lines
"""


def generate_candidate_playbook_draft(candidate_id: str, generated_by: str = "engineer", action: Optional[str] = None, playbook: Optional[str] = None, target_host: Optional[str] = None) -> Dict[str, Any]:
    items = _load_candidates()
    found = None
    for item in items:
        if str(item.get("candidate_id")) == str(candidate_id or "").strip():
            found = item
            break
    if found is None:
        return {"status": "not_found", "candidate_id": candidate_id}
    classification = _normalize_key(found.get("classification", ""))
    draft_steps = found.get("draft_steps", []) or []
    forced = _forced_mapping(classification)
    if forced:
        resolved_action = forced["action"]
        resolved_playbook = forced["playbook"]
        resolved_target = forced["target_host"]
    else:
        resolved_action = _normalize_key(action or found.get("suggested_action") or _infer_default_action(classification, draft_steps))
        resolved_playbook = _safe_playbook_name(playbook or found.get("suggested_playbook") or _infer_default_playbook(classification, resolved_action, draft_steps))
        resolved_target = _default_target_host(classification, resolved_action, target_host or found.get("target_host") or "")
    if resolved_action == "cleanup_linux_temp_files" or "disk" in classification or "temporary" in classification:
        content = _build_disk_cleanup_playbook(resolved_target)
    else:
        content = _build_generic_diagnostic_playbook(resolved_target, classification, draft_steps)
    found["suggested_action"] = resolved_action
    found["suggested_playbook"] = resolved_playbook
    found["target_host"] = resolved_target
    found["status"] = "playbook_draft_generated"
    found["generated_playbook"] = {"generated": True, "generated_by": generated_by or "engineer", "generated_at": _utc_now_iso(), "playbook": resolved_playbook, "target_host": resolved_target, "action": resolved_action, "content": content, "saved": False, "saved_path": ""}
    found["updated_by"] = generated_by or "engineer"
    found["updated_at"] = _utc_now_iso()
    _save_candidates(items)
    return {"status": "ok", "candidate": found, "playbook_draft": found["generated_playbook"]}


def save_reviewed_candidate_playbook(candidate_id: str, playbook_content: Optional[str] = None, reviewed_by: str = "engineer", review_notes: str = "", overwrite: bool = True) -> Dict[str, Any]:
    items = _load_candidates()
    found = None
    for item in items:
        if str(item.get("candidate_id")) == str(candidate_id or "").strip():
            found = item
            break
    if found is None:
        return {"status": "not_found", "candidate_id": candidate_id}
    generated = found.get("generated_playbook") if isinstance(found.get("generated_playbook"), dict) else {}
    content = str(playbook_content if playbook_content is not None else generated.get("content", "")).strip()
    if not content:
        return {"status": "invalid", "detail": "playbook_content is required. Generate a draft first or provide reviewed YAML."}
    playbook_name = _safe_playbook_name(found.get("suggested_playbook") or generated.get("playbook") or "")
    if not playbook_name:
        return {"status": "invalid", "detail": "suggested_playbook is required before saving"}
    classification = _normalize_key(found.get("classification", ""))
    if _is_mismatched_protected_playbook(classification, playbook_name):
        # Remap to a safe classification-derived name rather than blocking; the
        # mismatch happens when AI suggests a VPN/disk playbook for a different
        # classification (e.g. certificate_expiry_warning → vpn playbook name).
        playbook_name = f"{classification}_candidate.yml"
        found["suggested_playbook"] = playbook_name
    if "hosts:" not in content or "tasks:" not in content:
        return {"status": "invalid", "detail": "reviewed playbook must include hosts and tasks sections"}
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    path = PLAYBOOK_DIR / playbook_name
    if path.exists() and not overwrite:
        return {"status": "exists", "detail": "playbook already exists", "path": str(path)}
    if path.exists() and playbook_name in PROTECTED_PLAYBOOK_CLASSIFICATIONS:
        # Do not overwrite previously validated static/protected playbooks from
        # the candidate UI. Mark the review as saved against the existing file.
        existing_content = path.read_text(encoding="utf-8")
        content_to_record = existing_content.strip() or content
        found["status"] = "playbook_created"
        found["generated_playbook"] = {**generated, "content": content_to_record, "saved": True, "saved_path": str(path), "saved_at": _utc_now_iso(), "saved_by": reviewed_by or "engineer", "existing_playbook_preserved": True}
        found["playbook_review"] = {"reviewed": True, "reviewed_by": reviewed_by or "engineer", "reviewed_at": _utc_now_iso(), "review_notes": review_notes or "Reviewed and saved from Automation Development stage. Existing protected playbook preserved."}
        found["updated_by"] = reviewed_by or "engineer"
        found["updated_at"] = _utc_now_iso()
        _save_candidates(items)
        return {"status": "ok", "candidate": found, "saved_path": str(path), "playbook": playbook_name, "existing_playbook_preserved": True}
    path.write_text(content + "\n", encoding="utf-8")
    found["status"] = "playbook_created"
    found["generated_playbook"] = {**generated, "content": content, "saved": True, "saved_path": str(path), "saved_at": _utc_now_iso(), "saved_by": reviewed_by or "engineer"}
    found["playbook_review"] = {"reviewed": True, "reviewed_by": reviewed_by or "engineer", "reviewed_at": _utc_now_iso(), "review_notes": review_notes or "Reviewed and saved from Automation Development stage."}
    found["updated_by"] = reviewed_by or "engineer"
    found["updated_at"] = _utc_now_iso()
    _save_candidates(items)
    return {"status": "ok", "candidate": found, "saved_path": str(path), "playbook": playbook_name}

def get_automation_registry() -> Dict[str, Any]:
    registry = _load_registry()
    if registry:
        return registry

    # Self-heal only when registry is empty but enabled candidates exist.
    # This preserves UI/API continuity after an accidental empty registry write.
    rebuilt = rebuild_automation_registry_from_enabled_candidates(rebuilt_by="system_autoheal")
    return rebuilt if rebuilt else registry
