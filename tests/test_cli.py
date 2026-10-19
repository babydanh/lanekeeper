"""Tests for the command line surface: help, version, usage errors and the fail-closed exit.

The CLI runs as a real subprocess (`python -m lanekeeper`). Nothing is mocked.
"""

from __future__ import annotations

import tempfile
import unittest

from support import commit_all, run_cli, sh, write


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.t = self.tmp.name.replace("\\", "/")
        self.home = f"{self.t}/home"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_help_lists_the_three_own_commands(self) -> None:
        r = run_cli(["--help"], self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for name in ("merge", "check", "overlap"):
            self.assertIn(name, r.stdout)

    def test_version_prints_the_package_version(self) -> None:
        r = run_cli(["--version"], self.home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("lanekeeper 0.1.0", r.stdout)

    def test_no_command_is_a_usage_error(self) -> None:
        r = run_cli([], self.home)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("usage", r.stderr.lower())

    def test_merge_without_a_worktree_is_a_usage_error(self) -> None:
        r = run_cli(["merge"], self.home)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("worktree", r.stderr)

    def test_check_without_a_repo_is_a_usage_error(self) -> None:
        r = run_cli(["check"], self.home)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("repo", r.stderr)

    def test_unknown_command_is_a_usage_error(self) -> None:
        r = run_cli(["frobnicate"], self.home)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_unexpected_error_fails_closed_with_exit_2(self) -> None:
        # An unknown key in .lanekeeper.toml makes merge raise ConfigError before it touches git.
        # That must reach the top-level handler as exit 2, never as a traceback or a clean exit.
        repo = f"{self.t}/repo"
        sh(self.t, "git", "-c", "core.autocrlf=false", "init", "-q", "-b", "main", repo)
        sh(repo, "git", "config", "user.email", "t@t")
        sh(repo, "git", "config", "user.name", "t")
        write(f"{repo}/a.txt", "a\n")
        commit_all(repo, "base")
        write(f"{repo}/.lanekeeper.toml", 'trgt = "main"\n')
        r = run_cli(["merge", repo, "--check", "true"], self.home)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("lanekeeper: error:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
