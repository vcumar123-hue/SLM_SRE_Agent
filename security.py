"""
Authentication, principals and route-level role enforcement (Release 1, W2).

Identity comes only from a credential (bearer token -> principal). The caller-supplied
X-User-Role header is ignored. Execution-critical routes also re-check the principal
inside the route, so this middleware is not the only line of defence.

Configuration (environment):
  AIOPS_PRINCIPALS       JSON object: {"<token>": {"id": "...", "role": "l1|l2|l3|admin",
                         "kind": "human|service", "tenants": ["client_a"]}}
                         (service identities use a normal role, usually l2, plus kind "service")
  AIOPS_PRINCIPALS_FILE  path to a JSON file with the same shape (used if AIOPS_PRINCIPALS is unset)
  AIOPS_API_KEY          legacy single shared key -> principal "shared-key", role l2, kind human
  AIOPS_ENV              "production" refuses to start without any configured principal;
                         anything else is dev mode (open, principal "dev-open" with role l3)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("aiops.security")

ROLE_RANK = {"l1": 1, "l2": 2, "l3": 3, "admin": 4}
AUTH_EXEMPT_PATHS = {"/health", "/startup", "/metrics", "/docs", "/openapi.json", "/redoc"}

# (methods or None for all, path prefix, minimum role). All matching rules must pass.
ROUTE_MIN_ROLE: Tuple[Tuple[Optional[Tuple[str, ...]], str, str], ...] = (
    (None, "/users", "admin"),
    (None, "/connectors/fetch", "admin"),
    (None, "/knowledge_candidates", "l3"),
    (None, "/playbooks", "l3"),
    (None, "/resolutions", "l3"),
    (None, "/automation/approve", "l3"),
    (None, "/automation/reject", "l3"),
    (("POST",), "/automation/rollback", "l3"),
    (("POST",), "/automation/autonomous/enable", "l3"),
    (("POST",), "/automation/candidates/approve", "l3"),
    (("POST",), "/automation/candidates/enable", "l3"),
    (("POST",), "/automation/candidates/save_playbook", "l3"),
    (("POST",), "/automation/queue", "l2"),
    (("POST",), "/automation/refresh_queue", "l2"),
)


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    kind: str = "human"            # human | service
    tenants: Tuple[str, ...] = ()  # empty = not tenant-restricted

    @property
    def rank(self) -> int:
        return ROLE_RANK.get(self.role, -1)

    def has_role(self, minimum: str) -> bool:
        return self.rank >= ROLE_RANK.get(minimum, 99)

    def allows_tenant(self, tenant_id: str) -> bool:
        return not self.tenants or str(tenant_id or "") in self.tenants


DEV_PRINCIPAL = Principal(id="dev-open", role="l3", kind="human")

_principals_cache: Optional[Dict[str, Principal]] = None


def _parse_principals(raw: Any) -> Dict[str, Principal]:
    out: Dict[str, Principal] = {}
    if not isinstance(raw, dict):
        return out
    for token, spec in raw.items():
        if not isinstance(spec, dict) or not str(token).strip():
            continue
        role = str(spec.get("role", "l1")).lower()
        kind = str(spec.get("kind", "human")).lower()
        if role not in ROLE_RANK or kind not in {"human", "service"}:
            logger.warning("security: ignoring principal %r with invalid role/kind", spec.get("id"))
            continue
        out[str(token).strip()] = Principal(
            id=str(spec.get("id") or "unnamed"),
            role=role,
            kind=kind,
            tenants=tuple(str(t) for t in (spec.get("tenants") or [])),
        )
    return out


def load_principals(env: Optional[Dict[str, str]] = None) -> Dict[str, Principal]:
    env = os.environ if env is None else env
    principals: Dict[str, Principal] = {}
    raw = (env.get("AIOPS_PRINCIPALS") or "").strip()
    if not raw and (env.get("AIOPS_PRINCIPALS_FILE") or "").strip():
        try:
            with open(env["AIOPS_PRINCIPALS_FILE"].strip(), "r", encoding="utf-8") as fh:
                raw = fh.read()
        except Exception as exc:
            logger.error("security: cannot read AIOPS_PRINCIPALS_FILE: %s", exc)
    if raw:
        try:
            principals.update(_parse_principals(json.loads(raw)))
        except Exception as exc:
            logger.error("security: invalid principals JSON: %s", exc)
    legacy = (env.get("AIOPS_API_KEY") or "").strip()
    if legacy and legacy not in principals:
        principals[legacy] = Principal(id="shared-key", role="l2", kind="human")
    return principals


def get_principals() -> Dict[str, Principal]:
    global _principals_cache
    if _principals_cache is None:
        _principals_cache = load_principals()
    return _principals_cache


def reset_principals_cache() -> None:
    global _principals_cache
    _principals_cache = None


def is_production(env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("AIOPS_ENV") or "").strip().lower() == "production"


def auth_mode() -> str:
    """'keys' when principals are configured; 'dev' (open) otherwise; 'misconfigured' in production without keys."""
    if get_principals():
        return "keys"
    return "misconfigured" if is_production() else "dev"


def authenticate_headers(headers: Any) -> Optional[Principal]:
    """Resolve the principal for a request, or None if unauthenticated."""
    mode = auth_mode()
    if mode == "dev":
        return DEV_PRINCIPAL
    if mode != "keys":
        return None
    auth = headers.get("Authorization", "") or headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    presented = auth[7:].strip()
    match: Optional[Principal] = None
    for token, principal in get_principals().items():  # constant-time over all tokens
        if hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
            match = principal
    return match


def required_role_for(method: str, path: str) -> Optional[str]:
    best: Optional[str] = None
    for methods, prefix, role in ROUTE_MIN_ROLE:
        if methods is not None and method.upper() not in methods:
            continue
        if path.startswith(prefix) and (best is None or ROLE_RANK[role] > ROLE_RANK[best]):
            best = role
    return best


def principal_from_request(request: Any) -> Optional[Principal]:
    return getattr(getattr(request, "state", None), "principal", None)


def install_security(app: Any) -> None:
    """Attach the authentication/role middleware. Refuses to start misconfigured in production."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    if auth_mode() == "misconfigured":
        raise RuntimeError(
            "AIOPS_ENV=production requires AIOPS_PRINCIPALS, AIOPS_PRINCIPALS_FILE or AIOPS_API_KEY."
        )
    if auth_mode() == "dev":
        logger.warning("security: no principals configured - API is OPEN (dev mode, principal 'dev-open', role l3)")

    class _SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[override]
            path = request.url.path
            if path in AUTH_EXEMPT_PATHS:
                return await call_next(request)
            principal = authenticate_headers(request.headers)
            if principal is None:
                return JSONResponse(
                    {"error": "unauthorized", "message": "Valid Bearer token required."}, status_code=401
                )
            needed = required_role_for(request.method, path)
            if needed and not principal.has_role(needed):
                return JSONResponse(
                    {"error": "forbidden", "message": f"Role {needed} or above required."}, status_code=403
                )
            request.state.principal = principal
            return await call_next(request)

    app.add_middleware(_SecurityMiddleware)
