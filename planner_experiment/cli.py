"""
Command line for the preparation steps that need the live ticket data (see steps.py for the order and the rules each step enforces).

    python -m planner_experiment.cli reviewers --names "A B" "C D" --hours 40 40 --recorded-by "owner name"
    python -m planner_experiment.cli init-key                                              (once, before the first experiment draw)
    python -m planner_experiment.cli seed --artifact <40-character git commit id>               (partition seed, recorded once)
    python -m planner_experiment.cli select-development --artifact <40-character git commit id>
    python -m planner_experiment.cli void-development --reason "..." --authorised-by "..."     (a recorded departure; refused once planners are frozen)
    python -m planner_experiment.cli acknowledge-void --voids 1 2 --by "Reviewer Name" --comment 1="exact words" --comment 2="exact words" --overall "exact words" --sheet file.xlsx
    python -m planner_experiment.cli correct-acknowledgement --void 1 --by "Reviewer Name" --comment "exact words" --overall "exact words" --sheet file.xlsx --reason "..."
    python -m planner_experiment.cli retire-key --reason "..." --by "..."                      (retire the key on THIS machine by its check value)
    python -m planner_experiment.cli key-check                                                (print the check value of the key on this machine; never the key)
    python -m planner_experiment.cli register-custody --check <16 hex> --custodian "person, machine"
    python -m planner_experiment.cli verify-model                                              (the served model and its file against the manifest)
    python -m planner_experiment.cli freeze-planners --round 1 --planner rules_only=pkg.mod:Class ...
    python -m planner_experiment.cli draw-experiment --round 1
    python -m planner_experiment.cli acceptance-seed --artifact <full commit id>        (R2-3: records the derived acceptance draw seed)
    python -m planner_experiment.cli draw-acceptance                                    (R2-3: private draw of the 80 real tickets; prints counts and hashes only)
    python -m planner_experiment.cli capture-development --out <recording>          (no key; the 10 development tickets)
    python -m planner_experiment.cli capture-experiment --round 1 --out <file outside the repository>   (key holder only)
    python -m planner_experiment.cli build-cases --round 1 --out <file outside the repository>
    python -m planner_experiment.cli approve-golden --draft docs/drafts/golden_tickets_DRAFT.json --ids golden-1 golden-2 ... --approved-by "A B" "C D"
    python -m planner_experiment.cli create-freeze --round 1 --cases <file> --recording <file> --annotations <file> --reviewer-file "A B"=<file> --reviewer-file "C D"=<file>
    python -m planner_experiment.cli run-experiment --record <freeze record>        (the one-shot run: claimed, never a dry run)

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
                    "WHERE tenant_id IN ('client_a','client_c') AND ticket_id ~ '^INC[0-9]+$' AND short_description IS NOT NULL")
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
    sub.add_parser("init-key")
    d = sub.add_parser("seed")
    d.add_argument("--artifact", required=True)
    dev = sub.add_parser("select-development")
    dev.add_argument("--artifact", required=True)
    vd = sub.add_parser("void-development")
    vd.add_argument("--reason", required=True)
    vd.add_argument("--authorised-by", required=True)
    av = sub.add_parser("acknowledge-void")
    av.add_argument("--voids", type=int, nargs="+", required=True)
    av.add_argument("--by", required=True)
    av.add_argument("--note", default="")
    av.add_argument("--comment", action="append", default=[], help='the reviewer\'s exact words for one void: N="text" (repeat per void)')
    av.add_argument("--overall", default=None, help="the reviewer's exact overall comment")
    av.add_argument("--sheet", default="", help="the sheet the words came from")
    ca = sub.add_parser("correct-acknowledgement")
    ca.add_argument("--void", type=int, required=True)
    ca.add_argument("--by", required=True)
    ca.add_argument("--comment", required=True)
    ca.add_argument("--overall", required=True)
    ca.add_argument("--sheet", required=True)
    ca.add_argument("--reason", required=True)
    sub.add_parser("verify-model")
    rk = sub.add_parser("retire-key")
    rk.add_argument("--reason", required=True)
    rk.add_argument("--by", required=True)
    sub.add_parser("key-check")
    rc = sub.add_parser("register-custody")
    rc.add_argument("--check", required=True)
    rc.add_argument("--custodian", required=True)
    fp = sub.add_parser("freeze-planners")
    fp.add_argument("--round", type=int, required=True)
    fp.add_argument("--planner", action="append", required=True, help="mode=module:Class (rules_only, rules_then_model, model)")
    de = sub.add_parser("draw-experiment")
    de.add_argument("--round", type=int, required=True)
    ag = sub.add_parser("approve-golden")
    ag.add_argument("--draft", required=True)
    ag.add_argument("--ids", nargs="+", required=True, help="the ticket ids the reviewers approved (listed explicitly; there is no 'all')")
    ag.add_argument("--approved-by", nargs=2, required=True)
    ag.add_argument("--out", default=None)
    cf = sub.add_parser("create-freeze")
    cf.add_argument("--round", type=int, required=True)
    cf.add_argument("--cases", required=True)
    cf.add_argument("--recording", required=True)
    cf.add_argument("--annotations", required=True)
    cf.add_argument("--reviewer-file", action="append", required=True, help='"Reviewer Name"=<path to that reviewer\'s own annotations>')
    cf.add_argument("--out", default=None)
    rx = sub.add_parser("run-experiment")
    rx.add_argument("--record", required=True)
    sa = sub.add_parser("acceptance-seed")
    sa.add_argument("--artifact", required=True, help="a full 40-character commit id fixed before the draw")
    da = sub.add_parser("draw-acceptance")
    da.add_argument("--out", default=None, help="a private folder (default data/acceptance_private, git-ignored)")
    cd = sub.add_parser("capture-development")
    cd.add_argument("--out", required=True)
    cd.add_argument("--cases-out", default=None, help="also write the development cases (their ticket text, for local use: keep out of git) for benchmark-model")
    cd.add_argument("--invalidate", default=None, help="replace an existing complete recording: the recorded reason")
    cd.add_argument("--by", default=None)
    bm = sub.add_parser("benchmark-model")
    bm.add_argument("--cases", required=True, help="the development cases file written by capture-development --cases-out")
    bm.add_argument("--recording", required=True, help="the development recording")
    bm.add_argument("--repetitions", type=int, default=2)
    bm.add_argument("--out", default=None, help="write the report here (keep it with the freeze records)")
    bm.add_argument("--models-dir", default=None, help="LM Studio's models folder on this machine (default ~/.lmstudio/models); the GGUF's SHA-256 is verified there")
    ce = sub.add_parser("capture-experiment")
    ce.add_argument("--round", type=int, required=True)
    ce.add_argument("--out", required=True, help="a file outside the repository")
    ce.add_argument("--invalidate", default=None)
    ce.add_argument("--by", default=None)
    bc = sub.add_parser("build-cases")
    bc.add_argument("--round", type=int, required=True)
    bc.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    base = steps.BASE
    try:
        if args.cmd == "reviewers":
            print(json.dumps(steps.record_reviewers(base, args.names, dict(zip(args.names, args.hours)), args.recorded_by), indent=1))
        elif args.cmd == "init-key":
            from planner_experiment import secrecy
            try:
                print("experiment key created at", secrecy.KEY_FILE, "- check value", secrecy.create_key(), "- keep it out of the repository and back it up")
            except secrecy.KeyError_ as exc:
                print("refused:", exc)
                return 2
        elif args.cmd == "seed":
            print(json.dumps(seed_mod.write_record(steps.paths(base)["partition_seed"], args.artifact, "calibration-selection"), indent=1))
        elif args.cmd == "select-development":
            rec = steps.select_development(base, load_tickets(steps.reviewer_names(base)), args.artifact)
            print(json.dumps({"cases": len(rec["cases"]), "draw_seed": rec["draw_seed"]}, indent=1))
        elif args.cmd == "void-development":
            print(json.dumps(steps.void_development(base, args.reason, args.authorised_by), indent=1))
        elif args.cmd == "acknowledge-void":
            comments = {int(item.split("=", 1)[0]): item.split("=", 1)[1] for item in args.comment}
            entries = steps.acknowledge_voids(base, args.voids, args.by, args.note, comments or None, args.overall, args.sheet)
            print(json.dumps([{"void": e["void"], "acknowledged_by": [a["reviewer"] for a in e["acknowledged_by"]]} for e in entries], indent=1))
            still = steps.unacknowledged_voids(base)
            print("still to acknowledge:", still if still else "nothing: all voids are acknowledged by both reviewers")
        elif args.cmd in ("retire-key", "key-check", "register-custody"):
            from planner_experiment import secrecy
            try:
                if args.cmd == "key-check":
                    held = secrecy.load_key()
                    print("key check:", secrecy.key_check(held) if held is not None else "no key on this machine")
                elif args.cmd == "retire-key":
                    held = secrecy.load_key()
                    if held is None:
                        raise secrecy.KeyError_("there is no key on this machine to retire")
                    print(json.dumps(secrecy.retire(held, args.reason, args.by, base / "retired_keys.json"), indent=1))
                else:
                    print(json.dumps(secrecy.register_custody(args.check, args.custodian, base / "key_custody.json", base / "retired_keys.json"), indent=1))
            except secrecy.KeyError_ as exc:
                print("refused:", exc)
                return 2
        elif args.cmd == "correct-acknowledgement":
            ack = steps.correct_acknowledgement(base, args.void, args.by, args.comment, args.overall, args.sheet, args.reason)
            print(json.dumps(ack, indent=1))
        elif args.cmd == "verify-model":
            from agentruntime.planners import model_manifest
            report = model_manifest.verify()
            print(json.dumps(report, indent=1))
            return 0 if report["ok"] else 2
        elif args.cmd == "freeze-planners":
            entry = dict(item.split("=", 1) for item in args.planner)
            rec = steps.freeze_planners(base, args.round, entry)
            print(json.dumps({"record_hash": rec["record_hash"], "entrypoints": rec["entrypoints"]}, indent=1))
        elif args.cmd == "draw-experiment":
            rec = steps.draw_experiment(base, args.round, load_tickets(steps.reviewer_names(base)))
            print(json.dumps({"round": rec["round"], "cases": len(rec["cases"]), "draw_seed": rec["draw_seed"], "type_counts": rec["type_counts"]}, indent=1))
        elif args.cmd == "approve-golden":
            items = steps.approve_golden(base, Path(args.draft), Path(args.out) if args.out else GOLDEN, args.ids, args.approved_by)
            print("wrote", len(items), "approved golden tickets;", "ids:", ", ".join(i["ticket_id"] for i in items))
        elif args.cmd == "create-freeze":
            reviewer_files = dict(item.rsplit("=", 1) for item in args.reviewer_file)
            record = steps.create_freeze_for_round(base, args.round, Path(args.cases), Path(args.recording), Path(args.annotations), reviewer_files, GOLDEN,
                                                   Path(args.out) if args.out else None)
            print(json.dumps({"record_hash": record["record_hash"], "reviewers": record["reviewers"], "end_state_kappa": record["agreement"]["end_state_kappa"],
                              "disagreements": len(record["agreement"]["disagreements"])}, indent=1))
        elif args.cmd == "run-experiment":
            from planner_experiment.experiment import ExperimentInputError, run_once
            try:
                out = run_once(Path(args.record), steps.BASE / "results")
            except ExperimentInputError as exc:
                print("refused:", exc)
                return 2
            print(json.dumps({"verdict": out["decision"]["verdict"], "conditions": out["decision"]["conditions"]}, indent=1))
        elif args.cmd == "acceptance-seed":
            from planner_experiment import seed as seed_mod
            record = seed_mod.write_record(ROOT / "docs" / "baselines" / "release2" / "seed_acceptance.json", args.artifact, "acceptance-selection")
            print(json.dumps({"purpose": record["purpose"], "artifact": record["artifact"], "seed": record["seed"]}, indent=1))
        elif args.cmd == "draw-acceptance":
            from planner_experiment import acceptance
            summary = acceptance.draw_private(load_tickets(steps.reviewer_names(base)), steps.paths(base)["partition_seed"], ROOT / "docs" / "baselines" / "release2" / "seed_acceptance.json",
                                              Path(args.out) if args.out else None)
            print(json.dumps(summary, indent=1))
        elif args.cmd in ("capture-development", "capture-experiment"):
            tickets = load_tickets(steps.reviewer_names(base))
            if args.cmd == "capture-development":
                manifest = steps.capture_development(base, tickets, Path(args.out), invalidate=args.invalidate, by=args.by)
            else:
                manifest = steps.capture_experiment(base, args.round, tickets, Path(args.out), invalidate=args.invalidate, by=args.by)
            if args.cmd == "capture-development" and args.cases_out:
                Path(args.cases_out).write_text(json.dumps(steps.build_cases(steps.paths(base)["development"], tickets), indent=1, ensure_ascii=False), encoding="utf-8")
            dropped = sum(sum(c["dropped"].values()) for case in manifest["cases"].values() for c in case["calls"])
            print(json.dumps({"mode": manifest["mode"], "cases": manifest["case_count"], "held_out": manifest["held_out_count"],
                              "items_removed_as_held_out": dropped, "recording_sha256": manifest["recording_sha256"]}, indent=1))
        elif args.cmd == "benchmark-model":
            from planner_experiment import benchmark
            report = benchmark.run_benchmark(json.loads(Path(args.cases).read_text(encoding="utf-8")), Path(args.recording), args.repetitions, models_dir=args.models_dir)
            if args.out:
                Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
            summary = {k: report[k] for k in ("environment", "warmup", "runs", "p95_call_seconds", "p95_run_seconds", "fits_frozen_budgets", "qualifies", "verdict")}
            print(json.dumps(summary, indent=1))
            return 0 if report["qualifies"] else 3
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
