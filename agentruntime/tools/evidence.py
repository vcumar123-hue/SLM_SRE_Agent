"""
evidence_lookup: Safety Gate evidence recorded for THIS tenant (design 4.3).

Finding (R2-1): the platform's Safety Gate evidence store (`data/evidence/*.json`) is **not partitioned by tenant**: one file per check,
shared by every tenant. Reading it for a run would show one client's evidence to another, so this tool never reads it. It reads only a
tenant partition, `<evidence_store>/tenants/<tenant_id>/<check>.json`, which does not exist yet. Until a tenant has partitioned evidence the
tool returns an explicit "no tenant-scoped evidence" item for the check. Partitioning the gate's own store is a separate task for the owner.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agentruntime.context import TrustedContext
from agentruntime.contracts import EvidenceDraft, ToolSpec
from agentruntime.executor import CancelToken
from agentruntime.registry import BoundaryViolation
from agentruntime.tools._common import check_cancel, clip, parse_timestamp

NAME = "evidence_lookup"
VERSION = "1"
MAX_FILE_BYTES = 64 * 1024
CHECKS = ("infrastructure_drift", "error_budget_slo", "concurrent_activity", "change_window", "rollback_readiness", "blast_radius")

SPEC = ToolSpec(
    name=NAME, version=VERSION, max_items=len(CHECKS),
    description="Safety Gate evidence recorded for this client (for example change window, rollback readiness), with its status and age. Omit 'check' to list all.",
    input_schema={
        "type": "object", "additionalProperties": False,
        "properties": {"check": {"type": "string", "enum": list(CHECKS)}},
    },
)


def evidence_root() -> Path:
    return Path(os.getenv("AIOPS_EVIDENCE_STORE_DIR", "data/evidence"))


def make_tool(root: Optional[Callable[[], Path]] = None) -> Callable[..., List[EvidenceDraft]]:
    def evidence_lookup(ctx: TrustedContext, check: Optional[str] = None, *, cancel: Optional[CancelToken] = None,
                        timeout_s: Optional[float] = None) -> List[EvidenceDraft]:
        check_cancel(cancel)
        base = (root or evidence_root)() / "tenants" / ctx.tenant_id
        drafts: List[EvidenceDraft] = []
        for name in ([check] if check else list(CHECKS)):
            check_cancel(cancel)
            path = base / f"{name}.json"
            try:
                resolved = path.resolve()
                if base.resolve() not in resolved.parents:                  # a symlink or odd name must not leave the tenant folder
                    raise BoundaryViolation("evidence path left the tenant folder")
                if not resolved.is_file() or resolved.stat().st_size > MAX_FILE_BYTES:
                    raise FileNotFoundError(name)
                data: Dict[str, Any] = json.loads(resolved.read_text(encoding="utf-8-sig"))
            except BoundaryViolation:
                raise
            except (OSError, ValueError):
                drafts.append(EvidenceDraft(
                    tenant_id=ctx.tenant_id, source_type="evidence_check", source_id=name, provenance="computed", ttl_days=30,
                    excerpt=f"No tenant-scoped evidence is recorded for {name}.", facts={"status": "none"}))
                continue
            stamp = data.get("checked_at") or data.get("timestamp")
            drafts.append(EvidenceDraft(
                tenant_id=ctx.tenant_id, source_type="evidence_check", source_id=name, provenance="tenant_data",
                data_timestamp=parse_timestamp(stamp), ttl_days=int(data.get("ttl_days") or 30),
                excerpt=clip(f"{name}: {data.get('status', 'unknown')}. {data.get('summary', '')} (source {data.get('source_system', 'unknown')})"),
                facts={"status": clip(data.get("status"), 20), "severity": clip(data.get("severity"), 20),
                       "source_system": clip(data.get("source_system"), 40), "checked_at": clip(stamp, 40)}))
        return drafts

    return evidence_lookup
