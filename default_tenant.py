"""
The one place the default tenant is decided.

A request that names no tenant is handled as this tenant. Several modules used to read AIOPS_DEFAULT_TENANT_ID with
different fallbacks (client_a in some, client_c in others), so the same untagged request could be queued, gated and
egress-checked under different tenants. Every module now takes its default from here.
"""
import os

FALLBACK_TENANT_ID = "client_c"  # PYPL


def default_tenant_id() -> str:
    """AIOPS_DEFAULT_TENANT_ID when set and non-empty, otherwise client_c."""
    value = (os.getenv("AIOPS_DEFAULT_TENANT_ID") or "").strip().lower()
    return value or FALLBACK_TENANT_ID
