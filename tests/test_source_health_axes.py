"""Source health is three separate axes, and unknown is never rendered green.

Defect (Gate 1 correctness audit): the client reported one blended health value per
source. Covering three different facts with one value loses exactly the distinction
the PRD demands: whether the source can be reached *at all* (transport), when it was
last observed (freshness) and whether the history read is complete or partial, with
the gap reason (coverage). PRD §6 "Health presentation", §13, R09 and T09/T10/T20.

The rule this pins: an axis whose value is unknown reads as ``unknown`` with a reason
and is never green; health comes from the live typed probe outcome, while the last
state recorded in the ledger is a second, clearly labelled axis of its own.
"""

from __future__ import annotations

import unittest

from tests.helpers import REPO_ROOT, GraceTestCase

AXES = ("transport", "freshness", "coverage")
SEVERITIES = ("ok", "warn", "danger", "unknown")

#: The typed states each axis may report. ``unknown`` is a real state, not a blank.
AXIS_STATES = {
    "transport": {"connected", "offline", "permission_denied", "unsupported", "rate_limited",
                  "error", "partial", "outcome_unknown", "unknown"},
    "freshness": {"observed_now", "observed", "unknown"},
    "coverage": {"complete", "partial_history", "unknown"},
}

#: The states that mean "this axis is positively confirmed healthy". Anything else —
#: including every ``unknown`` — must not be green.
GREEN_STATES = {
    "transport": {"connected"},
    "freshness": {"observed_now"},
    "coverage": {"complete"},
}


class TestHealthAxes(GraceTestCase):
    faults: tuple[str, ...] = ()

    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def _assert_axis_shape(self, axis: dict, name: str) -> None:
        self.assertIn(axis["state"], AXIS_STATES[name], axis)
        self.assertIn(axis["severity"], SEVERITIES, axis)
        self.assertIsInstance(axis["green"], bool)
        self.assertTrue(axis["reason"], f"every axis states its reason: {axis}")
        self.assertTrue(axis["basis"], f"every axis names its basis: {axis}")
        self.assertEqual(axis["green"], axis["state"] in GREEN_STATES[name],
                         f"{name} may only be green on positive evidence: {axis}")
        if axis["state"] == "unknown":
            self.assertFalse(axis["green"], f"unknown is never green: {axis}")
            self.assertNotEqual(axis["severity"], "ok", f"unknown is never ok: {axis}")


class TestAxesComeFromTheProbe(TestHealthAxes):
    def test_every_source_reports_three_separate_axes(self) -> None:
        sources = self.svc.ingest.source_health()
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(account=source["account_id"]):
                axes = source["health_axes"]
                self.assertEqual(set(axes), set(AXES))
                for name in AXES:
                    self._assert_axis_shape(axes[name], name)
                # The last state recorded in the ledger stays a separate, labelled axis
                # rather than being blended into any of the three.
                self.assertTrue(source["health_state_basis"])
                self.assertIn("stored_health_state", source)

    def test_transport_follows_the_live_probe_not_the_stored_row(self) -> None:
        """The stored row says 'connected'; the probe says 'offline'. Transport must say offline."""
        account = self.account_id("mock_mail")
        stored = self.svc.store.one("SELECT health_state FROM source_account WHERE account_id = ?",
                                   (account,))
        self.assertEqual(stored["health_state"], "connected")
        offline = GraceFaults(self, ("offline",)).source_health()
        entry = next(s for s in offline if s["account_id"] == account)
        transport = entry["health_axes"]["transport"]
        self.assertEqual(transport["state"], "offline")
        self.assertFalse(transport["green"])
        self.assertIn("offline", transport["reason"].lower())
        self.assertIn("probe", transport["basis"].lower())

    def test_a_partial_history_is_reported_on_the_coverage_axis_with_its_gap_reason(self) -> None:
        sources = GraceFaults(self, ("partial_history",)).source_health()
        partial = [s["health_axes"]["coverage"] for s in sources
                   if s["health_axes"]["coverage"]["state"] == "partial_history"]
        self.assertTrue(partial, "the injected partial-history fault must show on coverage")
        for axis in partial:
            self.assertFalse(axis["green"])
            self.assertTrue(axis["gap_reason"])
            self.assertTrue(axis["reason"])

    def test_an_unobserved_source_is_unknown_on_freshness_and_never_green(self) -> None:
        """No recorded success and no probe confirmation: freshness is unknown, not green."""
        account = self.account_id("mock_mail")
        self.svc.store.update_row("source_account",
                                  {"last_success_at": None, "last_probe_at": None},
                                  "account_id = ?", (account,))
        source = next(s for s in self.svc.ingest.source_health() if s["account_id"] == account)
        freshness = source["health_axes"]["freshness"]
        self.assertEqual(freshness["state"], "unknown")
        self.assertFalse(freshness["green"])
        self.assertNotEqual(freshness["severity"], "ok")
        self.assertIsNone(freshness["last_success_at"])
        self.assertTrue(freshness["reason"])

    def test_coverage_without_any_checkpoint_is_unknown_not_complete(self) -> None:
        """An account this deployment never scanned has unproven coverage, never 'complete'."""
        account = self.account_id("mock_beeper")
        with self.svc.store.tx():
            self.svc.store.conn.execute("DELETE FROM sync_checkpoint WHERE account_id = ?",
                                        (account,))
        source = next(s for s in self.svc.ingest.source_health() if s["account_id"] == account)
        coverage = source["health_axes"]["coverage"]
        self.assertEqual(coverage["state"], "unknown")
        self.assertFalse(coverage["green"])
        self.assertEqual(coverage["severity"], "unknown")
        self.assertTrue(coverage["reason"])

    def test_mock_sources_stay_visibly_labelled(self) -> None:
        for source in self.svc.ingest.source_health():
            with self.subTest(account=source["account_id"]):
                self.assertTrue(source["mock_label"].startswith("MOCK:"))
                self.assertTrue(source["disclosure"])


class GraceFaults:
    """A second service over the same database, with adapter faults injected."""

    def __init__(self, case: GraceTestCase, faults: tuple[str, ...]):
        from grace.service import Grace
        self.case = case
        self.svc = Grace(case.db, faults=faults)

    def source_health(self) -> list[dict]:
        try:
            return self.svc.ingest.source_health()
        finally:
            self.svc.close()


class TestTheClientRendersThreeAxes(unittest.TestCase):
    """The phone client must render the axes, and must not paint unknown green."""

    def setUp(self) -> None:
        self.js = (REPO_ROOT / "grace" / "webui" / "app.js").read_text()
        self.css = (REPO_ROOT / "grace" / "webui" / "app.css").read_text()

    def test_the_bundle_names_all_three_axes(self) -> None:
        for label in ("transport", "freshness", "coverage"):
            self.assertIn(label, self.js.lower())
        self.assertIn("health_axes", self.js)

    def test_only_explicitly_healthy_states_get_the_ok_class(self) -> None:
        """One explicit list decides 'ok' per axis, and 'unknown' is not in any of them."""
        self.assertIn("AXIS_HEALTHY", self.js)
        self.assertIn("axisSeverity", self.js)
        block = self.js.split("const AXIS_HEALTHY")[1].split("};")[0]
        self.assertNotIn("unknown", block, "unknown must never be a healthy state")
        for axis, healthy in (("transport", "connected"), ("freshness", "observed_now"),
                              ("coverage", "complete")):
            with self.subTest(axis=axis):
                self.assertRegex(block, rf"{axis}\s*:\s*\[\s*'{healthy}'\s*\]")
        self.assertIn(".state-unknown", self.css)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
