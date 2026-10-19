"""Land one lane (a git worktree on its own branch) onto the target branch, one at a time.

Run from a lane worktree, never from the main checkout:

    lanekeeper merge <worktree> [--check "<command>"] [--gate "<command>"] [--push]

Steps:
  1. Refuse a dirty worktree, the main checkout, the target branch, or a protected branch.
  2. Outside the lock (other sessions never wait for these): fetch origin, rebase onto
     origin/<target>. Conflicts abort the rebase and write a resolution brief (exit 2). Then run
     the check command (exit 3 on failure). With --push, run the gate command once (exit 4).
  3. Take the repository merge lock (<git-common-dir>/merge-lane.lock). Rebase again. Re-run the
     check only if the target moved since step 2. Run the overlap check (exit 2 blocks).
  4. Squash the branch into one commit (default), then push with `git push origin <sha>:<target>`
     (never forced). Without --push, print the command that would push. Up to 3 attempts.

Train mode (`--train "branch-a branch-b"`) assembles several branches on a temporary worktree,
validates them once, and pushes once. Branches that conflict or fail are dropped (exit 6 when any
were dropped). If the combination fails, each branch is tested alone to find the culprit.

Exit codes: 0 done (pushed with --push, otherwise ready), 1 usage error, 2 conflict or blocked,
3 check failed, 4 gate failed, 6 a train branch was dropped.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from . import check as pushcheck, config, gitutil, proc
from .gitutil import GitError, split_nul

LOCK_NAME = "merge-lane.lock"
STALE_LOCK_SECONDS = 30 * 60
LOCK_POLL_SECONDS = 2
MAX_PUSH_ATTEMPTS = 3
MAX_REBASE_STEPS = 50
CONFLICT, CHECK_FAILED, GATE_FAILED, DROPPED = 2, 3, 4, 6

PRIMARY_TYPES = re.compile(r"^(feat|fix|perf|refactor)(\(|!|:)")
NOISE_SUBJECT = re.compile(r"^(Revert |fixup! |squash! |amend! )")
REVERT_SUBJECT = re.compile(r'^Revert "(.*)"$')
OPENSPEC_CHANGE = re.compile(r"^openspec/changes/(?:archive/\d{4}-\d{2}-\d{2}-)?([^/]+)/")
RERERE_RESOLVED = re.compile(r"(?:Resolved|Staged) '([^']+)' using previous resolution")

RESOLVE_GUIDE = [
    "## How to resolve",
    "1. In this worktree run `git rebase {upstream}` and open each conflicted file.",
    "2. MECHANICAL conflicts (both sides add different lines, keys or imports, or differ only in whitespace "
    "or order): keep BOTH, then `git add <file>`.",
    "3. SEMANTIC conflicts (the same logic line changed on both sides, one side deletes what the other edits, "
    "changed function signatures, schema or migrations, config, lockfiles, secrets): STOP. Run `git rebase --abort` "
    "and report the file and the two intents above to the user. Do not pick a side yourself.",
    "4. `git -c core.editor=true rebase --continue` until done, then rerun the merge. rerere remembers the resolution.",
    "5. If the check fails after resolving: `git rebase --abort` if the rebase is unfinished, and do not push.",
]


class MergeError(RuntimeError):
    """The target branch on origin cannot be trusted (missing, or out of sync with the tracking ref)."""


@dataclass(frozen=True)
class _Options:
    cfg: config.Config
    push: bool
    wait: float
    ready: str  # the command printed when the lane is ready but not pushed


def run(
    worktree: str | os.PathLike[str],
    *,
    target: str | None = None,
    check: str | None = None,
    gate: str | None = None,
    squash: bool | None = None,
    push: bool = False,
    wait: float = 900,
    train: str | None = None,
) -> int:
    """Land the lane at `worktree` (or assemble the `train` branches). `None` values come from .lanekeeper.toml."""
    path = os.path.abspath(os.fspath(worktree))
    try:
        common_dir = _abs(path, _out(path, "rev-parse", "--git-common-dir"))
        git_dir = _abs(path, _out(path, "rev-parse", "--git-dir"))
    except GitError:
        print(f"[lanekeeper] Not a git worktree: {path}", file=sys.stderr)
        return 1
    cfg = config.with_overrides(
        config.load(_out(path, "rev-parse", "--show-toplevel")),
        target=target,
        check=check,
        gate=gate,
        squash=squash,
    )
    opts = _Options(
        cfg=cfg,
        push=push,
        wait=wait,
        ready=_ready_command(path, _given_flags(target, check, gate, squash, train)),
    )
    if train is not None:
        branches = train.split()
        if not branches:
            print("[lanekeeper] --train needs at least one branch name.", file=sys.stderr)
            return 1
        return _run_train(path, common_dir, opts, branches)

    if git_dir == common_dir:
        print("[lanekeeper] This is the main checkout. Run merge from a lane worktree.", file=sys.stderr)
        return 1
    branch = _out(path, "branch", "--show-current")
    if not branch or branch == cfg.target or branch in cfg.protected:
        print(
            f"[lanekeeper] Refusing to merge from '{branch or 'detached HEAD'}': "
            "work on a feature branch in a lane worktree.",
            file=sys.stderr,
        )
        return 1
    if _out(path, "status", "--porcelain"):
        print("[lanekeeper] Uncommitted changes remain. Commit them and rerun.", file=sys.stderr)
        return 1
    code, validated = _pre_gates(path, branch, common_dir, opts)
    if code:
        return code
    return _land(path, branch, common_dir, opts, validated, squash=cfg.squash)


# ---------------------------------------------------------------- git helpers

def _out(cwd: str, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    """Stripped stdout. Raises GitError on a non-zero exit outside `ok`."""
    return gitutil.git(cwd, *args, ok=ok).stdout.strip()


def _probe(cwd: str, *args: str) -> str:
    """Stripped stdout of a command whose failure is expected or harmless; '' on failure."""
    try:
        return gitutil.git(cwd, *args).stdout.strip()
    except GitError:
        return ""


def _abs(base: str, relative: str) -> str:
    return os.path.abspath(os.path.join(base, relative))


def _changed_files(worktree: str, target: str) -> list[str]:
    return split_nul(gitutil.git(worktree, "diff", "-z", "--name-only", f"origin/{target}...HEAD").stdout)


def _unmerged(worktree: str) -> list[str]:
    return split_nul(gitutil.git(worktree, "diff", "-z", "--name-only", "--diff-filter=U").stdout)


def _rebase_env() -> dict[str, str]:
    """English git messages, so the rerere lines can be recognised."""
    return {**os.environ, "LC_ALL": "C"}


# ---------------------------------------------------------------- rebase

def _rebase_onto(worktree: str, target: str, common_dir: str, branch: str) -> tuple[list[str], str | None, list[str]]:
    """Fetch origin/<target> and rebase onto it. Returns (conflicting files, brief path or None, rerere replays)."""
    gitutil.git(worktree, "fetch", "origin", f"+refs/heads/{target}:refs/remotes/origin/{target}")
    listed = _out(worktree, "ls-remote", "origin", f"refs/heads/{target}").split()
    if not listed:
        raise MergeError(f"origin has no branch '{target}'")
    remote_sha = listed[0]
    tracking_sha = _out(worktree, "rev-parse", f"refs/remotes/origin/{target}")
    if remote_sha != tracking_sha:
        raise MergeError(
            f"origin/{target} is out of sync (remote {remote_sha[:8]} != tracking {tracking_sha[:8]})"
        )
    conflicts, replayed = _rebase_with_rerere(worktree, f"origin/{target}")
    if not conflicts:
        return [], None, replayed
    brief: str | None = None
    try:
        brief = _write_brief(worktree, target, common_dir, branch, conflicts)
    except (OSError, GitError) as exc:  # the brief is a convenience; it must not block the abort
        print(f"[lanekeeper] Could not write the resolution brief: {exc}", file=sys.stderr)
    _probe(worktree, "rebase", "--abort")
    return conflicts, brief, replayed


def _rebase_with_rerere(worktree: str, upstream: str) -> tuple[list[str], list[str]]:
    """Rebase HEAD onto `upstream`, continuing past conflicts that rerere already resolved.

    Returns (conflicting files, rerere replays). A non-empty conflict list leaves the rebase in progress;
    the caller must abort it.
    """
    result = gitutil.git(worktree, "-c", "core.editor=true", "rebase", upstream, ok=(0, 1), env=_rebase_env())
    replayed: list[str] = []
    for _ in range(MAX_REBASE_STEPS):
        if result.returncode == 0:
            return [], replayed
        replayed += RERERE_RESOLVED.findall(result.stdout + result.stderr)
        if _unmerged(worktree) or not os.path.isdir(os.path.join(_git_dir(worktree), "rebase-merge")):
            break
        result = gitutil.git(
            worktree, "-c", "core.editor=true", "rebase", "--continue", ok=(0, 1), env=_rebase_env()
        )
    conflicts = _unmerged(worktree)
    return conflicts or ["(could not determine the file; see the git rebase output)"], replayed


def _git_dir(worktree: str) -> str:
    return _out(worktree, "rev-parse", "--absolute-git-dir")


def _rebase_or_report(worktree: str, target: str, common_dir: str, branch: str) -> int:
    """0 when rebased; CONFLICT (after printing the files and the brief) when not."""
    conflicts, brief, replayed = _rebase_onto(worktree, target, common_dir, branch)
    if replayed:
        print(f"[lanekeeper] rerere replayed earlier resolutions: {', '.join(sorted(set(replayed)))} (the check verifies them).")
    if not conflicts:
        return 0
    print(f"[lanekeeper] Conflicts while rebasing onto origin/{target} (aborted, branch unchanged):")
    for name in conflicts:
        print(f"  - {name}")
    if brief:
        print(f"[lanekeeper] Resolution brief (conflict hunks, both commits, intents): {brief}")
    return CONFLICT


# ---------------------------------------------------------------- resolution brief

def _write_brief(worktree: str, target: str, common_dir: str, branch: str, conflicts: list[str]) -> str:
    upstream = f"origin/{target}"
    merge_base = _out(worktree, "merge-base", upstream, f"refs/heads/{branch}")
    applying = _probe(worktree, "log", "-1", "--format=%h %s", "REBASE_HEAD")
    lines = [
        f"# Conflict while rebasing `{branch}` onto `{upstream}`",
        "",
        f"Commit being applied: `{applying or '?'}`. Merge-base: `{merge_base[:8]}`.",
        "",
        "**Reading the markers during a REBASE:** the `<<<<<<<` block (ours/HEAD) is the version already merged on "
        "origin (someone else's work); the `>>>>>>>` block (theirs) is a commit from YOUR branch. `|||||||` is the text "
        "before both sides edited.",
        "",
    ]
    for name in conflicts:
        others = _probe(worktree, "log", "--format=- %h %an %ad %s", "--date=short", f"{merge_base}..{upstream}", "--", name)
        mine = _probe(worktree, "log", "--format=- %h %ad %s", "--date=short", f"{merge_base}..refs/heads/{branch}", "--", name)
        lines += [
            f"## {name}",
            "",
            f"Already merged on {upstream} (others):",
            others or "- (none)",
            "",
            "Your branch:",
            mine or "- (none)",
            "",
        ]
        intents = _ledger_intents(name)
        if intents:
            lines += ["Recent intents of sessions that edited this file (activity ledger):"] + [f"- {x}" for x in intents] + [""]
        lines += ["```", *_marked_excerpt(os.path.join(worktree, name)), "```", ""]
    lines += [guide.format(upstream=upstream) for guide in RESOLVE_GUIDE]

    folder = os.path.join(common_dir, "merge-lane")
    os.makedirs(folder, exist_ok=True)
    safe_branch = re.sub(r"[^A-Za-z0-9._-]", "_", branch)
    path = os.path.join(folder, f"conflict-{safe_branch}-{time.strftime('%Y%m%d-%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


def _marked_excerpt(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            file_lines = handle.read().split("\n")
    except OSError:
        return ["(file could not be read: binary, or deleted on one side)"]
    shown: list[str] = []
    inside = False
    for number, line in enumerate(file_lines, 1):
        if line.startswith("<<<<<<<"):
            inside = True
        if inside and len(shown) < 120:
            shown.append(f"{number:5}| {line[:160]}")
        if line.startswith(">>>>>>>"):
            inside = False
            shown.append("     | ...")
    return shown or ["(no conflict markers found in the file)"]


def _ledger_intents(name: str) -> list[str]:
    """Sessions that edited this file in the last 3 days and their recorded intent. Empty when no ledger exists."""
    try:
        from . import ledger

        db = ledger.db_path().resolve()
        if not db.exists():
            return []
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
    except (ImportError, OSError, sqlite3.Error):
        return []
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3 * 86400))
    try:
        rows = con.execute(
            """SELECT substr(session_id,-8), max(ts), branch,
                 (SELECT intent FROM events x WHERE x.session_id=e.session_id AND x.path_key=e.path_key
                    AND x.intent IS NOT NULL ORDER BY x.id DESC LIMIT 1)
               FROM events e WHERE kind IN ('write','edit') AND lower(path) LIKE ? AND ts>=?
               GROUP BY session_id, path_key ORDER BY max(ts) DESC LIMIT 6""",
            ("%/" + name.lower(), since),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [f"...{sid} [{br or '?'}] {ts[5:16]}: {intent or '(intent not recorded)'}" for sid, ts, br, intent in rows]


# ---------------------------------------------------------------- checks

def _shell(worktree: str, command: str, env: dict[str, str] | None = None) -> bool:
    """Run `command` through the OS shell in the worktree. True when it exits 0."""
    return subprocess.run(command, shell=True, cwd=worktree, env=env).returncode == 0


def _check_passes(worktree: str, command: str) -> bool:
    print(f"[lanekeeper] Running check: {command}")
    if _shell(worktree, command):
        return True
    print("[lanekeeper] Check FAILED, not pushing.", file=sys.stderr)
    return False


def _gate_passes(worktree: str, branch: str, target: str, head: str, command: str) -> bool:
    """The gate sees where the branch is: LANEKEEPER_BRANCH/BASE/HEAD and the changed files (newline-separated)."""
    env = {
        **os.environ,
        "LANEKEEPER_BRANCH": branch,
        "LANEKEEPER_BASE": f"origin/{target}",
        "LANEKEEPER_HEAD": head,
        "LANEKEEPER_CHANGED": "\n".join(_changed_files(worktree, target)),
    }
    print(f"[lanekeeper] Running gate: {command}")
    if _shell(worktree, command, env=env):
        return True
    print("[lanekeeper] Gate FAILED, not pushing.", file=sys.stderr)
    return False


def _pre_gates(worktree: str, branch: str, common_dir: str, opts: _Options) -> tuple[int, str]:
    """Everything that runs OUTSIDE the lock: rebase, check, and gate (--push only).

    Returns (exit code, the HEAD these checks validated). Exit 0 means the branch may proceed to the lock.
    """
    cfg = opts.cfg
    code = _rebase_or_report(worktree, cfg.target, common_dir, branch)
    if code:
        return code, ""
    if cfg.check and not _check_passes(worktree, cfg.check):
        return CHECK_FAILED, ""
    validated = _out(worktree, "rev-parse", "HEAD")
    if opts.push and cfg.gate and not _gate_passes(worktree, branch, cfg.target, validated, cfg.gate):
        return GATE_FAILED, ""
    return 0, validated


# ---------------------------------------------------------------- squash

def _squash(worktree: str, base: str, label: str) -> tuple[str, str | None]:
    """Squash base..HEAD into one commit on `base`, keeping HEAD's tree (the tree that was checked).

    Title: first feat/fix/perf/refactor subject, else the first subject that is not a revert or fixup, plus
    the OpenSpec change names. Body: the original subjects. Returns (sha, title); (HEAD, None) for one commit.
    """
    head = _out(worktree, "rev-parse", "HEAD")
    subjects = _out(worktree, "log", "--reverse", "--format=%s", f"{base}..HEAD").splitlines()
    if len(subjects) <= 1:
        return head, None
    reverted = {m.group(1) for s in subjects if (m := REVERT_SUBJECT.match(s))}
    kept = [s for s in subjects if s not in reverted and not NOISE_SUBJECT.match(s)] or subjects
    title = next((s for s in kept if PRIMARY_TYPES.match(s)), kept[0])
    changed = split_nul(gitutil.git(worktree, "diff", "-z", "--name-only", f"{base}...HEAD").stdout)
    changes = sorted({m.group(1) for f in changed if (m := OPENSPEC_CHANGE.match(f)) and m.group(1) != "archive"})
    missing = [name for name in changes if name not in title]
    if missing:
        title += f" (openspec: {', '.join(missing)})"
    body = f"Squashed {len(subjects)} commits from {label}:\n" + "\n".join(f"- {s}" for s in subjects)
    sha = _out(worktree, "commit-tree", "HEAD^{tree}", "-p", base, "-m", title, "-m", body)
    return sha, title


# ---------------------------------------------------------------- lock and landing

def _acquire_lock(lock_path: str, owner: dict, wait_seconds: float) -> None:
    """Create the lock file exclusively. A lock older than 30 minutes whose holder has died is removed."""
    deadline = time.time() + wait_seconds
    announced = False
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_holder(lock_path)
            age = time.time() - holder.get("since", time.time())
            if age > STALE_LOCK_SECONDS and not proc.pid_alive(holder.get("pid")):
                print(f"[lanekeeper] Stale lock from {holder.get('branch')} ({int(age)}s, process gone): removing it.")
                with contextlib.suppress(FileNotFoundError):
                    os.remove(lock_path)
                continue
            if not announced:
                print(f"[lanekeeper] Waiting for the merge lock held by {holder.get('branch')} (pid {holder.get('pid')})...")
                announced = True
            if time.time() > deadline:
                raise MergeError("timed out waiting for the merge lock")
            time.sleep(LOCK_POLL_SECONDS)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(owner, handle)
        return


def _read_holder(lock_path: str) -> dict:
    try:
        with open(lock_path, encoding="utf-8") as handle:
            holder = json.load(handle)
    except (OSError, ValueError):
        return {}
    return holder if isinstance(holder, dict) else {}


def _land(worktree: str, branch: str, common_dir: str, opts: _Options, validated: str, *, squash: bool) -> int:
    """Take the lock, rebase again, re-validate if the target moved, check overlaps, then squash and push."""
    target = opts.cfg.target
    lock_path = os.path.join(common_dir, LOCK_NAME)
    _acquire_lock(
        lock_path,
        {"pid": os.getpid(), "branch": branch, "worktree": worktree, "since": time.time()},
        opts.wait,
    )
    try:
        for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
            code = _rebase_or_report(worktree, target, common_dir, branch)
            if code:
                return code
            head = _out(worktree, "rev-parse", "HEAD")
            if head == validated:
                print(f"[lanekeeper] origin/{target} unchanged since the pre-lock checks: not re-running them.")
            else:
                print(f"[lanekeeper] origin/{target} moved while waiting: re-running the check on the new tree.")
                if opts.cfg.check and not _check_passes(worktree, opts.cfg.check):
                    return CHECK_FAILED
                validated = head
            changed = _changed_files(worktree, target)
            if pushcheck.run(worktree, changed, target=target) == pushcheck.BLOCK:
                print("[lanekeeper] Overlap check says DO NOT push (exit 2).", file=sys.stderr)
                return CONFLICT
            sha, title = _squash(worktree, f"origin/{target}", branch) if squash else (head, None)
            if title:
                print(f"[lanekeeper] Squashed {branch} into one commit: {title}")
            if not opts.push:
                print(f"[lanekeeper] READY: {branch} is rebased onto origin/{target}, {len(changed)} file(s). Not pushed.")
                print(f"[lanekeeper] Push with: {opts.ready}")
                return 0
            try:
                gitutil.git(worktree, "push", "origin", f"{sha}:refs/heads/{target}")
            except GitError as exc:
                print(f"[lanekeeper] Push rejected (attempt {attempt}), remote may have moved: {exc}", file=sys.stderr)
                continue
            if sha != head:
                gitutil.git(worktree, "reset", "-q", "--soft", sha)
            print(f"[lanekeeper] Pushed {branch} -> origin/{target}.")
            return 0
        print(f"[lanekeeper] Push failed {MAX_PUSH_ATTEMPTS} times.", file=sys.stderr)
        return CONFLICT
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(lock_path)


# ---------------------------------------------------------------- train

def _assemble(worktree: str, train: str, target: str, branches: list[str], excluded: dict[str, str], squash: bool) -> list[str]:
    """Reset the train branch to origin/<target>, then rebase each branch onto it in turn.

    A branch that conflicts with what is already assembled is recorded in `excluded` and skipped.
    Returns the branches that were assembled.
    """
    gitutil.git(worktree, "checkout", "-q", "-B", train, f"origin/{target}")
    included: list[str] = []
    for name in branches:
        gitutil.git(worktree, "checkout", "-q", "--detach", f"origin/{name}")
        conflicts, replayed = _rebase_with_rerere(worktree, train)
        if conflicts:
            _probe(worktree, "rebase", "--abort")
            gitutil.git(worktree, "checkout", "-q", train)
            excluded[name] = "conflicts with the other branches: " + ", ".join(conflicts)
            continue
        if replayed:
            print(f"[lanekeeper] train: {name}: rerere replayed {', '.join(sorted(set(replayed)))}")
        new_tip = _squash(worktree, train, name)[0] if squash else "HEAD"
        gitutil.git(worktree, "checkout", "-q", "-B", train, new_tip)
        included.append(name)
    return included


def _run_train(repo: str, common_dir: str, opts: _Options, branches: list[str]) -> int:
    target = opts.cfg.target
    refspecs = [f"+refs/heads/{name}:refs/remotes/origin/{name}" for name in branches]
    gitutil.git(repo, "fetch", "origin", f"+refs/heads/{target}:refs/remotes/origin/{target}", *refspecs)
    train = f"ci-train-{uuid.uuid4().hex[:6]}"
    wt = os.path.join(common_dir, "merge-lane", train)
    gitutil.git(repo, "worktree", "add", "-q", "-b", train, wt, f"origin/{target}")
    excluded: dict[str, str] = {}
    try:
        pending = branches
        while pending:
            included = _assemble(wt, train, target, pending, excluded, opts.cfg.squash)
            if not included:
                break
            print(f"[lanekeeper] train: assembled {len(included)} branch(es): {' '.join(included)}")
            code, validated = _pre_gates(wt, train, common_dir, opts)
            if code == 0:
                # Each branch was already squashed inside _assemble, so the train itself is not squashed again.
                code = _land(wt, train, common_dir, opts, validated, squash=False)
                if code:
                    return code
                done = "PUSHED" if opts.push else "READY"
                print(f"[lanekeeper] train: {done} {len(included)} branch(es): {' '.join(included)}")
                for name, why in excluded.items():
                    print(f"[lanekeeper] train: DROPPED {name}: {why}")
                return DROPPED if excluded else 0
            if code not in (CHECK_FAILED, GATE_FAILED):
                return code
            if len(included) == 1:
                excluded[included[0]] = f"failed (exit {code})"
                break
            print(f"[lanekeeper] train: failed together (exit {code}); testing each branch alone.")
            failed: list[str] = []
            for name in included:
                _assemble(wt, train, target, [name], excluded, opts.cfg.squash)
                alone, _ = _pre_gates(wt, train, common_dir, opts)
                if alone:
                    failed.append(name)
                    excluded[name] = f"failed alone (exit {alone})"
            if not failed:
                print(
                    f"[lanekeeper] train: each branch passes alone but they fail together: {' '.join(included)}. "
                    "They interact; merge them by hand.",
                    file=sys.stderr,
                )
                return code
            pending = [name for name in included if name not in failed]
        for name, why in excluded.items():
            print(f"[lanekeeper] train: DROPPED {name}: {why}", file=sys.stderr)
        print("[lanekeeper] train: no branch left to push.", file=sys.stderr)
        return CONFLICT
    finally:
        _probe(repo, "worktree", "remove", "--force", wt)
        _probe(repo, "branch", "-D", train)


# ---------------------------------------------------------------- command line for the printed hint

def _given_flags(
    target: str | None,
    check_cmd: str | None,
    gate_cmd: str | None,
    squash: bool | None,
    train: str | None,
) -> list[str]:
    """Only the flags the user actually typed; config-file values are not repeated in the hint."""
    flags: list[str] = []
    if target is not None:
        flags += ["--target", target]
    if check_cmd is not None:
        flags += ["--check", check_cmd]
    if gate_cmd is not None:
        flags += ["--gate", gate_cmd]
    if squash is False:
        flags.append("--no-squash")
    if train is not None:
        flags += ["--train", train]
    return flags


def _ready_command(path: str, flags: Sequence[str]) -> str:
    argv = ["lanekeeper", "merge", path, *flags, "--push"]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
