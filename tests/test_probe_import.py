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

The second half of this file is the **handover** rather than the refusal: what happens when
a row arrives for a capability the ledger already holds a row for. A measured row
supersedes a stored documentation row, and that is reported (``supersessions``); a
documentation row may never replace a stored measurement, and that is refused with a typed
reason and the smallest next action (``refusals``). Both directions are asserted here, and
neither may be silent.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from grace import contracts as C                            # noqa: E402
from grace.ingest import (import_probe_rows, probe_provenance,  # noqa: E402
                          probe_row_problems, stored_probe_rows)
from switchboard_mini import outcomes as O                  # noqa: E402
from switchboard_mini.mail_adapter import build_adapter     # noqa: E402
from switchboard_mini.probe import run_probe                # noqa: E402

from tests.helpers import GraceTestCase, run_cli            # noqa: E402


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


def row(capability: str, *, origin: str, supported: bool = False, state: Optional[str] = None,
        citations=(), supersedes=None) -> dict:
    """One row in the worker's contract, so the handover rules can be driven directly."""
    measured = origin in ("real", "fixture", "mock")
    document = {
        "capability": capability,
        "origin": origin,
        "supported": bool(supported),
        "state": state or (O.SUCCESS if measured else O.PROBE_UNMEASURED),
        "permission_state": (O.PERMISSION_GRANTED if measured
                             else O.PERMISSION_NOT_DETERMINED),
        "observed_version": O.VERSION_NOT_OBSERVED,
        "probe_method": "test row",
        "probe_assertion": "what this row's supported flag would assert",
        "limitation": None,
        "evidence": {},
        "source": "beeper",
        "citations": list(citations),
        "label": (O.fixture_label("beeper") if origin == O.FIXTURE
                  else (O.documentation_label("beeper", citations)
                        if origin == O.DOCUMENTATION else None)),
        "values_from_source": bool(measured and supported),
        "real_source_connected": bool(origin == O.REAL and supported),
        "supersedes": supersedes,
    }
    return document


def stored_by_name(store) -> dict:
    return {r["name"]: r for r in stored_probe_rows(store)}


def rows_for(store, name: str) -> list:
    """Every stored row for one capability key. Two would mean two disagreeing rows."""
    return [r for r in stored_probe_rows(store) if r["name"] == name]


class TestTheGraceSideHandover(GraceTestCase):
    """What may happen to a row already stored for the same capability key.

    The worker names the documentation row a measured row replaces in the row's own
    ``supersedes`` field; this is the Grace half of that handover. Two directions, both
    asserted here, and neither is allowed to be silent.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_mail")

    def imported(self, rows: list) -> dict:
        result = import_probe_rows(self.svc.store, self.account, rows)
        self.assertTrue(result["ok"], result)
        return result

    def test_a_measured_row_supersedes_the_stored_documentation_row(self) -> None:
        self.imported([row("beeper_message_search", origin=O.DOCUMENTATION,
                           citations=("O06",))])
        self.assertEqual(stored_by_name(self.svc.store)["beeper_message_search"]["origin"],
                         O.DOCUMENTATION)
        result = self.imported([row("beeper_message_search", origin=O.FIXTURE,
                                    supersedes={"origin": O.DOCUMENTATION,
                                                "capability": "beeper_message_search"})])
        self.assertEqual(len(result["supersessions"]), 1, result)
        supersession = result["supersessions"][0]
        self.assertEqual(supersession["capability"], "beeper_message_search")
        self.assertEqual(supersession["superseded_origin"], O.DOCUMENTATION)
        self.assertEqual(supersession["superseding_origin"], O.FIXTURE)
        self.assertFalse(supersession["superseding_supported"])
        self.assertTrue(supersession["note"])
        # The handover happened once and left exactly one row for the key, never two.
        stored = rows_for(self.svc.store, "beeper_message_search")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["origin"], O.FIXTURE)

    def test_a_documentation_row_may_not_replace_a_stored_real_measurement(self) -> None:
        self.imported([row("beeper_message_search", origin=O.REAL, supported=True)])
        before = stored_probe_rows(self.svc.store)
        result = import_probe_rows(self.svc.store, self.account, [
            row("beeper_message_search", origin=O.DOCUMENTATION, citations=("O06",)),
            row("beeper_account_contacts", origin=O.DOCUMENTATION, citations=("O12",))])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(len(result["refusals"]), 1, result)
        refusal = result["refusals"][0]
        self.assertEqual(refusal["capability"], "beeper_message_search")
        self.assertEqual(refusal["reason"],
                         "documentation_would_replace_a_real_measurement")
        self.assertEqual(refusal["stored_origin"], O.REAL)
        self.assertTrue(refusal["stored_supported"])
        self.assertTrue(refusal["next_action"], "a refusal names the smallest next action")
        self.assertEqual([p for p in result["problems"]
                          if "documentation_would_replace_a_real_measurement" in p],
                         result["problems"])
        # Never silently overwritten and never kept as two disagreeing rows: the stored
        # measurement is exactly as it was, and the ledger holds one row for the key.
        self.assertEqual(stored_probe_rows(self.svc.store), before)
        self.assertEqual(len(rows_for(self.svc.store, "beeper_message_search")), 1)
        # All-or-nothing: the other capability in that run was not stored either.
        self.assertNotIn("beeper_account_contacts", stored_by_name(self.svc.store))

    def test_a_documentation_row_may_not_replace_a_stored_fixture_measurement(self) -> None:
        self.imported([row("beeper_message_search", origin=O.FIXTURE)])
        before = stored_probe_rows(self.svc.store)
        result = import_probe_rows(self.svc.store, self.account,
                                   [row("beeper_message_search", origin=O.DOCUMENTATION,
                                        citations=("O06",))])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["refusals"][0]["reason"],
                         "documentation_would_replace_a_measurement")
        self.assertEqual(result["refusals"][0]["stored_origin"], O.FIXTURE)
        self.assertTrue(result["refusals"][0]["next_action"])
        self.assertEqual(stored_probe_rows(self.svc.store), before)

    def test_a_documentation_reread_is_neither_a_supersession_nor_a_refusal(self) -> None:
        first = [row("beeper_message_search", origin=O.DOCUMENTATION, citations=("O06",))]
        self.imported(first)
        result = self.imported(first)
        self.assertEqual(result["supersessions"], [])
        self.assertEqual(result["refusals"], [])
        self.assertEqual(len(rows_for(self.svc.store, "beeper_message_search")), 1)

    def test_a_measured_row_replacing_a_measurement_is_not_reported_as_a_supersession(self) -> None:
        self.imported([row("beeper_message_search", origin=O.FIXTURE)])
        result = self.imported([row("beeper_message_search", origin=O.FIXTURE, state=O.PARTIAL)])
        self.assertEqual(result["supersessions"], [])
        self.assertEqual(stored_by_name(self.svc.store)["beeper_message_search"]["state"],
                         O.PARTIAL)

    # -- the same handover through the shipped entry point -------------------
    def rows_file(self, rows: list) -> str:
        path = Path(self._tmp.name) / "rows.json"
        path.write_text(json.dumps({"rows": rows}))
        return str(path)

    def test_the_cli_reports_the_supersession_it_applied(self) -> None:
        run_cli(self.db, "seed")
        self.imported([row("beeper_message_search", origin=O.DOCUMENTATION,
                           citations=("O06",))])
        payload = run_cli(self.db, "probe-import",
                          "--file", self.rows_file([row("beeper_message_search",
                                                        origin=O.FIXTURE)]),
                          "--account", self.account)
        self.assertIn("superseded", payload["message"])
        self.assertEqual([s["capability"] for s in payload["data"]["supersessions"]],
                         ["beeper_message_search"])

    def test_the_cli_refusal_names_the_reason_and_the_next_action(self) -> None:
        run_cli(self.db, "seed")
        self.imported([row("beeper_message_search", origin=O.REAL, supported=True)])
        payload = run_cli(self.db, "probe-import",
                          "--file", self.rows_file([row("beeper_message_search",
                                                        origin=O.DOCUMENTATION)]),
                          "--account", self.account, expect_ok=False)
        self.assertFalse(payload["ok"])
        self.assertIn("documentation_would_replace_a_real_measurement", payload["message"])
        self.assertIn("Smallest next action", payload["message"])
        self.assertEqual(payload["data"]["refusals"][0]["stored_origin"], O.REAL)


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
