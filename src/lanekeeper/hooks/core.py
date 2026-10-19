"""Shared hook logic: one normalized Event in, one Result out, independent of the agent.

Every handler is fail-open: an internal error is logged to the ledger and the agent
proceeds. The one exception is a destructive git command, which stays blocked even when
the ledger is unavailable.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sqlite3
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from lanekeeper import config, gitutil, guard, ledger
from lanekeeper.foreign import foreign_edits

KINDS = ("shell", "edit", "post_edit", "prompt", "session_start", "stop", "session_end")
GIT_DISCARD = "git-discard"
SCAN_MIN_INTERVAL_S = 60
SCAN_BUDGET = 20
GREETING_DAYS = 14
GREETING_LIMIT = 30
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class Event:
    """Agent-independent hook input. Built by `event_from_payload` or an agent adapter."""

    agent: str
    kind: str
    session_id: str
    cwd: str
    command: str | None = None
    files: list[str] = field(default_factory=list)
    prompt: str | None = None
    tool: str = ""
    call_id: str | None = None
    ok: bool | None = None
    intent: str | None = None
    agent_kind: str | None = None
    agent_name: str | None = None
    parent_id: str | None = None
    session_file: str | None = None
    pid: int | None = None


@dataclass
class Result:
    """What the hook asks for. `block` denies the tool call; `context` goes to the agent;
    `notices` go to the user."""

    block: str | None = None
    context: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PendingState:
    checkout: str
    branch: str
    dirty: int
    unpushed: int
    not_in_main: int
    has_remote: bool

    @property
    def pending(self) -> bool:
        return self.dirty > 0 or self.unpushed > 0 or self.not_in_main > 0


# ---------------------------------------------------------------- payload normalization

_PATCH_HEADER = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)
_OMP_ANCHOR = re.compile(r"^\[([^\]\n]+?)#[0-9A-Fa-f]{4}\]", re.M)
_OMP_SUFFIX = re.compile(r":(?:\d|raw|img|conflicts).*$")
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_SAFE_WORD = re.compile(r"^[A-Za-z0-9_.:/\\\-@]+$")


def _text(value: object, strip: bool = True) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip() if strip else value
    return text or None


def extract_files(tool_name: str, tool_input: dict, cwd: str) -> list[str]:
    """Absolute, forward-slash paths a tool call writes to, in first-seen order."""
    raw: list[str] = []
    for key in ("file_path", "filePath", "path"):
        if isinstance(tool_input.get(key), str):
            raw.append(tool_input[key])
    if isinstance(tool_input.get("paths"), list):
        raw.extend(item for item in tool_input["paths"] if isinstance(item, str))
    if "patch" in tool_name.lower():
        for value in tool_input.values():
            if isinstance(value, str) and value.lstrip().startswith("*** Begin Patch"):
                raw.extend(_PATCH_HEADER.findall(value))
    if tool_name.lower() == "edit" and isinstance(tool_input.get("input"), str):
        raw.extend(_OMP_ANCHOR.findall(tool_input["input"]))

    files: list[str] = []
    seen: set[str] = set()
    for item in raw:
        path = _OMP_SUFFIX.sub("", item.strip())
        if not path or _URL.match(path):
            continue
        if path.startswith("~"):
            path = os.path.join(os.path.expanduser("~"), path[1:].lstrip("/\\"))
        full = ledger.norm(path if os.path.isabs(path) else os.path.join(cwd, path))
        if full not in seen:
            seen.add(full)
            files.append(full)
    return files


def command_names(command: str) -> str | None:
    """Short summary of a shell command for the ledger: first two words of each segment.

    Arguments and paths beyond that are dropped, so secrets passed on a command line are
    not stored.
    """
    names: list[str] = []
    segments = [s for s in re.split(r"&&|\|\||;|\|", command) if s.strip()][:6]
    for segment in segments:
        words = [w for w in segment.split() if _SAFE_WORD.match(w) and not w.startswith("-")][:2]
        words = [os.path.basename(w.replace("\\", "/")) if "/" in w else w for w in words]
        if words:
            names.append(" ".join(words))
    return ledger.clip(" && ".join(names), 200) if names else None


def event_from_payload(agent: str, kind: str, payload: dict) -> Event:
    """Build an Event from the common field names used across agents and the omp adapter."""
    raw_input = payload.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    tool = _text(payload.get("tool_name")) or ""
    cwd = ledger.norm(_text(payload.get("cwd")) or os.getcwd())
    command = None
    if kind == "shell":
        command = _text(tool_input.get("command")) or _text(payload.get("command"))
    files: list[str] = []
    if kind in ("edit", "post_edit"):
        extras = {key: payload[key] for key in ("file_path", "path") if isinstance(payload.get(key), str)}
        files = extract_files(tool, {**extras, **tool_input}, cwd)
    pid = payload.get("pid")
    ok = payload.get("ok")
    return Event(
        agent=agent,
        kind=kind,
        session_id=_text(payload.get("session_id")) or f"{agent}-{os.getppid()}",
        cwd=cwd,
        command=command,
        files=files,
        prompt=_text(payload.get("prompt"), strip=False),
        tool=tool,
        call_id=_text(payload.get("call_id")) or _text(payload.get("tool_use_id")),
        ok=ok if isinstance(ok, bool) else None,
        intent=ledger.clip(payload.get("intent") or tool_input.get("i"), 160),
        agent_kind=_text(payload.get("agent_kind")),
        agent_name=_text(payload.get("agent_name")),
        parent_id=_text(payload.get("parent_id")),
        session_file=_text(payload.get("session_file")) or _text(payload.get("transcript_path")),
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
    )


# ---------------------------------------------------------------- output shapes

def claude_style(native: str, kind: str, result: Result) -> dict | None:
    """Claude Code / Codex JSON: permissionDecision deny, additionalContext, systemMessage."""
    out: dict = {}
    if result.notices:
        out["systemMessage"] = "\n".join(result.notices)
    if kind in ("shell", "edit") and result.block:
        out["hookSpecificOutput"] = {
            "hookEventName": native,
            "permissionDecision": "deny",
            "permissionDecisionReason": result.block,
        }
    elif result.context:
        out["hookSpecificOutput"] = {
            "hookEventName": native,
            "additionalContext": "\n".join(result.context),
        }
    return out or None


# ---------------------------------------------------------------- scan scheduling

ScanSpawner = Callable[[str], None]


def _spawn_overlap(checkout: str) -> None:
    """Start `lanekeeper overlap` detached, so the hook returns immediately."""
    args = [
        sys.executable, "-m", "lanekeeper", "overlap", checkout,
        "--lane", checkout, "--notify", "--budget", str(SCAN_BUDGET),
    ]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(ledger.home()),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


SCAN_SPAWNER: ScanSpawner = _spawn_overlap


def schedule_scan(checkout: str) -> bool:
    """Start at most one overlap scan per checkout per minute. Returns True when started."""
    if os.environ.get("LANEKEEPER_NO_SCAN") == "1":
        return False

    stamp_dir = ledger.home() / "scan"
    digest = hashlib.sha1(ledger.key_of(checkout).encode("utf-8")).hexdigest()[:16]
    stamp = stamp_dir / f"{digest}.stamp"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < SCAN_MIN_INTERVAL_S:
            return False
        stamp_dir.mkdir(parents=True, exist_ok=True)
        stamp.write_text(ledger.now(), encoding="utf-8")
    except OSError:
        return False
    try:
        SCAN_SPAWNER(checkout)
    except OSError:
        return False
    return True


def _scan_quiet(checkout: str) -> None:
    try:
        schedule_scan(checkout)
    except Exception as exc:  # noqa: BLE001 - a failed scan must not fail the hook
        ledger.log_error("schedule scan", exc)


# ---------------------------------------------------------------- git state

def _count(checkout: str, *args: str) -> int:
    text = gitutil.out(checkout, *args)
    try:
        return int(text) if text else 0
    except ValueError:
        return 0


def _owned_dirty(checkout: str, owned: set[str]) -> int:
    raw = gitutil.out(checkout, "status", "--porcelain", "-z", "--untracked-files=all", strip=False)
    if raw is None:
        return 0
    entries = raw.split("\0")
    count = 0
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC" or entry[1] in "RC":
            index += 1  # rename/copy records carry the old path as the next field
        if ledger.key_of(os.path.join(checkout, entry[3:])) in owned:
            count += 1
    return count


def _target_of(checkout: str) -> str:
    """Branch work lands on, from `.lanekeeper.toml` (default main); a broken config means main."""
    try:
        return config.load(gitutil.out(checkout, "rev-parse", "--show-toplevel") or checkout).target
    except config.ConfigError:
        return "main"


def checkout_state(checkout: str, owned: set[str] | None = None) -> PendingState | None:
    """Git state of one checkout. With `owned`, only files this session edited are counted."""
    if not os.path.isdir(checkout):
        return None
    branch = gitutil.out(checkout, "branch", "--show-current")
    if branch is None:
        return None
    if owned is None:
        status = gitutil.out(checkout, "status", "--porcelain", strip=False) or ""
        dirty = len([line for line in status.splitlines() if line])
    else:
        dirty = _owned_dirty(checkout, owned)
    has_remote = bool((gitutil.out(checkout, "remote") or "").strip())
    unpushed = _count(checkout, "rev-list", "--count", "HEAD", "--not", "--remotes=origin") if has_remote else 0
    target = _target_of(checkout)
    has_target = has_remote and gitutil.out(checkout, "rev-parse", "-q", "--verify", f"origin/{target}") is not None
    not_in_main = (
        _count(checkout, "rev-list", "--count", f"origin/{target}..HEAD")
        if has_target and branch != target
        else 0
    )
    return PendingState(
        checkout=checkout,
        branch=branch or "(detached)",
        dirty=dirty,
        unpushed=unpushed,
        not_in_main=not_in_main,
        has_remote=has_remote,
    )


def describe_pending(state: PendingState) -> str:
    parts = []
    if state.dirty:
        parts.append(f"{state.dirty} uncommitted file(s)")
    if state.unpushed:
        parts.append(f"{state.unpushed} unpushed commit(s)")
    if state.not_in_main:
        parts.append(f"{state.not_in_main} commit(s) not in origin/main")
    if not state.has_remote:
        parts.append("repo has no remote")
    return f"{state.checkout} [{state.branch}]: " + ", ".join(parts)


def in_folder(checkout: str, cwd: str, top: str | None) -> bool:
    """Whether a pending checkout is relevant to a session started in `cwd`."""
    checkout_key = ledger.key_of(checkout).rstrip("/")
    cwd_key = ledger.key_of(cwd).rstrip("/")
    if checkout_key == cwd_key or checkout_key.startswith(cwd_key + "/"):
        return True
    if top and checkout_key == ledger.key_of(top).rstrip("/"):
        return True
    repo_a = ledger.main_root(checkout) or checkout
    repo_b = ledger.main_root(cwd) or cwd
    return ledger.key_of(repo_a) == ledger.key_of(repo_b)


# ---------------------------------------------------------------- database helpers

def _open() -> sqlite3.Connection | None:
    try:
        return ledger.connect()
    except Exception as exc:  # noqa: BLE001
        ledger.log_error("open ledger", exc)
        return None


def _touch(con: sqlite3.Connection | None, ev: Event, state: str | None = None) -> None:
    if con is None:
        return
    try:
        ledger.touch_session(
            con,
            session_id=ev.session_id,
            agent=ev.agent,
            cwd=ev.cwd,
            pid=ev.pid,
            agent_kind=ev.agent_kind,
            agent_name=ev.agent_name,
            parent_id=ev.parent_id,
            session_file=ev.session_file,
            state=state,
        )
    except Exception as exc:  # noqa: BLE001
        ledger.log_error(f"touch {ev.agent}", exc, con)


def _record(
    con: sqlite3.Connection | None,
    ev: Event,
    kind: str,
    *,
    path: str | None = None,
    detail: str | None = None,
    ok: int | None = None,
) -> ledger.RepoInfo | None:
    if con is None:
        return None
    try:
        return ledger.record(
            con, ev.session_id, kind, ev.tool or None,
            intent=ev.intent, detail=detail, path=path,
            call_id=ev.call_id, ok=ok, cwd=ev.cwd,
        )
    except Exception as exc:  # noqa: BLE001
        ledger.log_error(f"record {ev.agent} {kind}", exc, con)
        return None


def _granted(con: sqlite3.Connection | None, ev: Event) -> bool:
    """Whether the user allowed destructive git in this session (or its parent)."""
    if con is None:
        return False
    try:
        return any(
            ledger.is_allowed(con, sid, GIT_DISCARD)
            for sid in (ev.session_id, ev.parent_id)
            if sid
        )
    except Exception:  # noqa: BLE001 - unknown means not allowed
        return False


def _touched(con: sqlite3.Connection, session_id: str) -> list[tuple[str, bool]]:
    """Checkouts this session changed, each with a flag: handed off to another session since."""
    rows = con.execute(
        """SELECT e.checkout AS checkout, max(e.ts) AS mine,
             coalesce((SELECT max(o.ts) FROM events o WHERE o.checkout = e.checkout
                         AND o.kind IN ('edit','git') AND coalesce(o.ok,1)=1
                         AND o.session_id <> ?
                         AND o.session_id NOT IN (SELECT session_id FROM sessions WHERE parent_id = ?)), '') AS others
           FROM events e
           WHERE e.session_id = ? AND e.checkout IS NOT NULL
             AND e.kind IN ('edit','git') AND coalesce(e.ok,1)=1
           GROUP BY e.checkout""",
        (session_id, session_id, session_id),
    ).fetchall()
    return [(row["checkout"], row["mine"] < row["others"]) for row in rows]


def owned_keys(con: sqlite3.Connection, session_id: str, checkout: str) -> set[str]:
    rows = con.execute(
        """SELECT DISTINCT path_key FROM events WHERE session_id=? AND checkout=?
           AND kind='edit' AND path_key IS NOT NULL AND coalesce(ok,1)=1""",
        (session_id, checkout),
    ).fetchall()
    return {row[0] for row in rows}


def _save_pending(con: sqlite3.Connection, session_id: str, states: list[PendingState], checked: list[str]) -> None:
    stamp = ledger.now()
    for state in states:
        con.execute(
            """INSERT INTO pending(checkout, session_id, branch, dirty, unpushed, not_in_main, has_remote, ts)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(checkout) DO UPDATE SET session_id=excluded.session_id,
                 branch=excluded.branch, dirty=excluded.dirty, unpushed=excluded.unpushed,
                 not_in_main=excluded.not_in_main, has_remote=excluded.has_remote, ts=excluded.ts""",
            (state.checkout, session_id, state.branch, state.dirty, state.unpushed,
             state.not_in_main, int(state.has_remote), stamp),
        )
    keep = {state.checkout for state in states}
    for checkout in checked:
        if checkout not in keep:
            con.execute("DELETE FROM pending WHERE checkout=? AND session_id=?", (checkout, session_id))
    con.commit()


def _other_editors(con: sqlite3.Connection, ev: Event, key: str, info: ledger.RepoInfo, rel: str) -> list[sqlite3.Row]:
    """Other live sessions that edited this file, or the same relative path in this repo."""
    rows = con.execute(
        """SELECT e.session_id AS sid, s.parent_id AS parent, s.cwd AS cwd, max(e.ts) AS ts,
                  max(e.path_key = ?) AS same_file,
                  (SELECT x.intent FROM events x WHERE x.session_id = e.session_id
                     AND x.path_key = e.path_key AND x.intent IS NOT NULL
                     ORDER BY x.id DESC LIMIT 1) AS file_intent,
                  (SELECT x.intent FROM events x WHERE x.session_id = e.session_id
                     AND x.intent IS NOT NULL ORDER BY x.id DESC LIMIT 1) AS intent
           FROM events e JOIN sessions s ON s.session_id = e.session_id
           WHERE (e.path_key = ? OR (lower(e.repo) = lower(?) AND e.checkout IS NOT NULL
                  AND lower(substr(e.path, length(e.checkout) + 2)) = ?))
             AND e.session_id <> ? AND e.ts >= ? AND coalesce(e.ok,1) = 1
             AND s.ended_at IS NULL AND s.last_seen >= ?
           GROUP BY e.session_id""",
        (key, key, info.repo, rel, ev.session_id,
         ledger.ago(ledger.CONFLICT_WINDOW_MIN), ledger.ago(ledger.STALE_SESSION_MIN)),
    ).fetchall()
    return [row for row in rows if row["sid"] != ev.parent_id and row["parent"] != ev.session_id]


# ---------------------------------------------------------------- handlers

BLOCKED_HINT = (
    "[lanekeeper] Blocked {what}. Uncommitted changes, possibly another session's, would be "
    "lost or hidden in a stash and forgotten. Safer: commit your own work to a WIP commit on "
    "your branch (git add <files> && git commit -m \"wip: ...\"), revert only your own edits "
    "with the edit tool, or create a new worktree. The user can allow this for the rest of "
    "this session by typing exactly: allow git discard"
)


def _shell(ev: Event) -> Result:
    command = ev.command or ""
    what = guard.destructive_git(command)
    con = _open()
    try:
        _touch(con, ev)
        if what is not None and not _granted(con, ev):
            _record(con, ev, "blocked", detail=command_names(command), ok=0)
            return Result(block=BLOCKED_HINT.format(what=what))
        kind = "git" if guard.GIT_RISKY.search(command) else "bash"
        _record(con, ev, kind, detail=command_names(command))
    except Exception as exc:  # noqa: BLE001
        ledger.log_error("hook shell", exc, con)
        if what is not None:
            return Result(block=BLOCKED_HINT.format(what=what))
    finally:
        if con is not None:
            con.close()
    return Result()


def _edit(ev: Event, con: sqlite3.Connection) -> Result:
    _touch(con, ev)
    notes: list[str] = []
    for path in ev.files:
        info = _record(con, ev, "edit", path=path)
        if info is None or not path.lower().startswith(info.checkout.lower() + "/"):
            continue
        rel_display = path[len(info.checkout) + 1:]
        rel = rel_display.lower()
        key = ledger.key_of(path)

        for line in foreign_edits(info.repo, info.checkout, rel):
            if ledger.once(con, ev.session_id, f"fore:{key}|{line}"):
                notes.append(
                    f"[lanekeeper] Before editing {rel_display}: {line}. Overlapping edits "
                    "conflict on merge; different regions merge automatically. Keep your "
                    "change minimal, or message that session (lanekeeper send) to agree who "
                    "keeps which lines."
                )

        for row in _other_editors(con, ev, key, info, rel):
            same = bool(row["same_file"])
            where = (
                "same file, same directory: writes overwrite each other"
                if same
                else "same file in another worktree: non-overlapping hunks merge automatically"
            )
            their_task = row["file_intent"] or row["intent"]
            if ledger.once(con, ev.session_id, f"file:{key}:{row['sid']}"):
                suffix = f". Its task: {their_task}" if their_task else ""
                notes.append(
                    f"[lanekeeper] {where}: session ...{row['sid'][-8:]} (cwd {row['cwd']}) "
                    f"edited it at {row['ts'][11:16]}{suffix}. Re-read the file before "
                    "writing; change only your part; if the two tasks overlap, stop and tell the user."
                )
            if same:
                ledger.notify(
                    con, row["sid"], ev.session_id, key,
                    f"[lanekeeper] Session ...{ev.session_id[-8:]} (cwd {ev.cwd}) just edited "
                    f"the same file {rel_display} in this folder. Its task: "
                    f"{ev.intent or 'unknown'}. Re-read the file before writing; keep its "
                    "changes; if the tasks overlap, tell the user. No reply needed.",
                )
    return Result(context=notes)


def _post_edit(ev: Event, con: sqlite3.Connection) -> Result:
    _touch(con, ev)
    checkouts: list[str] = []
    for path in ev.files:
        if ev.ok is False:
            ledger.mark_ok(con, ev.session_id, ok=0, path=path, call_id=ev.call_id)
            continue
        if not ledger.mark_ok(con, ev.session_id, ok=1, path=path, call_id=ev.call_id):
            _record(con, ev, "edit", path=path, ok=1)
        info = ledger.gitinfo(os.path.dirname(path))
        if info is not None:
            checkouts.append(info.checkout)
    for checkout in dict.fromkeys(checkouts):
        _scan_quiet(checkout)
    return Result()


def _prompt(ev: Event, con: sqlite3.Connection) -> Result:
    _touch(con, ev, state="running")
    if ev.prompt and guard.is_discard_phrase(ev.prompt):
        ledger.grant(con, ev.session_id, GIT_DISCARD)
    return Result(context=ledger.take_notices(con, ev.session_id))


def _greeting(con: sqlite3.Connection, ev: Event) -> list[str]:
    rows = con.execute(
        "SELECT checkout, session_id, ts FROM pending WHERE session_id<>? AND ts>=? ORDER BY ts DESC",
        (ev.session_id, ledger.ago(GREETING_DAYS * 1440)),
    ).fetchall()
    top = gitutil.out(ev.cwd, "rev-parse", "--show-toplevel")
    lines: list[str] = []
    for row in rows:
        if len(lines) >= GREETING_LIMIT:
            break
        if not in_folder(row["checkout"], ev.cwd, top):
            continue
        state = checkout_state(row["checkout"])
        if state is None or not state.pending:
            con.execute("DELETE FROM pending WHERE checkout=?", (row["checkout"],))
            continue
        lines.append(f"- {describe_pending(state)} (session ...{row['session_id'][-8:]}, {row['ts'][5:16]})")
    con.commit()
    if not lines:
        return []
    return [
        "[lanekeeper] Unfinished work from other sessions in this folder (checked with git just now):\n"
        + "\n".join(lines)
        + "\nDetails: lanekeeper pending"
    ]


def _session_start(ev: Event, con: sqlite3.Connection) -> Result:
    _touch(con, ev)
    ledger.prune(con)
    greeting = [] if ev.agent_kind == "sub" else _greeting(con, ev)
    return Result(notices=greeting, context=ledger.take_notices(con, ev.session_id))


def _stop(ev: Event, con: sqlite3.Connection) -> Result:
    _touch(con, ev, state="idle")
    if ev.agent_kind == "sub":
        return Result()
    touched = _touched(con, ev.session_id)
    if not touched:
        return Result()

    states: list[PendingState] = []
    checked: list[str] = []
    for checkout, handed_off in touched:
        checked.append(checkout)
        state = checkout_state(checkout, owned_keys(con, ev.session_id, checkout))
        if state is None:
            continue
        if handed_off:
            state = replace(state, unpushed=0, not_in_main=0)
        if state.pending:
            states.append(state)
    _save_pending(con, ev.session_id, states, checked)
    for checkout in checked:
        _scan_quiet(checkout)

    if not states:
        return Result()
    signature = "|".join(sorted(describe_pending(s) for s in states))
    if not ledger.changed(con, ev.session_id, "pending-nag", signature):
        return Result()
    lines = "\n".join(f"- {describe_pending(s)}" for s in states)
    return Result(
        notices=[
            "[lanekeeper] This session left unfinished work:\n" + lines
            + "\nAsk the agent to commit or merge it. All sessions: lanekeeper pending"
        ]
    )


def _session_end(ev: Event, con: sqlite3.Connection) -> Result:
    stamp = ledger.now()
    con.execute(
        "UPDATE sessions SET ended_at=?, last_seen=?, state='idle' WHERE session_id=?",
        (stamp, stamp, ev.session_id),
    )
    ledger.revoke_all(con, ev.session_id)
    con.commit()
    return Result()


_HANDLERS: dict[str, Callable[[Event, sqlite3.Connection], Result]] = {
    "edit": _edit,
    "post_edit": _post_edit,
    "prompt": _prompt,
    "session_start": _session_start,
    "stop": _stop,
    "session_end": _session_end,
}


def _dispatch(ev: Event) -> Result:
    handler = _HANDLERS.get(ev.kind)
    if handler is None:
        return Result()
    con = _open()
    if con is None:
        return Result()
    try:
        return handler(ev, con)
    finally:
        con.close()


def handle(ev: Event) -> Result:
    """Run one hook event. Never raises."""
    try:
        if ev.kind == "shell":
            return _shell(ev)
        return _dispatch(ev)
    except Exception as exc:  # noqa: BLE001 - fail open
        ledger.log_error(f"hook {ev.agent} {ev.kind}", exc)
        return Result()
