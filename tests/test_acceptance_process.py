"""The R2-3 process tooling on synthetic fixtures only: amendments, the freeze, the blinded layout, the batch runner and the run-once command."""
import copy
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agentruntime.contracts import AgentPolicy, Budgets, EvidenceDraft
from agentruntime.executor import BoundedExecutor
from agentruntime.runner import Runner
from agentruntime.tools import TOOL_MODULES
from planner_experiment import acceptance, acceptance_freeze as af, acceptance_run as ar, amendments, batch_runner, review_layout, roles, seed as seed_mod
from planner_experiment.batch_runner import BatchError, HeldOutFilter
from tests.agentruntime_helpers import FakeClock, make_ctx
from tests.pe_world import PARTITION_ARTIFACT
from tests.test_acceptance_draw import ARTIFACT, tickets

REPO = pathlib.Path(__file__).resolve().parent.parent
GOOD_ROLES = {"roles": {"labeler_1": "Vijay", "labeler_2": "Sara", "adjudicator": "Ramanathan", "custodian": "Ramanathan", "golden_approver": "Ramanathan"},
              "owner": "Tom Okafor", "golden_author": "the developer (AI assistant)", "same_person": [], "different_people": []}


PASSING_CALIBRATION = {"passed": True, "recorded_by": "Ramanathan", "reviewers": ["Vijay", "Sara"], "calibration_tickets_only": True,
                       "agreement": {"passed": True, "kappa_determinable": 0.72},
                       "blinding": {"near_chance": True, "accuracy": 0.55, "reviewers": {
                           "Vijay": {"near_chance": True, "accuracy": 0.55}, "Sara": {"near_chance": True, "accuracy": 0.50}}}}


def golden_items(n=20, approver="Ramanathan"):
    topics = ["tape", "cert", "queue", "login", "print", "disk", "mail", "vpn", "dns", "cpu", "memory", "backup", "batch", "export", "sync", "search", "audio", "cron", "orders", "gateway"]
    return [{"ticket_id": f"golden-acc-{i}", "text": f"acceptance synthetic {topics[i - 1]} problem number {i} observed on the {topics[i - 1]} service", "host": f"accsrv{i:02d}",
             "opened_at": f"2026-{1 + i % 9:02d}-12 09:00:00", "set": "acceptance", "tenant_id": "poc_client", "approved_by": [approver], "approved_on": "2026-11-02",
             "expected_causes": [f"{topics[i - 1]} component fault"], "required_evidence": ["a similar earlier incident"]} for i in range(1, n + 1)]


class World:
    """A temporary repository root with the frozen public items, an approved A-1, valid roles, and a private package drawn from synthetic tickets."""

    def __init__(self, goldens=None, roles_record=None, approve=True, calibrated=True):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="acc-world-"))
        for rel in af.PUBLIC_ITEMS.values():
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, dst)
        (self.root / "data").mkdir(exist_ok=True)
        shutil.copy(REPO / "data" / "golden_tickets.json", self.root / "data" / "golden_tickets.json")
        (self.root / "docs" / "evaluation" / "roles.json").write_text(json.dumps(roles_record or GOOD_ROLES), encoding="utf-8")
        if calibrated:
            (self.root / "docs" / "evaluation" / "calibration_result.json").write_text(json.dumps(PASSING_CALIBRATION), encoding="utf-8")
        else:
            (self.root / "docs" / "evaluation" / "calibration_result.json").write_text(json.dumps({"passed": False}), encoding="utf-8")
        am = {"amendments": [{"id": "A-1", "approvals": ([{"approver": "Tom Okafor", "statement": "I approve amendment A-1 exactly as written, in my own words.", "recorded_at": "x"}] if approve else [])}]}
        (self.root / "docs" / "evaluation" / "amendments.json").write_text(json.dumps(am), encoding="utf-8")
        self.private = self.root / "private"
        partition, draw = self.root / "seed_calibration.json", self.root / "seed_acceptance.json"
        seed_mod.write_record(partition, PARTITION_ARTIFACT, "calibration-selection")
        seed_mod.write_record(draw, ARTIFACT, "acceptance-selection")
        acceptance.draw_private(tickets(), partition, draw, self.private)
        (self.private / af.PRIVATE_ITEMS["golden_tickets"]).write_text(json.dumps(goldens if goldens is not None else golden_items()), encoding="utf-8")
        self.record_path = self.root / "docs" / "evaluation" / "freeze_record.json"
        self.ids_path = self.root / "docs" / "evaluation" / "acceptance_ids.json"

    def freeze(self):
        return af.create_freeze(self.private, self.record_path, self.ids_path, self.root)


def valid_release():
    return patch("agentruntime.release.problems", return_value=[])


class AmendmentTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "a.json"
        self.path.write_text(json.dumps({"amendments": [{"id": "A-1", "approvals": []}]}), encoding="utf-8")

    def test_an_amendment_is_approved_only_by_a_statement_and_never_inferred(self):
        self.assertFalse(amendments.is_approved("A-1", self.path))
        for bad in (("", "I approve A-1 in my own words, fully."), ("Pat", "yes"), ("Pat", "")):
            with self.assertRaises(amendments.AmendmentError):
                amendments.approve("A-1", *bad, path=self.path)
        amendments.approve("A-1", "Pat", "I approve methodology amendment A-1 exactly as written.", self.path)
        self.assertTrue(amendments.is_approved("A-1", self.path))
        with self.assertRaises(amendments.AmendmentError):
            amendments.approve("A-9", "Pat", "I approve methodology amendment A-9 exactly as written.", self.path)

    def test_withdrawing_keeps_the_entry_and_removes_the_standing_approval(self):
        amendments.approve("A-1", "Pat", "I approve methodology amendment A-1 exactly as written.", self.path)
        with self.assertRaises(amendments.AmendmentError):
            amendments.withdraw("A-1", " ", self.path)
        amendments.withdraw("A-1", "the approval was inferred", self.path)
        self.assertFalse(amendments.is_approved("A-1", self.path))
        entry = amendments.load(self.path)["amendments"][0]["approvals"][0]
        self.assertEqual(entry["withdrawn"]["reason"], "the approval was inferred")
        with self.assertRaises(amendments.AmendmentError):
            amendments.require("A-1", self.path)
        amendments.approve("A-1", "Pat", "I approve methodology amendment A-1, restated in my own words.", self.path)
        self.assertTrue(amendments.is_approved("A-1", self.path))

    def test_the_repository_record_has_the_owners_explicit_a1_approval(self):
        self.assertTrue(amendments.is_approved("A-1"))
        entry = amendments.load()["amendments"][0]["approvals"][0]
        self.assertTrue(entry["withdrawn"]["reason"])


class FreezeTests(unittest.TestCase):
    def test_a_complete_valid_package_freezes_and_the_record_holds_hashes_and_ids_only(self):
        w = World()
        with valid_release():
            record = w.freeze()
        self.assertEqual((record["cases"], record["real_tickets"], record["goldens"]), (100, 80, 20))
        self.assertEqual(set(record["items"]), set(af.PUBLIC_ITEMS))
        text = w.record_path.read_text(encoding="utf-8") + w.ids_path.read_text(encoding="utf-8")
        for c in json.loads((w.private / af.PRIVATE_ITEMS["cases"]).read_text(encoding="utf-8"))[:5]:
            self.assertNotIn(c["ticket_text"], text)
        self.assertNotIn("acceptance synthetic", text)
        self.assertEqual(len(json.loads(w.ids_path.read_text(encoding="utf-8"))["cases"]), 100)
        self.assertEqual(af.verify_freeze(w.private, w.record_path, w.root), [])

    def test_it_is_created_once(self):
        w = World()
        with valid_release():
            w.freeze()
            with self.assertRaisesRegex(af.FreezeError, "already exists"):
                w.freeze()

    def test_it_refuses_without_a_standing_a1_approval(self):
        w = World(approve=False)
        with valid_release(), self.assertRaisesRegex(af.FreezeError, "amendment A-1"):
            w.freeze()

    def test_it_refuses_until_the_calibration_checks_have_passed(self):
        w = World(calibrated=False)
        with valid_release(), self.assertRaisesRegex(af.FreezeError, "calibration checks"):
            w.freeze()

    def test_it_refuses_when_the_roles_are_not_separated(self):
        for change in ({"custodian": "Vijay"}, {"golden_approver": "Sara"}, {"adjudicator": ""}):
            rr = copy.deepcopy(GOOD_ROLES)
            rr["roles"].update(change)
            w = World(roles_record=rr)
            with valid_release(), self.assertRaisesRegex(af.FreezeError, "roles|person|named"):
                w.freeze()

    def test_it_refuses_when_the_release_configuration_has_problems(self):
        w = World()
        with patch("agentruntime.release.problems", return_value=["the owner's named approval of the release freeze is not confirmed"]):
            with self.assertRaisesRegex(af.FreezeError, "configuration freeze"):
                w.freeze()

    def test_it_refuses_goldens_not_approved_per_ticket_by_the_named_approver(self):
        bad_sets = {"approved by someone else": golden_items(approver="Pat Other"), "wrong count": golden_items(19),
                    "no expected causes": [dict(g, expected_causes=[]) for g in golden_items()], "no approval date": [dict(g, approved_on="") for g in golden_items()]}
        for label, goldens in bad_sets.items():
            w = World(goldens=goldens)
            with valid_release(), self.assertRaises(af.FreezeError, msg=label):
                w.freeze()

    def test_it_refuses_goldens_that_overlap_the_calibration_goldens(self):
        cal = json.loads((REPO / "data" / "golden_tickets.json").read_text(encoding="utf-8"))[0]
        goldens = golden_items()
        goldens[0]["host"] = cal["host"]
        w = World(goldens=goldens)
        with valid_release(), self.assertRaisesRegex(af.FreezeError, "does not verify"):
            w.freeze()

    def test_it_refuses_once_any_label_exists(self):
        w = World()
        (w.private / "labels_vijay.json").write_text("{}", encoding="utf-8")
        with valid_release(), self.assertRaisesRegex(af.FreezeError, "before any labelling"):
            w.freeze()

    def test_it_refuses_a_tampered_or_incomplete_private_package(self):
        w = World()
        cases = json.loads((w.private / af.PRIVATE_ITEMS["cases"]).read_text(encoding="utf-8"))
        cases[0]["ticket_text"] += " edited"
        (w.private / af.PRIVATE_ITEMS["cases"]).write_text(json.dumps(cases), encoding="utf-8")
        with valid_release(), self.assertRaisesRegex(af.FreezeError, "does not verify"):
            w.freeze()
        w2 = World()
        (w2.private / af.PRIVATE_ITEMS["golden_tickets"]).unlink()
        with valid_release(), self.assertRaisesRegex(af.FreezeError, "incomplete"):
            w2.freeze()

    def test_every_later_change_to_a_frozen_item_or_the_package_is_reported(self):
        w = World()
        with valid_release():
            w.freeze()
        self.assertEqual(af.verify_freeze(w.private, w.record_path, w.root), [])
        for name, rel in af.PUBLIC_ITEMS.items():
            path = w.root / rel
            original = path.read_bytes()
            path.write_bytes(original + b"\n# edited\n")
            problems = af.verify_freeze(w.private, w.record_path, w.root)
            self.assertTrue(any(name in p for p in problems), name)
            path.write_bytes(original)
        goldens = w.private / af.PRIVATE_ITEMS["golden_tickets"]
        original = goldens.read_bytes()
        goldens.write_bytes(original + b" ")
        self.assertTrue(any("package golden_tickets: changed" in p for p in af.verify_freeze(w.private, w.record_path, w.root)))
        goldens.write_bytes(original)
        self.assertTrue(af.verify_freeze(w.private, w.root / "missing.json", w.root))

    def test_the_gates_and_rubric_are_frozen_through_the_design_text(self):
        self.assertEqual(len(af.design_sections_hash()), 64)
        with patch.object(af, "DESIGN_PATH", pathlib.Path(tempfile.mkdtemp()) / "none.md"):
            with self.assertRaises(Exception):
                af.design_sections_hash()


def sample_item(i, **over):
    item = {"case_id": f"c{i}", "evidence_packet": [f"Ticket text {i}.", "Related note from the knowledge base."],
            "baseline": {"causes": ["Recurrence of an earlier incident: disk quota too small", "log growth on the volume"], "evidence": ["Disk quota exceeded on the data volume", "Volume 98 percent full"], "escalate": False},
            "candidate": {"causes": ["same underlying issue as similar incident chk: log growth on the volume"], "evidence": ["Volume 98 percent full", "Disk quota exceeded on the data volume"], "escalate": False}}
    item.update(over)
    return item


class LayoutTests(unittest.TestCase):
    def test_outputs_have_one_identical_layout_and_no_source_markers(self):
        packets, key = review_layout.build_packets([sample_item(i) for i in range(30)], seed=5)
        self.assertEqual(len(packets), 30)
        self.assertTrue(review_layout.layout_is_identical(packets))
        for p in packets:
            for slot in ("A", "B"):
                self.assertEqual(review_layout.forbidden_cues(p["outputs"][slot]), [])
        self.assertEqual({v for k in key.values() for v in (k["A"], k["B"])}, {"baseline", "candidate"})

    def test_the_order_is_random_per_ticket_and_reproducible_from_the_seed(self):
        a = review_layout.build_packets([sample_item(i) for i in range(40)], seed=5)
        b = review_layout.build_packets([sample_item(i) for i in range(40)], seed=5)
        c = review_layout.build_packets([sample_item(i) for i in range(40)], seed=6)
        self.assertEqual(a[1], b[1])
        self.assertNotEqual(a[1], c[1])
        first = [k["A"] for k in a[1].values()]
        self.assertTrue(0.25 < first.count("baseline") / len(first) < 0.75)                 # neither side sits systematically in position A
        self.assertNotEqual([p["ticket"] for p in a[0]], sorted(p["ticket"] for p in a[0]) and [f"c{i}" for i in range(40)])

    def test_aliases_are_neutral_and_hide_the_case_ids(self):
        packets, key = review_layout.build_packets([sample_item(i) for i in range(5)], seed=1)
        self.assertTrue(all(p["ticket"].startswith("T-") for p in packets))
        self.assertNotIn("c0", json.dumps(packets))
        self.assertIn("case_id", next(iter(key.values())))

    def test_both_sides_are_normalised_the_same_way(self):
        out = review_layout.normalise_output(["Recurrence of an earlier incident: disk quota too small", "disk quota too small"], ["b evidence", "a evidence"], False)
        self.assertEqual(out["causes"][0], "Disk quota too small.")
        self.assertEqual(out["evidence"], ["A evidence.", "B evidence."])                       # sorted, so order carries no cue
        redacted = review_layout.normalise_output(["Cause [REDACTED_TOKEN] here"], [], True)
        self.assertIn("[removed]", redacted["causes"][0])

    def test_a_packet_that_would_reveal_its_source_is_refused(self):
        for leak in ("Investigation found e3 supports it", "The rules_only planner chose this", "weak hypothesis", "completed in 12 seconds", "baseline answer", "Groq said so"):
            item = sample_item(1, candidate={"causes": [leak], "evidence": ["x"], "escalate": False})
            with self.assertRaises(review_layout.LayoutError, msg=leak):
                review_layout.build_packets([item], seed=1)

    def test_judgements_are_unblinded_only_when_complete(self):
        packets, key = review_layout.build_packets([sample_item(i) for i in range(3)], seed=2)
        judgements = {p["ticket"]: {"A": {"grounded": True}, "B": {"grounded": False}} for p in packets}
        out = review_layout.unblind(judgements, key)
        self.assertEqual(set(out), {"c0", "c1", "c2"})
        self.assertEqual({tuple(sorted(v)) for v in out.values()}, {("baseline", "candidate")})
        del judgements["T-001"]
        with self.assertRaises(review_layout.LayoutError):
            review_layout.unblind(judgements, key)

    def test_the_blinding_check_passes_near_chance_and_fails_a_good_guesser(self):
        packets, key = review_layout.build_packets([sample_item(i) for i in range(20)], seed=9)
        always_a = {p["ticket"]: "A" for p in packets}
        result = review_layout.blinding_accuracy(always_a, key)
        self.assertEqual(result["n"], 20)
        perfect = {alias: ("A" if k["A"] == "candidate" else "B") for alias, k in key.items()}
        res = review_layout.blinding_accuracy(perfect, key)
        self.assertEqual((res["accuracy"], res["near_chance"]), (1.0, False))
        with self.assertRaises(review_layout.LayoutError):
            review_layout.blinding_accuracy({}, key)


HELD_OUT = [{"pool": "client_x", "tenant_id": "client_x", "ticket_id": "INC100", "text": "sapdb01 error 5 occurred", "host": "sapdb01"}]


def fake_tools():
    def kb(ctx, *, cancel=None, timeout_s=None, query="", top_k=5):
        return [EvidenceDraft(tenant_id=ctx.tenant_id, source_type="incident", source_id="chk_self", provenance="tenant_data", excerpt="sapdb01 error 5 occurred. the ticket itself", relevance=0.9),
                EvidenceDraft(tenant_id=ctx.tenant_id, source_type="incident", source_id="chk_ok", provenance="tenant_data", excerpt="Tape job failed. Root Cause: media error", relevance=0.5)]

    def history(ctx, *, cancel=None, timeout_s=None, **args):
        return [EvidenceDraft(tenant_id=ctx.tenant_id, source_type="ticket", source_id="INC100", provenance="tenant_data", excerpt="sapdb01 error 5 occurred | group: unix")]
    return {"incident_kb_search": kb, "ticket_history_lookup": history}


class BatchRunnerTests(unittest.TestCase):
    def test_held_out_items_never_reach_the_planner_through_any_tool(self):
        filt = HeldOutFilter(HELD_OUT)
        reg = batch_runner.filtered_registry(filt, fake_tools())
        ctx = make_ctx("client_x")
        rows = reg.call(ctx, "incident_kb_search", {"query": "sapdb01 error", "top_k": 5}, 5)
        self.assertEqual([r.source_id for r in rows], ["chk_ok"])
        self.assertEqual(reg.call(ctx, "ticket_history_lookup", {"host": "sapdb01", "days": 400}, 5), [])
        self.assertGreaterEqual(filt.removed, 2)

    def test_an_item_that_cannot_be_classified_stops_the_run(self):
        filt = HeldOutFilter(HELD_OUT)
        blank = {"incident_kb_search": lambda ctx, *, cancel=None, timeout_s=None, **a: [EvidenceDraft(tenant_id=ctx.tenant_id, source_type="incident", source_id="x", provenance="tenant_data", excerpt="  ")]}
        reg = batch_runner.filtered_registry(filt, blank)
        with self.assertRaises(Exception):
            reg.call(make_ctx("client_x"), "incident_kb_search", {"query": "abc def", "top_k": 5}, 5)

    def test_an_empty_held_out_set_cannot_be_used(self):
        with self.assertRaises(Exception):
            HeldOutFilter([])

    def test_the_deterministic_retrieval_is_filtered_for_the_block_and_restored_after(self):
        class Engine:
            @staticmethod
            def retrieve_incidents(**kw):
                return [{"id": "chk_self", "title": "sapdb01 error 5 occurred", "summary": "x"}, {"id": "chk_ok", "title": "Tape job failed", "summary": "media error"}]

            @staticmethod
            def hybrid_search(**kw):
                return Engine.retrieve_incidents()
        original = Engine.retrieve_incidents
        with batch_runner.held_out_retrieval(HeldOutFilter(HELD_OUT), Engine):
            self.assertEqual([r["id"] for r in Engine.retrieve_incidents(tenant_id="client_x")], ["chk_ok"])
            self.assertEqual([r["id"] for r in Engine.hybrid_search(tenant_id="client_x")], ["chk_ok"])
            self.assertEqual([r["id"] for r in Engine.retrieve_incidents(tenant_id="client_y")], ["chk_self", "chk_ok"])        # another tenant's data is not hidden
        self.assertIs(Engine.retrieve_incidents, original)
        with self.assertRaises(BatchError):
            with batch_runner.held_out_retrieval(HeldOutFilter(HELD_OUT), object()):
                pass

    def run_cases(self, planner_status_script, n=4):
        from agentruntime.release import planner_factory, policy_for
        cases = [{"case_id": f"c{i}", "tenant_id": "client_x", "ticket_text": f"sapdb0{i} error {i} occurred"} for i in range(n)]
        reg = batch_runner.filtered_registry(HeldOutFilter(HELD_OUT), fake_tools())
        runner = Runner(reg, clock=FakeClock(), planner_executor=BoundedExecutor(4, 2, "bt-p"), model_executor=BoundedExecutor(4, 2, "bt-m"))
        calls = []
        real_run_batch = runner.run_batch

        def run_batch(ctx, policy, task, planner, run_id=None):
            result = real_run_batch(ctx, policy, task, planner, run_id)
            calls.append(task["ticket_text"])
            script = planner_status_script.get(task["ticket_text"], [])
            if len(calls) and script and calls.count(task["ticket_text"]) <= len(script):
                return result.model_copy(update={"status": script[calls.count(task["ticket_text"]) - 1][0], "reason": script[calls.count(task["ticket_text"]) - 1][1]})
            return result
        runner.run_batch = run_batch
        policy = policy_for("batch")
        return batch_runner.run_batch(cases, reg, lambda t, c: make_ctx(t), planner_factory, policy, runner=runner), calls

    def test_sequential_batch_mode_records_each_case_and_restores_the_environment(self):
        before = os.environ.get("AIOPS_AGENT_BATCH_ENABLED")
        out, calls = self.run_cases({})
        self.assertEqual([r["case_id"] for r in out["records"]], ["c0", "c1", "c2", "c3"])
        self.assertEqual(out["reruns"], [])
        self.assertFalse(out["deviation"])
        self.assertEqual(os.environ.get("AIOPS_AGENT_BATCH_ENABLED"), before)

    def test_an_infrastructure_failure_is_rerun_up_to_twice_and_logged_other_outcomes_are_never_rerun(self):
        script = {"sapdb00 error 0 occurred": [("failed_internal", "database connection refused"), ("failed_internal", "database connection refused"), ("escalated_insufficient", "ok")],
                  "sapdb01 error 1 occurred": [("failed_model", "model returned garbage")]}
        out, calls = self.run_cases(script, n=2)
        self.assertEqual(calls.count("sapdb00 error 0 occurred"), 3)
        self.assertEqual(calls.count("sapdb01 error 1 occurred"), 1)                         # model-behaviour failures are never re-run
        self.assertEqual(len(out["reruns"]), 2)
        self.assertEqual(out["records"][0]["attempts"], 3)
        self.assertEqual(out["records"][1]["result"]["status"], "failed_model")

    def test_a_third_infrastructure_failure_is_kept_not_retried_forever_and_many_reruns_flag_a_deviation(self):
        script = {f"sapdb0{i} error {i} occurred": [("failed_internal", "database unavailable")] * 5 for i in range(4)}
        out, calls = self.run_cases(script)
        self.assertEqual(len(calls), 12)                                                     # 4 cases x (1 + 2 re-runs)
        self.assertTrue(out["deviation"])
        self.assertEqual({r["result"]["status"] for r in out["records"]}, {"failed_internal"})

    def test_only_batch_policies_are_accepted(self):
        from agentruntime.release import policy_for
        with self.assertRaises(BatchError):
            batch_runner.run_batch([], None, None, None, policy_for("shadow"))

    def test_infrastructure_detection_is_a_fixed_list(self):
        class R:
            def __init__(self, status, reason):
                self.status, self.reason = status, reason
        self.assertTrue(batch_runner.is_infrastructure_failure(R("failed_internal", "OperationalError: server closed the connection")))
        self.assertFalse(batch_runner.is_infrastructure_failure(R("failed_internal", "a bug in the planner")))
        self.assertFalse(batch_runner.is_infrastructure_failure(R("failed_model", "database")))


def good_labels(cases, **per_case):
    out = {}
    for cid in cases:
        base = {"determinable": "determinable", "classification": "disk_full", "conflicting": False}
        out[cid] = {"labeler_1": dict(base), "labeler_2": dict(base), "adjudicated": dict(base)}
    out.update(per_case)
    return out


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        with valid_release():
            self.w.freeze()
        self.cases = [c["case_id"] for c in ar.load_cases(self.w.private)]
        self.labels = self.w.root / "labels.json"
        self.labels.write_text(json.dumps(good_labels(self.cases)), encoding="utf-8")
        self.results = self.w.root / "results"
        self.calls = []

    def executor(self, cases):
        self.calls.append(len(cases))
        return {"records": [{"case_id": c["case_id"]} for c in cases], "reruns": [], "deviation": False}

    def go(self):
        return ar.run_once(self.w.private, self.labels, self.results, self.executor, self.w.record_path, self.w.root)

    def test_the_run_happens_once_with_all_100_cases_and_the_output_is_hashed(self):
        summary = self.go()
        self.assertEqual((self.calls, summary["cases"]), ([100], 100))
        self.assertEqual(len(summary["outputs_sha256"]), 64)
        claim = json.loads(next(self.results.glob("*.claim.json")).read_text(encoding="utf-8"))
        self.assertEqual(claim["status"], "completed")

    def test_a_second_run_is_refused_even_after_a_failure(self):
        self.go()
        with self.assertRaisesRegex(ar.RunError, "run once"):
            self.go()
        self.assertEqual(self.calls, [100])
        w = World()
        with valid_release():
            w.freeze()
        cases = [c["case_id"] for c in ar.load_cases(w.private)]
        (w.root / "l.json").write_text(json.dumps(good_labels(cases)), encoding="utf-8")

        def boom(cases):
            raise RuntimeError("model server down")
        with self.assertRaises(RuntimeError):
            ar.run_once(w.private, w.root / "l.json", w.root / "res", boom, w.record_path, w.root)
        with self.assertRaisesRegex(ar.RunError, "failed"):
            ar.run_once(w.private, w.root / "l.json", w.root / "res", self.executor, w.record_path, w.root)         # the claim stays: no retry until the result looks acceptable

    def test_nothing_runs_if_the_freeze_no_longer_verifies(self):
        (self.w.root / af.PUBLIC_ITEMS["analysis_code"]).write_bytes((self.w.root / af.PUBLIC_ITEMS["analysis_code"]).read_bytes() + b"\n# tweak\n")
        with self.assertRaisesRegex(ar.RunError, "freeze does not verify"):
            self.go()
        self.assertEqual(self.calls, [])
        self.assertFalse(list(self.results.glob("*.claim.json")))                                        # a refused run consumes nothing

    def test_labels_must_be_complete_and_the_adjudicator_may_only_rule_on_disagreements(self):
        cases = self.cases
        incomplete = good_labels(cases)
        incomplete.pop(cases[0])
        self.labels.write_text(json.dumps(incomplete), encoding="utf-8")
        with self.assertRaisesRegex(ar.RunError, "exactly the 100"):
            self.go()
        overwritten = good_labels(cases)
        overwritten[cases[1]]["adjudicated"]["determinable"] = "not_determinable"                      # both labelers agreed on 'determinable'
        self.labels.write_text(json.dumps(overwritten), encoding="utf-8")
        with self.assertRaisesRegex(ar.RunError, "both labelers agreed"):
            self.go()
        no_reason = good_labels(cases)
        no_reason[cases[2]]["labeler_2"]["determinable"] = "not_determinable"
        no_reason[cases[2]]["adjudicated"]["determinable"] = "not_determinable"
        self.labels.write_text(json.dumps(no_reason), encoding="utf-8")
        with self.assertRaisesRegex(ar.RunError, "rationale"):
            self.go()
        no_reason[cases[2]]["adjudicated"]["rationale"] = "the closure notes show no cause was ever recorded"
        self.labels.write_text(json.dumps(no_reason), encoding="utf-8")
        self.go()                                                                                          # a documented adjudication of a real disagreement is accepted
        self.assertEqual(self.calls, [100])

    def test_the_cases_cover_80_real_and_20_golden_tickets(self):
        cases = ar.load_cases(self.w.private)
        self.assertEqual({s: sum(1 for c in cases if c["source"] == s) for s in ("real", "golden")}, {"real": 80, "golden": 20})
        self.assertTrue(all(c["case_id"].startswith("golden-acc-") for c in cases if c["source"] == "golden"))


if __name__ == "__main__":
    unittest.main()
