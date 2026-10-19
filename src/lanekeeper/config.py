"""Repository settings from `.lanekeeper.toml` at the repository root.

Every key is optional and falls back to the default below. Unknown keys and wrong types
are errors: a typo such as `chek = "..."` must not silently disable a check.

    target = "main"                 # branch that merges land on
    protected = ["main", "master"]  # branches a worktree may never merge from
    check = "npm test"              # shell command run in the worktree before pushing
    gate = ""                       # heavier shell command, run once before --push
    squash = true                   # land each branch as one commit
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from typing import Any

CONFIG_FILE = ".lanekeeper.toml"
_KNOWN_KEYS = frozenset({"target", "protected", "check", "gate", "squash"})


class ConfigError(ValueError):
    """`.lanekeeper.toml` cannot be parsed or holds an invalid value."""


@dataclass(frozen=True)
class Config:
    target: str = "main"
    protected: tuple[str, ...] = ("main", "master")
    check: str = ""
    gate: str = ""
    squash: bool = True


def load(repo_root: str | os.PathLike[str]) -> Config:
    """Read `<repo_root>/.lanekeeper.toml`. A missing file means all defaults."""
    path = os.path.join(os.fspath(repo_root), CONFIG_FILE)
    if not os.path.isfile(path):
        return Config()
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{CONFIG_FILE}: invalid TOML: {exc}") from exc
    return _from_mapping(data)


def with_overrides(config: Config, **values: Any) -> Config:
    """Return `config` with every non-None value applied. Command-line flags win over the file."""
    return replace(config, **{key: value for key, value in values.items() if value is not None})


def _from_mapping(data: dict[str, Any]) -> Config:
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise ConfigError(f"{CONFIG_FILE}: unknown key(s): {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for key in ("target", "check", "gate"):
        if key in data:
            values[key] = _string(key, data[key])
    if "target" in values and not values["target"]:
        raise ConfigError(f"{CONFIG_FILE}: target must not be empty")
    if "protected" in data:
        names = data["protected"]
        if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
            raise ConfigError(f"{CONFIG_FILE}: protected must be a list of branch names")
        values["protected"] = tuple(names)
    if "squash" in data:
        if not isinstance(data["squash"], bool):
            raise ConfigError(f"{CONFIG_FILE}: squash must be true or false")
        values["squash"] = data["squash"]
    return Config(**values)


def _string(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{CONFIG_FILE}: {key} must be a string")
    return value
