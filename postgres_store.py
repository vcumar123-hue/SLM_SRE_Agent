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


if __name__ == "__main__":
    print("DB config:", get_db_config())
    print("DB reachable:", ping())
    if ping():
        print("Recent incidents:", len(get_incidents(5)))
