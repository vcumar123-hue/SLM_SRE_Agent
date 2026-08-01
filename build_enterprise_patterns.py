#!/usr/bin/env python3
"""
build_enterprise_patterns.py

Offline Enterprise Pattern Builder for AI Ops Copilot.

Purpose
-------
Build a sanitized, reusable Enterprise Correlation Pattern Library from
anonymized multi-client ticket/document dumps.

Input folders
-------------
Default input root:
    data/pattern_ingestion/anonymized/

Expected structure:
    data/pattern_ingestion/anonymized/client_a/*.txt|*.json|*.csv|*.xlsx|*.md
    data/pattern_ingestion/anonymized/client_b/*.txt|*.json|*.csv|*.xlsx|*.md
    data/pattern_ingestion/anonymized/client_c/*.txt|*.json|*.csv|*.xlsx|*.md

Output files
------------
    data/global_patterns/enterprise_correlation_patterns.json
    data/global_patterns/pattern_candidates_review.json
    data/global_patterns/pattern_builder_summary.json

Design principles
-----------------
1. Do not preserve raw client identifiers in global output.
2. Extract reusable incident/change/dependency correlation patterns.
3. Track source lineage only by anonymous client alias.
4. Produce reviewable candidates first; approval remains a human governance step.
5. No external API calls; fully offline and client-safe.

Usage
-----
    python scripts/build_enterprise_patterns.py

Optional:
    python scripts/build_enterprise_patterns.py \
        --input-root data/pattern_ingestion/anonymized \
        --output-root data/global_patterns \
        --min-clients 1 \
        --min-incidents 1

Dependencies
------------
Required: Python standard library
Optional: pandas, openpyxl for CSV/XLSX parsing
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# =========================================================
# Constants / pattern dictionaries
# =========================================================

SUPPORTED_EXTENSIONS = {".txt", ".md", ".json", ".jsonl", ".csv", ".xlsx"}

DEFAULT_INPUT_ROOT = Path("data/pattern_ingestion/anonymized")
DEFAULT_OUTPUT_ROOT = Path("data/global_patterns")

CLASSIFICATION_RULES: Dict[str, Dict[str, Any]] = {
    "cpu_saturation": {
        "domain": "compute",
        "keywords": [
            "cpu", "high cpu", "processor", "load average", "runaway process",
            "top process", "process spike", "cpu_percent", "utilization", "compute saturation",
        ],
    },
    "memory_pressure": {
        "domain": "compute",
        "keywords": [
            "memory", "ram", "oom", "out of memory", "heap", "memory leak",
            "swap", "gc pause", "garbage collection", "memory pressure",
        ],
    },
    "disk_full": {
        "domain": "storage",
        "keywords": [
            "disk full", "filesystem full", "no space", "storage full", "volume full",
            "log growth", "tmp full", "capacity", "inode", "disk utilization",
        ],
    },
    "api_gateway_timeout": {
        "domain": "application_network",
        "keywords": [
            "api timeout", "gateway timeout", "504", "http 504", "timeout", "latency",
            "response time", "downstream", "api gateway", "connection pool", "pool exhaustion",
        ],
    },
    "vpn_client_failure": {
        "domain": "network_security",
        "keywords": [
            "vpn", "globalprotect", "anyconnect", "ssl vpn", "ipsec", "remote access",
            "radius", "ldap bind", "vpn login", "vpn authentication", "split tunnel",
        ],
    },
    "dns_resolution_failure": {
        "domain": "network",
        "keywords": [
            "dns", "resolver", "name resolution", "lookup", "nxdomain", "dns latency",
            "dns timeout", "cname", "a record", "stale dns", "resolver failure",
        ],
    },
    "sso_mfa_failure": {
        "domain": "identity",
        "keywords": [
            "sso", "mfa", "single sign", "saml", "oauth", "oidc", "idp", "identity provider",
            "conditional access", "token", "jwt", "authentication loop", "login loop",
        ],
    },
    "pod_crash_loop": {
        "domain": "kubernetes",
        "keywords": [
            "crashloopbackoff", "pod crash", "container restart", "kubernetes", "k8s",
            "pod restart", "image pull", "container logs", "liveness probe", "readiness probe",
        ],
    },
    "certificate_expiry_warning": {
        "domain": "security",
        "keywords": [
            "certificate", "cert", "ssl", "tls", "expired cert", "certificate expiry",
            "handshake", "x509", "cert chain", "renew certificate",
        ],
    },
    "database_latency": {
        "domain": "database",
        "keywords": [
            "database", "db latency", "query slow", "deadlock", "connection pool",
            "sql timeout", "oracle", "postgres", "mysql", "db timeout", "lock wait",
        ],
    },
    "network_port_block": {
        "domain": "network_security",
        "keywords": [
            "port blocked", "port unreachable", "connection refused", "connection timeout",
            "firewall deny", "acl", "security group", "network policy", "egress", "ingress",
        ],
    },
}

SCENARIO_RULES: Dict[str, Dict[str, Any]] = {
    "firewall_policy_change_blocking_downstream_port": {
        "association_type": "change_network_dependency",
        "keywords": ["firewall", "policy", "acl", "security group", "deny", "blocked", "port", "rule", "egress", "ingress"],
        "time_window": "0 to 4 hours before incident",
        "evidence_signals": ["connection timeout", "port connectivity failure", "firewall deny logs", "recent firewall or security-group change"],
        "validation_checks": ["validate recent firewall/security-group changes", "test source-to-destination port connectivity", "compare deny logs with incident window", "confirm dependency path from topology"],
        "recommended_actions": ["rollback or correct policy rule after approval", "restore required downstream port access", "validate recovery with synthetic transaction"],
    },
    "dns_or_ip_change_after_workload_migration": {
        "association_type": "change_dns_cloud_migration",
        "keywords": ["dns", "ip change", "cname", "a record", "migration", "cloud", "cutover", "route53", "private dns", "stale ip", "endpoint"],
        "time_window": "0 to 24 hours before incident",
        "evidence_signals": ["DNS mismatch", "stale endpoint", "traffic routed to old IP", "cloud cutover or migration change"],
        "validation_checks": ["compare DNS records before/after change", "validate resolved IP from affected subnet", "check cloud migration/cutover change record", "verify route table and endpoint target"],
        "recommended_actions": ["correct DNS or endpoint mapping", "flush/refresh stale resolver cache where approved", "validate application path after correction"],
    },
    "patching_or_reboot_window_dependency_impact": {
        "association_type": "change_patching_dependency",
        "keywords": ["patch", "patching", "reboot", "maintenance", "server restart", "windows update", "kernel update", "firmware", "upgrade"],
        "time_window": "0 to 12 hours before incident",
        "evidence_signals": ["recent patch window", "dependency reboot", "service restart", "post-patch agent/process spike"],
        "validation_checks": ["review patch calendar", "check recent reboots for dependency CIs", "validate services restarted cleanly", "compare incident start with maintenance window"],
        "recommended_actions": ["restart failed dependency service after approval", "rollback problematic patch if confirmed", "update maintenance validation checklist"],
    },
    "recent_application_deployment_or_config_change": {
        "association_type": "change_application_config",
        "keywords": ["deployment", "release", "config", "configuration", "rollback", "version", "change", "deploy", "feature flag", "yaml", "properties"],
        "time_window": "0 to 6 hours before incident",
        "evidence_signals": ["recent app deployment", "configuration change", "error rate increase after release", "new version observed"],
        "validation_checks": ["review deployment/change records", "compare error rate before/after release", "inspect changed config keys", "validate rollback candidate"],
        "recommended_actions": ["rollback recent release if correlated", "correct bad configuration", "restart affected service after validation"],
    },
    "load_balancer_or_traffic_shift_issue": {
        "association_type": "topology_load_balancer",
        "keywords": ["load balancer", "lb", "f5", "nginx", "alb", "target group", "pool", "health check", "traffic shift", "weight", "vip"],
        "time_window": "same time to 2 hours before incident",
        "evidence_signals": ["unhealthy pool member", "health check mismatch", "traffic shifted to bad node", "VIP/target group change"],
        "validation_checks": ["review load-balancer pool health", "check recent target group or VIP changes", "validate traffic distribution", "compare node-specific error rates"],
        "recommended_actions": ["remove unhealthy node from pool after approval", "correct health check", "restore traffic weights", "validate service recovery"],
    },
    "downstream_dependency_latency_or_outage": {
        "association_type": "incident_dependency",
        "keywords": ["downstream", "dependency", "upstream", "service unavailable", "latency", "slow", "timeout", "db", "database", "queue", "third party"],
        "time_window": "same time to 30 minutes before incident",
        "evidence_signals": ["downstream latency spike", "dependency error rate increase", "connection pool wait", "queue backlog"],
        "validation_checks": ["check downstream dependency health", "inspect dependency latency/error metrics", "review connection pool and retry behavior", "validate topology dependency path"],
        "recommended_actions": ["restore or scale dependency", "reduce retry storm if present", "fail over to healthy dependency if approved"],
    },
    "batch_job_or_scheduled_task_collision": {
        "association_type": "process_schedule_telemetry",
        "keywords": ["batch", "job", "scheduled task", "cron", "scheduler", "etl", "nightly", "backup job", "reporting job"],
        "time_window": "same time to 2 hours before incident",
        "evidence_signals": ["scheduled job active", "CPU or IO spike", "batch overlap", "long-running process"],
        "validation_checks": ["review scheduled job calendar", "check top process during incident", "compare resource spike with job runtime", "inspect batch logs"],
        "recommended_actions": ["reschedule or throttle batch job", "terminate runaway job after approval", "separate resource-intensive workloads"],
    },
    "endpoint_security_or_antivirus_scan_overlap": {
        "association_type": "security_agent_telemetry",
        "keywords": ["antivirus", "edr", "endpoint", "security scan", "crowdstrike", "defender", "scan", "agent", "malware", "av"],
        "time_window": "same time to 2 hours before incident",
        "evidence_signals": ["security scan active", "agent CPU spike", "disk IO spike", "overlap with batch window"],
        "validation_checks": ["review endpoint security scan schedule", "check security agent process usage", "compare scan window with incident window", "verify approved exclusions"],
        "recommended_actions": ["tune approved exclusions", "reschedule scan window", "coordinate scan and batch schedules"],
    },
    "authentication_or_identity_provider_dependency": {
        "association_type": "identity_dependency",
        "keywords": ["idp", "sso", "mfa", "saml", "oauth", "oidc", "ldap", "radius", "adfs", "azure ad", "entra", "okta", "duo"],
        "time_window": "same time to 2 hours before incident",
        "evidence_signals": ["auth failures", "MFA latency", "token validation errors", "identity provider errors"],
        "validation_checks": ["check IdP/MFA health", "review conditional access changes", "validate LDAP/RADIUS bind status", "inspect token/certificate errors"],
        "recommended_actions": ["restore identity dependency", "rollback conditional-access change if confirmed", "renew/rotate auth certificate if expired"],
    },
    "certificate_or_secret_rotation_issue": {
        "association_type": "security_config_change",
        "keywords": ["certificate", "cert", "tls", "ssl", "secret", "key rotation", "keystore", "truststore", "x509", "handshake"],
        "time_window": "0 to 24 hours before incident",
        "evidence_signals": ["TLS handshake failure", "expired certificate", "secret/key mismatch", "rotation change record"],
        "validation_checks": ["validate certificate expiry and chain", "check recent secret/key rotation", "compare truststore/keystore versions", "inspect TLS handshake errors"],
        "recommended_actions": ["renew or redeploy certificate", "correct secret reference", "validate TLS chain after change"],
    },
    "cloud_migration_sizing_or_routing_mismatch": {
        "association_type": "cloud_migration_topology",
        "keywords": ["cloud migration", "migrated", "on-prem", "on prem", "subnet", "vpc", "vnet", "route table", "nat", "peering", "instance size", "rightsizing"],
        "time_window": "0 to 7 days before incident",
        "evidence_signals": ["recent workload migration", "new subnet/routing path", "undersized instance", "NAT/peering route issue"],
        "validation_checks": ["review migration/cutover record", "validate route tables and subnet ACLs", "compare old vs new capacity sizing", "test dependency connectivity from new location"],
        "recommended_actions": ["correct routing/security policy", "resize workload if undersized", "update DNS/endpoint mapping", "validate post-migration checklist"],
    },
    "retry_storm_after_dependency_failure": {
        "association_type": "application_dependency_behavior",
        "keywords": ["retry", "retry storm", "thread", "thread pool", "connection pool", "circuit breaker", "timeout", "queue backlog"],
        "time_window": "same time to 1 hour before incident",
        "evidence_signals": ["retry volume spike", "thread pool saturation", "connection pool exhaustion", "dependency timeout"],
        "validation_checks": ["inspect retry metrics", "check thread/connection pool saturation", "validate dependency timeout pattern", "review circuit breaker behavior"],
        "recommended_actions": ["restore dependency", "tune retry/circuit breaker settings", "scale affected service if approved"],
    },
}

# Classification-specific scenario priors. These help rank patterns even when raw dumps are sparse.
CLASSIFICATION_SCENARIO_PRIORS: Dict[str, List[str]] = {
    "cpu_saturation": [
        "batch_job_or_scheduled_task_collision",
        "endpoint_security_or_antivirus_scan_overlap",
        "patching_or_reboot_window_dependency_impact",
        "downstream_dependency_latency_or_outage",
        "retry_storm_after_dependency_failure",
        "cloud_migration_sizing_or_routing_mismatch",
        "recent_application_deployment_or_config_change",
    ],
    "api_gateway_timeout": [
        "firewall_policy_change_blocking_downstream_port",
        "downstream_dependency_latency_or_outage",
        "dns_or_ip_change_after_workload_migration",
        "load_balancer_or_traffic_shift_issue",
        "recent_application_deployment_or_config_change",
        "retry_storm_after_dependency_failure",
        "cloud_migration_sizing_or_routing_mismatch",
    ],
    "vpn_client_failure": [
        "authentication_or_identity_provider_dependency",
        "certificate_or_secret_rotation_issue",
        "firewall_policy_change_blocking_downstream_port",
        "dns_or_ip_change_after_workload_migration",
        "patching_or_reboot_window_dependency_impact",
    ],
    "dns_resolution_failure": [
        "dns_or_ip_change_after_workload_migration",
        "firewall_policy_change_blocking_downstream_port",
        "cloud_migration_sizing_or_routing_mismatch",
        "patching_or_reboot_window_dependency_impact",
    ],
    "pod_crash_loop": [
        "recent_application_deployment_or_config_change",
        "certificate_or_secret_rotation_issue",
        "cloud_migration_sizing_or_routing_mismatch",
        "downstream_dependency_latency_or_outage",
    ],
    "database_latency": [
        "downstream_dependency_latency_or_outage",
        "recent_application_deployment_or_config_change",
        "retry_storm_after_dependency_failure",
        "patching_or_reboot_window_dependency_impact",
    ],
}

SENSITIVE_PATTERNS = [
    # IP addresses
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    # URLs
    (re.compile(r"https?://[^\s,;\)\]\}]+", re.IGNORECASE), "[URL]"),
    # Windows / Linux-ish hostnames with digits/hyphens
    (re.compile(r"\b(?:srv|server|host|node|app|web|db|sql|fw|lb|vpn|dc|prd|prod|dev|qa|uat)[a-z0-9-]*\d+[a-z0-9-]*\b", re.IGNORECASE), "[HOST]"),
    # Common ticket/change identifiers
    (re.compile(r"\b(?:INC|CHG|REQ|TASK|PRB|RITM)[-_ ]?\d{4,}\b", re.IGNORECASE), "[RECORD_ID]"),
]


# =========================================================
# Dataclasses
# =========================================================

@dataclass
class EvidenceRecord:
    record_id: str
    client_alias: str
    source_file: str
    source_type: str
    text_hash: str
    classification: str
    domain: str
    matched_classification_terms: List[str]
    scenarios: List[str]
    matched_scenario_terms: Dict[str, List[str]]
    sanitized_excerpt: str


@dataclass
class EnterprisePatternCandidate:
    pattern_id: str
    classification: str
    domain: str
    associated_scenario: str
    association_type: str
    frequency_band: str
    observed_across_client_count: int
    observed_client_aliases: List[str]
    observed_record_count: int
    observed_incident_count_band: str
    typical_time_window: str
    evidence_signals: List[str]
    validation_checks: List[str]
    recommended_actions: List[str]
    triage_priority: int
    confidence_score: float
    sanitization_status: str
    approved_for_global_use: bool
    source_lineage: Dict[str, Any]
    review_notes: str = ""


# =========================================================
# Utilities
# =========================================================


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(text: str) -> str:
    text = str(text or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def stable_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:length]


def sanitize_text(text: str) -> str:
    out = str(text or "")
    for pattern, repl in SENSITIVE_PATTERNS:
        out = pattern.sub(repl, out)
    # Replace long numeric IDs that are likely internal IDs.
    out = re.sub(r"\b\d{7,}\b", "[NUMERIC_ID]", out)
    # Normalize whitespace.
    out = re.sub(r"\s+", " ", out).strip()
    return out


def first_n_words(text: str, n: int = 90) -> str:
    words = str(text or "").split()
    return " ".join(words[:n]) + ("..." if len(words) > n else "")


def band_count(count: int) -> str:
    if count <= 0:
        return "0"
    if count <= 5:
        return "1-5"
    if count <= 10:
        return "6-10"
    if count <= 25:
        return "11-25"
    if count <= 50:
        return "26-50"
    if count <= 100:
        return "51-100"
    return "100+"


def frequency_band(record_count: int, client_count: int) -> str:
    if client_count >= 3 and record_count >= 10:
        return "high"
    if client_count >= 2 and record_count >= 5:
        return "medium"
    if record_count >= 3:
        return "medium"
    return "low"


def safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        try:
            return path.read_text(encoding="latin-1", errors="ignore")
        except Exception:
            return ""


# =========================================================
# File readers
# =========================================================


def read_json_records(path: Path) -> List[str]:
    text = safe_read_text(path)
    if not text.strip():
        return []
    records: List[str] = []
    try:
        if path.suffix.lower() == ".jsonl":
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                records.append(flatten_json_to_text(obj))
        else:
            obj = json.loads(text)
            if isinstance(obj, list):
                for item in obj:
                    records.append(flatten_json_to_text(item))
            elif isinstance(obj, dict):
                # If dict contains list-like ticket arrays, split them.
                list_values = [v for v in obj.values() if isinstance(v, list)]
                if list_values:
                    for arr in list_values:
                        for item in arr:
                            records.append(flatten_json_to_text(item))
                else:
                    records.append(flatten_json_to_text(obj))
            else:
                records.append(str(obj))
    except Exception:
        records.append(text)
    return [r for r in records if r.strip()]


def flatten_json_to_text(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    parts: List[str] = []
    if isinstance(obj, dict):
        preferred_keys = [
            "title", "summary", "description", "short_description", "content", "notes", "resolution",
            "root_cause", "cause", "work_notes", "comments", "classification", "category", "subcategory",
            "change", "change_description", "close_notes", "symptom", "action", "remediation",
        ]
        for key in preferred_keys:
            if key in obj:
                parts.append(f"{key}: {flatten_json_to_text(obj.get(key))}")
        for key, value in obj.items():
            if key not in preferred_keys and isinstance(value, (str, int, float, bool)):
                parts.append(f"{key}: {value}")
    elif isinstance(obj, list):
        for item in obj:
            parts.append(flatten_json_to_text(item))
    return " | ".join([p for p in parts if p])


def read_csv_records(path: Path) -> List[str]:
    records: List[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
            except Exception:
                dialect = csv.excel
            reader = csv.DictReader(f, dialect=dialect)
            for row in reader:
                values = []
                for k, v in row.items():
                    if v is None:
                        continue
                    text = str(v).strip()
                    if text:
                        values.append(f"{k}: {text}")
                if values:
                    records.append(" | ".join(values))
    except Exception:
        # Fallback: entire file as one record.
        text = safe_read_text(path)
        if text.strip():
            records.append(text)
    return records


def read_xlsx_records(path: Path) -> List[str]:
    try:
        import pandas as pd  # type: ignore
    except Exception:
        return [f"[XLSX_REQUIRES_PANDAS] {path.name}"]

    records: List[str] = []
    try:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str)
        for sheet_name, df in sheets.items():
            df = df.fillna("")
            for _, row in df.iterrows():
                values = []
                for k, v in row.items():
                    text = str(v).strip()
                    if text:
                        values.append(f"{k}: {text}")
                if values:
                    records.append(f"sheet: {sheet_name} | " + " | ".join(values))
    except Exception as exc:
        records.append(f"[XLSX_READ_ERROR] {path.name}: {exc}")
    return records


def read_records_from_file(path: Path) -> List[str]:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        text = safe_read_text(path)
        # Split very large text into paragraph-ish records.
        chunks = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
        return chunks if chunks else ([text] if text.strip() else [])
    if suffix in {".json", ".jsonl"}:
        return read_json_records(path)
    if suffix == ".csv":
        return read_csv_records(path)
    if suffix == ".xlsx":
        return read_xlsx_records(path)
    return []


# =========================================================
# Extraction logic
# =========================================================


def keyword_hits(text: str, keywords: Iterable[str]) -> List[str]:
    low = text.lower()
    hits = []
    for kw in keywords:
        k = kw.lower().strip()
        if not k:
            continue
        # Flexible phrase match; for very short words use word boundary.
        if len(k) <= 3:
            if re.search(rf"\b{re.escape(k)}\b", low):
                hits.append(kw)
        elif k in low:
            hits.append(kw)
    return sorted(set(hits), key=lambda x: (len(x), x))


def classify_record(text: str) -> Tuple[str, str, List[str]]:
    scores: List[Tuple[int, str, List[str]]] = []
    for cls, meta in CLASSIFICATION_RULES.items():
        hits = keyword_hits(text, meta.get("keywords", []))
        if hits:
            # Weight phrase hits by length a bit.
            score = len(hits) + sum(1 for h in hits if " " in h)
            scores.append((score, cls, hits))
    if not scores:
        return "general_operational_issue", "general", []
    scores.sort(reverse=True, key=lambda x: x[0])
    _, classification, hits = scores[0]
    domain = CLASSIFICATION_RULES.get(classification, {}).get("domain", "general")
    return classification, domain, hits[:12]


def extract_scenarios(text: str, classification: str) -> Tuple[List[str], Dict[str, List[str]]]:
    scenario_scores: List[Tuple[int, str, List[str]]] = []
    for scenario, meta in SCENARIO_RULES.items():
        hits = keyword_hits(text, meta.get("keywords", []))
        if hits:
            score = len(hits) + sum(1 for h in hits if " " in h)
            # Give a small boost to scenario priors for the classification.
            if scenario in CLASSIFICATION_SCENARIO_PRIORS.get(classification, []):
                score += 2
            scenario_scores.append((score, scenario, hits))

    # Add classification priors if no scenario was found but classification is known.
    if not scenario_scores and classification in CLASSIFICATION_SCENARIO_PRIORS:
        priors = CLASSIFICATION_SCENARIO_PRIORS[classification][:3]
        return priors, {p: ["classification_prior"] for p in priors}

    scenario_scores.sort(reverse=True, key=lambda x: x[0])
    scenarios = [s for _, s, _ in scenario_scores[:8]]
    hit_map = {s: hits[:12] for _, s, hits in scenario_scores[:8]}
    return scenarios, hit_map


def infer_source_type(path: Path) -> str:
    name = path.name.lower()
    parent = path.parent.name.lower()
    blob = f"{parent} {name}"
    if any(k in blob for k in ["ticket", "incident", "servicenow", "snow"]):
        return "ticket_dump"
    if any(k in blob for k in ["change", "chg", "patch", "release"]):
        return "change_dump"
    if any(k in blob for k in ["cmdb", "asset", "topology", "dependency"]):
        return "cmdb_topology"
    if any(k in blob for k in ["monitor", "alert", "event", "prometheus", "splunk"]):
        return "monitoring_export"
    if any(k in blob for k in ["kb", "sop", "runbook", "playbook", "doc"]):
        return "kb_sop_document"
    return "unknown"


def extract_records(input_root: Path) -> List[EvidenceRecord]:
    evidence: List[EvidenceRecord] = []
    if not input_root.exists():
        return evidence

    files = [p for p in input_root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS]
    for path in sorted(files):
        try:
            rel = path.relative_to(input_root)
            client_alias = rel.parts[0] if len(rel.parts) > 1 else "unknown_client"
        except Exception:
            client_alias = "unknown_client"

        raw_records = read_records_from_file(path)
        source_type = infer_source_type(path)
        for idx, raw_text in enumerate(raw_records):
            if not raw_text or len(str(raw_text).strip()) < 20:
                continue
            sanitized = sanitize_text(raw_text)
            classification, domain, class_hits = classify_record(sanitized)
            scenarios, scenario_hits = extract_scenarios(sanitized, classification)
            if classification == "general_operational_issue" and not scenarios:
                continue
            rec_hash = stable_hash(f"{client_alias}|{path}|{idx}|{sanitized}")
            evidence.append(
                EvidenceRecord(
                    record_id=f"ev_{rec_hash}",
                    client_alias=normalize_key(client_alias) or "unknown_client",
                    source_file=str(path),
                    source_type=source_type,
                    text_hash=stable_hash(sanitized, 16),
                    classification=classification,
                    domain=domain,
                    matched_classification_terms=class_hits,
                    scenarios=scenarios,
                    matched_scenario_terms=scenario_hits,
                    sanitized_excerpt=first_n_words(sanitized, 80),
                )
            )
    return evidence


# =========================================================
# Pattern building / clustering
# =========================================================


def triage_priority_for(classification: str, scenario: str) -> int:
    priors = CLASSIFICATION_SCENARIO_PRIORS.get(classification, [])
    if scenario in priors:
        return priors.index(scenario) + 1
    return 10


def confidence_for(record_count: int, client_count: int, priority: int) -> float:
    # Simple deterministic confidence; deliberately conservative.
    base = 0.35
    base += min(record_count, 30) * 0.012
    base += min(client_count, 8) * 0.055
    if priority <= 3:
        base += 0.08
    elif priority <= 6:
        base += 0.04
    return round(max(0.35, min(base, 0.92)), 2)


def build_pattern_candidates(
    evidence: List[EvidenceRecord],
    min_clients: int = 1,
    min_incidents: int = 1,
) -> List[EnterprisePatternCandidate]:
    grouped: Dict[Tuple[str, str], List[EvidenceRecord]] = defaultdict(list)
    for rec in evidence:
        for scenario in rec.scenarios:
            grouped[(rec.classification, scenario)].append(rec)

    candidates: List[EnterprisePatternCandidate] = []
    for (classification, scenario), rows in sorted(grouped.items()):
        client_aliases = sorted({r.client_alias for r in rows})
        if len(client_aliases) < min_clients or len(rows) < min_incidents:
            continue

        domain_counter = Counter([r.domain for r in rows if r.domain])
        domain = domain_counter.most_common(1)[0][0] if domain_counter else CLASSIFICATION_RULES.get(classification, {}).get("domain", "general")
        scenario_meta = SCENARIO_RULES.get(scenario, {})
        record_count = len(rows)
        client_count = len(client_aliases)
        priority = triage_priority_for(classification, scenario)
        pattern_hash = stable_hash(f"{classification}|{scenario}", 8)

        candidates.append(
            EnterprisePatternCandidate(
                pattern_id=f"ecp_{normalize_key(classification)}_{normalize_key(scenario)}_{pattern_hash}",
                classification=classification,
                domain=domain,
                associated_scenario=scenario,
                association_type=scenario_meta.get("association_type", "associated_operational_pattern"),
                frequency_band=frequency_band(record_count, client_count),
                observed_across_client_count=client_count,
                observed_client_aliases=client_aliases,
                observed_record_count=record_count,
                observed_incident_count_band=band_count(record_count),
                typical_time_window=scenario_meta.get("time_window", "incident window to 24 hours before"),
                evidence_signals=scenario_meta.get("evidence_signals", []),
                validation_checks=scenario_meta.get("validation_checks", []),
                recommended_actions=scenario_meta.get("recommended_actions", []),
                triage_priority=priority,
                confidence_score=confidence_for(record_count, client_count, priority),
                sanitization_status="sanitized",
                approved_for_global_use=False,
                source_lineage={
                    "source_type": "multi_client_abstracted_pattern",
                    "client_aliases": client_aliases,
                    "raw_client_data_removed": True,
                    "source_record_ids_sample": [r.record_id for r in rows[:10]],
                    "source_file_count": len({r.source_file for r in rows}),
                },
                review_notes="Review required before promoting to global enterprise pattern library.",
            )
        )

    candidates.sort(key=lambda c: (c.classification, c.triage_priority, -c.confidence_score, c.associated_scenario))
    return candidates


def build_enterprise_patterns(candidates: List[EnterprisePatternCandidate]) -> List[Dict[str, Any]]:
    """Build grouped library by classification from candidates."""
    by_classification: Dict[str, List[EnterprisePatternCandidate]] = defaultdict(list)
    for c in candidates:
        by_classification[c.classification].append(c)

    library: List[Dict[str, Any]] = []
    for classification, rows in sorted(by_classification.items()):
        rows = sorted(rows, key=lambda r: (r.triage_priority, -r.confidence_score, -r.observed_record_count))
        domain = rows[0].domain if rows else CLASSIFICATION_RULES.get(classification, {}).get("domain", "general")
        pattern_id = f"ecp_{classification}_{stable_hash(classification, 8)}"
        library.append(
            {
                "pattern_id": pattern_id,
                "classification": classification,
                "domain": domain,
                "primary_symptom": humanize_classification(classification),
                "top_associated_scenarios": [
                    {
                        "scenario_id": row.associated_scenario,
                        "scenario": humanize_key(row.associated_scenario),
                        "association_type": row.association_type,
                        "frequency_band": row.frequency_band,
                        "observed_across_client_count": row.observed_across_client_count,
                        "observed_incident_count_band": row.observed_incident_count_band,
                        "typical_time_window": row.typical_time_window,
                        "evidence_signals": row.evidence_signals,
                        "validation_checks": row.validation_checks,
                        "recommended_actions": row.recommended_actions,
                        "triage_priority": row.triage_priority,
                        "confidence_score": row.confidence_score,
                        "pattern_candidate_id": row.pattern_id,
                    }
                    for row in rows[:10]
                ],
                "recommended_triage_order": derive_triage_order(rows[:10]),
                "source_lineage": {
                    "origin": "sanitized_multi_client_pattern_builder",
                    "raw_client_data_removed": True,
                    "approved_for_global_use": False,
                    "observed_client_count": len(set(a for row in rows for a in row.observed_client_aliases)),
                    "candidate_count": len(rows),
                },
                "build_metadata": {
                    "generated_at": utc_now_iso(),
                    "builder": "scripts/build_enterprise_patterns.py",
                    "status": "candidate_library_requires_review",
                },
            }
        )
    return library


def humanize_key(key: str) -> str:
    text = str(key or "").replace("_", " ").strip().title()
    replacements = {
        "Dns": "DNS",
        "Ip": "IP",
        "Api": "API",
        "Cpu": "CPU",
        "Db": "DB",
        "Ssl": "SSL",
        "Tls": "TLS",
        "Vpn": "VPN",
        "Sso": "SSO",
        "Mfa": "MFA",
        "Idp": "IdP",
        "Cmdb": "CMDB",
    }
    for src, repl in replacements.items():
        text = re.sub(rf"\b{src}\b", repl, text)
    return text


def humanize_classification(classification: str) -> str:
    mapping = {
        "cpu_saturation": "Sustained high CPU or compute saturation",
        "memory_pressure": "Memory pressure or out-of-memory behavior",
        "disk_full": "Disk or filesystem capacity breach",
        "api_gateway_timeout": "API gateway timeout, 504, or elevated response latency",
        "vpn_client_failure": "VPN authentication or remote access failure",
        "dns_resolution_failure": "DNS resolution failure or DNS latency",
        "sso_mfa_failure": "SSO or MFA authentication failure",
        "pod_crash_loop": "Kubernetes pod crash loop or container restart storm",
        "certificate_expiry_warning": "Certificate expiry or TLS handshake failure",
        "database_latency": "Database latency, query timeout, or connection pool exhaustion",
        "network_port_block": "Network port block or firewall path failure",
    }
    return mapping.get(classification, humanize_key(classification))


def derive_triage_order(rows: List[EnterprisePatternCandidate]) -> List[str]:
    order: List[str] = []
    for row in sorted(rows, key=lambda r: (r.triage_priority, -r.confidence_score)):
        for check in row.validation_checks[:2]:
            if check not in order:
                order.append(check)
        if len(order) >= 8:
            break
    return order


# =========================================================
# IO / command
# =========================================================


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def summarize(evidence: List[EvidenceRecord], candidates: List[EnterprisePatternCandidate], library: List[Dict[str, Any]]) -> Dict[str, Any]:
    class_counts = Counter([e.classification for e in evidence])
    scenario_counts = Counter([s for e in evidence for s in e.scenarios])
    clients = sorted({e.client_alias for e in evidence})
    return {
        "status": "ok",
        "generated_at": utc_now_iso(),
        "client_alias_count": len(clients),
        "client_aliases": clients,
        "evidence_record_count": len(evidence),
        "pattern_candidate_count": len(candidates),
        "enterprise_pattern_count": len(library),
        "top_classifications": class_counts.most_common(20),
        "top_associated_scenarios": scenario_counts.most_common(20),
        "output_files": {
            "enterprise_correlation_patterns": "enterprise_correlation_patterns.json",
            "pattern_candidates_review": "pattern_candidates_review.json",
            "pattern_builder_summary": "pattern_builder_summary.json",
        },
        "notes": [
            "All outputs are candidate artifacts and require human review before promotion.",
            "Client aliases are retained only as anonymized lineage metadata.",
            "Raw ticket/document text is not included in global pattern output; only sanitized excerpts are retained in evidence sample files if enabled.",
        ],
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build enterprise correlation patterns from anonymized multi-client dumps.")
    parser.add_argument("--input-root", default=str(DEFAULT_INPUT_ROOT), help="Root folder containing anonymized client alias subfolders.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Output folder for global pattern artifacts.")
    parser.add_argument("--min-clients", type=int, default=1, help="Minimum distinct client aliases required for a pattern candidate.")
    parser.add_argument("--min-incidents", type=int, default=1, help="Minimum evidence records required for a pattern candidate.")
    parser.add_argument("--write-evidence-sample", action="store_true", help="Write sanitized evidence sample for reviewer debugging.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    input_root = Path(args.input_root)
    output_root = Path(args.output_root)

    print("\nEnterprise Correlation Pattern Builder")
    print("=" * 48)
    print(f"Input root : {input_root}")
    print(f"Output root: {output_root}")
    print(f"Min clients: {args.min_clients}")
    print(f"Min records: {args.min_incidents}")

    if not input_root.exists():
        print(f"\nERROR: Input root does not exist: {input_root}")
        print("Create folders such as:")
        print(f"  {input_root}/client_a/")
        print(f"  {input_root}/client_b/")
        return 2

    evidence = extract_records(input_root)
    candidates = build_pattern_candidates(evidence, min_clients=args.min_clients, min_incidents=args.min_incidents)
    library = build_enterprise_patterns(candidates)
    summary = summarize(evidence, candidates, library)

    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "enterprise_correlation_patterns.json", library)
    write_json(output_root / "pattern_candidates_review.json", [asdict(c) for c in candidates])
    write_json(output_root / "pattern_builder_summary.json", summary)

    if args.write_evidence_sample:
        sample_payload = [asdict(e) for e in evidence[:500]]
        write_json(output_root / "pattern_evidence_sample_sanitized.json", sample_payload)

    print("\nBuild complete")
    print("-" * 48)
    print(f"Evidence records        : {len(evidence)}")
    print(f"Pattern candidates      : {len(candidates)}")
    print(f"Enterprise patterns     : {len(library)}")
    print(f"Client aliases detected : {', '.join(summary.get('client_aliases', [])) or 'none'}")
    print("\nOutputs:")
    print(f"  {output_root / 'enterprise_correlation_patterns.json'}")
    print(f"  {output_root / 'pattern_candidates_review.json'}")
    print(f"  {output_root / 'pattern_builder_summary.json'}")
    if args.write_evidence_sample:
        print(f"  {output_root / 'pattern_evidence_sample_sanitized.json'}")

    if not evidence:
        print("\nWARNING: No evidence records found. Check input folder structure and supported file types.")
    elif not candidates:
        print("\nWARNING: Evidence was found, but no candidates met thresholds. Try --min-clients 1 --min-incidents 1.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
