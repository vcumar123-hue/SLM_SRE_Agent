-- AI Ops initial PostgreSQL schema
-- Safe to run multiple times where possible.

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    classification TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    confidence DOUBLE PRECISION,
    linked_candidate_id TEXT,
    service_scope TEXT,
    environment TEXT,
    severity TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_incidents_domain ON incidents(domain);
CREATE INDEX IF NOT EXISTS idx_incidents_classification ON incidents(classification);
CREATE INDEX IF NOT EXISTS idx_incidents_service_scope ON incidents(service_scope);
CREATE INDEX IF NOT EXISTS idx_incidents_updated_at ON incidents(updated_at);

CREATE TABLE IF NOT EXISTS feedback_events (
    id BIGSERIAL PRIMARY KEY,
    request_id TEXT,
    ticket TEXT NOT NULL,
    original_domain TEXT,
    original_classification TEXT,
    corrected_classification TEXT NOT NULL,
    submitted_domain TEXT,
    stored_domain TEXT,
    is_correct BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feedback_events_request_id ON feedback_events(request_id);
CREATE INDEX IF NOT EXISTS idx_feedback_events_created_at ON feedback_events(created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_events_corrected_classification ON feedback_events(corrected_classification);

CREATE TABLE IF NOT EXISTS feedback_learning_weights (
    key TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    classification TEXT NOT NULL,
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    effective_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    positive_count INTEGER NOT NULL DEFAULT 0,
    negative_count INTEGER NOT NULL DEFAULT 0,
    correction_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    last_ticket TEXT,
    last_predicted_as TEXT,
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_feedback_learning_domain ON feedback_learning_weights(domain);
CREATE INDEX IF NOT EXISTS idx_feedback_learning_classification ON feedback_learning_weights(classification);

CREATE TABLE IF NOT EXISTS telemetry_events (
    event_id TEXT PRIMARY KEY,
    metric TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    resource TEXT,
    service TEXT,
    environment TEXT,
    source TEXT,
    labels_json JSONB,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_events_metric ON telemetry_events(metric);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_resource ON telemetry_events(resource);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_timestamp ON telemetry_events(timestamp);

CREATE TABLE IF NOT EXISTS telemetry_incidents (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    classification TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    resource TEXT,
    environment TEXT,
    severity TEXT,
    trigger_metric TEXT,
    trigger_value DOUBLE PRECISION,
    telemetry_rule_id TEXT,
    source TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_incidents_classification ON telemetry_incidents(classification);
CREATE INDEX IF NOT EXISTS idx_telemetry_incidents_resource ON telemetry_incidents(resource);
CREATE INDEX IF NOT EXISTS idx_telemetry_incidents_created_at ON telemetry_incidents(created_at);

CREATE TABLE IF NOT EXISTS automation_queue (
    queue_id TEXT PRIMARY KEY,
    incident_id TEXT,
    classification TEXT NOT NULL,
    requested_by TEXT,
    status TEXT NOT NULL,
    plan_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    execution_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_automation_queue_incident_id ON automation_queue(incident_id);
CREATE INDEX IF NOT EXISTS idx_automation_queue_status ON automation_queue(status);
CREATE INDEX IF NOT EXISTS idx_automation_queue_created_at ON automation_queue(created_at);

CREATE TABLE IF NOT EXISTS automation_executions (
    execution_id TEXT PRIMARY KEY,
    queue_id TEXT,
    incident_id TEXT,
    classification TEXT NOT NULL,
    executed_by TEXT,
    status TEXT NOT NULL,
    rollback_available BOOLEAN DEFAULT FALSE,
    steps_json JSONB NOT NULL,
    executed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_automation_executions_queue_id ON automation_executions(queue_id);
CREATE INDEX IF NOT EXISTS idx_automation_executions_incident_id ON automation_executions(incident_id);
CREATE INDEX IF NOT EXISTS idx_automation_executions_executed_at ON automation_executions(executed_at);
