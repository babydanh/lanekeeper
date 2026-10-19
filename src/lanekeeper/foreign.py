"""Foreign-edit warnings: what other worktrees ("lanes") of the same repo are changing in a file.

Reads <git-common-dir>/overlap-lanes.json, written by the overlap scanner, then diffs each
other lane's working tree against its merge base for that file. Read-only; never blocks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from lanekeeper import _git
from lanekeeper.ledger import norm

LANES_MAX_AGE_S = 10 * 60
DIFF_TIMEOUT_S = 5.0
MAX_HUNKS_SHOWN = 6
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@ ?(.*)$", re.M)


def foreign_edits(repo: str, checkout: str, rel: str) -> list[str]:
    """Warnings for lanes other than `checkout` that touch `rel` (repo-relative path).

    Returns an empty list when the snapshot is missing, older than 10 minutes, or unreadable.
    """
    try:
        lanes = _fresh_lanes(repo)
    except (OSError, ValueError):
        return []
    me = norm(checkout).lower()
    want = rel.replace("\\", "/").lower()

    found: list[tuple[int, str]] = []
    jobs: list[tuple[int, str, str, str, str]] = []
    for index, lane in enumerate(lanes):
        worktree = norm(str(lane.get("path") or ""))
        if worktree.lower() == me:
            continue
        name = f"lane {os.path.basename(worktree)} [{lane.get('branch') or '?'}]"
        if any(str(path).lower() == want for path in lane.get("new") or []):
            found.append((index, f"{name} is CREATING this file (not committed yet)"))
            continue
        file = next((str(p) for p in lane.get("files") or [] if str(p).lower() == want), None)
        mb = lane.get("mb")
        if file is not None and isinstance(mb, str) and mb:
            jobs.append((index, name, worktree, mb, file))

    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = [(i, name, pool.submit(_lane_hunks, wt, mb, f)) for i, name, wt, mb, f in jobs]
            for index, name, future in futures:
                detail = future.result()
                if detail:
                    found.append((index, f"{name} {detail}"))

    found.sort(key=lambda item: item[0])
    return [text for _, text in found]


def _fresh_lanes(repo: str) -> list[dict]:
    common = _git.common_dir(repo)
    if common is None:
        return []
    path = common / "overlap-lanes.json"
    if time.time() - path.stat().st_mtime > LANES_MAX_AGE_S:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    lanes = data.get("lanes") if isinstance(data, dict) else None
    return [lane for lane in lanes or [] if isinstance(lane, dict)]


def _lane_hunks(worktree: str, mb: str, file: str) -> str | None:
    """Describe the lines `worktree` changes in `file` relative to `mb`, or None."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", worktree, "diff", "-U0", "--no-color",
             "--no-ext-diff", mb, "--", file],
            capture_output=True,
            timeout=DIFF_TIMEOUT_S,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    diff = result.stdout.decode("utf-8", errors="replace")
    hunks: list[str] = []
    for match in _HUNK.finditer(diff):
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        if count == 0:
            where = f"insert after line {start}"
        elif count == 1:
            where = f"line {start}"
        else:
            where = f"lines {start}-{start + count - 1}"
        context = match.group(3).strip()
        hunks.append(f"{where} (in: {context[:60]})" if context else where)
    if not hunks:
        return None
    shown = "; ".join(hunks[:MAX_HUNKS_SHOWN])
    extra = len(hunks) - MAX_HUNKS_SHOWN
    more = f"; +{extra} more" if extra > 0 else ""
    return f"is editing {shown}{more}"
