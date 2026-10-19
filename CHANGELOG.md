# Changelog

## 0.1.0 — unreleased

First public release.

- `lanekeeper merge`: local merge queue for worktree lanes — repository lock, rebase with rerere, `check` before the lock, one-shot `gate` before pushing, one squashed commit per lane, conflict briefs, `--train` to land several lanes at once and drop the ones that fail.
- `lanekeeper overlap`: conflict prediction between lanes and against the target branch, including uncommitted and untracked work, using `git merge-tree` on temporary snapshots.
- `lanekeeper check`: fail-closed pre-push gate for files other sessions are editing or that changed upstream.
- Agent hooks for Claude Code, Codex CLI, Gemini CLI, Cursor and omp: pre-edit warnings with line ranges, blocking of git commands that discard uncommitted work, activity ledger, messages between sessions, unfinished-work reminders.
- `lanekeeper install <agent>`: idempotent hook installation with backup and `--uninstall`.
- `lanekeeper who | doctor | stash | pending | send`: who is doing what, health checks, forgotten stashes.
