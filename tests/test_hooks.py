"""Hook behavior through real `python -m lanekeeper.hooks` subprocesses, plus pure helpers."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from helpers import isolate, make_repo, run_hook

from lanekeeper import ledger
from lanekeeper.hooks import core


def claude_shell(session: str, cwd: Path, command: str, **extra: object) -> dict:
    return {"session_id": session, "cwd": str(cwd), "tool_name": "Bash",
            "tool_input": {"command": command}, **extra}


def claude_edit(session: str, cwd: Path, path: Path, *, call_id: str | None = None,
                intent: str | None = None) -> dict:
    payload: dict = {"session_id": session, "cwd": str(cwd), "tool_name": "Edit",
                     "tool_input": {"file_path": str(path)}}
    if call_id:
        payload["tool_use_id"] = call_id
    if intent:
        payload["intent"] = intent
    return payload


def claude_prompt(session: str, cwd: Path, prompt: str) -> dict:
    return {"session_id": session, "cwd": str(cwd), "prompt": prompt}


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = isolate(self)
        self.repo, self.origin = make_repo(self)
        self.repo_path = self.repo

    def leave_uncommitted(self, session: str) -> None:
        """Edit a.txt through the hooks, leaving the change uncommitted."""
        target = self.repo / "a.txt"
        pre = claude_edit(session, self.repo, target, call_id="c-" + session)
        self.assertIsNone(run_hook("claude", "edit", pre).data)
        target.write_text("one\ntwo\n", encoding="utf-8")
        post = claude_edit(session, self.repo, target, call_id="c-" + session)
        self.assertEqual(run_hook("claude", "post_edit", post).code, 0)

    # ---------------------------------------------------------------- blocking

    def test_destructive_command_is_denied_in_each_agent_shape(self) -> None:
        claude = run_hook("claude", "shell", claude_shell("s1", self.repo, "git stash")).data
        assert claude is not None
        self.assertEqual(claude["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(claude["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("allow git discard", claude["hookSpecificOutput"]["permissionDecisionReason"])

        codex = run_hook("codex", "shell", claude_shell("s1", self.repo, "git clean -fd")).data
        assert codex is not None
        self.assertEqual(codex["hookSpecificOutput"]["permissionDecision"], "deny")

        gemini = run_hook("gemini", "shell", {"session_id": "s1", "cwd": str(self.repo),
                                              "tool_name": "run_shell_command",
                                              "tool_input": {"command": "git checkout -- a.txt"}}).data
        assert gemini is not None
        self.assertEqual(gemini["decision"], "deny")
        self.assertIn("reason", gemini)

        cursor = run_hook("cursor", "shell", {"conversation_id": "c1", "workspace_roots": [str(self.repo)],
                                              "command": "git reset --hard"}).data
        assert cursor is not None
        self.assertEqual(cursor["permission"], "deny")
        self.assertIs(cursor["continue"], True)

        omp = run_hook("omp", "shell", {"session_id": "o1", "cwd": str(self.repo),
                                        "tool_name": "bash", "tool_input": {"command": "git stash"}}).data
        assert omp is not None
        self.assertIs(omp["block"], True)
        self.assertIn("allow git discard", omp["reason"])

    def test_safe_command_passes_silently(self) -> None:
        run = run_hook("claude", "shell", claude_shell("s1", self.repo, "git status"))
        self.assertEqual((run.code, run.stdout), (0, ""))

    def test_allow_phrase_grants_the_session(self) -> None:
        self.assertIsNone(run_hook("claude", "prompt", claude_prompt("s1", self.repo, "allow git discard")).data)
        self.assertIsNone(run_hook("claude", "shell", claude_shell("s1", self.repo, "git stash")).data)
        self.assertIsNotNone(run_hook("claude", "shell", claude_shell("s2", self.repo, "git stash")).data)

    def test_allow_phrase_inside_a_sentence_does_not_grant(self) -> None:
        run_hook("claude", "prompt", claude_prompt("s1", self.repo, "please allow git discard for now"))
        self.assertIsNotNone(run_hook("claude", "shell", claude_shell("s1", self.repo, "git stash")).data)

    def test_subagent_inherits_the_parent_grant(self) -> None:
        run_hook("claude", "prompt", claude_prompt("parent", self.repo, "cho phép git xoá"))
        child = claude_shell("child", self.repo, "git stash", parent_id="parent")
        self.assertIsNone(run_hook("claude", "shell", child).data)

    def test_session_end_revokes_the_grant(self) -> None:
        run_hook("claude", "prompt", claude_prompt("s1", self.repo, "allow git discard"))
        run_hook("claude", "session_end", {"session_id": "s1", "cwd": str(self.repo)})
        self.assertIsNotNone(run_hook("claude", "shell", claude_shell("s1", self.repo, "git stash")).data)

    def test_destructive_command_stays_blocked_when_the_ledger_is_unusable(self) -> None:
        broken = self.base / "not-a-database"
        broken.mkdir()
        with mock.patch.dict(os.environ, {"LANEKEEPER_DB": str(broken)}):
            run = run_hook("claude", "shell", claude_shell("s1", self.repo, "git stash"))
        self.assertEqual(run.code, 0)
        assert run.data is not None
        self.assertEqual(run.data["hookSpecificOutput"]["permissionDecision"], "deny")

    # ---------------------------------------------------------------- fail-open

    def test_malformed_payload_fails_open_and_is_logged(self) -> None:
        run = run_hook("claude", "shell", raw="{not json")
        self.assertEqual((run.code, run.stdout), (0, ""))
        con = ledger.connect()
        try:
            count = con.execute("SELECT count(*) FROM errors WHERE at='hook payload'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 1)

    def test_unknown_agent_is_a_usage_error(self) -> None:
        run = run_hook("nope", "shell", {})
        self.assertEqual(run.code, 1)

    # ---------------------------------------------------------------- edits

    def test_same_file_warning_and_notice_to_the_other_session(self) -> None:
        target = self.repo / "a.txt"
        first = run_hook("claude", "edit", claude_edit("sess-aaaa0001", self.repo, target,
                                                       call_id="ca", intent="refactor header"))
        self.assertIsNone(first.data)

        second = run_hook("claude", "edit", claude_edit("sess-bbbb0002", self.repo, target, call_id="cb"))
        assert second.data is not None
        context = second.data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("same file, same directory", context)
        self.assertIn("refactor header", context)

        notice = run_hook("claude", "prompt", claude_prompt("sess-aaaa0001", self.repo, "continue"))
        assert notice.data is not None
        self.assertIn("just edited the same file a.txt",
                      notice.data["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(run_hook("claude", "prompt", claude_prompt("sess-aaaa0001", self.repo, "more")).data)

    # ---------------------------------------------------------------- stop and greeting

    def test_stop_reports_owned_uncommitted_work_once(self) -> None:
        self.leave_uncommitted("sess-stop0001")
        stop = run_hook("claude", "stop", {"session_id": "sess-stop0001", "cwd": str(self.repo)}).data
        assert stop is not None
        self.assertIn("1 uncommitted file(s)", stop["systemMessage"])
        self.assertIn("This session left unfinished work", stop["systemMessage"])
        self.assertIsNone(run_hook("claude", "stop", {"session_id": "sess-stop0001", "cwd": str(self.repo)}).data)

    def test_new_session_is_told_about_unfinished_work_in_the_folder(self) -> None:
        self.leave_uncommitted("sess-old00001")
        run_hook("claude", "stop", {"session_id": "sess-old00001", "cwd": str(self.repo)})
        greeting = run_hook("claude", "session_start", {"session_id": "sess-new00002",
                                                        "cwd": str(self.repo)}).data
        assert greeting is not None
        self.assertIn("Unfinished work from other sessions in this folder", greeting["systemMessage"])

    # ---------------------------------------------------------------- scan scheduling

    def test_schedule_scan_is_rate_limited(self) -> None:
        calls: list[str] = []
        os.environ.pop("LANEKEEPER_NO_SCAN", None)
        with mock.patch.object(core, "SCAN_SPAWNER", calls.append):
            self.assertTrue(core.schedule_scan(str(self.repo)))
            self.assertFalse(core.schedule_scan(str(self.repo)))
        self.assertEqual(calls, [str(self.repo)])

    def test_schedule_scan_respects_the_disable_switch(self) -> None:
        calls: list[str] = []
        os.environ["LANEKEEPER_NO_SCAN"] = "1"
        with mock.patch.object(core, "SCAN_SPAWNER", calls.append):
            self.assertFalse(core.schedule_scan(str(self.repo)))
        self.assertEqual(calls, [])

    # ---------------------------------------------------------------- pure helpers

    def test_extract_files_reads_patch_headers_and_omp_anchors(self) -> None:
        patch = ("*** Begin Patch\n*** Update File: src/a.ts\n@@\n-x\n+y\n"
                 "*** Add File: docs/new.md\n*** End Patch")
        files = core.extract_files("apply_patch", {"input": patch}, str(self.repo))
        root = ledger.norm(self.repo)
        self.assertEqual(files, [f"{root}/src/a.ts", f"{root}/docs/new.md"])

        anchored = core.extract_files("edit", {"input": "[src/b.ts#ABCD]\n+1 x"}, str(self.repo))
        self.assertEqual(anchored, [f"{root}/src/b.ts"])

    def test_extract_files_ignores_content_and_urls(self) -> None:
        content = "*** Begin Patch\n*** Update File: evil.ts"
        files = core.extract_files("Write", {"file_path": "x.md", "content": content}, str(self.repo))
        self.assertEqual(files, [ledger.norm(os.path.join(str(self.repo), "x.md"))])
        self.assertEqual(core.extract_files("Fetch", {"path": "https://example.test/a"}, str(self.repo)), [])

    def test_command_names_drop_arguments_and_secrets(self) -> None:
        self.assertEqual(core.command_names("git push origin main && rm -rf build"), "git push && rm build")
        self.assertNotIn("abc123", core.command_names("export TOKEN=abc123 && make") or "")


if __name__ == "__main__":
    unittest.main()
