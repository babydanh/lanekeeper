"""Shared git helper: every lanekeeper merge/check/overlap path runs git through `git()`.

Output is decoded as UTF-8 with replacement, so non-ASCII file names and messages never
crash the caller on any platform. A command whose exit code is not in `ok` raises
GitError; callers that expect a non-zero exit list it in `ok`.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence


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
