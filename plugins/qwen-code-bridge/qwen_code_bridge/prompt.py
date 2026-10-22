# -*- coding: utf-8 -*-
"""Built-in system-prompt guidance for the Qwen Code bridge.

Registered as one host prompt section (``after="workspace"``). It is short on
purpose: it tells the agent what the bridge chats are and how to delegate,
without duplicating the dispatch API docs.
"""

from __future__ import annotations

PROMPT_TEXT = (
    "## Qwen Code bridge\n"
    "- Chats whose channel is `qwen-code-bridge` are bridge chats:\n"
    "  `QwenCode · 直接对话` is a passthrough to the local Qwen Code CLI "
    "(your messages are answered by the CLI, not by you); "
    "`<task> · <cli> · <state>` chats are read-only dispatch views — sending "
    "a message there refreshes a status card, it does not start a task.\n"
    "- For coding work, prefer dispatching through the bridge API "
    "(`POST /api/qwen-code-bridge/dispatch`) with `origin` set to your own "
    "session triple; the terminal wake returns to that session. Do not poll: "
    "read the task state file or wait for the wake.\n"
    "- `/qcode <prompt>` answers from Qwen Code inside any chat, with "
    "per-session continuity. Approval tiers follow the host tool-execution "
    "level (STRICT→plan, SMART→auto-edit, AUTO→auto, OFF→yolo).\n"
)


def bridge_prompt(_agent=None) -> str:
    """Provider for ``register_prompt_section``."""
    return PROMPT_TEXT


__all__ = ["PROMPT_TEXT", "bridge_prompt"]
