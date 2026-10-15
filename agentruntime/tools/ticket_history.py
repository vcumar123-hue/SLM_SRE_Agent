"""
ticket_history_lookup: this tenant's own tickets by host, service, text and time window (design 4.3).

The tenant is a bound SQL parameter taken from the trusted context. The model supplies only short search terms; they are matched with
parameterised, wildcard-escaped ILIKE and never reach the SQL text. The result size is a SQL LIMIT, and the statement timeout comes
from the call's remaining time.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agentruntime.context import TrustedContext
from agentruntime.contracts import EvidenceDraft, ToolSpec
from agentruntime.db_limits import statement_timeout
from agentruntime.executor import CancelToken
from agentruntime.registry import BoundaryViolation, ToolInputError
from agentruntime.tools._common import check_cancel, clip, like_pattern, parse_timestamp

NAME = "ticket_history_lookup"
VERSION = "1"
MAX_ITEMS = 8
TTL_DAYS = 400
HOSTNAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}"

SPEC = ToolSpec(
    name=NAME, version=VERSION, max_items=MAX_ITEMS,
    description="Look up this client's own earlier tickets by host name, service or text within a time window (default 180 days, at most 400). Give at least one of host, service or text.",
    input_schema={
        "type": "object", "additionalProperties": False,
        "properties": {
            "host": {"type": "string", "minLength": 2, "maxLength": 80, "pattern": HOSTNAME_PATTERN},
            "service": {"type": "string", "minLength": 2, "maxLength": 80},
            "text": {"type": "string", "minLength": 3, "maxLength": 200},
            "days": {"type": "integer", "minimum": 1, "maximum": TTL_DAYS},
        },
    },
)

# opened_at is stored as text; only values that start like a date are compared
_OPENED_TS = ("CASE WHEN opened_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
              "THEN substring(opened_at from 1 for 19)::timestamp END")


def _default_connect():
    import postgres_store
    return postgres_store.db_cursor(dict_rows=True)


def build_query(tenant_id: str, host: Optional[str], service: Optional[str], text: Optional[str], days: int) -> tuple:
    """(sql, params). The tenant and every search term are parameters; only fixed fragments are concatenated."""
    where = ["tenant_id = %(tenant)s", "opened_ts >= now() - make_interval(days => %(days)s)"]
    params: Dict[str, Any] = {"tenant": tenant_id, "days": int(days), "limit": MAX_ITEMS}
    if host:
        where.append("(short_description ILIKE %(host)s ESCAPE '\\' OR raw_json::text ILIKE %(host)s ESCAPE '\\')")
        params["host"] = like_pattern(host)
    if service:
        where.append("(service ILIKE %(service)s ESCAPE '\\' OR short_description ILIKE %(service)s ESCAPE '\\')")
        params["service"] = like_pattern(service)
    if text:
        where.append("(short_description ILIKE %(text)s ESCAPE '\\' OR raw_json::text ILIKE %(text)s ESCAPE '\\')")
        params["text"] = like_pattern(text)
    sql = (
        "SELECT ticket_id, opened_at, resolved_at, assignment_group, priority, service, short_description, raw_json, opened_ts FROM ("
        f"SELECT *, {_OPENED_TS} AS opened_ts FROM client_ticket_records) t "
        f"WHERE {' AND '.join(where)} ORDER BY opened_ts DESC NULLS LAST LIMIT %(limit)s"
    )
    return sql, params


def make_tool(connect: Optional[Callable[[], Any]] = None) -> Callable[..., List[EvidenceDraft]]:
    def ticket_history_lookup(ctx: TrustedContext, host: Optional[str] = None, service: Optional[str] = None,
                              text: Optional[str] = None, days: int = 180, *, cancel: Optional[CancelToken] = None,
                              timeout_s: Optional[float] = None) -> List[EvidenceDraft]:
        if not (host or service or text):
            raise ToolInputError("give at least one of host, service or text")
        check_cancel(cancel)
        sql, params = build_query(ctx.tenant_id, host, service, text, days)
        with statement_timeout(timeout_s), (connect or _default_connect)() as opened:
            cur = opened[1] if isinstance(opened, tuple) else opened
            cur.execute(sql, params)
            rows = cur.fetchmany(MAX_ITEMS)               # bounded again at fetch time
        check_cancel(cancel)
        drafts: List[EvidenceDraft] = []
        for row in rows[:MAX_ITEMS]:
            if str(row.get("tenant_id", ctx.tenant_id)) != ctx.tenant_id:       # the SQL filters it; this is defence in depth
                raise BoundaryViolation("ticket_history_lookup returned a ticket of another tenant")
            raw = row.get("raw_json") or {}
            raw = raw if isinstance(raw, dict) else {}
            detail = clip(raw.get("Description") or raw.get("Details") or raw.get("Description ") or "", 200)
            opened_at, resolved_at = row.get("opened_at"), row.get("resolved_at")
            opened, resolved = parse_timestamp(opened_at), parse_timestamp(resolved_at)
            hours = round((resolved - opened).total_seconds() / 3600.0, 2) if opened and resolved and resolved >= opened else None
            summary = clip(row.get("short_description"), 160)
            drafts.append(EvidenceDraft(
                tenant_id=ctx.tenant_id, source_type="ticket", source_id=str(row.get("ticket_id") or ""), provenance="tenant_data",
                data_timestamp=opened, ttl_days=TTL_DAYS,
                excerpt=clip(f"{summary} | group: {clip(row.get('assignment_group'), 60)} | opened {opened_at} | resolved {resolved_at or 'open'}"
                             + (f" | {detail}" if detail else "")),
                facts={"opened_at": clip(opened_at, 30), "resolved_at": clip(resolved_at, 30), "assignment_group": clip(row.get("assignment_group"), 60),
                       "priority": clip(row.get("priority"), 30), "service": clip(row.get("service"), 60), "hours_to_resolve": hours},
            ))
        return [d for d in drafts if d.source_id]

    return ticket_history_lookup
