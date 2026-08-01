"""
agents/helpers.py — shared helper functions used by multiple agent routers.
No FastAPI or app-level imports here to avoid circular dependencies.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional


def _safe_limit(limit: int = 100, maximum: int = 1000) -> int:
    try:
        value = int(limit)
    except Exception:
        value = 100
    return max(1, min(value, maximum))


def _normalize_api_row(row: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in dict(row or {}).items():
        if hasattr(value, "isoformat"):
            clean[key] = value.isoformat()
        else:
            clean[key] = value
    return clean


def _automation_classification_key(value: str) -> str:
    """Normalize UI display labels into executable automation mapping keys."""
    token = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    aliases = {
        "vpn_client_failure": "vpn_client_failure",
        "vpn_client_failures": "vpn_client_failure",
        "vpn_failure": "vpn_client_failure",
        "vpn_connectivity_failure": "vpn_client_failure",
        "vpn_connection_failure": "vpn_client_failure",
        "vpn_user_connectivity_failure": "vpn_client_failure",
    }
    return aliases.get(token, token)
