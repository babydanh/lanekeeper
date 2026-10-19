"""Helpers shared by the merge, check and overlap test modules.

Tests build real git repositories in temp directories with a local bare origin and run the
CLI as a real process. Nothing is mocked. LANEKEEPER_HOME points into each test's temp dir so
no test touches a real activity ledger.
"""

from __future__ import annotations

import os
import subprocess
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def sh(cwd: str, *args: str) -> str:
    """Run a command and return its stdout; raise with stderr when it fails."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{args}: {result.stderr}")
    return result.stdout


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def commit_all(repo: str, message: str) -> None:
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", message)


def py_command(code: str) -> str:
    """Shell command that runs `code` with this interpreter. `code` must not contain double quotes."""
    return f'"{sys.executable}" -c "{code}"'


def script_command(directory: str, name: str, source: str) -> str:
    """Write `source` to a Python file and return the shell command that runs it.

    Use this for checks that need quotes, paths or several statements.
    """
    path = os.path.join(directory, name)
    write(path, source)
    return f'"{sys.executable}" "{path}"'


def cli_env(home: str, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env["LANEKEEPER_HOME"] = home
    env["LANEKEEPER_DB"] = os.path.join(home, "activity.db")
    return env


def run_cli(args: list[str], home: str, *, cwd: str | None = None, base_env: dict[str, str] | None = None):
    """Run `python -m lanekeeper <args>` as a real process and return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "lanekeeper", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=cli_env(home, base_env),
    )
