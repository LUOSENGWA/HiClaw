"""Adapter projection tests for the agentteams-matrix bundle.

Covers the OpenClaw and Hermes adapters, which project the bundle
contract.yaml onto their runtime config file (openclaw.json for
OpenClaw, config.yaml for Hermes). The tests drive the real
install.sh / uninstall.sh scripts through subprocesses against temp
config files:

  1. --dry-run exits 0 and stdout parses as the target format
  2. a real install writes all required keys at the key paths the
     upstream runtimes actually read (evidence below, per key in
     _expected_projection and _assert_openclaw_nested)
  3. two installs in a row leave a byte-identical file
  4. uninstall removes only the adapter-owned keys, keeps foreign
     keys (including foreign sub-keys of the deep-merged blocks)
  5. without --with-credentials no token literal appears anywhere
  6. a missing contract.yaml fails with a non-zero exit and a clear
     error

Upstream evidence for the projected key paths:

OpenClaw (v2026.10.6, fe06181d) — extensions/matrix/src/
config-schema.ts: enabled:103, homeserver:127, userId:130,
groupPolicy:142, historyLimit:160, autoJoin:161,
autoJoinAllowlist:162, groupAllowFrom:163, groups map:169; the
nested dm block {enabled, policy, allowFrom} is defined in
src/channels/plugins/config-schema.ts:89-96; per-room
requireMention is a group-entry field at
src/channels/plugins/config-schema.ts:62; dm policy values
(pairing|allowlist|open|disabled) at
src/config/zod-schema.core.ts:436.

Hermes (v0.22.5, bf0203a7) — the platforms: block of
~/.hermes/config.yaml is parsed by PlatformConfig.from_dict
(gateway/config.py:781,445), which promotes every non-typed key of
a platform block into config.extra (gateway/config.py:448-452);
the matrix adapter reads its settings extra-first
(gateway/platforms/_shared.py:106): homeserver at
plugins/platforms/matrix/adapter.py:829, user_id at :831,
allowed_rooms at :863, allowed_users at :898 (applied at
:2611-2612), require_mention at :940; the typed token field
(gateway/config.py:411) is read at adapter.py:830.

No network access and no runtime binaries are involved: the
adapters are pure file projectors.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_DIR = REPO_ROOT / "plugins" / "agentteams-matrix"
CONTRACT = BUNDLE_DIR / "contract.yaml"
BUNDLE_INSTALL = BUNDLE_DIR / "scripts" / "install.sh"
BUNDLE_UNINSTALL = BUNDLE_DIR / "scripts" / "uninstall.sh"
TOKEN = "sentinel-matrix-token-not-a-real-secret-0123456789"

CONTRACT_MATRIX = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["matrix"]

ADAPTERS = {
    "openclaw": {
        "install": BUNDLE_DIR / "adapters" / "openclaw" / "install.sh",
        "uninstall": BUNDLE_DIR / "adapters" / "openclaw" / "uninstall.sh",
        # accessToken — extensions/matrix/src/config-schema.ts:131
        "token_key": "accessToken",
    },
    "hermes": {
        "install": BUNDLE_DIR / "adapters" / "hermes" / "install.sh",
        "uninstall": BUNDLE_DIR / "adapters" / "hermes" / "uninstall.sh",
        # token — typed PlatformConfig field, gateway/config.py:411,
        # read at plugins/platforms/matrix/adapter.py:830
        "token_key": "token",
    },
}


def _expected_projection(adapter: str) -> dict:
    """Flat keys the adapter writes, at the upstream key paths.

    OpenClaw channels.matrix (extensions/matrix/src/
    config-schema.ts): enabled:103, homeserver:127, userId:130,
    historyLimit:160, groupPolicy:142, groupAllowFrom:163,
    autoJoin:161, autoJoinAllowlist:162. The nested dm/ and groups/
    blocks are asserted in _assert_openclaw_nested (dm:
    src/channels/plugins/config-schema.ts:89-96; per-room
    requireMention: src/channels/plugins/config-schema.ts:62).

    Hermes platforms.matrix (gateway/config.py:445 promotes every
    non-typed platform-block key into config.extra, which the
    matrix adapter reads extra-first): enabled — typed field,
    gateway/config.py:410; homeserver — adapter.py:829;
    user_id — adapter.py:831; allowed_users — adapter.py:898
    (a single allowlist for DMs and groups, applied at
    adapter.py:2611-2612); allowed_rooms — adapter.py:863;
    require_mention — adapter.py:940.

    The contract's access_token_ref and history_limit /
    history_mode are deliberately NOT projected: no upstream key
    exists for them (see the adapter READMEs).
    """
    m = CONTRACT_MATRIX
    if adapter == "openclaw":
        return {
            "enabled": True,
            "homeserver": m["homeserver"],
            "userId": m["user_id"],
            "historyLimit": m["history_limit"],
            "groupPolicy": "allowlist",
            "groupAllowFrom": list(m["group_allow_from"]),
            "autoJoin": "allowlist",
            "autoJoinAllowlist": list(m["rooms"]),
        }
    return {
        "enabled": True,
        "homeserver": m["homeserver"],
        "user_id": m["user_id"],
        "allowed_users": list(
            dict.fromkeys(list(m["allow_from"]) + list(m["group_allow_from"]))
        ),
        "allowed_rooms": list(m["rooms"]),
        "require_mention": bool(m["require_mention"]),
    }


def _assert_openclaw_nested(block: dict) -> None:
    """Nested channels.matrix blocks, at the upstream key paths."""
    m = CONTRACT_MATRIX
    # dm.allowFrom lives in the nested dm block {enabled, policy,
    # allowFrom} (src/channels/plugins/config-schema.ts:89-96); the
    # runtime's default dm policy is "pairing" (monitor/index.ts:264),
    # so the explicit allowlist policy is part of the projection.
    assert block["dm"]["policy"] == "allowlist"
    assert block["dm"]["allowFrom"] == list(m["allow_from"])
    # groupPolicy accompanies groupAllowFrom so the list is a hard
    # gate (config-schema.ts:142,163; values at
    # src/config/zod-schema.core.ts:434).
    assert block["groupPolicy"] == "allowlist"
    # One groups.<room>.requireMention entry per contract room:
    # requireMention is a per-room group-entry field
    # (src/channels/plugins/config-schema.ts:62; groups map at
    # config-schema.ts:169) — there is no top-level requireMention.
    for room in m["rooms"]:
        assert block["groups"][room]["requireMention"] == m["require_mention"]


def _run(script: Path, args: list, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        env=env,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _env(tmp_path: Path, bundle_dir: Path | None = None) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    if bundle_dir is not None:
        env["AGENTTEAMS_BUNDLE_DIR"] = str(bundle_dir)
    env.pop("AGENTTEAMS_MATRIX_ACCESS_TOKEN", None)
    return env


def _load(adapter: str, text: str):
    if adapter == "openclaw":
        return json.loads(text)
    return yaml.safe_load(text)


def _load_file(adapter: str, path: Path):
    return _load(adapter, path.read_text(encoding="utf-8"))


def _matrix_block(adapter: str, config: dict) -> dict:
    if adapter == "openclaw":
        return config["channels"]["matrix"]
    return config["platforms"]["matrix"]


def _seed_config(adapter: str, path: Path) -> None:
    """Write a config file with foreign keys the adapter must preserve."""
    if adapter == "openclaw":
        config = {
            "agents": {"defaults": {"timeoutSeconds": 1800}},
            "channels": {
                "whatsapp": {"enabled": True},
                "matrix": {
                    "streaming": "partial",
                    "deviceName": "AgentTeams-adapter-test",
                    "dm": {"threadReplies": "off"},
                    "groups": {"!foreign:agentteams.local": {"autoReply": True}},
                },
            },
            "session": {"resetByType": {"dm": {"mode": "daily"}}},
        }
        path.write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        config = {
            "terminal": {"backend": "local", "cwd": "/root"},
            "memory": {"memory_enabled": True},
            "platforms": {"matrix": {"reply_to_mode": "first"}},
        }
        path.write_text(
            yaml.safe_dump(
                config,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )


def _foreign_keys_ok(adapter: str, config: dict) -> None:
    if adapter == "openclaw":
        assert config["agents"] == {"defaults": {"timeoutSeconds": 1800}}
        assert config["channels"]["whatsapp"] == {"enabled": True}
        assert config["channels"]["matrix"]["streaming"] == "partial"
        assert config["channels"]["matrix"]["deviceName"] == "AgentTeams-adapter-test"
        # Foreign sub-keys of the deep-merged dm/ and groups/ blocks
        # survive.
        assert config["channels"]["matrix"]["dm"]["threadReplies"] == "off"
        assert config["channels"]["matrix"]["groups"]["!foreign:agentteams.local"] == {
            "autoReply": True
        }
        assert config["session"] == {"resetByType": {"dm": {"mode": "daily"}}}
    else:
        assert config["terminal"] == {"backend": "local", "cwd": "/root"}
        assert config["memory"] == {"memory_enabled": True}
        # Foreign key inside the platforms.matrix block survives.
        assert config["platforms"]["matrix"]["reply_to_mode"] == "first"


@pytest.mark.parametrize("adapter", ["openclaw", "hermes"])
class TestMatrixAdapters:
    def test_dry_run_parses_and_writes_nothing(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "runtime-config"
        _seed_config(adapter, config_path)
        before = config_path.read_bytes()

        result = _run(
            specs["install"],
            ["--dry-run", "--config", str(config_path)],
            _env(tmp_path),
        )
        assert result.returncode == 0, result.stderr

        config = _load(adapter, result.stdout)
        block = _matrix_block(adapter, config)
        for key, value in _expected_projection(adapter).items():
            assert block[key] == value
        if adapter == "openclaw":
            _assert_openclaw_nested(block)
        _foreign_keys_ok(adapter, config)

        assert config_path.read_bytes() == before, "dry run must not write"

    def test_dry_run_without_existing_file(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "home" / "nested" / "runtime-config"

        result = _run(
            specs["install"],
            ["--dry-run", "--config", str(config_path)],
            _env(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        config = _load(adapter, result.stdout)
        block = _matrix_block(adapter, config)
        for key, value in _expected_projection(adapter).items():
            assert block[key] == value
        if adapter == "openclaw":
            _assert_openclaw_nested(block)
        assert not config_path.exists(), "dry run must not create the file"

    def test_install_projects_contract_and_preserves_foreign_keys(
        self, tmp_path, adapter
    ):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "runtime-config"
        _seed_config(adapter, config_path)

        result = _run(specs["install"], ["--config", str(config_path)], _env(tmp_path))
        assert result.returncode == 0, result.stderr

        config = _load_file(adapter, config_path)
        block = _matrix_block(adapter, config)
        for key, value in _expected_projection(adapter).items():
            assert block[key] == value
        if adapter == "openclaw":
            _assert_openclaw_nested(block)
        _foreign_keys_ok(adapter, config)
        # Default mode writes no credential material at all (the
        # contract's access_token_ref has no key on either runtime).
        assert specs["token_key"] not in block

    def test_install_twice_is_byte_identical(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]

        # Seeded config: merge path stays stable across runs.
        config_path = tmp_path / "runtime-config"
        _seed_config(adapter, config_path)
        _run(specs["install"], ["--config", str(config_path)], _env(tmp_path))
        first = config_path.read_bytes()
        result = _run(
            specs["install"], ["--config", str(config_path)], _env(tmp_path)
        )
        assert result.returncode == 0, result.stderr
        assert config_path.read_bytes() == first

        # Fresh config (file does not exist yet): same guarantee.
        fresh = tmp_path / "fresh" / "runtime-config"
        _run(specs["install"], ["--config", str(fresh)], _env(tmp_path))
        first_fresh = fresh.read_bytes()
        _run(specs["install"], ["--config", str(fresh)], _env(tmp_path))
        assert fresh.read_bytes() == first_fresh

    def test_uninstall_removes_owned_keys_only(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "runtime-config"
        _seed_config(adapter, config_path)
        _run(specs["install"], ["--config", str(config_path)], _env(tmp_path))

        result = _run(
            specs["uninstall"], ["--config", str(config_path)], _env(tmp_path)
        )
        assert result.returncode == 0, result.stderr

        assert config_path.is_file(), "uninstall must not delete the config file"
        config = _load_file(adapter, config_path)
        owned = set(_expected_projection(adapter)) | {specs["token_key"]}
        if adapter == "openclaw":
            block = config["channels"]["matrix"]
            assert not owned.intersection(block)
            # Foreign keys inside channels.matrix survive, including the
            # foreign sub-keys of the deep-merged dm/ and groups/ blocks
            # (the per-room entry the installer created is dropped once
            # empty; the foreign room entry is untouched).
            assert block == {
                "streaming": "partial",
                "deviceName": "AgentTeams-adapter-test",
                "dm": {"threadReplies": "off"},
                "groups": {"!foreign:agentteams.local": {"autoReply": True}},
            }
        else:
            block = config["platforms"]["matrix"]
            assert not owned.intersection(block)
            # Only the foreign key remains in the block.
            assert block == {"reply_to_mode": "first"}
        _foreign_keys_ok(adapter, config)

    def test_uninstall_is_idempotent(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "runtime-config"
        _seed_config(adapter, config_path)
        _run(specs["install"], ["--config", str(config_path)], _env(tmp_path))
        _run(specs["uninstall"], ["--config", str(config_path)], _env(tmp_path))
        after_first = config_path.read_bytes()

        result = _run(
            specs["uninstall"], ["--config", str(config_path)], _env(tmp_path)
        )
        assert result.returncode == 0, result.stderr
        assert config_path.read_bytes() == after_first

    def test_uninstall_after_fresh_install(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "runtime-config"

        _run(specs["install"], ["--config", str(config_path)], _env(tmp_path))
        _run(specs["uninstall"], ["--config", str(config_path)], _env(tmp_path))

        assert config_path.is_file(), "uninstall must not delete the config file"
        config = _load_file(adapter, config_path)
        if adapter == "openclaw":
            assert "channels" not in config
        else:
            assert "platforms" not in config

    def test_token_never_leaked_without_flag(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "runtime-config"
        _seed_config(adapter, config_path)
        env = _env(tmp_path)
        env["AGENTTEAMS_MATRIX_ACCESS_TOKEN"] = TOKEN

        # Real install without the flag: no token in file or output.
        result = _run(specs["install"], ["--config", str(config_path)], env)
        assert result.returncode == 0, result.stderr
        assert TOKEN not in config_path.read_text(encoding="utf-8")
        assert specs["token_key"] not in _matrix_block(
            adapter, _load_file(adapter, config_path)
        )
        assert TOKEN not in result.stdout
        assert TOKEN not in result.stderr

        # Dry run: stdout is the whole document, so it must be clean too.
        dry = _run(
            specs["install"],
            ["--dry-run", "--config", str(config_path)],
            env,
        )
        assert dry.returncode == 0, dry.stderr
        assert TOKEN not in dry.stdout
        assert TOKEN not in dry.stderr

    def test_with_credentials_writes_token_and_chmod600(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "runtime-config"
        _seed_config(adapter, config_path)
        env = _env(tmp_path)
        env["AGENTTEAMS_MATRIX_ACCESS_TOKEN"] = TOKEN

        result = _run(
            specs["install"],
            ["--with-credentials", "--config", str(config_path)],
            env,
        )
        assert result.returncode == 0, result.stderr
        block = _matrix_block(adapter, _load_file(adapter, config_path))
        assert block[specs["token_key"]] == TOKEN
        # The contract's access_token_ref must not leak in as a dead
        # key (no such key exists on either runtime).
        if adapter == "openclaw":
            assert "accessTokenRef" not in block
        else:
            assert "access_token_ref" not in block
        assert (config_path.stat().st_mode & 0o777) == 0o600
        assert TOKEN not in result.stdout
        assert TOKEN not in result.stderr

        # Dry run redacts the token instead of printing it.
        dry = _run(
            specs["install"],
            ["--with-credentials", "--dry-run", "--config", str(config_path)],
            env,
        )
        assert dry.returncode == 0, dry.stderr
        assert TOKEN not in dry.stdout
        assert TOKEN not in dry.stderr
        preview = _load(adapter, dry.stdout)
        assert (
            _matrix_block(adapter, preview)[specs["token_key"]] == "<redacted>"
        )

    def test_with_credentials_requires_env(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        config_path = tmp_path / "runtime-config"
        env = _env(tmp_path)  # no AGENTTEAMS_MATRIX_ACCESS_TOKEN

        result = _run(
            specs["install"],
            ["--with-credentials", "--config", str(config_path)],
            env,
        )
        assert result.returncode != 0
        assert "AGENTTEAMS_MATRIX_ACCESS_TOKEN" in result.stderr
        assert not config_path.exists()

    def test_missing_contract_fails_with_clear_error(self, tmp_path, adapter):
        specs = ADAPTERS[adapter]
        empty_bundle = tmp_path / "empty-bundle"
        empty_bundle.mkdir()
        env = _env(tmp_path, bundle_dir=empty_bundle)
        config_path = tmp_path / "runtime-config"

        result = _run(specs["install"], ["--config", str(config_path)], env)
        assert result.returncode != 0
        assert "contract" in (result.stderr + result.stdout).lower()
        assert not config_path.exists()


def test_openclaw_groups_entry_per_room(tmp_path):
    """One groups.<room>.requireMention entry per contract room.

    requireMention is a per-room group-entry field (src/channels/
    plugins/config-schema.ts:62; groups map at config-schema.ts:169)
    and the room list rides on autoJoinAllowlist
    (config-schema.ts:161-162), so a two-room contract must produce
    exactly two per-room entries — and the uninstaller must remove
    exactly those.
    """
    specs = ADAPTERS["openclaw"]
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    contract["matrix"]["rooms"] = [
        "!room-a:agentteams.local",
        "!room-b:agentteams.local",
    ]
    (bundle / "contract.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    config_path = tmp_path / "openclaw.json"
    env = _env(tmp_path, bundle_dir=bundle)
    result = _run(specs["install"], ["--config", str(config_path)], env)
    assert result.returncode == 0, result.stderr

    block = _load_file("openclaw", config_path)["channels"]["matrix"]
    assert block["autoJoinAllowlist"] == contract["matrix"]["rooms"]
    for room in contract["matrix"]["rooms"]:
        assert block["groups"][room] == {"requireMention": True}

    result = _run(specs["uninstall"], ["--config", str(config_path)], env)
    assert result.returncode == 0, result.stderr
    config = _load_file("openclaw", config_path)
    assert "channels" not in config


def test_hermes_projection_targets_platforms_matrix_block(tmp_path):
    """The projection must land in the platforms.matrix block, not a
    top-level matrix: block.

    The platforms: block is parsed by PlatformConfig.from_dict
    (gateway/config.py:781,445), which promotes non-typed keys into
    config.extra (gateway/config.py:448-452); the matrix adapter reads
    those extra-first (plugins/platforms/matrix/adapter.py:829,831,
    863,898,940). A top-level matrix: block would only bridge its few
    behavior keys (_YAML_BRIDGE, adapter.py:3235-3248) — homeserver,
    user_id and the token would never reach the adapter from there.
    """
    specs = ADAPTERS["hermes"]
    config_path = tmp_path / "config.yaml"
    result = _run(specs["install"], ["--config", str(config_path)], _env(tmp_path))
    assert result.returncode == 0, result.stderr

    config = _load_file("hermes", config_path)
    assert "matrix" not in config, "no top-level matrix: block (P1 regression)"
    block = config["platforms"]["matrix"]
    for key, value in _expected_projection("hermes").items():
        assert block[key] == value
    # allow_from + group_allow_from merge into the single allowed_users
    # allowlist (adapter.py:898), deduped with contract order preserved.
    m = CONTRACT_MATRIX
    assert block["allowed_users"] == list(
        dict.fromkeys(list(m["allow_from"]) + list(m["group_allow_from"]))
    )


def test_openclaw_uninstall_missing_contract_fails(tmp_path):
    specs = ADAPTERS["openclaw"]
    empty_bundle = tmp_path / "empty-bundle"
    empty_bundle.mkdir()
    env = _env(tmp_path, bundle_dir=empty_bundle)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps({"channels": {"matrix": {"enabled": True}}}) + "\n",
        encoding="utf-8",
    )

    result = _run(specs["uninstall"], ["--config", str(config_path)], env)
    assert result.returncode != 0
    assert "contract" in result.stderr
    # Nothing was removed: the config is untouched.
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "channels": {"matrix": {"enabled": True}}
    }


def _make_fake_runtime(bin_dir: Path, name: str) -> None:
    fake = bin_dir / name
    fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)


def test_bundle_installer_dispatches_selected_adapter(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_runtime(bin_dir, "openclaw")
    env = _env(tmp_path)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    config_path = tmp_path / "openclaw.json"
    result = _run(
        BUNDLE_INSTALL,
        ["--adapter", "openclaw", "--config", str(config_path)],
        env,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(config_path.read_text(encoding="utf-8"))
    block = config["channels"]["matrix"]
    for key, value in _expected_projection("openclaw").items():
        assert block[key] == value


def test_bundle_installer_rejects_unknown_adapter(tmp_path):
    result = _run(BUNDLE_INSTALL, ["--adapter", "nosuch"], _env(tmp_path))
    assert result.returncode != 0
    assert "unknown adapter" in result.stderr


def test_bundle_uninstaller_dispatches_selected_adapter(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_runtime(bin_dir, "hermes")
    env = _env(tmp_path)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    specs = ADAPTERS["hermes"]
    config_path = tmp_path / "config.yaml"
    _run(specs["install"], ["--config", str(config_path)], env)

    result = _run(
        BUNDLE_UNINSTALL,
        ["--adapter", "hermes", "--config", str(config_path)],
        env,
    )
    assert result.returncode == 0, result.stderr
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "platforms" not in config


def test_bundle_uninstaller_rejects_unknown_adapter(tmp_path):
    result = _run(BUNDLE_UNINSTALL, ["--adapter", "nosuch"], _env(tmp_path))
    assert result.returncode != 0
    assert "unknown adapter" in result.stderr
