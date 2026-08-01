from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import settings_portable as settings


def _safe_path(value: Any) -> Path:
    return value if isinstance(value, Path) else Path(str(value))


def _data_dir() -> Path:
    return _safe_path(getattr(settings, "DATA_DIR", Path("data")))


def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


TELEMETRY_EVENTS_FILE = _data_dir() / "telemetry_events.json"
TELEMETRY_INCIDENTS_FILE = _data_dir() / "telemetry_incidents.json"
INCIDENTS_FILE = _safe_path(getattr(settings, "INCIDENTS_FILE", _data_dir() / "incidents.json"))


TELEMETRY_RULES: List[Dict[str, Any]] = [
    {
        "id": "cpu_sustained_high",
        "metric": "cpu_percent",
        "operator": ">=",
        "threshold": 95.0,
        "required_count": 3,
        "window_minutes": 15,
        "classification": "cpu_saturation",
        "title": "Telemetry detected sustained high CPU",
        "domain": "compute",
        "service_scope": "compute",
        "severity": "high",
    },
    {
        "id": "memory_pressure_high",
        "metric": "memory_percent",
        "operator": ">=",
        "threshold": 92.0,
        "required_count": 3,
        "window_minutes": 15,
        "classification": "memory_pressure",
        "title": "Telemetry detected memory pressure",
        "domain": "compute",
        "service_scope": "compute",
        "severity": "high",
    },
    {
        "id": "disk_utilization_high",
        "metric": "disk_percent",
        "operator": ">=",
        "threshold": 90.0,
        "required_count": 2,
        "window_minutes": 20,
        "classification": "disk_full",
        "title": "Telemetry detected storage capacity threshold breach",
        "domain": "compute",
        "service_scope": "storage",
        "severity": "high",
    },
    {
        "id": "api_latency_spike",
        "metric": "api_latency_ms",
        "operator": ">=",
        "threshold": 2000.0,
        "required_count": 3,
        "window_minutes": 10,
        "classification": "api_gateway_timeout",
        "title": "Telemetry detected API latency spike",
        "domain": "general",
        "service_scope": "middleware",
        "severity": "high",
    },
    {
        "id": "pod_restart_storm",
        "metric": "pod_restarts",
        "operator": ">=",
        "threshold": 5.0,
        "required_count": 2,
        "window_minutes": 15,
        "classification": "pod_crash_loop",
        "title": "Telemetry detected pod restart storm",
        "domain": "general",
        "service_scope": "kubernetes",
        "severity": "critical",
    },
    {
        "id": "cert_expiry_warning",
        "metric": "cert_days_to_expiry",
        "operator": "<=",
        "threshold": 14.0,
        "required_count": 1,
        "window_minutes": 1440,
        "classification": "certificate_expiry_warning",
        "title": "Telemetry detected certificate nearing expiry",
        "domain": "general",
        "service_scope": "security",
        "severity": "medium",
    },
]


def _compare(value: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return value >= threshold
    if operator == ">":
        return value > threshold
    if operator == "<=":
        return value <= threshold
    if operator == "<":
        return value < threshold
    if operator == "==":
        return value == threshold
    return False


def _normalize_event(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_id": str(raw.get("event_id") or f"evt_{uuid.uuid4().hex[:12]}"),
        "timestamp": str(raw.get("timestamp") or _utc_now_iso()),
        "metric": str(raw.get("metric") or "").strip(),
        "value": float(raw.get("value", 0.0) or 0.0),
        "resource": str(raw.get("resource") or "").strip(),
        "service": str(raw.get("service") or "").strip(),
        "environment": str(raw.get("environment") or "production").strip(),
        "source": str(raw.get("source") or "telemetry").strip(),
        "labels": raw.get("labels") if isinstance(raw.get("labels"), dict) else {},
    }


def append_telemetry_event(raw: Dict[str, Any]) -> Dict[str, Any]:
    event = _normalize_event(raw)
    events = _safe_read_json(TELEMETRY_EVENTS_FILE, default=[])
    if not isinstance(events, list):
        events = []
    events.append(event)
    _safe_write_json(TELEMETRY_EVENTS_FILE, events)
    return event


def _events_matching_rule(events: List[Dict[str, Any]], rule: Dict[str, Any]) -> List[Dict[str, Any]]:
    metric = rule["metric"]
    threshold = float(rule["threshold"])
    operator = rule["operator"]
    matched = []
    for evt in events:
        if evt.get("metric") != metric:
            continue
        if _compare(float(evt.get("value", 0.0) or 0.0), operator, threshold):
            matched.append(evt)
    return matched


def _incident_exists(recent: List[Dict[str, Any]], rule: Dict[str, Any], resource: str, environment: str) -> bool:
    for item in recent:
        if (
            item.get("source") == "telemetry_trigger"
            and item.get("classification") == rule["classification"]
            and item.get("resource") == resource
            and item.get("environment") == environment
        ):
            return True
    return False


def _make_incident(rule: Dict[str, Any], event: Dict[str, Any], matched_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    resource = event.get("resource") or event.get("service") or "unknown_resource"
    env = event.get("environment") or "production"
    metric = event.get("metric")
    latest_value = event.get("value")
    content = (
        f"Telemetry rule '{rule['id']}' fired for resource '{resource}' in environment '{env}'. "
        f"Metric {metric} crossed threshold {rule['operator']} {rule['threshold']} "
        f"with latest value {latest_value}. "
        f"Matched {len(matched_events)} event(s) within the evaluation window."
    )
    return {
        "id": f"inc_tel_{uuid.uuid4().hex[:10]}",
        "domain": rule["domain"],
        "classification": rule["classification"],
        "title": rule["title"],
        "content": content,
        "source": "telemetry_trigger",
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
        "service_scope": rule.get("service_scope", "general"),
        "environment": env,
        "severity": rule.get("severity", "medium"),
        "status": "new",
        "ticket_sync_status": "not_linked",
        "resource": resource,
        "telemetry_rule_id": rule["id"],
        "trigger_metric": metric,
        "trigger_value": latest_value,
        "matched_event_ids": [x.get("event_id") for x in matched_events],
    }


def evaluate_telemetry_rules() -> Dict[str, Any]:
    events = _safe_read_json(TELEMETRY_EVENTS_FILE, default=[])
    if not isinstance(events, list):
        events = []
    incidents = _safe_read_json(TELEMETRY_INCIDENTS_FILE, default=[])
    if not isinstance(incidents, list):
        incidents = []

    created: List[Dict[str, Any]] = []
    for rule in TELEMETRY_RULES:
        matched = _events_matching_rule(events, rule)
        if len(matched) < int(rule["required_count"]):
            continue
        latest = matched[-1]
        resource = latest.get("resource") or latest.get("service") or "unknown_resource"
        environment = latest.get("environment") or "production"
        if _incident_exists(incidents, rule, resource, environment):
            continue
        incident = _make_incident(rule, latest, matched)
        incidents.append(incident)
        created.append(incident)

    _safe_write_json(TELEMETRY_INCIDENTS_FILE, incidents)
    return {
        "status": "ok",
        "event_count": len(events),
        "telemetry_incident_count": len(incidents),
        "created_count": len(created),
        "created_incidents": created,
    }


def promote_telemetry_incidents_to_main_knowledge(limit: Optional[int] = None) -> Dict[str, Any]:
    telemetry_incidents = _safe_read_json(TELEMETRY_INCIDENTS_FILE, default=[])
    main_incidents = _safe_read_json(INCIDENTS_FILE, default=[])
    if not isinstance(telemetry_incidents, list):
        telemetry_incidents = []
    if not isinstance(main_incidents, list):
        main_incidents = []

    existing_ids = {str(x.get("id")) for x in main_incidents if isinstance(x, dict)}
    promoted = []
    for item in telemetry_incidents:
        if not isinstance(item, dict):
            continue
        if item.get("id") in existing_ids:
            continue
        main_incidents.append(item)
        promoted.append(item)
        existing_ids.add(str(item.get("id")))
        if limit is not None and len(promoted) >= limit:
            break

    _safe_write_json(INCIDENTS_FILE, main_incidents)
    return {
        "status": "ok",
        "promoted_count": len(promoted),
        "incidents_file": str(INCIDENTS_FILE),
        "promoted_ids": [x.get("id") for x in promoted],
    }


def _sort_desc_by_timestamp(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key_fn(item: Dict[str, Any]) -> str:
        return str(item.get("updated_at") or item.get("created_at") or item.get("timestamp") or "")
    return sorted([x for x in items if isinstance(x, dict)], key=key_fn, reverse=True)


def list_telemetry_events(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent telemetry events from the JSON fallback store."""
    events = _safe_read_json(TELEMETRY_EVENTS_FILE, default=[])
    if not isinstance(events, list):
        events = []
    limit = max(1, min(int(limit or 100), 1000))
    return _sort_desc_by_timestamp(events)[:limit]


def list_telemetry_incidents(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent telemetry incidents from the JSON fallback store."""
    incidents = _safe_read_json(TELEMETRY_INCIDENTS_FILE, default=[])
    if not isinstance(incidents, list):
        incidents = []
    limit = max(1, min(int(limit or 100), 1000))
    return _sort_desc_by_timestamp(incidents)[:limit]


def get_telemetry_incident_by_id(incident_id: str) -> Optional[Dict[str, Any]]:
    """Read one telemetry incident from the JSON fallback store."""
    needle = str(incident_id or "").strip()
    if not needle:
        return None
    for item in list_telemetry_incidents(limit=1000):
        if str(item.get("id") or "") == needle:
            return item
    return None


TELEMETRY_STATUS_VALUES = {
    "new", "rca_complete", "acknowledged", "in_progress", "resolved", "suppressed"
}


def _normalize_status(status: Optional[str]) -> str:
    value = str(status or "new").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "rca": "rca_complete",
        "rca_completed": "rca_complete",
        "complete": "rca_complete",
        "completed": "rca_complete",
        "ack": "acknowledged",
        "acknowledge": "acknowledged",
        "working": "in_progress",
        "done": "resolved",
        "closed": "resolved",
        "suppress": "suppressed",
    }
    value = aliases.get(value, value)
    if value not in TELEMETRY_STATUS_VALUES:
        raise ValueError(f"Unsupported telemetry status: {status}")
    return value


def _history_file() -> Path:
    return _data_dir() / "telemetry_incident_status_history.json"


def update_telemetry_incident_status(
    incident_id: str,
    status: str,
    updated_by: Optional[str] = None,
    notes: Optional[str] = None,
    external_system: Optional[str] = None,
    external_ticket_id: Optional[str] = None,
    external_ticket_url: Optional[str] = None,
    ticket_sync_status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    needle = str(incident_id or "").strip()
    if not needle:
        return None
    incidents = _safe_read_json(TELEMETRY_INCIDENTS_FILE, default=[])
    if not isinstance(incidents, list):
        incidents = []
    new_status = _normalize_status(status)
    now = _utc_now_iso()
    updated_item = None
    old_status = "new"
    for item in incidents:
        if not isinstance(item, dict) or str(item.get("id") or "") != needle:
            continue
        old_status = str(item.get("status") or "new")
        item["status"] = new_status
        item["status_updated_by"] = updated_by or "system"
        item["status_updated_at"] = now
        item["status_note"] = notes or ""
        item["updated_at"] = now
        if new_status == "acknowledged":
            item.setdefault("acknowledged_by", updated_by or "system")
            item.setdefault("acknowledged_at", now)
        elif new_status == "in_progress":
            item.setdefault("in_progress_by", updated_by or "system")
            item.setdefault("in_progress_at", now)
        elif new_status == "resolved":
            item.setdefault("resolved_by", updated_by or "system")
            item.setdefault("resolved_at", now)
        elif new_status == "suppressed":
            item.setdefault("suppressed_by", updated_by or "system")
            item.setdefault("suppressed_at", now)
        if external_system is not None:
            item["external_system"] = external_system
        if external_ticket_id is not None:
            item["external_ticket_id"] = external_ticket_id
        if external_ticket_url is not None:
            item["external_ticket_url"] = external_ticket_url
        if ticket_sync_status is not None:
            item["ticket_sync_status"] = ticket_sync_status
            if ticket_sync_status != "not_linked":
                item["last_synced_at"] = now
        updated_item = dict(item)
        break
    if updated_item is None:
        return None
    _safe_write_json(TELEMETRY_INCIDENTS_FILE, incidents)
    history = _safe_read_json(_history_file(), default=[])
    if not isinstance(history, list):
        history = []
    history.append({
        "telemetry_incident_id": needle,
        "old_status": old_status,
        "new_status": new_status,
        "updated_by": updated_by or "system",
        "notes": notes or "",
        "created_at": now,
    })
    _safe_write_json(_history_file(), history)
    return updated_item


def mark_telemetry_incident_rca_complete(incident_id: str, updated_by: str = "ai_rca", notes: Optional[str] = None) -> Optional[Dict[str, Any]]:
    item = get_telemetry_incident_by_id(incident_id)
    if not item:
        return None
    current = str(item.get("status") or "new")
    if current in {"new", "rca_complete", ""}:
        return update_telemetry_incident_status(
            incident_id,
            "rca_complete",
            updated_by=updated_by,
            notes=notes or "AI RCA completed and stored.",
        )
    return item


def get_telemetry_incident_status_history(incident_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    needle = str(incident_id or "").strip()
    history = _safe_read_json(_history_file(), default=[])
    if not isinstance(history, list):
        history = []
    rows = [x for x in history if isinstance(x, dict) and str(x.get("telemetry_incident_id") or "") == needle]
    rows = _sort_desc_by_timestamp(rows)
    limit = max(1, min(int(limit or 100), 1000))
    return rows[:limit]


if __name__ == "__main__":
    # Example self-test
    append_telemetry_event({"metric": "cpu_percent", "value": 97, "resource": "srv-app-01"})
    append_telemetry_event({"metric": "cpu_percent", "value": 96, "resource": "srv-app-01"})
    append_telemetry_event({"metric": "cpu_percent", "value": 98, "resource": "srv-app-01"})
    print(json.dumps(evaluate_telemetry_rules(), indent=2))
