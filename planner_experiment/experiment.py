"""
Runs every planner mode over every case through the real agentruntime.Runner against the scripted environment (design 12).

Two entry points, deliberately different:

* `run_experiment_frozen(record_path, planner_factories)` is the only way to produce a result. It takes **nothing** from the caller except
  the planner factories: cases, annotations, recording, injection cases, budgets, modes and seed are all read from the freeze record's
  files, each checked against its recorded hash from the same bytes that are then parsed. Code (runtime, tools, planners, harness) is
  verified against the record, and every planner must be defined in the frozen `agentruntime/planners/` folder.
* `run_experiment_unfrozen(...)` exists for the harness's own self-tests; its output carries `"not_a_result": true`.

Both validate the inputs first and **fail** rather than shrink: a case without an annotation or a recording, an annotation without a case,
an injection case whose base case is missing, or a missing mode is an error, never a silently smaller experiment.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

from agentruntime.context import TrustedContext, build_context
from agentruntime.contracts import AgentPolicy, Budgets
from agentruntime.executor import BoundedExecutor
from agentruntime.runner import BATCH_ENV, Runner
from agentruntime.tools import TOOL_NAMES
from planner_experiment import HARNESS_VERSION
from planner_experiment import seed as seed_mod
from planner_experiment.annotations import Annotation
from planner_experiment.freeze import PLANNERS_DIR, load_record, read_frozen, verify_code
from planner_experiment.injection import InjectionCase, apply_to_recording, apply_to_task, build_injection_cases, injection_succeeded
from planner_experiment.metrics import decide, score_run, summarise_mode
from planner_experiment.scripted_env import Recording, ScriptedEnv

MODES = ("rules_only", "rules_then_model", "model")
EXPECTED_CASES = 50
MIN_INJECTION_CASES = 12
CASE_FIELDS = ("case_id", "tenant_id", "ticket_text")


class ExperimentInputError(Exception):
    """The experiment's inputs are incomplete or inconsistent. The run does not start."""


class _Principal:
    id = "planner-experiment"

    def allows_tenant(self, tenant: str) -> bool:
        return True


@dataclass
class ExperimentInputs:
    cases: List[Dict[str, Any]]
    annotations: Dict[str, Annotation]
    recording: Recording
    injection_cases: List[InjectionCase]
    budgets: Budgets
    seed: int
    modes: tuple = MODES
    strict: bool = True
    frozen: bool = False
    record_hash: Optional[str] = None


# -- inputs ---------------------------------------------------------------------------------------------------------------------------

def validate_inputs(inputs: ExperimentInputs) -> None:
    problems: List[str] = []
    ids = [c.get("case_id") for c in inputs.cases]
    for c in inputs.cases:
        missing = [f for f in CASE_FIELDS if not str(c.get(f) or "").strip()]
        if missing:
            problems.append(f"case {c.get('case_id')!r} is missing {missing}")
    if len(set(ids)) != len(ids):
        problems.append("duplicate case ids")
    case_ids = set(ids)
    no_annotation = sorted(case_ids - set(inputs.annotations))
    no_case = sorted(set(inputs.annotations) - case_ids)
    no_recording = sorted(i for i in case_ids if not inputs.recording.data.get(i))
    if no_annotation:
        problems.append(f"{len(no_annotation)} case(s) have no annotation: {no_annotation[:5]}")
    if no_case:
        problems.append(f"{len(no_case)} annotation(s) have no case: {no_case[:5]}")
    if no_recording:
        problems.append(f"{len(no_recording)} case(s) have no recorded tool output: {no_recording[:5]}")
    bad_base = sorted(i.case_id for i in inputs.injection_cases if i.base_case_id not in case_ids)
    if bad_base:
        problems.append(f"injection case(s) with a missing base case: {bad_base}")
    if len({i.case_id for i in inputs.injection_cases}) != len(inputs.injection_cases):
        problems.append("duplicate injection case ids")
    if inputs.strict:
        if len(inputs.cases) != EXPECTED_CASES:
            problems.append(f"expected exactly {EXPECTED_CASES} cases, found {len(inputs.cases)}")
        if len(inputs.injection_cases) < MIN_INJECTION_CASES:
            problems.append(f"at least {MIN_INJECTION_CASES} injection cases are required, found {len(inputs.injection_cases)}")
        if tuple(inputs.modes) != MODES:
            problems.append(f"all three modes {MODES} must be run, got {tuple(inputs.modes)}")
    elif not {"rules_only", "rules_then_model"} <= set(inputs.modes):
        problems.append("rules_only and rules_then_model are both needed for the decision rule")
    if problems:
        raise ExperimentInputError("; ".join(problems))


def load_frozen_inputs(record_path: Path) -> ExperimentInputs:
    """Verify the freeze record and read every input from it. The caller supplies no experiment data."""
    record = load_record(record_path)
    verify_code(record)

    def j(name: str) -> Any:                                   # verified bytes -> parsed value
        return json.loads(read_frozen(record, name).decode("utf-8"))

    cases, notes, config = j("cases"), j("annotations"), j("config")
    seed_record = seed_mod.check_record(j("seed_record"), "calibration-selection")
    if config.get("seed") != seed_record["seed"]:
        raise ExperimentInputError("the config seed is not the derived seed in the seed record")
    selection = j("calibration_selection")
    if sorted(c["case_id"] for c in selection.get("cases", [])) != sorted(c["case_id"] for c in cases):
        raise ExperimentInputError("the case list does not match the frozen calibration selection")
    annotations: Dict[str, Annotation] = {}
    for raw in notes:
        a = Annotation(**raw)
        a.check()
        if a.case_id in annotations:
            raise ExperimentInputError(f"duplicate annotation for {a.case_id}")
        annotations[a.case_id] = a
    injection_cases = [InjectionCase(**d) for d in j("injection_cases")]
    rebuilt = build_injection_cases([i.base_case_id for i in injection_cases]) if injection_cases else []
    if [(i.case_id, i.canary, i.payload) for i in injection_cases] != [(i.case_id, i.canary, i.payload) for i in rebuilt]:
        raise ExperimentInputError("the injection cases do not match the frozen payload templates")
    inputs = ExperimentInputs(
        cases=cases, annotations=annotations, recording=Recording(j("recording")), injection_cases=injection_cases,
        budgets=Budgets(**config.get("budgets", {})), seed=int(config["seed"]), modes=tuple(config.get("modes", MODES)),
        strict=True, frozen=True, record_hash=record["record_hash"])
    validate_inputs(inputs)
    return inputs


def check_planner_binding(planner_factories: Dict[str, Callable[[Dict[str, Any]], Any]], modes: tuple) -> None:
    """Every planner must be defined in the frozen planners folder, so the code compared is the code that is promoted."""
    planners = PLANNERS_DIR.resolve()
    for mode in modes:
        factory = planner_factories.get(mode)
        if factory is None:
            raise ExperimentInputError(f"no planner supplied for mode '{mode}'")
        target = factory.func if isinstance(factory, functools.partial) else factory
        target = target if inspect.isclass(target) or inspect.isfunction(target) else type(target)
        try:
            source = inspect.getsourcefile(target)
        except TypeError:
            source = None
        if not source or planners not in Path(source).resolve().parents:
            raise ExperimentInputError(f"the planner for '{mode}' is not defined in {PLANNERS_DIR.name}/: {source}")


# -- executors ------------------------------------------------------------------------------------------------------------------------

class _Executors:
    """The three executors a run needs, created once and reused so threads do not accumulate; replaced only if a run left a stuck call."""

    def __init__(self) -> None:
        self.replacements = 0
        self._make()

    def _make(self) -> None:
        self.tool = BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-tool", max_leaked=2)
        self.plan = BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-plan", max_leaked=2)
        self.model = BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-model", max_leaked=2)

    def _all(self):
        return (self.tool, self.plan, self.model)

    def stuck(self) -> bool:
        return any(e.abandoned or e.leaked for e in self._all())

    def replace_if_stuck(self) -> bool:
        if not self.stuck():
            return False
        self.close()
        self._make()
        self.replacements += 1
        return True

    def close(self) -> None:
        for e in self._all():
            e.close()


# -- the run --------------------------------------------------------------------------------------------------------------------------

def make_policy(mode: str, budgets: Optional[Budgets] = None) -> AgentPolicy:
    return AgentPolicy(allowed_tools=list(TOOL_NAMES), budgets=budgets or Budgets(), planner=mode, mode="batch")


def _tenant_dir(tenants) -> Path:
    root = Path(tempfile.mkdtemp(prefix="pe-tenants-"))
    for t in sorted(set(tenants)):
        (root / f"{t}.json").write_text(json.dumps({"tenant_id": t, "display_name": t}), encoding="utf-8")
    return root


def run_experiment_frozen(record_path: Path, planner_factories: Dict[str, Callable[[Dict[str, Any]], Any]]) -> Dict[str, Any]:
    inputs = load_frozen_inputs(record_path)
    check_planner_binding(planner_factories, inputs.modes)
    return _execute(inputs, planner_factories, None)


def run_experiment_unfrozen(cases, annotations: Dict[str, Annotation], recording: Recording, planner_factories, ctx_factory,
                            injection_cases: List[InjectionCase], seed: int, budgets: Optional[Budgets] = None, modes: tuple = MODES,
                            strict: bool = False) -> Dict[str, Any]:
    """For the harness's self-tests only. The result is marked `not_a_result`."""
    inputs = ExperimentInputs(cases=cases, annotations=annotations, recording=recording, injection_cases=injection_cases,
                              budgets=budgets or Budgets(), seed=seed, modes=modes, strict=strict, frozen=False)
    validate_inputs(inputs)
    missing = [m for m in modes if m not in planner_factories]
    if missing:
        raise ExperimentInputError(f"no planner supplied for mode(s) {missing}")
    return _execute(inputs, planner_factories, ctx_factory)


def _execute(inputs: ExperimentInputs, planner_factories, ctx_factory: Optional[Callable[[Dict[str, Any]], TrustedContext]]) -> Dict[str, Any]:
    cases, recording, budgets = inputs.cases, inputs.recording, inputs.budgets
    if ctx_factory is None:
        tenants = _tenant_dir(c["tenant_id"] for c in cases)

        def ctx_factory(case: Dict[str, Any]) -> TrustedContext:                      # noqa: F811
            return build_context(case["tenant_id"], case["case_id"], _Principal(), "batch", config_dir=tenants)
    by_id = {c["case_id"]: c for c in cases}
    original = json.loads(json.dumps(recording.data))                       # a deep, private copy of the recording
    results: Dict[str, Any] = {"harness_version": HARNESS_VERSION, "frozen": inputs.frozen, "not_a_result": not inputs.frozen,
                               "record_hash": inputs.record_hash, "seed": inputs.seed, "cases": len(cases),
                               "injection_cases": len(inputs.injection_cases), "modes": {}}
    success_by_case: Dict[str, Dict[str, float]] = {}
    summaries: Dict[str, Dict[str, Any]] = {}
    executors = _Executors()
    try:
        for mode in inputs.modes:
            factory = planner_factories[mode]
            policy = make_policy(mode, budgets)
            scores: List[Dict[str, Any]] = []
            latencies: List[float] = []
            injections: List[Dict[str, Any]] = []
            runs: List[Dict[str, Any]] = []

            def one(run_case: Dict[str, Any]):
                env = ScriptedEnv(recording)
                runner = Runner(env.registry(executors.tool), planner_executor=executors.plan, model_executor=executors.model)
                planner = factory(run_case)
                ctx = ctx_factory(run_case)
                run_id = f"{mode}-" + hashlib.sha1(run_case["case_id"].encode("utf-8")).hexdigest()[:12]
                started = time.perf_counter()
                with patch.dict(os.environ, {BATCH_ENV: "1"}):
                    result = runner.run_batch(ctx, policy, {"ticket_text": run_case.get("ticket_text", "")}, planner, run_id=run_id)
                seconds = time.perf_counter() - started
                return result, seconds, env.stats.get(run_case["case_id"], {}), executors.replace_if_stuck()

            for case in cases:
                result, seconds, lookup, replaced = one(case)
                score = score_run(result, inputs.annotations[case["case_id"]], policy.allowed_tools)    # every case is annotated (validated)
                scores.append(score)
                latencies.append(seconds)
                runs.append({"case_id": case["case_id"], "seconds": round(seconds, 3), "lookups": lookup, "executors_replaced": replaced, **score})
            if len(scores) != len(cases):
                raise ExperimentInputError(f"{mode}: scored {len(scores)} of {len(cases)} cases")
            success_by_case[mode] = {s["case_id"]: float(s["trajectory_success"]) for s in scores}

            for inj in inputs.injection_cases:
                base = by_id[inj.base_case_id]                                # validated: never skipped
                text = apply_to_task({"ticket_text": base["ticket_text"]}, inj)["ticket_text"]
                recording.data[inj.case_id] = apply_to_recording(original[inj.base_case_id], inj)
                try:
                    result, _seconds, _lookup, _replaced = one({**base, "case_id": inj.case_id, "ticket_text": text})
                finally:
                    recording.data.pop(inj.case_id, None)
                injections.append({"case_id": inj.case_id, "vector": inj.vector, "attack": inj.attack,
                                   **injection_succeeded(result, inj, policy.allowed_tools)})
            if len(injections) != len(inputs.injection_cases):
                raise ExperimentInputError(f"{mode}: ran {len(injections)} of {len(inputs.injection_cases)} injection cases")

            summaries[mode] = summarise_mode(scores, latencies, injections, inputs.seed)
            results["modes"][mode] = {"summary": summaries[mode], "runs": runs, "injections": injections}
        results["executor_replacements"] = executors.replacements
    finally:
        executors.close()
    results["decision"] = decide(summaries, success_by_case, budgets.deadline_s, inputs.seed)
    return results
