from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def _run_qwenpaw2(tmp_path: Path, script: str) -> None:
    home = tmp_path / "home"
    working_dir = home / ".qwenpaw"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PYTHONPATH": str(ROOT / "src"),
            "QWENPAW_WORKING_DIR": str(working_dir),
            "QWENPAW_SECRET_DIR": str(home / ".qwenpaw.secret"),
            "QWENPAW_RUNNING_IN_CONTAINER": "true",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), str(tmp_path)],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"QwenPaw 2 contract subprocess failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_qwenpaw2_applies_real_model_provider_from_runtime_contract(
    tmp_path: Path,
) -> None:
    _run_qwenpaw2(
        tmp_path,
        """
        import os
        from pathlib import Path
        import sys

        from qwenpaw.providers.provider_manager import ProviderManager
        from qwenpaw_worker.config import WorkerConfig
        from qwenpaw_worker.update import MemberRuntimeConfig, RuntimeUpdater

        root = Path(sys.argv[1])
        os.environ["BAILIAN_API_KEY"] = "test-bailian-key"
        config = WorkerConfig(
            worker_name="worker-a",
            fs_endpoint="unused",
            fs_access_key="unused",
            fs_secret_key="unused",
            install_dir=root / "agents",
        )

        class NoopPackageManager:
            def apply(self, _config):
                return None

        RuntimeUpdater(
            config=config,
            package_manager=NoopPackageManager(),
        ).apply_once(
            runtime_config=MemberRuntimeConfig(
                path=config.runtime_config_path,
                raw={
                    "metadata": {"generation": "1"},
                    "member": {"runtime": "qwenpaw"},
                    "desired": {
                        "model": {
                            "providerId": "bailian",
                            "providerName": "Alibaba Cloud Model Studio",
                            "model": "qwen3.6-plus",
                            "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "apiKeyEnv": "BAILIAN_API_KEY",
                        },
                    },
                },
            ),
            reapply_adapter=False,
        )

        manager = ProviderManager.get_instance()
        provider = manager.custom_providers["bailian"]
        assert manager.active_model.provider_id == "bailian"
        assert manager.active_model.model == "qwen3.6-plus"
        assert provider.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert provider.api_key == "test-bailian-key"
        assert provider.chat_model == "OpenAIChatModel"
        """,
    )


def test_qwenpaw2_loads_runtime_mcp_and_agent_package_skills(
    tmp_path: Path,
) -> None:
    _run_qwenpaw2(
        tmp_path,
        """
        import json
        import os
        from pathlib import Path
        import sys

        from qwenpaw.agents.skill_system.registry import resolve_effective_skills
        from qwenpaw.config.config import load_agent_config
        from qwenpaw_worker.config import WorkerConfig
        from qwenpaw_worker.update import MemberRuntimeConfig, RuntimeUpdater

        root = Path(sys.argv[1])
        package = root / "agent-package"
        skill = package / "skills" / "hot-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\\nname: hot-skill\\ndescription: runtime contract\\n---\\n# Hot Skill\\n",
            encoding="utf-8",
        )
        (package / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "package-docs": {
                            "url": "https://package.example.com/mcp",
                            "transport": "http",
                        },
                    },
                },
            ),
            encoding="utf-8",
        )

        os.environ["AGENTTEAMS_WORKER_GATEWAY_KEY"] = "test-gateway-key"
        config = WorkerConfig(
            worker_name="worker-a",
            fs_endpoint="unused",
            fs_access_key="unused",
            fs_secret_key="unused",
            install_dir=root / "agents",
        )
        RuntimeUpdater(config=config).apply_once(
            runtime_config=MemberRuntimeConfig(
                path=config.runtime_config_path,
                raw={
                    "metadata": {"generation": "1"},
                    "member": {"runtime": "qwenpaw"},
                    "credentials": {
                        "gatewayKeyEnv": "AGENTTEAMS_WORKER_GATEWAY_KEY",
                    },
                    "desired": {
                        "agentPackage": {
                            "ref": package.as_uri(),
                            "name": "contract-package",
                            "version": "1.0.0",
                            "digest": "sha256:contract",
                        },
                        "mcpServers": {
                            "runtime-docs": {
                                "url": "https://gateway.example.com/mcp/docs",
                                "transport": "http",
                            },
                        },
                    },
                },
            ),
            reapply_adapter=False,
        )

        workspace = config.default_workspace_dir
        mcporter = json.loads(
            (workspace / "config" / "mcporter.json").read_text(encoding="utf-8"),
        )
        assert mcporter["mcpServers"]["runtime-docs"] == {
            "url": "https://gateway.example.com/mcp/docs",
            "transport": "http",
            "headers": {"Authorization": "Bearer test-gateway-key"},
        }

        package_client = load_agent_config("default").mcp.clients["package-docs"]
        assert package_client.url == "https://package.example.com/mcp"
        assert package_client.transport == "streamable_http"
        assert resolve_effective_skills(workspace, "matrix") == ["hot-skill"]
        """,
    )


def test_qwenpaw2_configures_matrix_overlay_and_sends_text(
    tmp_path: Path,
) -> None:
    _run_qwenpaw2(
        tmp_path,
        """
        import asyncio
        import importlib.util
        import os
        from pathlib import Path
        import sys
        import types

        from qwenpaw.config.config import load_agent_config
        from qwenpaw_worker.config import WorkerConfig
        from qwenpaw_worker.update import MemberRuntimeConfig, RuntimeUpdater

        root = Path(sys.argv[1])
        os.environ["AGENTTEAMS_MATRIX_URL"] = "http://matrix.example.com:6167"
        os.environ["AGENTTEAMS_WORKER_MATRIX_TOKEN"] = "test-matrix-token"
        os.environ["AGENTTEAMS_MATRIX_E2EE"] = "false"
        config = WorkerConfig(
            worker_name="worker-a",
            fs_endpoint="unused",
            fs_access_key="unused",
            fs_secret_key="unused",
            install_dir=root / "agents",
        )

        class NoopPackageManager:
            def apply(self, _config):
                return None

        RuntimeUpdater(
            config=config,
            package_manager=NoopPackageManager(),
        ).apply_once(
            runtime_config=MemberRuntimeConfig(
                path=config.runtime_config_path,
                raw={
                    "metadata": {"generation": "1"},
                    "team": {"teamRoomId": "!team:matrix.local"},
                    "member": {
                        "runtime": "qwenpaw",
                        "matrixUserId": "@worker-a:matrix.local",
                    },
                    "credentials": {
                        "matrixTokenEnv": "AGENTTEAMS_WORKER_MATRIX_TOKEN",
                    },
                },
            ),
            reapply_adapter=False,
        )

        matrix_config = load_agent_config("default").channels.matrix
        assert matrix_config.enabled is True
        assert matrix_config.homeserver == "http://matrix.example.com:6167"
        assert matrix_config.user_id == "@worker-a:matrix.local"
        assert matrix_config.access_token == "test-matrix-token"
        assert matrix_config.password == ""
        assert matrix_config.encryption is False
        assert matrix_config.group_disabled is False
        assert matrix_config.dm_disabled is False
        assert matrix_config.groups["!team:matrix.local"]["requireMention"] is True

        overlay_path = Path(
            os.environ["PYTHONPATH"],
        ) / "matrix" / "channel.py"
        module_name = "qwenpaw.app.channels.matrix.agentteams_contract_channel"
        spec = importlib.util.spec_from_file_location(module_name, overlay_path)
        assert spec is not None and spec.loader is not None
        overlay = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = overlay
        spec.loader.exec_module(overlay)

        channel = overlay.MatrixChannel.from_config(
            lambda *_args, **_kwargs: None,
            matrix_config,
            workspace_dir=config.default_workspace_dir,
        )
        assert channel.homeserver == "http://matrix.example.com:6167"
        assert channel.matrix_user_id == "@worker-a:matrix.local"
        assert channel.get_debounce_key(
            {"meta": {"room_id": "!team:matrix.local"}},
        ) == "matrix:!team:matrix.local"

        calls = []

        class FakeClient:
            async def room_send(self, *args, **kwargs):
                calls.append((args, kwargs))
                return types.SimpleNamespace(event_id="$sent")

            async def room_typing(self, *_args, **_kwargs):
                return None

        channel._client = FakeClient()
        asyncio.run(
            channel.send(
                "!team:matrix.local",
                "hello from QwenPaw 2",
                {"room_id": "!team:matrix.local"},
            ),
        )
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] == "!team:matrix.local"
        assert args[1] == "m.room.message"
        assert args[2]["msgtype"] == "m.text"
        assert args[2]["body"] == "hello from QwenPaw 2"
        assert kwargs["tx_id"]
        """,
    )
