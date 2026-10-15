"""
Command line for the preparation steps that need the live ticket data.

    python -m planner_experiment.cli seed --artifact <40-character git commit id>
    python -m planner_experiment.cli select [--write]

`seed` records, once, how the selection seed is derived from an immutable artifact (seed.py). There is no way to pass a seed by hand.
`select` reads the seed from that record, re-verifies it, and draws the calibration set only (select_cases.py). Without --write it prints
counts and shortfalls. With --write it stores ids, pools and cluster ids (never ticket text) in
docs/baselines/planner_experiment/calibration_selection.json. Golden tickets are read from data/golden_tickets.json if it exists; each
must be approved by two named reviewers and marked calibration-only (golden.py); none exist yet, so the golden quota reports a shortfall.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from planner_experiment import seed as seed_mod
from planner_experiment import select_cases
from planner_experiment.golden import load_golden

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "baselines" / "planner_experiment" / "calibration_selection.json"
GOLDEN = ROOT / "data" / "golden_tickets.json"
SEED_RECORD = ROOT / "docs" / "baselines" / "planner_experiment" / "seed_calibration.json"
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
        out.extend(load_golden(GOLDEN))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="planner_experiment")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("seed")
    d.add_argument("--artifact", required=True, help="full 40-character git commit id fixed before the draw")
    s = sub.add_parser("select")
    s.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "seed":
        record = seed_mod.write_record(SEED_RECORD, args.artifact, "calibration-selection")
        print(json.dumps(record, indent=1))
    if args.cmd == "select":
        seed = seed_mod.load_record(SEED_RECORD, "calibration-selection")["seed"]
        args.seed = seed
        sel = select_cases.select_calibration(load_tickets(), seed)
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
