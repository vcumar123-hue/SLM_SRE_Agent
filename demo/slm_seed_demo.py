"""
slm_seed_demo.py
----------------
Comprehensive demo seed script for the AI SRE Platform (SLM).
Prepares everything needed for a full platform showcase demo.
Backend: http://127.0.0.1:8001
No external dependencies — stdlib only.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

# Force UTF-8 output so Unicode symbols don't crash on Windows console/subprocess
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------
GREEN  = "\033[92m"
AMBER  = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

BASE_URL = "http://127.0.0.1:8001"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict) -> tuple[int, dict | None]:
    """POST JSON to BASE_URL/path. Returns (status_code, response_dict|None)."""
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, None
    except Exception as exc:
        print(f"  {AMBER}[WARN] Request to {path} failed: {exc}{RESET}")
        return 0, None


def ok(msg: str) -> None:
    print(f"  {GREEN}[OK]  {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"  {AMBER}[WARN] {msg}{RESET}")


def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'='*60}{RESET}")


# ---------------------------------------------------------------------------
# SECTION 1: CMDB
# ---------------------------------------------------------------------------

CMDB_CIS = [
    {
        "ci_name": "linux-lab-01",
        "environment": "production",
        "redundancy_type": "active-active",
        "business_criticality": 3,
        "assignment_group": "Platform Engineering",
        "notes": "Web tier — nginx, redundant (active-active). Lower risk — automated remediation eligible.",
    },
    {
        "ci_name": "linux-lab-02",
        "environment": "production",
        "redundancy_type": "standalone",
        "business_criticality": 2,
        "assignment_group": "Application Engineering",
        "notes": "App tier — Python FastAPI, standalone. Critical — human approval required.",
    },
    {
        "ci_name": "network-lab-01",
        "environment": "production",
        "redundancy_type": "active-passive",
        "business_criticality": 3,
        "assignment_group": "Data Engineering",
        "notes": "Network/config tier — PostgreSQL, active-passive. Medium criticality.",
    },
]


def seed_cmdb() -> int:
    section("SECTION 1: CMDB — Register lab machines")
    registered = 0
    for ci in CMDB_CIS:
        status, resp = _post("/cmdb/ci", ci)
        if status in (200, 201):
            ok(f"Registered CI: {ci['ci_name']}  (status {status})")
            registered += 1
        elif status == 409:
            ok(f"CI already exists: {ci['ci_name']}  (409 conflict — skipping)")
            registered += 1
        else:
            warn(f"Failed to register {ci['ci_name']}  (status {status}, resp={resp})")
    return registered


# ---------------------------------------------------------------------------
# SECTION 2: Telemetry
# ---------------------------------------------------------------------------

TELEMETRY_EVENTS = [
    # linux-lab-02 — CPU saturation (critical path)
    {
        "metric": "cpu_percent",
        "value": 96.5,
        "resource": "linux-lab-02",
        "service": "claims-api",
        "environment": "production",
        "source": "prometheus",
        "labels": {"alertname": "CpuSaturation", "severity": "critical"},
    },
    {
        "metric": "cpu_percent",
        "value": 97.2,
        "resource": "linux-lab-02",
        "service": "claims-api",
        "environment": "production",
        "source": "prometheus",
        "labels": {"alertname": "CpuSaturation", "severity": "critical"},
    },
    {
        "metric": "cpu_percent",
        "value": 95.8,
        "resource": "linux-lab-02",
        "service": "claims-api",
        "environment": "production",
        "source": "prometheus",
        "labels": {"alertname": "CpuSaturation", "severity": "critical"},
    },
    # linux-lab-02 — memory pressure (critical path)
    {
        "metric": "memory_percent",
        "value": 94.1,
        "resource": "linux-lab-02",
        "service": "claims-api",
        "environment": "production",
        "source": "prometheus",
        "labels": {"alertname": "MemoryPressure", "severity": "high"},
    },
    {
        "metric": "memory_percent",
        "value": 93.8,
        "resource": "linux-lab-02",
        "service": "claims-api",
        "environment": "production",
        "source": "prometheus",
        "labels": {"alertname": "MemoryPressure", "severity": "high"},
    },
    {
        "metric": "memory_percent",
        "value": 95.2,
        "resource": "linux-lab-02",
        "service": "claims-api",
        "environment": "production",
        "source": "prometheus",
        "labels": {"alertname": "MemoryPressure", "severity": "high"},
    },
    # linux-lab-01 — API latency (automated path)
    {
        "metric": "api_latency_ms",
        "value": 2900,
        "resource": "linux-lab-01",
        "service": "web-frontend",
        "environment": "production",
        "source": "alertmanager",
        "labels": {"alertname": "ApiLatencySpike", "severity": "high"},
    },
    {
        "metric": "api_latency_ms",
        "value": 3200,
        "resource": "linux-lab-01",
        "service": "web-frontend",
        "environment": "production",
        "source": "alertmanager",
        "labels": {"alertname": "ApiLatencySpike", "severity": "high"},
    },
    {
        "metric": "api_latency_ms",
        "value": 2600,
        "resource": "linux-lab-01",
        "service": "web-frontend",
        "environment": "production",
        "source": "alertmanager",
        "labels": {"alertname": "ApiLatencySpike", "severity": "high"},
    },
    # network-lab-01 — disk pressure (drift demo)
    {
        "metric": "disk_percent",
        "value": 91.5,
        "resource": "network-lab-01",
        "service": "postgres-db",
        "environment": "production",
        "source": "prometheus",
        "labels": {"alertname": "DiskIOPressure", "severity": "medium"},
    },
    {
        "metric": "disk_percent",
        "value": 93.2,
        "resource": "network-lab-01",
        "service": "postgres-db",
        "environment": "production",
        "source": "prometheus",
        "labels": {"alertname": "DiskIOPressure", "severity": "medium"},
    },
]


def seed_telemetry() -> tuple[int, int]:
    section("SECTION 2: Telemetry — seed metrics & trigger incidents")
    posted = 0
    for ev in TELEMETRY_EVENTS:
        status, resp = _post("/telemetry/event", ev)
        if status in (200, 201, 202):
            ok(
                f"Posted {ev['metric']}={ev['value']} "
                f"for {ev['resource']} / {ev['service']}"
            )
            posted += 1
        else:
            warn(
                f"Failed event {ev['metric']} on {ev['resource']} "
                f"(status {status}, resp={resp})"
            )

    # Evaluate / trigger incident creation
    print(f"\n  {CYAN}Calling /telemetry/evaluate to trigger incident creation…{RESET}")
    status, resp = _post("/telemetry/evaluate", {"dry_run": False})
    incidents_created = 0
    if status in (200, 201, 202):
        if isinstance(resp, dict):
            incidents_created = resp.get("incidents_created", resp.get("created", 0))
        ok(f"Evaluate complete — incidents created: {incidents_created}  (status {status})")
    else:
        warn(f"/telemetry/evaluate returned status {status}, resp={resp}")

    return posted, incidents_created


# ---------------------------------------------------------------------------
# SECTION 3: Peer insights / RAG data
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RAG_CLIENTS_DIR = os.path.join(PROJECT_ROOT, "data", "rag", "clients")

CHECK_CLIENTS = ["client_a", "client_b", "client_c"]


def check_peer_data() -> list[str]:
    section("SECTION 3: Peer insights — RAG / backtesting data")
    found_clients: list[str] = []

    # Also report any other clients that exist in the directory
    all_clients: list[str] = []
    if os.path.isdir(RAG_CLIENTS_DIR):
        all_clients = [
            d for d in os.listdir(RAG_CLIENTS_DIR)
            if os.path.isdir(os.path.join(RAG_CLIENTS_DIR, d))
        ]

    for client in CHECK_CLIENTS:
        client_dir = os.path.join(RAG_CLIENTS_DIR, client)
        incidents_json = os.path.join(client_dir, "incidents.json")
        if os.path.isfile(incidents_json):
            ok(f"Peer data found for {client}  (incidents.json present)")
            found_clients.append(client)
        elif os.path.isdir(client_dir):
            files = os.listdir(client_dir)
            if files:
                ok(
                    f"Peer data found for {client}  "
                    f"(no incidents.json, but {len(files)} file(s): "
                    f"{', '.join(files[:3])}{'...' if len(files) > 3 else ''})"
                )
                found_clients.append(client)
            else:
                warn(f"Client dir exists but is empty: {client}")
        else:
            warn(f"No peer data directory found for {client}")

    # Report additional clients beyond the checked set
    extras = [c for c in sorted(all_clients) if c not in CHECK_CLIENTS]
    if extras:
        ok(
            f"Additional peer clients available for benchmarking: "
            f"{', '.join(extras)}"
        )
        found_clients.extend(extras)

    return found_clients


# ---------------------------------------------------------------------------
# SECTION 4: Automation registry candidates
# ---------------------------------------------------------------------------

AUTOMATION_CANDIDATES = [
    {
        "classification": "cpu_saturation",
        "name": "CPU Saturation Recovery",
        "description": (
            "Identify and kill runaway processes, clear cache, "
            "scale out if redundant"
        ),
        "confidence": 0.91,
        "automation_ready": True,
        "estimated_duration_minutes": 8,
    },
    {
        "classification": "memory_high",
        "name": "Memory Pressure Remediation",
        "description": (
            "Identify memory-intensive processes, trigger GC, "
            "consider restart or scale"
        ),
        "confidence": 0.87,
        "automation_ready": True,
        "estimated_duration_minutes": 6,
    },
    {
        "classification": "api_latency_spike",
        "name": "API Latency Recovery",
        "description": (
            "Flush connection pool, restart app tier, "
            "validate upstream dependencies"
        ),
        "confidence": 0.83,
        "automation_ready": True,
        "estimated_duration_minutes": 10,
    },
]


def seed_automation_registry() -> int:
    section("SECTION 4: Automation registry — seed candidates")
    seeded = 0
    for candidate in AUTOMATION_CANDIDATES:
        status, resp = _post("/automation/candidate", candidate)
        if status in (200, 201):
            ok(f"Registered candidate: {candidate['name']}  (status {status})")
            seeded += 1
        elif status == 409:
            ok(f"Candidate already exists: {candidate['name']}  (409 — skipping)")
            seeded += 1
        else:
            warn(
                f"Failed to register {candidate['name']}  "
                f"(status {status}, resp={resp})"
            )
    return seeded


# ---------------------------------------------------------------------------
# SECTION 5: Summary
# ---------------------------------------------------------------------------

def print_summary(
    ci_count: int,
    events_posted: int,
    incidents_created: int,
    peer_clients: list[str],
    registry_count: int,
) -> None:
    section("SECTION 5: Demo Readiness Summary")

    col_w = 32
    rows = [
        ("CMDB", f"{ci_count} CIs registered"),
        (
            "Telemetry",
            f"{events_posted} events posted  →  {incidents_created} incidents created",
        ),
        (
            "Peer data",
            (
                ", ".join(sorted(set(peer_clients))) if peer_clients
                else "none found — check data/rag/clients/"
            ),
        ),
        ("Registry candidates", f"{registry_count} seeded"),
    ]

    print()
    print(f"  {BOLD}{'Component':<{col_w}}Status{RESET}")
    print(f"  {'-' * (col_w + 40)}")
    for label, value in rows:
        color = GREEN if value and "none" not in value.lower() else AMBER
        print(f"  {BOLD}{label:<{col_w}}{RESET}{color}{value}{RESET}")

    print()
    print(
        f"  {BOLD}{GREEN}Demo ready.{RESET}  "
        f"Open {CYAN}http://localhost:8501{RESET} to begin."
    )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{BOLD}{CYAN}{'#'*60}{RESET}")
    print(f"{BOLD}{CYAN}  AI SRE Platform — Demo Seed Script{RESET}")
    print(f"{BOLD}{CYAN}  Target: {BASE_URL}{RESET}")
    print(f"{BOLD}{CYAN}{'#'*60}{RESET}")

    ci_count        = seed_cmdb()
    events, incidents = seed_telemetry()
    peer_clients    = check_peer_data()
    registry_count  = seed_automation_registry()

    print_summary(ci_count, events, incidents, peer_clients, registry_count)


if __name__ == "__main__":
    main()
