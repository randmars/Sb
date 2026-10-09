"""A stand-in run is labelled in the label column, and Grace may not store it as a source.

Defect fixed here (found by the lead while driving ``hermes probe --submit-test-run`` against
the worker's own ``StubHermesGateway``, 2026-10-09). With a stand-in responder
(``SWITCHBOARD_HERMES_STANDIN`` / ``--stand-in-server`` / a stubbed ``opener``) all eight
Hermes rows were ``origin: real`` with ``mock_label``, ``label`` and ``disclaimer`` all
``NULL``, and ``evidence.stand_in: true`` as the *only* tell. Under ``mini/README.md`` that
is defensible -- ``origin: real`` means "the real adapter produced this row", and nothing was
over-claimed -- but it broke two rules that matter more:

* **the labelling rule**: a mock is always labelled *in the label column*, and a stand-in is
  the most mock-like responder there is;
* **Grace's provenance and replace-protection**, which key on ``origin`` and
  ``real_source_connected``. A stand-in run imported today would sit in the ledger as a real
  measurement taken on this host, and would silently overwrite the row Randy's real sitting
  produces -- the one row in the ledger that is actual evidence.

What is asserted below, all of it driven in process on this Linux computer: the rows a
stand-in answers carry ``stand_in: true`` and a ``STAND-IN:`` label plus disclaimer; the
fixture and real modes are unchanged; Grace counts such a run as a stand-in and not as a
source measurement, stores the label, refuses a stand-in row that over-claims, and refuses a
stand-in run that would replace a stored real measurement (all-or-nothing, ledger untouched).

**No Hermes gateway is contacted anywhere in this file.** The stand-in is a local HTTP server
in this repository; the "stored real measurement" in the last test is a synthetic row written
into the test's own temporary ledger, and it is described as synthetic in the test that uses
it. Nothing here is an observation of Randy's machine, and nothing here is Randy's data.

Requirements covered (PRD; ``O``-numbers are Gate 2 probe-pack records):

* **R11 / T03** (a capability row is a measurement, and a mock is always labelled) — the
  ``STAND-IN:`` label and disclaimer on every row a stand-in answers, and the unchanged
  fixture/real labelling.
* **R12 / R11** (the ledger stores what was measured, and never more) — the Grace-side
  import, the provenance count and the replace-protection.
* **R15** (unsupported capabilities exposed rather than faked) — a stand-in row may never be
  ``supported``, at the row builder and at the write gate.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "mini")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from grace import contracts as C                                          # noqa: E402
from grace.ingest import (import_probe_rows, probe_provenance,            # noqa: E402
                          probe_row_problems, stored_probe_rows)
from switchboard_mini import hermes_transport as T                        # noqa: E402
from switchboard_mini import outcomes as O                                # noqa: E402
from switchboard_mini.hermes_adapter import (MEASURED_CAPABILITIES,       # noqa: E402
                                             build_adapter)
from switchboard_mini.probe import run_probe                              # noqa: E402
from tests.helpers import GraceTestCase                                   # noqa: E402
from tests.test_mini_hermes_transport import (STANDIN_TOKEN,              # noqa: E402
                                              StubHermesGateway,
                                              closed_port_base_url)

#: The environment variable this file puts the stand-in's synthetic bearer value in. A
#: private name on purpose: nothing here reads or writes the owner's real key.
TOKEN_ENV = "SWITCHBOARD_HERMES_STANDIN_LABEL_TEST_KEY"

FAST_SETTLE_SECONDS = 0.2
FAST_SETTLE_INTERVAL = 0.05


def hermes_rows(*, fixture_mode: bool = False, base_url: str | None = None,
                stand_in: bool = False) -> list:
    """One Hermes-only probe run, in process, returning its rows."""
    adapter = build_adapter(fixture_mode=fixture_mode, base_url=base_url, stand_in=stand_in,
                            token_env=TOKEN_ENV)
    run = run_probe([adapter], only_source="hermes",
                    hermes={"settle_seconds": FAST_SETTLE_SECONDS,
                            "settle_interval": FAST_SETTLE_INTERVAL})
    if run.harness_errors:            # a harness failure is an instrument failure, not a state
        raise AssertionError(f"the probe harness failed: {run.harness_errors}")
    return run.rows


@contextlib.contextmanager
def standin_run(**stub_kwargs):
    """A started labelled stand-in; yields its rows, and always stops the server."""
    saved = {name: os.environ.get(name) for name in (TOKEN_ENV, T.STANDIN_ENV)}
    os.environ[TOKEN_ENV] = STANDIN_TOKEN
    os.environ.pop(T.STANDIN_ENV, None)
    stub = StubHermesGateway(**stub_kwargs).start()
    try:
        yield hermes_rows(base_url=stub.base_url, stand_in=True)
    finally:
        stub.stop()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class TestAStandInRunIsLabelledInTheLabelColumn(unittest.TestCase):
    """R11/T03 (labelling rule): a labelled loopback stand-in is labelled where a reader looks."""

    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in (TOKEN_ENV, T.STANDIN_ENV)}

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_every_row_a_stand_in_answered_says_so_in_the_label_column(self) -> None:
        """R11/T03: ``stand_in``, the ``STAND-IN:`` label and the disclaimer are all present."""
        with standin_run() as rows:
            self.assertEqual(len(rows), len(MEASURED_CAPABILITIES))
            for row in rows:
                with self.subTest(capability=row["capability"]):
                    self.assertIs(row["stand_in"], True,
                                  "the flag is a top-level row field, not evidence-only")
                    self.assertEqual(row["label"], O.stand_in_label("hermes"))
                    self.assertTrue(O.is_stand_in_label(row["label"]), row["label"])
                    self.assertIn("STAND-IN", row["label"])
                    self.assertIn("hermes", row["label"])
                    self.assertTrue(row["disclaimer"], "a stand-in row carries a disclaimer")
                    self.assertIn("stand-in", row["disclaimer"])
                    self.assertIn("not the source", row["disclaimer"])
                    self.assertNotIn("FIXTURE", row["label"])

    def test_a_stand_in_row_never_claims_a_source_or_support(self) -> None:
        """R15/R11: a stand-in is not an observation, so nothing here is measured or granted.

        ``origin`` stays ``real`` -- the real transport class produced the row, which is what
        that field means -- and the label is what keeps that from being read as a source read.
        """
        with standin_run() as rows:
            for row in rows:
                with self.subTest(capability=row["capability"]):
                    self.assertEqual(row["origin"], O.REAL)
                    self.assertFalse(row["real_source_connected"])
                    self.assertFalse(row["values_from_source"])
                    self.assertFalse(row["supported"])
                    # A stand-in was never asked for permission, so it can never have been
                    # granted: a row it answered says ``not_applicable``, and a row that is a
                    # typed refusal says ``not_determined`` (nothing was asked either).
                    self.assertIn(row["permission_state"],
                                  (O.PERMISSION_NOT_APPLICABLE, O.PERMISSION_NOT_DETERMINED))
                    self.assertNotEqual(row["permission_state"], O.PERMISSION_GRANTED)
                    self.assertEqual(row["observed_version"], O.VERSION_NOT_OBSERVED)

    def test_the_label_column_still_says_fixture_and_nothing_for_a_real_row(self) -> None:
        """R11/T03: the new label must not have changed the two labels that already existed."""
        fixture = hermes_rows(fixture_mode=True)
        self.assertEqual(len(fixture), len(MEASURED_CAPABILITIES))
        for row in fixture:
            with self.subTest(capability=row["capability"]):
                self.assertIs(row["stand_in"], False)
                self.assertTrue(O.is_fixture_label(row["label"]), row["label"])
                self.assertEqual(row["disclaimer"],
                                 O.disclaimer_for(O.FIXTURE, source="hermes"))
        # Real mode with nothing listening: a socket fact, and no label at all.
        os.environ[TOKEN_ENV] = STANDIN_TOKEN
        real = hermes_rows(base_url=closed_port_base_url())
        for row in real:
            with self.subTest(capability=row["capability"]):
                self.assertIs(row["stand_in"], False)
                self.assertIsNone(row["label"])
                self.assertIsNone(row["disclaimer"])


class TestGraceTreatsAStandInRunAsAStandIn(GraceTestCase):
    """R12/R11: the ledger must not store a dry run as a measurement taken on this host."""

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_hermes")
        self.assertTrue(self.account, "grace seed must provide a Hermes source account")

    def test_the_import_counts_the_run_as_a_stand_in_and_not_as_a_source(self) -> None:
        """R12/R11: provenance says stand-in, the stored row keeps the label, nothing is real."""
        with standin_run() as rows:
            result = import_probe_rows(self.svc.store, self.account, rows)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["imported"], len(MEASURED_CAPABILITIES))
        self.assertEqual(result["refused"], 0)
        self.assertEqual(result["problems"], [])
        provenance = result["provenance"]
        self.assertEqual(provenance["stand_in_rows"], len(MEASURED_CAPABILITIES))
        self.assertEqual(provenance["real_source_connected"], 0)
        self.assertTrue(provenance["no_source_contacted"])
        self.assertIn("labelled loopback stand-in", provenance["statement"])
        self.assertIn("which is not the source", provenance["statement"])
        # The stored rows: the label is the tell, and the flag is read back out of it.
        stored = {row["name"]: row for row in stored_probe_rows(self.svc.store)}
        for capability in MEASURED_CAPABILITIES:
            with self.subTest(capability=capability):
                kept = stored[capability]
                self.assertEqual(kept["origin"], O.REAL)
                self.assertEqual(kept["label"], O.stand_in_label("hermes"))
                self.assertIs(kept["stand_in"], True)
                self.assertFalse(kept["supported"])
                self.assertFalse(kept["real_source_connected"])
        # ... and the same count is re-derived from the ledger, not only from the import.
        from_ledger = probe_provenance(stored_probe_rows(self.svc.store))
        self.assertEqual(from_ledger["stand_in_rows"], len(MEASURED_CAPABILITIES))
        self.assertIn("labelled loopback stand-in", from_ledger["statement"])

    def test_a_stand_in_row_that_over_claims_is_refused_whole(self) -> None:
        """R15/R12: support, a source claim, or a missing label all refuse the import."""
        with standin_run() as rows:
            cases = {
                "supported": {"supported": True},
                "real_source_connected": {"real_source_connected": True},
                "values_from_source": {"values_from_source": True},
                "unlabelled": {"label": None, "disclaimer": None},
            }
            for label, change in cases.items():
                with self.subTest(change=label):
                    broken = [{**row, **change} if row["capability"]
                              == "hermes_capability_discovery" else dict(row)
                              for row in rows]
                    problems = probe_row_problems(broken[0])
                    self.assertTrue(problems, f"{label} was accepted")
                    before = stored_probe_rows(self.svc.store)
                    result = import_probe_rows(self.svc.store, self.account, broken)
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["imported"], 0)
                    self.assertEqual(stored_probe_rows(self.svc.store), before,
                                     "all-or-nothing: nothing from that run was stored")

    def test_a_stand_in_run_may_not_replace_a_stored_real_measurement(self) -> None:
        """R12/R11: the row Randy's sitting produces is the one row a dry run may not touch.

        The stored row here is **synthetic** and is described as such: this test writes a
        real-origin measurement into its own temporary ledger so the protection can be
        exercised without any real source existing on this computer. No source is contacted,
        and no real measurement is read or written anywhere.
        """
        with standin_run() as rows:
            synthetic = dict(rows[0])
            synthetic.update({"stand_in": False, "label": None, "disclaimer": None,
                              "supported": True, "state": O.SUCCESS,
                              "values_from_source": True, "real_source_connected": True,
                              "permission_state": O.PERMISSION_GRANTED,
                              "observed_version": "9.9.9-synthetic",
                              "probe_assertion": "a synthetic stored measurement (a test row)"})
            self.assertEqual(probe_row_problems(synthetic), [],
                             "the synthetic row must be storable, or the test proves nothing")
            self.assertEqual(import_probe_rows(self.svc.store, self.account,
                                               [synthetic])["imported"], 1)
            before = stored_probe_rows(self.svc.store)
            result = import_probe_rows(self.svc.store, self.account, rows)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["refused"], len(rows))
        refusal = result["refusals"][0]
        self.assertEqual(refusal["capability"], rows[0]["capability"])
        self.assertEqual(refusal["reason"], "stand_in_would_replace_a_real_measurement")
        self.assertEqual(refusal["stored_origin"], O.REAL)
        self.assertTrue(refusal["next_action"])
        self.assertEqual(stored_probe_rows(self.svc.store), before,
                         "the stored real measurement is exactly as it was")


class TestTheSharedVocabularyCarriesTheStandInLabel(unittest.TestCase):
    """R11/T03: the label vocabulary is one vocabulary, held identically on both sides."""

    def test_grace_and_the_worker_spell_the_stand_in_label_the_same_way(self) -> None:
        self.assertEqual(C.STAND_IN_LABEL_PREFIX, O.STAND_IN_LABEL_PREFIX)
        label = O.stand_in_label("hermes")
        self.assertTrue(C.is_stand_in_label(label))
        self.assertTrue(O.is_stand_in_label(label))
        self.assertFalse(C.is_stand_in_label(O.fixture_label("hermes")))
        self.assertFalse(O.is_stand_in_label("FIXTURE:hermes"))
        self.assertFalse(O.is_stand_in_label("DOCUMENTATION:hermes(O17)"))

    def test_a_stand_in_row_may_never_be_supported_on_either_side(self) -> None:
        """R15/T03: the same rule at the row builder and at Grace's write gate."""
        row = {"capability": "hermes_capability_discovery", "origin": O.REAL,
               "supported": True, "stand_in": True}
        self.assertFalse(O.probe_row_supported_claim_allowed(row))
        self.assertFalse(C.probe_row_supported_claim_allowed(row))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
