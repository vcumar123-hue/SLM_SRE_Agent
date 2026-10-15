"""
Seeded calibration draw (design 13.2).

Tickets are grouped into near-duplicate clusters (same host or same alert-text pattern). Every cluster is assigned to a bucket by a hash of
(seed, cluster): buckets 0-39 are the calibration pool, 40-79 the acceptance pool, 80-99 unused headroom. So the two sets are disjoint by
construction, a cluster is never split, and **this module only ever selects from the calibration bucket**: a ticket's text is read only to compute its cluster id; if the
cluster is not a calibration cluster the ticket is dropped at once, never sampled, counted by content, printed or written. The acceptance draw is a separate step performed at the freeze (13.9).

Composition: PYPL 40%, PFG 40%, golden 20% of the experiment's 50 tickets (20 / 20 / 10); no ticket type above 20%; stratified by month.
"""
from __future__ import annotations

import hashlib
import random
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

SELECTION_VERSION = "1"
CALIBRATION_BUCKETS = range(0, 40)
ACCEPTANCE_BUCKETS = range(40, 80)          # reserved; never read by this module
DEFAULT_QUOTAS = {"client_c": 20, "client_a": 20, "golden": 10}
MAX_TYPE_SHARE = 0.20

_HOST = re.compile(r"\b[a-z]{2,}[a-z0-9]*\d{1,}[a-z0-9._-]*\b", re.I)
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """The alert-text pattern: hosts and numbers replaced so 'disk full on sapdb01' and 'disk full on sapdb02' match."""
    t = _HOST.sub("HOST", (text or "").lower())
    t = _DIGITS.sub("#", t)
    return _SPACE.sub(" ", t).strip()


def cluster_id(ticket: Dict[str, Any]) -> str:
    host = str(ticket.get("host") or "").strip().lower()
    pattern = " ".join(normalise(ticket.get("text") or "").split()[:8])
    owner = str(ticket.get("pool") or ticket.get("tenant_id") or "")       # clusters never span tenants
    return hashlib.sha256(f"{owner}|{host}|{pattern}".encode("utf-8")).hexdigest()[:12]


def ticket_type(ticket: Dict[str, Any]) -> str:
    return " ".join(normalise(ticket.get("text") or "").split()[:2]) or "unknown"


def bucket(seed: int, cluster: str) -> int:
    return int(hashlib.sha256(f"{seed}:{cluster}".encode("utf-8")).hexdigest()[:8], 16) % 100


def month_of(ticket: Dict[str, Any]) -> str:
    return str(ticket.get("opened_at") or "")[:7] or "unknown"


def select_calibration(tickets: Iterable[Dict[str, Any]], seed: int, quotas: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Pick calibration tickets. `tickets` need: tenant_id (or 'golden'), ticket_id, text, host, opened_at.

    Returns {"selected": [...], "clusters": {cluster: [ids]}, "shortfall": {pool: n}, "type_counts": {...}}. Only calibration-bucket
    clusters are examined; the rest are counted and discarded unread.
    """
    quotas = dict(quotas or DEFAULT_QUOTAS)
    skipped_other_buckets = 0
    pools: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for t in tickets:
        cid = cluster_id(t)
        if bucket(seed, cid) not in CALIBRATION_BUCKETS:
            skipped_other_buckets += 1                      # not examined further
            continue
        pools[str(t.get("pool") or t.get("tenant_id"))][cid].append(t)

    rng = random.Random(seed)
    selected: List[Dict[str, Any]] = []
    shortfall: Dict[str, int] = {}
    total_target = sum(quotas.values())
    type_cap = max(1, int(total_target * MAX_TYPE_SHARE))
    type_counts: Counter = Counter()
    for pool, want in quotas.items():
        clusters = pools.get(pool, {})
        order = sorted(clusters)                             # deterministic base order, then a seeded shuffle
        rng.shuffle(order)
        # stratify by month: round-robin over months so one period does not dominate
        by_month: Dict[str, List[str]] = defaultdict(list)
        for cid in order:
            by_month[month_of(sorted(clusters[cid], key=lambda x: str(x.get("ticket_id")))[0])].append(cid)
        months = sorted(by_month)
        queue: List[str] = []
        while any(by_month.values()):
            for m in months:
                if by_month[m]:
                    queue.append(by_month[m].pop(0))
        picked = 0
        for cid in queue:
            if picked >= want:
                break
            members = sorted(clusters[cid], key=lambda x: str(x.get("ticket_id")))
            ticket = members[rng.randrange(len(members))]      # one ticket per cluster: near-duplicates add no information
            kind = ticket_type(ticket)
            if type_counts[kind] >= type_cap:
                continue
            type_counts[kind] += 1
            selected.append({**ticket, "cluster_id": cid, "pool": pool})
            picked += 1
        if picked < want:
            shortfall[pool] = want - picked
    all_clusters = {cid: sorted(str(m.get("ticket_id")) for m in members) for p in pools.values() for cid, members in p.items()
                    if cid in {s["cluster_id"] for s in selected}}
    return {"selection_version": SELECTION_VERSION, "seed": seed, "selected": selected, "clusters": all_clusters,
            "shortfall": shortfall, "type_counts": dict(type_counts), "other_bucket_tickets_skipped_unread": skipped_other_buckets}


def case_ids(selection: Dict[str, Any]) -> List[str]:
    return [f"{s['pool']}:{s['ticket_id']}" for s in selection["selected"]]
