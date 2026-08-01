"""
build_temporal_index.py — build a temporal before/after correlation index.

For each incident classification, finds what incident types typically
preceded it (precursor signals) and followed it (cascading effects)
within a configurable time window across peer client data.

Confidence scoring:
  HIGH   — same infrastructure domain (e.g. server↔server, db↔db)
  MEDIUM — domains that are known to interact (e.g. network↔db, server↔db)
  LOW    — unrelated infrastructure stacks (e.g. endpoint/VDI↔server)

Output: data/backtest/temporal_index.json

Run:
    python3 tools/build_temporal_index.py
"""
from __future__ import annotations
import json, re, pathlib, sys, time
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
IN_FILE  = ROOT / "data" / "backtest" / "incidents_normalised.json"
OUT_FILE = ROOT / "data" / "backtest" / "temporal_index.json"

BEFORE_WINDOW_HOURS = 48
AFTER_WINDOW_HOURS  = 48
MIN_CO_OCCUR        = 3

# ---------------------------------------------------------------------------
# Infrastructure domain taxonomy
# ---------------------------------------------------------------------------
# Each classification belongs to one or more domains.
# Correlations within the same domain or between interacting domains are MEDIUM+.
# Correlations across unrelated domains are LOW confidence.

_DOMAIN: Dict[str, str] = {
    # Server/compute layer
    "cpu_saturation":               "server",
    "memory_pressure":              "server",
    "high_disk_usage":              "server",
    "disk_full":                    "server",
    "storage_connectivity_issue":   "server",
    "patch_failure":                "server",
    "backup_failure":               "server",
    "application_error":            "server",
    "batch_job_failure":            "server",
    "kubernetes_issue":             "server",
    "service_unavailable":          "server",
    # Database layer (runs on server but distinct)
    "database_performance_issue":   "database",
    # Network layer
    "network_connectivity_issue":   "network",
    "dns_resolution_failure":       "network",
    "load_balancer_issue":          "network",
    "api_gateway_timeout":          "network",
    "file_transfer_failure":        "network",
    # Identity/auth layer
    "authentication_failure":       "identity",
    "active_directory_issue":       "identity",
    "vpn_client_failure":           "identity",
    "certificate_expiry_warning":   "identity",
    # Cloud layer
    "cloud_resource_issue":         "cloud",
    # Endpoint/user-facing layer (physically separate from server infra)
    "vdi_citrix_connectivity_issue":"endpoint",
    "email_delivery_failure":       "endpoint",
    "teams_meeting_issue":          "endpoint",
    "printer_issue":                "endpoint",
    # Security (cross-cutting)
    "security_alert":               "security",
    "unclassified":                 "unknown",
}

# Pairs of domains that are known to interact causally.
# (a, b) means: an issue in domain a can cause/precede an issue in domain b.
_INTERACTING_DOMAINS: Set[frozenset] = {
    frozenset({"server", "database"}),      # DB runs on servers
    frozenset({"server", "server"}),        # same domain
    frozenset({"database", "database"}),
    frozenset({"network", "database"}),     # network needed to reach DB
    frozenset({"network", "server"}),       # network to reach server
    frozenset({"network", "network"}),
    frozenset({"identity", "server"}),      # auth to server
    frozenset({"identity", "database"}),    # auth to DB
    frozenset({"identity", "network"}),     # VPN/DNS affects connectivity
    frozenset({"identity", "identity"}),
    frozenset({"network", "api_gateway_timeout"}),  # absorbed into network
    frozenset({"server", "cloud"}),         # cloud VMs are servers
    frozenset({"cloud", "database"}),
    frozenset({"cloud", "network"}),
    frozenset({"security", "server"}),      # malware on server
    frozenset({"security", "database"}),
    frozenset({"security", "network"}),
    frozenset({"security", "identity"}),
    frozenset({"certificate_expiry_warning", "identity"}),  # handled via domain
}

def _confidence(cls_a: str, cls_b: str) -> str:
    """Return HIGH / MEDIUM / LOW based on domain compatibility."""
    da = _DOMAIN.get(cls_a, "unknown")
    db = _DOMAIN.get(cls_b, "unknown")
    if da == "unknown" or db == "unknown":
        return "low"
    if da == db:
        return "high"
    pair = frozenset({da, db})
    if pair in _INTERACTING_DOMAINS:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Keyword classifier (same 28-rule set used in backtest_run.py)
# ---------------------------------------------------------------------------
_RULES: List[Tuple[str, List[str]]] = [
    ("vpn_client_failure",          ["vpn", "cisco anyconnect", "remote access", "tunnel", "split tunnel"]),
    ("certificate_expiry_warning",  ["certificate", "cert expir", "ssl expir", "tls expir", "x509"]),
    ("high_disk_usage",             ["disk", "storage full", "volume full", "filesystem full", "disk usage", "disk space"]),
    ("cpu_saturation",              ["cpu", "processor", "high utilization", "cpu spike", "cpu usage", "100%"]),
    ("memory_pressure",             ["memory", "ram", "heap", "out of memory", "oom", "swap"]),
    ("database_performance_issue",  ["database", "db slow", "sql slow", "oracle", "db2", "postgres", "mysql", "query slow", "deadlock", "replication lag", "connection pool"]),
    ("network_connectivity_issue",  ["network", "connectivity", "ping", "latency", "packet loss", "unreachable", "connection refused", "timeout", "firewall", "route"]),
    ("authentication_failure",      ["authentication", "auth fail", "login fail", "password", "credential", "ldap", "kerberos", "radius", "mfa", "sso"]),
    ("dns_resolution_failure",      ["dns", "name resolution", "nslookup", "dns fail", "domain lookup"]),
    ("load_balancer_issue",         ["load balancer", "f5", "nginx", "haproxy", "upstream", "backend pool", "health check fail"]),
    ("application_error",           ["application", "app error", "500 error", "exception", "crash", "core dump", "segfault"]),
    ("batch_job_failure",           ["batch", "job fail", "scheduled task", "cron", "etl fail", "pipeline fail", "abend", "mf job"]),
    ("file_transfer_failure",       ["ftp", "sftp", "file transfer", "scp", "transfer fail", "upload fail", "download fail"]),
    ("email_delivery_failure",      ["email", "mail", "smtp", "exchange", "outlook", "mailbox", "mail flow", "delivery fail"]),
    ("storage_connectivity_issue",  ["san", "nas", "nfs", "iscsi", "lun", "storage", "mount fail", "disk detach"]),
    ("vdi_citrix_connectivity_issue",["vdi", "citrix", "virtual desktop", "xenapp", "storefront", "ica"]),
    ("service_unavailable",         ["service down", "service unavailable", "service unreachable", "service stopped", "service fail"]),
    ("api_gateway_timeout",         ["api gateway", "api timeout", "rest api", "endpoint timeout", "http timeout", "503", "504"]),
    ("backup_failure",              ["backup fail", "backup job", "veeam", "commvault", "snapshot fail", "restore fail"]),
    ("patch_failure",               ["patch fail", "windows update", "update fail", "hotfix", "package install fail"]),
    ("kubernetes_issue",            ["kubernetes", "k8s", "pod", "container", "helm", "namespace", "kubectl", "aks", "eks"]),
    ("cloud_resource_issue",        ["aws", "azure", "gcp", "cloud", "ec2", "s3", "blob", "rds", "cloudfront"]),
    ("security_alert",              ["malware", "ransomware", "intrusion", "breach", "vulnerability", "exploit", "suspicious"]),
    ("printer_issue",               ["printer", "print queue", "print fail", "driver conflict", "spooler"]),
    ("teams_meeting_issue",         ["teams", "zoom", "webex", "meeting", "audio fail", "video fail", "conference"]),
    ("active_directory_issue",      ["active directory", "ad ", "group policy", "gpo", "domain controller", "dc ", "ldap"]),
    ("disk_full",                   ["disk full", "no space", "volume full", "inode", "filesystem full", "disk 100"]),
    ("high_disk_usage",             ["disk usage", "disk utilization", "storage utilization"]),
]

def _classify(text: str) -> str:
    t = str(text or "").lower()
    for cls, keywords in _RULES:
        if any(kw in t for kw in keywords):
            return cls
    return "unclassified"


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_temporal_index() -> None:
    if not IN_FILE.exists():
        sys.exit(f"ERROR: {IN_FILE} not found. Run backtest_ingest.py first.")

    print(f"Loading {IN_FILE} …")
    data = json.loads(IN_FILE.read_text(encoding="utf-8"))
    incidents = data["incidents"]
    print(f"Total incidents: {len(incidents):,}")

    # Step 1: classify and parse dates
    print("Classifying and parsing dates …")
    t0 = time.time()
    classified: List[Dict[str, Any]] = []
    skipped_no_date = 0

    for inc in incidents:
        dt = _parse_dt(inc.get("created_at"))
        if not dt:
            skipped_no_date += 1
            continue
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        cls = _classify(inc.get("short_description", "") or inc.get("text", ""))
        ag = str(inc.get("assignment_group") or "").strip().lower()
        classified.append({
            "cls": cls,
            "dt": dt,
            "client_id": inc.get("client_id", "unknown"),
            "assignment_group": ag,
            "category": str(inc.get("category") or "").strip(),
        })

    print(f"  Classified: {len(classified):,}  (skipped {skipped_no_date:,} without dates)")
    print(f"  Done in {time.time() - t0:.1f}s")

    # Step 2: sort per client
    print("Sorting by client and time …")
    by_client: Dict[str, List[Dict]] = defaultdict(list)
    for inc in classified:
        by_client[inc["client_id"]].append(inc)
    for cid in by_client:
        by_client[cid].sort(key=lambda x: x["dt"])

    client_counts = {cid: len(incs) for cid, incs in by_client.items()}
    print("  Per client:", client_counts)

    # Step 3: temporal co-occurrence scan
    # Track co-occurrence counts and also "same_ag_count" (shared assignment group).
    # confidence = high/medium based on domain; further boosted if same assignment group.
    print("Building temporal correlations …")
    t0 = time.time()

    # index: cls → {before: Counter(cls), after: Counter(cls), total: int,
    #                before_gaps: {cls: [h]}, after_gaps: {cls: [h]},
    #                before_same_ag: Counter(cls), after_same_ag: Counter(cls)}
    index: Dict[str, Any] = defaultdict(lambda: {
        "total": 0,
        "before": Counter(),
        "after": Counter(),
        "before_gaps": defaultdict(list),
        "after_gaps": defaultdict(list),
        "before_same_ag": Counter(),
        "after_same_ag":  Counter(),
        "before_categories": Counter(),
        "after_categories": Counter(),
    })

    for cid, incs in by_client.items():
        n = len(incs)
        for i, inc in enumerate(incs):
            dt_i = inc["dt"]
            cls_i = inc["cls"]
            ag_i  = inc["assignment_group"]
            entry = index[cls_i]
            entry["total"] += 1

            # Scan backwards
            j = i - 1
            seen_before: set = set()
            while j >= 0:
                dt_j = incs[j]["dt"]
                gap = (dt_i - dt_j).total_seconds() / 3600
                if gap > BEFORE_WINDOW_HOURS:
                    break
                cls_j = incs[j]["cls"]
                ag_j  = incs[j]["assignment_group"]
                if cls_j != cls_i and cls_j not in seen_before:
                    seen_before.add(cls_j)
                    entry["before"][cls_j] += 1
                    entry["before_gaps"][cls_j].append(round(gap, 1))
                    if ag_i and ag_j and ag_i == ag_j:
                        entry["before_same_ag"][cls_j] += 1
                cat_j = incs[j]["category"]
                if cat_j and f"cat:{cat_j}" not in seen_before:
                    seen_before.add(f"cat:{cat_j}")
                    entry["before_categories"][cat_j] += 1
                j -= 1

            # Scan forwards
            j = i + 1
            seen_after: set = set()
            while j < n:
                dt_j = incs[j]["dt"]
                gap = (dt_j - dt_i).total_seconds() / 3600
                if gap > AFTER_WINDOW_HOURS:
                    break
                cls_j = incs[j]["cls"]
                ag_j  = incs[j]["assignment_group"]
                if cls_j != cls_i and cls_j not in seen_after:
                    seen_after.add(cls_j)
                    entry["after"][cls_j] += 1
                    entry["after_gaps"][cls_j].append(round(gap, 1))
                    if ag_i and ag_j and ag_i == ag_j:
                        entry["after_same_ag"][cls_j] += 1
                cat_j = incs[j]["category"]
                if cat_j and f"cat:{cat_j}" not in seen_after:
                    seen_after.add(f"cat:{cat_j}")
                    entry["after_categories"][cat_j] += 1
                j += 1

    print(f"  Correlation pass done in {time.time() - t0:.1f}s")

    # Step 4: serialise
    print("Serialising …")
    out: Dict[str, Any] = {}

    for cls, entry in index.items():
        def _top_patterns(counter, gaps_dict, same_ag_counter, n=8):
            top = [(c, cnt) for c, cnt in counter.most_common(n) if cnt >= MIN_CO_OCCUR]
            result = []
            for c, cnt in top:
                gaps = gaps_dict.get(c, [])
                avg_gap = round(sum(gaps) / len(gaps), 1) if gaps else None
                same_ag = same_ag_counter.get(c, 0)
                same_ag_pct = round(100 * same_ag / cnt) if cnt else 0

                # Confidence: start from domain compatibility, boost if high same-AG rate
                conf = _confidence(cls, c)
                # If >30% share an assignment group, it's likely same-infrastructure signal
                if same_ag_pct >= 30 and conf != "high":
                    conf = "medium"

                result.append({
                    "classification": c,
                    "co_occurrence_count": cnt,
                    "same_ag_count": same_ag,
                    "same_ag_pct": same_ag_pct,
                    "avg_gap_hours": avg_gap,
                    "avg_gap_label": f"{avg_gap:.1f}h" if avg_gap is not None else "—",
                    "confidence": conf,
                    "domain": _DOMAIN.get(c, "unknown"),
                })
            return result

        before_pats = _top_patterns(entry["before"], entry["before_gaps"], entry["before_same_ag"])
        after_pats  = _top_patterns(entry["after"],  entry["after_gaps"],  entry["after_same_ag"])
        top_before_cats = [(c, n) for c, n in entry["before_categories"].most_common(4) if n >= MIN_CO_OCCUR]
        top_after_cats  = [(c, n) for c, n in entry["after_categories"].most_common(4)  if n >= MIN_CO_OCCUR]

        if not before_pats and not after_pats:
            continue

        out[cls] = {
            "total": entry["total"],
            "domain": _DOMAIN.get(cls, "unknown"),
            "before_patterns": before_pats,
            "after_patterns":  after_pats,
            "before_categories": top_before_cats,
            "after_categories":  top_after_cats,
        }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "index": out}, indent=2),
        encoding="utf-8",
    )
    size_kb = OUT_FILE.stat().st_size // 1024
    print(f"Written: {OUT_FILE}  ({size_kb}KB)  — {len(out)} classifications with correlations")

    # Preview with confidence
    print("\nCorrelation confidence breakdown for database_performance_issue:")
    entry = out.get("database_performance_issue")
    if entry:
        for direction, pats in [("BEFORE", entry["before_patterns"]), ("AFTER", entry["after_patterns"])]:
            for p in pats[:5]:
                conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(p["confidence"], "⚪")
                print(f"  {direction} {conf_icon} {p['classification']} ({p['confidence']}) "
                      f"gap={p['avg_gap_label']}  same_ag={p['same_ag_pct']}%")

    print("\nTop correlations for key classifications:")
    for cls in ["database_performance_issue", "network_connectivity_issue", "cpu_saturation"]:
        entry = out.get(cls)
        if not entry:
            continue
        # Show only high/medium confidence
        hi_before = [p for p in entry["before_patterns"] if p["confidence"] in ("high", "medium")][:3]
        hi_after  = [p for p in entry["after_patterns"]  if p["confidence"] in ("high", "medium")][:3]
        before_str = " → ".join(f"{p['classification']}(-{p['avg_gap_label']})" for p in hi_before) or "none"
        after_str  = " → ".join(f"{p['classification']}(+{p['avg_gap_label']})" for p in hi_after)  or "none"
        print(f"  {cls} ({entry['total']} incidents):")
        print(f"    HIGH/MED BEFORE: {before_str}")
        print(f"    HIGH/MED AFTER:  {after_str}")


if __name__ == "__main__":
    build_temporal_index()
