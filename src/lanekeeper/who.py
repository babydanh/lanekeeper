"""Ledger reports for people: who is active, what is pending, and whether the setup is healthy.

Commands:
  who (default)            active sessions
  file <substring>         recent edits to matching paths
  conflicts [hours]        files edited by more than one session (default 6 h)
  pending                  unfinished work per checkout, checked with git now
  session <id> [n]         last n ledger rows of one session (default 20)
  send <id|all> <message>  queue a message for live sessions
  stash                    stashes in repos this ledger has seen (30 days)
  doctor                   health checks; exit 1 when something needs attention
"""

from __future__ import annotations

import sqlite3
import sys
import time
from typing import Sequence

from lanekeeper import __version__, _git, foreign, install, ledger
from lanekeeper.hooks import core

STASH_WINDOW_DAYS = 30
ERROR_WINDOW_MIN = 24 * 60
NOTICE_STUCK_MIN = 60
USAGE = """usage: lanekeeper who [command]
  (no command)            active sessions
  file <substring>        recent edits to matching paths
  conflicts [hours]       files edited by more than one session (default 6)
  pending                 unfinished work, checked with git now
  session <id> [n]        last n ledger rows of one session (default 20)
  send <id|all> <message> queue a message for live sessions
  stash                   stashes in repos seen in the last 30 days
  doctor                  health checks"""


def _short(session_id: str) -> str:
    return "..." + session_id[-8:]


def _hm(stamp: str | None) -> str:
    return stamp[11:16] if stamp else "--:--"


def _known_repos(con: sqlite3.Connection, days: int) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT repo FROM events WHERE repo IS NOT NULL AND ts >= ?",
        (ledger.ago(days * 1440),),
    ).fetchall()
    return [row[0] for row in rows]


def _stashes(repo: str) -> list[tuple[str, float, str]]:
    raw = _git.out(repo, "stash", "list", "--format=%gd%x09%ct%x09%gs")
    entries: list[tuple[str, float, str]] = []
    for line in (raw or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        try:
            entries.append((parts[0], float(parts[1]), parts[2]))
        except ValueError:
            continue
    return entries


def cmd_who(con: sqlite3.Connection) -> int:
    rows = con.execute(
        """SELECT s.session_id, s.agent, s.agent_name, s.agent_kind, s.cwd, s.state, s.last_seen,
             (SELECT count(*) FROM events e WHERE e.session_id = s.session_id
                AND e.kind = 'edit' AND coalesce(e.ok,1) = 1) AS edits,
             (SELECT x.intent FROM events x WHERE x.session_id = s.session_id
                AND x.intent IS NOT NULL ORDER BY x.id DESC LIMIT 1) AS intent
           FROM sessions s
           WHERE s.ended_at IS NULL AND s.last_seen >= ?
           ORDER BY s.last_seen DESC""",
        (ledger.ago(ledger.STALE_SESSION_MIN),),
    ).fetchall()
    if not rows:
        print(f"No active sessions in the last {ledger.STALE_SESSION_MIN // 60} hours.")
        return 0
    for row in rows:
        name = f" {row['agent_name']}" if row["agent_name"] else ""
        sub = " (subagent)" if row["agent_kind"] == "sub" else ""
        print(
            f"{row['agent']}{name} {_short(row['session_id'])}{sub}  {row['state'] or 'unknown'}"
            f"  last seen {_hm(row['last_seen'])}  edits {row['edits']}"
        )
        print(f"    cwd {row['cwd']}")
        if row["intent"]:
            print(f"    task: {row['intent']}")
    return 0


def cmd_file(con: sqlite3.Connection, needle: str) -> int:
    rows = con.execute(
        """SELECT min(e.path) AS path, e.session_id AS sid, s.agent AS agent, max(e.ts) AS ts, count(*) AS n
           FROM events e LEFT JOIN sessions s ON s.session_id = e.session_id
           WHERE e.kind = 'edit' AND coalesce(e.ok,1) = 1 AND lower(e.path_key) LIKE ? AND e.ts >= ?
           GROUP BY e.path_key, e.session_id
           ORDER BY ts DESC LIMIT 50""",
        (f"%{needle.lower()}%", ledger.ago(6 * 60)),
    ).fetchall()
    if not rows:
        print(f"No edits to paths matching '{needle}' in the last 6 hours.")
        return 0
    for row in rows:
        print(f"{row['path']}  {row['agent'] or '?'} {_short(row['sid'])}  {_hm(row['ts'])}  {row['n']} edit(s)")
    return 0


def cmd_conflicts(con: sqlite3.Connection, hours: float) -> int:
    rows = con.execute(
        """SELECT min(e.path) AS path, count(DISTINCT e.session_id) AS n,
                  group_concat(DISTINCT e.session_id) AS sids, max(e.ts) AS last
           FROM events e
           WHERE e.kind = 'edit' AND coalesce(e.ok,1) = 1 AND e.ts >= ? AND e.path_key IS NOT NULL
           GROUP BY e.path_key
           HAVING count(DISTINCT e.session_id) >= 2
           ORDER BY last DESC LIMIT 50""",
        (ledger.ago(hours * 60),),
    ).fetchall()
    if not rows:
        print(f"No file was edited by more than one session in the last {hours:g} hours.")
        return 0
    for row in rows:
        sids = ", ".join(_short(sid) for sid in (row["sids"] or "").split(","))
        print(f"{row['path']}  {row['n']} sessions: {sids}  last {_hm(row['last'])}")
    return 0


def cmd_pending(con: sqlite3.Connection) -> int:
    rows = con.execute(
        "SELECT checkout, session_id, ts FROM pending ORDER BY ts DESC"
    ).fetchall()
    shown = 0
    for row in rows:
        state = core.checkout_state(row["checkout"], core.owned_keys(con, row["session_id"], row["checkout"]))
        if state is None or not state.pending:
            continue
        shown += 1
        print(f"- {core.describe_pending(state)} (session {_short(row['session_id'])}, {row['ts'][5:16]})")
    if not shown:
        print("Nothing pending.")
    return 0


def cmd_session(con: sqlite3.Connection, prefix: str, limit: int) -> int:
    row = con.execute(
        "SELECT session_id, agent, cwd FROM sessions WHERE session_id LIKE ? ORDER BY last_seen DESC LIMIT 1",
        (f"%{prefix}%",),
    ).fetchone()
    if row is None:
        print(f"No session matches '{prefix}'.")
        return 1
    print(f"{row['agent']} {row['session_id']}  cwd {row['cwd']}")
    events = con.execute(
        """SELECT ts, kind, tool, path, ok, detail, intent FROM events
           WHERE session_id = ? ORDER BY id DESC LIMIT ?""",
        (row["session_id"], limit),
    ).fetchall()
    for event in reversed(events):
        target = event["path"] or event["detail"] or ""
        flag = " [failed]" if event["ok"] == 0 else ""
        print(f"{_hm(event['ts'])} {event['kind']:<10} {(event['tool'] or ''):<12} {target}{flag}")
    return 0


def cmd_send(con: sqlite3.Connection, target: str, message: str) -> int:
    live = con.execute(
        """SELECT session_id FROM sessions
           WHERE ended_at IS NULL AND last_seen >= ? AND coalesce(agent_kind, '') <> 'sub'""",
        (ledger.ago(ledger.STALE_SESSION_MIN),),
    ).fetchall()
    ids = [row[0] for row in live]
    if target != "all":
        matches = [sid for sid in ids if sid == target or sid.endswith(target)]
        if not matches:
            print(f"No live session matches '{target}'.")
            return 1
        if len(matches) > 1:
            print("Several sessions match; use a longer id: " + ", ".join(_short(s) for s in matches))
            return 1
        ids = matches
    if not ids:
        print("No live sessions to message.")
        return 1
    content = f"[lanekeeper] Message from the user: {message}"
    stamp = ledger.now()
    for sid in ids:
        ledger.post_notice(sid, "user", f"user:{stamp}", content, con=con)
    print(f"Queued for {len(ids)} session(s); delivered on their next hook call.")
    return 0


def cmd_stash(con: sqlite3.Connection) -> int:
    cutoff = time.time() - STASH_WINDOW_DAYS * 86400
    found = 0
    for repo in _known_repos(con, STASH_WINDOW_DAYS):
        for ref, created, subject in _stashes(repo):
            if created < cutoff:
                continue
            found += 1
            age_days = int((time.time() - created) // 86400)
            print(f"{ref} in {repo} ({age_days} day(s) old): {subject}")
    if not found:
        print(f"No stashes in the last {STASH_WINDOW_DAYS} days.")
    return 0


def cmd_doctor(con: sqlite3.Connection) -> int:
    problems = 0

    def ok(message: str) -> None:
        print(f"[OK] {message}")

    def bad(message: str) -> None:
        nonlocal problems
        problems += 1
        print(f"[!] {message}")

    errors = con.execute(
        """SELECT at, count(*) AS n, max(ts) AS last, max(message) AS message
           FROM errors WHERE ts >= ? GROUP BY at ORDER BY n DESC""",
        (ledger.ago(ERROR_WINDOW_MIN),),
    ).fetchall()
    if errors:
        for row in errors:
            bad(f"hook error at {row['at']}: {row['n']}x, last {row['last']}: {row['message']}")
    else:
        ok("no hook errors in the last 24 hours")

    records = install.load_records()
    if not records:
        bad("no agent hooks recorded as installed; run: lanekeeper install <agent>")
    for config, record in records.items():
        agent = record.get("agent", "?")
        if not install.hook_present(config, agent):
            bad(f"{agent} hooks missing from {config}; reinstall: lanekeeper install {agent}")
        elif record.get("version") != __version__:
            bad(f"{agent} hooks installed by lanekeeper {record.get('version')}, now {__version__}; reinstall")
        else:
            ok(f"{agent} hooks present in {config}")

    pending = con.execute("SELECT count(*) FROM pending").fetchone()[0]
    if pending:
        bad(f"{pending} checkout(s) with unfinished work; see: lanekeeper pending")
    else:
        ok("nothing pending")

    stuck = con.execute(
        """SELECT n.to_session AS sid, count(*) AS c
           FROM notices n LEFT JOIN sessions s ON s.session_id = n.to_session
           WHERE n.delivered = 0 AND n.ts < ?
             AND (s.session_id IS NULL OR s.ended_at IS NOT NULL OR s.last_seen < ?)
           GROUP BY n.to_session""",
        (ledger.ago(NOTICE_STUCK_MIN), ledger.ago(ledger.STALE_SESSION_MIN)),
    ).fetchall()
    for row in stuck:
        bad(f"{row['c']} message(s) for ended session {_short(row['sid'])} were never delivered")

    for repo in _known_repos(con, STASH_WINDOW_DAYS):
        for ref, created, subject in _stashes(repo):
            if created >= time.time() - STASH_WINDOW_DAYS * 86400:
                bad(f"stash {ref} in {repo}: {subject}; apply or drop it")

    live_repos = con.execute(
        """SELECT DISTINCT e.repo FROM events e JOIN sessions s ON s.session_id = e.session_id
           WHERE e.repo IS NOT NULL AND s.ended_at IS NULL AND s.last_seen >= ?""",
        (ledger.ago(foreign.LANES_MAX_AGE_S / 60),),
    ).fetchall()
    for row in live_repos:
        repo = row[0]
        common = _git.common_dir(repo)
        if common is None or not (common / "worktrees").exists():
            continue
        lanes = common / "overlap-lanes.json"
        if not lanes.exists():
            bad(f"{repo} has worktrees but no overlap-lanes.json; foreign-edit warnings are off")
        elif time.time() - lanes.stat().st_mtime > foreign.LANES_MAX_AGE_S:
            bad(f"{repo}: overlap-lanes.json is stale; the overlap scan is not running")
        else:
            ok(f"{repo}: overlap-lanes.json is fresh")

    return 1 if problems else 0


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str]) -> int:
    """Entry point for `lanekeeper who ...`. Returns an exit code."""
    _force_utf8()
    args = list(argv)
    if args and args[0] == "who":
        args = args[1:]
    command = args[0] if args else ""
    rest = args[1:]
    if command in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    try:
        con = ledger.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"lanekeeper: cannot open the ledger: {exc}", file=sys.stderr)
        return 1
    try:
        if command == "":
            return cmd_who(con)
        if command == "file" and len(rest) == 1:
            return cmd_file(con, rest[0])
        if command == "conflicts" and len(rest) <= 1:
            try:
                hours = float(rest[0]) if rest else 6.0
            except ValueError:
                print("conflicts takes an optional number of hours")
                return 1
            return cmd_conflicts(con, hours)
        if command == "pending" and not rest:
            return cmd_pending(con)
        if command == "session" and len(rest) in (1, 2):
            try:
                limit = int(rest[1]) if len(rest) == 2 else 20
            except ValueError:
                print("session takes an optional row count")
                return 1
            return cmd_session(con, rest[0], limit)
        if command == "send" and len(rest) >= 2:
            return cmd_send(con, rest[0], " ".join(rest[1:]))
        if command == "stash" and not rest:
            return cmd_stash(con)
        if command == "doctor" and not rest:
            return cmd_doctor(con)
        print(USAGE)
        return 1
    finally:
        con.close()
