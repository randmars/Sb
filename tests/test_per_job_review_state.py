"""Per-job review state: two jobs in one conversation must not overwrite each other.

Defect (Gate 1 correctness audit): review / needs-me / working state was derived one
row per *workspace conversation* (``workspace_conversation.queue_state``,
``review_state``, ``needs_me_reason``), so a second job in the same conversation
overwrote the first job's state and one job's draft disappeared from the surfaces.
The PRD keeps these states independent per unit of work (PRD §5, §12; R04, R12) and
T04/T05/T12 require five independent delegations, several results returned to one
conversation, and concurrent input on a job to stay tellable apart.

These tests pin the fix: the state is derived **per job**, a conversation carries
several concurrent jobs, its own review items and its own failures, and each job
reaches its own review page.
"""

from __future__ import annotations

import unittest

from tests.helpers import GraceTestCase
from tests.test_web_ui import WebCase


class TwoJobsInOneConversation(GraceTestCase):
    """Service-level: the ledger keeps one state per job, not one per conversation."""

    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def _two_draft_jobs(self) -> dict:
        """Two independent jobs in ONE source conversation, each producing its own draft."""
        ws = self.alex_ws()
        first = self.svc.assign(ws, "Check the order status and draft a reply.", "researcher")
        second = self.svc.assign(ws, "Draft a separate note about the invoice.", "accounts_agent")
        assert first.ok and second.ok, (first.detail, second.detail)
        ran_first = self.svc.run_job(first.data["job_id"])
        ran_second = self.svc.run_job(second.data["job_id"])
        assert ran_first.ok and ran_second.ok, (ran_first.detail, ran_second.detail)
        return {
            "ws_conv_id": ws,
            "jobs": [first.data["job_id"], second.data["job_id"]],
            "drafts": [ran_first.data["draft"]["draft_id"], ran_second.data["draft"]["draft_id"]],
        }

    def test_each_job_keeps_its_own_review_state(self) -> None:
        fixture = self._two_draft_jobs()
        rows = {row["job_id"]: row for row in self.svc.store.all(
            "SELECT * FROM job WHERE ws_conv_id = ?", (fixture["ws_conv_id"],))}
        self.assertEqual(set(rows), set(fixture["jobs"]))
        for job_id in fixture["jobs"]:
            with self.subTest(job_id=job_id):
                # Every job carries its own filter/review/needs-me state...
                self.assertEqual(rows[job_id]["queue_state"], "needs_me")
                self.assertEqual(rows[job_id]["review_state"], "awaiting_review")
                self.assertEqual(rows[job_id]["needs_me_reason"], "draft")
                self.assertEqual(rows[job_id]["job_state"], "ready_for_review")

    def test_both_jobs_appear_on_needs_me_as_separate_work_items(self) -> None:
        fixture = self._two_draft_jobs()
        items = [item for item in self.svc.needs_me()
                 if item["ws_conv_id"] == fixture["ws_conv_id"]]
        self.assertEqual(sorted(item["job_id"] for item in items),
                         sorted(fixture["jobs"]),
                         "both jobs in one conversation must be listed, not just the newest")
        for item in items:
            with self.subTest(job_id=item["job_id"]):
                self.assertEqual(item["queue_state"], "needs_me")
                self.assertEqual(item["needs_me_reason"], "draft")
                self.assertEqual(item["review_state"], "awaiting_review")
                self.assertNotEqual(item["work_item_id"], None)

    def test_one_jobs_state_survives_the_other_job_finishing(self) -> None:
        """The defect in its purest form: the second job's transition must not clear the first."""
        fixture = self._two_draft_jobs()
        first, second = fixture["jobs"]
        # The second job is cancelled; the first still needs the owner.
        cancelled = self.svc.cancel_job(second, "not needed after all")
        self.assertTrue(cancelled.ok, cancelled.detail)
        self.assertEqual(self.svc.store.one(
            "SELECT job_state FROM job WHERE job_id = ?", (second,))["job_state"], "cancelled")
        still_waiting = self.svc.store.one(
            "SELECT queue_state, review_state, needs_me_reason FROM job WHERE job_id = ?", (first,))
        self.assertEqual(still_waiting["queue_state"], "needs_me")
        self.assertEqual(still_waiting["review_state"], "awaiting_review")
        items = [item["job_id"] for item in self.svc.needs_me()
                 if item["ws_conv_id"] == fixture["ws_conv_id"]]
        self.assertEqual(items, [first])
        # ...and the conversation's own aggregate still reports the outstanding work.
        ws_row = self.svc.store.one(
            "SELECT * FROM workspace_conversation WHERE ws_conv_id = ?", (fixture["ws_conv_id"],))
        self.assertEqual(ws_row["queue_state"], "needs_me")
        self.assertEqual(ws_row["review_state"], "awaiting_review")

    def test_conversation_level_need_still_lists_when_no_job_needs_the_owner(self) -> None:
        """A conversation's own review item (an untriaged message) has no job and must stay."""
        untriaged = [item for item in self.svc.needs_me() if item["needs_me_reason"] == "untriaged_message"]
        self.assertTrue(untriaged)
        for item in untriaged:
            self.assertIsNone(item["job_id"])
            self.assertTrue(item["work_item_id"])


class TestWebSurfacesArePerJob(WebCase):
    """The review client shows one work item per job and a review page for each."""

    def _two_jobs(self) -> dict:
        ws = self.alex_ws()
        first = self.assign(ws, "Check the order status and draft a reply.", "researcher")
        second = self.assign(ws, "Draft a separate note about the invoice.", "accounts_agent")
        run_first = self.post(f"/api/jobs/{first['data']['job_id']}/run", {}).json
        run_second = self.post(f"/api/jobs/{second['data']['job_id']}/run", {}).json
        self.assertTrue(run_first["ok"], run_first)
        self.assertTrue(run_second["ok"], run_second)
        return {"ws_conv_id": ws, "jobs": [first["data"]["job_id"], second["data"]["job_id"]],
                "drafts": [run_first["data"]["draft"]["draft_id"],
                           run_second["data"]["draft"]["draft_id"]]}

    def test_both_jobs_are_on_needs_me_and_counts_agree(self) -> None:
        fixture = self._two_jobs()
        data = self.data(self.get("/api/needs-me"))
        items = [item for item in data["items"] if item["ws_conv_id"] == fixture["ws_conv_id"]]
        self.assertEqual(sorted(item["job_id"] for item in items), sorted(fixture["jobs"]))
        self.assertEqual(data["counts"]["needs_me"], len(data["items"]))
        # The whole-list count is conversation-level and must stay independent of that.
        self.assertIn("all", data["counts"])

    def test_each_job_reaches_its_own_review_page_with_its_own_draft(self) -> None:
        fixture = self._two_jobs()
        seen = {}
        for job_id, draft_id in zip(fixture["jobs"], fixture["drafts"]):
            with self.subTest(job_id=job_id):
                data = self.data(self.get(
                    f"/api/conversation/{fixture['ws_conv_id']}?job={job_id}"))
                conversation = data["conversation"]
                self.assertEqual(conversation["job_id"], job_id)
                self.assertEqual(conversation["review_state"], "awaiting_review")
                self.assertEqual([d["draft_id"] for d in data["drafts"]], [draft_id])
                self.assertEqual(conversation["draft"]["draft_id"], draft_id)
                self.assertTrue(data["jobs"], "the conversation's other jobs stay visible")
                seen[job_id] = conversation["draft"]["draft_id"]
        self.assertEqual(seen, dict(zip(fixture["jobs"], fixture["drafts"])),
                         "each job's review page shows that job's draft, not the newest one")

    def test_an_unknown_job_on_the_review_page_is_refused(self) -> None:
        fixture = self._two_jobs()
        payload = self.get(f"/api/conversation/{fixture['ws_conv_id']}?job=job_nope").json
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "not_found")

    def test_working_lists_each_assigned_job_separately(self) -> None:
        ws = self.alex_ws()
        self.assign(ws, "Wait for the courier to answer.", "researcher")
        self.assign(ws, "Check the invoice against the ledger.", "accounts_agent")
        data = self.data(self.get("/api/working"))
        items = [item for item in data["items"] if item["ws_conv_id"] == ws]
        self.assertEqual(len(items), 2)
        self.assertEqual(len({item["job_id"] for item in items}), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
