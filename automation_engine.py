from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("automation_engine")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# =========================================================
# Paths / config
# =========================================================

DATA_DIR = Path(os.getenv("AIOPS_DATA_DIR", "data"))
AUTOMATION_LOG_FILE = DATA_DIR / "automation_executions.json"
AUTOMATION_QUEUE_FILE = DATA_DIR / "automation_queue.json"
AUTOMATION_QUEUE_ARCHIVE_FILE = DATA_DIR / "automation_queue_archive.json"
LEARNED_MAPPINGS_FILE = DATA_DIR / "automation_learned_mappings.json"
AUTOMATION_REGISTRY_FILE = DATA_DIR / "automation_registry.json"

# Terminal statuses eligible for archiving
_TERMINAL_STATUSES = {"executed", "closed_remediated", "execution_failed", "cancelled", "skipped"}
# Keep this many recent terminal entries in the active queue for context; archive the rest
_QUEUE_ACTIVE_TERMINAL_KEEP = 10

DEFAULT_EXECUTION_MODE = os.getenv("AUTOMATION_EXECUTION_MODE", "simulation").strip().lower()
DEFAULT_TENANT_ID = os.getenv("AIOPS_DEFAULT_TENANT_ID", "client_a")


# =========================================================
# Static controls / mappings
# =========================================================

SAFE_ACTIONS: Dict[str, Dict[str, Any]] = {
    "validate_container_connectivity": {"risk_tier": "medium", "approval_required": True},
    "restart_service": {"risk_tier": "medium", "approval_required": True},
    "restart_pod": {"risk_tier": "medium", "approval_required": True},
    "scale_deployment": {"risk_tier": "medium", "approval_required": True},
    "clear_cache": {"risk_tier": "low", "approval_required": True},
    "drain_lb_node": {"risk_tier": "high", "approval_required": True},
    "rollback_release": {"risk_tier": "high", "approval_required": True},
    "renew_certificate": {"risk_tier": "medium", "approval_required": True},
    "restart_iis": {"risk_tier": "medium", "approval_required": True},
    "playbook_script_execution": {"risk_tier": "medium", "approval_required": True},
}

CLASSIFICATION_TO_ACTIONS: Dict[str, List[str]] = {
    "cpu_saturation": ["restart_service", "scale_deployment"],
    "memory_pressure": ["restart_service", "scale_deployment"],
    "disk_full": ["clear_cache"],
    "pod_crash_loop": ["restart_pod", "rollback_release"],
    "api_gateway_timeout": ["restart_service"],
    # Container connectivity must use validation/remediation guardrail.
    # Do not infer DNS cache flush just because the runbook mentions DNS as a dependency check.
    "container_connection_issue": ["validate_container_connectivity"],
    "container_service_registration_issue": ["validate_container_connectivity"],
    "ecs_service_networking_issue": ["validate_container_connectivity"],
    "alb_target_group_health_check_failure": ["validate_container_connectivity"],
    "container_port_binding_mismatch": ["validate_container_connectivity"],
    "kubernetes_service_connectivity_issue": ["validate_container_connectivity"],
    "load_balancer_target_registration_issue": ["validate_container_connectivity"],
    "certificate_expiry_warning": ["renew_certificate"],
    "service_crash_loop": ["restart_service", "rollback_release"],
    "cache_staleness_issue": ["clear_cache"],
    "dns_resolution_delay": ["clear_cache"],
    "vpn_client_failure": ["recover_vpn_connectivity"],
    "ipv6_enabled_interface_connectivity_issue": ["restart_service"],
    "jwt_signing_key_rotation_issue": ["restart_service"],
    "jwt_signing_key_rotation_failure": ["restart_service"],
    "vpn_password_reset_authentication_failure": ["clear_cache"],
    "wireless_network_connectivity_issue": ["restart_service"],
    "load_balancer_node_configuration_mismatch": ["drain_lb_node"],
    "dns_issue": ["clear_cache"],
    "dns_resolution_failure": ["clear_cache"],
    "app_server_thread_exhaustion": ["restart_service"],
    "iis_service_unavailable": ["restart_service"],
}

ACTION_TO_CONNECTOR: Dict[str, str] = {
    "validate_container_connectivity": "ansible",
    "restart_service": "ansible",
    "restart_pod": "kubernetes",
    "scale_deployment": "kubernetes",
    "clear_cache": "ansible",
    "drain_lb_node": "ansible",
    "rollback_release": "ansible",
    "renew_certificate": "ansible",
    "restart_iis": "ansible",
    "playbook_script_execution": "ansible",
}

ACTION_TO_PLAYBOOK: Dict[str, str] = {
    "validate_container_connectivity": "container_connectivity_check.yml",
    "restart_service": "linux_restart_nginx.yml",
    "recover_vpn_connectivity": "vpn_recover_client_connectivity.yml",
    "clear_cache": "dns_flush.yml",
    "drain_lb_node": "drain_lb_node.yml",
    "rollback_release": "rollback_release.yml",
    "restart_iis": "restart_iis.yml",
    "renew_certificate": "renew_certificate.yml",
}


CONTAINER_CONNECTIVITY_ACTION = "validate_container_connectivity"
CONTAINER_CONNECTIVITY_PLAYBOOK = "container_connectivity_check.yml"
CONTAINER_CONNECTIVITY_TARGET = os.getenv("AIOPS_CONTAINER_TARGET_HOST", "container-lab-01")

CONTAINER_CONNECTIVITY_CLASSIFICATIONS = {
    "container_connection_issue",
    "container_connectivity_issue",
    "container_service_registration_issue",
    "ecs_service_networking_issue",
    "alb_target_group_health_check_failure",
    "container_port_binding_mismatch",
    "kubernetes_service_connectivity_issue",
    "load_balancer_target_registration_issue",
}

CONTAINER_BLOCKED_ACTIONS = {"clear_cache"}
CONTAINER_BLOCKED_PLAYBOOKS = {"dns_flush.yml"}


# =========================================================
# Generic helpers
# =========================================================

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed reading JSON %s: %s", path, exc)
        return default


def _safe_write_json(path: Path, payload: Any) -> None:
    import tempfile
    import os as _os
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file first then atomically replace to prevent partial reads
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2))
        _os.replace(tmp_path, path)
    except Exception:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _archive_old_queue_entries(queue: list) -> list:
    """Move excess terminal-status queue entries to the archive file.

    Keeps all active (non-terminal) entries plus the _QUEUE_ACTIVE_TERMINAL_KEEP
    most-recent terminal entries in the live queue.  Everything older is appended
    to automation_queue_archive.json so history is preserved but the active queue
    stays lean.
    """
    active = [e for e in queue if e.get("status") not in _TERMINAL_STATUSES]
    terminal = [e for e in queue if e.get("status") in _TERMINAL_STATUSES]

    # Sort terminal entries by created_at so oldest go to archive first
    terminal.sort(key=lambda e: str(e.get("created_at", "")))

    to_archive = terminal[:-_QUEUE_ACTIVE_TERMINAL_KEEP] if len(terminal) > _QUEUE_ACTIVE_TERMINAL_KEEP else []
    to_keep = terminal[-_QUEUE_ACTIVE_TERMINAL_KEEP:] if terminal else []

    if to_archive:
        existing_archive = _safe_read_json(AUTOMATION_QUEUE_ARCHIVE_FILE, default=[])
        if not isinstance(existing_archive, list):
            existing_archive = []
        _safe_write_json(AUTOMATION_QUEUE_ARCHIVE_FILE, existing_archive + to_archive)

    return active + to_keep


def _load_automation_registry() -> Dict[str, Any]:
    data = _safe_read_json(AUTOMATION_REGISTRY_FILE, default={})
    return data if isinstance(data, dict) else {}



def _normalize_key(text: str) -> str:
    text = str(text or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")
    text = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in text)
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def _tokenize_key(text: str) -> set[str]:
    norm = _normalize_key(text).replace("_", " ")
    stop = {"issue", "failure", "error", "problem", "warning"}
    return {t for t in norm.split() if t and t not in stop}


def _jaccard_similarity(a: str, b: str) -> float:
    ta = _tokenize_key(a)
    tb = _tokenize_key(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _is_container_connectivity_classification(classification: str) -> bool:
    key = _normalize_key(classification)
    if key in CONTAINER_CONNECTIVITY_CLASSIFICATIONS:
        return True
    tokens = _tokenize_key(key)
    return bool(
        ("container" in tokens and ({"connection", "connectivity", "networking", "registration", "port"} & tokens))
        or ("ecs" in tokens and {"service", "networking"} <= tokens)
        or ("alb" in tokens and {"target", "group"} <= tokens)
    )


def _container_context_present(
    classification: str,
    linked_resolution: Optional[Dict[str, Any]],
    linked_playbook: Optional[Dict[str, Any]],
) -> bool:
    texts = [classification]
    if isinstance(linked_resolution, dict):
        texts.extend(linked_resolution.get("validated_actions", []) or [])
        texts.extend(linked_resolution.get("validated_checks", []) or [])
        texts.append(linked_resolution.get("root_cause", "") or "")
    if isinstance(linked_playbook, dict):
        texts.extend(linked_playbook.get("steps", []) or [])
        texts.extend(linked_playbook.get("scripts", []) or [])
        texts.append(linked_playbook.get("description", "") or "")

    blob = " ".join(str(x or "").lower() for x in texts)
    container_terms = [
        "container",
        "ecs",
        "kubernetes",
        "pod",
        "alb",
        "application load balancer",
        "target group",
        "load balancer",
        "port mapping",
        "port binding",
        "service selector",
        "endpoint registration",
        "task networking",
    ]
    connectivity_terms = [
        "cannot connect",
        "connectivity",
        "connection",
        "timeout",
        "timing out",
        "traffic",
        "target registration",
        "health check",
        "unhealthy",
    ]
    return any(t in blob for t in container_terms) and any(t in blob for t in connectivity_terms)


def _is_dns_primary_classification(classification: str) -> bool:
    key = _normalize_key(classification)
    return key in {"dns_issue", "dns_resolution_failure", "dns_resolution_delay", "dns_in_cluster_failure"}


def _force_container_connectivity_steps(plan: Dict[str, Any], execution_mode: str = "simulation") -> Dict[str, Any]:
    existing = dict(plan or {})
    mode = existing.get("execution_mode") or execution_mode or DEFAULT_EXECUTION_MODE or "simulation"
    step = {
        "step_id": "container_connectivity_validation_1",
        "action": CONTAINER_CONNECTIVITY_ACTION,
        "playbook": CONTAINER_CONNECTIVITY_PLAYBOOK,
        "connector": ACTION_TO_CONNECTOR.get(CONTAINER_CONNECTIVITY_ACTION, "ansible"),
        "target_host": CONTAINER_CONNECTIVITY_TARGET,
        "risk_tier": "medium",
        "approval_required": True,
        "rollback_available": bool(existing.get("rollback_available", False)),
        "rollback_strategy": existing.get("rollback_strategy"),
        "execution_mode": mode,
        "description": (
            "Validate container service reachability across security group/firewall, "
            "load balancer target registration, container port mapping, and ECS/Kubernetes service networking."
        ),
        "status": "planned",
    }
    existing.update({
        "classification": "container_connection_issue",
        "automation_ready": bool(existing.get("automation_ready", False)),
        "approval_mode": existing.get("approval_mode") or "manual_approval_required",
        "rollback_available": bool(existing.get("rollback_available", False)),
        "risk_tier": existing.get("risk_tier") or "medium",
        "execution_mode": mode,
        "mapping_source": "static_container_guardrail",
        "step_count": 1,
        "steps": [step],
    })
    return existing


def _safe_list_of_strings(values: Optional[List[Any]]) -> List[str]:
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


def _clean_actions(actions: List[str]) -> List[str]:
    cleaned: List[str] = []
    for action in actions or []:
        key = _normalize_key(action)
        if key and key in SAFE_ACTIONS and key not in cleaned:
            cleaned.append(key)
    return cleaned


def _find_queue_entry(queue: List[Dict[str, Any]], queue_id: str) -> Optional[Dict[str, Any]]:
    for item in queue:
        if isinstance(item, dict) and item.get("queue_id") == queue_id:
            return item
    return None


# =========================================================
# Learned mappings
# =========================================================

def _load_learned_mappings() -> Dict[str, Any]:
    data = _safe_read_json(LEARNED_MAPPINGS_FILE, default={})
    return data if isinstance(data, dict) else {}


def _save_learned_mappings(payload: Dict[str, Any]) -> None:
    _safe_write_json(LEARNED_MAPPINGS_FILE, payload)


def get_learned_action_mappings() -> Dict[str, Any]:
    return _load_learned_mappings()


def get_all_action_mappings() -> Dict[str, Any]:
    learned = _load_learned_mappings()
    merged: Dict[str, Any] = {}

    for key, actions in CLASSIFICATION_TO_ACTIONS.items():
        merged[_normalize_key(key)] = {
            "actions": list(actions),
            "source": "static",
        }

    for key, meta in learned.items():
        norm_key = _normalize_key(key)
        if isinstance(meta, dict):
            merged[norm_key] = dict(meta)
        else:
            merged[norm_key] = {"actions": list(meta), "source": "learned"}

    return merged


def _infer_actions_from_text(
    classification: str,
    linked_resolution: Optional[Dict[str, Any]],
    linked_playbook: Optional[Dict[str, Any]],
) -> List[str]:
    texts = [classification]

    if isinstance(linked_resolution, dict):
        texts.extend(linked_resolution.get("validated_actions", []) or [])
        texts.extend(linked_resolution.get("validated_checks", []) or [])
        texts.append(linked_resolution.get("root_cause", "") or "")

    if isinstance(linked_playbook, dict):
        texts.extend(linked_playbook.get("steps", []) or [])
        texts.extend(linked_playbook.get("scripts", []) or [])
        texts.append(linked_playbook.get("description", "") or "")

    blob = " ".join(str(x or "").lower() for x in texts)
    suggestions: List[str] = []

    # Container connectivity runbooks often include DNS as one dependency check.
    # That must not infer dns_flush.yml unless the classification is actually DNS-primary.
    if _is_container_connectivity_classification(classification) or _container_context_present(classification, linked_resolution, linked_playbook):
        return [CONTAINER_CONNECTIVITY_ACTION]

    if any(k in blob for k in ["cache", "dns", "resolver", "lookup", "flush"]):
        suggestions.append("clear_cache")
    if "restart" in blob and "pod" in blob:
        suggestions.append("restart_pod")
    if "scale" in blob or "replica" in blob or "deployment" in blob:
        # suggestions.append("scale_deployment")  # disabled for Docker lab live validation
        pass
    if "rollback" in blob:
        suggestions.append("rollback_release")
    if any(k in blob for k in ["certificate", "cert", "tls", "ssl", "renew"]):
        suggestions.append("renew_certificate")
    if any(k in blob for k in ["load balancer", "lb node", "drain"]):
        suggestions.append("drain_lb_node")
    if "restart" in blob or "service" in blob:
        suggestions.append("restart_service")

    return _clean_actions(suggestions)


def suggest_action_mapping(
    classification: str,
    linked_resolution: Optional[Dict[str, Any]] = None,
    linked_playbook: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    classification = _normalize_key(classification)
    learned = _load_learned_mappings()

    # Hard guardrail: container connectivity must not inherit DNS/VPN learned mappings.
    if _is_container_connectivity_classification(classification) or _container_context_present(classification, linked_resolution, linked_playbook):
        return {
            "classification": "container_connection_issue",
            "actions": [CONTAINER_CONNECTIVITY_ACTION],
            "source": "static_container_guardrail",
            "confidence": 1.0,
            "matched_from": classification,
        }

    if classification in learned:
        row = learned[classification]
        return {
            "classification": classification,
            "actions": _clean_actions(list((row or {}).get("actions", []))),
            "source": "learned",
            "confidence": 1.0,
            "matched_from": classification,
        }

    if classification in CLASSIFICATION_TO_ACTIONS:
        return {
            "classification": classification,
            "actions": _clean_actions(list(CLASSIFICATION_TO_ACTIONS[classification])),
            "source": "static",
            "confidence": 1.0,
            "matched_from": classification,
        }

    all_map = get_all_action_mappings()
    best_key = None
    best_score = 0.0
    for key in all_map:
        score = _jaccard_similarity(classification, key)
        if score > best_score:
            best_score = score
            best_key = key

    inferred = _infer_actions_from_text(classification, linked_resolution, linked_playbook)

    if best_key and best_score >= 0.34:
        inherited = _clean_actions(list((all_map.get(best_key) or {}).get("actions", [])))
        actions = inferred or inherited
        return {
            "classification": classification,
            "actions": actions,
            "source": "similarity",
            "confidence": round(best_score, 3),
            "matched_from": best_key,
        }

    return {
        "classification": classification,
        "actions": inferred,
        "source": "inferred" if inferred else "none",
        "confidence": 0.25 if inferred else 0.0,
        "matched_from": None,
    }


def learn_action_mapping(
    classification: str,
    actions: Optional[List[str]] = None,
    approved_actions: Optional[List[str]] = None,
    learned_by: str = "engineer",
    approved_by: Optional[str] = None,
    source: str = "manual_approval",
    source_ticket: Optional[str] = None,
) -> Dict[str, Any]:
    classification = _normalize_key(classification)
    chosen_actions = _clean_actions((approved_actions or []) + (actions or []))

    if not chosen_actions:
        return {
            "status": "invalid",
            "detail": "No valid actions supplied",
            "classification": classification,
        }

    learned = _load_learned_mappings()
    learned[classification] = {
        "actions": chosen_actions,
        "learned_by": learned_by,
        "approved_by": approved_by or learned_by,
        "source": source,
        "source_ticket": source_ticket or "",
        "updated_at": _utc_now_iso(),
    }
    _save_learned_mappings(learned)

    return {
        "status": "ok",
        "classification": classification,
        "actions": chosen_actions,
        "mapping": learned[classification],
    }


# =========================================================
# Connector integration
# =========================================================

def _import_connectors():
    try:
        from automation_connectors import (
            run_ansible_playbook,
            run_kubernetes_action,
            run_ssh_command,
            run_powershell_command,
        )
        return {
            "run_ansible_playbook": run_ansible_playbook,
            "run_kubernetes_action": run_kubernetes_action,
            "run_ssh_command": run_ssh_command,
            "run_powershell_command": run_powershell_command,
        }
    except Exception as exc:
        LOGGER.warning("automation_connectors unavailable, live execution will fall back: %s", exc)
        return {}


# =========================================================
# Plan generation
# =========================================================

def build_automation_plan(
    classification: str,
    linked_resolution: Optional[Dict[str, Any]] = None,
    linked_playbook: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    classification = _normalize_key(classification)

    container_guardrail = (
        _is_container_connectivity_classification(classification)
        or _container_context_present(classification, linked_resolution, linked_playbook)
    )

    actions = _clean_actions(CLASSIFICATION_TO_ACTIONS.get(classification, []))
    if container_guardrail and not _is_dns_primary_classification(classification):
        classification = "container_connection_issue"
        actions = [CONTAINER_CONNECTIVITY_ACTION]
    learned_match = None
    if not actions:
        learned_match = suggest_action_mapping(classification, linked_resolution, linked_playbook)
        actions = _clean_actions(list(learned_match.get("actions", [])))

    playbook_scripts: List[str] = []
    if isinstance(linked_playbook, dict):
        playbook_scripts = _safe_list_of_strings(linked_playbook.get("scripts", []) or [])

    if container_guardrail and not _is_dns_primary_classification(classification):
        actions = [
            action for action in actions
            if action not in CONTAINER_BLOCKED_ACTIONS
            and ACTION_TO_PLAYBOOK.get(action) not in CONTAINER_BLOCKED_PLAYBOOKS
        ] or [CONTAINER_CONNECTIVITY_ACTION]

    if not actions and playbook_scripts:
        actions = ["playbook_script_execution"]

    approval_mode = "manual_approval_required"
    rollback_available = False
    risk_tier = "medium"
    rollback_strategy = None
    automation_ready = False

    if isinstance(linked_playbook, dict):
        approval_mode = str(linked_playbook.get("approval_mode") or approval_mode)
        rollback_available = bool(linked_playbook.get("rollback_available", False))
        risk_tier = str(linked_playbook.get("risk_tier") or risk_tier)
        rollback_strategy = linked_playbook.get("rollback_strategy")

    if isinstance(linked_resolution, dict):
        automation_ready = bool(linked_resolution.get("automation_ready", False))

    steps: List[Dict[str, Any]] = []

    for action in actions:
        action_meta = SAFE_ACTIONS.get(action, {"risk_tier": risk_tier, "approval_required": True})
        steps.append(
            {
                "action": action,
                "connector": ACTION_TO_CONNECTOR.get(action, "simulation"),
                "playbook": ACTION_TO_PLAYBOOK.get(action),
                "risk_tier": action_meta.get("risk_tier", risk_tier),
                "approval_required": bool(action_meta.get("approval_required", True)),
                "rollback_available": rollback_available,
                "rollback_strategy": rollback_strategy,
                "status": "planned",
            }
        )

    for script in playbook_scripts:
        steps.append(
            {
                "action": "playbook_script_execution",
                "connector": ACTION_TO_CONNECTOR.get("playbook_script_execution", "ansible"),
                "script": script,
                "risk_tier": risk_tier,
                "approval_required": True,
                "rollback_available": rollback_available,
                "rollback_strategy": rollback_strategy,
                "status": "planned",
            }
        )

    mapping_source = "static" if classification in CLASSIFICATION_TO_ACTIONS else "unmapped"
    if classification in _load_learned_mappings():
        mapping_source = "learned"
    elif learned_match and actions:
        mapping_source = str(learned_match.get("source") or "inferred_or_similarity")

    return {
        "classification": classification,
        "automation_ready": automation_ready,
        "approval_mode": approval_mode,
        "rollback_available": rollback_available,
        "rollback_strategy": rollback_strategy,
        "risk_tier": risk_tier,
        "execution_mode": DEFAULT_EXECUTION_MODE,
        "mapping_source": mapping_source,
        "step_count": len(steps),
        "steps": steps,
    }


# =========================================================
# Queue lifecycle
# =========================================================

def queue_automation_plan(
    incident_id: str,
    classification: str,
    linked_resolution: Optional[Dict[str, Any]] = None,
    linked_playbook: Optional[Dict[str, Any]] = None,
    requested_by: str = "system",
) -> Dict[str, Any]:
    queue = _safe_read_json(AUTOMATION_QUEUE_FILE, default=[])
    if not isinstance(queue, list):
        queue = []

    queue = _archive_old_queue_entries(queue)

    plan = build_automation_plan(classification, linked_resolution, linked_playbook)

    entry = {
        "queue_id": f"aq_{uuid.uuid4().hex[:10]}",
        "incident_id": str(incident_id or "").strip(),
        "classification": _normalize_key(classification),
        "tenant_id": DEFAULT_TENANT_ID,
        "requested_by": str(requested_by or "system").strip(),
        "created_at": _utc_now_iso(),
        "status": "queued",
        "linked_resolution": linked_resolution,
        "linked_playbook": linked_playbook,
        "plan": plan,
    }
    queue.append(entry)
    _safe_write_json(AUTOMATION_QUEUE_FILE, queue)

    return {"status": "ok", "queue_entry": entry}


def refresh_automation_queue(queue_id: str) -> Dict[str, Any]:
    queue = _safe_read_json(AUTOMATION_QUEUE_FILE, default=[])
    if not isinstance(queue, list):
        queue = []

    entry = _find_queue_entry(queue, queue_id)
    if not entry:
        return {"status": "not_found", "queue_id": queue_id}

    if entry.get("status") in {"closed_remediated", "execution_failed"}:
        return {
            "status": "not_refreshable",
            "queue_id": queue_id,
            "current_status": entry.get("status"),
            "queue_entry": entry,
        }

    plan = build_automation_plan(
        classification=entry.get("classification", ""),
        linked_resolution=entry.get("linked_resolution"),
        linked_playbook=entry.get("linked_playbook"),
    )

    entry["plan"] = plan
    entry["refreshed_at"] = _utc_now_iso()

    if entry.get("status") not in {"approved"}:
        entry["status"] = "queued"

    _safe_write_json(AUTOMATION_QUEUE_FILE, queue)
    return {"status": "ok", "queue_entry": entry}


def close_queue_if_remediated(
    queue_id: str,
    closed_by: str = "automation_engine",
    reason: str = "remediated",
) -> Dict[str, Any]:
    queue = _safe_read_json(AUTOMATION_QUEUE_FILE, default=[])
    if not isinstance(queue, list):
        queue = []

    entry = _find_queue_entry(queue, queue_id)
    if not entry:
        return {"status": "not_found", "queue_id": queue_id}

    entry["status"] = "closed_remediated"
    entry["closed_at"] = _utc_now_iso()
    entry["closed_by"] = str(closed_by or "automation_engine")
    entry["closure_reason"] = str(reason or "remediated")
    _safe_write_json(AUTOMATION_QUEUE_FILE, queue)

    return {"status": "ok", "queue_entry": entry}


def approve_automation(queue_id: str, approved_by: str = "engineer") -> Dict[str, Any]:
    queue = _safe_read_json(AUTOMATION_QUEUE_FILE, default=[])
    if not isinstance(queue, list):
        queue = []

    found = None
    for item in queue:
        if isinstance(item, dict) and item.get("queue_id") == queue_id:
            item["status"] = "approved"
            item["approved_by"] = str(approved_by or "engineer")
            item["approved_at"] = _utc_now_iso()
            found = item
            break

    _safe_write_json(AUTOMATION_QUEUE_FILE, queue)
    return {"status": "ok" if found else "not_found", "queue_entry": found}


# =========================================================
# Execution
# =========================================================

def _extract_evidence_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    stdout = str(result.get("stdout", "") or "")
    stderr = str(result.get("stderr", "") or "")
    return {
        "returncode": result.get("returncode"),
        "stdout_preview": stdout[:4000],
        "stderr_preview": stderr[:4000],
        "playbook": result.get("playbook"),
        "service": result.get("service"),
        "working_directory": result.get("working_directory"),
        "compose_file": result.get("compose_file"),
        "raw_result": result,
    }


def _simulate_step(step: Dict[str, Any], executed_by: str) -> Dict[str, Any]:
    executed_step = dict(step)
    executed_step["status"] = "simulated_success"
    executed_step["executed_at"] = _utc_now_iso()
    executed_step["executed_by"] = executed_by
    executed_step["evidence"] = {
        "mode": "simulation",
        "summary": f"Simulated execution for action {step.get('action')}",
    }
    return executed_step


def _execute_step_live(step: Dict[str, Any], executed_by: str) -> Dict[str, Any]:
    executed_step = dict(step)
    executed_step["executed_at"] = _utc_now_iso()
    executed_step["executed_by"] = executed_by

    connectors = _import_connectors()
    connector_name = str(step.get("connector") or "simulation").lower()
    action = str(step.get("action") or "").strip().lower()

    if not connectors:
        executed_step["status"] = "simulation_fallback"
        executed_step["reason"] = "automation_connectors_unavailable"
        executed_step["evidence"] = {"mode": "simulation_fallback"}
        return executed_step

    try:
        if connector_name == "ansible":
            playbook_name = step.get("playbook")
            if not playbook_name and action == "playbook_script_execution":
                playbook_name = "adhoc_script.yml"
            runner = connectors.get("run_ansible_playbook")
            if runner is None:
                raise RuntimeError("run_ansible_playbook not available")
            result = runner(playbook_name=playbook_name, step=step)

        elif connector_name == "kubernetes":
            runner = connectors.get("run_kubernetes_action")
            if runner is None:
                raise RuntimeError("run_kubernetes_action not available")
            result = runner(action=action, step=step)

        elif connector_name == "ssh":
            runner = connectors.get("run_ssh_command")
            if runner is None:
                raise RuntimeError("run_ssh_command not available")
            result = runner(step=step)

        elif connector_name == "powershell":
            runner = connectors.get("run_powershell_command")
            if runner is None:
                raise RuntimeError("run_powershell_command not available")
            result = runner(step=step)

        else:
            result = {
                "ok": True,
                "mode": "noop",
                "message": f"No live connector configured for {connector_name}",
            }

        executed_step["connector_result"] = result
        executed_step["evidence"] = _extract_evidence_from_result(result)
        executed_step["status"] = "success" if bool(result.get("ok", False)) else "failed"
        return executed_step

    except Exception as exc:
        executed_step["status"] = "failed"
        executed_step["error"] = str(exc)
        executed_step["evidence"] = {"exception": str(exc)}
        return executed_step


def execute_automation(
    queue_id: str,
    executed_by: str = "automation_engine",
    execution_mode: Optional[str] = None,
) -> Dict[str, Any]:
    queue = _safe_read_json(AUTOMATION_QUEUE_FILE, default=[])
    log = _safe_read_json(AUTOMATION_LOG_FILE, default=[])

    if not isinstance(queue, list):
        queue = []
    if not isinstance(log, list):
        log = []

    target = _find_queue_entry(queue, queue_id)
    if not target:
        return {"status": "not_found", "queue_id": queue_id}

    if target.get("status") not in {"approved", "queued"}:
        return {
            "status": "not_executable",
            "queue_id": queue_id,
            "current_status": target.get("status"),
        }

    mode = str(
        execution_mode or target.get("plan", {}).get("execution_mode") or DEFAULT_EXECUTION_MODE
    ).strip().lower()

    plan = target.get("plan") or {}
    steps = plan.get("steps") or []
    if not steps:
        return {
            "status": "not_executable",
            "queue_id": queue_id,
            "reason": "no_executable_steps",
            "message": "Cannot execute automation because the queue plan has zero executable steps. Enable a registry mapping and recreate or refresh the queue.",
            "plan": plan,
        }

    executed_steps: List[Dict[str, Any]] = []
    failed = False

    for step in steps:
        executed_step = (
            _execute_step_live(step, executed_by=executed_by)
            if mode == "live"
            else _simulate_step(step, executed_by=executed_by)
        )
        if executed_step.get("status") == "failed":
            failed = True
        executed_steps.append(executed_step)

    execution_status = (
        "completed_live"
        if mode == "live" and not failed
        else "completed_with_failures"
        if failed
        else "completed_simulation"
    )

    execution = {
        "execution_id": f"ax_{uuid.uuid4().hex[:10]}",
        "queue_id": queue_id,
        "incident_id": target.get("incident_id"),
        "classification": target.get("classification"),
        "tenant_id": target.get("tenant_id", DEFAULT_TENANT_ID),
        "executed_by": str(executed_by or "automation_engine"),
        "executed_at": _utc_now_iso(),
        "execution_mode": mode,
        "status": execution_status,
        "rollback_available": bool(plan.get("rollback_available", False)),
        "rollback_strategy": plan.get("rollback_strategy"),
        "evidence_summary": {
            "step_count": len(executed_steps),
            "successful_steps": len([s for s in executed_steps if s.get("status") == "success"]),
            "failed_steps": len([s for s in executed_steps if s.get("status") == "failed"]),
        },
        "steps": executed_steps,
    }

    log.append(execution)

    target["status"] = (
        "closed_remediated"
        if (mode == "live" and not failed)
        else ("executed" if not failed else "execution_failed")
    )
    target["executed_at"] = execution["executed_at"]
    target["execution_id"] = execution["execution_id"]
    target["execution_mode"] = mode

    if target["status"] == "closed_remediated":
        target["closed_at"] = execution["executed_at"]
        target["closure_reason"] = "completed_live_remediation"

    # Archive terminal entries so the live queue stays lean (BF-06).
    queue = _archive_old_queue_entries(queue)
    _safe_write_json(AUTOMATION_QUEUE_FILE, queue)
    _safe_write_json(AUTOMATION_LOG_FILE, log)

    return {"status": "ok", "execution": execution}


def list_automation_queue(limit: int = 100, status_filter: Optional[str] = None) -> Dict[str, Any]:
    """Return recent queue entries, newest first, optionally filtered by status."""
    queue = _safe_read_json(AUTOMATION_QUEUE_FILE, default=[])
    if not isinstance(queue, list):
        queue = []
    items = sorted(queue, key=lambda x: x.get("queued_at") or "", reverse=True)
    if status_filter:
        items = [e for e in items if str(e.get("status") or "").lower() == status_filter.lower()]
    return {"items": items[:limit], "total": len(queue)}


if __name__ == "__main__":
    sample = queue_automation_plan("inc_tel_demo", "dns_issue", requested_by="vijay")
    qid = sample["queue_entry"]["queue_id"]
    approve_automation(qid, approved_by="vijay")
    print(json.dumps(execute_automation(qid, execution_mode="simulation"), indent=2))




# VPN lab automation mapping guidance:
# Add the following mappings in your automation action map if not already present:
#   "vpn_client_failure": ["recover_vpn_connectivity"]
#   "vpn_latency_spike": ["recover_vpn_connectivity"]
#   "vpn_network_connectivity_issue": ["recover_vpn_connectivity"]
# And add this action-to-playbook mapping:
#   "recover_vpn_connectivity": "vpn_recover_client_connectivity.yml"

# VPN lab automation mapping guidance:
# Add the following mappings in your automation action map if not already present:
#   "vpn_client_failure": ["recover_vpn_connectivity"]
#   "vpn_latency_spike": ["recover_vpn_connectivity"]
#   "vpn_network_connectivity_issue": ["recover_vpn_connectivity"]
# And add this action-to-playbook mapping:
#   "recover_vpn_connectivity": "vpn_recover_client_connectivity.yml"



# ---- VPN lab executable mapping override ----
# Narrow additive mapping for Phase 5 VPN automation demo.
# Keeps existing automation behavior intact and only forces executable steps
# for vpn_client_failure when the generic static planner produces zero steps.

VPN_LAB_ACTION = "recover_vpn_connectivity"
VPN_LAB_PLAYBOOK = "vpn_recover_client_connectivity.yml"
VPN_LAB_TARGET = "vpn-lab-01"

def _aiops_vpn_lab_step(execution_mode: str = "simulation") -> dict:
    return {
        "step_id": "vpn_recover_client_connectivity_1",
        "action": VPN_LAB_ACTION,
        "playbook": VPN_LAB_PLAYBOOK,
        "connector": "ansible",
        "target_host": VPN_LAB_TARGET,
        "risk_tier": "medium",
        "approval_required": True,
        "execution_mode": execution_mode or "simulation",
        "description": "Recover mock VPN client connectivity on vpn-lab-01.",
    }

_ORIGINAL_BUILD_AUTOMATION_PLAN = build_automation_plan

def build_automation_plan(classification: str, linked_resolution=None, linked_playbook=None) -> dict:
    import re

    normalized = re.sub(r"[^a-z0-9]+", "_", str(classification or "").strip().lower()).strip("_")
    if normalized in {
        "vpn_client_failure",
        "vpn_client_failures",
        "vpn_failure",
        "vpn_connectivity_failure",
        "vpn_connection_failure",
        "vpn_user_connectivity_failure",
    }:
        existing = _ORIGINAL_BUILD_AUTOMATION_PLAN(
            classification="vpn_client_failure",
            linked_resolution=linked_resolution,
            linked_playbook=linked_playbook,
        )
        if not isinstance(existing, dict):
            existing = {}

        steps = existing.get("steps") or []
        if not steps:
            mode = existing.get("execution_mode") or "simulation"
            steps = [_aiops_vpn_lab_step(mode)]

        existing.update({
            "classification": "vpn_client_failure",
            "automation_ready": True,
            "approval_mode": "manual_approval_required",
            "rollback_available": True,
            "rollback_strategy": "Re-run vpn_check_tunnel.yml and rollback to backup gateway if health remains down.",
            "risk_tier": "medium",
            "execution_mode": existing.get("execution_mode") or "simulation",
            "mapping_source": "static",
            "step_count": len(steps),
            "steps": steps,
        })
        return existing

    return _ORIGINAL_BUILD_AUTOMATION_PLAN(
        classification=classification,
        linked_resolution=linked_resolution,
        linked_playbook=linked_playbook,
    )
# ---- end VPN lab executable mapping override ----


# ---- Container connectivity automation mapping override ----
# Narrow additive mapping for container/ECS/Kubernetes/ALB connectivity scenarios.
# Keeps existing automation behavior intact while preventing DNS/VPN inference from
# selecting dns_flush.yml or clear_cache when DNS/VPN are only dependency checks.
_ORIGINAL_BUILD_AUTOMATION_PLAN_WITH_VPN = build_automation_plan

def build_automation_plan(classification: str, linked_resolution=None, linked_playbook=None) -> dict:
    normalized = _normalize_key(classification)
    if (
        _is_container_connectivity_classification(normalized)
        or _container_context_present(normalized, linked_resolution, linked_playbook)
    ) and not _is_dns_primary_classification(normalized):
        existing = _ORIGINAL_BUILD_AUTOMATION_PLAN_WITH_VPN(
            classification="container_connection_issue",
            linked_resolution=linked_resolution,
            linked_playbook=linked_playbook,
        )
        return _force_container_connectivity_steps(existing, existing.get("execution_mode") if isinstance(existing, dict) else DEFAULT_EXECUTION_MODE)

    return _ORIGINAL_BUILD_AUTOMATION_PLAN_WITH_VPN(
        classification=classification,
        linked_resolution=linked_resolution,
        linked_playbook=linked_playbook,
    )
# ---- end container connectivity automation mapping override ----



# ---- Automation Candidate Registry enabled mapping override ----
# Additive, governed mapping stage. This wrapper checks data/automation_registry.json
# first and only uses entries explicitly marked enabled=True. Existing static,
# learned, VPN, and container guardrail behavior remains intact for everything else.
_ORIGINAL_BUILD_AUTOMATION_PLAN_WITH_CANDIDATE_REGISTRY = build_automation_plan

def build_automation_plan(classification: str, linked_resolution=None, linked_playbook=None) -> dict:
    normalized = _normalize_key(classification)
    registry_entry = _load_automation_registry().get(normalized)
    if isinstance(registry_entry, dict) and registry_entry.get("enabled") is True:
        action = _normalize_key(registry_entry.get("action") or (registry_entry.get("actions") or [""])[0])
        playbook = str(registry_entry.get("playbook") or "").strip()
        connector = str(registry_entry.get("connector") or "ansible").strip().lower()
        risk_tier = str(registry_entry.get("risk_tier") or "medium").strip().lower()
        target_host = str(registry_entry.get("target_host") or "").strip()
        target_group = str(registry_entry.get("target_group") or "").strip()
        approval_required = bool(registry_entry.get("approval_required", True))
        mode = str(registry_entry.get("execution_mode") or DEFAULT_EXECUTION_MODE or "simulation").strip().lower()

        if action and playbook:
            step = {
                "step_id": f"{action}_1",
                "action": action,
                "connector": connector,
                "playbook": playbook,
                "target_host": target_host,
                "target_group": target_group,
                "risk_tier": risk_tier,
                "approval_required": approval_required,
                "rollback_available": False,
                "rollback_strategy": None,
                "execution_mode": mode,
                "status": "planned",
                "mapping_source": "automation_registry",
                "description": registry_entry.get("description") or f"Execute approved registry automation for {normalized}.",
            }
            return {
                "classification": normalized,
                "automation_ready": True,
                "approval_mode": "manual_approval_required" if approval_required else "auto_allowed",
                "rollback_available": False,
                "rollback_strategy": None,
                "risk_tier": risk_tier,
                "execution_mode": mode,
                "mapping_source": "automation_registry",
                "step_count": 1,
                "steps": [step],
                "registry_entry": registry_entry,
            }

    return _ORIGINAL_BUILD_AUTOMATION_PLAN_WITH_CANDIDATE_REGISTRY(
        classification=classification,
        linked_resolution=linked_resolution,
        linked_playbook=linked_playbook,
    )
# ---- end Automation Candidate Registry enabled mapping override ----
