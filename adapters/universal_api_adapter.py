"""
Universal API Adapter runner for AIOps Safety Gate evidence.

Purpose:
- Read config/client_onboarding/api_adapter_config.json
- Normalize incoming/pulled data to the universal evidence contract
- Write data/evidence/<evidence_type>.json using atomic replace

This file is intentionally lightweight. Day-1 rollout can use file evidence only.
API adapters can be enabled one-by-one without changing the safety gate engine.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "client_onboarding" / "api_adapter_config.json"
PULL_SPECS_DIR = ROOT / "config" / "client_onboarding" / "api_pull_specs"
EVIDENCE_DIR = ROOT / "data" / "evidence"

EVIDENCE_FILES = {
    "drift_status": "drift_status.json",
    "change_conflict": "change_conflict.json",
    "error_budget_status": "error_budget_status.json",
    "concurrent_activity": "concurrent_activity.json",
    "rollback_readiness": "rollback_readiness.json",
    "blast_radius": "blast_radius.json",
}

ALLOWED_STATUS = {"pass", "warn", "fail", "unknown"}


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")
    tmp.replace(path)


def normalize_evidence(
    evidence_type: str,
    service: str,
    environment: str,
    resource: str,
    status: str,
    source_system: str,
    summary: str,
    details: Optional[Dict[str, Any]] = None,
    severity: str = "low",
    source_record_id: Optional[str] = None,
    confidence: float = 0.8,
) -> Dict[str, Any]:
    status = (status or "unknown").lower()
    if status not in ALLOWED_STATUS:
        status = "unknown"
    if evidence_type not in EVIDENCE_FILES:
        raise ValueError(f"Unsupported evidence_type: {evidence_type}")
    return {
        "schema_version": "universal_evidence_contract_v1",
        "evidence_type": evidence_type,
        "service": service,
        "environment": environment,
        "resource": resource,
        "status": status,
        "severity": severity,
        "source_system": source_system,
        "source_record_id": source_record_id,
        "summary": summary,
        "details": details or {},
        "checked_at": utc_now(),
        "confidence": confidence,
    }


def write_evidence(evidence: Dict[str, Any]) -> Path:
    evidence_type = evidence.get("evidence_type")
    if evidence_type not in EVIDENCE_FILES:
        raise ValueError(f"Unsupported evidence_type: {evidence_type}")
    path = EVIDENCE_DIR / EVIDENCE_FILES[evidence_type]
    atomic_write_json(path, evidence)
    return path


class UniversalAdapter:
    """Base adapter interface. Concrete adapters can override collect()."""

    def __init__(self, name: str, cfg: Dict[str, Any]):
        self.name = name
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def collect(self, service: str, environment: str, resource: str) -> Iterable[Dict[str, Any]]:
        return []


class PlaceholderApiAdapter(UniversalAdapter):
    """Safe placeholder: emits UNKNOWN when adapter is enabled but not implemented."""

    def collect(self, service: str, environment: str, resource: str) -> Iterable[Dict[str, Any]]:
        for evidence_type in self.cfg.get("evidence_types", []):
            yield normalize_evidence(
                evidence_type=evidence_type,
                service=service,
                environment=environment,
                resource=resource,
                status="unknown",
                source_system=f"{self.name}:{self.cfg.get('provider', 'unknown')}",
                summary=f"{self.name} adapter enabled but API collector is not implemented yet.",
                details={"adapter_type": self.cfg.get("adapter_type"), "rollout_mode": "placeholder"},
                severity="medium",
                confidence=0.2,
            )


def load_config() -> Dict[str, Any]:
    return load_json(CONFIG_PATH, default={}) or {}


def run_enabled_adapters(service: str, environment: str, resource: str) -> Dict[str, Any]:
    cfg = load_config()
    connectors = cfg.get("connectors", {})
    written = []
    skipped = []
    for name, c in connectors.items():
        adapter = PlaceholderApiAdapter(name, c)
        if not adapter.enabled:
            skipped.append(name)
            continue
        for evidence in adapter.collect(service, environment, resource):
            path = write_evidence(evidence)
            written.append(str(path.relative_to(ROOT)))
    return {
        "status": "ok",
        "mode": "universal_api_adapter_runner",
        "service": service,
        "environment": environment,
        "resource": resource,
        "written": written,
        "skipped_disabled_connectors": skipped,
    }


def submit_evidence_file(path: Path) -> Dict[str, Any]:
    evidence = load_json(path, default=None)
    if not isinstance(evidence, dict):
        raise ValueError(f"Evidence file is not JSON object: {path}")
    out = write_evidence(evidence)
    return {"status": "ok", "written": str(out.relative_to(ROOT)), "evidence_type": evidence.get("evidence_type")}


def run_from_pull_specs(dry_run: bool = False) -> Dict[str, Any]:
    """Read all saved API pull spec files and execute HTTP pulls to refresh evidence.

    Spec files are written by the Client Onboarding Studio UI and stored in
    config/client_onboarding/api_pull_specs/<evidence_key>_api_pull_spec.json.
    Each spec defines the source API URL, auth headers, and field mappings.
    """
    try:
        import urllib.request
        import urllib.error
    except ImportError:
        return {"status": "error", "detail": "urllib not available"}

    if not PULL_SPECS_DIR.exists():
        return {"status": "ok", "detail": "No api_pull_specs directory found — nothing to run.", "results": []}

    spec_files = list(PULL_SPECS_DIR.glob("*_api_pull_spec.json"))
    if not spec_files:
        return {"status": "ok", "detail": "No pull specs saved yet.", "results": []}

    results: List[Dict[str, Any]] = []
    for spec_file in sorted(spec_files):
        key = spec_file.name.replace("_api_pull_spec.json", "")
        spec: Dict[str, Any] = load_json(spec_file, default={}) or {}
        url = str(spec.get("source_api_url") or "").strip()
        if not url or url.startswith("<"):
            results.append({"key": key, "status": "skipped", "detail": "No source_api_url configured"})
            continue

        # Build query params
        qp_text = str(spec.get("query_params_text") or "")
        params: Dict[str, str] = {}
        for line in qp_text.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("<"):
                k, _, v = line.partition("=")
                v = v.replace("<service>", str(spec.get("service") or ""))
                v = v.replace("<environment>", str(spec.get("environment") or ""))
                v = v.replace("<resource>", str(spec.get("resource") or ""))
                params[k.strip()] = v.strip()
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}" if "?" not in url else f"{url}&{qs}"

        # Build request
        auth_name = str(spec.get("auth_header_name") or "").strip()
        auth_value = str(spec.get("auth_header_value_hint") or "").strip()
        if dry_run:
            results.append({"key": key, "status": "dry_run", "url": url, "auth_header": auth_name})
            continue

        try:
            req = urllib.request.Request(url)
            if auth_name and auth_value and not auth_value.startswith("<"):
                req.add_header(auth_name, auth_value)
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                payload = json.loads(body)
        except urllib.error.HTTPError as e:
            results.append({"key": key, "status": "http_error", "code": e.code, "url": url})
            continue
        except Exception as ex:
            results.append({"key": key, "status": "error", "detail": str(ex), "url": url})
            continue

        # Map response to evidence contract using field names from spec
        required_fields = [f.strip() for f in str(spec.get("required_fields_text") or "").splitlines() if f.strip()]
        evidence_status = "unknown"
        for field in ("status", "gate_status", "health"):
            if field in payload:
                raw = str(payload[field]).lower()
                if raw in {"pass", "ok", "healthy", "clear"}:
                    evidence_status = "pass"
                elif raw in {"warn", "warning", "degraded"}:
                    evidence_status = "warn"
                elif raw in {"fail", "failed", "blocked", "critical"}:
                    evidence_status = "fail"
                else:
                    evidence_status = "unknown"
                break

        evidence = normalize_evidence(
            evidence_type=key,
            service=str(spec.get("service") or ""),
            environment=str(spec.get("environment") or ""),
            resource=str(spec.get("resource") or ""),
            status=evidence_status,
            source_system=str(spec.get("source_system_name") or spec_file.stem),
            summary=str(payload.get("summary") or payload.get("message") or f"{key} evidence from {spec.get('source_system_name', url)}."),
            details={f: payload.get(f) for f in required_fields if f in payload},
            severity=str(payload.get("severity") or "low"),
            confidence=float(payload.get("confidence") or 0.7),
        )
        # Provenance marker: evidence pulled from the configured source system is authoritative (see safety_evidence.py).
        evidence["_submitted_via"] = "api_pull"
        evidence["pulled_from"] = url
        path = write_evidence(evidence)
        results.append({"key": key, "status": "ok", "written": str(path.relative_to(ROOT)), "evidence_status": evidence_status})

    written = [r for r in results if r.get("status") == "ok"]
    return {
        "status": "ok",
        "mode": "api_pull_from_specs",
        "specs_found": len(spec_files),
        "written": len(written),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Universal API Adapter evidence runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run enabled adapters and write evidence files")
    run.add_argument("--service", required=True)
    run.add_argument("--environment", required=True)
    run.add_argument("--resource", required=True)

    submit = sub.add_parser("submit-file", help="Submit one universal evidence JSON file into data/evidence")
    submit.add_argument("path")

    pull = sub.add_parser("pull-specs", help="Run HTTP pulls from saved api_pull_specs/ and write evidence files")
    pull.add_argument("--dry-run", action="store_true", help="Show what would run without making HTTP calls")

    args = parser.parse_args()
    if args.cmd == "run":
        result = run_enabled_adapters(args.service, args.environment, args.resource)
    elif args.cmd == "pull-specs":
        result = run_from_pull_specs(dry_run=args.dry_run)
    else:
        result = submit_evidence_file(Path(args.path))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
