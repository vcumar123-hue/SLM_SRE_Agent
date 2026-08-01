"""
Nightly backtesting scheduler.

Run as a background process alongside the main app:
    python backtesting_scheduler.py          # runs at 00:00 local time
    python backtesting_scheduler.py --hour 2 --minute 30  # runs at 02:30

The scheduler hits POST /backtesting/run on the local API, so the main app
must be running.  Results are written by the backtesting engine itself;
the scheduler only records the schedule state and last-run outcome to:
    data/backtesting/scheduler_state.json
"""
from __future__ import annotations
import argparse
import json
import os
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

BACKEND = os.getenv("AIOPS_BACKEND_URL", "http://localhost:8000")
STATE_FILE = Path("data/backtesting/scheduler_state.json")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_state() -> Dict[str, Any]:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _trigger_backtest() -> Dict[str, Any]:
    url = f"{BACKEND}/backtesting/run"
    req = urllib.request.Request(url, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return {"http_status": r.status, "response": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        return {"http_status": e.code, "error": body}
    except Exception as exc:
        return {"http_status": 0, "error": str(exc)}


def run_scheduler(hour: int = 0, minute: int = 0) -> None:
    print(f"[backtesting_scheduler] scheduled daily at {hour:02d}:{minute:02d} local time  (backend={BACKEND})")
    fired_today: str | None = None

    while True:
        now = datetime.now()
        today_key = now.strftime("%Y-%m-%d")

        if now.hour == hour and now.minute == minute and fired_today != today_key:
            fired_today = today_key
            fired_at = _utc_iso()
            print(f"[backtesting_scheduler] {fired_at}  triggering nightly backtest run...")

            outcome = _trigger_backtest()
            state = _load_state()
            state.update(
                {
                    "last_run_at": fired_at,
                    "last_run_date": today_key,
                    "last_run_outcome": outcome,
                    "scheduled_hour": hour,
                    "scheduled_minute": minute,
                    "backend": BACKEND,
                }
            )
            _save_state(state)

            if outcome.get("http_status") == 200:
                print(f"[backtesting_scheduler] run OK  summary={outcome.get('response', {}).get('summary', {})}")
            else:
                print(f"[backtesting_scheduler] run FAILED  {outcome}")

        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nightly backtesting scheduler")
    parser.add_argument("--hour", type=int, default=0, help="Hour to run (24h, local time, default 0)")
    parser.add_argument("--minute", type=int, default=0, help="Minute to run (default 0)")
    args = parser.parse_args()

    try:
        run_scheduler(hour=args.hour, minute=args.minute)
    except KeyboardInterrupt:
        print("\n[backtesting_scheduler] stopped.")
