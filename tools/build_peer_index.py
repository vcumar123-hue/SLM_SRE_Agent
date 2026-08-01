"""
build_peer_index.py — build an inverted token index for peer pattern matching.

For every historical incident, extract meaningful terms from short_description
and index them as: token → {client_id → aggregated stats}.

On lookup (peer_insights endpoint) the live incident's tokens are extracted
the same way and matched directly against the index — no classifier involved.

Output: data/backtest/peer_index.json

Run:
    python3 tools/build_peer_index.py
"""
from __future__ import annotations
import json, re, pathlib, sys, time
from collections import defaultdict, Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
IN_FILE  = ROOT / "data" / "backtest" / "incidents_normalised.json"
OUT_FILE = ROOT / "data" / "backtest" / "peer_index.json"

# ---------------------------------------------------------------------------
# Tokeniser — extracts meaningful terms from incident text
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "is","was","are","were","be","been","being","have","has","had","do","does",
    "did","will","would","could","should","may","might","shall","can","not",
    "no","nor","so","yet","both","either","each","all","any","few","more",
    "most","other","some","such","than","then","too","very","just","also",
    "as","by","from","into","through","during","before","after","above",
    "below","up","down","out","off","over","under","again","further","that",
    "this","these","those","it","its","we","our","i","my","he","she","they",
    "them","their","you","your","if","when","where","which","who","how",
    "please","hi","hello","dear","thank","thanks","regards","per","re","fw",
    "inc","ticket","request","issue","problem","error","alert","event",
    "notification","resolve","resolved","close","closed","open","new",
    "assigned","pending","completed","status","update","updated","note",
    "client","customer","user","team","group","service","system","server",
    "impact","priority","p1","p2","p3","p4","high","medium","low","critical",
    "date","time","am","pm","est","utc","gmt","jan","feb","mar","apr","may",
    "jun","jul","aug","sep","oct","nov","dec","monday","tuesday","wednesday",
    "thursday","friday","saturday","sunday",
}

# Terms we DO want to keep even if short
_FORCE_KEEP = {
    "cpu","ram","db","vm","os","ip","dns","vpn","ssl","tls","api","url",
    "k8s","aws","gcp","nfs","ntp","ldap","sso","mfa","iam","aks","eks",
    "pod","pvc","rca","sla","slo","sre",
}

_TOKEN_RE = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9_\-\.]*[a-zA-Z0-9])?")


def extract_terms(text: str, max_terms: int = 12) -> List[str]:
    """Extract meaningful lowercase tokens from incident text."""
    tokens = _TOKEN_RE.findall(str(text or "").lower())
    out = []
    seen = set()
    for tok in tokens:
        # normalise: strip trailing digits (e.g. server01 → server)
        base = re.sub(r"\d+$", "", tok)
        key = base if len(base) >= 3 else tok
        if key in seen:
            continue
        seen.add(key)
        if key in _FORCE_KEEP:
            out.append(key)
        elif key in _STOPWORDS or len(key) < 3 or key.isdigit():
            continue
        else:
            out.append(key)
        if len(out) >= max_terms:
            break
    return out


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _resolve_hours(created: str, resolved: str) -> Optional[float]:
    if not created or not resolved:
        return None
    try:
        from datetime import datetime as dt
        c = dt.fromisoformat(str(created).replace("Z", "+00:00"))
        r = dt.fromisoformat(str(resolved).replace("Z", "+00:00"))
        h = (r - c).total_seconds() / 3600
        return round(h, 2) if 0 < h < 8760 else None
    except Exception:
        return None


def _snippet(text: str, max_len: int = 110) -> str:
    s = re.sub(r"\s+", " ", str(text or "").strip())
    return (s[:max_len] + "…") if len(s) > max_len else s


def _parse_year(d: str) -> Optional[str]:
    if not d:
        return None
    try:
        from datetime import datetime as dt
        return str(dt.fromisoformat(str(d).replace("Z", "+00:00")).year)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_index() -> None:
    if not IN_FILE.exists():
        sys.exit(f"ERROR: {IN_FILE} not found. Run backtest_ingest.py first.")

    print(f"Loading {IN_FILE} …")
    data = json.loads(IN_FILE.read_text(encoding="utf-8"))
    incidents: List[Dict] = data["incidents"]
    print(f"Indexing {len(incidents):,} incidents …")

    # token → client_id → accumulator
    index: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(lambda: {
        "count": 0,
        "resolved_count": 0,
        "total_resolve_hours": 0.0,
        "reassignment_sum": 0,
        "reassignment_count": 0,
        "categories": Counter(),
        "resolution_snippets": [],
        "by_year": Counter(),
    }))

    t0 = time.time()
    for i, inc in enumerate(incidents):
        if i and i % 100_000 == 0:
            elapsed = time.time() - t0
            print(f"  {i:,}/{len(incidents):,}  ({i/elapsed:.0f}/s)")

        sd = str(inc.get("short_description") or "").strip()
        if not sd:
            continue

        terms = extract_terms(sd)
        if not terms:
            continue

        cid = inc.get("client_id", "unknown")
        rh  = _resolve_hours(inc.get("created_at"), inc.get("resolved_at"))
        yr  = _parse_year(inc.get("created_at"))
        cat = str(inc.get("category") or "").strip()
        notes = str(inc.get("resolution_notes") or "").strip()

        try:
            rc = int(float(inc["reassignment_count"])) if inc.get("reassignment_count") is not None else None
        except Exception:
            rc = None

        for term in terms:
            acc = index[term][cid]
            acc["count"] += 1
            if rh is not None:
                acc["resolved_count"] += 1
                acc["total_resolve_hours"] += rh
            if rc is not None:
                acc["reassignment_sum"] += rc
                acc["reassignment_count"] += 1
            if cat:
                acc["categories"][cat] += 1
            if yr:
                acc["by_year"][yr] += 1
            if notes and len(acc["resolution_snippets"]) < 5:
                s = _snippet(notes)
                if s and s not in acc["resolution_snippets"]:
                    acc["resolution_snippets"].append(s)

    elapsed_total = time.time() - t0
    print(f"Indexed {len(incidents):,} in {elapsed_total:.1f}s  |  unique tokens: {len(index):,}")

    # Prune: drop tokens with fewer than 10 total incidents (noise)
    MIN_COUNT = 10
    pruned = {
        tok: clients
        for tok, clients in index.items()
        if sum(c["count"] for c in clients.values()) >= MIN_COUNT
    }
    print(f"After pruning (min {MIN_COUNT}): {len(pruned):,} tokens")

    # Serialise
    out_index: Dict[str, Any] = {}
    for tok, clients in pruned.items():
        client_data: Dict[str, Any] = {}
        for cid, acc in clients.items():
            avg_rh = round(acc["total_resolve_hours"] / acc["resolved_count"], 1) if acc["resolved_count"] else None
            avg_ra = round(acc["reassignment_sum"] / acc["reassignment_count"], 2) if acc["reassignment_count"] else None
            pct_ra = round(100 * acc["reassignment_count"] / acc["count"], 1) if acc["count"] else None
            client_data[cid] = {
                "count": acc["count"],
                "resolved_count": acc["resolved_count"],
                "avg_resolve_hours": avg_rh,
                "avg_reassignments": avg_ra,
                "pct_with_reassignment": pct_ra,
                "top_categories": acc["categories"].most_common(4),
                "resolution_snippets": acc["resolution_snippets"],
                "by_year": dict(sorted(acc["by_year"].items())),
            }
        out_index[tok] = {
            "total": sum(c["count"] for c in client_data.values()),
            "peer_count": len(client_data),
            "peers": client_data,
        }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "index": out_index}, indent=2),
        encoding="utf-8",
    )
    print(f"Written: {OUT_FILE}  ({OUT_FILE.stat().st_size // 1024 // 1024}MB)")

    # Preview top tokens
    top = sorted(out_index.items(), key=lambda x: -x[1]["total"])[:12]
    print("\nTop tokens by incident count:")
    for tok, info in top:
        peers = ", ".join(f"{c}:{d['count']:,}" for c, d in info["peers"].items())
        print(f"  '{tok}': {info['total']:,} total [{peers}]")


if __name__ == "__main__":
    build_index()
