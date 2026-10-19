"""Ledger schema, pairing of edit results, notices, grants, and retention."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from helpers import isolate, tmpdir

from lanekeeper import ledger


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = isolate(self)
        self.con = ledger.connect()
        self.addCleanup(self.con.close)

    def test_schema_and_wal_mode(self) -> None:
        tables = {row[0] for row in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"sessions", "events", "pending", "notices", "errors", "allow", "warned"} <= tables)
        self.assertEqual(self.con.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_default_database_lives_under_lanekeeper_home(self) -> None:
        self.assertEqual(ledger.db_path(), self.base / "lk" / "activity.db")

    def test_lanekeeper_db_overrides_the_default(self) -> None:
        other = tmpdir(self) / "other.db"
        with mock.patch.dict(os.environ, {"LANEKEEPER_DB": str(other)}):
            self.assertEqual(ledger.db_path(), other)

    def test_touch_session_upserts_and_reopens(self) -> None:
        kwargs = dict(agent="claude", cwd=str(self.base), pid=None, agent_kind=None, agent_name=None,
                      parent_id=None, session_file=None, state="running")
        ledger.touch_session(self.con, session_id="s1", **kwargs)
        ledger.touch_session(self.con, session_id="s1", **kwargs)
        rows = self.con.execute("SELECT count(*), max(ended_at) FROM sessions WHERE session_id='s1'").fetchone()
        self.assertEqual(rows[0], 1)
        self.assertIsNone(rows[1])

    def test_mark_ok_pairs_with_the_pre_edit_row(self) -> None:
        target = str(self.base / "f.txt")
        ledger.record(self.con, "s1", "edit", "Edit", path=target, call_id="c1", ok=None, cwd=None)
        self.assertTrue(ledger.mark_ok(self.con, "s1", ok=1, path=target, call_id="c1"))
        rows = self.con.execute("SELECT ok FROM events WHERE session_id='s1' AND kind='edit'").fetchall()
        self.assertEqual([row[0] for row in rows], [1])

    def test_mark_ok_without_a_pending_row_reports_false(self) -> None:
        self.assertFalse(ledger.mark_ok(self.con, "s1", ok=1, path=str(self.base / "x.txt"), call_id=None))

    def test_failed_edit_is_marked_not_ok(self) -> None:
        target = str(self.base / "g.txt")
        ledger.record(self.con, "s1", "edit", "Edit", path=target, call_id="c2", ok=None, cwd=None)
        ledger.mark_ok(self.con, "s1", ok=0, path=target, call_id="c2")
        self.assertEqual(self.con.execute("SELECT ok FROM events WHERE call_id='c2'").fetchone()[0], 0)

    def test_notify_dedupes_and_take_notices_delivers_once(self) -> None:
        self.assertTrue(ledger.notify(self.con, "s2", "s1", "k", "hello"))
        self.assertFalse(ledger.notify(self.con, "s2", "s1", "k", "hello again"))
        self.assertEqual(ledger.take_notices(self.con, "s2"), ["hello"])
        self.assertEqual(ledger.take_notices(self.con, "s2"), [])

    def test_post_notice_is_delivered_to_the_target(self) -> None:
        ledger.post_notice("s2", "user", "u1", "msg", con=self.con)
        self.assertEqual(ledger.take_notices(self.con, "s2"), ["msg"])

    def test_log_error_stores_the_failure(self) -> None:
        ledger.log_error("hook shell", ValueError("boom"), self.con)
        row = self.con.execute("SELECT at, message FROM errors").fetchone()
        self.assertEqual((row["at"], row["message"]), ("hook shell", "ValueError: boom"))

    def test_once_and_changed(self) -> None:
        self.assertTrue(ledger.once(self.con, "s1", "k"))
        self.assertFalse(ledger.once(self.con, "s1", "k"))
        self.assertTrue(ledger.changed(self.con, "s1", "sig", "a"))
        self.assertFalse(ledger.changed(self.con, "s1", "sig", "a"))
        self.assertTrue(ledger.changed(self.con, "s1", "sig", "b"))

    def test_grant_and_revoke(self) -> None:
        ledger.grant(self.con, "s1", "git-discard")
        self.assertTrue(ledger.is_allowed(self.con, "s1", "git-discard"))
        ledger.revoke_all(self.con, "s1")
        self.assertFalse(ledger.is_allowed(self.con, "s1", "git-discard"))

    def test_prune_drops_rows_past_retention(self) -> None:
        self.con.execute("INSERT INTO events(ts, session_id, kind) VALUES(?,?,?)",
                         (ledger.ago(40 * 1440), "s1", "edit"))
        self.con.execute("INSERT INTO events(ts, session_id, kind) VALUES(?,?,?)",
                         (ledger.now(), "s1", "edit"))
        self.con.execute("INSERT INTO notices(ts, to_session, from_session, rel_key, content, delivered) "
                         "VALUES(?,?,?,?,?,0)", (ledger.ago(8 * 1440), "s2", "s1", "k", "old"))
        self.con.commit()
        ledger.prune(self.con)
        self.assertEqual(self.con.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM notices").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
