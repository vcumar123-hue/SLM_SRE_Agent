"""
seed_findings.py — Seeds realistic vulnerability findings into the AI SRE Platform.
Usage: python demo/seed_findings.py
"""

import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

BACKEND = "http://127.0.0.1:8001"

DETECTED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

FINDINGS = [
    # ── linux-lab-01 ──────────────────────────────────────────────────────────
    {
        "host": "linux-lab-01",
        "cve_id": "CVE-2022-0778",
        "severity": "high",
        "cvss_score": 7.5,
        "affected_package": "openssl",
        "affected_version": "1.1.1f",
        "fixed_version": "1.1.1n",
        "evidence": "OpenSSL BN_mod_sqrt infinite loop — DoS via crafted cert",
        "detection_tool": "trivy",
        "detected_at": DETECTED_AT,
    },
    {
        "host": "linux-lab-01",
        "cve_id": "CUSTOM-NGINX-001",
        "severity": "medium",
        "cvss_score": 5.3,
        "affected_package": "nginx-config",
        "affected_version": "1.18.0",
        "fixed_version": "N/A",
        "evidence": "Missing security headers: X-Frame-Options, X-Content-Type-Options, CSP",
        "detection_tool": "config-audit",
        "detected_at": DETECTED_AT,
    },
    {
        "host": "linux-lab-01",
        "cve_id": "CUSTOM-PORT-001",
        "severity": "medium",
        "cvss_score": 4.8,
        "affected_package": "netcat",
        "affected_version": "1.10",
        "fixed_version": "N/A",
        "evidence": "Debug port 9999 exposed — unauthenticated access",
        "detection_tool": "port-scan",
        "detected_at": DETECTED_AT,
    },
    # ── linux-lab-02 ──────────────────────────────────────────────────────────
    {
        "host": "linux-lab-02",
        "cve_id": "CVE-2018-18074",
        "severity": "high",
        "cvss_score": 7.4,
        "affected_package": "requests",
        "affected_version": "2.20.0",
        "fixed_version": "2.20.1",
        "evidence": "HTTP redirect sends auth headers to 3rd party hosts",
        "detection_tool": "trivy",
        "detected_at": DETECTED_AT,
    },
    {
        "host": "linux-lab-02",
        "cve_id": "CUSTOM-SSH-001",
        "severity": "high",
        "cvss_score": 7.8,
        "affected_package": "openssh-server",
        "affected_version": "8.2p1",
        "fixed_version": "N/A",
        "evidence": "PermitRootLogin=yes and PasswordAuthentication=yes in sshd_config",
        "detection_tool": "oscap",
        "detected_at": DETECTED_AT,
    },
    {
        "host": "linux-lab-02",
        "cve_id": "CUSTOM-PERM-001",
        "severity": "medium",
        "cvss_score": 5.5,
        "affected_package": "filesystem",
        "affected_version": "N/A",
        "fixed_version": "N/A",
        "evidence": "World-writable /tmp/app-data (chmod 777)",
        "detection_tool": "config-audit",
        "detected_at": DETECTED_AT,
    },
    # ── network-lab-01 ────────────────────────────────────────────────────────
    {
        "host": "network-lab-01",
        "cve_id": "CUSTOM-PG-001",
        "severity": "high",
        "cvss_score": 7.2,
        "affected_package": "postgresql-config",
        "affected_version": "14.0",
        "fixed_version": "N/A",
        "evidence": "listen_addresses='*' with ssl=off — unencrypted remote connections",
        "detection_tool": "config-audit",
        "detected_at": DETECTED_AT,
    },
    {
        "host": "network-lab-01",
        "cve_id": "CUSTOM-BACKUP-001",
        "severity": "medium",
        "cvss_score": 5.9,
        "affected_package": "backup-policy",
        "affected_version": "N/A",
        "fixed_version": "N/A",
        "evidence": "Unencrypted DB backup found in /tmp — accessible to all users",
        "detection_tool": "file-scan",
        "detected_at": DETECTED_AT,
    },
    {
        "host": "network-lab-01",
        "cve_id": "CUSTOM-HOSTS-001",
        "severity": "low",
        "cvss_score": 3.1,
        "affected_package": "tcp-wrappers",
        "affected_version": "N/A",
        "fixed_version": "N/A",
        "evidence": "hosts.allow: ALL:ALL — no IP restriction on TCP wrappers",
        "detection_tool": "config-audit",
        "detected_at": DETECTED_AT,
    },
]


def ingest_finding(finding: dict) -> str | None:
    """POST /vulnerabilities/ingest. Returns finding_id on success, None on failure."""
    url = f"{BACKEND}/vulnerabilities/ingest"
    payload = json.dumps(finding).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("finding_id") or body.get("id")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"    [INGEST FAIL] {finding['cve_id']} — HTTP {exc.code}: {detail[:200]}")
    except urllib.error.URLError as exc:
        print(f"    [INGEST FAIL] {finding['cve_id']} — connection error: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        print(f"    [INGEST FAIL] {finding['cve_id']} — unexpected error: {exc}")
    return None


def enrich_finding(finding_id: str, cve_id: str) -> None:
    """POST /vulnerabilities/findings/{finding_id}/enrich."""
    url = f"{BACKEND}/vulnerabilities/findings/{finding_id}/enrich"
    req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
            print(f"    Enrichment complete          ({cve_id} / {finding_id})")
    except urllib.error.HTTPError as exc:
        if exc.code in (502, 503, 504):
            print(f"    Enrichment unavailable (NVD offline)  ({cve_id} / {finding_id})")
        else:
            detail = exc.read().decode("utf-8", errors="replace")
            print(f"    [ENRICH FAIL] {cve_id} — HTTP {exc.code}: {detail[:200]}")
    except urllib.error.URLError:
        print(f"    Enrichment unavailable (NVD offline)  ({cve_id} / {finding_id})")
    except Exception as exc:  # noqa: BLE001
        print(f"    [ENRICH FAIL] {cve_id} — unexpected error: {exc}")


def print_summary(rows: list[tuple[str, str, str, str]]) -> None:
    col_host = max(len(r[0]) for r in rows) + 2
    col_cve  = max(len(r[1]) for r in rows) + 2
    col_sev  = max(len(r[2]) for r in rows) + 2
    col_fid  = max(len(r[3]) for r in rows) + 2

    header = (
        f"{'host':<{col_host}}{'CVE':<{col_cve}}"
        f"{'severity':<{col_sev}}{'finding_id':<{col_fid}}"
    )
    divider = "-" * len(header)
    print(f"\n{header}")
    print(divider)
    for host, cve, sev, fid in rows:
        print(f"{host:<{col_host}}{cve:<{col_cve}}{sev:<{col_sev}}{fid:<{col_fid}}")
    print(divider)


def main() -> None:
    print(f"Seeding vulnerability findings at {BACKEND} …\n")
    print(f"detected_at = {DETECTED_AT}\n")

    summary_rows: list[tuple[str, str, str, str]] = []
    high_findings: list[tuple[str, str]] = []  # (finding_id, cve_id)

    for finding in FINDINGS:
        finding_id = ingest_finding(finding)
        fid_label = finding_id or "FAILED"
        summary_rows.append((finding["host"], finding["cve_id"], finding["severity"], fid_label))
        if finding_id and finding["severity"] == "high":
            high_findings.append((finding_id, finding["cve_id"]))

    print_summary(summary_rows)

    if high_findings:
        print(f"\nEnriching {len(high_findings)} HIGH severity finding(s) …")
        for finding_id, cve_id in high_findings:
            enrich_finding(finding_id, cve_id)
    else:
        print("\nNo HIGH severity findings to enrich.")

    print("\nDone.")


if __name__ == "__main__":
    main()
