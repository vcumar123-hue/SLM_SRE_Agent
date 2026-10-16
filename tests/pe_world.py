"""
A complete, consistent synthetic planner-experiment world, built through the REAL workflow steps (planner_experiment.steps) in a temp
folder: reviewers, partition seed, development subset, planner freeze, experiment draw, cases, recording, annotations, injection cases,
config and golden tickets. Tests then mutate one thing and check the freeze or the run refuses it. Synthetic tickets only.
"""
import contextlib
import json
import os
import pathlib
import tempfile
from unittest.mock import patch
from dataclasses import dataclass, field
from typing import Any, Dict, List

from planner_experiment import golden as golden_mod
from planner_experiment import injection, metrics, planner_freeze, secrecy, steps
from planner_experiment import seed as seed_mod
from planner_experiment.experiment import MODES
from planner_experiment.freeze import ROOT
from agentruntime.contracts import EvidenceDraft
from planner_experiment import capture as capture_mod
from planner_experiment.scripted_env import Recording
from tests.test_planner_experiment import annotation, drafts_for

TESTS_DIR = pathlib.Path(__file__).resolve().parent
TEST_KEY_HEX = "ab" * 32                          # the experiment key used by every test world (a throwaway, never a real key)
TEST_KEY = bytes.fromhex(TEST_KEY_HEX)
os.environ[secrecy.KEY_ENV] = TEST_KEY_HEX        # worlds are drawn, frozen and run in this process, which therefore "holds the key"
ENTRYPOINTS = {"rules_only": "tests.pe_fake_planners:GiveUpPlanner", "rules_then_model": "tests.pe_fake_planners:GoodPlanner",
               "model": "tests.pe_fake_planners:GoodPlanner"}
KINDS = ["disk", "login", "service", "cert", "cpu", "queue", "memory", "network", "backup", "batch", "print", "mail"]
NAMES = ("Ann", "Bob")
PARTITION_ARTIFACT = "a" * 40
DEVELOPMENT_ARTIFACT = "b" * 40


def make_tickets(n: int = 400) -> List[Dict[str, Any]]:
    out = []
    for pool in ("client_c", "client_a"):
        for i in range(n):
            wording = "problem reported on" if pool == "client_c" else "fault noted against"             # the two pools never share a text
            out.append({"pool": pool, "tenant_id": pool, "ticket_id": f"INC{i:07d}", "text": f"{KINDS[i % len(KINDS)]} {wording} host{i}",
                        "host": f"host{i}", "opened_at": f"2026-0{1 + i % 6}-10 08:00:00"})
    return out


def golden_raw(approvers=NAMES, n: int = 30) -> List[Dict[str, Any]]:
    return [{"ticket_id": f"golden-{i}", "text": f"{KINDS[i % len(KINDS)]} golden incident on gold{i}", "host": f"gold{i}",
             "opened_at": f"2026-0{1 + i % 6}-12 09:00:00", "set": "calibration", "approved_by": list(approvers), "tenant_id": "poc_client"}
            for i in range(n)]


def fake_code_root() -> pathlib.Path:
    """A small stand-in code tree, so a world can revise 'planner code' between rounds without touching the real repository."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="pe-root-"))
    for top in ("agentruntime", "planner_experiment"):
        (root / top).mkdir()
        (root / top / "m.py").write_text("x = 1\n", encoding="utf-8")
    (root / "agentruntime" / "planners").mkdir()
    (root / "agentruntime" / "planners" / "p.py").write_text("RULES = 1\n", encoding="utf-8")
    return root


def register_test_custody(base):
    """Put the test key in custody for a base folder, the way the workflow requires before any draw (no key is retired)."""
    base = pathlib.Path(base)
    base.mkdir(parents=True, exist_ok=True)
    if not (base / "retired_keys.json").exists():
        (base / "retired_keys.json").write_text("[]", encoding="utf-8")
    secrecy.register_custody(secrecy.key_check(TEST_KEY), "test custodian (a throwaway key)", base / "key_custody.json", base / "retired_keys.json")


@contextlib.contextmanager
def no_key():
    """A process that holds NO experiment key: the environment variable is empty and the key file is somewhere that does not exist. Tests that
    need 'no key' use this, so they never depend on whether the machine running them happens to hold a real key file."""
    with patch.dict(os.environ, {secrecy.KEY_ENV: ""}), patch.object(secrecy, "KEY_FILE", pathlib.Path(tempfile.mkdtemp(prefix="pe-nokey-")) / "absent.key"):
        yield


@dataclass
class World:
    dir: pathlib.Path
    base: pathlib.Path
    tickets: List[Dict[str, Any]]
    cases: List[Dict[str, Any]]
    files: Dict[str, pathlib.Path]
    reviewer_files: Dict[str, pathlib.Path]
    entrypoints: Dict[str, str]
    round: int = 1
    code_root: Any = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def out(self) -> pathlib.Path:
        return self.dir / "freeze.json"

    def write(self, key: str, value) -> None:
        path = self.files[key] if key in self.files else self.reviewer_files[key]
        path.write_text(json.dumps(value), encoding="utf-8")

    def read(self, key: str):
        path = self.files[key] if key in self.files else self.reviewer_files[key]
        return json.loads(path.read_text(encoding="utf-8"))

    def freeze(self):
        from planner_experiment import freeze
        if self.code_root is not None:
            return freeze.create_freeze(self.files, self.reviewer_files, self.out, root=self.code_root)
        return freeze.create_freeze(self.files, self.reviewer_files, self.out)


def capture_for(cases, selection_path, recording_path, rnd) -> Dict[str, Any]:
    """A real, official capture (fake tools): the same code path as production, so every world's recording carries a genuine manifest."""
    from agentruntime.context import build_context
    from agentruntime.executor import BoundedExecutor
    from agentruntime.registry import ToolRegistry
    from agentruntime.tools import TOOL_MODULES
    from tests.agentruntime_helpers import tenant_dir
    cfg = tenant_dir("client_c", "client_a", "poc_client")
    reg = ToolRegistry(executor=BoundedExecutor(4, 2, "t-world-capture"))

    def make(name):
        def fn(ctx, *, cancel=None, timeout_s=None, **args):
            source = {"incident_kb_search": "incident", "ticket_history_lookup": "ticket", "outcome_lookup": "outcome", "pattern_lookup": "pattern",
                      "evidence_lookup": "evidence_check"}[name]
            return [EvidenceDraft(tenant_id=ctx.tenant_id, source_type=source, source_id="I1", provenance="tenant_data", relevance=0.5,
                                  excerpt="disk full on the database host")]
        fn.__name__ = name
        return fn
    for m in TOOL_MODULES:
        reg.register(m.SPEC, make(m.NAME))
    held = [{"pool": c["pool"], "tenant_id": c["tenant_id"], "ticket_id": c["ticket_id"], "text": c["ticket_text"], "host": c.get("host", "")} for c in cases]
    return capture_mod.run_capture(cases, held, reg, lambda t, cid: build_context(t, cid, capture_mod.CapturePrincipal(t), "batch", config_dir=cfg), recording_path,
                                   mode="experiment", selection_path=selection_path, round=rnd, key=TEST_KEY, key_check=secrecy.key_check(TEST_KEY), warmup=False)


def build_world(reviewers=NAMES, root=None, entrypoints=None, golden_approvers=NAMES, n_tickets: int = 400, directory=None, rounds: int = 1,
                with_void: bool = False) -> World:
    """`rounds=2` draws round 1, revises the (stand-in) planner code, freezes round 2 and draws it too; the frozen set is that of round 2."""
    d = pathlib.Path(directory or tempfile.mkdtemp(prefix="pe-world-"))
    base = d / "baselines"
    if rounds > 1 and root is None:
        root = fake_code_root()
    root = root or ROOT
    entrypoints = entrypoints or dict(ENTRYPOINTS)
    steps.record_reviewers(base, list(reviewers), {n: 40.0 for n in reviewers}, "owner")
    register_test_custody(base)
    paths = steps.paths(base)
    seed_mod.write_record(paths["partition_seed"], PARTITION_ARTIFACT, "calibration-selection")
    golden_path = d / "golden_tickets.json"
    golden_path.write_text(json.dumps(golden_raw(golden_approvers)), encoding="utf-8")
    tickets = make_tickets(n_tickets) + golden_mod.load_golden(golden_path, list(reviewers))
    steps.select_development(base, tickets, DEVELOPMENT_ARTIFACT)
    if with_void:                                       # a recorded void-and-redraw, acknowledged by both reviewers, before the planners are frozen
        steps.void_development(base, "a defect in the draw", "project owner")
        steps.select_development(base, tickets, DEVELOPMENT_ARTIFACT)
        for name in reviewers:
            steps.acknowledge_voids(base, [1], name, "read the notice")
    steps.freeze_planners(base, 1, entrypoints, root=root, planners_dir=TESTS_DIR)
    steps.draw_experiment(base, 1, tickets, root=root)
    for n in range(2, rounds + 1):
        (root / "agentruntime" / "planners" / "p.py").write_text(f"RULES = {n}\n", encoding="utf-8")          # planners revised on the development subset
        steps.freeze_planners(base, n, entrypoints, root=root, planners_dir=TESTS_DIR)
        steps.draw_experiment(base, n, tickets, root=root)
    rp = steps.round_paths(base, rounds)
    cases = steps.build_cases(rp["selection"], tickets)

    def write(name, value):
        p = d / name
        p.write_text(json.dumps(value), encoding="utf-8")
        return p

    recording_path = d / "recording.json"
    manifest = capture_for(cases, rp["selection"], recording_path, rounds)
    recording = Recording.load(recording_path)
    draw_seed = json.loads(rp["seed"].read_text(encoding="utf-8"))["seed"]
    files = {
        "annotations": write("annotations.json", [annotation(c["case_id"], "adjudicated").model_dump() for c in cases]),
        "cases": write("cases.json", cases), "recording": recording_path, "recording_manifest": capture_mod.manifest_path(recording_path),
        "injection_cases": write("injection_cases.json", [vars(i) for i in injection.build_injection_cases([c["case_id"] for c in cases[:4]])]),
        "config": write("config.json", {"round": rounds, "seed": draw_seed, "modes": list(MODES), "planners": entrypoints, "budgets": {}}),
        "seed_record": rp["seed"], "partition_seed_record": paths["partition_seed"], "experiment_selection": rp["selection"],
        "development_selection": paths["development"], "planner_freeze": rp["planner_freeze"], "reviewers": paths["reviewers"],
        "golden": golden_path, "key_custody": base / "key_custody.json", "retired_keys": base / "retired_keys.json",
    }
    for n in range(1, rounds):
        files[f"prior_selection:{n}"] = steps.round_paths(base, n)["selection"]
    if with_void:
        files["development_voids"] = base / "development_voids.json"
    reviewer_files = {n: write(f"reviewer_{n}.json", [annotation(c["case_id"], n).model_dump() for c in cases]) for n in reviewers}
    return World(dir=d, base=base, tickets=tickets, cases=cases, files=files, reviewer_files=reviewer_files, entrypoints=entrypoints,
                 round=rounds, code_root=(root if root is not ROOT else None), extra={"golden_path": golden_path, "recording": recording})
