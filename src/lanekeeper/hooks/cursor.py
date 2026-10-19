"""Cursor adapter: hooks in ~/.cursor/hooks.json (or the project's .cursor/hooks.json).

Unverified against a live Cursor build: the stdin field names for session id, cwd, prompt
and file path; whether preToolUse exists for file writes; and the response fields other
than the deny shape. Pass-through answers print nothing, so Cursor's own permission flow
still applies.
"""

from __future__ import annotations

from lanekeeper.hooks import core
from lanekeeper.hooks.core import Event, Result

AGENT = "cursor"

SPEC: tuple[tuple[str, str | None, str], ...] = (
    ("beforeShellExecution", None, "shell"),
    ("preToolUse", "Write", "edit"),
    ("afterFileEdit", None, "post_edit"),
    ("beforeSubmitPrompt", None, "prompt"),
    ("sessionStart", None, "session_start"),
    ("stop", None, "stop"),
    ("sessionEnd", None, "session_end"),
)


def normalize(kind: str, payload: dict) -> Event:
    data = dict(payload)
    if not data.get("session_id") and data.get("conversation_id"):
        data["session_id"] = data["conversation_id"]
    if not isinstance(data.get("cwd"), str):
        roots = data.get("workspace_roots")
        if isinstance(roots, list) and roots and isinstance(roots[0], str):
            data["cwd"] = roots[0]
    return core.event_from_payload(AGENT, kind, data)


def render(kind: str, result: Result) -> dict | None:
    if kind == "shell":
        if result.block:
            return {
                "continue": True,
                "permission": "deny",
                "user_message": result.block,
                "agent_message": result.block,
            }
        return None
    if kind in ("edit", "prompt", "session_start"):
        out: dict = {}
        if result.notices:
            out["user_message"] = "\n".join(result.notices)
        if result.context:
            out["agent_message"] = "\n".join(result.context)
        if out:
            out["continue"] = True
            return out
    return None
