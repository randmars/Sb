"""The ``manifest`` row: a measurement of *the worker*, declared as one.

Defect fixed here (reproduced on master ``7c38fd6``, 2026-10-09): in real mode the worker
emitted its own ``manifest`` row as ``origin: real, supported: true,
real_source_connected: false``. Grace's contract refuses any row that claims support while
``real_source_connected`` is false -- correctly, because support means "the source answered
this row" -- so **one** worker row refused a whole real 38-row handoff
(``imported: 0``, ``refused: 1 problem(s), nothing stored``). The Gate 2 readiness pack had
to work around it by dropping the row with ``jq``, which is exactly the kind of quiet
workaround that hides a defect.

The fix is not to relax Grace's rule and not to make the row lie. It is:

* the row says, in a new contract field, **what it measured**: ``measurement_target``,
  ``"source"`` for every row that is about Mail/Beeper/Contacts/Hermes, ``"worker"`` for a
  row that measured the worker itself;
* ``measurement_target: "worker"`` is accepted for support-without-a-source **only** for
  the frozen set of worker self-measurements (``manifest``) -- never a generic bypass, and
  never for a documentation row;
* the ``manifest`` row's ``supported`` flag now reports whether its own
  ``probe_assertion`` actually held ("the worker emits a manifest that declares every
  capability and marks each unprobed capability supported=false"), instead of being
  hardcoded true. The guard is still there and is now genuinely checkable: an adapter whose
  manifest claims an unprobed capability is reported ``supported: false``.

Nothing in this file contacts a source. The real-mode rows are what the real adapter
answers on this Linux computer: typed refusals, ``real_source_connected: false``
everywhere. Gate 2 labels: fixture rows stay ``fixture``/``FIXTURE:``, documentation rows
stay ``documentation``/``DOCUMENTATION:`` and ``supported: false``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import sys as _sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "mini")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from grace import contracts as C                                   # noqa: E402
from grace.ingest import (import_probe_rows, probe_row_problems,    # noqa: E402
                          stored_probe_rows)
from switchboard_mini import outcomes as O                          # noqa: E402
from switchboard_mini.mail_adapter import build_adapter             # noqa: E402
from switchboard_mini.probe import (CAPABILITIES, ProbeContext,     # noqa: E402
                                    _probe_manifest, run_probe)

from tests.helpers import GraceTestCase, run_cli_raw                 # noqa: E402


def real_probe_rows() -> list:
    """What the *real* adapter answers on this host: 38 rows, none from a source."""
    adapter = build_adapter(fixture_mode=False)
    run = run_probe(adapter)
    assert run.ok, run.harness_errors
    return run.rows


def row_for(rows: list, capability: str) -> dict:
    for row in rows:
        if row["capability"] == capability:
            return row
    raise AssertionError(f"no row for {capability}")


def manifest_capability():
    return [c for c in CAPABILITIES if c.name == "manifest"][0]


def write_jsonl(path: Path, rows: list) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def wrap_with_jq(jsonl: Path, out: Path) -> str:
    """Wrap one JSONL run the way the Gate 2 pack documents: ``jq -s '{rows: .}'``."""
    if shutil.which("jq"):
        proc = subprocess.run(["jq", "-s", "{rows: .}", str(jsonl)],
                              capture_output=True, text=True, check=True)
        out.write_text(proc.stdout)
        return "jq"
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    out.write_text(json.dumps({"rows": rows}))
    return "python3 (jq is not installed on this host)"


class TestARealRunImportsWhole(GraceTestCase):
    """The defect: one worker self-description refused the entire real handoff."""

    def setUp(self) -> None:
        if sys.platform == "darwin":      # pragma: no cover - this host is Linux
            self.skipTest("this test asserts the Linux stand-in's real-mode rows")
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_mail")
        self.rows = real_probe_rows()

    def test_a_real_run_imports_with_its_manifest_row(self) -> None:
        result = import_probe_rows(self.svc.store, self.account, self.rows)
        self.assertEqual(result["problems"], [], result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["imported"], 38)
        self.assertEqual(result["refused"], 0)
        stored = {r["name"]: r for r in stored_probe_rows(self.svc.store)}
        manifest = stored["manifest"]
        self.assertTrue(manifest["supported"])
        self.assertFalse(manifest["real_source_connected"],
                         "the manifest row described the worker, not a source")
        self.assertFalse(manifest["values_from_source"])
        self.assertEqual(manifest["measurement_target"], "worker")

    def test_every_other_real_row_still_claims_nothing(self) -> None:
        self.assertTrue(import_probe_rows(self.svc.store, self.account, self.rows)["ok"])
        # Only the rows this run produced: the seeded ledger already holds the labelled
        # MOCK capability rows of the fixture adapters, which are not part of this handoff.
        stored = [r for r in stored_probe_rows(self.svc.store)
                  if r["origin"] == "real" and r["name"] != "manifest"]
        self.assertTrue(stored)
        for row in stored:
            with self.subTest(capability=row["name"]):
                self.assertFalse(row["supported"])
                self.assertFalse(row["real_source_connected"])
                self.assertNotEqual(row["measurement_target"], "worker")

    def test_the_manifest_row_is_the_only_supported_row_in_a_real_run(self) -> None:
        supported = [r["capability"] for r in self.rows if r["supported"]]
        self.assertEqual(supported, ["manifest"])
        # ... and it is supported *as a worker self-measurement*, which is the whole point
        self.assertEqual(row_for(self.rows, "manifest")["measurement_target"], "worker")
        for row in self.rows:
            if row["capability"] != "manifest":
                self.assertEqual(row["measurement_target"], "source", row["capability"])

    def test_the_cli_imports_an_unfiltered_real_run(self) -> None:
        """``probe --out`` -> ``jq -s '{rows: .}'`` -> ``probe-import``: no filter step."""
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "probe-rows.jsonl"
            wrapped = Path(tmp) / "probe-import.json"
            write_jsonl(jsonl, self.rows)
            how = wrap_with_jq(jsonl, wrapped)
            self.assertEqual(len(json.loads(wrapped.read_text())["rows"]), 38, how)
            proc = run_cli_raw(self.db, "probe-import", "--file", str(wrapped),
                               "--account", self.account)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"]["imported"], 38, payload)
        self.assertEqual(payload["data"]["problems"], [], payload)
        self.assertNotIn("Traceback", proc.stderr)


class TestTheManifestAssertionIsStillCheckable(unittest.TestCase):
    """``do not delete the guard``: the supported flag must follow its own assertion."""

    class Precocious:
        """An adapter whose manifest claims a capability nobody probed."""

        origin = O.FIXTURE
        adapter_is_real = False
        name = "precocious"

        def _label(self):
            return O.fixture_label("mail")

        def manifest(self, probe_rows=None):
            return {
                "manifest_version": "1.0",
                "adapter": "precocious",
                "adapter_version": "0.0.1",
                "probed": False,
                "capabilities": {
                    "account_enumeration": {"supported": True, "state": "success"},
                    "health": {"supported": False, "state": "unverified"},
                },
            }

    def manifest_row(self, adapter) -> dict:
        return _probe_manifest(ProbeContext(adapter), manifest_capability())

    def test_the_row_reports_the_worker_it_describes(self) -> None:
        row = self.manifest_row(build_adapter(fixture_mode=False))
        self.assertEqual(row["capability"], "manifest")
        self.assertEqual(row["measurement_target"], "worker")
        self.assertEqual(row["permission_state"], O.PERMISSION_NOT_APPLICABLE)
        self.assertFalse(row["values_from_source"])
        self.assertFalse(row["real_source_connected"])
        self.assertEqual(row["observed_version"], row["observed_version"])
        self.assertTrue(row["observed_version"])
        evidence = row["evidence"]
        self.assertIn("capabilities_declared", evidence)
        self.assertIn("capabilities_claimed_supported_without_probe", evidence)
        self.assertIn("claimed_without_probe", evidence)
        self.assertIn("probe_contract_version", evidence)

    def test_an_unprobed_claim_by_the_manifest_makes_the_row_unsupported(self) -> None:
        row = self.manifest_row(self.Precocious())
        self.assertFalse(row["supported"],
                         "the row's own assertion did not hold, so it may not claim support")
        self.assertEqual(row["evidence"]["claimed_without_probe"], ["account_enumeration"])
        self.assertEqual(row["evidence"]["capabilities_claimed_supported_without_probe"], 1)
        self.assertTrue(row["limitation"])
        self.assertIn("account_enumeration", row["limitation"])

    def test_a_manifest_that_declares_nothing_supported_is_supported(self) -> None:
        row = self.manifest_row(build_adapter(fixture_mode=False))
        self.assertEqual(row["evidence"]["claimed_without_probe"], [])
        self.assertTrue(row["supported"])


class TestTheMarkerIsNotAGenericBypass(unittest.TestCase):
    """A marker that named nothing would be a bypass. These are the doors it does not open."""

    def test_grace_refuses_the_marker_on_a_capability_that_is_not_a_self_measurement(self):
        problems = probe_row_problems({
            "capability": "account_enumeration", "origin": O.REAL, "supported": True,
            "state": O.SUCCESS, "permission_state": O.PERMISSION_GRANTED,
            "observed_version": O.VERSION_NOT_OBSERVED, "probe_method": "x",
            "probe_assertion": "x", "values_from_source": False,
            "real_source_connected": False, "measurement_target": "worker",
        })
        self.assertTrue(any("real_source_connected" in p for p in problems), problems)

    def test_grace_refuses_the_marker_bearing_source_values(self) -> None:
        problems = probe_row_problems({
            "capability": "manifest", "origin": O.REAL, "supported": True,
            "state": O.SUCCESS, "permission_state": O.PERMISSION_NOT_APPLICABLE,
            "observed_version": O.VERSION_NOT_OBSERVED, "probe_method": "x",
            "probe_assertion": "x", "values_from_source": True,
            "real_source_connected": False, "measurement_target": "worker",
        })
        self.assertTrue(any("self-measurement" in p or "worker" in p for p in problems),
                        problems)

    def test_grace_refuses_an_unknown_measurement_target(self) -> None:
        problems = probe_row_problems({
            "capability": "manifest", "origin": O.REAL, "supported": True,
            "state": O.SUCCESS, "permission_state": O.PERMISSION_NOT_APPLICABLE,
            "observed_version": O.VERSION_NOT_OBSERVED, "probe_method": "x",
            "probe_assertion": "x", "values_from_source": False,
            "real_source_connected": False, "measurement_target": "everything",
        })
        self.assertTrue(any("measurement_target" in p for p in problems), problems)

    def test_a_documentation_row_may_not_use_the_marker_either(self) -> None:
        problems = probe_row_problems({
            "capability": "manifest", "origin": O.DOCUMENTATION, "supported": True,
            "state": O.PROBE_UNMEASURED, "permission_state": O.PERMISSION_NOT_DETERMINED,
            "observed_version": O.VERSION_NOT_OBSERVED, "probe_method": "x",
            "probe_assertion": "x", "values_from_source": False,
            "real_source_connected": False, "measurement_target": "worker",
        })
        self.assertTrue(any("documentation" in p for p in problems), problems)

    def test_the_worker_refuses_to_build_the_marker_on_a_source_capability(self) -> None:
        from switchboard_mini.probe import _row
        context = ProbeContext(build_adapter(fixture_mode=False))
        source_capability = [c for c in CAPABILITIES if c.name == "account_enumeration"][0]
        with self.assertRaises(ValueError):
            _row(context, source_capability, supported=True, state=O.SUCCESS,
                 permission_state=O.PERMISSION_GRANTED, limitation=None, evidence={},
                 measurement_target=O.MEASUREMENT_WORKER)

    def test_the_worker_refuses_a_self_measurement_that_claims_source_values(self) -> None:
        from switchboard_mini.probe import _row
        context = ProbeContext(build_adapter(fixture_mode=False))
        with self.assertRaises(ValueError):
            _row(context, manifest_capability(), supported=True, state=O.SUCCESS,
                 permission_state=O.PERMISSION_NOT_APPLICABLE, limitation=None, evidence={},
                 measurement_target=O.MEASUREMENT_WORKER, values_from_source=True)

    def test_both_sides_answer_the_same_question_the_same_way(self) -> None:
        cases = (
            {"capability": "manifest", "origin": O.REAL, "supported": True,
             "real_source_connected": False, "measurement_target": O.MEASUREMENT_WORKER},
            {"capability": "manifest", "origin": O.REAL, "supported": True,
             "real_source_connected": False, "measurement_target": O.MEASUREMENT_SOURCE},
            {"capability": "account_enumeration", "origin": O.REAL, "supported": True,
             "real_source_connected": False, "measurement_target": O.MEASUREMENT_WORKER},
            {"capability": "manifest", "origin": O.DOCUMENTATION, "supported": True,
             "real_source_connected": False, "measurement_target": O.MEASUREMENT_WORKER},
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(
                    C.probe_row_supported_without_a_source_allowed(case),
                    O.probe_row_supported_without_a_source_allowed(case))
        # and the two frozen sets of worker self-measurements are the same set
        self.assertEqual(set(C.SELF_MEASURED_CAPABILITIES),
                         set(O.SELF_MEASURED_CAPABILITIES))
        self.assertEqual(set(C.MEASUREMENT_TARGETS), set(O.MEASUREMENT_TARGETS))
        self.assertEqual(set(C.MEASUREMENT_TARGETS),
                         {C.MEASUREMENT_SOURCE, C.MEASUREMENT_WORKER})

    def test_the_marker_is_what_grace_accepts_not_the_capability_name(self) -> None:
        """The same row without the marker is still refused: the field is load-bearing."""
        with_marker = {
            "capability": "manifest", "origin": O.REAL, "supported": True,
            "real_source_connected": False, "measurement_target": O.MEASUREMENT_WORKER,
        }
        without = dict(with_marker)
        without.pop("measurement_target")
        self.assertTrue(C.probe_row_supported_without_a_source_allowed(with_marker))
        self.assertFalse(C.probe_row_supported_without_a_source_allowed(without))


class TestUnmeasuredThingsAreUntouched(GraceTestCase):
    """No fixture row and no documentation row gains a claim from this fix."""

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_mail")

    def test_no_row_is_supported_without_a_source_a_fixture_or_the_marker(self) -> None:
        for fixture_mode in (True, False):
            with self.subTest(fixture_mode=fixture_mode):
                adapter = build_adapter(fixture_mode=fixture_mode,
                                        fixture_scenario="granted")
                for row in run_probe(adapter).rows:
                    if not row["supported"]:
                        continue
                    allowed = (row["real_source_connected"]
                               or row["origin"] == O.FIXTURE
                               or O.probe_row_supported_without_a_source_allowed(row))
                    self.assertTrue(allowed, row["capability"])

    def test_a_fixture_run_still_imports_and_keeps_its_labelling(self) -> None:
        adapter = build_adapter(fixture_mode=True, fixture_scenario="granted")
        rows = run_probe(adapter).rows
        result = import_probe_rows(self.svc.store, self.account, rows)
        self.assertTrue(result["ok"], result)
        stored = stored_probe_rows(self.svc.store)
        documented = [r for r in stored if r["origin"] == O.DOCUMENTATION]
        measured = [r for r in stored if r["origin"] == O.FIXTURE]
        self.assertTrue(measured)
        self.assertTrue(documented)
        for row in documented:
            with self.subTest(capability=row["name"]):
                self.assertFalse(row["supported"])
                self.assertEqual(row["state"], O.PROBE_UNMEASURED)
        for row in measured:
            with self.subTest(capability=row["name"]):
                self.assertFalse(row["real_source_connected"])
                self.assertTrue(O.is_fixture_label(row["label"]))
                # The manifest row measures the worker whatever the run's origin is; every
                # other row is a measurement of a source.
                if row["name"] == "manifest":
                    self.assertEqual(row["measurement_target"], O.MEASUREMENT_WORKER)
                else:
                    self.assertEqual(row["measurement_target"], O.MEASUREMENT_SOURCE)
