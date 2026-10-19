"""Shared git helpers for every lanekeeper module.

Output is decoded as UTF-8 with replacement, so non-ASCII file names and messages never
crash the caller on any platform.

- `git()` is strict: an exit code outside `ok` raises GitError (merge/check/overlap).
- `out()` / `common_dir()` are best-effort: any failure returns None (hooks, ledger, who),
  because a hook must never break the agent it runs inside.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

# Hide the console window of git subprocesses started from hooks on Windows.
_NO_WINDOW: dict = {"creationflags": 0x08000000} if os.name == "nt" else {}


class GitError(RuntimeError):
    """A git command exited with a code outside the caller's `ok` set."""


def git(
    cwd: str | os.PathLike[str],
    *args: str,
    ok: Sequence[int] = (0,),
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `git -C <cwd> <args>` and return the finished process with text stdout/stderr.

    `env` replaces the whole environment when given. `stdin` is sent as UTF-8 text.
    `timeout` (seconds) raises subprocess.TimeoutExpired, which callers treat as a failure.
    """
    result = subprocess.run(
        ["git", "-C", os.fspath(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=None if env is None else dict(env),
        input=stdin,
        timeout=timeout,
    )
    if result.returncode not in ok:
        detail = result.stderr.strip()[:300]
        raise GitError(f"git {' '.join(args[:3])} failed with exit {result.returncode}: {detail}")
    return result


def split_nul(text: str) -> list[str]:
    """Records of a `-z` git output, without the empty tail after the final NUL."""
    return [record for record in text.split("\0") if record]


def out(cwd: str | os.PathLike[str], *args: str, timeout: float = 8.0, strip: bool = True) -> str | None:
    """Best-effort `git --no-optional-locks -C cwd <args>`: stdout, or None on any failure.

    --no-optional-locks keeps background reads from taking the index lock of a worktree
    another agent is using.
    """
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", os.fspath(cwd), *args],
            capture_output=True, timeout=timeout, **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    return text.strip() if strip else text


def common_dir(repo: str | os.PathLike[str]) -> Path | None:
    """Absolute git common dir (the main repo's .git) for a main checkout or a worktree."""
    text = out(repo, "rev-parse", "--git-common-dir")
    if text is None:
        fallback = Path(repo) / ".git"
        return fallback if fallback.exists() else None
    path = Path(text)
    return path if path.is_absolute() else Path(repo) / path
