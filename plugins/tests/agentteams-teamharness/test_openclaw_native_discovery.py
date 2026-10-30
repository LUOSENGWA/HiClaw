"""OpenClaw native-discovery regression for the agentteams-teamharness adapter.

Drives the real install.sh / uninstall.sh against a real ``openclaw`` CLI in
an isolated scratch HOME, reproducing the maintainer's P1 acceptance: after
install, ``openclaw plugins list --json`` must report the plugin id without
"plugin not found" diagnostics (both ``plugins.entries`` and
``plugins.allow`` resolve), and after uninstall the id must be gone without
the warnings returning.

Runs only when an openclaw binary is available (``OPENCLAW_BIN`` or
``openclaw`` on PATH); otherwise the module skips with a clear reason. The
CI job ``openclaw-native-discovery`` (``.github/workflows/test-controller.yml``)
installs ``openclaw@2026.4.14`` and sets ``OPENCLAW_BIN`` +
``OPENCLAW_EXPECTED_VERSION``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE = REPO_ROOT / "plugins" / "agentteams-teamharness"
OPENCLAW_ADAPTER = BUNDLE / "adapters" / "openclaw"
INSTALL = OPENCLAW_ADAPTER / "install.sh"
UNINSTALL = OPENCLAW_ADAPTER / "uninstall.sh"
PLUGIN_ID = "agentteams-teamharness"
NOT_FOUND = re.compile(r"plugin not found", re.IGNORECASE)
EXPECTED_VERSION = os.environ.get("OPENCLAW_EXPECTED_VERSION", "")
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN") or shutil.which("openclaw")

pytestmark = pytest.mark.skipif(
    OPENCLAW_BIN is None,
    reason=(
        "no openclaw binary available for native-discovery regression "
        "(set OPENCLAW_BIN or put openclaw on PATH; CI installs "
        "openclaw@2026.4.14)"
    ),
)


def _isolated_env(home: Path) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    for key in ("OPENCLAW_CONFIG", "OPENCLAW_STATE_DIR", "OPENCLAW_HOME"):
        env.pop(key, None)
    return env


def _run_script(script: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _openclaw(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = _isolated_env(home)
    env["PATH"] = os.pathsep.join(
        part for part in (os.path.dirname(OPENCLAW_BIN), env.get("PATH", "")) if part
    )
    return subprocess.run(
        [OPENCLAW_BIN, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Tolerate non-JSON noise around the payload (banners, notices).
        start, end = text.find("{"), text.rfind("}")
        assert start != -1 and end > start, f"no JSON object in openclaw output:\n{text}"
        return json.loads(text[start : end + 1])


def _find_plugin(payload: dict) -> dict | None:
    for record in payload.get("plugins", []):
        if record.get("id") == PLUGIN_ID:
            return record
    return None


def _assert_no_plugin_not_found(result: subprocess.CompletedProcess, payload: dict) -> None:
    assert not NOT_FOUND.search(result.stdout + result.stderr), (
        f"'plugin not found' warning in openclaw output:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    for key in ("diagnostics",):
        for diagnostic in payload.get(key, []):
            message = diagnostic.get("message", "") if isinstance(diagnostic, dict) else str(diagnostic)
            assert not NOT_FOUND.search(message), f"'plugin not found' in diagnostics: {message}"
    registry = payload.get("registry", {})
    if isinstance(registry, dict):
        for diagnostic in registry.get("diagnostics", []):
            message = (
                diagnostic.get("message", "")
                if isinstance(diagnostic, dict)
                else str(diagnostic)
            )
            assert not NOT_FOUND.search(message), f"'plugin not found' in registry diagnostics: {message}"


@pytest.fixture(scope="module")
def staged_bundle(tmp_path_factory) -> Path:
    """A copy of the bundle owned by the running process.

    OpenClaw's discovery rejects a plugin directory whose ownership differs
    from the running user ("suspicious ownership"), so a checkout owned by
    another uid is not a valid plugin root. Real deployments extract the
    bundle as the runtime user; copying here reproduces that condition.
    """
    destination = tmp_path_factory.mktemp("staged") / BUNDLE.name
    shutil.copytree(BUNDLE, destination)
    return destination


@pytest.fixture(scope="module")
def scratch_home(tmp_path_factory) -> tuple[Path, Path]:
    """Empty HOME with the config at the pinned runtime's default path."""
    home = tmp_path_factory.mktemp("openclaw-home")
    (home / ".openclaw").mkdir()
    return home, home / ".openclaw" / "openclaw.json"


def test_openclaw_binary_reports_expected_version(scratch_home):
    home = scratch_home[0]
    result = _openclaw(home, "--version")
    assert result.returncode == 0, result.stderr
    if EXPECTED_VERSION:
        assert EXPECTED_VERSION in result.stdout + result.stderr, (
            f"openclaw version mismatch (expected {EXPECTED_VERSION}):\n"
            f"{result.stdout}\n{result.stderr}"
        )


def test_install_registers_loadable_plugin(scratch_home, staged_bundle):
    home, config = scratch_home
    workspace = home.parent / f"{home.name}-workspace"
    env = _isolated_env(home)
    adapter = staged_bundle / "adapters" / "openclaw"
    result = _run_script(
        adapter / "install.sh", "--config", str(config), "--workspace", str(workspace), env=env
    )
    assert result.returncode == 0, result.stderr
    assert workspace.is_dir(), "--workspace directory was not created"

    # The registration install.sh wrote is the one native discovery resolves.
    cfg = json.loads(config.read_text())
    assert PLUGIN_ID in cfg["plugins"]["allow"]
    assert str(adapter) in cfg["plugins"]["load"]["paths"]
    assert cfg["plugins"]["entries"][PLUGIN_ID]["enabled"] is True
    assert cfg["mcp"]["servers"]["teamharness"]["cwd"] == str(staged_bundle)

    # The loadable entry exists where discovery looks (adapter dir is the
    # staged plugin root registered in plugins.load.paths).
    assert (adapter / "index.mjs").is_file()

    result = _openclaw(home, "plugins", "list", "--json")
    assert result.returncode == 0, result.stderr
    payload = _extract_json(result.stdout)
    record = _find_plugin(payload)
    assert record is not None, (
        f"{PLUGIN_ID} missing from `openclaw plugins list --json`:\n"
        f"{json.dumps(payload, indent=2)}"
    )
    _assert_no_plugin_not_found(result, payload)


def test_uninstall_removes_plugin_and_warnings_do_not_return(scratch_home, staged_bundle):
    home, config = scratch_home
    env = _isolated_env(home)
    adapter = staged_bundle / "adapters" / "openclaw"

    # Make the state deterministic regardless of test order.
    result = _run_script(adapter / "install.sh", "--config", str(config), env=env)
    assert result.returncode == 0, result.stderr

    result = _run_script(adapter / "uninstall.sh", "--config", str(config), env=env)
    assert result.returncode == 0, result.stderr
    cfg = json.loads(config.read_text())
    assert PLUGIN_ID not in cfg.get("plugins", {}).get("allow", [])
    assert str(adapter) not in cfg.get("plugins", {}).get("load", {}).get("paths", [])
    assert PLUGIN_ID not in cfg.get("plugins", {}).get("entries", {})
    assert "teamharness" not in cfg.get("mcp", {}).get("servers", {})

    result = _openclaw(home, "plugins", "list", "--json")
    assert result.returncode == 0, result.stderr
    payload = _extract_json(result.stdout)
    assert _find_plugin(payload) is None, (
        f"{PLUGIN_ID} still listed after uninstall:\n{json.dumps(payload, indent=2)}"
    )
    _assert_no_plugin_not_found(result, payload)
