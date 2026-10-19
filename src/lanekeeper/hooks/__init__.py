"""Hook entry point: `lanekeeper hook <agent> <event>` or `python -m lanekeeper.hooks <agent> <event>`.

Reads the agent's hook payload (JSON) on stdin, runs the shared logic, and prints the
agent-specific JSON decision on stdout. Exit code is 0 for every well-formed call, including
internal failures (fail-open); 1 only for a malformed command line.
"""

from __future__ import annotations

import json
import sys

from lanekeeper import ledger
from lanekeeper.hooks import claude, codex, cursor, gemini, omp
from lanekeeper.hooks.core import KINDS, handle

AGENTS = {module.AGENT: module for module in (claude, codex, gemini, cursor, omp)}


def _read_payload() -> dict:
    stream = sys.stdin
    if stream is None:
        return {}
    raw = stream.buffer.read() if hasattr(stream, "buffer") else stream.read().encode("utf-8")
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        ledger.log_error("hook payload", exc)
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str]) -> int:
    args = list(argv)
    if len(args) != 2 or args[0] not in AGENTS or args[1] not in KINDS:
        sys.stderr.write(
            "usage: lanekeeper hook <" + "|".join(AGENTS) + "> <" + "|".join(KINDS) + ">\n"
        )
        return 1
    agent, kind = args
    module = AGENTS[agent]
    try:
        event = module.normalize(kind, _read_payload())
        decision = module.render(kind, handle(event))
    except Exception as exc:  # noqa: BLE001 - a hook must never break the agent
        ledger.log_error(f"hook {agent} {kind}", exc)
        return 0
    if decision:
        sys.stdout.write(json.dumps(decision, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    return 0
