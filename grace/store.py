"""SQLite store: connection, schema migration, labelling-enforcing writes, audit.

Design notes
------------
* One short transaction per operation. Nothing here ever holds a transaction open
  across a model call, a source call or a browser session (PRD §9). Callers use
  ``store.tx()`` and do all network-ish work outside it.
* ``insert_row``/``update_row`` enforce the mock-labelling invariant on the way in
  (PRD §12, Gate 1 "Mocks must be visibly labeled"). A mock value cannot be
  persisted without a ``MOCK:`` label.
* All timestamps written here are UTC ISO-8601 strings produced by contracts.now().
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from . import contracts as C

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB = os.environ.get("SWITCHBOARD_DB", str(Path.home() / ".switchboard" / "grace.sqlite3"))

# Tables that carry an origin/mock_label pair and therefore must be labelled.
LABELLED_TABLES = {
    "source_account", "capability", "source_conversation", "message_ref", "person",
    "identity_link", "person_merge", "grp", "workspace_conversation", "rule",
    "rule_run", "rule_run_item", "job", "job_input", "attempt", "session_binding",
    "draft", "attachment_ref", "approval", "effect_operation", "effect_attempt",
    "result", "receipt", "memory_fact", "relation", "sync_checkpoint", "outbox",
    "audit_event",
}


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


class Store:
    """Thin, explicit data-access layer. No ORM, no third-party dependency."""

    def __init__(self, path: str | Path = DEFAULT_DB, migrate: bool = True):
        self.path = str(path)
        self.conn = connect(path)
        self._tx_depth = 0
        if migrate:
            self.migrate()

    # ------------------------------------------------------------- lifecycle --
    def migrate(self) -> None:
        # executescript() manages its own transaction and implicitly commits, so this is
        # deliberately not wrapped in tx().
        self.conn.executescript(SCHEMA_PATH.read_text())
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Short transaction. Re-entrant via savepoints.

        Re-entrancy matters for correctness, not convenience: an ingest event must commit
        its dedup record and its projection together, so the projection helper runs inside
        the caller's transaction rather than in a second one. Nested ``tx()`` blocks use
        SAVEPOINTs and only the outermost block commits.
        """
        depth = self._tx_depth
        savepoint = None
        if depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            savepoint = f"sb_sp_{depth}"
            self.conn.execute(f"SAVEPOINT {savepoint}")
        self._tx_depth = depth + 1
        try:
            yield self.conn
        except BaseException:
            self._tx_depth = depth
            if savepoint is None:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            self._tx_depth = depth
            if savepoint is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")

    # ---------------------------------------------------------------- writes --
    @staticmethod
    def _check_labels(table: str, row: Mapping[str, Any]) -> None:
        """Enforce the mock-labelling invariant on the columns a statement touches."""
        if table not in LABELLED_TABLES:
            return
        if "origin" in row:
            C.assert_labelled(row["origin"], row.get("mock_label"), f"{table} write")
        elif "mock_label" in row and row["mock_label"] is not None:
            if not C.is_mock_label(row["mock_label"]):
                raise AssertionError(f"{table} write: mock_label must start with 'MOCK:'")

    @staticmethod
    def _require_origin(table: str, row: Mapping[str, Any]) -> None:
        if table in LABELLED_TABLES and "origin" not in row:
            raise AssertionError(f"{table}: a new row must declare origin mock|real")

    def insert_row(self, table: str, row: Mapping[str, Any]) -> None:
        self._require_origin(table, row)
        self._check_labels(table, row)
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

    def update_row(self, table: str, row: Mapping[str, Any], where: str,
                   where_args: Sequence[Any] = ()) -> int:
        self._check_labels(table, row)
        sets = ", ".join(f"{k} = ?" for k in row)
        cur = self.conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", tuple(row.values()) + tuple(where_args))
        return cur.rowcount

    def upsert_row(self, table: str, row: Mapping[str, Any], conflict_cols: Iterable[str]) -> None:
        self._require_origin(table, row)
        self._check_labels(table, row)
        cols = list(row.keys())
        marks = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in set(conflict_cols))
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks}) "
               f"ON CONFLICT({', '.join(conflict_cols)}) DO UPDATE SET {updates}")
        self.conn.execute(sql, tuple(row.values()))

    # ----------------------------------------------------------------- reads --
    def one(self, sql: str, args: Sequence[Any] = ()) -> Optional[dict]:
        row = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def scalar(self, sql: str, args: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, tuple(args)).fetchone()
        return None if row is None else row[0]

    # ---------------------------------------------------------------- audit --
    def audit(self, *, actor: str, operation: str, entity_kind: str, entity_id: str,
              reason: str = "", version_before: int | None = None,
              version_after: int | None = None, operation_id: str | None = None,
              details: Optional[Mapping[str, Any]] = None, origin: str = C.REAL,
              mock_label: str | None = None, within_tx: bool = False) -> str:
        """PRD §12 AuditEvent: actor, operation, version, reason, trace ref.

        ``details`` must never contain secrets or copied message bodies; callers
        only pass identifiers, states and hashes.
        """
        audit_id = C.new_id("audit")
        row = {
            "audit_id": audit_id, "at": C.now(), "actor": actor, "operation": operation,
            "entity_kind": entity_kind, "entity_id": entity_id,
            "version_before": version_before, "version_after": version_after,
            "operation_id": operation_id, "reason": reason,
            "trace_ref": C.new_id("trace"),
            "details_json": C.canonical_json(dict(details or {})),
            "secret_redacted": 1, "origin": origin, "mock_label": mock_label,
        }
        if within_tx:
            self.insert_row("audit_event", row)
        else:
            with self.tx():
                self.insert_row("audit_event", row)
        return audit_id

    # --------------------------------------------------------------- health --
    def health(self) -> dict:
        return {
            "db_path": self.path,
            "schema_version": self.scalar("SELECT value FROM schema_meta WHERE key='schema_version'"),
            "counts": {
                t: self.scalar(f"SELECT COUNT(*) FROM {t}")
                for t in ("source_account", "message_ref", "workspace_conversation", "job",
                          "draft", "approval", "effect_operation", "receipt", "outbox")
            },
        }
