"""
One default tenant for the whole platform: a request that names no tenant must be handled as the same tenant by the
API, the automation queue, egress policy, telemetry, governance and backtesting.

Run from the project root:  python -m unittest tests.test_default_tenant -v
"""
import os
import pathlib
import unittest
from unittest.mock import patch

import automation_engine
import default_tenant
import egress_policy

ROOT = pathlib.Path(__file__).resolve().parent.parent


class DefaultTenantTests(unittest.TestCase):
    def test_fallback_is_pypl(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIOPS_DEFAULT_TENANT_ID", None)
            self.assertEqual(default_tenant.default_tenant_id(), "client_c")

    def test_environment_override_is_normalised(self):
        with patch.dict(os.environ, {"AIOPS_DEFAULT_TENANT_ID": "  Poc_Client "}):
            self.assertEqual(default_tenant.default_tenant_id(), "poc_client")

    def test_empty_override_falls_back(self):
        with patch.dict(os.environ, {"AIOPS_DEFAULT_TENANT_ID": "   "}):
            self.assertEqual(default_tenant.default_tenant_id(), "client_c")

    def test_modules_share_the_value(self):
        expected = default_tenant.default_tenant_id()
        self.assertEqual(automation_engine.DEFAULT_TENANT_ID, expected)
        self.assertEqual(egress_policy.resolve_tenant(None), expected)
        import agents.backtesting as backtesting
        import agents.governance as governance
        import agents.telemetry as telemetry
        for module in (backtesting, governance, telemetry):
            self.assertEqual(module.DEFAULT_TENANT_ID, expected, module.__name__)

    def test_egress_policy_follows_the_environment(self):
        with patch.dict(os.environ, {"AIOPS_DEFAULT_TENANT_ID": "client_b"}):
            self.assertEqual(egress_policy.resolve_tenant(None), "client_b")
            self.assertEqual(egress_policy.resolve_tenant("default"), "client_b")
            self.assertEqual(egress_policy.resolve_tenant("client_a"), "client_a")  # an explicit tenant always wins

    def test_no_module_reads_the_setting_on_its_own(self):
        offenders = []
        skip = {".venv", "tests", "data", "bak", "__pycache__", ".git", "node_modules"}
        for path in ROOT.rglob("*.py"):
            if set(path.relative_to(ROOT).parts) & skip or path.name == "default_tenant.py":
                continue
            if "AIOPS_DEFAULT_TENANT_ID" in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "read the default tenant through default_tenant.default_tenant_id()")

    def test_local_compose_matches_the_default(self):
        text = (ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("AIOPS_DEFAULT_TENANT_ID=client_a", text)


if __name__ == "__main__":
    unittest.main()
