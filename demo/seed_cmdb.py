"""
seed_cmdb.py — Seeds CMDB data for the 3 demo lab machines.
Usage: python demo/seed_cmdb.py
"""

import json
import urllib.request
import urllib.error

BACKEND = "http://127.0.0.1:8001"

CIS = [
    {
        "ci_name": "linux-lab-01",
        "environment": "production",
        "operational_status": "operational",
        "redundancy_type": "active-active",
        "business_criticality": 3,
        "owner_team": "Platform Engineering",
        "tier": "prod-redundant",
        "description": "Web tier — nginx load balancer, redundant nodes",
    },
    {
        "ci_name": "linux-lab-02",
        "environment": "production",
        "operational_status": "operational",
        "redundancy_type": "standalone",
        "business_criticality": 2,
        "owner_team": "Application Engineering",
        "tier": "prod-standalone",
        "description": "App tier — Python FastAPI service, no failover",
    },
    {
        "ci_name": "network-lab-01",
        "environment": "production",
        "operational_status": "operational",
        "redundancy_type": "active-passive",
        "business_criticality": 3,
        "owner_team": "Data Engineering",
        "tier": "prod-redundant",
        "description": "DB/Config tier — PostgreSQL, active-passive replication",
    },
]


def register_ci(ci: dict) -> None:
    url = f"{BACKEND}/cmdb/ci"
    payload = json.dumps(ci).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            ci_id = body.get("ci_id") or body.get("id") or "?"
            print(f"  [OK]   {ci['ci_name']} registered  (ci_id={ci_id})")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"  [FAIL] {ci['ci_name']} — HTTP {exc.code}: {detail[:200]}")
    except urllib.error.URLError as exc:
        print(f"  [FAIL] {ci['ci_name']} — connection error: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {ci['ci_name']} — unexpected error: {exc}")


def main() -> None:
    print(f"Seeding CMDB at {BACKEND} …\n")
    for ci in CIS:
        register_ci(ci)
    print("\nDone.")


if __name__ == "__main__":
    main()
