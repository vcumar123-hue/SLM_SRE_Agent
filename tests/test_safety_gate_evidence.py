"""
Wave 2: Safety Gate evidence semantics and provenance.

Evidence that is missing, malformed, unattested, synthetic or estimated is never reported as passed:
  simulation  may proceed (reported `unknown`, a gate warning, never blocked by the gate itself)
  live        needs a recorded human override (`warn`)
  autonomous  is blocked (`fail`)
Gate-generated evidence is stored under <evidence_dir>/_generated and never overwrites client evidence.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_safety_gate_evidence -v
"""
import copy
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import adapters.universal_api_adapter as adapter
import agents.automation as aa
import agents.evidence as evidence_router
import app as app_module
import automation_engine as ae
import safety_evidence as ev
from models import AutomationPreExecutionCheckRequest
from tests.test_execution_authorization import AuthzBase, bearer

REAL_GATE = aa._build_pre_execution_safety_gate  # captured before AuthzBase replaces it with a stub
ROOT = pathlib.Path(__file__).resolve().parent.parent

MODES = ("simulation", "live", "autonomous")
DEGRADED = {"simulation": "unknown", "live": "warn", "autonomous": "fail"}
STALE = {"simulation": "warn", "live": "warn", "autonomous": "fail"}
CHECKS = ("infrastructure_drift", "error_budget_slo", "concurrent_activity", "change_window",
          "rollback_readiness", "blast_radius")


def now_iso(hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# -- pure semantics ---------------------------------------------------------------------------------------

class StatusSemanticsTests(unittest.TestCase):
    def test_unrecognised_or_absent_status_is_unknown_never_pass(self):
        for value in (None, "", "banana", "   ", 0, "n/a"):
            self.assertEqual(ev.normalize_status(value), "unknown", value)
        for value, expected in (("OK", "pass"), ("healthy", "pass"), ("Warning", "warn"), ("failed", "fail"),
                                ("missing", "unknown")):
            self.assertEqual(ev.normalize_status(value), expected)

    def test_mode_classes(self):
        self.assertEqual(ev.gate_mode_class("simulation"), "simulation")
        self.assertEqual(ev.gate_mode_class("live"), "live")
        self.assertEqual(ev.gate_mode_class("live_human_approved"), "live")
        self.assertEqual(ev.gate_mode_class("autonomous"), "autonomous")
        self.assertEqual(ev.gate_mode_class(None), "simulation")
        self.assertEqual(ev.gate_mode_class("something-else"), "simulation")

    def test_verified_clean_evidence_passes_in_every_mode(self):
        policy = ev.resolve_policy({})
        for mode in MODES:
            status, reasons = ev.effective_status("pass", "verified", "", False, mode, policy)
            self.assertEqual((status, reasons), ("pass", []), mode)

    def test_degraded_evidence_never_passes_and_follows_the_mode_policy(self):
        policy = ev.resolve_policy({})
        for provenance in ("synthetic", "estimated", "unknown"):
            for mode in MODES:
                status, _ = ev.effective_status("pass", provenance, "", False, mode, policy)
                self.assertEqual(status, DEGRADED[mode], (provenance, mode))
        for issue in ("missing", "unreadable", "unrecognized_status", "empty"):
            for mode in MODES:
                status, reasons = ev.effective_status("pass", "verified", issue, False, mode, policy)
                self.assertEqual(status, DEGRADED[mode], (issue, mode))
                self.assertIn(issue, reasons)

    def test_a_reported_problem_is_never_softened(self):
        policy = ev.resolve_policy({})
        for provenance in ev.PROVENANCE_VALUES:
            for mode in MODES:
                self.assertEqual(ev.effective_status("fail", provenance, "", False, mode, policy)[0], "fail")
                self.assertEqual(ev.effective_status("warn", provenance, "", False, mode, policy)[0], "warn")

    def test_stale_passing_evidence(self):
        policy = ev.resolve_policy({})
        for mode in MODES:
            status, reasons = ev.effective_status("pass", "verified", "", True, mode, policy)
            self.assertEqual(status, STALE[mode])
            self.assertIn("stale", reasons)

    def test_policy_overrides_are_validated(self):
        policy = ev.resolve_policy({"evidence_policy": {
            "degraded_status": {"live": "fail", "simulation": "pass", "autonomous": "bogus"},
            "stale_status": "not-a-dict",
            "treat_uploaded_files_as_verified": "yes"}})
        self.assertEqual(policy["degraded_status"], {"simulation": "unknown", "live": "fail", "autonomous": "fail"})
        self.assertEqual(policy["stale_status"], STALE)
        self.assertFalse(policy["treat_uploaded_files_as_verified"])  # only a real boolean is accepted

    def test_degraded_status_can_never_be_configured_to_pass(self):
        policy = ev.resolve_policy({"evidence_policy": {"degraded_status": {m: "pass" for m in MODES}}})
        self.assertEqual(policy["degraded_status"], DEGRADED)


class ProvenanceRulesTests(unittest.TestCase):
    POLICY = ev.resolve_policy({})

    def test_api_pull_and_authenticated_push_are_verified(self):
        self.assertEqual(ev.client_provenance({}, "api", self.POLICY), "verified")
        self.assertEqual(ev.client_provenance({"_submitted_via": "api_pull"}, "file", self.POLICY), "verified")
        self.assertEqual(ev.client_provenance({"_submitted_via": "api_push", "submitted_by": "svc"}, "file", self.POLICY),
                         "verified")

    def test_push_without_an_authenticated_submitter_is_not_verified(self):
        self.assertEqual(ev.client_provenance({"_submitted_via": "api_push"}, "file", self.POLICY), "unknown")
        self.assertEqual(ev.client_provenance({"_submitted_via": "api_push", "submitted_by": "  "}, "file", self.POLICY),
                         "unknown")

    def test_uploaded_and_manual_files_are_unattested(self):
        for doc in ({"source_system": "ui_json_upload"}, {"source_system": "manual"}, {"source_system": "client_file_adapter"}):
            self.assertEqual(ev.client_provenance(doc, "file", self.POLICY), "unknown")

    def test_self_asserted_provenance_in_a_client_file_is_ignored(self):
        self.assertEqual(ev.client_provenance({"provenance": "verified", "status": "pass"}, "file", self.POLICY), "unknown")

    def test_uploaded_files_can_be_trusted_only_by_explicit_policy(self):
        policy = ev.resolve_policy({"evidence_policy": {"treat_uploaded_files_as_verified": True}})
        self.assertEqual(ev.client_provenance({"source_system": "manual"}, "file", policy), "verified")

    def test_legacy_gate_files_in_the_client_folder_are_labelled_by_their_source(self):
        for source, expected in (("snapshot_engine_dry", "synthetic"), ("snapshot_engine_lab", "verified"),
                                 ("blast_radius_estimator_live", "estimated"), ("dry_run_simulation", "synthetic")):
            self.assertEqual(ev.client_provenance({"source_system": source}, "file", self.POLICY), expected)

    def test_generated_provenance_accepts_only_known_values(self):
        self.assertEqual(ev.generated_provenance({"provenance": "Synthetic"}), "synthetic")
        self.assertEqual(ev.generated_provenance({"provenance": "gold-plated"}), "unknown")
        self.assertEqual(ev.generated_provenance({}), "unknown")


class CandidateSelectionTests(unittest.TestCase):
    def cand(self, raw, provenance, status=None):
        return {"raw_status": raw, "provenance": provenance, "status": status or raw}

    def test_a_reported_problem_beats_a_more_trusted_pass(self):
        chosen = ev.choose_candidate([self.cand("pass", "verified"), self.cand("warn", "synthetic")])
        self.assertEqual(chosen["raw_status"], "warn")

    def test_fail_beats_warn(self):
        chosen = ev.choose_candidate([self.cand("warn", "verified"), self.cand("fail", "synthetic")])
        self.assertEqual(chosen["raw_status"], "fail")

    def test_degraded_generated_evidence_cannot_veto_verified_client_evidence(self):
        chosen = ev.choose_candidate([self.cand("pass", "synthetic", "unknown"), self.cand("pass", "verified", "pass")])
        self.assertEqual(chosen["provenance"], "verified")

    def test_unusable_client_evidence_is_not_hidden_by_synthetic_evidence(self):
        unusable = {"raw_status": "unknown", "provenance": "unknown", "status": "warn"}
        chosen = ev.choose_candidate([self.cand("pass", "synthetic", "unknown"), unusable])
        self.assertIs(chosen, unusable)


class WarningReasonTests(unittest.TestCase):
    def test_reason_is_stable_and_mode_independent(self):
        sim = {"status": "unknown", "raw_status": "pass", "provenance": "synthetic", "evidence_issue": ""}
        live = {"status": "warn", "raw_status": "pass", "provenance": "synthetic", "evidence_issue": ""}
        self.assertEqual(ev.warning_reason(sim), ev.warning_reason(live))
        self.assertEqual(ev.warning_reason(sim), "synthetic")
        self.assertEqual(ev.warning_reason({"evidence_issue": "missing", "provenance": "unknown"}), "missing")
        self.assertEqual(ev.warning_reason({"policy_reasons": ["stale"], "provenance": "verified"}), "stale")
        self.assertEqual(ev.warning_reason({"status": "warn", "raw_status": "warn", "provenance": "verified"}), "warn")


# -- the real gate against a temporary evidence folder ----------------------------------------------------

class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.evidence = self.tmp / "evidence"
        self.evidence.mkdir()
        self.cfg = copy.deepcopy(aa.DEFAULT_SAFETY_GATE_CONFIG)
        self.cfg["evidence_store_dir"] = str(self.evidence)
        self.engine_calls = []
        self.fail_drift_capture = False
        self.fail_simulation = False
        self.anomalies = []
        self.sim_status = "simulated"
        outer = self

        class FakeEngine:
            def capture(self_, ci_name, mode, notes=""):
                outer.engine_calls.append((mode, notes))
                if outer.fail_drift_capture and notes.startswith("safety_gate pre-check"):
                    raise RuntimeError("snapshot backend down")
                if outer.fail_simulation and notes.startswith("safety_gate simulation"):
                    raise RuntimeError("simulation backend down")
                return {"snapshot_id": f"snap-{len(outer.engine_calls)}", "state": {"anomalies": list(outer.anomalies)}}

            def apply_remediation(self_, snapshot_id, playbook_name, changes):
                return {"remediation": {"result": {"status": outer.sim_status}}}

        fake = types.ModuleType("tools.snapshot_engine")
        fake.SnapshotEngine = FakeEngine
        self.tenant_settings = {}
        self._patches = [
            patch.object(aa, "_load_safety_gate_config", lambda: copy.deepcopy(self.cfg)),
            patch.object(aa, "_tenant_evidence_settings", lambda tenant: dict(self.tenant_settings)),
            patch.object(aa, "ROLLBACK_BACKUP_DIR", self.tmp / "bak"),
            patch.dict(sys.modules, {"tools.snapshot_engine": fake}),
            patch.dict(os.environ, {"CMDB_ENABLED": "", "LAB_SNAPSHOT_ENABLED": ""}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    # helpers
    def gate(self, mode="live", **kw):
        return REAL_GATE(AutomationPreExecutionCheckRequest(
            queue_id="q1", incident_id="i1", classification="cpu_saturation", playbook="x.yml",
            target_host="linux-lab-01", risk_tier="medium", execution_mode=mode, **kw))

    def write_client(self, filename, doc, bom=False, raw=None):
        path = self.evidence / filename
        text = raw if raw is not None else json.dumps(doc)
        path.write_text(("﻿" if bom else "") + text, encoding="utf-8")
        return path

    def verified_doc(self, evidence_type, status="pass", **extra):
        return {"evidence_type": evidence_type, "status": status, "severity": "low", "source_system": "ServiceNow",
                "summary": f"{evidence_type} ok", "resource": "linux-lab-01", "checked_at": now_iso(),
                "_submitted_via": "api_push", "submitted_by": "evidence-svc", **extra}

    def upload_doc(self, evidence_type, status="pass", **extra):
        return {"evidence_type": evidence_type, "status": status, "severity": "low", "source_system": "ui_json_upload",
                "summary": f"{evidence_type} ok", "resource": "linux-lab-01", "checked_at": now_iso(), **extra}

    @staticmethod
    def fingerprint(directory):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob("*.json"))}


class NothingPassesWithoutVerifiedEvidenceTests(GateBase):
    def test_no_evidence_anywhere_never_passes_in_any_mode(self):
        for mode in MODES:
            r = self.gate(mode)
            for name in CHECKS:
                self.assertEqual(r["checks"][name]["status"], DEGRADED[mode], (mode, name))
                self.assertNotEqual(r["checks"][name]["provenance"], "verified", (mode, name))
            self.assertEqual(r["gate_status"], "blocked" if mode == "autonomous" else "warning", mode)
            self.assertFalse(r["safe_to_execute_live"], mode)

    def test_provenance_of_each_check_without_client_evidence(self):
        r = self.gate("live")
        prov = {n: d["provenance"] for n, d in r["checks"].items()}
        self.assertEqual(prov, {
            "infrastructure_drift": "synthetic", "error_budget_slo": "synthetic", "concurrent_activity": "unknown",
            "change_window": "unknown", "rollback_readiness": "estimated", "blast_radius": "estimated"})
        self.assertEqual(r["evidence_provenance"]["verified"], 0)
        self.assertEqual(sorted(r["unverified_checks"]), sorted(CHECKS))

    def test_simulation_proceeds_the_gate_reports_a_warning_and_does_not_block(self):
        r = self.gate("simulation")
        self.assertEqual(r["gate_status"], "warning")
        self.assertTrue(r["safe_to_approve"])
        self.assertFalse(r["safe_to_execute_live"])
        self.assertEqual(r["blocking_checks"], [])
        self.assertEqual(sorted(r["warning_checks"]), sorted(CHECKS))

    def test_missing_evidence_no_longer_passes_by_default(self):
        # The shipped config used to say: missing_evidence_status = "pass"
        self.cfg["pilot_defaults"] = {"missing_evidence_status": "pass"}  # an old config file still carrying it
        r = self.gate("live")
        self.assertEqual(r["checks"]["concurrent_activity"]["status"], "warn")
        self.assertEqual(r["checks"]["concurrent_activity"]["evidence_issue"], "missing")

    def test_corrupt_client_evidence_is_unreadable_not_pass(self):
        self.write_client("concurrent_activity.json", None, raw="{ this is not json")
        for mode in MODES:
            d = self.gate(mode)["checks"]["concurrent_activity"]
            self.assertEqual((d["status"], d["evidence_issue"]), (DEGRADED[mode], "unreadable"), mode)

    def test_unrecognised_and_absent_status_are_unknown(self):
        self.write_client("change_conflict.json", self.upload_doc("change_conflict", status="banana"))
        self.write_client("concurrent_activity.json", {k: v for k, v in self.upload_doc("concurrent_activity").items()
                                                       if k != "status"})
        r = self.gate("live")
        self.assertEqual(r["checks"]["change_window"]["evidence_issue"], "unrecognized_status")
        self.assertEqual(r["checks"]["concurrent_activity"]["evidence_issue"], "status_missing")
        for name in ("change_window", "concurrent_activity"):
            self.assertEqual(r["checks"][name]["raw_status"], "unknown")
            self.assertEqual(r["checks"][name]["status"], "warn")

    def test_byte_order_mark_files_are_read_not_treated_as_missing(self):
        self.write_client("concurrent_activity.json", self.verified_doc("concurrent_activity"), bom=True)
        d = self.gate("autonomous")["checks"]["concurrent_activity"]
        self.assertEqual((d["status"], d["provenance"]), ("pass", "verified"))

    def test_empty_and_unmatched_evidence_are_flagged(self):
        self.write_client("concurrent_activity.json", {"items": []})
        self.write_client("change_conflict.json", {"items": [self.upload_doc("change_conflict", resource="another-host")]})
        r = self.gate("live")
        self.assertEqual(r["checks"]["concurrent_activity"]["evidence_issue"], "empty")
        self.assertEqual(r["checks"]["change_window"]["evidence_issue"], "no_match")


class VerifiedEvidenceTests(GateBase):
    def test_verified_fresh_client_evidence_passes_in_every_mode(self):
        self.write_client("concurrent_activity.json", self.verified_doc("concurrent_activity"))
        for mode in MODES:
            d = self.gate(mode)["checks"]["concurrent_activity"]
            self.assertEqual((d["status"], d["provenance"], d["evidence_source"]), ("pass", "verified", "client_file"), mode)

    def test_verified_evidence_beats_degraded_generated_evidence(self):
        self.write_client("drift_status.json", self.verified_doc("drift_status"))
        for mode in MODES:
            d = self.gate(mode)["checks"]["infrastructure_drift"]
            self.assertEqual((d["status"], d["provenance"]), ("pass", "verified"), mode)
            sources = {s["source"]: s["provenance"] for s in d["evidence_sources_considered"]}
            self.assertEqual(sources, {"client_file": "verified", "generated": "synthetic"})

    def test_stale_verified_evidence_warns_or_blocks(self):
        self.write_client("concurrent_activity.json", self.verified_doc("concurrent_activity", checked_at=now_iso(200)))
        for mode in MODES:
            d = self.gate(mode)["checks"]["concurrent_activity"]
            self.assertEqual(d["status"], STALE[mode], mode)
            self.assertIn("stale", d["policy_reasons"])

    def test_unattested_upload_is_unknown_until_policy_trusts_files(self):
        self.write_client("concurrent_activity.json", self.upload_doc("concurrent_activity"))
        for mode in MODES:
            d = self.gate(mode)["checks"]["concurrent_activity"]
            self.assertEqual((d["status"], d["provenance"]), (DEGRADED[mode], "unknown"), mode)
        self.cfg["evidence_policy"] = {"treat_uploaded_files_as_verified": True}
        for mode in MODES:
            d = self.gate(mode)["checks"]["concurrent_activity"]
            self.assertEqual((d["status"], d["provenance"]), ("pass", "verified"), mode)

    def test_self_asserted_provenance_is_ignored(self):
        self.write_client("concurrent_activity.json", self.upload_doc("concurrent_activity", provenance="verified"))
        d = self.gate("live")["checks"]["concurrent_activity"]
        self.assertEqual((d["status"], d["provenance"]), ("warn", "unknown"))

    def test_push_without_a_submitter_is_not_trusted(self):
        doc = self.verified_doc("concurrent_activity")
        doc.pop("submitted_by")
        self.write_client("concurrent_activity.json", doc)
        self.assertEqual(self.gate("live")["checks"]["concurrent_activity"]["provenance"], "unknown")

    def test_legacy_gate_written_client_files_are_labelled_synthetic(self):
        self.write_client("drift_status.json", {"status": "pass", "severity": "low", "summary": "x",
                                                "source_system": "snapshot_engine_dry", "checked_at": now_iso()})
        d = self.gate("live")["checks"]["infrastructure_drift"]
        self.assertEqual((d["status"], d["provenance"]), ("warn", "synthetic"))

    def test_a_reported_problem_in_client_evidence_is_never_masked(self):
        self.write_client("drift_status.json", self.verified_doc("drift_status", status="fail"))
        for mode in MODES:
            d = self.gate(mode)["checks"]["infrastructure_drift"]
            self.assertEqual(d["status"], "fail", mode)
        self.assertIn("infrastructure_drift", self.gate("simulation")["blocking_checks"])

    def test_disabled_check_is_not_applicable_in_simulation_and_live(self):
        self.cfg["checks"]["change_window"]["enabled"] = False
        for mode in ("simulation", "live"):
            r = self.gate(mode)
            d = r["checks"]["change_window"]
            self.assertEqual((d["status"], d["provenance"], d.get("disabled")), ("pass", "not_applicable", True), mode)
            self.assertNotIn("change_window", r["unverified_checks"])

    def test_autonomous_execution_refuses_any_disabled_check(self):
        self.cfg["checks"]["change_window"]["enabled"] = False
        r = self.gate("autonomous")
        d = r["checks"]["change_window"]
        self.assertEqual((d["status"], d["evidence_issue"], d.get("disabled")), ("fail", "check_disabled", True))
        self.assertIn("autonomous_requires_all_checks_enabled", d["policy_reasons"])
        self.assertIn("change_window", r["blocking_checks"])

    def test_the_disabled_check_rule_can_be_relaxed_by_policy(self):
        self.cfg["checks"]["change_window"]["enabled"] = False
        self.cfg["evidence_policy"] = {"autonomous_requires_all_checks_enabled": False}
        self.assertEqual(self.gate("autonomous")["checks"]["change_window"]["status"], "pass")


class GeneratedEvidenceTests(GateBase):
    def test_the_gate_never_writes_to_client_evidence_files(self):
        self.write_client("drift_status.json", self.verified_doc("drift_status"))
        self.write_client("rollback_readiness.json", self.upload_doc("rollback_readiness"))
        before = self.fingerprint(self.evidence)
        for mode in MODES:
            self.gate(mode)
        self.assertEqual(self.fingerprint(self.evidence), before)

    def test_generated_documents_are_written_under_the_generated_folder_with_provenance(self):
        r = self.gate("live")
        gen = self.evidence / "_generated"
        self.assertEqual(sorted(p.name for p in gen.glob("*.json")),
                         ["drift_status.json", "error_budget_status.json", "rollback_readiness.json"])
        docs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in gen.glob("*.json")}
        self.assertEqual(docs["drift_status"]["provenance"], "synthetic")
        self.assertEqual(docs["error_budget_status"]["provenance"], "synthetic")
        self.assertEqual(docs["rollback_readiness"]["provenance"], "estimated")
        for doc in docs.values():
            self.assertEqual(doc["generated_by"], "safety_gate_refresh")
        self.assertEqual(r["live_refresh"]["generated_evidence_dir"], str(gen))
        self.assertNotIn("_documents", r["live_refresh"])

    def test_cmdb_and_lab_snapshots_are_verified(self):
        for env in ("CMDB_ENABLED", "LAB_SNAPSHOT_ENABLED"):
            with patch.dict(os.environ, {env: "true"}):
                for mode in MODES:
                    d = self.gate(mode)["checks"]["infrastructure_drift"]
                    self.assertEqual((d["status"], d["provenance"]), ("pass", "verified"), (env, mode))

    def test_dry_snapshot_is_synthetic(self):
        d = self.gate("live")["checks"]["infrastructure_drift"]
        self.assertEqual((d["status"], d["provenance"]), ("warn", "synthetic"))
        self.assertIn("synthetic", d["summary"])

    def test_snapshot_failure_is_unknown_not_pass_and_is_recorded(self):
        self.fail_drift_capture = True
        for mode in MODES:
            d = self.gate(mode)["checks"]["infrastructure_drift"]
            self.assertEqual((d["status"], d["evidence_issue"]), (DEGRADED[mode], "snapshot_failed"), mode)
        doc = json.loads((self.evidence / "_generated" / "drift_status.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["status"], doc["evidence_issue"]), ("unknown", "snapshot_failed"))

    def test_simulation_failure_is_unknown_and_simulation_issues_warn(self):
        self.fail_simulation = True
        d = self.gate("live")["checks"]["error_budget_slo"]
        self.assertEqual((d["status"], d["evidence_issue"]), ("warn", "simulation_failed"))
        self.fail_simulation = False
        self.sim_status = "failed"
        d = self.gate("simulation")["checks"]["error_budget_slo"]
        self.assertEqual((d["raw_status"], d["status"]), ("warn", "warn"))  # a reported problem is not softened

    def test_simulation_always_uses_a_dry_snapshot_and_the_lab_snapshot_is_never_a_simulation_target(self):
        with patch.dict(os.environ, {"LAB_SNAPSHOT_ENABLED": "true"}):
            self.gate("live")
        modes = {notes.split()[1]: mode for mode, notes in self.engine_calls}  # "pre-check" / "simulation-only"
        self.assertEqual(modes["pre-check"], "lab")
        self.assertEqual(modes["simulation-only"], "dry")

    def test_error_budget_is_labelled_as_a_simulation_result_not_an_slo(self):
        d = self.gate("live")["checks"]["error_budget_slo"]
        self.assertIn("not an SLO error budget", d["summary"])
        self.assertEqual(d["provenance"], "synthetic")

    def test_rollback_readiness_does_not_claim_artifacts_were_verified(self):
        d = self.gate("live")["checks"]["rollback_readiness"]
        self.assertEqual(d["provenance"], "estimated")
        self.assertIn("not verified", d["summary"])
        self.assertNotIn("Rollback artifacts available", json.dumps(d))

    def test_high_blast_radius_warns_even_with_verified_provenance_absent(self):
        r = self.gate("simulation", )
        self.assertIn("blast_radius", r["checks"])
        high = REAL_GATE(AutomationPreExecutionCheckRequest(
            queue_id="q1", classification="dns_issue", playbook="dns_flush.yml", target_host="h",
            risk_tier="high", execution_mode="simulation"))
        self.assertEqual(high["checks"]["blast_radius"]["raw_status"], "warn")
        self.assertEqual(high["checks"]["rollback_readiness"]["raw_status"], "warn")

    def test_verified_client_blast_radius_overrides_the_local_estimate(self):
        self.write_client("blast_radius.json", self.verified_doc("blast_radius", blast_radius_level="low"))
        d = self.gate("autonomous")["checks"]["blast_radius"]
        self.assertEqual((d["status"], d["provenance"]), ("pass", "verified"))

    def test_gate_report_describes_the_policy_and_enforcement(self):
        r = self.gate("autonomous")
        self.assertEqual(r["execution_mode_class"], "autonomous")
        self.assertEqual(r["evidence_policy"]["degraded_status"], DEGRADED)
        self.assertEqual(r["enforcement_mode"], "server_enforced")
        self.assertEqual(r["schema_version"], "pre_execution_safety_gate_v3_evidence_provenance")


class ShippedConfigTests(unittest.TestCase):
    def test_shipped_config_has_no_pass_by_default_and_declares_the_evidence_policy(self):
        cfg = json.loads((ROOT / "config" / "client_onboarding" / "safety_gate_config.json").read_text(encoding="utf-8"))
        self.assertNotIn("pilot_defaults", cfg)
        self.assertNotIn("policy", cfg)
        self.assertEqual(cfg["evidence_policy"]["degraded_status"], DEGRADED)
        self.assertFalse(cfg["evidence_policy"]["treat_uploaded_files_as_verified"])

    def test_default_config_matches_the_shipped_policy(self):
        self.assertEqual(ev.resolve_policy(aa.DEFAULT_SAFETY_GATE_CONFIG), ev.resolve_policy({}))
        self.assertNotIn("pilot_defaults", aa.DEFAULT_SAFETY_GATE_CONFIG)


# -- evidence submission and the approval flow with the real gate ------------------------------------------

class EvidenceSubmitTests(AuthzBase):
    def setUp(self):
        super().setUp()
        self.ev_dir = self.tmp / "evidence"
        self.ev_dir.mkdir()
        for p in (patch.object(evidence_router, "EVIDENCE_STORE_DIR", self.ev_dir),
                  patch.object(evidence_router, "EVIDENCE_UPLOAD_MANIFEST", self.tmp / "manifest.json")):
            p.start()
            self.addCleanup(p.stop)

    PAYLOAD = {"evidence_type": "change_conflict", "service": "claims-api", "environment": "production",
               "resource": "linux-lab-01", "status": "pass", "source_system": "ServiceNow", "summary": "no conflicts"}

    def submit(self, token):
        return self.client.post("/evidence/submit", headers=bearer(token), json=self.PAYLOAD)

    def test_submit_requires_authentication_and_l2(self):
        self.assertEqual(self.client.post("/evidence/submit", json=self.PAYLOAD).status_code, 401)
        self.assertEqual(self.submit("tok-l1").status_code, 403)
        self.assertEqual(self.client.post("/evidence/run_api_pull", headers=bearer("tok-l1")).status_code, 403)

    def test_submit_records_the_authenticated_submitter(self):
        r = self.submit("tok-svc")
        self.assertEqual(r.status_code, 200, r.text)
        doc = json.loads((self.ev_dir / "change_conflict.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["_submitted_via"], doc["submitted_by"], doc["submitted_role"]),
                         ("api_push", "telemetry-svc", "l2"))
        self.assertTrue(doc["submitted_at"])

    def test_the_submitter_cannot_be_spoofed_through_the_body(self):
        payload = {**self.PAYLOAD, "submitted_by": "someone-important", "_submitted_via": "api_pull"}
        r = self.client.post("/evidence/submit", headers=bearer("tok-l2"), json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        doc = json.loads((self.ev_dir / "change_conflict.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["_submitted_via"], doc["submitted_by"]), ("api_push", "bob-l2"))

    def test_pushed_evidence_is_verified_for_the_gate(self):
        self.assertEqual(self.submit("tok-l2").status_code, 200)
        cfg = copy.deepcopy(aa.DEFAULT_SAFETY_GATE_CONFIG)
        cfg["evidence_store_dir"] = str(self.ev_dir)
        fake = types.ModuleType("tools.snapshot_engine")

        class Engine:
            def capture(self, **kw):
                return {"snapshot_id": "s", "state": {}}

            def apply_remediation(self, **kw):
                return {"remediation": {"result": {"status": "simulated"}}}

        fake.SnapshotEngine = Engine
        with patch.object(aa, "_load_safety_gate_config", lambda: cfg), \
             patch.object(aa, "_tenant_evidence_settings", lambda tenant: {}), \
             patch.object(aa, "ROLLBACK_BACKUP_DIR", self.tmp / "bak"), \
             patch.dict(sys.modules, {"tools.snapshot_engine": fake}):
            r = REAL_GATE(AutomationPreExecutionCheckRequest(
                queue_id="q", classification="cpu_saturation", playbook="x.yml", target_host="linux-lab-01",
                execution_mode="autonomous"))
        d = r["checks"]["change_window"]
        self.assertEqual((d["status"], d["provenance"]), ("pass", "verified"))

    def test_gate_trusts_the_api_pull_marker(self):
        # run_from_pull_specs stamps `_submitted_via: api_pull` on what it writes (the pull itself is network
        # bound, so this asserts the contract between the adapter and the gate).
        evidence = adapter.normalize_evidence("drift_status", "svc", "prod", "host", "pass", "SourceSys", "ok")
        self.assertEqual(ev.client_provenance(evidence, "file", ev.resolve_policy({})), "unknown")  # unmarked file
        evidence["_submitted_via"] = "api_pull"
        self.assertEqual(ev.client_provenance(evidence, "file", ev.resolve_policy({})), "verified")

    def test_adapter_source_marks_its_output(self):
        source = (ROOT / "adapters" / "universal_api_adapter.py").read_text(encoding="utf-8")
        self.assertIn('evidence["_submitted_via"] = "api_pull"', source)


class ApprovalFlowWithTheRealGateTests(AuthzBase):
    """Warning identities are mode independent, so an approval given from a simulation-class gate run still
    matches the live-class re-run at execution - and a change in the underlying condition is rejected."""

    def setUp(self):
        super().setUp()
        self.evid = self.tmp / "evidence"
        self.evid.mkdir()
        self.cfg = copy.deepcopy(aa.DEFAULT_SAFETY_GATE_CONFIG)
        self.cfg["evidence_store_dir"] = str(self.evid)
        self.fail_snapshot = False
        outer = self

        class Engine:
            def capture(self, ci_name, mode, notes=""):
                if outer.fail_snapshot and notes.startswith("safety_gate pre-check"):
                    raise RuntimeError("down")
                return {"snapshot_id": "s", "state": {}}

            def apply_remediation(self, **kw):
                return {"remediation": {"result": {"status": "simulated"}}}

        fake = types.ModuleType("tools.snapshot_engine")
        fake.SnapshotEngine = Engine
        for p in (patch.object(aa, "_build_pre_execution_safety_gate", REAL_GATE),
                  patch.object(aa, "_tenant_evidence_settings", lambda tenant: {}),
                  patch.object(aa, "_load_safety_gate_config", lambda: copy.deepcopy(self.cfg)),
                  patch.object(aa, "ROLLBACK_BACKUP_DIR", self.tmp / "bak"),
                  patch.dict(sys.modules, {"tools.snapshot_engine": fake})):
            p.start()
            self.addCleanup(p.stop)

    def simulation_gate(self, qid):
        r = self.client.post("/automation/pre_execution_check", headers=bearer("tok-l2"),
                             json={"queue_id": qid, "classification": "cpu_saturation", "playbook": "linux_restart_nginx.yml",
                                   "target_host": "linux-lab-01", "execution_mode": "simulation"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_default_evidence_requires_an_override_and_the_override_survives_to_execution(self):
        q = self.queue()
        gate = self.simulation_gate(q["queue_id"])
        self.assertEqual(gate["gate_status"], "warning")  # nothing is verified in this environment
        self.assertEqual(self.approve(q["queue_id"]).status_code, 409)
        r = self.approve(q["queue_id"], override_gate_warnings=True, override_reason="Lab evidence is synthetic; accepted")
        self.assertEqual(r.status_code, 200, r.text)
        approval = ae.get_queue_entry(q["queue_id"])["approval"]
        self.assertTrue(approval["override_requested"])
        self.assertEqual({w["check_id"] for w in approval["acknowledged_warnings"]}, set(CHECKS))
        # live re-run is a different execution class (statuses differ) but the same underlying conditions
        self.assertEqual(self.execute(q["queue_id"], "live").status_code, 403)  # no reconfirmation
        r = self.execute(q["queue_id"], "live", override_gate_warnings=True, override_reason="Reconfirmed")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["authorization"]["gate_status"], "warning")

    def test_a_changed_underlying_condition_between_approval_and_execution_is_rejected(self):
        q = self.queue()
        self.simulation_gate(q["queue_id"])
        self.assertEqual(self.approve(q["queue_id"], override_gate_warnings=True, override_reason="ok").status_code, 200)
        self.fail_snapshot = True  # drift evidence goes from "synthetic" to "snapshot_failed"
        r = self.execute(q["queue_id"], "live", override_gate_warnings=True, override_reason="Reconfirmed")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("infrastructure_drift", r.text)
        self.assertEqual(self.connector_calls, [])

    def test_improved_evidence_does_not_require_an_override(self):
        q = self.queue()
        self.simulation_gate(q["queue_id"])
        self.assertEqual(self.approve(q["queue_id"], override_gate_warnings=True, override_reason="ok").status_code, 200)
        self.assertEqual(self.execute(q["queue_id"], "live").status_code, 403)
        # all six checks become verified by client policy + attested evidence
        for check, ev_type, fname in (("infrastructure_drift", "drift_status", "drift_status.json"),
                                      ("error_budget_slo", "error_budget_status", "error_budget_status.json"),
                                      ("concurrent_activity", "concurrent_activity", "concurrent_activity.json"),
                                      ("change_window", "change_conflict", "change_conflict.json"),
                                      ("rollback_readiness", "rollback_readiness", "rollback_readiness.json"),
                                      ("blast_radius", "blast_radius", "blast_radius.json")):
            doc = {"evidence_type": ev_type, "status": "pass", "severity": "low", "source_system": "SourceSys",
                   "summary": "ok", "resource": "linux-lab-01", "checked_at": now_iso(),
                   "_submitted_via": "api_push", "submitted_by": "svc"}
            (self.evid / fname).write_text(json.dumps(doc), encoding="utf-8")
        r = self.execute(q["queue_id"], "live")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["authorization"]["gate_status"], "passed")
        self.assertFalse(r.json()["authorization"]["override"])

    def test_autonomous_entries_are_gated_as_the_autonomous_class(self):
        q = self.queue("tok-svc")
        self.assertEqual((q.get("intent") or {}).get("kind"), "autonomous")
        r = self.execute(q["queue_id"], "live", token="tok-svc")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("blocked", r.text)  # degraded evidence blocks autonomous execution
        self.assertEqual(self.connector_calls, [])

    def test_gate_mode_follows_the_entry_kind(self):
        seen = {}

        def spy(req):
            seen["mode"] = req.execution_mode
            return {"gate_status": "passed", "checks": {}, "warning_checks": [], "blocking_checks": []}

        with patch.object(aa, "_build_pre_execution_safety_gate", spy):
            principal = type("P", (), {"id": "x"})()
            aa._run_gate_for_entry({"queue_id": "a", "plan": {}, "intent": {"kind": "autonomous"}}, principal)
            self.assertEqual(seen["mode"], "autonomous")
            aa._run_gate_for_entry({"queue_id": "b", "plan": {}}, principal)
            self.assertEqual(seen["mode"], "live")


if __name__ == "__main__":
    unittest.main()
