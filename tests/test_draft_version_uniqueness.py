"""Draft version uniqueness, including when ``job_id`` is NULL.

Defect (Gate 1 correctness audit): ``draft`` declared ``UNIQUE (ws_conv_id, job_id,
version)``, but SQLite treats every NULL as distinct, so the constraint silently did
nothing for a draft created without a job — two drafts with the same idempotency key
(an explicitly supplied version for the same conversation/job scope) could both exist.
``effects.create_draft`` computed the version with ``COALESCE(job_id, '')``, so the
guard only ever held on the code path that happened to have a job.

The fix is in the schema and the persistence layer, not only in that one call path
(PRD §12 immutable, append-only draft versions; §10 the approval binds to an exact
``draft_version``; T13/T14).
"""

from __future__ import annotations

import sqlite3
import unittest

from grace import contracts as C
from tests.helpers import GraceTestCase


class TestDraftVersionUniqueness(GraceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.ws = self.alex_ws()
        self.destination = self.svc._default_destination(self.ws)
        self.assertTrue(self.destination)

    def _create(self, *, job_id: str | None, version: int, body: str = "MOCK draft body") -> object:
        return self.svc.effects.create_draft(
            ws_conv_id=self.ws, job_id=job_id, destination_conv_id=self.destination,
            mode="reply", subject="Re: order status", body=body,
            purpose="reply to the owner's instruction", version=version,
            origin=C.MOCK, mock_label=C.mock_label("fixtures"))

    def _draft_rows(self, job_id: str | None) -> list[dict]:
        return self.svc.store.all(
            "SELECT * FROM draft WHERE ws_conv_id = ? AND COALESCE(job_id, '') = COALESCE(?, '') "
            "AND version = 1", (self.ws, job_id))

    def test_two_drafts_with_the_same_key_and_no_job_cannot_both_exist(self) -> None:
        first = self._create(job_id=None, version=1)
        self.assertTrue(first.ok, first.detail)
        second = self._create(job_id=None, version=1)
        self.assertTrue(second.code in (C.IDEMPOTENT_REPLAY, C.CONFLICT, C.ALREADY_IN_STATE),
                        f"a duplicate version must be a typed refusal, got {second.code}")
        self.assertEqual(second.data["draft_id"], first.data["draft_id"])
        rows = self._draft_rows(None)
        self.assertEqual(len(rows), 1, "exactly one draft row may exist for that version")

    def test_a_different_version_or_body_is_still_a_new_draft(self) -> None:
        """The guard must block the duplicate version, not legitimate new versions."""
        first = self._create(job_id=None, version=1)
        second = self._create(job_id=None, version=2, body="MOCK second version")
        self.assertTrue(first.ok and second.ok, (first.detail, second.detail))
        self.assertNotEqual(first.data["draft_id"], second.data["draft_id"])
        versions = [row["version"] for row in self.svc.store.all(
            "SELECT version FROM draft WHERE ws_conv_id = ? AND job_id IS NULL ORDER BY version",
            (self.ws,))]
        self.assertEqual(versions, [1, 2])

    def test_the_persistence_layer_refuses_the_duplicate_without_the_service(self) -> None:
        """The schema enforces it: a raw insert of the same key raises IntegrityError."""
        created = self._create(job_id=None, version=1)
        row = self.svc.store.one("SELECT * FROM draft WHERE draft_id = ?",
                                 (created.data["draft_id"],))
        duplicate = {key: value for key, value in row.items() if key != "draft_id"}
        duplicate["draft_id"] = C.new_id("draft")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.svc.store.tx():
                self.svc.store.insert_row("draft", duplicate)

    def test_the_unique_index_covers_a_null_job_id(self) -> None:
        """Named evidence: the expression index exists and treats NULL as one scope."""
        sql = self.svc.store.scalar(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("ux_draft_version_scope",))
        self.assertIsNotNone(sql, "the draft version scope index is missing")
        self.assertIn("COALESCE", sql.upper())
        self.assertIn("job_id", sql.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
