"""
The official recording-capture command (design 12, 20.16).

A planner experiment replays recorded tool outputs, so every planner sees the same data. This module makes that recording. It is the only way a recording
of record is produced, and it is built to fail closed:

  * the real tools are called through the real registry and a trusted tenant context (the tenant comes from the case, never from the ticket text);
  * every held-out ticket, its text and its cluster are removed **before** anything is recorded (exclusion.py), and the exclusion is proven: the matcher
    must first recognise every held-out ticket as its own evidence (self-test), every case's own ticket must be covered, no item may be undecidable, and the
    stored (sanitised) form is checked again. If any of that fails the capture stops and writes nothing usable. An unfiltered result is never recorded;
  * every returned item is rechecked for the expected tenant (a foreign item aborts the capture, it is not merely dropped);
  * only sanitised excerpts and facts are stored, in a stable order, in a deterministic serialisation, so the same inputs give the same bytes;
  * the output is atomic: the recording and its manifest are written to temporary files and renamed; the manifest (which carries the recording's SHA-256) is
    written last, so an interrupted capture can never look complete, and `verify_capture` accepts only a recording whose manifest is complete and matches;
  * an existing recording is never overwritten: replacing one needs an explicit, recorded invalidation (reason, who, hashes of what was replaced);
  * the recording holds tool output and the arguments' hashes, never the ticket's own text, the held-out records, or another tenant's evidence.

No key is needed for the development subset. The hidden-set capture (`mode="experiment"`) needs the active registered key; its digests are HMACs under that
key, and its manifest, like the case package, belongs on the key-holder side (outside the repository).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from agentruntime.context import build_context
from agentruntime.contracts import EvidenceDraft
from agentruntime.registry import BoundaryViolation, ToolBusy, ToolRegistry, ToolTimeout
from agentruntime.runner import EXCERPT_MAX, RUNTIME_VERSION, clean_obj
from agentruntime.sanitize import sanitize_text
from agentruntime.tools import TOOL_MODULES, build_default_registry
from agentruntime.tools.ticket_history import HOSTNAME_PATTERN
from planner_experiment import exclusion, freeze
from planner_experiment.exclusion import Matcher, Undecidable
from planner_experiment.scripted_env import Recording, args_key

CAPTURE_VERSION = "1"
MODES = ("development", "experiment")
TIMEOUT_S = 20.0
RETRIES = 1                       # a call that times out or is busy is tried once more; then the capture stops
HOST_CALLS = 3
CLASS_CALLS = 3
QUERY_CHARS = 300
_NO_CLASSIFICATION = {"unknown", "unclassified", "none", "other", "general"}
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,200}")
MANIFEST_SUFFIX = ".manifest.json"


class CaptureError(Exception):
    """The capture could not be completed or its exclusion could not be proven; nothing usable was written."""


class CaptureCancelled(CaptureError):
    """The capture was interrupted; the partial work was discarded."""


class CapturePrincipal:
    """The principal under which tools run during capture: it may act for exactly one tenant."""
    id = "planner-capture"

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant

    def allows_tenant(self, tenant: str) -> bool:
        return tenant == self.tenant


def real_registry() -> ToolRegistry:
    """The five production tools against the live systems (database, retrieval, ledger, evidence store)."""
    return build_default_registry()


def default_context(tenant: str, case_id: str):
    return build_context(tenant, case_id, CapturePrincipal(tenant), "batch")


# -- the calls recorded for a case ---------------------------------------------------------------------------------------------------

def host_candidates(case: Dict[str, Any]) -> List[str]:
    out: List[str] = []

    def add(value: Any) -> None:
        v = str(value or "").strip().lower()[:80]
        if len(v) >= 2 and re.fullmatch(HOSTNAME_PATTERN, v) and v not in out:
            out.append(v)

    host = str(case.get("host") or "")
    add(host)
    for part in re.split(r"[-_./\\]+", host):
        if any(ch.isdigit() for ch in part):
            add(part)
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*", str(case.get("ticket_text") or "")):
        add(word)
    return out[:HOST_CALLS]


def symptom_query(text: str, hosts: Iterable[str]) -> str:
    """The ticket's descriptive words with identifiers, numbers and host names removed (the usual reformulation)."""
    hosts = [h for h in hosts if h]
    words = []
    for word in re.split(r"[\s,;:()\[\]<>\"'=]+", text or ""):
        w = word.strip("-_./\\")
        if len(w) < 3 or any(ch.isdigit() for ch in w) or any(h in w.lower() for h in hosts):
            continue
        words.append(w)
    return " ".join(words)[:QUERY_CHARS]


def classification_of(draft: Dict[str, Any]) -> Optional[str]:
    raw = str((draft.get("facts") or {}).get("classification") or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    return raw if re.fullmatch(r"[a-z0-9_]{2,80}", raw) and raw not in _NO_CLASSIFICATION else None


# -- one call -------------------------------------------------------------------------------------------------------------------------

def _digest(key: Optional[bytes], tag: str, data: str) -> str:
    payload = f"{tag}\0{data}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest() if key else hashlib.sha256(payload).hexdigest()


def _safe_source_id(source_id: str) -> str:
    """Ids are kept (the runner de-duplicates on them); one that is not plainly safe is replaced by a stable, unique token."""
    sid = str(source_id)
    if _SAFE_ID.fullmatch(sid) and sanitize_text(sid, 200)[0] == sid:
        return sid
    return "src_" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]


def _sanitised_row(draft: EvidenceDraft) -> Dict[str, Any]:
    excerpt = sanitize_text(draft.excerpt, EXCERPT_MAX)[0]
    facts = clean_obj(draft.facts, 200) if draft.facts is not None else None
    relevance = None if draft.relevance is None else round(float(draft.relevance), 6)
    row = draft.model_copy(update={"excerpt": excerpt, "facts": facts, "relevance": relevance,
                                   "source_id": _safe_source_id(draft.source_id)}).model_dump(mode="json")
    return dict(sorted(row.items()))


def _order(tool: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if tool == "incident_kb_search":
        return sorted(rows, key=lambda r: (-(r.get("relevance") or 0.0), r["source_id"]))
    if tool == "ticket_history_lookup":
        return sorted(rows, key=lambda r: (str(r.get("data_timestamp") or ""), r["source_id"]), reverse=True)
    return rows


def _row_hash(row: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def _call(registry: ToolRegistry, ctx, tool: str, args: Dict[str, Any], matcher: Matcher, tenant: str, timeout_s: float,
          key: Optional[bytes]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run one tool call; return the sanitised rows to record (held-out items removed) and the exclusion proof for the call."""
    drafts = None
    last: Optional[Exception] = None
    for _attempt in range(RETRIES + 1):
        try:
            drafts = registry.call(ctx, tool, args, timeout_s)
            break
        except (ToolTimeout, ToolBusy) as exc:
            last = exc
        except BoundaryViolation as exc:
            raise CaptureError(f"{tool}: a record of another tenant was returned; the capture is stopped ({exc})") from exc
        except Exception as exc:  # noqa: BLE001 - any failure stops the capture; nothing is recorded around it
            raise CaptureError(f"{tool}: the call failed ({type(exc).__name__}: {exc})") from exc
    if drafts is None:
        raise CaptureError(f"{tool}: the call did not complete after {RETRIES + 1} attempts ({last})")
    kept: List[Dict[str, Any]] = []
    dropped = {"ticket_id": 0, "text": 0, "cluster": 0}
    dropped_ids: List[str] = []
    for d in drafts:
        if d.tenant_id != tenant:
            raise CaptureError(f"{tool}: an item for tenant {d.tenant_id!r} was returned to a capture for {tenant!r}; the capture is stopped")
        try:
            why = matcher.reason(tenant, d.source_id, d.excerpt)
        except Undecidable as exc:
            raise CaptureError(f"{tool}: held-out exclusion could not be decided for an item ({exc})") from exc
        if why:
            dropped[why] += 1
            dropped_ids.append(str(d.source_id))
            continue
        row = _sanitised_row(d)
        try:
            leaked = matcher.leaks(tenant, row["source_id"], row["excerpt"])      # the stored form is checked again, after sanitising
        except Undecidable as exc:
            raise CaptureError(f"{tool}: the stored form of an item could not be checked ({exc})") from exc
        if leaked:
            raise CaptureError(f"{tool}: a held-out record survived exclusion in its stored form; the capture is stopped")
        kept.append(row)
    kept = _order(tool, kept)
    proof = {"tool": tool, "args_key": args_key(tool, args), "returned": len(drafts), "kept": len(kept), "dropped": dropped,
             "dropped_digest": _digest(key, "dropped", "\n".join(sorted(dropped_ids))), "kept_hashes": [_row_hash(r) for r in kept]}
    return kept, proof


def _capture_case(registry, ctx_factory, case: Dict[str, Any], matcher: Matcher, recording: Recording, timeout_s: float, key: Optional[bytes],
                  cancelled: Callable[[], bool]) -> Dict[str, Any]:
    case_id, tenant = case["case_id"], str(case["tenant_id"]).strip().lower()
    ctx = ctx_factory(tenant, case_id)
    if ctx.tenant_id != tenant or ctx.request_id != case_id:
        raise CaptureError(f"{case_id}: the trusted context does not match the case")
    text = str(case.get("ticket_text") or "")
    calls: List[Dict[str, Any]] = []
    classifications: Dict[str, float] = {}

    def run(tool: str, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        if cancelled():
            raise CaptureCancelled("the capture was cancelled")
        rows, proof = _call(registry, ctx, tool, args, matcher, tenant, timeout_s, key)
        recording.data.setdefault(case_id, {}).setdefault(tool, {})[args_key(tool, args)] = rows
        calls.append(proof)
        return rows

    hosts = host_candidates(case)
    kb_args = {"query": text[:QUERY_CHARS] or "incident", "top_k": 5}
    kb_rows = list(run("incident_kb_search", kb_args))
    if hosts:
        for host in hosts:
            run("ticket_history_lookup", {"host": host, "days": 400})
    else:
        run("ticket_history_lookup", {"text": text[:200] or "incident", "days": 400})
    symptom = symptom_query(text, hosts)
    if len(symptom) >= 3 and symptom != kb_args["query"]:
        kb_rows += run("incident_kb_search", {"query": symptom, "top_k": 5})
    for row in kb_rows:
        name = classification_of(row)
        if name:
            classifications[name] = max(classifications.get(name, 0.0), row.get("relevance") or 0.0)
    for name in sorted(classifications, key=lambda n: (-classifications[n], n))[:CLASS_CALLS]:
        run("outcome_lookup", {"classification": name})
        run("pattern_lookup", {"classification": name})
    run("evidence_lookup", {})
    return {"tenant_id": tenant, "calls": calls}


# -- the capture ----------------------------------------------------------------------------------------------------------------------

def _fsync_write(path: Path, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def manifest_path(recording: Path) -> Path:
    return Path(recording).with_name(Path(recording).name + MANIFEST_SUFFIX)


def _sha256(data: bytes) -> str:
    """The freeze's own hash (CRLF read as LF), so a file hashes the same on every platform."""
    return freeze.sha256_bytes(data)


def _sha256_file(path: Path) -> str:
    return _sha256(Path(path).read_bytes())


def _invalidate_existing(out: Path, invalidate: Optional[str], by: Optional[str], now: datetime) -> None:
    man = manifest_path(out)
    present = [p for p in (out, man) if p.exists()]
    if not present:
        return
    complete = out.exists() and man.exists()
    if complete and not (invalidate and str(invalidate).strip() and by and str(by).strip()):
        raise CaptureError(f"{out} already holds a recording: it is never overwritten. Replacing it needs a recorded invalidation "
                           f"(invalidate=<reason>, by=<who>)")
    log_path = out.with_name(out.name + ".invalidations.json")
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    n = len(log) + 1
    log.append({"n": n, "date": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": (invalidate or "an earlier capture did not complete").strip(),
                "by": (by or "capture").strip(), "complete": complete,
                "previous_recording_sha256": _sha256_file(out) if out.exists() else None,
                "previous_manifest_sha256": _sha256_file(man) if man.exists() else None})
    for path in present:
        os.replace(path, path.with_name(f"{path.name}.invalidated-{n}"))
    _fsync_write(log_path, (json.dumps(log, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def run_capture(cases: List[Dict[str, Any]], held_out: List[Dict[str, Any]], registry: ToolRegistry, ctx_factory: Callable[[str, str], Any], out: Path,
                *, mode: str, selection_path: Optional[Path] = None, round: Optional[int] = None, key: Optional[bytes] = None, key_check: Optional[str] = None,
                invalidate: Optional[str] = None, by: Optional[str] = None, cancel: Optional[Callable[[], bool]] = None, warmup: bool = True,
                timeout_s: float = TIMEOUT_S, now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> Dict[str, Any]:
    """Capture the tool environment for `cases`, with every ticket in `held_out` (a superset of the cases) and its cluster removed.

    Returns the manifest. Nothing usable exists at `out` unless this returns."""
    if mode not in MODES:
        raise CaptureError(f"unknown capture mode {mode!r}")
    if mode == "experiment" and (key is None or not key_check):
        raise CaptureError("an experiment capture needs the active registered experiment key")
    if mode == "development" and key is not None:
        raise CaptureError("a development capture does not use the experiment key")
    if not cases:
        raise CaptureError("there are no cases to capture")
    ids = [c.get("case_id") for c in cases]
    if len(set(ids)) != len(ids) or not all(ids):
        raise CaptureError("case ids must be present and unique")
    for c in cases:
        if not str(c.get("tenant_id") or "").strip() or not str(c.get("ticket_text") or "").strip():
            raise CaptureError(f"{c.get('case_id')}: a case needs a tenant and ticket text")
    try:
        matcher = Matcher(held_out)
        matcher.self_test()
        for c in cases:                                              # every case's own ticket must be one the matcher removes
            own = matcher.reason(str(c["tenant_id"]).strip().lower(), "own", str(c["ticket_text"]))
            if own is None:
                raise Undecidable(f"{c['case_id']}: the case's own ticket is not covered by the held-out set")
    except Undecidable as exc:
        raise CaptureError(f"exclusion cannot be proven, so nothing was captured: {exc}") from exc

    out = Path(out)
    now = now_fn()
    cancelled = cancel or (lambda: False)
    _invalidate_existing(out, invalidate, by, now)
    tmp_rec, tmp_man = out.with_name(out.name + ".partial"), manifest_path(out).with_name(manifest_path(out).name + ".partial")
    for p in (tmp_rec, tmp_man):
        if p.exists():
            p.unlink()                                               # leftovers of an interrupted capture are not a recording

    if warmup:                                                       # the first retrieval loads the embedding model; do that before the real calls
        try:
            registry.call(ctx_factory(str(cases[0]["tenant_id"]).strip().lower(), "warmup"), "incident_kb_search", {"query": "service health check", "top_k": 1}, timeout_s)
        except Exception:  # noqa: BLE001 - a failed warm-up changes nothing recorded; the real calls decide
            pass

    recording = Recording()
    per_case: Dict[str, Any] = {}
    for case in sorted(cases, key=lambda c: c["case_id"]):
        per_case[case["case_id"]] = _capture_case(registry, ctx_factory, case, matcher, recording, timeout_s, key, cancelled)

    if cancelled():                                                  # the last chance to stop: nothing has been published yet
        raise CaptureCancelled("the capture was cancelled before it was published")
    blob = (json.dumps(recording.data, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
    held_digest = _digest(key, "held_out", "\n".join(sorted(f"{h['tenant']}|{h['id']}|{h['cluster']}" for h in matcher.tickets)))
    manifest = {
        "capture_version": CAPTURE_VERSION, "exclusion_version": exclusion.EXCLUSION_VERSION, "mode": mode, "round": round,
        "selection_sha256": _sha256_file(selection_path) if selection_path else None, "key_check": key_check,
        "runtime": RUNTIME_VERSION, "tools": {m.NAME: m.VERSION for m in TOOL_MODULES}, "held_out_count": len(matcher.tickets), "held_out_digest": held_digest,
        "exclusion_proof": {"self_test": "ok", "own_tickets_covered": True, "foreign_tenant_items": 0, "stored_form_rechecked": True},
        "case_count": len(per_case), "cases": per_case, "recording_sha256": _sha256(blob), "recording_bytes": len(blob),
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "complete": True,
    }
    man_blob = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")
    try:
        _fsync_write(tmp_rec, blob)
        _fsync_write(tmp_man, man_blob)
        os.replace(tmp_rec, out)
        os.replace(tmp_man, manifest_path(out))                      # last: only now does the recording count as complete
    except BaseException:
        for p in (tmp_rec, tmp_man):
            if p.exists():
                p.unlink()
        if out.exists() and not manifest_path(out).exists():          # the recording was published but its manifest was not: it must not linger
            out.unlink()
        raise
    return manifest


def check_manifest(manifest: Dict[str, Any], blob: bytes, *, expected_case_ids: Optional[Iterable[str]] = None, expected_mode: Optional[str] = None,
                   expected_key_check: Optional[str] = None) -> Dict[str, Any]:
    """Check a recording's bytes against its manifest and the expectations of its use. Returns the manifest, or raises CaptureError."""
    if manifest.get("complete") is not True:
        raise CaptureError("the capture manifest is not marked complete")
    if manifest.get("capture_version") != CAPTURE_VERSION or manifest.get("exclusion_version") != exclusion.EXCLUSION_VERSION:
        raise CaptureError("the recording was made by a different capture or exclusion version")
    if _sha256(blob) != manifest.get("recording_sha256"):
        raise CaptureError("the recording does not match its manifest: it was altered or is incomplete")
    if not all(manifest.get("exclusion_proof", {}).get(k) for k in ("self_test", "own_tickets_covered", "stored_form_rechecked")):
        raise CaptureError("the manifest does not carry a complete exclusion proof")
    if manifest.get("runtime") != RUNTIME_VERSION:
        raise CaptureError(f"the recording was captured under runtime {manifest.get('runtime')!r}, not {RUNTIME_VERSION!r}")
    if manifest.get("recording_bytes") != len(blob.replace(b"\r\n", b"\n")):
        raise CaptureError("the recording's size does not match its manifest")
    data = json.loads(blob.decode("utf-8"))
    if set(data) != set(manifest.get("cases", {})):
        raise CaptureError("the recording and its manifest list different cases")
    if manifest.get("case_count") != len(data) or manifest.get("case_count") != len(manifest.get("cases", {})):
        raise CaptureError("the manifest's case count does not match the recording and its own case map")
    if expected_case_ids is not None and set(expected_case_ids) != set(data):
        raise CaptureError("the recording does not cover exactly the cases it is used for")
    if expected_mode is not None and manifest.get("mode") != expected_mode:
        raise CaptureError(f"the recording was made in {manifest.get('mode')!r} mode, not {expected_mode!r}")
    if expected_key_check is not None and manifest.get("key_check") != expected_key_check:
        raise CaptureError("the recording was captured under a different experiment key")
    if manifest.get("tools") != {m.NAME: m.VERSION for m in TOOL_MODULES}:
        raise CaptureError("the recording was captured with different tool versions")
    return manifest


def verify_capture(recording: Path, **expectations: Any) -> Dict[str, Any]:
    """The manifest of a complete, unaltered recording next to `recording`, or CaptureError. A recording without its manifest is refused."""
    rec, man = Path(recording), manifest_path(recording)
    if not rec.is_file() or not man.is_file():
        raise CaptureError(f"{rec} is not a complete capture: the recording or its manifest is missing")
    return check_manifest(json.loads(man.read_text(encoding="utf-8")), rec.read_bytes(), **expectations)
