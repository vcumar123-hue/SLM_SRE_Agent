"""
Composition checks for a planner-experiment set (design 13.2), run identically when the experiment is frozen and when it is loaded to run.

A set is valid only if ALL of these hold; every problem is reported, not just the first:
  * exactly 20 PYPL (client_c), 20 PFG (client_a) and 10 golden cases, each case id unique;
  * one case per cluster, and the cluster of every case, recomputed from the case's own content, equals the cluster recorded at the draw;
  * every real-ticket cluster belongs to a calibration bucket under the derived partition seed (golden tickets are calibration-only by
    rule: they never enter acceptance, so they do not go through the bucket partition);
  * no ticket type has more than 20% of the cases;
  * every case's content (id, text, host, opened_at) hashes to the content hash recorded at the draw, so the frozen cases are the drawn tickets;
  * every golden case is exactly an item of the approved golden file;
  * the set shares no case or cluster with the development subset.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from planner_experiment import secrecy
from planner_experiment import select_cases as sc


class CompositionError(Exception):
    pass


def _as_ticket(case: Dict[str, Any]) -> Dict[str, Any]:
    return {"pool": case.get("pool"), "tenant_id": case.get("tenant_id"), "ticket_id": case.get("ticket_id"),
            "text": case.get("ticket_text"), "host": case.get("host"), "opened_at": case.get("opened_at")}


def problems(cases: List[Dict[str, Any]], selection: Dict[str, Any], partition_seed: int, golden_items: Iterable[Dict[str, Any]],
             development_selection: Dict[str, Any], quotas: Optional[Dict[str, int]] = None, key: Optional[bytes] = None) -> List[str]:
    quotas = dict(quotas or sc.EXPERIMENT_QUOTAS)
    out: List[str] = []
    keyed = bool(selection.get("keyed"))
    if keyed:
        if key is None:
            return ["the experiment set is keyed and this machine does not hold the experiment key (freeze and run where the key is kept)"]
        if secrecy.key_check(key) != selection.get("key_check"):
            return ["the experiment key does not match the one the set was drawn with"]
    ids = [c.get("case_id") for c in cases]
    if len(set(ids)) != len(ids):
        out.append("duplicate case ids")
    counts = Counter(c.get("pool") for c in cases)
    if dict(counts) != quotas:
        out.append(f"composition must be exactly {quotas}, found {dict(counts)}")
    recorded = {e["case_id"]: e for e in selection.get("cases", [])}
    if set(recorded) != set(ids):
        out.append("the case list does not match the cases recorded at the draw")
    development = development_selection.get("cases", [])
    dev_ids = {e["case_id"] for e in development}
    dev_clusters = {e["cluster_id"] for e in development}
    golden_by_id = {str(g.get("ticket_id")): g for g in golden_items}

    clusters: Counter = Counter()
    types: Counter = Counter()
    for c in cases:
        cid_label = c.get("case_id")
        ticket = _as_ticket(c)
        cluster = sc.cluster_id(ticket)
        clusters[cluster] += 1
        types[sc.ticket_type(ticket)] += 1
        entry = recorded.get(cid_label)
        if entry is not None:
            want_cluster = sc.keyed_cluster(key, cluster) if keyed else cluster
            want_type = sc.keyed_type(key, sc.ticket_type(ticket)) if keyed else sc.ticket_type(ticket)
            if entry["cluster_id"] != want_cluster:
                out.append(f"{cid_label}: cluster differs from the draw")
            if entry["content_hash"] != sc.content_hash(ticket, key if keyed else None):
                out.append(f"{cid_label}: content does not match the drawn ticket")
            if entry["ticket_type"] != want_type:
                out.append(f"{cid_label}: ticket type differs from the draw")
        if c.get("pool") != "golden" and sc.bucket(partition_seed, cluster) not in sc.CALIBRATION_BUCKETS:
            out.append(f"{cid_label}: cluster is not in a calibration bucket")    # golden tickets are calibration-only by rule, not by bucket
        if cid_label in dev_ids or cluster in dev_clusters:
            out.append(f"{cid_label}: shares a case or cluster with the development subset")
        if c.get("pool") == "golden":
            item = golden_by_id.get(str(c.get("ticket_id")))
            if item is None:
                out.append(f"{cid_label}: not an item of the approved golden file")
            elif (str(item.get("text")), str(item.get("host")), str(item.get("opened_at"))) != (
                    str(c.get("ticket_text")), str(c.get("host")), str(c.get("opened_at"))):
                out.append(f"{cid_label}: differs from the approved golden item")
    out.extend(f"cluster {k} holds {v} cases: one case per cluster" for k, v in clusters.items() if v > 1)
    cap = sc.type_cap(len(cases))
    out.extend(f"ticket type '{k}' has {v} of {len(cases)} cases (cap {cap}, 20%)" for k, v in types.items() if v > cap)
    return out


def validate(cases, selection, partition_seed, golden_items, development_selection, quotas=None, key=None) -> None:
    found = problems(cases, selection, partition_seed, golden_items, development_selection, quotas, key)
    if found:
        raise CompositionError("; ".join(found))
