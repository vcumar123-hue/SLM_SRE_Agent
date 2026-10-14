from __future__ import annotations
from default_tenant import default_tenant_id
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import APIRouter

router = APIRouter(tags=["governance"])

DEFAULT_TENANT_ID = default_tenant_id()
TENANT_CONFIG_DIR = Path(os.getenv("AIOPS_TENANT_CONFIG_DIR", "tenant_config"))


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def load_tenant_config(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    tenant = clean_text(tenant_id or DEFAULT_TENANT_ID).lower().replace(" ", "_") or DEFAULT_TENANT_ID
    path = TENANT_CONFIG_DIR / f"{tenant}.json"
    default_cfg = {
        "tenant_id": tenant,
        "display_name": tenant.upper() if tenant else "CLIENT",
        "knowledge_scope_default": "client_private",
        "collections": {
            "client_private": f"{tenant}_index",
            "global_learned": "global_learned_patterns_index",
            "global_base": "global_base_index",
        },
        "branding": {
            "client_label": tenant.upper() if tenant else "CLIENT",
            "product_label": os.getenv("APP_NAME", "AIOps Copilot"),
        },
        "fallback_policy": {
            "enabled": True,
            "allow_force_fallback_for_engineers": True,
            "user_visible_force_fallback": False,
        },
        "retrieval_policy": {
            "default_search_order": [
                f"{tenant}_index",
                "global_learned_patterns_index",
                "global_base_index",
            ],
            "tenant_match_priority": "high",
            "global_learned_priority": "medium_high",
            "global_base_priority": "medium",
        },
    }
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = default_cfg.copy()
                merged.update(data)
                # shallow-merge nested dicts
                for key in ["collections", "branding", "fallback_policy", "retrieval_policy"]:
                    base_val = dict(default_cfg.get(key, {}))
                    base_val.update(data.get(key, {}) if isinstance(data.get(key), dict) else {})
                    merged[key] = base_val
                return merged
    except Exception:
        pass
    return default_cfg


def check_export_governance(tenant_id: Optional[str] = None, operation: str = "export") -> Dict[str, Any]:
    """MF-08: Cross-tenant export enforcement gate.

    Returns {"allowed": True} or {"allowed": False, "reason": "...", "blocked_by": "..."}.
    Call this before any operation that writes client data to global/cross-tenant scope.
    """
    cfg = load_tenant_config(tenant_id)
    governance = cfg.get("knowledge_governance") or {}

    if operation in {"export", "data_export"}:
        if not governance.get("allow_client_data_export", False):
            return {
                "allowed": False,
                "reason": "Client data export is disabled by tenant governance policy.",
                "blocked_by": "allow_client_data_export=false",
                "governance": governance,
            }

    if operation in {"sanitized_export", "pattern_export"}:
        if not governance.get("allow_sanitized_pattern_export", False):
            return {
                "allowed": False,
                "reason": "Sanitized pattern export is disabled by tenant governance policy.",
                "blocked_by": "allow_sanitized_pattern_export=false",
                "governance": governance,
            }

    if operation in {"cross_client", "cross_client_contribution"}:
        if not governance.get("allow_cross_client_learning_contribution", False):
            return {
                "allowed": False,
                "reason": "Cross-client learning contribution is disabled by tenant governance policy.",
                "blocked_by": "allow_cross_client_learning_contribution=false",
                "governance": governance,
            }

    if operation in {"promote", "global_promote"}:
        ext_contrib = str(governance.get("external_knowledge_contribution") or "disabled").lower()
        if ext_contrib == "disabled":
            return {
                "allowed": False,
                "reason": "External knowledge contribution is disabled by tenant governance policy.",
                "blocked_by": "external_knowledge_contribution=disabled",
                "governance": governance,
            }

    return {"allowed": True, "governance": governance}


@router.get("/governance/check")
def governance_check_route(tenant_id: Optional[str] = None, operation: str = "export") -> Dict[str, Any]:
    """MF-08: Check governance policy for a given tenant and operation."""
    tenant = clean_text(tenant_id or DEFAULT_TENANT_ID)
    gate = check_export_governance(tenant, operation=operation)
    return {
        "status": "ok",
        "tenant_id": tenant,
        "operation": operation,
        "allowed": gate.get("allowed"),
        "blocked_by": gate.get("blocked_by"),
        "reason": gate.get("reason"),
        "governance": gate.get("governance", {}),
    }
