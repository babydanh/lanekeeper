"""Activity ledger: the SQLite store behind hooks, `who` and the overlap scanner.

Location: $LANEKEEPER_DB, else $LANEKEEPER_HOME/activity.db, else ~/.lanekeeper/activity.db.
Stores paths, intents, command names and counters. Never stores prompt text, file
contents or command arguments.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from lanekeeper import __version__
from lanekeeper import _git

EVENT_RETENTION_DAYS = 30
SHORT_RETENTION_DAYS = 7  # notices, errors, warned keys
CONFLICT_WINDOW_MIN = 120  # another session's edit conflicts for this long
STALE_SESSION_MIN = 180  # a session silent this long counts as stopped

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, pid INTEGER, cwd TEXT, agent TEXT, agent_kind TEXT,
  agent_name TEXT, parent_id TEXT, session_file TEXT, started_at TEXT, last_seen TEXT,
  ended_at TEXT, state TEXT, heartbeat TEXT, ext_ver TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT, session_id TEXT, kind TEXT, tool TEXT, path TEXT,
  path_key TEXT, repo TEXT, checkout TEXT, is_main_checkout INTEGER, branch TEXT,
  intent TEXT, detail TEXT, call_id TEXT, ok INTEGER);
CREATE INDEX IF NOT EXISTS ev_path ON events(path_key, ts);
CREATE INDEX IF NOT EXISTS ev_sess ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ev_call ON events(call_id);
CREATE TABLE IF NOT EXISTS pending(
  checkout TEXT PRIMARY KEY, session_id TEXT, branch TEXT, dirty INTEGER,
  unpushed INTEGER, not_in_main INTEGER, has_remote INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS notices(
  id INTEGER PRIMARY KEY, ts TEXT, to_session TEXT, from_session TEXT, rel_key TEXT,
  content TEXT, delivered INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS nt_to ON notices(to_session, delivered);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, ts TEXT, at TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS allow(session_id TEXT, kind TEXT, ts TEXT,
  PRIMARY KEY(session_id, kind));
CREATE TABLE IF NOT EXISTS warned(session_id TEXT, key TEXT, value TEXT, ts TEXT,
  PRIMARY KEY(session_id, key));
"""


@dataclass(frozen=True)
class RepoInfo:
    """Where a path lives: the main repo root, the checkout root, and whether it is the main tree."""

    repo: str
    checkout: str
    main: bool


# ---------------------------------------------------------------- paths and time

def home() -> Path:
    """Lanekeeper state directory."""
    env = os.environ.get("LANEKEEPER_HOME")
    return Path(env) if env else Path.home() / ".lanekeeper"


def db_path() -> Path:
    env = os.environ.get("LANEKEEPER_DB")
    return Path(env) if env else home() / "activity.db"


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def now() -> str:
    return _stamp(datetime.now())


def ago(minutes: float) -> str:
    return _stamp(datetime.now() - timedelta(minutes=minutes))


def slash(path: str | os.PathLike[str]) -> str:
    return str(path).replace("\\", "/")


def norm(path: str | os.PathLike[str]) -> str:
    """Absolute path with forward slashes (no symlink resolution)."""
    return slash(os.path.abspath(os.fspath(path)))


def realish(path: str | os.PathLike[str]) -> str:
    """Real path of the longest existing prefix, plus the non-existent tail."""
    current = os.path.abspath(os.fspath(path))
    tail: list[str] = []
    while True:
        if os.path.exists(current):
            return slash(os.path.join(os.path.realpath(current), *tail))
        parent = os.path.dirname(current)
        if parent == current:
            return slash(os.path.abspath(os.fspath(path)))
        tail.insert(0, os.path.basename(current))
        current = parent


def key_of(path: str | os.PathLike[str]) -> str:
    """Comparison key for a path: real path, lowercased on Windows."""
    real = realish(path)
    return real.lower() if os.name == "nt" else real


def clip(value: object, limit: int) -> str | None:
    """Collapse whitespace and cut to `limit` characters; None for empty input."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- connection

def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open the ledger (creating directory, file and schema as needed)."""
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(target), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=3000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


@contextmanager
def _conn(con: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    if con is not None:
        yield con
        return
    opened = connect()
    try:
        yield opened
    finally:
        opened.close()


# ---------------------------------------------------------------- repository info

def _read_gitdir(dotgit_file: str, base: str) -> str | None:
    with open(dotgit_file, encoding="utf-8", errors="replace") as handle:
        match = re.search(r"gitdir:\s*(.+)", handle.read())
    if not match:
        return None
    return os.path.normpath(os.path.join(base, match.group(1).strip()))


def gitinfo(directory: str | os.PathLike[str] | None) -> RepoInfo | None:
    """Locate the repo and checkout containing `directory` by walking up to `.git`."""
    if directory is None:
        return None
    current = os.path.abspath(os.fspath(directory))
    for _ in range(40):
        dotgit = os.path.join(current, ".git")
        try:
            if os.path.isdir(dotgit):
                return RepoInfo(norm(current), norm(current), True)
            if os.path.isfile(dotgit):
                gitdir = _read_gitdir(dotgit, current)
                in_worktrees = bool(gitdir and re.search(r"[\\/]worktrees[\\/][^\\/]+$", gitdir))
                root = os.path.dirname(os.path.dirname(os.path.dirname(gitdir))) if in_worktrees and gitdir else current
                return RepoInfo(norm(root), norm(current), False)
        except OSError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def branch_of(checkout: str) -> str | None:
    """Branch name from HEAD; the first 8 characters of a detached HEAD."""
    dotgit = os.path.join(checkout, ".git")
    try:
        gitdir = _read_gitdir(dotgit, checkout) if os.path.isfile(dotgit) else dotgit
        if not gitdir:
            return None
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as handle:
            head = handle.read().strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else head[:8]


def main_root(directory: str | os.PathLike[str]) -> str | None:
    """Root of the main working tree shared by this checkout (None outside a repo)."""
    common = _git.common_dir(directory)
    return norm(common.parent) if common is not None else None


# ---------------------------------------------------------------- writes

def touch_session(
    con: sqlite3.Connection,
    *,
    session_id: str,
    agent: str,
    cwd: str,
    pid: int | None = None,
    agent_kind: str | None = None,
    agent_name: str | None = None,
    parent_id: str | None = None,
    session_file: str | None = None,
    state: str | None = None,
) -> None:
    """Create or refresh a session row; a touch after SessionEnd reopens it."""
    stamp = now()
    con.execute(
        """INSERT INTO sessions(session_id, pid, cwd, agent, agent_kind, agent_name, parent_id,
                                session_file, started_at, last_seen, heartbeat, state, ext_ver)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET
             pid=excluded.pid, cwd=excluded.cwd, agent=excluded.agent,
             agent_kind=coalesce(excluded.agent_kind, sessions.agent_kind),
             agent_name=coalesce(excluded.agent_name, sessions.agent_name),
             parent_id=coalesce(excluded.parent_id, sessions.parent_id),
             session_file=coalesce(excluded.session_file, sessions.session_file),
             last_seen=excluded.last_seen, heartbeat=excluded.heartbeat, ended_at=NULL,
             state=coalesce(excluded.state, sessions.state), ext_ver=excluded.ext_ver""",
        (session_id, pid, cwd, agent, agent_kind, agent_name, parent_id, session_file,
         stamp, stamp, stamp, state, __version__),
    )
    con.commit()


def record(
    con: sqlite3.Connection,
    session_id: str,
    kind: str,
    tool: str | None,
    *,
    intent: str | None = None,
    detail: str | None = None,
    path: str | None = None,
    call_id: str | None = None,
    ok: int | None = None,
    cwd: str | None = None,
) -> RepoInfo | None:
    """Append one event; repo info comes from the file's directory, or from cwd."""
    anchor = os.path.dirname(path) if path else cwd
    info = gitinfo(anchor) if anchor else None
    con.execute(
        """INSERT INTO events(ts, session_id, kind, tool, path, path_key, repo, checkout,
                              is_main_checkout, branch, intent, detail, call_id, ok)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now(), session_id, kind, tool, norm(path) if path else None,
            key_of(path) if path else None,
            info.repo if info else None,
            info.checkout if info else None,
            (1 if info.main else 0) if info else None,
            branch_of(info.checkout) if info else None,
            clip(intent, 160), clip(detail, 400), call_id, ok,
        ),
    )
    con.commit()
    return info


def mark_ok(
    con: sqlite3.Connection,
    session_id: str,
    *,
    ok: int,
    path: str | None = None,
    call_id: str | None = None,
) -> bool:
    """Set the result of a pending edit event (pre-edit recorded with ok NULL).

    Matches by call id when present, else by path within the last 10 minutes.
    Returns True when a pending row was updated.
    """
    if call_id:
        cur = con.execute(
            "UPDATE events SET ok=? WHERE session_id=? AND call_id=? AND ok IS NULL",
            (ok, session_id, call_id),
        )
        if cur.rowcount:
            con.commit()
            return True
    if path:
        cur = con.execute(
            """UPDATE events SET ok=? WHERE id=(
                 SELECT id FROM events WHERE session_id=? AND path_key=? AND kind='edit'
                   AND ok IS NULL AND ts>=? ORDER BY id DESC LIMIT 1)""",
            (ok, session_id, key_of(path), ago(10)),
        )
        con.commit()
        return cur.rowcount > 0
    return False


def post_notice(
    to: str,
    from_: str,
    key: str,
    content: str,
    con: sqlite3.Connection | None = None,
) -> None:
    """Queue a message for a session; it is delivered on that session's next hook call."""
    with _conn(con) as active:
        active.execute(
            "INSERT INTO notices(ts, to_session, from_session, rel_key, content) VALUES(?,?,?,?,?)",
            (now(), to, from_, key, content),
        )
        active.commit()


def notify(con: sqlite3.Connection, to: str, from_: str, rel_key: str, content: str) -> bool:
    """Like post_notice, but skips when the same pair/key was queued in the last 30 minutes."""
    dup = con.execute(
        "SELECT 1 FROM notices WHERE to_session=? AND from_session=? AND rel_key=? AND ts>=? LIMIT 1",
        (to, from_, rel_key, ago(30)),
    ).fetchone()
    if dup:
        return False
    post_notice(to, from_, rel_key, content, con)
    return True


def take_notices(con: sqlite3.Connection, session_id: str, limit: int = 20) -> list[str]:
    """Undelivered messages for a session, marked delivered in the same call."""
    rows = con.execute(
        "SELECT id, content FROM notices WHERE to_session=? AND delivered=0 ORDER BY id LIMIT ?",
        (session_id, limit),
    ).fetchall()
    if rows:
        con.executemany("UPDATE notices SET delivered=1 WHERE id=?", [(row["id"],) for row in rows])
        con.commit()
    return [row["content"] for row in rows]


def log_error(at: str, exc: BaseException, con: sqlite3.Connection | None = None) -> None:
    """Record a hook failure. Never raises; falls back to stderr if the ledger is unusable."""
    message = f"{type(exc).__name__}: {exc}"[:300]
    try:
        with _conn(con) as active:
            active.execute("INSERT INTO errors(ts, at, message) VALUES(?,?,?)", (now(), at, message))
            active.commit()
    except Exception:  # noqa: BLE001 - logging must never fail the caller
        try:
            sys.stderr.write(f"lanekeeper: {at}: {message}\n")
        except Exception:  # noqa: BLE001
            pass


def grant(con: sqlite3.Connection, session_id: str, kind: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO allow(session_id, kind, ts) VALUES(?,?,?)",
        (session_id, kind, now()),
    )
    con.commit()


def revoke_all(con: sqlite3.Connection, session_id: str) -> None:
    con.execute("DELETE FROM allow WHERE session_id=?", (session_id,))
    con.commit()


def is_allowed(con: sqlite3.Connection, session_id: str, kind: str) -> bool:
    return con.execute(
        "SELECT 1 FROM allow WHERE session_id=? AND kind=?", (session_id, kind)
    ).fetchone() is not None


def once(con: sqlite3.Connection, session_id: str, key: str) -> bool:
    """True the first time `key` is seen for this session."""
    cur = con.execute(
        "INSERT OR IGNORE INTO warned(session_id, key, value, ts) VALUES(?,?,'',?)",
        (session_id, key, now()),
    )
    con.commit()
    return cur.rowcount == 1


def changed(con: sqlite3.Connection, session_id: str, key: str, value: str) -> bool:
    """True when `value` differs from the last value stored under `key` (then stores it)."""
    row = con.execute(
        "SELECT value FROM warned WHERE session_id=? AND key=?", (session_id, key)
    ).fetchone()
    if row is not None and row["value"] == value:
        return False
    con.execute(
        "INSERT OR REPLACE INTO warned(session_id, key, value, ts) VALUES(?,?,?,?)",
        (session_id, key, value, now()),
    )
    con.commit()
    return True


def prune(con: sqlite3.Connection) -> None:
    """Drop rows past their retention window."""
    con.execute("DELETE FROM events WHERE ts < ?", (ago(EVENT_RETENTION_DAYS * 1440),))
    short = ago(SHORT_RETENTION_DAYS * 1440)
    for table in ("notices", "errors", "warned"):
        con.execute(f"DELETE FROM {table} WHERE ts < ?", (short,))
    con.commit()
