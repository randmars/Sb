"""The write-time refusal: a documentation read can never be stored as supported.

Mirror of the Mini worker's own rule. ``switchboard_mini/outcomes.py::
probe_row_supported_claim_allowed`` refuses to *build* such a row; Grace holds the same
rule in ``grace/contracts.py::probe_row_supported_claim_allowed`` and applies it in
``grace/ingest.py::probe_row_problems``, all-or-nothing, in ``import_probe_rows``. A
single over-claiming row refuses the whole import and leaves the ledger exactly as it was,
because a partially imported probe would describe a machine that was never measured
(probe-pack scope statement: "A documentation read can never set ``supported: true``";
PRD §11 capability manifest, Gate 2).

Nothing here contacts a source. The rows come from the Mini worker's recorded fixtures and
from the probe pack, both of which are labelled as what they are.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from grace import contracts as C                            # noqa: E402
from grace.ingest import (import_probe_rows, probe_provenance,  # noqa: E402
                          probe_row_problems, stored_probe_rows)
from switchboard_mini import outcomes as O                  # noqa: E402
from switchboard_mini.mail_adapter import build_adapter     # noqa: E402
from switchboard_mini.probe import run_probe                # noqa: E402

from tests.helpers import GraceTestCase                     # noqa: E402


def fixture_probe_rows() -> list:
    """One whole recorded probe run from the fixture twin, labelled ``fixture``/``documentation``."""
    adapter = build_adapter(fixture_mode=True, fixture_scenario="granted")
    run = run_probe(adapter)
    assert run.ok, run.harness_errors
    return run.rows


def documentation_rows(rows: list) -> list:
    return [r for r in rows if r["origin"] == O.DOCUMENTATION]


class TestProbeImportRefusesAnOverClaimingRow(GraceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_mail")
        self.rows = fixture_probe_rows()
        self.before = stored_probe_rows(self.svc.store)

    def test_a_documentation_row_marked_supported_is_refused(self) -> None:
        rows = [dict(r) for r in self.rows]
        victim = documentation_rows(rows)[0]
        victim["supported"] = True
        result = import_probe_rows(self.svc.store, self.account, rows)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["refused"], len(rows))
        self.assertTrue(any("documentation" in p and "supported" in p
                            for p in result["problems"]), result["problems"])
        # nothing was stored, for any row: the import is all-or-nothing
        self.assertEqual(stored_probe_rows(self.svc.store), self.before)

    def test_the_same_run_without_the_claim_imports(self) -> None:
        result = import_probe_rows(self.svc.store, self.account, self.rows)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["imported"], len(self.rows))
        stored = stored_probe_rows(self.svc.store)
        documented = [r for r in stored if r["origin"] == O.DOCUMENTATION]
        self.assertEqual(len(documented), len(documentation_rows(self.rows)))
        for row in documented:
            with self.subTest(capability=row["name"]):
                self.assertFalse(row["supported"])
                self.assertEqual(row["state"], O.PROBE_UNMEASURED)
                self.assertFalse(row["real_source_connected"])
                self.assertFalse(row["values_from_source"])
                self.assertTrue(row["sourced_refs"])

    def test_no_stored_row_is_supported_from_documentation(self) -> None:
        self.assertTrue(import_probe_rows(self.svc.store, self.account, self.rows)["ok"])
        for row in stored_probe_rows(self.svc.store):
            if row["origin"] == O.DOCUMENTATION:
                with self.subTest(capability=row["name"]):
                    self.assertFalse(row["supported"])

    def test_an_unknown_account_is_refused_rather_than_stored(self) -> None:
        result = import_probe_rows(self.svc.store, "acct_not_in_this_ledger", self.rows)
        self.assertFalse(result["ok"], result)
        self.assertIn("source_account", result["problems"][0])

    def test_the_provenance_statement_says_no_source_was_contacted(self) -> None:
        self.assertTrue(import_probe_rows(self.svc.store, self.account, self.rows)["ok"])
        provenance = probe_provenance(stored_probe_rows(self.svc.store))
        self.assertTrue(provenance["no_source_contacted"])
        self.assertEqual(provenance["real_source_connected"], 0)
        self.assertIn("No source was contacted", provenance["statement"])
        self.assertIn("documented-only reads", provenance["statement"])


class TestBothSidesOfTheBoundaryAgree(unittest.TestCase):
    """The Mini worker's rule and Grace's rule answer the same question the same way."""

    ROWS = (
        {"capability": "beeper_send_message", "origin": O.DOCUMENTATION, "supported": True},
        {"capability": "beeper_send_message", "origin": O.DOCUMENTATION, "supported": False},
        {"capability": "account_enumeration", "origin": O.FIXTURE, "supported": True},
        {"capability": "account_enumeration", "origin": O.FIXTURE, "supported": False},
        {"capability": "account_enumeration", "origin": O.REAL, "supported": True},
        {"capability": "account_enumeration", "origin": O.REAL, "supported": False},
    )

    def test_the_rule_agrees_on_every_case(self) -> None:
        for row in self.ROWS:
            with self.subTest(row=row):
                self.assertEqual(C.probe_row_supported_claim_allowed(row),
                                 O.probe_row_supported_claim_allowed(row))

    def test_only_a_documentation_row_supported_true_is_refused(self) -> None:
        for row in self.ROWS:
            with self.subTest(row=row):
                refused = not C.probe_row_supported_claim_allowed(row)
                self.assertEqual(refused, row["origin"] == O.DOCUMENTATION
                                 and row["supported"] is True)

    def test_no_measured_row_is_affected_by_the_rule(self) -> None:
        for row in fixture_probe_rows():
            with self.subTest(capability=row["capability"]):
                self.assertTrue(C.probe_row_supported_claim_allowed(row))


class TestProbeRowProblemsNamesEveryOverClaim(GraceTestCase):
    """The reasons are typed strings, so a refusal can be read by a person or a script."""

    def test_a_documentation_row_supported_true_is_named(self) -> None:
        problems = probe_row_problems({"capability": "x", "origin": O.DOCUMENTATION,
                                       "supported": True, "state": O.PROBE_UNMEASURED,
                                       "permission_state": O.PERMISSION_NOT_DETERMINED,
                                       "probe_assertion": "nothing", "observed_version":
                                       O.VERSION_NOT_OBSERVED})
        self.assertTrue(any("may never be supported" in p for p in problems), problems)

    def test_a_clean_row_has_no_problems(self) -> None:
        for row in fixture_probe_rows():
            with self.subTest(capability=row["capability"]):
                self.assertEqual(probe_row_problems(row), [])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
