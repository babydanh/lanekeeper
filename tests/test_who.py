"""who, pending, send, and doctor reports over a real ledger."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from helpers import isolate

from lanekeeper import ledger, who


class WhoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = isolate(self)
        self.con = ledger.connect()
        self.addCleanup(self.con.close)

    def capture(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = who.main(list(argv))
        return code, buf.getvalue()

    def add_session(self, session_id: str) -> None:
        ledger.touch_session(
            self.con, session_id=session_id, agent="claude", cwd=str(self.base), pid=None,
            agent_kind=None, agent_name=None, parent_id=None, session_file=None, state="running",
        )

    def test_lists_active_sessions_by_short_id(self) -> None:
        self.add_session("claude-session-1234abcd")
        code, out = self.capture()
        self.assertEqual(code, 0)
        self.assertIn("...1234abcd", out)

    def test_pending_with_nothing_to_report(self) -> None:
        code, out = self.capture("pending")
        self.assertEqual(code, 0)
        self.assertIn("Nothing pending.", out)

    def test_send_queues_a_message_for_live_sessions(self) -> None:
        self.add_session("claude-live-0001")
        code, out = self.capture("send", "all", "stop", "now")
        self.assertEqual(code, 0)
        self.assertIn("Queued for 1 session(s)", out)
        notices = ledger.take_notices(self.con, "claude-live-0001")
        self.assertEqual(len(notices), 1)
        self.assertIn("Message from the user: stop now", notices[0])

    def test_doctor_reports_logged_hook_errors_and_fails(self) -> None:
        ledger.log_error("hook shell", ValueError("boom"), self.con)
        code, out = self.capture("doctor")
        self.assertEqual(code, 1)
        self.assertIn("[!] hook error at hook shell", out)

    def test_doctor_flags_an_install_made_by_another_version(self) -> None:
        config = self.base / "settings.json"
        config.write_text(json.dumps({"hooks": {}, "note": "-m lanekeeper hook claude session_start"}),
                          encoding="utf-8")
        records = {str(config): {"agent": "claude", "scope": "user", "version": "0.0.1"}}
        (ledger.home() / "installed.json").write_text(json.dumps(records), encoding="utf-8")
        code, out = self.capture("doctor")
        self.assertEqual(code, 1)
        self.assertIn("installed by lanekeeper 0.0.1", out)

    def test_unknown_command_prints_usage(self) -> None:
        code, out = self.capture("bogus")
        self.assertEqual(code, 1)
        self.assertIn("usage: lanekeeper who", out)


if __name__ == "__main__":
    unittest.main()
