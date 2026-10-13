"""
Retrieval snapshot: run a fixed set of sample tickets for each tenant and record what retrieval returns.

Used to compare retrieval before and after a change (see docs/baselines/). Prints one JSON document prefixed with
@@JSON@@ so it can be extracted from log noise.

    python tools/retrieval_snapshot.py                      # uses the PG* environment variables
    python tools/retrieval_snapshot.py client_a client_c    # choose tenants
"""
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)

import app  # noqa: E402
import retrieval_engine as engine  # noqa: E402

TICKETS = {
    "cpu": "CPU utilization on linux-lab-01 is at 98% for the last 20 minutes, application is slow",
    "vpn": "VPN client cannot connect, tunnel drops after authentication",
    "sap_login": "Users cannot log in to SAP since the overnight reboot of hana-db-01",
    "erp_queue": "ERP interface queue is stuck and messages are not being processed",
    "integration": "Downstream integration failure, API partner endpoint returning 503",
    "release": "Deployment of the latest release failed and rollback is needed",
}


def snapshot(tenants):
    taxonomy = app.load_taxonomy()
    out = []
    for tenant in tenants:
        for name, ticket in TICKETS.items():
            row = {"tenant": tenant, "ticket": name}
            try:
                started = time.monotonic()
                raw = engine.retrieve_incidents(ticket=ticket, limit=5, tenant_id=tenant, knowledge_scope="client_private")
                row["raw_ms"] = round((time.monotonic() - started) * 1000)
                row["raw_hits"] = len(raw)
                row["raw_top"] = [(x.get("classification"), round(float(x.get("score") or 0), 3),
                                   x.get("collection_name"), x.get("retrieval_method")) for x in raw[:3]]
                final = app.run_hybrid_retrieval(ticket, 5, "snapshot", taxonomy, tenant_id=tenant,
                                                 knowledge_scope="client_private")[0]
                row["final_hits"] = len(final)
                row["final_top"] = [(x.get("classification"), round(float(x.get("score") or 0), 3)) for x in final[:3]]
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"{type(exc).__name__}: {exc}"[:160]
            out.append(row)
    return out


if __name__ == "__main__":
    print("@@JSON@@" + json.dumps(snapshot(sys.argv[1:] or ["client_a", "client_c"]), default=str))
