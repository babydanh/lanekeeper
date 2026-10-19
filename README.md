# lanekeeper

[![CI](https://github.com/babydanh/lanekeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/babydanh/lanekeeper/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

**Run several AI coding agents on one repo at the same time without them clobbering each other's work.**

Claude Code, Codex CLI, Gemini CLI, Cursor and omp each get their own git worktree ("lane"). lanekeeper watches the lanes and:

1. **Warns an agent *before* it edits code another agent is changing** — including changes that are not committed yet — with the exact lines and enclosing function.
2. **Lands finished lanes through a local merge queue**: lock, rebase, run your checks, squash to one commit, push. No pull requests or hosted service needed.
3. **Blocks git commands that silently lose work** (`git stash`, `reset --hard`, `checkout .`, `clean -f`, …) unless *you* allow them.

Zero dependencies (Python ≥ 3.11 standard library + git). Works on Windows, macOS and Linux. [Tiếng Việt](README.vi.md)

```text
agent A (lane feat-a) ── edits src/api.ts ──┐
                                            ├─ lanekeeper: "lane feat-b is changing lines 40–58
agent B (lane feat-b) ── about to edit ─────┘   (in: export function createMatch) — avoid or coordinate"

lk merge ../feat-a --push   →  rebase · check · gate · 1 squashed commit · push   (others wait on the lock, not on your tests)
```

## Install

```bash
uv tool install git+https://github.com/babydanh/lanekeeper   # or: pipx install git+https://github.com/babydanh/lanekeeper
lanekeeper install claude         # writes hooks into ~/.claude/settings.json (merged, backed up)
lanekeeper install codex          # ~/.codex/hooks.json
lanekeeper install gemini         # ~/.gemini/settings.json
lanekeeper install cursor         # ~/.cursor/hooks.json
lanekeeper install omp            # prints where to copy adapters/omp/lanekeeper.ts
```

Add `--project` to write the repository's config instead of your user config, `--dry-run` to preview, `--uninstall` to remove only lanekeeper's entries. Re-running `install` never duplicates hooks.

## Daily use

```bash
git worktree add ../feat-a -b feat-a          # one lane per agent/task
# ... agents work, commit as often as they like (WIP commits are fine) ...

lk overlap .                                  # which lanes will conflict, where (also runs automatically after edits)
lk merge ../feat-a                            # rebase + checks, report READY — nothing pushed
lk merge ../feat-a --push                     # land it: one commit per lane on main
lk merge . --train "feat-a feat-b feat-c" --push   # land several lanes in one go, drop the ones that break
lk                                            # who is doing what right now
lk doctor                                     # health: hook errors, stale hooks, forgotten stashes, unfinished work
```

`lk` is an alias of `lanekeeper`.

## Configuration (optional)

`.lanekeeper.toml` at the repository root. Every key is optional:

```toml
target = "main"                 # branch lanes land on
protected = ["main", "master"]  # branches that are never treated as a lane
check = "npm test"              # fast check, run in the lane before taking the merge lock
gate = "npx playwright test"    # slow check (E2E, remote CI...), run once per landing with --push
squash = true                   # one commit per lane on the target branch
```

The gate gets `LANEKEEPER_BRANCH`, `LANEKEEPER_BASE`, `LANEKEEPER_HEAD` and `LANEKEEPER_CHANGED` (changed files, one per line), so it can run only the tests a lane affects or trigger a CI job and wait for it. Unknown keys are errors, so a typo cannot silently disable a check.

## How it works

| Piece | What it does |
|---|---|
| `lk overlap` | Snapshots every lane **including uncommitted and untracked files** into temporary commits (through a temporary index — your index and working tree are never touched), then runs `git merge-tree` for each pair of lanes and each lane against the target, with your merge drivers (e.g. [Mergiraf](https://mergiraf.org)). It is a prediction from the two final trees; `lk merge` replays commit by commit, and that rebase plus your checks is the final word. |
| Pre-edit warning | After each scan the lane → files map is cached in `.git/overlap-lanes.json`. When an agent is about to edit a file, the hook diffs the other lanes for that file (one `git diff -U0` per lane, in parallel, 5 s timeout) and tells the agent which lines are taken. |
| `lk merge` | Rebase (with rerere) and run `check` **outside** the lock, so other lanes are not blocked by your tests. Inside the lock it only re-runs checks if the target moved meanwhile. Conflicts produce a brief (both sides, line numbers, what each session was doing) and abort cleanly. Squashed commit titles prefer `feat`/`fix` subjects and skip reverted or `fixup!` commits. |
| `lk merge --train` | Assembles several lanes on the target in a temporary worktree, tests once, pushes once. A failing combination is bisected per lane; the culprits are dropped and the rest lands. |
| `lk check` | Pre-push gate: files you are about to push that another session is still editing, or that changed upstream. Any unexpected error exits 2 (fail closed). |
| Activity ledger | Hooks record which session edited which file in which lane (SQLite, `~/.lanekeeper/activity.db`), pass messages between sessions, and keep a list of unfinished work. No prompt text or file contents are stored. |

## Agent support

| | Claude Code | Codex CLI | Gemini CLI | Cursor | omp |
|---|---|---|---|---|---|
| Block destructive git | `PreToolUse` | `PreToolUse` | `BeforeTool` | `beforeShellExecution` | ✅ |
| Warn before editing a taken region | `PreToolUse` (`Edit`, `Write`) | `PreToolUse` (`apply_patch`, `Edit`, `Write`) | `BeforeTool` | `preToolUse` (`Write`) | ✅ |
| Activity ledger + overlap scan after edits | `PostToolUse` | `PostToolUse` | `AfterTool` | `afterFileEdit` | ✅ |
| Status | experimental | experimental | experimental | experimental | used daily |

**Experimental** means: the adapter follows the vendor's published hook reference (October 2026) and is tested with payloads from that reference, but has not yet been run inside the real agent. Reports and fixes are very welcome.

Limits worth knowing:
- Hooks **fail open**: if a hook crashes or gets a payload it does not understand, it logs the error (`lk doctor` shows it) and lets the agent continue, so a broken hook never blocks your work. The guard is a seatbelt, not a sandbox.
- The guard matches shell command text. It catches `git stash`, `git -C dir reset --hard`, chained commands and so on, but not aliases (`g stash`), scripts that call git internally, or other indirection.
- To let an agent run a blocked command in one session, type exactly `allow git discard` as your prompt.

## Compared to

| Project | Focus | Difference |
|---|---|---|
| [clash](https://github.com/clash-sh/clash) | Worktree conflict detection | lanekeeper also sees uncommitted work, warns before the edit with line ranges, and lands lanes |
| [mergetrain](https://github.com/yongjip/mergetrain) | Local merge queue | lanekeeper adds config-driven check/gate, squash per lane, train bisection, agent hooks |
| [bors-ng](https://github.com/bors-ng/bors-ng) / [kodiak](https://github.com/chdsbd/kodiak) | PR merge bots | Need pull requests and a hosted bot; lanekeeper runs on your machine against any git remote named `origin` (GitHub, GitLab, self-hosted) |
| [mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail) | Messaging + file leases for agents | lanekeeper records activity through hooks (agents do not have to call a tool) and uses git to detect real overlaps instead of leases |
| [vibe-kanban](https://github.com/BloopAI/vibe-kanban), [claude-squad](https://github.com/smtg-ai/claude-squad) | Launch and manage many agents | Complementary: they start agents in worktrees; lanekeeper keeps those worktrees from colliding and lands them |

## Development

```bash
git clone https://github.com/babydanh/lanekeeper && cd lanekeeper
python -m pip install -e .
python -m unittest discover -s tests
```

Tests use real git repositories with a local bare remote — no mocks.

## License

MIT
