"""
Client Ticket Ingestion Pipeline
Accepts ITSM/Jira/ServiceNow exports (CSV, JSON, XLSX) and transforms them into:
  1. Normalised incidents   → data/clients/poc_client/incidents_normalised.json
  2. Knowledge candidates   → POSTed to /knowledge_candidates (via API)
  3. Backtest cases         → data/clients/poc_client/backtest_cases.json
  4. Classification summary → printed + saved to data/clients/poc_client/ingest_report.json

Usage:
  python poc/ingest_client_tickets.py --file tickets.xlsx --format servicenow
  python poc/ingest_client_tickets.py --file jira_export.json --format jira
  python poc/ingest_client_tickets.py --file tickets.csv --format csv --auto-detect
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data" / "clients" / "poc_client"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BACKEND_URL = "http://localhost:8001"

# ---------------------------------------------------------------------------
# Field maps per source system
# ---------------------------------------------------------------------------

SERVICENOW_FIELDS = {
    "ticket_id":      ["number", "sys_id"],
    "title":          ["short_description"],
    "description":    ["description"],
    "category":       ["category"],
    "subcategory":    ["subcategory"],
    "priority":       ["priority"],
    "status":         ["state"],
    "created_at":     ["opened_at", "sys_created_on"],
    "resolved_at":    ["closed_at", "resolved_at"],
    "resolution":     ["close_notes", "work_notes"],
    "assignee":       ["assigned_to", "assignment_group"],
    "affected_ci":    ["cmdb_ci", "configuration_item"],
}

JIRA_FIELDS = {
    "ticket_id":      ["key", "id"],
    "title":          ["summary"],
    "description":    ["description"],
    "category":       ["issuetype", "components"],
    "subcategory":    ["labels"],
    "priority":       ["priority"],
    "status":         ["status"],
    "created_at":     ["created"],
    "resolved_at":    ["resolutiondate"],
    "resolution":     ["resolution", "comment"],
    "assignee":       ["assignee", "team"],
    "affected_ci":    ["labels", "customfield_10200"],
}

CSV_FIELD_ALIASES = {
    "ticket_id":   ["ticket_id", "id", "incident_id", "number", "key", "ref"],
    "title":       ["title", "summary", "short_description", "subject", "headline"],
    "description": ["description", "detail", "body", "notes", "long_description"],
    "category":    ["category", "type", "issue_type", "class"],
    "priority":    ["priority", "severity", "sev"],
    "status":      ["status", "state", "resolution_state"],
    "created_at":  ["created", "created_at", "opened_at", "date_opened", "start_date"],
    "resolved_at": ["resolved", "resolved_at", "closed_at", "date_closed", "end_date"],
    "resolution":  ["resolution", "close_notes", "fix_notes", "solution", "workaround"],
    "assignee":    ["assignee", "owner", "assigned_to", "team", "group"],
    "affected_ci": ["ci", "cmdb_ci", "host", "server", "service", "component"],
}

# ---------------------------------------------------------------------------
# Classification keyword map (used when LLM is not available)
# ---------------------------------------------------------------------------

KEYWORD_CLASSIFICATIONS: list[tuple[list[str], str]] = [
    (["cpu", "processor", "utilisation", "utilization", "high cpu"], "high_cpu_utilisation"),
    (["disk", "storage", "space", "inode", "partition full"], "disk_space_exhaustion"),
    (["memory", "ram", "oom", "out of memory", "heap", "swap"], "memory_exhaustion_oom"),
    (["network", "latency", "packet loss", "connectivity", "rtt", "timeout"], "network_latency_degradation"),
    (["vpn", "remote access", "tunnel"], "vpn_authentication_failure"),
    (["sso", "saml", "oauth", "token", "login", "authentication", "auth"], "sso_authentication_failure"),
    (["etl", "pipeline", "data load", "ingestion", "batch job", "airflow"], "etl_pipeline_failure"),
    (["replication", "replica", "lag", "standby", "sync"], "database_replication_lag"),
    (["null", "data quality", "schema", "missing field", "validation"], "data_quality_violation"),
    (["500", "error rate", "api error", "internal server error", "5xx"], "api_high_error_rate"),
    (["pod", "container", "kubernetes", "k8s", "crash", "oom kill", "restart loop"], "container_crash_loop"),
    (["certificate", "ssl", "tls", "cert expir"], "ssl_certificate_expiry"),
    (["deployment", "release", "rollback", "deploy"], "failed_deployment_rollback"),
    (["database", "db", "postgres", "mysql", "oracle", "connection pool"], "database_connectivity_failure"),
    (["dns", "name resolution", "nxdomain"], "dns_resolution_failure"),
    (["backup", "restore", "snapshot"], "backup_failure"),
]

PRIORITY_NORMALISE = {
    "1": "P1", "critical": "P1", "blocker": "P1", "highest": "P1",
    "2": "P2", "high": "P2", "major": "P2",
    "3": "P3", "medium": "P3", "moderate": "P3", "normal": "P3",
    "4": "P4", "low": "P4", "minor": "P4",
    "5": "P5", "trivial": "P5", "planning": "P5", "lowest": "P5",
}


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsb", ".xls"):
        return "excel"
    if suffix == ".json":
        raw = path.read_text(encoding="utf-8", errors="replace")[:2000]
        data = json.loads(raw if raw.strip().startswith(("{", "[")) else "{}")
        if isinstance(data, list) and data and "key" in data[0]:
            return "jira"
        if isinstance(data, list) and data and "number" in data[0]:
            return "servicenow"
        return "json_generic"
    if suffix == ".csv":
        return "csv"
    return "csv"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _first(row: dict, keys: list[str], default: str = "") -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def load_excel(path: Path) -> list[dict]:
    try:
        import pandas as pd
        df = pd.read_excel(path, dtype=str).fillna("")
        return df.to_dict(orient="records")
    except ImportError:
        print("[ERROR] pandas/openpyxl not installed. Run: pip install pandas openpyxl")
        sys.exit(1)


def load_csv(path: Path) -> list[dict]:
    try:
        import pandas as pd
        df = pd.read_csv(path, dtype=str).fillna("")
        return df.to_dict(orient="records")
    except ImportError:
        import csv
        with path.open(encoding="utf-8-sig", errors="replace") as f:
            return list(csv.DictReader(f))


def load_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Jira bulk export wraps issues in {"issues": [...]}
        for key in ("issues", "incidents", "records", "data", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return [data]
    return []


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _map_fields(raw: dict, field_map: dict[str, list[str]]) -> dict:
    out = {}
    for canonical, candidates in field_map.items():
        out[canonical] = _first(raw, candidates)
    return out


def _auto_map_csv(raw: dict) -> dict:
    cols_lower = {k.lower().strip(): v for k, v in raw.items()}
    out = {}
    for canonical, aliases in CSV_FIELD_ALIASES.items():
        for alias in aliases:
            if alias in cols_lower:
                out[canonical] = str(cols_lower[alias]).strip()
                break
        if canonical not in out:
            out[canonical] = ""
    return out


def classify_by_keywords(text: str) -> str:
    text_lower = text.lower()
    for keywords, classification in KEYWORD_CLASSIFICATIONS:
        if any(kw in text_lower for kw in keywords):
            return classification
    return "general_it_incident"


def normalise_priority(raw: str) -> str:
    key = raw.lower().strip()
    return PRIORITY_NORMALISE.get(key, raw.upper() if raw else "P3")


def parse_datetime(s: str) -> str:
    if not s:
        return ""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
        "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%m/%d/%Y %H:%M", "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(s.strip(), fmt).isoformat()
        except (ValueError, AttributeError):
            continue
    return s.strip()


def normalise_row(raw: dict, fmt: str) -> dict | None:
    if fmt == "servicenow":
        mapped = _map_fields(raw, SERVICENOW_FIELDS)
    elif fmt == "jira":
        # Jira sometimes nests fields under a "fields" sub-dict
        if "fields" in raw and isinstance(raw["fields"], dict):
            flat = {**raw, **raw["fields"]}
            flat["key"] = raw.get("key", "")
        else:
            flat = raw
        mapped = _map_fields(flat, JIRA_FIELDS)
    else:
        mapped = _auto_map_csv(raw)

    full_text = f"{mapped.get('title', '')} {mapped.get('description', '')}"
    if len(full_text.strip()) < 10:
        return None  # skip empty rows

    classification = classify_by_keywords(full_text)

    return {
        "ticket_id":        mapped.get("ticket_id") or f"UNKNOWN-{hash(full_text) & 0xFFFF}",
        "title":            mapped.get("title", ""),
        "description":      mapped.get("description", ""),
        "category":         mapped.get("category", ""),
        "priority":         normalise_priority(mapped.get("priority", "P3")),
        "status":           mapped.get("status", ""),
        "created_at":       parse_datetime(mapped.get("created_at", "")),
        "resolved_at":      parse_datetime(mapped.get("resolved_at", "")),
        "resolution":       mapped.get("resolution", ""),
        "assignee":         mapped.get("assignee", ""),
        "affected_ci":      mapped.get("affected_ci", ""),
        "classification":   classification,
        "source_system":    fmt,
        "ingested_at":      datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Knowledge candidate builder
# ---------------------------------------------------------------------------

def build_knowledge_candidates(incidents: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for inc in incidents:
        if inc.get("resolution"):
            grouped[inc["classification"]].append(inc)

    candidates = []
    for classification, group in grouped.items():
        representative = max(group, key=lambda x: len(x.get("resolution", "")))
        resolution_texts = [
            g["resolution"] for g in group if g.get("resolution")
        ]
        combined_resolution = _pick_best_resolution(resolution_texts)

        candidates.append({
            "classification": classification,
            "title": f"[Ticket-derived] {classification.replace('_', ' ').title()}",
            "description": representative["title"],
            "resolution": combined_resolution,
            "playbook_steps": _extract_steps(combined_resolution),
            "confidence": min(0.95, 0.5 + 0.05 * len(group)),
            "source": "ticket_ingestion",
            "ticket_count": len(group),
            "sample_ticket_ids": [g["ticket_id"] for g in group[:3]],
        })

    return candidates


def _pick_best_resolution(texts: list[str]) -> str:
    if not texts:
        return ""
    return max(texts, key=len)


def _extract_steps(text: str) -> list[str]:
    if not text:
        return []
    lines = re.split(r"[\n•\-\d\.\)]+", text)
    steps = [l.strip() for l in lines if len(l.strip()) > 20]
    return steps[:8]


# ---------------------------------------------------------------------------
# Backtest case builder
# ---------------------------------------------------------------------------

def build_backtest_cases(incidents: list[dict]) -> list[dict]:
    cases = []
    for inc in incidents:
        if not inc.get("title"):
            continue
        cases.append({
            "case_id": f"bt_{inc['ticket_id']}",
            "input_text": f"{inc['title']}\n\n{inc['description']}".strip(),
            "expected_classification": inc["classification"],
            "expected_resolution_exists": bool(inc.get("resolution")),
            "priority": inc["priority"],
            "source_ticket": inc["ticket_id"],
        })
    return cases


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict) -> dict | None:
    try:
        r = requests.post(f"{BACKEND_URL}{path}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] POST {path} failed: {e}")
        return None


def push_knowledge_candidates(candidates: list[dict]) -> int:
    pushed = 0
    for c in candidates:
        result = _post("/knowledge_candidates", c)
        if result:
            pushed += 1
            time.sleep(0.1)
    return pushed


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def build_report(
    incidents: list[dict],
    candidates: list[dict],
    cases: list[dict],
) -> dict:
    class_counter = Counter(i["classification"] for i in incidents)
    priority_counter = Counter(i["priority"] for i in incidents)
    category_counter = Counter(i.get("category", "unknown") for i in incidents)

    return {
        "ingested_at": datetime.utcnow().isoformat(),
        "total_tickets": len(incidents),
        "classifications_found": len(class_counter),
        "knowledge_candidates_built": len(candidates),
        "backtest_cases_built": len(cases),
        "top_classifications": class_counter.most_common(15),
        "priority_breakdown": dict(priority_counter),
        "category_breakdown": dict(category_counter),
    }


def print_report(report: dict) -> None:
    print("\n" + "=" * 60)
    print("TICKET INGESTION REPORT")
    print("=" * 60)
    print(f"Total tickets processed : {report['total_tickets']}")
    print(f"Unique classifications  : {report['classifications_found']}")
    print(f"Knowledge candidates    : {report['knowledge_candidates_built']}")
    print(f"Backtest cases built    : {report['backtest_cases_built']}")
    print("\nTop classifications:")
    for cls, count in report["top_classifications"][:10]:
        bar = "█" * min(30, count // max(1, report["total_tickets"] // 30))
        print(f"  {cls:<40} {count:>5}  {bar}")
    print("\nPriority breakdown:", report["priority_breakdown"])
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def ingest(
    file_path: Path,
    fmt: str = "auto",
    push_to_api: bool = True,
    verbose: bool = False,
) -> dict:
    if fmt == "auto":
        fmt = detect_format(file_path)
    print(f"[ingest] Loading {file_path.name} as format={fmt}")

    if fmt == "excel":
        raw_rows = load_excel(file_path)
    elif fmt in ("jira", "json_generic", "json"):
        raw_rows = load_json(file_path)
        if fmt == "json_generic":
            fmt = "jira" if any("key" in r for r in raw_rows[:5]) else "servicenow"
    else:
        raw_rows = load_csv(file_path)

    print(f"[ingest] Raw rows loaded: {len(raw_rows)}")

    incidents: list[dict] = []
    skipped = 0
    for row in raw_rows:
        normalised = normalise_row(row, fmt)
        if normalised:
            incidents.append(normalised)
        else:
            skipped += 1

    print(f"[ingest] Normalised: {len(incidents)} tickets ({skipped} skipped)")

    # Save normalised incidents
    norm_path = DATA_DIR / "incidents_normalised.json"
    norm_path.write_text(
        json.dumps({"total": len(incidents), "by_client": {"poc_client": len(incidents)},
                    "incidents": incidents}, indent=2),
        encoding="utf-8",
    )
    print(f"[ingest] Saved → {norm_path}")

    # Build knowledge candidates
    candidates = build_knowledge_candidates(incidents)
    cands_path = DATA_DIR / "knowledge_candidates.json"
    cands_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"[ingest] {len(candidates)} knowledge candidates → {cands_path}")

    # Build backtest cases
    cases = build_backtest_cases(incidents)
    cases_path = DATA_DIR / "backtest_cases.json"
    cases_path.write_text(json.dumps(cases, indent=2), encoding="utf-8")
    print(f"[ingest] {len(cases)} backtest cases → {cases_path}")

    # Push to API
    if push_to_api:
        pushed = push_knowledge_candidates(candidates)
        print(f"[ingest] Pushed {pushed}/{len(candidates)} candidates to API")

    # Report
    report = build_report(incidents, candidates, cases)
    report_path = DATA_DIR / "ingest_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_report(report)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest client ITSM/Jira tickets into the POC platform")
    parser.add_argument("--file", required=True, help="Path to ticket export file (CSV, JSON, XLSX)")
    parser.add_argument(
        "--format",
        choices=["auto", "servicenow", "jira", "csv", "excel"],
        default="auto",
        help="Source system format (default: auto-detect)",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Skip pushing knowledge candidates to the API",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"[ERROR] File not found: {path}")
        sys.exit(1)

    ingest(
        file_path=path,
        fmt=args.format,
        push_to_api=not args.no_push,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
