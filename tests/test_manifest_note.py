"""The rules-surface ``manifest_note`` is derived from the stored capability rows.

Outstanding from PR #4. The note used to be a fixed sentence -- a variant of "Nothing here
has been probed" -- which went on saying the same thing about a ledger that had since
recorded rows, and about one that had recorded a real measurement. The fix made the
sentence a derivation (``grace/service.py::capability_honesty`` ->
``grace/ingest.py::probe_provenance``) and this test pins it, because the regression is
invisible in any single snapshot: a hardcoded sentence looks right until the row
population changes.

So the test asserts the two properties a constant cannot have:

* the note **is** the derived sentence -- prefix plus ``capability_honesty()["explanation"]``
  -- and never the old fixed wording;
* the note **changes** when the row population changes, and the number it names is the
  number of rows that population holds (no rows / labelled mocks / one real measurement).

Read over the shipped HTTP surface (``GET /api/rules-source-health``), which is what the
client renders -- not by calling the derivation directly.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from grace.ingest import (import_probe_rows, probe_provenance,  # noqa: E402
                          stored_probe_rows)
from grace.service import Grace                             # noqa: E402
from grace.web import WebApp                                # noqa: E402
from switchboard_mini import outcomes as O                  # noqa: E402

TOKEN = "test-owner-token-manifest-note"
#: The wording that must never come back: a sentence about the row population that was
#: written into the code rather than derived from the rows.
HARDCODED_PHRASES = ("Nothing here has been probed", "No source has been contacted yet")


def a_row(capability: str, *, origin: str, supported: bool = False) -> dict:
    """One row in the worker's contract, measured or read out of the pack."""
    measured = origin in (O.REAL, O.FIXTURE)
    return {
        "capability": capability,
        "origin": origin,
        "supported": bool(supported),
        "state": O.SUCCESS if measured else O.PROBE_UNMEASURED,
        "permission_state": (O.PERMISSION_GRANTED if measured
                             else O.PERMISSION_NOT_DETERMINED),
        "observed_version": O.VERSION_NOT_OBSERVED,
        "probe_method": "test row",
        "probe_assertion": "what this row's supported flag would assert",
        "limitation": None,
        "evidence": {},
        "source": "beeper",
        "citations": ["O06"] if origin == O.DOCUMENTATION else [],
        "label": (O.fixture_label("beeper") if origin == O.FIXTURE
                  else (O.documentation_label("beeper") if origin == O.DOCUMENTATION
                        else None)),
        "values_from_source": bool(measured and supported),
        "real_source_connected": bool(origin == O.REAL and supported),
    }


class TestTheManifestNoteIsDerived(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "grace.sqlite3")
        # A ledger with no capability rows at all: the population the sentence has to speak
        # about before anything has been probed.
        self.svc = Grace(self.db)
        self._stderr = sys.stderr
        sys.stderr = io.StringIO()
        self.app = WebApp(self.db, TOKEN, host="127.0.0.1", port=0)
        self.app.start_background()
        self.port = self.app.port

    def tearDown(self) -> None:
        try:
            self.app.stop()
        finally:
            sys.stderr = self._stderr
            self.svc.close()
            self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def note(self) -> str:
        """The sentence the client renders, read off the shipped surface."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("GET", "/api/rules-source-health",
                         headers={"X-Switchboard-Token": TOKEN, "Connection": "close"})
            response = conn.getresponse()
            payload = json.loads(response.read())
        finally:
            conn.close()
        self.assertTrue(payload.get("ok"), payload)
        return payload["data"]["manifest_note"]

    def derived_prefix(self) -> str:
        return "A capability stays unsupported until a probe on Randy's Mac confirms it"

    def stored_count(self) -> int:
        return len(stored_probe_rows(self.svc.store))

    # -- the properties a hardcoded sentence cannot have ---------------------
    def test_the_note_is_the_derived_sentence_and_never_the_fixed_wording(self) -> None:
        note = self.note()
        explanation = self.svc.capability_honesty()["explanation"]
        self.assertTrue(note.startswith(self.derived_prefix()), note)
        self.assertIn(explanation, note)
        # An empty ledger: the derivation says so, in words that name the row count.
        self.assertIn("No capability rows have been measured", note)
        for phrase in HARDCODED_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, note)

    def test_the_note_changes_when_the_row_population_changes(self) -> None:
        empty = self.note()
        self.assertEqual(self.stored_count(), 0)

        # Population 2: the labelled mock rows the fixture corpus writes on seed.
        res = self.svc.seed(reset=True)
        self.assertTrue(res.ok, res.detail)
        seeded = self.note()
        self.assertNotEqual(seeded, empty, "the note ignored the seeded rows")
        self.assertIn(f"of the {self.stored_count()} capability rows", seeded)
        self.assertIn("recorded fixtures", seeded)
        for phrase in HARDCODED_PHRASES:
            self.assertNotIn(phrase, seeded)

        # Population 3: one capability measured from a real source (a Mac row). The
        # sentence must now be the other branch -- and must not still be saying that no
        # source was contacted.
        account = self.svc.store.scalar(
            "SELECT account_id FROM source_account WHERE adapter = ?", ("mock_mail",))
        imported = import_probe_rows(self.svc.store, account,
                                     [a_row("beeper_message_search", origin=O.REAL,
                                            supported=True),
                                      a_row("beeper_account_contacts",
                                            origin=O.DOCUMENTATION)])
        self.assertTrue(imported["ok"], imported)
        measured = self.note()
        self.assertNotEqual(measured, seeded, "the note ignored the measured row")
        self.assertIn(f"{len([r for r in stored_probe_rows(self.svc.store) if r['real_source_connected']])}"
                      f" of {self.stored_count()} capability rows answered from a real source",
                      measured)
        for phrase in HARDCODED_PHRASES:
            self.assertNotIn(phrase, measured)

    def test_the_note_follows_the_rows_and_not_the_other_way_round(self) -> None:
        """Re-importing the same population reproduces the same sentence, word for word."""
        self.assertTrue(self.svc.seed(reset=True).ok)
        first = self.note()
        second = self.note()
        self.assertEqual(first, second)
        self.assertEqual(first, self.derived_prefix() + " (PRD §11, Gate 2). "
                         + self.svc.capability_honesty()["explanation"])

    def test_the_derivation_is_not_a_constant(self) -> None:
        """The provenance statement itself moves with the rows it describes."""
        empty = probe_provenance([])
        self.assertEqual(empty["statement"],
                         "No capability rows have been measured: no source has been "
                         "contacted, and no capability is claimed.")
        one_real = probe_provenance([{"origin": O.REAL, "supported": True,
                                      "state": O.SUCCESS, "real_source_connected": True,
                                      "source": "beeper"}])
        self.assertNotEqual(one_real["statement"], empty["statement"])
        self.assertIn("1 of 1 capability rows answered from a real source",
                      one_real["statement"])
        self.assertFalse(one_real["no_source_contacted"])
        self.assertTrue(empty["no_source_contacted"])
        self.assertTrue(one_real["real_source_connected"])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
