"""Mini worker: the capability probe's row contract and its typed failure states.

Every test here runs on this Linux computer, against the recorded fixtures in
``mini/switchboard_mini/fixtures/mail/``. Nothing in this file touches Mail.app, and the
fixture rows are asserted to be labelled ``FIXTURE:`` so a run here can never be mistaken
for a measurement of Randy's machine.
"""

from __future__ import annotations

import json
import sys
import unittest

from switchboard_mini import outcomes as O
from switchboard_mini.mail_adapter import build_adapter
from switchboard_mini.probe import (CAPABILITY_NAMES, CAPABILITIES,
                                    DOCUMENTED_CAPABILITY_NAMES,
                                    ROW_LABELLING_FIELDS, run_probe)

PERMISSION_STATES = set(O.PERMISSION_STATES)


def probe_rows(scenario: str = "granted", **kw) -> list:
    adapter = build_adapter(fixture_mode=True, fixture_scenario=scenario)
    run = run_probe(adapter, sample=kw.pop("sample", 3), **kw)
    assert run.ok, run.harness_errors
    return run.rows


def row_for(rows: list, capability: str) -> dict:
    matches = [r for r in rows if r["capability"] == capability]
    assert len(matches) == 1, f"expected exactly one {capability} row, got {len(matches)}"
    return matches[0]


class TestProbeRowShape(unittest.TestCase):
    """One row per capability, carrying the whole PRD §11 contract."""

    def setUp(self) -> None:
        self.rows = probe_rows()

    def test_one_row_per_capability_in_declared_order(self) -> None:
        self.assertEqual([r["capability"] for r in self.rows], list(CAPABILITY_NAMES))
        self.assertEqual(len(CAPABILITY_NAMES), len(set(CAPABILITY_NAMES)))

    def test_every_required_contract_field_is_present_and_typed(self) -> None:
        for row in self.rows:
            missing = [f for f in ROW_FIELDS + ROW_LABELLING_FIELDS if f not in row]
            self.assertEqual(missing, [], f"{row['capability']} is missing {missing}")
            self.assertIsInstance(row["supported"], bool)
            self.assertIn(row["permission_state"], PERMISSION_STATES)
            self.assertIsInstance(row["evidence"], dict)
            self.assertTrue(row["probe_method"], "probe_method must say how it was probed")
            self.assertTrue(row["probe_assertion"],
                            "probe_assertion must say what supported=true claims")
            if row["observed_version"] is not None:
                self.assertIsInstance(row["observed_version"], str)
            if row["limitation"] is not None:
                self.assertIsInstance(row["limitation"], str)

    def test_rows_are_json_serialisable(self) -> None:
        for row in self.rows:
            self.assertEqual(json.loads(json.dumps(row))["capability"], row["capability"])

    def test_a_capability_that_was_not_measured_is_not_claimed_supported(self) -> None:
        """The manifest declares nothing supported before a probe measured it."""
        adapter = build_adapter(fixture_mode=True, fixture_scenario="granted")
        manifest = adapter.manifest()
        self.assertFalse(manifest["probed"])
        self.assertEqual([n for n, c in manifest["capabilities"].items() if c["supported"]],
                         [])
        for entry in manifest["capabilities"].values():
            if entry.get("origin") == O.DOCUMENTATION:
                # A documented capability cannot be measured by this worker at all: it is
                # unmeasured, and saying "run the probe" would imply otherwise.
                self.assertEqual(entry["state"], "unmeasured")
                self.assertTrue(entry["citations"], entry["name"])
                continue
            self.assertEqual(entry["state"], "unverified")
            self.assertIn("probe", entry["limitation"])

    def test_manifest_folds_in_the_measured_rows(self) -> None:
        adapter = build_adapter(fixture_mode=True, fixture_scenario="granted")
        manifest = adapter.manifest(self.rows)
        self.assertTrue(manifest["probed"])
        for row in self.rows:
            entry = manifest["capabilities"][row["capability"]]
            self.assertEqual(entry["supported"], row["supported"])
            self.assertEqual(entry["state"], row["state"])
            self.assertEqual(entry["observed_version"], row["observed_version"])

    def test_unimplemented_capabilities_record_a_demonstrated_refusal(self) -> None:
        """Sending, drafting, reconciling and attachment bytes are absent by design."""
        for name in ("attachment_materialization", "draft_preparation", "authorized_send",
                     "send_reconciliation"):
            row = row_for(self.rows, name)
            self.assertFalse(row["supported"])
            self.assertEqual(row["state"], O.UNSUPPORTED)
            self.assertEqual(row["evidence"]["outcome_code"], O.UNSUPPORTED)
            self.assertTrue(row["evidence"]["reason"])
            self.assertIn("refusal", row["evidence"]["demonstrated"])

    def test_no_row_claims_a_source_value_it_did_not_read(self) -> None:
        for row in self.rows:
            if row["values_from_source"]:
                self.assertIn(row["state"], (O.SUCCESS, O.PARTIAL))
        # the manifest row describes the worker itself, not Mail
        self.assertFalse(row_for(self.rows, "manifest")["values_from_source"])


class TestProbeLabelling(unittest.TestCase):
    """Fixture rows must be visibly fixture rows."""

    def test_fixture_rows_are_labelled_and_never_claim_a_real_source(self) -> None:
        for scenario in ("granted", "permission_denied", "offline", "partial_history"):
            for row in probe_rows(scenario):
                self.assertEqual(row["origin"], O.FIXTURE)
                self.assertFalse(row["real_source_connected"])
                self.assertTrue(O.is_fixture_label(row["label"]), row["label"])
                self.assertIn("No Mail.app was contacted", row["disclaimer"])

    def test_fixture_outcomes_are_labelled_too(self) -> None:
        adapter = build_adapter(fixture_mode=True, fixture_scenario="granted")
        payload = adapter.accounts().to_dict()
        self.assertEqual(payload["origin"], O.FIXTURE)
        self.assertFalse(payload["real_source_connected"])
        self.assertTrue(payload["fixture_mode"])
        self.assertTrue(O.is_fixture_label(payload["label"]))
        self.assertIn("No Mail.app was contacted", payload["disclaimer"])
        # The worker has no 'mocked' flag: a recorded fixture is not a live mock adapter,
        # and every non-real document says 'fixture' in its origin and its label.
        self.assertNotIn("mocked", payload)

    def test_reported_outcomes_are_never_unlabelled(self) -> None:
        adapter = build_adapter(fixture_mode=True, fixture_scenario="offline")
        for outcome in (adapter.accounts(), adapter.mailboxes("FIXTURE Account A"),
                        adapter.enumerate("FIXTURE Account A", "mailbox:INBOX"),
                        adapter.retrieve("FIXTURE Account A", "mail:x:INBOX:1")):
            payload = outcome.to_dict()
            if payload["origin"] != O.REAL:
                self.assertTrue(O.is_fixture_label(payload["label"]), payload)


class TestPermissionDeniedProbe(unittest.TestCase):
    """A denied automation permission is reported as denied, not as an empty source."""

    def setUp(self) -> None:
        self.rows = probe_rows("permission_denied")

    def test_permission_state_is_denied_across_the_read_capabilities(self) -> None:
        for name in ("health", "account_enumeration", "mailbox_enumeration",
                     "bounded_message_listing", "message_dates", "body_retrieval"):
            row = row_for(self.rows, name)
            self.assertEqual(row["permission_state"], O.PERMISSION_STATE_DENIED,
                             f"{name} must carry the denied permission")
            self.assertFalse(row["supported"])
            self.assertEqual(row["state"], O.PERMISSION_DENIED)

    def test_the_row_carries_the_apple_event_code_and_the_fix(self) -> None:
        row = row_for(self.rows, "account_enumeration")
        self.assertEqual(row["evidence"]["ae_code"], -1743)
        self.assertEqual(row["evidence"]["reason"], "not_authorized_to_send_apple_events")
        self.assertIn("System Settings", row["evidence"]["next_action"])

    def test_no_capability_is_silently_empty(self) -> None:
        for row in self.rows:
            if not row["supported"]:
                self.assertTrue(row["limitation"], f"{row['capability']} has no reason")
                self.assertTrue(row["state"])


class TestOfflineProbe(unittest.TestCase):
    """A mailbox that cannot be reached says so instead of returning nothing."""

    def setUp(self) -> None:
        self.rows = probe_rows("offline")

    def test_reads_report_offline(self) -> None:
        for name in ("account_enumeration", "bounded_message_listing", "message_dates",
                     "attachment_enumeration"):
            row = row_for(self.rows, name)
            self.assertEqual(row["state"], O.OFFLINE)
            self.assertFalse(row["supported"])
            self.assertEqual(row["evidence"]["ae_code"], -600)

    def test_health_separates_capability_from_reachability(self) -> None:
        row = row_for(self.rows, "health")
        self.assertTrue(row["supported"], "the health capability still exists")
        self.assertEqual(row["state"], O.OFFLINE)
        self.assertIn("not running", row["limitation"])
        self.assertEqual(row["permission_state"], O.PERMISSION_GRANTED)

    def test_offline_does_not_invent_a_permission_state(self) -> None:
        row = row_for(self.rows, "account_enumeration")
        self.assertEqual(row["permission_state"], O.PERMISSION_NOT_DETERMINED)


class TestProbeHarnessFailure(unittest.TestCase):
    """Only a broken harness makes the probe fail; an unsupported capability does not."""

    def test_unsupported_capabilities_still_produce_a_successful_run(self) -> None:
        adapter = build_adapter(fixture_mode=True, fixture_scenario="permission_denied")
        run = run_probe(adapter)
        self.assertTrue(run.ok)
        self.assertEqual(run.harness_errors, [])
        summary = run.summary()
        self.assertEqual(summary["capabilities"], len(CAPABILITY_NAMES))
        self.assertTrue(summary["unsupported"])

    def test_an_exception_inside_a_probe_is_reported_as_a_harness_error(self) -> None:
        class Exploding:
            origin = O.FIXTURE

            def _label(self):
                return None

            def identity(self):
                raise RuntimeError("boom")

            def accounts(self):
                raise RuntimeError("boom")

            def mailboxes(self, account):
                raise RuntimeError("boom")

            def manifest(self, rows=None):
                raise RuntimeError("boom")

        run = run_probe(Exploding())
        self.assertFalse(run.ok)
        self.assertEqual(len(run.harness_errors),
                         len(CAPABILITY_NAMES) - len(DOCUMENTED_CAPABILITY_NAMES))
        for row in [r for r in run.rows if r["origin"] != O.DOCUMENTATION]:
            self.assertEqual(row["state"], "harness_error")
            self.assertFalse(row["supported"])
            self.assertFalse(row["values_from_source"])
            self.assertIn("RuntimeError", row["limitation"])


class TestProvenanceHonesty(unittest.TestCase):
    """`adapter_is_real` and `real_source_connected` are two different claims.

    `real_source_connected` means "a real source was contacted and this value came from
    it" -- the meaning it has in Grace's health output, in every receipt and in the
    mock-labelling rules. It must never be overloaded to mean "this is the real adapter
    rather than a fixture twin"; that is `adapter_is_real`. On this Linux computer the
    real adapter is selected and can read nothing, so both modes here must report
    `real_source_connected: false` on every row.
    """

    def test_fixture_rows_report_neither(self) -> None:
        for scenario in ("granted", "permission_denied", "offline", "partial_history"):
            for row in probe_rows(scenario):
                self.assertFalse(row["adapter_is_real"], row["capability"])
                self.assertFalse(row["real_source_connected"], row["capability"])
                self.assertEqual(row["origin"], O.FIXTURE)

    def test_real_adapter_rows_claim_no_source_on_a_host_without_mail(self) -> None:
        if sys.platform == "darwin":     # pragma: no cover - this computer is Linux
            self.skipTest("this computer is the Linux stand-in for Grace")
        adapter = build_adapter(fixture_mode=False)
        run = run_probe(adapter)
        self.assertTrue(run.ok, run.harness_errors)
        for row in [r for r in run.rows if r["origin"] != O.DOCUMENTATION]:
            self.assertTrue(row["adapter_is_real"], row["capability"])
            self.assertFalse(row["real_source_connected"], row["capability"])
            self.assertFalse(row["values_from_source"], row["capability"])
            if row["capability"] == "manifest":
                # the manifest row describes the worker itself, so it may be supported.
                # It is still not a source value, so it claims no contact either.
                self.assertTrue(row["supported"])
                continue
            self.assertFalse(row["supported"], row["capability"])
            self.assertEqual(row["state"], O.UNSUPPORTED)
            if row["capability"] in ("attachment_materialization", "draft_preparation",
                                     "authorized_send", "send_reconciliation"):
                # a deliberate absence in this slice, with its own reason
                self.assertIn(row["evidence"]["reason"],
                              ("read_only_slice_1", "materialize_not_implemented"))
                continue
            # every capability that needs Mail refused at the host, not at Mail
            self.assertEqual(row["evidence"]["reason"], "host_not_macos")
        # the real adapter answered every row and read nothing: that is the whole point
        self.assertNotIn(True, [r["real_source_connected"] for r in run.rows])

    def test_a_usable_read_is_what_earns_the_claim(self) -> None:
        """A row that carries source data is the only row that may set the flag."""
        adapter = build_adapter(fixture_mode=True, fixture_scenario="granted")
        rows = run_probe(adapter).rows
        for row in rows:
            expected = bool(row["adapter_is_real"] and row["values_from_source"])
            self.assertEqual(row["real_source_connected"], expected, row["capability"])

    def test_the_manifest_never_claims_a_contact_it_did_not_make(self) -> None:
        for fixture_mode in (True, False):
            adapter = build_adapter(fixture_mode=fixture_mode)
            manifest = adapter.manifest(probe_rows())
            self.assertEqual(manifest["real_source_connected"], False, fixture_mode)
            self.assertEqual(manifest["source_contacted"], False, fixture_mode)
            self.assertEqual(manifest["adapter_is_real"], not fixture_mode)
            self.assertEqual(manifest["folded_probe_rows"]["any_from_real_source"], False)
            json.dumps(manifest)


class TestCapabilityDeclaration(unittest.TestCase):
    def test_every_capability_declares_its_method_and_assertion(self) -> None:
        for capability in CAPABILITIES:
            self.assertTrue(capability.probe_method)
            self.assertTrue(capability.probe_assertion)
            self.assertTrue(capability.title)
            if not capability.implemented:
                self.assertTrue(capability.limitation,
                                f"{capability.name} must say why it is absent")

    def test_read_only_slice_declares_no_write_capability(self) -> None:
        absent = {c.name for c in CAPABILITIES if not c.implemented}
        self.assertEqual(absent, {"attachment_materialization", "draft_preparation",
                                  "authorized_send", "send_reconciliation"})


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
