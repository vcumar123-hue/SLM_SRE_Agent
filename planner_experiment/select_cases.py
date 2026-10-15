"""
Seeded, cluster-aware draws from the calibration pool (design 13.2) for the development subset and the planner-experiment sets.

Tickets are grouped into near-duplicate clusters (same host or same alert-text pattern, never across tenants). Every cluster is assigned to
a bucket by a hash of (partition seed, cluster): buckets 0-39 are the calibration pool, 40-79 the acceptance pool, 80-99 unused headroom.
So calibration and acceptance are disjoint by construction, a cluster is never split, and **this module only ever selects from the
calibration buckets**: a ticket's text is read only to compute its cluster id; a ticket whose cluster is not a calibration cluster is
dropped at once, never sampled, counted by content, printed or written. Golden (synthetic) tickets are calibration-only by rule and skip
the partition, so none of them is lost to the acceptance buckets. The acceptance draw is a separate step performed at the freeze (13.9).

Within the calibration pool the draws are separate and disjoint:
  * the development subset (10 tickets: 4 PYPL, 4 PFG, 2 golden) is where candidate rules and prompts are built and tuned;
  * each planner-experiment set (50 tickets: 20 PYPL, 20 PFG, 10 golden) excludes every cluster used by the development subset and by any
    earlier planner-experiment set, so a set is never reused after planners have been revised.
One ticket per cluster; no ticket type above 20% of a set; stratified by month. The sampling seed differs from the partition seed.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from typing import Any, Dict, FrozenSet, Iterable, List, Optional

SELECTION_VERSION = "2"
CALIBRATION_BUCKETS = range(0, 40)
ACCEPTANCE_BUCKETS = range(40, 80)          # reserved; never read by this module
EXPERIMENT_QUOTAS = {"client_c": 20, "client_a": 20, "golden": 10}
DEVELOPMENT_QUOTAS = {"client_c": 4, "client_a": 4, "golden": 2}
DEFAULT_QUOTAS = EXPERIMENT_QUOTAS
MAX_TYPE_SHARE = 0.20

_HOST = re.compile(r"\b[a-z]{2,}[a-z0-9]*\d{1,}[a-z0-9._-]*\b", re.I)
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9#]+")


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


def type_cap(total: int) -> int:
    return max(1, int(total * MAX_TYPE_SHARE))


def bucket(seed: int, cluster: str) -> int:
    return int(hashlib.sha256(f"{seed}:{cluster}".encode("utf-8")).hexdigest()[:8], 16) % 100


def month_of(ticket: Dict[str, Any]) -> str:
    return str(ticket.get("opened_at") or "")[:7] or "unknown"


def content_hash(ticket: Dict[str, Any]) -> str:
    """Binds a case to the exact ticket it was drawn from (id, text, host, opened_at)."""
    payload = json.dumps([str(ticket.get("pool") or ticket.get("tenant_id")), str(ticket.get("ticket_id")), str(ticket.get("text") or ""),
                          str(ticket.get("host") or ""), str(ticket.get("opened_at") or "")], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_calibration(tickets: Iterable[Dict[str, Any]], partition_seed: int, draw_seed: int, quotas: Optional[Dict[str, int]] = None,
                       exclude_clusters: FrozenSet[str] = frozenset()) -> Dict[str, Any]:
    """Pick tickets from the calibration buckets. `tickets` need: pool (or tenant_id), ticket_id, text, host, opened_at.

    `partition_seed` decides which clusters are calibration clusters; `draw_seed` drives the sampling; `exclude_clusters` holds the clusters
    of the development subset and of earlier planner-experiment sets.
    Returns {"selected": [...], "shortfall": {pool: n}, "type_counts": {...}, ...}.
    """
    quotas = dict(quotas or EXPERIMENT_QUOTAS)
    skipped_other_buckets = 0
    pools: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for t in tickets:
        cid = cluster_id(t)
        # golden tickets are calibration-only by rule (approved by the reviewers, never in acceptance), so they skip the partition
        if str(t.get("pool") or t.get("tenant_id")) != "golden" and bucket(partition_seed, cid) not in CALIBRATION_BUCKETS:
            skipped_other_buckets += 1                      # not examined further
            continue
        if cid in exclude_clusters:
            continue
        pools[str(t.get("pool") or t.get("tenant_id"))][cid].append(t)

    rng = random.Random(draw_seed)
    selected: List[Dict[str, Any]] = []
    shortfall: Dict[str, int] = {}
    cap = type_cap(sum(quotas.values()))
    type_counts: Counter = Counter()
    for pool, want in quotas.items():
        clusters = pools.get(pool, {})
        order = sorted(clusters)                             # deterministic base order, then a seeded shuffle
        rng.shuffle(order)
        by_month: Dict[str, List[str]] = defaultdict(list)   # stratify: round-robin over months so one period does not dominate
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
            if type_counts[kind] >= cap:
                continue
            type_counts[kind] += 1
            selected.append({**ticket, "cluster_id": cid, "pool": pool})
            picked += 1
        if picked < want:
            shortfall[pool] = want - picked
    return {"selection_version": SELECTION_VERSION, "partition_seed": partition_seed, "draw_seed": draw_seed, "selected": selected,
            "shortfall": shortfall, "type_counts": dict(type_counts), "other_bucket_tickets_skipped_unread": skipped_other_buckets}


SKETCH_K = 32


def _tokens(text: str) -> set:
    words = _WORD.findall(str(text).lower())                    # case, spacing and punctuation do not matter; host names and numbers DO,
    return set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}          # so same-template tickets on other hosts stay distinct


def text_sketch(text: str, k: int = SKETCH_K) -> List[int]:
    """A bottom-k sketch of the ticket text: the k smallest 48-bit hashes of its words and word pairs (lower-cased, punctuation ignored). It
    lets a ticket be recognised later even if it was re-id'd, lost its fields, was re-cased or re-spaced, or had a few words added or removed,
    and it does not store or reveal the text. Host names and numbers are kept on purpose: normalising them would make every ticket of the same
    template identical and wrongly block the development subset. For short texts the sketch is the complete set, so similarity is exact."""
    hashes = sorted({int(hashlib.sha256(t.encode("utf-8")).hexdigest()[:12], 16) for t in _tokens(text)})
    return hashes[:k]


def sketch_similarity(a: Iterable[int], b: Iterable[int], k: int = SKETCH_K) -> float:
    """Estimated Jaccard similarity of two texts from their bottom-k sketches."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    smallest = sorted(sa | sb)[:k]
    return sum(1 for h in smallest if h in sa and h in sb) / len(smallest)


def case_id_for(ticket: Dict[str, Any]) -> str:
    return f"{ticket.get('pool') or ticket.get('tenant_id')}:{ticket['ticket_id']}"


def case_ids(selection: Dict[str, Any]) -> List[str]:
    return [case_id_for(s) for s in selection["selected"]]


def selection_entries(selection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The recordable form of a draw: ids, clusters, types and content hashes, never ticket text."""
    return [{"case_id": case_id_for(t), "pool": t["pool"], "ticket_id": str(t["ticket_id"]), "cluster_id": t["cluster_id"],
             "ticket_type": ticket_type(t), "content_hash": content_hash(t), "text_sketch": text_sketch(str(t.get("text") or ""))}
            for t in selection["selected"]]
