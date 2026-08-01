"""
POC End-to-End Seed Orchestrator
Bootstraps the platform for a client-facing POC demonstration:
  1. Registers POC CIs in the CMDB
  2. Ingests client ticket data (if provided)
  3. Seeds the knowledge base with POC-relevant patterns
  4. Runs reactive scenarios (ticket → analysis → runbook)
  5. Runs proactive scenarios (telemetry escalation → incident → analysis)
  6. Prints a full status summary

Usage:
  python poc/seed_poc.py --all
  python poc/seed_poc.py --scenario 1 --mode reactive
  python poc/seed_poc.py --scenario 3 --mode proactive --speed fast
  python poc/seed_poc.py --all --tickets data/clients/poc_client/tickets.xlsx
  python poc/seed_poc.py --setup-only      # only register CIs and seed knowledge
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from poc.scenarios import ALL_SCENARIOS, SCENARIOS_BY_ID, Scenario
from poc.telemetry_simulator import run_simulation as run_proactive_sim

BACKEND_URL = "http://localhost:8001"
SESSION = requests.Session()
SESSION.headers["Content-Type"] = "application/json"

# ---------------------------------------------------------------------------
# POC CI Registry — what we register in the CMDB for the demo environment
# ---------------------------------------------------------------------------

POC_CIS = [
    {
        "ci_name": "linux-lab-01",
        "display_name": "App Server (linux-lab-01)",
        "tier": "production",
        "criticality": "high",
        "redundancy_type": "active-standby",
        "services": ["customer-portal", "payment-api", "sso-idp", "report-generator"],
        "os": "Ubuntu 22.04",
        "role": "application_server",
    },
    {
        "ci_name": "linux-lab-02",
        "display_name": "Data Node (linux-lab-02)",
        "tier": "production",
        "criticality": "high",
        "redundancy_type": "active-passive",
        "services": ["postgres-replica", "etl-engine", "audit-logger", "order-processor"],
        "os": "Ubuntu 22.04",
        "role": "data_node",
    },
    {
        "ci_name": "network-lab-01",
        "display_name": "Network Gateway (network-lab-01)",
        "tier": "production",
        "criticality": "critical",
        "redundancy_type": "standalone",
        "services": ["north-south-gateway", "vpn-concentrator", "dns-resolver"],
        "os": "Ubuntu 22.04",
        "role": "network_gateway",
    },
]

# ---------------------------------------------------------------------------
# POC Knowledge Seeds — pre-load key runbooks from scenario expected resolutions
# ---------------------------------------------------------------------------

def _build_knowledge_seeds() -> list[dict]:
    seeds = []
    for s in ALL_SCENARIOS:
        seeds.append({
            "classification": s.classification,
            "title":          f"[POC] {s.title}",
            "description":    s.ticket_text[:300].strip(),
            "resolution":     s.expected_resolution_summary,
            "playbook_steps": [
                step.strip()
                for step in s.expected_resolution_summary.split(";")
                if step.strip()
            ],
            "confidence":     0.85,
            "source":         "poc_seed",
            "labels":         s.jira_labels,
        })
    return seeds


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict, timeout: int = 30, label: str = "") -> dict | None:
    try:
        r = SESSION.post(f"{BACKEND_URL}{path}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        print(f"  [ERROR] Cannot connect to {BACKEND_URL}. Is the backend running?")
        return None
    except Exception as e:
        print(f"  [WARN] POST {path}{' (' + label + ')' if label else ''}: {e}")
        return None


def _get(path: str, timeout: int = 15) -> Any:
    try:
        r = SESSION.get(f"{BACKEND_URL}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] GET {path}: {e}")
        return None


def wait_for_backend(max_wait: int = 120) -> bool:
    print(f"[health] Waiting for backend at {BACKEND_URL} ...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(f"{BACKEND_URL}/health", timeout=5)
            if r.status_code == 200:
                print(f"[health] Backend is up. ✓")
                return True
        except Exception:
            pass
        print("  ... not ready yet, retrying in 5s")
        time.sleep(5)
    print(f"[health] Backend did not become available in {max_wait}s.")
    return False


# ---------------------------------------------------------------------------
# Setup steps
# ---------------------------------------------------------------------------

def register_cis() -> int:
    print("\n[setup] Registering POC CIs in CMDB...")
    registered = 0
    for ci in POC_CIS:
        result = _post("/cmdb/ci", ci, label=ci["ci_name"])
        if result:
            print(f"  ✓ {ci['ci_name']} registered ({ci['criticality']}/{ci['redundancy_type']})")
            registered += 1
        else:
            print(f"  ✗ {ci['ci_name']} failed (may already exist)")
        time.sleep(0.2)
    print(f"[setup] {registered}/{len(POC_CIS)} CIs registered.")
    return registered


def seed_knowledge() -> int:
    print("\n[setup] Seeding knowledge base with POC runbooks...")
    seeds = _build_knowledge_seeds()
    pushed = 0
    for seed in seeds:
        result = _post("/knowledge_candidates", seed, label=seed["classification"])
        if result:
            pushed += 1
        time.sleep(0.1)
    print(f"[setup] {pushed}/{len(seeds)} knowledge candidates seeded.")
    return pushed


def seed_evidence_baselines() -> None:
    print("\n[setup] Seeding safety gate evidence baselines...")
    evidence_payloads = [
        {
            "evidence_type": "blast_radius",
            "data": {"assessment": "contained", "affected_users": 0, "affected_services": 1, "score": 0.2},
        },
        {
            "evidence_type": "rollback_readiness",
            "data": {"rollback_available": True, "snapshot_age_minutes": 15, "validated": True},
        },
        {
            "evidence_type": "change_conflict",
            "data": {"conflicts": [], "change_window_clear": True},
        },
        {
            "evidence_type": "error_budget_slo",
            "data": {"budget_remaining_percent": 78, "burn_rate": 0.8, "slo_at_risk": False},
        },
    ]
    for payload in evidence_payloads:
        _post("/evidence/submit", payload, label=payload["evidence_type"])
        time.sleep(0.1)
    print("[setup] Evidence baselines seeded.")


# ---------------------------------------------------------------------------
# Reactive scenario runner
# ---------------------------------------------------------------------------

def run_reactive_scenario(scenario: Scenario, verbose: bool = False) -> dict:
    print(f"\n{'─' * 60}")
    print(f"[S{scenario.id:02d}] REACTIVE — {scenario.title}")
    print(f"      Category: {scenario.category} | Priority: {scenario.priority}")
    print(f"{'─' * 60}")

    if verbose:
        print(f"\n  Ticket:\n  {scenario.ticket_text[:400]}...\n")

    print("  📨 Submitting ticket for AI analysis...")
    result = _post(
        "/analyze",
        {
            "text":     scenario.ticket_text,
            "source":   "poc_simulation",
            "priority": scenario.priority,
            "ci":       scenario.ci_target,
        },
        timeout=60,
        label=f"S{scenario.id:02d}",
    )

    if not result:
        print(f"  ✗ Analysis failed for S{scenario.id:02d}")
        return {"status": "failed", "scenario_id": scenario.id}

    classification = result.get("classification", result.get("category", "unknown"))
    confidence = result.get("confidence", result.get("confidence_score", 0.0))
    runbook_steps = result.get("runbook_steps", result.get("playbook_steps", []))
    runbook_count = len(runbook_steps) if isinstance(runbook_steps, list) else 0

    print(f"  ✅ Analysis complete:")
    print(f"     Classification : {classification}")
    print(f"     Confidence     : {confidence:.0%}" if isinstance(confidence, float) else f"     Confidence: {confidence}")
    print(f"     Runbook steps  : {runbook_count}")

    if verbose and runbook_steps:
        print("     Steps:")
        for i, step in enumerate(runbook_steps[:4], 1):
            step_text = step if isinstance(step, str) else step.get("action", str(step))
            print(f"       {i}. {step_text}")

    expected = scenario.classification
    match = classification == expected or expected in str(classification)
    match_label = "✓ correct" if match else f"⚠ expected '{expected}'"
    print(f"     Expected match : {match_label}")

    return {
        "status": "success",
        "scenario_id": scenario.id,
        "classification": classification,
        "confidence": confidence,
        "runbook_steps": runbook_count,
        "classification_match": match,
        "raw": result,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_poc(
    scenario_ids: list[int],
    mode: str,
    speed: str,
    tickets_file: Path | None,
    setup_only: bool,
    verbose: bool,
) -> None:
    print(f"\n{'=' * 60}")
    print("  AI SRE PLATFORM — POC SEED ORCHESTRATOR")
    print(f"{'=' * 60}")
    print(f"  Backend  : {BACKEND_URL}")
    print(f"  Scenarios: {scenario_ids if not setup_only else 'SETUP ONLY'}")
    print(f"  Mode     : {mode}")
    print(f"  Started  : {datetime.utcnow().isoformat()} UTC")
    print(f"{'=' * 60}\n")

    # Wait for backend
    if not wait_for_backend():
        print("[abort] Backend not available. Exiting.")
        sys.exit(1)

    # Setup phase
    register_cis()
    seed_knowledge()
    seed_evidence_baselines()

    # Ingest client tickets if provided
    if tickets_file and tickets_file.exists():
        print(f"\n[tickets] Ingesting client tickets from {tickets_file}...")
        from poc.ingest_client_tickets import ingest
        ingest(file_path=tickets_file, push_to_api=True)

    if setup_only:
        print("\n[done] Setup complete. Platform is ready for demo.")
        return

    # Scenario runs
    reactive_results: list[dict] = []
    proactive_scenario_ids: list[int] = []

    for sid in scenario_ids:
        if sid not in SCENARIOS_BY_ID:
            print(f"[WARN] Unknown scenario ID {sid}, skipping.")
            continue
        scenario = SCENARIOS_BY_ID[sid]
        run_reactive = mode in ("reactive", "both") and scenario.detection_mode in ("reactive", "both")
        run_proactive = mode in ("proactive", "both") and scenario.detection_mode in ("proactive", "both")

        if run_reactive:
            result = run_reactive_scenario(scenario, verbose=verbose)
            reactive_results.append(result)
            time.sleep(1)

        if run_proactive and scenario.telemetry_events:
            proactive_scenario_ids.append(sid)

    # Proactive batch
    proactive_results: dict[int, list] = {}
    if proactive_scenario_ids:
        print(f"\n[proactive] Starting telemetry simulation for scenarios: {proactive_scenario_ids}")
        proactive_results = run_proactive_sim(
            scenario_ids=proactive_scenario_ids,
            speed=speed,
            seed_baseline=True,
            verbose=verbose,
        )

    # Final summary
    _print_summary(scenario_ids, reactive_results, proactive_results)


def _print_summary(
    scenario_ids: list[int],
    reactive: list[dict],
    proactive: dict[int, list],
) -> None:
    print(f"\n{'=' * 60}")
    print("  POC RESULTS SUMMARY")
    print(f"{'=' * 60}")

    reactive_ok = sum(1 for r in reactive if r.get("status") == "success")
    reactive_match = sum(1 for r in reactive if r.get("classification_match"))
    proactive_total = sum(len(v) for v in proactive.values())

    print(f"  Reactive scenarios run     : {len(reactive)}")
    print(f"  Reactive analyses OK       : {reactive_ok}/{len(reactive)}")
    print(f"  Classification accuracy    : {reactive_match}/{len(reactive)}")
    print(f"  Proactive incidents raised : {proactive_total}")
    print()

    for sid in scenario_ids:
        if sid not in SCENARIOS_BY_ID:
            continue
        s = SCENARIOS_BY_ID[sid]
        r_result = next((r for r in reactive if r.get("scenario_id") == sid), None)
        p_incidents = proactive.get(sid, [])

        parts = []
        if r_result:
            icon = "✅" if r_result.get("classification_match") else "⚠️ "
            conf = r_result.get("confidence", 0)
            conf_str = f"{conf:.0%}" if isinstance(conf, float) else str(conf)
            parts.append(f"Reactive {icon} ({conf_str})")
        if p_incidents:
            parts.append(f"Proactive ✅ ({len(p_incidents)} incident)")
        elif s.detection_mode in ("proactive", "both") and s.telemetry_events:
            parts.append("Proactive ⚠️  (no incidents promoted)")

        status_str = " | ".join(parts) if parts else "—"
        category_badge = s.category.replace("_", "-").upper()
        print(f"  S{sid:02d} [{category_badge:<15}] {s.title:<40} {status_str}")

    print(f"\n{'=' * 60}")
    print("  Platform is ready. Open the Streamlit UI to explore results.")
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="POC seed orchestrator for AI SRE Platform")
    parser.add_argument(
        "--scenario",
        type=int,
        help="Run a single scenario by ID (1–10)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all 10 scenarios",
    )
    parser.add_argument(
        "--mode",
        choices=["reactive", "proactive", "both"],
        default="both",
        help="Detection mode to exercise (default: both)",
    )
    parser.add_argument(
        "--speed",
        choices=["slow", "demo", "fast", "turbo"],
        default="demo",
        help="Telemetry escalation speed for proactive scenarios",
    )
    parser.add_argument(
        "--tickets",
        help="Path to client ticket export file (CSV/JSON/XLSX) for ingestion",
    )
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="Only register CIs and seed knowledge; do not run scenarios",
    )
    parser.add_argument(
        "--backend",
        default=BACKEND_URL,
        help=f"Backend URL (default: {BACKEND_URL})",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    global BACKEND_URL
    BACKEND_URL = args.backend.rstrip("/")

    if args.all:
        ids = list(range(1, 11))
    elif args.scenario:
        ids = [args.scenario]
    else:
        ids = list(range(1, 11))

    tickets_file = Path(args.tickets) if args.tickets else None

    run_poc(
        scenario_ids=ids,
        mode=args.mode,
        speed=args.speed,
        tickets_file=tickets_file,
        setup_only=args.setup_only,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
