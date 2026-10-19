"""Shared test helpers: isolated state directories, real git repos, hook subprocesses.

Not named test*.py, so unittest discovery does not import it as a test module.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"

# Some Python builds (for example ones with a ._pth file) ignore PYTHONPATH.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_GIT_CONFIG = [
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
    "-c", "user.name=lanekeeper-test",
    "-c", "user.email=test@example.invalid",
    "-c", "commit.gpgsign=false",
    "-c", "init.defaultBranch=main",
]


def tmpdir(test: unittest.TestCase) -> Path:
    """A fresh temp directory, removed when the test ends."""
    path = Path(tempfile.mkdtemp(prefix="lanekeeper-test-"))
    test.addCleanup(shutil.rmtree, path, True)
    return path


def isolate(test: unittest.TestCase) -> Path:
    """Point HOME, USERPROFILE and LANEKEEPER_HOME at a temp tree for this test only.

    Returns the temp base. Also disables the detached overlap scan.
    """
    base = tmpdir(test)
    home = base / "home"
    home.mkdir()
    patcher = mock.patch.dict(
        os.environ,
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LANEKEEPER_HOME": str(base / "lk"),
            "LANEKEEPER_NO_SCAN": "1",
        },
    )
    patcher.start()
    test.addCleanup(patcher.stop)
    os.environ.pop("LANEKEEPER_DB", None)
    return base


def git(cwd: Path | str, *args: str, check: bool = True) -> str:
    """Run git with fixed identity and line-ending settings; return stripped stdout."""
    proc = subprocess.run(
        ["git", *_GIT_CONFIG, *args],
        cwd=os.fspath(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(test: unittest.TestCase, *, with_origin: bool = True) -> tuple[Path, Path | None]:
    """A repo on `main` with one commit a.txt; with a bare `origin` pushed to, if requested."""
    base = tmpdir(test)
    work = base / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "init")
    if not with_origin:
        return work, None
    origin = base / "origin.git"
    git(base, "init", "-q", "--bare", "-b", "main", str(origin))
    git(work, "remote", "add", "origin", origin.as_posix())
    git(work, "push", "-q", "-u", "origin", "main")
    return work, origin


# Hook entry point with SRC placed on sys.path explicitly: some Python builds ignore PYTHONPATH.
_HOOK_BOOT = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from lanekeeper.hooks import main; raise SystemExit(main(sys.argv[2:]))")


def hook_env() -> dict[str, str]:
    """Environment for hook subprocesses: current (isolated) env with scan disabled."""
    env = dict(os.environ)
    env["LANEKEEPER_NO_SCAN"] = "1"
    return env


@dataclass
class HookRun:
    code: int
    stdout: str
    stderr: str

    @property
    def data(self) -> dict | None:
        """Parsed JSON the hook printed, or None when it printed nothing."""
        text = self.stdout.strip()
        return json.loads(text) if text else None


def run_hook(agent: str, kind: str, payload: dict | None = None, *, raw: str | None = None) -> HookRun:
    """Run `python -m lanekeeper.hooks <agent> <kind>` with a JSON payload on stdin."""
    body = raw if raw is not None else json.dumps(payload or {})
    proc = subprocess.run(
        [sys.executable, "-c", _HOOK_BOOT, str(SRC), agent, kind],
        input=body.encode("utf-8"),
        capture_output=True,
        env=hook_env(),
    )
    return HookRun(proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8", "replace"))
