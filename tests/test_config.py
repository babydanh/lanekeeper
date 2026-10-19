"""Tests for .lanekeeper.toml loading and command-line overrides."""

from __future__ import annotations

import os
import tempfile
import unittest

from support import write

from lanekeeper import config
from lanekeeper.config import Config, ConfigError


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _toml(self, text: str) -> None:
        write(os.path.join(self.root, config.CONFIG_FILE), text)

    def test_missing_file_gives_defaults(self) -> None:
        self.assertEqual(config.load(self.root), Config())

    def test_all_keys_are_read(self) -> None:
        self._toml(
            'target = "trunk"\n'
            'protected = ["trunk", "release"]\n'
            'check = "npm test"\n'
            'gate = "make e2e"\n'
            "squash = false\n"
        )
        cfg = config.load(self.root)
        self.assertEqual(cfg.target, "trunk")
        self.assertEqual(cfg.protected, ("trunk", "release"))
        self.assertEqual(cfg.check, "npm test")
        self.assertEqual(cfg.gate, "make e2e")
        self.assertFalse(cfg.squash)

    def test_unknown_key_is_an_error(self) -> None:
        self._toml('chek = "npm test"\n')
        with self.assertRaisesRegex(ConfigError, "unknown key.*chek"):
            config.load(self.root)

    def test_wrong_type_is_an_error(self) -> None:
        self._toml('squash = "yes"\n')
        with self.assertRaisesRegex(ConfigError, "squash must be true or false"):
            config.load(self.root)

    def test_invalid_toml_is_an_error(self) -> None:
        self._toml("target = \n")
        with self.assertRaisesRegex(ConfigError, "invalid TOML"):
            config.load(self.root)

    def test_overrides_skip_none(self) -> None:
        base = Config(target="main", check="a", squash=True)
        merged = config.with_overrides(base, target="dev", check=None, squash=False)
        self.assertEqual(merged.target, "dev")
        self.assertEqual(merged.check, "a")
        self.assertFalse(merged.squash)


if __name__ == "__main__":
    unittest.main()
