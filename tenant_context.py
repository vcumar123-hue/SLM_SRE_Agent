"""
tenant_context.py — Item #19
Hard tenant isolation: thread-local tenant context + per-tenant data path helpers.

Usage:
    from tenant_context import set_tenant, get_tenant, tenant_data_dir

    set_tenant("client_c")
    dir = tenant_data_dir()          # data/tenants/client_c/
    path = tenant_data_file("resolutions.json")  # data/tenants/client_c/resolutions.json
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

_local = threading.local()

# Default tenant used when no context is set (single-tenant / dev mode)
_DEFAULT_TENANT = "default"


# ── Thread-local tenant context ────────────────────────────────────────────────

def set_tenant(tenant_id: str) -> None:
    _local.tenant_id = str(tenant_id).strip().lower().replace(" ", "_") or _DEFAULT_TENANT


def get_tenant() -> str:
    return getattr(_local, "tenant_id", _DEFAULT_TENANT)


def clear_tenant() -> None:
    _local.tenant_id = _DEFAULT_TENANT


# ── Path helpers ───────────────────────────────────────────────────────────────

def tenant_data_dir(tenant_id: Optional[str] = None) -> Path:
    """Return the data directory for the given (or current) tenant.
    Creates the directory if it does not exist.
    """
    tid = tenant_id or get_tenant()
    if tid == _DEFAULT_TENANT:
        base = Path("data")  # backwards-compatible: single-tenant uses data/ directly
    else:
        base = Path("data") / "tenants" / tid
    base.mkdir(parents=True, exist_ok=True)
    return base


def tenant_data_file(filename: str, tenant_id: Optional[str] = None) -> Path:
    """Return a file path scoped to the current tenant's data directory."""
    return tenant_data_dir(tenant_id) / filename


# ── PostgreSQL row-level tenant isolation helpers ──────────────────────────────

def pg_tenant_clause(alias: str = "") -> str:
    """
    Return a SQL WHERE fragment for tenant filtering, e.g. 'AND tenant_id = %s'.
    Pass an alias like 'r' to get 'AND r.tenant_id = %s'.
    col = f"{alias}.tenant_id" if alias else "tenant_id"
    """
    col = f"{alias}.tenant_id" if alias else "tenant_id"
    return f"AND {col} = %s"


def pg_tenant_value() -> str:
    """Return the current tenant id for use as a query parameter."""
    return get_tenant()


def ensure_tenant_column(table: str) -> str:
    """Return SQL to add tenant_id column to an existing table (idempotent)."""
    return f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT '{_DEFAULT_TENANT}'"


# ── Context manager ────────────────────────────────────────────────────────────

class TenantContext:
    """
    Scoped tenant context for use in request handlers or tests.

        with TenantContext("client_c"):
            resolutions = get_resolutions()
    """
    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id
        self._previous: Optional[str] = None

    def __enter__(self) -> "TenantContext":
        self._previous = get_tenant()
        set_tenant(self._tenant_id)
        return self

    def __exit__(self, *_) -> None:
        if self._previous is not None:
            set_tenant(self._previous)
        else:
            clear_tenant()
