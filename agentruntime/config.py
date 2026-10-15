"""
Mode and budget resolution (design 1 invariant 7, 7.1 and owner decision 5).

`off` is the default. Missing, empty or invalid configuration resolves to `off`; so does a requirement for encryption without a key.
Budgets come from config/agent_runtime.json (human-edited, never agent-writable) and can only lower the defaults; the hard maxima are
constants in contracts.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple

from agentruntime import crypto
from agentruntime.contracts import BUDGET_DEFAULTS, BUDGET_HARD_MAX, Budgets

MODE_ENV = "AIOPS_AGENTIC_RCA_LOOP"
VALID_MODES = {"off", "shadow", "on"}
CONFIG_PATH = Path(os.getenv("AIOPS_AGENT_RUNTIME_CONFIG", "config/agent_runtime.json"))


def resolve_mode(env_value: Optional[str] = None) -> Tuple[str, str]:
    """Return (mode, reason). Anything other than a valid `shadow` or `on` with a usable setup is `off`."""
    raw = os.getenv(MODE_ENV, "") if env_value is None else env_value
    value = (raw or "").strip().lower()
    if not value:
        return "off", "not configured"
    if value not in VALID_MODES:
        return "off", f"invalid value '{value}'"
    if value == "off":
        return "off", "configured off"
    try:
        crypto.get_cipher()
    except crypto.KeyMissing as exc:
        return "off", f"stored run data cannot be protected: {exc}"
    return value, "configured"


def load_budgets(path: Optional[Path] = None) -> Budgets:
    """Defaults, lowered (never raised) by the config file. An unreadable or invalid file yields the defaults."""
    cfg_path = path or CONFIG_PATH
    values = dict(BUDGET_DEFAULTS)
    try:
        if cfg_path.is_file():
            data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            for key, value in (data.get("budgets") or {}).items():
                if key in BUDGET_HARD_MAX:
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= number <= BUDGET_DEFAULTS[key]:      # configuration may only lower the default
                        values[key] = number
    except (OSError, ValueError, AttributeError):
        values = dict(BUDGET_DEFAULTS)
    return Budgets(**values)


def retention_days(tenant_config: Optional[dict] = None) -> int:
    """90 days by default; a tenant may shorten or lengthen it with `agent_run_retention_days` (1-3650)."""
    try:
        value = int((tenant_config or {}).get("agent_run_retention_days", 90))
    except (TypeError, ValueError):
        return 90
    return value if 1 <= value <= 3650 else 90


def clamp_retention_days(value: object) -> int:
    """Bound a caller-supplied retention to 1-3650 days (an unusable value means the 90-day default). Used by the store itself."""
    try:
        days = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 90
    return max(1, min(days, 3650))
