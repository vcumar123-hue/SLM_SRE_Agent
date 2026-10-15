"""
Runs every planner mode over every case through the real agentruntime.Runner against the scripted environment (design 12).

The planners under test come from `planner_factories` ({mode: callable(case) -> Planner}). Nothing here knows how a planner decides.
`run_experiment(..., freeze_record=PATH)` verifies the freeze first and refuses to run if any input changed; `freeze_record=None` is for
harness self-tests only and marks the output `"frozen": false`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

from agentruntime.context import TrustedContext
from agentruntime.contracts import AgentPolicy, Budgets
from agentruntime.executor import BoundedExecutor
from agentruntime.runner import BATCH_ENV, Runner
from agentruntime.tools import TOOL_NAMES
from planner_experiment import HARNESS_VERSION
from planner_experiment.annotations import Annotation
from planner_experiment.freeze import verify_freeze
from planner_experiment.injection import InjectionCase, apply_to_recording, apply_to_task, injection_succeeded
from planner_experiment.metrics import decide, score_run, summarise_mode
from planner_experiment.scripted_env import Recording, ScriptedEnv

MODES = ("rules_only", "rules_then_model", "model")


def make_policy(mode: str, budgets: Optional[Budgets] = None) -> AgentPolicy:
    return AgentPolicy(allowed_tools=list(TOOL_NAMES), budgets=budgets or Budgets(), planner=mode, mode="batch")


def run_experiment(
    cases: List[Dict[str, Any]],                                  # {case_id, tenant_id, ticket_text}
    annotations: Dict[str, Annotation],                           # adjudicated, by case_id
    recording: Recording,
    planner_factories: Dict[str, Callable[[Dict[str, Any]], Any]],
    ctx_factory: Callable[[Dict[str, Any]], TrustedContext],      # builds the trusted context; request_id must be the case id
    injection_cases: List[InjectionCase],
    seed: int,
    budgets: Optional[Budgets] = None,
    freeze_record: Optional[Path] = None,
    modes: tuple = MODES,
) -> Dict[str, Any]:
    if freeze_record is not None:
        verify_freeze(freeze_record)
    budgets = budgets or Budgets()
    by_id = {c["case_id"]: c for c in cases}
    results: Dict[str, Any] = {"harness_version": HARNESS_VERSION, "frozen": freeze_record is not None, "seed": seed, "modes": {}}
    success_by_case: Dict[str, Dict[str, float]] = {}
    summaries: Dict[str, Dict[str, Any]] = {}

    original = dict(recording.data)
    for mode in modes:
        factory = planner_factories[mode]
        policy = make_policy(mode, budgets)
        scores: List[Dict[str, Any]] = []
        latencies: List[float] = []
        injections: List[Dict[str, Any]] = []
        runs: List[Dict[str, Any]] = []

        def one(run_case: Dict[str, Any]) -> Any:
            env = ScriptedEnv(recording)
            # one executor per run keeps a stuck planner or tool in one case from affecting the next
            executor = BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-tool", max_leaked=2)
            runner = Runner(env.registry(executor), planner_executor=BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-plan", max_leaked=2),
                            model_executor=BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-model", max_leaked=2))
            planner = factory(run_case)
            ctx = ctx_factory(run_case)
            started = time.perf_counter()
            with patch.dict(os.environ, {BATCH_ENV: "1"}):
                result = runner.run_batch(ctx, policy, {"ticket_text": run_case.get("ticket_text", "")}, planner, run_id=f"{mode}-{run_case['case_id']}"[:64])
            return result, time.perf_counter() - started, env.stats.get(run_case["case_id"], {})

        for case in cases:
            annotation = annotations.get(case["case_id"])
            if annotation is None:
                continue
            result, seconds, lookup = one(case)
            score = score_run(result, annotation, policy.allowed_tools)
            scores.append(score)
            latencies.append(seconds)
            runs.append({"case_id": case["case_id"], "seconds": round(seconds, 3), "lookups": lookup, **score})
        success_by_case[mode] = {s["case_id"]: float(s["trajectory_success"]) for s in scores}

        for inj in injection_cases:
            base = by_id.get(inj.base_case_id)
            if base is None:
                continue
            case = {**base, "case_id": inj.case_id, "ticket_text": apply_to_task({"ticket_text": base.get("ticket_text", "")}, inj)["ticket_text"]}
            recording.data[inj.case_id] = apply_to_recording(original.get(inj.base_case_id, {}), inj)
            result, seconds, _lookup = one(case)
            verdict = injection_succeeded(result, inj, policy.allowed_tools)
            injections.append({"case_id": inj.case_id, "vector": inj.vector, "attack": inj.attack, **verdict})
            recording.data.pop(inj.case_id, None)

        summaries[mode] = summarise_mode(scores, latencies, injections, seed)
        results["modes"][mode] = {"summary": summaries[mode], "runs": runs, "injections": injections}

    results["decision"] = decide(summaries, success_by_case, budgets.deadline_s, seed)
    return results
