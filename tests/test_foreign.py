"""Foreign-edit warnings from another worktree's overlap snapshot."""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from helpers import git, isolate, make_repo

from lanekeeper import foreign
from lanekeeper.foreign import foreign_edits

SOURCE = "function f() {\n  return 1;\n}\n"


class ForeignEditsTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate(self)
        self.repo, _ = make_repo(self, with_origin=False)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.ts").write_text(SOURCE, encoding="utf-8")
        git(self.repo, "add", "src/a.ts")
        git(self.repo, "commit", "-q", "-m", "add source")
        base = self.repo.parent
        self.lane = base / "lane1"
        self.mine = base / "mine"
        git(self.repo, "worktree", "add", "-q", "-b", "feat/a", str(self.lane), "main")
        git(self.repo, "worktree", "add", "-q", "-b", "feat/me", str(self.mine), "main")
        self.mb = git(self.repo, "rev-parse", "main")
        self.lanes_file = self.repo / ".git" / "overlap-lanes.json"

    def write_lanes(self, lanes: list[dict]) -> None:
        self.lanes_file.write_text(json.dumps({"lanes": lanes}), encoding="utf-8")

    def lane1_entry(self, **overrides: object) -> dict:
        entry: dict = {
            "path": self.lane.as_posix(),
            "branch": "feat/a",
            "mb": self.mb,
            "files": ["src/a.ts"],
            "new": [],
        }
        entry.update(overrides)
        return entry

    def edit_lane1(self, text: str) -> None:
        (self.lane / "src" / "a.ts").write_text(text, encoding="utf-8")

    def test_reports_the_hunk_line_and_function_context(self) -> None:
        self.write_lanes([self.lane1_entry()])
        self.edit_lane1("function f() {\n  return 2;\n}\n")
        messages = foreign_edits(str(self.repo), str(self.mine), "src/a.ts")
        self.assertEqual(len(messages), 1)
        self.assertIn("lane lane1 [feat/a]", messages[0])
        self.assertIn("line 2", messages[0])
        self.assertIn("function f()", messages[0])

    def test_new_file_is_reported_as_being_created(self) -> None:
        self.write_lanes([self.lane1_entry(new=["src/new.ts"], files=[])])
        messages = foreign_edits(str(self.repo), str(self.mine), "src/new.ts")
        self.assertEqual(len(messages), 1)
        self.assertIn("CREATING this file", messages[0])

    def test_own_lane_is_excluded(self) -> None:
        self.write_lanes([self.lane1_entry()])
        self.edit_lane1("function f() {\n  return 2;\n}\n")
        self.assertEqual(foreign_edits(str(self.repo), str(self.lane), "src/a.ts"), [])

    def test_file_not_shared_by_any_lane_gives_nothing(self) -> None:
        self.write_lanes([self.lane1_entry()])
        self.edit_lane1("other\n")
        self.assertEqual(foreign_edits(str(self.repo), str(self.mine), "src/other.ts"), [])

    def test_stale_snapshot_is_ignored(self) -> None:
        self.write_lanes([self.lane1_entry()])
        self.edit_lane1("function f() {\n  return 2;\n}\n")
        old = time.time() - 11 * 60
        os.utime(self.lanes_file, (old, old))
        self.assertEqual(foreign_edits(str(self.repo), str(self.mine), "src/a.ts"), [])

    def test_missing_snapshot_gives_nothing(self) -> None:
        self.assertEqual(foreign_edits(str(self.repo), str(self.mine), "src/a.ts"), [])

    def test_diff_timeout_gives_nothing(self) -> None:
        self.write_lanes([self.lane1_entry()])
        self.edit_lane1("function f() {\n  return 2;\n}\n")
        with mock.patch.object(foreign, "DIFF_TIMEOUT_S", 1e-6):
            self.assertEqual(foreign_edits(str(self.repo), str(self.mine), "src/a.ts"), [])

    def test_content_already_merged_into_the_base_gives_nothing(self) -> None:
        self.edit_lane1("function f() {\n  return 2;\n}\n")
        git(self.lane, "commit", "-qam", "change")
        head = git(self.lane, "rev-parse", "HEAD")
        self.write_lanes([self.lane1_entry(mb=head)])
        self.assertEqual(foreign_edits(str(self.repo), str(self.mine), "src/a.ts"), [])


if __name__ == "__main__":
    unittest.main()
