"""
Queue entries belong to the tenant that raised the incident, not to a fixed default tenant.

Run from the project root:  python -m unittest tests.test_queue_tenant -v
"""
import unittest
from unittest.mock import patch

import agents.automation as aa
import automation_engine as ae
from tests.test_execution_authorization import AuthzBase, bearer


class QueueTenantTests(AuthzBase):
    def post(self, token="tok-l2", **extra):
        return self.client.post("/automation/queue", headers=bearer(token),
                                json={"incident_id": "inc-1", "classification": "cpu_saturation", **extra})

    def test_entry_is_saved_under_the_requested_tenant(self):
        r = self.post(tenant_id="client_c")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["queue_entry"]["tenant_id"], "client_c")
        self.assertEqual(ae.get_queue_entry(r.json()["queue_entry"]["queue_id"])["tenant_id"], "client_c")

    def test_omitted_tenant_falls_back_to_the_engine_default(self):
        entry = self.post().json()["queue_entry"]
        self.assertEqual(entry["tenant_id"], ae.DEFAULT_TENANT_ID)

    def test_unknown_or_malformed_tenant_is_rejected(self):
        for bad in ("no_such_tenant", "../client_a", "client a"):
            with self.subTest(bad):
                self.assertEqual(self.post(tenant_id=bad).status_code, 400)

    def test_principal_limited_to_other_tenants_cannot_queue_for_this_one(self):
        r = self.post("tok-tenant", tenant_id="client_c")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(ae.list_automation_queue(limit=10).get("items", ae.list_automation_queue(limit=10).get("queue", [])), [])

    def test_autonomy_intent_is_judged_for_the_requested_tenant(self):
        seen = []
        original = aa.evaluate_autonomy

        def spy(classification, tenant_id, purpose, *a, **k):
            seen.append((tenant_id, purpose))
            return original(classification, tenant_id, purpose, *a, **k)

        with patch.object(aa, "evaluate_autonomy", spy):
            self.post("tok-svc", tenant_id="client_c")
        self.assertIn(("client_c", "intent"), seen)

    def test_later_steps_use_the_entry_tenant(self):
        qid = self.post(tenant_id="client_c").json()["queue_entry"]["queue_id"]
        request = aa.AutomationPreExecutionCheckRequest(queue_id=qid, classification="cpu_saturation", playbook="x.yml",
                                                        target_host="h", execution_mode="live")
        self.assertEqual(aa._gate_tenant_id(request), "client_c")


if __name__ == "__main__":
    unittest.main()
