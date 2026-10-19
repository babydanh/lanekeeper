"""Guards for agent shell commands and the user's allow phrase.

`destructive_git` lists git commands that can discard uncommitted work, or hide it in a
stash and forget it. Read-only forms (stash list/show, stash apply/pop/branch,
restore --staged, reset --soft, checkout -b, ...) stay allowed.
"""

from __future__ import annotations

import re
import unicodedata

GIT_PREFIX = r"\bgit(?:\s+-[cC]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+"
STOP = r"[^;&|\n]*"

_DESTRUCTIVE: tuple[tuple[str, str], ...] = (
    (
        GIT_PREFIX + r"stash(?:\s*(?:$|[;&|)\n])|\s+(?:push|save|drop|clear)\b|\s+-)",
        "git stash (hides or drops changes; easy to forget)",
    ),
    (
        GIT_PREFIX + "reset" + STOP + "--hard",
        "git reset --hard (discards every uncommitted change)",
    ),
    (
        GIT_PREFIX + "checkout" + STOP + r"(?:\s--(?:\s|$)|\s\.(?:\s|$)|\s-f\b|\s--force\b)",
        "git checkout -- <file> or . (overwrites files with the committed version)",
    ),
    (
        GIT_PREFIX + r"restore\b(?!" + STOP + "--staged)",
        "git restore (overwrites files with the committed version)",
    ),
    (
        GIT_PREFIX + "clean" + STOP + r"\s-[a-zA-Z]*f",
        "git clean -f (deletes untracked files)",
    ),
    (
        GIT_PREFIX + r"worktree\s+remove" + STOP + r"(?:--force|\s-f\b)",
        "git worktree remove --force (deletes a worktree that may hold unfinished files)",
    ),
    (
        GIT_PREFIX + "branch" + STOP + r"\s(?:-D|--delete\s+--force|-d\s+-f)\b",
        "git branch -D (deletes a branch that is not merged)",
    ),
)

_DESTRUCTIVE_RE = tuple((re.compile(pattern), what) for pattern, what in _DESTRUCTIVE)

GIT_RISKY = re.compile(
    r"\bgit(?:\s+-[cC]\s+\S+)*\s+"
    r"(commit|push|rebase|reset|checkout|switch|stash|merge|cherry-pick|clean|restore|worktree|branch)\b"
)

# "cho phép git xoá" / "allow git discard", as the whole message, trailing . or ! allowed.
_DISCARD_PHRASE = re.compile(
    r"\s*(?:cho\s+ph\u00e9p\s+git\s+xo[\u00e1a]|allow\s+git\s+discard)\s*[.!]*\s*",
    re.IGNORECASE,
)


def destructive_git(command: str) -> str | None:
    """Description of the first destructive git form in `command`, or None."""
    for pattern, what in _DESTRUCTIVE_RE:
        if pattern.search(command):
            return what
    return None


def is_discard_phrase(text: str) -> bool:
    """True when the whole prompt is the user's allow phrase."""
    return _DISCARD_PHRASE.fullmatch(unicodedata.normalize("NFC", text)) is not None
