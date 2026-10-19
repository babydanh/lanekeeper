"""Regression tests for `lanekeeper check`: overlaps between lanes, edits already on origin,
deploy branches, and git failures that must close the gate (exit 2).

Real git repositories and a local bare origin; the CLI runs as a subprocess. Nothing is mocked.
Exit codes: 0 safe, 1 warning, 2 do not push.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

from support import commit_all, run_cli, sh, write

DEPLOY_WORKFLOW = "name: deploy\non:\n  push:\n    branches: [main]\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n"
FEAT_WORKFLOW = "name: deploy\non:\n  push:\n    branches: [feat]\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n"


def setup_identity(repo: str) -> None:
    sh(repo, "git", "config", "user.email", "t@t")
    sh(repo, "git", "config", "user.name", "t")
    sh(repo, "git", "config", "core.autocrlf", "false")
    sh(repo, "git", "config", "core.quotepath", "true")  # git's default: non-ASCII names get quoted


def gate_env() -> dict[str, str]:
    """Environment without the UTF-8 overrides, so stdout follows the locale when piped."""
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    return env


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name.replace("\\", "/")
        self.t = t
        self.home = f"{t}/home"
        self.origin = f"{t}/origin.git"
        self.repo = f"{t}/repo"  # the session's checkout: branch feat, upstream origin/feat
        self.other = f"{t}/other"  # another session that pushes to origin
        self.lane = f"{t}/lane"  # extra worktree, used by the worktree tests
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh(t, "git", "-c", "core.autocrlf=false", "clone", "-q", self.origin, self.repo)
        setup_identity(self.repo)
        write(f"{self.repo}/a.txt", "a\n")
        write(f"{self.repo}/bản.md", "ban\n")
        commit_all(self.repo, "base")
        sh(self.repo, "git", "push", "-q", "origin", "HEAD:main")
        sh(self.repo, "git", "checkout", "-q", "-b", "feat")
        sh(self.repo, "git", "push", "-q", "-u", "origin", "feat")
        sh(t, "git", "-c", "core.autocrlf=false", "clone", "-q", self.origin, self.other)
        setup_identity(self.other)
        sh(self.other, "git", "checkout", "-q", "feat")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_check(self, *args: str, env: dict[str, str] | None = None):
        return run_cli(["check", *args], self.home, base_env=env or gate_env())

    def session_edit(self, name: str = "a.txt", text: str = "session sua\n") -> None:
        write(f"{self.repo}/{name}", text)

    def push_from_other(self, name: str, text: str) -> None:
        write(f"{self.other}/{name}", text)
        commit_all(self.other, f"other session changes {name}")
        sh(self.other, "git", "push", "-q", "origin", "HEAD:feat")

    def add_lane(self) -> str:
        sh(self.repo, "git", "worktree", "add", "-q", "-b", "lane-x", self.lane, "origin/main")
        return self.lane

    # Clean repository and overlaps with other sessions

    def test_clean_repo_with_only_session_files_is_safe(self) -> None:
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SAFE: no overlaps found.", r.stdout)

    def test_edited_file_outside_session_warns_and_is_named(self) -> None:
        self.session_edit()
        write(f"{self.repo}/ghi-chu-khac.md", "other session\n")
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("ghi-chu-khac.md", r.stdout)

    def test_session_file_changed_on_origin_blocks(self) -> None:
        self.push_from_other("a.txt", "other\n")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("CONFLICT", r.stdout)
        self.assertIn("a.txt", r.stdout)

    def test_other_file_changed_on_origin_only_warns(self) -> None:
        self.push_from_other("b.txt", "other\n")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("CONFLICT", r.stdout)
        self.assertIn("Must fetch + rebase", r.stdout)

    # Deploy branches

    def test_main_with_deploy_workflow_warns(self) -> None:
        sh(self.repo, "git", "checkout", "-q", "main")
        write(f"{self.repo}/.github/workflows/deploy.yml", DEPLOY_WORKFLOW)
        commit_all(self.repo, "add deploy workflow")
        sh(self.repo, "git", "push", "-q", "origin", "HEAD")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("TRIGGERS", r.stdout)
        self.assertIn("deploy.yml", r.stdout)

    def test_main_without_deploy_workflow_is_safe(self) -> None:
        sh(self.repo, "git", "checkout", "-q", "main")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_feature_branch_with_deploy_workflow_is_not_a_deploy(self) -> None:
        write(f"{self.repo}/.github/workflows/deploy.yml", DEPLOY_WORKFLOW)
        commit_all(self.repo, "add deploy workflow")
        sh(self.repo, "git", "push", "-q", "origin", "HEAD")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("TRIGGERS", r.stdout)

    def test_target_main_on_feature_branch_still_warns_for_deploy(self) -> None:
        write(f"{self.repo}/.github/workflows/deploy.yml", DEPLOY_WORKFLOW)
        commit_all(self.repo, "add deploy workflow")
        sh(self.repo, "git", "push", "-q", "origin", "HEAD")
        self.session_edit()
        r = self.run_check(self.repo, "--target", "main", "a.txt")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("TRIGGERS", r.stdout)

    def test_protected_branch_from_config_triggers_deploy_warning(self) -> None:
        write(f"{self.repo}/.lanekeeper.toml", 'protected = ["feat"]\n')
        write(f"{self.repo}/.github/workflows/deploy.yml", FEAT_WORKFLOW)
        commit_all(self.repo, "protect feat and add deploy workflow")
        sh(self.repo, "git", "push", "-q", "origin", "HEAD")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("TRIGGERS", r.stdout)

    def test_invalid_config_key_exits_2(self) -> None:
        write(f"{self.repo}/.lanekeeper.toml", 'chek = "npm test"\n')
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("Cannot read configuration", r.stdout)
        self.assertIn("RESULT: DO NOT PUSH", r.stdout)

    # Git failures close the gate

    def test_directory_that_is_not_a_repo_exits_2(self) -> None:
        plain = f"{self.t}/not-a-repo"
        os.makedirs(plain)
        r = self.run_check(plain, "a.txt")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("Cannot read repository", r.stdout)

    def test_fetch_from_missing_origin_exits_2(self) -> None:
        sh(self.repo, "git", "remote", "set-url", "origin", f"{self.t}/missing.git")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("git fetch origin", r.stdout)

    def test_no_merge_base_exits_2(self) -> None:
        sh(self.repo, "git", "worktree", "add", "-q", "--detach", self.lane)
        sh(self.lane, "git", "checkout", "-q", "--orphan", "lonely")
        write(f"{self.lane}/z.txt", "z\n")
        commit_all(self.lane, "unrelated root")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("No merge-base found", r.stdout)

    def test_missing_git_on_path_exits_2(self) -> None:
        py_dir = os.path.dirname(sys.executable)
        if shutil.which("git", path=py_dir):
            self.skipTest("git lives in the python directory; cannot simulate its absence")
        env = {"PATH": py_dir}
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        self.session_edit()
        r = self.run_check(self.repo, "a.txt", env=env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("RESULT: DO NOT PUSH", r.stdout)

    # Other worktrees

    def test_worktree_editing_same_file_blocks_with_target(self) -> None:
        lane = self.add_lane()
        self.session_edit()  # this checkout edits a.txt
        write(f"{lane}/a.txt", "lane editing\n")  # the other worktree edits the same file
        r = self.run_check(lane, "--target", "main", "a.txt")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("EDITING", r.stdout)
        self.assertIn("a.txt", r.stdout)

    def test_worktree_committed_same_file_only_warns_with_target(self) -> None:
        lane = self.add_lane()
        write(f"{self.repo}/a.txt", "feat change\n")
        commit_all(self.repo, "feat change a.txt")
        write(f"{lane}/a.txt", "lane change\n")
        r = self.run_check(lane, "--target", "main", "a.txt")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("has committed the same file", r.stdout)

    def test_worktree_committed_same_file_blocks_without_target(self) -> None:
        lane = self.add_lane()
        write(f"{self.repo}/a.txt", "feat change\n")
        commit_all(self.repo, "feat change a.txt")
        write(f"{lane}/a.txt", "lane change\n")
        r = self.run_check(lane, "a.txt")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("has committed the same file", r.stdout)

    # Non-ASCII file names

    def test_non_ascii_file_changed_on_origin_blocks(self) -> None:
        self.push_from_other("bản.md", "other\n")
        self.session_edit("bản.md")
        r = self.run_check(self.repo, "bản.md")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("CONFLICT", r.stdout)
        self.assertIn("bản.md", r.stdout)

    def test_non_ascii_file_committed_in_worktree_is_detected(self) -> None:
        lane = self.add_lane()
        write(f"{self.repo}/bản.md", "feat change\n")
        commit_all(self.repo, "feat change bản.md")
        r = self.run_check(lane, "--target", "main", "bản.md")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("has committed the same file", r.stdout)

    def test_non_ascii_name_in_piped_output_does_not_crash(self) -> None:
        write(f"{self.repo}/bản.md", "other session\n")
        self.session_edit()
        r = self.run_check(self.repo, "a.txt")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("bản.md", r.stdout)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
