"""
The controlled shadow test (Release 2, R2-2). Run it in the container, where the database, retrieval and embedding model are reachable:

    python scripts/controlled_shadow_test.py [--tenant client_c] [--ticket "..."]

It uses the REAL confirmed release freeze and the REAL recorded deployment decision (nothing is patched), the real /analyze route of the application and the real
tools. The only thing redirected is where the run record is written: to the throwaway PostgreSQL in PG_TEST_HOST/PORT/USER/PASSWORD/DB (the password has no default and must be supplied), so
that no new table appears in the main database. It checks, on one request in shadow and one with the loop off:

  1. the response in shadow equals the response with the loop off (volatile ids aside) and has no `investigation`;
  2. the run was queued only after the response body had been sent;
  3. the run completed and was stored (mode shadow), encrypted at rest, decryptable for its tenant and not another;
  4. after the rollback switch a further request queues and stores nothing.

Exit code 0 only if every check holds. Nothing is enabled by running it.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

VOLATILE = {"request_id", "trace_id", "timestamp", "created_at", "updated_at", "generated_at", "latency_ms", "duration_ms", "elapsed_s", "retrieved_at"}


def _required(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise SystemExit(f"{name} must be set in the environment (the throwaway PostgreSQL's password is never kept in the repository)")
    return value


def strip(value):
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="client_c")
    parser.add_argument("--ticket", default="gcncu-asprasu10-File System Full")
    args = parser.parse_args()

    from agentruntime import config as runtime_config
    from agentruntime import crypto, release
    from agentruntime.integration import gate, hook
    from agentruntime.integration.shadow import ShadowQueue
    import agentruntime.store as store_module

    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""), flush=True)

    # -- the real gate, unpatched ------------------------------------------------------------------------------------------------
    os.environ["AIOPS_AGENT_DATA_KEY"] = crypto.generate_key()          # a throwaway key for this test's run records
    os.environ.pop(gate.KILL_ENV, None)
    os.environ.pop(gate.ORCHESTRATOR_ENV, None)
    os.environ[runtime_config.MODE_ENV] = "shadow"
    gate.reset_cache()
    mode, why = gate.effective_mode(args.tenant)
    check("the real gate resolves the tenant to shadow (confirmed freeze, recorded decision, nothing patched)", mode == "shadow", f"{mode}: {why}")
    check("the release freeze has no problems", release.problems() == [], "; ".join(release.problems()))
    other_mode, _ = gate.effective_mode("poc_client")
    check("another tenant with no recorded decision stays off", other_mode == "off")
    if mode != "shadow":
        return 1

    # -- run records go to the throwaway PostgreSQL only ------------------------------------------------------------------------------
    import psycopg2
    test_params = dict(host=os.getenv("PG_TEST_HOST", "host.docker.internal"), port=int(os.getenv("PG_TEST_PORT", "5455")), user=os.getenv("PG_TEST_USER", "aiops_user"),
                       password=_required("PG_TEST_PASSWORD"), dbname=os.getenv("PG_TEST_DB", "agent_test"))

    def test_connect():
        return psycopg2.connect(**test_params)

    class ThrowawayStore(store_module.RunStore):
        def __init__(self, *a, **k):
            super().__init__(connect=test_connect, **k)
    real_store = store_module.RunStore
    store_module.RunStore = ThrowawayStore
    side = ThrowawayStore()
    side.ensure_schema()
    side.delete_tenant(args.tenant)

    # -- the real application route ---------------------------------------------------------------------------------------------------
    import app as application
    queue = ShadowQueue(lambda job: hook._execute(job.ctx, job.policy, job.task, job.request_id))
    hook._state["queue"] = queue
    gate.on_rollback(queue.clear)
    body = json.dumps({"ticket": args.ticket, "tenant_id": args.tenant, "use_fallback": False}).encode()

    def post(events):
        async def drive():
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/analyze", "raw_path": b"/analyze",
                     "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"host", b"test")],
                     "client": ("127.0.0.1", 1), "server": ("test", 80)}
            state, chunks = {"sent": False}, []

            async def receive():
                if state["sent"]:
                    await asyncio.sleep(3600)
                state["sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    chunks.append(("status", message["status"]))
                elif message["type"] == "http.response.body":
                    chunks.append(("body", message.get("body", b"")))
                    if not message.get("more_body"):
                        events.append("body_sent")
            await application.app(scope, receive, send)
            status = next(v for k, v in chunks if k == "status")
            return status, json.loads(b"".join(v for k, v in chunks if k == "body").decode("utf-8"))
        return asyncio.run(drive())

    real_submit = queue.submit

    def traced_submit(job):
        traced.append("queued")
        return real_submit(job)
    queue.submit = traced_submit

    # 1) the loop off: the reference response
    os.environ[runtime_config.MODE_ENV] = ""
    off_events: list = []
    traced: list = off_events
    status_off, off_response = post(off_events)
    check("with the loop off the request succeeds and nothing is queued", status_off == 200 and "queued" not in off_events, f"status {status_off}")

    # 2) shadow
    os.environ[runtime_config.MODE_ENV] = "shadow"
    gate.reset_cache()
    events: list = []
    traced = events
    status, shadow_response = post(events)
    check("shadow: the request succeeds", status == 200, f"status {status}")
    check("shadow: no investigation block in the response", "investigation" not in shadow_response)
    same = strip(off_response) == strip(shadow_response)
    if not same:
        diff = [k for k in set(off_response) | set(shadow_response) if strip(off_response.get(k)) != strip(shadow_response.get(k))]
        check("shadow: the response equals the loop-off response (volatile ids aside)", False, "differs in " + ", ".join(sorted(diff)[:6]))
    else:
        check("shadow: the response equals the loop-off response (volatile ids aside)", True)
    check("shadow: the run was queued only after the response body was sent", events[:2] == ["body_sent", "queued"], str(events))

    idle = queue.wait_until_idle(180)
    stats = queue.stats()
    check("shadow: the run finished", idle and stats["completed"] == 1, str(stats))

    runs = side.count_runs(args.tenant)
    check("shadow: exactly one run record was stored", runs == 1, f"{runs}")
    if runs == 1:
        with side._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT run_id, mode, status, reason, counters, versions FROM agent_runs WHERE tenant_id = %s", (args.tenant,))
            run_id, run_mode, run_status, reason, counters, versions = cur.fetchone()
            cur.execute("SELECT item::text FROM agent_evidence WHERE tenant_id = %s", (args.tenant,))
            evidence_rows = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT output_json::text FROM agent_run_events WHERE tenant_id = %s", (args.tenant,))
            event_rows = [r[0] for r in cur.fetchall()]
        check("shadow: the stored run is in mode shadow with the frozen planner", run_mode == "shadow" and versions.get("ruleset") == "rules-1" and versions.get("model") is None,
              f"status {run_status}; ruleset {versions.get('ruleset')}; model {versions.get('model')}")
        check("shadow: evidence and events are ciphertext at rest", all('"enc"' in t or "enc" in t for t in evidence_rows + event_rows) and not any("File System" in t for t in evidence_rows + event_rows),
              f"{len(evidence_rows)} evidence rows, {len(event_rows)} event rows")
        loaded = side.load_run(args.tenant, run_id)
        check("shadow: the run decrypts for its own tenant", loaded is not None and loaded.run_id == run_id)
        check("shadow: the run does not decrypt for another tenant", side.load_run("poc_client", run_id) is None)
        if loaded is not None:
            print("\n  the investigation (what the frozen rules_only planner did):")
            print(f"    status {loaded.status}: {loaded.reason}; counters {loaded.counters}")
            for s in loaded.steps:
                if s.kind == "tool":
                    print(f"    step {s.n}: {s.tool} {json.dumps(s.tool_input)[:80]} -> {s.result_summary}")
            for h in loaded.hypotheses:
                print(f"    hypothesis [{h.status}] {h.cause[:90]}  evidence {h.evidence}")
            print()

    # 3) rollback
    gate.rollback("controlled shadow test: pull the switch")
    after: list = []
    traced = after
    status_rb, _ = post(after)
    check("rollback: a further request still succeeds", status_rb == 200)
    check("rollback: nothing was queued", "queued" not in after and queue.stats()["submitted"] == 1, str(queue.stats()))
    check("rollback: nothing further was stored", side.count_runs(args.tenant) == runs)
    check("rollback: the gate reports off", gate.effective_mode(args.tenant)[0] == "off")

    side.delete_tenant(args.tenant)
    store_module.RunStore = real_store
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
