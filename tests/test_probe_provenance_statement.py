"""The provenance sentence that says what a probe population measured (Gate 2, R12).

Defect fixed here (reproduced by the lead on 09bcdcb, 2026-10-09, and below on the code
before this change): importing the *real* 38-row probe document into a seeded ledger and
reading ``data.provenance.statement`` produced

    "...; 1 measure the worker itself (?), not a source."

Two faults in one owner-facing sentence. The self-measurement count wore the plural verb for
a single row, and the capability name came from ``row.get("capability") or
row.get("name")`` applied to the import-time provenance rows, which were built without a
capability name -- so a literal ``?`` was printed where a capability belongs.

The sentence is now built from the rows for 0, 1 and n self-measurements, names what they
are, and says nothing rather than a placeholder when a row names nothing. These tests pin it
against the real document, and derive every expectation from the rows themselves, so the
sentence cannot become a constant that agrees by luck.

Nothing here contacts a source: the rows are what the real adapter answers on this Linux
host -- typed refusals with ``real_source_connected: false`` everywhere -- and no capability
is ever marked supported by anything but its own assertion.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "mini")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from grace import contracts as C                                          # noqa: E402
from grace import ingest as I                                             # noqa: E402
from grace.ingest import (import_probe_rows, probe_provenance,            # noqa: E402
                          stored_probe_rows)
from tests.helpers import GraceTestCase, run_cli_raw                      # noqa: E402
from tests.test_probe_manifest_self_measurement import (                  # noqa: E402
    real_probe_rows, wrap_with_jq, write_jsonl)


def worker_self_names(rows: list[dict]) -> list[str]:
    """The capability names of the rows that measured the worker, read from the rows."""
    return sorted({r.get("capability") or r.get("name") or ""
                   for r in rows if r.get("measurement_target") == C.MEASUREMENT_WORKER}
                  - {""})


def worker_self_clause(rows: list[dict]) -> str:
    """The clause the sentence must carry for this population, derived from the rows."""
    count = len([r for r in rows if r.get("measurement_target") == C.MEASUREMENT_WORKER])
    noun, verb = ("row", "measures") if count == 1 else ("rows", "measure")
    names = worker_self_names(rows)
    named = f" ({', '.join(names)})" if names else ""
    return f"{count} {noun} {verb} the worker itself{named}, not a source"


class TestTheSentenceOverTheRealDocument(GraceTestCase):
    """The real 38-row run: the sentence names the manifest row instead of printing '?'."""

    def setUp(self) -> None:
        if sys.platform == "darwin":      # pragma: no cover - this host is Linux
            self.skipTest("this test asserts the Linux stand-in's real-mode rows")
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_mail")
        self.rows = real_probe_rows()

    def imported(self) -> dict:
        result = import_probe_rows(self.svc.store, self.account, self.rows)
        self.assertTrue(result["ok"], result)
        return result

    def test_the_real_document_has_exactly_one_worker_self_measurement(self) -> None:
        """The premise of every assertion below, read off the rows, not assumed."""
        self.assertEqual(len(self.rows), 38, [r["capability"] for r in self.rows])
        self.assertEqual(worker_self_names(self.rows), ["manifest"])
        self.assertTrue(all(not r["real_source_connected"] for r in self.rows))

    def test_the_sentence_names_the_self_measurement_and_has_no_placeholder(self) -> None:
        statement = self.imported()["provenance"]["statement"]
        self.assertNotIn("?", statement)
        self.assertIn(worker_self_clause(self.rows), statement)
        self.assertIn("(manifest)", statement)
        self.assertIn("Nothing here is an observation of Randy's Mac.", statement)

    def test_the_sentence_counts_come_from_the_rows(self) -> None:
        statement = self.imported()["provenance"]["statement"]
        unmeasured = [r for r in self.rows if r["state"] == C.PROBE_UNMEASURED]
        documented = [r for r in self.rows if r["origin"] == C.DOCUMENTATION]
        fixture = [r for r in self.rows if r["origin"] in (C.FIXTURE, C.MOCK)]
        self.assertIn(f"No source was contacted by any of the {len(self.rows)} "
                      f"capability rows", statement)
        self.assertIn(f"{len(unmeasured)} are unmeasured", statement)
        self.assertIn(f"{len(documented)} are documented-only reads", statement)
        if fixture:
            self.assertIn(f"{len(fixture)} came from recorded fixtures", statement)
        else:
            self.assertNotIn("came from recorded fixtures", statement)
        self.assertNotIn("0 are", statement)

    def test_the_stored_rows_keep_naming_what_measured_the_worker(self) -> None:
        """Reading the population back out of the ledger cannot lose the capability name."""
        self.imported()
        account_rows = [I._capability_row(row) for row in self.svc.store.all(
            "SELECT c.*, a.adapter AS source FROM capability c JOIN source_account a "
            "ON a.account_id = c.account_id WHERE c.account_id = ? ORDER BY c.name",
            (self.account,))]
        imported_names = {r["capability"] for r in self.rows}
        self.assertTrue(imported_names.issubset({r["name"] for r in account_rows}))
        self.assertEqual(len([r for r in account_rows
                              if r["measurement_target"] == C.MEASUREMENT_WORKER]), 1)
        statement = probe_provenance(account_rows)["statement"]
        self.assertNotIn("?", statement)
        self.assertIn("1 row measures the worker itself (manifest), not a source", statement)


class TestTheCliStatementOnARealRun(GraceTestCase):
    """The path the pack documents: ``probe --out`` -> wrap -> ``probe-import`` -> read it."""

    def setUp(self) -> None:
        if sys.platform == "darwin":      # pragma: no cover - this host is Linux
            self.skipTest("this test asserts the Linux stand-in's real-mode rows")
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_mail")
        self.rows = real_probe_rows()

    def test_the_imported_document_names_its_self_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "probe-rows.jsonl"
            wrapped = Path(tmp) / "probe-import.json"
            write_jsonl(jsonl, self.rows)
            how = wrap_with_jq(jsonl, wrapped)
            proc = run_cli_raw(self.db, "probe-import", "--file", str(wrapped),
                               "--account", self.account)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"]["imported"], len(self.rows), how)
        statement = payload["data"]["provenance"]["statement"]
        self.assertNotIn("?", statement)
        self.assertIn(worker_self_clause(self.rows), statement)
        self.assertNotIn("?", payload["message"])
        self.assertIn(str(len(self.rows)), statement)


class TestTheSentenceForZeroOneAndManySelfMeasurements(unittest.TestCase):
    """Grammar and naming for 0, 1 and n -- no source is involved anywhere here."""

    @staticmethod
    def row(name, target=C.MEASUREMENT_SOURCE, **over):
        row = {"capability": name, "origin": C.DOCUMENTATION, "supported": False,
               "state": C.PROBE_UNMEASURED, "real_source_connected": False,
               "measurement_target": target}
        row.update(over)
        return row

    def test_zero_self_measurements_says_nothing_about_the_worker(self) -> None:
        statement = probe_provenance([self.row("mail_accounts")])["statement"]
        self.assertNotIn("worker", statement)
        self.assertNotIn("?", statement)

    def test_one_self_measurement_takes_the_singular_verb_and_names_the_capability(self) -> None:
        statement = probe_provenance(
            [self.row("manifest", C.MEASUREMENT_WORKER),
             self.row("mail_accounts")])["statement"]
        self.assertIn("1 row measures the worker itself (manifest), not a source", statement)
        self.assertNotIn("1 measure", statement)
        self.assertNotIn("?", statement)

    def test_many_self_measurements_take_the_plural_verb_and_list_their_names(self) -> None:
        statement = probe_provenance(
            [self.row("manifest", C.MEASUREMENT_WORKER),
             self.row("worker_install", C.MEASUREMENT_WORKER),
             self.row("mail_accounts")])["statement"]
        self.assertIn("2 rows measure the worker itself (manifest, worker_install), "
                      "not a source", statement)
        self.assertNotIn("?", statement)

    def test_a_self_measurement_with_no_name_is_described_without_a_placeholder(self) -> None:
        """The last resort is silence about the name, never a '?' the owner would read."""
        bare = {"measurement_target": C.MEASUREMENT_WORKER, "origin": C.FIXTURE,
                "supported": True, "state": C.SUCCESS, "real_source_connected": False}
        statement = probe_provenance([bare, self.row("mail_accounts")])["statement"]
        self.assertIn("1 row measures the worker itself, not a source", statement)
        self.assertNotIn("?", statement)

    def test_a_stored_row_shape_is_named_too(self) -> None:
        """``name`` is what a stored capability row carries; that must name the row as well."""
        statement = probe_provenance(
            [{"name": "manifest", "origin": C.FIXTURE, "supported": True,
              "state": C.SUCCESS, "real_source_connected": False,
              "measurement_target": C.MEASUREMENT_WORKER}])["statement"]
        self.assertIn("1 row measures the worker itself (manifest), not a source", statement)

    def test_zero_counts_are_not_narrated(self) -> None:
        """A population with no fixture rows must not say '0 came from recorded fixtures'."""
        statement = probe_provenance([self.row("mail_accounts")])["statement"]
        self.assertNotIn("0 ", statement)

    def test_the_supported_count_is_reported_without_claiming_a_source(self) -> None:
        """A worker self-measurement may be supported; that never makes it a source read."""
        provenance = probe_provenance(
            [self.row("manifest", C.MEASUREMENT_WORKER, supported=True, state=C.SUCCESS),
             self.row("mail_accounts")])
        self.assertEqual(provenance["supported"], 1)
        self.assertEqual(provenance["real_source_connected"], 0)
        self.assertTrue(provenance["no_source_contacted"])
        self.assertIn("No source was contacted", provenance["statement"])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
