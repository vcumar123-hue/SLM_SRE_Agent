"""
The live R2-3 acceptance executor (design 13.5, "Arms"), invoked once with the 100 frozen cases by the acceptance orchestration layer.

Two arms see the same ticket and the same frozen retrieval state:
  * baseline  = the existing deterministic pipeline with the loop off, with its existing fallback behaviour;
  * candidate = the as-deployed system with the loop on: the frozen `rules_only` planner runs when the engagement rule says so, *before* the one-shot fallback, exactly as the hook does. A supported, verified run
    (merged, or recorded as a conflict) replaces the fallback: the candidate answer is `reconcile(<the deterministic answer without the fallback>, run)`, produced by the same reconcile and decision code the hook uses.
    Anything else leaves the baseline answer standing (the deterministic output wherever the loop produced no accepted result).
Held-out exclusion applies to both arms through the same filter: the caller wraps the whole run in `batch_runner.held_out_retrieval` (the deterministic pipeline's retrieval) and builds the registry with
`batch_runner.filtered_registry` (the agent's tools), both from one `HeldOutFilter` over all 100 acceptance tickets, failing closed. A corpus fingerprint taken before and after detects a corpus that changed during the run.
Everything that reaches a review packet is sanitised first. The executor records raw outputs and does not judge them; judgements come later from the blinded reviewers.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Dict, List, Optional

from agentruntime.integration import engagement, reconcile
from agentruntime.sanitize import sanitize_text
from planner_experiment import batch_runner

PROBE_THRESHOLDS = {"confidence_threshold": 0.65, "min_retrieval_score": 0.25}
MAX_EVIDENCE = 5
EXCERPT = 300


class ExecutorError(Exception):
    pass


def _clean(text: Any, limit: int = EXCERPT) -> str:
    return sanitize_text(text, limit)[0]


def summarise(response: Dict[str, Any]) -> Dict[str, Any]:
    """The sanitised parts of an answer a reviewer may see: classification, causes, evidence excerpts, escalation flag."""
    analysis = response.get("analysis") if isinstance(response.get("analysis"), dict) else {}
    evidence: List[str] = []
    for item in (response.get("retrieved_incidents") or [])[:MAX_EVIDENCE]:
        if isinstance(item, dict):
            evidence.append(_clean(" ".join(str(item.get(k) or "") for k in ("title", "summary", "content") if item.get(k))))
    inv = response.get("investigation") if isinstance(response.get("investigation"), dict) else {}
    for e in (inv.get("evidence") or [])[:MAX_EVIDENCE]:
        evidence.append(_clean(e.get("excerpt")))
    return {"classification": str(analysis.get("classification") or ""), "causes": [_clean(c, 200) for c in (analysis.get("probable_causes") or [])][:5],
            "evidence": [e for e in evidence if e][:2 * MAX_EVIDENCE], "escalate": bool(analysis.get("escalate"))}


def candidate_outcome(run: Optional[Any], decision_outcome: str, engaged: bool) -> str:
    """The category the analysis code scores (evaluate_release2.OUTCOMES) for the loop's result on this ticket."""
    if not engaged or run is None:
        return "not_engaged"
    if run.status == "supported" and decision_outcome in ("merged", "conflict"):
        return "accepted_supported"
    if run.status == "supported":
        return "escalated_insufficient"                                   # supported but not accepted by reconciliation: the baseline answer stands, with no accepted loop result
    if run.status in ("escalated_insufficient", "escalated_conflict"):
        return run.status
    return "failure"                                                      # failed_*, timeout, budget exhausted, denied, dropped, cancelled


class LiveAcceptanceExecutor:
    def __init__(self, pipeline: Callable[[Dict[str, Any], bool], Dict[str, Any]], registry: Any, ctx_factory: Callable[[str, str], Any], planner_factory: Callable[[Dict[str, Any]], Any],
                 policy: Any, clock: Callable[[], float] = time.monotonic, fingerprint: Optional[Callable[[], Dict[str, Any]]] = None,
                 thresholds: Optional[Dict[str, float]] = None, held_out_filter: Optional[batch_runner.HeldOutFilter] = None) -> None:
        self.pipeline, self.registry, self.ctx_factory, self.planner_factory, self.policy = pipeline, registry, ctx_factory, planner_factory, policy
        self.clock, self.fingerprint, self.thresholds, self.filter = clock, fingerprint, thresholds or PROBE_THRESHOLDS, held_out_filter

    def _timed(self, fn: Callable[[], Any]):
        start = self.clock()
        out = fn()
        return out, self.clock() - start

    def _guard(self, case: Dict[str, Any], response: Dict[str, Any]) -> None:
        """Held-out material can also reach the deterministic pipeline through its RAG context or a stored feedback correction. Either containing a held-out ticket makes the run invalid (fail closed)."""
        if self.filter is None:
            return
        rag = str(response.get("rag_context") or "")
        if rag.strip() and not self.filter.keep(case["tenant_id"], "rag_context", rag):
            raise ExecutorError(f"{case['case_id']}: the pipeline's RAG context contains a held-out ticket, so exclusion is not proven and the run is not valid")
        if response.get("feedback_override"):
            raise ExecutorError(f"{case['case_id']}: a stored feedback correction matched this ticket (it was analysed before), so exclusion is not proven and the run is not valid")

    def one(self, case: Dict[str, Any]) -> Dict[str, Any]:
        # Production decides whether to engage immediately before the fallback.
        # Build that fallback-free state first, before the baseline arm is allowed
        # to call an external fallback or create any of its side effects.
        nofallback, nf_s = self._timed(lambda: self.pipeline(case, False))
        self._guard(case, nofallback)
        conf = nofallback.get("confidence") if isinstance(nofallback.get("confidence"), dict) else {}
        probe = {"ticket": case["ticket_text"], "confidence": {"overall_confidence": conf.get("primary_confidence", 0.0), "retrieval_quality": conf.get("retrieval_quality", 0.0)},
                 "retrieved_incidents": nofallback.get("retrieved_incidents") or [], "linked_resolution": nofallback.get("linked_resolution"),
                 "linked_playbook": nofallback.get("linked_playbook")}
        engaged, reasons = engagement.should_engage(probe, **self.thresholds)
        run, loop_s, outcome, reruns, loop_dump = None, 0.0, "none", [], None
        if engaged:
            batch, loop_s = self._timed(lambda: batch_runner.run_batch([case], self.registry, self.ctx_factory, self.planner_factory, self.policy, clock=self.clock))
            reruns = batch["reruns"]
            from agentruntime.contracts import AgentRunResult
            run = AgentRunResult(**batch["records"][0]["result"])
            loop_dump = batch["records"][0]["result"]
        # The baseline is a separate experimental arm, but it is deliberately
        # executed only after the candidate's pre-fallback state and loop have
        # been fixed. It therefore cannot influence candidate engagement.
        base, base_s = self._timed(lambda: self.pipeline(case, True))
        self._guard(case, base)
        candidate_resp, cand_s = base, base_s + loop_s
        if run is not None and run.status == "supported":
            merged, summary = reconcile.reconcile(nofallback, run)
            outcome = summary["outcome"]
            if outcome in ("merged", "conflict"):
                candidate_resp, cand_s = merged, nf_s + loop_s              # the investigation replaced the fallback call
        elif run is not None:
            outcome = "kept"
        category = candidate_outcome(run, outcome, engaged)
        return {"case_id": case["case_id"], "source": case.get("source", "real"), "engaged": engaged, "engagement_reasons": reasons, "baseline": summarise(base),
                "candidate": summarise(candidate_resp), "baseline_seconds": round(base_s, 3), "candidate_seconds": round(cand_s, 3), "loop_seconds": round(loop_s, 3),
                "candidate_outcome": category, "reconcile_outcome": outcome, "run": loop_dump, "reruns": reruns,
                "schema_valid": run is None or run.status not in ("failed_model", "failed_internal")}

    def __call__(self, cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        before = self.fingerprint() if self.fingerprint else None
        records, reruns = [], []
        for case in cases:
            rec = self.one(case)
            if getattr(self.filter, "undecided", 0):
                raise ExecutorError(f"{case['case_id']}: held-out exclusion could not be decided for an item, so exclusion is not proven and the run is not valid")
            reruns += rec["reruns"]
            records.append(rec)
        after = self.fingerprint() if self.fingerprint else None
        changed = before is not None and before != after
        if changed:
            raise ExecutorError("the corpus changed while the acceptance set was running (the frozen retrieval state was not held): the run is not valid")
        return {"records": records, "reruns": reruns, "deviation": bool(cases) and len(reruns) / len(cases) > batch_runner.RERUN_DEVIATION_SHARE,
                "corpus_fingerprint": before, "held_out_items_removed": getattr(self.filter, "removed", None), "executor_version": "1"}


def review_items(records: List[Dict[str, Any]], cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The inputs of `review_layout.build_packets`, deterministic given the records: one shared evidence packet per ticket (its text and the top retrieved material), and the two sanitised outputs."""
    by_id = {c["case_id"]: c for c in cases}
    out = []
    for r in sorted(records, key=lambda r: r["case_id"]):
        packet = [_clean(by_id[r["case_id"]]["ticket_text"], 600)] + r["baseline"]["evidence"][:MAX_EVIDENCE]
        out.append({"case_id": r["case_id"], "evidence_packet": packet,
                    "baseline": {k: r["baseline"][k] for k in ("causes", "evidence", "escalate")}, "candidate": {k: r["candidate"][k] for k in ("causes", "evidence", "escalate")}})
    return out


def default_pipeline(use_hooks_off: bool = True) -> Callable[[Dict[str, Any], bool], Dict[str, Any]]:
    """The real deterministic pipeline (`app._analyze_core_legacy`) with the loop off. A pipeline error is raised, never swallowed into an empty answer."""
    def run(case: Dict[str, Any], use_fallback: bool) -> Dict[str, Any]:
        import app
        from models import AnalyzeRequest
        request = AnalyzeRequest(ticket=case["ticket_text"], tenant_id=case["tenant_id"], use_fallback=use_fallback, force_fallback=False)
        return app._analyze_core_legacy(request)
    return run


# -- assembly of the real thing --------------------------------------------------------------------------------------------------------------------

class _BatchPrincipal:
    id = "acceptance-batch"

    def allows_tenant(self, tenant: str) -> bool:
        return True


def corpus_fingerprint() -> Dict[str, Any]:
    """Content hashes of the database sources read by the run.

    Counts alone miss in-place edits.  Hash the canonical JSON representation of
    every row, in a deterministic order, so a changed classification, excerpt,
    embedding, tenant or identifier invalidates the acceptance run even when the
    number of rows is unchanged.
    """
    import postgres_store
    out: Dict[str, Any] = {}
    with postgres_store.db_cursor(dict_rows=True) as (_c, cur):
        for key, table in (("knowledge_chunks", "client_knowledge_chunks"), ("ticket_records", "client_ticket_records")):
            cur.execute(f"SELECT row_to_json(t)::text AS row_json FROM {table} AS t ORDER BY row_to_json(t)::text")
            digest = hashlib.sha256()
            count = 0
            for row in cur:
                digest.update(str(row["row_json"]).encode("utf-8"))
                digest.update(b"\n")
                count += 1
            out[key] = {"rows": count, "sha256": digest.hexdigest()}
    return out


def build_live(held_out: List[Dict[str, Any]], fingerprint: Optional[Callable[[], Dict[str, Any]]] = corpus_fingerprint):
    """The real executor over the real tools and pipeline, with one exclusion filter for both. Returns (executor, filter); run it inside `live_scope(filter)`."""
    from agentruntime import config as runtime_config
    from agentruntime import release
    from agentruntime.context import build_context
    filt = batch_runner.HeldOutFilter(held_out)
    registry = batch_runner.filtered_registry(filt)
    policy = release.policy_for("batch", budgets=runtime_config.load_budgets())
    executor = LiveAcceptanceExecutor(default_pipeline(), registry, lambda tenant, case_id: build_context(tenant, case_id, _BatchPrincipal(), "batch"), release.planner_factory, policy,
                                      fingerprint=fingerprint, held_out_filter=filt)
    return executor, filt


def live_scope(filt: "batch_runner.HeldOutFilter"):
    """Patch every place the deterministic pipeline reads incidents from (the retrieval engine's functions, and the copy of `retrieve_incidents` that app.py imported at load) for the block."""
    import contextlib
    import app
    import retrieval_engine

    @contextlib.contextmanager
    def scope():
        with batch_runner.held_out_retrieval(filt, retrieval_engine):
            with batch_runner.held_out_retrieval(filt, app, names=("retrieve_incidents",)):
                yield
    return scope()
