"""
postgres_store.py
AI Ops PostgreSQL storage layer (safe parallel-write version)

Purpose:
- Introduce PostgreSQL without breaking existing JSON flows.
- Can be imported by app.py, telemetry_trigger.py, automation_engine.py,
  feedback_learning_enhanced.py, loaders, etc.

Environment variables supported:
    Preferred:
        PGHOST
        PGPORT
        PGDATABASE
        PGUSER
        PGPASSWORD

    Backward-compatible:
        AIOPS_DB_HOST
        AIOPS_DB_PORT
        AIOPS_DB_NAME
        AIOPS_DB_USER
        AIOPS_DB_PASSWORD
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json, RealDictCursor

LOGGER = logging.getLogger("postgres_store")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = str(value).strip()
    return value if value else default


def get_db_config() -> Dict[str, Any]:
    return {
        "host": _env("PGHOST", _env("AIOPS_DB_HOST", "localhost")),
        "port": int(_env("PGPORT", _env("AIOPS_DB_PORT", "5432"))),
        "dbname": _env("PGDATABASE", _env("AIOPS_DB_NAME", "aiops")),
        "user": _env("PGUSER", _env("AIOPS_DB_USER", "aiops_user")),
        "password": _env("PGPASSWORD", _env("AIOPS_DB_PASSWORD", "StrongPassword123")),
        "connect_timeout": int(_env("PGCONNECT_TIMEOUT", "5")),
    }


def get_connection():
    return psycopg2.connect(**get_db_config())


@contextmanager
def db_cursor(commit: bool = False, dict_rows: bool = False):
    conn = None
    cur = None
    try:
        conn = get_connection()
        factory = RealDictCursor if dict_rows else None
        cur = conn.cursor(cursor_factory=factory)
        # Inject tenant context into PostgreSQL session for RLS
        try:
            from tenant_context import get_tenant as _get_tenant
            _tid = _get_tenant()
            cur.execute("SET LOCAL app.current_tenant = %s", (_tid,))
        except Exception:
            pass
        yield conn, cur
        if commit:
            conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        LOGGER.exception("Database operation failed")
        raise
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def ping() -> bool:
    try:
        with db_cursor() as (_, cur):
            cur.execute("SELECT 1")
            row = cur.fetchone()
        return bool(row)
    except Exception:
        cfg = get_db_config()
        LOGGER.exception(
            "Database ping failed with config host=%s port=%s dbname=%s user=%s",
            cfg.get("host"),
            cfg.get("port"),
            cfg.get("dbname"),
            cfg.get("user"),
        )
        return False


# ---------------------------------------------------------
# INCIDENTS
# ---------------------------------------------------------

def upsert_incident(record: Dict[str, Any]) -> None:
    sql = """
    INSERT INTO incidents (
        id, domain, classification, title, content, source,
        confidence, linked_candidate_id, service_scope,
        environment, severity
    )
    VALUES (
        %(id)s, %(domain)s, %(classification)s, %(title)s, %(content)s, %(source)s,
        %(confidence)s, %(linked_candidate_id)s, %(service_scope)s,
        %(environment)s, %(severity)s
    )
    ON CONFLICT (id) DO UPDATE SET
        domain = EXCLUDED.domain,
        classification = EXCLUDED.classification,
        title = EXCLUDED.title,
        content = EXCLUDED.content,
        source = EXCLUDED.source,
        confidence = EXCLUDED.confidence,
        linked_candidate_id = EXCLUDED.linked_candidate_id,
        service_scope = EXCLUDED.service_scope,
        environment = EXCLUDED.environment,
        severity = EXCLUDED.severity,
        updated_at = NOW()
    """
    with db_cursor(commit=True) as (_, cur):
        cur.execute(sql, record)


def get_incidents(limit: int = 100) -> List[Dict[str, Any]]:
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT *
            FROM incidents
            ORDER BY updated_at DESC NULLS LAST, created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())


def get_incidents_by_domain(domain: str, limit: int = 100) -> List[Dict[str, Any]]:
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT *
            FROM incidents
            WHERE domain = %s
            ORDER BY updated_at DESC NULLS LAST
            LIMIT %s
            """,
            (domain, limit),
        )
        return list(cur.fetchall())


# ---------------------------------------------------------
# FEEDBACK EVENTS
# ---------------------------------------------------------

def insert_feedback_event(
    ticket: str,
    corrected_classification: str,
    request_id: Optional[str] = None,
    original_domain: Optional[str] = None,
    original_classification: Optional[str] = None,
    submitted_domain: Optional[str] = None,
    stored_domain: Optional[str] = None,
    is_correct: bool = False,
) -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO feedback_events (
                request_id, ticket, original_domain, original_classification,
                corrected_classification, submitted_domain, stored_domain, is_correct
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                request_id,
                ticket,
                original_domain,
                original_classification,
                corrected_classification,
                submitted_domain,
                stored_domain,
                is_correct,
            ),
        )


def get_recent_feedback(limit: int = 100) -> List[Dict[str, Any]]:
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT *
            FROM feedback_events
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())


# ---------------------------------------------------------
# FEEDBACK LEARNING WEIGHTS
# ---------------------------------------------------------

def upsert_learning_weight(
    key: str,
    domain: str,
    classification: str,
    weight: float = 1.0,
    effective_weight: float = 1.0,
    positive_count: int = 0,
    negative_count: int = 0,
    correction_count: int = 0,
    conflict_count: int = 0,
    last_ticket: Optional[str] = None,
    last_predicted_as: Optional[str] = None,
) -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO feedback_learning_weights (
                key, domain, classification, weight, effective_weight,
                positive_count, negative_count, correction_count,
                conflict_count, last_ticket, last_predicted_as, updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (key) DO UPDATE SET
                domain = EXCLUDED.domain,
                classification = EXCLUDED.classification,
                weight = EXCLUDED.weight,
                effective_weight = EXCLUDED.effective_weight,
                positive_count = EXCLUDED.positive_count,
                negative_count = EXCLUDED.negative_count,
                correction_count = EXCLUDED.correction_count,
                conflict_count = EXCLUDED.conflict_count,
                last_ticket = EXCLUDED.last_ticket,
                last_predicted_as = EXCLUDED.last_predicted_as,
                updated_at = NOW()
            """,
            (
                key,
                domain,
                classification,
                weight,
                effective_weight,
                positive_count,
                negative_count,
                correction_count,
                conflict_count,
                last_ticket,
                last_predicted_as,
            ),
        )


def get_learning_weights(limit: int = 500) -> List[Dict[str, Any]]:
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT *
            FROM feedback_learning_weights
            ORDER BY updated_at DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())


# ---------------------------------------------------------
# TELEMETRY
# ---------------------------------------------------------

def insert_telemetry_event(
    event_id: str,
    metric: str,
    value: float,
    resource: Optional[str] = None,
    service: Optional[str] = None,
    environment: Optional[str] = None,
    source: Optional[str] = None,
    labels: Optional[Dict[str, Any]] = None,
) -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO telemetry_events (
                event_id, metric, value, resource, service,
                environment, source, labels_json
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event_id,
                metric,
                value,
                resource,
                service,
                environment,
                source,
                Json(labels or {}),
            ),
        )


def insert_telemetry_incident(record: Dict[str, Any]) -> None:
    ensure_telemetry_lifecycle_schema()
    record = dict(record or {})
    record.setdefault("status", "new")
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO telemetry_incidents (
                id, domain, classification, title, content,
                resource, environment, severity,
                trigger_metric, trigger_value,
                telemetry_rule_id, source, status
            )
            VALUES (
                %(id)s, %(domain)s, %(classification)s, %(title)s, %(content)s,
                %(resource)s, %(environment)s, %(severity)s,
                %(trigger_metric)s, %(trigger_value)s,
                %(telemetry_rule_id)s, %(source)s, %(status)s
            )
            ON CONFLICT (id) DO UPDATE SET
                updated_at = NOW()
            """,
            record,
        )


def _coerce_limit(limit: int = 100, maximum: int = 1000) -> int:
    try:
        value = int(limit)
    except Exception:
        value = 100
    return max(1, min(value, maximum))


TELEMETRY_STATUS_VALUES = {
    "new",
    "rca_complete",
    "acknowledged",
    "in_progress",
    "resolved",
    "suppressed",
}


def normalize_telemetry_status(status: Optional[str]) -> str:
    value = str(status or "new").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "rca": "rca_complete",
        "rca_completed": "rca_complete",
        "complete": "rca_complete",
        "completed": "rca_complete",
        "ack": "acknowledged",
        "acknowledge": "acknowledged",
        "work_in_progress": "in_progress",
        "working": "in_progress",
        "done": "resolved",
        "closed": "resolved",
        "suppress": "suppressed",
    }
    value = aliases.get(value, value)
    if value not in TELEMETRY_STATUS_VALUES:
        raise ValueError(f"Unsupported telemetry status: {status}")
    return value


def ensure_telemetry_lifecycle_schema() -> None:
    """Add lifecycle/ITSM-readiness fields without requiring a destructive migration."""
    with db_cursor(commit=True) as (_, cur):
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'new'")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS acknowledged_by TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMP")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS in_progress_by TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS in_progress_at TIMESTAMP")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS resolved_by TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS suppressed_by TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMP")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS status_updated_by TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMP")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS status_note TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS external_system TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS external_ticket_id TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS external_ticket_url TEXT")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS ticket_sync_status TEXT DEFAULT 'not_linked'")
        cur.execute("ALTER TABLE telemetry_incidents ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_incident_status_history (
                id SERIAL PRIMARY KEY,
                telemetry_incident_id TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                updated_by TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


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
    ensure_telemetry_lifecycle_schema()
    new_status = normalize_telemetry_status(status)
    with db_cursor(commit=True, dict_rows=True) as (_, cur):
        cur.execute("SELECT status FROM telemetry_incidents WHERE id = %s LIMIT 1", (incident_id,))
        existing = cur.fetchone()
        if not existing:
            return None
        old_status = str(existing.get("status") or "new")
        updates = [
            "status = %s",
            "status_updated_by = %s",
            "status_updated_at = NOW()",
            "status_note = %s",
            "updated_at = NOW()",
        ]
        params: List[Any] = [new_status, updated_by, notes]

        if new_status == "acknowledged":
            updates.extend(["acknowledged_by = COALESCE(acknowledged_by, %s)", "acknowledged_at = COALESCE(acknowledged_at, NOW())"])
            params.append(updated_by)
        elif new_status == "in_progress":
            updates.extend(["in_progress_by = COALESCE(in_progress_by, %s)", "in_progress_at = COALESCE(in_progress_at, NOW())"])
            params.append(updated_by)
        elif new_status == "resolved":
            updates.extend(["resolved_by = COALESCE(resolved_by, %s)", "resolved_at = COALESCE(resolved_at, NOW())"])
            params.append(updated_by)
        elif new_status == "suppressed":
            updates.extend(["suppressed_by = COALESCE(suppressed_by, %s)", "suppressed_at = COALESCE(suppressed_at, NOW())"])
            params.append(updated_by)

        if external_system is not None:
            updates.append("external_system = %s")
            params.append(external_system)
        if external_ticket_id is not None:
            updates.append("external_ticket_id = %s")
            params.append(external_ticket_id)
        if external_ticket_url is not None:
            updates.append("external_ticket_url = %s")
            params.append(external_ticket_url)
        if ticket_sync_status is not None:
            updates.append("ticket_sync_status = %s")
            params.append(ticket_sync_status)
        if ticket_sync_status and ticket_sync_status != "not_linked":
            updates.append("last_synced_at = NOW()")

        params.append(incident_id)
        cur.execute(f"UPDATE telemetry_incidents SET {', '.join(updates)} WHERE id = %s", tuple(params))
        cur.execute(
            """
            INSERT INTO telemetry_incident_status_history (
                telemetry_incident_id, old_status, new_status, updated_by, notes
            ) VALUES (%s,%s,%s,%s,%s)
            """,
            (incident_id, old_status, new_status, updated_by, notes),
        )
        cur.execute("SELECT * FROM telemetry_incidents WHERE id = %s LIMIT 1", (incident_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def mark_telemetry_incident_rca_complete(incident_id: str, updated_by: str = "ai_rca", notes: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Set RCA complete only when the incident is still in a pre-workflow state."""
    ensure_telemetry_lifecycle_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute("SELECT status FROM telemetry_incidents WHERE id = %s LIMIT 1", (incident_id,))
        row = cur.fetchone()
    current = str((row or {}).get("status") or "new")
    if current in {"new", "rca_complete", ""}:
        return update_telemetry_incident_status(
            incident_id,
            "rca_complete",
            updated_by=updated_by,
            notes=notes or "AI RCA completed and stored.",
        )
    return get_telemetry_incident_by_id(incident_id)


def get_telemetry_incident_status_history(incident_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    ensure_telemetry_lifecycle_schema()
    limit = _coerce_limit(limit)
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT telemetry_incident_id, old_status, new_status, updated_by, notes, created_at
            FROM telemetry_incident_status_history
            WHERE telemetry_incident_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (incident_id, limit),
        )
        return list(cur.fetchall())


def get_telemetry_events(limit: int = 100) -> List[Dict[str, Any]]:
    limit = _coerce_limit(limit)
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT
                event_id, metric, value, resource, service, environment, source,
                labels_json AS labels, created_at AS timestamp
            FROM telemetry_events
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())


def get_telemetry_incidents(limit: int = 100) -> List[Dict[str, Any]]:
    ensure_telemetry_lifecycle_schema()
    limit = _coerce_limit(limit)
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT
                id, domain, classification, title, content, resource, environment, severity,
                trigger_metric, trigger_value, telemetry_rule_id, source, created_at, updated_at,
                COALESCE(status, 'new') AS status, acknowledged_by, acknowledged_at,
                in_progress_by, in_progress_at, resolved_by, resolved_at,
                suppressed_by, suppressed_at, status_updated_by, status_updated_at, status_note,
                external_system, external_ticket_id, external_ticket_url, ticket_sync_status, last_synced_at
            FROM telemetry_incidents
            ORDER BY updated_at DESC NULLS LAST, created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())


def get_telemetry_incident_by_id(incident_id: str) -> Optional[Dict[str, Any]]:
    ensure_telemetry_lifecycle_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT
                id, domain, classification, title, content, resource, environment, severity,
                trigger_metric, trigger_value, telemetry_rule_id, source, created_at, updated_at,
                COALESCE(status, 'new') AS status, acknowledged_by, acknowledged_at,
                in_progress_by, in_progress_at, resolved_by, resolved_at,
                suppressed_by, suppressed_at, status_updated_by, status_updated_at, status_note,
                external_system, external_ticket_id, external_ticket_url, ticket_sync_status, last_synced_at
            FROM telemetry_incidents
            WHERE id = %s
            LIMIT 1
            """,
            (incident_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def ensure_telemetry_rca_table() -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_rca_results (
                id SERIAL PRIMARY KEY,
                telemetry_incident_id TEXT NOT NULL UNIQUE,
                alert_name TEXT,
                classification TEXT,
                domain TEXT,
                confidence NUMERIC,
                likely_cause TEXT,
                next_checks JSONB,
                recommended_actions JSONB,
                similar_incidents JSONB,
                related_knowledge JSONB,
                suggested_playbook JSONB,
                automation_eligible BOOLEAN DEFAULT FALSE,
                fallback_used BOOLEAN DEFAULT FALSE,
                rca_json JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def upsert_telemetry_rca_result(telemetry_incident_id: str, result: Dict[str, Any]) -> None:
    ensure_telemetry_rca_table()
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    confidence_obj = result.get("confidence") if isinstance(result.get("confidence"), dict) else {}
    linked_playbook = result.get("linked_playbook") if isinstance(result.get("linked_playbook"), dict) else {}
    automation_summary = result.get("automation_summary") if isinstance(result.get("automation_summary"), dict) else {}
    similar = result.get("similar_incidents") or result.get("retrieved_incidents") or []
    likely_causes = analysis.get("probable_causes") or []
    likely_cause = "; ".join(str(x) for x in likely_causes[:3]) if isinstance(likely_causes, list) else str(likely_causes or "")
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO telemetry_rca_results (
                telemetry_incident_id, alert_name, classification, domain, confidence,
                likely_cause, next_checks, recommended_actions, similar_incidents, related_knowledge,
                suggested_playbook, automation_eligible, fallback_used, rca_json, updated_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (telemetry_incident_id) DO UPDATE SET
                alert_name = EXCLUDED.alert_name,
                classification = EXCLUDED.classification,
                domain = EXCLUDED.domain,
                confidence = EXCLUDED.confidence,
                likely_cause = EXCLUDED.likely_cause,
                next_checks = EXCLUDED.next_checks,
                recommended_actions = EXCLUDED.recommended_actions,
                similar_incidents = EXCLUDED.similar_incidents,
                related_knowledge = EXCLUDED.related_knowledge,
                suggested_playbook = EXCLUDED.suggested_playbook,
                automation_eligible = EXCLUDED.automation_eligible,
                fallback_used = EXCLUDED.fallback_used,
                rca_json = EXCLUDED.rca_json,
                updated_at = NOW()
            """,
            (
                telemetry_incident_id,
                str(result.get("telemetry_alert_name") or analysis.get("classification") or ""),
                str(analysis.get("classification") or result.get("classification") or ""),
                str(result.get("domain") or ""),
                confidence_obj.get("overall_confidence"),
                likely_cause,
                Json(analysis.get("next_checks") or []),
                Json(analysis.get("recommended_actions") or []),
                Json(similar if isinstance(similar, list) else []),
                Json(result.get("knowledge_summary") or {}),
                Json(linked_playbook or {}),
                bool(automation_summary.get("automation_ready") or linked_playbook.get("automation_ready") or False),
                bool(result.get("fallback_used", False)),
                Json(result),
            ),
        )


def get_telemetry_rca_result(telemetry_incident_id: str) -> Optional[Dict[str, Any]]:
    ensure_telemetry_rca_table()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            """
            SELECT *
            FROM telemetry_rca_results
            WHERE telemetry_incident_id = %s
            LIMIT 1
            """,
            (telemetry_incident_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------
# AUTOMATION
# ---------------------------------------------------------

def insert_automation_queue(
    queue_id: str,
    classification: str,
    status: str,
    plan: Dict[str, Any],
    incident_id: Optional[str] = None,
    requested_by: Optional[str] = None,
) -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO automation_queue (
                queue_id, incident_id, classification,
                requested_by, status, plan_json
            )
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                queue_id,
                incident_id,
                classification,
                requested_by,
                status,
                Json(plan),
            ),
        )


def update_automation_queue_status(
    queue_id: str,
    status: str,
    approved_by: Optional[str] = None,
    execution_id: Optional[str] = None,
) -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            UPDATE automation_queue
            SET status = %s,
                approved_by = COALESCE(%s, approved_by),
                approved_at = CASE WHEN %s IS NOT NULL THEN NOW() ELSE approved_at END,
                execution_id = COALESCE(%s, execution_id),
                executed_at = CASE
                    WHEN %s IN ('executed','completed','failed') THEN NOW()
                    ELSE executed_at
                END
            WHERE queue_id = %s
            """,
            (
                status,
                approved_by,
                approved_by,
                execution_id,
                status,
                queue_id,
            ),
        )


def insert_automation_execution(
    execution_id: str,
    classification: str,
    status: str,
    steps: Dict[str, Any],
    queue_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    executed_by: Optional[str] = None,
    rollback_available: bool = False,
) -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO automation_executions (
                execution_id, queue_id, incident_id,
                classification, executed_by,
                status, rollback_available, steps_json
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                execution_id,
                queue_id,
                incident_id,
                classification,
                executed_by,
                status,
                rollback_available,
                Json(steps),
            ),
        )


# ---------------------------------------------------------
# RESOLUTIONS  (Item #9 + #10)
# ---------------------------------------------------------

def ensure_resolutions_schema() -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS resolutions (
                resolution_id TEXT PRIMARY KEY,
                classification TEXT NOT NULL,
                root_cause TEXT,
                validated_checks JSONB DEFAULT '[]',
                validated_actions JSONB DEFAULT '[]',
                engineer_confirmed BOOLEAN DEFAULT FALSE,
                confirmed_by TEXT,
                success_notes TEXT,
                automation_ready BOOLEAN DEFAULT FALSE,
                linked_candidate_id TEXT,
                linked_playbook_id TEXT,
                version INT DEFAULT 1,
                content_hash TEXT,
                created_by TEXT,
                approved_by TEXT,
                approved_at TIMESTAMP,
                signature TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_resolutions_classification ON resolutions (classification)")


def _resolution_hash(rec: Dict[str, Any]) -> str:
    import hashlib, json as _json
    payload = _json.dumps({
        "classification": rec.get("classification"),
        "root_cause": rec.get("root_cause"),
        "validated_checks": rec.get("validated_checks"),
        "validated_actions": rec.get("validated_actions"),
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def upsert_resolution(record: Dict[str, Any]) -> None:
    ensure_resolutions_schema()
    rec = dict(record)
    rec.setdefault("resolution_id", f"res_{rec.get('classification','x')[:12]}")
    rec["content_hash"] = _resolution_hash(rec)
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO resolutions (
                resolution_id, classification, root_cause,
                validated_checks, validated_actions,
                engineer_confirmed, confirmed_by, success_notes,
                automation_ready, linked_candidate_id, linked_playbook_id,
                version, content_hash, created_by, approved_by, approved_at, signature,
                created_at, updated_at
            )
            VALUES (
                %(resolution_id)s, %(classification)s, %(root_cause)s,
                %(validated_checks)s, %(validated_actions)s,
                %(engineer_confirmed)s, %(confirmed_by)s, %(success_notes)s,
                %(automation_ready)s, %(linked_candidate_id)s, %(linked_playbook_id)s,
                %(version)s, %(content_hash)s, %(created_by)s, %(approved_by)s, %(approved_at)s, %(signature)s,
                %(created_at)s, NOW()
            )
            ON CONFLICT (resolution_id) DO UPDATE SET
                classification = EXCLUDED.classification,
                root_cause = EXCLUDED.root_cause,
                validated_checks = EXCLUDED.validated_checks,
                validated_actions = EXCLUDED.validated_actions,
                engineer_confirmed = EXCLUDED.engineer_confirmed,
                confirmed_by = EXCLUDED.confirmed_by,
                success_notes = EXCLUDED.success_notes,
                automation_ready = EXCLUDED.automation_ready,
                linked_candidate_id = EXCLUDED.linked_candidate_id,
                linked_playbook_id = EXCLUDED.linked_playbook_id,
                version = resolutions.version + 1,
                content_hash = EXCLUDED.content_hash,
                approved_by = COALESCE(EXCLUDED.approved_by, resolutions.approved_by),
                approved_at = COALESCE(EXCLUDED.approved_at, resolutions.approved_at),
                signature = COALESCE(EXCLUDED.signature, resolutions.signature),
                updated_at = NOW()
            """,
            {
                "resolution_id": rec.get("resolution_id"),
                "classification": rec.get("classification", "unknown"),
                "root_cause": rec.get("root_cause", ""),
                "validated_checks": Json(rec.get("validated_checks") or []),
                "validated_actions": Json(rec.get("validated_actions") or []),
                "engineer_confirmed": bool(rec.get("engineer_confirmed", False)),
                "confirmed_by": rec.get("confirmed_by", ""),
                "success_notes": rec.get("success_notes", ""),
                "automation_ready": bool(rec.get("automation_ready", False)),
                "linked_candidate_id": rec.get("linked_candidate_id", ""),
                "linked_playbook_id": rec.get("linked_playbook_id", ""),
                "version": rec.get("version", 1),
                "content_hash": rec["content_hash"],
                "created_by": rec.get("created_by") or rec.get("confirmed_by", ""),
                "approved_by": rec.get("approved_by"),
                "approved_at": rec.get("approved_at"),
                "signature": rec.get("signature"),
                "created_at": rec.get("created_at"),
            },
        )


def get_resolutions(limit: int = 200) -> List[Dict[str, Any]]:
    ensure_resolutions_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            "SELECT * FROM resolutions ORDER BY updated_at DESC NULLS LAST LIMIT %s",
            (_coerce_limit(limit, 1000),),
        )
        return list(cur.fetchall())


def find_resolution_by_classification_pg(classification: str) -> Optional[Dict[str, Any]]:
    ensure_resolutions_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            "SELECT * FROM resolutions WHERE LOWER(classification) = LOWER(%s) ORDER BY updated_at DESC NULLS LAST LIMIT 1",
            (classification,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------
# PLAYBOOKS  (Item #9 + #10)
# ---------------------------------------------------------

def ensure_playbooks_schema() -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS playbooks (
                playbook_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                classification TEXT NOT NULL,
                description TEXT,
                steps JSONB DEFAULT '[]',
                scripts JSONB DEFAULT '[]',
                approval_mode TEXT DEFAULT 'manual_approval_required',
                rollback_available BOOLEAN DEFAULT TRUE,
                risk_tier TEXT DEFAULT 'medium',
                enabled BOOLEAN DEFAULT TRUE,
                status TEXT DEFAULT 'draft',
                source TEXT DEFAULT 'manual',
                linked_incident_id TEXT,
                linked_rca_id TEXT,
                review JSONB DEFAULT '{}',
                version INT DEFAULT 1,
                content_hash TEXT,
                created_by TEXT,
                approved_by TEXT,
                approved_at TIMESTAMP,
                signature TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_playbooks_classification ON playbooks (classification)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks (status)")


def _playbook_hash(rec: Dict[str, Any]) -> str:
    import hashlib, json as _json
    payload = _json.dumps({
        "classification": rec.get("classification"),
        "steps": rec.get("steps"),
        "scripts": rec.get("scripts"),
        "risk_tier": rec.get("risk_tier"),
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def upsert_playbook(record: Dict[str, Any]) -> None:
    ensure_playbooks_schema()
    rec = dict(record)
    rec["content_hash"] = _playbook_hash(rec)
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO playbooks (
                playbook_id, name, classification, description,
                steps, scripts, approval_mode, rollback_available, risk_tier,
                enabled, status, source, linked_incident_id, linked_rca_id, review,
                version, content_hash, created_by, approved_by, approved_at, signature,
                created_at, updated_at
            )
            VALUES (
                %(playbook_id)s, %(name)s, %(classification)s, %(description)s,
                %(steps)s, %(scripts)s, %(approval_mode)s, %(rollback_available)s, %(risk_tier)s,
                %(enabled)s, %(status)s, %(source)s, %(linked_incident_id)s, %(linked_rca_id)s, %(review)s,
                %(version)s, %(content_hash)s, %(created_by)s, %(approved_by)s, %(approved_at)s, %(signature)s,
                %(created_at)s, NOW()
            )
            ON CONFLICT (playbook_id) DO UPDATE SET
                name = EXCLUDED.name,
                classification = EXCLUDED.classification,
                description = EXCLUDED.description,
                steps = EXCLUDED.steps,
                scripts = EXCLUDED.scripts,
                approval_mode = EXCLUDED.approval_mode,
                rollback_available = EXCLUDED.rollback_available,
                risk_tier = EXCLUDED.risk_tier,
                enabled = EXCLUDED.enabled,
                status = EXCLUDED.status,
                source = EXCLUDED.source,
                review = EXCLUDED.review,
                version = playbooks.version + 1,
                content_hash = EXCLUDED.content_hash,
                approved_by = COALESCE(EXCLUDED.approved_by, playbooks.approved_by),
                approved_at = COALESCE(EXCLUDED.approved_at, playbooks.approved_at),
                signature = COALESCE(EXCLUDED.signature, playbooks.signature),
                updated_at = NOW()
            """,
            {
                "playbook_id": rec.get("playbook_id"),
                "name": rec.get("name", ""),
                "classification": rec.get("classification", "unknown"),
                "description": rec.get("description", ""),
                "steps": Json(rec.get("steps") or []),
                "scripts": Json(rec.get("scripts") or []),
                "approval_mode": rec.get("approval_mode", "manual_approval_required"),
                "rollback_available": bool(rec.get("rollback_available", True)),
                "risk_tier": rec.get("risk_tier", "medium"),
                "enabled": bool(rec.get("enabled", True)),
                "status": rec.get("status", "draft"),
                "source": rec.get("source", "manual"),
                "linked_incident_id": rec.get("linked_incident_id", ""),
                "linked_rca_id": rec.get("linked_rca_id", ""),
                "review": Json(rec.get("review") or {}),
                "version": rec.get("version", 1),
                "content_hash": rec["content_hash"],
                "created_by": rec.get("created_by", ""),
                "approved_by": rec.get("approved_by"),
                "approved_at": rec.get("approved_at"),
                "signature": rec.get("signature"),
                "created_at": rec.get("created_at"),
            },
        )


def get_playbooks(limit: int = 200, status: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_playbooks_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        if status:
            cur.execute(
                "SELECT * FROM playbooks WHERE status = %s ORDER BY updated_at DESC NULLS LAST LIMIT %s",
                (status, _coerce_limit(limit, 1000)),
            )
        else:
            cur.execute(
                "SELECT * FROM playbooks ORDER BY updated_at DESC NULLS LAST LIMIT %s",
                (_coerce_limit(limit, 1000),),
            )
        return list(cur.fetchall())


def find_playbook_by_classification_pg(classification: str) -> Optional[Dict[str, Any]]:
    ensure_playbooks_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            "SELECT * FROM playbooks WHERE LOWER(classification) = LOWER(%s) ORDER BY updated_at DESC NULLS LAST LIMIT 1",
            (classification,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------
# KNOWLEDGE CANDIDATES  (Item #9)
# ---------------------------------------------------------

def ensure_candidates_schema() -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_candidates (
                candidate_id TEXT PRIMARY KEY,
                ticket TEXT,
                domain TEXT,
                classification TEXT NOT NULL,
                probable_causes JSONB DEFAULT '[]',
                next_checks JSONB DEFAULT '[]',
                recommended_actions JSONB DEFAULT '[]',
                confidence NUMERIC,
                fallback_provider TEXT,
                fallback_model TEXT,
                retrieved_incidents JSONB DEFAULT '[]',
                tenant_id TEXT DEFAULT 'client_a',
                knowledge_scope TEXT DEFAULT 'client_private',
                status TEXT DEFAULT 'candidate',
                source TEXT,
                source_layer TEXT,
                promotion_target TEXT,
                promoted_by TEXT,
                promoted_at TIMESTAMP,
                engineer_review JSONB DEFAULT '{}',
                grounding_incident_count INT DEFAULT 0,
                draft_runbook JSONB DEFAULT '{}',
                draft_steps JSONB DEFAULT '[]',
                risk_tier TEXT DEFAULT 'medium',
                approval_required BOOLEAN DEFAULT TRUE,
                linked_resolution JSONB DEFAULT '{}',
                linked_playbook JSONB DEFAULT '{}',
                drift_type TEXT,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_kc_classification ON knowledge_candidates (classification)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_kc_status ON knowledge_candidates (status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_kc_tenant ON knowledge_candidates (tenant_id)")


def upsert_knowledge_candidate(record: Dict[str, Any]) -> None:
    ensure_candidates_schema()
    rec = dict(record)
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO knowledge_candidates (
                candidate_id, ticket, domain, classification,
                probable_causes, next_checks, recommended_actions,
                confidence, fallback_provider, fallback_model, retrieved_incidents,
                tenant_id, knowledge_scope, status, source, source_layer,
                promotion_target, promoted_by, promoted_at, engineer_review,
                grounding_incident_count, draft_runbook, draft_steps, risk_tier,
                approval_required, linked_resolution, linked_playbook, drift_type, created_by,
                created_at, updated_at
            )
            VALUES (
                %(candidate_id)s, %(ticket)s, %(domain)s, %(classification)s,
                %(probable_causes)s, %(next_checks)s, %(recommended_actions)s,
                %(confidence)s, %(fallback_provider)s, %(fallback_model)s, %(retrieved_incidents)s,
                %(tenant_id)s, %(knowledge_scope)s, %(status)s, %(source)s, %(source_layer)s,
                %(promotion_target)s, %(promoted_by)s, %(promoted_at)s, %(engineer_review)s,
                %(grounding_incident_count)s, %(draft_runbook)s, %(draft_steps)s, %(risk_tier)s,
                %(approval_required)s, %(linked_resolution)s, %(linked_playbook)s, %(drift_type)s, %(created_by)s,
                %(created_at)s, NOW()
            )
            ON CONFLICT (candidate_id) DO UPDATE SET
                status = EXCLUDED.status,
                classification = EXCLUDED.classification,
                probable_causes = EXCLUDED.probable_causes,
                next_checks = EXCLUDED.next_checks,
                recommended_actions = EXCLUDED.recommended_actions,
                engineer_review = EXCLUDED.engineer_review,
                promotion_target = EXCLUDED.promotion_target,
                promoted_by = EXCLUDED.promoted_by,
                promoted_at = EXCLUDED.promoted_at,
                draft_runbook = EXCLUDED.draft_runbook,
                draft_steps = EXCLUDED.draft_steps,
                risk_tier = EXCLUDED.risk_tier,
                linked_resolution = EXCLUDED.linked_resolution,
                linked_playbook = EXCLUDED.linked_playbook,
                drift_type = EXCLUDED.drift_type,
                updated_at = NOW()
            """,
            {
                "candidate_id": rec.get("candidate_id"),
                "ticket": rec.get("ticket", ""),
                "domain": rec.get("domain", "general"),
                "classification": rec.get("classification", "unknown"),
                "probable_causes": Json(rec.get("probable_causes") or []),
                "next_checks": Json(rec.get("next_checks") or []),
                "recommended_actions": Json(rec.get("recommended_actions") or []),
                "confidence": rec.get("confidence"),
                "fallback_provider": rec.get("fallback_provider", ""),
                "fallback_model": rec.get("fallback_model", ""),
                "retrieved_incidents": Json(rec.get("retrieved_incidents") or []),
                "tenant_id": rec.get("tenant_id", "client_a"),
                "knowledge_scope": rec.get("knowledge_scope", "client_private"),
                "status": rec.get("status", "candidate"),
                "source": rec.get("source", ""),
                "source_layer": rec.get("source_layer", ""),
                "promotion_target": rec.get("promotion_target", ""),
                "promoted_by": rec.get("promoted_by", ""),
                "promoted_at": rec.get("promoted_at") or None,
                "engineer_review": Json(rec.get("engineer_review") or {}),
                "grounding_incident_count": int(rec.get("grounding_incident_count") or 0),
                "draft_runbook": Json(rec.get("draft_runbook") or {}),
                "draft_steps": Json(rec.get("draft_steps") or []),
                "risk_tier": rec.get("risk_tier", "medium"),
                "approval_required": bool(rec.get("approval_required", True)),
                "linked_resolution": Json(rec.get("linked_resolution") or {}),
                "linked_playbook": Json(rec.get("linked_playbook") or {}),
                "drift_type": rec.get("drift_type"),
                "created_by": rec.get("created_by") or rec.get("created_by", ""),
                "created_at": rec.get("created_at"),
            },
        )


def get_knowledge_candidates(status: Optional[str] = None, tenant_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    ensure_candidates_schema()
    clauses, params = [], []
    if status:
        clauses.append("status = %s"); params.append(status)
    if tenant_id:
        clauses.append("tenant_id = %s"); params.append(tenant_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(_coerce_limit(limit, 1000))
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(f"SELECT * FROM knowledge_candidates {where} ORDER BY updated_at DESC NULLS LAST LIMIT %s", params)
        return list(cur.fetchall())


def get_knowledge_candidate_by_id_pg(candidate_id: str) -> Optional[Dict[str, Any]]:
    ensure_candidates_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute("SELECT * FROM knowledge_candidates WHERE candidate_id = %s LIMIT 1", (candidate_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------
# GENERATED PLAYBOOKS — PlaybookAgent cache  (Item #9 + #10)
# ---------------------------------------------------------

def ensure_generated_playbooks_schema() -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_playbooks (
                tenant_id TEXT NOT NULL DEFAULT 'default',
                classification TEXT NOT NULL,
                steps JSONB NOT NULL DEFAULT '[]',
                version INT DEFAULT 1,
                content_hash TEXT,
                created_by TEXT DEFAULT 'playbook_agent',
                model_used TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, classification)
            )
            """
        )
        # Migration guard: handles three upgrade cases on existing DBs:
        # Case 1 — no tenant_id column at all: add it, rebuild PK
        # Case 2 — tenant_id exists but PK is still classification-only: rebuild PK
        # Case 3 — composite PK already correct: no-op
        cur.execute("""
            DO $$
            DECLARE
                _has_tenant   BOOLEAN;
                _has_comp_pk  BOOLEAN;
            BEGIN
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='generated_playbooks' AND column_name='tenant_id'
                ) INTO _has_tenant;

                IF NOT _has_tenant THEN
                    ALTER TABLE generated_playbooks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
                END IF;

                -- Check whether the composite PK (tenant_id, classification) already exists
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_attribute a1 ON a1.attrelid = c.conrelid AND a1.attnum = ANY(c.conkey)
                    JOIN pg_attribute a2 ON a2.attrelid = c.conrelid AND a2.attnum = ANY(c.conkey)
                    WHERE c.contype = 'p'
                      AND c.conrelid = 'generated_playbooks'::regclass
                      AND a1.attname = 'tenant_id'
                      AND a2.attname = 'classification'
                ) INTO _has_comp_pk;

                IF NOT _has_comp_pk THEN
                    ALTER TABLE generated_playbooks DROP CONSTRAINT IF EXISTS generated_playbooks_pkey;
                    ALTER TABLE generated_playbooks ADD PRIMARY KEY (tenant_id, classification);
                END IF;
            END $$;
        """)


def get_generated_playbook_pg(classification: str, tenant_id: str = "default") -> Optional[List[Dict[str, Any]]]:
    ensure_generated_playbooks_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute(
            "SELECT steps FROM generated_playbooks WHERE tenant_id = %s AND classification = %s LIMIT 1",
            (tenant_id, classification),
        )
        row = cur.fetchone()
        if row and row.get("steps"):
            steps = row["steps"]
            return steps if isinstance(steps, list) else []
        return None


def upsert_generated_playbook(classification: str, steps: List[Dict[str, Any]], model_used: str = "", tenant_id: str = "default") -> None:
    import hashlib, json as _json
    ensure_generated_playbooks_schema()
    content_hash = hashlib.sha256(_json.dumps(steps, sort_keys=True, default=str).encode()).hexdigest()[:32]
    with db_cursor(commit=True) as (_, cur):
        cur.execute(
            """
            INSERT INTO generated_playbooks (tenant_id, classification, steps, version, content_hash, model_used, created_at, updated_at)
            VALUES (%s, %s, %s, 1, %s, %s, NOW(), NOW())
            ON CONFLICT (tenant_id, classification) DO UPDATE SET
                steps = EXCLUDED.steps,
                version = generated_playbooks.version + 1,
                content_hash = EXCLUDED.content_hash,
                model_used = EXCLUDED.model_used,
                updated_at = NOW()
            """,
            (tenant_id, classification, Json(steps), content_hash, model_used or ""),
        )


# RBAC  (Item #16)
ROLES = ("l1", "l2", "l3", "admin")


def ensure_rbac_schema() -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT,
                email TEXT,
                role TEXT NOT NULL DEFAULT 'l1',
                tenant_id TEXT NOT NULL DEFAULT 'client_a',
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users (role)")
        # seed a default admin if none exists
        cur.execute("""
            INSERT INTO users (user_id, username, display_name, email, role, tenant_id)
            VALUES ('admin_001', 'vijay', 'Vijay', 'vijayakumaar.r@hcltech.com', 'admin', 'client_c')
            ON CONFLICT (user_id) DO NOTHING
        """)


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    ensure_rbac_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute("SELECT * FROM users WHERE username = %s AND is_active = TRUE LIMIT 1", (username,))
        row = cur.fetchone()
        return dict(row) if row else None


def upsert_user(user_id: str, username: str, display_name: str = "", email: str = "",
                role: str = "l1", tenant_id: str = "client_a") -> Dict[str, Any]:
    ensure_rbac_schema()
    role = role if role in ROLES else "l1"
    with db_cursor(commit=True, dict_rows=True) as (_, cur):
        cur.execute("""
            INSERT INTO users (user_id, username, display_name, email, role, tenant_id, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                display_name=EXCLUDED.display_name, email=EXCLUDED.email,
                role=EXCLUDED.role, tenant_id=EXCLUDED.tenant_id, updated_at=NOW()
            RETURNING *
        """, (user_id, username, display_name, email, role, tenant_id))
        return dict(cur.fetchone())


def list_users(tenant_id: str = None) -> List[Dict[str, Any]]:
    ensure_rbac_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        if tenant_id:
            cur.execute("SELECT * FROM users WHERE tenant_id=%s ORDER BY role,username", (tenant_id,))
        else:
            cur.execute("SELECT * FROM users ORDER BY tenant_id,role,username")
        return list(cur.fetchall())


# AGENT TRACES  (Item #12)
def ensure_agent_traces_schema() -> None:
    with db_cursor(commit=True) as (_, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_traces (
                id SERIAL PRIMARY KEY,
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL,
                parent_span_id TEXT,
                agent_name TEXT NOT NULL,
                operation TEXT,
                status TEXT DEFAULT 'started',
                input_summary TEXT,
                output_summary TEXT,
                latency_ms INT,
                error TEXT,
                metadata JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_traces_trace_id ON agent_traces (trace_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_traces_created ON agent_traces (created_at)")

def insert_agent_trace(trace_id: str, agent_name: str, operation: str = "",
                        span_id: str = "", parent_span_id: str = "",
                        status: str = "started", input_summary: str = "",
                        output_summary: str = "", latency_ms: int = None,
                        error: str = "", metadata: dict = None) -> None:
    ensure_agent_traces_schema()
    import uuid as _uuid
    sid = span_id or _uuid.uuid4().hex[:16]
    with db_cursor(commit=True) as (_, cur):
        cur.execute("""
            INSERT INTO agent_traces (trace_id, span_id, parent_span_id, agent_name, operation,
                status, input_summary, output_summary, latency_ms, error, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (trace_id, sid, parent_span_id or None, agent_name, operation,
              status, input_summary[:500] if input_summary else "",
              output_summary[:500] if output_summary else "",
              latency_ms, error[:500] if error else "", Json(metadata or {})))

def get_agent_traces(trace_id: str, limit: int = 50) -> list:
    ensure_agent_traces_schema()
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute("""
            SELECT * FROM agent_traces WHERE trace_id = %s ORDER BY created_at ASC LIMIT %s
        """, (trace_id, limit))
        return list(cur.fetchall())


def ensure_version_history_schema() -> None:
    """
    Create immutable version-history tables for resolutions and playbooks (Item #10).
    Every upsert appends a snapshot row — rows are never updated or deleted.
    """
    with db_cursor(commit=True) as (_, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS resolution_versions (
                id SERIAL PRIMARY KEY,
                resolution_id TEXT NOT NULL,
                version INT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                classification TEXT,
                root_cause TEXT,
                validated_checks JSONB DEFAULT '[]',
                validated_actions JSONB DEFAULT '[]',
                automation_ready BOOLEAN,
                content_hash TEXT,
                changed_by TEXT,
                change_reason TEXT,
                snapshot JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_rv_resolution_id ON resolution_versions (resolution_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_rv_tenant ON resolution_versions (tenant_id, resolution_id)"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS playbook_versions (
                id SERIAL PRIMARY KEY,
                playbook_id TEXT NOT NULL,
                version INT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                classification TEXT,
                content_hash TEXT,
                changed_by TEXT,
                change_reason TEXT,
                snapshot JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_playbook_id ON playbook_versions (playbook_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_tenant ON playbook_versions (tenant_id, playbook_id)"
        )


def append_resolution_version(resolution: dict, changed_by: str = "", change_reason: str = "") -> None:
    """Append an immutable snapshot to resolution_versions. Call after every upsert."""
    try:
        with db_cursor(commit=True) as (_, cur):
            cur.execute("""
                INSERT INTO resolution_versions
                    (resolution_id, version, tenant_id, classification, root_cause,
                     validated_checks, validated_actions, automation_ready,
                     content_hash, changed_by, change_reason, snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                resolution.get("resolution_id") or resolution.get("id", ""),
                int(resolution.get("version") or 1),
                resolution.get("tenant_id", "default"),
                resolution.get("classification", ""),
                resolution.get("root_cause", ""),
                Json(resolution.get("validated_checks") or []),
                Json(resolution.get("validated_actions") or []),
                bool(resolution.get("automation_ready")),
                resolution.get("content_hash", ""),
                changed_by,
                change_reason,
                Json(resolution),
            ))
    except Exception as exc:
        LOGGER.warning("append_resolution_version failed: %s", exc)


def append_playbook_version(playbook: dict, changed_by: str = "", change_reason: str = "") -> None:
    """Append an immutable snapshot to playbook_versions. Call after every upsert."""
    try:
        with db_cursor(commit=True) as (_, cur):
            cur.execute("""
                INSERT INTO playbook_versions
                    (playbook_id, version, tenant_id, classification,
                     content_hash, changed_by, change_reason, snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                playbook.get("playbook_id") or playbook.get("id", ""),
                int(playbook.get("version") or 1),
                playbook.get("tenant_id", "default"),
                playbook.get("classification", ""),
                playbook.get("content_hash", ""),
                changed_by,
                change_reason,
                Json(playbook),
            ))
    except Exception as exc:
        LOGGER.warning("append_playbook_version failed: %s", exc)


def get_resolution_versions(resolution_id: str) -> list:
    """Return full version history for a resolution, oldest first."""
    try:
        with db_cursor(dict_rows=True) as (_, cur):
            cur.execute("""
                SELECT id, version, changed_by, change_reason, content_hash, created_at
                FROM resolution_versions WHERE resolution_id = %s ORDER BY version ASC
            """, (resolution_id,))
            return list(cur.fetchall())
    except Exception:
        return []


def get_playbook_versions(playbook_id: str) -> list:
    """Return full version history for a playbook, oldest first."""
    try:
        with db_cursor(dict_rows=True) as (_, cur):
            cur.execute("""
                SELECT id, version, changed_by, change_reason, content_hash, created_at
                FROM playbook_versions WHERE playbook_id = %s ORDER BY version ASC
            """, (playbook_id,))
            return list(cur.fetchall())
    except Exception:
        return []


def ensure_tenant_isolation() -> None:
    """
    Idempotent migration: add tenant_id column + PostgreSQL RLS policies to all core tables.
    Safe to run on an existing database — uses ADD COLUMN IF NOT EXISTS and CREATE POLICY IF NOT EXISTS.
    Call once at application startup after all ensure_*_schema() calls.

    RLS policy: each session must SET app.current_tenant = '<tenant_id>' before querying.
    The DB role aiops_user sees only rows matching that setting.
    """
    _TABLES = [
        "resolutions",
        "playbooks",
        "knowledge_candidates",
        "generated_playbooks",
        "classification_outcomes",
        "incident_episodes",
        "agent_traces",
        "users",
    ]
    with db_cursor(commit=True) as (_, cur):
        for table in _TABLES:
            # 1. Add tenant_id column
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'"
            )
            # 2. Column index
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table} (tenant_id)"
            )
            # 3. Enable RLS on the table
            cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            # 4. DROP and re-create the policy (CREATE POLICY has no IF NOT EXISTS before PG 17)
            cur.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            cur.execute(
                f"""CREATE POLICY tenant_isolation ON {table}
                    USING (
                        tenant_id = current_setting('app.current_tenant', true)
                        OR current_setting('app.current_tenant', true) IS NULL
                        OR current_setting('app.current_tenant', true) = 'default'
                    )"""
            )
            # 5. Allow the app user to bypass RLS for admin operations
            cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        # Composite indexes for the most common lookup pattern: tenant + classification
        for table in ("resolutions", "playbooks", "knowledge_candidates",
                      "classification_outcomes", "incident_episodes"):
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant_cls "
                f"ON {table} (tenant_id, classification)"
            )


# ---------------------------------------------------------
# RESOLUTION OUTCOMES  (append-only outcome capture ledger)
# ---------------------------------------------------------

def ensure_resolution_outcomes_schema() -> None:
    """Idempotent: creates the append-only resolution_outcomes table."""
    with db_cursor(commit=True) as (_, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS resolution_outcomes (
                outcome_id            TEXT PRIMARY KEY,
                incident_id           TEXT NOT NULL,
                classification        TEXT NOT NULL DEFAULT '',
                resolution_id         TEXT NOT NULL DEFAULT '',
                playbook_id           TEXT NOT NULL DEFAULT '',
                tenant_id             TEXT NOT NULL DEFAULT 'default',
                execution_mode        TEXT NOT NULL DEFAULT 'manual',
                ai_recommendation_used TEXT NOT NULL DEFAULT 'no',
                outcome_status        TEXT NOT NULL DEFAULT 'resolved',
                opened_at             TIMESTAMPTZ,
                resolved_at           TIMESTAMPTZ,
                resolution_minutes    DOUBLE PRECISION,
                recurrence_window_hours INTEGER NOT NULL DEFAULT 72,
                recurrence_detected   BOOLEAN NOT NULL DEFAULT FALSE,
                confirmed_by          TEXT NOT NULL DEFAULT '',
                confirmed_at          TIMESTAMPTZ,
                notes                 TEXT NOT NULL DEFAULT '',
                created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_ro_incident   ON resolution_outcomes (incident_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_ro_cls_tenant ON resolution_outcomes (classification, tenant_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_ro_created    ON resolution_outcomes (created_at DESC)"
        )


def insert_resolution_outcome(outcome: Dict[str, Any]) -> None:
    """Append one resolution outcome record. outcome must contain outcome_id and incident_id."""
    required = {"outcome_id", "incident_id"}
    missing = required - outcome.keys()
    if missing:
        raise ValueError(f"insert_resolution_outcome: missing required fields {missing}")

    with db_cursor(commit=True) as (_, cur):
        cur.execute("""
            INSERT INTO resolution_outcomes (
                outcome_id, incident_id, classification, resolution_id, playbook_id,
                tenant_id, execution_mode, ai_recommendation_used, outcome_status,
                opened_at, resolved_at, resolution_minutes,
                recurrence_window_hours, recurrence_detected,
                confirmed_by, confirmed_at, notes
            ) VALUES (
                %(outcome_id)s, %(incident_id)s, %(classification)s, %(resolution_id)s,
                %(playbook_id)s, %(tenant_id)s, %(execution_mode)s,
                %(ai_recommendation_used)s, %(outcome_status)s,
                %(opened_at)s, %(resolved_at)s, %(resolution_minutes)s,
                %(recurrence_window_hours)s, %(recurrence_detected)s,
                %(confirmed_by)s, %(confirmed_at)s, %(notes)s
            )
            ON CONFLICT (outcome_id) DO NOTHING
        """, {
            "outcome_id":              outcome["outcome_id"],
            "incident_id":             outcome["incident_id"],
            "classification":          outcome.get("classification", ""),
            "resolution_id":           outcome.get("resolution_id", ""),
            "playbook_id":             outcome.get("playbook_id", ""),
            "tenant_id":               outcome.get("tenant_id", "default"),
            "execution_mode":          outcome.get("execution_mode", "manual"),
            "ai_recommendation_used":  outcome.get("ai_recommendation_used", "no"),
            "outcome_status":          outcome.get("outcome_status", "resolved"),
            "opened_at":               outcome.get("opened_at"),
            "resolved_at":             outcome.get("resolved_at"),
            "resolution_minutes":      outcome.get("resolution_minutes"),
            "recurrence_window_hours": outcome.get("recurrence_window_hours", 72),
            "recurrence_detected":     outcome.get("recurrence_detected", False),
            "confirmed_by":            outcome.get("confirmed_by", ""),
            "confirmed_at":            outcome.get("confirmed_at"),
            "notes":                   outcome.get("notes", ""),
        })


def get_outcomes_by_classification(
    classification: str,
    tenant_id: str = "default",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return recent outcomes for a classification, newest first."""
    with db_cursor(dict_rows=True) as (_, cur):
        cur.execute("""
            SELECT * FROM resolution_outcomes
            WHERE classification = %s
              AND (tenant_id = %s OR tenant_id = 'default')
            ORDER BY created_at DESC
            LIMIT %s
        """, (classification, tenant_id, limit))
        return [dict(r) for r in cur.fetchall()]


if __name__ == "__main__":
    print("DB config:", get_db_config())
    print("DB reachable:", ping())
    if ping():
        print("Recent incidents:", len(get_incidents(5)))
