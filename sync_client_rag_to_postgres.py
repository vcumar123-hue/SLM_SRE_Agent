#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sync_client_rag_to_postgres.py

Tenant-aware client data ingestion/sync for AI Ops Copilot.

Purpose
-------
Safely loads raw client files from the existing data\\rag folder into NEW
tenant-aware Postgres tables without touching existing platform tables such as:
  - incidents
  - feedback_events
  - taxonomy
  - automation_queue
  - telemetry_incidents

This script is intentionally additive. It does not move files, rename folders,
delete existing data, or replace your enterprise baseline loaders.

Recommended command
-------------------
python sync_client_rag_to_postgres.py `
  --tenant-id client_a `
  --manifest data\\clients\\client_a\\governance\\client_data_manifest.json `
  --source-root data\\rag

Useful validation
-----------------
docker exec -it aiops-postgres psql -U aiops_user -d aiops -c "\\dt client_*"

docker exec -it aiops-postgres psql -U aiops_user -d aiops -c "
SELECT tenant_id, COUNT(*) FROM client_ticket_records GROUP BY tenant_id;
"

Notes
-----
- Skips placeholder-only tickets.json safely.
- Reads raw Excel/PPT/DOCX/TXT/MD/JSON/CSV where possible.
- Classifies Excel files by filename, sheet names, and columns.
- Routes ticket dump sheets into typed client_* record tables.
- Routes all useful content into client_knowledge_chunks for RAG/reference.
- Preserves original row data in JSONB raw_json/metadata_json.
"""

from __future__ import annotations

import csv
import argparse
import hashlib
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, execute_values

try:
    import pandas as pd
except Exception:
    pd = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    from pptx import Presentation
except Exception:
    Presentation = None


SUPPORTED_EXTENSIONS = {
    ".csv",
    ".xlsx",
    ".xls",
    ".json",
    ".txt",
    ".md",
    ".docx",
    ".pptx",
}

CLIENT_TABLES = [
    "client_source_files",
    "client_ingestion_runs",
    "client_knowledge_chunks",
    "client_ticket_records",
    "client_change_records",
    "client_request_records",
    "client_problem_records",
    "client_asset_records",
    "client_topology_records",
    "client_network_records",
]


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        # pandas NaN
        if pd is not None and pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", clean_text(value).lower()).strip("_")


def sha_id(*parts: Any, prefix: str = "") -> str:
    raw = "|".join(clean_text(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:32]
    return f"{prefix}{digest}" if prefix else digest


def safe_json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd is not None and pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return str(value)


def row_to_json(row: Dict[str, Any]) -> Dict[str, Any]:
    return {clean_text(k): safe_json_value(v) for k, v in row.items()}


def first_present(row: Dict[str, Any], candidates: Iterable[str]) -> str:
    """
    Find a field by exact normalized name first, then fuzzy contains.
    """
    if not row:
        return ""

    normalized_to_key = {normalize_key(k): k for k in row.keys()}

    # exact normalized match
    for candidate in candidates:
        nk = normalize_key(candidate)
        actual = normalized_to_key.get(nk)
        if actual is not None:
            val = clean_text(row.get(actual))
            if val:
                return val

    # contains match
    candidate_norms = [normalize_key(c) for c in candidates]
    for actual_norm, actual_key in normalized_to_key.items():
        for wanted in candidate_norms:
            if wanted and wanted in actual_norm:
                val = clean_text(row.get(actual_key))
                if val:
                    return val

    return ""


def looks_like_placeholder(value: Any) -> bool:
    text = clean_text(value).lower()
    if not text:
        return True

    placeholder_patterns = [
        r"^(ticket|incident|inc|case|record|row|item)[\s_\-]*[0-9]+$",
        r"^chg[\s_\-]*[0-9]+$",
        r"^req[\s_\-]*[0-9]+$",
        r"^prb[\s_\-]*[0-9]+$",
    ]
    return any(re.fullmatch(p, text) for p in placeholder_patterns)


def is_meaningful_content(value: Any, min_words: int = 4) -> bool:
    text = clean_text(value)
    if not text:
        return False
    if looks_like_placeholder(text):
        return False
    words = re.findall(r"[A-Za-z]{3,}", text)
    return len(words) >= min_words


def build_row_content(row: Dict[str, Any], max_len: int = 8000) -> str:
    parts = []
    for k, v in row.items():
        key = clean_text(k)
        val = clean_text(v)
        if not key or not val:
            continue
        if val.lower() in {"nan", "nat", "none", "null"}:
            continue
        parts.append(f"{key}: {val}")
    return " | ".join(parts)[:max_len]


def parse_datetime_or_none(value: Any) -> Optional[str]:
    text = clean_text(value)
    if not text:
        return None
    if text.lower() in {"nan", "nat", "none", "null"}:
        return None
    return text


def file_checksum(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def load_manifest(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def slug(value: str) -> str:
    return normalize_key(value)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_conn():
    """
    Defaults align with the local Docker Postgres config already validated:
      POSTGRES_DB=aiops
      POSTGRES_USER=aiops_user

    Prefer setting PGPASSWORD in the shell. A local-dev default is included
    only to match the current docker-compose environment shared in the session.
    """
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "aiops"),
        user=os.getenv("PGUSER", "aiops_user"),
        password=os.getenv("PGPASSWORD", "StrongPassword123"),
    )


def create_tables(cur) -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS client_ingestion_runs (
            run_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            input_root TEXT,
            manifest_path TEXT,
            status TEXT,
            files_discovered INTEGER DEFAULT 0,
            files_processed INTEGER DEFAULT 0,
            files_failed INTEGER DEFAULT 0,
            tickets_created INTEGER DEFAULT 0,
            changes_created INTEGER DEFAULT 0,
            requests_created INTEGER DEFAULT 0,
            problems_created INTEGER DEFAULT 0,
            assets_created INTEGER DEFAULT 0,
            topology_created INTEGER DEFAULT 0,
            network_created INTEGER DEFAULT 0,
            knowledge_chunks_created INTEGER DEFAULT 0,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            summary_json JSONB
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_source_files (
            source_file_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            knowledge_scope TEXT,
            collection_name TEXT,
            source_file TEXT NOT NULL,
            file_name TEXT,
            file_extension TEXT,
            source_category TEXT,
            checksum TEXT,
            file_size_bytes BIGINT,
            record_count INTEGER DEFAULT 0,
            ingestion_status TEXT,
            error TEXT,
            ingested_at TIMESTAMPTZ DEFAULT NOW(),
            metadata_json JSONB
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_knowledge_chunks (
            chunk_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            knowledge_scope TEXT,
            collection_name TEXT,
            knowledge_type TEXT,
            source_category TEXT,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            title TEXT,
            content TEXT,
            domain TEXT,
            classification TEXT,
            service_scope TEXT,
            environment TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            metadata_json JSONB
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_ticket_records (
            record_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            ticket_id TEXT,
            priority TEXT,
            caller TEXT,
            assignment_group TEXT,
            opened_at TEXT,
            resolved_at TEXT,
            on_call_group TEXT,
            short_description TEXT,
            service TEXT,
            service_offering TEXT,
            major_incident_state TEXT,
            channel TEXT,
            created_by TEXT,
            reassignment_count TEXT,
            record_type TEXT,
            raw_json JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_change_records (
            record_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            change_id TEXT,
            change_type TEXT,
            state TEXT,
            short_description TEXT,
            created_at_source TEXT,
            planned_start_date TEXT,
            actual_start_date TEXT,
            planned_end_date TEXT,
            actual_end_date TEXT,
            requested_by TEXT,
            assigned_to TEXT,
            service TEXT,
            service_offering TEXT,
            category TEXT,
            closed_at TEXT,
            priority TEXT,
            raw_json JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_request_records (
            record_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            request_id TEXT,
            state TEXT,
            short_description TEXT,
            description TEXT,
            requested_for TEXT,
            requested_by TEXT,
            assignment_group TEXT,
            assigned_to TEXT,
            service TEXT,
            service_offering TEXT,
            category TEXT,
            priority TEXT,
            opened_at TEXT,
            closed_at TEXT,
            raw_json JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_problem_records (
            record_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            problem_id TEXT,
            state TEXT,
            short_description TEXT,
            description TEXT,
            assignment_group TEXT,
            assigned_to TEXT,
            service TEXT,
            service_offering TEXT,
            category TEXT,
            priority TEXT,
            opened_at TEXT,
            closed_at TEXT,
            root_cause TEXT,
            resolution TEXT,
            raw_json JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_asset_records (
            record_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            asset_id TEXT,
            asset_name TEXT,
            hostname TEXT,
            ip_address TEXT,
            environment TEXT,
            service TEXT,
            owner TEXT,
            location TEXT,
            content TEXT,
            raw_json JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_topology_records (
            record_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            topology_id TEXT,
            object_name TEXT,
            object_type TEXT,
            parent_object TEXT,
            cluster_name TEXT,
            host_name TEXT,
            datastore TEXT,
            network_name TEXT,
            content TEXT,
            raw_json JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_network_records (
            record_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            source_file TEXT,
            source_sheet TEXT,
            source_row INTEGER,
            network_id TEXT,
            site TEXT,
            circuit_id TEXT,
            carrier TEXT,
            bandwidth TEXT,
            device TEXT,
            ip_address TEXT,
            subnet TEXT,
            vlan TEXT,
            content TEXT,
            raw_json JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
    ]

    for statement in ddl:
        cur.execute(statement)

    index_ddl = [
        "CREATE INDEX IF NOT EXISTS idx_client_source_files_tenant ON client_source_files (tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_source_files_category ON client_source_files (source_category)",
        "CREATE INDEX IF NOT EXISTS idx_client_chunks_tenant_type ON client_knowledge_chunks (tenant_id, knowledge_type)",
        "CREATE INDEX IF NOT EXISTS idx_client_chunks_source ON client_knowledge_chunks (source_file)",
        "CREATE INDEX IF NOT EXISTS idx_client_ticket_tenant ON client_ticket_records (tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_ticket_number ON client_ticket_records (ticket_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_change_tenant ON client_change_records (tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_change_number ON client_change_records (change_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_request_tenant ON client_request_records (tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_problem_tenant ON client_problem_records (tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_asset_tenant ON client_asset_records (tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_topology_tenant ON client_topology_records (tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_client_network_tenant ON client_network_records (tenant_id)",
    ]
    for statement in index_ddl:
        cur.execute(statement)


def upsert_rows(cur, table_name: str, pk_col: str, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    columns = sorted({k for row in rows for k in row.keys()})
    values = []
    for row in rows:
        record = []
        for col in columns:
            value = row.get(col)
            if isinstance(value, (dict, list)):
                value = Json(value)
            record.append(value)
        values.append(tuple(record))

    update_cols = [c for c in columns if c != pk_col]
    stmt = sql.SQL(
        """
        INSERT INTO {table} ({cols})
        VALUES %s
        ON CONFLICT ({pk})
        DO UPDATE SET {updates}
        """
    ).format(
        table=sql.Identifier(table_name),
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        pk=sql.Identifier(pk_col),
        updates=sql.SQL(", ").join(
            sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(c))
            for c in update_cols
        ),
    )

    execute_values(cur, stmt.as_string(cur.connection), values, page_size=500)
    return len(rows)


# ---------------------------------------------------------------------------
# File classification and readers
# ---------------------------------------------------------------------------

def discover_files(source_root: Path) -> List[Path]:
    files = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("~$"):
            continue
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    return sorted(files)


def read_excel_sheet_with_header_detection(path: Path, sheet_name: str) -> Tuple[Optional["pd.DataFrame"], Dict[str, Any]]:
    """
    Reads a sheet with a simple header detection fallback.

    First attempt: header=0.
    If many columns are Unnamed or key ticket/asset columns are not found,
    scan first 15 rows for likely header row and re-read.
    """
    info: Dict[str, Any] = {"sheet": sheet_name, "header_row_used": 0, "header_detection": "default"}

    if pd is None:
        info["error"] = "pandas is not installed"
        return None, info

    try:
        df = pd.read_excel(path, sheet_name=sheet_name)
    except Exception as e:
        info["error"] = str(e)
        return None, info

    if df is None:
        return None, info

    df = df.dropna(how="all")
    if df.empty:
        info["rows"] = 0
        return df, info

    cols = [clean_text(c) for c in df.columns]
    unnamed_count = sum(1 for c in cols if not c or c.lower().startswith("unnamed"))
    unnamed_ratio = unnamed_count / max(1, len(cols))
    info["initial_columns"] = cols
    info["initial_unnamed_ratio"] = unnamed_ratio

    if unnamed_ratio <= 0.30 and has_useful_columns(cols):
        info["rows"] = len(df)
        return df, info

    # Try to detect header row from raw data
    try:
        raw = pd.read_excel(path, sheet_name=sheet_name, header=None, nrows=20)
    except Exception as e:
        info["header_detection_error"] = str(e)
        info["rows"] = len(df)
        return df, info

    best_row = None
    best_score = 0
    for idx in range(min(20, len(raw))):
        row_values = [clean_text(x) for x in raw.iloc[idx].tolist()]
        score = header_score(row_values)
        if score > best_score:
            best_score = score
            best_row = idx

    if best_row is not None and best_score >= 2:
        try:
            df2 = pd.read_excel(path, sheet_name=sheet_name, header=best_row)
            df2 = df2.dropna(how="all")
            df2.columns = [clean_text(c) for c in df2.columns]
            info["header_row_used"] = best_row
            info["header_detection"] = "detected"
            info["detected_header_score"] = best_score
            info["rows"] = len(df2)
            info["columns"] = list(df2.columns)
            return df2, info
        except Exception as e:
            info["header_detection_error"] = str(e)

    info["rows"] = len(df)
    info["columns"] = list(df.columns)
    return df, info


def header_score(values: List[str]) -> int:
    normalized = {normalize_key(v) for v in values if v}
    indicators = [
        "number",
        "priority",
        "caller",
        "assignment_group",
        "opened",
        "resolved",
        "short_description",
        "service",
        "service_offering",
        "type",
        "state",
        "created",
        "planned_start_date",
        "actual_start_date",
        "hostname",
        "ip_address",
        "cluster",
        "datastore",
        "vm",
        "circuit",
        "carrier",
    ]
    return sum(1 for i in indicators if i in normalized)


def has_useful_columns(cols: List[str]) -> bool:
    n = {normalize_key(c) for c in cols}
    groups = [
        {"number", "short_description"},
        {"opened", "resolved"},
        {"assignment_group", "priority"},
        {"hostname", "ip_address"},
        {"vm", "cluster"},
        {"circuit", "carrier"},
        {"service", "service_offering"},
    ]
    return any(len(g & n) >= 1 for g in groups)


def classify_excel_workbook(path: Path, workbook_meta: List[Dict[str, Any]]) -> str:
    file_name = path.name.lower()
    all_sheets = " ".join(m.get("sheet", "") for m in workbook_meta).lower()
    all_cols = " ".join(" ".join(m.get("columns", []) or m.get("initial_columns", [])) for m in workbook_meta).lower()
    combined = f"{file_name} {all_sheets} {all_cols}"

    ticket_sheet_indicators = {"incident", "change", "request", "problem", "unified_data"}
    actual_sheets = {normalize_key(m.get("sheet", "")) for m in workbook_meta}
    if actual_sheets & ticket_sheet_indicators:
        if any(x in combined for x in ["short description", "assignment group", "opened", "resolved", "priority"]):
            return "ticket_dump"

    if any(x in combined for x in ["rvtools", "vinfo", "vcpu", "vmemory", "vdisk", "vnetwork", "vhost", "vcluster", "datastore", "esxi"]):
        return "vmware_topology"

    if any(x in combined for x in ["circuit", "carrier", "wan", "lan", "bandwidth", "router", "firewall", "subnet", "vlan"]):
        return "network_inventory"

    if any(x in combined for x in ["infra assets", "asset", "hostname", "server", "application", "environment", "owner", "serial number", "cmdb", "ci"]):
        return "asset_inventory"

    if any(x in combined for x in ["workload", "dependency", "topology", "cluster", "host", "data center", "datacenter"]):
        return "topology_inventory"

    return "generic_excel"


def infer_sheet_record_type(sheet_name: str, cols: List[str]) -> str:
    s = normalize_key(sheet_name)
    c = {normalize_key(x) for x in cols}

    if "incident" in s:
        return "incident"
    if "change" in s:
        return "change"
    if "request" in s or "ritm" in s:
        return "request"
    if "problem" in s:
        return "problem"

    if {"number", "short_description"} & c and {"opened", "resolved", "assignment_group", "priority"} & c:
        return "incident"

    if {"planned_start_date", "planned_end_date", "actual_start_date", "actual_end_date"} & c:
        return "change"

    return "generic"


def read_docx(path: Path) -> str:
    if Document is None:
        raise RuntimeError("python-docx is not installed")
    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if clean_text(p.text))


def read_pptx(path: Path) -> str:
    if Presentation is None:
        raise RuntimeError("python-pptx is not installed")
    prs = Presentation(str(path))
    out = []
    for idx, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                t = clean_text(shape.text)
                if t:
                    texts.append(t)
        if texts:
            out.append(f"Slide {idx}: " + " | ".join(texts))
    return "\n".join(out)


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def read_json_text(path: Path) -> str:
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return json.dumps(obj, ensure_ascii=False, default=str)[:200000]
    except Exception:
        return read_text_file(path)


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------

def map_ticket_row(row: Dict[str, Any], tenant_id: str, source_file: str, sheet: str, source_row: int) -> Optional[Dict[str, Any]]:
    short_description = first_present(row, ["Short description", "Summary", "Title", "Subject"])
    ticket_id = first_present(row, ["Number", "Incident Number", "Ticket Number", "Ticket ID", "ID"])
    content = build_row_content(row)

    if not is_meaningful_content(short_description) and not is_meaningful_content(content):
        return None

    return {
        "record_id": sha_id(tenant_id, source_file, sheet, source_row, ticket_id, prefix="tic_"),
        "tenant_id": tenant_id,
        "source_file": source_file,
        "source_sheet": sheet,
        "source_row": source_row,
        "ticket_id": ticket_id,
        "priority": first_present(row, ["Priority"]),
        "caller": first_present(row, ["Caller", "Opened by"]),
        "assignment_group": first_present(row, ["Assignment group", "Assigned group", "Support group", "Resolver group"]),
        "opened_at": parse_datetime_or_none(first_present(row, ["Opened", "Opened at", "Created", "Created at"])),
        "resolved_at": parse_datetime_or_none(first_present(row, ["Resolved", "Resolved at", "Closed", "Closed at"])),
        "on_call_group": first_present(row, ["On call group", "On-call group"]),
        "short_description": short_description or content[:250],
        "service": first_present(row, ["Service"]),
        "service_offering": first_present(row, ["Service offering", "Service Offering"]),
        "major_incident_state": first_present(row, ["Major incident state", "Major Incident State"]),
        "channel": first_present(row, ["Channel"]),
        "created_by": first_present(row, ["Created by", "Created By"]),
        "reassignment_count": first_present(row, ["Reassignment count", "Reassignment Count"]),
        "record_type": first_present(row, ["Type"]) or "Incident",
        "raw_json": row_to_json(row),
        "updated_at": utc_now(),
    }


def map_change_row(row: Dict[str, Any], tenant_id: str, source_file: str, sheet: str, source_row: int) -> Optional[Dict[str, Any]]:
    short_description = first_present(row, ["Short description", "Summary", "Title", "Subject"])
    change_id = first_present(row, ["Number", "Change Number", "Change ID", "ID"])
    content = build_row_content(row)

    if not is_meaningful_content(short_description) and not is_meaningful_content(content):
        return None

    return {
        "record_id": sha_id(tenant_id, source_file, sheet, source_row, change_id, prefix="chg_"),
        "tenant_id": tenant_id,
        "source_file": source_file,
        "source_sheet": sheet,
        "source_row": source_row,
        "change_id": change_id,
        "change_type": first_present(row, ["Type"]),
        "state": first_present(row, ["State"]),
        "short_description": short_description or content[:250],
        "created_at_source": parse_datetime_or_none(first_present(row, ["Created", "Created at"])),
        "planned_start_date": parse_datetime_or_none(first_present(row, ["Planned start date", "Planned start"])),
        "actual_start_date": parse_datetime_or_none(first_present(row, ["Actual start date", "Actual start"])),
        "planned_end_date": parse_datetime_or_none(first_present(row, ["Planned end date", "Planned end"])),
        "actual_end_date": parse_datetime_or_none(first_present(row, ["Actual end date", "Actual end"])),
        "requested_by": first_present(row, ["Requested by", "Requested By"]),
        "assigned_to": first_present(row, ["Assigned to", "Assigned To"]),
        "service": first_present(row, ["Service"]),
        "service_offering": first_present(row, ["Service offering"]),
        "category": first_present(row, ["Category"]),
        "closed_at": parse_datetime_or_none(first_present(row, ["Closed", "Closed at"])),
        "priority": first_present(row, ["Priority"]),
        "raw_json": row_to_json(row),
        "updated_at": utc_now(),
    }


def map_request_row(row: Dict[str, Any], tenant_id: str, source_file: str, sheet: str, source_row: int) -> Optional[Dict[str, Any]]:
    short_description = first_present(row, ["Short description", "Summary", "Title", "Subject", "Item"])
    request_id = first_present(row, ["Number", "Request Number", "Request ID", "RITM", "REQ", "ID"])
    content = build_row_content(row)

    if not is_meaningful_content(short_description) and not is_meaningful_content(content):
        return None

    return {
        "record_id": sha_id(tenant_id, source_file, sheet, source_row, request_id, prefix="req_"),
        "tenant_id": tenant_id,
        "source_file": source_file,
        "source_sheet": sheet,
        "source_row": source_row,
        "request_id": request_id,
        "state": first_present(row, ["State", "Status"]),
        "short_description": short_description or content[:250],
        "description": first_present(row, ["Description", "Details"]),
        "requested_for": first_present(row, ["Requested for", "Requested For"]),
        "requested_by": first_present(row, ["Requested by", "Requested By", "Opened by"]),
        "assignment_group": first_present(row, ["Assignment group", "Assigned group"]),
        "assigned_to": first_present(row, ["Assigned to"]),
        "service": first_present(row, ["Service"]),
        "service_offering": first_present(row, ["Service offering"]),
        "category": first_present(row, ["Category"]),
        "priority": first_present(row, ["Priority"]),
        "opened_at": parse_datetime_or_none(first_present(row, ["Opened", "Opened at", "Created", "Created at"])),
        "closed_at": parse_datetime_or_none(first_present(row, ["Closed", "Closed at", "Resolved", "Resolved at"])),
        "raw_json": row_to_json(row),
        "updated_at": utc_now(),
    }


def map_problem_row(row: Dict[str, Any], tenant_id: str, source_file: str, sheet: str, source_row: int) -> Optional[Dict[str, Any]]:
    short_description = first_present(row, ["Short description", "Summary", "Title", "Subject"])
    problem_id = first_present(row, ["Number", "Problem Number", "Problem ID", "ID"])
    content = build_row_content(row)

    if not is_meaningful_content(short_description) and not is_meaningful_content(content):
        return None

    return {
        "record_id": sha_id(tenant_id, source_file, sheet, source_row, problem_id, prefix="prb_"),
        "tenant_id": tenant_id,
        "source_file": source_file,
        "source_sheet": sheet,
        "source_row": source_row,
        "problem_id": problem_id,
        "state": first_present(row, ["State", "Status"]),
        "short_description": short_description or content[:250],
        "description": first_present(row, ["Description", "Details"]),
        "assignment_group": first_present(row, ["Assignment group", "Assigned group"]),
        "assigned_to": first_present(row, ["Assigned to"]),
        "service": first_present(row, ["Service"]),
        "service_offering": first_present(row, ["Service offering"]),
        "category": first_present(row, ["Category"]),
        "priority": first_present(row, ["Priority"]),
        "opened_at": parse_datetime_or_none(first_present(row, ["Opened", "Opened at", "Created", "Created at"])),
        "closed_at": parse_datetime_or_none(first_present(row, ["Closed", "Closed at", "Resolved", "Resolved at"])),
        "root_cause": first_present(row, ["Root cause", "Cause", "Known error"]),
        "resolution": first_present(row, ["Resolution", "Close notes", "Resolution notes"]),
        "raw_json": row_to_json(row),
        "updated_at": utc_now(),
    }


def map_asset_row(row: Dict[str, Any], tenant_id: str, source_file: str, sheet: str, source_row: int) -> Optional[Dict[str, Any]]:
    content = build_row_content(row)
    if not is_meaningful_content(content):
        return None

    hostname = first_present(row, ["Hostname", "Host name", "Server", "Name", "VM", "Computer"])
    asset_id = first_present(row, ["Asset ID", "Asset", "CI", "Configuration Item", "Serial Number", "Number"])
    return {
        "record_id": sha_id(tenant_id, source_file, sheet, source_row, asset_id, hostname, prefix="ast_"),
        "tenant_id": tenant_id,
        "source_file": source_file,
        "source_sheet": sheet,
        "source_row": source_row,
        "asset_id": asset_id,
        "asset_name": first_present(row, ["Name", "Asset Name", "Application", "Service"]),
        "hostname": hostname,
        "ip_address": first_present(row, ["IP Address", "IP", "IPv4 Address"]),
        "environment": first_present(row, ["Environment", "Env"]),
        "service": first_present(row, ["Service", "Application", "Business Service"]),
        "owner": first_present(row, ["Owner", "Application Owner", "Support Owner"]),
        "location": first_present(row, ["Location", "Site", "Data Center", "Datacenter"]),
        "content": content,
        "raw_json": row_to_json(row),
        "updated_at": utc_now(),
    }


def map_topology_row(row: Dict[str, Any], tenant_id: str, source_file: str, sheet: str, source_row: int) -> Optional[Dict[str, Any]]:
    content = build_row_content(row)
    if not is_meaningful_content(content):
        return None

    object_name = first_present(row, ["VM", "Name", "Hostname", "Host", "Cluster", "Object", "Application"])
    return {
        "record_id": sha_id(tenant_id, source_file, sheet, source_row, object_name, prefix="top_"),
        "tenant_id": tenant_id,
        "source_file": source_file,
        "source_sheet": sheet,
        "source_row": source_row,
        "topology_id": first_present(row, ["ID", "Object ID", "MoRef", "UUID"]),
        "object_name": object_name,
        "object_type": first_present(row, ["Type", "Object Type", "Resource Type"]),
        "parent_object": first_present(row, ["Parent", "Folder", "Datacenter", "Data Center"]),
        "cluster_name": first_present(row, ["Cluster", "Cluster Name"]),
        "host_name": first_present(row, ["Host", "Host Name", "ESXi Host"]),
        "datastore": first_present(row, ["Datastore", "Datastore Name"]),
        "network_name": first_present(row, ["Network", "Network Name", "Portgroup", "VLAN"]),
        "content": content,
        "raw_json": row_to_json(row),
        "updated_at": utc_now(),
    }


def map_network_row(row: Dict[str, Any], tenant_id: str, source_file: str, sheet: str, source_row: int) -> Optional[Dict[str, Any]]:
    content = build_row_content(row)
    if not is_meaningful_content(content):
        return None

    circuit_id = first_present(row, ["Circuit", "Circuit ID", "Circuit Number", "Service ID"])
    return {
        "record_id": sha_id(tenant_id, source_file, sheet, source_row, circuit_id, prefix="net_"),
        "tenant_id": tenant_id,
        "source_file": source_file,
        "source_sheet": sheet,
        "source_row": source_row,
        "network_id": first_present(row, ["ID", "Network ID", "Number"]),
        "site": first_present(row, ["Site", "Location", "Office", "Data Center", "Datacenter"]),
        "circuit_id": circuit_id,
        "carrier": first_present(row, ["Carrier", "Provider", "Telco"]),
        "bandwidth": first_present(row, ["Bandwidth", "Speed", "Circuit Speed"]),
        "device": first_present(row, ["Device", "Router", "Firewall", "Switch"]),
        "ip_address": first_present(row, ["IP Address", "IP", "WAN IP", "LAN IP"]),
        "subnet": first_present(row, ["Subnet", "CIDR", "Network"]),
        "vlan": first_present(row, ["VLAN", "VLAN ID"]),
        "content": content,
        "raw_json": row_to_json(row),
        "updated_at": utc_now(),
    }


def make_knowledge_chunk(
    tenant_id: str,
    knowledge_scope: str,
    collection_name: str,
    source_category: str,
    knowledge_type: str,
    source_file: str,
    source_sheet: str,
    source_row: Optional[int],
    title: str,
    content: str,
    metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not is_meaningful_content(content):
        return None

    return {
        "chunk_id": sha_id(tenant_id, source_file, source_sheet, source_row, knowledge_type, title, content[:500], prefix="chk_"),
        "tenant_id": tenant_id,
        "knowledge_scope": knowledge_scope,
        "collection_name": collection_name,
        "knowledge_type": knowledge_type,
        "source_category": source_category,
        "source_file": source_file,
        "source_sheet": source_sheet,
        "source_row": source_row,
        "title": title[:500] if title else content[:250],
        "content": content[:8000],
        "domain": metadata.get("domain"),
        "classification": metadata.get("classification"),
        "service_scope": metadata.get("service_scope"),
        "environment": metadata.get("environment"),
        "metadata_json": metadata,
        "updated_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

class Counters(dict):
    def inc(self, key: str, value: int = 1) -> None:
        self[key] = self.get(key, 0) + value


def process_excel_file(
    path: Path,
    tenant_id: str,
    knowledge_scope: str,
    collection_name: str,
    max_rows_per_sheet: Optional[int],
) -> Dict[str, Any]:
    if pd is None:
        raise RuntimeError("pandas is required to process Excel files")

    source_file = str(path)
    result = {
        "source_category": "generic_excel",
        "source_rows": {
            "client_ticket_records": [],
            "client_change_records": [],
            "client_request_records": [],
            "client_problem_records": [],
            "client_asset_records": [],
            "client_topology_records": [],
            "client_network_records": [],
            "client_knowledge_chunks": [],
        },
        "metadata": {"sheets": []},
        "record_count": 0,
    }

    xl = pd.ExcelFile(path)
    sheet_frames: List[Tuple[str, Any, Dict[str, Any]]] = []

    # First pass reads sheets + metadata so file can be classified accurately.
    for sheet_name in xl.sheet_names:
        df, info = read_excel_sheet_with_header_detection(path, sheet_name)
        if df is not None:
            df.columns = [clean_text(c) for c in df.columns]
            info["columns"] = list(df.columns)
            info["rows"] = len(df)
        result["metadata"]["sheets"].append(info)
        sheet_frames.append((sheet_name, df, info))

    source_category = classify_excel_workbook(path, result["metadata"]["sheets"])
    result["source_category"] = source_category

    for sheet_name, df, info in sheet_frames:
        if df is None or df.empty:
            continue

        if max_rows_per_sheet is not None and max_rows_per_sheet > 0:
            df = df.head(max_rows_per_sheet)

        df = df.dropna(how="all")
        if df.empty:
            continue

        record_type = infer_sheet_record_type(sheet_name, list(df.columns))
        rows = df.to_dict(orient="records")

        for idx, row in enumerate(rows, start=1):
            row_content = build_row_content(row)
            if not is_meaningful_content(row_content):
                continue

            title = (
                first_present(row, ["Short description", "Summary", "Title", "Subject", "Name", "Hostname", "Service"])
                or row_content[:250]
            )

            metadata = {
                "source_file_name": path.name,
                "source_file_extension": path.suffix.lower(),
                "source_category": source_category,
                "record_type": record_type,
                "sheet": sheet_name,
                "raw_json": row_to_json(row),
            }

            # Always create a knowledge chunk for useful rows.
            if source_category == "ticket_dump":
                knowledge_type = record_type if record_type in {"incident", "change", "request", "problem"} else "ticket_dump_row"
            elif source_category in {"vmware_topology", "topology_inventory"}:
                knowledge_type = "topology"
            elif source_category == "network_inventory":
                knowledge_type = "network"
            elif source_category == "asset_inventory":
                knowledge_type = "asset"
            else:
                knowledge_type = "document"

            chunk = make_knowledge_chunk(
                tenant_id,
                knowledge_scope,
                collection_name,
                source_category,
                knowledge_type,
                source_file,
                sheet_name,
                idx,
                title,
                row_content,
                metadata,
            )
            if chunk:
                result["source_rows"]["client_knowledge_chunks"].append(chunk)

            # Route typed rows.
            if source_category == "ticket_dump":
                if record_type == "incident":
                    mapped = map_ticket_row(row, tenant_id, source_file, sheet_name, idx)
                    if mapped:
                        result["source_rows"]["client_ticket_records"].append(mapped)
                elif record_type == "change":
                    mapped = map_change_row(row, tenant_id, source_file, sheet_name, idx)
                    if mapped:
                        result["source_rows"]["client_change_records"].append(mapped)
                elif record_type == "request":
                    mapped = map_request_row(row, tenant_id, source_file, sheet_name, idx)
                    if mapped:
                        result["source_rows"]["client_request_records"].append(mapped)
                elif record_type == "problem":
                    mapped = map_problem_row(row, tenant_id, source_file, sheet_name, idx)
                    if mapped:
                        result["source_rows"]["client_problem_records"].append(mapped)

            elif source_category == "asset_inventory":
                mapped = map_asset_row(row, tenant_id, source_file, sheet_name, idx)
                if mapped:
                    result["source_rows"]["client_asset_records"].append(mapped)

            elif source_category in {"vmware_topology", "topology_inventory"}:
                mapped = map_topology_row(row, tenant_id, source_file, sheet_name, idx)
                if mapped:
                    result["source_rows"]["client_topology_records"].append(mapped)

                # VM/topology exports also often contain asset-like rows.
                asset = map_asset_row(row, tenant_id, source_file, sheet_name, idx)
                if asset:
                    result["source_rows"]["client_asset_records"].append(asset)

            elif source_category == "network_inventory":
                mapped = map_network_row(row, tenant_id, source_file, sheet_name, idx)
                if mapped:
                    result["source_rows"]["client_network_records"].append(mapped)

            result["record_count"] += 1

    return result


# --- ServiceNow-style incident exports (CSV, or CSV saved with a .txt extension) -----------------------------------------

_AUTO_ASSIGN_NOTE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - System Administrator \(Work notes\)\s*)?"
    r"This Incident was not assigned to anyone in the last 24 hours\.?\s*"
    r"Hence system has auto-assigned this either to the active group manager or an active assignment group member\.?",
    re.IGNORECASE,
)


def looks_like_ticket_columns(columns: Iterable[Any]) -> bool:
    """True for an incident export: a ticket number, a summary, a creation date and workflow fields."""
    n = {normalize_key(c) for c in columns}
    return (
        "number" in n
        and bool(n & {"summary", "short_description"})
        and bool(n & {"created", "opened"})
        and bool(n & {"assignment_group", "incident_state", "resolved"})
    )


def looks_like_ticket_csv(path: Path) -> bool:
    """Peek at the header line of a delimited text file."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            header = fh.readline()
    except OSError:
        return False
    return "," in header and looks_like_ticket_columns(next(csv.reader([header]), []))


def ticket_row_content(row: Dict[str, Any]) -> str:
    """The text a ticket is searched by: what happened, what was found and what was done (no system boilerplate)."""
    summary = first_present(row, ["Summary", "Short description"])
    parts = [summary.rstrip(".") + "." if summary else ""]
    for label, names in (
        ("", ["Details", "Description"]),
        ("Executive summary: ", ["Executive Summary"]),
        ("Findings: ", ["Findings"]),
        ("Actions taken: ", ["Actions taken"]),
        ("Work notes: ", ["Work notes"]),
    ):
        value = _AUTO_ASSIGN_NOTE.sub("", first_present(row, names))
        value = re.sub(r"\s+", " ", value).strip()
        if value and value.lower() not in {"nan", "none", "null"}:
            parts.append(label + value[:1500])
    return " ".join(p for p in parts if p)[:4000]


def process_csv_file(
    path: Path,
    tenant_id: str,
    knowledge_scope: str,
    collection_name: str,
    max_rows: Optional[int],
) -> Dict[str, Any]:
    if pd is None:
        raise RuntimeError("pandas is required to process CSV files")

    df = pd.read_csv(path)
    is_ticket_export = looks_like_ticket_columns(df.columns)
    if is_ticket_export:
        # keep every field as text: ticket numbers and notes must not be coerced
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if max_rows:
        df = df.head(max_rows)

    df.columns = [clean_text(c) for c in df.columns]
    rows = df.dropna(how="all").to_dict(orient="records")

    result = {
        "source_category": "generic_csv",
        "source_rows": {
            "client_ticket_records": [],
            "client_change_records": [],
            "client_request_records": [],
            "client_problem_records": [],
            "client_asset_records": [],
            "client_topology_records": [],
            "client_network_records": [],
            "client_knowledge_chunks": [],
        },
        "metadata": {"columns": list(df.columns), "rows": len(rows)},
        "record_count": 0,
    }

    # Simple category inference for CSV.
    if is_ticket_export:
        category = "ticket_dump"
    else:
        category = classify_excel_workbook(path, [{"sheet": "csv", "columns": list(df.columns)}])
        if category == "generic_excel":
            category = "generic_csv"
    result["source_category"] = category

    for idx, row in enumerate(rows, start=1):
        content = ticket_row_content(row) if is_ticket_export else build_row_content(row)
        if not is_meaningful_content(content):
            continue

        title = first_present(row, ["Short description", "Summary", "Title", "Name", "Hostname", "Service"]) or content[:250]
        chunk = make_knowledge_chunk(
            tenant_id,
            knowledge_scope,
            collection_name,
            category,
            "incident" if is_ticket_export else ("document" if category == "generic_csv" else category),
            str(path),
            "csv",
            idx,
            title,
            content,
            {"raw_json": row_to_json(row), "source_category": category},
        )
        if chunk:
            result["source_rows"]["client_knowledge_chunks"].append(chunk)

        if is_ticket_export:
            mapped = map_ticket_row(row, tenant_id, str(path), "csv", idx)
            if mapped:
                result["source_rows"]["client_ticket_records"].append(mapped)
        elif category == "asset_inventory":
            mapped = map_asset_row(row, tenant_id, str(path), "csv", idx)
            if mapped:
                result["source_rows"]["client_asset_records"].append(mapped)
        elif category == "network_inventory":
            mapped = map_network_row(row, tenant_id, str(path), "csv", idx)
            if mapped:
                result["source_rows"]["client_network_records"].append(mapped)
        elif category in {"vmware_topology", "topology_inventory"}:
            mapped = map_topology_row(row, tenant_id, str(path), "csv", idx)
            if mapped:
                result["source_rows"]["client_topology_records"].append(mapped)

        result["record_count"] += 1

    return result


def process_document_file(
    path: Path,
    tenant_id: str,
    knowledge_scope: str,
    collection_name: str,
) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = read_docx(path)
        category = "document"
    elif suffix == ".pptx":
        text = read_pptx(path)
        category = "topology_document" if any(x in path.name.lower() for x in ["network", "topology", "infrastructure", "data center", "dns"]) else "document"
    elif suffix in {".txt", ".md"}:
        text = read_text_file(path)
        category = "document"
    elif suffix == ".json":
        text = read_json_text(path)
        # Skip placeholder-only RAG tickets.json from prior bad ingestion.
        if path.name.lower() == "tickets.json" and detect_placeholder_only_json(path):
            return {
                "source_category": "skipped_placeholder_json",
                "source_rows": empty_source_rows(),
                "metadata": {"skipped_reason": "placeholder_only_normalized_rag_file"},
                "record_count": 0,
            }
        category = "json_document"
    else:
        text = ""
        category = "document"

    rows = empty_source_rows()
    if is_meaningful_content(text, min_words=8):
        chunk = make_knowledge_chunk(
            tenant_id,
            knowledge_scope,
            collection_name,
            category,
            "topology" if "topology" in category else "document",
            str(path),
            "",
            None,
            path.stem,
            text,
            {"source_file_name": path.name, "source_category": category},
        )
        if chunk:
            rows["client_knowledge_chunks"].append(chunk)

    return {
        "source_category": category,
        "source_rows": rows,
        "metadata": {"chars": len(text), "source_category": category},
        "record_count": 1 if text else 0,
    }


def empty_source_rows() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "client_ticket_records": [],
        "client_change_records": [],
        "client_request_records": [],
        "client_problem_records": [],
        "client_asset_records": [],
        "client_topology_records": [],
        "client_network_records": [],
        "client_knowledge_chunks": [],
    }


def detect_placeholder_only_json(path: Path, sample_size: int = 10000) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return False

    if not isinstance(data, list) or not data:
        return False

    checked = 0
    placeholders = 0
    meaningful = 0

    for row in data[:sample_size]:
        if not isinstance(row, dict):
            continue
        checked += 1
        content = clean_text(row.get("content"))
        title = clean_text(row.get("title"))
        classification = clean_text(row.get("classification"))
        combined = " ".join([content, title, classification]).strip()

        if looks_like_placeholder(content) or looks_like_placeholder(combined):
            placeholders += 1
        if is_meaningful_content(combined):
            meaningful += 1

    if checked == 0:
        return False

    return placeholders / checked >= 0.95 and meaningful == 0


def process_file(
    path: Path,
    tenant_id: str,
    knowledge_scope: str,
    collection_name: str,
    max_rows_per_sheet: Optional[int],
) -> Dict[str, Any]:
    suffix = path.suffix.lower()

    if suffix in {".xlsx", ".xls"}:
        return process_excel_file(path, tenant_id, knowledge_scope, collection_name, max_rows_per_sheet)

    if suffix == ".csv" or (suffix == ".txt" and looks_like_ticket_csv(path)):
        return process_csv_file(path, tenant_id, knowledge_scope, collection_name, max_rows_per_sheet)

    if suffix in {".docx", ".pptx", ".txt", ".md", ".json"}:
        return process_document_file(path, tenant_id, knowledge_scope, collection_name)

    return {
        "source_category": "unsupported",
        "source_rows": empty_source_rows(),
        "metadata": {"skipped_reason": "unsupported_extension", "extension": suffix},
        "record_count": 0,
    }


def make_source_file_row(
    path: Path,
    tenant_id: str,
    knowledge_scope: str,
    collection_name: str,
    source_category: str,
    record_count: int,
    status: str,
    metadata: Dict[str, Any],
    error: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        checksum = file_checksum(path)
    except Exception:
        checksum = ""

    return {
        "source_file_id": sha_id(tenant_id, str(path), checksum, prefix="src_"),
        "tenant_id": tenant_id,
        "knowledge_scope": knowledge_scope,
        "collection_name": collection_name,
        "source_file": str(path),
        "file_name": path.name,
        "file_extension": path.suffix.lower(),
        "source_category": source_category,
        "checksum": checksum,
        "file_size_bytes": path.stat().st_size if path.exists() else None,
        "record_count": record_count,
        "ingestion_status": status,
        "error": error,
        "ingested_at": utc_now(),
        "metadata_json": metadata,
    }


def flatten_counts(rows_by_table: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    return {table: len(rows) for table, rows in rows_by_table.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Tenant-aware client RAG/source sync to Postgres.")
    parser.add_argument("--tenant-id", default="client_a")
    parser.add_argument("--manifest", default="data/clients/client_a/governance/client_data_manifest.json")
    parser.add_argument("--source-root", default=None)
    parser.add_argument("--max-rows-per-sheet", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-json", action="store_true", help="Also process JSON files. tickets.json placeholder file is skipped automatically.")
    args = parser.parse_args()

    tenant_id = args.tenant_id
    manifest = load_manifest(args.manifest) if args.manifest else {}

    source_root = Path(args.source_root or manifest.get("current_source_root") or manifest.get("rag_root") or "data/rag")
    if not source_root.exists():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    governance = manifest.get("knowledge_governance", {}) if isinstance(manifest, dict) else {}
    knowledge_scope = manifest.get("knowledge_scope_default") or "client_private"
    collection_name = f"{slug(tenant_id)}_index"

    run_id = sha_id(tenant_id, str(source_root), utc_now().isoformat(), prefix="run_")
    started_at = utc_now()

    files = discover_files(source_root)
    if not args.include_json:
        files = [f for f in files if f.suffix.lower() != ".json"]

    max_rows = args.max_rows_per_sheet if args.max_rows_per_sheet and args.max_rows_per_sheet > 0 else None

    all_rows = empty_source_rows()
    source_file_rows: List[Dict[str, Any]] = []
    file_summaries = []
    counters = Counters()
    counters["files_discovered"] = len(files)

    print("AI Ops Client RAG to Postgres Sync")
    print("=" * 60)
    print(f"Tenant          : {tenant_id}")
    print(f"Source Root     : {source_root}")
    print(f"Manifest        : {args.manifest}")
    print(f"Knowledge Scope : {knowledge_scope}")
    print(f"Collection      : {collection_name}")
    print(f"Files Found     : {len(files)}")
    print(f"Dry Run         : {args.dry_run}")
    print()

    for path in files:
        print(f"Processing: {path}")
        try:
            result = process_file(path, tenant_id, knowledge_scope, collection_name, max_rows)
            category = result["source_category"]
            rows_by_table = result["source_rows"]
            metadata = result.get("metadata", {})
            record_count = int(result.get("record_count", 0))

            for table, rows in rows_by_table.items():
                all_rows[table].extend(rows)

            source_file_rows.append(
                make_source_file_row(
                    path,
                    tenant_id,
                    knowledge_scope,
                    collection_name,
                    category,
                    record_count,
                    "skipped" if category.startswith("skipped") else "ok",
                    metadata,
                )
            )

            file_summary = {
                "file": str(path),
                "source_category": category,
                "record_count": record_count,
                "rows_by_table": flatten_counts(rows_by_table),
                "metadata": metadata,
            }
            file_summaries.append(file_summary)
            counters.inc("files_processed")
            print(f"  -> {category}; records={record_count}; chunks={len(rows_by_table['client_knowledge_chunks'])}")

        except Exception as e:
            err = str(e)
            print(f"  ERROR: {err}")
            counters.inc("files_failed")
            source_file_rows.append(
                make_source_file_row(
                    path,
                    tenant_id,
                    knowledge_scope,
                    collection_name,
                    "failed",
                    0,
                    "failed",
                    {"traceback": traceback.format_exc()},
                    error=err,
                )
            )
            file_summaries.append({"file": str(path), "source_category": "failed", "error": err})

    # Add source file rows to write set
    all_rows["client_source_files"] = source_file_rows  # type: ignore[index]

    summary = {
        "tenant_id": tenant_id,
        "source_root": str(source_root),
        "manifest": args.manifest,
        "governance": governance,
        "files_discovered": len(files),
        "files_processed": counters.get("files_processed", 0),
        "files_failed": counters.get("files_failed", 0),
        "table_counts": {table: len(rows) for table, rows in all_rows.items()},
        "file_summaries": file_summaries,
    }

    run_row = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "input_root": str(source_root),
        "manifest_path": args.manifest,
        "status": "dry_run" if args.dry_run else "running",
        "files_discovered": len(files),
        "files_processed": counters.get("files_processed", 0),
        "files_failed": counters.get("files_failed", 0),
        "tickets_created": len(all_rows["client_ticket_records"]),
        "changes_created": len(all_rows["client_change_records"]),
        "requests_created": len(all_rows["client_request_records"]),
        "problems_created": len(all_rows["client_problem_records"]),
        "assets_created": len(all_rows["client_asset_records"]),
        "topology_created": len(all_rows["client_topology_records"]),
        "network_created": len(all_rows["client_network_records"]),
        "knowledge_chunks_created": len(all_rows["client_knowledge_chunks"]),
        "started_at": started_at,
        "completed_at": utc_now(),
        "summary_json": summary,
    }

    print()
    print("Prepared row counts")
    print("-" * 60)
    for table, rows in all_rows.items():
        print(f"{table:30s}: {len(rows)}")
    print(f"{'client_ingestion_runs':30s}: 1")
    print()

    if args.dry_run:
        print("Dry run only. No database changes made.")
        return 0

    conn = get_conn()
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            create_tables(cur)

            # Upsert source files first, then typed records/chunks, then run.
            upsert_rows(cur, "client_source_files", "source_file_id", source_file_rows)
            upsert_rows(cur, "client_ticket_records", "record_id", all_rows["client_ticket_records"])
            upsert_rows(cur, "client_change_records", "record_id", all_rows["client_change_records"])
            upsert_rows(cur, "client_request_records", "record_id", all_rows["client_request_records"])
            upsert_rows(cur, "client_problem_records", "record_id", all_rows["client_problem_records"])
            upsert_rows(cur, "client_asset_records", "record_id", all_rows["client_asset_records"])
            upsert_rows(cur, "client_topology_records", "record_id", all_rows["client_topology_records"])
            upsert_rows(cur, "client_network_records", "record_id", all_rows["client_network_records"])
            upsert_rows(cur, "client_knowledge_chunks", "chunk_id", all_rows["client_knowledge_chunks"])

            run_row["status"] = "ok"
            run_row["completed_at"] = utc_now()
            upsert_rows(cur, "client_ingestion_runs", "run_id", [run_row])

        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("Sync completed successfully.")
    # New or changed chunks have no vector yet; embed them so they are searchable (resumable, safe to skip).
    try:
        import pg_vector_store
        pg_vector_store.ensure_schema()
        print("Embedded rows:", pg_vector_store.backfill())
    except Exception as exc:
        print(f"WARNING: vector indexing skipped ({exc}). Run: python pg_vector_store.py --backfill")
    print()
    print("Validation commands:")
    print(r'docker exec -it aiops-postgres psql -U aiops_user -d aiops -c "\dt client_*"')
    print(r'docker exec -it aiops-postgres psql -U aiops_user -d aiops -c "SELECT tenant_id, COUNT(*) FROM client_ticket_records GROUP BY tenant_id;"')
    print(r'docker exec -it aiops-postgres psql -U aiops_user -d aiops -c "SELECT tenant_id, knowledge_type, COUNT(*) FROM client_knowledge_chunks GROUP BY tenant_id, knowledge_type ORDER BY COUNT(*) DESC;"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
