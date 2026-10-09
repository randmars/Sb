"""A malformed probe document is a typed refusal, never a traceback.

Defect fixed here (reproduced on master ``7c38fd6``, 2026-10-09): ``grace/cli.py`` fed the
whole file to ``json.loads``, so importing a probe run in the form the worker actually
writes it -- **JSONL, one row per line, which is what ``switchboard-mini probe --out``
produces** -- crashed with::

    json.decoder.JSONDecodeError: Extra data: line 2 column 1 (char 1135)

Exit status 1 with a raw traceback is not a refusal: it tells Randy nothing he can act on
and it makes a malformed document look like a bug in Grace. Every shape below now answers
in the same shape as the command's other refusals -- a typed reason code, a message naming
the smallest next action, ``imported: 0`` and nothing stored, exit status 1.

Shapes covered (each with a case below):

* unwrapped JSONL (the natural artefact of ``probe --out``)
* an empty file
* a JSON array instead of an object
* valid JSON of the wrong shape (``rows`` not a list, no ``rows`` key, a single row object)
* a file that is not JSON at all
* a missing file
* a document with no rows at all (nothing to store is not a success)

Nothing here contacts a source, and no row is stored by any of it.
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

from grace import ingest as I                                        # noqa: E402
from grace.ingest import load_probe_document, stored_probe_rows      # noqa: E402

from tests.helpers import GraceTestCase, run_cli_raw                 # noqa: E402

ONE_ROW = {"capability": "manifest", "origin": "fixture", "supported": False,
           "state": "unsupported", "permission_state": "not_determined",
           "observed_version": "not_observed", "probe_method": "test row",
           "probe_assertion": "test assertion", "limitation": None, "evidence": {},
           "values_from_source": False, "real_source_connected": False,
           "measurement_target": "source", "label": "FIXTURE:test"}


def two_rows_jsonl() -> str:
    return "\n".join(json.dumps(dict(ONE_ROW, capability=f"cap_{i}"))
                     for i in (1, 2)) + "\n"


class TestTheLoaderAnswersEveryShapeWithATypedReason(unittest.TestCase):
    """The loader itself: one reason code per malformed shape, and no exception."""

    def load(self, text: str, name: str = "run.jsonl"):
        result = load_probe_document(text, source=name)
        self.assertIn("ok", result)
        return result

    def test_unwrapped_jsonl_names_the_wrap_as_the_smallest_next_action(self) -> None:
        result = self.load(two_rows_jsonl(), "/tmp/run.jsonl")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], I.PROBE_DOCUMENT_IS_JSONL)
        self.assertIn("jq -s '{rows: .}' /tmp/run.jsonl > /tmp/run.json",
                      result["next_action"])
        self.assertIn("JSONL", result["problem"])

    def test_an_empty_file_is_refused(self) -> None:
        for text in ("", "   \n\n"):
            with self.subTest(text=repr(text)):
                result = self.load(text)
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], I.PROBE_DOCUMENT_EMPTY)
                self.assertTrue(result["next_action"])

    def test_a_json_array_is_refused(self) -> None:
        result = self.load(json.dumps([ONE_ROW]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], I.PROBE_DOCUMENT_WRONG_SHAPE)

    def test_valid_json_of_the_wrong_shape_is_refused(self) -> None:
        for text in (json.dumps({"rows": {"not": "a list"}}),
                     json.dumps({"nothing": 1}),
                     json.dumps(ONE_ROW),
                     json.dumps("a string"),
                     json.dumps(3)):
            with self.subTest(text=text[:40]):
                result = self.load(text)
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], I.PROBE_DOCUMENT_WRONG_SHAPE)

    def test_a_file_that_is_not_json_at_all_is_refused(self) -> None:
        result = self.load("this is not JSON {{{")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], I.PROBE_DOCUMENT_NOT_JSON)

    def test_a_document_with_no_rows_is_not_a_success(self) -> None:
        result = self.load(json.dumps({"rows": []}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], I.PROBE_DOCUMENT_NO_ROWS)

    def test_a_wrapped_document_is_accepted_and_keeps_its_rows(self) -> None:
        rows = [dict(ONE_ROW, capability="cap_1")]
        result = self.load(json.dumps({"rows": rows}))
        self.assertTrue(result["ok"], result)
        self.assertEqual([r["capability"] for r in result["rows"]], ["cap_1"])

    def test_every_reason_code_is_named_and_distinct(self) -> None:
        codes = (I.PROBE_DOCUMENT_UNREADABLE, I.PROBE_DOCUMENT_EMPTY,
                 I.PROBE_DOCUMENT_NOT_JSON, I.PROBE_DOCUMENT_IS_JSONL,
                 I.PROBE_DOCUMENT_WRONG_SHAPE, I.PROBE_DOCUMENT_NO_ROWS)
        self.assertEqual(len(set(codes)), len(codes))
        for code in codes:
            self.assertTrue(code.startswith("probe_document_"), code)


class TestTheCliRefusesMalformedInput(GraceTestCase):
    """Exit status, refused output and an untouched ledger, for every malformed shape."""

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_mail")
        self.before = stored_probe_rows(self.svc.store)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_import(self, name: str, text: str, reason: str) -> dict:
        path = self.dir / name
        path.write_text(text)
        return self.assert_refused(path, name, reason)

    def assert_refused(self, path: Path, name: str, reason: str) -> dict:
        proc = run_cli_raw(self.db, "probe-import", "--file", str(path),
                           "--account", self.account)
        self.assertNotIn("Traceback", proc.stderr, proc.stderr)
        self.assertEqual(proc.stderr, "", proc.stderr)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)          # a JSON document, not a traceback
        self.assertFalse(payload["ok"], payload)
        self.assertEqual(payload["command"], "probe-import")
        data = payload["data"]
        self.assertEqual(data["reason"], reason, payload)
        self.assertEqual(data["imported"], 0, payload)
        self.assertEqual(data["refused"], 0, payload)
        self.assertTrue(data["problems"], payload)
        self.assertTrue(data["next_action"], payload)
        self.assertIn("nothing stored", payload["message"])
        # nothing was stored, for the whole ledger
        self.assertEqual(stored_probe_rows(self.svc.store), self.before)
        self.assertEqual(name, path.name)
        return payload

    def test_unwrapped_jsonl_is_refused_with_the_wrap_as_the_next_action(self) -> None:
        payload = self.run_import("run.jsonl", two_rows_jsonl(), I.PROBE_DOCUMENT_IS_JSONL)
        self.assertEqual(payload["data"]["reason"], I.PROBE_DOCUMENT_IS_JSONL)
        self.assertIn("jq -s '{rows: .}'", payload["message"])
        self.assertIn(str(self.dir / "run.jsonl"), payload["message"])

    def test_an_empty_file_is_refused(self) -> None:
        self.run_import("empty.json", "", I.PROBE_DOCUMENT_EMPTY)

    def test_a_json_array_is_refused(self) -> None:
        payload = self.run_import("array.json", json.dumps([ONE_ROW]),
                                  I.PROBE_DOCUMENT_WRONG_SHAPE)
        self.assertEqual(payload["data"]["reason"], I.PROBE_DOCUMENT_WRONG_SHAPE)
        self.assertIn("rows", payload["message"])

    def test_valid_json_of_the_wrong_shape_is_refused(self) -> None:
        self.run_import("rows_not_a_list.json", json.dumps({"rows": "nope"}),
                        I.PROBE_DOCUMENT_WRONG_SHAPE)
        self.run_import("no_rows_key.json", json.dumps({"capabilities": []}),
                        I.PROBE_DOCUMENT_WRONG_SHAPE)
        self.run_import("one_row.json", json.dumps(ONE_ROW), I.PROBE_DOCUMENT_WRONG_SHAPE)

    def test_a_file_that_is_not_json_at_all_is_refused(self) -> None:
        payload = self.run_import("garbage.json", "probe output, but not JSON",
                                  I.PROBE_DOCUMENT_NOT_JSON)
        self.assertEqual(payload["data"]["reason"], I.PROBE_DOCUMENT_NOT_JSON)

    def test_a_missing_file_is_refused(self) -> None:
        payload = self.assert_refused(self.dir / "not-there.json", "not-there.json",
                                      I.PROBE_DOCUMENT_UNREADABLE)
        self.assertIn("--out", payload["data"]["next_action"])

    def test_a_document_with_no_rows_is_refused_rather_than_called_a_success(self) -> None:
        payload = self.run_import("no-rows.json", json.dumps({"rows": []}),
                                  I.PROBE_DOCUMENT_NO_ROWS)
        self.assertEqual(payload["data"]["reason"], I.PROBE_DOCUMENT_NO_ROWS)

    def test_every_refusal_is_the_same_shape_as_the_commands_other_refusals(self) -> None:
        """The reason/next-action pair is additive: the refusal keys are unchanged."""
        payload = self.run_import("garbage.json", "not json", I.PROBE_DOCUMENT_NOT_JSON)
        for key in ("imported", "refused", "problems", "provenance", "supersessions",
                    "refusals", "ok"):
            self.assertIn(key, payload["data"])

    def test_a_wrapped_document_still_imports(self) -> None:
        path = self.dir / "wrapped.json"
        path.write_text(json.dumps({"rows": [dict(ONE_ROW, capability="cap_1")]}))
        proc = run_cli_raw(self.db, "probe-import", "--file", str(path),
                           "--account", self.account)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"]["imported"], 1)
        self.assertNotIn("reason", payload["data"])
