"""
`GET /agentic/runs/by-request/{request_id}` (design: operator-visible shadow inspection, section 22.6).

Authentication and tenant authorization go through the real security middleware (so this exercises the actual route, not a stand-in);
the store lookup is faked so the test needs no database. A separate, lighter FastAPI app carries only this router (the same pattern as
tests/test_r2_2_integration.py's OffParityThroughTheRouteTests): booting the whole application is unnecessary for a read-only endpoint.
"""
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agents.agentic_runs as agentic_runs
import security
from agentruntime.contracts import AgentRunResult, AgentStep, EvidenceItem, Hypothesis
from agentruntime.integration import gate

PRINCIPALS = {
    "tok-client-c": {"id": "operator-1", "role": "l2", "kind": "human", "tenants": ["client_c"]},
    "tok-client-x": {"id": "operator-2", "role": "l2", "kind": "human", "tenants": ["client_x"]},
    "tok-any-tenant": {"id": "svc-1", "role": "l2", "kind": "service"},          # empty tenants list = unrestricted, per security.Principal
}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make_run(tenant="client_c", mode="shadow", status="supported", marker="SECRET-EXCERPT-MARKER-771"):
    return AgentRunResult(
        run_id="run-1", tenant_id=tenant, status=status, reason="a verified hypothesis is supported by evidence",
        hypotheses=[Hypothesis(cause="disk full on the data volume", status="supported", evidence=["e1"], verified=True)],
        evidence=[EvidenceItem(evidence_id="e1", run_id="run-1", tenant_id=tenant, tool="incident_kb_search", tool_version="1",
                               source_type="incident", source_id="inc-1", provenance="tenant_data", retrieved_at=datetime.now(timezone.utc),
                               sanitization="clean", relevance=0.8, excerpt=marker, content_hash="h")],
        steps=[AgentStep(n=1, kind="tool", tool="ticket_history_lookup", result_summary="0 evidence item(s)", evidence_added=[], duration_ms=30),
               AgentStep(n=2, kind="tool", tool="incident_kb_search", result_summary="1 evidence item(s)", evidence_added=["e1"], duration_ms=90)],
        counters={"steps": 2, "tool_calls": 2, "model_calls": 0, "elapsed_s": 0.121},
        versions={"ruleset": "rules-1", "model": None, "tools": {"incident_kb_search": "1"}, "policy_hash": "should-never-appear-in-the-api-response"},
        mode=mode, flags=[],
    )


class FakeStore:
    def __init__(self, run=None, raise_on_lookup=None):
        self.run, self.raise_on_lookup = run, raise_on_lookup
        self.calls = []

    def load_run_by_request(self, tenant_id, request_id):
        self.calls.append((tenant_id, request_id))
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        return self.run


class ApiBase(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {"AIOPS_PRINCIPALS": json.dumps(PRINCIPALS), "AIOPS_API_KEY": ""})
        self._env.start()
        security.reset_principals_cache()
        self.web = FastAPI()
        security.install_security(self.web)
        self.web.include_router(agentic_runs.router)
        self.client = TestClient(self.web)
        self._state_patch = patch.dict(agentic_runs._state, {"store": None})
        self._state_patch.start()

    def tearDown(self):
        self._state_patch.stop()
        self._env.stop()
        security.reset_principals_cache()

    def get(self, request_id="req-123", tenant_id="client_c", token="tok-client-c", **kw):
        headers = bearer(token) if token else {}
        return self.client.get(f"/agentic/runs/by-request/{request_id}", params={"tenant_id": tenant_id}, headers=headers, **kw)

    def with_store(self, store):
        return patch.object(agentic_runs, "_get_store", return_value=store)


class AuthenticationAndAuthorizationTests(ApiBase):
    def test_an_authorized_tenant_receives_its_run(self):
        with self.with_store(FakeStore(run=make_run())):
            r = self.get(token="tok-client-c", tenant_id="client_c")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "complete")

    def test_an_unauthorized_tenant_receives_403(self):
        with self.with_store(FakeStore(run=make_run(tenant="client_c"))):
            r = self.get(token="tok-client-x", tenant_id="client_c")       # tok-client-x may only act for client_x
        self.assertEqual(r.status_code, 403)

    def test_a_missing_bearer_token_is_rejected_before_the_route_runs(self):
        with self.with_store(FakeStore(run=make_run())):
            r = self.get(token=None)
        self.assertEqual(r.status_code, 401)

    def test_an_unrestricted_service_principal_may_read_any_tenant_it_is_not_explicitly_denied(self):
        with self.with_store(FakeStore(run=make_run(tenant="client_a"))):
            r = self.get(token="tok-any-tenant", tenant_id="client_a")
        self.assertEqual(r.status_code, 200)

    def test_no_elevated_role_is_required_an_l2_principal_authorized_for_the_tenant_is_enough(self):
        # PRINCIPALS above are all role "l2"; the request still succeeds, confirming no additional role restriction was added.
        with self.with_store(FakeStore(run=make_run())):
            r = self.get(token="tok-client-c")
        self.assertEqual(r.status_code, 200)


class MalformedIdentifierTests(ApiBase):
    def test_a_malformed_tenant_id_is_rejected(self):
        for bad in ("Client-C", "client c", "../etc", "", "x" * 65):
            with self.with_store(FakeStore(run=make_run())):
                r = self.get(tenant_id=bad)
            self.assertEqual(r.status_code, 400, bad)

    def test_a_malformed_request_id_is_rejected(self):
        for bad in ("a b c", "x" * 65, "<script>"):
            with self.with_store(FakeStore(run=make_run())):
                r = self.client.get(f"/agentic/runs/by-request/{bad}", params={"tenant_id": "client_c"}, headers=bearer("tok-client-c"))
            self.assertEqual(r.status_code, 400, bad)

    def test_a_path_traversal_attempt_never_reaches_a_stored_run(self):
        # Starlette's own route matching refuses a literal '/' inside a single path segment before our handler ever runs (404);
        # either way nothing is looked up or returned.
        with self.with_store(FakeStore(run=make_run())) as _s:
            r = self.client.get("/agentic/runs/by-request/../../etc/passwd", params={"tenant_id": "client_c"}, headers=bearer("tok-client-c"))
        self.assertIn(r.status_code, (400, 404))

    def test_a_missing_tenant_id_query_parameter_is_rejected(self):
        r = self.client.get("/agentic/runs/by-request/req-1", headers=bearer("tok-client-c"))
        self.assertIn(r.status_code, (400, 422))


class StateTests(ApiBase):
    def test_pending_when_no_run_is_stored_but_the_tenant_is_actively_shadow_or_on(self):
        with self.with_store(FakeStore(run=None)), patch.object(gate, "effective_mode", return_value=("shadow", "recorded decision")):
            r = self.get()
        self.assertEqual(r.json(), {"state": "pending", "mode": "shadow"})

    def test_disabled_when_no_run_is_stored_and_the_tenant_is_off(self):
        with self.with_store(FakeStore(run=None)), patch.object(gate, "effective_mode", return_value=("off", "no recorded decision")):
            r = self.get()
        self.assertEqual(r.json(), {"state": "disabled"})

    def test_a_completed_run_is_visible_even_though_the_tenant_is_now_off(self):
        """Stopping shadow mode must not make an already-completed investigation disappear (reviewer correction #1)."""
        with self.with_store(FakeStore(run=make_run())), patch.object(gate, "effective_mode", return_value=("off", "the test decision ended")):
            r = self.get()
        self.assertEqual(r.json()["state"], "complete")

    def test_an_escalated_run_reports_the_escalation_not_a_fabricated_cause(self):
        run = make_run(status="escalated_insufficient")
        run = run.model_copy(update={"hypotheses": [], "reason": "no new evidence"})
        with self.with_store(FakeStore(run=run)):
            r = self.get()
        body = r.json()
        self.assertEqual((body["status"], body["hypotheses"], body["reason"]), ("escalated_insufficient", [], "no new evidence"))

    def test_a_lookup_failure_is_reported_as_unavailable_never_a_500(self):
        with self.with_store(FakeStore(raise_on_lookup=RuntimeError("database is down"))):
            r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"state": "unavailable"})

    def test_a_store_construction_failure_is_also_unavailable_not_a_500(self):
        with patch.object(agentic_runs, "_get_store", side_effect=RuntimeError("no encryption key")):
            r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"state": "unavailable"})
        # the failure is caught entirely inside this route; nothing here can reach or disturb the /analyze pipeline, which never calls it


class ResponseAppliedAndImpactTests(ApiBase):
    def test_a_shadow_run_never_claims_to_have_applied(self):
        with self.with_store(FakeStore(run=make_run(mode="shadow"))):
            body = self.get().json()
        self.assertEqual((body["response_applied"], body["response_impact"]), (False, "none_shadow"))

    def test_an_on_run_is_reported_as_not_recorded_never_inferred_from_the_current_mode(self):
        """Reviewer correction #2: response_applied must come from a recorded reconciliation outcome, never be inferred from mode=='on'."""
        with self.with_store(FakeStore(run=make_run(mode="on"))):
            body = self.get().json()
        self.assertEqual((body["response_applied"], body["response_impact"]), (False, "not_recorded"))


class SanitizationTests(ApiBase):
    def test_the_response_contains_no_ticket_text_encryption_metadata_or_ciphertext_markers(self):
        with self.with_store(FakeStore(run=make_run())):
            body = self.get().json()
        blob = json.dumps(body)
        self.assertNotIn("SECRET-EXCERPT-MARKER", blob)                 # the raw evidence excerpt, never returned
        self.assertNotIn("should-never-appear", blob)                   # policy_hash, dropped by summarize_run
        for forbidden in ("encryption", "cipher", "key_id", "policy_hash", "tool_input", "ticket_text", "traceback"):
            self.assertNotIn(forbidden, blob.lower())

    def test_stored_events_become_a_bounded_summary_tool_names_and_counts_only(self):
        with self.with_store(FakeStore(run=make_run())):
            body = self.get().json()
        self.assertEqual(body["tool_count"], 2)
        self.assertEqual([t["name"] for t in body["tools"]], ["ticket_history_lookup", "incident_kb_search"])
        for t in body["tools"]:
            self.assertEqual(set(t), {"step", "name", "ok", "evidence_added", "duration_ms"})
        self.assertEqual(body["evidence_count"], 1)
        self.assertNotIn("excerpt", json.dumps(body))

    def test_the_store_is_queried_with_exactly_the_requested_identifiers(self):
        store = FakeStore(run=make_run())
        with self.with_store(store):
            self.get(request_id="req-xyz", tenant_id="client_c")
        self.assertEqual(store.calls, [("client_c", "req-xyz")])


if __name__ == "__main__":
    unittest.main()
