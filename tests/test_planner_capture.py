"""The official recording-capture command (design 20.16): held-out exclusion that fails closed, tenant safety, determinism, atomic output."""
import hashlib
import json
import pathlib
import random
import tempfile
import unittest
from unittest.mock import patch

from agentruntime.context import build_context
from agentruntime.contracts import AgentPolicy, Budgets, EvidenceDraft
from agentruntime.executor import BoundedExecutor
from agentruntime.planners.rules_only import RulesOnlyPlanner
from agentruntime.registry import ToolBusy, ToolRegistry, ToolTimeout
from agentruntime.runner import Runner
from agentruntime.tools import TOOL_MODULES
from planner_experiment import capture, exclusion, steps
from planner_experiment.capture import CaptureCancelled, CaptureError, CapturePrincipal
from planner_experiment.scripted_env import Recording, ScriptedEnv
from tests.agentruntime_helpers import FakeClock, batch_env, tenant_dir
from tests.pe_world import build_world

TENANTS = ("client_x", "client_y", "poc_client", "client_a", "client_c")
CONFIG = tenant_dir(*TENANTS)
TOOLS = [m.NAME for m in TOOL_MODULES]


def ctx_factory(tenant, case_id):
    return build_context(tenant, case_id, CapturePrincipal(tenant), "batch", config_dir=CONFIG)


def d(tenant, source_id, excerpt, relevance=0.5, classification=None, source_type="incident", **kw):
    facts = {"classification": classification} if classification else None
    return EvidenceDraft(tenant_id=tenant, source_type=source_type, source_id=source_id, provenance="tenant_data", excerpt=excerpt,
                         relevance=relevance, facts=facts, **kw)


CASE_X = {"case_id": "client_x:INC100", "pool": "client_x", "tenant_id": "client_x", "ticket_id": "INC100", "ticket_text": "sapdb01 error 5 occurred", "host": "sapdb01"}
CASE_Y = {"case_id": "client_y:INC200", "pool": "client_y", "tenant_id": "client_y", "ticket_id": "INC200", "ticket_text": "Disk full on orders volume", "host": ""}


def held(*cases):
    return [{"pool": c["pool"], "tenant_id": c["tenant_id"], "ticket_id": c["ticket_id"], "text": c["ticket_text"], "host": c["host"]} for c in cases]


def kb_rows(tenant):
    """Five rows (the tool's own cap), written for the tenant's own case so one tenant's fixtures never carry another's ticket text."""
    if tenant == "client_y":
        return [d(tenant, "chk_y_self", "Disk full on orders volume. the ticket itself", 0.95),
                d(tenant, "chk_y_a", "Archive job slow on warehouse. unrelated one", 0.6, "slow_job"),
                d(tenant, "chk_y_b", "Mail queue growing. unrelated two", 0.5),
                d(tenant, "chk_y_c", "Certificate near expiry. unrelated three", 0.4),
                d(tenant, "chk_y_d", "Printer offline. unrelated four", 0.3)]
    return [
        d(tenant, "chk_self", "sapdb01 error 5 occurred. the ticket itself", 0.95),
        d(tenant, "chk_cluster", "sapdb01 error 7 occurred. same host and alert", 0.9),
        d(tenant, "chk_near", "sapdb01 error 5 occurred again today. a near copy", 0.88),
        d(tenant, "chk_idref", "Follow up of INC100. unrelated wording", 0.7),
        d(tenant, "chk_sibling", "sapdb02 error 7 occurred. Root Cause: log growth; quota too small Resolution: purge", 0.6, "disk_full"),
    ]


def fake_registry(handlers=None, log=None, shuffle=None):
    """The five real tool specs, backed by deterministic fakes."""
    handlers = handlers or {}
    reg = ToolRegistry(executor=BoundedExecutor(4, 2, "t-capture"))

    def make(name):
        def fn(ctx, *, cancel=None, timeout_s=None, **args):
            if log is not None:
                log.append((name, ctx.tenant_id, dict(args)))
            handler = handlers.get(name)
            if handler is not None:
                rows = handler(ctx, args)
            elif name == "incident_kb_search":
                rows = kb_rows(ctx.tenant_id)
            elif name == "ticket_history_lookup":
                if ctx.tenant_id == "client_y":
                    rows = [d(ctx.tenant_id, "INC200", "Disk full on orders volume | group: ops", 0.0, source_type="ticket"),
                            d(ctx.tenant_id, "INC150", "Archive job slow | group: ops", 0.0, source_type="ticket")]
                else:
                    rows = [d(ctx.tenant_id, "INC100", "sapdb01 error 5 occurred | group: unix", 0.0, source_type="ticket"),
                            d(ctx.tenant_id, "INC055", "sapdb01 backup slow | group: unix", 0.0, source_type="ticket")]
            elif name == "outcome_lookup":
                rows = [d(ctx.tenant_id, args["classification"], f"3 recorded outcome(s) for {args['classification']}", source_type="outcome")]
            elif name == "pattern_lookup":
                rows = [d(ctx.tenant_id, args["classification"], f"Patterns for {args['classification']}", source_type="pattern")]
            else:
                rows = [d(ctx.tenant_id, "chk-change-window", "change window: open", source_type="evidence_check")]
            rows = list(rows)
            if shuffle is not None:
                random.Random(shuffle).shuffle(rows)
            return rows
        fn.__name__ = name
        return fn

    for m in TOOL_MODULES:
        reg.register(m.SPEC, make(m.NAME))
    return reg


def capture_to(out, cases=(CASE_X, CASE_Y), held_out=None, registry=None, **kw):
    kw.setdefault("mode", "development")
    kw.setdefault("warmup", False)
    return capture.run_capture([dict(c) for c in cases], held_out if held_out is not None else held(*cases), registry or fake_registry(), ctx_factory, out, **kw)


def tmp():
    return pathlib.Path(tempfile.mkdtemp(prefix="pe-capture-")) / "rec.json"


def rows_of(out, case_id, tool):
    data = json.loads(pathlib.Path(out).read_text(encoding="utf-8"))
    first = next(iter(data[case_id][tool].values()))
    return first


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.out = tmp()
        self.manifest = capture_to(self.out)
        self.kb = [r["source_id"] for r in rows_of(self.out, CASE_X["case_id"], "incident_kb_search")]

    def test_the_ticket_itself_is_never_recorded(self):
        self.assertNotIn("chk_self", self.kb)

    def test_a_near_duplicate_is_removed(self):
        self.assertNotIn("chk_near", self.kb)

    def test_a_cluster_mate_is_removed_and_a_sibling_host_is_kept(self):
        self.assertNotIn("chk_cluster", self.kb)                          # same host, same alert pattern
        self.assertIn("chk_sibling", self.kb)                             # another host: a different cluster by the draw's own definition

    def test_an_item_that_names_the_ticket_id_is_removed(self):
        self.assertNotIn("chk_idref", self.kb)
        history = [r["source_id"] for r in rows_of(self.out, CASE_X["case_id"], "ticket_history_lookup")]
        self.assertNotIn("INC100", history)
        self.assertIn("INC055", history)

    def test_other_cases_keep_their_own_results_and_the_proof_counts_the_removals(self):
        proofs = {c["tool"]: c for c in self.manifest["cases"][CASE_X["case_id"]]["calls"] if c["tool"] == "incident_kb_search"}
        first = self.manifest["cases"][CASE_X["case_id"]]["calls"][0]
        self.assertEqual((first["returned"], first["kept"]), (5, 1))
        self.assertEqual(sum(first["dropped"].values()), 4)
        self.assertEqual(first["dropped"]["ticket_id"], 1)
        self.assertTrue(proofs)

    def test_every_held_out_ticket_is_removed_from_every_case(self):
        # case Y's search returns case X's ticket too: the whole held-out set is excluded everywhere in its own tenant only
        out = tmp()
        capture_to(out, registry=fake_registry({"incident_kb_search": lambda ctx, a: [d(ctx.tenant_id, "chk_other_ticket", "sapdb01 error 5 occurred. the other case's ticket", 0.9),
                                                                                       d(ctx.tenant_id, "chk_ok", "something else entirely different", 0.4)]}))
        ids_y = [r["source_id"] for r in rows_of(out, CASE_Y["case_id"], "incident_kb_search")]
        ids_x = [r["source_id"] for r in rows_of(out, CASE_X["case_id"], "incident_kb_search")]
        self.assertEqual(ids_x, ["chk_ok"])
        self.assertEqual(ids_y, ["chk_other_ticket", "chk_ok"])            # X's ticket text in tenant Y's data is not X's record: tenants never hide each other

    def test_the_recording_and_manifest_do_not_contain_the_tickets_text(self):
        blob = self.out.read_text(encoding="utf-8") + capture.manifest_path(self.out).read_text(encoding="utf-8")
        for text in (CASE_X["ticket_text"], CASE_Y["ticket_text"]):
            self.assertNotIn(text, blob)

    def test_the_self_test_recognises_every_held_out_ticket(self):
        exclusion.Matcher(held(CASE_X, CASE_Y)).self_test()


class FailClosedTests(unittest.TestCase):
    def test_an_empty_held_out_set_cannot_be_proven(self):
        out = tmp()
        with self.assertRaisesRegex(CaptureError, "exclusion cannot be proven"):
            capture_to(out, held_out=[])
        self.assertFalse(out.exists() or capture.manifest_path(out).exists())

    def test_a_case_whose_ticket_is_not_held_out_is_refused(self):
        out = tmp()
        with self.assertRaisesRegex(CaptureError, "not covered"):
            capture_to(out, held_out=held(CASE_X))                       # case Y is not in the held-out set
        self.assertFalse(out.exists())

    def test_a_held_out_ticket_without_text_is_refused(self):
        out = tmp()
        with self.assertRaisesRegex(CaptureError, "no tenant or no text"):
            capture_to(out, held_out=held(CASE_X, CASE_Y) + [{"pool": "client_x", "tenant_id": "client_x", "ticket_id": "INC9", "text": "", "host": ""}])

    def test_an_item_that_cannot_be_classified_stops_the_capture_and_is_not_kept(self):
        out = tmp()
        bad = fake_registry({"incident_kb_search": lambda ctx, a: [d(ctx.tenant_id, "chk_blank", "   ", 0.5)]})
        with self.assertRaisesRegex(CaptureError, "could not be decided"):
            capture_to(out, registry=bad)
        self.assertFalse(out.exists() or capture.manifest_path(out).exists())

    def test_a_foreign_tenant_item_stops_the_capture(self):
        out = tmp()
        leaky = fake_registry({"incident_kb_search": lambda ctx, a: [d("client_y" if ctx.tenant_id == "client_x" else "client_x", "chk_foreign", "another tenant's incident", 0.9)]})
        with self.assertRaisesRegex(CaptureError, "another tenant"):
            capture_to(out, registry=leaky)
        self.assertFalse(out.exists())

    def test_the_capture_rechecks_the_tenant_even_if_the_registry_did_not(self):
        class Lax:
            def call(self, ctx, tool, args, timeout_s):
                return [d("client_y", "x1", "evidence from elsewhere", 0.5).model_copy(update={"tool": tool})]
        with self.assertRaisesRegex(CaptureError, "another tenant|was returned to a capture"):
            capture_to(tmp(), registry=Lax())

    def test_a_context_for_the_wrong_tenant_is_refused(self):
        with self.assertRaisesRegex(CaptureError, "does not match the case"):
            capture.run_capture([dict(CASE_X)], held(CASE_X), fake_registry(), lambda t, c: ctx_factory("client_y", c), tmp(), mode="development", warmup=False)

    def test_modes_and_key_rules(self):
        with self.assertRaisesRegex(CaptureError, "needs the active registered experiment key"):
            capture_to(tmp(), mode="experiment")
        with self.assertRaisesRegex(CaptureError, "does not use the experiment key"):
            capture_to(tmp(), mode="development", key=b"k" * 32, key_check="abc")
        with self.assertRaisesRegex(CaptureError, "unknown capture mode"):
            capture_to(tmp(), mode="whatever")

    def test_duplicate_or_missing_case_ids_and_empty_input_are_refused(self):
        with self.assertRaises(CaptureError):
            capture_to(tmp(), cases=[CASE_X, CASE_X])
        with self.assertRaises(CaptureError):
            capture_to(tmp(), cases=[])
        with self.assertRaises(CaptureError):
            capture_to(tmp(), cases=[{**CASE_X, "ticket_text": ""}])


class InterruptionTests(unittest.TestCase):
    def assert_nothing_usable(self, out):
        self.assertFalse(out.exists(), "a recording exists")
        self.assertFalse(capture.manifest_path(out).exists(), "a manifest exists")
        self.assertEqual([p.name for p in out.parent.iterdir()], [], "leftovers in the folder")

    def test_a_tool_timeout_is_retried_once_then_the_capture_stops(self):
        out, attempts = tmp(), []

        def always(ctx, a):
            attempts.append(1)
            raise ToolTimeout("kb exceeded")
        with self.assertRaisesRegex(CaptureError, "did not complete after 2 attempts"):
            capture_to(out, registry=self.registry_raising("incident_kb_search", ToolTimeout("kb exceeded")))
        self.assert_nothing_usable(out)

    @staticmethod
    def registry_raising(tool, exc, first_n=None):
        counter = {"n": 0}

        def handler(ctx, a):
            counter["n"] += 1
            if first_n is None or counter["n"] <= first_n:
                raise exc
            return kb_rows(ctx.tenant_id)
        reg = fake_registry({tool: handler})
        original = reg.call

        def call(ctx, name, args, timeout_s):
            try:
                return original(ctx, name, args, timeout_s)
            except Exception as err:                                   # the registry wraps handler errors; surface the intended type
                if name == tool and isinstance(err.__cause__ or err, type(exc)):
                    raise exc
                raise
        reg.call = call
        return reg

    def test_a_single_timeout_is_absorbed_by_the_retry(self):
        out = tmp()
        capture_to(out, registry=self.registry_raising("incident_kb_search", ToolBusy("busy"), first_n=1))
        capture.verify_capture(out)

    def test_a_failure_part_way_leaves_nothing_usable(self):
        out = tmp()
        broken = fake_registry({"outcome_lookup": lambda ctx, a: (_ for _ in ()).throw(RuntimeError("ledger down"))})
        with self.assertRaisesRegex(CaptureError, "ledger down|call failed"):
            capture_to(out, registry=broken)
        self.assert_nothing_usable(out)

    def test_cancellation_discards_the_partial_work(self):
        out, seen = tmp(), {"n": 0}

        def cancel():
            seen["n"] += 1
            return seen["n"] > 3
        with self.assertRaises(CaptureCancelled):
            capture_to(out, cancel=cancel)
        self.assert_nothing_usable(out)

    def test_a_crash_while_publishing_does_not_leave_a_recording_that_looks_complete(self):
        out = tmp()
        real_replace, calls = capture.os.replace, {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk went away")
            return real_replace(src, dst)
        with patch.object(capture.os, "replace", flaky):
            with self.assertRaises(OSError):
                capture_to(out)
        with self.assertRaises(CaptureError):
            capture.verify_capture(out)                                   # the recording may exist; without its manifest it is not a capture
        capture_to(out)                                                   # a rerun is allowed: the earlier attempt never completed
        capture.verify_capture(out)
        log = json.loads(out.with_name(out.name + ".invalidations.json").read_text(encoding="utf-8")) if out.with_name(out.name + ".invalidations.json").exists() else []
        self.assertTrue(all(e["complete"] is False for e in log))

    def test_leftover_partial_files_are_not_mistaken_for_a_recording(self):
        out = tmp()
        out.with_name(out.name + ".partial").write_text("{garbage", encoding="utf-8")
        capture_to(out)
        capture.verify_capture(out)
        self.assertFalse(out.with_name(out.name + ".partial").exists())


class OverwriteTests(unittest.TestCase):
    def test_an_existing_recording_is_not_overwritten(self):
        out = tmp()
        capture_to(out)
        before = out.read_bytes()
        with self.assertRaisesRegex(CaptureError, "never overwritten"):
            capture_to(out)
        self.assertEqual(out.read_bytes(), before)

    def test_replacing_needs_a_reason_and_a_name_and_is_recorded(self):
        out = tmp()
        first = capture_to(out)
        with self.assertRaises(CaptureError):
            capture_to(out, invalidate="because")                        # no 'by'
        with self.assertRaises(CaptureError):
            capture_to(out, invalidate="  ", by="Owner")
        capture_to(out, invalidate="tool version changed", by="Owner")
        log = json.loads(out.with_name(out.name + ".invalidations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(log), 1)
        self.assertEqual((log[0]["reason"], log[0]["by"], log[0]["previous_recording_sha256"]), ("tool version changed", "Owner", first["recording_sha256"]))
        self.assertTrue(out.with_name(out.name + ".invalidated-1").exists())
        capture_to(out, invalidate="second", by="Owner")
        self.assertEqual(len(json.loads(out.with_name(out.name + ".invalidations.json").read_text(encoding="utf-8"))), 2)


class DeterminismAndReplayTests(unittest.TestCase):
    def test_the_same_inputs_give_the_same_bytes_whatever_order_the_tools_answer_in(self):
        a, b, c = tmp(), tmp(), tmp()
        ma = capture_to(a, registry=fake_registry(shuffle=1))
        mb = capture_to(b, registry=fake_registry(shuffle=2))
        mc = capture_to(c, registry=fake_registry(), cases=(CASE_Y, CASE_X))               # case order in the input does not matter either
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(a.read_bytes(), c.read_bytes())
        self.assertEqual(ma["recording_sha256"], mb["recording_sha256"])
        self.assertEqual({k: v for k, v in ma.items() if k != "created_at"}, {k: v for k, v in mc.items() if k != "created_at"})

    def test_output_is_lf_only_ascii_and_the_hash_in_the_manifest_matches(self):
        out = tmp()
        manifest = capture_to(out)
        blob = out.read_bytes()
        self.assertNotIn(b"\r", blob)
        blob.decode("ascii")
        self.assertEqual(hashlib.sha256(blob).hexdigest(), manifest["recording_sha256"])

    def test_kept_rows_are_sorted_and_sanitised(self):
        out = tmp()
        capture_to(out, registry=fake_registry({"incident_kb_search": lambda ctx, a: [
            d(ctx.tenant_id, "b", "other incident one. Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789 was in the log", 0.4),
            d(ctx.tenant_id, "a", "other incident two. harmless text", 0.4),
            d(ctx.tenant_id, "c", "other incident three", 0.9)]}))
        rows = rows_of(out, CASE_X["case_id"], "incident_kb_search")
        self.assertEqual([r["source_id"] for r in rows], ["c", "a", "b"])         # by relevance, then id
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", json.dumps(rows))

    def test_unsafe_source_ids_are_replaced_by_stable_unique_tokens(self):
        out = tmp()
        capture_to(out, registry=fake_registry({"incident_kb_search": lambda ctx, a: [
            d(ctx.tenant_id, "weird id/with spaces?1", "alpha unrelated item", 0.5), d(ctx.tenant_id, "weird id/with spaces?2", "beta unrelated item", 0.4)]}))
        ids = [r["source_id"] for r in rows_of(out, CASE_X["case_id"], "incident_kb_search")]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(i.startswith("src_") for i in ids))

    def test_replaying_the_recording_gives_the_same_investigation_every_time_and_for_every_capture(self):
        def investigate(path):
            rec = Recording.load(path)
            env = ScriptedEnv(rec)
            runner = Runner(env.registry(), clock=FakeClock(), planner_executor=BoundedExecutor(4, 2, "t-r-planner"), model_executor=BoundedExecutor(4, 2, "t-r-model"))
            policy = AgentPolicy(allowed_tools=TOOLS, budgets=Budgets(), mode="batch", planner="rules_only")
            with batch_env():
                res = runner.run_batch(ctx_factory("client_x", CASE_X["case_id"]), policy, {"ticket_text": CASE_X["ticket_text"]}, RulesOnlyPlanner({}))
            return res.status, [(s.kind, s.tool, s.tool_input) for s in res.steps], [(h.cause, h.status, h.evidence) for h in res.hypotheses], dict(env.stats[CASE_X["case_id"]])
        a, b = tmp(), tmp()
        capture_to(a)
        capture_to(b, registry=fake_registry(shuffle=7))
        first = investigate(a)
        self.assertEqual(first, investigate(a))
        self.assertEqual(first, investigate(b))
        self.assertEqual(first[3]["missing"], 0)
        self.assertEqual(first[3]["default"], 0, "the planner's own natural calls are all recorded exactly")

    def test_the_calls_recorded_are_the_natural_ones(self):
        log = []
        capture_to(tmp(), cases=(CASE_X,), registry=fake_registry(log=log))
        tools = [t for t, _, _ in log]
        self.assertEqual(tools[0], "incident_kb_search")
        self.assertEqual(set(tools), {"incident_kb_search", "ticket_history_lookup", "outcome_lookup", "pattern_lookup", "evidence_lookup"})
        self.assertIn(("ticket_history_lookup", "client_x", {"host": "sapdb01", "days": 400}), log)
        self.assertTrue(all(t == "client_x" for _, t, _ in log))                     # the tenant is the case's, always


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.out = tmp()
        self.manifest = capture_to(self.out)

    def test_a_complete_capture_verifies(self):
        m = capture.verify_capture(self.out, expected_case_ids=[CASE_X["case_id"], CASE_Y["case_id"]], expected_mode="development")
        self.assertEqual(m["recording_sha256"], self.manifest["recording_sha256"])

    def test_an_altered_recording_is_refused(self):
        self.out.write_bytes(self.out.read_bytes().replace(b"chk_sibling", b"chk_sibling2", 1))
        with self.assertRaisesRegex(CaptureError, "does not match its manifest"):
            capture.verify_capture(self.out)

    def test_a_missing_manifest_or_recording_is_refused(self):
        capture.manifest_path(self.out).unlink()
        with self.assertRaisesRegex(CaptureError, "not a complete capture"):
            capture.verify_capture(self.out)

    def test_wrong_cases_mode_or_key_are_refused(self):
        with self.assertRaisesRegex(CaptureError, "exactly the cases"):
            capture.verify_capture(self.out, expected_case_ids=[CASE_X["case_id"]])
        with self.assertRaisesRegex(CaptureError, "mode"):
            capture.verify_capture(self.out, expected_mode="experiment")
        with self.assertRaisesRegex(CaptureError, "different experiment key"):
            capture.verify_capture(self.out, expected_key_check="deadbeef")

    def test_a_manifest_that_is_not_marked_complete_or_has_no_proof_is_refused(self):
        path = capture.manifest_path(self.out)
        m = json.loads(path.read_text(encoding="utf-8"))
        for change in ({"complete": False}, {"exclusion_proof": {"self_test": "ok"}}, {"capture_version": "0"}, {"tools": {}}):
            path.write_text(json.dumps({**m, **change}), encoding="utf-8")
            with self.assertRaises(CaptureError, msg=str(change)):
                capture.verify_capture(self.out)

    def test_experiment_digests_are_keyed(self):
        key = b"\x07" * 32
        out = tmp()
        m = capture_to(out, mode="experiment", key=key, key_check="check")
        plain = capture_to(tmp())
        self.assertNotEqual(m["held_out_digest"], plain["held_out_digest"])
        self.assertEqual(capture.verify_capture(out, expected_key_check="check")["mode"], "experiment")


def neutral_registry():
    return fake_registry({"incident_kb_search": lambda ctx, a: [d(ctx.tenant_id, "chk_neutral", "an unrelated earlier incident about mail routing", 0.5)]})


def neutral_ctx(tenant, case_id):
    return build_context(tenant, case_id, CapturePrincipal(tenant), "batch", config_dir=tenant_dir(tenant))


class StepsTests(unittest.TestCase):
    """The glue: held-out sets come from the recorded draws, the key rules are enforced, and a hidden-set recording cannot be written into the repository."""

    @classmethod
    def setUpClass(cls):
        cls.world = build_world()

    registry = staticmethod(neutral_registry)
    ctx = staticmethod(neutral_ctx)

    def test_the_development_capture_needs_no_key_and_covers_exactly_the_development_cases(self):
        from planner_experiment import secrecy
        w, out = self.world, tmp()
        with patch.dict("os.environ", {secrecy.KEY_ENV: ""}), patch.object(secrecy, "KEY_FILE", pathlib.Path(tempfile.mkdtemp()) / "none.key"):
            manifest = steps.capture_development(w.base, w.tickets, out, registry=self.registry(), ctx_factory=self.ctx, warmup=False)
        dev_cases = steps.build_cases(steps.paths(w.base)["development"], w.tickets)
        self.assertEqual(manifest["mode"], "development")
        self.assertEqual(set(manifest["cases"]), {c["case_id"] for c in dev_cases})
        self.assertEqual(manifest["held_out_count"], len(dev_cases))
        capture.verify_capture(out, expected_case_ids=[c["case_id"] for c in dev_cases], expected_mode="development")

    def test_the_experiment_capture_uses_the_active_key_and_holds_out_the_round_and_the_development_subset(self):
        w, out = self.world, tmp()
        manifest = steps.capture_experiment(w.base, 1, w.tickets, out, registry=self.registry(), ctx_factory=self.ctx, warmup=False)
        dev_cases = steps.build_cases(steps.paths(w.base)["development"], w.tickets)
        self.assertEqual(manifest["mode"], "experiment")
        self.assertEqual(set(manifest["cases"]), {c["case_id"] for c in w.cases})
        self.assertEqual(manifest["held_out_count"], len(w.cases) + len(dev_cases))
        self.assertTrue(manifest["key_check"])

    def test_the_experiment_capture_refuses_the_repository_as_a_destination(self):
        w = self.world
        with self.assertRaisesRegex(steps.StepError, "outside the repository"):
            steps.capture_experiment(w.base, 1, w.tickets, steps.ROOT / "data" / "x_recording.json", registry=self.registry(), ctx_factory=self.ctx, warmup=False)
        self.assertFalse((steps.ROOT / "data" / "x_recording.json").exists())

    def test_the_experiment_capture_needs_a_draw_a_planner_freeze_and_a_usable_key(self):
        from planner_experiment import secrecy
        w = self.world
        with self.assertRaisesRegex(steps.StepError, "has not been drawn"):
            steps.capture_experiment(w.base, 2, w.tickets, tmp(), registry=self.registry(), ctx_factory=self.ctx, warmup=False)
        with patch.dict("os.environ", {secrecy.KEY_ENV: ""}), patch.object(secrecy, "KEY_FILE", pathlib.Path(tempfile.mkdtemp()) / "none.key"):
            with self.assertRaises(steps.StepError):
                steps.capture_experiment(w.base, 1, w.tickets, tmp(), registry=self.registry(), ctx_factory=self.ctx, warmup=False)

    def test_a_failed_capture_is_reported_as_a_refusal(self):
        w = self.world
        broken = fake_registry({"incident_kb_search": lambda ctx, a: [d(ctx.tenant_id, "chk_blank", "  ", 0.5)]})
        with self.assertRaisesRegex(steps.StepError, "was not made"):
            steps.capture_development(w.base, w.tickets, tmp(), registry=broken, ctx_factory=self.ctx, warmup=False)


class FreezeIntegrationTests(unittest.TestCase):
    """The experiment freeze accepts only a recording made by this command, unaltered, for this draw, under the key in custody."""

    def fresh(self):
        return build_world()

    def man(self, w):
        return json.loads(w.files["recording_manifest"].read_text(encoding="utf-8"))

    def write_man(self, w, manifest):
        w.files["recording_manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    def test_a_world_with_an_official_capture_freezes(self):
        self.fresh().freeze()

    def test_the_manifest_is_a_required_frozen_input(self):
        from planner_experiment.freeze import FreezeError, REQUIRED_FILES
        self.assertIn("recording_manifest", REQUIRED_FILES)
        w = self.fresh()
        del w.files["recording_manifest"]
        with self.assertRaisesRegex(FreezeError, "missing"):
            w.freeze()

    def test_an_altered_recording_is_refused(self):
        from planner_experiment.freeze import FreezeError
        w = self.fresh()
        w.files["recording"].write_text(w.files["recording"].read_text(encoding="utf-8").replace("disk full", "disk empty", 1), encoding="utf-8")
        with self.assertRaisesRegex(FreezeError, "not an official capture"):
            w.freeze()

    def test_a_development_capture_cannot_stand_in_for_the_experiment(self):
        from planner_experiment.freeze import FreezeError
        w = self.fresh()
        self.write_man(w, {**self.man(w), "mode": "development"})
        with self.assertRaisesRegex(FreezeError, "mode"):
            w.freeze()

    def test_a_capture_for_another_draw_or_key_is_refused(self):
        from planner_experiment.freeze import FreezeError
        w = self.fresh()
        self.write_man(w, {**self.man(w), "selection_sha256": "0" * 64})
        with self.assertRaisesRegex(FreezeError, "different draw"):
            w.freeze()
        w = self.fresh()
        self.write_man(w, {**self.man(w), "key_check": "f" * 16})
        with self.assertRaisesRegex(FreezeError, "different experiment key"):
            w.freeze()

    def test_a_hand_made_recording_without_a_real_manifest_is_refused(self):
        from planner_experiment.freeze import FreezeError
        w = self.fresh()
        self.write_man(w, {"complete": True})
        with self.assertRaises(FreezeError):
            w.freeze()

    def test_the_recording_and_manifest_hold_no_ticket_text_from_the_cases(self):
        w = self.fresh()
        blob = w.files["recording"].read_text(encoding="utf-8") + w.files["recording_manifest"].read_text(encoding="utf-8")
        for c in w.cases:
            self.assertNotIn(c["ticket_text"], blob)


class CommandLineTests(unittest.TestCase):
    def test_the_commands_exist_and_are_wired(self):
        from planner_experiment import cli
        w = build_world()
        out = tmp()
        with patch.object(cli, "load_tickets", lambda names: w.tickets), patch.object(steps, "BASE", w.base), \
                patch.object(capture, "real_registry", neutral_registry), patch.object(capture, "default_context", neutral_ctx),                 patch.object(capture, "run_capture", wraps=capture.run_capture) as spy:
            self.assertEqual(cli.main(["capture-development", "--out", str(out)]), 0)
            self.assertEqual(cli.main(["capture-development", "--out", str(out)]), 2)           # never overwritten
            self.assertEqual(cli.main(["capture-development", "--out", str(out), "--invalidate", "re-run", "--by", "Owner"]), 0)
            self.assertEqual(spy.call_args.kwargs["mode"], "development")
        capture.verify_capture(out)


if __name__ == "__main__":
    unittest.main()
