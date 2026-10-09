"""An existing ledger gains the new column when this code opens it (Gate 2, R12).

``measurement_target`` is a column added to the ``capability`` table, which already exists in
every ledger written before it. SQLite's ``CREATE TABLE IF NOT EXISTS`` does nothing to a
table that already exists, so on a pre-existing ledger the column is added in place by
``ADDED_COLUMNS`` in ``grace/store.py`` -- or it is not there at all.

The lead asked for this to be **proved, not assumed** (2026-10-09): open a ledger built from
master's ``7c38fd6`` schema with this branch's code, then run ``probe-import``,
``source-health`` and ``health`` against it. These tests do the same thing without depending
on git history -- they strip every added column out of the current ``schema.sql``, which is
what an older release's ledger looks like -- and then:

* assert every added column is present after the ledger is opened, and that the rows already
  in it survived;
* assert the upgraded tables match a freshly created ledger column for column;
* drive the CLI over the upgraded ledger end to end: seed, import the real 38-row probe
  document, ``source-health``, ``health``;
* assert opening it again changes nothing (the upgrade is idempotent).

Nothing here contacts a source beyond the real-mode probe rows this Linux host answers, which
are typed refusals with ``real_source_connected: false``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "mini")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from grace import contracts as C                                          # noqa: E402
from grace.ingest import _capability_row, stored_probe_rows                 # noqa: E402
from grace.store import ADDED_COLUMNS, SCHEMA_PATH, Store                 # noqa: E402
from tests.helpers import GraceTestCase, run_cli, run_cli_raw          # noqa: E402
from tests.test_probe_manifest_self_measurement import (                  # noqa: E402
    real_probe_rows, wrap_with_jq, write_jsonl)


def schema_without_added_columns() -> str:
    """``schema.sql`` as a release that predates every added column wrote it.

    Each table's block is edited, not the whole file: ``evidence``/``permission`` and friends
    are common column names, and only the block that ADDED_COLUMNS names may lose them.
    """
    text = SCHEMA_PATH.read_text()
    for table, columns in ADDED_COLUMNS.items():
        marker = f"CREATE TABLE IF NOT EXISTS {table} ("
        start = text.index(marker)
        end = text.index(");", start)
        block = text[start:end]
        for name in columns:
            new_block = re.sub(rf"^ *{re.escape(name)} +[^\n]*\n", "", block, flags=re.M)
            if new_block == block:
                raise AssertionError(f"{table}.{name} is not in schema.sql to strip")
            block = new_block
        block = re.sub(r",\s*$", "", block)     # no dangling comma before the closing paren
        text = text[:start] + block + text[end:]
    # An index over a column that did not exist yet did not exist either (job.queue_state
    # carries one). Dropping those statements is what the older release's schema looked like.
    stripped = {name for columns in ADDED_COLUMNS.values() for name in columns}

    def keep_index(match: re.Match) -> str:
        statement = match.group(0)
        if any(re.search(rf"\b{re.escape(name)}\b", statement) for name in stripped):
            return ""
        return statement

    return re.sub(r"CREATE (?:UNIQUE )?INDEX[^;]*;", keep_index, text)


def build_ledger(path: Path, schema: str) -> None:
    """Create a ledger file from a schema text, with one labelled row already in it."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(schema)
        conn.execute(
            "INSERT INTO source_account (account_id, adapter, adapter_version, "
            "account_identity, host_role, display_name, health_state, permission_state, "
            "origin, mock_label) VALUES ('acct_pre', 'pre_release', '0', 'pre@example.test', "
            "'mini', 'MOCK: pre-release account', 'current', 'granted', 'mock', "
            "'MOCK:pre_release')")
        conn.execute(
            "INSERT INTO capability (account_id, name, supported, state, origin, mock_label) "
            "VALUES ('acct_pre', 'manifest', 0, 'unmeasured', 'documentation', "
            "'DOCUMENTATION:pre')")
        conn.commit()
    finally:
        conn.close()


def columns_of(path: str | Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return sorted(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
    finally:
        conn.close()


def tables_of(path: str | Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    finally:
        conn.close()


def remove_ledger(path: str | Path) -> None:
    """Delete a ledger and its write-ahead files, so nothing can survive into the next one."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


class TestALedgerFromAnEarlierRelease(GraceTestCase):
    """A ledger written before ``measurement_target`` existed still works after this branch."""

    def setUp(self) -> None:
        super().setUp()
        self.svc.close()                     # the base case opened a fresh ledger
        remove_ledger(self.db)
        self.schema = schema_without_added_columns()
        build_ledger(Path(self.db), self.schema)
        self.assertNotIn("measurement_target",
                         columns_of(self.db, "capability"),
                         "the fixture must really predate the column")

    def test_every_added_column_is_added_and_the_existing_rows_survive(self) -> None:
        store = Store(self.db)
        try:
            have = columns_of(self.db, "capability")
            for table, columns in ADDED_COLUMNS.items():
                with self.subTest(table=table):
                    present = columns_of(self.db, table)
                    for name in columns:
                        self.assertIn(name, present, f"{table}.{name} was not added")
            self.assertIn("measurement_target", have)
            row = store.one("SELECT * FROM capability WHERE account_id = 'acct_pre'")
            self.assertIsNotNone(row, "the pre-existing row was lost by the upgrade")
            self.assertEqual(row["name"], "manifest")
            self.assertEqual(row["origin"], "documentation")
            self.assertIsNone(row["measurement_target"],
                              "a row that never said what it measured must say nothing")
        finally:
            store.close()

    def test_the_upgraded_ledger_matches_a_freshly_created_one(self) -> None:
        Store(self.db).close()
        with tempfile.TemporaryDirectory() as tmp:
            fresh = str(Path(tmp) / "fresh.sqlite3")
            Store(fresh).close()
            self.assertEqual(tables_of(self.db), tables_of(fresh))
            for table in tables_of(fresh):
                with self.subTest(table=table):
                    self.assertEqual(columns_of(self.db, table), columns_of(fresh, table))

    def test_opening_an_upgraded_ledger_again_changes_nothing(self) -> None:
        Store(self.db).close()
        before = {t: columns_of(self.db, t) for t in tables_of(self.db)}
        store = Store(self.db)
        try:
            self.assertEqual({t: columns_of(self.db, t) for t in tables_of(self.db)}, before)
            self.assertEqual(store.scalar("SELECT COUNT(*) FROM capability"), 1)
        finally:
            store.close()


class TestTheCliOverAnUpgradedLedger(GraceTestCase):
    """The commands the pack tells Randy to run, on a ledger that predates the column."""

    def setUp(self) -> None:
        super().setUp()
        self.svc.close()
        remove_ledger(self.db)
        build_ledger(Path(self.db), schema_without_added_columns())

    def wrapped_real_run(self, tmp: str) -> tuple[str, str]:
        rows = real_probe_rows()
        jsonl = Path(tmp) / "probe-rows.jsonl"
        wrapped = Path(tmp) / "probe-import.json"
        write_jsonl(jsonl, rows)
        how = wrap_with_jq(jsonl, wrapped)
        return str(wrapped), how

    def account_capability_rows(self, account: str) -> dict[str, dict]:
        """This account's capability rows, keyed by name, as the review surfaces see them."""
        store = Store(self.db)
        try:
            return {row["name"]: _capability_row(row) for row in store.all(
                "SELECT c.*, a.adapter AS source FROM capability c JOIN source_account a "
                "ON a.account_id = c.account_id WHERE c.account_id = ?", (account,))}
        finally:
            store.close()

    def test_seed_source_health_health_and_probe_import_all_work(self) -> None:
        seeded = run_cli(self.db, "seed")
        self.assertTrue(seeded["ok"], seeded)
        sources = run_cli(self.db, "source-health")
        account = sources["data"]["sources"][0]["account_id"]
        self.assertTrue(account)
        health = run_cli(self.db, "health")
        honesty_before = health["data"]["capability_honesty"]
        self.assertGreaterEqual(honesty_before["rows"], 1)
        self.assertFalse(honesty_before["real_sources_connected"],
                         "nothing on this host contacted a source")
        before_rows = self.account_capability_rows(account)
        before_names = set(before_rows)
        self.assertGreaterEqual(len(before_names), 1,
                                "the seeded account carries capability rows of its own")
        with tempfile.TemporaryDirectory() as tmp:
            document, how = self.wrapped_real_run(tmp)
            run_rows = json.loads(Path(document).read_text())["rows"]
            names = {row["capability"] for row in run_rows}
            proc = run_cli_raw(self.db, "probe-import", "--file", document,
                               "--account", account)
        self.assertEqual(len(names), 38, how)
        # The seeded account already carries two of these names (health, manifest), so this
        # import is a supersession as well as an addition -- the case worth pinning.
        self.assertTrue(names & before_names, "the fixture must exercise a supersession")
        after = run_cli(self.db, "health")["data"]["capability_honesty"]
        # Rows are stored by (account_id, name): the ledger gains exactly one row per name
        # this run carried that the account did not already have, and loses none.
        self.assertEqual(after["rows"], honesty_before["rows"] + len(names - before_names),
                         "the ledger gained exactly the rows this run added")
        after_rows = self.account_capability_rows(account)
        self.assertEqual(set(after_rows), before_names | names,
                         "the account holds its own rows and every imported one, and no others")
        for row in run_rows:
            name = row["capability"]
            stored = after_rows[name]
            with self.subTest(imported=name, how=how):
                self.assertEqual(stored["origin"], row["origin"])
                self.assertEqual(stored["state"], row["state"])
                self.assertEqual(stored["supported"], bool(row["supported"]))
                self.assertEqual(stored["measurement_target"],
                                 row.get("measurement_target", C.MEASUREMENT_SOURCE))
                self.assertFalse(stored["values_from_source"],
                                 "no row of this run read a source")
                self.assertFalse(stored["real_source_connected"])
        for name in sorted(before_names - names):
            with self.subTest(untouched=name):
                self.assertEqual(after_rows[name], before_rows[name],
                                 "a row this run did not mention is unchanged")
        self.assertNotIn("?", after["statement"])
        self.assertIn("(manifest)", after["statement"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"]["imported"], 38, how)
        self.assertEqual(payload["data"]["problems"], [])
        self.assertNotIn("?", payload["data"]["provenance"]["statement"])
        worker = [row for row in after_rows.values()
                  if row["measurement_target"] == C.MEASUREMENT_WORKER]
        self.assertEqual([row["name"] for row in worker], ["manifest"],
                         "the marker is frozen to the single manifest capability")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
