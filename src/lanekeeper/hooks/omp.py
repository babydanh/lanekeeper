"""omp adapter bridge: the TypeScript extension sends events here as JSON on stdin.

Payload fields: session_id, session_file, cwd, agent_kind, agent_name, parent_id, pid,
tool_name, tool_input, intent, call_id, ok, prompt. The extension is in
adapters/omp/lanekeeper.ts; it has no hooks file, so install.py only prints instructions.
"""

from __future__ import annotations

from lanekeeper.hooks import core
from lanekeeper.hooks.core import Event, Result

AGENT = "omp"


def normalize(kind: str, payload: dict) -> Event:
    return core.event_from_payload(AGENT, kind, payload)


def render(kind: str, result: Result) -> dict | None:
    out: dict = {}
    if result.block:
        out["block"] = True
        out["reason"] = result.block
    if result.context:
        out["additionalContext"] = "\n".join(result.context)
    if result.notices:
        out["notify"] = list(result.notices)
    return out or None
