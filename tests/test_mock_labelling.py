"""Test (e): every mocked value is labelled as mock.

Requirement coverage: PRD §14 Gate 1 ("Mocks must be visibly labeled"), PRD §12
(origin/audit metadata), PRD §13 ("a clean deployment with no prior code must produce
a usable mock environment and truthful disconnected-source states"), T20.

The test deliberately tries to store an unlabelled mock value and an unlabelled mock
payload to prove the guard actually fires — a labelling rule nobody can fail is not a
rule.
"""

from __future__ import annotations

import json
import unittest

from grace import contracts as C
from grace.adapters import MockBeeperAdapter, MockMailAdapter, MockSourceAdapter, default_adapters
from grace.store import LABELLED_TABLES
from tests.helpers import GraceTestCase, run_cli

CLI_COMMANDS = (
    ("seed", "--reset"), ("needs-me",), ("working",), ("all",), ("counts",), ("health",),
    ("coverage",), ("source-health",), ("jobs",), ("audit",), ("scenarios",),
)


class LabellingTest(GraceTestCase):
    def test_every_mocked_database_row_carries_a_mock_label(self) -> None:
        self.seed()
        checked = 0
        for table in sorted(LABELLED_TABLES):
            rows = self.svc.store.all(f"SELECT origin, mock_label FROM {table}")
            for row in rows:
                self.assertIn(row["origin"], (C.MOCK, C.REAL), f"{table} has a bogus origin")
                if row["origin"] == C.MOCK:
                    self.assertTrue(C.is_mock_label(row["mock_label"]),
                                    f"{table} row is mocked but unlabelled: {row}")
                checked += 1
        self.assertGreater(checked, 100, "the fixture corpus should populate many labelled rows")
        # And the deployment is honest about which sources exist.
        self.assertEqual(4, self.svc.store.scalar(
            "SELECT COUNT(*) FROM source_account WHERE origin = 'mock'"))
        self.assertEqual(0, self.svc.store.scalar(
            "SELECT COUNT(*) FROM source_account WHERE origin = 'real'"))

    def test_cli_output_is_self_identifying_for_every_command(self) -> None:
        for args in CLI_COMMANDS:
            payload = run_cli(self.db, *args)
            self.assertTrue(payload["mocked"],
                            f"{args} produced output but did not declare it as mocked")
            self.assertIn("MOCK", payload["mock_label"])
            self.assertIn("No real Mail, Beeper, Contacts or Hermes source",
                          payload["mock_disclaimer"])
            self.assertNotIn("labelling_problems", payload)

    def test_adapter_results_are_labelled_and_deny_being_real(self) -> None:
        adapters = default_adapters(self.svc.corpus)
        for adapter in adapters.values():
            described = adapter.describe()
            self.assertTrue(described["simulated"])
            self.assertFalse(described["real_source_connected"])
            self.assertTrue(C.is_mock_label(described["mock_label"]))
            manifest = adapter.manifest().to_dict()
            self.assertTrue(manifest["simulated"])
            self.assertTrue(C.is_mock_label(manifest["mock_label"]))
            for cap in manifest["capabilities"].values():
                self.assertIn("mock", (cap["probe_method"] or "").lower())
        account = self.svc.corpus["accounts"][0]
        mailbox = MockMailAdapter(self.svc.corpus)
        outcome = mailbox.retrieve(account["account_id"],
                                   "mock_mail:%s:mail-alex-1" % account["account_id"])
        self.assertTrue(outcome.mocked)
        self.assertTrue(C.is_mock_label(outcome.to_dict()["mock_label"]))
        self.assertEqual("mock", outcome.to_dict()["origin"])
        self.assertIn("No real Mail", outcome.to_dict()["disclaimer"])
        # A returned record is labelled too, not just the envelope.
        record = outcome.data["message"]
        self.assertEqual("mock", record["origin"])
        self.assertTrue(C.is_mock_label(record["mock_label"]))

    def test_unsupported_capabilities_are_declared_not_silently_empty(self) -> None:
        beeper = MockBeeperAdapter(self.svc.corpus)
        account = next(a for a in self.svc.corpus["accounts"] if a["adapter"] == "mock_beeper")
        outcome = beeper.change_poll(account["account_id"])
        self.assertEqual("unsupported", outcome.code)
        self.assertFalse(outcome.usable)
        self.assertTrue(outcome.mocked)
        self.assertTrue(C.is_mock_label(outcome.to_dict()["mock_label"]))
        manifest = beeper.manifest().to_dict()
        self.assertFalse(manifest["capabilities"]["change_poll"]["supported"])
        self.assertIn("not declared", manifest["capabilities"]["change_poll"]["limitation"])

    def test_the_guard_actually_rejects_an_unlabelled_mock(self) -> None:
        with self.assertRaises(AssertionError):
            self.svc.store.insert_row("person", {
                "person_id": "p-x", "display_name": "Unlabelled", "notes": None,
                "user_override": 0, "created_at": C.now(), "origin": C.MOCK,
                "mock_label": None})
        with self.assertRaises(AssertionError):
            self.svc.store.insert_row("person", {
                "person_id": "p-y", "display_name": "No origin", "notes": None,
                "user_override": 0, "created_at": C.now(), "mock_label": "MOCK:x"})
        problems = C.find_unlabelled_mock({"data": {"origin": "mock", "value": 1}})
        self.assertEqual(1, len(problems))
        self.assertEqual([], C.find_unlabelled_mock(
            {"data": {"origin": "mock", "mock_label": "MOCK:x"}}))

    def test_no_fixture_contains_a_secret_or_real_endpoint(self) -> None:
        blob = json.dumps(self.svc.corpus, sort_keys=True, default=str)
        for forbidden in ("password", "api_key", "token", "BEGIN PRIVATE KEY", "http://",
                          "https://", "1password", "op://"):
            self.assertNotIn(forbidden, blob.lower())
        # Fixture identities use reserved .test domains only.
        self.assertIn(".test", blob)


if __name__ == "__main__":
    unittest.main()
