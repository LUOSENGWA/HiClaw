"""Tests for WorkerFlow QwenPaw adapter wiring."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[5]
PLUGIN_PATH = REPO_ROOT / "plugins" / "workerflow" / "adapters" / "qwenpaw" / "plugin.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("workerflow_qwenpaw_plugin_test", PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin from {PLUGIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerFlowQwenPawAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env_keys = [
            "TEAMHARNESS_RUNTIME_CONFIG",
            "AGENTTEAMS_MEMBER_RUNTIME_CONFIG",
            "AGENTTEAMS_MATRIX_URL",
            "AGENTTEAMS_MATRIX_SERVER",
            "AGENTTEAMS_MATRIX_HOMESERVER",
            "AGENTTEAMS_WORKER_MATRIX_TOKEN",
            "AGENTTEAMS_MATRIX_TOKEN",
            "AGENTTEAMS_MATRIX_USER_ID",
            "AGENTTEAMS_MATRIX_DOMAIN",
            "AGENTTEAMS_WORKER_ROLE",
            "AGENTTEAMS_AGENT_ROLE",
            "AGENTTEAMS_WORKER_NAME",
            "QWENPAW_WORKING_DIR",
        ]
        self.old_env = {key: os.environ.get(key) for key in self.env_keys}
        for key in self.env_keys:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_mcp_client_env_includes_runtime_and_matrix_inputs(self) -> None:
        module = load_plugin()
        os.environ["TEAMHARNESS_RUNTIME_CONFIG"] = "/shared/runtime/worker/runtime.yaml"
        os.environ["AGENTTEAMS_MATRIX_URL"] = "http://matrix.local"
        os.environ["AGENTTEAMS_WORKER_MATRIX_TOKEN"] = "matrix-token"
        os.environ["AGENTTEAMS_MATRIX_USER_ID"] = "@worker:matrix.local"
        os.environ["AGENTTEAMS_WORKER_NAME"] = "worker-a"
        os.environ["QWENPAW_WORKING_DIR"] = "/root/.qwenpaw"

        env = module._mcp_client_env()

        self.assertEqual(env["TEAMHARNESS_RUNTIME_CONFIG"], "/shared/runtime/worker/runtime.yaml")
        self.assertEqual(env["AGENTTEAMS_MATRIX_URL"], "http://matrix.local")
        self.assertEqual(env["AGENTTEAMS_WORKER_MATRIX_TOKEN"], "matrix-token")
        self.assertEqual(env["AGENTTEAMS_MATRIX_USER_ID"], "@worker:matrix.local")
        self.assertEqual(env["AGENTTEAMS_WORKER_NAME"], "worker-a")
        self.assertEqual(env["QWENPAW_WORKING_DIR"], "/root/.qwenpaw")

    def test_internal_mcp_policy_allows_only_workerflow(self) -> None:
        module = load_plugin()

        def legacy_mcp_client_to_driver(_client_key, _config):
            policy = types.SimpleNamespace(default_effect="ask", rules=["ask"])
            return types.SimpleNamespace(policy=policy), None

        qwenpaw_module = types.ModuleType("qwenpaw")
        drivers_module = types.ModuleType("qwenpaw.drivers")
        adapters_module = types.ModuleType("qwenpaw.drivers.adapters")
        legacy_module = types.ModuleType("qwenpaw.drivers.adapters.mcp_legacy_config")
        legacy_module.legacy_mcp_client_to_driver = legacy_mcp_client_to_driver
        adapters_module.mcp_legacy_config = legacy_module

        with mock.patch.dict(
            sys.modules,
            {
                "qwenpaw": qwenpaw_module,
                "qwenpaw.drivers": drivers_module,
                "qwenpaw.drivers.adapters": adapters_module,
                "qwenpaw.drivers.adapters.mcp_legacy_config": legacy_module,
            },
        ):
            result = module.install_internal_mcp_allow_policy()
            internal, _ = legacy_module.legacy_mcp_client_to_driver("workerflow", {})
            external, _ = legacy_module.legacy_mcp_client_to_driver("external", {})

        self.assertEqual(result, {"ok": True, "installed": True, "action": "created"})
        self.assertEqual(internal.policy.default_effect, "allow")
        self.assertEqual(internal.policy.rules, [])
        self.assertEqual(external.policy.default_effect, "ask")
        self.assertEqual(external.policy.rules, ["ask"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
