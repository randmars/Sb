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

SCHEMA_VERSION = 2
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: The one authoritative default ledger path. It is relative to the *service's* home, not
#: to whatever directory a command happens to be run from, because Grace is the always-on
#: headless service (PRD §1) and a working-directory default would silently create a second
#: ledger. ``SWITCHBOARD_DB`` is the only override; the README documents both.
DEFAULT_DB_ENV = "SWITCHBOARD_DB"
DEFAULT_DB_DISPLAY = "~/.switchboard/grace.sqlite3"
DEFAULT_DB = os.environ.get(DEFAULT_DB_ENV) or str(Path(os.path.expanduser(DEFAULT_DB_DISPLAY)))

#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` cannot add a
#: column to an existing database, so an existing ledger is upgraded in place here rather
#: than silently missing the new state.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    # The probe-row contract (probe-pack 00-TEMPLATE.md). A ledger written before these
    # columns existed must gain them, or a real Gate 2 measurement would be dropped.
    "capability": {
        "observed_version": "TEXT",
        "permission": "TEXT",
        "probe_assertion": "TEXT",
        "evidence": "TEXT",
        "values_from_source": "INTEGER",
        "real_source_connected": "INTEGER",
        # what a probe row measured: 'source' | 'worker' (defect fix, 2026-10-09)
        "measurement_target": "TEXT",
        "sourced_refs": "TEXT",
    },
    "workspace_conversation": {
        "archived_at": "TEXT",
        "archive_reason": "TEXT",
        "deleted_at": "TEXT",
        "deletion_reason": "TEXT",
    },
    "job": {
        "queue_state": "TEXT NOT NULL DEFAULT 'working'",
        "review_state": "TEXT NOT NULL DEFAULT 'none'",
        "needs_me_reason": "TEXT",
        "archived_at": "TEXT",
        "archive_reason": "TEXT",
        "deleted_at": "TEXT",
        "deletion_reason": "TEXT",
    },
}


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
        # The added columns go in FIRST. ``schema.sql`` creates indexes over columns that an
        # older ledger does not have yet (``ix_job_filter`` covers ``job.queue_state``), and
        # ``CREATE INDEX`` fails on a missing column -- so adding the columns after the script
        # could not open the very ledger this upgrade exists for. Idempotent both ways round:
        # the first call covers an existing table, the second covers anything the script just
        # created. Defect fixed 2026-10-09, proved on a pre-column ledger in
        # tests/test_ledger_upgrade.py.
        self._add_missing_columns()
        self.conn.executescript(SCHEMA_PATH.read_text())
        self._add_missing_columns()
        # Per-job triage state is derived state: recomputing it here is idempotent and
        # self-healing, and it backfills a ledger written before the column existed.
        self._backfill_filter_states()
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def _add_missing_columns(self) -> None:
        """Upgrade an existing ledger in place: CREATE TABLE IF NOT EXISTS adds nothing."""
        for table, columns in ADDED_COLUMNS.items():
            have = {row["name"] for row in self.all(f"PRAGMA table_info({table})")}
            if not have:
                continue  # table absent: schema.sql created nothing, nothing to upgrade
            for name, declaration in columns.items():
                if name not in have:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def _backfill_filter_states(self) -> None:
        """Derive ``job.queue_state``/``review_state``/``needs_me_reason`` from job_state.

        The mapping in :data:`contracts.JOB_FILTER_STATES` is a pure function of the
        lifecycle state, so applying it at open time cannot contradict the ledger — it can
        only repair a row that was written before per-job triage state existed.
        """
        with self.tx():
            for job_state, (queue_state, review_state, reason) in C.JOB_FILTER_STATES.items():
                self.conn.execute(
                    "UPDATE job SET queue_state = ?, review_state = ?, needs_me_reason = ? "
                    "WHERE job_state = ?", (queue_state, review_state, reason, job_state))
            # A conversation's own state is the aggregate of its jobs. Conversations with
            # no job keep whatever they have (an untriaged message has no job to derive
            # from, and that state is not this function's to invent).
            conversations = [row["ws_conv_id"] for row in self.all(
                "SELECT DISTINCT ws_conv_id FROM job")]
        for ws_conv_id in conversations:
            # Opening the service must not look like activity: the backfill derives state
            # only, and leaves ``updated_at`` (which drives list order and "age") alone.
            self.recompute_conversation_filter_state(ws_conv_id, touch_updated_at=False)

    def recompute_conversation_filter_state(self, ws_conv_id: str, *,
                                            touch_updated_at: bool = True,
                                            at: Optional[str] = None) -> Optional[dict]:
        """Aggregate a conversation's own triage state from its live work items.

        Most urgent job wins: needs_me beats working beats idle, and the reported reason
        is the most urgent one present (``contracts.most_urgent_reason``). Archived and
        deleted jobs do not drive the conversation's state — they are out of triage.
        """
        jobs = self.all(
            "SELECT job_state, queue_state, review_state, needs_me_reason FROM job "
            "WHERE ws_conv_id = ? AND archived_at IS NULL AND deleted_at IS NULL "
            "ORDER BY created_at DESC", (ws_conv_id,))
        if not jobs:
            return None
        at = at or C.now()
        if any(job["queue_state"] == C.QueueState.NEEDS_ME for job in jobs):
            active = [job for job in jobs if job["queue_state"] == C.QueueState.NEEDS_ME]
            reason = C.most_urgent_reason([job["needs_me_reason"] for job in active])
            review_state = next((job["review_state"] for job in active
                                 if job["needs_me_reason"] == reason), active[0]["review_state"])
            row = {"queue_state": C.QueueState.NEEDS_ME, "review_state": review_state,
                   "needs_me_reason": reason, "updated_at": at}
        elif any(job["queue_state"] == C.QueueState.WORKING for job in jobs):
            row = {"queue_state": C.QueueState.WORKING, "review_state": C.ReviewState.NONE,
                   "needs_me_reason": None, "updated_at": at}
        else:
            row = {"queue_state": C.QueueState.IDLE, "review_state": C.ReviewState.NONE,
                   "needs_me_reason": None, "updated_at": at}
        if not touch_updated_at:
            row.pop("updated_at")
        with self.tx():
            self.update_row("workspace_conversation", row, "ws_conv_id = ?", (ws_conv_id,))
        return row


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
            labelled = (C.is_mock_label(row["mock_label"])
                        or C.is_fixture_label(row["mock_label"])
                        or C.is_documentation_label(row["mock_label"]))
            if not labelled:
                raise AssertionError(
                    f"{table} write: mock_label must start with 'MOCK:', 'FIXTURE:' or "
                    f"'DOCUMENTATION:'")
        if table == "capability":
            # The probe pack's scope statement, enforced where the row is written: a
            # documentation read can never claim a capability, and a permission state must
            # come from the one closed vocabulary. Enforced here rather than only in the
            # import path so that no writer -- seed, import or a future adapter -- can put
            # an over-claiming capability row in the ledger.
            if not C.probe_row_supported_claim_allowed(row):
                raise AssertionError(
                    "capability write: origin 'documentation' may never be stored with "
                    "supported truthy (Gate 2 probe pack scope statement)")
            permission = row.get("permission")
            if permission is not None and permission not in C.PERMISSION_STATES:
                raise AssertionError(
                    f"capability write: permission {permission!r} is not one of "
                    f"{C.PERMISSION_STATES}")

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

    def try_insert_row(self, table: str, row: Mapping[str, Any]) -> bool:
        """Insert a row, returning False when a uniqueness constraint already holds it.

        Used where the schema — not the caller — owns an invariant (for example the
        ``(ws_conv_id, COALESCE(job_id,''), version)`` scope of a draft version). Reporting
        the conflict as ``False`` keeps the refusal typed instead of raising.
        """
        try:
            self.insert_row(table, row)
            return True
        except sqlite3.IntegrityError:
            return False

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
