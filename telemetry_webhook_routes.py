from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

from telemetry_trigger import append_telemetry_event, evaluate_telemetry_rules

router = APIRouter()


class TelemetryEventRequest(BaseModel):
    metric: str
    value: float
    resource: str = ""
    service: str = ""
    environment: str = "production"
    source: str = "manual"
    labels: Dict[str, Any] = {}


@router.post("/telemetry/event")
def ingest_telemetry_event(request: TelemetryEventRequest) -> Dict[str, Any]:
    event = append_telemetry_event(request.model_dump())
    evaluation = evaluate_telemetry_rules()
    return {
        "status": "ok",
        "event": event,
        "evaluation": evaluation,
    }


@router.post("/telemetry/alertmanager")
def ingest_alertmanager_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    alerts: List[Dict[str, Any]] = payload.get("alerts") or []
    ingested = []

    for alert in alerts:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}

        metric_name = labels.get("metric_name") or "alert_value"
        raw_value = alert.get("valueString") or labels.get("observed_value") or "0"
        numeric_value = 0.0

        try:
            # Pull the first float-looking token out of the string
            import re
            match = re.search(r"-?\d+(?:\.\d+)?", str(raw_value))
            if match:
                numeric_value = float(match.group(0))
        except Exception:
            numeric_value = 0.0

        resource = labels.get("instance") or labels.get("pod") or labels.get("job") or "unknown_resource"
        event = append_telemetry_event(
            {
                "metric": metric_name,
                "value": numeric_value,
                "resource": resource,
                "service": labels.get("job") or "",
                "environment": labels.get("environment") or "production",
                "source": "alertmanager",
                "labels": {
                    **labels,
                    "summary": annotations.get("summary", ""),
                    "description": annotations.get("description", ""),
                    "status": alert.get("status", "firing"),
                },
            }
        )
        ingested.append(event)

    evaluation = evaluate_telemetry_rules()
    return {
        "status": "ok",
        "ingested_count": len(ingested),
        "evaluation": evaluation,
    }
