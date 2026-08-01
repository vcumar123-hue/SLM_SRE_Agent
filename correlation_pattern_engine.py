#!/usr/bin/env python3
"""
correlation_pattern_engine.py

Runtime Enterprise Correlation Pattern Engine for AI Ops Copilot.

This standalone module consumes:
    data/global_patterns/enterprise_correlation_patterns.json

It returns top associated incident/change/dependency scenarios for a given
classification and optional ticket text.

No app.py, ui.py, Postgres, Qdrant, or external API dependency in v1.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_PATTERN_FILE = Path("data/global_patterns/enterprise_correlation_patterns.json")

FREQUENCY_WEIGHT = {
    "high": 0.16,
    "medium": 0.10,
    "low": 0.04,
}

CLASSIFICATION_SYNONYMS = {
    "vpn_client_failure": ["vpn_access_failure", "vpn_failure", "remote_access_failure"],
    "cpu_saturation": ["cpu_utilization_high", "high_cpu", "server_performance_issue"],
    "api_gateway_timeout": ["api_timeout", "http_504", "gateway_timeout", "api_latency"],
    "dns_resolution_failure": ["dns_issue", "dns_resolution_delay", "dns_timeout"],
    "certificate_expiry_warning": ["certificate_expiry", "tls_handshake_failure", "cert_expired"],
    "database_latency": ["database_performance_issue", "db_latency", "sql_timeout"],
    "pod_crash_loop": ["crashloopbackoff", "container_restart", "pod_restart_storm"],
    "network_port_block": ["firewall_path_failure", "port_block", "connection_refused"],
    "memory_pressure": ["out_of_memory", "oom", "memory_leak"],
    "disk_full": ["filesystem_full", "storage_full", "no_space_left"],
}

SCENARIO_SIGNAL_TERMS = {
    "firewall_policy_change_blocking_downstream_port": [
        "firewall", "policy", "acl", "security group", "deny", "blocked", "port",
        "egress", "ingress", "connection timeout", "port connectivity",
    ],
    "dns_or_ip_change_after_workload_migration": [
        "dns", "ip change", "cname", "a record", "migration", "cloud", "cutover",
        "stale ip", "endpoint", "route53", "private dns",
    ],
    "patching_or_reboot_window_dependency_impact": [
        "patch", "patching", "reboot", "maintenance", "server restart",
        "windows update", "kernel update", "firmware", "upgrade",
    ],
    "recent_application_deployment_or_config_change": [
        "deployment", "release", "config", "configuration", "rollback", "version",
        "change", "deploy", "feature flag", "yaml", "properties",
    ],
    "load_balancer_or_traffic_shift_issue": [
        "load balancer", "lb", "f5", "nginx", "alb", "target group", "pool",
        "health check", "traffic shift", "weight", "vip",
    ],
    "downstream_dependency_latency_or_outage": [
        "downstream", "dependency", "upstream", "service unavailable", "latency",
        "slow", "timeout", "db", "database", "queue", "third party",
    ],
    "batch_job_or_scheduled_task_collision": [
        "batch", "job", "scheduled task", "cron", "scheduler", "etl", "nightly",
        "backup job", "reporting job",
    ],
    "endpoint_security_or_antivirus_scan_overlap": [
        "antivirus", "edr", "endpoint", "security scan", "crowdstrike", "defender",
        "scan", "agent", "malware", "av",
    ],
    "authentication_or_identity_provider_dependency": [
        "idp", "sso", "mfa", "saml", "oauth", "oidc", "ldap", "radius",
        "adfs", "azure ad", "entra", "okta", "duo", "password reset",
    ],
    "certificate_or_secret_rotation_issue": [
        "certificate", "cert", "tls", "ssl", "secret", "key rotation", "keystore",
        "truststore", "x509", "handshake", "expired",
    ],
    "cloud_migration_sizing_or_routing_mismatch": [
        "cloud migration", "migrated", "on-prem", "on prem", "subnet", "vpc",
        "vnet", "route table", "nat", "peering", "instance size", "rightsizing",
    ],
    "retry_storm_after_dependency_failure": [
        "retry", "retry storm", "thread", "thread pool", "connection pool",
        "circuit breaker", "timeout", "queue backlog",
    ],
}


def normalize_token(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def normalize_text(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("_", " ")
    text = re.sub(r"[^a-z0-9 .:/#-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def humanize_key(key: str) -> str:
    text = str(key or "").replace("_", " ").strip().title()
    replacements = {
        "Dns": "DNS", "Ip": "IP", "Api": "API", "Cpu": "CPU", "Db": "DB",
        "Ssl": "SSL", "Tls": "TLS", "Vpn": "VPN", "Sso": "SSO",
        "Mfa": "MFA", "Idp": "IdP", "Cmdb": "CMDB",
    }
    for src, repl in replacements.items():
        text = re.sub(rf"\b{src}\b", repl, text)
    return text


def dedupe_preserve_order(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        key = normalize_text(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def load_patterns(pattern_file: Path = DEFAULT_PATTERN_FILE) -> List[Dict[str, Any]]:
    if not pattern_file.exists():
        raise FileNotFoundError(
            f"Pattern file not found: {pattern_file}. Run build_enterprise_patterns.py first."
        )
    data = json.loads(pattern_file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Pattern file must contain a list. Found: {type(data).__name__}")
    return [row for row in data if isinstance(row, dict)]


def candidate_classification_tokens(classification: str) -> List[str]:
    cls = normalize_token(classification)
    tokens = [cls]
    for canonical, synonyms in CLASSIFICATION_SYNONYMS.items():
        synonym_tokens = [normalize_token(x) for x in synonyms]
        if cls == canonical or cls in synonym_tokens:
            tokens.append(normalize_token(canonical))
            tokens.extend(synonym_tokens)
    return dedupe_preserve_order(tokens)


def find_pattern_by_classification(
    patterns: List[Dict[str, Any]],
    classification: str,
    domain: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    wanted_tokens = set(candidate_classification_tokens(classification))
    domain_norm = normalize_token(domain)

    exact_matches = []
    synonym_matches = []
    domain_matches = []

    for pattern in patterns:
        pattern_cls = normalize_token(pattern.get("classification"))
        pattern_domain = normalize_token(pattern.get("domain"))
        if pattern_cls == normalize_token(classification):
            exact_matches.append(pattern)
        elif pattern_cls in wanted_tokens:
            synonym_matches.append(pattern)
        elif domain_norm and pattern_domain == domain_norm:
            domain_matches.append(pattern)

    if exact_matches:
        return exact_matches[0]
    if synonym_matches:
        return synonym_matches[0]
    if len(domain_matches) == 1:
        return domain_matches[0]
    return None


def scenario_signal_hits(scenario_id: str, ticket: Optional[str]) -> List[str]:
    if not ticket:
        return []
    blob = normalize_text(ticket)
    hits = []
    for term in SCENARIO_SIGNAL_TERMS.get(scenario_id, []):
        normalized = normalize_text(term)
        if normalized and normalized in blob:
            hits.append(term)
    return dedupe_preserve_order(hits)


def score_scenario(scenario: Dict[str, Any], ticket: Optional[str] = None) -> Dict[str, Any]:
    scenario_id = normalize_token(scenario.get("scenario_id") or scenario.get("associated_scenario"))
    confidence = float(scenario.get("confidence_score", 0.0) or 0.0)
    freq = str(scenario.get("frequency_band", "low") or "low").lower()
    priority = int(scenario.get("triage_priority", 10) or 10)
    hits = scenario_signal_hits(scenario_id, ticket)

    score = confidence
    score += FREQUENCY_WEIGHT.get(freq, 0.03)
    score += max(0.0, (11 - min(priority, 10)) * 0.015)
    score += min(0.18, len(hits) * 0.045)
    score = round(max(0.0, min(score, 1.0)), 4)

    return {
        "scenario_id": scenario_id,
        "scenario": str(scenario.get("scenario") or humanize_key(scenario_id)),
        "association_type": str(scenario.get("association_type") or "associated_operational_pattern"),
        "frequency_band": freq,
        "typical_time_window": str(scenario.get("typical_time_window") or ""),
        "confidence_score": round(confidence, 4),
        "triage_priority": priority,
        "ticket_signal_hits": hits,
        "match_score": score,
        "evidence_signals": dedupe_preserve_order(scenario.get("evidence_signals") or []),
        "validation_checks": dedupe_preserve_order(scenario.get("validation_checks") or []),
        "recommended_actions": dedupe_preserve_order(scenario.get("recommended_actions") or []),
        "pattern_candidate_id": scenario.get("pattern_candidate_id"),
    }


def score_scenarios(pattern: Dict[str, Any], ticket: Optional[str] = None, top_n: int = 10) -> List[Dict[str, Any]]:
    scenarios = pattern.get("top_associated_scenarios") or []
    scored = [score_scenario(s, ticket=ticket) for s in scenarios if isinstance(s, dict)]
    scored.sort(key=lambda x: (x.get("match_score", 0.0), -int(x.get("triage_priority", 10))), reverse=True)
    return scored[: max(1, int(top_n or 10))]


def derive_triage_order(scored_scenarios: List[Dict[str, Any]], pattern: Dict[str, Any]) -> List[str]:
    checks = []
    for scenario in scored_scenarios:
        checks.extend((scenario.get("validation_checks") or [])[:2])
    checks.extend(pattern.get("recommended_triage_order") or [])
    return dedupe_preserve_order(checks)[:10]


def get_correlation_intelligence(
    classification: str,
    domain: Optional[str] = None,
    ticket: Optional[str] = None,
    pattern_file: Path = DEFAULT_PATTERN_FILE,
    top_n: int = 10,
) -> Dict[str, Any]:
    warnings = []
    cls = normalize_token(classification)

    try:
        patterns = load_patterns(pattern_file)
    except Exception as exc:
        return {
            "status": "error",
            "classification": cls,
            "domain": normalize_token(domain),
            "matched_pattern": False,
            "pattern_id": None,
            "source_file": str(pattern_file),
            "top_associated_scenarios": [],
            "recommended_triage_order": [],
            "summary": {"error": str(exc)},
            "warnings": [str(exc)],
        }

    pattern = find_pattern_by_classification(patterns, cls, domain=domain)
    if not pattern:
        warnings.append(f"No enterprise correlation pattern found for classification={cls!r}.")
        return {
            "status": "ok",
            "classification": cls,
            "domain": normalize_token(domain),
            "matched_pattern": False,
            "pattern_id": None,
            "source_file": str(pattern_file),
            "top_associated_scenarios": [],
            "recommended_triage_order": [],
            "summary": {
                "pattern_count_loaded": len(patterns),
                "scenario_count": 0,
                "top_match_score": 0.0,
                "note": "Run build_enterprise_patterns.py with richer anonymized data or add pattern coverage.",
            },
            "warnings": warnings,
        }

    scored = score_scenarios(pattern, ticket=ticket, top_n=top_n)
    triage_order = derive_triage_order(scored, pattern)
    source_lineage = pattern.get("source_lineage") or {}

    return {
        "status": "ok",
        "classification": str(pattern.get("classification") or cls),
        "domain": str(pattern.get("domain") or domain or ""),
        "matched_pattern": True,
        "pattern_id": pattern.get("pattern_id"),
        "source_file": str(pattern_file),
        "top_associated_scenarios": scored,
        "recommended_triage_order": triage_order,
        "summary": {
            "pattern_count_loaded": len(patterns),
            "scenario_count": len(scored),
            "top_match_score": round(scored[0].get("match_score", 0.0), 4) if scored else 0.0,
            "source_origin": source_lineage.get("origin"),
            "observed_client_count": source_lineage.get("observed_client_count"),
            "candidate_count": source_lineage.get("candidate_count"),
            "raw_client_data_removed": source_lineage.get("raw_client_data_removed", True),
            "approved_for_global_use": source_lineage.get("approved_for_global_use", False),
        },
        "warnings": warnings,
    }


def print_human_readable(result: Dict[str, Any]) -> None:
    print("\nEnterprise Correlation Pattern Intelligence")
    print("=" * 56)
    print(f"Status           : {result.get('status')}")
    print(f"Classification   : {result.get('classification')}")
    print(f"Domain           : {result.get('domain')}")
    print(f"Matched Pattern  : {result.get('matched_pattern')}")
    print(f"Pattern ID       : {result.get('pattern_id') or '-'}")
    print(f"Source File      : {result.get('source_file')}")

    warnings = result.get("warnings") or []
    if warnings:
        print("\nWarnings")
        print("-" * 56)
        for warning in warnings:
            print(f"- {warning}")

    scenarios = result.get("top_associated_scenarios") or []
    if scenarios:
        print("\nTop Associated Scenarios")
        print("-" * 56)
        for idx, scenario in enumerate(scenarios, 1):
            print(f"{idx}. {scenario.get('scenario')}")
            print(f"   Scenario ID : {scenario.get('scenario_id')}")
            print(f"   Frequency   : {scenario.get('frequency_band')}")
            print(f"   Match Score : {scenario.get('match_score')}")
            print(f"   Time Window : {scenario.get('typical_time_window')}")
            hits = scenario.get("ticket_signal_hits") or []
            if hits:
                print(f"   Ticket Hits : {', '.join(hits)}")
            checks = scenario.get("validation_checks") or []
            if checks:
                print("   Checks      :")
                for check in checks[:4]:
                    print(f"     - {check}")
            actions = scenario.get("recommended_actions") or []
            if actions:
                print("   Actions     :")
                for action in actions[:3]:
                    print(f"     - {action}")
            print()

    triage = result.get("recommended_triage_order") or []
    if triage:
        print("\nRecommended Triage Order")
        print("-" * 56)
        for idx, item in enumerate(triage, 1):
            print(f"{idx}. {item}")

    print("\nSummary")
    print("-" * 56)
    print(json.dumps(result.get("summary") or {}, indent=2))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Runtime enterprise correlation pattern lookup engine.")
    parser.add_argument("--classification", required=True, help="Incident classification, e.g. vpn_client_failure.")
    parser.add_argument("--domain", default="", help="Optional domain, e.g. network.")
    parser.add_argument("--ticket", default="", help="Optional ticket text used to boost scenario ranking.")
    parser.add_argument("--pattern-file", default=str(DEFAULT_PATTERN_FILE), help="Path to enterprise_correlation_patterns.json.")
    parser.add_argument("--top-n", type=int, default=10, help="Number of scenarios to return.")
    parser.add_argument("--json", action="store_true", help="Print raw JSON only.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    result = get_correlation_intelligence(
        classification=args.classification,
        domain=args.domain,
        ticket=args.ticket,
        pattern_file=Path(args.pattern_file),
        top_n=args.top_n,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_human_readable(result)
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
