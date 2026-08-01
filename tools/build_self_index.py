"""
build_self_index.py — build a per-client self-history index for same-client backtesting.

Reads client_a's own historical incident cases (from generated backtest cases)
and builds a token-based index identical in structure to the peer index, but
keyed by classification and year — so when a live incident arrives it can be
matched against that client's OWN past incidents.

Output: data/backtest/self_index_{client_id}.json

Run:
    python3 tools/build_self_index.py
    python3 tools/build_self_index.py --client client_a
"""
from __future__ import annotations
import argparse, json, re, pathlib, sys, time
from collections import defaultdict, Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Source files for each client's own historical data
_SELF_SOURCES: Dict[str, List[pathlib.Path]] = {
    "client_a": [
        ROOT / "data/clients/client_a/processed/backtesting/client_a_backtest_cases_generated.json",
        ROOT / "data/clients/client_a/processed/backtesting/client_a_postgres_114_class_benchmark.json",
    ]
}

# ---------------------------------------------------------------------------
# Tokeniser (mirror of build_peer_index.py)
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
    "thursday","friday","saturday","sunday","number","description",
}
_FORCE_KEEP = {
    "cpu","ram","db","vm","os","ip","dns","vpn","ssl","tls","api","url",
    "k8s","aws","gcp","nfs","ntp","ldap","sso","mfa","iam","aks","eks",
    "pod","pvc","rca","sla","slo","sre","mq","ftp","sftp","smb","nfs",
}
_TOKEN_RE = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9_\-\.]*[a-zA-Z0-9])?")


def extract_terms(text: str, max_terms: int = 15) -> List[str]:
    tokens = _TOKEN_RE.findall(str(text or "").lower())
    out, seen = [], set()
    for tok in tokens:
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


_SYNTHETIC_RESOLVED = "2026-04-17 22:21:52"  # shared timestamp across 114 benchmark cases

def _resolve_hours(opened: str, resolved: str) -> Optional[float]:
    if not opened or not resolved:
        return None
    # Exclude the shared synthetic resolved_at from benchmark cases
    if str(resolved).startswith(_SYNTHETIC_RESOLVED):
        return None
    try:
        o = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        r = datetime.fromisoformat(str(resolved).replace("Z", "+00:00"))
        h = (r - o).total_seconds() / 3600
        return round(h, 2) if 0 < h < 8760 else None
    except Exception:
        return None


def _snippet(text: str, max_len: int = 120) -> str:
    s = re.sub(r"\s+", " ", str(text or "").strip())
    # Strip "Number: INC... | " prefixes common in ServiceNow exports
    s = re.sub(r"^Number:\s*\S+\s*\|[^|]+\|\s*", "", s)
    s = re.sub(r"^Description:\s*", "", s)
    return (s[:max_len] + "…") if len(s) > max_len else s


def _parse_year(d: str) -> Optional[str]:
    if not d:
        return None
    try:
        return str(datetime.fromisoformat(str(d).replace("Z", "+00:00")).year)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Load cases from backtest case files
# ---------------------------------------------------------------------------

def _load_cases(sources: List[pathlib.Path]) -> List[Dict[str, Any]]:
    cases = []
    seen_ids = set()
    for src in sources:
        if not src.exists():
            print(f"  SKIP (not found): {src}")
            continue
        d = json.loads(src.read_text(encoding="utf-8"))
        raw = d.get("cases", d) if isinstance(d, dict) else d
        if not isinstance(raw, list):
            print(f"  SKIP (unexpected format): {src}")
            continue
        added = 0
        for c in raw:
            cid = c.get("case_id") or c.get("id") or str(id(c))
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            cases.append(c)
            added += 1
        print(f"  Loaded {added} cases from {src.name}")
    return cases


# ---------------------------------------------------------------------------
# Build index
# ---------------------------------------------------------------------------

def build_self_index(client_id: str = "client_a") -> None:
    sources = _SELF_SOURCES.get(client_id)
    if not sources:
        sys.exit(f"No source files configured for client '{client_id}'")

    out_file = ROOT / "data" / "backtest" / f"self_index_{client_id}.json"

    print(f"Building self-history index for {client_id} …")
    cases = _load_cases(sources)
    if not cases:
        sys.exit("No cases loaded — check source file paths.")

    print(f"Processing {len(cases)} historical cases …")

    # Two structures:
    # 1. Token index: token → accumulated stats (same shape as peer_index)
    # 2. Classification index: classification → {count, cases[], by_year{}}
    token_index: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "resolved_count": 0,
        "total_resolve_hours": 0.0,
        "resolution_snippets": [],
        "by_year": Counter(),
        "classifications": Counter(),
    })

    cls_index: Dict[str, Any] = defaultdict(lambda: {
        "count": 0,
        "resolved_count": 0,
        "total_resolve_hours": 0.0,
        "by_year": Counter(),
        "resolution_snippets": [],
        "sample_titles": [],
    })

    t0 = time.time()
    for case in cases:
        inp = case.get("input", {}) or {}
        pr = case.get("primary_record", {}) or {}
        exp = case.get("expected", {}) or {}

        # Text to tokenise: prefer primary_record short_description, fall back to input title
        sd = (
            pr.get("short_description")
            or pr.get("description")
            or inp.get("ticket_text")
            or inp.get("title")
            or ""
        )
        # Strip "Number: INC... |" prefix noise
        sd_clean = re.sub(r"^Number:\s*\S+[^|]*\|[^|]*\|\s*Description:\s*", "", sd).strip()
        if not sd_clean:
            sd_clean = sd

        terms = extract_terms(sd_clean)
        if not terms:
            continue

        classification = exp.get("classification", "unknown")
        opened_at = pr.get("opened_at") or inp.get("date_created") or ""
        resolved_at = pr.get("resolved_at") or ""
        rh = _resolve_hours(opened_at, resolved_at)
        yr = _parse_year(opened_at)
        res_notes = pr.get("resolution_notes") or exp.get("resolution_action") or ""

        # Token index
        for term in terms:
            acc = token_index[term]
            acc["count"] += 1
            if rh is not None:
                acc["resolved_count"] += 1
                acc["total_resolve_hours"] += rh
            if yr:
                acc["by_year"][yr] += 1
            if classification:
                acc["classifications"][classification] += 1
            if res_notes and len(acc["resolution_snippets"]) < 4:
                s = _snippet(res_notes)
                if s and s not in acc["resolution_snippets"]:
                    acc["resolution_snippets"].append(s)

        # Classification index
        ca = cls_index[classification]
        ca["count"] += 1
        if rh is not None:
            ca["resolved_count"] += 1
            ca["total_resolve_hours"] += rh
        if yr:
            ca["by_year"][yr] += 1
        if res_notes and len(ca["resolution_snippets"]) < 5:
            s = _snippet(res_notes)
            if s and s not in ca["resolution_snippets"]:
                ca["resolution_snippets"].append(s)
        title = _snippet(sd_clean, 80)
        if title and len(ca["sample_titles"]) < 6 and title not in ca["sample_titles"]:
            ca["sample_titles"].append(title)

    elapsed = time.time() - t0
    print(f"Indexed {len(cases)} cases in {elapsed:.1f}s")

    # Prune token index — keep tokens with >= 2 hits (low bar, small dataset)
    pruned_tokens = {
        tok: acc for tok, acc in token_index.items()
        if acc["count"] >= 2
    }
    print(f"Token index: {len(token_index):,} raw → {len(pruned_tokens):,} after pruning (min 2)")

    # Serialise token index
    serial_tokens: Dict[str, Any] = {}
    for tok, acc in pruned_tokens.items():
        avg_rh = round(acc["total_resolve_hours"] / acc["resolved_count"], 1) if acc["resolved_count"] else None
        serial_tokens[tok] = {
            "count": acc["count"],
            "resolved_count": acc["resolved_count"],
            "avg_resolve_hours": avg_rh,
            "by_year": dict(sorted(acc["by_year"].items())),
            "top_classifications": acc["classifications"].most_common(4),
            "resolution_snippets": acc["resolution_snippets"],
        }

    # Serialise classification index
    serial_cls: Dict[str, Any] = {}
    for cls, acc in cls_index.items():
        avg_rh = round(acc["total_resolve_hours"] / acc["resolved_count"], 1) if acc["resolved_count"] else None
        serial_cls[cls] = {
            "count": acc["count"],
            "resolved_count": acc["resolved_count"],
            "avg_resolve_hours": avg_rh,
            "by_year": dict(sorted(acc["by_year"].items())),
            "resolution_snippets": acc["resolution_snippets"],
            "sample_titles": acc["sample_titles"],
        }

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps({
            "client_id": client_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_cases": len(cases),
            "token_index": serial_tokens,
            "classification_index": serial_cls,
        }, indent=2),
        encoding="utf-8",
    )
    size_kb = out_file.stat().st_size // 1024
    print(f"Written: {out_file}  ({size_kb}KB)")

    # Summary
    print("\nClassification breakdown:")
    for cls, info in sorted(serial_cls.items(), key=lambda x: -x[1]["count"]):
        avg = f"{info['avg_resolve_hours']:.1f}h" if info["avg_resolve_hours"] else "—"
        print(f"  {cls}: {info['count']} incidents  avg resolve: {avg}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="client_a")
    args = parser.parse_args()
    build_self_index(args.client)
