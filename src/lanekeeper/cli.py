"""Command line entry point: `python -m lanekeeper` and the `lanekeeper` console script.

Own commands:
  merge     land a lane worktree on origin/<target> (see lanekeeper.merge)
  check     report overlaps before a push (see lanekeeper.check)
  overlap   scan lanes for files they will conflict on (see lanekeeper.overlap)

Forwarded to peer modules, which own their own argument parsing:
  who, file, conflicts, pending, session, send, stash, doctor   lanekeeper.who
  hook <agent> <event>                                          lanekeeper.hooks
  install <agent> [flags]                                       lanekeeper.install

Exit codes come from each command. Usage errors exit 1. An unexpected error exits 2, so a
failure never looks like success to a caller.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import NoReturn

from . import __version__, check, merge, overlap

SESSION_COMMANDS = ("file", "conflicts", "pending", "session", "send", "stash", "doctor")

EPILOG = """\
session tools (lanekeeper.who):
  who, file, conflicts, pending, session, send, stash, doctor
hooks and install:
  hook <agent> <event>        agent hook entry point (lanekeeper.hooks)
  install <agent> [flags]     wire lanekeeper into an agent (lanekeeper.install)
"""


class _UsageParser(argparse.ArgumentParser):
    """Argument errors exit 1 (not argparse's 2), which is reserved for failures."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    """Top-level parser (for --help and --version) and the per-command parsers that do the parsing."""
    parser = _UsageParser(
        prog="lanekeeper",
        description="Land and check parallel agent lanes on one shared git repository.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"lanekeeper {__version__}")
    subs = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands: dict[str, argparse.ArgumentParser] = {}

    merge_p = subs.add_parser("merge", help="land a lane worktree on origin/<target>")
    merge_p.add_argument("worktree", help="lane worktree to land (not the main checkout)")
    merge_p.add_argument("--target", metavar="BRANCH", help="branch to land on (default: config, else main)")
    merge_p.add_argument("--check", metavar="CMD", help="shell command run before the lock; must exit 0")
    merge_p.add_argument("--gate", metavar="CMD", help="shell command run once before the lock; needs --push")
    merge_p.add_argument("--push", action="store_true", help="push to origin/<target>; otherwise only report READY")
    merge_p.add_argument(
        "--wait", type=float, default=900.0, metavar="SECONDS",
        help="how long to wait for the merge lock (default: 900)",
    )
    merge_p.add_argument("--no-squash", action="store_true", help="land each commit instead of one squashed commit")
    merge_p.add_argument("--train", metavar="BRANCHES", help="space-separated branches to assemble and land together")
    commands["merge"] = merge_p

    check_p = subs.add_parser("check", help="report overlaps before pushing")
    check_p.add_argument("repo", help="checkout to check")
    check_p.add_argument("files", nargs="*", metavar="FILE", help="files this session edited")
    check_p.add_argument("--target", metavar="BRANCH", help="merge target branch, when pushing to one")
    commands["check"] = check_p

    overlap_p = subs.add_parser("overlap", help="scan lanes for files they will conflict on")
    overlap_p.add_argument("repo", nargs="?", default=".", help="any checkout of the repository (default: .)")
    overlap_p.add_argument("--lane", metavar="BRANCH", help="only report overlaps involving this lane")
    overlap_p.add_argument("--base", default="origin/main", metavar="REF", help="ref lanes are compared against")
    overlap_p.add_argument("--json", action="store_true", help="print the result as JSON")
    overlap_p.add_argument("--notify", action="store_true", help="post a notice to sessions on overlapping lanes")
    overlap_p.add_argument(
        "--budget", type=float, default=60.0, metavar="SECONDS",
        help="stop scanning after this many seconds (default: 60)",
    )
    commands["overlap"] = overlap_p

    return parser, commands


def _call(module_name: str, run: Callable[[ModuleType], int]) -> int:
    """Import a peer module on demand. A missing peer is reported, not crashed on."""
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise  # a dependency of the peer is missing: surface the real error
        print(f"lanekeeper: {module_name.rsplit('.', 1)[-1]} is not available yet", file=sys.stderr)
        return 1
    return run(module)


def _forward(argv: Sequence[str]) -> int | None:
    """Run a peer-owned command. Returns None when `argv` is not one of them."""
    head, rest = argv[0], list(argv[1:])
    if head == "who":
        return _call("lanekeeper.who", lambda module: module.main(rest))
    if head in SESSION_COMMANDS:
        return _call("lanekeeper.who", lambda module: module.main([head, *rest]))
    if head == "hook":
        return _call("lanekeeper.hooks", lambda module: module.main(rest))
    if head == "install":
        return _call("lanekeeper.install", lambda module: module.main(rest))
    return None


def _run(args: argparse.Namespace) -> int:
    if args.command == "merge":
        return merge.run(
            args.worktree,
            target=args.target,
            check=args.check,
            gate=args.gate,
            squash=False if args.no_squash else None,
            push=args.push,
            wait=args.wait,
            train=args.train,
        )
    if args.command == "check":
        return check.run(args.repo, args.files, target=args.target)
    return overlap.run(
        args.repo,
        lane=args.lane,
        base=args.base,
        as_json=args.json,
        send_notices=args.notify,
        budget=args.budget,
    )


def _configure_stdio() -> None:
    """Non-ASCII repo paths must print the same on Windows pipes as on UTF-8 terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one lanekeeper command and return its exit code."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    _configure_stdio()
    try:
        parser, commands = _build_parser()
        if not args_list:
            parser.print_help(sys.stderr)
            return 1
        forwarded = _forward(args_list)
        if forwarded is not None:
            return forwarded
        if args_list[0] in commands:
            args = commands[args_list[0]].parse_intermixed_args(args_list[1:])
            args.command = args_list[0]
            return _run(args)
        parser.parse_args(args_list)  # --help, --version, or an unknown command (exits here)
        parser.print_help(sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # fail closed: an unexpected error must never look like a clean run
        print(f"lanekeeper: error: {exc}", file=sys.stderr)
        return 2
