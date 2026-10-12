"""
connectors/servicenow.py — Item #20
ServiceNow inbound connector: polling + webhook normalisation.

Environment variables required:
    SNOW_INSTANCE   — e.g. "mycompany.service-now.com"
    SNOW_USER       — Basic-auth username (or service account)
    SNOW_PASSWORD   — Basic-auth password
    SNOW_TABLE      — Table to poll (default: "incident")
    SNOW_TENANT_ID  — Tenant id injected into normalised tickets (default: "default")

Usage:
    connector = ServiceNowConnector()
    tickets = connector.fetch(limit=20)
    for t in tickets:
        classify(t.full_text, ticket_id=t.ticket_id)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from connectors.base import BaseConnector, TicketPayload

logger = logging.getLogger(__name__)

_PRIORITY_MAP = {
    "1": "critical",
    "2": "high",
    "3": "medium",
    "4": "low",
    "5": "low",
}


def _norm_priority(raw: Any) -> str:
    return _PRIORITY_MAP.get(str(raw).strip(), "medium")


class ServiceNowConnector(BaseConnector):
    source = "servicenow"

    def __init__(self) -> None:
        self._instance = os.getenv("SNOW_INSTANCE", "").rstrip("/")
        self._user     = os.getenv("SNOW_USER", "")
        self._password = os.getenv("SNOW_PASSWORD", "")
        self._table    = os.getenv("SNOW_TABLE", "incident")
        self._tenant   = os.getenv("SNOW_TENANT_ID", "default")
        if not self._instance:
            logger.warning("ServiceNowConnector: SNOW_INSTANCE not set — fetch will return []")

    def _base_url(self) -> str:
        return f"https://{self._instance}/api/now/table/{self._table}"

    def _record_to_ticket(self, rec: Dict[str, Any]) -> TicketPayload:
        sys_id    = str(rec.get("sys_id") or rec.get("number") or "")
        number    = str(rec.get("number") or sys_id)
        title     = str(rec.get("short_description") or "")
        desc      = str(rec.get("description") or rec.get("work_notes") or "")
        priority  = _norm_priority(rec.get("priority"))
        status_v  = str(rec.get("state") or rec.get("incident_state") or "open").lower()
        category  = str(rec.get("category") or rec.get("subcategory") or "")
        assignee  = str((rec.get("assigned_to") or {}).get("display_value") or
                        rec.get("assigned_to") or "")
        reporter  = str((rec.get("caller_id") or {}).get("display_value") or
                        rec.get("caller_id") or "")
        created   = str(rec.get("sys_created_on") or "")
        updated   = str(rec.get("sys_updated_on") or "")
        return TicketPayload(
            ticket_id=number or sys_id,
            source=self.source,
            title=title,
            description=desc,
            priority=priority,
            status=status_v,
            category=category,
            assignee=assignee,
            reporter=reporter,
            tenant_id=self._tenant,
            created_at=created,
            updated_at=updated,
            raw=rec,
        )

    def fetch(self, limit: int = 50, since: Optional[str] = None) -> List[TicketPayload]:
        if not self._instance or not self._user:
            logger.warning("ServiceNowConnector: not configured — skipping fetch")
            return []
        try:
            import requests
        except ImportError:
            logger.error("ServiceNowConnector: requests not installed")
            return []

        params: Dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_fields": ("sys_id,number,short_description,description,priority,"
                               "state,incident_state,category,subcategory,"
                               "assigned_to,caller_id,sys_created_on,sys_updated_on"),
            "sysparm_query": "active=true^ORDERBYDESCsys_created_on",
        }
        if since:
            params["sysparm_query"] += f"^sys_created_on>={since}"

        try:
            resp = requests.get(
                self._base_url(),
                auth=(self._user, self._password),
                params=params,
                timeout=20,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            records = resp.json().get("result", [])
            tickets = [self._record_to_ticket(r) for r in records]
            logger.info("ServiceNowConnector: fetched %d tickets", len(tickets))
            return tickets
        except Exception as exc:
            logger.warning("ServiceNowConnector.fetch failed: %s", exc)
            return []

    def webhook_payload_to_ticket(self, payload: Dict[str, Any]) -> Optional[TicketPayload]:
        """Normalise a ServiceNow webhook (outbound REST message) payload."""
        try:
            rec = payload.get("records", [payload])[0] if "records" in payload else payload
            return self._record_to_ticket(rec)
        except Exception as exc:
            logger.warning("ServiceNowConnector.webhook_payload_to_ticket failed: %s", exc)
            return None
