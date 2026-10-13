"""
Safety Gate evidence semantics (Release 1, Wave 2).

Two independent questions are answered for every piece of evidence:

  1. What does it SAY?          raw status: pass | warn | fail | unknown
                                (anything missing, malformed or unrecognised is `unknown`, never `pass`)
  2. How far can it be TRUSTED? provenance:
        verified   authoritative: pulled from a configured source system, pushed by an authenticated
                   principal, or captured from real state (CMDB / lab container)
        synthetic  produced from simulated state (dry-mode snapshot, dry-run simulation)
        estimated  inferred by the platform (blast radius, rollback readiness) and not verified
        unknown    origin cannot be established: missing, unreadable, malformed or unattested

The effective status then depends on the execution class:

  simulation  may proceed on degraded evidence, but its safety evidence is never reported as passed
  live        degraded evidence needs an explicit, recorded human override
  autonomous  degraded evidence blocks

Provenance is a platform assertion recorded when evidence is written (authenticated API push, API pull,
gate-generated evidence). Evidence files are not tamper-proof; anyone with write access to the evidence
folder can forge them. Self-asserted `provenance` fields inside client files are ignored.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

PROVENANCE_VALUES = ("verified", "synthetic", "estimated", "unknown")
MODE_CLASSES = ("simulation", "live", "autonomous")

_STATUS_ORDER = {"pass": 0, "unknown": 1, "warn": 2, "fail": 3}
_PROVENANCE_RANK = {"verified": 3, "unknown": 2, "estimated": 1, "synthetic": 0, "not_applicable": 3}

_PASS = {"pass", "passed", "ok", "healthy", "clear", "green"}
_WARN = {"warn", "warning", "degraded", "amber"}
_FAIL = {"fail", "failed", "block", "blocked", "critical", "red"}
_UNKNOWN = {"unknown", "missing", "not_available"}

# Evidence files written by older versions of the gate into the client folder, identified by source_system.
_LEGACY_GENERATED_SOURCES = {
    "snapshot_engine_dry": "synthetic",
    "snapshot_engine_lab": "verified",
    "snapshot_engine_cmdb": "verified",
    "blast_radius_estimator_live": "estimated",
    "dry_run_simulation": "synthetic",
}

DEFAULT_EVIDENCE_POLICY: Dict[str, Any] = {
    # Applied when a check would otherwise pass but its evidence is missing, malformed, unrecognised
    # or not verified.
    "degraded_status": {"simulation": "unknown", "live": "warn", "autonomous": "fail"},
    # Applied when passing evidence is older than its TTL.
    "stale_status": {"simulation": "warn", "live": "warn", "autonomous": "fail"},
    # Evidence uploaded as a file (UI upload, manual entry, seed data) cannot be authenticated. Set true
    # only if the client accepts file uploads as authoritative.
    "treat_uploaded_files_as_verified": False,
}


# -- statuses ---------------------------------------------------------------------------------------

def _token(value: Any) -> str:
    return str(value if value is not None else "").strip().lower().replace(" ", "_").replace("-", "_")


def is_recognized_status(value: Any) -> bool:
    t = _token(value)
    return t in _PASS or t in _WARN or t in _FAIL or t in _UNKNOWN


def normalize_status(value: Any) -> str:
    """pass | warn | fail | unknown. Missing or unrecognised values are `unknown`, never `pass`."""
    t = _token(value)
    if t in _PASS:
        return "pass"
    if t in _WARN:
        return "warn"
    if t in _FAIL:
        return "fail"
    return "unknown"


def worse_status(a: str, b: str) -> str:
    return a if _STATUS_ORDER.get(a, 1) >= _STATUS_ORDER.get(b, 1) else b


# -- mode and policy --------------------------------------------------------------------------------

def gate_mode_class(execution_mode: Any) -> str:
    t = _token(execution_mode)
    if t in {"autonomous", "auto_execute"}:
        return "autonomous"
    if t in {"live", "live_human_approved", "human_approved"}:
        return "live"
    return "simulation"


def resolve_policy(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge the configured evidence policy over the defaults, ignoring invalid entries."""
    policy = copy.deepcopy(DEFAULT_EVIDENCE_POLICY)
    raw = (config or {}).get("evidence_policy")
    if not isinstance(raw, dict):
        return policy
    for key in ("degraded_status", "stale_status"):
        override = raw.get(key)
        if isinstance(override, dict):
            for mode in MODE_CLASSES:
                value = _token(override.get(mode))
                if value in {"unknown", "warn", "fail"}:
                    policy[key][mode] = value
    if isinstance(raw.get("treat_uploaded_files_as_verified"), bool):
        policy["treat_uploaded_files_as_verified"] = raw["treat_uploaded_files_as_verified"]
    return policy


# -- provenance -------------------------------------------------------------------------------------

def client_provenance(evidence: Dict[str, Any], source: str, policy: Dict[str, Any]) -> str:
    """Provenance of evidence supplied by or on behalf of the client (API, push, or a file in the evidence folder)."""
    if source == "api":
        return "verified"  # pulled live from a configured source system
    via = _token(evidence.get("_submitted_via"))
    if via == "api_pull":
        return "verified"
    if via == "api_push" and str(evidence.get("submitted_by") or "").strip():
        return "verified"  # accepted from an authenticated principal
    legacy = _LEGACY_GENERATED_SOURCES.get(_token(evidence.get("source_system")))
    if legacy:
        return legacy  # written by an older version of the gate into the client folder
    if policy.get("treat_uploaded_files_as_verified"):
        return "verified"
    return "unknown"


def generated_provenance(evidence: Dict[str, Any]) -> str:
    """Provenance of platform-generated evidence (read only from the generated folder)."""
    value = _token(evidence.get("provenance"))
    return value if value in PROVENANCE_VALUES else "unknown"


# -- effective status -------------------------------------------------------------------------------

def effective_status(
    raw_status: Any,
    provenance: str,
    issue: str,
    stale: bool,
    mode_class: str,
    policy: Dict[str, Any],
) -> Tuple[str, List[str]]:
    """Apply the execution-class policy. Returns (status, reasons)."""
    mode_class = mode_class if mode_class in MODE_CLASSES else "simulation"
    status = normalize_status(raw_status)
    reasons: List[str] = []

    if stale:
        reasons.append("stale")
        if status == "pass":
            status = policy["stale_status"][mode_class]

    degraded = bool(issue) or provenance != "verified" or status == "unknown"
    if degraded and status in {"pass", "unknown"}:
        if issue:
            reasons.append(issue)
        if provenance != "verified":
            reasons.append(provenance)
        if status == "unknown" and not issue and provenance == "verified":
            reasons.append("unknown_status")
        status = worse_status(status, policy["degraded_status"][mode_class])
    return status, reasons


_RAW_PROBLEM_ORDER = {"warn": 1, "fail": 2}


def choose_candidate(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick the evidence a check is judged on when there is more than one source.

    1. Evidence that actually reports a problem (raw warn/fail) wins, whatever its provenance: a weak source
       can raise a concern but never hide one.
    2. Otherwise the most trustworthy source wins (verified > unknown > estimated > synthetic), so degraded
       generated evidence cannot veto genuinely verified client evidence. Ties go to the more conservative
       effective status.
    """
    problems = [c for c in candidates if c.get("raw_status") in _RAW_PROBLEM_ORDER]
    if problems:
        return max(problems, key=lambda d: (_RAW_PROBLEM_ORDER[d["raw_status"]], _PROVENANCE_RANK.get(d.get("provenance"), 0)))
    return max(
        candidates,
        key=lambda d: (_PROVENANCE_RANK.get(d.get("provenance"), 0), _STATUS_ORDER.get(d.get("status"), 1)),
    )


def warning_reason(detail: Dict[str, Any]) -> str:
    """Stable, mode-independent reason a check is not clean. Used for warning identities."""
    reasons = detail.get("policy_reasons") or []
    if detail.get("ttl_warning") or "stale" in reasons:
        return "stale"
    issue = str(detail.get("evidence_issue") or "").strip()
    if issue:
        return issue
    provenance = str(detail.get("provenance") or "").strip()
    if provenance and provenance not in {"verified", "not_applicable"}:
        return provenance
    return "unknown" if normalize_status(detail.get("raw_status", detail.get("status"))) == "unknown" else "warn"


def provenance_summary(checks: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    counts = {"verified": 0, "synthetic": 0, "estimated": 0, "unknown": 0, "not_applicable": 0}
    for detail in checks.values():
        key = str((detail or {}).get("provenance") or "unknown")
        counts[key if key in counts else "unknown"] += 1
    return counts
