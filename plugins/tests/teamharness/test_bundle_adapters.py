"""Static-projection tests for the agentteams-teamharness OpenClaw and Hermes
runtime adapters.

These tests exercise install.sh / uninstall.sh as black-box scripts against
throwaway config files and assert the shape of the runtime configuration the
adapters project — no live OpenClaw or Hermes runtime is required (the
conformance E2E is a separate, later gate).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

BUNDLE = Path(__file__).resolve().parents[3] / "plugins" / "agentteams-teamharness"
OPENCLAW_ADAPTER = BUNDLE / "adapters" / "openclaw"
HERMES_ADAPTER = BUNDLE / "adapters" / "hermes"
MCP_SERVER_PY = BUNDLE / "mcp" / "server.py"
ENV_VAR = "TEAMHARNESS_RUNTIME_CONFIG"
ENV_REF = "${" + ENV_VAR + "}"


def run_script(script: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# 1. dry-run exits 0 and output is parseable
# ---------------------------------------------------------------------------


def test_dry_run_both_adapters_exit_zero_and_parse(tmp_path):
    result = run_script(
        OPENCLAW_ADAPTER / "install.sh",
        "--dry-run",
        "--config",
        str(tmp_path / "openclaw.json"),
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert isinstance(config, dict)

    result = run_script(
        HERMES_ADAPTER / "install.sh",
        "--dry-run",
        "--config",
        str(tmp_path / "config.yaml"),
        "--plugin-home",
        str(tmp_path / "plugin-home"),
    )
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load(result.stdout)
    assert isinstance(config, dict)

    # dry-run changes nothing
    assert not (tmp_path / "openclaw.json").exists()
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "plugin-home").exists()


# ---------------------------------------------------------------------------
# 2. MCP entry exists and points at the bundle's mcp/server.py
# ---------------------------------------------------------------------------


def test_mcp_entries_point_at_bundle_server():
    result = run_script(
        OPENCLAW_ADAPTER / "install.sh", "--dry-run", "--config", "/tmp/unused-oc.json"
    )
    assert result.returncode == 0, result.stderr
    entry = json.loads(result.stdout)["mcp"]["servers"]["teamharness"]
    assert entry["command"] == "python3"
    assert entry["args"] == ["-m", "mcp.server"]
    assert Path(entry["cwd"]) == BUNDLE
    assert (Path(entry["cwd"]) / "mcp" / "server.py").is_file()
    assert entry["env"][ENV_VAR] == ENV_REF

    result = run_script(
        HERMES_ADAPTER / "install.sh",
        "--dry-run",
        "--config",
        "/tmp/unused-hm.yaml",
        "--plugin-home",
        "/tmp/unused-hm-plugin",
    )
    assert result.returncode == 0, result.stderr
    entry = yaml.safe_load(result.stdout)["mcp_servers"]["teamharness"]
    assert entry["command"] == "python3"
    assert entry["args"] == [str(MCP_SERVER_PY)]
    assert MCP_SERVER_PY.is_file()
    assert entry["env"][ENV_VAR] == ENV_REF


# ---------------------------------------------------------------------------
# 3. two consecutive runs are byte-identical
# ---------------------------------------------------------------------------


def test_install_twice_is_byte_identical(tmp_path):
    manifest = OPENCLAW_ADAPTER / "openclaw.plugin.json"
    manifest_before = manifest.read_bytes()
    oc_config = tmp_path / "openclaw.json"
    oc_config.write_text(json.dumps({"agent": {"displayName": "kept"}}) + "\n")
    for _ in range(2):
        result = run_script(OPENCLAW_ADAPTER / "install.sh", "--config", str(oc_config))
        assert result.returncode == 0, result.stderr
    assert json.loads(oc_config.read_text())["agent"] == {"displayName": "kept"}
    assert manifest.read_bytes() == manifest_before, (
        "install must not rewrite an in-sync manifest"
    )

    hm_config = tmp_path / "config.yaml"
    hm_plugin = tmp_path / "plugin-home"
    hm_config.write_text(yaml.safe_dump({"tools": {"enabled": ["kept"]}}))
    for _ in range(2):
        result = run_script(
            HERMES_ADAPTER / "install.sh",
            "--config",
            str(hm_config),
            "--plugin-home",
            str(hm_plugin),
        )
        assert result.returncode == 0, result.stderr
    after = hm_config.read_bytes()
    assert yaml.safe_load(after)["tools"] == {"enabled": ["kept"]}
    for name in ("plugin.yaml", "register.py"):
        assert (hm_plugin / name).read_bytes() == (HERMES_ADAPTER / name).read_bytes()
    skill_md = hm_plugin / "skills" / "agent" / "find-skills" / "SKILL.md"
    assert skill_md.read_bytes() == (BUNDLE / "skills" / "agent" / "find-skills" / "SKILL.md").read_bytes()


# ---------------------------------------------------------------------------
# 4. uninstall removes exactly this adapter's keys, keeps the rest
# ---------------------------------------------------------------------------


def test_uninstall_removes_only_own_keys(tmp_path):
    oc_config = tmp_path / "openclaw.json"
    seeded = {
        "mcp": {"servers": {"other": {"command": "other-cmd"}}},
        "plugins": {
            "allow": ["other-plugin"],
            "load": {"paths": ["/elsewhere/plugin"]},
            "entries": {"other-plugin": {"enabled": True}},
        },
        "agent": {"displayName": "kept"},
    }
    oc_config.write_text(json.dumps(seeded, indent=2) + "\n")
    for script, in ((OPENCLAW_ADAPTER / "install.sh",), (OPENCLAW_ADAPTER / "uninstall.sh",)):
        result = run_script(script, "--config", str(oc_config))
        assert result.returncode == 0, result.stderr
    config = json.loads(oc_config.read_text())
    assert "teamharness" not in config["mcp"]["servers"]
    assert config["mcp"]["servers"]["other"] == {"command": "other-cmd"}
    assert "agentteams-teamharness" not in config["plugins"]["allow"]
    assert "other-plugin" in config["plugins"]["allow"]
    assert str(OPENCLAW_ADAPTER) not in config["plugins"]["load"]["paths"]
    assert config["plugins"]["load"]["paths"] == ["/elsewhere/plugin"]
    assert "agentteams-teamharness" not in config["plugins"]["entries"]
    assert config["plugins"]["entries"]["other-plugin"] == {"enabled": True}
    assert config["agent"] == {"displayName": "kept"}

    hm_config = tmp_path / "config.yaml"
    hm_plugin = tmp_path / "plugin-home"
    hm_config.write_text(yaml.safe_dump({"mcp_servers": {"other": {"url": "http://example.internal/mcp"}}}))
    result = run_script(
        HERMES_ADAPTER / "install.sh",
        "--config",
        str(hm_config),
        "--plugin-home",
        str(hm_plugin),
    )
    assert result.returncode == 0, result.stderr
    assert hm_plugin.is_dir()
    result = run_script(
        HERMES_ADAPTER / "uninstall.sh",
        "--config",
        str(hm_config),
        "--plugin-home",
        str(hm_plugin),
    )
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load(hm_config.read_text())
    assert "teamharness" not in config.get("mcp_servers", {})
    assert config["mcp_servers"]["other"] == {"url": "http://example.internal/mcp"}
    assert not hm_plugin.exists()

    # a foreign plugin dir with the same path is not touched
    hm_plugin.mkdir(parents=True)
    (hm_plugin / "plugin.yaml").write_text("name: some-other-plugin\nversion: 1.0\n")
    result = run_script(
        HERMES_ADAPTER / "uninstall.sh",
        "--config",
        str(hm_config),
        "--plugin-home",
        str(hm_plugin),
    )
    assert result.returncode == 0, result.stderr
    assert hm_plugin.is_dir()


# ---------------------------------------------------------------------------
# 5. register.py imports cleanly and exposes a callable register
# ---------------------------------------------------------------------------


def test_register_py_importable_with_callable_register(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "th_hermes_register_test", HERMES_ADAPTER / "register.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.register)

    # a ctx without the hermes API is a safe no-op (no real registration)
    class ForeignCtx:
        pass

    assert module.register(ForeignCtx()) == 0

    # pointed at the bundle skills, it registers exactly the bundle's skills
    monkeypatch.setattr(module, "SKILLS_DIR", BUNDLE / "skills")

    class FakeCtx:
        def __init__(self):
            self.registered = []

        def register_skill(self, name, path):
            self.registered.append((name, path))

    ctx = FakeCtx()
    count = module.register(ctx)
    assert count == len(ctx.registered) > 0
    names = {name for name, _ in ctx.registered}
    assert "find-skills" in names and "roomflow" in names
    for _name, path in ctx.registered:
        assert Path(path).is_file()


# ---------------------------------------------------------------------------
# 6. no token literal ever reaches stdout, stderr, or the written files
# ---------------------------------------------------------------------------


def test_no_secret_value_in_output_or_configs(tmp_path):
    secret = "sk-test-secret-9f8e7d6c5b4a"
    env = {ENV_VAR: secret}
    oc_config = tmp_path / "openclaw.json"
    hm_config = tmp_path / "config.yaml"
    hm_plugin = tmp_path / "plugin-home"

    collected = []
    for script, args in (
        (OPENCLAW_ADAPTER / "install.sh", ["--dry-run", "--config", str(oc_config)]),
        (OPENCLAW_ADAPTER / "install.sh", ["--config", str(oc_config)]),
        (
            HERMES_ADAPTER / "install.sh",
            [
                "--dry-run",
                "--config",
                str(hm_config),
                "--plugin-home",
                str(hm_plugin),
            ],
        ),
        (
            HERMES_ADAPTER / "install.sh",
            ["--config", str(hm_config), "--plugin-home", str(hm_plugin)],
        ),
    ):
        result = run_script(script, *args, env_extra=env)
        assert result.returncode == 0, result.stderr
        collected.append(result.stdout + result.stderr)

    collected.append(oc_config.read_text())
    collected.append(hm_config.read_text())
    for path in sorted(hm_plugin.rglob("*")):
        if path.is_file():
            collected.append(path.read_text())

    for blob in collected:
        assert secret not in blob
    assert ENV_REF in oc_config.read_text()
    assert ENV_REF in hm_config.read_text()
