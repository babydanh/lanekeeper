"""Pre-push overlap check: would pushing this checkout collide with other lanes or origin?

Read-only apart from `git fetch origin`. `lanekeeper check` runs it on its own, and
`lanekeeper merge` runs it before taking the merge lock.

Verdicts: 0 SAFE, 1 WARNING (read before pushing), 2 DO NOT PUSH. Any unexpected failure
returns 2, so a broken check never lets a push through.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

from . import config, gitutil
from .gitutil import GitError, split_nul

SAFE, WARN, BLOCK = 0, 1, 2
_VERDICT = {
    SAFE: "RESULT: SAFE",
    WARN: "RESULT: WARNING, review before pushing",
    BLOCK: "RESULT: DO NOT PUSH",
}
_FAIL_CLOSED = "RESULT: DO NOT PUSH (fail closed when unsure)"
_COMPARISON_CANDIDATES = (
    "@{u}",
    "refs/remotes/origin/HEAD",
    "refs/remotes/origin/main",
    "refs/remotes/origin/master",
)
_FETCH_TIMEOUT = 120.0


class _Report:
    """Collects notes and the strongest verdict level seen so far."""

    def __init__(self) -> None:
        self.level = SAFE
        self.notes: list[str] = []

    def add(self, level: int, note: str) -> None:
        self.level = max(self.level, level)
        self.notes.append(note)

    def render(self) -> int:
        print("-" * 60)
        if not self.notes:
            print("SAFE: no overlaps found.")
        for note in self.notes:
            print("*", note)
        print(_VERDICT[self.level])
        return self.level


def run(repo: str, files: Sequence[str], target: str | None = None) -> int:
    """Print the overlap report for `repo` and return 0 (safe), 1 (warning) or 2 (do not push).

    `files` are the paths this session edited. `target` is the merge target when the check
    runs inside `lanekeeper merge`; a branch that will be rebased onto that target only gets a
    warning for overlaps, not a block.
    """
    try:
        return _check(repo, list(files), target)
    except config.ConfigError as exc:
        print(f"Cannot read configuration: {exc}")
        print(_FAIL_CLOSED)
        return BLOCK
    except Exception as exc:  # an unexpected failure must close the gate, never open it
        print(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}")
        print(_FAIL_CLOSED)
        return BLOCK


def status_paths(repo: str) -> set[str]:
    """Paths with a pending change in this checkout: modified, staged, untracked or renamed."""
    # raw=True: a leading space in the status column is significant and must survive.
    records = split_nul(_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", raw=True))
    paths: set[str] = set()
    skip_next = False
    for record in records:
        if skip_next:  # the record after a rename/copy is its old path
            skip_next = False
            continue
        paths.add(record[3:])
        if set(record[:2]) & {"R", "C"}:
            skip_next = True
    return paths


def _check(repo: str, files: list[str], target: str | None) -> int:
    mine = {name.replace("\\", "/") for name in files}
    report = _Report()

    try:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    except GitError as exc:
        print(f"Cannot read repository: {exc}")
        print(_FAIL_CLOSED)
        return BLOCK
    top = _toplevel(repo)
    cfg = config.load(top)

    try:
        gitutil.git(repo, "fetch", "-q", "origin", timeout=_FETCH_TIMEOUT)
    except (GitError, subprocess.TimeoutExpired) as exc:
        report.add(BLOCK, f"`git fetch origin` failed: {exc}. Origin state unknown, treating as UNSAFE.")

    # 1. Comparison point: the branch's upstream, else origin's default branch.
    ref = _pick_ref(repo)
    ref_sha = ""
    ahead = behind = 0
    if ref is None:
        report.add(BLOCK, "NO comparison point found (no upstream, origin/HEAD, origin/main or origin/master). Treating as UNSAFE.")
    else:
        ref_sha = _git(repo, "rev-parse", ref)
        try:
            left, right = _git(repo, "rev-list", "--left-right", "--count", f"HEAD...{ref}").split()
            ahead, behind = int(left), int(right)
        except (GitError, ValueError) as exc:
            report.add(BLOCK, f"Cannot compare with `{ref}`: {exc}. Treating as UNSAFE.")
        if ref != "@{u}":
            report.add(WARN, f"Branch `{branch}` has no upstream: compared with `{ref}` instead. The first push will create the upstream.")
    print(f"Branch: {branch} | ahead {ahead} / behind {behind} vs origin")
    if behind:
        report.add(WARN, f"Origin has {behind} new commit(s). Must fetch + rebase before pushing.")

    # 2. Files edited in this checkout that this session did not touch.
    try:
        wip = status_paths(repo)
    except GitError as exc:
        print(f"Cannot read working-tree status: {exc}")
        print(_FAIL_CLOSED)
        return BLOCK
    others = wip - mine if mine else wip
    print(f"Files being edited in this checkout: {len(wip)}")
    if mine and others:
        report.add(WARN, f"{len(others)} edited file(s) NOT in this session (another session?). Only `git add` your own files, never `git add .`.")
        for name in sorted(others)[:8]:
            print(f"   [other session?] {name}")

    # 3. Files this session edited that origin has changed since the comparison point.
    if mine and behind:
        try:
            changed = _git(repo, "diff", "-z", "--name-only", f"HEAD...{ref}", raw=True)
        except GitError as exc:
            report.add(BLOCK, f"Cannot compare files with origin: {exc}. Treating as UNSAFE.")
            changed = ""
        clash = mine & set(split_nul(changed))
        if clash:
            report.add(BLOCK, "LIKELY CONFLICT: files you edited were also changed on origin: " + ", ".join(sorted(clash)))

    # 4. Other worktrees of the same repository.
    _check_worktrees(report, repo, top, mine, ref_sha, target)

    # 5. Pushing a protected branch that triggers a deploy workflow.
    _deploy_warning(report, top, target or branch, cfg.protected)
    return report.render()


def _check_worktrees(
    report: _Report, repo: str, top: str, mine: set[str], base: str, target: str | None
) -> None:
    try:
        listing = _git(repo, "worktree", "list", "--porcelain")
    except GitError as exc:
        report.add(BLOCK, f"Cannot list worktrees: {exc}. Treating as UNSAFE.")
        return
    me = os.path.normcase(os.path.realpath(top))
    for block in listing.split("\n\n"):
        info: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            info[key] = value
        path = info.get("worktree", "")
        if not path or os.path.normcase(os.path.realpath(path)) == me or not mine:
            continue
        label = info.get("branch", "").replace("refs/heads/", "") or "(detached)"
        try:
            edited = status_paths(path)
        except GitError as exc:
            report.add(BLOCK, f"Cannot read status of worktree {path}: {exc}. Treating as UNSAFE.")
            continue
        theirs = set(edited)
        head = info.get("HEAD", "")
        if base and head:
            # Files that worktree has committed on top of the comparison point.
            try:
                merge_base = _git(repo, "merge-base", base, head)
            except GitError:
                merge_base = ""
            if not merge_base:
                report.add(BLOCK, f"No merge-base found with branch `{label}` ({path}). Treating as UNSAFE.")
                continue
            try:
                committed = _git(repo, "diff", "-z", "--name-only", f"{merge_base}..{head}", raw=True)
            except GitError as exc:
                report.add(BLOCK, f"Cannot compare commits of branch `{label}`: {exc}. Treating as UNSAFE.")
                continue
            theirs |= set(split_nul(committed))
        elif head:
            report.add(WARN, f"No upstream to compare commits of branch `{label}`; only its edited files are compared.")
        both = mine & theirs
        overlap_wip = mine & edited
        if overlap_wip:
            # Another session is editing the same file right now: always block.
            report.add(BLOCK, f"Worktree `{label}` ({path}) IS EDITING (uncommitted): " + ", ".join(sorted(overlap_wip)))
        committed_only = both - overlap_wip
        if committed_only:
            # Inside a merge the other branch rebases onto this change on its own turn, so warn only.
            level = WARN if target else BLOCK
            suffix = " (during a merge: that branch will rebase onto this change on its turn)" if target else ""
            report.add(
                level,
                f"Branch/worktree `{label}` ({path}) has committed the same file(s): " + ", ".join(sorted(committed_only)) + suffix,
            )


def _deploy_warning(report: _Report, top: str, push_branch: str, protected: tuple[str, ...]) -> None:
    if push_branch not in protected:
        return
    workflows = os.path.join(top, ".github", "workflows")
    if not os.path.isdir(workflows):
        return
    hits: list[str] = []
    for name in sorted(os.listdir(workflows)):
        try:
            with open(os.path.join(workflows, name), encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        if "push:" in text and push_branch in text and ("deploy" in name.lower() or "docker" in text.lower()):
            hits.append(name)
    if hits:
        report.add(
            WARN,
            f"Push `{push_branch}` TRIGGERS workflow ({', '.join(hits)}) = may DEPLOY to production. "
            "Push only via `lanekeeper merge --push` (check passes), and tell the user right after pushing.",
        )


def _toplevel(repo: str) -> str:
    try:
        return _git(repo, "rev-parse", "--show-toplevel") or repo
    except GitError:
        return repo


def _pick_ref(repo: str) -> str | None:
    for candidate in _COMPARISON_CANDIDATES:
        try:
            if _git(repo, "rev-parse", "--verify", "-q", candidate):
                return candidate
        except GitError:
            continue
    return None


def _git(repo: str, *args: str, raw: bool = False) -> str:
    """Stdout of a git command: stripped unless `raw`. Raises GitError on failure."""
    result = gitutil.git(repo, *args)
    return result.stdout if raw else result.stdout.strip()
