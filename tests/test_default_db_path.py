"""One authoritative default database path, documented and asserted.

Defect (Gate 1 correctness audit): the code defaulted to ``$SWITCHBOARD_DB`` or
``~/.switchboard/grace.sqlite3`` (``grace/store.py``), while the README told the reader
the default was ``.grace/grace.sqlite3`` and that ``grace serve`` runs "against the same
SQLite database ``.grace/grace.sqlite3``". Two different answers to "where is the
ledger?" is exactly the ambiguity that makes a recovery runbook wrong.

The fix: one default, defined once in ``grace.store``, overridable only by the
documented environment variable, documented in the README with the same literal, and
asserted here end to end (a CLI run with an empty HOME must write to exactly that path).
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from grace import cli, store
from grace.service import Grace
from tests.helpers import REPO_ROOT


class TestOneDefaultDatabasePath(unittest.TestCase):
    def test_every_code_path_uses_the_same_default(self) -> None:
        parser = cli._build_parser()
        self.assertEqual(parser.parse_args(["needs-me"]).db, store.DEFAULT_DB)
        self.assertEqual(
            inspect.signature(Grace.__init__).parameters["db_path"].default, store.DEFAULT_DB)
        self.assertEqual(inspect.signature(store.connect).parameters["path"].default,
                         store.DEFAULT_DB)
        self.assertEqual(store.Store.__init__.__defaults__[0], store.DEFAULT_DB)

    def test_the_default_is_the_documented_literal_when_the_override_is_unset(self) -> None:
        if os.environ.get(store.DEFAULT_DB_ENV):
            self.skipTest(f"{store.DEFAULT_DB_ENV} is set in this environment")
        self.assertEqual(store.DEFAULT_DB_DISPLAY, "~/.switchboard/grace.sqlite3")
        self.assertEqual(store.DEFAULT_DB,
                         str(Path(os.path.expanduser(store.DEFAULT_DB_DISPLAY))))

    def test_the_readme_documents_exactly_that_default(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text()
        self.assertIn(store.DEFAULT_DB_DISPLAY, readme,
                      "the README must name the default path exactly as the code defines it")
        self.assertIn(store.DEFAULT_DB_ENV, readme,
                      "the README must name the override variable")
        self.assertNotIn(".grace/grace.sqlite3", readme,
                         "the withdrawn default must not still be documented")

    def test_a_cli_run_with_no_db_flag_writes_to_the_documented_path(self) -> None:
        """End to end: HOME is the only thing that decides the default path."""
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if k != store.DEFAULT_DB_ENV}
            env["HOME"] = home
            proc = subprocess.run([sys.executable, "-m", "grace", "seed", "--reset"],
                                 cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            expected = Path(home) / ".switchboard" / "grace.sqlite3"
            self.assertTrue(expected.exists(),
                            f"the CLI must use {store.DEFAULT_DB_DISPLAY} ({expected})")

    def test_the_documented_override_wins_over_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            override = str(Path(tmp) / "override.sqlite3")
            env = dict(os.environ)
            env["HOME"] = str(home)
            env[store.DEFAULT_DB_ENV] = override
            proc = subprocess.run([sys.executable, "-m", "grace", "seed", "--reset"],
                                 cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(Path(override).exists())
            self.assertFalse((home / ".switchboard" / "grace.sqlite3").exists(),
                             "the documented override must replace the default, not shadow it")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
