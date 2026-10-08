"""Archive is a state, not a delete.

Defect (Gate 1 correctness audit): there was no archive state at all, so an item could
only be "kept" or (implicitly) removed; nothing in the model distinguished an archived
conversation or job from a deleted/forgotten one.

The contract this pins (PRD §4/§8 — the source stays authoritative; R08 keeps derived
knowledge without a redundant full archive; PRD §12 independent states):

* an archived conversation or job **stays in storage** and is **retrievable**;
* it leaves the **default triage surfaces** (Needs me, Working);
* it is **distinguishable everywhere** from a deleted/forgotten item;
* archiving **never deletes a source row** — not a source conversation, not a message
  reference, not a job, draft, result or ledger link.
"""

from __future__ import annotations

import sqlite3
import unittest

from tests.helpers import GraceTestCase
from tests.test_web_ui import WebCase

#: Storage rows whose count an archive must not change. ``audit_event`` is deliberately
#: not in this list: archiving *records itself* in the ledger's audit trail, and that is the
#: only row an archive may add.
#:
#: ``input_request`` appeared in an earlier draft of this census and does not exist in this
#: release, in ``grace/schema.sql`` or anywhere in the code: the question an agent returns
#: and the owner's answer are both recorded as ``job_input`` versions (kind
#: 'answer'/'follow_up'/'information', PRD §5) with the question itself held as a ``result``
#: of kind 'question'. The census therefore names the tables the schema actually has —
#: including ``job_input``, which is the real row behind an input request — so that it can
#: only fail for a genuine reason rather than for a guess.
STORAGE_TABLES = ("source_conversation", "message_ref", "workspace_source_link",
                  "workspace_conversation", "job", "job_input", "job_transition", "attempt",
                  "draft", "result", "outbox", "session_binding")


class TestArchiveIsNotDelete(GraceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.ws = self.alex_ws()
        assigned = self.svc.assign(self.ws, "Check the order status and draft a reply.",
                                   "researcher")
        assert assigned.ok, assigned.detail
        self.job_id = assigned.data["job_id"]
        ran = self.svc.run_job(self.job_id)
        assert ran.ok, ran.detail
        self.draft_id = ran.data["draft"]["draft_id"]

    # -- helpers -----------------------------------------------------------
    def _counts(self) -> dict:
        return {table: self.svc.store.scalar(f"SELECT COUNT(*) FROM {table}")
                for table in STORAGE_TABLES}

    def _archive(self) -> dict:
        result = self.svc.ledger.archive_item(ws_conv_id=self.ws, reason="kept for reference",
                                             actor="owner")
        self.assertTrue(result.ok, result.detail)
        return result.data

    # -- the contract ------------------------------------------------------
    def test_an_archived_conversation_leaves_triage_and_stays_retrievable(self) -> None:
        data = self._archive()
        self.assertEqual(data["archive_state"], "archived")
        self.assertEqual(data["deletion_state"], "retained")
        self.assertFalse(data["source_rows_deleted"])

        # out of the default triage surfaces
        self.assertNotIn(self.ws, [item["ws_conv_id"] for item in self.svc.needs_me()])
        self.assertNotIn(self.ws, [item["ws_conv_id"] for item in self.svc.working()])
        self.assertEqual(self.svc.ledger.counts()["needs_me"], len(self.svc.needs_me()))

        # still stored and directly retrievable
        row = self.svc.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?",
                                 (self.ws,))
        self.assertIsNotNone(row["archived_at"])
        self.assertIsNone(row["deleted_at"])
        listed = {item["ws_conv_id"]: item for item in self.svc.all_conversations()}
        self.assertIn(self.ws, listed)
        self.assertEqual(listed[self.ws]["archive_state"], "archived")
        self.assertEqual(listed[self.ws]["deletion_state"], "retained")

    def test_archiving_deletes_no_source_row(self) -> None:
        before = self._counts()
        self._archive()
        after = self._counts()
        self.assertEqual(before, after, "archive must not remove storage rows")
        messages = self.svc.store.all(
            "SELECT msg_ref_id, read_state, hidden_state, mute_state, availability "
            "FROM message_ref WHERE conv_id IN (SELECT conv_id FROM workspace_source_link "
            "WHERE ws_conv_id = ?) ORDER BY msg_ref_id", (self.ws,))
        self.assertTrue(messages)
        for message in messages:
            self.assertEqual(message["hidden_state"], "visible")

    def test_archive_and_delete_are_distinguishable(self) -> None:
        archived = self._archive()
        self.assertEqual((archived["archive_state"], archived["deletion_state"]),
                         ("archived", "retained"))
        deleted = self.svc.ledger.delete_item(ws_conv_id=self.ws,
                                             reason="forgotten at the owner's request",
                                             actor="owner")
        self.assertTrue(deleted.ok, deleted.detail)
        self.assertEqual(deleted.data["deletion_state"], "deleted")
        self.assertEqual(deleted.data["archive_state"], "archived")
        self.assertTrue(deleted.data["recoverable"])
        # A deleted item leaves every list but is still in storage and still retrievable,
        # because deletion here is an application tombstone, never a source-row delete.
        self.assertNotIn(self.ws, [item["ws_conv_id"] for item in self.svc.all_conversations()])
        row = self.svc.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?",
                                 (self.ws,))
        self.assertIsNotNone(row["deleted_at"])
        self.assertIsNotNone(row["archive_reason"])
        self.assertNotEqual(row["archive_reason"], row["deletion_reason"])
        with_deleted = {item["ws_conv_id"]: item
                        for item in self.svc.all_conversations(include_deleted=True)}
        self.assertIn(self.ws, with_deleted)
        self.assertEqual(with_deleted[self.ws]["deletion_state"], "deleted")
        self.assertEqual(with_deleted[self.ws]["archive_state"], "archived")

    def test_unarchive_returns_the_item_to_triage(self) -> None:
        self._archive()
        restored = self.svc.ledger.unarchive_item(ws_conv_id=self.ws, reason="still open",
                                                 actor="owner")
        self.assertTrue(restored.ok, restored.detail)
        self.assertEqual(restored.data["archive_state"], "active")
        items = [item["job_id"] for item in self.svc.needs_me() if item["ws_conv_id"] == self.ws]
        self.assertEqual(items, [self.job_id])

    def test_a_job_can_be_archived_on_its_own(self) -> None:
        """Archiving one job in a conversation must not archive the conversation."""
        second = self.svc.assign(self.ws, "Draft a separate note about the invoice.",
                                 "accounts_agent")
        self.assertTrue(second.ok, second.detail)
        result = self.svc.ledger.archive_item(job_id=self.job_id, reason="handled elsewhere",
                                             actor="owner")
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.data["job_id"], self.job_id)
        self.assertEqual(
            self.svc.store.one("SELECT archived_at FROM job WHERE job_id = ?",
                               (self.job_id,))["archived_at"] is not None, True)
        ws_row = self.svc.store.one("SELECT archived_at FROM workspace_conversation "
                                   "WHERE ws_conv_id = ?", (self.ws,))
        self.assertIsNone(ws_row["archived_at"])
        listed = [item["job_id"] for item in self.svc.needs_me()
                  if item["ws_conv_id"] == self.ws]
        self.assertNotIn(self.job_id, listed)
        self.assertIn(second.data["job_id"], [item["job_id"] for item in self.svc.working()])


class TestArchiveOverTheReviewClient(WebCase):
    def _archive_route(self, ws: str, action: str, body: dict | None = None) -> dict:
        return self.post(f"/api/conversations/{ws}/{action}", body or {}).json

    def test_the_client_archives_and_shows_the_state_without_deleting(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        job_id = assigned["data"]["job_id"]
        self.post(f"/api/jobs/{job_id}/run", {})

        response = self._archive_route(ws, "archive", {"reason": "not urgent"})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"]["archive_state"], "archived")
        self.assertIn("archive is not deletion", response["retention_note"].lower())

        needs = self.data(self.get("/api/needs-me"))
        self.assertNotIn(ws, [item["ws_conv_id"] for item in needs["items"]])
        listing = {item["ws_conv_id"]: item for item in self.data(self.get("/api/all"))["items"]}
        self.assertIn(ws, listing)
        self.assertEqual(listing[ws]["archive_state"], "archived")
        self.assertEqual(listing[ws]["deletion_state"], "retained")
        self.assertTrue(listing[ws]["retention_note"])

        page = self.data(self.get(f"/api/conversation/{ws}"))
        self.assertEqual(page["conversation"]["archive_state"], "archived")
        self.assertEqual(page["retention"]["storage_state"], "retained")
        self.assertTrue(page["retention"]["note"])

        restored = self._archive_route(ws, "unarchive", {})
        self.assertTrue(restored["ok"], restored)
        needs = self.data(self.get("/api/needs-me"))
        self.assertIn(ws, [item["ws_conv_id"] for item in needs["items"]])

    def test_archiving_an_unknown_conversation_is_refused_typed(self) -> None:
        payload = self._archive_route("ws_nope", "archive", {"reason": "x"})
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "not_found")

    def test_the_client_bundle_shows_the_archived_state_and_a_restore_control(self) -> None:
        from tests.helpers import REPO_ROOT
        js = (REPO_ROOT / "grace" / "webui" / "app.js").read_text()
        self.assertIn("archive_state", js)
        self.assertIn("unarchive", js)
        self.assertIn("archived", js)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
