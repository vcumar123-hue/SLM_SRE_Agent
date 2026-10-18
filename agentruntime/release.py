"""
The Release 2 configuration freeze (owner amendment, design section 21).

Release 2 ships the deterministic `rules_only` planner. This module defines exactly what that means and makes it checkable:

  * the planner (`RELEASE_PLANNER`) and its entry point; the loop's default mode (`off`);
  * the frozen core: every runtime file the shipped investigation depends on (runner, executor, registry, gateway, contracts, verifier, sanitiser, store, replay,
    context, config, crypto, db limits), the five tools, and `planners/rules_only.py`. Their SHA-256 (CRLF read as LF) is recorded;
  * the budgets in force and their hard maxima, the rule constants and the ruleset version, the tool versions.

Also frozen (review 2026-10-17): the whole R2-2 integration package (`agentruntime/integration/`), and, as source fingerprints of the functions and lines that matter, the
route hooks (`agents/analyze.py`: the /analyze handler and the `_loop_*` helpers), the pipeline call site and helper (`app.py`: `_r2_before_fallback` and every line that
mentions `_r2_`), and the confidence input (`confidence_engine.py`: `apply_evidence_grounding`, `compute_confidence_score`, `EVIDENCE_GROUNDING_WEIGHT`). Unrelated edits to
those big files do not move the freeze; any edit to the loop's hooks does.

Deliberately NOT frozen: the experimental model planners (`rules_then_model`, `model`, their prompts and support code). They are not approved for deployment:
`policy_for` refuses to build a policy for any planner other than the frozen one.

The freeze also records WHO approved it. `approval_confirmed` stays false until the owner has named themselves and said so (`confirm_approval`); `problems()` reports an
unconfirmed approval and the integration gate resolves the loop to `off` while it does.

`verify()` compares the code on disk with the record and returns the differences; a test makes any difference a failing build. Changing frozen code is
allowed only by re-freezing with a recorded reason and approver (`refreeze`), which keeps the earlier record in `history`, so a change is always visible.
"""
from __future__ import annotations

import ast
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
    for path in sorted((base / "integration").glob("*.py")):
        out[f"agentruntime/integration/{path.name}"] = path
    return out


# (file, function names) whose source is fingerprinted, and (file, token) whose mentioning lines are fingerprinted
FUNCTIONS = (("agents/analyze.py", ("analyze", "_loop_scope", "_loop_after", "_loop_background", "_loop_response")),
             ("app.py", ("_r2_before_fallback",)),
             ("confidence_engine.py", ("apply_evidence_grounding", "compute_confidence_score")))
TOKEN_LINES = (("app.py", "_r2_"), ("confidence_engine.py", "EVIDENCE_GROUNDING_WEIGHT"), ("agents/analyze.py", "_loop_"))


def _norm_text(data: bytes) -> str:
    return data.decode("utf-8").replace("\r\n", "\n")


def source_fingerprints() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for rel, names in FUNCTIONS:
        text = _norm_text((ROOT / rel).read_bytes())
        found = {}
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                found[node.name] = ast.get_source_segment(text, node) or ""
        for name in names:
            out[f"{rel}::{name}"] = hashlib.sha256(found[name].encode("utf-8")).hexdigest() if name in found else "MISSING"
    for rel, token in TOKEN_LINES:
        lines = [ln.rstrip() for ln in _norm_text((ROOT / rel).read_bytes()).split("\n") if token in ln]
        out[f"{rel}::lines-with::{token}"] = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
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
        "source_fingerprints": source_fingerprints(),
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
    for group in ("files", "source_fingerprints"):
        frozen, live = record.get(group, {}), now[group]
        for name in sorted(set(frozen) | set(live)):
            if frozen.get(name) != live.get(name):
                out.append(f"{name}: " + ("added" if name not in frozen else "removed" if name not in live else "changed"))
    return out


def approval_problems(record: Dict[str, Any]) -> List[str]:
    """The freeze is usable only once the owner has been named and has confirmed it; until then the loop stays off."""
    out: List[str] = []
    if record.get("approval_confirmed") is not True:
        out.append("the owner's named approval of the release freeze is not confirmed")
    if not str(record.get("approved_by") or "").strip():
        out.append("the release freeze names no approver")
    return out


def problems(path: Optional[Path] = None) -> List[str]:
    """Everything that should keep the loop off: code that differs from the record, and an approval that is not confirmed."""
    record = load(path)
    return differences(record) + approval_problems(record)


def confirm_approval(approver: str, statement: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Record that the named owner has explicitly approved the freeze, with their statement. This function records an approval; it cannot make one."""
    p = Path(path) if path else FREEZE_PATH
    record = load(p)
    if not str(approver).strip() or len(str(statement).strip()) < 20:
        raise ReleaseError("an approval needs the owner's name and their statement")
    record.setdefault("approval_history", []).append({"approver": approver.strip(), "statement": statement.strip(),
                                                      "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "for_record_frozen_at": record.get("frozen_at")})
    record["approved_by"], record["approval_confirmed"] = approver.strip(), True
    p.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return record


def verify(path: Optional[Path] = None) -> List[str]:
    return differences(load(path))


def write_first(reason: str, approved_by: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Create the record once. The approver is whoever the owner names; this function records it, it does not make an approval."""
    p = Path(path) if path else FREEZE_PATH
    if p.exists():
        raise ReleaseError(f"{p} already exists: use refreeze(), which keeps the earlier record")
    if not str(reason).strip() or not str(approved_by).strip():
        raise ReleaseError("a freeze needs a reason and the name of the owner who approved it")
    record = {**current(), "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason.strip(), "approved_by": approved_by.strip(), "approval_confirmed": False, "approval_history": [],
              "history": []}
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
    record = {**current(), "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason.strip(), "approved_by": approved_by.strip(), "approval_confirmed": False,
              "approval_history": list(old.get("approval_history", [])), "history": list(old.get("history", [])) + [entry]}
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
    found = problems()
    print("\n".join(found) if found else "the release freeze matches the code and its approval is confirmed")
    sys.exit(1 if found else 0)
