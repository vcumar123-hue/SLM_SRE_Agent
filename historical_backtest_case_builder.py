#!/usr/bin/env python3
"""
historical_backtest_case_builder.py

Client-local historical backtest case builder for AI Ops Copilot.

Reads client_data_manifest.json, ingests historical ticket data from the configured
client source/RAG folder, infers deterministic backtest expectations, and emits
contextual backtest cases for backtesting_engine.py.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_MANIFEST = Path("data/clients/client_a/governance/client_data_manifest.json")
DEFAULT_LIMIT = 25

# Stratified sampling keeps generated backtest packs representative of the
# source incident population rather than allowing one high-confidence family
# such as TWS/batch jobs to dominate the case set. Quotas are expressed as a
# 100-case target and are scaled for other --limit values.
TARGET_FAMILY_QUOTAS_100 = {
    "batch_scheduler": 20,
    "network_connectivity": 15,
    "file_transfer": 15,
    "vdi_citrix": 10,
    "storage_disk": 8,
    "database": 8,
    "dns": 6,
    "cpu": 6,
    "memory": 4,
    "certificate": 4,
    "vpn": 2,
    "other_valid": 2,
}

FAMILY_TO_EXPECTED_CLASSIFICATION = {
    "batch_scheduler": {"batch_job_failure", "batch_job_delay"},
    "network_connectivity": {"network_connectivity_issue"},
    "file_transfer": {"file_transfer_failure"},
    "vdi_citrix": {"vdi_citrix_connectivity_issue"},
    "storage_disk": {"disk_full"},
    "database": {"database_performance_issue"},
    "dns": {"dns_issue"},
    "cpu": {"cpu_saturation"},
    "memory": {"memory_pressure"},
    "certificate": {"certificate_expiry_warning"},
    "vpn": {"vpn_client_failure"},
    "other_valid": {"general_operational_issue"},
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_phrase(value: Any) -> str:
    text = clean_text(value).lower().replace("_", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9 /:.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_token(value: Any) -> str:
    text = clean_text(value).lower().replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def stable_id(prefix: str, *parts: Any, length: int = 10) -> str:
    raw = "|".join(clean_text(p) for p in parts if clean_text(p)) or str(time.time())
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def safe_read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def first_nonempty(row: Dict[str, Any], keys: Iterable[str]) -> str:
    lower_map = {str(k).lower().strip(): v for k, v in row.items()}
    for key in keys:
        value = lower_map.get(str(key).lower().strip())
        if clean_text(value):
            return clean_text(value)
    for wanted in keys:
        wanted_norm = normalize_phrase(wanted)
        for actual_key, value in lower_map.items():
            if wanted_norm and wanted_norm in normalize_phrase(actual_key) and clean_text(value):
                return clean_text(value)
    return ""


def parse_datetime_safe(value: Any) -> Optional[str]:
    text = clean_text(value)
    if not text:
        return None
    if re.search(r"\d{4}-\d{2}-\d{2}", text):
        return text
    for fmt in ["%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"]:
        try:
            return datetime.strptime(text, fmt).isoformat()
        except Exception:
            pass
    return text if re.search(r"\d{4}|\d{1,2}/\d{1,2}/\d{2,4}", text) else None


def load_manifest(path: Path) -> Dict[str, Any]:
    data = safe_read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"Manifest not found or invalid JSON: {path}")
    return data


def resolve_path(base: Path, value: Any) -> Path:
    p = Path(clean_text(value))
    return p if p.is_absolute() else base / p


def iter_candidate_ticket_files(source_root: Path) -> List[Path]:
    candidates: List[Path] = []
    preferred_names = [
        "tickets.json",
        "Ticket Dump IS Combined Data Q1 2025.xlsx",
        "Ticket Dump IS Combined Data Q2 2025_.xlsx",
        "Ticket Dump IS Combined Data Q3 2025_.xlsx",
        "Ticket Dump IS Combined Data Q4 2025_.xlsx",
        "2025_INFR_Incident_data.xlsx",
    ]
    for name in preferred_names:
        p = source_root / name
        if p.exists():
            candidates.append(p)
    seen = {p.resolve() for p in candidates}
    for pattern in ["*ticket*.json", "*ticket*.jsonl", "*ticket*.csv", "*ticket*.xlsx", "*incident*.xlsx", "*incident*.csv"]:
        for p in source_root.glob(pattern):
            if p.is_file() and p.resolve() not in seen:
                candidates.append(p)
                seen.add(p.resolve())
    return candidates


def read_rows_from_json(path: Path, max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
    data = safe_read_json(path, default=[])
    if isinstance(data, list):
        iterable = data
    elif isinstance(data, dict):
        iterable = data.get("tickets") or data.get("items") or data.get("records") or data.get("chunks") or [data]
    else:
        iterable = []
    rows: List[Dict[str, Any]] = []
    for item in iterable:
        if isinstance(item, dict):
            rows.append(dict(item))
            if max_rows and len(rows) >= max_rows:
                break
    return rows


def read_rows_from_jsonl(path: Path, max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
            if max_rows and len(rows) >= max_rows:
                break
    return rows


def read_rows_from_csv(path: Path, max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(dict(row))
            if max_rows and len(rows) >= max_rows:
                break
    return rows


def read_rows_from_excel(path: Path, max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
    try:
        import pandas as pd
    except Exception as exc:
        raise RuntimeError("pandas/openpyxl are required for Excel input. Use tickets.json or install pandas openpyxl.") from exc
    rows: List[Dict[str, Any]] = []
    try:
        xls = pd.ExcelFile(path)
        for sheet in xls.sheet_names:
            try:
                df = pd.read_excel(path, sheet_name=sheet, dtype=str).fillna("")
                for item in df.to_dict(orient="records"):
                    item = {str(k): v for k, v in item.items()}
                    item["_source_sheet"] = sheet
                    rows.append(item)
                    if max_rows and len(rows) >= max_rows:
                        return rows
            except Exception:
                continue
    except Exception:
        return []
    return rows


def read_rows(path: Path, max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            return read_rows_from_json(path, max_rows)
        if suffix == ".jsonl":
            return read_rows_from_jsonl(path, max_rows)
        if suffix == ".csv":
            return read_rows_from_csv(path, max_rows)
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            return read_rows_from_excel(path, max_rows)
    except Exception:
        return []
    return []


FIELD_ALIASES = {
    "ticket_id": ["ticket_id", "id", "number", "ticket number", "incident number", "task number", "key", "sys_id", "incident_id"],
    "record_type": ["record_type", "type", "ticket type", "task type", "table", "category type"],
    "title": ["title", "short_description", "short description", "summary", "subject", "incident title", "name", "chunk_title"],
    "description": ["description", "details", "content", "long_description", "long description", "work notes", "comments", "text", "body", "ticket", "chunk_text", "page_content", "raw_text"],
    "resolution": ["resolution", "resolution_notes", "resolution notes", "close_notes", "close notes", "resolved notes", "fix notes", "workaround", "solution"],
    "opened_at": ["opened_at", "opened", "created", "created_at", "created on", "opened on", "start time", "submit date", "opened date"],
    "resolved_at": ["resolved_at", "resolved", "closed", "closed_at", "closed on", "resolved on", "end time", "close date"],
    "assignment_group": ["assignment_group", "assignment group", "support group", "resolver group", "owned by group", "team"],
    "service": ["service", "business service", "application", "app", "product", "service offering"],
    "ci": ["ci", "configuration item", "affected_ci", "affected ci", "cmdb_ci", "host", "server", "device", "resource"],
    "severity": ["severity", "priority", "impact", "urgency"],
    "environment": ["environment", "env", "stage", "prod", "production"],
}


def infer_record_type(blob: str, explicit_type: str = "", ticket_id: str = "") -> str:
    text = normalize_phrase(" ".join([explicit_type, ticket_id, blob]))
    if any(x in text for x in ["change", "chg", "implementation", "release", "deployment"]):
        return "change"
    if any(x in text for x in ["problem", "prb", "known error", "root cause analysis", "rca"]):
        return "problem"
    if any(x in text for x in ["service request", "ritm", "catalog"]):
        return "service_request"
    if re.search(r"\b(req|request)\b", text) and not any(x in text for x in ["failed", "failure", "outage", "degraded"]):
        return "service_request"
    return "incident"


def infer_environment(blob: str) -> str:
    text = f" {normalize_phrase(blob)} "
    if any(x in text for x in [" production ", " prod ", " prd "]):
        return "production"
    if any(x in text for x in [" nonprod ", " non prod ", " staging ", " test ", " uat ", " dev "]):
        return "non-production"
    return "unknown"



# --------------------------------------------------------------------------------------
# Text quality helpers
# --------------------------------------------------------------------------------------

def looks_like_placeholder_text(value: Any) -> bool:
    """Reject pure IDs/chunk labels being treated as incident text.

    V1.3 note:
    Do not reject every short value here. Short titles can be valid when paired
    with a meaningful content field. Placeholder filtering should catch IDs such
    as ticket_504, not suppress valid records with concise titles.
    """
    text = clean_text(value)
    norm = normalize_token(text)
    if not text:
        return True
    if re.fullmatch(r"(ticket|inc|case|chunk|row|record)_?\d+", norm):
        return True
    if re.fullmatch(r"\d+", norm):
        return True
    if norm in {"na", "n_a", "none", "null", "unknown", "blank"}:
        return True
    return False


def has_minimum_operational_text(value: Any) -> bool:
    text = clean_text(value)
    if looks_like_placeholder_text(text):
        return False
    phrase = normalize_phrase(text)
    # Accept descriptive content even when concise, but reject one-word labels.
    return len(phrase) >= 12 and len(phrase.split()) >= 2


def meaningful_ticket_text(*values: Any) -> str:
    """Join only meaningful incident text, excluding pure placeholder IDs/labels."""
    parts: List[str] = []
    for value in values:
        text = clean_text(value)
        if text and has_minimum_operational_text(text):
            parts.append(text)
    return clean_text(" ".join(parts))


def contains_any(blob: str, keywords: Iterable[str]) -> bool:
    norm = normalize_phrase(blob)
    return any(normalize_phrase(kw) in norm for kw in keywords)

def is_low_value_field_name(name: Any) -> bool:
    key = normalize_phrase(name)
    valuable = {
        "title", "summary", "subject", "description", "content", "details",
        "short description", "long description", "work notes", "close notes",
        "comments", "resolution", "resolution notes", "chunk text",
        "page content", "raw text",
    }
    if key in valuable:
        return False

    low_value_terms = [
        "sys id", "row", "index", "source", "file", "sheet",
        "created", "updated", "opened", "closed", "resolved", "date", "time",
        "tenant", "scope", "collection", "score", "embedding", "vector",
        "knowledge scope", "collection name",
    ]
    # Only treat plain 'id'/'number' as low value, not words that merely contain them.
    if key in {"id", "number"}:
        return True
    return any(term == key or key.endswith(" " + term) for term in low_value_terms)


def row_text_fallback(row: Dict[str, Any]) -> str:
    """
    Build meaningful text from descriptive-looking row values.
    This is needed because tickets.json/RAG rows use keys like title/content.
    """
    priority_keys = [
        "title", "summary", "subject", "short_description", "short description",
        "content", "description", "details", "chunk_text", "page_content",
        "raw_text", "work_notes", "work notes", "close_notes", "close notes",
        "resolution", "resolution_notes", "comments",
    ]

    parts: List[str] = []
    lower = {str(k).lower().strip(): v for k, v in row.items()}

    for key in priority_keys:
        value_text = clean_text(lower.get(key))
        if has_minimum_operational_text(value_text):
            parts.append(value_text)

    # Fallback across all columns if priority fields did not produce anything.
    if not parts:
        for key, value in row.items():
            key_text = clean_text(key)
            value_text = clean_text(value)
            if not value_text:
                continue
            if key_text.startswith("_"):
                continue
            if is_low_value_field_name(key_text):
                continue
            if has_minimum_operational_text(value_text):
                parts.append(value_text)

    # De-duplicate while preserving order.
    seen = set()
    deduped = []
    for part in parts:
        norm = normalize_phrase(part)
        if norm not in seen:
            deduped.append(part)
            seen.add(norm)
    return clean_text(" ".join(deduped))


def has_meaningful_operational_text(row: Dict[str, Any], *values: Any) -> bool:
    if meaningful_ticket_text(*values):
        return True
    return bool(row_text_fallback(row))


def normalize_ticket_row(row: Dict[str, Any], source_file: Path) -> Dict[str, Any]:
    title = first_nonempty(row, FIELD_ALIASES["title"])
    description = first_nonempty(row, FIELD_ALIASES["description"])
    resolution = first_nonempty(row, FIELD_ALIASES["resolution"])

    if not title:
        title = first_nonempty(row, ["incident_title", "name", "chunk_title"])
    if not description:
        description = first_nonempty(row, ["content", "ticket", "chunk_text", "page_content", "raw_text", "text"])

    # Avoid treating generated IDs/chunk labels such as ticket_504 as incident symptoms.
    if looks_like_placeholder_text(title):
        title = ""
    if looks_like_placeholder_text(description):
        description = ""

    # RAG outputs may keep useful text in non-standard fields. Use a broad fallback
    # only when title/description are not meaningful.
    fallback_text = ""
    if not meaningful_ticket_text(title, description, resolution):
        fallback_text = row_text_fallback(row)
        if fallback_text:
            if not title:
                title = fallback_text[:140]
            if not description:
                description = fallback_text

    ticket_id = first_nonempty(row, FIELD_ALIASES["ticket_id"]) or stable_id("ticket", source_file.name, title, description, resolution)
    record_type = first_nonempty(row, FIELD_ALIASES["record_type"])
    blob = " ".join([title, description, resolution, fallback_text])

    return {
        "ticket_id": ticket_id,
        "record_type": infer_record_type(blob, record_type, ticket_id),
        "title": title or description[:140],
        "description": description,
        "resolution": "" if looks_like_placeholder_text(resolution) else resolution,
        "opened_at": parse_datetime_safe(first_nonempty(row, FIELD_ALIASES["opened_at"])),
        "resolved_at": parse_datetime_safe(first_nonempty(row, FIELD_ALIASES["resolved_at"])),
        "assignment_group": first_nonempty(row, FIELD_ALIASES["assignment_group"]),
        "service": first_nonempty(row, FIELD_ALIASES["service"]),
        "service_offering": first_nonempty(row, ["service_offering", "service offering"]),
        "affected_ci": first_nonempty(row, FIELD_ALIASES["ci"]),
        "severity": first_nonempty(row, FIELD_ALIASES["severity"]),
        "environment": first_nonempty(row, FIELD_ALIASES["environment"]) or infer_environment(blob),
        "source_file": str(source_file),
        "source_sheet": clean_text(row.get("_source_sheet")),
        "raw": row,
    }


CLASSIFICATION_RULES = [
    ("batch_job_failure", "compute", ["tws", "tws job", "job failed", "job_failed", "abended", "abend", "oppwait", "waiting on dataset", "waiting on datasets", "dataset"], "batch_job_or_scheduled_task_collision", "batch scheduler job failed or waited on an upstream dataset/dependency", "batch_schedule_or_dependency_change", "recurring_batch_job_failure_or_delay", "batch_rerun_or_dependency_release_request", "batch_schedule_or_dependency_drift"),
    ("batch_job_delay", "compute", ["job late", "job_late", "late in tws", "not started", "waiting on dataset", "waiting on datasets", "oppwait"], "batch_job_or_scheduled_task_collision", "batch scheduler job delayed by an upstream dataset/dependency", "batch_schedule_or_dependency_change", "recurring_batch_job_failure_or_delay", "batch_rerun_or_dependency_release_request", "batch_schedule_or_dependency_drift"),
    ("api_gateway_timeout", "application_network", ["api", "gateway", "504", "timeout", "http 504", "gateway timeout"], "firewall_policy_change_blocking_downstream_port", "firewall policy change blocked downstream service port", "firewall_policy_change", "recurring_api_gateway_timeout_after_network_policy_change", "downstream_port_access_request", "firewall_policy_drift"),
    ("vpn_client_failure", "network", ["vpn", "mfa", "radius", "password reset", "remote access", "authentication"], "authentication_or_identity_provider_dependency", "cached credential or identity-provider authentication issue after password reset", "identity_policy_change", "recurring_vpn_login_failure_after_password_reset", "password_reset_and_vpn_access_revalidation", "identity_policy_drift"),
    ("cpu_saturation", "compute", ["cpu", "processor", "utilization", "saturation", "high cpu"], "batch_job_or_scheduled_task_collision", "scheduled batch job collision with endpoint security scan", "batch_schedule_change", "recurring_cpu_saturation_from_batch_and_endpoint_scan_overlap", "batch_window_adjustment_request", "operational_schedule_drift"),
    ("memory_pressure", "compute", ["memory", "oom", "out of memory", "heap", "swap"], "resource_pressure_or_capacity_constraint", "memory pressure or out-of-memory condition degraded the service", "capacity_or_runtime_config_change", "recurring_memory_pressure", "capacity_increase_or_restart_request", "runtime_configuration_drift"),
    ("disk_full", "storage", ["disk", "space", "filesystem", "file system", "volume full", "no space"], "storage_capacity_threshold_breach", "disk or filesystem capacity threshold was breached", "storage_capacity_or_cleanup_change", "recurring_storage_capacity_issue", "storage_cleanup_or_expansion_request", "storage_capacity_drift"),
    ("certificate_expiry_warning", "security", ["certificate", "cert", "ssl", "tls", "expiry", "expired"], "certificate_expiry_or_rotation_required", "certificate expiry or TLS/SSL rotation issue impacted service trust", "certificate_rotation_change", "recurring_certificate_expiry", "certificate_renewal_request", "certificate_inventory_drift"),
    ("dns_issue", "network", ["dns", "resolver", "lookup", "name resolution", "resolution delay"], "dns_or_ip_change_after_workload_migration", "DNS or resolver change caused name resolution failure or delay", "dns_or_network_change", "recurring_dns_resolution_issue", "dns_record_or_access_request", "dns_or_ip_mapping_drift"),
    ("network_connectivity_issue", "network", ["network", "connectivity", "firewall", "global connectivity", "connection", "unreachable", "latency"], "network_path_or_firewall_connectivity_issue", "network path, firewall, or connectivity dependency affected the service", "network_or_firewall_change", "recurring_network_connectivity_issue", "network_access_or_firewall_request", "network_policy_or_route_drift"),
    ("file_transfer_failure", "integration", ["ftp", "sftp", "file transfer", "goanywhere", "transfer failure", "file failed"], "file_transfer_dependency_or_endpoint_failure", "file transfer endpoint, credential, or dependency failed", "file_transfer_config_or_credential_change", "recurring_file_transfer_failure", "file_transfer_access_or_rerun_request", "file_transfer_configuration_drift"),
    ("vdi_citrix_connectivity_issue", "end_user_compute", ["citrix", "vdi", "virtual desktop", "workspace", "desktop connectivity"], "virtual_desktop_access_or_broker_issue", "VDI/Citrix broker, profile, or network path affected desktop connectivity", "vdi_or_endpoint_access_change", "recurring_vdi_connectivity_issue", "vdi_access_or_profile_request", "vdi_configuration_drift"),
    ("database_performance_issue", "database", ["database", "sql", "oracle", "deadlock", "query", "connection pool"], "downstream_dependency_latency_or_outage", "database latency or contention degraded application transaction response", "database_or_application_change", "recurring_database_performance_issue", "database_access_or_capacity_request", "database_configuration_drift"),
]

CHECKS_ACTIONS = {
    "batch_job_failure": (["validate TWS job status and abend code", "check upstream dataset availability", "inspect predecessor/dependent job completion", "review scheduler calendar and rerun policy", "confirm application/batch owner escalation path"], ["rerun failed job after dependency validation", "release or correct waiting dataset dependency", "resubmit scheduled job where approved", "escalate to batch operations/application owner if dependency is unresolved"]),
    "batch_job_delay": (["validate TWS late-job status", "check upstream dataset availability", "inspect predecessor/dependent job completion", "review scheduler calendar and expected start window", "confirm application/batch owner escalation path"], ["release or correct waiting dataset dependency", "rerun or resubmit delayed job where approved", "adjust scheduler dependency/calendar after validation", "escalate to batch operations/application owner if delay persists"]),
    "api_gateway_timeout": (["validate recent firewall/security-group changes", "test source-to-destination port connectivity", "compare deny logs with incident window", "confirm dependency path from topology", "validate recovery with synthetic transaction"], ["validate firewall rule", "restore downstream port access", "confirm recovery using synthetic transaction"]),
    "vpn_client_failure": (["check IdP/MFA health", "review conditional access changes", "validate LDAP/RADIUS bind status", "inspect VPN client logs", "inspect token/certificate errors"], ["clear cached VPN credentials", "validate MFA/IdP health", "refresh VPN client profile"]),
    "cpu_saturation": (["review scheduled job calendar", "check top process during incident", "review endpoint security scan schedule", "compare scan window with incident window", "inspect batch logs"], ["review scheduled job window", "check top CPU processes", "reschedule scan or batch job"]),
    "memory_pressure": (["check memory utilization trend", "identify top memory-consuming process", "inspect OOM or heap errors", "validate recent runtime/config changes"], ["restart or recycle affected process after approval", "increase memory or tune runtime settings", "escalate to application/platform owner if memory leak is suspected"]),
    "disk_full": (["check filesystem utilization", "identify largest files/directories", "review backup/log retention", "validate volume expansion options"], ["clean approved temporary/log files", "expand filesystem or volume after approval", "adjust retention policy if recurring"]),
    "certificate_expiry_warning": (["inspect certificate expiry date", "validate certificate chain", "check TLS handshake errors", "confirm certificate inventory owner"], ["renew or rotate certificate", "update trust chain where required", "validate service recovery with TLS check"]),
    "dns_issue": (["validate DNS lookup from affected subnet", "compare DNS records before and after change", "check resolver latency and timeout metrics"], ["correct DNS or endpoint mapping", "flush stale resolver cache where approved", "route queries to healthy resolver"]),
    "network_connectivity_issue": (["test source-to-destination connectivity", "review recent firewall/routing changes", "check packet loss or latency", "validate impacted subnet/path"], ["restore approved network/firewall path", "rollback or correct route/policy if validated", "confirm recovery with connectivity test"]),
    "file_transfer_failure": (["validate source and destination endpoint reachability", "check FTP/SFTP or GoAnywhere job logs", "verify credentials/keys and file permissions", "confirm upstream/downstream file availability"], ["rerun transfer after dependency validation", "restore credential/key or permission", "restart transfer workflow where approved"]),
    "vdi_citrix_connectivity_issue": (["check Citrix/VDI broker health", "validate user session/profile state", "test desktop connectivity path", "review endpoint/client errors"], ["reset affected session/profile where approved", "restore VDI broker or gateway dependency", "escalate to EUC/VDI resolver group if persistent"]),
    "database_performance_issue": (["inspect database wait events", "review deadlock logs", "check long running queries", "validate connection pool saturation"], ["tune or terminate blocking transaction after approval", "adjust connection pool configuration", "optimize query plan"]),
}


def generic_inference() -> Dict[str, Any]:
    return {
        "classification": "general_operational_issue",
        "domain": "general",
        "associated_scenario": "downstream_dependency_latency_or_outage",
        "root_cause": "general operational degradation requiring engineer review",
        "recommended_checks": ["review incident work notes", "check related changes", "validate affected service health"],
        "recommended_actions": ["apply documented resolution if validated", "escalate to resolver group if unresolved"],
        "change_correlation": "unknown_or_not_detected",
        "problem_correlation": "unknown_or_not_detected",
        "service_request_correlation": "unknown_or_not_detected",
        "drift_type": "unknown_or_not_detected",
        "classification_confidence": 0.35,
    }


def rule_to_inference(rule: Tuple[Any, ...], confidence: float) -> Dict[str, Any]:
    classification, domain, _, scenario, root, change, problem, req, drift = rule
    checks, actions = CHECKS_ACTIONS[classification]
    return {
        "classification": classification,
        "domain": domain,
        "associated_scenario": scenario,
        "root_cause": root,
        "recommended_checks": checks,
        "recommended_actions": actions,
        "change_correlation": change,
        "problem_correlation": problem,
        "service_request_correlation": req,
        "drift_type": drift,
        "classification_confidence": confidence,
    }


def infer_classification(ticket: Dict[str, Any]) -> Dict[str, Any]:
    """
    Conservative deterministic classifier.

    V1.1 fix:
    Do not classify as api_gateway_timeout merely because placeholder IDs such
    as ticket_504 contain "504". API timeout now requires API/gateway context
    plus timeout/504 evidence in meaningful ticket text.
    """
    meaningful_blob = meaningful_ticket_text(
        ticket.get("title", ""),
        ticket.get("description", ""),
        ticket.get("resolution", ""),
    )
    context_blob = normalize_phrase(" ".join([
        meaningful_blob,
        ticket.get("assignment_group", ""),
        ticket.get("service", ""),
    ]))

    if not meaningful_blob:
        return generic_inference()

    rules_by_class = {r[0]: r for r in CLASSIFICATION_RULES}

    if contains_any(context_blob, ["tws_job_failed", "tws job failed", "job failed", "job_failed", "failed/abended", "abended", "abend"]):
        return rule_to_inference(rules_by_class["batch_job_failure"], 0.78)

    if contains_any(context_blob, ["tws_job_late", "tws job late", "job late", "job_late", "not started", "oppwait", "waiting on dataset", "waiting on datasets"]):
        return rule_to_inference(rules_by_class["batch_job_delay"], 0.76)

    if (
        contains_any(context_blob, ["api", "gateway", "apigee", "kong", "load balancer", "lb"])
        and contains_any(context_blob, ["timeout", "504", "gateway timeout", "http 504", "5xx"])
    ):
        return rule_to_inference(rules_by_class["api_gateway_timeout"], 0.78)

    if (
        contains_any(context_blob, ["vpn", "remote access", "anyconnect", "globalprotect", "zscaler"])
        and contains_any(context_blob, ["mfa", "radius", "password", "auth", "login", "connect", "connection", "failure", "failed"])
    ):
        return rule_to_inference(rules_by_class["vpn_client_failure"], 0.76)

    if (
        contains_any(context_blob, ["cpu", "processor"])
        and contains_any(context_blob, ["high", "utilization", "saturation", "spike", "threshold", "95", "97", "performance"])
    ):
        return rule_to_inference(rules_by_class["cpu_saturation"], 0.76)

    if (
        contains_any(context_blob, ["dns", "resolver", "name resolution"])
        and contains_any(context_blob, ["lookup", "resolution", "resolve", "latency", "timeout", "failure", "failed"])
    ):
        return rule_to_inference(rules_by_class["dns_issue"], 0.74)

    if (
        contains_any(context_blob, ["database", "sql", "oracle", "db", "mainframe database"])
        and contains_any(context_blob, ["latency", "deadlock", "query", "connection pool", "slow", "timeout", "contention", "performance"])
    ):
        return rule_to_inference(rules_by_class["database_performance_issue"], 0.74)

    if contains_any(context_blob, ["ftp", "sftp", "file transfer", "goanywhere", "transfer failure", "ftp failure"]):
        return rule_to_inference(rules_by_class["file_transfer_failure"], 0.72)

    if contains_any(context_blob, ["citrix", "vdi", "virtual desktop", "virtual desktops", "workspace"]):
        return rule_to_inference(rules_by_class["vdi_citrix_connectivity_issue"], 0.72)

    # Capacity/security families should be checked before broad network terms;
    # otherwise values like "Networked Storage" can be misread as a network case.
    if contains_any(context_blob, ["disk", "space", "filesystem", "file system", "volume full", "no space", "nas storage", "server backups"]):
        return rule_to_inference(rules_by_class["disk_full"], 0.70)

    if contains_any(context_blob, ["memory", "oom", "out of memory", "heap", "swap"]):
        return rule_to_inference(rules_by_class["memory_pressure"], 0.70)

    if contains_any(context_blob, ["certificate", " cert ", "ssl", "tls", "expiry", "expired"]):
        return rule_to_inference(rules_by_class["certificate_expiry_warning"], 0.70)

    if contains_any(context_blob, ["network", "connectivity", "firewall", "global connectivity", "nsi infrastructure", "end user network", "connection"]):
        return rule_to_inference(rules_by_class["network_connectivity_issue"], 0.70)

    return generic_inference()


def related_record_text(ticket: Dict[str, Any]) -> str:
    return normalize_phrase(" ".join([ticket.get("ticket_id", ""), ticket.get("title", ""), ticket.get("description", ""), ticket.get("resolution", ""), ticket.get("service", ""), ticket.get("affected_ci", ""), ticket.get("assignment_group", "")]))


def related_score(primary: Dict[str, Any], other: Dict[str, Any]) -> float:
    p_tokens = set(x for x in related_record_text(primary).split() if len(x) > 3)
    o_tokens = set(x for x in related_record_text(other).split() if len(x) > 3)
    if not p_tokens or not o_tokens:
        return 0.0
    overlap = len(p_tokens & o_tokens) / max(len(p_tokens | o_tokens), 1)
    boost = 0.0
    for field in ["service", "affected_ci", "assignment_group"]:
        if clean_text(primary.get(field)) and clean_text(primary.get(field)) == clean_text(other.get(field)):
            boost += 0.15
    return min(1.0, overlap + boost)


def select_related(primary: Dict[str, Any], records_by_type: Dict[str, List[Dict[str, Any]]], record_type: str, limit: int = 2) -> List[Dict[str, Any]]:
    scored = [(related_score(primary, other), other) for other in records_by_type.get(record_type, [])]
    scored = [(s, r) for s, r in scored if s >= 0.08]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[:limit]]


def compact_related_record(ticket: Dict[str, Any], fallback_type: str) -> Dict[str, Any]:
    return {
        "id": ticket.get("ticket_id") or stable_id("anon", ticket.get("title")),
        "type": ticket.get("record_type") or fallback_type,
        "title": ticket.get("title"),
        "description": (ticket.get("description") or "")[:500],
        "opened_at": ticket.get("opened_at"),
        "resolved_at": ticket.get("resolved_at"),
        "service": ticket.get("service"),
        "service_offering": ticket.get("service_offering"),
        "affected_ci": ticket.get("affected_ci"),
        "assignment_group": ticket.get("assignment_group"),
        "source_file": ticket.get("source_file"),
    }


def metric_for_classification(cls: str) -> str:
    return {
        "cpu_saturation": "cpu_percent",
        "memory_pressure": "memory_percent",
        "disk_full": "filesystem_used_percent",
        "certificate_expiry_warning": "certificate_days_to_expiry",
        "api_gateway_timeout": "api_gateway_5xx_rate",
        "vpn_client_failure": "vpn_auth_failure_rate",
        "dns_issue": "dns_lookup_latency_ms",
        "network_connectivity_issue": "network_packet_loss_percent",
        "file_transfer_failure": "file_transfer_failure_count",
        "vdi_citrix_connectivity_issue": "vdi_connection_failure_rate",
        "database_performance_issue": "db_query_latency_ms",
        "batch_job_failure": "batch_job_failure_count",
        "batch_job_delay": "batch_job_delay_minutes",
    }.get(normalize_token(cls), "service_health_score")


def dependency_path_for_classification(cls: str) -> str:
    return {
        "api_gateway_timeout": "api_gateway -> firewall_policy -> downstream_service",
        "vpn_client_failure": "vpn_client -> vpn_gateway -> radius_service -> identity_provider -> mfa_provider",
        "cpu_saturation": "batch_scheduler -> app_server -> endpoint_security_agent",
        "memory_pressure": "application_process -> runtime_memory -> host_capacity",
        "disk_full": "application_logs -> filesystem -> storage_volume",
        "certificate_expiry_warning": "client -> tls_endpoint -> certificate_inventory",
        "dns_issue": "affected_host -> dns_resolver -> authoritative_dns",
        "network_connectivity_issue": "source_endpoint -> network_path -> firewall_or_route -> destination_service",
        "file_transfer_failure": "source_system -> file_transfer_platform -> destination_endpoint",
        "vdi_citrix_connectivity_issue": "user_endpoint -> citrix_gateway -> vdi_broker -> virtual_desktop",
        "database_performance_issue": "application -> connection_pool -> database",
        "batch_job_failure": "batch_scheduler -> predecessor_job -> dataset_dependency -> target_job",
        "batch_job_delay": "batch_scheduler -> predecessor_job -> dataset_dependency -> target_job",
    }.get(normalize_token(cls), "affected_user_or_service -> affected_ci")


def approved_state_for_drift(drift_type: str) -> str:
    return {
        "firewall_policy_drift": "approved policy allows required downstream service port",
        "identity_policy_drift": "identity policy and MFA rules match approved access design",
        "operational_schedule_drift": "batch and endpoint security scan windows do not overlap",
        "dns_or_ip_mapping_drift": "DNS/IP mapping matches approved service endpoint",
        "database_configuration_drift": "database configuration matches approved performance baseline",
        "network_policy_or_route_drift": "network routes/firewall policies match approved connectivity design",
        "file_transfer_configuration_drift": "file transfer endpoint, credential, and schedule match approved runbook",
        "vdi_configuration_drift": "VDI broker, profile, and access configuration match approved design",
        "storage_capacity_drift": "storage capacity and retention policy match approved baseline",
        "runtime_configuration_drift": "runtime memory configuration matches approved baseline",
        "certificate_inventory_drift": "certificate inventory and expiry state match approved rotation plan",
        "batch_schedule_or_dependency_drift": "batch schedule, predecessor jobs, and dataset dependencies match approved runbook",
    }.get(normalize_token(drift_type), "approved state matches intended configuration")


def observed_state_for_drift(drift_type: str) -> str:
    return {
        "firewall_policy_drift": "observed connectivity indicates required downstream port may be blocked",
        "identity_policy_drift": "observed authentication behavior differs from approved identity policy",
        "operational_schedule_drift": "observed batch and scan activities overlap during incident window",
        "dns_or_ip_mapping_drift": "observed DNS/IP resolution differs from expected endpoint mapping",
        "database_configuration_drift": "observed database behavior differs from approved baseline",
        "network_policy_or_route_drift": "observed connectivity differs from approved network route or firewall policy",
        "file_transfer_configuration_drift": "observed transfer behavior differs from approved file transfer configuration",
        "vdi_configuration_drift": "observed VDI access behavior differs from approved desktop configuration",
        "storage_capacity_drift": "observed storage capacity differs from approved threshold baseline",
        "runtime_configuration_drift": "observed runtime memory behavior differs from approved baseline",
        "certificate_inventory_drift": "observed certificate state differs from approved inventory/rotation plan",
        "batch_schedule_or_dependency_drift": "observed batch job status or dataset dependency differs from approved runbook",
    }.get(normalize_token(drift_type), "observed state differs from intended configuration")


def build_timeline_events(primary: Dict[str, Any], inf: Dict[str, Any]) -> List[str]:
    cls = normalize_token(inf.get("classification"))
    if cls == "api_gateway_timeout":
        return ["T-24h service request fulfilled for downstream access", "T-4h firewall policy change implemented", "T-10m 5xx and downstream connectivity failures spike", "T0 API gateway timeout incident opened", "T+60m downstream port access restored and recovery validated"]
    if cls == "vpn_client_failure":
        return ["T-24h password reset or VPN access request completed", "T-4h identity/MFA policy changed or validated", "T-10m VPN authentication failures spike", "T0 VPN access incident opened", "T+45m cached credentials cleared and VPN client profile refreshed"]
    if cls == "cpu_saturation":
        return ["T-24h batch schedule or workload request approved", "T-2h batch or endpoint scan schedule active", "T0 CPU telemetry threshold breached", "T+30m overlap or high process identified", "T+2h batch or scan schedule adjusted"]
    if cls in {"batch_job_failure", "batch_job_delay"}:
        return ["T-24h batch schedule and dependency runbook in effect", "T-30m predecessor job or dataset dependency not complete", "T0 TWS batch job failed, abended, or missed expected start", "T+30m scheduler logs and dependency status reviewed", "T+60m job rerun/resubmitted after dependency validation"]
    return ["T-24h related operational context reviewed", "T0 incident opened", "T+1h resolution or workaround applied"]


def build_synthetic_context(primary: Dict[str, Any], inf: Dict[str, Any], related: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    changes = [compact_related_record(x, "change") for x in related.get("change", [])]
    problems = [compact_related_record(x, "problem") for x in related.get("problem", [])]
    service_requests = [compact_related_record(x, "service_request") for x in related.get("service_request", [])]
    if not changes and inf.get("change_correlation") != "unknown_or_not_detected":
        changes = [{"id": stable_id("ANON_CHG", primary.get("ticket_id"), inf.get("change_correlation")), "type": inf.get("change_correlation"), "description": f"Potential related {inf.get('change_correlation')} near incident window.", "state": "candidate_context_placeholder", "source": "synthetic_context_placeholder"}]
    if not problems and inf.get("problem_correlation") != "unknown_or_not_detected":
        problems = [{"id": stable_id("ANON_PRB", primary.get("ticket_id"), inf.get("problem_correlation")), "description": f"Potential recurring pattern: {inf.get('problem_correlation')}.", "known_error": False, "source": "synthetic_context_placeholder"}]
    if not service_requests and inf.get("service_request_correlation") != "unknown_or_not_detected":
        service_requests = [{"id": stable_id("ANON_REQ", primary.get("ticket_id"), inf.get("service_request_correlation")), "description": f"Potential related request: {inf.get('service_request_correlation')}.", "status": "candidate_context_placeholder", "source": "synthetic_context_placeholder"}]
    cls = inf.get("classification")
    return {
        "changes": changes,
        "problems": problems,
        "service_requests": service_requests,
        "telemetry": [{"metric": metric_for_classification(cls), "value": 95.0 if normalize_token(cls) == "cpu_saturation" else 12.0, "timestamp": primary.get("opened_at"), "signal": f"{cls} signal", "source": "synthetic_context_placeholder"}],
        "cmdb_topology": [{"ci": primary.get("affected_ci") or primary.get("service") or "unknown_ci", "depends_on": dependency_path_for_classification(cls).split(" -> "), "dependency_path": dependency_path_for_classification(cls), "source": "synthetic_context_placeholder"}],
        "drift_snapshots": [{"snapshot_id": stable_id("ANON_DRIFT", primary.get("ticket_id"), inf.get("drift_type")), "timestamp": primary.get("opened_at"), "approved_state": approved_state_for_drift(inf.get("drift_type")), "observed_state": observed_state_for_drift(inf.get("drift_type")), "drift_detected": inf.get("drift_type") != "unknown_or_not_detected", "drift_type": inf.get("drift_type"), "drift_severity": "medium", "source": "synthetic_context_placeholder"}] if inf.get("drift_type") != "unknown_or_not_detected" else [],
    }


def build_drift_context(inf: Dict[str, Any]) -> Dict[str, Any]:
    drift = inf.get("drift_type")
    if not drift or drift == "unknown_or_not_detected":
        return {"enabled": False, "drift_detected": False, "drift_type": "unknown_or_not_detected"}
    return {"enabled": True, "approved_state": {"summary": approved_state_for_drift(drift)}, "observed_state_before_incident": {"summary": observed_state_for_drift(drift)}, "observed_state_after_resolution": {"summary": "observed state returned to expected or accepted condition after remediation"}, "drift_detected": True, "drift_type": drift, "drift_severity": "medium"}


def build_case(primary: Dict[str, Any], related_by_type: Dict[str, List[Dict[str, Any]]], tenant_id: str) -> Dict[str, Any]:
    inf = infer_classification(primary)
    derived_family = primary.get("_derived_family") or derive_incident_family(primary, inf)
    related = {"change": select_related(primary, related_by_type, "change"), "problem": select_related(primary, related_by_type, "problem"), "service_request": select_related(primary, related_by_type, "service_request")}
    related_records = build_synthetic_context(primary, inf, related)
    title = primary.get("title") or primary.get("description", "")[:140]
    ticket_text = meaningful_ticket_text(primary.get("title", ""), primary.get("description", ""))
    if not ticket_text:
        ticket_text = inf.get("root_cause") or title or f"Historical operational issue {primary.get('ticket_id')}"
    return {
        "case_id": stable_id("bt", tenant_id, primary.get("ticket_id"), inf.get("classification")),
        "tenant_id": tenant_id,
        "mode": "client_local_generated",
        "source": {"original_ticket_id": primary.get("ticket_id"), "source_file": primary.get("source_file"), "source_sheet": primary.get("source_sheet"), "record_type": primary.get("record_type"), "derived_family": derived_family, "generated_by": "historical_backtest_case_builder.py"},
        "input": {"title": title, "ticket_text": ticket_text, "environment": primary.get("environment") or "unknown", "severity": primary.get("severity") or "unknown", "affected_ci": primary.get("affected_ci") or primary.get("service") or "", "service": primary.get("service"), "service_offering": primary.get("service_offering")},
        "primary_record": {"type": primary.get("record_type") or "incident", "id": primary.get("ticket_id"), "opened_at": primary.get("opened_at"), "resolved_at": primary.get("resolved_at"), "short_description": title, "description": primary.get("description"), "environment": primary.get("environment") or "unknown", "affected_ci": primary.get("affected_ci") or primary.get("service") or "", "service": primary.get("service"), "service_offering": primary.get("service_offering"), "severity": primary.get("severity") or "unknown", "resolution_notes": primary.get("resolution")},
        "context_window": {"before_hours": 24, "after_hours": 24},
        "related_records": related_records,
        "drift_context": build_drift_context(inf),
        "expected": {"classification": inf.get("classification"), "domain": inf.get("domain"), "associated_scenario": inf.get("associated_scenario"), "root_cause": primary.get("resolution") if not looks_like_placeholder_text(primary.get("resolution")) else inf.get("root_cause"), "recommended_checks": inf.get("recommended_checks"), "recommended_actions": inf.get("recommended_actions"), "change_correlation": inf.get("change_correlation"), "problem_correlation": inf.get("problem_correlation"), "service_request_correlation": inf.get("service_request_correlation"), "drift_detected": inf.get("drift_type") != "unknown_or_not_detected", "drift_type": inf.get("drift_type"), "timeline_events": build_timeline_events(primary, inf), "classification_confidence": inf.get("classification_confidence")},
        "scoring_weights": {"classification_match": 0.20, "domain_match": 0.08, "root_cause_match": 0.16, "recommended_action_match": 0.16, "recommended_check_match": 0.08, "associated_scenario_match": 0.14, "retrieval_quality": 0.04, "change_correlation_match": 0.05, "problem_correlation_match": 0.03, "service_request_correlation_match": 0.03, "drift_correlation_match": 0.03},
        "tags": ["generated", derived_family, inf.get("classification"), inf.get("domain")],
    }




# --------------------------------------------------------------------------------------
# Postgres client_* table loader
# --------------------------------------------------------------------------------------

def pg_value(value: Any) -> str:
    """Return a clean string for DB values without leaking None/NaN style tokens."""
    text = clean_text(value)
    if text.lower() in {"none", "nan", "nat", "null"}:
        return ""
    return text


def related_text_from_pg(row: Dict[str, Any]) -> str:
    return normalize_phrase(" ".join([
        pg_value(row.get("short_description")),
        pg_value(row.get("description")),
        pg_value(row.get("service")),
        pg_value(row.get("service_offering")),
        pg_value(row.get("assignment_group")),
        pg_value(row.get("category")),
    ]))


def postgres_related_score(primary: Dict[str, Any], other: Dict[str, Any]) -> float:
    """Lightweight relationship score across incident/change/request/problem tables."""
    score = 0.0
    for field, weight in [("service", 0.28), ("service_offering", 0.28), ("assignment_group", 0.18)]:
        a = normalize_phrase(primary.get(field))
        b = normalize_phrase(other.get(field))
        if a and b and a == b:
            score += weight

    p_tokens = {x for x in related_text_from_pg(primary).split() if len(x) > 3}
    o_tokens = {x for x in related_text_from_pg(other).split() if len(x) > 3}
    if p_tokens and o_tokens:
        score += min(0.26, len(p_tokens & o_tokens) / max(len(p_tokens | o_tokens), 1))
    return min(1.0, score)


def compact_postgres_related(row: Dict[str, Any], record_type: str, score: float) -> Dict[str, Any]:
    rid = row.get("change_id") or row.get("request_id") or row.get("problem_id") or row.get("ticket_id") or ""
    return {
        "id": pg_value(rid),
        "type": record_type,
        "title": pg_value(row.get("short_description"))[:250],
        "description": pg_value(row.get("description"))[:500],
        "service": pg_value(row.get("service")),
        "service_offering": pg_value(row.get("service_offering")),
        "assignment_group": pg_value(row.get("assignment_group")),
        "opened_at": pg_value(row.get("opened_at") or row.get("created_at_source") or row.get("planned_start_date")),
        "resolved_at": pg_value(row.get("closed_at") or row.get("actual_end_date")),
        "_match_score": round(score, 4),
    }


def find_postgres_related(primary: Dict[str, Any], candidates: List[Dict[str, Any]], record_type: str, limit: int = 5) -> List[Dict[str, Any]]:
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for candidate in candidates:
        score = postgres_related_score(primary, candidate)
        if score > 0.0:
            scored.append((score, candidate))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [compact_postgres_related(row, record_type, score) for score, row in scored[:limit]]


def infer_postgres_record_type(prefix: str, explicit: Any = "") -> str:
    explicit_norm = normalize_token(explicit)
    if explicit_norm in {"change", "chg"} or prefix == "change":
        return "change"
    if explicit_norm in {"problem", "prb"} or prefix == "problem":
        return "problem"
    if explicit_norm in {"request", "service_request", "ritm", "req"} or prefix == "request":
        return "service_request"
    return "incident"


def postgres_ticket_to_record(row: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
    title = pg_value(row.get("short_description"))
    service_blob = " ".join([pg_value(row.get("service")), pg_value(row.get("service_offering"))])
    description = title
    return {
        "ticket_id": pg_value(row.get("ticket_id")) or stable_id("pg_ticket", title, row.get("opened_at")),
        "record_type": "incident",
        "title": title,
        "description": description,
        "resolution": "",
        "opened_at": pg_value(row.get("opened_at")),
        "resolved_at": pg_value(row.get("resolved_at")),
        "assignment_group": pg_value(row.get("assignment_group")),
        "service": pg_value(row.get("service")),
        "service_offering": pg_value(row.get("service_offering")),
        "affected_ci": pg_value(row.get("service_offering")) or pg_value(row.get("service")),
        "severity": pg_value(row.get("priority")),
        "environment": infer_environment(" ".join([title, service_blob])),
        "source_file": pg_value(row.get("source_file")) or "postgres:client_ticket_records",
        "source_sheet": pg_value(row.get("source_sheet")),
        "raw": dict(row),
    }


def postgres_related_to_record(row: Dict[str, Any], tenant_id: str, record_type: str) -> Dict[str, Any]:
    title = pg_value(row.get("short_description")) or pg_value(row.get("description"))[:140]
    rid = row.get("change_id") or row.get("request_id") or row.get("problem_id") or stable_id("pg_related", record_type, title)
    return {
        "ticket_id": pg_value(rid),
        "record_type": record_type,
        "title": title,
        "description": pg_value(row.get("description")) or title,
        "resolution": pg_value(row.get("resolution") or row.get("root_cause")),
        "opened_at": pg_value(row.get("opened_at") or row.get("created_at_source") or row.get("planned_start_date")),
        "resolved_at": pg_value(row.get("closed_at") or row.get("actual_end_date")),
        "assignment_group": pg_value(row.get("assignment_group")),
        "service": pg_value(row.get("service")),
        "service_offering": pg_value(row.get("service_offering")),
        "affected_ci": pg_value(row.get("service_offering")) or pg_value(row.get("service")),
        "severity": pg_value(row.get("priority")),
        "environment": infer_environment(" ".join([title, pg_value(row.get("service")), pg_value(row.get("service_offering"))])),
        "source_file": f"postgres:client_{record_type}_records",
        "source_sheet": "postgres",
        "raw": dict(row),
    }


def load_historical_records_from_postgres(tenant_id: str, limit: int = DEFAULT_LIMIT) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Load historical backtest source records from tenant-aware Postgres client_* tables.

    This intentionally reads the new structured client tables created by
    sync_client_rag_to_postgres.py instead of the legacy placeholder-only
    data/rag/tickets.json.
    """
    try:
        import os
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception as exc:
        raise RuntimeError("psycopg2 is required for --source postgres") from exc

    summary: Dict[str, Any] = {
        "source_root": "postgres",
        "candidate_files": [],
        "file_summaries": [],
        "postgres_tables": {},
        "record_count": 0,
    }

    conn = psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "aiops"),
        user=os.getenv("PGUSER", "aiops_user"),
        password=os.getenv("PGPASSWORD", "StrongPassword123"),
    )

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    ticket_id,
                    priority,
                    caller,
                    assignment_group,
                    opened_at,
                    resolved_at,
                    short_description,
                    service,
                    service_offering,
                    channel,
                    record_type,
                    source_file,
                    source_sheet,
                    source_row,
                    raw_json
                FROM client_ticket_records
                WHERE tenant_id = %s
                  AND short_description IS NOT NULL
                  AND TRIM(short_description) <> ''
                ORDER BY opened_at NULLS LAST, ticket_id
                LIMIT %s
                """,
                (tenant_id, limit),
            )
            tickets = [dict(x) for x in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    change_id,
                    change_type,
                    state,
                    short_description,
                    created_at_source,
                    planned_start_date,
                    actual_start_date,
                    planned_end_date,
                    actual_end_date,
                    service,
                    service_offering,
                    category,
                    priority,
                    raw_json
                FROM client_change_records
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            changes = [dict(x) for x in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    request_id,
                    state,
                    short_description,
                    description,
                    requested_for,
                    requested_by,
                    assignment_group,
                    service,
                    service_offering,
                    category,
                    priority,
                    opened_at,
                    closed_at,
                    raw_json
                FROM client_request_records
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            requests = [dict(x) for x in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    problem_id,
                    state,
                    short_description,
                    description,
                    assignment_group,
                    service,
                    service_offering,
                    category,
                    priority,
                    opened_at,
                    closed_at,
                    root_cause,
                    resolution,
                    raw_json
                FROM client_problem_records
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            problems = [dict(x) for x in cur.fetchall()]
    finally:
        conn.close()

    summary["postgres_tables"] = {
        "client_ticket_records_selected": len(tickets),
        "client_change_records_available": len(changes),
        "client_request_records_available": len(requests),
        "client_problem_records_available": len(problems),
    }

    records: List[Dict[str, Any]] = []
    rejected = 0
    for row in tickets:
        rec = postgres_ticket_to_record(row, tenant_id)
        if has_meaningful_operational_text(rec.get("raw", {}), rec.get("title"), rec.get("description"), rec.get("resolution")):
            # Attach compact context for transparency/debugging. build_case will still
            # use the normalized related records below for scoring/context fallback.
            raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
            raw["postgres_related_context"] = {
                "changes": find_postgres_related(row, changes, "change", limit=5),
                "requests": find_postgres_related(row, requests, "service_request", limit=5),
                "problems": find_postgres_related(row, problems, "problem", limit=5),
            }
            rec["raw"] = raw
            records.append(rec)
        else:
            rejected += 1

    # Include related records so existing build_cases/select_related logic can work.
    for row in changes:
        rec = postgres_related_to_record(row, tenant_id, "change")
        if has_meaningful_operational_text(rec.get("raw", {}), rec.get("title"), rec.get("description"), rec.get("resolution")):
            records.append(rec)
    for row in requests:
        rec = postgres_related_to_record(row, tenant_id, "service_request")
        if has_meaningful_operational_text(rec.get("raw", {}), rec.get("title"), rec.get("description"), rec.get("resolution")):
            records.append(rec)
    for row in problems:
        rec = postgres_related_to_record(row, tenant_id, "problem")
        if has_meaningful_operational_text(rec.get("raw", {}), rec.get("title"), rec.get("description"), rec.get("resolution")):
            records.append(rec)

    summary["file_summaries"].append({
        "source": "postgres_client_tables",
        "rows_read": len(tickets) + len(changes) + len(requests) + len(problems),
        "records_accepted": len(records),
        "records_rejected_placeholder_or_unmapped": rejected,
        "tables": summary["postgres_tables"],
    })
    summary["record_count"] = len(records)
    return records, summary


def load_historical_records(source_root: Path, max_source_rows: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    files = iter_candidate_ticket_files(source_root)
    all_records: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    for path in files:
        rows = read_rows(path, max_rows=max_source_rows)
        normalized: List[Dict[str, Any]] = []
        rejected_placeholder_or_unmapped = 0
        for row in rows:
            if isinstance(row, dict):
                rec = normalize_ticket_row(row, path)
                if has_meaningful_operational_text(row, rec.get("title"), rec.get("description"), rec.get("resolution")):
                    normalized.append(rec)
                else:
                    rejected_placeholder_or_unmapped += 1
        all_records.extend(normalized)
        sample_keys = list(rows[0].keys())[:30] if rows and isinstance(rows[0], dict) else []
        summaries.append({
            "file": str(path),
            "rows_read": len(rows),
            "records_accepted": len(normalized),
            "records_rejected_placeholder_or_unmapped": rejected_placeholder_or_unmapped,
            "sample_keys": sample_keys,
        })
    return all_records, {"source_root": str(source_root), "candidate_files": [str(x) for x in files], "file_summaries": summaries, "record_count": len(all_records)}




def derived_family_blob(ticket: Dict[str, Any]) -> str:
    raw = ticket.get("raw") if isinstance(ticket.get("raw"), dict) else {}
    return normalize_phrase(" ".join([
        ticket.get("title", ""),
        ticket.get("description", ""),
        ticket.get("resolution", ""),
        ticket.get("assignment_group", ""),
        ticket.get("service", ""),
        ticket.get("service_offering", ""),
        ticket.get("affected_ci", ""),
        pg_value(raw.get("service_offering")) if isinstance(raw, dict) else "",
        pg_value(raw.get("service")) if isinstance(raw, dict) else "",
        pg_value(raw.get("assignment_group")) if isinstance(raw, dict) else "",
        pg_value(raw.get("short_description")) if isinstance(raw, dict) else "",
    ]))


def derive_incident_family(ticket: Dict[str, Any], inference: Optional[Dict[str, Any]] = None) -> str:
    """Derive a stable sampling family from source ticket fields.

    This is intentionally separate from expected classification. It is used to
    balance the generated benchmark across the actual client ticket population.
    """
    blob = derived_family_blob(ticket)
    cls = normalize_token((inference or {}).get("classification"))

    if cls in {"batch_job_failure", "batch_job_delay"} or contains_any(blob, ["tws", "job failed", "job_failed", "abended", "abend", "oppwait", "job late", "not started", "distributed job monitoring"]):
        return "batch_scheduler"
    if cls == "file_transfer_failure" or contains_any(blob, ["ftp", "sftp", "file transfer", "goanywhere", "transfer failure"]):
        return "file_transfer"
    if cls == "vdi_citrix_connectivity_issue" or contains_any(blob, ["citrix", "vdi", "virtual desktop", "virtual desktops", "workspace"]):
        return "vdi_citrix"
    if cls == "vpn_client_failure" or contains_any(blob, ["vpn", "private access", "remote access"]):
        return "vpn"
    if cls == "dns_issue" or contains_any(blob, ["dns", "dns management", "resolver", "name resolution"]):
        return "dns"
    if cls == "certificate_expiry_warning" or contains_any(blob, ["certificate", " cert ", "ssl", "tls", "expiry", "expired"]):
        return "certificate"
    if cls == "cpu_saturation" or contains_any(blob, ["cpu", "processor", "high cpu"]):
        return "cpu"
    if cls == "memory_pressure" or contains_any(blob, ["memory", "oom", "out of memory", "heap"]):
        return "memory"
    if cls == "disk_full" or contains_any(blob, ["disk", "space", "filesystem", "file system", "storage", "nas storage", "server backups"]):
        return "storage_disk"
    if cls == "database_performance_issue" or contains_any(blob, ["database", "mainframe database", "sql", "oracle", "db2", "deadlock"]):
        return "database"
    if cls == "network_connectivity_issue" or contains_any(blob, ["network", "connectivity", "firewall", "global connectivity", "nsi infrastructure", "end user network", "connection"]):
        return "network_connectivity"
    return "other_valid"


def normalized_case_fingerprint(ticket: Dict[str, Any]) -> str:
    """Suppress near-duplicate generated cases, especially repetitive batch titles."""
    raw_text = meaningful_ticket_text(ticket.get("title"), ticket.get("description")) or ticket.get("title") or ticket.get("ticket_id")
    norm = normalize_phrase(raw_text)
    # Replace obvious run-specific tokens so repeated variants do not dominate.
    norm = re.sub(r"\binc\d+\b", " inc# ", norm)
    norm = re.sub(r"\b[a-z]{1,6}\d{4,}\b", " job# ", norm)
    norm = re.sub(r"\b\d{4,}\b", " # ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm[:220]


def scaled_family_quotas(limit: int) -> Dict[str, int]:
    if limit <= 0:
        limit = DEFAULT_LIMIT
    total_base = sum(TARGET_FAMILY_QUOTAS_100.values()) or 100
    raw = {fam: (quota * limit) / total_base for fam, quota in TARGET_FAMILY_QUOTAS_100.items()}
    quotas = {fam: int(value) for fam, value in raw.items()}

    # Ensure important families get at least one slot when the pack is small.
    for fam, value in raw.items():
        if TARGET_FAMILY_QUOTAS_100.get(fam, 0) > 0 and value > 0 and quotas[fam] == 0:
            quotas[fam] = 1

    # Adjust to exact limit.
    while sum(quotas.values()) > limit:
        # remove from the largest non-zero quota
        fam = max((f for f in quotas if quotas[f] > 0), key=lambda f: quotas[f])
        quotas[fam] -= 1
    while sum(quotas.values()) < limit:
        # add to the family with the largest fractional remainder / priority
        fam = max(TARGET_FAMILY_QUOTAS_100, key=lambda f: (raw.get(f, 0) - int(raw.get(f, 0)), TARGET_FAMILY_QUOTAS_100[f]))
        quotas[fam] = quotas.get(fam, 0) + 1
    return quotas

def select_primary_incidents(records: List[Dict[str, Any]], limit: int, min_confidence: float) -> List[Dict[str, Any]]:
    """Select incident primaries using derived-family stratified sampling.

    Previous behavior sorted by deterministic confidence and took the first N.
    That over-selected high-confidence TWS/batch rows. This version still uses
    confidence, but first groups by derived family and applies quotas scaled to
    --limit, then fills any remaining slots from the best unused candidates.
    """
    candidates_by_family: Dict[str, List[Tuple[float, Dict[str, Any], Dict[str, Any]]]] = defaultdict(list)
    all_candidates: List[Tuple[float, str, Dict[str, Any], Dict[str, Any]]] = []
    seen_ticket_ids = set()
    seen_family_fingerprints = set()

    for rec in records:
        if rec.get("record_type") != "incident":
            continue
        if not meaningful_ticket_text(rec.get("title"), rec.get("description"), rec.get("resolution")):
            raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
            if not row_text_fallback(raw):
                continue

        inf = infer_classification(rec)
        confidence = float(inf.get("classification_confidence", 0.0) or 0.0)
        family = derive_incident_family(rec, inf)

        # Do not allow generic/other rows to flood the benchmark, but permit a
        # small quota of other_valid rows for coverage when they have useful text.
        if confidence < min_confidence and family != "other_valid":
            continue
        if family == "other_valid" and confidence < 0.30:
            continue

        rid = clean_text(rec.get("ticket_id")) or stable_id("rid", rec.get("title"), rec.get("opened_at"))
        if rid in seen_ticket_ids:
            continue

        fingerprint = normalized_case_fingerprint(rec)
        family_fp = (family, fingerprint)
        if family_fp in seen_family_fingerprints:
            continue

        seen_ticket_ids.add(rid)
        seen_family_fingerprints.add(family_fp)
        rec["_derived_family"] = family
        rec["_classification_confidence"] = confidence
        score = (
            confidence
            + (0.08 if clean_text(rec.get("resolution")) else 0.0)
            + min(0.08, len(clean_text(rec.get("description"))) / 2000.0)
        )
        candidates_by_family[family].append((score, rec, inf))
        all_candidates.append((score, family, rec, inf))

    for family in list(candidates_by_family.keys()):
        candidates_by_family[family].sort(key=lambda x: x[0], reverse=True)
    all_candidates.sort(key=lambda x: x[0], reverse=True)

    quotas = scaled_family_quotas(limit)
    selected: List[Dict[str, Any]] = []
    selected_ids = set()

    # First pass: honor family quotas.
    for family, quota in quotas.items():
        if quota <= 0:
            continue
        for _, rec, _ in candidates_by_family.get(family, [])[:quota]:
            rid = clean_text(rec.get("ticket_id"))
            if rid and rid not in selected_ids:
                selected.append(rec)
                selected_ids.add(rid)
            if len(selected) >= limit:
                return selected[:limit]

    # Second pass: fill unused slots with strongest remaining candidates.
    for _, _, rec, _ in all_candidates:
        if len(selected) >= limit:
            break
        rid = clean_text(rec.get("ticket_id"))
        if rid and rid in selected_ids:
            continue
        selected.append(rec)
        if rid:
            selected_ids.add(rid)

    return selected[:limit]

def build_cases(tenant_id: str, records: List[Dict[str, Any]], limit: int, min_confidence: float) -> List[Dict[str, Any]]:
    by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_type[rec.get("record_type") or "incident"].append(rec)
    return [build_case(primary, by_type, tenant_id) for primary in select_primary_incidents(records, limit, min_confidence)]


def write_outputs(tenant_id: str, manifest: Dict[str, Any], records_summary: Dict[str, Any], cases: List[Dict[str, Any]], output_root: Path) -> Tuple[Path, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    cases_path = output_root / f"{tenant_id}_backtest_cases_generated.json"
    summary_path = output_root / "builder_summary.json"
    metadata = {"version": "1.0", "tenant_id": tenant_id, "generated_at": utc_now_iso(), "generated_by": "historical_backtest_case_builder.py", "source_manifest": {"migration_status": manifest.get("migration_status"), "current_source_root": manifest.get("current_source_root"), "rag_root": manifest.get("rag_root"), "backtesting_output_root": manifest.get("backtesting_output_root"), "knowledge_governance": manifest.get("knowledge_governance") or {}}, "description": "Generated client-local contextual backtesting cases from historical client ticket data."}
    cases_path.write_text(json.dumps({"metadata": metadata, "cases": cases}, indent=2, ensure_ascii=False), encoding="utf-8")
    class_counts = Counter(case.get("expected", {}).get("classification") for case in cases)
    domain_counts = Counter(case.get("expected", {}).get("domain") for case in cases)
    source_counts = Counter(case.get("source", {}).get("source_file") for case in cases)
    family_counts = Counter(case.get("source", {}).get("derived_family") for case in cases)
    summary = {"status": "ok", "tenant_id": tenant_id, "generated_at": metadata["generated_at"], "records_loaded": records_summary.get("record_count", 0), "cases_generated": len(cases), "classification_counts": class_counts.most_common(), "domain_counts": domain_counts.most_common(), "derived_family_counts": family_counts.most_common(), "target_family_quotas_100": TARGET_FAMILY_QUOTAS_100, "source_counts": source_counts.most_common(20), "records_summary": records_summary, "output_files": {"cases": str(cases_path), "summary": str(summary_path)}, "notes": ["Generated cases are deterministic candidates and should be reviewed before client-facing reporting.", "V1.4 adds derived-family stratified sampling, per-family quotas, and near-duplicate suppression so batch/TWS records do not dominate generated packs.", "V1.3 maps RAG title/content fields, filters placeholder-only rows such as ticket_504, and uses schema-aware row-text fallback.",
            "V1 may create synthetic contextual placeholders when explicit change/problem/request records are not detected.", "No raw client data is exported outside the configured client-local output path."]}
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return cases_path, summary_path


def build_from_manifest(manifest_path: Path, tenant_id: Optional[str], limit: int, min_confidence: float, source_root_override: Optional[Path], output_root_override: Optional[Path], max_source_rows: Optional[int], source: str = "auto") -> Dict[str, Any]:
    manifest = load_manifest(manifest_path)
    tenant = tenant_id or clean_text(manifest.get("tenant_id")) or "client_a"
    source_root = source_root_override or resolve_path(Path("."), manifest.get("current_source_root") or manifest.get("rag_root") or "data/rag")
    output_root = output_root_override or resolve_path(Path("."), manifest.get("backtesting_output_root") or f"data/clients/{tenant}/processed/backtesting")
    governance = manifest.get("knowledge_governance") or {}
    if governance.get("client_local_backtesting_enabled") is False:
        raise RuntimeError(f"Client-local backtesting is disabled in manifest: {manifest_path}")
    source_mode = normalize_token(source or "auto")
    if source_mode == "postgres":
        # Load a larger candidate pool than --limit so stratified sampling has
        # enough records across domains/families. Use --max-source-rows as an
        # optional explicit cap for smoke tests.
        candidate_pool_limit = max_source_rows if max_source_rows and max_source_rows > 0 else max(5000, limit * 50)
        records, records_summary = load_historical_records_from_postgres(tenant, limit=max(candidate_pool_limit, 1))
        effective_source_root = "postgres"
    elif source_mode in {"auto", "files", "rag_json", "excel"}:
        records, records_summary = load_historical_records(source_root, max_source_rows)
        effective_source_root = str(source_root)
    else:
        raise ValueError(f"Unsupported --source value: {source}. Expected auto, postgres, files, rag_json, or excel.")
    cases = build_cases(tenant, records, limit, min_confidence)
    cases_path, summary_path = write_outputs(tenant, manifest, records_summary, cases, output_root)
    return {"status": "ok", "tenant_id": tenant, "source_root": effective_source_root, "output_root": str(output_root), "records_loaded": len(records), "cases_generated": len(cases), "cases_path": str(cases_path), "summary_path": str(summary_path)}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build client-local historical backtest cases from client ticket history.")
    p.add_argument("--tenant-id", default="", help="Tenant ID. Defaults to manifest tenant_id.")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to client_data_manifest.json.")
    p.add_argument("--source-root", default="", help="Override source root. Defaults to manifest current_source_root/rag_root.")
    p.add_argument("--output-root", default="", help="Override output root. Defaults to manifest backtesting_output_root.")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Max number of generated cases.")
    p.add_argument("--min-confidence", type=float, default=0.55, help="Minimum deterministic inference confidence.")
    p.add_argument("--max-source-rows", type=int, default=0, help="Optional cap per source file for faster smoke tests.")
    p.add_argument("--source", default="auto", choices=["auto", "postgres", "files", "rag_json", "excel"], help="Input source. Use postgres for tenant-aware client_* tables.")
    p.add_argument("--json", action="store_true", help="Print full JSON result.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    result = build_from_manifest(
        manifest_path=Path(args.manifest),
        tenant_id=args.tenant_id or None,
        limit=int(args.limit),
        min_confidence=float(args.min_confidence),
        source_root_override=Path(args.source_root) if clean_text(args.source_root) else None,
        output_root_override=Path(args.output_root) if clean_text(args.output_root) else None,
        max_source_rows=int(args.max_source_rows) if int(args.max_source_rows or 0) > 0 else None,
        source=args.source,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\nAI Ops Historical Backtest Case Builder")
        print("=" * 60)
        print(f"Status          : {result.get('status')}")
        print(f"Tenant          : {result.get('tenant_id')}")
        print(f"Source Root     : {result.get('source_root')}")
        print(f"Output Root     : {result.get('output_root')}")
        print(f"Records Loaded  : {result.get('records_loaded')}")
        print(f"Cases Generated : {result.get('cases_generated')}")
        print(f"Cases File      : {result.get('cases_path')}")
        print(f"Summary File    : {result.get('summary_path')}")
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
