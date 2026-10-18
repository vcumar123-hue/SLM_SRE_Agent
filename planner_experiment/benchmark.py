"""
Model speed benchmark (review 2026-10-17): can the exact frozen model, prompts and settings meet the frozen budgets on THIS machine?

The model candidates only make sense if a consultation finishes inside `model_call_timeout_s` (15 s) and a run inside `deadline_s` (40 s); the experiment's decision rule
also requires p95 latency within the deadline. This command measures that before anything is drawn, on the machine that will run the experiment, using only the development
subset and its official recording (never the hidden set):

  * one explicitly declared warm-up request (it loads the model; its time is reported separately and excluded from the statistics);
  * then `repetitions` passes over the development cases with the real rules_then_model planner, real prompts and the real local model;
  * the budgets are relaxed *for the measurement only* (so slow calls finish and can be timed); each call and each run is then judged against the frozen budgets, which
    are never changed anywhere else.

Before it can say a machine *qualifies* it verifies the actual model: the server must serve the manifest's model (publisher, architecture, quantisation), the GGUF file must
have the manifest's size and SHA-256, the loaded context must be at least the prompt budget, and every reply must come from the manifest's served id. A speed result for a
different model file would be meaningless for the frozen configuration, so any failed check means "does not qualify" whatever the speed.

The report records the execution environment, the model verification, the manifest hash and the prompt hashes, so a run on faster hardware is recorded as such while the
model file, prompts, inference settings and budgets stay unchanged. It writes a verdict: whether the model can be selected at all on this machine.
"""
from __future__ import annotations

import math
import os
import platform
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional

from agentruntime.contracts import AgentPolicy, Budgets
from agentruntime.executor import BoundedExecutor
from agentruntime.planners import model_manifest, prompts
from agentruntime.planners.model_support import LocalModelClient
from agentruntime.planners.rules_then_model import RulesThenModelPlanner
from agentruntime.runner import Runner
from agentruntime.tools import TOOL_MODULES
from planner_experiment import capture
from planner_experiment.scripted_env import Recording, ScriptedEnv

BENCH_ENV = "AIOPS_AGENT_BATCH_ENABLED"
RELAXED = {"model_call_timeout_s": 600, "deadline_s": 3600, "max_tokens_run": 100000, "max_model_calls": 100}


def percentile(values: List[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (q in 0..100); None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def stats(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {"n": len(values), "mean": round(sum(values) / len(values), 3), "p50": round(percentile(values, 50), 3),
            "p95": round(percentile(values, 95), 3), "max": round(max(values), 3)}


def environment() -> Dict[str, Any]:
    return {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(), "python": platform.python_version(),
            "cpu_count": os.cpu_count()}


class _Timed:
    """Wraps the client: records the duration, tokens and outcome of every model request (including ones that fail)."""

    def __init__(self, client: Any, clock: Callable[[], float]) -> None:
        self.client, self.clock = client, clock
        self.calls: List[Dict[str, Any]] = []

    def complete(self, *args: Any, **kwargs: Any):
        start = self.clock()
        try:
            response = self.client.complete(*args, **kwargs)
        except Exception as exc:
            self.calls.append({"seconds": self.clock() - start, "ok": False, "error": type(exc).__name__, "tokens_in": 0, "tokens_out": 0})
            raise
        self.calls.append({"seconds": self.clock() - start, "ok": True, "error": None, "tokens_in": response.tokens_in, "tokens_out": response.tokens_out,
                           "model": response.model})
        return response


def run_benchmark(cases: List[Dict[str, Any]], recording_path, repetitions: int, *, client: Any = None, ctx_factory: Optional[Callable[[str, str], Any]] = None,
                  clock: Callable[[], float] = time.perf_counter, warmup_timeout_s: float = 300.0, verify: Optional[Callable[[], Dict[str, Any]]] = None,
                  models_dir: Optional[str] = None, check_file: bool = True) -> Dict[str, Any]:
    if repetitions < 1:
        raise ValueError("at least one repetition is needed")
    manifest = capture.verify_capture(recording_path, expected_case_ids=[c["case_id"] for c in cases], expected_mode="development")
    base_client = client or LocalModelClient()
    frozen = Budgets()
    check = verify or (lambda: model_manifest.verify(models_dir=Path(models_dir) if models_dir else None, check_file=check_file))
    verification_before = check()
    # the declared warm-up: one request that loads the model; not part of the statistics
    t0 = clock()
    warm_error = None
    try:
        from agentruntime.planners.model_support import reply_schema
        base_client.complete("Reply with the JSON object only.", "Say escalate.", "warmup", reply_schema(["finish", "escalate"]), 60, warmup_timeout_s)
    except Exception as exc:  # noqa: BLE001 - reported, and the measurement continues (it will show the failures)
        warm_error = f"{type(exc).__name__}: {exc}"
    warmup_s = clock() - t0
    verification = check()      # again after the warm-up: the loaded context length is only known once the model is loaded

    timed = _Timed(base_client, clock)
    rec = Recording.load(recording_path)
    make_ctx = ctx_factory or capture.default_context
    runs: List[Dict[str, Any]] = []
    previous = os.environ.get(BENCH_ENV)
    os.environ[BENCH_ENV] = "1"
    planner_executor, model_executor = BoundedExecutor(4, 2, "bench-planner"), BoundedExecutor(4, 2, "bench-model")      # one pair for the whole benchmark
    try:
        for rep in range(repetitions):
            for case in cases:
                env = ScriptedEnv(rec)
                runner = Runner(env.registry(), clock=clock, planner_executor=planner_executor, model_executor=model_executor)
                budgets = Budgets.model_construct(**{**frozen.model_dump(), **RELAXED})          # measurement only
                policy = AgentPolicy(allowed_tools=[m.NAME for m in TOOL_MODULES], budgets=budgets, mode="batch", planner="rules_then_model")
                planner = RulesThenModelPlanner(case, timed)
                before = len(timed.calls)
                started = clock()
                result = runner.run_batch(make_ctx(case["tenant_id"], case["case_id"]), policy, {"ticket_text": case["ticket_text"]}, planner)
                calls = timed.calls[before:]
                runs.append({"case_id": case["case_id"], "repetition": rep + 1, "status": result.status, "seconds": clock() - started,
                             "model_calls": len(calls), "fallbacks": list(planner.fallbacks)})
    finally:
        planner_executor.close()
        model_executor.close()
        if previous is None:
            os.environ.pop(BENCH_ENV, None)
        else:
            os.environ[BENCH_ENV] = previous

    ok_calls = [c for c in timed.calls if c["ok"]]
    call_seconds = [c["seconds"] for c in timed.calls]
    run_seconds = [r["seconds"] for r in runs]
    within_call = [s for s in call_seconds if s <= frozen.model_call_timeout_s]
    within_run = [s for s in run_seconds if s <= frozen.deadline_s]
    p95_call, p95_run = percentile(call_seconds, 95), percentile(run_seconds, 95)
    fits = bool(call_seconds) and p95_call is not None and p95_call <= frozen.model_call_timeout_s and p95_run is not None and p95_run <= frozen.deadline_s
    served = model_manifest.MODEL["served_id"]
    wrong_model = sorted({str(c.get("model")) for c in ok_calls if c.get("model") != served})
    context = (verification.get("observed") or {}).get("loaded_context_length")
    file_checked = any(c.get("check", "").startswith("model file sha256") and c.get("ok") for c in verification.get("checks", []))
    problems: List[str] = []
    if not verification.get("ok"):
        failed = "; ".join(f"{c['check']} ({c['detail']})" for c in verification.get("checks", []) if not c.get("ok"))
        problems.append("the model verification failed: " + (failed or "no checks ran"))
    if not file_checked:
        problems.append("the model file's SHA-256 was not verified, so it is not established that the frozen GGUF was measured")
    if not isinstance(context, int) or context < model_manifest.INFERENCE["prompt_budget_tokens"]:
        problems.append(f"the loaded context length ({context}) is below the {model_manifest.INFERENCE['prompt_budget_tokens']}-token prompt budget")
    if wrong_model:
        problems.append(f"replies came from a model other than {served}: {wrong_model}")
    if warm_error:
        problems.append("the warm-up request failed: " + warm_error)
    if model_manifest.incomplete():
        problems.append("the model manifest is incomplete")
    qualifies = fits and not problems
    if not call_seconds:
        verdict = "no model request was made, so nothing could be measured"
    elif problems:
        verdict = "this machine does NOT qualify: " + " | ".join(problems) + ("" if fits else " | and the speed does not fit the frozen budgets either")
    elif fits:
        verdict = "the model fits the frozen budgets on this machine (p95 call and p95 run are within them)"
    else:
        verdict = ("the model does NOT fit the frozen budgets on this machine: consultations would time out and rules_then_model would fall back to rules_only; "
                   "the experiment would select rules_only. Use faster inference with the same model, prompts, settings and budgets, or accept that outcome.")
    return {
        "benchmark_version": "1", "environment": environment(), "model_manifest_hash": model_manifest.manifest_hash(), "manifest_complete": not model_manifest.incomplete(),
        "prompt_versions": dict(model_manifest.PROMPTS), "prompt_hashes": prompts.hashes(), "model": dict(model_manifest.MODEL), "inference": dict(model_manifest.INFERENCE),
        "frozen_budgets": {"model_call_timeout_s": frozen.model_call_timeout_s, "deadline_s": frozen.deadline_s, "max_tokens_run": frozen.max_tokens_run,
                           "max_model_calls": frozen.max_model_calls},
        "recording_sha256": manifest["recording_sha256"], "cases": len(cases), "repetitions": repetitions, "runs": len(runs),
        "warmup": {"declared": True, "seconds": round(warmup_s, 3), "error": warm_error},
        "model_verification": {"before_warmup_ok": bool(verification_before.get("ok")), "after_warmup": verification, "file_hash_verified": file_checked,
                               "loaded_context_length": context, "served_id_in_replies": not wrong_model},
        "qualifies": qualifies, "problems": problems,
        "model_requests": {"count": len(timed.calls), "failed": len(timed.calls) - len(ok_calls), "seconds": stats(call_seconds),
                           "tokens_in": stats([float(c["tokens_in"]) for c in ok_calls]), "tokens_out": stats([float(c["tokens_out"]) for c in ok_calls]),
                           "share_within_call_timeout": round(len(within_call) / len(call_seconds), 3) if call_seconds else None},
        "runs_seconds": stats(run_seconds), "share_runs_within_deadline": round(len(within_run) / len(run_seconds), 3) if run_seconds else None,
        "p95_call_seconds": p95_call, "p95_run_seconds": p95_run, "fits_frozen_budgets": fits, "verdict": verdict,
        "run_detail": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()} for r in runs],
    }
