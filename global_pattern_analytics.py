"""
Problem Management Pattern Analytics Engine.

Reads confidence_log.jsonl (timestamped incident classifications) to:
  1. Detect recurring incident patterns (frequency, trend, variance)
  2. Discover upstream precursors and downstream impacts via temporal
     co-occurrence analysis — without requiring a topology map
  3. Produce Problem Management Candidates stored in global_learned_patterns.json

Each Problem Management Candidate answers:
  - How often does this pattern occur and is it getting worse?
  - How consistent is the pattern (low variance = exact repeat, high = drift)?
  - What incidents reliably precede it (upstream root cause signals)?
  - What incidents reliably follow it (downstream blast radius)?

This is the missing capability in most enterprise ITSM tools: automatic
causal chain discovery from incident history alone.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import settings_portable as settings
from file_manager import write_json, utc_now_iso


# ── Thresholds (env-overridable) ─────────────────────────────────────────────
MIN_OCCURRENCES: int        = int(os.getenv("PMC_MIN_OCCURRENCES", "3"))
CORRELATION_WINDOW_MIN: int = int(os.getenv("PMC_CORRELATION_WINDOW_MIN", "30"))
MIN_CORRELATION_HIT_RATE: float = float(os.getenv("PMC_MIN_HIT_RATE", "0.30"))
MIN_CORRELATION_COUNT: int  = int(os.getenv("PMC_MIN_CORR_COUNT", "2"))
TREND_WINDOW_DAYS: int      = int(os.getenv("PMC_TREND_WINDOW_DAYS", "30"))
MAX_CHAIN_ENTRIES: int      = int(os.getenv("PMC_MAX_CHAIN_ENTRIES", "3"))

_CONFIDENCE_LOG  = settings.LOG_DIR / "confidence_log.jsonl"
_GLOBAL_FILE     = settings.DATA_DIR / "global_learned_patterns.json"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                pass
    except Exception:
        pass
    return rows


def _load_list(path: Path) -> List[Dict[str, Any]]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def _cls_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _stddev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _variance_label(stddev: float) -> str:
    if stddev <= 0.05:
        return "low"
    if stddev <= 0.15:
        return "medium"
    return "high"


def _trend_label(current: int, prior: int) -> Tuple[str, str]:
    """Returns (trend_word, arrow)."""
    if prior == 0:
        return ("new", "→") if current < 3 else ("increasing", "↑")
    ratio = current / prior
    if ratio >= 1.25:
        return ("increasing", "↑")
    if ratio <= 0.75:
        return ("declining", "↓")
    return ("stable", "→")


def _mean_hours_between(timestamps: List[datetime]) -> Optional[float]:
    if len(timestamps) < 2:
        return None
    sorted_ts = sorted(timestamps)
    gaps = [(sorted_ts[i + 1] - sorted_ts[i]).total_seconds() / 3600
            for i in range(len(sorted_ts) - 1)]
    return round(sum(gaps) / len(gaps), 1)


# ── Core analytics ────────────────────────────────────────────────────────────

def _load_entries() -> List[Dict[str, Any]]:
    """Load confidence_log entries, enrich with parsed timestamp."""
    raw = _load_jsonl(_CONFIDENCE_LOG)
    entries = []
    for row in raw:
        ts = _parse_ts(row.get("timestamp"))
        cls = _cls_key(row.get("classification"))
        if not ts or not cls or cls == "unknown":
            continue
        conf = row.get("display_confidence") or row.get("primary_confidence") or 0.0
        entries.append({
            "ts": ts,
            "cls": cls,
            "conf": float(conf),
            "request_id": row.get("request_id", ""),
            "domain": str(row.get("domain") or ""),
            "ticket": str(row.get("ticket") or ""),
        })
    entries.sort(key=lambda e: e["ts"])
    return entries


def _compute_per_classification_stats(
    entries: List[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Dict[str, Any]]:
    """Frequency, trend, variance, MTBO per classification."""
    current_cutoff = now.timestamp() - TREND_WINDOW_DAYS * 86400
    prior_cutoff   = current_cutoff - TREND_WINDOW_DAYS * 86400

    cls_stats: Dict[str, Dict[str, Any]] = {}

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        grouped[e["cls"]].append(e)

    for cls, evts in grouped.items():
        timestamps = [e["ts"] for e in evts]
        confs      = [e["conf"] for e in evts]

        current_count = sum(1 for e in evts if e["ts"].timestamp() >= current_cutoff)
        prior_count   = sum(1 for e in evts if prior_cutoff <= e["ts"].timestamp() < current_cutoff)
        trend, arrow  = _trend_label(current_count, prior_count)

        avg_conf = round(sum(confs) / len(confs), 4) if confs else 0.0
        sd       = _stddev(confs)
        mtbo     = _mean_hours_between(timestamps)

        # Representative ticket text (highest confidence occurrence)
        best = max(evts, key=lambda e: e["conf"])

        cls_stats[cls] = {
            "classification": cls,
            "occurrence_count": len(evts),
            "current_period_count": current_count,
            "prior_period_count": prior_count,
            "trend": trend,
            "trend_arrow": arrow,
            "avg_confidence": avg_conf,
            "confidence_stddev": round(sd, 4),
            "variance": _variance_label(sd),
            "mean_time_between_hours": mtbo,
            "first_seen": evts[0]["ts"].isoformat(),
            "last_seen": evts[-1]["ts"].isoformat(),
            "domain": best.get("domain", ""),
            "sample_ticket": best.get("ticket", "")[:200],
        }

    return cls_stats


def _compute_temporal_correlations(
    entries: List[Dict[str, Any]],
    cls_stats: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    For each occurrence of each classification, scan a time window to find
    what other classifications co-occur before (upstream) and after (downstream).

    Returns a dict keyed by classification with upstream/downstream lists.
    """
    window_sec = CORRELATION_WINDOW_MIN * 60
    n = len(entries)

    # For each classification, per co-occurring classification track:
    #   before_count, after_count, before_deltas, after_deltas
    corr: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {
            "before_count": 0, "after_count": 0,
            "before_deltas_sec": [], "after_deltas_sec": [],
        })
    )

    for i, anchor in enumerate(entries):
        anchor_ts = anchor["ts"].timestamp()
        anchor_cls = anchor["cls"]

        # Scan entries within window using two pointers
        j = i - 1
        while j >= 0:
            delta = anchor_ts - entries[j]["ts"].timestamp()
            if delta > window_sec:
                break
            other_cls = entries[j]["cls"]
            if other_cls != anchor_cls:
                corr[anchor_cls][other_cls]["before_count"] += 1
                corr[anchor_cls][other_cls]["before_deltas_sec"].append(delta)
            j -= 1

        j = i + 1
        while j < n:
            delta = entries[j]["ts"].timestamp() - anchor_ts
            if delta > window_sec:
                break
            other_cls = entries[j]["cls"]
            if other_cls != anchor_cls:
                corr[anchor_cls][other_cls]["after_count"] += 1
                corr[anchor_cls][other_cls]["after_deltas_sec"].append(delta)
            j += 1

    # Convert raw counts to structured upstream/downstream lists
    result: Dict[str, Dict[str, Any]] = {}

    for anchor_cls, others in corr.items():
        total = cls_stats.get(anchor_cls, {}).get("occurrence_count", 1)
        upstream: List[Dict[str, Any]] = []
        downstream: List[Dict[str, Any]] = []

        for other_cls, data in others.items():
            # Upstream: reliably appears BEFORE anchor
            bc = data["before_count"]
            if bc >= MIN_CORRELATION_COUNT:
                hit_rate = round(bc / total, 3)
                if hit_rate >= MIN_CORRELATION_HIT_RATE:
                    avg_min = round(
                        sum(data["before_deltas_sec"]) / len(data["before_deltas_sec"]) / 60, 1
                    ) if data["before_deltas_sec"] else 0.0
                    upstream.append({
                        "classification": other_cls,
                        "co_occurrence_count": bc,
                        "total_anchor_occurrences": total,
                        "hit_rate": hit_rate,
                        "avg_minutes_before": avg_min,
                        "label": f"{bc}/{total} cases, avg {avg_min} min before",
                    })

            # Downstream: reliably appears AFTER anchor
            ac = data["after_count"]
            if ac >= MIN_CORRELATION_COUNT:
                hit_rate = round(ac / total, 3)
                if hit_rate >= MIN_CORRELATION_HIT_RATE:
                    avg_min = round(
                        sum(data["after_deltas_sec"]) / len(data["after_deltas_sec"]) / 60, 1
                    ) if data["after_deltas_sec"] else 0.0
                    downstream.append({
                        "classification": other_cls,
                        "co_occurrence_count": ac,
                        "total_anchor_occurrences": total,
                        "hit_rate": hit_rate,
                        "avg_minutes_after": avg_min,
                        "label": f"{ac}/{total} cases, avg {avg_min} min after",
                    })

        # Sort by hit_rate descending, cap list length
        upstream.sort(key=lambda x: -x["hit_rate"])
        downstream.sort(key=lambda x: -x["hit_rate"])
        result[anchor_cls] = {
            "upstream_precursors": upstream[:MAX_CHAIN_ENTRIES],
            "downstream_impacts": downstream[:MAX_CHAIN_ENTRIES],
        }

    return result


def _build_recommendation(
    cls: str,
    stats: Dict[str, Any],
    correlations: Dict[str, Any],
) -> str:
    """Generate a plain-English Problem Management recommendation."""
    parts: List[str] = []
    title = cls.replace("_", " ").title()

    upstream = correlations.get("upstream_precursors", [])
    downstream = correlations.get("downstream_impacts", [])

    if upstream:
        top_up = upstream[0]
        up_title = top_up["classification"].replace("_", " ").title()
        parts.append(
            f"'{up_title}' precedes this pattern in {top_up['co_occurrence_count']} of "
            f"{top_up['total_anchor_occurrences']} cases (avg {top_up['avg_minutes_before']} min before). "
            f"Resolving '{up_title}' at source is likely to prevent '{title}' occurrences."
        )

    if downstream:
        impacts = [d["classification"].replace("_", " ").title() for d in downstream[:2]]
        parts.append(
            f"When '{title}' occurs, it triggers '{', '.join(impacts)}' downstream "
            f"in {downstream[0]['co_occurrence_count']} of {downstream[0]['total_anchor_occurrences']} cases — "
            f"indicating blast radius beyond the immediate incident."
        )

    if stats.get("trend") == "increasing":
        parts.append(
            f"Occurrence rate is increasing ({stats['current_period_count']} in the last "
            f"{TREND_WINDOW_DAYS} days vs {stats['prior_period_count']} in the prior period). "
            f"Escalating urgency for Problem Management intervention."
        )

    if stats.get("variance") == "low":
        parts.append("Low pattern variance indicates a deterministic root cause — high confidence in targeted fix.")
    elif stats.get("variance") == "high":
        parts.append("High pattern variance suggests multiple contributing causes — broader infrastructure review recommended.")

    if not parts:
        parts.append(
            f"'{title}' has occurred {stats['occurrence_count']} times. "
            f"Raise a Problem record for root cause investigation."
        )

    return " ".join(parts)


# ── Public API ────────────────────────────────────────────────────────────────

def compute_problem_management_candidates() -> List[Dict[str, Any]]:
    """
    Analyse confidence_log.jsonl and return Problem Management Candidates
    with causal chain data (upstream precursors + downstream impacts).
    Does NOT write to disk.
    """
    entries = _load_entries()
    if not entries:
        return []

    now = datetime.now(tz=timezone.utc)
    cls_stats = _compute_per_classification_stats(entries, now)
    correlations = _compute_temporal_correlations(entries, cls_stats)

    candidates: List[Dict[str, Any]] = []
    for cls, stats in cls_stats.items():
        if stats["occurrence_count"] < MIN_OCCURRENCES:
            continue

        corr = correlations.get(cls, {"upstream_precursors": [], "downstream_impacts": []})
        recommendation = _build_recommendation(cls, stats, corr)

        candidates.append({
            **stats,
            "upstream_precursors": corr["upstream_precursors"],
            "downstream_impacts": corr["downstream_impacts"],
            "has_causal_chain": bool(corr["upstream_precursors"] or corr["downstream_impacts"]),
            "recommendation": recommendation,
            "problem_management_priority": (
                "high" if (
                    stats["trend"] == "increasing" and
                    bool(corr["upstream_precursors"])
                ) else
                "medium" if stats["occurrence_count"] >= MIN_OCCURRENCES * 2 else
                "low"
            ),
        })

    # Sort: high priority first, then by occurrence count
    priority_order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(
        key=lambda c: (priority_order.get(c["problem_management_priority"], 3), -c["occurrence_count"])
    )
    return candidates


def run_analytics(*, dry_run: bool = False) -> Dict[str, Any]:
    """
    Run the Problem Management analytics pass and write results to
    global_learned_patterns.json. Each entry is a Problem Management Candidate
    with causal chain, trend, and variance data.
    """
    candidates = compute_problem_management_candidates()

    if not dry_run and candidates:
        records = []
        for c in candidates:
            records.append({
                "id": f"pmc_{uuid.uuid4().hex[:10]}",
                "type": "problem_management_candidate",
                "knowledge_scope": "global_learned",
                "source": "analytics_engine",
                "classification": c["classification"],
                "title": c["classification"].replace("_", " ").title(),
                "domain": c.get("domain", ""),
                "sample_ticket": c.get("sample_ticket", ""),
                "occurrence_count": c["occurrence_count"],
                "current_period_count": c["current_period_count"],
                "prior_period_count": c["prior_period_count"],
                "trend": c["trend"],
                "trend_arrow": c["trend_arrow"],
                "avg_confidence": c["avg_confidence"],
                "confidence_stddev": c["confidence_stddev"],
                "variance": c["variance"],
                "mean_time_between_hours": c.get("mean_time_between_hours"),
                "first_seen": c["first_seen"],
                "last_seen": c["last_seen"],
                "upstream_precursors": c["upstream_precursors"],
                "downstream_impacts": c["downstream_impacts"],
                "has_causal_chain": c["has_causal_chain"],
                "recommendation": c["recommendation"],
                "problem_management_priority": c["problem_management_priority"],
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            })
        write_json(_GLOBAL_FILE, records, indent=2)

    high   = sum(1 for c in candidates if c["problem_management_priority"] == "high")
    medium = sum(1 for c in candidates if c["problem_management_priority"] == "medium")
    with_chain = sum(1 for c in candidates if c["has_causal_chain"])

    return {
        "total_candidates": len(candidates),
        "high_priority": high,
        "medium_priority": medium,
        "low_priority": len(candidates) - high - medium,
        "with_causal_chain": with_chain,
        "dry_run": dry_run,
        "timestamp": utc_now_iso(),
        "thresholds": {
            "min_occurrences": MIN_OCCURRENCES,
            "correlation_window_minutes": CORRELATION_WINDOW_MIN,
            "min_hit_rate": MIN_CORRELATION_HIT_RATE,
            "trend_window_days": TREND_WINDOW_DAYS,
        },
    }
