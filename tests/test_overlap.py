"""Regression tests for `lanekeeper overlap`: conflicting edits vs auto-merged edits, add/add of new
files, changes already squashed into the base, a stale base lane, no writes to the index or any
worktree, correct overlap-lanes.json, exit 3 outside a repo, budget exhaustion and mid-merge lanes.

Real git repositories and a local bare origin; nothing is mocked and --notify is not used.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

from support import commit_all, run_cli, sh, write

TEN_LINES = "".join(f"L{i}\n" for i in range(1, 11))


def set_line(path: str, n: int, text: str) -> None:
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    lines[n - 1] = text
    write(path, "\n".join(lines))


def tree_bytes(root: str) -> dict[str, bytes]:
    """Content of every file in a worktree (excluding .git): catches stray or modified files."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as handle:
                out[os.path.relpath(full, root)] = handle.read()
    return out


class OverlapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name.replace("\\", "/")
        self.t = t
        self.home = f"{t}/home"
        self.origin = f"{t}/origin.git"
        self.main = f"{t}/main"
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh(t, "git", "clone", "-q", self.origin, self.main)
        for key, value in (("user.email", "t@t"), ("user.name", "t"), ("core.autocrlf", "false")):
            sh(self.main, "git", "config", key, value)
        write(f"{self.main}/f.txt", TEN_LINES)
        write(f"{self.main}/g.txt", "g1\n")
        write(f"{self.main}/done.txt", "done\n")
        commit_all(self.main, "base")
        sh(self.main, "git", "push", "-q", "origin", "HEAD:main")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def lane(self, name: str) -> str:
        path = f"{self.t}/{name}"
        sh(self.main, "git", "worktree", "add", "-q", "-b", name, path, "origin/main")
        return path

    def push_main(self, message: str) -> None:
        commit_all(self.main, message)
        sh(self.main, "git", "push", "-q", "origin", "HEAD:main")

    def overlap(self, *args: str, env: dict[str, str] | None = None):
        return run_cli(["overlap", self.main, *args], self.home, base_env=env)

    def pair_with(self, result: dict, name: str) -> dict:
        found = [p for p in result["pairs"] if any(c["file"] == name for c in p["conflicts"])]
        self.assertEqual(len(found), 1, result["pairs"])
        return found[0]

    @staticmethod
    def hunks(pair: dict, name: str) -> list[dict]:
        return [h for c in pair["conflicts"] if c["file"] == name for h in c["hunks"]]

    def state(self, wts: tuple[str, ...]) -> dict:
        snapshot = {
            "refs": sh(self.main, "git", "for-each-ref"),
            "stash": sh(self.main, "git", "stash", "list"),
        }
        for wt in wts:
            snapshot[wt] = {
                "status": sh(wt, "git", "status", "--porcelain=v1", "--untracked-files=all"),
                "index": sh(wt, "git", "ls-files", "--stage"),
                "staged": sh(wt, "git", "diff", "--cached", "--no-ext-diff"),
                "files": tree_bytes(wt),
            }
        return snapshot

    def test_same_line_edited_on_both_sides_reports_conflict_at_that_line(self) -> None:
        a, b = self.lane("lane-a"), self.lane("lane-b")
        set_line(f"{a}/f.txt", 5, "A5")
        commit_all(a, "lane A edits line 5")
        set_line(f"{b}/f.txt", 5, "B5")  # uncommitted
        r = self.overlap("--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        result = json.loads(r.stdout)
        pair = self.pair_with(result, "f.txt")
        self.assertEqual({os.path.basename(pair["a"]), os.path.basename(pair["b"])}, {"lane-a", "lane-b"})
        hunks = self.hunks(pair, "f.txt")
        self.assertEqual([h["line"] for h in hunks], [5])
        shown = " ".join(hunks[0]["ours"] + hunks[0]["theirs"])
        self.assertIn("A5", shown)
        self.assertIn("B5", shown)
        self.assertEqual(pair["auto"], [])
        text = self.overlap()
        self.assertEqual(text.returncode, 1, text.stdout + text.stderr)
        self.assertIn("f.txt line 5", text.stdout)
        self.assertIn("CONFLICT", text.stdout)

    def test_same_file_different_hunks_auto_merges_with_exit_0(self) -> None:
        a, b = self.lane("lane-a"), self.lane("lane-b")
        set_line(f"{a}/f.txt", 1, "A1")
        commit_all(a, "lane A edits line 1")
        set_line(f"{b}/f.txt", 10, "B10")  # uncommitted
        r = self.overlap("--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        result = json.loads(r.stdout)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(result["pairs"][0]["conflicts"], [])
        self.assertEqual(result["pairs"][0]["auto"], ["f.txt"])

    def test_new_file_with_same_name_and_different_content_is_add_add(self) -> None:
        a, b = self.lane("lane-a"), self.lane("lane-b")
        write(f"{a}/n.txt", "from lane A\n")
        write(f"{b}/n.txt", "from lane B\n")
        r = self.overlap("--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        pair = self.pair_with(json.loads(r.stdout), "n.txt")
        hunks = self.hunks(pair, "n.txt")
        self.assertEqual([h["line"] for h in hunks], [1])
        shown = " ".join(hunks[0]["ours"] + hunks[0]["theirs"])
        self.assertIn("from lane A", shown)
        self.assertIn("from lane B", shown)

    def test_new_file_with_same_name_and_same_content_is_not_reported(self) -> None:
        a, b = self.lane("lane-a"), self.lane("lane-b")
        write(f"{a}/n.txt", "same\n")
        write(f"{b}/n.txt", "same\n")
        r = self.overlap("--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        result = json.loads(r.stdout)
        self.assertEqual(result["pairs"][0]["conflicts"], [])
        self.assertEqual(result["pairs"][0]["auto"], ["n.txt"])

    def test_change_already_squashed_into_base_is_not_reported_as_conflict(self) -> None:
        a = self.lane("lane-a")
        set_line(f"{a}/f.txt", 5, "A5")
        commit_all(a, "lane A edits line 5")
        # base receives the same change as a squash: different commit id, identical content
        set_line(f"{self.main}/f.txt", 5, "A5")
        self.push_main("squash: edit line 5")
        # a fresh lane based on the squashed base edits the same region again
        b = self.lane("lane-b")
        set_line(f"{b}/f.txt", 5, "B5")
        commit_all(b, "lane B edits line 5 again")
        r = self.overlap("--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        result = json.loads(r.stdout)
        self.assertTrue(result["complete"])
        self.assertEqual(result["main"], [])
        for pair in result["pairs"]:
            self.assertEqual(pair["conflicts"], [])
            self.assertEqual(pair["auto"], [])

    def test_stale_base_lane_editing_region_base_changed_conflicts_with_base(self) -> None:
        a = self.lane("lane-a")
        set_line(f"{a}/f.txt", 5, "LANE5")
        commit_all(a, "lane edits line 5")
        set_line(f"{self.main}/f.txt", 5, "MAIN5")
        self.push_main("base edits line 5")
        r = self.overlap("--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        result = json.loads(r.stdout)
        self.assertEqual(result["pairs"], [])
        self.assertEqual(len(result["main"]), 1)
        entry = result["main"][0]
        self.assertEqual(os.path.basename(entry["lane"]), "lane-a")
        self.assertEqual([c["file"] for c in entry["conflicts"]], ["f.txt"])
        self.assertEqual([h["line"] for h in entry["conflicts"][0]["hunks"]], [5])
        self.assertIn("CONFLICT", self.overlap().stdout)

    def test_scan_leaves_index_and_worktrees_untouched_and_writes_overlap_lanes(self) -> None:
        a, b = self.lane("lane-a"), self.lane("lane-b")
        write(f"{a}/done.txt", "done-A\n")
        write(f"{a}/mine.txt", "mine\n")
        commit_all(a, "lane A: done + mine")
        set_line(f"{a}/f.txt", 3, "A3")  # uncommitted edit
        write(f"{a}/g.txt", "g2\n")
        sh(a, "git", "add", "g.txt")  # staged
        write(f"{a}/new.txt", "new\n")  # untracked
        set_line(f"{b}/f.txt", 9, "B9")  # uncommitted edit
        write(f"{self.main}/done.txt", "done-A\n")
        self.push_main("squash: done")  # lane A owes this change; content is identical
        wts = (self.main, a, b)
        before = self.state(wts)
        r = self.overlap("--json")
        self.assertEqual(self.state(wts), before)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["main"], [])
        with open(f"{self.main}/.git/overlap-lanes.json", encoding="utf-8") as handle:
            lanes = json.load(handle)["lanes"]
        by_name = {os.path.basename(x["path"]): x for x in lanes}
        self.assertEqual(sorted(by_name), ["lane-a", "lane-b"])
        self.assertEqual(by_name["lane-a"]["files"], ["f.txt", "g.txt", "mine.txt"])
        self.assertEqual(by_name["lane-a"]["new"], ["new.txt"])
        self.assertEqual(by_name["lane-b"]["files"], ["f.txt"])
        self.assertEqual(by_name["lane-b"]["new"], [])

    def test_not_a_repository_exits_3_and_is_not_reported_clean(self) -> None:
        nonrepo = f"{self.t}/not-a-repo"
        os.makedirs(nonrepo)
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": self.t}
        r = run_cli(["overlap", nonrepo], self.home, base_env=env)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("ERROR", r.stderr)

    def test_exhausted_budget_exits_2_and_is_not_reported_clean(self) -> None:
        a, b = self.lane("lane-a"), self.lane("lane-b")
        set_line(f"{a}/f.txt", 1, "A1")
        commit_all(a, "lane A edits line 1")
        set_line(f"{b}/f.txt", 10, "B10")  # uncommitted
        r = self.overlap("--json", "--budget", "0")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        result = json.loads(r.stdout)
        self.assertFalse(result["complete"])
        self.assertTrue(result["pending"])

    def test_lane_in_the_middle_of_a_merge_is_not_reported_clean(self) -> None:
        a, b = self.lane("lane-a"), self.lane("lane-b")
        sh(a, "git", "checkout", "-q", "-b", "side")
        set_line(f"{a}/f.txt", 5, "SIDE5")
        commit_all(a, "side edits line 5")
        sh(a, "git", "checkout", "-q", "lane-a")
        set_line(f"{a}/f.txt", 5, "A5")
        commit_all(a, "lane A edits line 5")
        merge = subprocess.run(["git", "merge", "-q", "side"], cwd=a, capture_output=True, text=True)
        self.assertEqual(merge.returncode, 1, merge.stdout + merge.stderr)  # conflict leaves a merge in progress
        set_line(f"{b}/f.txt", 5, "B5")  # uncommitted
        r = self.overlap("--json")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        result = json.loads(r.stdout)
        self.assertFalse(result["complete"])
        self.assertTrue(any("lane-a" in u for u in result["unscanned"]), result["unscanned"])


if __name__ == "__main__":
    unittest.main()
