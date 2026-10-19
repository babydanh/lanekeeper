"""Regression tests for `lanekeeper merge`: the check runs before the lock and again inside it only
when origin/<target> moved; the lane is squashed into one commit; --train assembles branches,
checks once and drops the branches that break.

Real git repositories with a local bare origin; the CLI runs as a subprocess. Nothing is mocked.
Exit codes: 0 landed, 1 usage error, 2 conflict or blocked, 3 check failed, 4 gate failed,
6 a train branch was dropped.
"""

from __future__ import annotations

import glob
import json
import os
import tempfile
import unittest

from support import commit_all, py_command, run_cli, script_command, sh, write

PASS = py_command("pass")
FAIL = py_command("import sys; sys.exit(1)")


class MergeLaneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name.replace("\\", "/")
        self.t = t
        self.home = f"{t}/home"
        self.origin = f"{t}/origin.git"
        self.main = f"{t}/main"
        self.other = f"{t}/other"
        self.lane = f"{t}/main.worktrees/lane"
        self.count = f"{t}/count.txt"
        self.gate_log = f"{t}/gate.log"
        self.lock = f"{self.main}/.git/merge-lane.lock"
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.origin)
        for repo in (self.main, self.other):
            sh(t, "git", "-c", "core.autocrlf=false", "clone", "-q", self.origin, repo)
            sh(repo, "git", "config", "user.email", "t@t")
            sh(repo, "git", "config", "user.name", "t")
            sh(repo, "git", "config", "core.autocrlf", "false")
        write(f"{self.main}/a.txt", "a\n")
        commit_all(self.main, "base")
        sh(self.main, "git", "push", "-q", "origin", "HEAD:main")
        sh(self.other, "git", "pull", "-q", "origin", "main")
        sh(self.main, "git", "worktree", "add", "-q", "-b", "lane", self.lane)
        write(f"{self.lane}/b.txt", "b\n")
        commit_all(self.lane, "lane work")

    def tearDown(self) -> None:
        sh(self.main, "git", "worktree", "remove", "--force", self.lane)
        self.tmp.cleanup()

    def merge(self, *flags: str, worktree: str | None = None):
        return run_cli(["merge", worktree or self.lane, "--wait", "30", *flags], self.home)

    def run_lane(self, check: str, *flags: str):
        return self.merge("--check", check, "--push", *flags)

    def recording_check(self) -> str:
        """Check that logs 'co' when merge holds the lock and 'khong' otherwise."""
        source = (
            "import os\n"
            f"with open({self.count!r}, 'a', encoding='utf-8') as f:\n"
            f"    f.write('co ' if os.path.exists({self.lock!r}) else 'khong ')\n"
        )
        return script_command(self.t, "record.py", source)

    def check_runs(self) -> list[str]:
        if not os.path.exists(self.count):
            return []
        with open(self.count, encoding="utf-8") as f:
            return f.read().split()

    def origin_log(self) -> list[str]:
        sh(self.main, "git", "fetch", "-q", "origin")
        return sh(self.main, "git", "log", "--format=%s", "refs/remotes/origin/main").split("\n")

    def test_idle_main_checks_once_outside_the_lock(self):
        r = self.run_lane(self.recording_check())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.check_runs(), ["khong"], r.stdout)
        self.assertEqual(self.origin_log()[:2], ["lane work", "base"])

    def test_main_moves_during_check_so_the_check_reruns_inside_the_lock(self):
        # The first check run pretends another session landed on main; later runs only record.
        source = "\n".join([
            "import os, subprocess",
            f"count = {self.count!r}",
            f"other = {self.other!r}",
            f"lock = {self.lock!r}",
            "if not os.path.exists(count):",
            "    with open(os.path.join(other, 'c.txt'), 'w', encoding='utf-8', newline='\\n') as f:",
            "        f.write('c\\n')",
            "    for args in (['add', '-A'], ['commit', '-q', '-m', 'other'], ['push', '-q', 'origin', 'HEAD:main']):",
            "        subprocess.run(['git', '-C', other, *args], check=True)",
            "with open(count, 'a', encoding='utf-8') as f:",
            "    f.write('co ' if os.path.exists(lock) else 'khong ')",
            "",
        ])
        r = self.run_lane(script_command(self.t, "moving.py", source))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.check_runs(), ["khong", "co"], r.stdout)
        self.assertEqual(self.origin_log()[:3], ["lane work", "other", "base"])

    def test_failing_check_outside_the_lock_pushes_nothing(self):
        r = self.run_lane(FAIL)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.lock))
        self.assertEqual(self.origin_log()[0], "base")

    def test_many_commits_squash_into_one_titled_after_the_openspec_change(self):
        write(f"{self.lane}/openspec/changes/archive/2026-10-20-them-loc-vdv/proposal.md", "# de xuat\n")
        commit_all(self.lane, "chore(openspec): archive them-loc-vdv")
        write(f"{self.lane}/README.md", "ghi chu\n")
        commit_all(self.lane, "docs: ghi chu loc")
        r = self.run_lane(PASS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # main gains one commit whose title comes from the lane, plus the OpenSpec change name.
        self.assertEqual(self.origin_log()[:2], ["lane work (openspec: them-loc-vdv)", "base"])
        files = sh(self.main, "git", "ls-tree", "-r", "--name-only", "origin/main").split()
        self.assertIn("README.md", files)
        self.assertIn("b.txt", files)
        # The lane branch points at the landed commit, so the ledger does not report it as unmerged.
        self.assertEqual(sh(self.lane, "git", "rev-list", "--count", "origin/main..HEAD").strip(), "0")
        self.assertEqual(sh(self.lane, "git", "status", "--porcelain"), "")

    def test_title_prefers_feat_or_fix_and_skips_reverts(self):
        write(f"{self.lane}/README.md", "x\n")
        commit_all(self.lane, "docs: thu")
        sh(self.lane, "git", "revert", "--no-edit", "HEAD")
        write(f"{self.lane}/b.txt", "b2\n")
        commit_all(self.lane, "fix(b): sua b")
        r = self.run_lane(PASS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[0], "fix(b): sua b")

    def test_without_feat_or_fix_the_revert_is_skipped(self):
        sh(self.main, "git", "worktree", "remove", "--force", self.lane)
        sh(self.main, "git", "branch", "-D", "lane")
        sh(self.main, "git", "worktree", "add", "-q", "-b", "lane", self.lane, "origin/main")
        write(f"{self.lane}/README.md", "x\n")
        commit_all(self.lane, "docs: thu")
        sh(self.lane, "git", "revert", "--no-edit", "HEAD")
        write(f"{self.lane}/.ci.yml", "on: push\n")
        commit_all(self.lane, "ci: them ci")
        r = self.run_lane(PASS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[0], "ci: them ci")

    def test_no_squash_keeps_every_commit(self):
        write(f"{self.lane}/c.txt", "c\n")
        commit_all(self.lane, "docs: them c")
        r = self.merge("--check", PASS, "--push", "--no-squash")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[:3], ["docs: them c", "lane work", "base"])

    def test_red_gate_stops_the_push(self):
        r = self.run_lane(PASS, "--gate", FAIL)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[0], "base")
        self.assertFalse(os.path.exists(self.lock))

    def test_gate_sees_branch_base_head_and_changed_files_and_runs_once(self):
        head = sh(self.lane, "git", "rev-parse", "HEAD").strip()
        source = "\n".join([
            "import json, os",
            f"with open({self.gate_log!r}, 'a', encoding='utf-8') as f:",
            "    env = {k: os.environ.get('LANEKEEPER_' + k, '') for k in ('BRANCH', 'BASE', 'HEAD', 'CHANGED')}",
            "    f.write(json.dumps(env) + '\\n')",
            "",
        ])
        r = self.run_lane(PASS, "--gate", script_command(self.t, "gate.py", source))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.gate_log, encoding="utf-8") as f:
            runs = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(runs, [{"BRANCH": "lane", "BASE": "origin/main", "HEAD": head, "CHANGED": "b.txt"}])

    def test_gate_is_skipped_without_push(self):
        r = self.merge("--check", PASS, "--gate", FAIL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("READY", r.stdout)
        self.assertEqual(self.origin_log()[0], "base")

    def test_check_from_lanekeeper_toml_is_used_when_no_flag_is_given(self):
        write(f"{self.lane}/.lanekeeper.toml", f"check = '{FAIL}'\n")
        commit_all(self.lane, "chore: lanekeeper config")
        r = self.merge("--push")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[0], "base")

    def test_protected_branch_from_config_is_refused(self):
        write(f"{self.lane}/.lanekeeper.toml", 'protected = ["lane"]\n')
        commit_all(self.lane, "chore: protect lane")
        r = self.run_lane(PASS)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[0], "base")

    def test_uncommitted_changes_are_refused(self):
        write(f"{self.lane}/scratch.txt", "x\n")
        r = self.run_lane(PASS)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[0], "base")

    def test_main_checkout_is_refused(self):
        r = self.merge("--check", PASS, "--push", worktree=self.main)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.origin_log()[0], "base")

    def test_rebase_conflict_aborts_and_writes_a_brief(self):
        write(f"{self.lane}/a.txt", "lane\n")
        commit_all(self.lane, "lane edits a")
        write(f"{self.other}/a.txt", "other\n")
        commit_all(self.other, "other edits a")
        sh(self.other, "git", "push", "-q", "origin", "HEAD:main")
        head = sh(self.lane, "git", "rev-parse", "HEAD").strip()
        r = self.run_lane(PASS)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(sh(self.lane, "git", "rev-parse", "HEAD").strip(), head)
        self.assertEqual(sh(self.lane, "git", "status", "--porcelain"), "")
        briefs = glob.glob(os.path.join(self.main, ".git", "merge-lane", "conflict-lane-*.md"))
        self.assertEqual(len(briefs), 1, r.stdout + r.stderr)


class MergeTrainTest(unittest.TestCase):
    """--train: assemble branches on origin/<target>, check once, push once; drop the branches that break."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name.replace("\\", "/")
        self.t = t
        self.home = f"{t}/home"
        self.origin = f"{t}/origin.git"
        self.main = f"{t}/main"
        self.dev = f"{t}/dev"
        self.count = f"{t}/count.txt"
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.origin)
        for repo in (self.main, self.dev):
            sh(t, "git", "-c", "core.autocrlf=false", "clone", "-q", self.origin, repo)
            sh(repo, "git", "config", "user.email", "t@t")
            sh(repo, "git", "config", "user.name", "t")
            sh(repo, "git", "config", "core.autocrlf", "false")
        write(f"{self.dev}/a.txt", "a\n")
        commit_all(self.dev, "base")
        sh(self.dev, "git", "push", "-q", "origin", "HEAD:main")
        # A real repository always has a local main checkout; the train runs from it.
        sh(self.main, "git", "pull", "-q", "origin", "main")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def branch(self, name: str, files: dict[str, str]) -> None:
        """Cloud branch on origin, cut from the base, one file per entry."""
        sh(self.dev, "git", "checkout", "-q", "-B", name, "origin/main")
        for path, text in files.items():
            write(f"{self.dev}/{path}", text)
        commit_all(self.dev, name)
        sh(self.dev, "git", "push", "-q", "origin", f"HEAD:refs/heads/{name}")

    def counted_check(self, failing_when: str) -> str:
        """Check that counts its runs and fails while `failing_when` (Python, evaluated in the worktree) holds."""
        source = (
            "import os, sys\n"
            f"with open({self.count!r}, 'a', encoding='utf-8') as f:\n"
            "    f.write('x ')\n"
            f"sys.exit(1 if ({failing_when}) else 0)\n"
        )
        return script_command(self.t, "counted.py", source)

    def run_train(self, branches: list[str], check: str):
        return run_cli(
            ["merge", self.main, "--train", " ".join(branches), "--check", check, "--push", "--wait", "30"],
            self.home,
        )

    def check_runs(self) -> int:
        if not os.path.exists(self.count):
            return 0
        with open(self.count, encoding="utf-8") as f:
            return len(f.read().split())

    def origin_files(self) -> list[str]:
        sh(self.main, "git", "fetch", "-q", "origin")
        return sorted(sh(self.main, "git", "ls-tree", "--name-only", "origin/main").split())

    def test_three_branches_that_do_not_overlap_check_once_and_push_once(self):
        for i in (1, 2, 3):
            self.branch(f"omp/issue-{i}", {f"f{i}.txt": f"{i}\n"})
        r = self.run_train(["omp/issue-1", "omp/issue-2", "omp/issue-3"], self.counted_check("False"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.check_runs(), 1, r.stdout)
        self.assertEqual(self.origin_files(), ["a.txt", "f1.txt", "f2.txt", "f3.txt"])
        # The temporary worktree and ci-train-* branch are removed; the main checkout is untouched.
        worktrees = sh(self.main, "git", "worktree", "list", "--porcelain")
        self.assertEqual(worktrees.count("worktree "), 1, worktrees)
        self.assertNotIn("ci-train-", worktrees)
        self.assertEqual(sh(self.main, "git", "branch", "--list", "ci-train-*").strip(), "")
        self.assertEqual(sh(self.main, "git", "status", "--porcelain"), "")

    def test_conflicting_branch_is_dropped_and_the_rest_still_land(self):
        self.branch("omp/issue-1", {"shared.txt": "ban 1\n"})
        self.branch("omp/issue-2", {"shared.txt": "ban 2\n"})
        self.branch("omp/issue-3", {"f3.txt": "3\n"})
        r = self.run_train(["omp/issue-1", "omp/issue-2", "omp/issue-3"], self.counted_check("False"))
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn("DROPPED omp/issue-2", r.stdout)
        self.assertEqual(self.origin_files(), ["a.txt", "f3.txt", "shared.txt"])

    def test_red_branch_is_split_off_and_the_green_ones_land(self):
        self.branch("omp/issue-1", {"f1.txt": "1\n"})
        self.branch("omp/issue-2", {"BAD": "x\n"})
        self.branch("omp/issue-3", {"f3.txt": "3\n"})
        r = self.run_train(
            ["omp/issue-1", "omp/issue-2", "omp/issue-3"],
            self.counted_check('os.path.exists("BAD")'),
        )
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn("DROPPED omp/issue-2", r.stdout)
        self.assertEqual(self.origin_files(), ["a.txt", "f1.txt", "f3.txt"])

    def test_red_combination_with_no_single_culprit_pushes_nothing(self):
        self.branch("omp/issue-1", {"x.txt": "1\n"})
        self.branch("omp/issue-2", {"y.txt": "2\n"})
        r = self.run_train(
            ["omp/issue-1", "omp/issue-2"],
            self.counted_check('os.path.exists("x.txt") and os.path.exists("y.txt")'),
        )
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.origin_files(), ["a.txt"])

    def test_every_branch_in_the_train_squashes_into_one_commit(self):
        self.branch("omp/issue-1", {"f1.txt": "1\n"})
        write(f"{self.dev}/f1.md", "ghi chu\n")
        commit_all(self.dev, "docs: ghi chu issue 1")
        sh(self.dev, "git", "push", "-q", "origin", "HEAD:refs/heads/omp/issue-1")
        self.branch("omp/issue-2", {"f2.txt": "2\n"})
        r = self.run_train(["omp/issue-1", "omp/issue-2"], self.counted_check("False"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sh(self.main, "git", "fetch", "-q", "origin")
        log = sh(self.main, "git", "log", "--format=%s", "origin/main").split("\n")
        self.assertEqual(log[:3], ["omp/issue-2", "omp/issue-1", "base"])

    def test_missing_branch_stops_before_anything_is_pushed(self):
        self.branch("omp/issue-1", {"f1.txt": "1\n"})
        r = self.run_train(["omp/issue-1", "omp/missing"], self.counted_check("False"))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.origin_files(), ["a.txt"])
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
