"""
Runs every planner mode over every case through the real agentruntime.Runner against the scripted environment (design 12).

Two public entry points, deliberately different, and no third:

* `run_once(record_path, results_dir)` is the ONLY way to execute the frozen cases. It takes **nothing** from the caller but the two paths:
  cases, annotations, recording, injection cases, budgets, modes, seed and the decision rule are read from the freeze record's files, each
  checked against its recorded hash from the same bytes that are then parsed, and the planners are not passed in either: the record names
  their entry points ("module:Class" in the frozen `agentruntime/planners/` folder, the same ones the planner freeze recorded) and the
  harness imports and constructs them itself, `Class(case)`. All code is verified against the record. The run is claimed atomically before
  anything executes, a failed run keeps its claim, and the engine (`_execute`) refuses frozen inputs unless it is handed the claim object that
  only `run_once` can create. There is no dry run of the frozen experiment: a dry run would show how the planners do on the 50 tickets, which
  is exactly what the one-shot design forbids.
* `run_experiment_unfrozen(...)` exists for the harness's own self-tests; its output carries `"not_a_result": true`, and the engine refuses
  to execute any ticket that was drawn into a planner-experiment set (matched by case id or by content hash) without the run claim, so it
  cannot be used to preview the frozen cases either.

Both validate the inputs first and **fail** rather than shrink: a case without an annotation or a recording, an annotation without a case,
an injection case whose base case is missing, or a missing mode is an error, never a silently smaller experiment.
"""
from __future__ import annotations

import hashlib
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
from planner_experiment import HARNESS_VERSION, planner_freeze
from planner_experiment.annotations import Annotation
from planner_experiment.freeze import RecordReader, check_decision_rule, load_record, validate_set, verify_code
from planner_experiment.injection import InjectionCase, apply_to_recording, apply_to_task, build_injection_cases, injection_succeeded
from planner_experiment.metrics import decide, score_run, summarise_mode
from planner_experiment.scripted_env import Recording, ScriptedEnv

MODES = ("rules_only", "rules_then_model", "model")
EXPECTED_CASES = 50
MIN_INJECTION_CASES = 12
CASE_FIELDS = ("case_id", "tenant_id", "ticket_text")


class ExperimentInputError(Exception):
    """The experiment's inputs are incomplete or inconsistent. The run does not start."""


class ExperimentAborted(Exception):
    """Too many stuck calls across the whole experiment: the infrastructure is failing and the run cannot be trusted. No result is produced."""


MAX_REPLACEMENTS = 3          # executor sets that may be replaced in one experiment
MAX_STUCK_CALLS = 4           # stuck (abandoned or leaked) calls still running, counted across EVERY executor the experiment ever created


_CLAIM_KEY = object()          # known only to this module: the run claim cannot be built anywhere else


class _RunClaim:
    """Proof that the atomic claim file for one freeze record was created. Only `run_once` constructs it."""
    __slots__ = ("record_hash",)

    def __init__(self, key: object, record_hash: str) -> None:
        if key is not _CLAIM_KEY:
            raise PermissionError("a run claim can only be created by run_once")
        self.record_hash = record_hash


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
    planners: Optional[Dict[str, str]] = None            # mode -> "module:Class", from the frozen config
    decision_rule: Optional[Dict[str, Any]] = None       # the frozen rule from the record; None only for self-tests (the code's own rule)


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


def _load_frozen_inputs(record_path: Path) -> ExperimentInputs:
    """Verify the freeze record and read every input from it. The caller supplies no experiment data."""
    record = load_record(record_path)
    verify_code(record)
    rule = check_decision_rule(record)               # the rule to apply is the frozen one, verified equal to the one this code defines
    reader = RecordReader(record)
    validate_set(reader, record["code"])             # reviewers, composition, seeds, planner freeze: the same checks as at the freeze
    j = reader.json                                   # verified bytes -> parsed value
    cases, notes, config = j("cases"), j("annotations"), j("config")
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
        budgets=Budgets(**config.get("budgets", {})), seed=int(config["seed"]), modes=tuple(config["modes"]),
        strict=True, frozen=True, record_hash=record["record_hash"], planners=dict(config["planners"]), decision_rule=rule)
    validate_inputs(inputs)
    return inputs


def resolve_planners(inputs: ExperimentInputs) -> Dict[str, Any]:
    """The planner classes named by the frozen config, imported from the frozen planners folder. Nothing is accepted from a caller."""
    if not inputs.planners or set(inputs.planners) != set(inputs.modes):
        raise ExperimentInputError("the frozen config must name a planner entry point for every mode")
    return {mode: planner_freeze.resolve_entrypoint(spec) for mode, spec in inputs.planners.items()}


# -- executors ------------------------------------------------------------------------------------------------------------------------

class _Executors:
    """The three executors a run needs, created once and reused so threads do not accumulate.

    If a run leaves a stuck call the set is replaced so the next case is not starved, but the cap is on the **whole experiment**, not on one
    set: replaced executors are kept (closed, so idle workers exit) and their still-running stuck calls keep counting. More than
    MAX_REPLACEMENTS replacements, or more than MAX_STUCK_CALLS stuck calls alive across all sets, aborts the experiment.
    """

    def __init__(self) -> None:
        self.replacements = 0
        self.retired: list = []
        self._make()

    def _make(self) -> None:
        self.tool = BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-tool", max_leaked=2)
        self.plan = BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-plan", max_leaked=2)
        self.model = BoundedExecutor(max_workers=3, max_abandoned=2, name="pe-model", max_leaked=2)

    def _current(self):
        return (self.tool, self.plan, self.model)

    def stuck(self) -> bool:
        return any(e.abandoned or e.leaked for e in self._current())

    def live_stuck(self) -> int:
        return sum(e.abandoned + e.leaked for e in list(self._current()) + self.retired)

    def live_threads(self) -> int:
        return sum(e.live_threads for e in list(self._current()) + self.retired)

    def replace_if_stuck(self) -> bool:
        if not self.stuck():
            return False
        if self.live_stuck() > MAX_STUCK_CALLS:
            raise ExperimentAborted(f"{self.live_stuck()} stuck calls are still running (limit {MAX_STUCK_CALLS})")
        if self.replacements >= MAX_REPLACEMENTS:
            raise ExperimentAborted(f"executors were already replaced {self.replacements} times (limit {MAX_REPLACEMENTS})")
        for e in self._current():
            e.close()
        self.retired.extend(self._current())
        self.retired = [e for e in self.retired if e.live_threads]          # finished ones hold nothing
        self._make()
        self.replacements += 1
        return True

    def close(self) -> None:
        for e in list(self._current()) + self.retired:
            e.close()


# -- the run --------------------------------------------------------------------------------------------------------------------------

def make_policy(mode: str, budgets: Optional[Budgets] = None) -> AgentPolicy:
    return AgentPolicy(allowed_tools=list(TOOL_NAMES), budgets=budgets or Budgets(), planner=mode, mode="batch")


def _tenant_dir(tenants) -> Path:
    root = Path(tempfile.mkdtemp(prefix="pe-tenants-"))
    for t in sorted(set(tenants)):
        (root / f"{t}.json").write_text(json.dumps({"tenant_id": t, "display_name": t}), encoding="utf-8")
    return root


def _write_json_atomic(path: Path, value: Any) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, path)                                    # all or nothing: a reader never sees a half-written result


def run_once(record_path: Path, results_dir: Path) -> Dict[str, Any]:
    """Run the frozen experiment once and store its result.

    The inputs are loaded and verified first (a bad freeze consumes nothing). Then the run is **claimed atomically**: a claim file is
    created with O_CREAT|O_EXCL, so of any number of concurrent or repeated calls exactly one proceeds and the rest are refused. The claim
    stays whatever happens: a run that fails or aborts has still been attempted, and trying again until the result is acceptable is exactly
    what run-once forbids, so a second attempt needs a new freeze (revised planners, fresh set) or a recorded owner decision that removes the
    claim by hand. The result is written atomically (temp file then rename).
    """
    inputs = _load_frozen_inputs(record_path)
    planners = resolve_planners(inputs)
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    stem = f"results_{inputs.record_hash[:16]}"
    claim, out = results_dir / f"{stem}.claim.json", results_dir / f"{stem}.json"
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            state = json.loads(claim.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            state = "unknown"
        raise ExperimentInputError(f"this freeze record has already been claimed (status: {state}); a new round needs revised planners and a fresh "
                                   f"set, or a recorded owner decision to remove {claim.name}") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"status": "running", "record_hash": inputs.record_hash, "pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, handle)
    if out.exists():                                         # a result without a claim (claim removed by hand): never overwrite it
        _write_json_atomic(claim, {"status": "refused", "reason": "a result already exists", "record_hash": inputs.record_hash})
        raise ExperimentInputError(f"{out.name} already exists")
    try:
        result = _execute(inputs, planners, None, _RunClaim(_CLAIM_KEY, inputs.record_hash))
    except BaseException as exc:                             # noqa: BLE001 - recorded, then re-raised
        _write_json_atomic(claim, {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "record_hash": inputs.record_hash})
        raise
    _write_json_atomic(out, result)
    _write_json_atomic(claim, {"status": "completed", "result_file": out.name, "record_hash": inputs.record_hash})
    return result


def run_experiment_unfrozen(cases, annotations: Dict[str, Annotation], recording: Recording, planner_factories, ctx_factory,
                            injection_cases: List[InjectionCase], seed: int, budgets: Optional[Budgets] = None, modes: tuple = MODES,
                            strict: bool = False, drawn_base: Optional[Path] = None) -> Dict[str, Any]:
    """For the harness's self-tests only. The result is marked `not_a_result`. Refuses any ticket drawn into a planner-experiment set
    (`drawn_base` is the folder holding the recorded draws; default: the repository's)."""
    inputs = ExperimentInputs(cases=cases, annotations=annotations, recording=recording, injection_cases=injection_cases,
                              budgets=budgets or Budgets(), seed=seed, modes=modes, strict=strict, frozen=False)
    validate_inputs(inputs)
    missing = [m for m in modes if m not in planner_factories]
    if missing:
        raise ExperimentInputError(f"no planner supplied for mode(s) {missing}")
    return _execute(inputs, planner_factories, ctx_factory, None, drawn_base)


def _drawn_experiment_cases(base: Path):
    """(case ids, content hashes) of every ticket recorded in a planner-experiment draw under `base`."""
    ids, hashes = set(), set()
    for p in sorted(Path(base).glob("experiment_selection_round*.json")):
        for entry in json.loads(p.read_text(encoding="utf-8")).get("cases", []):
            ids.add(entry["case_id"])
            hashes.add(entry["content_hash"])
    return ids, hashes


def _refuse_unclaimed_drawn_cases(cases: List[Dict[str, Any]], base: Optional[Path]) -> None:
    from planner_experiment import select_cases as sc
    from planner_experiment import steps

    ids, hashes = _drawn_experiment_cases(base if base is not None else steps.BASE)
    for c in cases:
        ticket = {"pool": c.get("pool"), "tenant_id": c.get("tenant_id"), "ticket_id": c.get("ticket_id"), "text": c.get("ticket_text"),
                  "host": c.get("host"), "opened_at": c.get("opened_at")}
        if c.get("case_id") in ids or (c.get("ticket_id") is not None and sc.content_hash(ticket) in hashes):
            raise ExperimentInputError(f"{c.get('case_id')} was drawn into a planner-experiment set: it can only be executed under the run "
                                       f"claim of run_once, never as a dry run")


def _execute(inputs: ExperimentInputs, planner_factories, ctx_factory: Optional[Callable[[Dict[str, Any]], TrustedContext]],
             claim: Optional[_RunClaim] = None, drawn_base: Optional[Path] = None) -> Dict[str, Any]:
    """The only place cases are executed. Frozen inputs need the run claim; anything else may not contain a drawn experiment ticket."""
    if inputs.frozen:
        if not isinstance(claim, _RunClaim) or claim.record_hash != inputs.record_hash:
            raise ExperimentInputError("frozen cases can only be executed under the run claim for their own freeze record")
    else:
        if claim is not None:
            raise ExperimentInputError("a run claim belongs to frozen inputs")
        _refuse_unclaimed_drawn_cases(inputs.cases, drawn_base)
    claimed = claim is not None
    cases, recording, budgets = inputs.cases, inputs.recording, inputs.budgets
    if ctx_factory is None:
        tenants = _tenant_dir(c["tenant_id"] for c in cases)

        def ctx_factory(case: Dict[str, Any]) -> TrustedContext:                      # noqa: F811
            return build_context(case["tenant_id"], case["case_id"], _Principal(), "batch", config_dir=tenants)
    by_id = {c["case_id"]: c for c in cases}
    original = json.loads(json.dumps(recording.data))                       # a deep, private copy of the recording
    results: Dict[str, Any] = {"harness_version": HARNESS_VERSION, "frozen": inputs.frozen, "claimed": claimed, "not_a_result": not (inputs.frozen and claimed),
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
        results["stuck_calls_at_end"] = executors.live_stuck()
    finally:
        executors.close()
    results["decision"] = decide(summaries, success_by_case, budgets.deadline_s, inputs.seed, inputs.decision_rule)
    return results
