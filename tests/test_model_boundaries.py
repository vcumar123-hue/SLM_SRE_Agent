"""Enforce ownership boundaries between HTTP and agent contract models."""

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _defined_classes(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


class ModelBoundaryTests(unittest.TestCase):
    def test_root_and_agent_model_modules_define_no_duplicate_classes(self):
        root_models = _defined_classes(ROOT / "models.py")
        agent_models = _defined_classes(ROOT / "agents" / "models.py")
        self.assertEqual(root_models & agent_models, set())

    def test_agents_models_contains_contracts_not_http_requests(self):
        agent_models = _defined_classes(ROOT / "agents" / "models.py")
        self.assertIn("OrchestratorState", agent_models)
        self.assertIn("RCAInput", agent_models)
        self.assertFalse({name for name in agent_models if name.endswith("Request")})


if __name__ == "__main__":
    unittest.main()
