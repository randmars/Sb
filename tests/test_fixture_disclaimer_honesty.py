"""One source's fixture wording must never be another source's fixture wording.

The hole this test closes. ``outcomes.fixture_disclaimer(source)`` falls back to
``FIXTURE_DISCLAIMER`` (the Mail wording) for a source with no entry in
``FIXTURE_DISCLAIMER_BY_SOURCE``. The Hermes slice shipped without an entry, so every
Hermes row told the reader *"No Mail.app was contacted and no mailbox was read"* — a
sentence about a source the row never touched, on a row that never mentioned the Hermes
gateway at all. The test that existed at the time could not catch it because it derived
the expected string from the *same helper*: it compared the fallback to the fallback.

So this test never asks the helper what it expects. It names, independently:

* every source the probe declares, read from ``probe.CAPABILITIES`` rather than from a
  hand-kept list, so a source added later is covered the day it appears;
* the phrase each source's wording must contain (its own app), and the phrases it must
  never contain (every other source's app).

Nothing here contacts anything: it is a test of the words a fixture row would carry.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "mini")):
    if path not in sys.path:
        sys.path.insert(0, path)

from switchboard_mini import outcomes as O     # noqa: E402
from switchboard_mini import probe as P        # noqa: E402

#: The app phrase each source's fixture wording must name — and that no other source's
#: wording may name. A distinct multi-word phrase, so a passing mention in prose cannot
#: be mistaken for the app itself.
OWN_APP = {
    "mail": "Mail.app",
    "beeper": "Beeper Desktop",
    "contacts": "Contacts database",
    "hermes": "Hermes gateway",
}


class TestEachSourceOwnsItsFixtureDisclaimer(unittest.TestCase):
    def sources(self) -> list:
        """Every source the probe declares, from the capabilities' own declarations."""
        return sorted({c.source for c in P.CAPABILITIES})

    def test_the_probe_declares_exactly_the_sources_this_test_covers(self) -> None:
        self.assertEqual(self.sources(), sorted(OWN_APP))

    def test_every_source_has_its_own_entry_so_none_falls_back(self) -> None:
        # The defect in one line: `hermes` had no entry. A source with no entry inherits
        # Mail's wording, which is the bug — so a missing entry must fail here.
        self.assertEqual(sorted(O.FIXTURE_DISCLAIMER_BY_SOURCE), self.sources())

    def test_only_mail_carries_the_default_wording(self) -> None:
        for source in self.sources():
            text = O.fixture_disclaimer(source)
            with self.subTest(source=source):
                if source == "mail":
                    self.assertEqual(text, O.FIXTURE_DISCLAIMER)
                else:
                    self.assertNotEqual(
                        text, O.FIXTURE_DISCLAIMER,
                        f"{source} carries the Mail fallback wording")

    def test_each_disclaimer_names_its_own_source_and_no_other_source_app(self) -> None:
        for source in self.sources():
            text = O.fixture_disclaimer(source)
            with self.subTest(source=source):
                self.assertIn(OWN_APP[source], text)
                self.assertIn("FIXTURE", text)
                self.assertIn("not observations of any", text)
                for other, other_app in OWN_APP.items():
                    if other == source:
                        continue
                    self.assertNotIn(
                        other_app, text,
                        f"the {source} fixture disclaimer names {other}'s app ({other_app})")
