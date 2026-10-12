"""
connectors/base.py — Item #20
Shared normalised ticket schema and base connector interface.
All connectors return TicketPayload dicts so the classification
pipeline never needs to know the source system.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class TicketPayload:
    """Normalised incident ticket — source-system agnostic."""
    ticket_id: str
    source: str                          # "servicenow" | "jira" | ...
    title: str
    description: str
    priority: str = "medium"            # critical / high / medium / low
    status: str = "open"
    category: str = ""
    assignee: str = ""
    reporter: str = ""
    tenant_id: str = "default"
    created_at: str = ""
    updated_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def full_text(self) -> str:
        """Combined text passed to the classifier."""
        return f"{self.title}\n\n{self.description}".strip()


class BaseConnector:
    """
    Abstract base for inbound ticket connectors.
    Subclasses implement fetch() and webhook_payload_to_ticket().
    """
    source: str = "base"

    def fetch(self, limit: int = 50, since: Optional[str] = None) -> List[TicketPayload]:
        raise NotImplementedError

    def webhook_payload_to_ticket(self, payload: Dict[str, Any]) -> Optional[TicketPayload]:
        raise NotImplementedError
