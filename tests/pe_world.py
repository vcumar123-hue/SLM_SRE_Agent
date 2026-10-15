"""
A complete, consistent synthetic planner-experiment world, built through the REAL workflow steps (planner_experiment.steps) in a temp
folder: reviewers, partition seed, development subset, planner freeze, experiment draw, cases, recording, annotations, injection cases,
config and golden tickets. Tests then mutate one thing and check the freeze or the run refuses it. Synthetic tickets only.
"""
import json
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List

from planner_experiment import golden as golden_mod
from planner_experiment import injection, metrics, planner_freeze, steps
from planner_experiment import seed as seed_mod
from planner_experiment.experiment import MODES
from planner_experiment.freeze import ROOT
from planner_experiment.scripted_env import Recording
from tests.test_planner_experiment import annotation, drafts_for

TESTS_DIR = pathlib.Path(__file__).resolve().parent
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
            out.append({"pool": pool, "tenant_id": pool, "ticket_id": f"{pool}-{i}", "text": f"{KINDS[i % len(KINDS)]} problem reported on host{i}",
                        "host": f"host{i}", "opened_at": f"2026-0{1 + i % 6}-10 08:00:00"})
    return out


def golden_raw(approvers=NAMES, n: int = 30) -> List[Dict[str, Any]]:
    return [{"ticket_id": f"golden-{i}", "text": f"{KINDS[i % len(KINDS)]} golden incident on gold{i}", "host": f"gold{i}",
             "opened_at": f"2026-0{1 + i % 6}-12 09:00:00", "set": "calibration", "approved_by": list(approvers), "tenant_id": "poc_client"}
            for i in range(n)]


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
        return freeze.create_freeze(self.files, self.reviewer_files, self.out)


def build_world(reviewers=NAMES, root=None, entrypoints=None, golden_approvers=NAMES, n_tickets: int = 400, directory=None) -> World:
    d = pathlib.Path(directory or tempfile.mkdtemp(prefix="pe-world-"))
    base = d / "baselines"
    root = root or ROOT
    entrypoints = entrypoints or dict(ENTRYPOINTS)
    steps.record_reviewers(base, list(reviewers), {n: 40.0 for n in reviewers}, "owner")
    paths = steps.paths(base)
    seed_mod.write_record(paths["partition_seed"], PARTITION_ARTIFACT, "calibration-selection")
    golden_path = d / "golden_tickets.json"
    golden_path.write_text(json.dumps(golden_raw(golden_approvers)), encoding="utf-8")
    tickets = make_tickets(n_tickets) + golden_mod.load_golden(golden_path, list(reviewers))
    steps.select_development(base, tickets, DEVELOPMENT_ARTIFACT)
    steps.freeze_planners(base, 1, entrypoints, root=root, planners_dir=TESTS_DIR)
    steps.draw_experiment(base, 1, tickets, root=root)
    rp = steps.round_paths(base, 1)
    cases = steps.build_cases(rp["selection"], tickets)

    def write(name, value):
        p = d / name
        p.write_text(json.dumps(value), encoding="utf-8")
        return p

    recording = Recording()
    for c in cases:
        recording.add(c["case_id"], "incident_kb_search", {"query": c["ticket_text"], "top_k": 5},
                      drafts_for(c["tenant_id"], ("incident", "I1", "disk full on the database host")))
    draw_seed = json.loads(rp["seed"].read_text(encoding="utf-8"))["seed"]
    files = {
        "annotations": write("annotations.json", [annotation(c["case_id"], "adjudicated").model_dump() for c in cases]),
        "cases": write("cases.json", cases), "recording": write("recording.json", recording.data),
        "injection_cases": write("injection_cases.json", [vars(i) for i in injection.build_injection_cases([c["case_id"] for c in cases[:4]])]),
        "config": write("config.json", {"round": 1, "seed": draw_seed, "modes": list(MODES), "planners": entrypoints, "budgets": {}}),
        "seed_record": rp["seed"], "partition_seed_record": paths["partition_seed"], "experiment_selection": rp["selection"],
        "development_selection": paths["development"], "planner_freeze": rp["planner_freeze"], "reviewers": paths["reviewers"],
        "golden": golden_path,
    }
    reviewer_files = {n: write(f"reviewer_{n}.json", [annotation(c["case_id"], n).model_dump() for c in cases]) for n in reviewers}
    return World(dir=d, base=base, tickets=tickets, cases=cases, files=files, reviewer_files=reviewer_files, entrypoints=entrypoints,
                 extra={"golden_path": golden_path, "recording": recording})
