"""Claude Code adapter: hooks in ~/.claude/settings.json (or the project's .claude/settings.json)."""

from __future__ import annotations

from lanekeeper.hooks import core
from lanekeeper.hooks.core import Event, Result

AGENT = "claude"

# (native event, matcher or None, lanekeeper kind). Read by install.py as well.
SPEC: tuple[tuple[str, str | None, str], ...] = (
    ("PreToolUse", "Bash", "shell"),
    ("PreToolUse", "PowerShell", "shell"),
    ("PreToolUse", "Edit|Write", "edit"),
    ("PostToolUse", "Edit|Write", "post_edit"),
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
