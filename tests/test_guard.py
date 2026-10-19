"""Destructive-git detection and the user's allow phrase."""

from __future__ import annotations

import unicodedata
import unittest

import helpers  # noqa: F401  (puts src/ on sys.path before lanekeeper imports)
from lanekeeper import guard

# Each of these can discard uncommitted work or hide it in a stash.
BLOCKED = (
    "git stash",
    "git stash -u",
    "git stash push -m wip",
    "cd repo && git stash save x && git pull",
    "git -C repo stash drop stash@{0}",
    "git stash clear",
    "git reset --hard origin/main",
    "git -c core.editor=true reset -q --hard",
    "git checkout -- src/a.ts",
    "git checkout .",
    "git checkout -f main",
    "git restore src/a.ts",
    "git restore --source=HEAD .",
    "git clean -fd",
    "git clean -xdf",
    "git worktree remove --force ../lane",
    "git branch -D feat/x",
)

# Read-only or non-destructive forms that must stay allowed.
ALLOWED = (
    "git stash list",
    "git stash show -p stash@{1}",
    "git stash apply stash@{0}",
    "git stash pop",
    "git stash branch rescue/x stash@{0}",
    "git reset --soft HEAD~1",
    "git reset HEAD src/a.ts",
    "git checkout -b feat/moi",
    "git checkout main",
    "git restore --staged src/a.ts",
    "git clean -n",
    "git worktree remove ../lane",
    "git branch -d feat/da-gop",
    "git-wt remove lane -y",
    "git -C repo stash list",
)


class DestructiveGitTests(unittest.TestCase):
    def test_blocks_destructive_forms(self) -> None:
        for command in BLOCKED:
            with self.subTest(command=command):
                self.assertIsNotNone(guard.destructive_git(command))

    def test_allows_read_only_and_safe_forms(self) -> None:
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(guard.destructive_git(command))

    def test_description_names_the_risk(self) -> None:
        self.assertIn("stash", guard.destructive_git("git stash") or "")


class DiscardPhraseTests(unittest.TestCase):
    def test_exact_phrases_grant(self) -> None:
        for phrase in ("allow git discard", "cho phép git xoá", "Allow git discard.", "  cho phép git xoá!  "):
            with self.subTest(phrase=phrase):
                self.assertTrue(guard.is_discard_phrase(phrase))

    def test_decomposed_unicode_grants_too(self) -> None:
        decomposed = unicodedata.normalize("NFD", "cho phép git xoá")
        self.assertTrue(guard.is_discard_phrase(decomposed))

    def test_phrase_inside_a_sentence_does_not_grant(self) -> None:
        for text in (
            "please allow git discard for this repo",
            "allow git discard and push",
            "không cho phép git xoá",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(guard.is_discard_phrase(text))


if __name__ == "__main__":
    unittest.main()
