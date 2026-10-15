"""
R2-1: the five read-only tools (design 4.3, acceptance requirements).

For every tool: two-tenant isolation, timeout, cancellation and bounded results. Plus the boundary rules: tenant only from the trusted
context, statement timeouts bounded by the call's time, no unmanaged threads, and only the tool modules touch retrieval or databases.

Live PostgreSQL cases run only with AIOPS_TEST_AGENT_PG=1:
    AIOPS_TEST_AGENT_PG=1 python -m unittest tests.test_agentruntime_tools -v
"""
import json
import os
import pathlib
import re
import tempfile
import threading
import time
import unittest
import uuid

from agentruntime import db_limits
from agentruntime.contracts import ToolSpec
from agentruntime.executor import BoundedExecutor, CancelToken, Cancelled
from agentruntime.registry import BoundaryViolation, RegistryError, ToolInputError, ToolRegistry, ToolTimeout
from agentruntime.tools import TOOL_NAMES, build_default_registry, evidence, kb_search, outcomes, patterns, ticket_history
from agentruntime.tools._common import like_pattern
from tests.agentruntime_helpers import make_ctx

LIVE = os.getenv("AIOPS_TEST_AGENT_PG") == "1"


def executor():
    return BoundedExecutor(max_workers=4, max_abandoned=2, name="t-tool", max_leaked=2, leak_after_s=5.0)


def cancelled_token():
    token = CancelToken()
    token.cancel()
    return token


# -- fake backends: they return data for the tenant they were asked about, and record what they were asked ------------------------------

class FakeRetrieve:
    def __init__(self, rows_per_tenant=None, n=1, delay=None, leak_other=False):
        self.calls, self.n, self.delay, self.leak_other = [], n, delay, leak_other
        self.rows = rows_per_tenant or {}

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.delay:
            self.delay.wait(10)
        tenant = kw["tenant_id"]
        rows = self.rows.get(tenant) or [
            {"id": f"{tenant}-{i}", "title": f"{tenant} incident {i}", "summary": f"summary {i}", "classification": "disk_full",
             "score": 0.5 + i / 100, "knowledge_scope": "client_private", "tenant_id": tenant} for i in range(self.n)]
        if self.leak_other:
            rows = rows + [{"id": "foreign", "title": "other tenant", "knowledge_scope": "client_private", "tenant_id": "someone_else"}]
        return rows


class IncidentKbSearchTests(unittest.TestCase):
    def setUp(self):
        self.ex = executor()

    def reg(self, retrieve):
        return build_default_registry(self.ex, retrieve=retrieve)

    def test_tenant_and_scope_come_only_from_the_trusted_context(self):
        fake = FakeRetrieve()
        reg = self.reg(fake)
        a = reg.call(make_ctx("client_x"), "incident_kb_search", {"query": "disk full on sapdb01"}, 5)
        b = reg.call(make_ctx("client_y"), "incident_kb_search", {"query": "disk full on sapdb01"}, 5)
        self.assertEqual([c["tenant_id"] for c in fake.calls], ["client_x", "client_y"])
        for call in fake.calls:
            self.assertEqual(call["knowledge_scope"], "client_private")
            self.assertIsNone(call["search_order"])
            self.assertIsNone(call["collection_name"])
        self.assertTrue(all(d.tenant_id == "client_x" for d in a))
        self.assertTrue(all(d.tenant_id == "client_y" for d in b))
        self.assertNotIn("client_y", " ".join(d.excerpt for d in a))

    def test_model_cannot_supply_tenant_scope_or_collection(self):
        reg = self.reg(FakeRetrieve())
        for bad in ({"tenant_id": "client_y"}, {"knowledge_scope": "global"}, {"collection_name": "client_y_index"}, {"search_order": ["x"]},
                    {"timeout_s": 999}, {"cancel": None}):
            with self.assertRaises(ToolInputError):
                reg.call(make_ctx("client_x"), "incident_kb_search", {"query": "disk full", **bad}, 5)

    def test_a_private_row_of_another_tenant_is_a_boundary_violation(self):
        reg = self.reg(FakeRetrieve(leak_other=True))
        with self.assertRaises(BoundaryViolation):
            reg.call(make_ctx("client_x"), "incident_kb_search", {"query": "disk full"}, 5)

    def test_results_are_bounded(self):
        fake = FakeRetrieve(n=50)
        reg = self.reg(fake)
        out = reg.call(make_ctx("client_x"), "incident_kb_search", {"query": "disk full", "top_k": 5}, 5)
        self.assertLessEqual(len(out), kb_search.MAX_ITEMS)
        self.assertEqual(fake.calls[0]["limit"], 5)
        with self.assertRaises(ToolInputError):
            reg.call(make_ctx("client_x"), "incident_kb_search", {"query": "disk full", "top_k": 6}, 5)
        with self.assertRaises(ToolInputError):
            reg.call(make_ctx("client_x"), "incident_kb_search", {"query": "x" * 301}, 5)

    def test_an_endless_backend_result_is_cut_off_before_it_is_materialised(self):
        pulled = []

        def endless(**kw):
            i = 0
            while True:
                pulled.append(i)
                yield {"id": f"r{i}", "title": "t", "summary": "s", "score": 0.5, "knowledge_scope": "client_private", "tenant_id": kw["tenant_id"]}
                i += 1

        out = self.reg(endless).call(make_ctx("client_x"), "incident_kb_search", {"query": "disk full", "top_k": 3}, 5)
        self.assertEqual(len(out), 3)
        self.assertLessEqual(len(pulled), 4)                    # only what was needed (one extra pull at most), not the whole stream

    def test_a_huge_list_is_not_walked_past_the_cap(self):
        class Counting(list):
            reads = 0

            def __iter__(self):
                for item in super().__iter__():
                    Counting.reads += 1
                    yield item

        rows = Counting({"id": f"r{i}", "title": "t", "knowledge_scope": "client_private", "tenant_id": "client_x"} for i in range(10000))
        out = kb_search.make_tool(lambda **kw: rows)(make_ctx("client_x"), "disk full", 2)
        self.assertEqual(len(out), 2)
        self.assertLessEqual(Counting.reads, 2)

    def test_long_text_is_clipped_and_scores_are_clamped(self):
        rows = {"client_x": [{"id": "1", "title": "t" * 900, "summary": "s" * 900, "score": 7.3, "knowledge_scope": "client_private"}]}
        out = self.reg(FakeRetrieve(rows)).call(make_ctx("client_x"), "incident_kb_search", {"query": "disk full"}, 5)
        self.assertLessEqual(len(out[0].excerpt), 500)
        self.assertEqual(out[0].relevance, 1.0)

    def test_timeout(self):
        gate = threading.Event()
        reg = self.reg(FakeRetrieve(delay=gate))
        started = time.monotonic()
        with self.assertRaises(ToolTimeout):
            reg.call(make_ctx("client_x"), "incident_kb_search", {"query": "disk full"}, 0.3)
        self.assertLess(time.monotonic() - started, 3)
        gate.set()

    def test_cancellation(self):
        fake = FakeRetrieve()
        tool = kb_search.make_tool(fake)
        with self.assertRaises(Cancelled):
            tool(make_ctx("client_x"), "disk full", 3, cancel=cancelled_token())
        self.assertEqual(fake.calls, [])                       # cancelled before any backend work

    def test_statement_timeout_is_set_from_the_call_time_and_reset_after(self):
        seen = []

        def retrieve(**kw):
            seen.append(db_limits.STATEMENT_TIMEOUT_MS.get())
            return []

        tool = kb_search.make_tool(retrieve)
        tool(make_ctx("client_x"), "disk full", 3, timeout_s=6.0)
        self.assertEqual(seen, [5750])
        self.assertIsNone(db_limits.STATEMENT_TIMEOUT_MS.get())


class OutcomeAndPatternTests(unittest.TestCase):
    def setUp(self):
        self.ex = executor()

    def test_outcome_lookup_is_tenant_scoped_and_reports_absence(self):
        calls = []

        def summarize(classification, tenant_id=None):
            calls.append((classification, tenant_id))
            return {"total_outcomes": 4, "success_rate": 0.75, "recurrence_rate": 0.1, "confidence_signal": "medium"} if tenant_id == "client_x" else None

        reg = build_default_registry(self.ex, summarize=summarize)
        a = reg.call(make_ctx("client_x"), "outcome_lookup", {"classification": "disk_full"}, 5)
        b = reg.call(make_ctx("client_y"), "outcome_lookup", {"classification": "disk_full"}, 5)
        self.assertEqual(calls, [("disk_full", "client_x"), ("disk_full", "client_y")])
        self.assertEqual(a[0].facts["total_outcomes"], 4)
        self.assertEqual(b[0].facts["total_outcomes"], 0)
        self.assertIn("No recorded outcomes", b[0].excerpt)
        self.assertEqual(len(a), 1)

    def test_outcome_inputs_are_strict(self):
        reg = build_default_registry(self.ex, summarize=lambda c, tenant_id=None: None)
        for bad in ({"classification": "Disk Full!"}, {"classification": "disk_full", "tenant_id": "client_y"}, {}):
            with self.assertRaises(ToolInputError):
                reg.call(make_ctx("client_x"), "outcome_lookup", bad, 5)

    def test_outcome_timeout_and_cancel(self):
        gate = threading.Event()
        reg = build_default_registry(self.ex, summarize=lambda c, tenant_id=None: gate.wait(10))
        with self.assertRaises(ToolTimeout):
            reg.call(make_ctx("client_x"), "outcome_lookup", {"classification": "disk_full"}, 0.3)
        gate.set()
        with self.assertRaises(Cancelled):
            outcomes.make_tool(lambda c, tenant_id=None: None)(make_ctx("client_x"), "disk_full", cancel=cancelled_token())

    def test_pattern_lookup_always_passes_the_trusted_tenant_and_flags_shared_records(self):
        calls = []

        def intelligence(classification, tenant_id=None, strict_tenant=False):
            calls.append((tenant_id, strict_tenant))
            return {"total_resolutions": 9, "candidate_count": 12, "dominant_root_causes": ["a", "b", "c", "d", "e"],
                    "dominant_actions": ["x"] * 9, "pattern_confidence": "high", "recurrence_trend": "stable", "insight": "disk issues"}

        reg = build_default_registry(self.ex, intelligence=intelligence)
        out = reg.call(make_ctx("client_y"), "pattern_lookup", {"classification": "disk_full"}, 5)
        self.assertEqual(calls, [("client_y", True)])           # strict: untagged records are never counted
        self.assertLessEqual(len(out[0].facts["dominant_root_causes"]), patterns.MAX_LISTED)
        self.assertLessEqual(len(out[0].facts["dominant_actions"]), patterns.MAX_LISTED)
        self.assertEqual(len(out), 1)

    def test_pattern_absence_timeout_and_cancel(self):
        reg = build_default_registry(self.ex, intelligence=lambda c, tenant_id=None, strict_tenant=False: None)
        out = reg.call(make_ctx("client_x"), "pattern_lookup", {"classification": "disk_full"}, 5)
        self.assertEqual(out[0].facts["total_resolutions"], 0)
        gate = threading.Event()
        slow = build_default_registry(self.ex, intelligence=lambda c, tenant_id=None, strict_tenant=False: gate.wait(10))
        with self.assertRaises(ToolTimeout):
            slow.call(make_ctx("client_x"), "pattern_lookup", {"classification": "disk_full"}, 0.3)
        gate.set()
        with self.assertRaises(Cancelled):
            patterns.make_tool(lambda c, tenant_id=None, strict_tenant=False: None)(make_ctx("client_x"), "disk_full", cancel=cancelled_token())


class StrictPatternIntelligenceTests(unittest.TestCase):
    """The real knowledge_manager function with strict_tenant: untagged and other-tenant records never count."""

    def setUp(self):
        import knowledge_manager as km
        self.km = km
        d = pathlib.Path(tempfile.mkdtemp())
        rec = lambda tenant, cause: {"classification": "disk_full", "tenant_id": tenant, "root_cause": cause, "validated_actions": [f"fix-{cause}"]}
        res = [rec("client_x", "mine"), rec("client_y", "THEIRS"), rec(None, "UNTAGGED"), rec("", "BLANK"), rec("   ", "SPACES")]
        (d / "res.json").write_text(json.dumps(res), encoding="utf-8")
        (d / "cand.json").write_text(json.dumps([rec(None, "UNTAGGED-CAND"), rec("client_y", "THEIRS-CAND")]), encoding="utf-8")
        from unittest.mock import patch
        self.patches = [patch.object(km, "RESOLUTIONS_FILE", d / "res.json"), patch.object(km, "KNOWLEDGE_CANDIDATES_FILE", d / "cand.json")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_strict_counts_only_exactly_tagged_records(self):
        out = self.km.get_pattern_intelligence("disk_full", tenant_id="client_x", strict_tenant=True)
        self.assertEqual(out["total_resolutions"], 1)
        self.assertEqual(out["candidate_count"], 0)
        self.assertEqual(out["dominant_root_causes"], ["mine"])
        text = json.dumps(out)
        for leaked in ("THEIRS", "UNTAGGED", "BLANK", "SPACES"):
            self.assertNotIn(leaked, text)

    def test_strict_with_no_matching_records_or_no_tenant_returns_nothing(self):
        self.assertIsNone(self.km.get_pattern_intelligence("disk_full", tenant_id="client_z", strict_tenant=True))
        self.assertIsNone(self.km.get_pattern_intelligence("disk_full", tenant_id="", strict_tenant=True))

    def test_the_tool_uses_strict_mode_end_to_end(self):
        reg = build_default_registry(executor())
        out = reg.call(make_ctx("client_x"), "pattern_lookup", {"classification": "disk_full"}, 5)
        self.assertEqual(out[0].facts["total_resolutions"], 1)
        self.assertNotIn("UNTAGGED", json.dumps(out[0].model_dump(mode="json")))

    def test_the_default_behaviour_for_existing_callers_is_unchanged(self):
        out = self.km.get_pattern_intelligence("disk_full", tenant_id="client_x")
        self.assertGreater(out["total_resolutions"], 1)           # still includes untagged records, as before


class EvidenceLookupTests(unittest.TestCase):
    def setUp(self):
        self.ex = executor()
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "change_window.json").write_text(json.dumps({"status": "GLOBAL-SECRET", "summary": "global file"}), encoding="utf-8")
        for tenant in ("client_x", "client_y"):
            d = self.root / "tenants" / tenant
            d.mkdir(parents=True)
            (d / "change_window.json").write_text(json.dumps(
                {"status": f"open-{tenant}", "summary": f"window for {tenant}", "source_system": "cab", "checked_at": "2026-10-10T08:00:00Z",
                 "severity": "low"}), encoding="utf-8")
        self.reg = build_default_registry(self.ex, root=lambda: self.root)

    def test_each_tenant_sees_only_its_own_partition(self):
        a = self.reg.call(make_ctx("client_x"), "evidence_lookup", {"check": "change_window"}, 5)
        b = self.reg.call(make_ctx("client_y"), "evidence_lookup", {"check": "change_window"}, 5)
        self.assertIn("open-client_x", a[0].excerpt)
        self.assertIn("open-client_y", b[0].excerpt)
        self.assertNotIn("client_y", a[0].excerpt)

    def test_the_global_untenanted_evidence_is_never_read(self):
        out = self.reg.call(make_ctx("client_x"), "evidence_lookup", {}, 5)
        self.assertNotIn("GLOBAL-SECRET", json.dumps([d.model_dump(mode="json") for d in out]))
        none = self.reg.call(make_ctx("brand_new"), "evidence_lookup", {"check": "change_window"}, 5)
        self.assertEqual(none[0].facts, {"status": "none"})
        self.assertIn("No tenant-scoped evidence", none[0].excerpt)

    def test_listing_is_bounded_and_check_is_an_enum(self):
        out = self.reg.call(make_ctx("client_x"), "evidence_lookup", {}, 5)
        self.assertEqual(len(out), len(evidence.CHECKS))
        for bad in ({"check": "../../etc/passwd"}, {"check": "nope"}, {"path": "x"}, {"tenant_id": "client_y"}):
            with self.assertRaises(ToolInputError):
                self.reg.call(make_ctx("client_x"), "evidence_lookup", bad, 5)

    def test_oversized_file_and_bad_json_become_absence_not_errors(self):
        d = self.root / "tenants" / "client_x"
        (d / "blast_radius.json").write_text("x" * (evidence.MAX_FILE_BYTES + 10), encoding="utf-8")
        (d / "rollback_readiness.json").write_text("{not json", encoding="utf-8")
        out = {x.source_id: x for x in self.reg.call(make_ctx("client_x"), "evidence_lookup", {}, 5)}
        self.assertEqual(out["blast_radius"].facts, {"status": "none"})
        self.assertEqual(out["rollback_readiness"].facts, {"status": "none"})

    def test_timeout_and_cancel(self):
        gate = threading.Event()
        slow = build_default_registry(self.ex, root=lambda: (gate.wait(10), self.root)[1])
        with self.assertRaises(ToolTimeout):
            slow.call(make_ctx("client_x"), "evidence_lookup", {}, 0.3)
        gate.set()
        with self.assertRaises(Cancelled):
            evidence.make_tool(lambda: self.root)(make_ctx("client_x"), cancel=cancelled_token())


class FakeCursor:
    def __init__(self, rows, delay=None):
        self.rows, self.delay, self.executed = rows, delay, []

    def execute(self, sql, params):
        self.executed.append((sql, params))
        if self.delay:
            self.delay.wait(10)

    def fetchmany(self, n):
        return self.rows[:n]


class FakeConnect:
    def __init__(self, cursor):
        self.cursor = cursor

    def __call__(self):
        outer = self

        class CM:
            def __enter__(self_inner):
                return (object(), outer.cursor)

            def __exit__(self_inner, *a):
                return False
        return CM()


class TicketHistoryTests(unittest.TestCase):
    def setUp(self):
        self.ex = executor()

    def reg(self, cursor):
        return build_default_registry(self.ex, connect=FakeConnect(cursor))

    def test_tenant_and_terms_are_parameters_never_sql_text(self):
        cur = FakeCursor([])
        self.reg(cur).call(make_ctx("client_y"), "ticket_history_lookup", {"host": "sapdb01", "text": "100%_done'; DROP TABLE x;--"}, 5)
        sql, params = cur.executed[0]
        self.assertEqual(params["tenant"], "client_y")
        self.assertNotIn("client_y", sql)
        self.assertNotIn("sapdb01", sql)
        self.assertNotIn("DROP", sql)
        self.assertIn("tenant_id = %(tenant)s", sql)
        self.assertEqual(params["text"], like_pattern("100%_done'; DROP TABLE x;--"))
        self.assertIn("100\\%\\_done", params["text"])
        self.assertEqual(params["limit"], ticket_history.MAX_ITEMS)

    def test_requires_a_filter_and_validates_inputs(self):
        reg = self.reg(FakeCursor([]))
        with self.assertRaises(ToolInputError):
            reg.call(make_ctx("client_x"), "ticket_history_lookup", {}, 5)
        for bad in ({"host": "bad host!"}, {"host": "a"}, {"host": "sapdb01", "days": 401}, {"host": "sapdb01", "days": 0},
                    {"host": "sapdb01", "tenant_id": "client_y"}, {"host": "sapdb01", "sql": "select 1"}):
            with self.assertRaises(ToolInputError):
                reg.call(make_ctx("client_x"), "ticket_history_lookup", bad, 5)

    def test_results_are_bounded_and_mapped(self):
        rows = [{"ticket_id": f"T{i}", "opened_at": "2026-05-24 12:00:00", "resolved_at": "2026-05-24 14:30:00", "assignment_group": "ops",
                 "priority": "P2", "service": "sap", "short_description": "disk full " * 40, "raw_json": {"Description": "d" * 600}}
                for i in range(40)]
        out = self.reg(FakeCursor(rows)).call(make_ctx("client_x"), "ticket_history_lookup", {"service": "sap"}, 5)
        self.assertLessEqual(len(out), ticket_history.MAX_ITEMS)
        self.assertEqual(out[0].facts["hours_to_resolve"], 2.5)
        self.assertLessEqual(len(out[0].excerpt), 500)
        self.assertEqual(out[0].provenance, "tenant_data")
        self.assertEqual(out[0].ttl_days, ticket_history.TTL_DAYS)

    def test_another_tenants_row_is_a_boundary_violation(self):
        rows = [{"ticket_id": "T1", "tenant_id": "client_y", "short_description": "x"}]
        with self.assertRaises(BoundaryViolation):
            self.reg(FakeCursor(rows)).call(make_ctx("client_x"), "ticket_history_lookup", {"host": "sapdb01"}, 5)

    def test_timeout(self):
        gate = threading.Event()
        with self.assertRaises(ToolTimeout):
            self.reg(FakeCursor([], delay=gate)).call(make_ctx("client_x"), "ticket_history_lookup", {"host": "sapdb01"}, 0.3)
        gate.set()

    def test_cancellation_stops_before_the_query(self):
        cur = FakeCursor([])
        tool = ticket_history.make_tool(FakeConnect(cur))
        with self.assertRaises(Cancelled):
            tool(make_ctx("client_x"), host="sapdb01", cancel=cancelled_token())
        self.assertEqual(cur.executed, [])

    def test_statement_timeout_applies_while_connecting(self):
        seen = []

        class SpyConnect(FakeConnect):
            def __call__(self):
                seen.append(db_limits.STATEMENT_TIMEOUT_MS.get())
                return super().__call__()

        ticket_history.make_tool(SpyConnect(FakeCursor([])))(make_ctx("client_x"), host="sapdb01", timeout_s=4.0)
        self.assertEqual(seen, [3750])


class StatementTimeoutPlumbingTests(unittest.TestCase):
    def test_timeout_is_bounded_by_the_call_time(self):
        self.assertIsNone(db_limits.timeout_ms_for(None))
        self.assertEqual(db_limits.timeout_ms_for(6.0), 5750)
        self.assertLess(db_limits.timeout_ms_for(6.0), 6000)

    def test_timeout_never_reaches_the_time_left_and_is_never_zero(self):
        for left in (0.002, 0.003, 0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 45.0):
            ms = db_limits.timeout_ms_for(left)
            self.assertGreaterEqual(ms, 1, left)                        # zero would mean "no limit" in PostgreSQL
            self.assertLess(ms, left * 1000, left)                      # strictly inside the remaining budget
        self.assertEqual(db_limits.timeout_ms_for(0.1), 75)

    def test_no_time_left_refuses_to_start_a_statement(self):
        for left in (0, 0.0, 0.001, -1.0):
            with self.assertRaises(db_limits.DeadlineExhausted):
                db_limits.timeout_ms_for(left)
        with self.assertRaises(db_limits.DeadlineExhausted):
            with db_limits.statement_timeout(0):
                pass
        self.assertIsNone(db_limits.STATEMENT_TIMEOUT_MS.get())

    def test_registry_passes_the_call_time_to_tools_that_accept_it(self):
        got = {}

        def tool(ctx, query, cancel=None, timeout_s=None):
            got.update(timeout_s=timeout_s, cancel=cancel)
            return []

        reg = ToolRegistry(executor=executor())
        reg.register(ToolSpec(name="t", description="d", input_schema={"type": "object", "additionalProperties": False,
                                                                       "properties": {"query": {"type": "string"}}}), tool)
        reg.call(make_ctx("client_x"), "t", {"query": "q"}, 3.5)
        self.assertEqual(got["timeout_s"], 3.5)
        self.assertIsInstance(got["cancel"], CancelToken)

    def test_timeout_and_cancel_are_not_registrable_model_arguments(self):
        for name in ("timeout_s", "cancel"):
            with self.assertRaises(RegistryError):
                ToolRegistry(executor=executor()).register(
                    ToolSpec(name="t", description="d", input_schema={"type": "object", "additionalProperties": False,
                                                                      "properties": {name: {"type": "integer"}}}), lambda ctx: [])

    def test_connection_options_carry_the_statement_timeout(self):
        try:
            import postgres_store
        except Exception:
            self.skipTest("psycopg2 is not installed")
        captured = {}
        from unittest.mock import patch
        with patch.object(postgres_store.psycopg2, "connect", lambda **kw: captured.update(kw) or object()):
            postgres_store.get_connection()
            self.assertNotIn("options", captured)
            captured.clear()
            with db_limits.statement_timeout(2.0):
                postgres_store.get_connection()
            self.assertEqual(captured["options"], "-c statement_timeout=1750")


class RegistryBuildTests(unittest.TestCase):
    def test_exactly_the_five_read_tools_with_strict_schemas(self):
        reg = build_default_registry(executor())
        self.assertEqual(sorted(reg.names()), sorted(["incident_kb_search", "ticket_history_lookup", "outcome_lookup", "pattern_lookup", "evidence_lookup"]))
        self.assertEqual(sorted(TOOL_NAMES), sorted(reg.names()))
        for name in reg.names():
            schema = reg.spec(name).input_schema
            self.assertIs(schema["additionalProperties"], False)
            self.assertNotIn("tenant_id", schema["properties"])

    def test_versions_are_part_of_the_manifest(self):
        self.assertEqual(set(build_default_registry(executor()).versions()), set(TOOL_NAMES))


class BoundaryScanTests(unittest.TestCase):
    """Structural: who may import retrieval, database and evidence code, and who may start threads."""

    ROOT = pathlib.Path(__file__).resolve().parent.parent / "agentruntime"
    BACKEND = re.compile(r"^\s*(?:from|import)\s+(retrieval_engine|pg_vector_store|postgres_store|resolution_outcome|knowledge_manager|"
                         r"psycopg2|sqlalchemy|requests|httpx|subprocess)\b", re.M)
    THREADS = re.compile(r"\b(threading\.Thread|ThreadPoolExecutor|multiprocessing|asyncio\.create_task|concurrent\.futures\.Thread)")

    def modules(self):
        return [p for p in self.ROOT.rglob("*.py")]

    def test_only_tool_modules_import_backends(self):
        tool_modules = {"kb_search.py", "ticket_history.py", "outcomes.py", "patterns.py"}
        for path in self.modules():
            hits = self.BACKEND.findall(path.read_text(encoding="utf-8"))
            if path.name == "store.py" and path.parent == self.ROOT:
                self.assertEqual(set(hits), {"postgres_store"})             # the run store: its own tables only
            elif hits:
                self.assertIn(path.name, tool_modules, f"{path} imports {hits}")
                self.assertEqual(path.parent.name, "tools")

    def test_only_the_executor_creates_threads(self):
        for path in self.modules():
            if path.name == "executor.py":
                continue
            self.assertIsNone(self.THREADS.search(path.read_text(encoding="utf-8")), f"{path} creates its own threads")

    def test_tools_take_the_tenant_from_the_context_only(self):
        for path in (self.ROOT / "tools").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"os\.getenv\(['\"](?:DEFAULT_TENANT|AIOPS_TENANT)", path.name)
            self.assertNotIn("default_tenant", text, path.name)
            self.assertNotIn("get_tenant", text, path.name)

    def test_every_tool_checks_for_cancellation(self):
        for mod in (kb_search, ticket_history, outcomes, patterns, evidence):
            text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
            self.assertIn("check_cancel(cancel)", text, mod.__name__)
            self.assertIn("timeout_s", text, mod.__name__)


@unittest.skipUnless(LIVE, "set AIOPS_TEST_AGENT_PG=1 with a reachable PostgreSQL to run")
class LiveTicketHistoryTests(unittest.TestCase):
    """The real SQL against PostgreSQL: two-tenant isolation, wildcard escaping, bounded results, the server-side statement timeout."""

    @classmethod
    def setUpClass(cls):
        import postgres_store
        cls.ps = postgres_store
        cls.tenant_a, cls.tenant_b = "ta_" + uuid.uuid4().hex[:8], "tb_" + uuid.uuid4().hex[:8]
        with postgres_store.db_cursor(commit=True) as (_c, cur):
            cur.execute("""CREATE TABLE IF NOT EXISTS client_ticket_records (
                record_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, ticket_id TEXT, priority TEXT, assignment_group TEXT,
                opened_at TEXT, resolved_at TEXT, short_description TEXT, service TEXT, raw_json JSONB)""")
            for tenant in (cls.tenant_a, cls.tenant_b):
                for i in range(12):
                    cur.execute(
                        "INSERT INTO client_ticket_records (record_id, tenant_id, ticket_id, priority, assignment_group, opened_at, resolved_at,"
                        " short_description, service, raw_json) VALUES (%s,%s,%s,'P3','ops', to_char(now() - make_interval(days => %s), "
                        "'YYYY-MM-DD HH24:MI:SS'), NULL, %s, 'sap', %s)",
                        (f"{tenant}-{i}", tenant, f"{tenant}-T{i}", i, f"sapdb01 disk full {tenant}", json.dumps({"Description": "d"})))
            cur.execute("INSERT INTO client_ticket_records (record_id, tenant_id, ticket_id, opened_at, short_description) "
                        "VALUES (%s,%s,'PCT', to_char(now(), 'YYYY-MM-DD HH24:MI:SS'), %s)", (cls.tenant_a + "-pct", cls.tenant_a, "100% literal"))

    @classmethod
    def tearDownClass(cls):
        with cls.ps.db_cursor(commit=True) as (_c, cur):
            cur.execute("DELETE FROM client_ticket_records WHERE tenant_id = ANY(%s)", ([cls.tenant_a, cls.tenant_b],))

    def reg(self):
        return build_default_registry(executor())

    def test_two_tenants_see_only_their_own_tickets_and_results_are_bounded(self):
        for mine, other in ((self.tenant_a, self.tenant_b), (self.tenant_b, self.tenant_a)):
            out = self.reg().call(make_ctx(mine), "ticket_history_lookup", {"host": "sapdb01", "days": 400}, 10)
            self.assertEqual(len(out), ticket_history.MAX_ITEMS)
            self.assertTrue(all(d.tenant_id == mine and d.source_id.startswith(mine) for d in out))
            self.assertNotIn(other, json.dumps([d.model_dump(mode="json") for d in out]))

    def test_a_percent_sign_is_matched_literally(self):
        out = self.reg().call(make_ctx(self.tenant_a), "ticket_history_lookup", {"text": "100% literal"}, 10)
        self.assertEqual([d.source_id for d in out], ["PCT"])
        none = self.reg().call(make_ctx(self.tenant_a), "ticket_history_lookup", {"text": "%%%"}, 10)
        self.assertEqual(none, [])

    def test_time_window_filters_old_tickets(self):
        out = self.reg().call(make_ctx(self.tenant_a), "ticket_history_lookup", {"host": "sapdb01", "days": 3}, 10)
        self.assertLessEqual(len(out), 4)

    def test_the_server_cancels_a_statement_that_outlives_the_call_time(self):
        with db_limits.statement_timeout(0.6):
            with self.ps.db_cursor() as (_c, cur):
                started = time.monotonic()
                with self.assertRaises(Exception) as caught:
                    cur.execute("SELECT pg_sleep(5)")
                self.assertLess(time.monotonic() - started, 2.5)
                self.assertIn("statement timeout", str(caught.exception).lower())
        with self.ps.db_cursor() as (_c, cur):                      # a connection opened outside the block has no such limit
            cur.execute("SHOW statement_timeout")
            self.assertEqual(list(cur.fetchone())[0], "0")


if __name__ == "__main__":
    unittest.main()
