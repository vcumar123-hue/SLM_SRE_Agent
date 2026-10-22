"""
Run storage (design 11): sanitised, tenant-keyed, encrypted by default, expiring.

What is stored: the run summary (no ticket text, only a reference and a hash), each evidence item (sanitised, immutable, encrypted),
and each step as an event (encrypted). Raw tool output is never stored. Every function takes the tenant; there is no read that spans
tenants. Encryption follows owner decision 3: on unless waived, and persistence is refused if it is required and no key exists.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from agentruntime.config import clamp_retention_days
from agentruntime.contracts import AgentRunResult, AgentStep, EvidenceItem, Hypothesis
from agentruntime.crypto import Cipher, Waiver, get_cipher, load_waiver
from agentruntime.sanitize import sanitize_text

SCHEMA_SQL = """
ALTER TABLE IF EXISTS agent_traces ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE IF EXISTS agent_traces ADD COLUMN IF NOT EXISTS run_id TEXT;

CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT NOT NULL, tenant_id TEXT NOT NULL, request_id TEXT, mode TEXT NOT NULL,
  status TEXT NOT NULL, reason TEXT, ticket_ref TEXT, ticket_hash TEXT,
  counters JSONB, versions JSONB, policy_hash TEXT, duration_ms INTEGER,
  created_at TIMESTAMPTZ DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, run_id));
CREATE TABLE IF NOT EXISTS agent_evidence (
  tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
  item JSONB NOT NULL, content_hash TEXT NOT NULL,
  PRIMARY KEY (tenant_id, run_id, evidence_id),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, run_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS agent_run_events (
  tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, step INTEGER NOT NULL, kind TEXT NOT NULL,
  input_hash TEXT NOT NULL, evidence_refs TEXT[], output_json JSONB,
  model TEXT, prompt_version TEXT, tokens_in INTEGER, tokens_out INTEGER, latency_ms INTEGER,
  PRIMARY KEY (tenant_id, run_id, step, kind),
  FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs (tenant_id, run_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_agent_runs_expiry ON agent_runs (expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_request ON agent_runs (tenant_id, request_id);
"""

SUMMARY_STEP = -1    # the event that carries hypotheses, analysis and flags

# Bounds for the operator-visible summary (`summarize_run`): a sanitised, size-capped view, never the raw record.
SUMMARY_MAX_STEPS = 20
SUMMARY_MAX_HYPOTHESES = 5
SUMMARY_MAX_FLAGS = 10
SUMMARY_MAX_REASON_CHARS = 300
SUMMARY_MAX_CAUSE_CHARS = 200
SUMMARY_COUNTER_KEYS = ("steps", "tool_calls", "model_calls", "tokens", "no_progress", "invalid_actions", "tool_failures", "skipped_duplicates", "elapsed_s")


class StoreError(Exception):
    pass


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RunStore:
    def __init__(self, connect: Optional[Callable[[], Any]] = None, cipher: Optional[Cipher] = None,
                 waiver_loader: Optional[Callable[[], Optional[Waiver]]] = None):
        """Production code passes nothing: encryption is on, or a recorded waiver file permits plaintext. `waiver_loader` lets a test point
        at a waiver file of its own; whatever it returns must still be `verified` (loaded from a record), never a hand-built object."""
        if connect is None:
            import postgres_store
            connect = postgres_store.get_connection
        self._connect = connect
        self._cipher: Optional[Cipher] = None
        self.waiver: Optional[Waiver] = None
        if cipher is not None:
            self._cipher = cipher
        else:
            loader = waiver_loader or load_waiver
            self._cipher = get_cipher(loader)       # raises KeyMissing when encryption is required and no key exists
            if self._cipher is None:                # only a recorded waiver gets here
                self.waiver = loader()
                if self.waiver is None or not self.waiver.verified:
                    raise StoreError("plaintext storage needs a waiver loaded from the recorded waiver file; refusing to store run data")
        self._encrypt = self._cipher is not None

    def encryption_record(self) -> Dict[str, Any]:
        if self._cipher is not None:
            return {"mode": "aes-256-gcm", "key_id": self._cipher.key_id}
        return {"mode": "waived", **(self.waiver.record() if self.waiver else {})}

    # -- encoding of sensitive JSON ---------------------------------------------------------------------------------------

    def _pack(self, obj: Any, tenant: str, run: str, field: str) -> str:
        text = json.dumps(obj, default=str, sort_keys=True)
        if self._encrypt:
            return json.dumps({"enc": self._cipher.encrypt(text, tenant, run, field)})   # type: ignore[union-attr]
        return json.dumps({"plain": obj}, default=str)

    def _unpack(self, stored: Any, tenant: str, run: str, field: str) -> Any:
        data = stored if isinstance(stored, dict) else json.loads(stored)
        if "enc" in data:
            if self._cipher is None:
                raise StoreError("the data is encrypted but no key is available")
            return json.loads(self._cipher.decrypt(data["enc"], tenant, run, field))
        return data["plain"]

    # -- schema ------------------------------------------------------------------------------------------------------------

    def ensure_schema(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)

    # -- write -------------------------------------------------------------------------------------------------------------

    def save_run(self, result: AgentRunResult, *, ticket_ref: str = "", ticket_text: str = "", request_id: str = "",
                 retention_days: int = 90, duration_ms: int = 0) -> None:
        tenant, run = result.tenant_id, result.run_id
        if not tenant:
            raise StoreError("a run needs a tenant")
        for item in result.evidence:       # defence in depth: nothing from another tenant is ever persisted under this run
            if item.tenant_id != tenant or item.run_id != run:
                raise StoreError(f"evidence {item.evidence_id} does not belong to tenant '{tenant}' run '{run}'")
        retention_days = clamp_retention_days(retention_days)   # never trust the caller's value
        versions = {**result.versions, "encryption": self.encryption_record()}
        expires = datetime.now(timezone.utc) + timedelta(days=retention_days)
        reason = sanitize_text(result.reason, 200)[0]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_runs (run_id, tenant_id, request_id, mode, status, reason, ticket_ref, ticket_hash, counters,"
                " versions, policy_hash, duration_ms, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
                (run, tenant, request_id, result.mode, result.status, reason, ticket_ref, _hash(ticket_text) if ticket_text else "",
                 json.dumps(result.counters, default=str), json.dumps(versions, default=str),
                 str(result.versions.get("policy_hash", "")), duration_ms or int(float(result.counters.get("elapsed_s", 0)) * 1000),
                 expires))
            for item in result.evidence:
                cur.execute(
                    "INSERT INTO agent_evidence (tenant_id, run_id, evidence_id, item, content_hash) VALUES (%s,%s,%s,%s::jsonb,%s)",
                    (tenant, run, item.evidence_id,
                     self._pack(item.model_dump(mode="json"), tenant, run, f"agent_evidence.item:{item.evidence_id}"),
                     item.content_hash))
            summary = {"hypotheses": [h.model_dump(mode="json") for h in result.hypotheses], "analysis": result.analysis,
                       "flags": result.flags, "evidence_grounding": result.evidence_grounding, "reason": reason}
            cur.execute(
                "INSERT INTO agent_run_events (tenant_id, run_id, step, kind, input_hash, evidence_refs, output_json)"
                " VALUES (%s,%s,%s,'model',%s,%s,%s::jsonb)",
                (tenant, run, SUMMARY_STEP, _hash("summary"), [],
                 self._pack(summary, tenant, run, f"agent_run_events.output_json:{SUMMARY_STEP}:model")))
            for step in result.steps:
                cur.execute(
                    "INSERT INTO agent_run_events (tenant_id, run_id, step, kind, input_hash, evidence_refs, output_json, model,"
                    " prompt_version, tokens_in, tokens_out, latency_ms) VALUES (%s,%s,%s,'model',%s,%s,%s::jsonb,%s,%s,%s,%s,%s)",
                    (tenant, run, step.n, _hash(f"plan:{step.n}"), step.evidence_added,
                     self._pack(step.model_dump(mode="json"), tenant, run, f"agent_run_events.output_json:{step.n}:model"),
                     step.model, str(result.versions.get("prompt", "")), step.tokens_in, step.tokens_out, step.duration_ms))
                if step.kind == "tool" and not step.skipped:
                    cur.execute(
                        "INSERT INTO agent_run_events (tenant_id, run_id, step, kind, input_hash, evidence_refs, output_json, latency_ms)"
                        " VALUES (%s,%s,%s,'tool',%s,%s,%s::jsonb,%s)",
                        (tenant, run, step.n, _hash(json.dumps(step.tool_input, sort_keys=True, default=str)), step.evidence_added,
                         self._pack({"tool": step.tool, "evidence": step.evidence_added, "error": step.error}, tenant, run,
                                    f"agent_run_events.output_json:{step.n}:tool"), step.duration_ms))

    # -- read --------------------------------------------------------------------------------------------------------------

    def load_run(self, tenant_id: str, run_id: str) -> Optional[AgentRunResult]:
        if not tenant_id:
            raise StoreError("a tenant is required to read a run")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, reason, mode, counters, versions FROM agent_runs WHERE tenant_id = %s AND run_id = %s",
                        (tenant_id, run_id))
            row = cur.fetchone()
            if row is None:
                return None
            status, reason, mode, counters, versions = row
            cur.execute("SELECT evidence_id, item FROM agent_evidence WHERE tenant_id = %s AND run_id = %s", (tenant_id, run_id))
            evidence = [EvidenceItem(**self._unpack(item, tenant_id, run_id, f"agent_evidence.item:{eid}"))
                        for eid, item in cur.fetchall()]
            cur.execute("SELECT step, output_json FROM agent_run_events WHERE tenant_id = %s AND run_id = %s AND kind = 'model'"
                        " ORDER BY step", (tenant_id, run_id))
            steps: List[AgentStep] = []
            summary: Dict[str, Any] = {}
            for step_no, stored in cur.fetchall():
                data = self._unpack(stored, tenant_id, run_id, f"agent_run_events.output_json:{step_no}:model")
                if step_no == SUMMARY_STEP:
                    summary = data
                else:
                    steps.append(AgentStep(**data))
        evidence.sort(key=lambda e: int(e.evidence_id[1:]))
        return AgentRunResult(
            run_id=run_id, tenant_id=tenant_id, status=status, reason=summary.get("reason", reason or ""),
            hypotheses=[Hypothesis(**h) for h in summary.get("hypotheses", [])], evidence=evidence, steps=steps,
            analysis=summary.get("analysis"), evidence_grounding=summary.get("evidence_grounding"),
            counters=counters or {}, versions=versions or {}, mode=mode, flags=summary.get("flags", []))

    def load_run_by_request(self, tenant_id: str, request_id: str) -> Optional[AgentRunResult]:
        """The newest run of THIS tenant's stored under this analysis `request_id` (the value `agentruntime.integration.hook` persists
        alongside every run it stores), or None when none is stored yet — a background shadow run may still be queued or running, or
        none may ever have been queued. Tenant-scoped like every read here: a request id from another tenant can never match, because
        the WHERE clause binds both tenant_id and request_id as parameters."""
        if not tenant_id or not request_id:
            raise StoreError("a tenant and a request id are required to look up a run")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT run_id FROM agent_runs WHERE tenant_id = %s AND request_id = %s ORDER BY created_at DESC LIMIT 1",
                        (tenant_id, request_id))
            row = cur.fetchone()
        if row is None:
            return None
        return self.load_run(tenant_id, row[0])

    # -- retention and deletion ---------------------------------------------------------------------------------------------

    def purge_expired(self, now: Optional[datetime] = None) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agent_runs WHERE expires_at < %s", (now or datetime.now(timezone.utc),))
            return cur.rowcount

    def delete_tenant(self, tenant_id: str) -> int:
        if not tenant_id:
            raise StoreError("a tenant is required")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agent_runs WHERE tenant_id = %s", (tenant_id,))
            return cur.rowcount

    def count_runs(self, tenant_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM agent_runs WHERE tenant_id = %s", (tenant_id,))
            return int(cur.fetchone()[0])


# -- operator-visible summary ------------------------------------------------------------------------------------------------

def summarize_run(run: AgentRunResult) -> Dict[str, Any]:
    """A bounded, sanitised view of a decrypted `AgentRunResult`, for the Incident Console (design: operator-visible shadow inspection).

    Pure (no I/O): decryption already happened inside `RunStore.load_run`/`load_run_by_request`; this only shapes what is already in
    memory. It never returns encryption metadata, ticket text, tool inputs, raw tool output, or an unbounded amount of evidence: steps,
    hypotheses and flags are each capped in count, and every free-text field is capped in length. The caller decides what else (state,
    response_applied, response_impact) to add; this function speaks only to what the stored run itself says.
    """
    from agentruntime.release import RELEASE_PLANNER            # every run the integration stores was built by this planner (lazy import: no cycle, avoids importing release at module load)

    counters_in = run.counters or {}
    counters = {k: counters_in[k] for k in SUMMARY_COUNTER_KEYS if k in counters_in}
    duration_ms = int(round(float(counters_in.get("elapsed_s") or 0.0) * 1000))

    tools = []
    for step in run.steps:
        if step.kind != "tool" or step.skipped:
            continue
        tools.append({"step": step.n, "name": str(step.tool or ""), "ok": step.error is None, "evidence_added": len(step.evidence_added or []),
                      "duration_ms": int(step.duration_ms or 0)})
        if len(tools) >= SUMMARY_MAX_STEPS:
            break

    hypotheses = []
    for h in run.hypotheses[:SUMMARY_MAX_HYPOTHESES]:
        hypotheses.append({"cause": sanitize_text(h.cause, SUMMARY_MAX_CAUSE_CHARS)[0], "status": h.status, "verified": bool(h.verified),
                           "evidence_count": len(h.evidence or [])})

    versions = run.versions or {}
    return {
        "run_id": run.run_id, "mode": run.mode, "status": run.status, "reason": sanitize_text(run.reason, SUMMARY_MAX_REASON_CHARS)[0],
        "duration_ms": duration_ms, "planner": RELEASE_PLANNER, "ruleset": str(versions.get("ruleset") or ""), "model": versions.get("model"),
        "tool_versions": dict(versions.get("tools") or {}), "counters": counters, "hypotheses": hypotheses, "tools": tools,
        "tool_count": len(tools), "evidence_count": len(run.evidence), "flags": [sanitize_text(f, 80)[0] for f in (run.flags or [])][:SUMMARY_MAX_FLAGS],
    }
