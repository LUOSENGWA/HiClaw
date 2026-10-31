"""Package lifecycle entrypoint tests for the agentteams-teamharness bundle.

``scripts/install.sh`` and ``scripts/uninstall.sh`` are the documented default
installation path; these tests exercise them as black boxes against a
throwaway HOME and PATH-stubbed runtime binaries — no live runtime, no
network, no credentials (the style of test_bundle_adapters.py).

The seven entrypoint-level cases from the #1459 P1 fix: openclaw-only
install/uninstall, the qwenpaw success path, multi-runtime coexistence, the
no-runtime error, failure isolation, artifact-driven uninstall after the
runtime binary disappears, and --dry-run writing nothing. A final test runs
the ``bash -n`` syntax gate on both entrypoints (the container's shell
approval blocks direct ``bash`` invocations; the entrypoints are otherwise
exercised end-to-end by the cases above).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE = REPO_ROOT / "plugins" / "agentteams-teamharness"
INSTALL = BUNDLE / "scripts" / "install.sh"
UNINSTALL = BUNDLE / "scripts" / "uninstall.sh"
PLUGIN_ID = "agentteams-teamharness"
RUNTIME_BINARIES = ("qwenpaw", "openclaw", "hermes")
SKILL_COUNT = 10

# Variables the adapters read; the entrypoints must see a controlled env.
SCRUBBED_VARS = (
    "OPENCLAW_CONFIG",
    "OPENCLAW_WORKSPACE",
    "HERMES_HOME",
    "HERMES_PLUGIN_HOME",
    "QWENPAW_WORKING_DIR",
    "TEAMHARNESS_INSTALL_LOG",
    "AGENTTEAMS_BUNDLE_DIR",
    "AGENTTEAMS_PLUGIN_DIR",
    "TH_OC_ADAPTER_DIR",
    "TH_OC_BUNDLE_DIR",
    "TH_OC_CONFIG",
    "TH_OC_DRY_RUN",
    "TH_HM_BUNDLE_DIR",
    "TH_HM_CONFIG",
    "TH_HM_PLUGIN_HOME",
    "TH_HM_DRY_RUN",
)


def _clean_env(tmp_path: Path, stubs: tuple = (), extra: dict | None = None) -> dict:
    """Controlled env for a package run: temp HOME, controlled runtime PATH.

    PATH is a single overlay bin directory: symlinks to every program of the
    process PATH (first hit wins, as in PATH semantics) except the real
    qwenpaw/openclaw/hermes binaries, plus the requested stub executables.
    Dropping whole directories would also hide the python3 the adapters need
    (a runtime binary can share a directory with it), so the overlay is
    per-program. ``python3`` is a wrapper around the interpreter running the
    tests: a symlink would not do, because CPython finds a venv by its
    launch path, so the wrapper execs the venv python and the adapters'
    inline python (e.g. ``import yaml``) resolves to the same packages the
    tests use. Calling the helper again for the same tmp_path (a later phase
    of one test) refreshes the stubs: earlier stub files are removed, so a
    phase that "loses" a binary really loses it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    # Stub files are regular files named after a runtime binary; anything
    # from an earlier phase of this test that is not requested now is stale.
    for existing in bin_dir.iterdir():
        if (
            existing.is_file()
            and not existing.is_symlink()
            and existing.name in RUNTIME_BINARIES
            and existing.name not in stubs
        ):
            existing.unlink()

    for name in stubs:
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(stub.stat().st_mode | 0o111)

    linked = set(stubs)
    for existing in bin_dir.iterdir():
        linked.add(existing.name)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        for name in entries:
            if name in RUNTIME_BINARIES or name in linked:
                continue
            src = os.path.join(d, name)
            if not os.path.isfile(src):
                continue
            os.symlink(src, bin_dir / name)
            linked.add(name)

    # Point python3 at the interpreter running the tests via a wrapper
    # script (the first PATH hit may be a system python without the
    # packages the adapters import; a symlink would drop a venv because
    # CPython resolves venvs by launch path).
    python3_shim = bin_dir / "python3"
    shim_text = '#!/bin/sh\nexec "%s" "$@"\n' % sys.executable
    if python3_shim.is_symlink() or not python3_shim.is_file() \
            or python3_shim.read_text(encoding="utf-8") != shim_text:
        if python3_shim.is_symlink() or python3_shim.exists():
            python3_shim.unlink()
        python3_shim.write_text(shim_text, encoding="utf-8")
    python3_shim.chmod(python3_shim.stat().st_mode | 0o111)

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in SCRUBBED_VARS and key not in ("PATH", "HOME")
    }
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = str(bin_dir)
    if extra:
        env.update(extra)
    return env


def run_entrypoint(script: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def _qwenpaw_marker(home: Path) -> Path:
    return home / ".qwenpaw" / ".agentteams" / "teamharness" / "installed.json"


def test_openclaw_only_install_and_uninstall(tmp_path):
    home = tmp_path / "home"
    config = home / ".openclaw" / "openclaw.json"
    env = _clean_env(tmp_path, extra={"OPENCLAW_CONFIG": str(config)})

    result = run_entrypoint(INSTALL, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "openclaw: installed" in result.stdout
    assert "qwenpaw: skipped (not detected)" in result.stdout
    assert "hermes: skipped (not detected)" in result.stdout

    data = json.loads(config.read_text(encoding="utf-8"))
    assert "teamharness" in data["mcp"]["servers"]
    staged_root = home / ".openclaw" / "plugins" / PLUGIN_ID
    assert staged_root.is_dir()
    manifest = json.loads((staged_root / "openclaw.plugin.json").read_text(encoding="utf-8"))
    assert len(manifest["skills"]) == SKILL_COUNT

    result = run_entrypoint(UNINSTALL, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "openclaw: uninstalled" in result.stdout
    data = json.loads(config.read_text(encoding="utf-8"))
    assert "teamharness" not in data.get("mcp", {}).get("servers", {})
    assert not staged_root.exists()


def test_qwenpaw_only_success_path(tmp_path):
    home = tmp_path / "home"
    env = _clean_env(tmp_path, stubs=("qwenpaw",))

    result = run_entrypoint(INSTALL, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "qwenpaw: installed" in result.stdout
    assert "openclaw: skipped (not detected)" in result.stdout
    assert "hermes: skipped (not detected)" in result.stdout

    marker = _qwenpaw_marker(home)
    assert marker.is_file()
    state = json.loads(marker.read_text(encoding="utf-8"))
    assert state["runtime"] == "qwenpaw"

    result = run_entrypoint(UNINSTALL, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "qwenpaw: uninstalled" in result.stdout
    assert not marker.exists()


def test_coexistence_installs_and_uninstalls_every_runtime(tmp_path):
    home = tmp_path / "home"
    oc_config = home / ".openclaw" / "openclaw.json"
    hermes_home = home / ".hermes"
    env = _clean_env(
        tmp_path,
        stubs=("qwenpaw",),
        extra={"OPENCLAW_CONFIG": str(oc_config), "HERMES_HOME": str(hermes_home)},
    )

    result = run_entrypoint(INSTALL, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    for runtime in RUNTIME_BINARIES:
        assert f"{runtime}: installed" in result.stdout

    marker = _qwenpaw_marker(home)
    assert marker.is_file()
    assert "teamharness" in json.loads(oc_config.read_text(encoding="utf-8"))["mcp"]["servers"]
    staged_root = home / ".openclaw" / "plugins" / PLUGIN_ID
    assert staged_root.is_dir()
    hermes_config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert "teamharness" in hermes_config["mcp_servers"]
    hermes_plugin = hermes_home / "plugins" / PLUGIN_ID
    assert hermes_plugin.is_dir()

    result = run_entrypoint(UNINSTALL, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    for runtime in RUNTIME_BINARIES:
        assert f"{runtime}: uninstalled" in result.stdout

    assert not marker.exists()
    data = json.loads(oc_config.read_text(encoding="utf-8"))
    assert "teamharness" not in data.get("mcp", {}).get("servers", {})
    assert not staged_root.exists()
    hermes_config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert "teamharness" not in hermes_config.get("mcp_servers", {})
    assert not hermes_plugin.exists()


def test_no_runtime_detected_exits_one_and_lists_runtimes(tmp_path):
    env = _clean_env(tmp_path)

    result = run_entrypoint(INSTALL, env=env)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    for runtime in RUNTIME_BINARIES:
        assert runtime in combined
        assert f"{runtime}: skipped (not detected)" in result.stdout


def test_openclaw_failure_isolates_from_qwenpaw(tmp_path):
    home = tmp_path / "home"
    # The "config file" is a directory: the adapter can stage the plugin but
    # cannot write the config, so its run fails.
    bad_config = home / ".openclaw" / "openclaw.json"
    bad_config.mkdir(parents=True)
    env = _clean_env(
        tmp_path, stubs=("qwenpaw",), extra={"OPENCLAW_CONFIG": str(bad_config)}
    )

    result = run_entrypoint(INSTALL, env=env)
    assert result.returncode != 0
    assert "qwenpaw: installed" in result.stdout
    assert "openclaw: failed (exit" in result.stdout
    assert "hermes: skipped (not detected)" in result.stdout
    # the failure summary names the failing runtime
    assert "openclaw" in result.stdout + result.stderr

    marker = _qwenpaw_marker(home)
    assert marker.is_file()


def test_uninstall_removes_residue_after_binary_disappears(tmp_path):
    home = tmp_path / "home"
    # Install while the runtime is visible (binary on PATH + env); the
    # config sits at the temp-HOME default path.
    env_install = _clean_env(
        tmp_path,
        stubs=("openclaw",),
        extra={"OPENCLAW_CONFIG": str(home / ".openclaw" / "openclaw.json")},
    )
    result = run_entrypoint(INSTALL, env=env_install)
    assert result.returncode == 0, result.stdout + result.stderr

    # The binary disappears and the env is unset; only the artifacts remain.
    env_uninstall = _clean_env(tmp_path)
    assert "OPENCLAW_CONFIG" not in env_uninstall
    result = run_entrypoint(UNINSTALL, env=env_uninstall)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "openclaw: uninstalled" in result.stdout

    config = home / ".openclaw" / "openclaw.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    assert "teamharness" not in data.get("mcp", {}).get("servers", {})
    assert not (home / ".openclaw" / "plugins" / PLUGIN_ID).exists()


def test_dry_run_forwards_to_every_adapter_and_writes_nothing(tmp_path):
    home = tmp_path / "home"
    oc_config = home / ".openclaw" / "openclaw.json"
    hermes_home = home / ".hermes"
    log_file = tmp_path / "logs" / "teamharness-install.jsonl"
    env = _clean_env(
        tmp_path,
        stubs=("qwenpaw",),
        extra={
            "OPENCLAW_CONFIG": str(oc_config),
            "HERMES_HOME": str(hermes_home),
            "TEAMHARNESS_INSTALL_LOG": str(log_file),
        },
    )

    result = run_entrypoint(INSTALL, "--dry-run", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    for runtime in RUNTIME_BINARIES:
        assert f"{runtime}: installed (dry-run)" in result.stdout

    # Nothing was written: no config, no staged roots, no marker, no log.
    assert not (home / ".openclaw").exists()
    assert not (hermes_home / "config.yaml").exists()
    assert not (hermes_home / "plugins").exists()
    assert not (home / ".qwenpaw").exists()
    assert not log_file.exists()

    result = run_entrypoint(UNINSTALL, "--dry-run", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (home / ".openclaw").exists()
    assert not (home / ".qwenpaw").exists()
    assert not log_file.exists()


def test_entrypoints_pass_bash_syntax_check():
    for script in (INSTALL, UNINSTALL):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"bash -n {script.name}: {result.stderr}"
