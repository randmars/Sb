"""The Contacts read path: what is proven here, and what is only written here.

Three kinds of check live in this module, and they are labelled because they prove
different things:

* **fixture checks** drive the whole worker (the real CLI, as installed) against the nine
  recorded Contacts scenarios. They prove the typed-state path, the row path and the
  labelling: a fixture row can never come out ``supported: true``, every refusal is typed,
  and the permission vocabulary carries macOS's own statuses including ``restricted``.
* **host checks** run the real adapter on this Linux computer. Contacts cannot be read
  here, so every command must answer with a typed ``host_not_macos`` refusal -- no
  traceback, and nothing claiming to have contacted a source.
* **stand-in checks** execute the shipped JavaScript helper text itself under Node, against
  a stand-in Objective-C bridge written in this test. They prove the helper's *logic* --
  above all that no read path can issue a fetch while the authorization status is not
  ``authorized``, which is the property that keeps a probe from raising the system consent
  dialog. They prove nothing about macOS: the bridge, the framework and the store are all
  invented here, and the test says so in its own messages. What only Randy's Mac can settle
  is the real bridge, the real store and the real timestamp/identifier behaviour.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from switchboard_mini import cli, outcomes as O
from switchboard_mini.contacts_adapter import (HEALTHY_SOURCE_STATES,
                                               build_adapter as build_contacts_adapter)
from switchboard_mini.contacts_probe import DOCUMENTED_EVENT_CLASSES
from switchboard_mini.contacts_transport import (AUTHORIZATION_STATUS_NAMES,
                                                 AUTHORIZATION_VOCABULARY,
                                                 CALL_SOURCES, FIXTURE_SCENARIOS, HELPER,
                                                 JXA_HELPER, KEY_SOURCES, MINIMAL_KEYS,
                                                 RESULT_MARKER, SCHEMA, build_script,
                                                 compare_identifier_fingerprints,
                                                 extract_response, interpret_document,
                                                 load_fixture, permission_state_for,
                                                 status_without_an_equivalent)
from switchboard_mini.mail_adapter import build_adapter as build_mail_adapter
from switchboard_mini.probe import run_probe
from switchboard_mini.version import WORKER_VERSION

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO_ROOT, "mini", "bin", "switchboard-mini")

CONTACTS_CAPABILITIES = ("contacts_read_authorization", "contacts_enumerate_contacts",
                         "contacts_identifier_scope", "contacts_unified_constituents",
                         "contacts_change_history")
#: One command per read the Contacts family ships, with the flags each needs. Used for the
#: sweep that proves every command answers in every scenario.
COMMANDS = (
    (["authorization"], ()),
    (["request-access"], ()),
    (["health"], ()),
    (["enumerate"], ()),
    (["enumerate", "--unify-off"], ()),
    (["restricted-keys"], ()),
    (["restricted-keys", "--key-symbol", "CNContactNoteKey"], ()),
    (["change-history"], ()),
    (["change-history", "--invalid-token"], ()),
    (["probe"], ()),
)


def run_worker(args, *, env=None):
    """Run the installed launcher exactly as Randy's runbook does."""
    proc = subprocess.run(["bash", LAUNCHER] + list(args), capture_output=True, text=True,
                          timeout=180, env=env)
    return proc


def rows_from(text: str) -> list:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            row = json.loads(line)
            if "capability" in row:
                rows.append(row)
    return rows


def documents_from(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and "code" in doc:
            out.append(doc)
    return out


def first_json_object(text: str) -> dict:
    """The first JSON object on a line, whatever keys it has.

    ``documents_from`` finds *outcome* documents (they carry ``code``). ``version`` prints a
    description of the worker, not an outcome, so it is read with this instead of being
    mistaken for a command that produced no answer.
    """
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON document in the output: {text[:400]!r}")


class ContactsFixtureTests(unittest.TestCase):
    """The recorded scenarios, driven through the real CLI."""

    def test_every_command_answers_in_every_scenario(self):
        for scenario in FIXTURE_SCENARIOS:
            for command, _flags in COMMANDS:
                with self.subTest(scenario=scenario, command=" ".join(command)):
                    proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                                       scenario] + command)
                    self.assertNotIn("Traceback", proc.stderr + proc.stdout)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    docs = documents_from(proc.stdout) or rows_from(proc.stdout)
                    self.assertTrue(docs, f"{scenario} {command} produced no document")

    def test_every_document_is_labelled_a_fixture_for_contacts(self):
        for scenario in FIXTURE_SCENARIOS:
            proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario", scenario,
                               "enumerate"])
            doc = documents_from(proc.stdout)[0]
            self.assertEqual(doc["origin"], O.FIXTURE)
            self.assertTrue(doc["label"].startswith("FIXTURE:contacts"), doc["label"])
            self.assertFalse(doc["source_contacted"])
            self.assertFalse(doc["real_source_connected"])
            self.assertIn("No Contacts database was read", doc["disclaimer"])

    def test_the_permission_vocabulary_carries_every_documented_status(self):
        expected = {
            "authorization_granted": O.PERMISSION_GRANTED,
            "authorization_denied": O.PERMISSION_STATE_DENIED,
            "authorization_not_determined": O.PERMISSION_NOT_DETERMINED,
            "authorization_restricted": O.PERMISSION_RESTRICTED,
        }
        for scenario, state in expected.items():
            with self.subTest(scenario=scenario):
                proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                                   scenario, "authorization"])
                doc = documents_from(proc.stdout)[0]
                self.assertEqual(doc["data"]["permission_state"], state)
                self.assertIn(state, O.PERMISSION_STATES)
                self.assertFalse(doc["data"]["prompt_requested"])

    def test_a_read_without_a_grant_refuses_and_never_prompts(self):
        for scenario in ("authorization_denied", "authorization_not_determined",
                         "authorization_restricted"):
            for command in ("enumerate", "change-history"):
                with self.subTest(scenario=scenario, command=command):
                    proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                                       scenario, command])
                    doc = documents_from(proc.stdout)[0]
                    self.assertEqual(doc["code"], O.PERMISSION_DENIED)
                    self.assertIn("consent dialog", doc["detail"])
                    self.assertIn(doc["reason"], ("contacts_access_denied",
                                                  "contacts_access_restricted",
                                                  "contacts_authorization_not_determined"))
                    self.assertTrue(doc["data"]["prompt_requested"] is False)
                    self.assertFalse(doc["source_contacted"])
                    # The grant is a separate command, and the refusal says which one.
                    self.assertIn("request-access", (doc.get("next_action") or ""))

    def test_the_bridge_refusal_is_the_named_typed_state(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "bridge_unavailable", "enumerate"])
        doc = documents_from(proc.stdout)[0]
        self.assertEqual(doc["code"], O.UNSUPPORTED)
        self.assertEqual(doc["reason"], "helper_missing")
        self.assertFalse(doc["real_source_connected"])

    def test_enumerate_returns_identifiers_as_fingerprints_and_no_values(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "authorization_granted", "enumerate"])
        doc = documents_from(proc.stdout)[0]
        items = doc["data"]["items"]
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertTrue(item["identifier_fingerprint"])
            self.assertEqual(item["identifier_length"], 36)
            self.assertEqual(item["identifier_shape"], "uuid-shaped")
            self.assertNotIn("identifier", item)
        self.assertFalse(doc["data"]["values_emitted"])
        # No name, address or number anywhere in the emitted document.
        self.assertNotIn("S0000000", json.dumps(doc))

    def test_identifier_comparison_needs_two_runs_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ids.txt")
            first = compare_identifier_fingerprints(path, ["a", "b"])
            self.assertEqual(first["persistence_across_runs"], "first run recorded")
            second = compare_identifier_fingerprints(path, ["a", "b"])
            self.assertEqual(second["persistence_across_runs"], "stable")
            third = compare_identifier_fingerprints(path, ["a"])
            self.assertEqual(third["persistence_across_runs"], "changed_or_incomplete")
            mode = oct(os.stat(path).st_mode & 0o777)
            self.assertEqual(mode, "0o600")
            self.assertFalse(compare_identifier_fingerprints(None, ["a"])["persistence_across_runs"]
                             != "not evaluated")

    def test_change_history_reports_the_documented_reset_sequence_as_a_trigger(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "change_history_reset", "probe"])
        rows = {row["capability"]: row for row in rows_from(proc.stdout)}
        row = rows["contacts_change_history"]
        reset = row["evidence"]["reset_attempt"]
        self.assertTrue(reset["drop_event_first"])
        self.assertTrue(reset["not_a_genuine_reset"])
        self.assertIn("deliberately invalid", reset["trigger"])
        self.assertEqual(reset["outcome"], "success")
        self.assertIn("CNChangeHistoryDropEverythingEvent",
                      row["evidence"]["reset_attempt"]["event_counts"])
        self.assertFalse(row["supported"])
        self.assertTrue(row["evidence"]["token_value_recorded"] is False)

    def test_the_unification_toggle_absent_is_reported_as_the_negative_branch(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "change_history_steady", "probe"])
        rows = {row["capability"]: row for row in rows_from(proc.stdout)}
        row = rows["contacts_unified_constituents"]
        self.assertEqual(row["evidence"]["assertion_branch"], "negative_recorded")
        self.assertFalse(row["evidence"]["unify_off_selector_present"])
        self.assertFalse(row["supported"])
        self.assertEqual(row["state"], O.PROBE_UNMEASURED)

    def test_the_unification_toggle_present_gives_a_positive_branch(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "authorization_granted", "probe"])
        rows = {row["capability"]: row for row in rows_from(proc.stdout)}
        row = rows["contacts_unified_constituents"]
        self.assertEqual(row["evidence"]["assertion_branch"], "individual_records_returned")
        self.assertEqual(row["evidence"]["identifiers_in_both_reads"], 0)
        self.assertFalse(row["supported"])       # a fixture is never supported
        self.assertEqual(row["evidence"]["individual_returned"], 5)

    def test_restricted_keys_records_the_guarded_failure(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "restricted_keys", "probe"])
        rows = {row["capability"]: row for row in rows_from(proc.stdout)}
        row = rows["contacts_enumerate_contacts"]
        self.assertTrue(row["evidence"]["restricted_key_plain_fetch_ok"])
        self.assertTrue(row["evidence"]["restricted_key_guarded_fetch_raised"])
        self.assertTrue(row["evidence"]["restricted_key_symbol_defined_at_runtime"])
        self.assertIn("no notes-guarded key symbol is documented",
                      row["evidence"]["restricted_key_ask"])

    def test_no_fixture_row_is_ever_supported(self):
        for scenario in FIXTURE_SCENARIOS:
            proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario", scenario,
                               "probe"])
            rows = rows_from(proc.stdout)
            self.assertEqual(sorted(row["capability"] for row in rows),
                             sorted(CONTACTS_CAPABILITIES))
            for row in rows:
                with self.subTest(scenario=scenario, capability=row["capability"]):
                    self.assertFalse(row["supported"], row)
                    self.assertEqual(row["origin"], O.FIXTURE)
                    self.assertTrue(row["label"].startswith("FIXTURE:contacts"), row["label"])
                    self.assertFalse(row["real_source_connected"], row)
                    self.assertEqual(row["citations"], [])
                    self.assertIn("recorded fixture", row["limitation"])

    def test_each_row_names_the_documentation_row_it_supersedes(self):
        proc = run_worker(["--fixture-mode", "contacts", "probe"])
        rows = rows_from(proc.stdout)
        for row in rows:
            with self.subTest(capability=row["capability"]):
                self.assertIsInstance(row["supersedes"], dict)
                self.assertEqual(row["supersedes"]["capability"], row["capability"])
                self.assertTrue(row["supersedes"]["citations"])
                self.assertEqual(row["supersedes"]["documented_state"], O.PROBE_UNMEASURED)

    def test_the_empty_database_is_a_result_and_not_a_defect(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "contacts_authorized_user_choice", "probe"])
        rows = {row["capability"]: row for row in rows_from(proc.stdout)}
        row = rows["contacts_enumerate_contacts"]
        self.assertEqual(row["state"], O.PROBE_UNMEASURED)
        self.assertFalse(row["supported"])
        self.assertIn("empty Contacts database is a result, not a defect", row["limitation"])
        self.assertFalse(row["evidence"]["assertion_evaluated"])

    def test_the_authorization_row_names_the_declaration_gap(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "authorization_granted", "probe"])
        rows = {row["capability"]: row for row in rows_from(proc.stdout)}
        row = rows["contacts_read_authorization"]
        self.assertIsNone(row["evidence"]["declaration_in_an_info_plist"])
        self.assertIn("Info.plist", row["limitation"])
        self.assertFalse(row["supported"])
        self.assertTrue(row["evidence"]["denial_observed"] is False)

    def test_denied_status_is_observed_and_typed(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "authorization_denied", "probe"])
        rows = {row["capability"]: row for row in rows_from(proc.stdout)}
        row = rows["contacts_read_authorization"]
        self.assertTrue(row["evidence"]["denial_observed"])
        self.assertTrue(row["evidence"]["non_granted_status_typed_as_a_typed_state"])
        self.assertEqual(row["permission_state"], O.PERMISSION_STATE_DENIED)
        self.assertFalse(row["supported"])

    def test_restricted_status_keeps_its_own_permission_state(self):
        proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario",
                           "authorization_restricted", "probe"])
        rows = {row["capability"]: row for row in rows_from(proc.stdout)}
        row = rows["contacts_read_authorization"]
        self.assertEqual(row["permission_state"], O.PERMISSION_RESTRICTED)
        self.assertNotEqual(row["permission_state"], O.PERMISSION_STATE_DENIED)
        self.assertFalse(row["supported"])

    def test_probe_out_writes_the_same_rows_it_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "rows.jsonl")
            proc = run_worker(["--fixture-mode", "contacts", "probe", "--out", out])
            with open(out, "r", encoding="utf-8") as handle:
                written = rows_from(handle.read())
            printed = rows_from(proc.stdout)
            self.assertEqual(len(written), len(printed))
            self.assertEqual([r["capability"] for r in written],
                             [r["capability"] for r in printed])

    def test_the_fixture_mode_sweep_never_writes_a_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = os.path.join(tmp, "token")
            run_worker(["--fixture-mode", "contacts", "--token-file", token, "change-history"])
            self.assertFalse(os.path.exists(token),
                             "fixture mode must not persist anything that looks like a token")

    def test_version_names_the_contacts_scenarios(self):
        proc = run_worker(["version"])
        doc = first_json_object(proc.stdout)
        self.assertEqual(doc["worker_version"], WORKER_VERSION)
        self.assertEqual(sorted(doc["contacts_fixture_scenarios"]), sorted(FIXTURE_SCENARIOS))


class ContactsHostTests(unittest.TestCase):
    """The real adapter on this Linux computer: typed refusals, never a claim."""

    def test_every_read_refuses_with_host_not_macos(self):
        for command, _flags in COMMANDS[:-1]:
            with self.subTest(command=" ".join(command)):
                proc = run_worker(["contacts"] + command)
                self.assertNotIn("Traceback", proc.stderr + proc.stdout)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                doc = documents_from(proc.stdout)[0]
                self.assertEqual(doc["code"], O.UNSUPPORTED)
                self.assertEqual(doc["reason"], "host_not_macos")
                self.assertIn("platform=linux", doc["detail"])
                self.assertFalse(doc["source_contacted"])
                self.assertFalse(doc["real_source_connected"])
                # A real outcome carries no label at all (``Outcome.to_dict`` adds one only
                # for a non-real value, and then it must be a FIXTURE: label): the refusal
                # must not look like a recording of anything.
                self.assertIsNone(doc.get("label"))
                self.assertNotIn("label", doc)

    def test_the_real_adapter_is_not_a_fixture_and_claims_no_source(self):
        adapter = build_contacts_adapter()
        self.assertTrue(adapter.adapter_is_real)
        self.assertFalse(adapter.origin == O.FIXTURE)
        for outcome in (adapter.authorization(), adapter.request_access(),
                        adapter.enumerate_contacts(), adapter.restricted_keys("CNContactNoteKey"),
                        adapter.change_history()):
            with self.subTest(operation=outcome.code):
                self.assertFalse(outcome.usable)
                self.assertFalse(outcome.source_contacted)
                self.assertFalse(outcome.real_source_connected)
                self.assertEqual(outcome.reason, "host_not_macos")

    def test_the_host_refusal_rows_are_unsupported_with_no_values(self):
        proc = run_worker(["contacts", "probe"])
        rows = rows_from(proc.stdout)
        self.assertEqual(sorted(row["capability"] for row in rows),
                         sorted(CONTACTS_CAPABILITIES))
        for row in rows:
            with self.subTest(capability=row["capability"]):
                self.assertFalse(row["supported"])
                self.assertEqual(row["state"], O.UNSUPPORTED)
                self.assertEqual(row["origin"], O.REAL)
                self.assertTrue(row["adapter_is_real"])
                self.assertFalse(row["values_from_source"])
                self.assertFalse(row["real_source_connected"])
                self.assertIn("platform=linux", row["limitation"])
                self.assertEqual(row["permission_state"], O.PERMISSION_NOT_DETERMINED)

    def test_without_a_contacts_adapter_the_rows_stay_documentation(self):
        """The Mail-only run must still produce the five Contacts rows, as documentation."""
        adapter = build_mail_adapter(fixture_mode=True, fixture_scenario="granted", max_scan=20)
        run = run_probe([adapter])
        rows = {row["capability"]: row for row in run.rows}
        for capability in CONTACTS_CAPABILITIES:
            with self.subTest(capability=capability):
                row = rows[capability]
                self.assertEqual(row["origin"], O.DOCUMENTATION)
                self.assertFalse(row["supported"])
                self.assertEqual(row["state"], O.PROBE_UNMEASURED)
                self.assertFalse(row["values_from_source"])
                self.assertIsNone(row["supersedes"])

    def test_the_probe_run_with_every_adapter_labels_each_source(self):
        proc = run_worker(["--fixture-mode", "probe"])
        rows = rows_from(proc.stdout)
        labels = {row["source"]: row["label"] for row in rows}
        self.assertTrue(labels["contacts"].startswith("FIXTURE:contacts"))
        self.assertTrue(labels["beeper"].startswith("FIXTURE:beeper"))
        self.assertTrue(labels["mail"].startswith("FIXTURE:mail"))
        for row in rows:
            with self.subTest(capability=row["capability"]):
                self.assertFalse(row["real_source_connected"])
                self.assertFalse(row["adapter_is_real"])
                if row["origin"] == O.FIXTURE:
                    self.assertTrue(O.is_fixture_label(row["label"]), row["label"])
                else:
                    # A capability no adapter in this run measured is carried as the pack's
                    # documentation row, and is labelled as documentation -- never as a read.
                    self.assertEqual(row["origin"], O.DOCUMENTATION, row["capability"])
                    self.assertTrue(O.is_documentation_label(row["label"]), row["label"])
                    continue
                if row.get("measurement_target") == "worker":
                    # The one frozen worker self-measurement: it is about the worker, not a
                    # source, and it is the only row in the product allowed to be supported
                    # without a source read (PR #6).
                    self.assertTrue(row["supported"])
                    continue
                if row["source"] != "contacts":
                    # Mail's and Beeper's fixture rows report their assertion as having held
                    # against the *recording* (``supported: true``, PR #4/#5). That is a
                    # pre-existing decision about those two sources, outside this slice: the
                    # rows above are checked to claim no real source and to be labelled
                    # FIXTURE:, so the claim is scoped to a recording and says so. It is
                    # reported to the lead rather than silently changed here; the rule this
                    # slice adds -- a recorded Contacts scenario is never a measurement --
                    # is asserted below and in test_no_fixture_row_is_ever_supported.
                    continue
                self.assertFalse(row["supported"], row["capability"])


class ContactsCallSourceTests(unittest.TestCase):
    """Nothing is called that no pack record or Apple page names."""

    def test_every_call_and_key_names_its_source(self):
        for name, (source, quote) in list(CALL_SOURCES.items()) + list(KEY_SOURCES.items()):
            with self.subTest(name=name):
                self.assertTrue(source.strip(), name)
                self.assertGreater(len(quote), 20, name)
                self.assertIn("http", source + quote, name)

    def test_the_undocumented_names_say_they_are_undocumented(self):
        uncertain = {name: entry for name, entry in CALL_SOURCES.items()
                     if "NOT documented in the pack" in entry[0]}
        self.assertIn("CNContactFetchRequest.setShouldUnifyResults:", uncertain)
        self.assertIn("CNContactStore.enumerateContactsWithFetchRequest:error:usingBlock:",
                      uncertain)
        for name, (_source, quote) in uncertain.items():
            with self.subTest(name=name):
                self.assertIn("respondsToSelector", quote)
                self.assertIn("undocumented", quote.lower())

    def test_the_script_only_reads_and_never_writes_to_contacts(self):
        for forbidden in ("saveRequest", "addContact", "deleteContact", "executeSaveRequest",
                          "updateContact"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, JXA_HELPER)

    def test_the_script_guards_every_fetch_behind_the_authorization_read(self):
        """The no-prompt property, asserted structurally as well as by running it."""
        self.assertIn("function authorizeForRead(store, doc)", JXA_HELPER)
        for operation in ("enumerate", "restrictedKeys", "changeHistory"):
            with self.subTest(operation=operation):
                body = JXA_HELPER.split(f"function {operation}(store)")[1].split("\nfunction ")[0]
                self.assertIn("authorizeForRead(store, doc)", body)
                guard = body.index("authorizeForRead(store, doc)")
                for fetch in ("runEnumeration", "enumeratorForChangeHistoryFetchRequestError"):
                    if fetch in body:
                        self.assertLess(guard, body.index(fetch), operation)
        self.assertIn("prompt_requested: false", JXA_HELPER)

    def test_the_helper_never_emits_a_contact_value(self):
        for forbidden in ("givenName.js", "familyName.js", "emailAddresses.js",
                          "phoneNumbers.js", "note.js"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, JXA_HELPER)

    def test_the_marker_and_schema_are_single_sourced(self):
        self.assertIn(RESULT_MARKER, build_script({"operation": "authorization_status"}))
        self.assertIn(json.dumps(SCHEMA), build_script({"operation": "authorization_status"}))
        self.assertIn(HELPER, build_script({"operation": "authorization_status"}))
        self.assertIn(json.dumps(list(MINIMAL_KEYS)),
                      build_script({"operation": "authorization_status"}))

    def test_extract_response_records_stray_output_instead_of_dropping_it(self):
        stdout = ("osascript: some notice\n" + RESULT_MARKER + '{"outcome": "ok"}\n')
        document, other, malformed = extract_response(stdout)
        self.assertEqual(document, {"outcome": "ok"})
        self.assertEqual(other, ["osascript: some notice"])
        self.assertEqual(malformed, [])
        document, other, malformed = extract_response(RESULT_MARKER + "not json\n")
        self.assertIsNone(document)
        self.assertTrue(malformed)

    def test_a_second_marked_line_is_recorded_not_taken_as_the_answer(self):
        stdout = (RESULT_MARKER + '{"outcome": "ok", "which": 1}\n'
                  + RESULT_MARKER + '{"outcome": "ok", "which": 2}\n')
        document, _other, malformed = extract_response(stdout)
        self.assertEqual(document["which"], 1)
        self.assertTrue(malformed)


STAND_IN_HARNESS = r"""
// A STAND-IN Objective-C bridge for the shipped helper. Nothing here is macOS: the store,
// the framework objects and the data are invented in the test that writes this file, so a
// green run proves the helper's logic and proves nothing about the real Contacts framework.
const fs = require('fs');
const vm = require('vm');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const code = fs.readFileSync(process.argv[3], 'utf8');
const calls = [];
const lines = [];

function box(s) { return { js: s, isNil: () => false }; }
function nilBox() { return { isNil: () => true }; }
// JXA reaches a class instance as a *property* (`$.NSFoo.alloc.init`) and sometimes as a
// call. The stand-in answers both by making every object it hands over callable.
function callable(methods) {
  const fn = function () { return fn; };
  return Object.assign(fn, methods);
}

const contacts = [];
for (let i = 0; i < (cfg.items || 0); i++) {
  contacts.push(callable({
    identifier: box('ST' + String(i + 1).padStart(6, '0') + '-0000-4000-8000-000000000000'),
    isKeyAvailable: () => true,
    givenName: box('Given' + (i + 1)),
    familyName: box('Family' + (i + 1)),
    emailAddresses: { count: (i + 1) >= (cfg.email_from || 1) ? 1 : 0 },
    phoneNumbers: { count: (i + 1) >= (cfg.phone_from || 2) ? 1 : 0 },
    isNil: () => false
  }));
}

function responds(sel) {
  const table = {
    'authorizationStatusForEntityType:': cfg.has_status_selector !== false,
    'requestAccessForEntityType:completionHandler:': cfg.has_request_access !== false,
    'enumerateContactsWithFetchRequest:error:usingBlock:': cfg.has_enumerate_selector !== false,
    'enumeratorForChangeHistoryFetchRequest:error:': cfg.has_history_selector !== false
  };
  return table[sel] === undefined ? false : table[sel];
}

const store = callable({
  respondsToSelector: responds,
  authorizationStatusForEntityType: function () { return cfg.status === undefined ? 3 : cfg.status; },
  requestAccessForEntityTypeCompletionHandler: function (entity, cb) { cb(!!cfg.granted, null); },
  enumerateContactsWithFetchRequestErrorUsingBlock: function (req, errRef, block) {
    calls.push('fetch');
    if (cfg.fetch === 'throw') { throw new Error('stand-in: the fetch threw'); }
    const stop = [false];
    for (const c of contacts) { block(c, stop); }
  },
  enumeratorForChangeHistoryFetchRequestError: function (req, errRef) {
    calls.push('history');
    calls.push('startingToken:' + (req.startingToken === undefined ? 'absent' : 'present'));
    if (cfg.history === 'nil') { return null; }
    const names = cfg.history_events || [];
    // JXA calls a *zero-argument* ObjC method by property access: `events.objectEnumerator` is
    // `-objectEnumerator`, and each read of `en.nextObject` is a call to `-nextObject`. The
    // stand-in answers the same way -- a property holding the enumerator, and a getter for
    // nextObject -- so the helper's loop advances here exactly as it would on the Mac.
    let i = 0;
    const enumerator = { isNil: () => false };
    Object.defineProperty(enumerator, 'nextObject', {
      get: () => (i < names.length ? { className: box(names[i++]), isNil: () => false } : null)
    });
    const events = { isNil: () => false, objectEnumerator: enumerator };
    let token = nilBox();
    if (cfg.token_bytes) {
      // A plain object, not the callable wrapper: the helper reads `token.length`, and a
      // JavaScript function's own `length` is read-only, so the wrapper cannot carry it.
      token = {
        length: Buffer.byteLength(cfg.token_bytes), isNil: () => false,
        writeToFileAtomically: (p) => { fs.writeFileSync(p, cfg.token_bytes); return true; }
      };
    }
    return callable({ isNil: () => false, value: events, currentHistoryToken: token });
  }
});

function request(cls) {
  return callable({
    respondsToSelector: (sel) => (sel === 'setShouldUnifyResults:'
                                  ? cfg.has_unify_toggle !== false : false),
    shouldUnifyResults: true, includeGroupChanges: false, mutableObjects: false,
    startingToken: undefined
  });
}

global.ObjC = { import: function (name) {
  calls.push('import:' + name);
  if (cfg.bridge === 'throw') { throw new Error('stand-in: cannot reach the framework'); }
} };
global.Ref = function () { return [null]; };
global.console = { log: function (s) { lines.push(String(s)); } };

const constants = {
  CNEntityTypeContacts: 0,
  CNAuthorizationStatusNotDetermined: 0, CNAuthorizationStatusRestricted: 1,
  CNAuthorizationStatusDenied: 2, CNAuthorizationStatusAuthorized: 3,
  CNAuthorizationStatusLimited: 4,
  CNContactIdentifierKey: box('id'), CNContactGivenNameKey: box('given'),
  CNContactFamilyNameKey: box('family'), CNContactEmailAddressesKey: box('email'),
  CNContactPhoneNumbersKey: box('phone'), CNContactNoteKey: box('note'),
  NSDefaultRunLoopMode: box('mode'), NSUTF8StringEncoding: 4
};
global.$ = Object.assign({}, constants, {
  // The same property-vs-call rule as above: JXA writes `$.CNContactStore.alloc.init` with no
  // parentheses for a zero-argument initialiser (`init` and `+array` take none), so those are
  // getters here and hand over a fresh instance per access, as a real alloc/init does. Only the
  // shapes the shipped helper actually uses are modelled.
  CNContactStore: { alloc: { get init() { return store; } } },
  CNContactFetchRequest: { alloc: { initWithKeysToFetch: (keys) => request() } },
  CNChangeHistoryFetchRequest: { alloc: { get init() { return request(); } } },
  NSMutableArray: { get array() { return callable({ addObject: () => {}, count: 0 }); } },
  NSData: {
    dataWithContentsOfFile: (p) => (fs.existsSync(p) ? callable({ isNil: () => false }) : null)
  },
  NSString: { alloc: { initWithUTF8String: (s) => ({ dataUsingEncoding: () => ({ isNil: () => false }) }) } },
  NSRunLoop: { currentRunLoop: { runModeBeforeDate: () => true } },
  NSDate: { dateWithTimeIntervalSinceNow: () => ({}) }
});

vm.runInThisContext(code, { filename: 'switchboard-contacts-helper.js' });
process.stdout.write(JSON.stringify({ calls: calls, lines: lines }));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed on this host")
class ContactsStandInBridgeTests(unittest.TestCase):
    """The shipped helper text, executed against a stand-in bridge.

    Every assertion below is about the helper's *logic*. The bridge, the framework objects
    and the data are the stand-ins defined in ``STAND_IN_HARNESS``; a pass here is not
    evidence about macOS and is never reported as such.
    """

    def run_helper(self, request, config):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.json")
            script = os.path.join(tmp, "helper.js")
            harness = os.path.join(tmp, "harness.js")
            with open(cfg, "w", encoding="utf-8") as handle:
                json.dump(config, handle)
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(build_script(request))
            with open(harness, "w", encoding="utf-8") as handle:
                handle.write(STAND_IN_HARNESS)
            proc = subprocess.run(["node", harness, cfg, script], capture_output=True,
                                  text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            documents = []
            for line in payload["lines"]:
                self.assertTrue(line.startswith(RESULT_MARKER), line)
                documents.append(json.loads(line[len(RESULT_MARKER):]))
            self.assertEqual(len(documents), 1, payload["lines"])
            return documents[0], payload["calls"], tmp

    def test_a_granted_store_answers_the_status_without_prompting(self):
        document, calls, _tmp = self.run_helper({"operation": "authorization_status"},
                                               {"status": 3})
        self.assertEqual(document["outcome"], "ok")
        self.assertEqual(document["resolved_name"], "CNAuthorizationStatusAuthorized")
        self.assertEqual(document["raw_value"], 3)
        self.assertFalse(document["prompt_requested"])
        self.assertNotIn("fetch", calls)
        self.assertNotIn("history", calls)

    def test_no_read_path_fetches_without_a_grant(self):
        """The property that keeps a probe from raising the consent dialog."""
        for status in (0, 1, 2, 4):
            for operation in ("enumerate", "change_history", "restricted_keys"):
                with self.subTest(status=status, operation=operation):
                    request = {"operation": operation, "keys": list(MINIMAL_KEYS)}
                    if operation == "restricted_keys":
                        request["key_symbol"] = "CNContactNoteKey"
                    document, calls, _tmp = self.run_helper(request, {"status": status,
                                                                      "items": 3})
                    self.assertEqual(document["outcome"], "refused", document)
                    self.assertEqual(document["refusal"]["reason"], "authorization_not_granted")
                    self.assertNotIn("fetch", calls)
                    self.assertNotIn("history", calls)
                    self.assertFalse(document["prompt_requested"])

    def test_a_denied_status_is_reported_with_the_status_it_saw(self):
        document, _calls, _tmp = self.run_helper(
            {"operation": "enumerate", "keys": list(MINIMAL_KEYS)}, {"status": 2})
        self.assertEqual(document["authorization"]["resolved_name"],
                         "CNAuthorizationStatusDenied")
        self.assertEqual(document["authorization"]["raw_value"], 2)
        self.assertEqual(permission_state_for(document["authorization"]),
                         O.PERMISSION_STATE_DENIED)
        self.assertIsNone(status_without_an_equivalent(document["authorization"]))

    def test_restricted_and_limited_are_not_rounded_to_another_state(self):
        restricted, _c, _t = self.run_helper({"operation": "authorization_status"},
                                             {"status": 1})
        self.assertEqual(permission_state_for(restricted), O.PERMISSION_RESTRICTED)
        limited, _c2, _t2 = self.run_helper({"operation": "authorization_status"},
                                            {"status": 4})
        self.assertIsNone(permission_state_for(limited))
        self.assertIn("limited", status_without_an_equivalent(limited))
        outcome = interpret_document(limited, operation="authorization_status")
        self.assertEqual(outcome.code, O.UNSUPPORTED)
        self.assertEqual(outcome.reason, "authorization_status_without_an_equivalent")

    def test_a_granted_enumeration_reports_counts_and_counts_only(self):
        document, calls, _tmp = self.run_helper(
            {"operation": "enumerate", "keys": list(MINIMAL_KEYS), "limit": 25},
            {"status": 3, "items": 3})
        self.assertEqual(document["outcome"], "ok")
        self.assertEqual(len(document["items"]), 3)
        self.assertEqual(document["keys_resolved"], list(MINIMAL_KEYS))
        self.assertEqual(document["call_used"],
                         "enumerateContactsWithFetchRequest:error:usingBlock:")
        self.assertFalse(document["values_emitted"])
        self.assertFalse(document["prompt_requested"])
        self.assertIn("fetch", calls)
        item = document["items"][0]
        self.assertTrue(item["has_name"] and item["has_email"])
        self.assertNotIn("name", item)
        outcome = interpret_document(document, operation="enumerate")
        public = outcome.data["items"][0]
        self.assertNotIn("identifier", public)
        self.assertTrue(public["identifier_fingerprint"])
        self.assertEqual(public["identifier_length"], 36)

    def test_an_empty_database_returns_no_items_and_no_error(self):
        document, _calls, _tmp = self.run_helper(
            {"operation": "enumerate", "keys": list(MINIMAL_KEYS)},
            {"status": 3, "items": 0})
        self.assertEqual(document["outcome"], "ok")
        self.assertEqual(document["items"], [])
        self.assertIsNone(document["error"])

    def test_a_bridge_that_cannot_be_imported_is_named_as_such(self):
        document, calls, _tmp = self.run_helper({"operation": "enumerate"},
                                                {"bridge": "throw"})
        self.assertEqual(document["outcome"], "refused")
        self.assertEqual(document["refusal"]["reason"], "bridge_unavailable")
        self.assertEqual(calls, ["import:Contacts"])
        outcome = interpret_document(document, operation="enumerate")
        self.assertEqual(outcome.code, O.UNSUPPORTED)
        self.assertEqual(outcome.reason, "helper_missing")

    def test_an_absent_selector_is_refused_with_the_selector_it_checked(self):
        document, calls, _tmp = self.run_helper(
            {"operation": "enumerate", "keys": list(MINIMAL_KEYS)},
            {"status": 3, "items": 2, "has_enumerate_selector": False})
        self.assertEqual(document["outcome"], "refused")
        self.assertEqual(document["refusal"]["reason"], "selector_unavailable")
        self.assertIn("enumerateContactsWithFetchRequest:error:usingBlock:",
                      document["refusal"]["detail"])
        self.assertNotIn("fetch", calls)

    def test_the_unification_toggle_is_checked_and_never_assumed(self):
        document, calls, _tmp = self.run_helper(
            {"operation": "enumerate", "keys": list(MINIMAL_KEYS), "unify_off": True},
            {"status": 3, "items": 2, "has_unify_toggle": False})
        self.assertEqual(document["outcome"], "refused")
        self.assertEqual(document["refusal"]["reason"], "unification_toggle_absent")
        self.assertFalse(document["unify_off_selector_present"])
        self.assertNotIn("fetch", calls)
        outcome = interpret_document(document, operation="enumerate")
        self.assertEqual(outcome.reason, "unify_toggle_not_in_this_sdk")

    def test_change_history_counts_events_and_writes_the_token_to_a_file(self):
        token_bytes = "stand-in-token-not-from-any-mac"
        with tempfile.TemporaryDirectory() as tmp:
            token_path = os.path.join(tmp, "token")
            document, calls, _tmp = self.run_helper(
                {"operation": "change_history", "token_file": token_path,
                 "include_group_changes": False, "should_unify_results": True},
                {"status": 3,
                 "history_events": ["CNChangeHistoryDropEverythingEvent",
                                    "CNChangeHistoryAddContactEvent",
                                    "CNChangeHistoryAddContactEvent"],
                 "token_bytes": token_bytes})
            self.assertEqual(document["outcome"], "ok")
            self.assertTrue(document["fetch_succeeded"])
            self.assertTrue(document["drop_event_first"])
            self.assertEqual(document["event_counts"]["CNChangeHistoryAddContactEvent"], 2)
            self.assertTrue(document["token_file_written"])
            self.assertTrue(document["starting_token_present"] is False)
            self.assertIn("startingToken:absent", calls)
            outcome = interpret_document(document, operation="change_history",
                                         token_file=token_path)
            self.assertTrue(outcome.usable)
            self.assertEqual(outcome.data["token_length"], len(token_bytes))
            self.assertTrue(outcome.data["token_fingerprint"])
            self.assertFalse(outcome.data["token_value_recorded"])
            self.assertNotIn(token_bytes, json.dumps(outcome.to_dict()))

    def test_an_invalid_token_is_a_deliberate_trigger_when_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = os.path.join(tmp, "token")
            with open(token_path, "w", encoding="utf-8") as handle:
                handle.write("previous-token")
            document, calls, _tmp = self.run_helper(
                {"operation": "change_history", "token_file": token_path, "invalid_token": True},
                {"status": 3, "history_events": ["CNChangeHistoryAddContactEvent"]})
            self.assertEqual(document["outcome"], "ok")
            self.assertTrue(document["invalid_token_used"])
            self.assertIn("startingToken:present", calls)
            self.assertNotIn("startingToken:absent", calls)

    def test_a_reset_by_an_invalid_token_is_not_a_genuine_reset(self):
        document, _calls, _tmp = self.run_helper(
            {"operation": "change_history", "invalid_token": True},
            {"status": 3, "history_events": ["CNChangeHistoryDropEverythingEvent",
                                             "CNChangeHistoryAddContactEvent"]})
        self.assertTrue(document["drop_event_first"])
        self.assertTrue(document["invalid_token_used"])
        self.assertIn("not-a-real-token", json.dumps(document))

    def test_a_nil_change_history_fetch_is_the_documented_failure_shape(self):
        document, _calls, _tmp = self.run_helper({"operation": "change_history"},
                                                 {"status": 3, "history": "nil"})
        self.assertEqual(document["outcome"], "refused")
        self.assertEqual(document["refusal"]["reason"], "change_history_fetch_failed")
        self.assertIn("returns nil", document["refusal"]["detail"])
        outcome = interpret_document(document, operation="change_history")
        self.assertEqual(outcome.code, O.PERMANENT_ERROR)

    def test_the_restricted_key_path_needs_the_symbol_from_the_header(self):
        document, _calls, _tmp = self.run_helper({"operation": "restricted_keys"},
                                                 {"status": 3, "items": 2})
        self.assertEqual(document["outcome"], "refused")
        self.assertEqual(document["refusal"]["reason"], "key_symbol_not_supplied")
        self.assertIn("xcrun", document["refusal"]["detail"])
        outcome = interpret_document(document, operation="restricted_keys")
        self.assertEqual(outcome.reason, "restricted_key_symbol_not_supplied")

    def test_a_supplied_symbol_that_this_build_lacks_is_refused(self):
        document, _calls, _tmp = self.run_helper(
            {"operation": "restricted_keys", "key_symbol": "CNNotARealKey"},
            {"status": 3, "items": 2})
        self.assertEqual(document["outcome"], "refused")
        self.assertEqual(document["refusal"]["reason"], "key_symbol_absent")

    def test_a_guarded_key_that_raises_does_not_take_the_plain_fetch_with_it(self):
        document, calls, _tmp = self.run_helper(
            {"operation": "restricted_keys", "key_symbol": "CNContactNoteKey"},
            {"status": 3, "items": 2, "fetch": "throw"})
        self.assertEqual(document["outcome"], "ok")
        self.assertFalse(document["plain_fetch_ok"])
        self.assertEqual(calls.count("fetch"), 2)
        self.assertIsNone(document["guarded_fetch_error"])

    def test_request_access_is_the_only_command_that_asks(self):
        document, calls, _tmp = self.run_helper({"operation": "request_access"},
                                                {"status": 0, "granted": True})
        self.assertEqual(document["outcome"], "ok")
        self.assertTrue(document["granted"])
        self.assertTrue(document["selector_present"])
        self.assertTrue(document["prompt_expected"])
        self.assertFalse(document["prompt_text_recorded"])
        self.assertNotIn("fetch", calls)

    def test_the_helper_never_claims_a_contacted_source_when_it_refused(self):
        for config in ({"bridge": "throw"}, {"status": 2, "items": 3}):
            with self.subTest(config=config):
                document, _calls, _tmp = self.run_helper(
                    {"operation": "enumerate", "keys": list(MINIMAL_KEYS)}, config)
                self.assertEqual(document["outcome"], "refused")
                self.assertFalse(document["source_contacted"])
                outcome = interpret_document(document, operation="enumerate")
                self.assertFalse(outcome.source_contacted)
                self.assertFalse(outcome.real_source_connected)


class ContactsVocabularyTests(unittest.TestCase):
    """One vocabulary, in one place, on both sides of the product."""

    def test_restricted_is_in_the_shared_permission_vocabulary(self):
        from grace import contracts as C
        self.assertIn(O.PERMISSION_RESTRICTED, O.PERMISSION_STATES)
        self.assertIn(C.PERMISSION_RESTRICTED, C.PERMISSION_STATES)
        self.assertEqual(O.PERMISSION_RESTRICTED, C.PERMISSION_RESTRICTED)
        self.assertEqual(tuple(C.PERMISSION_STATES), tuple(O.PERMISSION_STATES))

    def test_the_vocabulary_matches_the_framework_statuses_it_has_to_express(self):
        self.assertEqual(sorted(AUTHORIZATION_VOCABULARY),
                         ["CNAuthorizationStatusAuthorized", "CNAuthorizationStatusDenied",
                          "CNAuthorizationStatusNotDetermined",
                          "CNAuthorizationStatusRestricted"])
        for name, state in AUTHORIZATION_VOCABULARY.items():
            with self.subTest(name=name):
                self.assertIn(state, O.PERMISSION_STATES)
                self.assertIn(name, AUTHORIZATION_STATUS_NAMES)

    def test_the_healthy_source_definition_is_the_shared_one(self):
        from grace.ingest import Ingest
        self.assertEqual(tuple(HEALTHY_SOURCE_STATES), tuple(Ingest.HEALTHY_SOURCE_STATES))

    def test_the_documented_event_classes_include_the_ones_o15_requires(self):
        for name in ("CNChangeHistoryDropEverythingEvent", "CNChangeHistoryAddContactEvent",
                     "CNChangeHistoryUpdateContactEvent", "CNChangeHistoryDeleteContactEvent"):
            with self.subTest(name=name):
                self.assertIn(name, DOCUMENTED_EVENT_CLASSES)

    def test_a_fixture_row_cannot_carry_a_documented_limitation_it_contradicts(self):
        """The pack's text says no adapter exists; a measured row may not repeat it."""
        for scenario in FIXTURE_SCENARIOS:
            proc = run_worker(["--fixture-mode", "contacts", "--fixture-scenario", scenario,
                               "probe"])
            for row in rows_from(proc.stdout):
                with self.subTest(scenario=scenario, capability=row["capability"]):
                    self.assertNotIn("no Contacts adapter exists in this worker",
                                     row["limitation"])


if __name__ == "__main__":          # pragma: no cover
    unittest.main()
