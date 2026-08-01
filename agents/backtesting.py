from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks

router = APIRouter(tags=["backtesting"])

DEFAULT_TENANT_ID = os.getenv("AIOPS_DEFAULT_TENANT_ID", "client_a")
HISTORY_FILE = Path("data/backtesting/results/backtest_history.jsonl")
_HISTORY_KEEP = 90  # keep up to 90 daily snapshots


def _append_history_snapshot(summary: Dict[str, Any], run_at: Optional[str] = None) -> None:
    """Append a summary snapshot to the rolling history JSONL file."""
    entry = {
        "run_at": run_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tenant_id": DEFAULT_TENANT_ID,
        "average_score": summary.get("average_score"),
        "classification_accuracy": summary.get("classification_accuracy"),
        "core_correlation_pass_rate": summary.get("core_correlation_pass_rate"),
        "rca_quality_average": summary.get("rca_quality_average"),
        "case_count": summary.get("case_count"),
    }
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        # Trim to last _HISTORY_KEEP lines
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > _HISTORY_KEEP:
            HISTORY_FILE.write_text("\n".join(lines[-_HISTORY_KEEP:]) + "\n", encoding="utf-8")
    except Exception:
        pass


@router.post("/backtesting/run")
def backtesting_run_route() -> Dict[str, Any]:
    """MF-06: Trigger backtesting run using the default cases file for the active tenant."""
    try:
        from backtesting_engine import run_backtest, DEFAULT_BACKEND_URL  # type: ignore[attr-defined]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"backtesting_engine not available: {e}")

    cases_file = Path("data/backtesting/client_a_backtest_cases.json")
    if not cases_file.exists():
        raise HTTPException(status_code=404, detail="Default backtest cases file not found: data/backtesting/client_a_backtest_cases.json")

    output_file = Path("data/backtesting/results/client_a_backtest_results.json")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    pattern_file = Path("data/enterprise_correlation_patterns.json")

    try:
        result = run_backtest(
            cases_file=cases_file,
            output_file=output_file,
            tenant_id=DEFAULT_TENANT_ID,
            knowledge_scope="client_private",
            backend_url=DEFAULT_BACKEND_URL,
            offline=True,
            pattern_file=pattern_file,
            include_raw=False,
            max_cases=20,
            quiet_progress=True,
        )
        summary = result.get("summary", {})
        _append_history_snapshot(summary)
        return {
            "status": "ok",
            "output_file": str(output_file),
            "summary": summary,
            "total_cases": result.get("total_cases", 0),
            "results_status": result.get("status", "completed"),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/backtesting/results")
def backtesting_results_route() -> Dict[str, Any]:
    """MF-06: Return the latest backtesting results."""
    results_file = Path("data/backtesting/results/client_a_backtest_results.json")
    if not results_file.exists():
        return {"status": "no_results", "message": "No backtesting results available. Run a backtest first."}
    try:
        data = json.loads(results_file.read_text(encoding="utf-8"))
        return {"status": "ok", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read results: {e}")


@router.get("/backtesting/history")
def backtesting_history_route(limit: int = 30) -> Dict[str, Any]:
    """Return the rolling history of backtest summaries (up to limit entries, newest last)."""
    if not HISTORY_FILE.exists():
        return {"status": "no_history", "entries": [], "count": 0}
    try:
        lines = [l.strip() for l in HISTORY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        entries: List[Dict[str, Any]] = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        entries = entries[-limit:]
        return {"status": "ok", "entries": entries, "count": len(entries)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read history: {e}")


@router.get("/backtesting/scheduler/status")
def backtesting_scheduler_status_route() -> Dict[str, Any]:
    """Return current scheduler state (last run, outcome, next scheduled time)."""
    state_file = Path("data/backtesting/scheduler_state.json")
    if not state_file.exists():
        return {"status": "no_scheduler_state", "message": "Scheduler has not run yet or state file missing."}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        return {"status": "ok", "scheduler": state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read scheduler state: {e}")


# ---------------------------------------------------------------------------
# Client data backtest routes (multi-client pattern correlation)
# ---------------------------------------------------------------------------

CLIENT_BACKTEST_IN = Path("data/backtest/incidents_normalised.json")
CLIENT_BACKTEST_OUT = Path("data/backtest/backtest_report.json")
_backtest_running: Dict[str, Any] = {}


def _run_client_backtest_bg(limit: Optional[int], clients: Optional[List[str]]) -> None:
    _backtest_running["status"] = "running"
    _backtest_running["started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from backtest_run import run_backtest  # type: ignore
        run_backtest(limit=limit, client_filter=clients)
        _backtest_running["status"] = "completed"
        _backtest_running["completed_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        _backtest_running["status"] = "failed"
        _backtest_running["error"] = str(e)


@router.post("/backtesting/client/run")
def client_backtest_run_route(
    background_tasks: BackgroundTasks,
    limit: Optional[int] = None,
    clients: Optional[str] = None,
) -> Dict[str, Any]:
    """Trigger the multi-client pattern correlation backtest.

    Query params:
      limit  — cap number of incidents (default: all)
      clients — comma-separated client IDs to include (default: all)
    """
    if not CLIENT_BACKTEST_IN.exists():
        raise HTTPException(
            status_code=404,
            detail="Normalised incident file not found. Run tools/backtest_ingest.py first.",
        )
    if _backtest_running.get("status") == "running":
        return {"status": "already_running", "started_at": _backtest_running.get("started_at")}

    client_list = [c.strip() for c in clients.split(",")] if clients else None
    background_tasks.add_task(_run_client_backtest_bg, limit, client_list)
    return {
        "status": "started",
        "message": "Backtest running in background. Poll /backtesting/client/status for progress.",
        "limit": limit,
        "clients": client_list,
    }


@router.get("/backtesting/client/status")
def client_backtest_status_route() -> Dict[str, Any]:
    """Return current run status and last-completed report summary."""
    run_status = dict(_backtest_running) or {"status": "idle"}
    report_summary: Dict[str, Any] = {}
    if CLIENT_BACKTEST_OUT.exists():
        try:
            data = json.loads(CLIENT_BACKTEST_OUT.read_text(encoding="utf-8"))
            report_summary = {
                "generated_at": data.get("generated_at"),
                "total_incidents": data.get("total_incidents"),
                "clients": data.get("clients"),
                "summary": data.get("summary"),
                "shared_pattern_count": len(data.get("shared_patterns", [])),
            }
        except Exception:
            pass
    return {"status": "ok", "run": run_status, "last_report": report_summary}


@router.get("/backtesting/client/report")
def client_backtest_report_route(
    top_n: int = 20,
    min_clients: int = 2,
) -> Dict[str, Any]:
    """Return the full multi-client pattern correlation report.

    Query params:
      top_n       — return top N shared patterns (default 20)
      min_clients — only patterns shared across >= this many clients (default 2)
    """
    if not CLIENT_BACKTEST_OUT.exists():
        return {"status": "no_report", "message": "Run POST /backtesting/client/run first."}
    try:
        data = json.loads(CLIENT_BACKTEST_OUT.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read report: {e}")

    shared = [
        p for p in data.get("shared_patterns", [])
        if p.get("client_count", 0) >= min_clients
    ][:top_n]

    return {
        "status": "ok",
        "generated_at": data.get("generated_at"),
        "total_incidents": data.get("total_incidents"),
        "clients": data.get("clients"),
        "summary": data.get("summary"),
        "by_client": data.get("by_client", {}),
        "shared_patterns": shared,
        "resolution_stats": data.get("resolution_stats_by_classification", {}),
        "reassignment_stats": data.get("reassignment_stats_by_classification", {}),
    }


@router.get("/backtesting/client/ingest_status")
def client_ingest_status_route() -> Dict[str, Any]:
    """Return status of the normalised incident file."""
    if not CLIENT_BACKTEST_IN.exists():
        return {"status": "missing", "message": "Run tools/backtest_ingest.py to load client data."}
    try:
        data = json.loads(CLIENT_BACKTEST_IN.read_text(encoding="utf-8"))
        return {
            "status": "ok",
            "generated_at": data.get("generated_at"),
            "total": data.get("total"),
            "by_client": data.get("by_client"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read ingest file: {e}")


# ---------------------------------------------------------------------------
# Peer insights — fast lookup from pre-built index
# ---------------------------------------------------------------------------

PEER_INDEX_FILE = Path("data/backtest/peer_index.json")
_peer_index_cache: Dict[str, Any] = {}


def _load_peer_index() -> Dict[str, Any]:
    """Load peer index into memory once; reload if file is newer."""
    global _peer_index_cache
    if not PEER_INDEX_FILE.exists():
        return {}
    mtime = PEER_INDEX_FILE.stat().st_mtime
    if _peer_index_cache.get("_mtime") == mtime:
        return _peer_index_cache.get("index", {})
    try:
        data = json.loads(PEER_INDEX_FILE.read_text(encoding="utf-8"))
        _peer_index_cache = {"_mtime": mtime, "index": data.get("index", {}), "generated_at": data.get("generated_at")}
        return _peer_index_cache["index"]
    except Exception:
        return {}


def _anonymise_clients(peers: Dict[str, Any], exclude_client: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return peer list with real client IDs replaced by 'Peer N', sorted by count desc.
    The requesting client is excluded so they only see other environments.
    """
    filtered = {k: v for k, v in peers.items() if k != exclude_client}
    sorted_peers = sorted(filtered.items(), key=lambda x: -x[1].get("count", 0))
    result = []
    for idx, (cid, info) in enumerate(sorted_peers, 1):
        avg_rh = info.get("avg_resolve_hours")
        pct_ra = info.get("pct_with_reassignment")
        result.append({
            "peer_id": f"Peer {idx}",
            "count": info.get("count", 0),
            "resolved_count": info.get("resolved_count", 0),
            "avg_resolve_hours": avg_rh,
            "avg_resolve_label": f"{avg_rh:.1f}h" if avg_rh is not None else "—",
            "pct_with_reassignment": pct_ra,
            "top_categories": info.get("top_categories", [])[:3],
            "top_subcategories": info.get("top_subcategories", [])[:3],
            "resolution_snippets": info.get("resolution_snippets", [])[:3],
            "by_year": info.get("by_year", {}),
        })
    return result


def _timeline_summary(peers_anon: List[Dict], ref_year: Optional[int] = None) -> Dict[str, Any]:
    """Aggregate before/after counts relative to ref_year (default: current year)."""
    from datetime import datetime
    ref_year = ref_year or datetime.now().year
    before, after, same = 0, 0, 0
    year_totals: Dict[str, int] = {}
    for peer in peers_anon:
        for yr, cnt in (peer.get("by_year") or {}).items():
            yr_int = int(yr)
            year_totals[yr] = year_totals.get(yr, 0) + cnt
            if yr_int < ref_year:
                before += cnt
            elif yr_int > ref_year:
                after += cnt
            else:
                same += cnt
    return {
        "ref_year": ref_year,
        "before_count": before,
        "during_count": same,
        "after_count": after,
        "by_year": dict(sorted(year_totals.items())),
    }


def _extract_terms_api(text: str, max_terms: int = 12) -> List[str]:
    """Mirror of tools/build_peer_index.py::extract_terms — must stay in sync."""
    import re as _re
    _STOP = {
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
    _KEEP = {
        "cpu","ram","db","vm","os","ip","dns","vpn","ssl","tls","api","url",
        "k8s","aws","gcp","nfs","ntp","ldap","sso","mfa","iam","aks","eks",
        "pod","pvc","rca","sla","slo","sre",
    }
    _TOK = _re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9_\-\.]*[a-zA-Z0-9])?")
    tokens = _TOK.findall(str(text or "").lower())
    out, seen = [], set()
    for tok in tokens:
        base = _re.sub(r"\d+$", "", tok)
        key = base if len(base) >= 3 else tok
        if key in seen:
            continue
        seen.add(key)
        if key in _KEEP:
            out.append(key)
        elif key in _STOP or len(key) < 3 or key.isdigit():
            continue
        else:
            out.append(key)
        if len(out) >= max_terms:
            break
    return out


def _merge_peer_stats(
    per_client: Dict[str, Dict[str, Any]],
    token_entry: Dict[str, Any],
    weight: float = 1.0,
) -> None:
    """Accumulate weighted stats from one token entry into per_client dict."""
    for cid, info in token_entry.get("peers", {}).items():
        if cid not in per_client:
            per_client[cid] = {
                "weighted_count": 0.0,
                "raw_count": 0,
                "resolved_count": 0,
                "total_resolve_hours": 0.0,
                "reassignment_sum": 0.0,
                "reassignment_count": 0,
                "top_categories": {},
                "resolution_snippets": [],
                "by_year": {},
                "token_hits": 0,
            }
        acc = per_client[cid]
        cnt = info.get("count", 0)
        acc["weighted_count"] += cnt * weight
        acc["raw_count"] += cnt
        acc["token_hits"] += 1
        rh = info.get("avg_resolve_hours")
        rc = info.get("resolved_count", 0)
        if rh is not None and rc:
            acc["resolved_count"] += rc
            acc["total_resolve_hours"] += rh * rc
        pct_ra = info.get("pct_with_reassignment")
        if pct_ra is not None:
            acc["reassignment_sum"] += pct_ra * cnt
            acc["reassignment_count"] += cnt
        for cat, cat_cnt in (info.get("top_categories") or []):
            acc["top_categories"][cat] = acc["top_categories"].get(cat, 0) + cat_cnt
        for snip in (info.get("resolution_snippets") or []):
            if snip not in acc["resolution_snippets"] and len(acc["resolution_snippets"]) < 5:
                acc["resolution_snippets"].append(snip)
        for yr, yr_cnt in (info.get("by_year") or {}).items():
            acc["by_year"][yr] = acc["by_year"].get(yr, 0) + yr_cnt


def _finalise_peer(cid: str, acc: Dict[str, Any]) -> Dict[str, Any]:
    avg_rh = (
        round(acc["total_resolve_hours"] / acc["resolved_count"], 1)
        if acc["resolved_count"] else None
    )
    pct_ra = (
        round(acc["reassignment_sum"] / acc["reassignment_count"], 1)
        if acc["reassignment_count"] else None
    )
    top_cats = sorted(acc["top_categories"].items(), key=lambda x: -x[1])[:4]
    return {
        "client_id": cid,
        "weighted_count": round(acc["weighted_count"]),
        "count": acc["raw_count"],
        "resolved_count": acc["resolved_count"],
        "avg_resolve_hours": avg_rh,
        "avg_resolve_label": f"{avg_rh:.1f}h" if avg_rh is not None else "—",
        "pct_with_reassignment": pct_ra,
        "top_categories": top_cats,
        "resolution_snippets": acc["resolution_snippets"][:3],
        "by_year": dict(sorted(acc["by_year"].items())),
        "token_hits": acc["token_hits"],
    }


@router.get("/backtesting/client/peer_insights")
def peer_insights_route(
    incident_text: Optional[str] = None,
    classification: Optional[str] = None,
    client_id: Optional[str] = None,
    ref_year: Optional[int] = None,
    max_peers: int = 5,
) -> Dict[str, Any]:
    """Return anonymised peer benchmark matched by direct text pattern matching.

    Query params:
      incident_text  — raw incident short_description (or any free text) to match against
      classification — fallback label if incident_text not supplied (used as search text)
      client_id      — requesting client to exclude from peer list (default: none)
      ref_year       — reference year for before/after timeline (default: current year)
      max_peers      — max peer environments to return (default 5)
    """
    index = _load_peer_index()
    if not index:
        return {
            "status": "index_missing",
            "message": "Peer index not built yet. Run tools/build_peer_index.py.",
        }

    # Use incident_text if provided, else fall back to classification label as search text
    search_text = incident_text or classification or ""
    if not search_text.strip():
        return {"status": "no_data", "message": "Provide incident_text or classification."}

    terms = _extract_terms_api(search_text, max_terms=12)
    if not terms:
        return {"status": "no_data", "message": "No meaningful terms found in incident text."}

    # Score: each matched token contributes proportionally to how specific it is
    # (rarer tokens = higher IDF-like weight)
    total_index_count = sum(v.get("total", 0) for v in index.values()) or 1
    per_client: Dict[str, Dict[str, Any]] = {}
    matched_terms: List[str] = []

    for term in terms:
        entry = index.get(term)
        if not entry:
            continue
        matched_terms.append(term)
        # IDF-style weight: log(total / term_total + 1)
        import math
        term_total = entry.get("total", 1)
        weight = math.log(total_index_count / term_total + 1)
        _merge_peer_stats(per_client, entry, weight=weight)

    if not per_client:
        return {
            "status": "no_data",
            "search_terms": terms,
            "matched_terms": [],
            "message": "No peer incidents match the terms extracted from the incident text.",
        }

    # Finalise and exclude requesting client
    all_peers = [_finalise_peer(cid, acc) for cid, acc in per_client.items() if cid != client_id]
    all_peers.sort(key=lambda p: -p["weighted_count"])

    # Anonymise
    peers_anon: List[Dict[str, Any]] = []
    for idx, p in enumerate(all_peers[:max_peers], 1):
        p_out = dict(p)
        p_out["peer_id"] = f"Peer {idx}"
        del p_out["client_id"]
        peers_anon.append(p_out)

    timeline = _timeline_summary(peers_anon, ref_year=ref_year)

    total_peer_incidents = sum(p["count"] for p in peers_anon)
    rh_vals = [p["avg_resolve_hours"] for p in peers_anon if p.get("avg_resolve_hours") is not None]
    overall_avg_rh = round(sum(rh_vals) / len(rh_vals), 1) if rh_vals else None

    return {
        "status": "ok",
        "search_terms": terms,
        "matched_terms": matched_terms,
        "peer_environments": len(peers_anon),
        "total_peer_incidents": total_peer_incidents,
        "overall_avg_resolve_hours": overall_avg_rh,
        "index_generated_at": _peer_index_cache.get("generated_at"),
        "peers": peers_anon,
        "timeline": timeline,
    }


# ---------------------------------------------------------------------------
# Self-history — same client's own past incidents matched by text pattern
# ---------------------------------------------------------------------------

_self_index_cache: Dict[str, Any] = {}


def _load_self_index(client_id: str) -> Dict[str, Any]:
    """Load the client's own self-history index; reload if file changed."""
    global _self_index_cache
    idx_file = Path(f"data/backtest/self_index_{client_id}.json")
    if not idx_file.exists():
        return {}
    mtime = idx_file.stat().st_mtime
    cached = _self_index_cache.get(client_id, {})
    if cached.get("_mtime") == mtime:
        return cached
    try:
        data = json.loads(idx_file.read_text(encoding="utf-8"))
        data["_mtime"] = mtime
        _self_index_cache[client_id] = data
        return data
    except Exception:
        return {}


def _cls_from_text(text: str) -> str:
    """Lightweight keyword classifier — shared by self_history and temporal_patterns routes."""
    t = str(text or "").lower()
    rules = [
        ("vpn_client_failure",           ["vpn", "cisco anyconnect", "remote access", "tunnel"]),
        ("certificate_expiry_warning",   ["certificate", "cert expir", "ssl expir", "x509"]),
        ("high_disk_usage",              ["disk", "storage full", "filesystem full", "disk space"]),
        ("cpu_saturation",               ["cpu", "processor", "high utilization", "cpu spike"]),
        ("memory_pressure",              ["memory", "ram", "heap", "out of memory", "oom"]),
        ("database_performance_issue",   ["database", "db slow", "sql slow", "oracle", "db2", "postgres", "mysql", "query slow", "deadlock", "connection pool"]),
        ("network_connectivity_issue",   ["network", "connectivity", "ping", "latency", "packet loss", "unreachable", "timeout", "firewall", "route"]),
        ("authentication_failure",       ["authentication", "auth fail", "login fail", "password", "credential", "ldap", "kerberos", "mfa", "sso"]),
        ("dns_resolution_failure",       ["dns", "name resolution", "dns fail"]),
        ("load_balancer_issue",          ["load balancer", "f5", "nginx", "haproxy", "upstream", "backend pool", "health check"]),
        ("application_error",            ["application", "app error", "500 error", "exception", "crash"]),
        ("batch_job_failure",            ["batch", "job fail", "scheduled task", "cron", "etl fail", "abend"]),
        ("file_transfer_failure",        ["ftp", "sftp", "file transfer", "transfer fail"]),
        ("email_delivery_failure",       ["email", "mail", "smtp", "exchange", "outlook", "mail flow"]),
        ("api_gateway_timeout",          ["api gateway", "api timeout", "endpoint timeout", "http timeout", "503", "504"]),
        ("kubernetes_issue",             ["kubernetes", "k8s", "pod", "container", "helm"]),
        ("active_directory_issue",       ["active directory", " ad ", "group policy", "gpo", "domain controller"]),
        ("vdi_citrix_connectivity_issue",["vdi", "citrix", "virtual desktop"]),
    ]
    for cls, keywords in rules:
        if any(kw in t for kw in keywords):
            return cls
    return ""


@router.get("/backtesting/client/self_history")
def self_history_route(
    incident_text: Optional[str] = None,
    classification: Optional[str] = None,
    client_id: str = "client_a",
    ref_year: Optional[int] = None,
    max_tokens: int = 10,
) -> Dict[str, Any]:
    """Return the requesting client's OWN historical pattern matches.

    Looks up the client's self-history index (built from their own past incidents)
    and returns token-matched history, classification breakdown, resolution stats,
    and year-over-year timeline — all from the same client's records.

    Query params:
      incident_text  — raw incident description to match
      classification — fallback if no text (used as search text)
      client_id      — the client whose own history to search
      ref_year       — reference year for before/after split
      max_tokens     — number of top matching tokens to aggregate over
    """
    data = _load_self_index(client_id)
    if not data:
        return {
            "status": "index_missing",
            "message": f"Self-history index not built for {client_id}. Run tools/build_self_index.py.",
        }

    token_index = data.get("token_index", {})
    cls_index = data.get("classification_index", {})
    total_cases = data.get("total_cases", 0)

    search_text = incident_text or classification or ""
    if not search_text.strip():
        return {"status": "no_data", "message": "Provide incident_text or classification."}

    terms = _extract_terms_api(search_text, max_terms=12)
    if not terms:
        return {"status": "no_data", "message": "No meaningful terms extracted."}

    # Score tokens by IDF weight, accumulate history
    import math
    total_tok_count = sum(v.get("count", 0) for v in token_index.values()) or 1

    matched_terms: List[str] = []
    agg: Dict[str, Any] = {
        "count": 0,
        "resolved_count": 0,
        "total_resolve_hours": 0.0,
        "by_year": {},
        "classifications": {},
        "resolution_snippets": [],
    }

    for term in terms[:max_tokens]:
        entry = token_index.get(term)
        if not entry:
            continue
        matched_terms.append(term)
        weight = math.log(total_tok_count / (entry.get("count", 1)) + 1)
        cnt = entry.get("count", 0)
        agg["count"] += cnt
        rc = entry.get("resolved_count", 0)
        rh = entry.get("avg_resolve_hours")
        if rc and rh is not None:
            agg["resolved_count"] += rc
            agg["total_resolve_hours"] += rh * rc
        for yr, c in (entry.get("by_year") or {}).items():
            agg["by_year"][yr] = agg["by_year"].get(yr, 0) + c
        for cls, c in (entry.get("top_classifications") or []):
            agg["classifications"][cls] = agg["classifications"].get(cls, 0) + c
        for snip in (entry.get("resolution_snippets") or []):
            if snip not in agg["resolution_snippets"] and len(agg["resolution_snippets"]) < 5:
                agg["resolution_snippets"].append(snip)

    if not matched_terms:
        # Fallback: try direct classification-name lookup in cls_index
        # This catches cases where tokens are pruned due to low frequency (small dataset)
        fallback_cls = _cls_from_text(search_text)  # reuse classifier
        if not fallback_cls:
            # Try matching search words directly against classification names
            search_lower = search_text.lower().replace(" ", "_").replace("-", "_")
            for cls_name in cls_index:
                if search_lower in cls_name or cls_name in search_lower:
                    fallback_cls = cls_name
                    break
        if fallback_cls and fallback_cls in cls_index:
            ci = cls_index[fallback_cls]
            ci_avg_rh = ci.get("avg_resolve_hours")
            from datetime import datetime as _dt
            ref_year_fb = ref_year or _dt.now().year
            by_year_fb = ci.get("by_year", {})
            before_fb = sum(c for yr, c in by_year_fb.items() if int(yr) < ref_year_fb)
            during_fb = sum(c for yr, c in by_year_fb.items() if int(yr) == ref_year_fb)
            return {
                "status": "ok",
                "client_id": client_id,
                "search_terms": terms,
                "matched_terms": [fallback_cls],
                "total_own_cases": total_cases,
                "matched_incident_count": ci.get("count", 0),
                "avg_resolve_hours": ci_avg_rh,
                "avg_resolve_label": f"{ci_avg_rh:.1f}h" if ci_avg_rh is not None else "—",
                "resolution_snippets": ci.get("resolution_snippets", []),
                "top_classifications": [{
                    "classification": fallback_cls,
                    "historical_count": ci.get("count", 0),
                    "token_hits": 1,
                    "avg_resolve_hours": ci_avg_rh,
                    "avg_resolve_label": f"{ci_avg_rh:.1f}h" if ci_avg_rh is not None else "—",
                    "resolution_snippets": ci.get("resolution_snippets", [])[:3],
                    "sample_titles": ci.get("sample_titles", [])[:3],
                    "by_year": by_year_fb,
                }],
                "timeline": {
                    "ref_year": ref_year_fb,
                    "by_year": by_year_fb,
                    "before_count": before_fb,
                    "during_count": during_fb,
                    "after_count": 0,
                },
                "index_generated_at": data.get("generated_at"),
                "fallback_used": True,
            }
        return {
            "status": "no_data",
            "search_terms": terms,
            "matched_terms": [],
            "message": "No self-history matches found for this incident text.",
        }

    avg_rh = (
        round(agg["total_resolve_hours"] / agg["resolved_count"], 1)
        if agg["resolved_count"] else None
    )

    # Classification breakdown — top matching classifications with full stats
    top_cls = sorted(agg["classifications"].items(), key=lambda x: -x[1])[:6]
    cls_detail = []
    for cls, tok_hits in top_cls:
        ci = cls_index.get(cls, {})
        ci_avg_rh = ci.get("avg_resolve_hours")
        cls_detail.append({
            "classification": cls,
            "historical_count": ci.get("count", 0),
            "token_hits": tok_hits,
            "avg_resolve_hours": ci_avg_rh,
            "avg_resolve_label": f"{ci_avg_rh:.1f}h" if ci_avg_rh is not None else "—",
            "resolution_snippets": ci.get("resolution_snippets", [])[:3],
            "sample_titles": ci.get("sample_titles", [])[:3],
            "by_year": ci.get("by_year", {}),
        })

    # Timeline
    from datetime import datetime as _dt
    ref_year = ref_year or _dt.now().year
    by_year = dict(sorted(agg["by_year"].items()))
    before = sum(c for yr, c in by_year.items() if int(yr) < ref_year)
    during = sum(c for yr, c in by_year.items() if int(yr) == ref_year)
    after = sum(c for yr, c in by_year.items() if int(yr) > ref_year)

    return {
        "status": "ok",
        "client_id": client_id,
        "search_terms": terms,
        "matched_terms": matched_terms,
        "total_own_cases": total_cases,
        "matched_incident_count": agg["count"],
        "avg_resolve_hours": avg_rh,
        "avg_resolve_label": f"{avg_rh:.1f}h" if avg_rh is not None else "—",
        "resolution_snippets": agg["resolution_snippets"],
        "top_classifications": cls_detail,
        "timeline": {
            "ref_year": ref_year,
            "by_year": by_year,
            "before_count": before,
            "during_count": during,
            "after_count": after,
        },
        "index_generated_at": data.get("generated_at"),
    }


# ---------------------------------------------------------------------------
# Temporal correlation — before/after pattern chains
# ---------------------------------------------------------------------------

TEMPORAL_INDEX_FILE = Path("data/backtest/temporal_index.json")
_temporal_index_cache: Dict[str, Any] = {}


def _load_temporal_index() -> Dict[str, Any]:
    global _temporal_index_cache
    if not TEMPORAL_INDEX_FILE.exists():
        return {}
    mtime = TEMPORAL_INDEX_FILE.stat().st_mtime
    if _temporal_index_cache.get("_mtime") == mtime:
        return _temporal_index_cache.get("index", {})
    try:
        data = json.loads(TEMPORAL_INDEX_FILE.read_text(encoding="utf-8"))
        _temporal_index_cache = {"_mtime": mtime, "index": data.get("index", {}), "generated_at": data.get("generated_at")}
        return _temporal_index_cache["index"]
    except Exception:
        return {}


@router.get("/backtesting/client/temporal_patterns")
def temporal_patterns_route(
    incident_text: Optional[str] = None,
    classification: Optional[str] = None,
) -> Dict[str, Any]:
    """Return before/after temporal correlation patterns for an incident type.

    Looks up the temporal index (built from 169k dated peer incidents) to show
    what incident types typically precede and follow the given classification.

    Query params:
      incident_text  — raw incident description (auto-classified)
      classification — explicit classification key (overrides auto-classification)
    """
    index = _load_temporal_index()
    if not index:
        return {
            "status": "index_missing",
            "message": "Temporal index not built. Run tools/build_temporal_index.py.",
        }

    # Resolve classification
    cls_key = classification or ""
    if not cls_key and incident_text:
        cls_key = _cls_from_text(incident_text)

    if not cls_key:
        return {"status": "no_data", "message": "Could not determine classification from input."}

    entry = index.get(cls_key)

    # Fuzzy fallback
    if not entry:
        for k in index:
            if cls_key in k or k in cls_key:
                entry = index[k]
                cls_key = k
                break

    if not entry:
        return {
            "status": "no_data",
            "classification": cls_key,
            "message": f"No temporal correlation data for '{cls_key}'.",
        }

    return {
        "status": "ok",
        "classification": cls_key,
        "total_incidents": entry.get("total", 0),
        "before_patterns": entry.get("before_patterns", []),
        "after_patterns":  entry.get("after_patterns", []),
        "before_categories": entry.get("before_categories", []),
        "after_categories":  entry.get("after_categories", []),
        "index_generated_at": _temporal_index_cache.get("generated_at"),
    }
