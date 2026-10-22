# -*- coding: utf-8 -*-
"""Qwen Code Bridge plugin entry (management + dispatch + session tooling).

Compatibility contract (design doc §2):

- Only the two-generation intersection ``PluginApi`` methods are used
  (``register_http_router`` / ``register_startup_hook`` /
  ``register_shutdown_hook`` / ``register_middleware``; the last one
  carries the P2 session-injection middleware).
- New-framework-only capabilities are probed with ``hasattr`` (P1
  declares none).
- No runtime patching of framework internals, no module-level mutable
  state: all state lives
  under ``$WORKSPACE/.qwenpaw/coding-cli/`` as files (see
  ``qwen_code_bridge/state.py``).
- ``register`` is idempotent: a repeated call (e.g. new-framework
  update_config replay) detects existing registrations through the
  public registry introspection API and skips them instead of raising.
"""

import logging
import os
import sys
from pathlib import Path

from qwenpaw.plugins.api import PluginApi

logger = logging.getLogger(__name__)

PLUGIN_ID = "qwen-code-bridge"
HTTP_PREFIX = "/qwen-code-bridge"
STARTUP_HOOK_NAME = "qwen_code_bridge.ensure_state_dir"
SHUTDOWN_HOOK_NAME = "qwen_code_bridge.shutdown"

_PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


def _ensure_importable() -> None:
    """Make the sibling backend package importable (workbench pattern)."""
    plugin_dir = str(_PLUGIN_DIR)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)


def _registry(api: PluginApi):
    return getattr(api, "_registry", None)


def _http_prefix_owner(api: PluginApi, prefix: str) -> str | None:
    registry = _registry(api)
    if registry is None:
        return None
    for reg in registry.get_http_router_registrations():
        if reg.prefix == prefix:
            return reg.plugin_id
    return None


def _hook_owner(api: PluginApi, hooks, hook_name: str) -> str | None:
    for hook in hooks:
        if hook.hook_name == hook_name:
            return hook.plugin_id
    return None


def _ensure_state_dir() -> None:
    from qwen_code_bridge.state import state_dir

    state_dir().mkdir(parents=True, exist_ok=True)


def _on_shutdown() -> None:
    logger.info(
        "%s shutdown (no persistent state to release in P1)",
        PLUGIN_ID,
    )


class QwenCodeBridgePlugin:
    """Plugin entry: P1 management surface (probe/settings/install)."""

    def register(self, api: PluginApi) -> None:
        _ensure_importable()
        from qwen_code_bridge.middleware import (
            session_middleware_factory,
        )
        from qwen_code_bridge.router import build_router

        registry = _registry(api)

        # 1) Management API under /api/qwen-code-bridge (idempotent).
        if registry is None:
            # Degraded path (framework setup incomplete): plain register;
            # the framework's own duplicate check still applies.
            api.register_http_router(
                build_router(),
                prefix=HTTP_PREFIX,
                tags=["qwen-code-bridge"],
            )
            api.register_startup_hook(
                STARTUP_HOOK_NAME,
                _ensure_state_dir,
                priority=200,
            )
            api.register_shutdown_hook(
                SHUTDOWN_HOOK_NAME,
                _on_shutdown,
                priority=200,
            )
            api.register_middleware(session_middleware_factory, priority=50)
            self._probe_new_framework(api)
            return

        owner = _http_prefix_owner(api, HTTP_PREFIX)
        if owner is None:
            api.register_http_router(
                build_router(),
                prefix=HTTP_PREFIX,
                tags=["qwen-code-bridge"],
            )
        elif owner == PLUGIN_ID:
            logger.debug(
                "HTTP prefix %s already registered; skipping",
                HTTP_PREFIX,
            )
        else:
            raise RuntimeError(
                f"HTTP prefix {HTTP_PREFIX} is owned by plugin {owner!r}; "
                f"refusing to register {PLUGIN_ID!r} on the same prefix",
            )

        # 2) Startup: pre-create the file-backed state directory (§3).
        owner = _hook_owner(
            api, registry.get_startup_hooks(), STARTUP_HOOK_NAME
        )
        if owner is None:
            api.register_startup_hook(
                STARTUP_HOOK_NAME,
                _ensure_state_dir,
                priority=200,
            )
        elif owner == PLUGIN_ID:
            logger.debug(
                "startup hook %s already registered; skipping",
                STARTUP_HOOK_NAME,
            )
        else:
            raise RuntimeError(
                f"startup hook {STARTUP_HOOK_NAME!r} is owned by plugin "
                f"{owner!r}",
            )

        # 3) Shutdown: log only (file-backed state needs no release).
        owner = _hook_owner(
            api, registry.get_shutdown_hooks(), SHUTDOWN_HOOK_NAME
        )
        if owner is None:
            api.register_shutdown_hook(
                SHUTDOWN_HOOK_NAME,
                _on_shutdown,
                priority=200,
            )
        elif owner == PLUGIN_ID:
            logger.debug(
                "shutdown hook %s already registered; skipping",
                SHUTDOWN_HOOK_NAME,
            )
        else:
            raise RuntimeError(
                f"shutdown hook {SHUTDOWN_HOOK_NAME!r} is owned by plugin "
                f"{owner!r}",
            )

        # 4) Middleware: session env injection (design §5), priority 50.
        if any(
            reg.plugin_id == PLUGIN_ID
            for reg in registry.get_middleware_factories()
        ):
            logger.debug(
                "middleware factory already registered; skipping",
            )
        else:
            api.register_middleware(session_middleware_factory, priority=50)

        self._probe_new_framework(api)
        logger.info(
            "%s registered: management API at /api%s",
            PLUGIN_ID,
            HTTP_PREFIX,
        )

    @staticmethod
    def _probe_new_framework(api: PluginApi) -> None:
        """Probe new-framework-only capabilities via hasattr (design §2).

        P1 declares no provision dependencies; keep the probe shape so
        the P2 dispatch surface can declare its dependencies here.
        """
        if hasattr(api, "provision"):
            logger.debug(
                "new-framework provision API detected (unused in P1)",
            )


plugin = QwenCodeBridgePlugin()
