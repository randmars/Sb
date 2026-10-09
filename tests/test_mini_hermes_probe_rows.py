"""The Hermes probe rows. HALF TWO of two test files.

Half one (``tests/test_mini_hermes_transport.py``) established the transport and security
surface: the status mapper, the consent gate, the bounds, the refusals, the labelling. This
file is the other half, and it builds on that rig rather than re-deriving it: the eight
capability rows ``switchboard_mini.hermes_probe`` emits, what each one may and may not
claim, and the Grace-side import that consumes a run.

**What is exercised here, and against what.** Three modes, none of which is Randy's Mac:

* **recorded fixtures** (``mini/switchboard_mini/fixtures/hermes/``) — ``origin: fixture``,
  a ``FIXTURE:`` label, a disclaimer, ``real_source_connected: false``;
* **the labelled loopback stand-in** (``StubHermesGateway``, imported from half one) — a real
  socket answering the documented paths and nothing else. Rows produced against it carry
  ``responder: stand_in_http_server``, ``stand_in: true``, ``source_contacted: false`` and a
  limitation that says outright that nothing here is an observation of Randy's Hermes;
* **real mode with nothing listening** — a bound-and-released loopback port, so the answer is
  a *socket* fact (``offline`` / ``hermes_not_reachable``) and never a Mac-shaped reason.

There is no Hermes gateway on this Linux computer and this file never contacts one. Every
row asserted below is therefore ``supported: false``: the only capability rows in the product
that may be supported without a source are the worker's own self-measurements, and none of
these eight is one. What only Randy's sitting can settle — his build's capability-discovery
output, his ``terminal.backend``, his approval prompt, his credential path — is named in each
row's evidence as unmeasured, and no test here may claim otherwise.

Requirements covered (PRD; ``O``-numbers are Gate 2 probe-pack records):

* **R11 / T03** (capability manifest is a measurement, never an assertion) — the eight rows'
  shape in all three modes: ``TestTheEightRowsInFixtureMode``,
  ``TestTheEightRowsInRealModeWithNothingListening``,
  ``TestARealRunWithNoGatewayKeyLeavesTheBoxAlone``.
* **R11/T03 + the labelling rule** (mocks always labelled) — no row claims a contacted source
  while the gateway is absent: asserted in every class here, on the row flag
  (``real_source_connected``) and on the provenance statement Grace derives from it.
* **R16/R11** (record what was and was not observed) — the narrowed
  ``hermes_execution_modes`` assertion: ``TestExecutionModesNarrowsWhatItClaims``.
* **R14/T12** (no unapproved outbound or executing action) — the one frozen run and its
  consent gate, at the row and wire level: ``TestTheSubmitTestRunConsentGate``.
* **R11/R16** (half a measurement is reported as half) — ``TestTheHalfMeasuredRowsRecordBothHalves``.
* **R12/R11/T03** (the ledger stores what was measured, and never more) —
  ``TestGraceImportsAHermesRun``: the ship path ``probe --out`` -> wrap -> ``probe-import``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "mini")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from grace.ingest import (import_probe_rows, probe_provenance,  # noqa: E402
                          probe_row_problems, stored_probe_rows)
from switchboard_mini import cli, outcomes as O                     # noqa: E402
from switchboard_mini import hermes_transport as T                  # noqa: E402
from switchboard_mini.hermes_adapter import (                       # noqa: E402
    MEASURED_CAPABILITIES, PROBE_TEST_RUN_INPUT, build_adapter, probe_idempotency_key)
from switchboard_mini.hermes_probe import (APPROVAL_OBSERVATIONS,   # noqa: E402
                                           COMPUTER_ACTION_TERMS,
                                           OWNER_APPROVAL_TRIGGER_COMMAND)
from switchboard_mini.probe import run_probe                        # noqa: E402

from tests.helpers import GraceTestCase, run_cli                    # noqa: E402
from tests.test_mini_hermes_transport import (                      # noqa: E402
    STANDIN_TOKEN, STUB_CREATED_RUN_ID, STUB_SESSION_ID, StubHermesGateway,
    closed_port_base_url)
from tests.test_probe_manifest_self_measurement import (write_jsonl,  # noqa: E402
                                                        wrap_with_jq)

#: The environment variable this file puts the synthetic bearer value in. A private name on
#: purpose: no test here reads or writes the owner's real ``API_SERVER_KEY``, and the value
#: is the stand-in's synthetic string, not a credential.
TOKEN_ENV = "SWITCHBOARD_HERMES_PROBE_TEST_KEY"

#: This worker's own settling bound, shrunk so a test drives the poll path without waiting.
#: O17 states no timeout for a stop, so the bound is ours either way (audited as such).
FAST_SETTLE_SECONDS = 0.2
FAST_SETTLE_INTERVAL = 0.05

#: The state each of the eight rows is required to reach in fixture mode, and the typed reason
#: that makes it specific. Contract facts, so they are written down rather than derived: a
#: change to any of them is a change to what the row claims about the owner's machine.
FIXTURE_STATES = {
    "hermes_capability_discovery": O.SUCCESS,
    "hermes_run_submission_and_status": O.UNSUPPORTED,
    "hermes_run_progress_events": O.UNSUPPORTED,
    "hermes_run_stop": O.UNSUPPORTED,
    "hermes_session_continuity": O.PARTIAL,
    "hermes_approval_modes": O.PARTIAL,
    "hermes_credential_resolution": O.PROBE_UNMEASURED,
    "hermes_execution_modes": O.PARTIAL,
}

#: Where each of those reasons is recorded: a refusal that made no request carries ``reason``
#: in its evidence (``_blocked_row``); a row that read something and still is not a
#: measurement names its sub-state in ``state_reason`` (``_measured``).
FIXTURE_REASONS = {
    "hermes_run_submission_and_status": ("reason", "run_submission_not_consented"),
    "hermes_run_progress_events": ("reason", "run_id_required"),
    "hermes_run_stop": ("reason", "run_id_required"),
    "hermes_credential_resolution": ("state_reason",
                                     "credential_path_not_measurable_by_this_adapter"),
}

#: The rows whose read is a socket call: in real mode with nothing listening they answer the
#: socket reason (T14). The other four act on a run id or on a local check and answer their
#: own typed state instead -- measured below, not assumed.
SOCKET_SETTLED_ROWS = ("hermes_capability_discovery", "hermes_session_continuity",
                       "hermes_approval_modes", "hermes_execution_modes")

#: The rows that act on a run the owner (or the probe's own consent flag) names.
RUN_SCOPED_ROWS = ("hermes_run_submission_and_status", "hermes_run_progress_events",
                   "hermes_run_stop")

#: The two never-read credential files this worker refuses to open (O21).
NEVER_READ_PATHS = ("~/.hermes/.env", "~/.hermes/config.yaml")


# ---------------------------------------------------------------------------------
# the rig: one Hermes-only probe run, in whichever honest mode a test needs
# ---------------------------------------------------------------------------------


def hermes_rows(*, fixture_mode: bool = False, base_url: str | None = None,
                stand_in: bool = False, **hermes) -> list:
    """One Hermes-only probe run, in process, returning its rows.

    A harness error is an instrument failure, not a source state, so it fails the test that
    asked for the run rather than being asserted around: every assertion below is about what
    a *working* probe honestly says.
    """
    options = {"settle_seconds": FAST_SETTLE_SECONDS, "settle_interval": FAST_SETTLE_INTERVAL}
    options.update(hermes)
    adapter = build_adapter(fixture_mode=fixture_mode, base_url=base_url, stand_in=stand_in,
                            token_env=TOKEN_ENV)
    run = run_probe([adapter], only_source="hermes", hermes=options)
    if run.harness_errors:
        raise AssertionError(f"the probe harness failed: {run.harness_errors}")
    return run.rows


def fixture_rows() -> list:
    """The eight rows from the recorded Hermes scenario. Nothing is contacted."""
    return hermes_rows(fixture_mode=True)


def row_for(rows: list, capability: str) -> dict:
    matches = [r for r in rows if r["capability"] == capability]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one row for {capability!r}, got {len(matches)}")
    return matches[0]


def dump(rows: list) -> str:
    """One row set as text, for the "this string appears nowhere" assertions."""
    return json.dumps(rows, sort_keys=True, default=str)


@contextlib.contextmanager
def hermes_standin(**stub_kwargs):
    """A started labelled stand-in, with the synthetic bearer value in the environment.

    The token is restored afterwards, so no other test in the process inherits it.
    """
    saved = {name: os.environ.get(name)
             for name in (TOKEN_ENV, T.BASE_URL_ENV, T.STANDIN_ENV)}
    os.environ[TOKEN_ENV] = STANDIN_TOKEN
    os.environ.pop(T.STANDIN_ENV, None)
    stub = StubHermesGateway(**stub_kwargs).start()
    try:
        yield stub
    finally:
        stub.stop()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class HermesProbeCase(unittest.TestCase):
    """Base case: the stand-in is always stopped, and the environment always restored."""

    def setUp(self) -> None:
        self._saved_env = {name: os.environ.get(name)
                           for name in (TOKEN_ENV, T.BASE_URL_ENV, T.STANDIN_ENV)}
        self._stubs: list = []
        os.environ.pop(T.STANDIN_ENV, None)

    def tearDown(self) -> None:
        for stub in self._stubs:
            stub.stop()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def standin(self, **stub_kwargs) -> StubHermesGateway:
        """A started labelled stand-in, stopped by ``tearDown`` even if a test fails."""
        os.environ[TOKEN_ENV] = STANDIN_TOKEN
        stub = StubHermesGateway(**stub_kwargs).start()
        self._stubs.append(stub)
        return stub

    def standin_rows(self, stub: StubHermesGateway, **hermes) -> list:
        return hermes_rows(base_url=stub.base_url, stand_in=True, **hermes)

    # -- the shipped CLI, in process ---------------------------------------
    def cli_rows(self, *extra: str, stub: StubHermesGateway, submit: bool = False):
        """``switchboard-mini hermes probe`` through the shipped entry point.

        The flags live where the parser puts them: the base URL, the stand-in switch and the
        token variable name belong to the ``hermes`` parser, everything else to its ``probe``
        subcommand. Driven in process so the rows can be read back as data.
        """
        args = ["hermes", "--base-url", stub.base_url, "--stand-in-server",
                "--token-env", TOKEN_ENV, "probe",
                "--settle-seconds", str(FAST_SETTLE_SECONDS),
                "--settle-interval", str(FAST_SETTLE_INTERVAL)]
        if submit:
            args.append("--submit-test-run")
        args.extend(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args)
        rows = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(code, 0, err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        # Exactly one CLI run. A second identical run would double the wire count the stub
        # actually observed and make every ``requests_made`` assertion below a lie about the
        # number of requests one probe makes. Determinism of the idempotency key is pinned
        # separately, by two stubs, in
        # ``test_the_key_is_the_same_on_a_second_run_against_a_second_gateway``.
        return rows, err.getvalue()


# ---------------------------------------------------------------------------------
# R11 / T03 -- the eight rows in fixture mode
# ---------------------------------------------------------------------------------


class TestTheEightRowsInFixtureMode(HermesProbeCase):
    """R11/T03 (labelling rule: a recorded answer is a recorded answer).

    One row per capability key, every one of them ``origin: fixture`` with the shared
    ``FIXTURE:`` label and the Hermes waiver, and not one of them claiming a contacted
    source. These rows are what the pack's Mac procedure will replace, not what it measured.
    """

    def setUp(self) -> None:
        super().setUp()
        self.rows = fixture_rows()

    def test_the_run_emits_one_row_per_capability_and_nothing_else(self) -> None:
        """R11/T03: the Hermes run produces exactly the eight Hermes capability rows."""
        names = [row["capability"] for row in self.rows]
        self.assertEqual(sorted(names), sorted(MEASURED_CAPABILITIES))
        self.assertEqual(len(names), len(set(names)), "one row per capability key")
        for row in self.rows:
            with self.subTest(capability=row["capability"]):
                self.assertEqual(row["source"], "hermes")
                self.assertEqual(row["measurement_target"], O.MEASUREMENT_SOURCE)

    def test_every_row_is_labelled_a_fixture_and_carries_the_waiver(self) -> None:
        """R11/T03 (labelling rule): the label and the disclaimer are on every fixture row."""
        for row in self.rows:
            with self.subTest(capability=row["capability"]):
                self.assertEqual(row["origin"], O.FIXTURE)
                self.assertTrue(O.is_fixture_label(row["label"]), row["label"])
                self.assertIn("hermes", row["label"])
                self.assertTrue(row["disclaimer"], "a fixture row carries a disclaimer")
                self.assertIn("No Hermes gateway was contacted", row["disclaimer"])
                self.assertIsInstance(row["evidence"], dict)

    def test_no_fixture_row_claims_a_contacted_source_or_a_supported_capability(self) -> None:
        """R11/T03: a recorded answer is not an observation of any install.

        This is the whole of the rule in one place: ``real_source_connected`` false on every
        row, ``supported`` false on every row, and no row in the closed permission vocabulary
        claiming a permission a fixture never asked for.
        """
        for row in self.rows:
            with self.subTest(capability=row["capability"]):
                self.assertFalse(row["real_source_connected"])
                self.assertFalse(row["supported"])
                self.assertEqual(row["observed_version"], O.VERSION_NOT_OBSERVED)
                self.assertEqual(row["citations"], [],
                                 "a measurement quotes no page: the pack refs belong in "
                                 "supersedes")
                self.assertIn(row["permission_state"],
                              (O.PERMISSION_NOT_APPLICABLE, O.PERMISSION_NOT_DETERMINED))
                # ``permission_state_reason`` exists only where a state of ``not_applicable``
                # needs explaining: a responder answered and no permission was ever asked
                # for. The rows that are ``not_determined`` carry no such key at all, so an
                # assertion that every row has one is a KeyError waiting to happen.
                reason = row["evidence"].get("permission_state_reason")
                if row["permission_state"] == O.PERMISSION_NOT_APPLICABLE:
                    self.assertTrue(reason, row["capability"])
                else:
                    self.assertIsNone(reason, row["capability"])

    def test_every_row_reaches_its_typed_state_and_names_the_reason(self) -> None:
        """R11/T03: the state is what the read answered, and the reason makes it specific."""
        self.assertEqual(len(self.rows), len(FIXTURE_STATES))
        for row in self.rows:
            capability = row["capability"]
            with self.subTest(capability=capability):
                self.assertEqual(row["state"], FIXTURE_STATES[capability])
        for capability, (where, reason) in FIXTURE_REASONS.items():
            with self.subTest(capability=capability):
                self.assertEqual(row_for(self.rows, capability)["evidence"][where], reason)
        # The three refusals made no request at all, and say so rather than looking empty.
        for capability in ("hermes_run_submission_and_status", "hermes_run_progress_events",
                           "hermes_run_stop"):
            with self.subTest(capability=capability):
                evidence = row_for(self.rows, capability)["evidence"]
                self.assertEqual(evidence["requests_made"], 0)
                self.assertIs(evidence["no_request_made"], True)
                self.assertTrue(evidence["next_action"])

    def test_every_row_names_the_documentation_row_it_supersedes(self) -> None:
        """R11/T03: a measured row says which pack record it replaces, refs and all."""
        for row in self.rows:
            with self.subTest(capability=row["capability"]):
                supersedes = row["supersedes"]
                self.assertIsInstance(supersedes, dict)
                self.assertEqual(supersedes["origin"], O.DOCUMENTATION)
                self.assertEqual(supersedes["capability"], row["capability"])
                citations = supersedes["citations"]
                self.assertTrue(citations, "the superseded page read is named by its refs")
                for citation in citations:
                    self.assertRegex(citation, r"^O\d\d$")
                self.assertEqual(supersedes["documented_state"], O.PROBE_UNMEASURED)


# ---------------------------------------------------------------------------------
# T14 / R11 -- the eight rows in real mode with nothing listening
# ---------------------------------------------------------------------------------


class TestTheEightRowsInRealModeWithNothingListening(HermesProbeCase):
    """T14: with no gateway the answer is a socket fact, and the run-id rows say what they
    are waiting for rather than inventing a result. The real transport is the one under test
    here -- ``adapter_is_real`` true -- and still not one row claims a source that answered.
    """

    def setUp(self) -> None:
        super().setUp()
        # A bound-and-released port: the socket genuinely refuses. The bearer value is
        # present, so what stops the reads is reachability and not the credential gate.
        os.environ[TOKEN_ENV] = STANDIN_TOKEN
        self.base_url = closed_port_base_url()
        self.rows = hermes_rows(base_url=self.base_url)

    def test_the_socket_settled_rows_answer_the_socket_reason(self) -> None:
        """T14: every read that goes to the wire reports ``hermes_not_reachable``/``offline``."""
        for capability in SOCKET_SETTLED_ROWS:
            with self.subTest(capability=capability):
                row = row_for(self.rows, capability)
                self.assertEqual(row["state"], O.OFFLINE)
                self.assertEqual(row["evidence"]["reason"], "hermes_not_reachable")
                self.assertGreaterEqual(row["evidence"]["requests_made"], 1,
                                        "the socket was tried, not skipped")
                self.assertFalse(row["real_source_connected"])
                self.assertFalse(row["supported"])

    def test_the_run_scoped_rows_answer_their_own_typed_state(self) -> None:
        """R14/R11: a run id this worker does not have is refused, not guessed, and the
        consent gate is reported as the gate it is."""
        submission = row_for(self.rows, "hermes_run_submission_and_status")
        self.assertEqual(submission["state"], O.UNSUPPORTED)
        self.assertEqual(submission["evidence"]["reason"], "run_submission_not_consented")
        self.assertFalse(submission["evidence"]["consent_flag_supplied"])
        self.assertEqual(submission["evidence"]["input_text"], PROBE_TEST_RUN_INPUT)
        self.assertIs(submission["evidence"]["general_submission_available"], False)
        for capability in ("hermes_run_progress_events", "hermes_run_stop"):
            with self.subTest(capability=capability):
                row = row_for(self.rows, capability)
                self.assertEqual(row["state"], O.UNSUPPORTED)
                self.assertEqual(row["evidence"]["reason"], "run_id_required")
                self.assertIs(row["evidence"]["no_request_made"], True)
                self.assertTrue(row["evidence"]["next_action"])
        credential = row_for(self.rows, "hermes_credential_resolution")
        self.assertEqual(credential["state"], O.PROBE_UNMEASURED)
        self.assertEqual(credential["evidence"]["state_reason"],
                         "credential_path_not_measurable_by_this_adapter")

    def test_no_row_names_a_mac_only_reason_or_claims_a_source(self) -> None:
        """T14/R11: reachability is a socket fact. Every row is real, unlabelled as a
        fixture, and honest that nothing answered it."""
        for row in self.rows:
            with self.subTest(capability=row["capability"]):
                self.assertEqual(row["origin"], O.REAL)
                self.assertTrue(row["adapter_is_real"])
                self.assertIsNone(row["label"], "a real-origin row is not a fixture row")
                self.assertFalse(row["real_source_connected"])
                self.assertFalse(row["supported"])
                self.assertEqual(row["observed_version"], O.VERSION_NOT_OBSERVED)
        text = dump(self.rows)
        for mac_reason in ("host_not_macos", "not_macos", "darwin"):
            with self.subTest(reason=mac_reason):
                self.assertNotIn(mac_reason, text)


class TestARealRunWithNoGatewayKeyLeavesTheBoxAlone(HermesProbeCase):
    """T14/R14: with no bearer key nothing is contacted at all -- the gate fires first, and
    the rows say ``permission_denied``/``token_absent`` instead of a source-shaped answer."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop(TOKEN_ENV, None)
        self.rows = hermes_rows(base_url=closed_port_base_url())

    def test_the_gated_rows_report_a_denied_permission_and_no_request(self) -> None:
        """R14/T14: no key means no request, and the permission state says why."""
        for capability in ("hermes_capability_discovery", "hermes_session_continuity",
                           "hermes_approval_modes", "hermes_execution_modes",
                           "hermes_credential_resolution"):
            with self.subTest(capability=capability):
                row = row_for(self.rows, capability)
                self.assertEqual(row["state"], O.PERMISSION_DENIED)
                # The *state* word for a denied permission is ``denied``; ``permission_denied``
                # is the outcome code above. Two different vocabularies, one row.
                self.assertEqual(row["permission_state"], O.PERMISSION_STATE_DENIED)
                self.assertEqual(row["evidence"]["reason"], "token_absent")
                self.assertIs(row["evidence"]["token_present"], False)
                self.assertIs(row["evidence"]["token_value_recorded"], False)
                self.assertFalse(row["supported"])
                self.assertFalse(row["real_source_connected"])

    def test_the_run_scoped_rows_still_refuse_rather_than_report(self) -> None:
        """R11: a row that never reached a source reports the state that stopped it."""
        for capability in RUN_SCOPED_ROWS:
            with self.subTest(capability=capability):
                row = row_for(self.rows, capability)
                self.assertEqual(row["state"], O.UNSUPPORTED)
                self.assertFalse(row["supported"])
                self.assertFalse(row["real_source_connected"])
        self.assertNotIn("host_not_macos", dump(self.rows))


# ---------------------------------------------------------------------------------
# R16 / R11 -- the execution-modes row, narrowed to what was observed
# ---------------------------------------------------------------------------------


class TestExecutionModesNarrowsWhatItClaims(HermesProbeCase):
    """R16/R11 (audit finding A): the row's assertion names a *search* over the capability
    surface this build returns, so the row may not claim the computer-action half from a
    constant. These tests put three different surfaces in front of it and read the result."""

    CAPABILITY = "hermes_execution_modes"

    def test_the_row_is_partial_and_unsupported_while_a_half_is_missing(self) -> None:
        """R16: one half measured is not a supported row, and the missing half is named."""
        with hermes_standin() as stub:
            row = row_for(self.standin_rows(stub), self.CAPABILITY)
        self.assertEqual(row["state"], O.PARTIAL)
        self.assertFalse(row["supported"])
        self.assertIsNone(row["evidence"]["terminal_backend_owner_supplied"])
        self.assertIsNone(row["evidence"]["terminal_backend_supplied_by"])
        self.assertTrue(row["limitation"])

    def test_the_computer_action_half_is_a_search_over_the_names_the_build_returned(self) -> None:
        """R16: the searched surface is recorded, so a search that found nothing is
        distinguishable from a search that never happened."""
        with hermes_standin() as stub:
            row = row_for(self.standin_rows(stub), self.CAPABILITY)
        evidence = row["evidence"]
        observed = evidence["computer_action_observed"]
        searched = set(observed["feature_keys_searched"]) | set(evidence["toolset_names"]) \
            | set(evidence["tool_names_seen"])
        self.assertEqual(observed["names_searched"], len(searched))
        self.assertEqual(observed["terms_searched_for"], list(COMPUTER_ACTION_TERMS))
        self.assertEqual(sorted(observed["names_searched_sample"]), sorted(searched))
        self.assertEqual(observed["matches"], [])
        self.assertIs(observed["observed_absent"], True)
        self.assertIsNone(evidence["computer_action_mechanism"])
        self.assertFalse(row["supported"], "a search that found nothing is not support")

    def test_a_match_is_recorded_as_the_name_it_was_and_never_as_reachability(self) -> None:
        """R16: when a name matches, the row records *that name* and nothing more."""
        with hermes_standin(body_overrides={
            "/v1/capabilities": json.dumps({
                "object": "hermes.api_server.capabilities", "platform": "stand-in",
                "model": "stand-in-model", "auth": {"type": "bearer", "required": True},
                "features": {"runs": True, "computer_use": True}}),
            "/v1/toolsets": json.dumps({"toolsets": [
                {"name": "terminal", "enabled": True, "configured": True,
                 "tools": ["shell"]},
                {"name": "screen", "enabled": True, "configured": False,
                 "tools": ["screen_click", "read_file"]}]}),
        }) as stub:
            row = row_for(self.standin_rows(stub), self.CAPABILITY)
        observed = row["evidence"]["computer_action_observed"]
        self.assertEqual(observed["observed_absent"], False)
        self.assertIn("computer_use", observed["matches"])
        self.assertIn("screen_click", observed["matches"])
        self.assertEqual(row["evidence"]["computer_action_mechanism"],
                         sorted(observed["matches"])[0],
                         "the recorded mechanism is one of the names actually seen")
        self.assertIn(observed["matches"][0], observed["names_searched_sample"])
        # A match is a name, not a reachability claim: the row still may not be supported,
        # and its own assertion says reachability is not what it is asserting.
        self.assertFalse(row["supported"])
        self.assertIn("is not asserted by this row", row["probe_assertion"])

    def test_an_empty_surface_means_nothing_claimed_rather_than_absence(self) -> None:
        """R16/R11: an empty document is not evidence of absence.

        ``observed_absent`` stays ``null`` when the surface returned no names at all, so the
        row can never read as "searched and found none" -- the very rounding-up the audit
        finding was about.
        """
        with hermes_standin(body_overrides={
            "/v1/capabilities": json.dumps({"object": "hermes.api_server.capabilities"}),
            "/v1/toolsets": json.dumps({"toolsets": []}),
            "/v1/skills": json.dumps({"skills": []}),
        }) as stub:
            row = row_for(self.standin_rows(stub), self.CAPABILITY)
        observed = row["evidence"]["computer_action_observed"]
        self.assertEqual(observed["names_searched"], 0)
        self.assertIsNone(observed["observed_absent"],
                          "nothing observed must mean nothing claimed")
        self.assertIsNone(row["evidence"]["computer_action_mechanism"])
        self.assertEqual(observed["matches"], [])
        self.assertEqual(row["state"], O.PARTIAL)
        self.assertFalse(row["supported"])
        self.assertIn("observed_absent is null when the surface returned no names",
                      observed["basis"])

    def test_the_assertion_names_exactly_the_surfaces_the_evidence_holds(self) -> None:
        """R16/R11: the row's assertion and its evidence have to be the same claim.

        The assertion may only describe surfaces the evidence records, and it has to name
        the recorded outcome ``observed_absent`` -- otherwise a reader would be checking a
        sentence the row cannot answer for.
        """
        with hermes_standin() as stub:
            row = row_for(self.standin_rows(stub), self.CAPABILITY)
        assertion = row["probe_assertion"]
        evidence = row["evidence"]
        for phrase in ("advertised feature keys", "toolset names", "tool names",
                       "observed_absent"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, assertion)
        observed = evidence["computer_action_observed"]
        self.assertIn("feature_keys_searched", observed)
        self.assertIn("toolset_names", evidence)
        self.assertIn("tool_names_seen", evidence)
        self.assertIn("observed_absent", observed)
        # And every surface noun the assertion uses is one the row actually read: the
        # toolset read answered, and the advertised-feature read is recorded either way.
        self.assertIs(evidence["computer_action_observed"]["capabilities_read_answered"], True)
        self.assertTrue(evidence["toolset_field_names_seen"] or evidence["tool_names_seen"])


# ---------------------------------------------------------------------------------
# R14 / T12 -- the one frozen run and its consent gate, at the row level
# ---------------------------------------------------------------------------------


class TestTheSubmitTestRunConsentGate(HermesProbeCase):
    """R14/T12: ``hermes probe --submit-test-run`` is the only door to a run this worker
    creates, and it creates exactly one kind: its own frozen constant, quoted in the row."""

    def created_run_stub(self) -> StubHermesGateway:
        """A stand-in that also answers the run this probe creates for itself.

        Without these overrides the created run is a path the stand-in does not know, so the
        run-scoped rows would report the 404 -- true, but it would prove nothing about the
        consent path. With them, the rows can be read as what the probe did with its own run.
        """
        return self.standin(body_overrides={
            f"/v1/runs/{STUB_CREATED_RUN_ID}": json.dumps(
                {"run_id": STUB_CREATED_RUN_ID, "status": "completed"}),
            f"/v1/runs/{STUB_CREATED_RUN_ID}/stop": json.dumps({"status": "stopping"}),
            f"/v1/runs/{STUB_CREATED_RUN_ID}/events": "event: run.completed\ndata: {}\n",
        })

    def test_with_consent_the_row_quotes_the_frozen_constant_and_the_key(self) -> None:
        """R14/T12: the row shows what was sent, and the key is deterministic and ours."""
        stub = self.created_run_stub()
        rows, _ = self.cli_rows(stub=stub, submit=True)
        row = row_for(rows, "hermes_run_submission_and_status")
        evidence = row["evidence"]
        self.assertIs(evidence["consent_flag_supplied"], True)
        self.assertEqual(evidence["input_text"], PROBE_TEST_RUN_INPUT)
        self.assertIs(evidence["input_text_is_a_frozen_constant_of_this_worker"], True)
        self.assertIs(evidence["general_submission_available"], False)
        self.assertEqual(evidence["idempotency_key_used"],
                         probe_idempotency_key(PROBE_TEST_RUN_INPUT))
        self.assertEqual(evidence["idempotency_key_used"],
                         probe_idempotency_key(PROBE_TEST_RUN_INPUT),
                         "the key is derived from the payload, so it cannot drift")
        self.assertIs(evidence["idempotency_key_is_ours_and_is_not_a_secret"], True)
        self.assertTrue(row["probe_assertion"])

    def test_the_key_is_the_same_on_a_second_run_against_a_second_gateway(self) -> None:
        """R14/T12: a re-run of the probe cannot accidentally use a fresh key.

        O17's replay behaviour is keyed on the ``Idempotency-Key``, so a key that changed
        between runs would turn a retry into a second run on a real gateway. Two distinct
        stand-ins are used so this cannot pass by sharing one server's state.
        """
        keys = []
        for _ in range(2):
            stub = self.created_run_stub()
            rows, _ = self.cli_rows(stub=stub, submit=True)
            keys.append(row_for(rows, "hermes_run_submission_and_status")
                        ["evidence"]["idempotency_key_used"])
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0], probe_idempotency_key(PROBE_TEST_RUN_INPUT))

    def test_every_run_creating_post_carries_the_same_frozen_input_and_the_same_key(self) -> None:
        """R14/T12: one logical run, and it is the frozen constant.

        Two POSTs arrive at ``/v1/runs`` and both are the submission: the probe sends it
        once and then once more *identically*, under the same key, to observe O17's documented
        replay ("An identical retry returns the original run_id with HTTP 202 and
        ``Idempotency-Replayed: true``"). So the count is asserted as the shipped behaviour,
        and what matters for R14 is asserted separately: there is exactly **one distinct**
        body-and-key pair, so nothing but the frozen constant -- and no second run -- can come
        out of this run.
        """
        stub = self.created_run_stub()
        self.cli_rows(stub=stub, submit=True)
        posts = [r for r in stub.requests if r["method"] == "POST"]
        submissions = [r for r in posts if r["path"] == "/v1/runs"]
        self.assertEqual(len(submissions), 2,
                         "the submission and its identical replay, as recorded here")
        bodies = {r["body_text"] for r in submissions}
        self.assertEqual(bodies, {json.dumps({"input": PROBE_TEST_RUN_INPUT})})
        self.assertEqual(json.loads(submissions[0]["body_text"]),
                         {"input": PROBE_TEST_RUN_INPUT})
        self.assertTrue(all(r["idempotency_key_present"] for r in submissions))
        self.assertEqual(len({(r["body_text"], r["idempotency_key_present"])
                              for r in submissions}), 1,
                         "one distinct submission: the replay is the same request again")
        # No other POST creates anything: the only other one is the stop of the run this
        # probe itself created, and it names that run.
        others = [r for r in posts if r["path"] != "/v1/runs"]
        for request in others:
            with self.subTest(path=request["path"]):
                self.assertEqual(request["path"], f"/v1/runs/{STUB_CREATED_RUN_ID}/stop")

    def test_the_one_run_the_probe_creates_is_the_run_the_run_scoped_rows_read(self) -> None:
        """R11 (audit finding 2): the run the probe created is the run the rows read.

        The events and stop rows used to refuse for a missing run id and advise the reader to
        pass the flag they had just passed. Now the created run is recorded in the shared
        context, and the rows say whose id it is.
        """
        stub = self.created_run_stub()
        rows, _ = self.cli_rows(stub=stub, submit=True)
        submission = row_for(rows, "hermes_run_submission_and_status")["evidence"]
        self.assertIs(submission["created_run_id_recorded_for_the_run_scoped_rows"], True)
        self.assertIs(submission["run_id_present"], True)
        for capability in ("hermes_run_progress_events", "hermes_run_stop"):
            with self.subTest(capability=capability):
                evidence = row_for(rows, capability)["evidence"]
                self.assertIs(evidence["run_id_supplied"], True)
                self.assertIn("this probe's own frozen test run",
                              evidence["run_id_supplied_by"])
                self.assertNotEqual(evidence.get("reason"), "run_id_required")
        # No identifier is printed in full: the rows carry a fingerprint, and the raw value
        # never appears in the document.
        self.assertNotIn(STUB_CREATED_RUN_ID, dump(rows))
        self.assertTrue(row_for(rows, "hermes_run_progress_events")
                        ["evidence"]["run_id_fingerprint"])

    def test_the_stand_in_can_never_make_these_rows_a_measurement(self) -> None:
        """R11 (labelling rule): an answering stand-in is still not a gateway."""
        stub = self.created_run_stub()
        rows, _ = self.cli_rows(stub=stub, submit=True)
        for row in rows:
            with self.subTest(capability=row["capability"]):
                self.assertFalse(row["supported"])
                self.assertFalse(row["real_source_connected"])
                self.assertEqual(row["origin"], O.REAL)
        row = row_for(rows, "hermes_run_submission_and_status")
        self.assertEqual(row["state"], O.PARTIAL)
        self.assertIn("stand_in_http_server", dump([row]))
        self.assertIn("Nothing here is an observation of Randy's Hermes.", row["limitation"])

    def test_without_consent_not_one_post_reaches_a_source_that_is_answering(self) -> None:
        """R14: a reachable source and the frozen constant are still not consent."""
        stub = self.standin()
        rows, _ = self.cli_rows(stub=stub)
        self.assertTrue([r for r in stub.requests if r["method"] == "GET"],
                        "the stand-in must be answering, or this proves nothing")
        self.assertEqual([r for r in stub.requests if r["method"] == "POST"], [])
        submission = row_for(rows, "hermes_run_submission_and_status")
        self.assertEqual(submission["state"], O.UNSUPPORTED)
        self.assertEqual(submission["evidence"]["reason"], "run_submission_not_consented")
        self.assertIs(submission["evidence"]["consent_flag_supplied"], False)
        self.assertIs(submission["evidence"]["no_request_made"], True)
        self.assertEqual(submission["evidence"]["input_text"], PROBE_TEST_RUN_INPUT)
        for capability in ("hermes_run_progress_events", "hermes_run_stop"):
            with self.subTest(capability=capability):
                evidence = row_for(rows, capability)["evidence"]
                self.assertFalse(evidence["submit_test_run_consent_supplied"])

    def test_no_row_and_no_cli_line_carries_the_bearer_key_or_touches_approval(self) -> None:
        """R14/T12: the key is not in the rows, and the approval endpoint is never called.

        The approval endpoint is Hermes's own runtime safeguard for shell/file/MCP gates
        (O20) and is never Switchboard's send approval; this worker will not construct its
        body, so it must not touch the path at all.
        """
        stub = self.created_run_stub()
        rows, err = self.cli_rows(stub=stub, submit=True)
        self.assertNotIn(STANDIN_TOKEN, dump(rows))
        self.assertNotIn(STANDIN_TOKEN, err)
        self.assertEqual([r for r in stub.requests if "approval" in r["path"]], [])
        self.assertFalse([r for r in stub.requests
                          if STANDIN_TOKEN in (r["body_text"] or "")])
        # The trigger command is *named* on the approval row's evidence so the owner can run it
        # by hand; what must never happen is this worker submitting it. So the assertion is
        # about the wire and about the row's own record of who submitted it -- not about the
        # string appearing in a row the row is entitled to print.
        self.assertFalse([r for r in stub.requests
                          if OWNER_APPROVAL_TRIGGER_COMMAND in (r["body_text"] or "")],
                         "the worker never submits the approval trigger command")
        for row in rows:
            self.assertIsNot(
                row["evidence"].get("worker_submitted_a_command_to_trip_approval"), True,
                f"{row['capability']} claims to have submitted the trigger command")


# ---------------------------------------------------------------------------------
# R11 / R16 -- the half-measured rows
# ---------------------------------------------------------------------------------


class TestTheHalfMeasuredRowsRecordBothHalves(HermesProbeCase):
    """R11/R16: four of the eight assertions cannot be fully evaluated by this worker alone.
    Each of those rows has to say which half it reached, which half it did not, and must not
    report a permission it never had."""

    def setUp(self) -> None:
        super().setUp()
        self.stub = self.standin()
        self.rows = self.standin_rows(
            self.stub, terminal_backend="local", approval_observation="waiting_for_approval",
            version="9.9.9-test", session_id=STUB_SESSION_ID)
        self.half_measured = ("hermes_session_continuity", "hermes_execution_modes",
                              "hermes_approval_modes", "hermes_credential_resolution")

    def test_session_continuity_records_the_read_half_and_withholds_the_identifiers(self) -> None:
        """R11: the read half is what was exercised, and it says so -- no identifier printed."""
        row = row_for(self.rows, "hermes_session_continuity")
        evidence = row["evidence"]
        self.assertEqual(row["state"], O.PARTIAL)
        self.assertIs(evidence["listing_is_a_list"], True)
        self.assertEqual(evidence["session_count"], 1)
        self.assertIs(evidence["session_read_answered"], True)
        self.assertIs(evidence["messages_read_answered"], True)
        self.assertEqual(evidence["documented_params_sent"], ["limit"])
        self.assertEqual(evidence["session_id_supplied_by"], "owner (--session-id)")
        self.assertIs(evidence["session_id_value_withheld"], True)
        self.assertIs(evidence["session_identifiers_withheld"], True)
        self.assertIs(evidence["message_text_recorded"], False)
        self.assertNotIn(STUB_SESSION_ID, dump([row]))
        self.assertTrue(evidence["session_id_fingerprint"])
        self.assertFalse(row["supported"])

    def test_execution_modes_records_the_owners_backend_and_keeps_the_rest_unmeasured(self) -> None:
        """R11: an owner-supplied ``terminal.backend`` is recorded as the owner's, and the
        row still may not claim the half this worker cannot read."""
        row = row_for(self.rows, "hermes_execution_modes")
        self.assertEqual(row["evidence"]["terminal_backend_owner_supplied"], "local")
        self.assertEqual(row["evidence"]["terminal_backend_supplied_by"],
                         "the owner (--terminal-backend)")
        self.assertIn("local", row["evidence"]["documented_terminal_backends"])
        self.assertFalse(row["supported"])
        self.assertEqual(row["state"], O.PARTIAL)

    def test_approval_modes_records_the_observation_and_what_it_does_not_settle(self) -> None:
        """R11 (audit finding B): one observation settles one sub-behaviour, and the row must
        not round that up into the whole assertion."""
        row = row_for(self.rows, "hermes_approval_modes")
        evidence = row["evidence"]
        self.assertEqual(evidence["approval_observation_supplied_by_owner"],
                         "waiting_for_approval")
        self.assertEqual(evidence["approval_observation_allowed_values"],
                         list(APPROVAL_OBSERVATIONS))
        self.assertIs(evidence["worker_submitted_a_command_to_trip_approval"], False)
        self.assertIs(evidence["is_the_send_approval"], False)
        self.assertIn(OWNER_APPROVAL_TRIGGER_COMMAND, evidence["owner_trigger_command"])
        self.assertFalse(row["supported"])
        # The sub-behaviour split is only written when a real gateway answered (the
        # stand-in short-circuits first), so what is asserted here is that the row does not
        # claim the split it did not observe.
        self.assertNotIn("approval_sub_behaviours_measured", evidence)

    def test_credential_resolution_records_why_it_cannot_be_measured_here(self) -> None:
        """R11: the credential path is an owner-side measurement, and the row says so."""
        row = row_for(self.rows, "hermes_credential_resolution")
        evidence = row["evidence"]
        self.assertEqual(row["state"], O.PROBE_UNMEASURED)
        self.assertIs(evidence["not_measurable_by_this_adapter"], True)
        self.assertIs(evidence["no_credential_value_recorded"], True)
        self.assertEqual(evidence["never_read_paths"], list(NEVER_READ_PATHS))
        self.assertIn("only that API_SERVER_KEY", evidence["authenticated_read_proves"])
        self.assertIn("op://", evidence["authenticated_read_does_not_prove"])
        self.assertEqual(evidence["state_reason"],
                         "credential_path_not_measurable_by_this_adapter")
        self.assertTrue(evidence["state_reason_next_action"])
        self.assertFalse(row["supported"])

    def test_none_of_the_four_reports_a_permission_it_never_had(self) -> None:
        """R11/T03 (labelling rule): a stand-in and a local check grant no permission, so
        the row may not report one -- ``not_applicable``/``not_determined``, never granted."""
        for capability in self.half_measured:
            with self.subTest(capability=capability):
                row = row_for(self.rows, capability)
                self.assertIn(row["permission_state"],
                              (O.PERMISSION_NOT_APPLICABLE, O.PERMISSION_NOT_DETERMINED))
                self.assertNotEqual(row["permission_state"], O.PERMISSION_GRANTED)
                # A row the stand-in answered explains why no permission applies; a row that
                # contacted nothing explains why nothing was determined. Both reasons, where
                # present, are explanations of NOT having a permission.
                reason = row["evidence"].get("permission_state_reason")
                if row["permission_state"] == O.PERMISSION_NOT_APPLICABLE:
                    self.assertTrue(reason, capability)
                elif reason is not None:
                    self.assertIn("no permission", reason, capability)
                self.assertFalse(row["real_source_connected"])

    def test_an_owner_supplied_version_is_recorded_as_the_owners_not_as_observed(self) -> None:
        """R11: no documented Hermes surface exposes a build version, so a version this row
        reports can only be the owner's, and it says so."""
        for capability in self.half_measured:
            with self.subTest(capability=capability):
                row = row_for(self.rows, capability)
                self.assertEqual(row["observed_version"], "9.9.9-test")
                self.assertIn("owner-supplied", row["observed_version_reason"])
                self.assertIn("did not observe it itself", row["observed_version_reason"])


# ---------------------------------------------------------------------------------
# R12 / R11 -- Grace consumes a Hermes run
# ---------------------------------------------------------------------------------


def stored_by_name(store) -> dict:
    return {row["name"]: row for row in stored_probe_rows(store)}


class TestGraceImportsAHermesRun(GraceTestCase):
    """R12/R11/T03: the ship path -- ``hermes probe --out`` -> wrap -> ``grace probe-import``.

    No test anywhere else in this repository pushes a measured Hermes row through
    ``probe_row_problems``/``import_probe_rows`` (the lead drove the handover by hand at
    5780185; this is that drive, made repeatable and read back out of the ledger). The ledger
    is built here and seeded by ``grace seed``: nothing depends on a database outside the
    test's own temporary directory.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.account = self.account_id("mock_hermes")
        self.assertTrue(self.account, "grace seed must provide a Hermes source account")
        self.rows = fixture_rows()

    def wrapped(self, rows: list, tmp: str) -> str:
        """One JSONL run wrapped the way the readiness pack documents: ``jq -s '{rows: .}'``."""
        jsonl = Path(tmp) / "hermes-rows.jsonl"
        wrapped = Path(tmp) / "hermes-import.json"
        write_jsonl(jsonl, rows)
        wrap_with_jq(jsonl, wrapped)
        return str(wrapped)

    def import_via_cli(self, path: str) -> dict:
        return run_cli(self.db, "probe-import", "--file", path, "--account", self.account)

    def test_the_cli_imports_the_eight_hermes_rows_whole(self) -> None:
        """R12/T03: the run the worker emits imports as it is -- no filter step, nothing
        refused, and the count is the number of rows it carried."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.import_via_cli(self.wrapped(self.rows, tmp))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"]["imported"], len(self.rows))
        self.assertEqual(payload["data"]["refused"], 0)
        self.assertEqual(payload["data"]["problems"], [])
        self.assertIn(self.account, payload["message"])
        self.assertEqual(len(payload["data"]["provenance"]["sources"]), 1)
        self.assertEqual(payload["data"]["provenance"]["sources"], ["hermes"])

    def test_the_stored_rows_read_back_as_what_the_worker_emitted(self) -> None:
        """R11/R12: the ledger keeps the row, and every honesty flag survives the round trip."""
        with tempfile.TemporaryDirectory() as tmp:
            self.import_via_cli(self.wrapped(self.rows, tmp))
        stored = stored_by_name(self.svc.store)
        for row in self.rows:
            capability = row["capability"]
            with self.subTest(capability=capability):
                self.assertIn(capability, stored)
                kept = stored[capability]
                self.assertEqual(kept["state"], row["state"])
                self.assertEqual(kept["supported"], bool(row["supported"]))
                self.assertEqual(kept["permission"], row["permission_state"])
                self.assertEqual(kept["origin"], row["origin"])
                self.assertEqual(kept["origin"], O.FIXTURE)
                self.assertTrue(O.is_fixture_label(kept["label"]), kept["label"])
                self.assertFalse(kept["real_source_connected"])
                self.assertIs(kept["values_from_source"], bool(row["values_from_source"]))
                self.assertEqual(kept["observed_version"], O.VERSION_NOT_OBSERVED)
                self.assertEqual(kept["measurement_target"], O.MEASUREMENT_SOURCE)
                self.assertEqual(kept["sourced_refs"], [],
                                 "a measurement quotes no page")
                self.assertTrue(kept["limitation"] or row["limitation"] is None)

    def test_the_ledger_still_says_no_source_was_contacted(self) -> None:
        """R11/T03 (labelling rule): the provenance sentence is derived from the stored rows,
        so it cannot keep saying nothing was contacted once a row does contact something."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.import_via_cli(self.wrapped(self.rows, tmp))
        provenance = payload["data"]["provenance"]
        self.assertEqual(provenance["real_source_connected"], 0)
        self.assertTrue(provenance["no_source_contacted"])
        self.assertEqual(provenance["fixture_rows"], len(self.rows))
        self.assertEqual(provenance["documentation_rows"], 0)
        self.assertEqual(provenance["supported"], 0)
        unmeasured = [r for r in self.rows if r["state"] == O.PROBE_UNMEASURED]
        self.assertEqual(provenance["unmeasured"], len(unmeasured))
        self.assertIn(f"No source was contacted by any of the {len(self.rows)} capability rows",
                      provenance["statement"])
        self.assertIn("came from recorded fixtures", provenance["statement"])
        self.assertIn("Nothing here is an observation of Randy's Mac.",
                      provenance["statement"])
        # The same sentence, re-derived from the ledger rather than from the import result.
        from_ledger = probe_provenance(stored_probe_rows(self.svc.store))
        self.assertIn("No source was contacted", from_ledger["statement"])
        self.assertTrue(from_ledger["no_source_contacted"])

    def test_a_documentation_read_may_not_replace_the_stored_measurement(self) -> None:
        """R12/R11: once a Hermes capability is measured, a page read cannot displace it."""
        with tempfile.TemporaryDirectory() as tmp:
            self.import_via_cli(self.wrapped(self.rows, tmp))
        before = stored_probe_rows(self.svc.store)
        page_read = dict(row_for(self.rows, "hermes_capability_discovery"))
        page_read.update({"origin": O.DOCUMENTATION, "supported": False,
                          "state": O.PROBE_UNMEASURED, "citations": ["O17"],
                          "values_from_source": False, "real_source_connected": False,
                          "label": O.documentation_label("hermes", ("O17",)),
                          "supersedes": None})
        result = import_probe_rows(self.svc.store, self.account, [page_read])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["refusals"][0]["capability"], "hermes_capability_discovery")
        self.assertEqual(result["refusals"][0]["reason"],
                         "documentation_would_replace_a_measurement")
        self.assertEqual(result["refusals"][0]["stored_origin"], O.FIXTURE)
        self.assertTrue(result["refusals"][0]["next_action"])
        self.assertEqual(stored_probe_rows(self.svc.store), before,
                         "a refused import leaves the ledger exactly as it was")

    def test_a_documentation_row_still_may_never_be_supported(self) -> None:
        """R11/T03 (probe-pack scope statement): a page read can never set ``supported``."""
        page_read = dict(row_for(self.rows, "hermes_capability_discovery"))
        page_read.update({"origin": O.DOCUMENTATION, "supported": True,
                          "state": O.PROBE_UNMEASURED, "citations": ["O17"],
                          "values_from_source": False, "real_source_connected": False,
                          "supersedes": None})
        problems = probe_row_problems(page_read)
        self.assertTrue(any("may never be supported" in p for p in problems), problems)
        before = stored_probe_rows(self.svc.store)
        result = import_probe_rows(self.svc.store, self.account, [page_read])
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["refused"], 1)
        self.assertEqual(stored_probe_rows(self.svc.store), before)

    def test_a_run_against_the_stand_in_may_not_arrive_as_a_measurement(self) -> None:
        """R14/R11: the stand-in rows are ``origin: real`` with ``real_source_connected:
        false``, so they may be stored as what they are -- a record of a dry run against a
        labelled responder -- and never as a source measurement.

        Two halves, both asserted: as emitted they carry no support and no source claim (and
        store as such, with the ledger still saying nothing was contacted); the moment one of
        them claims ``supported``, Grace refuses the whole import (``grace/ingest.py``'s
        real-origin rule) and the ledger is untouched.
        """
        with hermes_standin() as stub:
            standin = hermes_rows(base_url=stub.base_url, stand_in=True)
        self.assertEqual(len(standin), len(MEASURED_CAPABILITIES))
        for row in standin:
            with self.subTest(capability=row["capability"]):
                self.assertEqual(row["origin"], O.REAL)
                self.assertFalse(row["real_source_connected"])
                self.assertFalse(row["supported"])
                self.assertEqual(probe_row_problems(row), [],
                                 "an honest stand-in row is storable as what it is")
        # ... and storing them never produces a source claim.
        self.assertTrue(import_probe_rows(self.svc.store, self.account, standin)["ok"])
        ledger = probe_provenance(stored_probe_rows(self.svc.store))
        self.assertEqual(ledger["real_source_connected"], 0)
        self.assertIn("No source was contacted", ledger["statement"])
        # Which rows the stand-in actually answered: only those can carry the stand-in wording.
        # The rows that contacted nothing record their own typed reason instead, so a blanket
        # assertion that every stored limitation names the responder is false of this run.
        answered = {row["capability"] for row in standin
                    if row["evidence"].get("responder") == "stand_in_http_server"}
        self.assertTrue(answered, "the stand-in answered at least the read rows of this run")
        for row in stored_probe_rows(self.svc.store):
            if row["name"] in set(MEASURED_CAPABILITIES):
                with self.subTest(capability=row["name"]):
                    self.assertFalse(row["supported"])
                    self.assertFalse(row["real_source_connected"])
                    self.assertTrue(row["limitation"],
                                    "every stored row still carries its limitation text")
                    if row["name"] in answered:
                        self.assertIn("stand_in_http_server", row["limitation"])

    def test_a_stand_in_row_that_claims_support_is_refused_whole(self) -> None:
        """R14/R11: the moment a stand-in row claims a measurement, Grace refuses it.

        This is the same rule as the next test from the other side: a stand-in row *is*
        ``origin: real``, so a support claim on it is exactly the over-claim
        ``grace/ingest.py`` refuses.
        """
        with hermes_standin() as stub:
            standin = hermes_rows(base_url=stub.base_url, stand_in=True)
        before = stored_probe_rows(self.svc.store)
        overclaiming = [{**row, "supported": True, "values_from_source": True}
                        if row["capability"] == "hermes_capability_discovery" else dict(row)
                        for row in standin]
        problems = probe_row_problems(overclaiming[0])
        self.assertTrue(any("real_source_connected" in p for p in problems), problems)
        result = import_probe_rows(self.svc.store, self.account, overclaiming)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["refused"], len(overclaiming))
        self.assertEqual(stored_probe_rows(self.svc.store), before,
                         "all-or-nothing: nothing from that run was stored")

    def test_a_real_row_that_claims_support_without_a_source_is_refused(self) -> None:
        """R11/R12: ``supported`` means a source answered this row, and Grace holds that.

        The rows here are the real-mode rows this Linux host produces (typed refusals, no
        source contacted). One of them flipped to ``supported`` is exactly the claim the
        ledger must never store, and the refusal names the flag that contradicts it.
        """
        real = hermes_rows(base_url=closed_port_base_url())
        overclaiming = [{**row, "supported": True}
                        if row["capability"] == "hermes_session_continuity" else dict(row)
                        for row in real]
        problems = probe_row_problems(row_for(overclaiming, "hermes_session_continuity"))
        self.assertTrue(any("real_source_connected=false" in p for p in problems), problems)
        before = stored_probe_rows(self.svc.store)
        result = import_probe_rows(self.svc.store, self.account, overclaiming)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["refused"], len(overclaiming))
        self.assertEqual(stored_probe_rows(self.svc.store), before)
        # The unmutated rows are storable -- so the refusal above is about the claim, not
        # about real-mode rows being rejected wholesale.
        self.assertEqual([p for row in real for p in probe_row_problems(row)], [])


# ---------------------------------------------------------------------------------
# scope guard
# ---------------------------------------------------------------------------------


class TestThisFileIsHalfTwoOnly(unittest.TestCase):
    """Scope guard: this file covers the probe rows and does not re-derive half one's rig."""

    def test_this_file_builds_on_half_ones_rig_instead_of_re_deriving_it(self) -> None:
        """R14/T12 (coverage traceability): the stand-in and the free-port helper come from
        half one, so a change to the transport rig cannot leave two copies to keep in step."""
        from tests import test_mini_hermes_transport as half_one
        self.assertIs(StubHermesGateway, half_one.StubHermesGateway)
        self.assertIs(closed_port_base_url, half_one.closed_port_base_url)
        module = sys.modules[__name__]
        # A *re-derived* rig class is one this module defines itself. The imported
        # ``StubHermesGateway`` is the opposite of a duplicate, and a test class whose name
        # merely contains "Gateway" (``TestARealRunWithNoGatewayKeyLeavesTheBoxAlone``) is a
        # test, not a rig -- flagging either would make this guard fire on a correct file.
        duplicated = [name for name in dir(module)
                      if isinstance(getattr(module, name), type)
                      and not issubclass(getattr(module, name), unittest.TestCase)
                      and getattr(getattr(module, name), "__module__", None) == __name__
                      and any(word in name for word in ("Gateway", "Stub", "Transport"))]
        self.assertEqual(duplicated, [], f"this file re-derives half one's rig: {duplicated}")

    def test_every_test_names_the_requirement_it_covers(self) -> None:
        """R14/T12 (coverage traceability): a claim in a report must be traceable to a test
        that made it, so every test docstring names an R-number, an acceptance test or the
        labelling rule."""
        module = sys.modules[__name__]
        missing = []
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase) \
                    and obj is not unittest.TestCase:
                for attribute in vars(obj):
                    if attribute.startswith("test_"):
                        text = getattr(obj, attribute).__doc__ or ""
                        if not any(token in text
                                   for token in ("R0", "R1", "T0", "T1", "Labelling")):
                            missing.append(f"{obj.__name__}.{attribute}")
        self.assertEqual(missing, [], f"tests without a named requirement: {missing}")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
