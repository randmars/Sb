"""Mini worker CLI: all nine commands, both modes, through the real entry point.

Why this file exists. Three commands -- ``accounts``, ``health`` and ``run`` -- used to
exit 1 with an uncaught ``TypeError: Object of type method is not JSON serializable``
while the unit suite stayed green, because nothing drove a command end to end. The suite
tested the pieces the commands are built from and never the commands themselves.

So every test here runs ``python3 -m switchboard_mini <command>`` as a subprocess, in real
mode and in ``--fixture-mode``, and asserts the four properties that were missing:

* an exit code, and never a crash (no ``Traceback`` on either stream);
* a well-formed JSON document on stdout (every non-blank line parses);
* a typed state in the document (an adapter outcome code, a row state, or a named event);
* ``real_source_connected: false`` on a host with no source -- true on this Linux
  computer in real mode, where the AppleScript transport refuses before it reaches Mail.

What this does **not** prove: that Mail.app answers any of these commands. Only Randy's
Mac can show that (Gate 2). A green run here proves the worker's own documents are
well-formed and honest, not that a mailbox was read.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest

from switchboard_mini import cli
from switchboard_mini import outcomes as O
from switchboard_mini.beeper_adapter import MEASURED_CAPABILITIES as BEEPER_MEASURED
from switchboard_mini.contacts_adapter import MEASURED_CAPABILITIES as CONTACTS_MEASURED
from switchboard_mini.hermes_adapter import MEASURED_CAPABILITIES as HERMES_MEASURED
from switchboard_mini.hermes_transport import TYPED_STATES as HERMES_TYPED_STATES
from switchboard_mini.mail_adapter import build_adapter, make_ref
from switchboard_mini.probe import (CAPABILITIES, CAPABILITY_NAMES,
                                    DOCUMENTED_CAPABILITY_NAMES, ROW_FIELDS,
                                    ROW_LABELLING_FIELDS)

#: Which capability each adapter measures, derived from the adapters themselves rather than
#: hand-maintained here -- a new slice lands and moves its own rows out of the documentation
#: set, and no count in this file has to be edited. The names are the ``source`` field the
#: adapter's rows carry.
MEASURED_BY = {
    **{name: "beeper" for name in BEEPER_MEASURED},
    **{name: "contacts" for name in CONTACTS_MEASURED},
    **{name: "hermes" for name in HERMES_MEASURED},
}
#: Every capability an adapter in the worker can measure on a host (fixture mode included).
MEASURED_CAPABILITIES = tuple(MEASURED_BY)
#: The rows still answered only by the Gate 2 probe pack: nothing in the worker measures
#: them, so they stay documentation reads. 5 of the pack's 22 documented capabilities as of
#: the Hermes slice: the 5 Beeper rows this read-only slice deliberately does not build
#: (send, send reconciliation, attachment materialisation, composer prefill, live event
#: stream). The 8 Hermes rows left this set when the Hermes read adapter landed.
DOCUMENTATION_ONLY_CAPABILITIES = tuple(name for name in DOCUMENTED_CAPABILITY_NAMES
                                        if name not in MEASURED_BY)
#: Every source a capability in the pack belongs to, in the order the pack declares them.
SOURCES = tuple(dict.fromkeys(capability.source for capability in CAPABILITIES))
#: What each source's adapter refuses with on a computer that cannot reach its source, and
#: the adapter outcome code that reason maps to -- read out of the sources, never typed in
#: here. Hermes is not a Mac-bound source: its transport's own module docstring says a
#: ``host_not_macos``-shaped reason "would assert a fact no record in the pack states", so
#: its refusals come from its own named-reason table (``hermes_transport.TYPED_STATES``). The
#: remaining sources are the Mac-bound ones, whose transports raise one host-gate reason
#: before anything is contacted.
REFUSAL_CODES = {
    **{source: {"host_not_macos": O.UNSUPPORTED}
       for source in SOURCES if source != "hermes"},
    "hermes": {reason: entry["code"] for reason, entry in HERMES_TYPED_STATES.items()},
}

REPO_MINI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "mini")
FIXTURE_ACCOUNT = "FIXTURE Account A"
FIXTURE_MAILBOX = "INBOX"
FIXTURE_REF = make_ref(FIXTURE_ACCOUNT, FIXTURE_MAILBOX, "101")

#: Commands whose document is an adapter outcome (``Outcome.to_dict()``).
OUTCOME_COMMANDS = ("health", "accounts", "mailboxes", "list", "fetch")

ROW_STATES = (tuple(O.ADAPTER_OUTCOMES) + ("harness_error", O.PROBE_UNMEASURED))
RUN_EVENTS = ("worker_start", "poll", "worker_stop", "startup_failed", "backing_off",
              "harness_failure")
#: Capabilities this slice deliberately does not build: they refuse by design, not
#: because of the host, and their reason says so.
DELIBERATELY_ABSENT = ("attachment_materialization", "draft_preparation", "authorized_send",
                       "send_reconciliation")
ABSENT_REASONS = ("read_only_slice_1", "materialize_not_implemented")
HARNESS_EXIT = cli.EXIT_HARNESS_FAILURE


def run_cli(*args: str, fixture: bool = False, state: str = None,
            cwd: str = REPO_MINI) -> subprocess.CompletedProcess:
    """Run the worker through its real entry point, as a separate process."""
    argv = [sys.executable, "-m", "switchboard_mini"]
    if fixture:
        argv.append("--fixture-mode")
    argv.extend(args)
    env = dict(os.environ, PYTHONPATH=REPO_MINI)
    env["SWITCHBOARD_MINI_STATE"] = state or os.path.join(
        tempfile.mkdtemp(prefix="mini-cli-state-"), "state.json")
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, cwd=cwd)


def json_documents(proc: subprocess.CompletedProcess, case: str) -> list:
    """Every stdout line must be a JSON object; return them parsed."""
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    documents = []
    for index, line in enumerate(lines):
        try:
            document = json.loads(line)
        except ValueError as exc:            # pragma: no cover - a failure path
            raise AssertionError(
                f"{case}: stdout line {index + 1} is not JSON ({exc}): {line[:200]!r}")
        if not isinstance(document, dict):   # pragma: no cover - a failure path
            raise AssertionError(f"{case}: stdout line {index + 1} is not an object")
        documents.append(document)
    return documents


def documented_rows(rows: list) -> list:
    """Rows read out of the Gate 2 probe pack: they contacted nothing, on any host."""
    return [r for r in rows if r["origin"] == O.DOCUMENTATION]


def measured_rows(rows: list) -> list:
    """Rows this host produced itself (fixture or real), not documentation reads."""
    return [r for r in rows if r["origin"] != O.DOCUMENTATION]


def values_of(document, key: str) -> list:
    """Every value stored under ``key`` anywhere in the document."""
    found = []
    if isinstance(document, dict):
        for name, value in document.items():
            if name == key:
                found.append(value)
            found.extend(values_of(value, key))
    elif isinstance(document, list):
        for item in document:
            found.extend(values_of(item, key))
    return found


class CliCaseMixin:
    """Assertions every command must satisfy, in both modes."""

    def assertNoCrash(self, proc, case: str) -> None:
        self.assertNotIn("Traceback", proc.stderr,
                         f"{case}: a stack trace reached stderr:\n{proc.stderr}")
        self.assertNotIn("Traceback", proc.stdout,
                         f"{case}: a stack trace reached stdout")
        self.assertLess(proc.returncode, HARNESS_EXIT,
                        f"{case}: exit {proc.returncode} is a harness failure: "
                        f"{proc.stderr}")

    def assertTyped(self, document: dict, case: str) -> str:
        state = document.get("code") or document.get("state") or document.get("event")
        self.assertTrue(isinstance(state, str) and state,
                        f"{case}: document carries no typed state: "
                        f"{sorted(document)[:8]}")
        return state

    def assertNoSourceClaimed(self, documents: list, case: str) -> None:
        """On a host with no source, no document may claim a real source."""
        seen = [v for d in documents for v in values_of(d, "real_source_connected")]
        self.assertTrue(seen, f"{case}: no real_source_connected field to check?")
        self.assertEqual([v for v in seen if v is not False], [],
                         f"{case}: real_source_connected was not false: {seen}")


class TestEveryCommandRealMode(CliCaseMixin, unittest.TestCase):
    """Real mode: the adapter is the real one and refuses before touching Mail here."""

    def setUp(self) -> None:
        if sys.platform == "darwin":     # pragma: no cover - this computer is Linux
            self.skipTest("this computer is the Linux stand-in for Grace; real-mode "
                          "expectations here describe a host with no Mail")

    def test_version(self) -> None:
        proc = run_cli("version")
        self.assertNoCrash(proc, "real version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = json_documents(proc, "real version")
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["worker"], "switchboard-mini")
        self.assertEqual(tuple(documents[0]["capabilities"]), CAPABILITY_NAMES)
        self.assertTrue(documents[0]["standard_library_only"])

    def test_probe(self) -> None:
        proc = run_cli("probe")
        self.assertNoCrash(proc, "real probe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json_documents(proc, "real probe")
        self.assertEqual([r["capability"] for r in rows], list(CAPABILITY_NAMES))
        documented = documented_rows(rows)
        measured = measured_rows(rows)
        self.assertTrue(documented, "the documented capabilities must still be reported")
        self.assertTrue(measured)
        for row in rows:
            missing = [f for f in ROW_FIELDS + ROW_LABELLING_FIELDS if f not in row]
            self.assertEqual(missing, [], f"{row['capability']} is missing {missing}")
            self.assertIn(row["state"], ROW_STATES)
            self.assertFalse(row["real_source_connected"], row["capability"])
        for row in documented:
            # a page read is not a measurement on any host, including this one
            self.assertFalse(row["supported"], row["capability"])
            self.assertEqual(row["state"], O.PROBE_UNMEASURED)
            self.assertFalse(row["adapter_is_real"], row["capability"])
            self.assertFalse(row["values_from_source"], row["capability"])
            self.assertTrue(O.is_documentation_label(row["label"]), row["label"])
            self.assertTrue(row["citations"], row["capability"])
        for row in measured:
            # the real adapter answered on a host where it cannot read Mail
            self.assertTrue(row["adapter_is_real"])
            self.assertFalse(row["values_from_source"])
            self.assertEqual(list(row["citations"]), [],
                             "only a documentation row may cite the pack")
            if row["capability"] == "manifest":
                # ... except the manifest row, which describes the worker itself and so
                # can be supported while holding no source value.
                self.assertTrue(row["supported"])
                self.assertFalse(row["values_from_source"])
                self.assertEqual(
                    row["evidence"]["capabilities_claimed_supported_without_probe"], 0)
            elif row["capability"] in DELIBERATELY_ABSENT:
                # a deliberate absence in this slice, refused with its own reason
                self.assertFalse(row["supported"])
                self.assertEqual(row["state"], O.UNSUPPORTED)
                self.assertIn(row["evidence"]["reason"], ABSENT_REASONS)
            else:
                # Every capability that needs a source is a typed refusal on this host: no
                # Mail.app, Beeper Desktop, address book or Hermes gateway is reachable from
                # here. The reason is the row's *own* source adapter's reason and the state is
                # the code that reason maps to, both read out of the adapters -- a reason
                # borrowed from another source (a Mac-shaped one on a Hermes row) would be a
                # claim this host cannot make.
                reason = row["evidence"]["reason"]
                self.assertFalse(row["supported"], row["capability"])
                self.assertIn(reason, REFUSAL_CODES[row["source"]],
                              f"{row['capability']}: {reason!r} is not a reason the "
                              f"{row['source']} adapter declares")
                self.assertEqual(row["state"], REFUSAL_CODES[row["source"]][reason],
                                 f"{row['capability']}: {reason!r} maps to a different state")
        self.assertNoSourceClaimed(rows, "real probe")

    def test_probe_claims_nothing_supported_without_measurement(self) -> None:
        rows = json_documents(run_cli("probe"), "real probe")
        manifest_row = [r for r in rows if r["capability"] == "manifest"][0]
        evidence = manifest_row["evidence"]
        self.assertEqual(evidence["capabilities_claimed_supported_without_probe"], 0)
        self.assertEqual(evidence["claimed_without_probe"], [])

    def test_run(self) -> None:
        proc = run_cli("run", "--once", "--limit", "2")
        self.assertNoCrash(proc, "real run")
        # No account can be listed on this host, so the loop stops with a typed document
        # and exit 2 (a documented outcome), never a crash.
        self.assertEqual(proc.returncode, 2, f"exit {proc.returncode}: {proc.stderr}")
        documents = json_documents(proc, "real run")
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["event"], "startup_failed")
        self.assertIn(documents[0]["reason"], ("no account could be listed to poll",))
        outcome = documents[0]["outcome"]
        self.assertEqual(outcome["code"], O.UNSUPPORTED)
        self.assertEqual(outcome["reason"], "host_not_macos")
        self.assertFalse(outcome["real_source_connected"])
        self.assertNoSourceClaimed(documents, "real run")

    def test_health_accounts_mailboxes_list_fetch(self) -> None:
        cases = [
            ("health", ("health",)),
            ("accounts", ("accounts",)),
            ("mailboxes", ("mailboxes", "--account", "Any Account")),
            ("list", ("list", "--account", "Any Account", "--mailbox", "INBOX")),
            ("fetch", ("fetch", "--account", "Any Account",
                       "--ref", "mail:Any%20Account:INBOX:1")),
        ]
        for name, argv in cases:
            with self.subTest(command=name):
                proc = run_cli(*argv)
                self.assertNoCrash(proc, f"real {name}")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                documents = json_documents(proc, f"real {name}")
                self.assertEqual(len(documents), 1)
                self.assertIn(name, OUTCOME_COMMANDS)
                self.assertIn(documents[0]["code"], O.ADAPTER_OUTCOMES)
                self.assertEqual(documents[0]["code"], O.UNSUPPORTED)
                self.assertEqual(documents[0]["reason"], "host_not_macos")
                self.assertFalse(documents[0]["partial"])
                self.assertNoSourceClaimed(documents, f"real {name}")

    def test_manifest(self) -> None:
        proc = run_cli("manifest")
        self.assertNoCrash(proc, "real manifest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = json_documents(proc, "real manifest")
        self.assertEqual(len(documents), 1)
        manifest = documents[0]
        self.assertTrue(manifest["adapter_is_real"])
        self.assertFalse(manifest["real_source_connected"])
        self.assertEqual(sorted(manifest["capabilities"]), sorted(CAPABILITY_NAMES))
        self.assertFalse(manifest["probed"])
        self.assertEqual([n for n, c in manifest["capabilities"].items() if c["supported"]],
                         [])
        self.assertNoSourceClaimed(documents, "real manifest")


class TestEveryCommandFixtureMode(CliCaseMixin, unittest.TestCase):
    """Fixture mode: every command answers, every document is labelled FIXTURE:."""

    def test_version(self) -> None:
        proc = run_cli("version", fixture=True)
        self.assertNoCrash(proc, "fixture version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = json_documents(proc, "fixture version")
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["worker"], "switchboard-mini")

    def test_probe(self) -> None:
        proc = run_cli("probe", fixture=True)
        self.assertNoCrash(proc, "fixture probe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json_documents(proc, "fixture probe")
        self.assertEqual([r["capability"] for r in rows], list(CAPABILITY_NAMES))
        documented = documented_rows(rows)
        measured = measured_rows(rows)
        self.assertTrue(measured)
        self.assertTrue(documented)
        for row in measured:
            with self.subTest(capability=row["capability"]):
                self.assertEqual(row["origin"], O.FIXTURE)
                self.assertFalse(row["adapter_is_real"])
                self.assertFalse(row["real_source_connected"])
                self.assertTrue(O.is_fixture_label(row["label"]))
                # A fixture disclaimer names the source it did *not* contact, and the row
                # itself says which source that is: the expectation is read out of the
                # worker's own per-source table (``fixture_disclaimer(source)``), so a new
                # adapter's wording reaches this test with no hand-maintained table to drift.
                self.assertEqual(row["disclaimer"], O.fixture_disclaimer(row["source"]),
                                 f"{row['capability']}: fixture disclaimer must name its own "
                                 f"source ({row['source']!r}) and claim no other")
        for row in documented:
            # Recorded fixtures answer ``fixture``; the pack rows contacted nothing at all on
            # any host, so they are ``documentation`` -- and never supported.
            with self.subTest(capability=row["capability"]):
                self.assertEqual(row["origin"], O.DOCUMENTATION)
                self.assertNotEqual(row["source"], "mail")
                self.assertFalse(row["supported"])
                self.assertEqual(row["state"], O.PROBE_UNMEASURED)
                self.assertFalse(row["adapter_is_real"])
                self.assertFalse(row["real_source_connected"])
                self.assertTrue(O.is_documentation_label(row["label"]), row["label"])
                self.assertTrue(row["citations"], row["capability"])
        self.assertNoSourceClaimed(rows, "fixture probe")

    def test_run(self) -> None:
        proc = run_cli("run", "--once", "--limit", "2", "--account", FIXTURE_ACCOUNT,
                       "--mailbox", FIXTURE_MAILBOX, fixture=True)
        self.assertNoCrash(proc, "fixture run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = json_documents(proc, "fixture run")
        self.assertEqual([d["event"] for d in documents],
                         ["worker_start", "poll", "worker_stop"])
        poll = documents[1]
        self.assertEqual(poll["outcome"]["code"], O.PARTIAL)
        self.assertEqual(poll["outcome"]["origin"], O.FIXTURE)
        self.assertFalse(poll["outcome"]["adapter_is_real"])
        self.assertFalse(poll["outcome"]["real_source_connected"])
        self.assertTrue(O.is_fixture_label(poll["outcome"]["label"]))
        self.assertTrue(poll["cursor_advanced"])
        self.assertNoSourceClaimed(documents, "fixture run")

    def test_health_accounts_mailboxes_list_fetch(self) -> None:
        cases = [
            ("health", ("health",), O.SUCCESS),
            ("accounts", ("accounts",), O.SUCCESS),
            ("mailboxes", ("mailboxes", "--account", FIXTURE_ACCOUNT), O.SUCCESS),
            ("list", ("list", "--account", FIXTURE_ACCOUNT, "--mailbox", FIXTURE_MAILBOX,
                      "--limit", "2"), O.PARTIAL),
            ("fetch", ("fetch", "--account", FIXTURE_ACCOUNT, "--ref", FIXTURE_REF),
             O.SUCCESS),
        ]
        for name, argv, expected_code in cases:
            with self.subTest(command=name):
                proc = run_cli(*argv, fixture=True)
                self.assertNoCrash(proc, f"fixture {name}")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                documents = json_documents(proc, f"fixture {name}")
                self.assertEqual(len(documents), 1)
                document = documents[0]
                self.assertEqual(document["code"], expected_code)
                self.assertEqual(document["origin"], O.FIXTURE)
                self.assertTrue(document["fixture_mode"])
                self.assertFalse(document["adapter_is_real"])
                self.assertFalse(document["real_source_connected"])
                self.assertTrue(O.is_fixture_label(document["label"]))
                self.assertIn("No Mail.app was contacted", document["disclaimer"])
                self.assertNoSourceClaimed(documents, f"fixture {name}")

    def test_manifest(self) -> None:
        proc = run_cli("manifest", fixture=True)
        self.assertNoCrash(proc, "fixture manifest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = json_documents(proc, "fixture manifest")
        self.assertEqual(len(documents), 1)
        manifest = documents[0]
        self.assertFalse(manifest["adapter_is_real"])
        self.assertFalse(manifest["real_source_connected"])
        self.assertTrue(O.is_fixture_label(manifest["label"]))
        self.assertIn("No Mail.app was contacted", manifest["disclaimer"])
        self.assertNoSourceClaimed(documents, "fixture manifest")

    def test_manifest_never_claims_a_source_even_with_real_probe_rows(self) -> None:
        """Folding real probe rows in does not make the manifest a measurement."""
        adapter = build_adapter(fixture_mode=False)          # the real adapter, no Mail
        rows = [{**r, "real_source_connected": True}
                for r in json_documents(run_cli("probe", fixture=True), "rows")]
        manifest = adapter.manifest(rows)
        self.assertTrue(manifest["adapter_is_real"])
        self.assertFalse(manifest["real_source_connected"])
        self.assertEqual(manifest["folded_probe_rows"]["count"], len(rows))
        self.assertEqual(manifest["folded_probe_rows"]["any_from_real_source"], True)


class TestFlagPositions(unittest.TestCase):
    """The global flags work before *and* after the subcommand.

    The runbook must not trip Randy on argument order: `probe --fixture-mode` has to mean
    what `--fixture-mode probe` means.
    """

    def test_fixture_mode_is_accepted_in_both_positions(self) -> None:
        before = run_cli("--fixture-mode", "probe")
        after = run_cli("probe", "--fixture-mode")
        for proc in (before, after):
            self.assertEqual(proc.returncode, 0, proc.stderr)
        rows_before = json_documents(before, "--fixture-mode probe")
        rows_after = json_documents(after, "probe --fixture-mode")
        self.assertEqual([r["capability"] for r in rows_before],
                         [r["capability"] for r in rows_after])
        # The Beeper/Contacts/Hermes rows are documentation reads (never fixtures) and are
        # asserted separately; every row this machine actually produced is a labelled fixture.
        fixture_origins = [r["origin"] for r in rows_after if r["origin"] != O.DOCUMENTATION]
        self.assertEqual(fixture_origins, [O.FIXTURE] * len(fixture_origins))
        # The multi-adapter probe changed what may be asserted here. The Beeper, Contacts
        # *and Hermes* adapters are in this run, so the rows for the capabilities they
        # participate in carry their own adapter's origin (a labelled fixture, never
        # `documentation`, never supported) and the documentation row for each of those
        # capabilities is absent from the run altogether: one run emits one row per
        # capability key. Every capability no adapter in this run measures -- the 5 Beeper
        # rows this read-only slice deliberately does not build -- is still the documentation
        # read the pack records, and still unmeasured.
        by_capability: dict = {}
        for row in rows_after:
            by_capability.setdefault(row["capability"], []).append(row)
        self.assertTrue(MEASURED_BY, "each adapter declares the rows it measures")
        for name in MEASURED_CAPABILITIES:
            with self.subTest(capability=name, whence="measured by an adapter in this run"):
                rows_for = by_capability.get(name, [])
                self.assertEqual(len(rows_for), 1, "one row per capability key, per run")
                row = rows_for[0]
                self.assertEqual(row["source"], MEASURED_BY[name],
                                 f"{name}: measured by the {MEASURED_BY[name]} adapter")
                self.assertEqual(row["origin"], O.FIXTURE,     # the adapter's own origin
                                 f"{name}: a measured row carries its adapter's origin")
                self.assertTrue(row["supersedes"],
                                f"{name}: it names the documentation row it replaces")
                self.assertFalse(row["supported"], f"{name}: a fixture row is never supported")
        self.assertTrue(DOCUMENTATION_ONLY_CAPABILITIES,
                        "some capabilities still have no adapter")
        for name in DOCUMENTATION_ONLY_CAPABILITIES:
            with self.subTest(capability=name, whence="no adapter in this run measures it"):
                rows_for = by_capability.get(name, [])
                self.assertEqual(len(rows_for), 1)
                self.assertEqual(rows_for[0]["origin"], O.DOCUMENTATION)
                self.assertEqual(rows_for[0]["state"], O.PROBE_UNMEASURED)
                self.assertFalse(rows_for[0]["supported"])
        # The documentation row for a capability an adapter measures is not in the run at
        # all -- a run may never carry both rows for one capability key. The split this
        # run actually produced (38 rows: 33 adapter-measured, 5 documentation-only) is
        # derived from the adapters, so the next slice moves its own rows without an edit.
        self.assertEqual([r["capability"] for r in documented_rows(rows_after)
                          if r["capability"] in MEASURED_BY], [])
        self.assertEqual(len(documented_rows(rows_after)), len(DOCUMENTATION_ONLY_CAPABILITIES),
                         "every documentation row in this run has no adapter measuring it")
        self.assertEqual([r["state"] for r in rows_before],
                         [r["state"] for r in rows_after])

    def test_fixture_scenario_is_accepted_in_both_positions(self) -> None:
        before = run_cli("--fixture-mode", "--fixture-scenario", "offline", "accounts")
        after = run_cli("accounts", "--fixture-mode", "--fixture-scenario", "offline")
        for proc in (before, after):
            self.assertEqual(proc.returncode, 0, proc.stderr)
            document = json_documents(proc, "offline accounts")[0]
            self.assertEqual(document["code"], O.OFFLINE)
            self.assertEqual(document["data"]["ae_code"], -600)

    def test_pretty_is_accepted_in_both_positions_and_only_changes_layout(self) -> None:
        before = run_cli("--fixture-mode", "--pretty", "accounts")
        after = run_cli("accounts", "--fixture-mode", "--pretty")
        for proc in (before, after):
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("\n  ", proc.stdout, "pretty output is not indented")
        parsed = [json.loads(p.stdout) for p in (before, after)]
        self.assertEqual(parsed[0]["code"], parsed[1]["code"])
        self.assertEqual(parsed[0]["origin"], O.FIXTURE)

    def test_a_flag_given_before_the_subcommand_still_applies(self) -> None:
        """A subcommand copy must not reset a value set before the subcommand."""
        proc = run_cli("--fixture-mode", "--fixture-scenario", "permission_denied",
                       "accounts")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = json_documents(proc, "permission_denied accounts")[0]
        self.assertEqual(document["code"], O.PERMISSION_DENIED)
        self.assertEqual(document["data"]["ae_code"], -1743)

    def test_a_missing_required_argument_is_a_usage_error_not_a_traceback(self) -> None:
        proc = run_cli("mailboxes")
        self.assertEqual(proc.returncode, cli.EXIT_USAGE)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


class TestSerialisationDefect(unittest.TestCase):
    """The defect itself must stay fixed, at the level where it happened.

    A dataclass field whose name is later shadowed by a constructor of the same name
    picks the *method* up as its default. Every instance that did not pass it explicitly
    then carried a bound method, and ``json.dumps`` raised
    ``TypeError: Object of type method is not JSON serializable`` -- which is what made
    `accounts`, `health` and `run` print a stack trace on every platform.
    """

    def test_no_outcome_field_holds_a_callable(self) -> None:
        constructed = [
            O.Outcome.ok({"a": 1}),
            O.Outcome.partial({"a": 1}, "partial page"),
            O.Outcome.unsupported("nope"),
            O.Outcome.permission_denied("nope"),
            O.Outcome.offline("nope"),
            O.Outcome.rate_limited("nope"),
            O.Outcome.retryable("nope"),
            O.Outcome.permanent("nope"),
            O.Outcome.uncertain("nope"),
        ]
        for outcome in constructed:
            for field in dataclasses.fields(O.Outcome):
                value = getattr(outcome, field.name)
                self.assertFalse(callable(value),
                                 f"Outcome.{field.name} is a method, not a value")
                self.assertEqual(O.json_problem_paths({"v": value}), [],
                                 f"Outcome.{field.name} is not JSON-safe")
            json.loads(O.emit(outcome.to_dict()))

    def test_the_partial_json_key_is_a_boolean(self) -> None:
        self.assertIs(O.Outcome.partial({"a": 1}, "d").to_dict()["partial"], True)
        self.assertIs(O.Outcome.ok({"a": 1}).to_dict()["partial"], False)
        self.assertIs(O.Outcome.unsupported("d").to_dict()["partial"], False)
        self.assertIs(O.Outcome.uncertain("d").to_dict()["partial"], False)

    def test_emit_refuses_a_document_that_is_not_json_safe(self) -> None:
        with self.assertRaises(O.SerializationError) as caught:
            O.emit({"event": "x", "bad": O.Outcome.ok, "nested": [{"worse": {1, 2}}]})
        message = str(caught.exception)
        self.assertIn("$.bad", message)
        self.assertIn("$.nested[0].worse", message)

    def test_the_probe_harness_failure_document_is_json_safe(self) -> None:
        document = cli.harness_failure_document(
            "probe", O.SerializationError(["$.row[0].partial"]))
        json.loads(O.emit(document))
        self.assertEqual(document["state"], "harness_error")
        self.assertEqual(document["reason"], "document_not_serialisable")
        self.assertFalse(document["real_source_connected"])
        self.assertEqual(document["unserialisable_paths"], ["$.row[0].partial"])

    def test_the_cli_reports_an_unexpected_exception_as_a_typed_document(self) -> None:
        """A simulated fault: the guard, not a real defect, is what is under test here."""
        code = (
            "import sys\n"
            "import switchboard_mini.cli as cli\n"
            "def boom(*a, **k):\n"
            "    raise RuntimeError('simulated fault for the guard test')\n"
            "cli.run_probe = boom\n"
            "sys.exit(cli.main(['probe']))\n"
        )
        env = dict(os.environ, PYTHONPATH=REPO_MINI)
        proc = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              env=env, cwd=REPO_MINI)
        self.assertEqual(proc.returncode, HARNESS_EXIT,
                         f"exit {proc.returncode}; stderr={proc.stderr}")
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("Traceback", proc.stdout)
        document = json.loads(proc.stdout.strip().splitlines()[0])
        self.assertEqual(document["event"], "harness_failure")
        self.assertEqual(document["state"], "harness_error")
        self.assertEqual(document["failure_type"], "RuntimeError")
        self.assertFalse(document["real_source_connected"])
        self.assertIn("probe", proc.stderr)

    def test_the_cli_guard_never_claims_a_source(self) -> None:
        document = cli.harness_failure_document("run", RuntimeError("simulated"))
        self.assertFalse(document["source_contacted"])
        self.assertFalse(document["real_source_connected"])
        self.assertFalse(document["fixture_mode"])
        self.assertTrue(document["adapter_is_real"])
        fixture_document = cli.harness_failure_document("run", RuntimeError("simulated"),
                                                        fixture_mode=True)
        self.assertFalse(fixture_document["adapter_is_real"])
        self.assertEqual(fixture_document["origin"], O.FIXTURE)
        self.assertFalse(fixture_document["real_source_connected"])


class TestNoTracebackWithoutMail(unittest.TestCase):
    """A last line of defence: no command may exit with a stack trace on this host."""

    def test_every_command_exits_zero_or_two_and_never_three(self) -> None:
        commands = [
            ("version",), ("probe",), ("run", "--once"), ("health",), ("accounts",),
            ("mailboxes", "--account", "Any Account"),
            ("list", "--account", "Any Account", "--mailbox", "INBOX"),
            ("fetch", "--account", "Any Account", "--ref", "mail:Any%20Account:INBOX:1"),
            ("manifest",),
        ]
        for argv in commands:
            with self.subTest(command=argv[0]):
                proc = run_cli(*argv)
                self.assertNotIn("Traceback", proc.stderr, proc.stderr)
                self.assertIn(proc.returncode, (0, 2), f"{argv}: exit {proc.returncode}")
                self.assertTrue(proc.stdout.strip(), f"{argv}: nothing on stdout")
                json_documents(proc, " ".join(argv))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
