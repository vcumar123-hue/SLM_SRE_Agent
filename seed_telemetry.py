"""
Seed the telemetry console with metric events matching the built-in rules, then evaluate.
Rules require: cpu_percent >= 95 (3x), memory_percent >= 92 (3x), disk_percent >= 90 (2x),
               api_latency_ms >= 2000 (3x), pod_restarts >= 5 (2x), cert_days_to_expiry <= 14 (1x)
"""
from __future__ import annotations
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = "http://localhost:8000"

try:
    import urllib.request, urllib.error
except ImportError:
    print("urllib not available"); sys.exit(1)


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BACKEND + path, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"error": e.code, "detail": body[:300]}
    except Exception as ex:
        return {"error": str(ex)}


# Events matched to rule metric names and thresholds.
# Each rule needs required_count hits within window_minutes.
EVENTS = [
    # cpu_percent >= 95, need 3
    {"metric": "cpu_percent", "value": 96.2, "resource": "linux-lab-01", "service": "claims-api", "environment": "production", "source": "prometheus", "labels": {"alertname": "CpuSaturation", "severity": "critical"}},
    {"metric": "cpu_percent", "value": 97.8, "resource": "linux-lab-01", "service": "claims-api", "environment": "production", "source": "prometheus", "labels": {"alertname": "CpuSaturation", "severity": "critical"}},
    {"metric": "cpu_percent", "value": 95.1, "resource": "linux-lab-01", "service": "claims-api", "environment": "production", "source": "prometheus", "labels": {"alertname": "CpuSaturation", "severity": "critical"}},
    # memory_percent >= 92, need 3
    {"metric": "memory_percent", "value": 94.3, "resource": "linux-lab-02", "service": "claims-api", "environment": "production", "source": "prometheus", "labels": {"alertname": "MemoryPressure", "severity": "high"}},
    {"metric": "memory_percent", "value": 93.7, "resource": "linux-lab-02", "service": "claims-api", "environment": "production", "source": "prometheus", "labels": {"alertname": "MemoryPressure", "severity": "high"}},
    {"metric": "memory_percent", "value": 95.0, "resource": "linux-lab-02", "service": "claims-api", "environment": "production", "source": "prometheus", "labels": {"alertname": "MemoryPressure", "severity": "high"}},
    # disk_percent >= 90, need 2
    {"metric": "disk_percent", "value": 98.0, "resource": "linux-lab-01", "service": "postgres-aiops", "environment": "production", "source": "prometheus", "labels": {"alertname": "DiskIOPressure", "severity": "medium", "device": "sda1"}},
    {"metric": "disk_percent", "value": 97.5, "resource": "linux-lab-01", "service": "postgres-aiops", "environment": "production", "source": "prometheus", "labels": {"alertname": "DiskIOPressure", "severity": "medium", "device": "sda1"}},
    # api_latency_ms >= 2000, need 3
    {"metric": "api_latency_ms", "value": 2840, "resource": "network-lab-01", "service": "claims-api", "environment": "production", "source": "alertmanager", "labels": {"alertname": "ApiLatencySpike", "severity": "high"}},
    {"metric": "api_latency_ms", "value": 3100, "resource": "network-lab-01", "service": "claims-api", "environment": "production", "source": "alertmanager", "labels": {"alertname": "ApiLatencySpike", "severity": "high"}},
    {"metric": "api_latency_ms", "value": 2200, "resource": "network-lab-01", "service": "claims-api", "environment": "production", "source": "alertmanager", "labels": {"alertname": "ApiLatencySpike", "severity": "high"}},
    # pod_restarts >= 5, need 2
    {"metric": "pod_restarts", "value": 6, "resource": "linux-lab-02", "service": "claims-api", "environment": "production", "source": "kubernetes", "labels": {"alertname": "OOMKill", "severity": "critical", "pod": "claims-api-7d4b9c8-xk2p1"}},
    {"metric": "pod_restarts", "value": 7, "resource": "linux-lab-02", "service": "claims-api", "environment": "production", "source": "kubernetes", "labels": {"alertname": "OOMKill", "severity": "critical", "pod": "claims-api-7d4b9c8-xk2p1"}},
    # cert_days_to_expiry <= 14, need 1
    {"metric": "cert_days_to_expiry", "value": 8, "resource": "vpn-lab-01", "service": "vpn-gateway", "environment": "production", "source": "alertmanager", "labels": {"alertname": "CertExpiry", "severity": "medium", "cn": "vpn-lab-01.pfg.internal"}},
]

print(f"Posting {len(EVENTS)} metric events ...")
ok_count = 0
for event in EVENTS:
    result = post("/telemetry/event", event)
    if result.get("status") == "ok":
        ok_count += 1
    else:
        print(f"  FAIL {event['metric']}: {result}")
print(f"  {ok_count}/{len(EVENTS)} OK")

print("\nTriggering evaluate ...")
result = post("/telemetry/evaluate", {"dry_run": False})
print(f"  events: {result.get('event_count')} | incidents created: {result.get('created_count')}")
if result.get("error"):
    print(f"  ERROR: {result}")

# Final count via API
try:
    with urllib.request.urlopen(BACKEND + "/telemetry/incidents", timeout=5) as r:
        data = json.loads(r.read())
    items = data.get("items", []) if isinstance(data, dict) else data
    print(f"\nTelemetry incidents total: {len(items)}")
    for inc in items:
        sev = inc.get("severity", "?")
        title = inc.get("title", "no title")
        res = inc.get("resource", "?")
        print(f"  [{sev}] {title[:55]}  ({res})")
except Exception as ex:
    print(f"Could not fetch: {ex}")

print("\nDone — reload Telemetry Console at http://localhost:8501")
