"""Install and remove lanekeeper hooks in each agent's configuration file.

Idempotent: every install first removes earlier lanekeeper entries, so re-running keeps
exactly one copy per event, and entries written by other tools are left alone. The first
change to an existing file keeps the previous content as <file>.bak.

omp has no hooks file; `install omp` prints the steps to load the TypeScript adapter.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from lanekeeper import __version__, gitutil, ledger
from lanekeeper.hooks import claude, codex, cursor, gemini

HOOK_MODULES = {"claude": claude, "codex": codex, "gemini": gemini, "cursor": cursor}
AGENTS = ("claude", "codex", "gemini", "cursor", "omp")
CONFIG_REL: dict[str, Path] = {
    "claude": Path(".claude", "settings.json"),
    "codex": Path(".codex", "hooks.json"),
    "gemini": Path(".gemini", "settings.json"),
    "cursor": Path(".cursor", "hooks.json"),
}
DEFAULT_TIMEOUT_S = 15
SESSION_END_TIMEOUT_S = 3
OMP_ADAPTER_REL = Path("adapters", "omp", "lanekeeper.ts")


def config_for(agent: str, project: bool) -> tuple[Path, str]:
    """Config file for `agent`: the user file, or the project file under the repo root."""
    rel = CONFIG_REL[agent]
    if not project:
        return Path.home() / rel, "user"
    root = gitutil.out(Path.cwd(), "rev-parse", "--show-toplevel")
    return (Path(root) if root else Path.cwd()) / rel, "project"


def _command(agent: str, kind: str) -> str:
    """Hook command line. Forward slashes work in bash, cmd and PowerShell; quoted only if needed."""
    exe = Path(sys.executable).as_posix()
    if not re.fullmatch(r"[A-Za-z0-9_:/.+\-]+", exe):
        exe = f'"{exe}"'
    return f"{exe} -m lanekeeper hook {agent} {kind}"


def _entry(agent: str, matcher: str | None, kind: str) -> dict:
    seconds = SESSION_END_TIMEOUT_S if kind == "session_end" else DEFAULT_TIMEOUT_S
    command = _command(agent, kind)
    if agent == "cursor":
        entry: dict = {}
        if matcher:
            entry["matcher"] = matcher
        entry["command"] = command
        entry["timeout"] = seconds
        return entry
    hook = {
        "type": "command",
        "command": command,
        "timeout": seconds * 1000 if agent == "gemini" else seconds,
    }
    entry = {}
    if matcher:
        entry["matcher"] = matcher
    entry["hooks"] = [hook]
    return entry


def _is_ours(command: object, agent: str) -> bool:
    return isinstance(command, str) and re.search(rf"-m lanekeeper hook {agent} \S+", command) is not None


def _strip(doc: dict, agent: str) -> None:
    """Remove lanekeeper entries for `agent`, keeping every other hook untouched."""
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept: list = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            if agent == "cursor":
                if not _is_ours(entry.get("command"), agent):
                    kept.append(entry)
                continue
            inner = entry.get("hooks")
            if not isinstance(inner, list):
                kept.append(entry)
                continue
            rest = [h for h in inner if not (isinstance(h, dict) and _is_ours(h.get("command"), agent))]
            if not rest:
                continue
            kept.append(entry if len(rest) == len(inner) else {**entry, "hooks": rest})
        if len(kept) != len(entries):
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not hooks:
        doc.pop("hooks", None)


def _add(doc: dict, agent: str) -> None:
    hooks = doc.setdefault("hooks", {})
    if agent == "cursor":
        doc.setdefault("version", 1)
    for event, matcher, kind in HOOK_MODULES[agent].SPEC:
        hooks.setdefault(event, []).append(_entry(agent, matcher, kind))


# ---------------------------------------------------------------- install records

def records_path() -> Path:
    return ledger.home() / "installed.json"


def load_records() -> dict:
    """Map of config path -> {agent, scope, version} written by the last installs."""
    path = records_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_records(records: dict) -> None:
    path = records_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def hook_present(config: str, agent: str) -> bool:
    """Whether the config file still contains lanekeeper's hook command for `agent`."""
    try:
        text = Path(config).read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(rf"-m lanekeeper hook {agent} \S+", text) is not None


# ---------------------------------------------------------------- command

def _apply(agent: str, target: Path, scope: str, *, uninstall: bool, dry_run: bool) -> int:
    original = target.read_text(encoding="utf-8") if target.exists() else None
    try:
        doc = json.loads(original) if original and original.strip() else {}
    except ValueError as exc:
        print(f"lanekeeper: {target} is not valid JSON ({exc}); fix it first. Nothing written.", file=sys.stderr)
        return 1
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        print(f"lanekeeper: {target} has an unexpected shape. Nothing written.", file=sys.stderr)
        return 1
    if uninstall and original is None:
        print(f"nothing to remove: {target} does not exist")
        _forget(target)
        return 0

    _strip(doc, agent)
    if not uninstall:
        _add(doc, agent)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    if dry_run:
        verb = "remove lanekeeper hooks from" if uninstall else "install lanekeeper hooks into"
        print(f"[dry-run] would {verb} {target}:\n{text}")
        return 0

    if text == original:
        print(f"unchanged: {target}")
    else:
        if original is not None:
            target.with_name(target.name + ".bak").write_text(original, encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"{'removed lanekeeper hooks from' if uninstall else 'installed lanekeeper hooks into'} {target}")

    if uninstall:
        _forget(target)
    else:
        _remember(target, agent, scope)
        print("Start a new agent session for the hooks to load. Check with: lanekeeper doctor")
    return 0


def _remember(target: Path, agent: str, scope: str) -> None:
    records = load_records()
    records[str(target)] = {"agent": agent, "scope": scope, "version": __version__}
    _save_records(records)


def _forget(target: Path) -> None:
    records = load_records()
    if records.pop(str(target), None) is not None:
        _save_records(records)


def _omp(uninstall: bool) -> int:
    if uninstall:
        print("omp: delete lanekeeper.ts from omp's extensions directory.")
        return 0
    adapter = Path(__file__).resolve().parents[2] / OMP_ADAPTER_REL
    if adapter.exists():
        source = f"Copy {adapter}"
    else:
        source = f"Copy {OMP_ADAPTER_REL.as_posix()} from the source repository"
    print(
        "omp loads lanekeeper as a TypeScript extension, not from a hooks file.\n"
        f"1. {source} into omp's extensions directory, as lanekeeper.ts.\n"
        "2. The extension runs `python -m lanekeeper hook omp <event>`. If `python` on PATH is a\n"
        "   different interpreter from this one, set LANEKEEPER_PYTHON to:\n"
        f"   {sys.executable}\n"
        "3. Restart omp. Check later with: lanekeeper doctor"
    )
    return 0


def main(argv: Sequence[str]) -> int:
    """`lanekeeper install <agent> [--project] [--dry-run] [--uninstall]`. Returns an exit code."""
    parser = argparse.ArgumentParser(prog="lanekeeper install", description="Install lanekeeper hooks for an agent.")
    parser.add_argument("agent", choices=AGENTS)
    parser.add_argument("--project", action="store_true", help="write the project's config, not the user's")
    parser.add_argument("--dry-run", action="store_true", help="print the resulting config, write nothing")
    parser.add_argument("--uninstall", action="store_true", help="remove lanekeeper's entries")
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    if args.agent == "omp":
        return _omp(args.uninstall)
    target, scope = config_for(args.agent, args.project)
    return _apply(args.agent, target, scope, uninstall=args.uninstall, dry_run=args.dry_run)
