"""
Command line for the preparation steps that need the live ticket data.

    python -m planner_experiment.cli select --seed 20261014 [--write]

`select` draws the calibration set only (select_cases.py). Without --write it prints counts and shortfalls. With --write it stores ids,
pools and cluster ids (never ticket text) in docs/baselines/planner_experiment/calibration_selection.json. Golden tickets are read from
data/golden_tickets.json if it exists (a list of {ticket_id, text, host, opened_at}); none exist yet, so the golden quota reports a shortfall.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from planner_experiment import select_cases

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "baselines" / "planner_experiment" / "calibration_selection.json"
GOLDEN = ROOT / "data" / "golden_tickets.json"
HOST = re.compile(r"\b[a-z]{2,}[a-z0-9]*\d+[a-z0-9._-]*\b", re.I)


def load_tickets() -> List[Dict[str, Any]]:
    import postgres_store
    out: List[Dict[str, Any]] = []
    with postgres_store.db_cursor(dict_rows=True) as (_c, cur):
        cur.execute("SELECT tenant_id, ticket_id, short_description, opened_at, service FROM client_ticket_records "
                    "WHERE tenant_id IN ('client_a','client_c') AND ticket_id IS NOT NULL AND short_description IS NOT NULL")
        for r in cur.fetchall():
            text = r["short_description"] or ""
            m = HOST.search(text)
            out.append({"pool": r["tenant_id"], "tenant_id": r["tenant_id"], "ticket_id": r["ticket_id"], "text": text,
                        "host": m.group(0).lower() if m else "", "service": r.get("service") or "", "opened_at": r["opened_at"] or ""})
    if GOLDEN.is_file():
        for g in json.loads(GOLDEN.read_text(encoding="utf-8")):
            out.append({"pool": "golden", "tenant_id": "golden", **g})
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="planner_experiment")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "select":
        sel = select_cases.select_calibration(load_tickets(), args.seed)
        summary = {"seed": args.seed, "selected": len(sel["selected"]), "by_pool": {p: sum(1 for x in sel["selected"] if x["pool"] == p) for p in select_cases.DEFAULT_QUOTAS},
                   "shortfall": sel["shortfall"], "type_counts": sel["type_counts"]}
        print(json.dumps(summary, indent=1))
        if args.write:
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps({**summary, "selection_version": sel["selection_version"],
                                       "cases": [{"case_id": f"{x['pool']}:{x['ticket_id']}", "pool": x["pool"], "cluster_id": x["cluster_id"]}
                                                 for x in sel["selected"]]}, indent=1), encoding="utf-8")
            print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
