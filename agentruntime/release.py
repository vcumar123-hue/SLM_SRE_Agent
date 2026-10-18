"""
The Release 2 configuration freeze (owner amendment, design section 21).

Release 2 ships the deterministic `rules_only` planner. This module defines exactly what that means and makes it checkable:

  * the planner (`RELEASE_PLANNER`) and its entry point; the loop's default mode (`off`);
  * the frozen core: every runtime file the shipped investigation depends on (runner, executor, registry, gateway, contracts, verifier, sanitiser, store, replay,
    context, config, crypto, db limits), the five tools, and `planners/rules_only.py`. Their SHA-256 (CRLF read as LF) is recorded;
  * the budgets in force and their hard maxima, the rule constants and the ruleset version, the tool versions.

Deliberately NOT in the frozen core: the experimental model planners (`rules_then_model`, `model`, their prompts and support code), and the R2-2 integration
package (`agentruntime/integration/`), which wraps the frozen core and is tested on its own. The model planners are not approved for deployment: `policy_for`
refuses to build a policy for any planner other than the frozen one.

`verify()` compares the code on disk with the record and returns the differences; a test makes any difference a failing build. Changing frozen code is
allowed only by re-freezing with a recorded reason and approver (`refreeze`), which keeps the earlier record in `history`, so a change is always visible.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentruntime.contracts import BUDGET_DEFAULTS, BUDGET_HARD_MAX, AgentPolicy, Budgets
from agentruntime.tools import TOOL_MODULES

ROOT = Path(__file__).resolve().parent.parent
RELEASE_PLANNER = "rules_only"
PLANNER_ENTRY = "agentruntime.planners.rules_only:RulesOnlyPlanner"
DEFAULT_MODE = "off"
FREEZE_PATH = ROOT / "docs" / "baselines" / "release2" / "rules_only_release_freeze.json"
CORE_FILES = ("config", "context", "contracts", "crypto", "db_limits", "executor", "gateway", "registry", "replay", "runner", "sanitize", "store", "verifier")
FREEZE_VERSION = "1"


class ReleaseError(Exception):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def core_paths() -> Dict[str, Path]:
    base = ROOT / "agentruntime"
    out = {f"agentruntime/{name}.py": base / f"{name}.py" for name in CORE_FILES}
    for path in sorted((base / "tools").glob("*.py")):
        out[f"agentruntime/tools/{path.name}"] = path
    out["agentruntime/planners/rules_only.py"] = base / "planners" / "rules_only.py"
    return out


def current() -> Dict[str, Any]:
    """What the code on disk says the release is."""
    from agentruntime.planners import rules_only
    return {
        "freeze_version": FREEZE_VERSION, "planner": RELEASE_PLANNER, "planner_entry": PLANNER_ENTRY, "default_mode": DEFAULT_MODE,
        "ruleset_version": rules_only.RulesOnlyPlanner.ruleset_version, "prompt_version": rules_only.RulesOnlyPlanner.prompt_version, "model_id": None,
        "rule_constants": {k: getattr(rules_only, k) for k in ("MIN_RETRIEVAL_SCORE", "CONFLICT_GAP", "MIN_PATTERN_RESOLUTIONS", "QUERY_CHARS", "HISTORY_DAYS",
                                                                "MAX_HYPOTHESES", "CAUSE_CHARS")},
        "budgets": Budgets().model_dump(), "budget_defaults": dict(BUDGET_DEFAULTS), "budget_hard_max": dict(BUDGET_HARD_MAX),
        "tools": {m.NAME: m.VERSION for m in TOOL_MODULES},
        "files": {name: _sha256(path.read_bytes()) for name, path in core_paths().items()},
    }


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else FREEZE_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"the release freeze record cannot be read: {exc}") from exc


def differences(record: Dict[str, Any], now: Optional[Dict[str, Any]] = None) -> List[str]:
    """Human-readable differences between the frozen record and the code on disk (empty when unchanged)."""
    now = now or current()
    out: List[str] = []
    for key in ("freeze_version", "planner", "planner_entry", "default_mode", "ruleset_version", "prompt_version", "model_id", "rule_constants", "budgets",
                "budget_defaults", "budget_hard_max", "tools"):
        if record.get(key) != now.get(key):
            out.append(f"{key} differs from the record")
    frozen, live = record.get("files", {}), now["files"]
    for name in sorted(set(frozen) | set(live)):
        if frozen.get(name) != live.get(name):
            out.append(f"{name}: " + ("added" if name not in frozen else "removed" if name not in live else "changed"))
    return out


def verify(path: Optional[Path] = None) -> List[str]:
    return differences(load(path))


def write_first(reason: str, approved_by: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Create the record once. The approver is whoever the owner names; this function records it, it does not make an approval."""
    p = Path(path) if path else FREEZE_PATH
    if p.exists():
        raise ReleaseError(f"{p} already exists: use refreeze(), which keeps the earlier record")
    if not str(reason).strip() or not str(approved_by).strip():
        raise ReleaseError("a freeze needs a reason and the name of the owner who approved it")
    record = {**current(), "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason.strip(), "approved_by": approved_by.strip(), "history": []}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return record


def refreeze(reason: str, approved_by: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Replace the record after a deliberate change to frozen code. The previous record (hash, date, reason, approver, what changed) is kept in `history`."""
    p = Path(path) if path else FREEZE_PATH
    old = load(p)
    if not str(reason).strip() or not str(approved_by).strip():
        raise ReleaseError("re-freezing needs a reason and the name of the owner who approved it")
    changed = differences(old)
    if not changed:
        raise ReleaseError("nothing changed: there is nothing to re-freeze")
    entry = {"frozen_at": old.get("frozen_at"), "reason": old.get("reason"), "approved_by": old.get("approved_by"),
             "record_sha256": _sha256(json.dumps({k: v for k, v in old.items() if k != "history"}, sort_keys=True).encode("utf-8")), "superseded_because": reason.strip(),
             "superseded_by": approved_by.strip(), "changes": changed}
    record = {**current(), "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason.strip(), "approved_by": approved_by.strip(),
              "history": list(old.get("history", [])) + [entry]}
    p.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return record


def policy_for(mode: str = DEFAULT_MODE, planner: str = RELEASE_PLANNER, budgets: Optional[Budgets] = None) -> AgentPolicy:
    """The only way the application builds a run policy: the frozen planner, all five tools, and a mode that defaults to `off`.
    A planner other than the frozen one is refused (the model planners are experimental, not approved for deployment)."""
    if planner != RELEASE_PLANNER:
        raise ReleaseError(f"planner {planner!r} is not approved for deployment; Release 2 ships {RELEASE_PLANNER!r} (design section 21)")
    return AgentPolicy(allowed_tools=[m.NAME for m in TOOL_MODULES], budgets=budgets or Budgets(), planner=RELEASE_PLANNER, mode=mode)


def planner_factory(case: Optional[Dict[str, Any]] = None):
    """A fresh instance of the frozen planner."""
    from agentruntime.planners.rules_only import RulesOnlyPlanner
    return RulesOnlyPlanner(case)


if __name__ == "__main__":
    import sys
    problems = verify()
    print("\n".join(problems) if problems else "the release freeze matches the code")
    sys.exit(1 if problems else 0)
