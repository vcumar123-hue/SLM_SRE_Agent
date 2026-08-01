"""
servicenow_cmdb.py — ServiceNow CMDB adapter with local cache fallback.

Reads CI records from ServiceNow Table API (cmdb_ci, cmdb_rel_ci tables).
Falls back to local JSON cache when ServiceNow is unreachable.

Credentials via environment variables:
  AIOPS_ADAPTER_ITSM_BASE_URL   e.g. https://myinstance.service-now.com
  AIOPS_ADAPTER_ITSM_USERNAME
  AIOPS_ADAPTER_ITSM_PASSWORD
  AIOPS_ADAPTER_ITSM_TOKEN      (alternative: Bearer token)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Root of the project — two levels up from this file (adapters/servicenow_cmdb.py)
ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "cmdb" / "ci_cache.json"

# ServiceNow CMDB CI fields to fetch
_CI_FIELDS = ",".join([
    "sys_id",
    "name",
    "short_description",
    "environment",
    "u_environment",
    "operational_status",
    "category",
    "subcategory",
    "assignment_group",
    "company",
    "location",
    "support_group",
    "u_redundancy_type",
    "u_business_criticality",
    "u_failover_target",
])

# Operational status mapping (ServiceNow numeric values → strings)
_OPERATIONAL_STATUS_MAP: dict[str, str] = {
    "1": "operational",
    "2": "non-operational",
    "3": "non-operational",
    "4": "non-operational",
    "6": "retired",
    "operational": "operational",
    "non-operational": "non-operational",
    "retired": "retired",
}

# Redundancy type normalisation
_REDUNDANCY_MAP: dict[str, str] = {
    "standalone": "standalone",
    "single": "standalone",
    "active-active": "active-active",
    "active_active": "active-active",
    "activeactive": "active-active",
    "active-passive": "active-passive",
    "active_passive": "active-passive",
    "activepassive": "active-passive",
    "clustered": "clustered",
    "cluster": "clustered",
}


def _display_value(field: Any) -> str:
    """Extract display_value from a ServiceNow field dict or return raw string."""
    if isinstance(field, dict):
        return field.get("display_value") or field.get("value") or ""
    return str(field) if field is not None else ""


class ServiceNowCMDBAdapter:
    """Adapter for ServiceNow CMDB with local JSON cache fallback."""

    def __init__(self) -> None:
        self._base_url = (os.getenv("AIOPS_ADAPTER_ITSM_BASE_URL") or "").rstrip("/")
        self._username = os.getenv("AIOPS_ADAPTER_ITSM_USERNAME", "")
        self._password = os.getenv("AIOPS_ADAPTER_ITSM_PASSWORD", "")
        self._token = os.getenv("AIOPS_ADAPTER_ITSM_TOKEN", "")
        self._cache_path = CACHE_PATH

        self._session = self._build_session()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True if the adapter has a base URL configured."""
        return bool(self._base_url)

    def get_ci(self, ci_name: str) -> dict | None:
        """
        Fetch a single CI by name from ServiceNow.

        Returns a normalised CI dict, or None if not found.
        Falls back to cache on connection / auth failures.
        """
        if not self.is_configured():
            logger.debug("ServiceNow not configured — reading from cache for %s", ci_name)
            return self._get_from_cache(ci_name)

        url = f"{self._base_url}/api/now/table/cmdb_ci"
        params = {
            "sysparm_query": f"name={ci_name}",
            "sysparm_limit": "1",
            "sysparm_fields": _CI_FIELDS,
            "sysparm_display_value": "all",
        }
        try:
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            records = resp.json().get("result", [])
            if not records:
                logger.info("CI '%s' not found in ServiceNow", ci_name)
                return None
            ci = self._normalise_ci(records[0])
            # Opportunistically update cache
            self.sync_to_cache([ci])
            return ci
        except (requests.RequestException, ValueError) as exc:
            logger.warning("ServiceNow request failed for CI '%s': %s — falling back to cache", ci_name, exc)
            return self._get_from_cache(ci_name)

    def search_cis(self, query: str, limit: int = 20) -> list[dict]:
        """
        Search CIs whose name LIKE query.

        Returns a list of normalised CI dicts.
        """
        if not self.is_configured():
            logger.debug("ServiceNow not configured — searching cache for '%s'", query)
            return self._search_cache(query, limit)

        url = f"{self._base_url}/api/now/table/cmdb_ci"
        params = {
            "sysparm_query": f"nameLIKE{query}",
            "sysparm_limit": str(limit),
            "sysparm_fields": _CI_FIELDS,
            "sysparm_display_value": "all",
        }
        try:
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            records = resp.json().get("result", [])
            cis = [self._normalise_ci(r) for r in records]
            if cis:
                self.sync_to_cache(cis)
            return cis
        except (requests.RequestException, ValueError) as exc:
            logger.warning("ServiceNow search failed for query '%s': %s — falling back to cache", query, exc)
            return self._search_cache(query, limit)

    def get_ci_relationships(self, sys_id: str) -> list[dict]:
        """
        Fetch all CMDB relationships where the CI is parent OR child.

        Returns a list of raw relationship dicts.
        """
        if not self.is_configured():
            logger.debug("ServiceNow not configured — cannot fetch relationships for sys_id=%s", sys_id)
            return []

        url = f"{self._base_url}/api/now/table/cmdb_rel_ci"
        params = {
            "sysparm_query": f"parent={sys_id}^ORchild={sys_id}",
            "sysparm_display_value": "all",
            "sysparm_limit": "500",
        }
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            records = resp.json().get("result", [])
            return [
                {
                    "sys_id": _display_value(r.get("sys_id")),
                    "parent": _display_value(r.get("parent")),
                    "parent_sys_id": r.get("parent", {}).get("value", "") if isinstance(r.get("parent"), dict) else "",
                    "child": _display_value(r.get("child")),
                    "child_sys_id": r.get("child", {}).get("value", "") if isinstance(r.get("child"), dict) else "",
                    "type": _display_value(r.get("type")),
                }
                for r in records
            ]
        except (requests.RequestException, ValueError) as exc:
            logger.warning("ServiceNow relationship fetch failed for sys_id=%s: %s", sys_id, exc)
            return []

    def sync_to_cache(self, cis: list[dict]) -> None:
        """
        Merge the provided CI list into the local JSON cache.

        The cache is keyed by ci_name.
        """
        cache = self.load_cache()
        for ci in cis:
            name = ci.get("ci_name") or ci.get("name")
            if name:
                cache[name] = ci

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._cache_path.open("w", encoding="utf-8") as fh:
                json.dump(cache, fh, indent=2, default=str)
            logger.debug("CMDB cache synced — %d total entries", len(cache))
        except OSError as exc:
            logger.error("Failed to write CMDB cache: %s", exc)

    def load_cache(self) -> dict:
        """Load and return the full cache dict {ci_name: ci_dict}."""
        if not self._cache_path.exists():
            return {}
        try:
            with self._cache_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read CMDB cache: %s", exc)
            return {}

    def get_with_fallback(self, ci_name: str) -> dict | None:
        """Try live ServiceNow first; fall back to cache on any failure."""
        result = self.get_ci(ci_name)
        if result is not None:
            return result
        cached = self._get_from_cache(ci_name)
        if cached:
            logger.info("Returning cached CI for '%s'", ci_name)
        return cached

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _normalise_ci(self, raw: dict) -> dict:
        """Map raw ServiceNow CI fields to the standard schema."""
        env_raw = (_display_value(raw.get("u_environment")) or _display_value(raw.get("environment"))).lower()
        environment = self._normalise_environment(env_raw)

        op_raw = _display_value(raw.get("operational_status")).lower()
        operational_status = _OPERATIONAL_STATUS_MAP.get(op_raw, "non-operational")

        red_raw = _display_value(raw.get("u_redundancy_type")).lower().strip()
        redundancy_type = _REDUNDANCY_MAP.get(red_raw, "standalone")

        crit_raw = _display_value(raw.get("u_business_criticality"))
        try:
            business_criticality = max(1, min(5, int(crit_raw)))
        except (ValueError, TypeError):
            business_criticality = 3  # default to medium

        ci = {
            "ci_name": _display_value(raw.get("name")),
            "sys_id": _display_value(raw.get("sys_id")),
            "display_name": _display_value(raw.get("name")),
            "short_description": _display_value(raw.get("short_description")),
            "environment": environment,
            "operational_status": operational_status,
            "category": _display_value(raw.get("category")),
            "subcategory": _display_value(raw.get("subcategory")),
            "assignment_group": _display_value(raw.get("assignment_group")),
            "company": _display_value(raw.get("company")),
            "location": _display_value(raw.get("location")),
            "support_group": _display_value(raw.get("support_group")),
            "redundancy_type": redundancy_type,
            "business_criticality": business_criticality,
            "failover_target": _display_value(raw.get("u_failover_target")),
            "source": "servicenow",
            "last_synced": datetime.now(timezone.utc).isoformat(),
        }
        ci["tier"] = self._derive_tier(ci)
        return ci

    @staticmethod
    def _normalise_environment(env_raw: str) -> str:
        """Map raw environment string to standard values."""
        if any(k in env_raw for k in ("prod", "production", "prd")):
            return "production"
        if any(k in env_raw for k in ("dr", "disaster", "standby", "failover")):
            return "dr-standby"
        if any(k in env_raw for k in ("dev", "test", "uat", "stage", "staging", "qa", "sandbox", "non")):
            return "non-production"
        if env_raw:
            return "non-production"
        return "unknown"

    @staticmethod
    def _derive_tier(ci: dict) -> str:
        """Derive tier label from environment + redundancy_type."""
        env = ci.get("environment", "unknown")
        red = ci.get("redundancy_type", "standalone")

        if env == "production":
            if red == "standalone":
                return "prod-standalone"
            if red == "active-passive":
                return "prod-redundant"
            if red in ("active-active", "clustered"):
                return "prod-ha"
            return "prod-standalone"
        if env == "non-production":
            return "non-prod"
        if env == "dr-standby":
            return "dr-standby"
        return "unknown"

    def _get_from_cache(self, ci_name: str) -> dict | None:
        """Retrieve a single CI from the local cache."""
        cache = self.load_cache()
        entry = cache.get(ci_name)
        if entry:
            entry = dict(entry)
            entry["source"] = "cache"
        return entry

    def _search_cache(self, query: str, limit: int) -> list[dict]:
        """Search cache for CIs whose name contains query (case-insensitive)."""
        cache = self.load_cache()
        q = query.lower()
        results = []
        for name, ci in cache.items():
            if q in name.lower():
                entry = dict(ci)
                entry["source"] = "cache"
                results.append(entry)
                if len(results) >= limit:
                    break
        return results

    def _build_session(self) -> requests.Session:
        """Build a requests Session with auth and retry logic."""
        session = requests.Session()

        if self._token:
            session.headers.update({"Authorization": f"Bearer {self._token}"})
        elif self._username and self._password:
            session.auth = (self._username, self._password)

        session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------

_adapter_instance: ServiceNowCMDBAdapter | None = None


def get_adapter() -> ServiceNowCMDBAdapter:
    """Return the module-level singleton ServiceNowCMDBAdapter."""
    global _adapter_instance
    if _adapter_instance is None:
        _adapter_instance = ServiceNowCMDBAdapter()
    return _adapter_instance
