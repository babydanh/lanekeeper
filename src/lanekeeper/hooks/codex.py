"""Codex CLI adapter: hooks in ~/.codex/hooks.json (or the project's .codex/hooks.json).

The output shape mirrors Claude Code's hook JSON. Field names for Codex are not confirmed
against a live Codex build; see the README's unverified list.
"""

from __future__ import annotations

from lanekeeper.hooks import core
from lanekeeper.hooks.core import Event, Result

AGENT = "codex"

SPEC: tuple[tuple[str, str | None, str], ...] = (
    ("PreToolUse", "Bash", "shell"),
    ("PreToolUse", "apply_patch|Edit|Write", "edit"),
    ("PostToolUse", "apply_patch|Edit|Write", "post_edit"),
    ("UserPromptSubmit", None, "prompt"),
    ("SessionStart", None, "session_start"),
    ("Stop", None, "stop"),
    ("SessionEnd", None, "session_end"),
)
NATIVE: dict[str, str] = {kind: event for event, _, kind in SPEC}


def normalize(kind: str, payload: dict) -> Event:
    return core.event_from_payload(AGENT, kind, payload)


def render(kind: str, result: Result) -> dict | None:
    return core.claude_style(NATIVE[kind], kind, result)
