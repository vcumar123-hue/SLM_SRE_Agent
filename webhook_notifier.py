"""
Webhook / Slack alert helper for autonomous execution events.

Send a notification after an autonomous execution completes.  Supports:
  - Slack incoming webhooks (detected by payload shape)
  - Generic HTTP webhook (JSON POST)

Configuration (env vars):
  AIOPS_WEBHOOK_URL       - Webhook endpoint URL (required to enable)
  AIOPS_UI_BASE_URL       - Base URL of the Streamlit UI (default http://localhost:8501)
                            Used to build the one-click rollback deep-link.
  AIOPS_WEBHOOK_TIMEOUT   - Request timeout in seconds (default 8)
"""
from __future__ import annotations
import json
import os
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

_WEBHOOK_URL: Optional[str] = os.getenv("AIOPS_WEBHOOK_URL")
_UI_BASE: str = os.getenv("AIOPS_UI_BASE_URL", "http://localhost:8501").rstrip("/")
_TIMEOUT: int = int(os.getenv("AIOPS_WEBHOOK_TIMEOUT", "8"))


def _rollback_link(execution_id: str, queue_id: str) -> str:
    return f"{_UI_BASE}/?page=automation_queue&action=rollback&execution_id={execution_id}&queue_id={queue_id}"


def _is_slack_url(url: str) -> bool:
    return "hooks.slack.com" in url or url.startswith("https://hooks.slack")


def _slack_payload(execution: Dict[str, Any]) -> Dict[str, Any]:
    execution_id = execution.get("execution_id", "unknown")
    queue_id = execution.get("queue_id", "")
    classification = execution.get("classification", "unknown")
    status = execution.get("status", "unknown")
    host = execution.get("target_host") or execution.get("incident_id") or "unknown"
    mode = execution.get("execution_mode", "simulation")
    rollback_url = _rollback_link(execution_id, queue_id)
    rollback_available = bool(execution.get("rollback_available"))

    color = "#36a64f" if "ok" in status or "complete" in status else "#e74c3c"

    rollback_action = (
        f"<{rollback_url}|Rollback this execution>"
        if rollback_available
        else "_Rollback not available for this execution._"
    )

    return {
        "attachments": [
            {
                "color": color,
                "title": f"[AIOps] Autonomous Execution: {classification}",
                "fields": [
                    {"title": "Status", "value": status, "short": True},
                    {"title": "Mode", "value": mode, "short": True},
                    {"title": "Host / Incident", "value": host, "short": True},
                    {"title": "Execution ID", "value": execution_id, "short": True},
                ],
                "text": rollback_action,
                "footer": "AIOps SLM",
                "mrkdwn_in": ["text"],
            }
        ]
    }


def _generic_payload(execution: Dict[str, Any]) -> Dict[str, Any]:
    execution_id = execution.get("execution_id", "unknown")
    queue_id = execution.get("queue_id", "")
    return {
        "event": "autonomous_execution",
        "execution_id": execution_id,
        "queue_id": queue_id,
        "classification": execution.get("classification", "unknown"),
        "status": execution.get("status", "unknown"),
        "execution_mode": execution.get("execution_mode", "simulation"),
        "target_host": execution.get("target_host") or execution.get("incident_id"),
        "rollback_available": bool(execution.get("rollback_available")),
        "rollback_link": _rollback_link(execution_id, queue_id),
    }


def notify_execution(execution: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fire a webhook notification for an autonomous execution event.

    Returns a dict with keys: sent (bool), skipped (bool), error (str|None).
    Never raises — callers should not fail due to notification errors.
    """
    if not _WEBHOOK_URL:
        return {"sent": False, "skipped": True, "reason": "AIOPS_WEBHOOK_URL not set"}

    try:
        if _is_slack_url(_WEBHOOK_URL):
            payload = _slack_payload(execution)
        else:
            payload = _generic_payload(execution)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return {"sent": True, "skipped": False, "http_status": r.status, "error": None}
    except urllib.error.HTTPError as e:
        return {"sent": False, "skipped": False, "http_status": e.code, "error": str(e)}
    except Exception:
        return {"sent": False, "skipped": False, "error": traceback.format_exc()[-300:]}
