from __future__ import annotations
from typing import Optional
import streamlit as st

ROLE_RANK = {"l1": 1, "l2": 2, "l3": 3, "admin": 4}

def current_role() -> str:
    return str(st.session_state.get("user_role") or "l1").lower()

def has_role(minimum: str) -> bool:
    return ROLE_RANK.get(current_role(), 0) >= ROLE_RANK.get(minimum.lower(), 99)

def require_role(minimum: str, message: str = "") -> bool:
    if has_role(minimum):
        return True
    label = {"l1":"L1 Engineer","l2":"L2 Engineer","l3":"L3 Senior Engineer","admin":"Admin"}.get(minimum, minimum.upper())
    st.warning(message or f"🔒 This action requires {label} access or higher.")
    return False

# Capability gates
def can_approve_candidates() -> bool: return has_role("l2")
def can_save_resolution() -> bool: return has_role("l2")
def can_save_playbook() -> bool: return has_role("l2")
def can_promote_to_global() -> bool: return has_role("l3")
def can_manage_users() -> bool: return has_role("admin")
def can_view_backtesting() -> bool: return has_role("l2")
def can_trigger_automation() -> bool: return has_role("l3")
def can_view_all_tenants() -> bool: return has_role("admin")
