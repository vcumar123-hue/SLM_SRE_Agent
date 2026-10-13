"""
The single autonomy decision (Release 1, Wave 3).

Autonomous execution used to be judged in four places that checked different things (queue-intent creation,
execute authorization, the eligibility endpoint and the enable endpoint). `decide()` composes every component and
any deny wins:

  registry     the approved mapping is enabled for autonomous use (skipped when purpose is "enable")
  ledger       outcome-ledger tier AFTER the recurrence cap must be `auto_execute`
               (`auto_execute_watch` requires a human in Release 1; `hold_for_approval` denies)
  confidence   feedback-based maturity eligibility (clean runs, no pending review, last gate passed, ...)
  safety       every Safety Gate check must be enabled (a disabled check is an explicit client decision that
               autonomous execution does not inherit)

A component that cannot be evaluated denies (fail closed). The decision is only the PERMISSION to act
autonomously; a service identity, a server-created intent, an unchanged plan hash and a fresh passed Safety
Gate are still required by execution_authorization.authorize_execution.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

PURPOSES = ("enable", "intent", "execute", "eligibility")
TIER_ORDER = {"hold_for_approval": 0, "auto_execute_watch": 1, "auto_execute": 2}


@dataclass
class AutonomyDecision:
    allowed: bool
    purpose: str
    classification: str
    tenant_id: str
    tier: str = "hold_for_approval"
    requires_human_approval: bool = True
    reasons: List[str] = field(default_factory=list)
    components: Dict[str, Any] = field(default_factory=dict)
    decided_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe(name: str, fn: Callable[[], Any], reasons: List[str]) -> Optional[Any]:
    """Evaluate a component; an exception denies (fail closed)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - any failure to evaluate must deny
        reasons.append(f"{name} could not be evaluated ({type(exc).__name__}): autonomous execution denied (fail closed)")
        return None


def decide(
    classification: str,
    tenant_id: str,
    purpose: str,
    *,
    registry_check: Callable[[str, str, str], Dict[str, Any]],
    ledger_check: Callable[[str, str], Dict[str, Any]],
    confidence_check: Callable[[str, str, str], Dict[str, Any]],
    config_check: Callable[[], List[str]],
    action: str = "",
    playbook: str = "",
) -> AutonomyDecision:
    purpose = purpose if purpose in PURPOSES else "execute"
    decision = AutonomyDecision(
        allowed=False, purpose=purpose, classification=classification, tenant_id=tenant_id,
        decided_at=datetime.now(timezone.utc).isoformat(),
    )
    reasons: List[str] = []

    # 1. registry mapping (an "enable" request is what creates autonomous readiness, so it is not required yet)
    if purpose != "enable":
        registry = _safe("registry mapping", lambda: registry_check(classification, action, playbook), reasons)
        if registry is not None:
            decision.components["registry"] = {k: registry.get(k) for k in ("allowed", "status", "reason", "registry_key")}
            if not registry.get("allowed"):
                reasons.append(f"registry: {registry.get('reason') or 'mapping is not enabled for autonomous execution'}")

    # 2. outcome ledger tier, after the recurrence cap
    ledger = _safe("outcome ledger", lambda: ledger_check(classification, tenant_id), reasons)
    if ledger is not None:
        decision.components["ledger"] = ledger
        decision.tier = str(ledger.get("tier") or "hold_for_approval")
        if decision.tier == "auto_execute_watch":
            decision.requires_human_approval = True
            capped = " by recurrence" if ledger.get("recurrence_cap_applied") else ""
            reasons.append(
                f"outcome ledger tier is auto_execute_watch (capped{capped}): autonomous execution requires human approval"
                if capped else
                "outcome ledger tier is auto_execute_watch: autonomous execution requires human approval"
            )
        elif decision.tier != "auto_execute":
            reasons.append(f"outcome ledger tier '{decision.tier}': {ledger.get('reason', 'insufficient history')}")
        else:
            decision.requires_human_approval = False
    else:
        decision.tier = "hold_for_approval"

    # 3. feedback-based maturity eligibility
    confidence = _safe("confidence eligibility", lambda: confidence_check(classification, action, playbook), reasons)
    if confidence is not None:
        decision.components["confidence"] = {
            k: confidence.get(k) for k in ("automation_key", "automation_confidence", "autonomous_eligible",
                                           "eligibility_reasons", "clean_successful_runs", "pending_feedback",
                                           "latest_safety_gate")
        }
        if not confidence.get("autonomous_eligible"):
            why = "; ".join(str(r) for r in (confidence.get("eligibility_reasons") or [])) or "not yet eligible"
            reasons.append(f"confidence: {why}")

    # 4. safety configuration: every check enabled
    disabled = _safe("safety configuration", config_check, reasons)
    if disabled is not None:
        decision.components["safety_config"] = {"disabled_checks": list(disabled)}
        if disabled:
            reasons.append(
                f"safety checks disabled ({', '.join(sorted(disabled))}): autonomous execution requires every check enabled"
            )

    decision.reasons = reasons
    decision.allowed = not reasons
    if not decision.allowed:
        decision.requires_human_approval = True
    return decision
