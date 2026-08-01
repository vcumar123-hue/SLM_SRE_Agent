"""
POC Scenario Definitions
10 curated incidents spanning infra, end-user, data-engineering, and application domains.
Each scenario drives both reactive (ticket → RCA) and/or proactive (telemetry → incident) paths.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

DetectionMode = Literal["reactive", "proactive", "both"]
Category = Literal["infra", "end_user", "data_engineering", "application"]


@dataclass
class TelemetryEvent:
    metric: str
    host: str
    unit: str
    baseline: float       # normal operating value
    escalation_steps: list[float]  # values injected in sequence to build toward breach
    threshold: float      # value that triggers an alert rule
    tags: dict = field(default_factory=dict)


@dataclass
class Scenario:
    id: int
    title: str
    category: Category
    detection_mode: DetectionMode
    ci_target: str                # primary lab container
    classification: str           # expected RCA classification key
    ticket_text: str              # realistic ITSM/Jira ticket body
    expected_resolution_summary: str
    telemetry_events: list[TelemetryEvent] = field(default_factory=list)
    ansible_playbook: str = ""
    jira_labels: list[str] = field(default_factory=list)
    priority: str = "P2"


# ---------------------------------------------------------------------------
# INFRA SCENARIOS (3)
# ---------------------------------------------------------------------------

S01 = Scenario(
    id=1,
    title="CPU Exhaustion — Application Server",
    category="infra",
    detection_mode="both",
    ci_target="linux-lab-01",
    classification="high_cpu_utilisation",
    priority="P1",
    jira_labels=["infra", "performance", "cpu"],
    ticket_text="""
Summary: Application response times degraded — CPU at 98% on prod-app-01

Description:
Starting at approximately 14:32 UTC, end users began reporting significant slowness
across the customer-facing portal. Monitoring alerts fired at 14:35 UTC showing CPU
utilisation at 98% on prod-app-01 (linux-lab-01).

Impact:
- Portal response time degraded from avg 250ms to 12,000ms
- 3 background batch jobs have stalled
- On-call team paged via PagerDuty (INC0012847)

Initial investigation:
- `top` shows java process (PID 3821) consuming 94% CPU
- No recent deployments in the last 6 hours
- Memory usage is nominal at 62%
- No infrastructure changes in the change log

Environment: Production
Affected service: customer-portal, batch-processor
Host: linux-lab-01 (prod-app-01)
Assignee: Platform Engineering
""".strip(),
    expected_resolution_summary=(
        "Identify runaway JVM process; capture thread dump; restart application service; "
        "verify response time recovers; tune JVM GC settings; set CPU alert at 85%."
    ),
    telemetry_events=[
        TelemetryEvent(
            metric="cpu_usage_percent",
            host="linux-lab-01",
            unit="percent",
            baseline=35.0,
            escalation_steps=[45.0, 58.0, 71.0, 82.0, 91.0, 98.0],
            threshold=85.0,
            tags={"service": "customer-portal", "env": "prod"},
        )
    ],
    ansible_playbook="restart_app_service.yml",
)

S02 = Scenario(
    id=2,
    title="Disk Space Critical — Data Node",
    category="infra",
    detection_mode="reactive",
    ci_target="linux-lab-02",
    classification="disk_space_exhaustion",
    priority="P1",
    jira_labels=["infra", "disk", "storage"],
    ticket_text="""
Summary: Disk at 96% on linux-lab-02 — log writes failing, services degraded

Description:
The data node linux-lab-02 has reached 96% disk utilisation on the /var partition.
Application services are logging write errors and two microservices have entered a
degraded state due to inability to write to their local spool directories.

Timeline:
- 09:15 UTC — Disk usage crossed 90% (warning threshold)
- 09:48 UTC — Services began logging IOError: No space left on device
- 09:52 UTC — linux-lab-02 removed from load balancer rotation by health check
- 10:01 UTC — Ticket raised (INC0013204)

Root cause hypothesis:
- Unrotated application logs under /var/log/app/ (last rotation: 14 days ago)
- Temporary ETL staging files not cleaned up after last week's data migration
- /var partition size: 50 GB; current usage: 48.1 GB

Affected services: order-processor, audit-logger
Environment: Production
Assignee: SRE Team
""".strip(),
    expected_resolution_summary=(
        "Rotate and compress application logs; remove stale ETL temp files; "
        "re-add node to load balancer; schedule automated log rotation; expand /var if needed."
    ),
    ansible_playbook="cleanup_disk_space.yml",
)

S03 = Scenario(
    id=3,
    title="Network Latency Degradation — North-South Gateway",
    category="infra",
    detection_mode="proactive",
    ci_target="network-lab-01",
    classification="network_latency_degradation",
    priority="P2",
    jira_labels=["infra", "network", "latency"],
    ticket_text="""
Summary: Elevated RTT on north-south gateway — SLO at risk

Description:
Telemetry-driven incident. Network round-trip time on network-lab-01 has been trending
upward over the past 90 minutes. Current RTT: 218ms (SLO threshold: 150ms).
No user-reported incidents yet, but SLO burn rate is 4.2× — breach projected in 35 minutes.

Metrics at time of promotion:
- RTT p95: 218ms (baseline: 42ms)
- Packet loss: 0.4%
- BGP session: stable
- Interface utilisation: 67% (not saturated)

Hypothesis: Misconfigured QoS policy pushed in yesterday's change window may be
de-prioritising inter-service traffic.

Assignee: Network Engineering
Auto-created by AIOps telemetry engine.
""".strip(),
    expected_resolution_summary=(
        "Review QoS policy diff from last change window; roll back affected ACL; "
        "verify RTT returns to baseline; update change freeze policy for QoS rules."
    ),
    telemetry_events=[
        TelemetryEvent(
            metric="network_latency_ms",
            host="network-lab-01",
            unit="ms",
            baseline=42.0,
            escalation_steps=[65.0, 95.0, 130.0, 160.0, 195.0, 218.0],
            threshold=150.0,
            tags={"interface": "eth0", "direction": "north-south"},
        ),
        TelemetryEvent(
            metric="packet_loss_percent",
            host="network-lab-01",
            unit="percent",
            baseline=0.0,
            escalation_steps=[0.05, 0.1, 0.2, 0.35, 0.4],
            threshold=0.3,
            tags={"interface": "eth0"},
        ),
    ],
    ansible_playbook="rollback_qos_policy.yml",
)


# ---------------------------------------------------------------------------
# END-USER SCENARIOS (2)
# ---------------------------------------------------------------------------

S04 = Scenario(
    id=4,
    title="VPN Authentication Timeout — RADIUS Failure",
    category="end_user",
    detection_mode="reactive",
    ci_target="linux-lab-01",
    classification="vpn_authentication_failure",
    priority="P2",
    jira_labels=["end-user", "vpn", "auth", "remote-access"],
    ticket_text="""
Summary: Multiple users unable to connect VPN — authentication timeout after MFA

Description:
Service Desk has received 47 calls in the past 2 hours from remote employees unable to
connect to the corporate VPN. Users complete MFA successfully but then receive
"Authentication timeout" at the VPN client stage.

User-reported pattern:
- MFA push notification accepted ✓
- VPN client shows "Connecting..." for ~30 seconds
- Error: "Authentication timeout. Please try again." (Error Code: 691)
- Affects all three VPN concentrators (vpn-us-east-1, vpn-eu-west-1, vpn-ap-southeast-1)
- Started approximately 11:30 UTC

Initial checks:
- RADIUS server (linux-lab-01) load: 89% CPU (normally 12%)
- Authentication log shows "request queue full" errors beginning at 11:28 UTC
- Last config change: RADIUS shared secret rotated at 11:15 UTC for compliance audit

Workaround tested: None currently effective
Affected users: ~340 remote employees (entire remote workforce)
Severity: Business-critical (remote-first company)
Assignee: Identity & Access Management
""".strip(),
    expected_resolution_summary=(
        "Verify RADIUS shared secret consistency across all NPS nodes and VPN concentrators; "
        "restart RADIUS service to drain overloaded queue; validate auth flow end-to-end; "
        "add RADIUS queue depth to monitoring dashboards."
    ),
    ansible_playbook="restart_radius_service.yml",
)

S05 = Scenario(
    id=5,
    title="SSO Login Failure — SAML Token Expiry",
    category="end_user",
    detection_mode="both",
    ci_target="linux-lab-01",
    classification="sso_authentication_failure",
    priority="P2",
    jira_labels=["end-user", "sso", "saml", "auth"],
    ticket_text="""
Summary: Users reporting 'Invalid Token' error on application login via SSO

Description:
Multiple business units have reported users receiving "Invalid or expired token" errors
when attempting to log in via the corporate SSO portal. The issue is intermittent —
approximately 1 in 3 login attempts fails.

Error message: "SAML assertion validation failed: NotOnOrAfter condition not met"

Affected applications: ERP system, HR portal, Analytics dashboard, Jira
Not affected: Direct LDAP-authenticated services

Telemetry context:
- Auth API /sso/callback endpoint: 400 error rate at 34% (normal <1%)
- Token validation failures: 892 in last 60 minutes
- IdP response time: 2,100ms (normal: 180ms)

Hypothesis: Clock skew between IdP (linux-lab-01) and SP may have widened beyond
the SAML assertion tolerance window (typically ±5 minutes).

Last change: Identity Provider server NTP configuration updated at 22:00 UTC yesterday.

Assignee: Identity Platform Team
""".strip(),
    expected_resolution_summary=(
        "Sync NTP on IdP server to correct clock skew; force SAML metadata refresh on all SPs; "
        "clear session cache; validate token validation succeeds; add clock-skew monitoring alert."
    ),
    telemetry_events=[
        TelemetryEvent(
            metric="http_error_rate_percent",
            host="linux-lab-01",
            unit="percent",
            baseline=0.5,
            escalation_steps=[2.0, 8.0, 18.0, 28.0, 34.0],
            threshold=10.0,
            tags={"endpoint": "/sso/callback", "method": "POST"},
        )
    ],
    ansible_playbook="sync_ntp_idp.yml",
)


# ---------------------------------------------------------------------------
# DATA ENGINEERING SCENARIOS (3)
# ---------------------------------------------------------------------------

S06 = Scenario(
    id=6,
    title="ETL Pipeline Failure — Nightly Data Load",
    category="data_engineering",
    detection_mode="reactive",
    ci_target="linux-lab-02",
    classification="etl_pipeline_failure",
    priority="P2",
    jira_labels=["data-engineering", "etl", "pipeline", "warehouse"],
    ticket_text="""
Summary: Nightly ETL job failed at 02:15 AM — data not loaded to warehouse

Description:
The scheduled nightly ETL pipeline (job: customer_orders_full_load) failed at 02:15 UTC
with an out-of-memory error. As a result, the data warehouse has not been refreshed
with yesterday's transaction data. All dependent reports and dashboards are showing
stale data from 48 hours ago.

Error log excerpt:
  [02:15:03] FATAL: Java heap space — OutOfMemoryError
  [02:15:03] Pipeline step: transform_order_aggregates (step 4 of 7)
  [02:15:03] Records processed before failure: 4,847,223 / 12,500,000

Business impact:
- Daily sales report not generated (CFO dashboard unavailable)
- Inventory replenishment algorithm running on stale stock data
- Customer segmentation model not refreshed (marketing campaigns paused)

Context:
- Order volume yesterday was 2.3× normal due to promotional campaign
- ETL allocated JVM heap: 8 GB (unchanged for 18 months)
- Source database export completed successfully; failure is in transformation layer

Jira: DE-4821
Assignee: Data Engineering Team
""".strip(),
    expected_resolution_summary=(
        "Increase ETL JVM heap to 16 GB; rerun failed job with incremental chunking; "
        "validate row counts match source; add volume-adaptive memory scaling; "
        "alert on pipeline throughput anomalies before OOM."
    ),
    ansible_playbook="restart_etl_job.yml",
)

S07 = Scenario(
    id=7,
    title="Database Replication Lag — Read Replica Stale",
    category="data_engineering",
    detection_mode="proactive",
    ci_target="linux-lab-02",
    classification="database_replication_lag",
    priority="P2",
    jira_labels=["data-engineering", "database", "replication", "postgres"],
    ticket_text="""
Summary: Read replica replication lag exceeding SLO — auto-promoted by telemetry engine

Description:
The AIOps telemetry engine detected a sustained and growing replication lag on the
PostgreSQL read replica (linux-lab-02). Current lag: 4 minutes 12 seconds (SLO: 30 seconds).
If lag continues to grow, read-heavy workloads will be routed to the primary, risking
write performance degradation.

Telemetry at incident creation:
- pg_replication_delay_seconds: 252s (threshold: 30s)
- Primary write load: 1,840 TPS (elevated — bulk import running)
- Replica WAL receiver: running (not stalled)
- Network bandwidth primary→replica: 94% utilised

Hypothesis: Concurrent bulk data import on primary is generating WAL faster than the
replica can apply. Replica apply worker may need tuning.

Auto-created by AIOps at 16:42 UTC.
Assignee: Database Reliability Team
""".strip(),
    expected_resolution_summary=(
        "Throttle or pause bulk import job on primary; tune replica wal_receiver_timeout "
        "and max_standby_streaming_delay; monitor lag recovery; schedule imports during off-peak."
    ),
    telemetry_events=[
        TelemetryEvent(
            metric="pg_replication_delay_seconds",
            host="linux-lab-02",
            unit="seconds",
            baseline=5.0,
            escalation_steps=[15.0, 45.0, 90.0, 140.0, 210.0, 252.0],
            threshold=30.0,
            tags={"role": "replica", "primary": "linux-lab-01"},
        )
    ],
    ansible_playbook="remediate_replication_lag.yml",
)

S08 = Scenario(
    id=8,
    title="Data Quality Alert — Null Values in Critical Field",
    category="data_engineering",
    detection_mode="reactive",
    ci_target="linux-lab-02",
    classification="data_quality_violation",
    priority="P2",
    jira_labels=["data-engineering", "data-quality", "pipeline"],
    ticket_text="""
Summary: NULL values in customer_id column — downstream reports and ML models broken

Description:
A data quality scan run at 08:00 UTC detected 18,432 records (11.3% of yesterday's
load) with NULL customer_id in the transactions table. This is a mandatory foreign key
field. The nulls have propagated downstream:

Affected systems:
- Revenue attribution reports: showing "Unassigned" for 11% of transactions
- Customer LTV model: failing to join on customer_id (job aborted)
- Churn prediction pipeline: skipping affected records (silent data loss)
- Finance reconciliation: €2.1M in transactions unattributed

Root cause investigation:
- Null records trace to a new data feed from the EMEA regional CRM system
- The EMEA CRM started sending records with customer_id as empty string "" instead of
  the expected integer ID, following a schema migration on their side last Thursday
- The ETL pipeline's null-check validator was not updated to catch empty strings

Jira: DE-4833
Assignee: Data Engineering
""".strip(),
    expected_resolution_summary=(
        "Add empty-string-to-null coercion in ETL validation layer; quarantine affected records; "
        "trigger EMEA CRM to resend corrected records; backfill NULL records from CRM API; "
        "add customer_id null-rate to data quality dashboard."
    ),
    ansible_playbook="quarantine_null_records.yml",
)


# ---------------------------------------------------------------------------
# APPLICATION SCENARIOS (2)
# ---------------------------------------------------------------------------

S09 = Scenario(
    id=9,
    title="Payment API High Error Rate — HTTP 500 Storm",
    category="application",
    detection_mode="both",
    ci_target="linux-lab-01",
    classification="api_high_error_rate",
    priority="P1",
    jira_labels=["application", "api", "payment", "500-error"],
    ticket_text="""
Summary: Payment API returning 500s — customer transactions failing

Description:
Starting at 19:45 UTC, the payment processing API began returning HTTP 500 errors at
scale. Current error rate: 62%. Customers are experiencing failed checkout flows and
payment declines that are actually system errors, not legitimate declines.

Real-time metrics:
- /api/v2/payments/charge: 500 error rate 62% (normal <0.1%)
- /api/v2/payments/refund: 500 error rate 41%
- Latency p99: 28,000ms (timeout threshold)
- Throughput: 220 req/s (normal: 850 req/s — queue backing up)

Application log excerpt:
  ERROR PaymentProcessor: Connection pool exhausted — timeout waiting for connection
  ERROR PaymentProcessor: com.zaxxer.hikari.pool.HikariPool$PoolInitializationException
  WARN  HikariCP: Connection is not available, request timed out after 30,000ms

Root cause hypothesis:
- Database connection pool exhausted on payment-db (max_pool_size=50)
- A slow query introduced in today's 18:30 UTC deployment is holding connections open
- Each "slow" request holds a DB connection for ~45s instead of the normal <100ms

Financial impact: Estimated £18,000/minute in failed transactions
Jira: APP-7721
Assignee: Payments Engineering
""".strip(),
    expected_resolution_summary=(
        "Roll back 18:30 UTC deployment; flush HikariCP connection pool; verify error rate drops; "
        "add slow-query monitoring; tune connection pool timeout; add circuit breaker on payment DB."
    ),
    telemetry_events=[
        TelemetryEvent(
            metric="http_error_rate_percent",
            host="linux-lab-01",
            unit="percent",
            baseline=0.1,
            escalation_steps=[1.5, 8.0, 22.0, 41.0, 62.0],
            threshold=5.0,
            tags={"endpoint": "/api/v2/payments/charge", "method": "POST"},
        ),
        TelemetryEvent(
            metric="response_time_ms",
            host="linux-lab-01",
            unit="ms",
            baseline=180.0,
            escalation_steps=[450.0, 1200.0, 4500.0, 12000.0, 28000.0],
            threshold=2000.0,
            tags={"endpoint": "/api/v2/payments/charge"},
        ),
    ],
    ansible_playbook="rollback_deployment.yml",
)

S10 = Scenario(
    id=10,
    title="Memory Leak — Application Pod OOM Kill",
    category="application",
    detection_mode="proactive",
    ci_target="linux-lab-01",
    classification="memory_exhaustion_oom",
    priority="P1",
    jira_labels=["application", "memory", "oom", "kubernetes"],
    ticket_text="""
Summary: Memory utilisation trending to OOM — proactive incident before pod crash

Description:
Telemetry analysis detected a progressive memory leak pattern on linux-lab-01 running
the report-generator service. Memory has been climbing at a rate of ~2.3 GB/hour.
At current trajectory, the service will hit the 16 GB container limit and be OOM-killed
in approximately 52 minutes.

Proactive detection metrics:
- Memory usage: 12.8 GB / 16 GB limit (80%)
- Memory growth rate: 2.3 GB/hour (anomalous — 72-hour average: 0.1 GB/hour)
- GC frequency: 847/min (normal: 45/min — GC thrashing indicates heap pressure)
- Live heap objects: 48M (growing; expected plateau: 8M)
- Last pod restart: 6 days ago

Pattern match: Similar signature observed in INC0010234 (same service, 3 months ago)
— root cause was an unclosed ResultSet in the PDF export function triggered by
reports > 500 pages. Resolved then by patching the ResultSet close() call.

Risk: If not addressed, pod will OOM-kill causing ~4-minute service outage and
loss of all in-progress report generation jobs (estimated 23 jobs running).

Auto-created by AIOps at 03:18 UTC.
Assignee: Platform Engineering
""".strip(),
    expected_resolution_summary=(
        "Trigger JVM heap dump before OOM; identify leak source via heap analysis; "
        "apply known fix (close ResultSet in PDF export); rolling restart service; "
        "set Kubernetes memory limit to 20 GB with HPA to scale out on memory pressure."
    ),
    telemetry_events=[
        TelemetryEvent(
            metric="memory_usage_percent",
            host="linux-lab-01",
            unit="percent",
            baseline=42.0,
            escalation_steps=[52.0, 61.0, 68.0, 74.0, 80.0],
            threshold=75.0,
            tags={"service": "report-generator", "container": "report-gen-pod"},
        ),
        TelemetryEvent(
            metric="jvm_gc_frequency",
            host="linux-lab-01",
            unit="per_minute",
            baseline=45.0,
            escalation_steps=[120.0, 280.0, 450.0, 680.0, 847.0],
            threshold=300.0,
            tags={"service": "report-generator"},
        ),
    ],
    ansible_playbook="remediate_memory_leak.yml",
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_SCENARIOS: list[Scenario] = [S01, S02, S03, S04, S05, S06, S07, S08, S09, S10]

SCENARIOS_BY_ID: dict[int, Scenario] = {s.id: s for s in ALL_SCENARIOS}

CATEGORY_LABELS = {
    "infra": "Infrastructure",
    "end_user": "End-User",
    "data_engineering": "Data Engineering",
    "application": "Application",
}

DETECTION_LABELS = {
    "reactive": "Reactive",
    "proactive": "Proactive",
    "both": "Reactive + Proactive",
}


def get_scenario(scenario_id: int) -> Scenario:
    if scenario_id not in SCENARIOS_BY_ID:
        raise ValueError(f"Unknown scenario ID {scenario_id}. Valid IDs: 1–10.")
    return SCENARIOS_BY_ID[scenario_id]


def scenarios_by_category(category: Category) -> list[Scenario]:
    return [s for s in ALL_SCENARIOS if s.category == category]


def scenarios_by_mode(mode: DetectionMode) -> list[Scenario]:
    return [s for s in ALL_SCENARIOS if s.detection_mode in (mode, "both")]
