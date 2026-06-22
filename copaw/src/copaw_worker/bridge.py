"""
Bridge: translate openclaw.json (HiClaw Worker config) into CoPaw's
config.json + providers.json, then set COPAW_WORKING_DIR so CoPaw
picks up the right workspace.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import json
import os
import shutil
from importlib import resources
from pathlib import Path
from typing import Any


def _port_remap(url: str, is_container: bool) -> str:
    """Remap container-internal :8080 to host-exposed gateway port when needed."""
    if not is_container and url and ":8080" in url:
        gateway_port = os.environ.get("HICLAW_PORT_GATEWAY", "18080")
        return url.replace(":8080", f":{gateway_port}")
    return url


def _is_in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _secret_dir(working_dir: Path) -> Path:
    """Return the secret dir path that copaw uses alongside working_dir."""
    return Path(str(working_dir) + ".secret")


def _patch_copaw_paths(working_dir: Path) -> None:
    """Patch copaw's module-level path constants to point at working_dir.

    copaw.constant captures WORKING_DIR / SECRET_DIR at import time from
    env vars, so setting COPAW_WORKING_DIR after import has no effect.
    We must update the live module objects directly.
    """
    secret_dir = _secret_dir(working_dir)
    secret_dir.mkdir(parents=True, exist_ok=True)

    try:
        import copaw.constant as _const
        _const.WORKING_DIR = working_dir
        _const.SECRET_DIR = secret_dir
        _const.ACTIVE_SKILLS_DIR = working_dir / "active_skills"
        _const.CUSTOMIZED_SKILLS_DIR = working_dir / "customized_skills"
        _const.MEMORY_DIR = working_dir / "memory"
        _const.CUSTOM_CHANNELS_DIR = working_dir / "custom_channels"
        _const.MODELS_DIR = working_dir / "models"
    except ImportError:
        pass

    try:
        import copaw.providers.store as _store
        _store._PROVIDERS_JSON = secret_dir / "providers.json"
        _store._LEGACY_PROVIDERS_JSON_CANDIDATES = (
            Path(__file__).resolve().parent / "providers.json",
            working_dir / "providers.json",
        )
    except ImportError:
        pass

    try:
        import copaw.envs.store as _envs
        _envs._BOOTSTRAP_WORKING_DIR = working_dir
        _envs._BOOTSTRAP_SECRET_DIR = secret_dir
        _envs._ENVS_JSON = secret_dir / "envs.json"
        _envs._LEGACY_ENVS_JSON_CANDIDATES = (working_dir / "envs.json",)
    except (ImportError, AttributeError):
        pass

    # copaw.app.channels.registry binds CUSTOM_CHANNELS_DIR via
    # `from ...constant import CUSTOM_CHANNELS_DIR` at import time, so it keeps
    # a STALE copy of the default path even after we patch copaw.constant above.
    # _discover_custom_channels() / register_custom_channel_routes() read this
    # module global at CALL time, so rebinding it here (before ChannelManager
    # starts) makes them see our working_dir/custom_channels regardless of
    # import order. Without this the patched matrix_channel.py is never
    # discovered and copaw falls back to its builtin (broken) Matrix channel.
    try:
        import copaw.app.channels.registry as _channels_registry
        _channels_registry.CUSTOM_CHANNELS_DIR = working_dir / "custom_channels"
        logger.info(
            "bridge: patched channels registry CUSTOM_CHANNELS_DIR -> %s",
            _channels_registry.CUSTOM_CHANNELS_DIR,
        )
    except ImportError:
        pass


def bridge_openclaw_to_copaw(
    openclaw_cfg: dict[str, Any],
    working_dir: Path,
    *,
    profile: str = "manager",
) -> None:
    """
    Read openclaw_cfg (parsed openclaw.json) and write:
      - <working_dir>/config.json          (global config)
      - <working_dir>/workspaces/default/agent.json (per-agent config)
      - <working_dir>/providers.json       (LLM credentials, for reference)
      - <working_dir>.secret/providers.json (where copaw actually reads from)

    Also sets COPAW_WORKING_DIR env var and patches copaw's module-level
    path constants so the running process uses the correct directory.

    """
    working_dir.mkdir(parents=True, exist_ok=True)
    in_container = _is_in_container()

    _write_config_json(openclaw_cfg, working_dir, in_container)
    _write_agent_json(openclaw_cfg, working_dir, in_container, profile=profile)
    _write_providers_json(openclaw_cfg, working_dir, in_container)

    os.environ["COPAW_WORKING_DIR"] = str(working_dir)

    # Patch module-level constants (import-time values won't reflect env change)
    _patch_copaw_paths(working_dir)

    # Copy providers.json into secret_dir — that's where copaw actually reads it
    secret_dir = _secret_dir(working_dir)
    providers_src = working_dir / "providers.json"
    if providers_src.exists():
        shutil.copy2(providers_src, secret_dir / "providers.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_active_model(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Return the config dict of the active model from openclaw.json, or None.

    Prefers agents.defaults.model.primary ("provider_id/model_id");
    falls back to the first model of the first provider.
    """
    providers_raw = cfg.get("models", {}).get("providers", {})
    if not providers_raw:
        return None

    primary = (
        cfg.get("agents", {})
        .get("defaults", {})
        .get("model", {})
        .get("primary", "")
    )

    if primary and "/" in primary:
        pid, mid = primary.split("/", 1)
        provider = providers_raw.get(pid, {})
        for m in provider.get("models", []):
            if m.get("id") == mid:
                return m

    # Fallback: first provider, first model
    for provider_cfg in providers_raw.values():
        models = provider_cfg.get("models", [])
        if models:
            return models[0]

    return None


def _resolve_context_window(cfg: dict[str, Any]) -> int | None:
    """Return the contextWindow of the active (or first) model, or None."""
    m = _resolve_active_model(cfg)
    if m and "contextWindow" in m:
        return int(m["contextWindow"])
    return None


def _resolve_vision_enabled(cfg: dict[str, Any]) -> bool:
    """Return True if the active model declares image input support.

    The openclaw.json model's ``input`` field is a list of supported modalities
    (e.g. ["text", "image"]).  If the field is absent we assume text-only to
    avoid sending images to a model that cannot handle them.
    """
    m = _resolve_active_model(cfg)
    if m is None:
        return False
    input_types = m.get("input", [])
    return "image" in input_types


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------

def _write_config_json(
    cfg: dict[str, Any],
    working_dir: Path,
    in_container: bool,
) -> None:
    matrix_raw = cfg.get("channels", {}).get("matrix", {})
    homeserver = _port_remap(
        matrix_raw.get("homeserver", ""), in_container
    )
    access_token = matrix_raw.get("accessToken", "")

    # DM allowlist
    dm_cfg = matrix_raw.get("dm", {})
    dm_policy = dm_cfg.get("policy", "allowlist")
    dm_allow_from: list[str] = dm_cfg.get("allowFrom", [])

    # Group allowlist
    group_policy = matrix_raw.get("groupPolicy", "allowlist")
    group_allow_from: list[str] = matrix_raw.get("groupAllowFrom", [])

    # Per-room/group config (pass through as-is for MatrixChannel to use)
    groups = matrix_raw.get("groups", {})

    # History limit: openclaw uses camelCase "historyLimit", bridge to snake_case.
    history_limit = matrix_raw.get("historyLimit")
    if history_limit is None:
        history_limit = (
            cfg.get("messages", {}).get("groupChat", {}).get("historyLimit")
        )

    matrix_channel_cfg: dict[str, Any] = {
        "enabled": matrix_raw.get("enabled", True),
        "homeserver": homeserver,
        "access_token": access_token,
        "encryption": matrix_raw.get("encryption", False),
        "dm_policy": dm_policy,
        "allow_from": dm_allow_from,
        "group_policy": group_policy,
        "group_allow_from": group_allow_from,
        "groups": groups,
        "filter_tool_messages": True,
        "filter_thinking": True,
        "vision_enabled": _resolve_vision_enabled(cfg),
    }
    if history_limit is not None:
        matrix_channel_cfg["history_limit"] = int(history_limit)

    config_path = working_dir / "config.json"
    # Merge with existing config to avoid clobbering other settings
    existing: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path) as f:
            existing = json.load(f)

    existing.setdefault("channels", {})["matrix"] = matrix_channel_cfg
    # Disable console channel (we use Matrix)
    existing["channels"].setdefault("console", {})["enabled"] = False

    # Bridge model context window → agents.running.max_input_length so that
    # CoPaw's memory compaction threshold tracks the actual model capability.
    # We read contextWindow from the first model of the primary (or first)
    # provider to avoid hard-coding a default that mismatches the real model.
    context_window = _resolve_context_window(cfg)
    if context_window is not None:
        existing.setdefault("agents", {}).setdefault("running", {})[
            "max_input_length"
        ] = context_window

    with open(config_path, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)




# ---------------------------------------------------------------------------
# agent.json — per-agent config (CoPaw 1.0.2+ reads this, not config.json)
# ---------------------------------------------------------------------------
def _derive_matrix_user_id(cfg: dict[str, Any], _in_container: bool = False) -> Any:
    """Derive CoPaw Matrix user_id from OpenClaw config or env."""
    m = _matrix_raw(cfg)
    uid = m.get("userId") or m.get("user_id")
    if uid:
        return uid
    domain = os.environ.get("HICLAW_MATRIX_DOMAIN") or os.environ.get("MATRIX_DOMAIN", "")
    if not domain:
        return _MISSING
    local = os.environ.get("HICLAW_WORKER_NAME") or os.environ.get("WORKER_NAME", "manager")
    return f"@{local}:{domain}"


def _derive_heartbeat(cfg: dict[str, Any], _in_container: bool = False) -> Any:
    """Map openclaw agents.defaults.heartbeat -> copaw heartbeat block."""
    hb = cfg.get("agents", {}).get("defaults", {}).get("heartbeat")
    if not isinstance(hb, dict) or not hb:
        return _MISSING
    out: dict[str, Any] = {"enabled": True}
    if "every" in hb:
        out["every"] = hb["every"]
    if "target" in hb:
        out["target"] = hb["target"]
    if "activeHours" in hb:
        out["active_hours"] = hb["activeHours"]
    return out


def _get_path(container: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Return value at ``path`` inside nested dicts, or ``_MISSING``."""
    node: Any = container
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _set_path(container: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Assign ``value`` at ``path``, creating intermediate dicts as needed."""
    node = container
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _deep_merge_local_wins(remote: Any, local: Any) -> Any:
    """Deep-merge two JSON trees where local leaves win over remote."""
    if isinstance(remote, dict) and isinstance(local, dict):
        out: dict[str, Any] = {}
        for k in remote.keys() | local.keys():
            if k in remote and k in local:
                out[k] = _deep_merge_local_wins(remote[k], local[k])
            elif k in remote:
                out[k] = remote[k]
            else:
                out[k] = local[k]
        return out
    return local


def _union_list(remote: list[Any] | None, local: list[Any] | None) -> list[Any]:
    """Concat local then remote, dedup preserving order. Local entries win order."""
    seen: set[str] = set()
    out: list[Any] = []
    for item in (local or []) + (remote or []):
        try:
            key = (
                json.dumps(item, sort_keys=True)
                if isinstance(item, (dict, list))
                else repr(item)
            )
        except TypeError:
            key = repr(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _apply_policy(
    existing: dict[str, Any],
    path: tuple[str, ...],
    policy: str,
    remote_value: Any,
) -> None:
    """Apply one merge policy for one path. ``remote_value == _MISSING`` skips."""
    if remote_value is _MISSING:
        return

    if policy == "remote-wins":
        _set_path(existing, path, remote_value)
        return

    if policy == "union":
        local_value = _get_path(existing, path)
        local_list = local_value if isinstance(local_value, list) else []
        remote_list = remote_value if isinstance(remote_value, list) else []
        _set_path(existing, path, _union_list(remote_list, local_list))
        return

    if policy == "deep-merge":
        local_value = _get_path(existing, path)
        if local_value is _MISSING:
            _set_path(existing, path, remote_value)
        else:
            _set_path(existing, path, _deep_merge_local_wins(remote_value, local_value))
        return

    if policy == "seed":
        local_value = _get_path(existing, path)
        if local_value is _MISSING:
            _set_path(existing, path, remote_value)
        return

    raise ValueError(f"unknown merge policy: {policy}")


_PolicyDeriver = Callable[[dict[str, Any], bool], Any]


_CONTROLLER_FIELDS: list[tuple[tuple[str, ...], str, _PolicyDeriver]] = [
    (("channels", "matrix", "enabled"),
     "remote-wins", lambda c, _: _matrix_raw(c).get("enabled", True)),
    (("channels", "matrix", "homeserver"),
     "remote-wins", lambda c, ic: _port_remap(_matrix_raw(c).get("homeserver", ""), ic)),
    (("channels", "matrix", "access_token"),
     "remote-wins", lambda c, _: _matrix_raw(c).get("accessToken", "")),
    (("channels", "matrix", "user_id"),
     "remote-wins", _derive_matrix_user_id),
    (("channels", "matrix", "encryption"),
     "remote-wins", lambda c, _: _matrix_raw(c).get("encryption", False)),
    (("channels", "matrix", "dm_policy"),
     "remote-wins", lambda c, _: _matrix_raw(c).get("dm", {}).get("policy", "allowlist")),
    (("channels", "matrix", "group_policy"),
     "remote-wins", lambda c, _: _matrix_raw(c).get("groupPolicy", "allowlist")),
    (("channels", "matrix", "filter_tool_messages"),
     "remote-wins", lambda c, _: _matrix_bool(c, "filterToolMessages", "filter_tool_messages", False)),
    (("channels", "matrix", "filter_thinking"),
     "remote-wins", lambda c, _: _matrix_bool(c, "filterThinking", "filter_thinking", True)),
    (("channels", "matrix", "vision_enabled"),
     "remote-wins", lambda c, _: _resolve_vision_enabled(c)),
    (("channels", "matrix", "history_limit"),
     "remote-wins",
     lambda c, _: _resolve_history_limit(c) if _resolve_history_limit(c) is not None else _MISSING),
    (("channels", "matrix", "allow_from"),
     "union", lambda c, _: _matrix_raw(c).get("dm", {}).get("allowFrom", []) or []),
    (("channels", "matrix", "group_allow_from"),
     "union", lambda c, _: _matrix_raw(c).get("groupAllowFrom", []) or []),
    (("channels", "matrix", "groups"),
     "deep-merge", lambda c, _: _matrix_raw(c).get("groups", {}) or {}),
    (("running", "max_input_length"),
     "remote-wins",
     lambda c, _: _resolve_context_window(c) if _resolve_context_window(c) is not None else _MISSING),
    (("running", "embedding_config"),
     "remote-wins",
     lambda c, ic: _resolve_embedding_config(c, ic) if _resolve_embedding_config(c, ic) is not None else _MISSING),
    (("heartbeat",), "seed", _derive_heartbeat),
]


def _apply_credential_guard(standard_dir: Path, runtime_dir: Path) -> None:
    """Inject credagent.json paths into CoPaw's file guard config."""
    from copaw_worker.hooks.credential_guard import apply_credential_guard

    count = apply_credential_guard(standard_dir, runtime_dir)
    if count > 0:
        logger.info("bridge: credential guard applied %d protected paths", count)


def _write_config_json(working_dir: Path) -> None:
    """Install config.json from template if missing. Never overwrite."""
    _install_from_template(working_dir / "config.json", "config.json")
    # Ensure agents.profiles section exists (required by qwenpaw).
    cfg_path = working_dir / "config.json"
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    # Nested merge: preserve existing agents fields while ensuring required keys exist.
    cfg.setdefault("agents", {})
    cfg["agents"].setdefault("active_agent", "default")
    cfg["agents"].setdefault("profiles", {})
    cfg["agents"]["profiles"].setdefault("default", {})
    cfg["agents"]["profiles"]["default"].setdefault("id", "default")
    cfg["agents"]["profiles"]["default"].setdefault(
        "workspace_dir", str(working_dir / "workspaces" / "default")
    )
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _write_agent_json(
    cfg: dict[str, Any],
    working_dir: Path,
    in_container: bool,
    *,
    profile: str = "worker",
) -> None:
    """Create agent.json from template, then overlay Matrix channel config.

    CoPaw 1.0.2+ reads workspace/agent.json for per-agent configuration.
    The template provides defaults; we overlay controller-owned fields
    (Matrix access_token, homeserver, allowlists, context window).
    """
    workspace_dir = working_dir / "workspaces" / "default"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agent_path = workspace_dir / "agent.json"

    # Install from template if missing
    if not agent_path.exists():
        template_name = f"agent.{profile}.json"
        try:
            # Try loading from package templates directory
            tmpl_dir = Path(__file__).resolve().parent / "templates"
            tmpl_path = tmpl_dir / template_name
            if tmpl_path.exists():
                shutil.copy2(str(tmpl_path), str(agent_path))
            else:
                # Fallback: create minimal agent.json
                minimal = {
                    "id": "default",
                    "name": "Manager" if profile == "manager" else "Default Agent",
                    "language": "zh",
                    "channels": {
                        "console": {"enabled": True},
                        "matrix": {
                            "enabled": True,
                            "filter_tool_messages": False,
                            "filter_thinking": True,
                            "allow_from": [],
                            "group_allow_from": [],
                            "groups": {},
                        },
                    },
                    "running": {"max_iters": 200},
                }
                with open(agent_path, "w") as f:
                    json.dump(minimal, f, indent=2)
        except Exception:
            pass

    # Load existing agent.json
    try:
        with open(agent_path) as f:
            agent_cfg = json.load(f)
    except Exception:
        agent_cfg = {"id": "default", "channels": {}, "running": {}}

    # Overlay Matrix channel config from openclaw.json
    matrix_raw = cfg.get("channels", {}).get("matrix", {})
    homeserver = _port_remap(matrix_raw.get("homeserver", ""), in_container)
    access_token = matrix_raw.get("accessToken", "")

    dm_cfg = matrix_raw.get("dm", {})
    dm_allow_from: list[str] = dm_cfg.get("allowFrom", [])
    group_allow_from: list[str] = matrix_raw.get("groupAllowFrom", [])
    groups = matrix_raw.get("groups", {})

    matrix_ch = agent_cfg.setdefault("channels", {}).setdefault("matrix", {})
    matrix_ch["enabled"] = matrix_raw.get("enabled", True)
    if homeserver:
        matrix_ch["homeserver"] = homeserver
    if access_token:
        matrix_ch["access_token"] = access_token
    matrix_ch["allow_from"] = dm_allow_from
    matrix_ch["group_allow_from"] = group_allow_from
    matrix_ch["groups"] = groups
    matrix_ch["filter_tool_messages"] = True
    matrix_ch["filter_thinking"] = True

    # Disable console channel (we use Matrix)
    agent_cfg.setdefault("channels", {}).setdefault("console", {})["enabled"] = False

    # Bridge context window
    context_window = _resolve_context_window(cfg)
    if context_window is not None:
        agent_cfg.setdefault("running", {})["max_input_length"] = context_window

    # Set workspace_dir
    agent_cfg.setdefault("workspace_dir", str(workspace_dir))

    with open(agent_path, "w") as f:
        json.dump(agent_cfg, f, indent=2, ensure_ascii=False)

# ---------------------------------------------------------------------------
# providers.json
# ---------------------------------------------------------------------------

def _write_providers_json(
    cfg: dict[str, Any],
    working_dir: Path,
    in_container: bool,
) -> None:
    providers_raw = cfg.get("models", {}).get("providers", {})

    custom_providers: dict[str, Any] = {}
    active_provider_id = ""
    active_model = ""

    for provider_id, provider_cfg in providers_raw.items():
        base_url = _port_remap(
            provider_cfg.get("baseUrl", ""), in_container
        )
        api_key = provider_cfg.get("apiKey", "")

        models_raw = provider_cfg.get("models", [])
        models = [
            {"id": m["id"], "name": m.get("name", m["id"])}
            for m in models_raw
            if m.get("id")
        ]

        custom_providers[provider_id] = {
            "id": provider_id,
            "name": provider_id,
            "default_base_url": base_url,
            "api_key_prefix": "",
            "models": models,
            "base_url": base_url,
            "api_key": api_key,
            "chat_model": "OpenAIChatModel",
        }

        # Use first provider + first model as active LLM
        if not active_provider_id and models:
            active_provider_id = provider_id
            active_model = models[0]["id"]

    # Resolve active model from agents.defaults.model.primary
    # Format: "provider_id/model_id"
    primary = (
        cfg.get("agents", {})
        .get("defaults", {})
        .get("model", {})
        .get("primary", "")
    )
    if primary and "/" in primary:
        pid, mid = primary.split("/", 1)
        if pid in custom_providers:
            active_provider_id = pid
            active_model = mid

    providers_data: dict[str, Any] = {
        "providers": {},
        "custom_providers": custom_providers,
        "active_llm": {
            "provider_id": active_provider_id,
            "model": active_model,
        },
    }

    providers_path = working_dir / "providers.json"
    with open(providers_path, "w") as f:
        json.dump(providers_data, f, indent=2, ensure_ascii=False)



# ---------------------------------------------------------------------------
# Runtime-to-standard sync (worker uses this to push edits back to sync root)
# ---------------------------------------------------------------------------

def bridge_runtime_to_standard(standard_dir):
    """Materialize runtime-space edits back into the standard sync root."""
    sync_inner_prompt_files_to_outer(standard_dir)


def sync_inner_prompt_files_to_outer(local_dir):
    """Copy agent-edited prompt files from CoPaw workspace back to sync root."""
    inner_outer_files = ("AGENTS.md", "SOUL.md", "HEARTBEAT.md")
    copaw_ws_dir = Path(local_dir) / ".copaw" / "workspaces" / "default"
    for name in inner_outer_files:
        inner = copaw_ws_dir / name
        outer = Path(local_dir) / name
        if not inner.exists():
            continue
        try:
            inner_mtime = inner.stat().st_mtime
        except OSError:
            continue
        outer_mtime = outer.stat().st_mtime if outer.exists() else 0
        if inner_mtime > outer_mtime:
            inner_content = inner.read_text(errors="replace")
            outer_content = outer.read_text(errors="replace") if outer.exists() else ""
            if inner_content != outer_content:
                outer.write_text(inner_content)
                logger.debug(
                    "Inner->Outer sync: .copaw/workspaces/default/%s -> %s",
                    name,
                    name,
                )

# ---------------------------------------------------------------------------
# CLI entry point — used by manager/scripts/init/start-copaw-manager.sh
# ---------------------------------------------------------------------------

def _main_cli(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m copaw_worker.bridge",
        description="Bridge Controller config into CoPaw runtime files.",
    )
    parser.add_argument("--openclaw-json", required=True,
                        help="Path to openclaw.json")
    parser.add_argument("--working-dir", required=True,
                        help="CoPaw working dir (e.g. ~/.copaw)")
    parser.add_argument("--profile", default="manager",
                        choices=["worker", "manager"],
                        help="Template profile (default: manager)")
    args = parser.parse_args(argv)

    from pathlib import Path as _Path
    import json as _json

    openclaw_path = _Path(args.openclaw_json)
    if not openclaw_path.exists():
        print(f"ERROR: {openclaw_path} not found", flush=True)
        return 1

    working_dir = _Path(args.working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)

    with open(openclaw_path) as f:
        controller_config = _json.load(f)

    bridge_openclaw_to_copaw(
        controller_config,
        working_dir,
        profile=args.profile,
    )
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main_cli())
