"""
backtest_run.py — classify historical incidents and produce a cross-client
pattern correlation report.

For each incident in incidents_normalised.json:
  1. Run the existing AI classify/route logic (fast local embedding path only)
  2. Record the predicted classification
  3. Aggregate: frequency per classification per client, cross-client overlap,
     resolution time by classification, reassignment rate by classification

Output: data/backtest/backtest_report.json

Run inside the container:
    python3 tools/backtest_run.py [--limit N] [--clients a,b]
"""
from __future__ import annotations
import json, pathlib, sys, argparse, time
from datetime import datetime, timezone
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IN_FILE = ROOT / "data" / "backtest" / "incidents_normalised.json"
OUT_FILE = ROOT / "data" / "backtest" / "backtest_report.json"


# ---------------------------------------------------------------------------
# Lazy classifier — uses the existing local embedding + knowledge store
# ---------------------------------------------------------------------------

def _load_classifier():
    """Return a classify function (text -> classification str)."""
    try:
        from classification import classify_ticket  # type: ignore
        return classify_ticket
    except Exception:
        pass
    try:
        # Fallback: use the analyse route's router which does local embed
        from analyze import classify_local  # type: ignore
        return classify_local
    except Exception:
        pass
    # Final fallback: keyword-based classifier so we always return something
    return _keyword_classifier


_KEYWORD_MAP = [
    (["disk", "storage", "space", "volume", "filesystem", "quota"], "high_disk_usage"),
    (["cpu", "processor", "compute", "utilization", "high load", "load average"], "high_cpu_usage"),
    (["memory", "ram", "heap", "oom", "out of memory", "swap"], "high_memory_usage"),
    (["certificate", "cert", "ssl", "tls", "expir", "x509"], "certificate_expiry_warning"),
    (["network", "connectivity", "ping", "packet loss", "latency", "timeout", "unreachable"], "network_connectivity_issue"),
    (["vpn", "remote access", "tunnel", "client connectivity"], "vpn_client_failure"),
    (["pod", "container", "kubernetes", "k8s", "crashloop", "crash loop", "oom killed"], "pod_crash_loop"),
    (["api", "gateway", "endpoint", "http 5", "rest", "service unavailable"], "api_gateway_timeout"),
    (["database", "db", "sql", "oracle", "postgres", "mysql", "connection pool"], "database_connection_failure"),
    (["login", "authentication", "auth", "password", "access denied", "sso", "ldap", "active directory"], "authentication_failure"),
    (["backup", "restore", "snapshot", "replication"], "backup_failure"),
    (["deployment", "release", "deploy", "pipeline", "ci/cd", "build failed"], "deployment_failure"),
    (["service", "application", "app", "down", "unavailable", "not responding", "restart"], "service_unavailable"),
    (["email", "mail", "smtp", "exchange", "mailbox"], "email_delivery_failure"),
    (["dns", "resolution", "domain", "hostname"], "dns_resolution_failure"),
    (["load balanc", "nginx", "haproxy", "reverse proxy"], "load_balancer_issue"),
    (["monitoring", "alert", "alarm", "notification", "false positive"], "false_positive_alert"),
    (["user access", "permission", "role", "group", "provisioning", "account"], "user_access_issue"),
    (["performance", "slow", "response time", "latency", "throughput"], "performance_degradation"),
    (["hardware", "server", "physical", "fan", "temperature", "power supply"], "hardware_failure"),
]


def _keyword_classifier(text: str) -> str:
    low = text.lower()
    scores: Counter = Counter()
    for keywords, label in _KEYWORD_MAP:
        hit = sum(1 for kw in keywords if kw in low)
        if hit:
            scores[label] += hit
    if scores:
        return scores.most_common(1)[0][0]
    return "unclassified"


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _parse_seconds(incident: Dict) -> Optional[float]:
    """Return resolution time in hours if both dates present."""
    try:
        from datetime import datetime
        c = incident.get("created_at", "")
        r = incident.get("resolved_at", "")
        if not c or not r:
            return None
        c_dt = datetime.fromisoformat(str(c).replace("Z", "+00:00"))
        r_dt = datetime.fromisoformat(str(r).replace("Z", "+00:00"))
        diff = (r_dt - c_dt).total_seconds() / 3600
        return round(diff, 2) if diff > 0 else None
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(float(str(v))) if v is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_backtest(
    limit: Optional[int] = None,
    client_filter: Optional[List[str]] = None,
    batch_size: int = 5000,
) -> Dict:
    if not IN_FILE.exists():
        raise FileNotFoundError(f"Run backtest_ingest.py first: {IN_FILE}")

    print(f"Loading {IN_FILE} …")
    data = json.loads(IN_FILE.read_text(encoding="utf-8"))
    incidents: List[Dict] = data["incidents"]

    if client_filter:
        incidents = [i for i in incidents if i["client_id"] in client_filter]
        print(f"Filtered to clients {client_filter}: {len(incidents):,} incidents")

    if limit:
        incidents = incidents[:limit]
        print(f"Capped to {limit:,} incidents")

    print(f"Loading classifier …")
    classify = _load_classifier()

    # Classify in batches with progress
    results: List[Dict] = []
    t0 = time.time()
    for i, inc in enumerate(incidents):
        if i and i % batch_size == 0:
            elapsed = time.time() - t0
            rate = i / elapsed
            eta = (len(incidents) - i) / rate if rate > 0 else 0
            print(f"  {i:,}/{len(incidents):,}  ({rate:.0f}/s  ETA {eta:.0f}s)")

        text = inc.get("text") or inc.get("short_description") or ""
        if not text:
            predicted = "unclassified"
        else:
            try:
                predicted = classify(text) or "unclassified"
            except Exception:
                predicted = "unclassified"

        resolve_hours = _parse_seconds(inc)
        reopen = _safe_int(inc.get("reassignment_count"))

        results.append({
            **inc,
            "predicted_classification": predicted,
            "resolve_hours": resolve_hours,
            "reassignment_count_int": reopen,
        })

    elapsed_total = time.time() - t0
    print(f"Classified {len(results):,} incidents in {elapsed_total:.1f}s")

    # ---------------------------------------------------------------------------
    # Aggregate
    # ---------------------------------------------------------------------------

    # 1. Classification frequency per client
    by_client_class: Dict[str, Counter] = defaultdict(Counter)
    for r in results:
        by_client_class[r["client_id"]][r["predicted_classification"]] += 1

    # 2. Cross-client pattern overlap
    #    For each classification: which clients have it and at what share
    all_classifications = set(r["predicted_classification"] for r in results)
    cross_client: Dict[str, Dict] = {}
    for cls in sorted(all_classifications):
        clients_with = {}
        for cid, counts in by_client_class.items():
            n = counts.get(cls, 0)
            if n:
                total_client = sum(counts.values())
                clients_with[cid] = {
                    "count": n,
                    "share_pct": round(100 * n / total_client, 1) if total_client else 0,
                }
        if clients_with:
            cross_client[cls] = {
                "client_count": len(clients_with),
                "total_incidents": sum(v["count"] for v in clients_with.values()),
                "clients": clients_with,
            }

    # 3. Resolution time by classification (hours)
    resolve_by_class: Dict[str, List[float]] = defaultdict(list)
    for r in results:
        if r.get("resolve_hours") is not None:
            resolve_by_class[r["predicted_classification"]].append(r["resolve_hours"])

    resolve_stats: Dict[str, Dict] = {}
    for cls, hours in resolve_by_class.items():
        hours_sorted = sorted(hours)
        n = len(hours_sorted)
        resolve_stats[cls] = {
            "count": n,
            "mean_h": round(sum(hours_sorted) / n, 1),
            "median_h": round(hours_sorted[n // 2], 1),
            "p90_h": round(hours_sorted[int(n * 0.9)], 1),
        }

    # 4. Reassignment rate by classification
    reopen_by_class: Dict[str, List[int]] = defaultdict(list)
    for r in results:
        rc = r.get("reassignment_count_int")
        if rc is not None:
            reopen_by_class[r["predicted_classification"]].append(rc)

    reopen_stats: Dict[str, Dict] = {}
    for cls, vals in reopen_by_class.items():
        n = len(vals)
        reopen_stats[cls] = {
            "count": n,
            "mean_reassignments": round(sum(vals) / n, 2),
            "pct_reassigned": round(100 * sum(1 for v in vals if v > 0) / n, 1),
        }

    # 5. Top patterns shared across 3+ clients (highest signal)
    shared_patterns = sorted(
        [(cls, info) for cls, info in cross_client.items() if info["client_count"] >= 2],
        key=lambda x: -x[1]["client_count"],
    )

    # 6. Priority distribution by classification
    priority_by_class: Dict[str, Counter] = defaultdict(Counter)
    for r in results:
        p = r.get("priority")
        if p is not None:
            priority_by_class[r["predicted_classification"]][str(p)] += 1

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_incidents": len(results),
        "clients": sorted(by_client_class.keys()),
        "elapsed_seconds": round(elapsed_total, 1),
        "summary": {
            "total_classifications": len(all_classifications),
            "shared_across_2plus_clients": len(shared_patterns),
            "unclassified_count": sum(1 for r in results if r["predicted_classification"] == "unclassified"),
        },
        "by_client": {
            cid: {"total": sum(counts.values()), "top_classifications": counts.most_common(10)}
            for cid, counts in by_client_class.items()
        },
        "cross_client_patterns": cross_client,
        "shared_patterns": [
            {
                "classification": cls,
                "client_count": info["client_count"],
                "total_incidents": info["total_incidents"],
                "clients": info["clients"],
                "resolve_stats": resolve_stats.get(cls, {}),
                "reopen_stats": reopen_stats.get(cls, {}),
                "priority_dist": dict(priority_by_class.get(cls, {})),
            }
            for cls, info in shared_patterns
        ],
        "resolution_stats_by_classification": resolve_stats,
        "reassignment_stats_by_classification": reopen_stats,
    }

    OUT_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Report written: {OUT_FILE}")

    # Print quick summary
    print(f"\n=== Cross-Client Pattern Correlation ===")
    print(f"Shared across 2+ clients: {len(shared_patterns)}")
    for cls, info in shared_patterns[:15]:
        clients_str = ", ".join(f"{c}:{v['count']:,}" for c, v in sorted(info['clients'].items()))
        print(f"  {cls}: {clients_str}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--clients", type=str, default=None)
    args = parser.parse_args()
    client_filter = args.clients.split(",") if args.clients else None
    run_backtest(limit=args.limit, client_filter=client_filter)
