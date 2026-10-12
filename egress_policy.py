"""
External-model egress policy (Release 1, W5).

Every call that sends ticket/incident/KB text to an external LLM provider must pass
`evaluate_external_llm()`. The decision is deny-by-default: a tenant must explicitly allow external
LLM use and allowlist the provider in its tenant config:

    "external_llm_policy": {
        "allow_external_llm": true,
        "allowed_llm_providers": ["groq"],
        "external_llm_redaction": "basic"        // "none" (default) | "basic"
    }

Local endpoints (localhost, loopback, private addresses, host.docker.internal) are not egress and are
always allowed. Every external decision, allowed or denied, is appended to an audit log.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

AUDIT_FILE = Path(os.getenv("AIOPS_DATA_DIR", "data")) / "egress_audit.jsonl"
_AUDIT_LOCK = threading.Lock()
_LOCAL_HOSTS = {"localhost", "host.docker.internal", "127.0.0.1", "::1"}


class EgressDenied(Exception):
    """Raised when an external model call is not permitted by tenant policy."""

    def __init__(self, decision: "EgressDecision"):
        super().__init__(decision.reason)
        self.decision = decision


@dataclass
class EgressDecision:
    allowed: bool
    tenant_id: str
    purpose: str
    provider: str
    local: bool
    reason: str
    redaction: str = "none"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def provider_for_url(base_url: str) -> tuple[str, bool]:
    """Return (provider, is_local) for an endpoint URL."""
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    if not host:
        return "unknown", False
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return "local", True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private:
            return "local", True
    except ValueError:
        pass
    if "groq.com" in host:
        return "groq", False
    if "openai.com" in host:
        return "openai", False
    return host, False


def resolve_tenant(tenant_id: Optional[str] = None) -> str:
    """Explicit tenant first, then thread-local context, then the configured default tenant."""
    tid = str(tenant_id or "").strip().lower().replace(" ", "_")
    if tid and tid != "default":
        return tid
    try:
        from tenant_context import get_tenant
        ctx = get_tenant()
        if ctx and ctx != "default":
            return ctx
    except Exception:
        pass
    return os.getenv("AIOPS_DEFAULT_TENANT_ID", "client_a").strip().lower() or "client_a"


def _load_tenant_config(tenant_id: str) -> Dict[str, Any]:
    from agents.governance import load_tenant_config
    return load_tenant_config(tenant_id)


def evaluate_external_llm(
    tenant_id: Optional[str],
    purpose: str,
    base_url: str,
    *,
    config_loader: Optional[Callable[[str], Dict[str, Any]]] = None,
    audit: bool = True,
) -> EgressDecision:
    tenant = resolve_tenant(tenant_id)
    provider, is_local = provider_for_url(base_url)
    if is_local:
        return EgressDecision(True, tenant, purpose, provider, True, "local endpoint is not external egress")

    try:
        cfg = (config_loader or _load_tenant_config)(tenant) or {}
    except Exception:
        cfg = {}
    policy = cfg.get("external_llm_policy")
    policy = policy if isinstance(policy, dict) else {}
    redaction = str(policy.get("external_llm_redaction") or "none").lower()
    redaction = redaction if redaction in {"none", "basic"} else "basic"

    if not policy:
        decision = EgressDecision(False, tenant, purpose, provider, False,
                                  "no external_llm_policy for tenant (deny by default)", redaction)
    elif policy.get("allow_external_llm") is not True:
        decision = EgressDecision(False, tenant, purpose, provider, False,
                                  "tenant policy does not allow external LLM use", redaction)
    else:
        allowed = {str(p).strip().lower() for p in (policy.get("allowed_llm_providers") or [])}
        if provider.lower() not in allowed:
            decision = EgressDecision(False, tenant, purpose, provider, False,
                                      f"provider '{provider}' is not in the tenant allowlist", redaction)
        else:
            decision = EgressDecision(True, tenant, purpose, provider, False, "allowed by tenant policy", redaction)
    if audit:
        record_audit(decision)
    return decision


def require_external_llm(tenant_id: Optional[str], purpose: str, base_url: str, **kwargs: Any) -> EgressDecision:
    """Return the decision if allowed; raise EgressDenied otherwise."""
    decision = evaluate_external_llm(tenant_id, purpose, base_url, **kwargs)
    if not decision.allowed:
        raise EgressDenied(decision)
    return decision


def record_audit(decision: EgressDecision, prompt_chars: int = 0) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(), "prompt_chars": prompt_chars, **decision.to_dict()}
    try:
        with _AUDIT_LOCK:
            AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass  # auditing must never break the request path


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SECRET_KV = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b(\s*[:=]\s*)\S+")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9+/_-]{32,}\b")


def redact(text: str, mode: str = "none") -> str:
    """Basic redaction of obvious identifiers/secrets before text leaves the platform."""
    if mode != "basic" or not text:
        return text
    out = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    out = _EMAIL.sub("[REDACTED_EMAIL]", out)
    out = _IPV4.sub("[REDACTED_IP]", out)
    out = _LONG_TOKEN.sub("[REDACTED_TOKEN]", out)
    return out
