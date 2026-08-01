"""
FastAPI router — CMDB-backed Error Tolerance Engine.

Mounts under the prefix configured in app.py.
Endpoints live at /cmdb/... and /tolerance/...

Dependencies:
  adapters.servicenow_cmdb — get_adapter
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from adapters.servicenow_cmdb import get_adapter

router = APIRouter(tags=["error_tolerance"])

# ---------------------------------------------------------------------------
# Tier → tolerance policy mapping
# ---------------------------------------------------------------------------

_TIER_POLICIES: Dict[str, Dict[str, Any]] = {
    "prod-standalone": {
        "tolerance_minutes": 0,
        "auto_remediate": False,
        "escalation": "P1",
        "approval_required": True,
        "max_retry": 0,
    },
    "prod-redundant": {
        "tolerance_minutes": 5,
        "auto_remediate": True,
        "escalation": "P2",
        "approval_required": False,
        "max_retry": 2,
    },
    "prod-ha": {
        "tolerance_minutes": 10,
        "auto_remediate": True,
        "escalation": "P2",
        "approval_required": False,
        "max_retry": 3,
    },
    "non-prod": {
        "tolerance_minutes": 30,
        "auto_remediate": True,
        "escalation": "P3",
        "approval_required": False,
        "max_retry": 5,
    },
    "dr-standby": {
        "tolerance_minutes": 60,
        "auto_remediate": False,
        "escalation": "P3",
        "approval_required": True,
        "max_retry": 0,
    },
    "unknown": {
        "tolerance_minutes": 15,
        "auto_remediate": False,
        "escalation": "P2",
        "approval_required": True,
        "max_retry": 1,
    },
}

_VALID_TIERS = set(_TIER_POLICIES.keys())

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOT_FOUND_HINT = "Register via POST /cmdb/ci or sync from ServiceNow via POST /cmdb/sync"


def _not_found(ci_name: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"detail": f"CI not found: {ci_name}", "hint": _NOT_FOUND_HINT},
    )


def _derive_tier(ci: Dict[str, Any]) -> str:
    """
    Derive the logical tier from a CI profile dict.

    Rules (evaluated in order):
      1. If ci already has an explicit 'tier' field, use it if recognised.
      2. environment = non-prod variants → "non-prod"
      3. environment = prod + redundancy_type = standalone → "prod-standalone"
      4. environment = prod + redundancy_type = ha / active-active → "prod-ha"
      5. environment = prod + redundancy_type = active-passive / cluster → "prod-redundant"
      6. environment = dr / disaster-recovery → "dr-standby"
      7. Fallback → "unknown"
    """
    explicit = (ci.get("tier") or "").lower().strip()
    if explicit in _VALID_TIERS:
        return explicit

    env = (ci.get("environment") or "").lower().strip()
    redundancy = (ci.get("redundancy_type") or "").lower().strip()

    non_prod_envs = {"dev", "development", "test", "qa", "staging", "uat", "non-prod", "nonprod"}
    dr_envs = {"dr", "disaster-recovery", "disaster_recovery"}
    ha_redundancy = {"ha", "active-active", "active_active"}
    standalone_redundancy = {"standalone", "none", "single", ""}

    if env in dr_envs:
        return "dr-standby"
    if env in non_prod_envs:
        return "non-prod"
    if "prod" in env:
        if redundancy in ha_redundancy:
            return "prod-ha"
        if redundancy in standalone_redundancy:
            return "prod-standalone"
        return "prod-redundant"
    return "unknown"


def _get_policy(tier: str) -> Dict[str, Any]:
    return dict(_TIER_POLICIES.get(tier, _TIER_POLICIES["unknown"]))


def _recommendation(
    within_tolerance: bool,
    auto_remediate: bool,
    approval_required: bool,
    escalation: str,
) -> str:
    if within_tolerance and auto_remediate and not approval_required:
        return "auto_remediate"
    if within_tolerance and not auto_remediate:
        return "monitor"
    if escalation == "P1":
        return "page_oncall"
    return "escalate"


def _reasoning(
    ci_name: str,
    tier: str,
    within_tolerance: bool,
    policy: Dict[str, Any],
    elapsed_minutes: float,
) -> str:
    tol = policy["tolerance_minutes"]
    esc = policy["escalation"]
    if within_tolerance:
        return (
            f"{ci_name} ({tier}) has been impacted for {elapsed_minutes:.1f} min, "
            f"which is within the {tol}-minute tolerance window. "
            f"Auto-remediation is {'enabled' if policy['auto_remediate'] else 'disabled'} for this tier."
        )
    return (
        f"{ci_name} ({tier}) has exceeded its tolerance window "
        f"({elapsed_minutes:.1f} min > {tol} min). "
        f"Escalation level {esc} required. "
        f"{'Approval is required before any changes.' if policy['approval_required'] else 'No approval gate.'}"
    )


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class CIRegisterRequest(BaseModel):
    ci_name: str
    environment: str
    redundancy_type: str
    business_criticality: int
    assignment_group: str
    failover_target: Optional[str] = None
    notes: str = ""

    @field_validator("business_criticality")
    @classmethod
    def _validate_criticality(cls, v: int) -> int:
        if v < 1 or v > 5:
            raise ValueError("business_criticality must be between 1 and 5")
        return v


class SyncRequest(BaseModel):
    ci_names: List[str] = []


class EvaluateRequest(BaseModel):
    ci_name: str
    incident_classification: str
    severity: str
    elapsed_minutes: float

    @field_validator("elapsed_minutes")
    @classmethod
    def _validate_elapsed(cls, v: float) -> float:
        if v < 0:
            raise ValueError("elapsed_minutes must be >= 0")
        return v


# ---------------------------------------------------------------------------
# Endpoints — CMDB
# ---------------------------------------------------------------------------


@router.get("/cmdb/ci")
def list_cis(
    query: str = Query(""),
    limit: int = Query(50, ge=1, le=1000),
) -> Dict[str, Any]:
    """List or search CIs from the local cache."""
    try:
        adapter = get_adapter()
        cis = adapter.list_cache(query=query, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "cis": cis, "count": len(cis), "source": "cache"}


@router.get("/cmdb/ci/{ci_name}")
def get_ci_profile(ci_name: str) -> Dict[str, Any]:
    """Return a CI profile with derived tier and tolerance policy."""
    try:
        adapter = get_adapter()
        ci = adapter.get_with_fallback(ci_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if ci is None:
        raise _not_found(ci_name)
    tier = _derive_tier(ci)
    policy = _get_policy(tier)
    return {"status": "ok", "ci": ci, "tier": tier, "policy": policy}


@router.post("/cmdb/ci")
def register_ci(req: CIRegisterRequest) -> Dict[str, Any]:
    """Manually register or update a CI in the local cache."""
    ci_profile: Dict[str, Any] = {
        "ci_name": req.ci_name,
        "environment": req.environment,
        "redundancy_type": req.redundancy_type,
        "business_criticality": req.business_criticality,
        "assignment_group": req.assignment_group,
        "failover_target": req.failover_target,
        "notes": req.notes,
        "source": "manual",
    }
    try:
        adapter = get_adapter()
        saved = adapter.upsert_cache(req.ci_name, ci_profile)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "ci": saved or ci_profile}


@router.post("/cmdb/sync")
def sync_cis(req: SyncRequest) -> Dict[str, Any]:
    """Sync one or more CIs from ServiceNow into the local cache."""
    try:
        adapter = get_adapter()
        # If no names given, derive from existing cache keys
        names = req.ci_names if req.ci_names else adapter.cache_keys()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    results: List[Dict[str, Any]] = []
    synced = 0
    failed = 0

    for ci_name in names:
        try:
            ci = adapter.get_ci(ci_name)
            if ci:
                adapter.upsert_cache(ci_name, ci)
                results.append({"ci_name": ci_name, "status": "synced"})
                synced += 1
            else:
                results.append({"ci_name": ci_name, "status": "not_found"})
                failed += 1
        except Exception as exc:
            results.append({"ci_name": ci_name, "status": "error", "detail": str(exc)})
            failed += 1

    return {"status": "ok", "synced": synced, "failed": failed, "results": results}


@router.get("/cmdb/stats")
def cmdb_stats() -> Dict[str, Any]:
    """Return summary statistics of the local CMDB cache."""
    try:
        adapter = get_adapter()
        all_cis = adapter.list_cache(query="", limit=10_000)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    by_tier: Dict[str, int] = defaultdict(int)
    by_env: Dict[str, int] = defaultdict(int)
    by_redundancy: Dict[str, int] = defaultdict(int)

    for ci in all_cis:
        tier = _derive_tier(ci)
        by_tier[tier] += 1
        by_env[ci.get("environment", "unknown")] += 1
        by_redundancy[ci.get("redundancy_type", "unknown")] += 1

    return {
        "status": "ok",
        "total": len(all_cis),
        "by_tier": dict(by_tier),
        "by_environment": dict(by_env),
        "by_redundancy_type": dict(by_redundancy),
    }


# ---------------------------------------------------------------------------
# Endpoints — Tolerance
# ---------------------------------------------------------------------------


@router.get("/tolerance/{ci_name}")
def get_tolerance_policy(ci_name: str) -> Dict[str, Any]:
    """Return the full tolerance policy for a CI, derived from its tier."""
    try:
        adapter = get_adapter()
        ci = adapter.get_with_fallback(ci_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if ci is None:
        raise _not_found(ci_name)

    tier = _derive_tier(ci)
    policy = _get_policy(tier)
    source = ci.get("source", "cache")
    return {
        "status": "ok",
        "ci_name": ci_name,
        "tier": tier,
        "policy": policy,
        "source": source,
    }


@router.post("/tolerance/evaluate")
def evaluate_incident(req: EvaluateRequest) -> Dict[str, Any]:
    """
    Evaluate an active incident against the CI's tolerance policy.

    Determines whether the elapsed time is within tolerance, whether
    auto-remediation should fire, what escalation level applies, and
    provides a human-readable recommendation with reasoning.
    """
    try:
        adapter = get_adapter()
        ci = adapter.get_with_fallback(req.ci_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if ci is None:
        raise _not_found(req.ci_name)

    tier = _derive_tier(ci)
    policy = _get_policy(tier)

    tolerance_minutes: int = policy["tolerance_minutes"]
    auto_remediate: bool = policy["auto_remediate"]
    escalation: str = policy["escalation"]
    approval_required: bool = policy["approval_required"]

    within_tolerance = req.elapsed_minutes <= tolerance_minutes
    # auto-remediate only fires if within tolerance and the policy allows it
    should_auto_remediate = within_tolerance and auto_remediate and not approval_required
    recommendation = _recommendation(within_tolerance, auto_remediate, approval_required, escalation)
    reasoning = _reasoning(req.ci_name, tier, within_tolerance, policy, req.elapsed_minutes)

    return {
        "status": "ok",
        "ci_name": req.ci_name,
        "tier": tier,
        "within_tolerance": within_tolerance,
        "should_auto_remediate": should_auto_remediate,
        "escalation_level": escalation,
        "approval_required": approval_required,
        "recommendation": recommendation,
        "reasoning": reasoning,
        "policy": policy,
    }
