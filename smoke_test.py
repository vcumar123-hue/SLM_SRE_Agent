# -*- coding: utf-8 -*-
"""
E2E smoke test -- verifies all critical endpoints are responding correctly.
Usage:  python smoke_test.py [http://localhost:8001]
Exit 0 = all pass, Exit 1 = one or more failures.
"""
from __future__ import annotations
import json, os, sys, urllib.request, urllib.error
from typing import Any, Dict, List, Tuple

BACKEND = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8001"
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results: List[Tuple[str, bool, str]] = []


_DEFAULT_TIMEOUT = 15
_ANALYZE_TIMEOUT = 60  # analyze can be slow on first post-reload request


def _req(method: str, path: str, body: Dict[str, Any] | None = None, qs: str = "", timeout: int = _DEFAULT_TIMEOUT) -> Tuple[int, Any]:
    url = BACKEND + path + (f"?{qs}" if qs else "")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    # Release 1: the backend authenticates every request. Set AIOPS_SMOKE_API_KEY (or AIOPS_UI_API_KEY /
    # AIOPS_API_KEY) to the token of a configured principal. In open dev mode no token is needed.
    token = (os.getenv("AIOPS_SMOKE_API_KEY") or os.getenv("AIOPS_UI_API_KEY") or os.getenv("AIOPS_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_raw = e.read()
        try:
            return e.code, json.loads(body_raw)
        except Exception:
            return e.code, body_raw.decode("utf-8", errors="replace")[:200]
    except Exception as ex:
        return 0, str(ex)


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label}" + (f"  -- {detail}" if detail else ""))


# ── Health ─────────────────────────────────────────────────────────────────────
print("\n[Health]")
code, body = _req("GET", "/health")
check("GET /health ->200", code == 200, f"status={body.get('status') if isinstance(body, dict) else body}")
check("GET /health postgres_enabled", isinstance(body, dict) and body.get("postgres_enabled") is True)

code, body = _req("GET", "/startup")
check("GET /startup ->200", code == 200, f"ready={body.get('ready') if isinstance(body, dict) else body}")

# ── Analyze ────────────────────────────────────────────────────────────────────
print("\n[Analyze]")
code, body = _req("POST", "/analyze", {
    "ticket": "Server linux-lab-01 CPU at 98% for 15 minutes, OOM killer active.",
    "use_fallback": True,
}, timeout=_ANALYZE_TIMEOUT)
_cls = body.get("analysis", {}).get("classification", "") if isinstance(body, dict) else ""
check("POST /analyze ->200", code == 200, f"classification={str(_cls)[:40]}")
check("POST /analyze has classification", bool(_cls))

# ── Governance gate (must block for client_a) ───────────────────────────────────
print("\n[Governance]")
code, body = _req("GET", "/governance/check", qs="tenant_id=client_a&operation=promote")
check("GET /governance/check client_a promote ->allowed=false", code == 200 and isinstance(body, dict) and body.get("allowed") is False)

code, body = _req("POST", "/promote_candidate", {"candidate_id": "smoke-test-nonexistent"})
check("POST /promote_candidate blocked by governance ->403", code == 403)

code, body = _req("POST", "/create_playbook", {"name": "smoke", "classification": "cpu_saturation", "description": "smoke", "steps": [], "scripts": []})
check("POST /create_playbook blocked by governance ->403", code == 403)

# ── Automation ─────────────────────────────────────────────────────────────────
print("\n[Automation]")
code, body = _req("GET", "/automation/queue/list")
check("GET /automation/queue/list ->200", code == 200)
check("GET /automation/queue/list has items key", isinstance(body, dict) and "items" in body)

code, body = _req("POST", "/automation/autonomous/eligible_mapping", {"runtime_key": "cpu_saturation", "classification": "cpu_saturation"})
check("POST /automation/autonomous/eligible_mapping ->200", code == 200)

# ── Evidence ───────────────────────────────────────────────────────────────────
print("\n[Evidence]")
code, body = _req("GET", "/evidence/latest")
check("GET /evidence/latest ->200", code == 200)
check("GET /evidence/latest has evidence dict", isinstance(body, dict) and "evidence" in body)

# ── Safety gate ────────────────────────────────────────────────────────────────
print("\n[Safety gate]")
code, body = _req("POST", "/automation/pre_execution_check", {
    "runtime_key": "cpu_saturation",
    "execution_mode": "assisted",
    "ticket": "smoke test",
})
check("POST /automation/pre_execution_check ->200", code == 200)
check("pre_execution_check has gate_status", isinstance(body, dict) and "gate_status" in body)

# ── Telemetry ──────────────────────────────────────────────────────────────────
print("\n[Telemetry]")
code, body = _req("GET", "/telemetry/incidents")
check("GET /telemetry/incidents ->200", code == 200)

code, body = _req("POST", "/telemetry/event", {
    "metric": "cpu_percent", "value": 55.0,
    "resource": "smoke-host", "service": "smoke-svc", "environment": "test", "source": "smoke_test",
})
check("POST /telemetry/event ->200", code == 200, body.get("status") if isinstance(body, dict) else str(body))

# ── Knowledge ──────────────────────────────────────────────────────────────────
print("\n[Knowledge]")
code, body = _req("GET", "/knowledge/sources")
check("GET /knowledge/sources ->200", code == 200)

code, body = _req("GET", "/automation/candidates")
check("GET /automation/candidates ->200", code == 200)

# ── Backtesting ────────────────────────────────────────────────────────────────
print("\n[Backtesting]")
code, body = _req("GET", "/backtesting/results")
check("GET /backtesting/results ->200", code == 200)

# ── Summary ────────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
sep = "-" * 55
print(f"\n{sep}")
print(f"  Results: {passed} passed, {failed} failed  ({len(results)} total)")
print(f"{sep}\n")

if failed:
    print("Failed checks:")
    for label, ok, detail in results:
        if not ok:
            print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))
    sys.exit(1)
