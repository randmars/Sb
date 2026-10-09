"""Regression tests for the audit's remaining honesty-narrowing findings (A-E) and four guards.

This file covers, one class per finding, and each test's docstring names the audit finding it
covers:

* **A** — ``hermes_execution_modes`` could be ``supported: true`` on half its own assertion:
  ``computer_action_mechanism`` was the constant ``None`` while the row's assertion claimed the
  computer/desktop half was measured. The row now *searches* the names the build returned
  (advertised feature keys, toolset names, tool names) and records ``observed_absent``.
* **B** — ``hermes_approval_modes`` asserted three behaviours from one owner observation. The
  assertion is narrowed to the raises-a-prompt vs denies-without-one half, and the covered and
  uncovered sub-behaviours are recorded on the row.
* **C** — the worker printed "this worker has no <source> adapter in this slice", which stopped
  being true as each adapter landed. The fallback sentence now names the *run*, and every
  documentation row's limitation string names what is true at this revision.
* **D1** — ``unsubstituted_placeholders`` was defined, exported and never called.
* **D2** — 409/429/403 were mapped to named states on operations no record describes them for.
* **D3** — ``run_events(max_events=...)`` never reached the transport, so the cap was inert.
* **D4** — the 8-character floor in the token echo check let a 1-7 character key skip it
  silently, and ``stand_in`` was derived from an ``adapter_is_real`` flag whose meaning is the
  opposite of what it tested.
* **E** — ``_measured`` hardcoded ``permission_state=granted`` even for fixture and stand-in
  rows, where no permission exists to grant.

**Nothing in this file is an observation of any Hermes install**: there is no gateway on this
Linux computer. Stand-ins are stubs injected through the ``opener`` seam (which makes them
stand-ins by construction), fixtures are the recorded scenarios under
``mini/switchboard_mini/fixtures/hermes/``, and the two tests that need a source-contacted
outcome synthesise one and say so in their own docstring.
"""

from __future__ import annotations

import unittest
from unittest import mock

from switchboard_mini import hermes_probe, outcomes as O, probe as probe_module
from switchboard_mini.hermes_adapter import HermesReadOnlyAdapter
from switchboard_mini.hermes_transport import (DEFAULT_MAX_EVENTS,
                                               HermesTransport,
                                               MIN_TOKEN_LEN_FOR_ECHO_CHECK,
                                               TYPED_STATES, STATUSES_DESCRIBED_ELSEWHERE,
                                               HttpHermesTransport, build_transport,
                                               body_carries_token, load_fixture,
                                               unsubstituted_placeholders)
from switchboard_mini import hermes_transport as transport_module
from switchboard_mini.probe import ProbeContext

#: A synthetic stand-in bearer value. Not a credential and never read from the owner: it only
#: satisfies the transport's token gate so the wire layer can be driven.
STANDIN_TOKEN = "synthetic-stand-in-value-0123456789"


class _StubResponse:
    """A response-shaped object from a stub gateway. Never a real HTTP response."""

    def __init__(self, *, status: int = 200, body: bytes = b"{}"):
        self.status = status
        self.code = status
        self.headers = {}
        self._body = body

    def read(self) -> bytes:
        return self._body


class _StubOpener:
    """A stand-in responder for the ``opener`` seam. Not a Hermes gateway."""

    def __init__(self, response: _StubResponse | None = None, *, by_path: dict | None = None):
        self.requests: list = []
        self._response = response if response is not None else _StubResponse()
        self._by_path = dict(by_path or {})

    def __call__(self, request, timeout):
        self.requests.append(request)
        for suffix, response in self._by_path.items():
            if request.full_url.endswith(suffix):
                return response
        return self._response


def _status_transport(status: int):
    opener = _StubOpener(_StubResponse(status=status))
    return HttpHermesTransport(token=STANDIN_TOKEN, opener=opener), opener


def _canned_outcome(document, *, source_contacted: bool = True, adapter_is_real: bool = True):
    """An outcome as only a real gateway could produce it -- synthesised, and said so.

    ``source_contacted`` cannot be earned by any stub on this host (the ``opener`` seam makes a
    stand-in a stand-in by construction), so the two tests that need to reach a
    source-contacted branch build the outcome here instead of pretending to measure one.
    """
    outcome = O.Outcome.ok({"document": document, "http_status": 200, "stand_in": False})
    outcome.adapter_is_real = adapter_is_real
    outcome.source_contacted = source_contacted
    return outcome


class _CannedTransport(transport_module.HermesTransport):
    """A transport with no wire at all: it hands back prepared outcomes for the two reads the
    execution-mode and approval rows make, and raises if asked for anything else. No request is
    ever made and nothing is contacted."""

    adapter_is_real = True
    origin = O.REAL
    base_url = "http://127.0.0.1:8642"
    stand_in = False
    profile = None
    token_env = None

    def __init__(self, *, toolsets=None, capabilities=None, source_contacted: bool = True):
        self._documents = {"toolsets": toolsets or {}, "capabilities": capabilities or {}}
        self._source_contacted = source_contacted

    def token_status(self):
        return _canned_outcome({"token_present": True}, source_contacted=False)

    def call(self, operation, **kw):
        if operation not in self._documents:
            raise AssertionError(f"this stand-in answers no {operation!r} read")
        return _canned_outcome(self._documents[operation],
                               source_contacted=self._source_contacted)


def _canned_adapter(**kw) -> HermesReadOnlyAdapter:
    return HermesReadOnlyAdapter(_CannedTransport(**kw))


def _capability(name: str):
    return next(c for c in probe_module.CAPABILITIES if c.name == name)


def _drive(adapter, capability_name: str, hermes: dict | None = None):
    context = ProbeContext([adapter], hermes=hermes or {})
    capability = _capability(capability_name)
    return probe_module._handler_for(context, capability)(context, capability)


TOOLSETS_DOC = {"toolsets": [{"name": "web", "enabled": True,
                              "tools": ["web_search", "web_extract"]}]}


class TestAuditFindingATheComputerActionHalfIsSearched(unittest.TestCase):
    """Finding A: a ``supported: true`` row may not assert an evaluation its evidence lacks."""

    def test_the_mechanism_is_derived_from_the_names_the_build_returned(self) -> None:
        """Audit finding A: with the owner's ``terminal.backend`` supplied and a surface that
        names no computer/desktop mechanism, the row records ``observed_absent: true`` from a
        search over the names it actually saw -- the old row asserted the half from a constant."""
        adapter = _canned_adapter(toolsets=TOOLSETS_DOC,
                                  capabilities={"features": {"runs": True, "run_status": True}})
        row = _drive(adapter, "hermes_execution_modes", {"terminal_backend": "local"})
        observed = row["evidence"]["computer_action_observed"]
        self.assertIs(observed["observed_absent"], True)
        self.assertEqual(observed["matches"], [])
        self.assertEqual(observed["feature_keys_searched"], ["run_status", "runs"])
        self.assertEqual(observed["names_searched"], 5)
        self.assertEqual(observed["terms_searched_for"], list(hermes_probe.COMPUTER_ACTION_TERMS))
        self.assertIsNone(row["evidence"]["computer_action_mechanism"])
        self.assertTrue(row["supported"], "both halves of the narrowed assertion were put to the "
                                          "observed surface")

    def test_a_matching_name_is_recorded_as_the_name_not_as_reachability(self) -> None:
        """Audit finding A: when a name does match a computer-action term the row records the
        name it saw; it never upgrades a tool name into "an approved desktop action is
        reachable"."""
        adapter = _canned_adapter(toolsets={"toolsets": [{"name": "computer_use",
                                                            "tools": ["screenshot"]}]},
                                  capabilities={"features": {"runs": True}})
        row = _drive(adapter, "hermes_execution_modes", {"terminal_backend": "local"})
        observed = row["evidence"]["computer_action_observed"]
        self.assertEqual(observed["matches"], ["computer_use", "screenshot"])
        self.assertIs(observed["observed_absent"], False)
        self.assertEqual(row["evidence"]["computer_action_mechanism"], "computer_use")
        text = (row["limitation"] or "") + " " + row["evidence"]["computer_action_note"]
        self.assertNotIn("is reachable", text)

    def test_an_empty_surface_measures_nothing_and_says_so(self) -> None:
        """Audit finding A: an empty document is not evidence of absence, so the row stays
        unsupported with the search recorded as having had no names."""
        adapter = _canned_adapter(toolsets={"toolsets": []}, capabilities={})
        row = _drive(adapter, "hermes_execution_modes", {"terminal_backend": "local"})
        observed = row["evidence"]["computer_action_observed"]
        self.assertEqual(observed["names_searched"], 0)
        self.assertIsNone(observed["observed_absent"])
        self.assertFalse(row["supported"])
        self.assertIn("did not hold", row["limitation"] or "")
        # and the search itself says it had nothing to look at
        self.assertEqual(observed["feature_keys_searched"], [])

    def test_the_row_assertion_no_longer_claims_reachability(self) -> None:
        """Audit finding A, the row text: the assertion is narrowed to the search, so it matches
        the evidence the handler produces."""
        assertion = _capability("hermes_execution_modes").probe_assertion
        self.assertIn("searched for a computer/desktop/GUI mechanism", assertion)
        self.assertIn("observed_absent", assertion)
        self.assertIn("is not asserted by this row", assertion)
        self.assertNotIn("whether an approved computer/desktop action is reachable", assertion)


class TestAuditFindingBOneObservationSettlesOneHalf(unittest.TestCase):
    """Finding B: the approval row claimed three behaviours from one observation."""

    def test_the_covered_and_uncovered_sub_behaviours_are_recorded(self) -> None:
        """Audit finding B: the row keeps its support for the half the observation settles and
        names the three sub-behaviours it did not."""
        adapter = _canned_adapter(capabilities={"features": {"run_approval": True}})
        row = _drive(adapter, "hermes_approval_modes",
                     {"approval_observation": "waiting_for_approval"})
        evidence = row["evidence"]
        self.assertTrue(row["supported"])
        self.assertEqual(evidence["approval_sub_behaviour_observed"], "waiting_for_approval")
        self.assertEqual(len(evidence["approval_sub_behaviours_measured"]), 1)
        self.assertEqual(len(evidence["approval_sub_behaviours_unmeasured"]), 3)
        self.assertIn("held for a human decision",
                      evidence["approval_sub_behaviours_measured"][0])
        self.assertIs(evidence["approval_assertion_is_narrowed_to_the_observation"], True)

    def test_instant_deny_does_not_invent_the_surface_it_happened_on(self) -> None:
        """Audit finding B: ``instant_deny`` is a denial without a human decision; the row does
        not claim which surface produced it, because the observation does not say."""
        adapter = _canned_adapter(capabilities={"features": {"run_approval": True}})
        row = _drive(adapter, "hermes_approval_modes",
                     {"approval_observation": "instant_deny"})
        measured = row["evidence"]["approval_sub_behaviours_measured"][0]
        self.assertIn("denied without a human decision", measured)
        self.assertIn("does not say which surface", measured)
        for unmeasured in row["evidence"]["approval_sub_behaviours_unmeasured"]:
            self.assertNotIn("observed", unmeasured.lower().split("this worker")[0])

    def test_the_row_assertion_names_the_observation_it_is_narrowed_to(self) -> None:
        """Audit finding B, the row text: the configured mode, the timed-out prompt and the
        unattended surface are named as unmeasured unless the owner's observation covered them."""
        assertion = _capability("hermes_approval_modes").probe_assertion
        self.assertIn("the one trigger the owner ran", assertion)
        self.assertIn("recorded on the row as unmeasured", assertion)
        self.assertNotIn("which mode is configured, whether a timed-out prompt denies",
                         assertion)


class TestAuditFindingCTheFallbackNamesTheRunNotTheWorker(unittest.TestCase):
    """Finding C: no sentence may say an adapter the worker ships does not exist."""

    def test_the_fallback_sentence_names_the_run(self) -> None:
        """Audit finding C: with no Hermes adapter in this run the documentation row says so
        about the *run*, not about a worker that ships the adapter."""
        # A context with an adapter in it: what this test drives is the documentation row
        # builder, which is the fallback used when a source's adapter is not in the run.
        context = ProbeContext([HermesReadOnlyAdapter(
            build_transport(fixture_mode=True, fixture_scenario="healthy"))])
        capability = _capability("hermes_capability_discovery")
        row = probe_module._probe_documented(context, capability)
        self.assertEqual(row["evidence"]["why_unmeasured"].split(",")[0],
                         "no hermes adapter was in this run")
        self.assertNotIn("in this slice", row["evidence"]["why_unmeasured"])

    def test_no_capability_limitation_claims_a_shipped_adapter_is_absent(self) -> None:
        """Audit finding C, the per-row strings: every limitation that mentions an adapter
        names the run. Hermes, Beeper and Contacts adapters all exist at this revision."""
        stale = ("adapter exists in this worker", "this worker has no Beeper adapter",
                 "this worker has no Contacts adapter")
        for capability in probe_module.CAPABILITIES:
            for phrase in stale:
                self.assertNotIn(phrase, capability.limitation or "",
                                 f"{capability.name} still claims a shipped adapter is absent")
        self.assertIn("was in this run", _capability("hermes_execution_modes").limitation)
        self.assertTrue(any("no Beeper adapter was in this run" in (c.limitation or "")
                            for c in probe_module.CAPABILITIES))


class TestGuardsTheAuditFoundUnwired(unittest.TestCase):
    """Guards D1-D4: each was defined or accepted and then never used."""

    def test_d1_a_leftover_placeholder_is_refused_before_any_request(self) -> None:
        """Audit finding D1: ``unsubstituted_placeholders`` is now called in ``call()``, so a
        template that still holds a parameter is refused instead of going on the wire."""
        opener = _StubOpener()
        transport = HttpHermesTransport(token=STANDIN_TOKEN, opener=opener)
        self.assertEqual(unsubstituted_placeholders("/v1/runs/{run_id}"), ["run_id"])
        # The required-parameter gate is what normally catches this; patched out here so the
        # last-line guard is the only thing that can refuse the request.
        with mock.patch.dict(transport_module.REQUIRED_PATH_PARAMS, {}, clear=True):
            got = transport.call("run", path_params={})
        self.assertEqual(got.reason, "missing_path_parameter")
        self.assertEqual(got.data["missing"], ["run_id"])
        self.assertEqual(got.data["requests_made"], 0)
        self.assertEqual(opener.requests, [], "nothing may be sent with a literal placeholder")

    def test_d2_a_409_on_a_get_is_an_unexpected_status_not_a_key_conflict(self) -> None:
        """Audit finding D2: O17 documents 409 for a run-starting POST that reused an
        Idempotency-Key, so a 409 anywhere else is recorded as an observation."""
        transport, _ = _status_transport(409)
        got = transport.call("run", path_params={"run_id": "r-1"})
        self.assertEqual(got.reason, "unexpected_status")
        self.assertIs(got.data["status_has_no_documented_meaning_for_this_operation"], True)
        self.assertIn("run-starting POST", got.data["status_described_elsewhere"])

    def test_d2_a_429_on_a_get_is_not_a_rate_limit(self) -> None:
        """Audit finding D2: O17 records the 429 for new run-starting requests only."""
        transport, _ = _status_transport(429)
        got = transport.call("run", path_params={"run_id": "r-1"})
        self.assertEqual(got.reason, "unexpected_status")
        self.assertIn("run-starting", got.data["status_described_elsewhere"])

    def test_d2_the_run_starting_states_still_work_where_the_record_describes_them(self) -> None:
        """Audit finding D2, the other direction: narrowing must not remove a named state the
        record does describe for the operation that reaches it."""
        transport, _ = _status_transport(429)
        got = transport.call("run_submit", body={"input": "probe"},
                             headers={"Idempotency-Key": "probe-key-0001"})
        self.assertEqual(got.reason, "rate_limited")
        self.assertEqual(got.code, O.RATE_LIMITED)

        transport, _ = _status_transport(409)
        got = transport.call("run_submit", body={"input": "probe"},
                             headers={"Idempotency-Key": "probe-key-0001"})
        self.assertEqual(got.reason, "idempotency_key_conflict")

    def test_d2_a_409_without_the_documented_header_is_not_a_conflict(self) -> None:
        """Audit finding D2: the record's 409 is about reusing a key, so a POST that carried no
        key cannot be reported as that conflict."""
        transport, _ = _status_transport(409)
        got = transport.call("run_submit", body={"input": "probe"})
        self.assertEqual(got.reason, "unexpected_status")
        self.assertIn("does not match that description",
                      got.data["status_described_elsewhere"])

    def test_d2_a_403_has_no_named_state_at_all(self) -> None:
        """Audit finding D2: no record describes a 403 for any path this worker reads, so the
        ``forbidden_by_hermes`` reason (which asserted "the key is valid") is gone."""
        self.assertNotIn("forbidden_by_hermes", TYPED_STATES)
        self.assertEqual(set(STATUSES_DESCRIBED_ELSEWHERE), {403, 409, 429})
        transport, _ = _status_transport(403)
        got = transport.call("run_submit", body={"input": "probe"},
                             headers={"Idempotency-Key": "probe-key-0001"})
        self.assertEqual(got.reason, "unexpected_status")
        self.assertEqual(got.code, O.PERMANENT_ERROR)
        self.assertIn("404, never 403", got.data["status_described_elsewhere"])

    def test_d2_the_recorded_transport_narrows_the_same_way(self) -> None:
        """Audit finding D2: a recorded scenario cannot make a GET look rate-limited either."""
        transport = build_transport(fixture_mode=True, fixture_scenario="rate_limited")
        got = transport.call("run_submit", body={"input": "probe"},
                             headers={"Idempotency-Key": "probe-key-0001"})
        self.assertEqual(got.reason, "rate_limited")
        self.assertIn("FIXTURE:", got.detail)

    def test_d3_the_event_cap_reaches_the_transport_and_the_adapter(self) -> None:
        """Audit finding D3: ``run_events(max_events=...)`` used to be accepted and dropped, so
        the transport always parsed with its own default. The cap is now applied and recorded."""
        transport = build_transport(fixture_mode=True, fixture_scenario="healthy")
        capped = transport.call("run_events", path_params={"run_id": "fixture-run"},
                                max_events=1)
        self.assertEqual(capped.data["event_limit_applied"], 1)
        self.assertIs(capped.data["event_limit_came_from_caller"], True)
        self.assertLessEqual(len(capped.data["event_names_in_order"]), 1)
        uncapped = transport.call("run_events", path_params={"run_id": "fixture-run"})
        self.assertEqual(uncapped.data["event_limit_applied"], DEFAULT_MAX_EVENTS)
        self.assertIs(uncapped.data["event_limit_came_from_caller"], False)

        adapter = HermesReadOnlyAdapter(
            build_transport(fixture_mode=True, fixture_scenario="healthy"))
        through_adapter = adapter.run_events("fixture-run", max_events=1)
        self.assertEqual(through_adapter.data["event_limit_applied"], 1)

    def test_d4_a_key_below_the_echo_check_floor_is_refused_and_the_floor_is_labelled_ours(
            self) -> None:
        """Audit finding D4: a 1-7 character key used to skip the echo check silently. It is now
        refused at the gate, and the refusal says the floor is this worker's policy."""
        opener = _StubOpener(_StubResponse(body=b'{"leaked": "short-key"}'))
        transport = HttpHermesTransport(token="abc", opener=opener)
        self.assertLess(len("abc"), MIN_TOKEN_LEN_FOR_ECHO_CHECK)
        got = transport.call("health")
        self.assertEqual(got.reason, "token_too_short_for_the_echo_check")
        self.assertEqual(got.code, O.PERMANENT_ERROR)
        self.assertIs(got.data["refusal_is_ours"], True)
        self.assertIs(got.data["floor_is_ours"], True)
        self.assertIsNone(got.data["documented_minimum_key_length"])
        self.assertIn("this worker's own policy", got.detail)
        self.assertEqual(opener.requests, [], "no request may carry a key it cannot check")
        self.assertEqual(transport.host_gate().reason, "token_too_short_for_the_echo_check")
        self.assertIn("token_too_short_for_the_echo_check", TYPED_STATES)

    def test_d4_a_key_at_the_floor_still_runs_and_the_check_still_refuses_an_echo(self) -> None:
        """Audit finding D4, the other direction: the floor refuses too-short keys, it does not
        disable the check for keys that meet it."""
        key = "k" * MIN_TOKEN_LEN_FOR_ECHO_CHECK
        self.assertTrue(body_carries_token("body containing " + key, key))
        self.assertFalse(body_carries_token("body containing abc", "abc"))
        opener = _StubOpener(_StubResponse(body=b'{"status": "ok"}'))
        allowed = HttpHermesTransport(token=key, opener=opener).call("health")
        self.assertEqual(len(opener.requests), 1)
        self.assertNotEqual(allowed.reason, "token_too_short_for_the_echo_check")
        echoing = HttpHermesTransport(
            token=key, opener=_StubOpener(_StubResponse(body=("x" + key).encode())))
        refused = echoing.call("health")
        self.assertEqual(refused.reason, "token_echoed_in_response")

    def test_d4_stand_in_is_read_from_the_outcome_not_from_the_flag_whose_meaning_is_opposite(
            self) -> None:
        """Audit finding D4: ``stand_in = bool(adapter.adapter_is_real)`` tested the opposite of
        its name. A fixture-mode row (``adapter_is_real`` false) must still be told apart from a
        stand-in responder that arrived through the real transport."""
        fixture = HermesReadOnlyAdapter(build_transport(fixture_mode=True,
                                                        fixture_scenario="healthy"))
        fixture_outcome = fixture.capabilities()
        stand_in = HermesReadOnlyAdapter(HttpHermesTransport(token=STANDIN_TOKEN, opener=_StubOpener(
            _StubResponse(body=b'{"object": "hermes.api_server.capabilities"}'))))
        stand_in_outcome = stand_in.capabilities()
        self.assertIs(stand_in_outcome.data["stand_in"], True)
        self.assertIsNot((fixture_outcome.data or {}).get("stand_in"), True)

        capability = _capability("hermes_capability_discovery")
        fixture_row = hermes_probe._not_a_real_measurement(
            ProbeContext([fixture]), capability, fixture_outcome, {})
        stand_in_row = hermes_probe._not_a_real_measurement(
            ProbeContext([stand_in]), capability, stand_in_outcome, {})
        self.assertIn("recorded fixture", fixture_row["limitation"])
        self.assertIs(fixture_row["evidence"]["responder_was_a_stand_in"], False)
        self.assertIn("not a real Hermes gateway", stand_in_row["limitation"])
        self.assertIs(stand_in_row["evidence"]["responder_was_a_stand_in"], True)
        self.assertFalse(fixture_row["supported"])
        self.assertFalse(stand_in_row["supported"])


class TestAuditFindingEANonRealRowHasNoPermissionToReport(unittest.TestCase):
    """Finding E: ``granted`` was hardcoded for every measured row."""

    def test_a_fixture_row_reports_not_applicable_not_granted(self) -> None:
        """Audit finding E: a recorded fixture asked no one for permission, so the row reports
        the closed vocabulary's ``not_applicable`` and records why."""
        adapter = HermesReadOnlyAdapter(build_transport(fixture_mode=True,
                                                        fixture_scenario="healthy"))
        outcome = adapter.capabilities()
        row = hermes_probe._not_a_real_measurement(
            ProbeContext([adapter]), _capability("hermes_capability_discovery"), outcome, {})
        self.assertEqual(row["permission_state"], O.PERMISSION_NOT_APPLICABLE)
        self.assertIn("no permission was asked for", row["evidence"]["permission_state_reason"])

    def test_a_stand_in_row_reports_not_applicable_too(self) -> None:
        """Audit finding E: a stand-in responder is not the gateway, so it cannot grant a
        permission either."""
        adapter = HermesReadOnlyAdapter(HttpHermesTransport(
            token=STANDIN_TOKEN, opener=_StubOpener(_StubResponse(body=b"{}"))))
        row = hermes_probe._not_a_real_measurement(
            ProbeContext([adapter]), _capability("hermes_capability_discovery"),
            adapter.capabilities(), {})
        self.assertEqual(row["permission_state"], O.PERMISSION_NOT_APPLICABLE)

    def test_a_source_contacted_row_still_earns_granted(self) -> None:
        """Audit finding E, the other direction: the fix narrows a claim, it does not remove the
        one a real gateway read earns. The outcome is synthesised -- only a real gateway could
        produce it -- and says so."""
        adapter = HermesReadOnlyAdapter(HttpHermesTransport(token=STANDIN_TOKEN,
                                                            opener=_StubOpener()))
        state, reason = hermes_probe._permission_state_for(
            adapter, _canned_outcome({"object": "hermes.api_server.capabilities"}))
        self.assertEqual(state, O.PERMISSION_GRANTED)
        self.assertIn("accepted the bearer key", reason)

    def test_a_denied_outcome_is_still_denied(self) -> None:
        """Audit finding E: the outcome's own refusal keeps its state."""
        adapter = HermesReadOnlyAdapter(build_transport(fixture_mode=True,
                                                        fixture_scenario="token_rejected"))
        outcome = adapter.capabilities()
        state, _ = hermes_probe._permission_state_for(adapter, outcome)
        self.assertEqual(state, O.PERMISSION_STATE_DENIED)

    def test_only_the_measured_rows_are_affected(self) -> None:
        """Audit finding E, scope: ``_measured`` is the changed row builder, and the documented
        fallback still reports ``not_determined`` (nothing was contacted at all)."""
        row = probe_module._probe_documented(
            ProbeContext([HermesReadOnlyAdapter(
                build_transport(fixture_mode=True, fixture_scenario="healthy"))]),
            _capability("hermes_capability_discovery"))
        self.assertEqual(row["permission_state"], O.PERMISSION_NOT_DETERMINED)


class TestTheFixturesUsedHereAreTheRecordedOnes(unittest.TestCase):
    """A guard on the guard: the scenarios driven above exist and are labelled as fixtures."""

    def test_the_driven_scenarios_carry_a_fixture_label(self) -> None:
        for scenario in ("healthy", "rate_limited", "token_rejected"):
            fixture = load_fixture(scenario)
            self.assertEqual(fixture["scenario"], scenario)
            self.assertIn("FIXTURE", fixture["disclaimer"] + fixture["label"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
