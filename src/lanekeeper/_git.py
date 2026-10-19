"""Small git runner shared by lanekeeper's hook, ledger, foreign-edit and who code.

Every call is best-effort: failures return None instead of raising, because hooks must
never break an agent. Commands run with --no-optional-locks so background reads do not
take index locks.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_EXTRA: dict = {"creationflags": 0x08000000} if os.name == "nt" else {}


def run(cwd: str | os.PathLike[str], *args: str, timeout: float = 8.0) -> subprocess.CompletedProcess[bytes] | None:
    """Run `git -C cwd <args>`; None when git is missing, times out, or cwd is unusable."""
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", os.fspath(cwd), *args],
            capture_output=True,
            timeout=timeout,
            **_EXTRA,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def out(cwd: str | os.PathLike[str], *args: str, timeout: float = 8.0, strip: bool = True) -> str | None:
    """Stdout of a successful git call, or None on failure."""
    result = run(cwd, *args, timeout=timeout)
    if result is None or result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    return text.strip() if strip else text


def common_dir(repo: str | os.PathLike[str]) -> Path | None:
    """Absolute git common dir: the main repo's .git, for both main checkouts and worktrees."""
    text = out(repo, "rev-parse", "--git-common-dir")
    if text is None:
        fallback = Path(repo) / ".git"
        return fallback if fallback.exists() else None
    path = Path(text)
    return path if path.is_absolute() else Path(repo) / path
