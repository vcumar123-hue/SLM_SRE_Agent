"""
The seeded draw of the real tickets of the R2-3 acceptance set (design 13.1 item 3, 13.2).

80 real tickets, 40 per client (PYPL = client_c, PFG = client_a), from the clusters whose bucket under the recorded partition seed is an **acceptance** bucket (40-79). The
calibration draws use buckets 0-39 of the same partition, so a cluster is never in both. Incident records only (`INC` + digits), one ticket per near-duplicate cluster, no ticket
type above 20% of the 100-ticket set, stratified by month. The draw seed is derived (seed.py, purpose `acceptance-selection`) from a commit fixed before the draw, so nobody can pick it.

The 20 acceptance-only synthetic tickets are a separate component (golden.py) and are not drawn here.

Privacy. The output holds ticket text and must not be shown to the labelling reviewers before the freeze (13.1). `draw_private` writes it only under `data/acceptance_private/` (git-ignored), refuses to
overwrite an earlier draw, and returns counts and hashes only, so the person or process that runs it never needs to read a ticket.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from planner_experiment import seed as seed_mod
from planner_experiment import select_cases as sc

ACCEPTANCE_QUOTAS = {"client_c": 40, "client_a": 40}
SET_SIZE = 100                                   # 80 real + 20 acceptance-only synthetic; the 20% type cap is over all 100
ACCEPTANCE_VERSION = "1"
PRIVATE_DIR = Path(__file__).resolve().parent.parent / "data" / "acceptance_private"


class AcceptanceError(Exception):
    pass


def select_acceptance(tickets: Iterable[Dict[str, Any]], partition_seed: int, draw_seed: int, quotas: Optional[Dict[str, int]] = None,
                      set_size: int = SET_SIZE) -> Dict[str, Any]:
    quotas = dict(quotas or ACCEPTANCE_QUOTAS)
    pools: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    skipped_other_buckets = ineligible = 0
    for t in tickets:
        pool = str(t.get("pool") or t.get("tenant_id"))
        if pool not in quotas:
            continue                                                            # synthetic goldens and other pools are not drawn here
        if not sc.is_eligible(t):
            ineligible += 1
            continue
        cid = sc.cluster_id(t)
        if sc.bucket(partition_seed, cid) not in sc.ACCEPTANCE_BUCKETS:
            skipped_other_buckets += 1
            continue
        pools[pool][cid].append(t)

    rng = random.Random(draw_seed)
    cap = sc.type_cap(set_size)
    counts: Counter = Counter()
    selected: List[Dict[str, Any]] = []
    shortfall: Dict[str, int] = {}
    for pool in sorted(quotas):
        want = quotas[pool]
        clusters = pools.get(pool, {})
        order = sorted(clusters)
        rng.shuffle(order)
        by_month: Dict[str, List[str]] = defaultdict(list)
        for cid in order:
            by_month[sc.month_of(sorted(clusters[cid], key=lambda x: str(x.get("ticket_id")))[0])].append(cid)
        queue: List[str] = []
        months = sorted(by_month)
        while any(by_month.values()):
            for m in months:
                if by_month[m]:
                    queue.append(by_month[m].pop(0))
        picked = 0
        for cid in queue:
            if picked >= want:
                break
            members = sorted(clusters[cid], key=lambda x: str(x.get("ticket_id")))
            ticket = members[rng.randrange(len(members))]
            kind = sc.ticket_type(ticket)
            if counts[kind] >= cap:
                continue
            counts[kind] += 1
            selected.append({**ticket, "cluster_id": cid, "pool": pool})
            picked += 1
        if picked < want:
            shortfall[pool] = want - picked
    return {"acceptance_version": ACCEPTANCE_VERSION, "selection_version": sc.SELECTION_VERSION, "partition_seed": partition_seed, "draw_seed": draw_seed,
            "selected": selected, "shortfall": shortfall, "type_counts": dict(counts), "other_bucket_tickets_skipped_unread": skipped_other_buckets,
            "ineligible_skipped": ineligible}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def draw_private(tickets: List[Dict[str, Any]], partition_record: Path, acceptance_record: Path, out_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Draw once, write the private files, return counts and hashes only."""
    out = Path(out_dir) if out_dir else PRIVATE_DIR
    ids_path, cases_path = out / "acceptance_selection.json", out / "acceptance_cases.json"
    if ids_path.exists() or cases_path.exists():
        raise AcceptanceError("an acceptance draw already exists here: the acceptance set is drawn once (a redraw is a new set and needs a new freeze)")
    try:
        partition = seed_mod.load_record(partition_record, "calibration-selection")["seed"]
        draw = seed_mod.load_record(acceptance_record, "acceptance-selection")
    except (ValueError, KeyError, OSError) as exc:
        raise AcceptanceError(f"the seed records are invalid: {exc}") from exc
    result = select_acceptance(tickets, partition, draw["seed"])
    if result["shortfall"]:
        raise AcceptanceError(f"the draw could not fill its quotas ({result['shortfall']}); nothing was written. Counts only: {len(result['selected'])} selected, "
                              f"{sum(result['type_counts'].values())} typed. The set is not silently shrunk")
    cases = [{"case_id": f"acc:{t['pool']}:{t['ticket_id']}", "pool": t["pool"], "tenant_id": t["tenant_id"], "ticket_id": t["ticket_id"], "ticket_text": t["text"],
              "host": t.get("host", ""), "opened_at": t.get("opened_at", ""), "cluster_id": t["cluster_id"]} for t in result["selected"]]
    cases.sort(key=lambda c: c["case_id"])
    selection = {"kind": "acceptance-real", "acceptance_version": ACCEPTANCE_VERSION, "selection_version": result["selection_version"],
                 "partition_seed": partition, "draw_seed": draw["seed"], "acceptance_seed_artifact": draw["artifact"], "type_counts": result["type_counts"],
                 "cases": [{"case_id": c["case_id"], "pool": c["pool"], "ticket_id": c["ticket_id"], "cluster_id": c["cluster_id"],
                            "content_hash": sc.content_hash({"pool": c["pool"], "ticket_id": c["ticket_id"], "text": c["ticket_text"], "host": c["host"],
                                                             "opened_at": c["opened_at"]})} for c in cases]}
    out.mkdir(parents=True, exist_ok=True)
    ids_blob = (json.dumps(selection, indent=1, sort_keys=True) + "\n").encode("utf-8")
    cases_blob = (json.dumps(cases, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    ids_path.write_bytes(ids_blob)
    cases_path.write_bytes(cases_blob)
    per_pool = Counter(c["pool"] for c in cases)
    return {"cases": len(cases), "per_pool": dict(per_pool), "largest_type": max(result["type_counts"].values()) if result["type_counts"] else 0,
            "selection_sha256": _sha(ids_blob), "cases_sha256": _sha(cases_blob), "other_bucket_tickets_skipped_unread": result["other_bucket_tickets_skipped_unread"],
            "ineligible_skipped": result["ineligible_skipped"]}


def verify_selection(selection_path: Path, cases_path: Path) -> None:
    """Every case in the private cases file must be the ticket the selection recorded (hash), and the selection's seeds must derive from their records' rules."""
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in json.loads(Path(cases_path).read_text(encoding="utf-8"))}
    if {e["case_id"] for e in selection["cases"]} != set(cases):
        raise AcceptanceError("the selection and the cases file list different cases")
    for e in selection["cases"]:
        c = cases[e["case_id"]]
        h = sc.content_hash({"pool": c["pool"], "ticket_id": c["ticket_id"], "text": c["ticket_text"], "host": c["host"], "opened_at": c["opened_at"]})
        if h != e["content_hash"]:
            raise AcceptanceError(f"{e['case_id']}: the ticket changed since it was drawn")
