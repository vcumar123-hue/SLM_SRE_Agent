"""
connectors/jira.py — Item #20
Jira inbound connector: REST API polling + webhook normalisation.

Environment variables required:
    JIRA_URL        — e.g. "https://mycompany.atlassian.net"
    JIRA_USER       — Atlassian account email
    JIRA_TOKEN      — API token (from id.atlassian.com)
    JIRA_PROJECT    — Project key to poll (e.g. "INFRA")
    JIRA_JQL        — Optional override JQL (default: project=PROJECT AND status!=Done)
    JIRA_TENANT_ID  — Tenant id injected into normalised tickets (default: "default")

Usage:
    connector = JiraConnector()
    tickets = connector.fetch(limit=20)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from connectors.base import BaseConnector, TicketPayload

logger = logging.getLogger(__name__)

_PRIORITY_MAP = {
    "highest":  "critical",
    "blocker":  "critical",
    "critical":  "critical",
    "high":     "high",
    "medium":   "medium",
    "low":      "low",
    "lowest":   "low",
    "trivial":  "low",
}


def _norm_priority(raw: Any) -> str:
    return _PRIORITY_MAP.get(str(raw).strip().lower(), "medium")


class JiraConnector(BaseConnector):
    source = "jira"

    def __init__(self) -> None:
        self._url     = os.getenv("JIRA_URL", "").rstrip("/")
        self._user    = os.getenv("JIRA_USER", "")
        self._token   = os.getenv("JIRA_TOKEN", "")
        self._project = os.getenv("JIRA_PROJECT", "")
        self._jql     = os.getenv("JIRA_JQL", "")
        self._tenant  = os.getenv("JIRA_TENANT_ID", "default")
        if not self._url:
            logger.warning("JiraConnector: JIRA_URL not set — fetch will return []")

    def _issue_to_ticket(self, issue: Dict[str, Any]) -> TicketPayload:
        fields    = issue.get("fields") or {}
        issue_key = str(issue.get("key") or issue.get("id") or "")
        title     = str(fields.get("summary") or "")
        desc_raw  = fields.get("description") or ""
        # Jira description can be Atlassian Document Format (ADF) dict or plain text
        if isinstance(desc_raw, dict):
            desc = " ".join(
                node.get("text", "")
                for block in desc_raw.get("content", [])
                for node in (block.get("content") or [])
                if isinstance(node, dict)
            )
        else:
            desc = str(desc_raw)
        priority  = _norm_priority((fields.get("priority") or {}).get("name", "medium"))
        status_v  = str((fields.get("status") or {}).get("name") or "open").lower()
        category  = str((fields.get("issuetype") or {}).get("name") or "")
        assignee  = str((fields.get("assignee") or {}).get("displayName") or "")
        reporter  = str((fields.get("reporter") or {}).get("displayName") or "")
        created   = str(fields.get("created") or "")[:19]
        updated   = str(fields.get("updated") or "")[:19]
        return TicketPayload(
            ticket_id=issue_key,
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
            raw=issue,
        )

    def _build_jql(self) -> str:
        if self._jql:
            return self._jql
        base = f"project = {self._project}" if self._project else "project is not EMPTY"
        return f"{base} AND status != Done ORDER BY created DESC"

    def fetch(self, limit: int = 50, since: Optional[str] = None) -> List[TicketPayload]:
        if not self._url or not self._user or not self._token:
            logger.warning("JiraConnector: not configured — skipping fetch")
            return []
        try:
            import requests
        except ImportError:
            logger.error("JiraConnector: requests not installed")
            return []

        jql = self._build_jql()
        if since:
            jql += f" AND created >= {since}"

        params: Dict[str, Any] = {
            "jql": jql,
            "maxResults": limit,
            "fields": "summary,description,priority,status,issuetype,assignee,reporter,created,updated",
        }
        try:
            resp = requests.get(
                f"{self._url}/rest/api/3/search",
                auth=(self._user, self._token),
                params=params,
                timeout=20,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            issues = resp.json().get("issues", [])
            tickets = [self._issue_to_ticket(i) for i in issues]
            logger.info("JiraConnector: fetched %d tickets", len(tickets))
            return tickets
        except Exception as exc:
            logger.warning("JiraConnector.fetch failed: %s", exc)
            return []

    def webhook_payload_to_ticket(self, payload: Dict[str, Any]) -> Optional[TicketPayload]:
        """Normalise a Jira webhook payload (issue_created / issue_updated events)."""
        try:
            issue = payload.get("issue", payload)
            return self._issue_to_ticket(issue)
        except Exception as exc:
            logger.warning("JiraConnector.webhook_payload_to_ticket failed: %s", exc)
            return None
