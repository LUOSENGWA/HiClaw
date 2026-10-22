# -*- coding: utf-8 -*-
"""File-backed state directory for the coding-cli plugin (design doc §3).

All mutable plugin state (install task state; dispatch task state from P2)
lives under ``<workspace>/.qwenpaw/coding-cli/`` as files, so the plugin
survives hot reloads and restarts without module-level mutable globals.

Resolution priority:
1. ``QWENPAW_CODING_CLI_STATE_DIR`` env override (tests / ops).
2. ``$QWENPAW_WORKING_DIR/.qwenpaw/coding-cli`` — same env resolution as
   agentteams-matrix-channel (channel.py L3796-3802).
3. ``qwenpaw.constant.WORKING_DIR/.qwenpaw/coding-cli``.
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR_ENV = "QWENPAW_CODING_CLI_STATE_DIR"
WORKING_DIR_ENV = "QWENPAW_WORKING_DIR"


def _qwenpaw_working_dir() -> Path:
    env = os.environ.get(WORKING_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from qwenpaw.constant import WORKING_DIR
    except Exception:  # pragma: no cover - qwenpaw always present in host
        return Path.home() / ".qwenpaw"
    return Path(str(WORKING_DIR)).expanduser()


def state_dir() -> Path:
    """Resolve the plugin state directory (does not create it)."""
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _qwenpaw_working_dir() / ".qwenpaw" / "coding-cli"


def install_dir() -> Path:
    """Directory holding install task files (``<task_id>.json``)."""
    return state_dir() / "install"


__all__ = ["STATE_DIR_ENV", "WORKING_DIR_ENV", "install_dir", "state_dir"]
