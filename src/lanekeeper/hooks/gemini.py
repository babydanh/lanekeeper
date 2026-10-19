"""Gemini CLI adapter: hooks in ~/.gemini/settings.json (or the project's .gemini/settings.json).

Unverified against a live Gemini CLI: tool names (write_file, replace, run_shell_command),
which hook events accept systemMessage or additionalContext, and hook timeouts in ms.
"""

from __future__ import annotations

from lanekeeper.hooks import core
from lanekeeper.hooks.core import Event, Result

AGENT = "gemini"

SPEC: tuple[tuple[str, str | None, str], ...] = (
    ("BeforeTool", "run_shell_command", "shell"),
    ("BeforeTool", "write_file|replace", "edit"),
    ("AfterTool", "write_file|replace", "post_edit"),
    ("BeforeAgent", None, "prompt"),
    ("SessionStart", None, "session_start"),
    ("AfterAgent", None, "stop"),
    ("SessionEnd", None, "session_end"),
)
NATIVE: dict[str, str] = {kind: event for event, _, kind in SPEC}


def normalize(kind: str, payload: dict) -> Event:
    return core.event_from_payload(AGENT, kind, payload)


def render(kind: str, result: Result) -> dict | None:
    if kind in ("shell", "edit"):
        if result.block:
            return {"decision": "deny", "reason": result.block}
        messages = result.context + result.notices
        return {"systemMessage": "\n".join(messages)} if messages else None
    if kind in ("post_edit", "prompt", "session_start"):
        out: dict = {}
        if result.context:
            out["hookSpecificOutput"] = {
                "hookEventName": NATIVE[kind],
                "additionalContext": "\n".join(result.context),
            }
        if result.notices:
            out["systemMessage"] = "\n".join(result.notices)
        return out or None
    if kind == "stop" and result.notices:
        return {"systemMessage": "\n".join(result.notices)}
    return None
