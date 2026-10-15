"""
Command line for the preparation steps that need the live ticket data (see steps.py for the order and the rules each step enforces).

    python -m planner_experiment.cli reviewers --names "A B" "C D" --hours 40 40 --recorded-by "owner name"
    python -m planner_experiment.cli seed --artifact <40-character git commit id>               (partition seed, recorded once)
    python -m planner_experiment.cli select-development --artifact <40-character git commit id>
    python -m planner_experiment.cli freeze-planners --round 1 --planner rules_only=pkg.mod:Class ...
    python -m planner_experiment.cli draw-experiment --round 1
    python -m planner_experiment.cli build-cases --round 1 --out <file outside the repository>

There is no way to pass a seed: every seed is derived from an immutable artifact (seed.py). Golden tickets are read from
data/golden_tickets.json and must be approved by exactly the two reviewers of the reviewer record. Ticket text is written only by
`build-cases`, to the file you name (keep it out of git); the recorded selections hold ids, clusters, types and hashes only.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from planner_experiment import seed as seed_mod
from planner_experiment import steps
from planner_experiment.golden import load_golden

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "data" / "golden_tickets.json"
HOST = re.compile(r"\b[a-z]{2,}[a-z0-9]*\d+[a-z0-9._-]*\b", re.I)


def load_tickets(reviewer_names: List[str]) -> List[Dict[str, Any]]:
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
        out.extend(load_golden(GOLDEN, reviewer_names))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="planner_experiment")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reviewers")
    r.add_argument("--names", nargs=2, required=True)
    r.add_argument("--hours", nargs=2, type=float, required=True)
    r.add_argument("--recorded-by", required=True)
    d = sub.add_parser("seed")
    d.add_argument("--artifact", required=True)
    dev = sub.add_parser("select-development")
    dev.add_argument("--artifact", required=True)
    fp = sub.add_parser("freeze-planners")
    fp.add_argument("--round", type=int, required=True)
    fp.add_argument("--planner", action="append", required=True, help="mode=module:Class (rules_only, rules_then_model, model)")
    de = sub.add_parser("draw-experiment")
    de.add_argument("--round", type=int, required=True)
    bc = sub.add_parser("build-cases")
    bc.add_argument("--round", type=int, required=True)
    bc.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    base = steps.BASE
    try:
        if args.cmd == "reviewers":
            print(json.dumps(steps.record_reviewers(base, args.names, dict(zip(args.names, args.hours)), args.recorded_by), indent=1))
        elif args.cmd == "seed":
            print(json.dumps(seed_mod.write_record(steps.paths(base)["partition_seed"], args.artifact, "calibration-selection"), indent=1))
        elif args.cmd == "select-development":
            rec = steps.select_development(base, load_tickets(steps.reviewer_names(base)), args.artifact)
            print(json.dumps({"cases": len(rec["cases"]), "draw_seed": rec["draw_seed"]}, indent=1))
        elif args.cmd == "freeze-planners":
            entry = dict(item.split("=", 1) for item in args.planner)
            rec = steps.freeze_planners(base, args.round, entry)
            print(json.dumps({"record_hash": rec["record_hash"], "entrypoints": rec["entrypoints"]}, indent=1))
        elif args.cmd == "draw-experiment":
            rec = steps.draw_experiment(base, args.round, load_tickets(steps.reviewer_names(base)))
            print(json.dumps({"round": rec["round"], "cases": len(rec["cases"]), "draw_seed": rec["draw_seed"], "type_counts": rec["type_counts"]}, indent=1))
        elif args.cmd == "build-cases":
            cases = steps.build_cases(steps.round_paths(base, args.round)["selection"], load_tickets(steps.reviewer_names(base)))
            Path(args.out).write_text(json.dumps(cases, indent=1), encoding="utf-8")
            print("wrote", len(cases), "cases to", args.out)
    except steps.StepError as exc:
        print("refused:", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
