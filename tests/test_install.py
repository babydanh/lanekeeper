"""Installing, merging, and removing lanekeeper hooks in each agent's config."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import isolate, make_repo

from lanekeeper import __version__, install
from lanekeeper.hooks import claude


def commands_in(doc: dict) -> list[str]:
    found: list[str] = []
    for entries in doc.get("hooks", {}).values():
        for entry in entries:
            if isinstance(entry.get("command"), str):
                found.append(entry["command"])
            for hook in entry.get("hooks", []):
                if isinstance(hook.get("command"), str):
                    found.append(hook["command"])
    return found


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = isolate(self)
        self.home = Path(os.environ["HOME"])
        self.settings = self.home / ".claude" / "settings.json"

    def run_install(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = install.main(list(argv))
        return code, buf.getvalue()

    def test_install_twice_keeps_one_copy_per_event(self) -> None:
        self.assertEqual(self.run_install("claude")[0], 0)
        first = self.settings.read_text(encoding="utf-8")
        self.assertEqual(self.run_install("claude")[0], 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), first)
        ours = [c for c in commands_in(json.loads(first)) if "-m lanekeeper hook claude " in c]
        self.assertEqual(len(ours), len(claude.SPEC))

    def test_foreign_hooks_survive_install_and_uninstall(self) -> None:
        self.settings.parent.mkdir(parents=True)
        original = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "my-tool check"}]}]}}
        self.settings.write_text(json.dumps(original), encoding="utf-8")

        self.assertEqual(self.run_install("claude")[0], 0)
        backup = self.settings.with_name("settings.json.bak")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)
        self.assertIn("my-tool check", commands_in(json.loads(self.settings.read_text(encoding="utf-8"))))

        self.assertEqual(self.run_install("claude", "--uninstall")[0], 0)
        remaining = commands_in(json.loads(self.settings.read_text(encoding="utf-8")))
        self.assertEqual(remaining, ["my-tool check"])

    def test_dry_run_writes_nothing(self) -> None:
        code, out = self.run_install("claude", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[dry-run]", out)
        self.assertFalse(self.settings.exists())

    def test_uninstall_when_nothing_is_installed(self) -> None:
        code, out = self.run_install("claude", "--uninstall")
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out)

    def test_invalid_json_is_refused_and_left_alone(self) -> None:
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{bad", encoding="utf-8")
        code, _ = self.run_install("claude")
        self.assertEqual(code, 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{bad")

    def test_project_scope_writes_under_the_repo_root(self) -> None:
        repo, _ = make_repo(self, with_origin=False)
        previous = Path.cwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(self.run_install("claude", "--project")[0], 0)
        self.assertTrue((repo / ".claude" / "settings.json").exists())
        self.assertFalse(self.settings.exists())

    def test_cursor_uses_flat_entries_and_version_one(self) -> None:
        self.assertEqual(self.run_install("cursor")[0], 0)
        doc = json.loads((self.home / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["version"], 1)
        entry = doc["hooks"]["beforeShellExecution"][0]
        self.assertIn("-m lanekeeper hook cursor shell", entry["command"])
        self.assertEqual(entry["timeout"], install.DEFAULT_TIMEOUT_S)

    def test_gemini_timeouts_are_in_milliseconds(self) -> None:
        self.assertEqual(self.run_install("gemini")[0], 0)
        doc = json.loads((self.home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        hook = doc["hooks"]["BeforeTool"][0]["hooks"][0]
        self.assertEqual(hook["timeout"], install.DEFAULT_TIMEOUT_S * 1000)

    def test_codex_writes_its_own_hooks_file(self) -> None:
        self.assertEqual(self.run_install("codex")[0], 0)
        self.assertTrue((self.home / ".codex" / "hooks.json").exists())

    def test_omp_prints_instructions_and_writes_nothing(self) -> None:
        code, out = self.run_install("omp")
        self.assertEqual(code, 0)
        self.assertIn("lanekeeper.ts", out)
        self.assertIn("LANEKEEPER_PYTHON", out)
        self.assertFalse(any(self.home.iterdir()))

    def test_installation_is_recorded_and_forgotten_on_uninstall(self) -> None:
        self.run_install("claude")
        records = install.load_records()
        self.assertEqual(records[str(self.settings)],
                         {"agent": "claude", "scope": "user", "version": __version__})
        self.run_install("claude", "--uninstall")
        self.assertNotIn(str(self.settings), install.load_records())


if __name__ == "__main__":
    unittest.main()
