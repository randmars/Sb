"""Honest failure states: a broken or unpermitted source must say so.

Requirement coverage: R09 (report freshness and coverage honestly), R15 ("expose
unsupported capabilities and failures"), PRD §6 health presentation and unavailable
content, PRD §13 ("a source that has stopped syncing should not remain green
indefinitely"), T09 (coverage and unavailable content are explicit; no false
complete-history claim), T10 (Mini app closes / permission revoked: honest capability
states), T20 (truthful disconnected-source states).

Nothing here claims a real source was reachable: the faults are injected into labelled
mock adapters, and the assertions are about the *product's* honesty, not about Mail or
Beeper.
"""

from __future__ import annotations

import unittest

from grace import contracts as C
from grace.adapters import default_adapters
from grace.service import Grace
from tests.helpers import GraceTestCase, run_cli


class DisconnectedSourceTest(GraceTestCase):
    faults = ("offline",)

    def test_offline_source_reports_a_typed_state_and_projects_nothing(self) -> None:
        self.seed()
        account = self.account_id("mock_beeper")
        outcome = self.svc.adapters["mock_beeper"].health(account)
        self.assertEqual("offline", outcome.code)
        self.assertTrue(outcome.mocked)
        self.assertTrue(C.is_mock_label(outcome.to_dict()["mock_label"]))
        self.assertIsNone(outcome.data)

        before = self.svc.store.scalar("SELECT COUNT(*) FROM message_ref")
        result = self.svc.ingest.sync("mock_beeper", account_id=account)
        self.assertEqual(0, result.data["projected"])
        self.assertFalse(result.data["cursor_advanced"])
        self.assertEqual("offline", result.data["outcome"]["code"])
        self.assertEqual(before, self.svc.store.scalar("SELECT COUNT(*) FROM message_ref"))
        # The account still shows its previous success, so freshness stays comparable.
        row = self.svc.store.one("SELECT * FROM source_account WHERE account_id = ?", (account,))
        self.assertIsNotNone(row["last_success_at"])


class PermissionDeniedTest(GraceTestCase):
    def test_a_denied_permission_is_a_capability_state_not_an_empty_list(self) -> None:
        self.seed()
        contacts = self.svc.store.one("SELECT * FROM source_account WHERE adapter = 'mock_contacts'")
        self.assertEqual("denied", contacts["permission_state"])
        self.assertEqual("permission_denied", contacts["health_state"])
        health = run_cli(self.db, "source-health")
        row = next(s for s in health["data"]["sources"] if s["adapter"] == "mock_contacts")
        self.assertEqual("permission_denied", row["health_state"])
        self.assertIn("permission", row["disclosure"].lower() + row["health_detail"].lower())
        # Reading through it returns a typed denial rather than an empty success.
        outcome = self.svc.adapters["mock_contacts"].enumerate(contacts["account_id"], "all")
        self.assertEqual("permission_denied", outcome.code)
        self.assertIsNone(outcome.data)


class PartialHistoryTest(GraceTestCase):
    # permission_override frees the contacts fixture account from its denied permission so the
    # change-history reset path can be observed; the denial itself is covered above.
    faults = ("partial_history", "token_reset", "permission_override")

    def test_partial_history_and_token_reset_are_disclosed(self) -> None:
        self.seed()
        account = self.account_id("mock_mail")
        enumerated = self.svc.adapters["mock_mail"].enumerate(account, "inbox", limit=2)
        self.assertEqual("partial", enumerated.code)
        self.assertEqual("partial_history", enumerated.data["coverage"]["coverage_state"])
        self.assertIn("accessible result set", enumerated.data["coverage"]["gap_reason"])

        contacts = self.svc.adapters["mock_contacts"]
        contacts_account = self.account_id("mock_contacts")
        poll = contacts.change_poll(contacts_account)
        self.assertEqual("partial", poll.code)
        self.assertTrue(poll.data["reset"])
        self.assertEqual("rebuild", poll.data["projection_required"])

        coverage = run_cli(self.db, "coverage")
        self.assertTrue(coverage["data"]["checkpoints"])
        for row in coverage["data"]["checkpoints"]:
            self.assertIn("MOCK", row["coverage_disclosure"])


class MissingContentTest(GraceTestCase):
    def test_missing_attachment_and_deleted_message_are_explicit(self) -> None:
        self.seed()
        account = self.account_id("mock_mail")
        attachment = self.svc.adapters["mock_mail"].materialize_attachment(
            account, "mock_mail:attachment:statement-2023-04.pdf")
        self.assertEqual("partial", attachment.code)
        self.assertIn("not materialised", attachment.detail)

        deleted_ref = self.svc.store.scalar(
            "SELECT namespaced_id FROM message_ref WHERE deleted_at_source = 1")
        tombstone = self.svc.adapters["mock_mail"].retrieve(account, deleted_ref)
        self.assertEqual("partial", tombstone.code)
        self.assertIn("deleted upstream", tombstone.detail)
        self.assertIsNone(tombstone.data["body"])

        # An empty body is never confused with a failed fetch: the states differ.
        states = {r["body_state"] for r in self.svc.store.all("SELECT body_state FROM message_ref")}
        self.assertIn("not_fetched", states)
        self.assertIn("missing", states)
        self.assertIn("fetched", states)


class HealthHonestyTest(GraceTestCase):
    def test_service_health_never_claims_a_real_source(self) -> None:
        self.seed()
        payload = run_cli(self.db, "health")
        self.assertTrue(payload["mocked"])
        honesty = payload["data"]["capability_honesty"]
        self.assertFalse(honesty["real_sources_connected"])
        self.assertIn("Gate 2/3", honesty["explanation"])
        for adapter in payload["data"]["adapters"]:
            self.assertFalse(adapter["real_source_connected"])
            self.assertTrue(adapter["simulated"])

    def test_mock_hermes_runs_are_labelled_and_do_not_claim_agent_execution(self) -> None:
        run = self.assign_and_run()
        binding = self.svc.store.one("SELECT * FROM session_binding WHERE job_id = ?",
                                     (run["job_id"],))
        self.assertIsNotNone(binding)
        self.assertEqual("mock-profile", binding["hermes_profile"])
        self.assertTrue(binding["session_id"].startswith("mock-session-"))
        self.assertEqual("mock", binding["origin"])
        self.assertTrue(C.is_mock_label(binding["mock_label"]))
        summary = self.svc.store.one(
            "SELECT summary FROM result WHERE job_id = ? ORDER BY version LIMIT 1",
            (run["job_id"],))["summary"]
        self.assertIn("MOCK", summary)


if __name__ == "__main__":
    unittest.main()
