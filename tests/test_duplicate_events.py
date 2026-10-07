"""Test (b): replaying a duplicate source event creates no duplicate job.

Requirement coverage: R08/R12/R14, PRD §6 ("ingest events at least once and make
projection updates idempotent"), PRD §7 ("record each rule-version and message-version
evaluation key so the same event cannot repeatedly create work"), T08 ("replay events,
paginate across duplicates ... projection converges without duplicate jobs"), T06.
"""

from __future__ import annotations

import unittest

from tests.helpers import GraceTestCase, run_cli


class DuplicateEventTest(GraceTestCase):
    def test_replayed_event_projects_once_and_creates_no_second_job(self) -> None:
        self.seed()
        account = self.account_id("mock_beeper")
        # A genuinely new source arrival: the next poll must project it exactly once.
        template = dict(next(m for m in self.svc.corpus["messages"]
                             if m["namespaced_id"].endswith("beeper-alex-2")))
        template.update({
            "msg_ref_id": "msg_late_arrival",
            "namespaced_id": template["namespaced_id"] + ":late",
            "provider_message_id": "beeper-alex-late",
            "source_time": "2026-10-07T06:00:00.000000Z",
            "revision": "1",
        })
        self.svc.corpus["messages"].append(template)
        before_jobs = self.svc.store.scalar("SELECT COUNT(*) FROM job")

        # The CLI poll path sees only the seeded events, so everything is a replay of
        # work the seed already ingested with the same durable dedup keys.
        cli_poll = run_cli(self.db, "sync", "--adapter", "mock_beeper", "--account", account,
                           "--limit", "10")
        self.assertEqual(0, cli_poll["data"]["projected"])
        self.assertGreaterEqual(cli_poll["data"]["duplicates"], 1)

        first = self.svc.ingest.sync("mock_beeper", account_id=account, limit=10)
        self.assertEqual(1, first.data["projected"], first.detail)
        self.assertGreaterEqual(first.data["duplicates"], 1)
        second = self.svc.ingest.sync("mock_beeper", account_id=account, limit=10)
        self.assertEqual(0, second.data["projected"])
        self.assertGreaterEqual(second.data["duplicates"], 2)

        self.assertEqual(1, self.svc.store.scalar(
            "SELECT COUNT(*) FROM message_ref WHERE provider_message_id = 'beeper-alex-late'"))
        self.assertEqual(before_jobs, self.svc.store.scalar("SELECT COUNT(*) FROM job"))
        # The dedup record is durable and counted, not silently dropped.
        seen = self.svc.store.one(
            "SELECT seen_count, projection_state FROM event_dedup ORDER BY seen_count DESC LIMIT 1")
        self.assertGreaterEqual(seen["seen_count"], 2)
        self.assertEqual("applied", seen["projection_state"])

    def test_future_rule_evaluation_key_cannot_create_work_twice(self) -> None:
        self.seed()
        rule = self.svc.rules.save(
            rule_id=None, owner="owner",
            scope={"account_ids": [], "audience_kinds": []},
            conditions={"all": [{"kind": "sender_identity",
                                 "value": "alex.rivera@example-mail.test"}]},
            explanation="For messages from Alex, ask the accounts agent to check the invoice.",
            action="prepare_draft", agent="accounts_agent",
            instruction_template="Check whether invoice #4471 has been paid and prepare a reply.",
            origin="mock", mock_label="MOCK:fixtures")
        self.assertTrue(rule.ok)
        message = self.svc.store.one(
            "SELECT * FROM message_ref WHERE namespaced_id LIKE '%mail-inv-2'")
        first = self.svc.rules.evaluate_arrival(message)
        self.assertTrue(first.ok, first.detail)
        second = self.svc.rules.evaluate_arrival(message)
        self.assertEqual("idempotent_replay", second.code)
        self.assertEqual(first.data["job_id"], second.data["job_id"])
        self.assertEqual(1, self.svc.store.scalar(
            "SELECT COUNT(*) FROM job WHERE rule_id = ?", (rule.data["rule_id"],)))
        # Repeat-safe preview/apply: applying the frozen set again creates nothing new.
        preview = self.svc.rules.preview(rule.data["rule_id"], rule.data["version"],
                                         bounds={"limit": 20})
        self.assertTrue(preview.ok)
        applied = self.svc.rules.apply(preview.data["rule_run_id"])
        self.assertTrue(applied.ok)
        before = self.svc.store.scalar("SELECT COUNT(*) FROM job")
        again = self.svc.rules.apply(preview.data["rule_run_id"])
        self.assertEqual(0, len(again.data["created"]))
        self.assertEqual(before, self.svc.store.scalar("SELECT COUNT(*) FROM job"))

    def test_owner_sent_messages_never_trigger_a_rule(self) -> None:
        self.seed()
        rule = self.svc.rules.save(
            rule_id=None, owner="owner", scope={},
            conditions={"all": [{"kind": "sender_domain", "domain": "example-mail.test"}]},
            explanation="Any message from the example-mail.test domain",
            action="prepare_draft", agent="accounts_agent",
            instruction_template="Prepare a reply.", origin="mock", mock_label="MOCK:fixtures")
        outbound = self.svc.store.one(
            "SELECT * FROM message_ref WHERE namespaced_id LIKE '%mail-alex-2'")
        result = self.svc.rules.evaluate_arrival(outbound)
        self.assertEqual("already_in_state", result.code)
        self.assertEqual(0, self.svc.store.scalar(
            "SELECT COUNT(*) FROM job WHERE rule_id = ?", (rule.data["rule_id"],)))


if __name__ == "__main__":
    unittest.main()
