"""Regression tests for the independent audit's confirmed findings in the Hermes slice.

Five defects, four of them confirmed by the lead driving the shipped code (the fifth is the
one-line row-level companion of the first):

1. **A stub injected through the ``opener`` seam minted rows claiming a real source.**
   ``stand_in`` was ``bool(stand_in or standin_requested())``, so a caller that stubbed the
   socket with an ``opener`` and did not also pass ``stand_in=True`` produced documents with
   ``source_contacted: true`` → ``real_source_connected: true`` / ``origin: real`` / a
   ``supported: true`` row Grace would import as a real Mac measurement. Stand-in honesty now
   rests on construction (``opener is not None``).
2. **A refusal whose "smallest next action" could not unblock it.** ``hermes probe
   --fixture-mode --submit-test-run`` still refused the events and stop rows for a missing
   run id and told the reader to pass ``--submit-test-run`` — the flag they had just passed.
   The run the probe creates is now memoised in the run's shared context and read by the
   run-scoped rows, and where a row genuinely still needs an owner id its advice names a step
   that works.
3. **An aborted POST was reported as a failed read.** A body this worker could not read, or a
   timeout, on the one POST it makes returned ``retryable_error`` / ``body_unreadable`` /
   ``request_timeout`` — while the gateway may have created the run. Both paths now branch on
   the operation's HTTP method and reuse the ``outcome_unknown`` / ``submission_not_confirmed``
   shape: the same key, the same payload, never an assumption of failure.
4. **Two terminal event names were unreachable.** Terminal detection read only
   ``DOCUMENTED_EVENT_NAMES``, which omits ``run.failed`` and ``run.cancelled``, so a stream
   ending in one of those reported "no terminal event in window". One constant is now the
   truth for terminal names, while which names are documented is recorded separately and an
   undocumented name is never presented as a documented one.
5. **The row rule depended on the transport keeping its own flag honest.** ``real_source_connected``
   was re-derived in ``probe._row`` from ``adapter_is_real`` + ``values_from_source``; it is now
   read from the outcome's own ``source_contacted`` where a caller has one.

**Nothing in this file is an observation of any Hermes install.** There is no Hermes gateway on
this Linux computer. Every stand-in here is a stub injected through ``opener`` (`_StandInOpener`),
which is a stand-in *by construction* — that is exactly finding 1 — and the two rows that need a
source-contacted outcome to reach their branch say so in their own docstring instead of pretending
they were measured.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from switchboard_mini import cli, outcomes as O, probe as probe_module
from switchboard_mini.hermes_adapter import (PROBE_TEST_RUN_INPUT, HermesReadOnlyAdapter,
                                             probe_idempotency_key)
from switchboard_mini.hermes_probe import probe_run_progress_events
from switchboard_mini.hermes_transport import (DOCUMENTED_EVENT_NAMES,
                                               DOCUMENTED_TERMINAL_EVENT_NAMES,
                                               SCRUBBED_PLACEHOLDER, TERMINAL_EVENT_NAMES,
                                               HttpHermesTransport)
from switchboard_mini.probe import ProbeContext

#: A synthetic stand-in bearer value. Not a credential, not a secret, and never read from the
#: owner: it exists so the transport's token gate passes and its echo check has something to
#: look for. Longer than 8 characters so ``body_carries_token`` is meaningful.
STANDIN_TOKEN = "synthetic-stand-in-value-0123456789"

CAPABILITIES_BODY = {
    "object": "hermes.api_server.capabilities",
    "platform": "stand-in",
    "model": "stand-in-model",
    "auth": {"type": "bearer", "required": True},
    "features": {"runs": True, "run_approval": True},
}


class _StubResponse:
    """A response-shaped object from a stub gateway. Never a real HTTP response."""

    def __init__(self, *, status: int = 200, body: bytes = b"{}",
                 headers: dict | None = None, read_error: Exception | None = None):
        self.status = status
        self.code = status
        self.headers = dict(headers or {})
        self._body = body
        self._read_error = read_error

    def read(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body


class _StandInOpener:
    """A stand-in gateway for the ``opener`` seam: answers with a canned response, records.

    Not a Hermes gateway. It exists only so the transport's real wire layer can be driven on
    a host that has no install; because it arrives through ``opener`` the transport it drives
    is a stand-in by construction (audit finding 1).
    """

    def __init__(self, response: _StubResponse | None = None,
                 *, error: Exception | None = None):
        self.requests: list = []
        self._response = response if response is not None else _StubResponse()
        self._error = error

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._response


def _stub_transport(opener) -> HttpHermesTransport:
    """The real transport, driven against a stub responder, with no ``stand_in=True``.

    Deliberately without ``stand_in``: that is the caller-reachable defect finding 1 fixes.
    """
    return HttpHermesTransport(token=STANDIN_TOKEN, opener=opener)


def _stub_adapter(opener) -> HermesReadOnlyAdapter:
    return HermesReadOnlyAdapter(_stub_transport(opener))


def _capability(name: str):
    return next(c for c in probe_module.CAPABILITIES if c.name == name)


def _header(request, name: str):
    for key, value in (request.headers or {}).items():
        if key.lower() == name.lower():
            return value
    return None


class TestAuditFinding1AStubCannotMintARealSourceRow(unittest.TestCase):
    """Finding 1: stand-in honesty rests on construction, not on a flag a caller can forget."""

    def test_a_stub_opener_alone_makes_the_transport_a_stand_in(self) -> None:
        """Audit finding 1: an injected ``opener`` is "not the gateway", with no flag passed."""
        transport = _stub_transport(_StandInOpener(_StubResponse(
            body=json.dumps(CAPABILITIES_BODY).encode())))
        self.assertTrue(transport.stand_in,
                        "an injected opener must make the transport a stand-in by "
                        "construction, without the caller passing stand_in=True")

    def test_a_usable_answer_from_a_stub_opener_never_claims_a_real_source(self) -> None:
        """Audit finding 1: a usable body from a stub opener stays a non-real, unsupported row.

        Before the fix this minted ``source_contacted: true``, which the probe turns into
        ``real_source_connected: true`` and a ``supported: true`` row -- a row Grace would
        import as a measurement of Randy's Mac.
        """
        opener = _StandInOpener(_StubResponse(body=json.dumps(CAPABILITIES_BODY).encode()))
        adapter = _stub_adapter(opener)
        got = adapter.capabilities()
        self.assertTrue(got.usable, "the stub answered with a usable body")
        self.assertFalse(got.source_contacted)
        self.assertFalse(got.real_source_connected)
        self.assertEqual((got.data or {}).get("responder"), "stand_in_http_server")
        self.assertIs((got.data or {}).get("stand_in"), True)
        self.assertEqual(len(opener.requests), 1, "the request was written to the stub")

        run = probe_module.run_probe([adapter], only_source="hermes", hermes={})
        self.assertEqual(run.harness_errors, [])
        rows = {row["capability"]: row for row in run.rows}
        discovery = rows["hermes_capability_discovery"]
        self.assertFalse(discovery["supported"])
        self.assertFalse(discovery["real_source_connected"])
        self.assertIs(discovery["evidence"]["stand_in"], True)
        self.assertEqual(discovery["evidence"]["responder"], "stand_in_http_server")
        self.assertIn("not a real Hermes gateway", discovery["limitation"])
        self.assertEqual([row["capability"] for row in run.rows if row["supported"]], [],
                         "a stand-in run may contain no supported row")

    def test_the_opener_seam_is_where_the_token_echo_check_is_exercised(self) -> None:
        """Audit finding 1, companion: fixture mode stores no key, so the positive echo check
        is only reachable through a stand-in that answers with the value in the body."""
        body = json.dumps({**CAPABILITIES_BODY, "echoed": STANDIN_TOKEN}).encode()
        adapter = _stub_adapter(_StandInOpener(_StubResponse(body=body)))
        got = adapter.capabilities()
        self.assertEqual(got.code, O.PERMANENT_ERROR)
        self.assertEqual(got.reason, "token_echoed_in_response")
        self.assertFalse(got.source_contacted)
        self.assertNotIn(STANDIN_TOKEN, json.dumps(got.to_dict()),
                         "the body that carried the key must not be recorded anywhere")


class TestAuditFinding2TheRunTheProbeCreatedIsTheRunTheRowsRead(unittest.TestCase):
    """Finding 2: a refusal's smallest next action must be a step that works."""

    @staticmethod
    def _probe_rows(argv: list) -> tuple:
        """Run `hermes probe ...` in-process and return (exit code, {capability: row})."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["hermes", "probe"] + argv)
        rows = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
        return code, {row["capability"]: row for row in rows}

    def test_the_run_scoped_rows_read_the_run_the_probe_created(self) -> None:
        """Audit finding 2: `hermes probe --fixture-mode --submit-test-run` (the lead's own
        command) must not refuse the events and stop rows for a run id it just created."""
        code, rows = self._probe_rows(["--fixture-mode", "--submit-test-run"])
        self.assertEqual(code, 0)
        for name in ("hermes_run_progress_events", "hermes_run_stop"):
            row = rows[name]
            with self.subTest(capability=name):
                self.assertNotEqual(row["state"], O.UNSUPPORTED)
                self.assertNotEqual(row["evidence"].get("reason"), "run_id_required")
                self.assertIs(row["evidence"]["run_id_supplied"], True)
                self.assertIn("this probe's own frozen test run",
                              row["evidence"]["run_id_supplied_by"])
                self.assertEqual(row["evidence"]["run_id_fingerprint"],
                                 O.fingerprint("recorded-run-1"))
        submission = rows["hermes_run_submission_and_status"]
        self.assertIs(submission["evidence"]["created_run_id_recorded_for_the_run_scoped_rows"],
                      True)

    def test_an_owner_named_run_id_still_wins_over_the_probe_s_own(self) -> None:
        """Audit finding 2: memoising the probe's own run must not shadow ``--run-id``."""
        code, rows = self._probe_rows(["--fixture-mode", "--submit-test-run",
                                       "--run-id", "owner-run-9"])
        self.assertEqual(code, 0)
        events = rows["hermes_run_progress_events"]
        self.assertEqual(events["evidence"]["run_id_supplied_by"], "the owner (--run-id)")
        self.assertEqual(events["evidence"]["run_id_fingerprint"], O.fingerprint("owner-run-9"))

    def test_a_refusal_never_advises_a_flag_the_caller_already_supplied(self) -> None:
        """Audit finding 2, the refusals that remain: with ``--submit-test-run`` and no run id
        coming back, the advice names the submission row and ``--run-id``, not the flag that
        was already passed. Without the consent, naming it is still a working step."""
        with mock.patch.dict(os.environ, {"API_SERVER_KEY": ""}):
            code, rows = self._probe_rows(["--submit-test-run"])
        self.assertEqual(code, 0)
        for name in ("hermes_run_progress_events", "hermes_run_stop"):
            row = rows[name]
            with self.subTest(capability=name):
                self.assertEqual(row["evidence"]["reason"], "run_id_required")
                advice = row["evidence"]["next_action"]
                self.assertNotIn("--submit-test-run", advice,
                                 "the advice names the flag the caller already passed")
                self.assertIn("hermes_run_submission_and_status", advice)
                self.assertIn("--run-id", advice)

        code, rows = self._probe_rows(["--fixture-mode"])
        self.assertEqual(code, 0)
        advice = rows["hermes_run_stop"]["evidence"]["next_action"]
        self.assertIn("--submit-test-run", advice,
                      "without the consent, offering it is still advice that works")


class TestAuditFinding3AnAbortedPostIsNotAFailedRead(unittest.TestCase):
    """Finding 3: for the one POST this worker makes, the honest state is the uncertain one."""

    KEY = probe_idempotency_key(PROBE_TEST_RUN_INPUT)

    def _submit(self, opener):
        adapter = _stub_adapter(opener)
        got = adapter.submit_run(PROBE_TEST_RUN_INPUT, idempotency_key=self.KEY, consent=True)
        return got

    def _assert_uncertain_and_resendable(self, got, opener) -> None:
        self.assertEqual(got.code, O.OUTCOME_UNKNOWN)
        self.assertEqual(got.reason, "submission_not_confirmed")
        self.assertIs(got.submitted, True)
        self.assertNotEqual(got.code, O.RETRYABLE_ERROR)
        canonical = json.dumps({"input": PROBE_TEST_RUN_INPUT}, sort_keys=True)
        self.assertEqual(got.data["request_body_fingerprint"], O.fingerprint(canonical))
        self.assertEqual(got.data["resend_requires"],
                         "the identical payload under the same Idempotency-Key "
                         "(O17: identical retry -> 202 + Idempotency-Replayed)")
        self.assertFalse(got.data["assumption_of_failure"])
        # The key the retry needs is the same key that went on the wire, and the value the
        # reader acts on is the one in ``next_action``: the transport's key-name scrubber
        # (deliberately broad) redacts ``data['idempotency_key_used']`` by name.
        self.assertIn(self.KEY, got.next_action)
        self.assertIn(got.data["idempotency_key_used"],
                      (self.KEY, SCRUBBED_PLACEHOLDER))
        sent = opener.requests[0]
        self.assertEqual(json.loads(sent.data.decode("utf-8")),
                         {"input": PROBE_TEST_RUN_INPUT},
                         "the same payload the outcome asks to re-send")
        self.assertEqual(_header(sent, "Idempotency-Key"), self.KEY,
                         "the same key the outcome asks to reuse")

    def test_a_post_whose_response_body_could_not_be_read_is_outcome_unknown(self) -> None:
        """Audit finding 3: the response arrived, its body could not be read, and the gateway
        may still have created the run -- so this is not a retryable failed read."""
        opener = _StandInOpener(_StubResponse(
            read_error=ConnectionAbortedError("aborted mid-body")))
        self._assert_uncertain_and_resendable(self._submit(opener), opener)

    def test_a_post_timeout_is_outcome_unknown_and_a_get_timeout_is_still_retryable(self) -> None:
        """Audit finding 3, the timeout path: same uncertain shape for the POST (the request
        was written), while a GET timeout stays a retryable read, because nothing was changed."""
        opener = _StandInOpener(error=TimeoutError("timed out"))
        self._assert_uncertain_and_resendable(self._submit(opener), opener)

        get_opener = _StandInOpener(error=TimeoutError("timed out"))
        got = _stub_transport(get_opener).call("capabilities")
        self.assertEqual(got.code, O.RETRYABLE_ERROR)
        self.assertEqual(got.reason, "request_timeout")

    def test_a_post_timeout_arriving_as_a_url_error_is_outcome_unknown_too(self) -> None:
        """Audit finding 3, the third path: urllib wraps a socket timeout in ``URLError``,
        and that path had the same defect."""
        import urllib.error
        opener = _StandInOpener(error=urllib.error.URLError(TimeoutError("timed out")))
        self._assert_uncertain_and_resendable(self._submit(opener), opener)


class _OneReadAdapter:
    """A stand-in adapter that answers one read with an outcome this file hands it.

    It exists *only* to reach the two row branches that need ``source_contacted`` to be true,
    which no host without a Hermes gateway can produce. The outcome is synthesised here, and
    neither test below is a measurement of anything.
    """

    name = "hermes"
    origin = O.REAL
    adapter_is_real = True
    base_url = "http://127.0.0.1:8642"
    profile = None
    label = None

    def __init__(self, outcome):
        self._outcome = outcome
        self.reads: list = []

    def _label(self):
        return None

    def run_events(self, run_id, **kwargs):
        self.reads.append(run_id)
        return self._outcome


class TestAuditFinding4UndocumentedTerminalEventNames(unittest.TestCase):
    """Finding 4: one constant is the truth for terminal detection, and documented is separate."""

    STREAM = (b'event: tool.started\ndata: {"type": "tool.started"}\n\n'
              b'event: run.failed\ndata: {"type": "run.failed"}\n\n')

    def test_the_terminal_vocabulary_is_one_constant_the_transport_owns(self) -> None:
        """Audit finding 4: ``run.failed`` and ``run.cancelled`` are terminal names O17's
        documented event vocabulary does not list, and both facts are recorded, not conflated."""
        self.assertEqual(TERMINAL_EVENT_NAMES,
                         ("run.completed", "run.failed", "run.cancelled", "run.interrupted"))
        self.assertIn("run.failed", TERMINAL_EVENT_NAMES)
        self.assertNotIn("run.failed", DOCUMENTED_EVENT_NAMES)
        self.assertEqual(DOCUMENTED_TERMINAL_EVENT_NAMES,
                         ("run.completed", "run.interrupted"))
        from switchboard_mini import hermes_probe
        self.assertEqual(hermes_probe.TERMINAL_EVENT_NAMES, TERMINAL_EVENT_NAMES,
                         "the probe must not re-type the vocabulary")

    def test_a_stream_ending_in_run_failed_reads_as_terminal_not_as_absent(self) -> None:
        """Audit finding 4, end to end through a stand-in: the row reports the terminal event
        it saw (``run.failed``) instead of "no terminal event in window", and still marks it
        as *not* a documented name."""
        adapter = _stub_adapter(_StandInOpener(_StubResponse(body=self.STREAM)))
        context = ProbeContext([adapter], hermes={"run_id": "stand-in-run-1"})
        row = probe_run_progress_events(context, _capability("hermes_run_progress_events"))
        evidence = row["evidence"]
        self.assertEqual(evidence["terminal_event_names_seen"], ["run.failed"])
        self.assertEqual(evidence["terminal_event_names_documented"], [])
        self.assertEqual(evidence["terminal_event_names_not_documented"], ["run.failed"])
        self.assertEqual(evidence["terminal_event_names_documented_in_o17"],
                         ["run.completed", "run.interrupted"])
        self.assertIs(evidence["terminal_detection_reads_one_constant"], True)
        self.assertIs(evidence["undocumented_terminal_name_is_not_presented_as_documented"],
                      True)
        self.assertFalse(row["supported"])
        self.assertFalse(row["real_source_connected"])
        self.assertNotIn("no terminal event", (row["limitation"] or ""))

    def test_the_undocumented_only_terminal_is_reported_as_the_finding_it_is(self) -> None:
        """Audit finding 4, the row state itself: with documented events AND an undocumented
        terminal name, the row is partial/``terminal_event_name_not_documented`` and says the
        documented terminal name was not the one observed.

        The outcome in this test is *synthesised* with ``source_contacted=True`` purely to
        reach that branch: only a real gateway can produce one, and there is none here.
        """
        outcome = O.Outcome.ok({
            "http_status": 200, "event_stream": True,
            "documented_event_counts": {"tool.started": 1},
            "undocumented_event_counts": {"run.failed": 1},
            "event_names_in_order": ["tool.started", "run.failed"], "event_count": 2,
        })
        outcome.adapter_is_real = True
        outcome.source_contacted = True
        adapter = _OneReadAdapter(outcome)
        context = ProbeContext([adapter], hermes={"run_id": "stand-in-run-2"})
        row = probe_run_progress_events(context, _capability("hermes_run_progress_events"))
        self.assertEqual(adapter.reads, ["stand-in-run-2"])
        self.assertEqual(row["state"], O.PARTIAL)
        self.assertEqual(row["evidence"]["state_reason"],
                         "terminal_event_name_not_documented")
        self.assertFalse(row["supported"])
        self.assertEqual(row["evidence"]["terminal_event_names_seen"], ["run.failed"])
        self.assertEqual(row["evidence"]["terminal_event_names_documented"], [])
        self.assertIn("run.failed", row["limitation"])


class TestAuditFinding5TheRowRuleReadsTheOutcomesOwnFlag(unittest.TestCase):
    """Finding 5: the row-level rule must not depend on the transport's own flag being right."""

    def _context(self) -> ProbeContext:
        return ProbeContext([_stub_adapter(_StandInOpener(_StubResponse()))])

    def test_a_row_whose_outcome_contacted_no_source_cannot_claim_a_real_one(self) -> None:
        """Audit finding 5: ``source_contacted: false`` in, ``real_source_connected: false``
        out, even with ``adapter_is_real`` and ``values_from_source`` both true."""
        row = probe_module._row(
            self._context(), _capability("hermes_run_progress_events"),
            supported=False, state=O.PARTIAL, permission_state=O.PERMISSION_GRANTED,
            limitation="stand-in", evidence={},
            adapter_is_real=True, values_from_source=True, source_contacted=False)
        self.assertIs(row["adapter_is_real"], True)
        self.assertIs(row["values_from_source"], True)
        self.assertIs(row["real_source_connected"], False)

    def test_the_other_direction_still_holds_for_an_outcome_that_did_contact_one(self) -> None:
        """Audit finding 5: a row built from an outcome that says it contacted a real source
        still reports ``real_source_connected: true`` -- the fix narrows a claim, never
        removes one that is earned."""
        row = probe_module._row(
            self._context(), _capability("hermes_run_progress_events"),
            supported=False, state=O.PARTIAL, permission_state=O.PERMISSION_GRANTED,
            limitation="measured", evidence={},
            adapter_is_real=True, values_from_source=True, source_contacted=True)
        self.assertIs(row["real_source_connected"], True)

    def test_the_probe_hands_the_outcomes_own_flag_to_the_row(self) -> None:
        """Audit finding 5, end to end: the Hermes row builder passes the outcome's
        ``source_contacted`` through, so the stand-in row above cannot become a supported one."""
        adapter = _stub_adapter(_StandInOpener(_StubResponse(
            body=json.dumps(CAPABILITIES_BODY).encode())))
        context = ProbeContext([adapter], hermes={})
        row = probe_module._handler_for(
            context, _capability("hermes_capability_discovery"))(context,
            _capability("hermes_capability_discovery"))
        self.assertIs(row["adapter_is_real"], True)
        self.assertIs(row["real_source_connected"], False)
        self.assertIs(row["supported"], False)

    def test_an_unrelated_row_builder_keeps_its_previous_behaviour(self) -> None:
        """Audit finding 5: a caller with no outcome to hand is unaffected -- the row rule is
        unchanged for every source that does not pass the flag."""
        row = probe_module._row(
            self._context(), _capability("hermes_run_progress_events"),
            supported=False, state=O.PARTIAL, permission_state=O.PERMISSION_GRANTED,
            limitation="no flag passed", evidence={}, adapter_is_real=True,
            values_from_source=True)
        self.assertIs(row["real_source_connected"], True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
