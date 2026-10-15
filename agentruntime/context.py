"""
TrustedContext: the only source of the tenant for a run (design 4.1).

It is built by the server from the authenticated principal and the request, never from model output, is immutable, and construction
fails closed: a missing, unknown, `default` or not-permitted tenant raises ContextError and the run is `denied_policy`.
There is no default tenant inside the runtime.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

_TENANT_RE = re.compile(r"^[a-z0-9_]{1,64}$")
KNOWLEDGE_SCOPES: Tuple[str, ...] = ("client_private",)   # the shared layers are added by the retrieval wrapper, not chosen by anyone else


class ContextError(Exception):
    """The trusted context could not be built; the caller records `denied_policy` and uses the deterministic path."""


@dataclass(frozen=True)
class TrustedContext:
    tenant_id: str
    request_id: str
    principal_id: str
    knowledge_scopes: Tuple[str, ...]
    mode: str
    egress: Any = None            # an egress decision, or None when no external model call is permitted


def tenant_config_dir() -> Path:
    return Path(os.getenv("AIOPS_TENANT_CONFIG_DIR", "tenant_config"))


def tenant_is_configured(tenant_id: str, config_dir: Optional[Path] = None) -> bool:
    """True when tenant_config has a file whose tenant_id matches (a file name alone is not enough)."""
    root = config_dir or tenant_config_dir()
    path = root / f"{tenant_id}.json"
    try:
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return str(data.get("tenant_id") or "").strip().lower() == tenant_id
    except (OSError, ValueError):
        return False


def build_context(
    tenant_id: Any,
    request_id: str,
    principal: Any,
    mode: str,
    *,
    egress: Any = None,
    config_dir: Optional[Path] = None,
) -> TrustedContext:
    tenant = str(tenant_id or "").strip().lower()
    if not tenant:
        raise ContextError("no tenant in the request context")
    if tenant == "default":
        raise ContextError("'default' is not a tenant a run may use")
    if not _TENANT_RE.match(tenant):
        raise ContextError("malformed tenant id")
    if not tenant_is_configured(tenant, config_dir):
        raise ContextError(f"unknown tenant '{tenant}'")
    if principal is None:
        raise ContextError("no authenticated principal")
    allows = getattr(principal, "allows_tenant", None)
    if not callable(allows) or not allows(tenant):
        raise ContextError(f"principal is not permitted to act for tenant '{tenant}'")
    return TrustedContext(
        tenant_id=tenant,
        request_id=str(request_id or ""),
        principal_id=str(getattr(principal, "id", "") or ""),
        knowledge_scopes=KNOWLEDGE_SCOPES,
        mode=str(mode),
        egress=egress,
    )
