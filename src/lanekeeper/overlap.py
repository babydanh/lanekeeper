"""Predict conflicts between the lanes (worktrees) of one repository, including uncommitted edits.

Touches no worktree, the real index, branches or stashes. It only writes temporary objects into
the git object store, which `git gc` collects.

    lanekeeper overlap <path-in-repo> [--lane <worktree>] [--base origin/main] [--json] [--notify]

How it works: each lane with changes is snapshotted as a temporary commit (HEAD plus its
uncommitted edits, new files included) through a temporary index (GIT_INDEX_FILE). Then
`git merge-tree --write-tree` runs on every pair of lanes that edit a common file, and on each
lane against the base branch. That is the same merge git performs later, so the answer is exact.

  [CONFLICT]     git will report a conflict: file, line, enclosing block, both sides.
  [AUTO-MERGE]   same file, different hunks: git (or a merge driver from .gitattributes) merges it.

--lane limits the scan to pairs that include that lane. --notify queues messages in the activity
ledger for the sessions working in conflicting lanes.

When the budget runs out, the unscanned part is reported as [INCOMPLETE] and is never treated as
clean. Jobs against the base branch always run first, and the rest run least-recently-checked
first; the state lives in <git-common-dir>/overlap-state.json, so no pair is starved.

Exit codes: 0 fully scanned, no conflicts; 1 conflicts found; 2 incomplete (budget used up, or a
lane mid-merge/rebase could not be scanned); 3 error, nothing scanned.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import gitutil, proc
from .gitutil import GitError, split_nul

MAX_SNIPPET_LINES = 6
STATE_FILE = "overlap-state.json"
LANES_FILE = "overlap-lanes.json"
STATE_RETENTION_S = 7 * 86400
NOTICE_WINDOW_MIN = 24 * 60
BUSY_MARKERS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "index.lock")
_MARKER_PREFIXES = ("<<<<<<<", "|||||||", "=======", ">>>>>>>")


def norm(path: str | os.PathLike[str]) -> str:
    return os.fspath(path).replace("\\", "/").rstrip("/")


def _text(cwd: str, *args: str, ok: tuple[int, ...] = (0,), env: dict | None = None, stdin: str | None = None) -> str:
    """Stripped stdout of a git command."""
    return gitutil.git(cwd, *args, ok=ok, env=env, stdin=stdin).stdout.strip()


def worktrees(repo: str) -> list[dict]:
    entries: list[dict] = []
    current: dict = {}
    for line in gitutil.git(repo, "worktree", "list", "--porcelain").stdout.splitlines() + [""]:
        if not line:
            if current.get("path") and not current.get("bare") and not current.get("prunable") and os.path.isdir(current["path"]):
                entries.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = norm(line[9:])
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].removeprefix("refs/heads/")
        elif line in ("bare", "detached") or line.startswith("prunable"):
            current[line.split()[0]] = True
    return entries


def busy(wt: str) -> bool:
    """True while a merge, rebase, cherry-pick or index write is in progress: the lane cannot be snapshotted."""
    git_dir = _text(wt, "rev-parse", "--absolute-git-dir")
    return any(os.path.exists(os.path.join(git_dir, marker)) for marker in BUSY_MARKERS)


def status_entries(wt: str) -> list[tuple[str, str]]:
    """(XY, path) for every changed file in the worktree; XY '??' marks an untracked file."""
    raw = gitutil.git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames").stdout
    return [(entry[:2], entry[3:]) for entry in raw.split("\0") if len(entry) > 3]


def lane_state(wt: dict, base: str) -> dict | None:
    """Lane summary: its merge base with `base`, dirty files, untracked files and every file it owns. None when unscannable."""
    path = wt["path"]
    if busy(path):
        return None
    merge_base = _text(path, "merge-base", base, wt["head"], ok=(0, 1))
    if not merge_base:
        return None
    committed = set(split_nul(gitutil.git(path, "diff", "-z", "--name-only", "--no-renames", merge_base, wt["head"]).stdout))
    entries = status_entries(path)
    dirty = [name for _, name in entries]
    new = [name for xy, name in entries if xy == "??"]
    return {**wt, "mb": merge_base, "dirty": dirty, "new": new, "own": committed | set(dirty)}


def snapshot(lane: dict) -> str:
    """Temporary commit = HEAD plus the lane's uncommitted changes. The real index is never touched."""
    if not lane["dirty"]:
        return lane["head"]
    path = lane["path"]
    real_index = Path(_text(path, "rev-parse", "--git-path", "index"))
    if not real_index.is_absolute():
        real_index = Path(path) / real_index
    # An untracked file can vanish mid-scan (another process's temp file). Leave it out rather than fail the run.
    tracked = set(split_nul(gitutil.git(path, "ls-files", "-z", "--", *lane["dirty"]).stdout))
    paths = [name for name in lane["dirty"] if name in tracked or os.path.lexists(os.path.join(path, name))]
    if not paths:
        return lane["head"]
    fd, tmp = tempfile.mkstemp(prefix="overlap-idx-")
    os.close(fd)
    try:
        Path(tmp).write_bytes(real_index.read_bytes())
        env = {**os.environ, "GIT_INDEX_FILE": tmp}
        gitutil.git(path, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", env=env, stdin="\0".join(paths))
        tree = _text(path, "write-tree", env=env)
        return _text(path, "commit-tree", tree, "-p", lane["head"], "-m", "overlap snapshot")
    finally:
        os.unlink(tmp)


def enclosing(lines: list[str], idx: int) -> str:
    """Nearest opening line above the conflict with less indentation: the function, class or key around it."""
    first = next((line for line in lines[idx + 1:idx + 8] if line.strip() and not line.startswith(_MARKER_PREFIXES)), "")
    indent = len(first) - len(first.lstrip())
    for j in range(idx - 1, -1, -1):
        text = lines[j]
        if text.strip() and len(text) - len(text.lstrip()) < indent and text.rstrip().endswith(("{", "(", ":", "[", "=>")):
            return text.strip()[:100]
    return ""


def _blank_hunk() -> dict:
    return {"line": 0, "symbol": "", "ours": [], "theirs": []}


def conflict_hunks(text: str) -> list[dict]:
    lines = text.split("\n")
    hunks: list[dict] = []
    current: dict | None = None
    part = ""
    for i, line in enumerate(lines):
        if line.startswith("<<<<<<<"):
            current, part = {"line": i + 1, "symbol": enclosing(lines, i), "ours": [], "theirs": []}, "ours"
        elif current is None:
            continue
        elif line.startswith("|||||||"):
            part = "base"
        elif line.startswith("=======") and part in ("ours", "base"):
            part = "theirs"
        elif line.startswith(">>>>>>>"):
            hunks.append(current)
            current = None
        elif part in ("ours", "theirs"):
            current[part].append(line.rstrip()[:140])
    for hunk in hunks:
        hunk["ours"], hunk["theirs"] = hunk["ours"][:MAX_SNIPPET_LINES], hunk["theirs"][:MAX_SNIPPET_LINES]
    return hunks


def hunk_digest(hunks: list[dict]) -> str:
    """Identity of the conflicting content, without line numbers: edits elsewhere in the file do not re-trigger a notice."""
    core = [(hunk["symbol"], hunk["ours"], hunk["theirs"]) for hunk in hunks]
    return hashlib.sha1(json.dumps(core, ensure_ascii=False).encode()).hexdigest()[:12]


def merge_tree(common_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    """`git merge-tree` from a scratch directory. The Mergiraf driver writes temp files into the cwd,
    and inside a worktree that would litter the lane's `git status`."""
    with tempfile.TemporaryDirectory(prefix="overlap-mt-") as scratch:
        return gitutil.git(scratch, f"--git-dir={common_dir}", "merge-tree", *args, ok=(0, 1))


def conflicted_paths(result: subprocess.CompletedProcess[str], keep: set[str]) -> tuple[str, list[str]]:
    """Tree id and the conflicted paths from `merge-tree --write-tree --name-only`, limited to `keep`."""
    lines = result.stdout.split("\n")
    tree = lines[0].strip()
    names: list[str] = []
    if result.returncode == 1:
        for name in lines[1:]:
            if not name:
                break
            if name in keep and name not in names:
                names.append(name)
    return tree, names


def file_conflicts(repo: str, tree: str, names: list[str]) -> list[dict]:
    found = []
    for name in names:
        content = gitutil.git(repo, "cat-file", "-p", f"{tree}:{name}", ok=(0, 128)).stdout
        found.append({"file": name, "hunks": conflict_hunks(content) or [_blank_hunk()]})
    return found


def merge_conflicts(repo: str, common_dir: str, left: str, right: str, only: set[str]) -> list[dict]:
    result = merge_tree(common_dir, "--write-tree", "--name-only", left, right)
    if result.returncode == 0:
        return []
    tree, names = conflicted_paths(result, only)
    return file_conflicts(repo, tree, names)


def label(lane: dict) -> str:
    return f"{Path(lane['path']).name} [{lane.get('branch') or 'detached'}]"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def save_state(path: Path, done: dict[str, float]) -> None:
    """Merge into the newest copy on disk (another scan may have written meanwhile), prune old entries, replace atomically."""
    current = load_state(path)
    current.update(done)
    cutoff = time.time() - STATE_RETENTION_S
    _write_atomic(path, {key: stamp for key, stamp in current.items() if stamp >= cutoff})


def save_lanes(common_dir: str, base: str, lanes: list[dict]) -> None:
    """Write <git-common-dir>/overlap-lanes.json: which files each lane changes, committed or not.

    Read by the activity ledger before an agent edits a file, so it can warn about other lanes.
    files = tracked files whose worktree copy still differs from base (already-merged changes drop out);
    new = untracked files.
    """
    entries = []
    for lane in lanes:
        try:
            differs = set(split_nul(gitutil.git(lane["path"], "diff", "-z", "--name-only", "--no-renames", base).stdout))
        except GitError:
            continue
        entries.append({
            "path": lane["path"],
            "branch": lane.get("branch"),
            "mb": lane["mb"],
            "files": sorted((lane["own"] - set(lane["new"])) & differs),
            "new": sorted(lane["new"]),
        })
    _write_atomic(Path(common_dir) / LANES_FILE, {"ts": time.time(), "base": base, "lanes": entries})


def scan(repo: str, base_ref: str, focus: str | None, budget: float) -> dict:
    """Scan lane-versus-base and lane-versus-lane jobs until `budget` seconds are used up."""
    started = time.monotonic()
    common_dir = _text(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    base = _text(repo, "rev-parse", "--verify", base_ref, ok=(0, 128))
    if not base:
        raise RuntimeError(f"no {base_ref} (run git fetch)")
    focus_key = norm(focus).lower() if focus else None

    lanes: list[dict] = []
    unscanned: list[str] = []
    for wt in worktrees(repo):
        try:
            state = lane_state(wt, base)
        except GitError:
            state = None
        if state is None:  # merge/rebase in progress or git error: not scanned, so never reported as clean
            if not focus_key or norm(wt["path"]).lower() == focus_key:
                unscanned.append(label(wt))
        elif state["own"]:
            lanes.append(state)
    save_lanes(common_dir, base, lanes)

    result: dict = {
        "repo": norm(repo),
        "base": f"{base_ref}@{base[:8]}",
        "lanes": len(lanes),
        "pairs": [],
        "main": [],
        "pending": [],
        "unscanned": unscanned,
        "complete": not unscanned,
    }
    snaps: dict[str, str] = {}
    rebased_cache: dict[str, dict] = {}

    def snap(lane: dict) -> str:
        if lane["path"] not in snaps:
            snaps[lane["path"]] = snapshot(lane)
        return snaps[lane["path"]]

    def rebased(lane: dict) -> dict:
        """The lane as if rebased onto base: its temporary commit, the files it still carries beyond base,
        and its conflicts with base. Comparing pairs in this form means changes already merged into base
        drop out, and base's newer code is not mistaken for the lane's own change."""
        if lane["path"] in rebased_cache:
            return rebased_cache[lane["path"]]
        head = snap(lane)
        if lane["mb"] == base:
            info = {"commit": head, "own": lane["own"], "conflicts": []}
        else:
            merged = merge_tree(common_dir, "--write-tree", "--name-only", base, head)
            tree, bad = conflicted_paths(merged, lane["own"])
            own = set(split_nul(gitutil.git(repo, "diff", "-z", "--name-only", "--no-renames", base, tree).stdout))
            info = {
                "commit": _text(repo, "commit-tree", tree, "-p", base, "-m", "overlap rebased"),
                "own": own,
                "conflicts": file_conflicts(repo, tree, bad),
            }
        rebased_cache[lane["path"]] = info
        return info

    jobs: list[tuple[int, str, str, tuple]] = []  # (priority, key, kind, data)
    for lane in lanes:
        if focus_key and lane["path"].lower() != focus_key:
            continue
        drift = set(split_nul(gitutil.git(repo, "diff", "-z", "--name-only", "--no-renames", lane["mb"], base).stdout))
        shared = lane["own"] & drift
        if shared:
            jobs.append((0, f"main|{lane['path'].lower()}", "main", (lane, shared)))
    for i, a in enumerate(lanes):
        for b in lanes[i + 1:]:
            if focus_key and focus_key not in (a["path"].lower(), b["path"].lower()):
                continue
            shared = a["own"] & b["own"]
            if shared:
                jobs.append((1, f"pair|{a['path'].lower()}|{b['path'].lower()}", "pair", (a, b, shared)))

    state_path = Path(common_dir) / STATE_FILE
    last = load_state(state_path)
    jobs.sort(key=lambda job: (job[0], last.get(job[1], 0)))
    done: dict[str, float] = {}
    for _priority, key, kind, data in jobs:
        if time.monotonic() - started >= budget:
            result["complete"] = False
            result["pending"].append(
                f"{label(data[0])} <-> {result['base']}" if kind == "main" else f"{label(data[0])} <-> {label(data[1])}"
            )
            continue
        if kind == "main":
            lane, _shared = data
            conflicts = rebased(lane)["conflicts"]
            if conflicts:
                result["main"].append({"lane": lane["path"], "label": label(lane), "conflicts": conflicts})
        else:
            a, b, _shared = data
            ra, rb = rebased(a), rebased(b)
            # Files already conflicting with base are reported by the "main" job; comparing them here would use conflict markers.
            skip = {c["file"] for c in ra["conflicts"] + rb["conflicts"]}
            shared = (ra["own"] & rb["own"]) - skip
            conflicts = merge_conflicts(repo, common_dir, ra["commit"], rb["commit"], shared) if shared else []
            bad = {c["file"] for c in conflicts}
            result["pairs"].append({
                "a": a["path"],
                "b": b["path"],
                "a_label": label(a),
                "b_label": label(b),
                "conflicts": conflicts,
                "auto": sorted(shared - bad),
            })
        done[key] = time.time()
    if done:
        save_state(state_path, done)
    return result


def _in(hunk: dict) -> str:
    return f" (in: {hunk['symbol']})" if hunk["symbol"] else ""


def render(result: dict) -> str:
    out = [f"Repo {result['repo']} vs {result['base']}: {result['lanes']} lane(s) with changes of their own."]
    for pair in result["pairs"]:
        for conflict in pair["conflicts"]:
            for hunk in conflict["hunks"]:
                out.append(f"[CONFLICT] {pair['a_label']} <-> {pair['b_label']}: {conflict['file']} line {hunk['line']}{_in(hunk)}")
                out += [f"    {pair['a_label']}: | {line}" for line in hunk["ours"]]
                out += [f"    {pair['b_label']}: | {line}" for line in hunk["theirs"]]
        if pair["auto"]:
            names = pair["auto"]
            more = f" ... +{len(names) - 8}" if len(names) > 8 else ""
            out.append(
                f"[AUTO-MERGE] merges cleanly (same file, different hunks) {pair['a_label']} <-> {pair['b_label']}: "
                + ", ".join(names[:8]) + more
            )
    for entry in result["main"]:
        for conflict in entry["conflicts"]:
            hunk = conflict["hunks"][0]
            out.append(
                f"[MAIN CONFLICT] {entry['label']} will CONFLICT when rebased onto {result['base']}: "
                f"{conflict['file']} line {hunk['line']}{_in(hunk)} [{len(conflict['hunks'])} hunk(s)]"
            )
    for name in result["unscanned"]:
        out.append(
            f"[NOT SCANNED] {name}: merge or rebase in progress (or git error), so it was skipped. "
            "This lane is NOT checked and must not be treated as clean; rerun when it finishes."
        )
    if result["pending"]:
        out.append(
            f"[INCOMPLETE] budget of {result.get('budget', '?')} s used up: {len(result['pending'])} job(s) left; "
            "the next run takes these first: " + "; ".join(result["pending"][:6]) + (" ..." if len(result["pending"]) > 6 else "")
        )
        if not any(p["conflicts"] for p in result["pairs"]) and not result["main"]:
            out.append("   No conflict in the part that was scanned; the unscanned part is NOT considered clean.")
    elif len(out) == 1:
        out.append("No lanes edit the same file.")
    return "\n".join(out)


def _ago(minutes: float) -> str:
    """Local timestamp `minutes` ago, in the format the activity ledger stores."""
    return (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def sessions_on(con: sqlite3.Connection, lane: str) -> list[sqlite3.Row]:
    """Sessions that own a lane: they wrote, edited or ran git in it. Read-only commands run from inside the lane don't count."""
    rows = con.execute(
        """SELECT s.session_id sid, s.pid pid,
             (SELECT intent FROM events x WHERE x.session_id=s.session_id AND x.intent IS NOT NULL ORDER BY x.id DESC LIMIT 1) intent,
             (SELECT detail FROM events x WHERE x.session_id=s.session_id AND x.kind='todo' ORDER BY x.id DESC LIMIT 1) todo
           FROM sessions s
           WHERE s.ended_at IS NULL AND coalesce(s.agent_kind,'')<>'sub' AND (s.heartbeat>=? OR s.last_seen>=?)
             AND EXISTS (SELECT 1 FROM events e WHERE e.session_id=s.session_id AND lower(e.checkout)=? AND e.ts>=?
                         AND e.kind IN ('write','edit','git'))""",
        (_ago(1), _ago(180), lane.lower(), _ago(NOTICE_WINDOW_MIN)),
    ).fetchall()
    return [row for row in rows if proc.pid_alive(row["pid"])]


def _doing(rows: list[sqlite3.Row]) -> str:
    parts = []
    for row in rows:
        bits = []
        if row["todo"]:
            bits.append(f"todo {row['todo'][:120]}")
        if row["intent"]:
            bits.append(f"latest task {row['intent'][:120]}")
        parts.append(f"...{row['sid'][-8:]}: " + "; ".join(bits))
    return " | ".join(parts) or "no open omp session in this lane"


def notify(result: dict) -> int:
    """Queue messages for the sessions working in conflicting lanes. Returns how many were queued."""
    try:
        from . import ledger
    except ImportError as exc:  # overlap still works without the ledger
        print(f"[overlap] --notify skipped: ledger unavailable ({exc})", file=sys.stderr)
        return 0
    path = ledger.db_path()
    if not path.exists():
        return 0
    con = ledger.connect(path)
    con.row_factory = sqlite3.Row
    sent = 0

    def post(to: str, key: str, content: str) -> None:
        nonlocal sent
        duplicate = con.execute(
            "SELECT 1 FROM notices WHERE to_session=? AND rel_key=? AND ts>=?", (to, key, _ago(NOTICE_WINDOW_MIN))
        ).fetchone()
        if duplicate:
            return
        ledger.post_notice(to, "overlap", key, content, con)
        sent += 1

    try:
        for pair in result["pairs"]:
            if not pair["conflicts"]:
                continue
            side = {pair["a"]: sessions_on(con, pair["a"]), pair["b"]: sessions_on(con, pair["b"])}
            for me, other, my_label, other_label, mine, theirs in (
                (pair["a"], pair["b"], pair["a_label"], pair["b_label"], "ours", "theirs"),
                (pair["b"], pair["a"], pair["b_label"], pair["a_label"], "theirs", "ours"),
            ):
                for conflict in pair["conflicts"]:
                    hunk = conflict["hunks"][0]
                    digest = hunk_digest(conflict["hunks"])
                    message = (
                        f"[overlap] CONFLICT with lane {other_label}: {conflict['file']} line {hunk['line']}{_in(hunk)}, "
                        f"{len(conflict['hunks'])} hunk(s). The other side is working on: {_doing(side[other])}.\n"
                        "Your side:\n" + "\n".join(f"  | {line}" for line in hunk[mine])
                        + "\nTheir side:\n" + "\n".join(f"  | {line}" for line in hunk[theirs])
                        + "\nGit will report a conflict here when these branches merge. Keep your own intent, avoid editing "
                        "this region unless you must, and agree with the other session on who keeps which hunk using "
                        "`lanekeeper send`. If the work duplicates, stop and tell the user."
                        f'\nRe-check: lanekeeper overlap "{me}" --lane "{me}"'
                    )
                    for row in side[me]:
                        post(row["sid"], f"overlap:{me}|{other}|{conflict['file']}|{digest}", message)
        for entry in result["main"]:
            for row in sessions_on(con, entry["lane"]):
                for conflict in entry["conflicts"]:
                    hunk = conflict["hunks"][0]
                    digest = hunk_digest(conflict["hunks"])
                    post(
                        row["sid"],
                        f"overlap-main:{entry['lane']}|{conflict['file']}|{digest}",
                        f"[overlap] Lane {entry['label']} will CONFLICT when rebased onto {result['base']}: "
                        f"{conflict['file']} line {hunk['line']}{_in(hunk)}. Someone already merged changes to this region "
                        "into the base branch. Rebase early in your worktree while the conflict is small "
                        "(see: git log --oneline <merge-base>..origin/main -- <file>). Mechanical conflicts: resolve and run "
                        "check. Semantic ones: ask the user.",
                    )
    finally:
        con.close()
    return sent


def run(
    repo_arg: str,
    *,
    lane: str | None = None,
    base: str = "origin/main",
    as_json: bool = False,
    send_notices: bool = False,
    budget: float = 60.0,
) -> int:
    """Print the overlap report and return 0 (clean, complete), 1 (conflicts), 2 (incomplete) or 3 (error)."""
    try:
        repo = norm(_text(repo_arg, "rev-parse", "--show-toplevel"))
        result = scan(repo, base, lane, budget)
    except (RuntimeError, OSError) as exc:  # nothing scanned: must not be reported as clean
        print(f"[overlap] ERROR, nothing scanned: {exc}", file=sys.stderr)
        return 3
    result["budget"] = budget
    if send_notices:
        result["notified"] = notify(result)
    print(json.dumps(result, ensure_ascii=False) if as_json else render(result))
    if any(pair["conflicts"] for pair in result["pairs"]) or result["main"]:
        return 1
    return 0 if result["complete"] else 2
