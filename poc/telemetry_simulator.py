"""
Proactive Telemetry Simulator
Injects escalating metric events into the telemetry API to demonstrate proactive detection:
  1. Posts baseline metrics (normal operating values)
  2. Gradually escalates selected metrics through their breach thresholds
  3. Calls /telemetry/evaluate to trigger rule evaluation
  4. Calls /telemetry/promote to create incidents from fired rules
  5. Returns the list of incident IDs that were auto-promoted

Usage (standalone):
  python poc/telemetry_simulator.py --scenarios 3,7,10 --backend http://localhost:8001
  python poc/telemetry_simulator.py --all-proactive --speed fast
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

from poc.scenarios import ALL_SCENARIOS, SCENARIOS_BY_ID, Scenario, TelemetryEvent

BACKEND_URL = "http://localhost:8001"
SESSION = requests.Session()
SESSION.headers["Content-Type"] = "application/json"


# ---------------------------------------------------------------------------
# Speed profiles (seconds between escalation steps)
# ---------------------------------------------------------------------------

SPEED_PROFILES = {
    "slow":  {"step_delay": 8.0,  "eval_delay": 4.0},
    "demo":  {"step_delay": 3.0,  "eval_delay": 2.0},
    "fast":  {"step_delay": 1.0,  "eval_delay": 1.0},
    "turbo": {"step_delay": 0.2,  "eval_delay": 0.1},
}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict, timeout: int = 30) -> dict | None:
    try:
        r = SESSION.post(f"{BACKEND_URL}{path}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        print(f"  [ERROR] Cannot connect to backend at {BACKEND_URL}. Is it running?")
        return None
    except Exception as e:
        print(f"  [WARN] POST {path} → {e}")
        return None


def _get(path: str, timeout: int = 15) -> dict | None:
    try:
        r = SESSION.get(f"{BACKEND_URL}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] GET {path} → {e}")
        return None


# ---------------------------------------------------------------------------
# Telemetry event posting
# ---------------------------------------------------------------------------

def post_telemetry_event(event: TelemetryEvent, value: float, note: str = "") -> bool:
    payload = {
        "metric":     event.metric,
        "value":      round(value, 3),
        "host":       event.host,
        "unit":       event.unit,
        "tags":       {**event.tags, "poc_simulation": "true"},
        "timestamp":  datetime.utcnow().isoformat() + "Z",
    }
    result = _post("/telemetry/event", payload)
    label = f"[{event.host}] {event.metric}={value} {event.unit}"
    if result:
        print(f"  ↑ {label}{' — ' + note if note else ''}")
        return True
    else:
        print(f"  ✗ {label} (failed)")
        return False


def trigger_evaluation() -> dict:
    print("  ⚡ Evaluating telemetry rules...")
    result = _post("/telemetry/evaluate", {}, timeout=30) or {}
    fired = result.get("fired_count", result.get("alerts_fired", 0))
    print(f"  ⚡ Rules evaluated — {fired} alert(s) fired")
    return result


def promote_incidents(limit: int = 10) -> list[dict]:
    print("  🔺 Promoting telemetry incidents...")
    result = _post("/telemetry/promote", {"limit": limit}, timeout=30) or {}
    promoted = result.get("promoted", result.get("incidents", []))
    if isinstance(promoted, list):
        print(f"  🔺 {len(promoted)} incident(s) promoted")
        return promoted
    count = result.get("promoted_count", 0)
    print(f"  🔺 {count} incident(s) promoted")
    return []


def fetch_recent_incidents(limit: int = 5) -> list[dict]:
    result = _get(f"/telemetry/incidents?limit={limit}") or {}
    incidents = result if isinstance(result, list) else result.get("incidents", [])
    return incidents


# ---------------------------------------------------------------------------
# Baseline seeding (post normal values before escalation)
# ---------------------------------------------------------------------------

def seed_baselines(scenarios: list[Scenario]) -> None:
    print("\n[baseline] Seeding normal operating metrics...")
    for scenario in scenarios:
        for event in scenario.telemetry_events:
            post_telemetry_event(event, event.baseline, note="baseline")
            time.sleep(0.1)
    print("[baseline] Done.\n")


# ---------------------------------------------------------------------------
# Single scenario escalation
# ---------------------------------------------------------------------------

def run_proactive_scenario(
    scenario: Scenario,
    speed: str = "demo",
    verbose: bool = False,
) -> list[dict]:
    profile = SPEED_PROFILES.get(speed, SPEED_PROFILES["demo"])
    step_delay = profile["step_delay"]
    eval_delay = profile["eval_delay"]

    if not scenario.telemetry_events:
        print(f"  [SKIP] Scenario {scenario.id} has no telemetry events (reactive only).")
        return []

    print(f"\n{'─' * 60}")
    print(f"[S{scenario.id:02d}] {scenario.title}")
    print(f"      Category: {scenario.category} | CI: {scenario.ci_target}")
    print(f"{'─' * 60}")

    threshold_breached = False

    for event in scenario.telemetry_events:
        print(f"\n  Metric: {event.metric} (threshold: {event.threshold} {event.unit})")
        print(f"  Escalation: {event.baseline} → {event.escalation_steps[-1]} over {len(event.escalation_steps)} steps")

        for step_idx, value in enumerate(event.escalation_steps):
            breach_this_step = value >= event.threshold
            note = "⚠️  THRESHOLD BREACHED" if breach_this_step and not threshold_breached else ""
            if breach_this_step:
                threshold_breached = True

            post_telemetry_event(event, value, note=note)
            time.sleep(step_delay)

    # Evaluate rules and promote
    time.sleep(eval_delay)
    trigger_evaluation()
    time.sleep(eval_delay)
    promoted = promote_incidents(limit=3)

    if promoted:
        print(f"\n  ✅ Proactive incident(s) created for S{scenario.id:02d}:")
        for inc in promoted:
            inc_id = inc.get("incident_id", inc.get("id", "unknown"))
            title = inc.get("title", inc.get("summary", ""))
            print(f"     • [{inc_id}] {title}")
    else:
        print(f"\n  ℹ️  No incidents promoted (rules may need tuning or API not available).")
        # Show what would have been promoted based on recent incidents
        recent = fetch_recent_incidents(limit=3)
        for inc in recent:
            print(f"     Recent: [{inc.get('incident_id', '?')}] {inc.get('title', '')}")

    return promoted


# ---------------------------------------------------------------------------
# Multi-scenario runner
# ---------------------------------------------------------------------------

def run_simulation(
    scenario_ids: list[int],
    speed: str = "demo",
    seed_baseline: bool = True,
    verbose: bool = False,
) -> dict[int, list[dict]]:

    proactive_scenarios = [
        SCENARIOS_BY_ID[sid]
        for sid in scenario_ids
        if sid in SCENARIOS_BY_ID
        and SCENARIOS_BY_ID[sid].detection_mode in ("proactive", "both")
        and SCENARIOS_BY_ID[sid].telemetry_events
    ]

    if not proactive_scenarios:
        print("[sim] No proactive scenarios selected or none have telemetry events.")
        return {}

    print(f"\n[sim] Running proactive telemetry simulation")
    print(f"[sim] Scenarios: {[s.id for s in proactive_scenarios]}")
    print(f"[sim] Speed: {speed}")
    print(f"[sim] Backend: {BACKEND_URL}\n")

    if seed_baseline:
        seed_baselines(proactive_scenarios)

    results: dict[int, list[dict]] = {}
    for scenario in proactive_scenarios:
        promoted = run_proactive_scenario(scenario, speed=speed, verbose=verbose)
        results[scenario.id] = promoted

    # Summary
    print(f"\n{'=' * 60}")
    print("PROACTIVE SIMULATION SUMMARY")
    print(f"{'=' * 60}")
    total_incidents = sum(len(v) for v in results.values())
    print(f"Scenarios run         : {len(proactive_scenarios)}")
    print(f"Incidents promoted    : {total_incidents}")
    for sid, incidents in results.items():
        s = SCENARIOS_BY_ID[sid]
        status = f"{len(incidents)} incident(s) created" if incidents else "no incidents promoted"
        print(f"  S{sid:02d} {s.title:<45} → {status}")
    print(f"{'=' * 60}\n")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Proactive telemetry simulator for POC demonstration")
    parser.add_argument(
        "--scenarios",
        help="Comma-separated scenario IDs (e.g. 3,7,10). Default: all proactive.",
    )
    parser.add_argument(
        "--all-proactive",
        action="store_true",
        help="Run all proactive/both-mode scenarios",
    )
    parser.add_argument(
        "--speed",
        choices=["slow", "demo", "fast", "turbo"],
        default="demo",
        help="Escalation speed (default: demo ~3s/step)",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="Skip baseline metric seeding",
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
    SESSION.headers["X-Backend"] = BACKEND_URL

    if args.all_proactive:
        ids = [s.id for s in ALL_SCENARIOS if s.detection_mode in ("proactive", "both")]
    elif args.scenarios:
        try:
            ids = [int(x.strip()) for x in args.scenarios.split(",")]
        except ValueError:
            print("[ERROR] --scenarios must be comma-separated integers e.g. 3,7,10")
            sys.exit(1)
    else:
        ids = [s.id for s in ALL_SCENARIOS if s.detection_mode in ("proactive", "both")]

    run_simulation(
        scenario_ids=ids,
        speed=args.speed,
        seed_baseline=not args.no_baseline,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
