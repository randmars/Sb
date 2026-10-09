"""The read-only Hermes adapter: discovery, runs, sessions, health — and one boundary.

What this adapter can do is exactly what the Gate 2 probe pack documents for the Hermes
API server (O17) plus what the sessions and tools pages say about identity and execution
(O18, O19). Everything else is a typed refusal.

**The boundary that matters most: there is no general run submission here.** O17 states
plainly that "The API server gives full access to hermes-agent's toolset, including
terminal commands." A general ``hermes run --input TEXT`` would therefore be an
unrestricted remote-execution surface with no approval contract behind it, which PRD §10
(the draft-only launch default) and PRD §11 forbid. So:

* :meth:`HermesReadOnlyAdapter.submit_run` refuses every input except
  :data:`PROBE_TEST_RUN_INPUT`, the frozen trivial constant this slice ships, and it
  refuses even that unless the caller passes ``consent=True`` (the CLI's
  ``--submit-test-run``). The constant is quoted verbatim in the row's evidence so a
  reader can see exactly what was sent.
* :meth:`run_status`, :meth:`run_events`, :meth:`stop` and :meth:`approval` act only on a
  run id the owner named.
* :meth:`approval` refuses without an owner-supplied body: O17 says the body "carries the
  approval decision" and names no field, so a guessed shape would be a fabricated request.
  It is also never wired to Switchboard's own send approval — O20 is explicit that
  Hermes's approval is a runtime safeguard for shell commands, file writes and MCP trust
  gates, and that it "does not substitute for the product's recipient, data-disclosure or
  send approval" (PRD §11).

Read-only by construction: no session create/patch/delete/fork/chat, no response delete,
no Chat Completions submission, no jobs surface. Each is refused with a reason and the
smallest next action (see ``hermes_transport.REFUSED_ASKS``).

Three identifier concepts are kept apart and are never conflated (O17, O18): the
transcript ``session_id``, the memory-scope header ``X-Hermes-Session-Key``, and the
gateway routing key ``agent:main:<platform>:...``. This adapter reads a session id; it
never derives one, and it never uses a routing key as a conversation identity.
"""

from __future__ import annotations

import hashlib
import time
from typing import Optional

from . import outcomes as O
from .hermes_transport import (DEFAULT_MAX_EVENTS, DEFAULT_SETTLE_INTERVAL,
                               DEFAULT_SETTLE_SECONDS, DOCUMENTED_EVENT_NAMES,
                               RUN_TERMINAL_STATUSES, HermesTransport, build_transport,
                               refusal_outcome, token_env_name)
from .version import WORKER_VERSION

NAMESPACE = "hermes"
MANIFEST_VERSION = "1.0"

#: The capability rows this adapter can measure on a host. Each name is a row in
#: ``documented_capabilities.py``; this adapter is what turns such a row from a
#: documentation read into a measurement (or into an observed refusal).
MEASURED_CAPABILITIES = (
    "hermes_capability_discovery",
    "hermes_run_submission_and_status",
    "hermes_run_progress_events",
    "hermes_run_stop",
    "hermes_session_continuity",
    "hermes_execution_modes",
    "hermes_approval_modes",
    "hermes_credential_resolution",
)

#: The ONLY run input this worker will ever submit. Frozen, trivial, and quoted verbatim
#: in the row's evidence: it asks for one word and no tool use, so it cannot reach the
#: toolset O17 describes. Anything else is refused outright -- see the module docstring.
PROBE_TEST_RUN_INPUT = "Reply with the single word: ready. Do not use any tools."
PROBE_TEST_RUN_INPUT_NOTE = (
    "frozen constant of this worker: the only run this adapter will ever create. It asks "
    "for one word and forbids tool use, so it cannot reach the terminal/file/browser "
    "toolset that O17 says the API server exposes in full.")

#: The body shape O17 does *not* give for ``POST /v1/runs/{run_id}/approval``. It says only
#: that "The body carries the approval decision". No field is named on any record in the
#: pack, so this adapter will not invent one.
APPROVAL_BODY_SHAPE_REASON = "approval_body_shape_not_documented"
APPROVAL_REFUSAL = (
    "POST /v1/runs/{run_id}/approval is refused: no record in the pack names a field of the "
    "approval body. O17 says only that 'The body carries the approval decision' and O20 "
    "names the decision values once/deny for the MCP trust gate, but neither describes the "
    "request document, so this worker will not guess one. This endpoint is also the "
    "runtime safeguard for a shell/file/MCP gate and is never Switchboard's send approval "
    "(O20: it 'does not substitute for the product's recipient, data-disclosure or send "
    "approval').")

#: O17's own documented values for the decision, recorded as documentation only -- not used
#: to build a body.
DOCUMENTED_APPROVAL_DECISIONS = ("once", "deny")

#: What the documented capability surface exposes about its own build: `platform` and
#: `model`, and no build version. So `observed_version` stays `not_observed` with this
#: reason unless the owner supplies one (then it is recorded as owner-supplied).
NO_BUILD_VERSION_REASON = (
    "the documented capability surface exposes platform and model and no build version "
    "(O17 lists no version field), and `hermes --version` is a local command this adapter "
    "cannot run, so no version was observed. Pass --hermes-version to record the owner's "
    "value as owner-supplied.")

#: Our own stop-settling poll bound. Split out so a test can drive it without waiting.
SETTLE_SECONDS = DEFAULT_SETTLE_SECONDS
SETTLE_INTERVAL = DEFAULT_SETTLE_INTERVAL


def probe_idempotency_key(input_text: str) -> str:
    """A deterministic ``Idempotency-Key`` for the probe's one frozen submission.

    Ours, not a secret, and — the point — *identical for an identical retry*, which is what
    O17's replay behaviour requires ("An identical retry returns the original run_id with
    HTTP 202 and Idempotency-Replayed: true"). Derived from the payload so a re-run of the
    probe cannot accidentally use a fresh key for the same work.
    """
    digest = hashlib.sha256(input_text.encode("utf-8")).hexdigest()[:32]
    return f"switchboard-mini-hermes-probe-{digest}"


def documented_body_keys(document) -> list:
    """The top-level key names of a response document, sorted. Never its values."""
    if isinstance(document, dict):
        return sorted(document)
    return []


class HermesReadOnlyAdapter:
    """A ``SourceAdapter`` in the Grace sense, restricted to the documented Hermes reads."""

    name = NAMESPACE
    version = WORKER_VERSION
    host_role = "mini"
    simulated = False

    def __init__(self, transport: HermesTransport, *, settle_seconds: float = SETTLE_SECONDS,
                 settle_interval: float = SETTLE_INTERVAL, sleep=time.sleep):
        self.transport = transport
        self.settle_seconds = float(settle_seconds)
        self.settle_interval = float(settle_interval)
        self._sleep = sleep

    # -- provenance --------------------------------------------------------
    @property
    def origin(self) -> str:
        return self.transport.origin

    @property
    def adapter_is_real(self) -> bool:
        return bool(getattr(self.transport, "adapter_is_real", False))

    @property
    def base_url(self) -> str:
        return getattr(self.transport, "base_url", "")

    @property
    def profile(self) -> Optional[str]:
        return getattr(self.transport, "profile", None)

    def _label(self) -> Optional[str]:
        return getattr(self.transport, "label", None)

    def _stamp(self, outcome: O.Outcome, *, from_read: Optional[O.Outcome] = None) -> O.Outcome:
        outcome.adapter = self.name
        outcome.origin = self.origin
        outcome.adapter_is_real = self.adapter_is_real
        if from_read is not None:
            outcome.adapter_is_real = bool(from_read.adapter_is_real
                                           or self.adapter_is_real)
            outcome.source_contacted = bool(from_read.source_contacted)
        if not outcome.is_real:
            outcome.label = self._label() or O.fixture_label(self.name)
        return outcome

    def probe_handlers(self) -> dict:
        """The probe rows this adapter measures, keyed by capability name."""
        from .hermes_probe import HERMES_PROBES          # local import avoids a cycle
        return dict(HERMES_PROBES)

    @staticmethod
    def measured_capabilities() -> tuple:
        return MEASURED_CAPABILITIES

    # -- local checks ------------------------------------------------------
    def token_status(self) -> O.Outcome:
        return self._stamp(self.transport.token_status())

    def token_env_var(self) -> str:
        return token_env_name(getattr(self.transport, "token_env", None))

    def host_gate(self) -> Optional[O.Outcome]:
        """The token precondition, or None when this adapter may make a request."""
        outcome = getattr(self.transport, "host_gate", lambda: None)()
        return None if outcome is None else self._stamp(outcome)

    # -- documented reads --------------------------------------------------
    def _read(self, operation: str, **kw) -> O.Outcome:
        return self._stamp(self.transport.call(operation, **kw))

    def capabilities(self) -> O.Outcome:
        """``GET /v1/capabilities`` (O17): the build's own description of its surface.

        O17's own instruction is to call this rather than assume the documented feature
        list: "Use this endpoint when integrating dashboards, browser UIs, or control
        planes so they can discover whether the running Hermes version supports runs,
        streaming, cancellation, and session continuity without depending on private
        Python internals."
        """
        return self._read("capabilities")

    def toolsets(self) -> O.Outcome:
        """``GET /v1/toolsets`` (O17): read-only, bearer-gated tool discovery."""
        return self._read("toolsets")

    def skills(self) -> O.Outcome:
        """``GET /v1/skills`` (O17): read-only, bearer-gated skill discovery."""
        return self._read("skills")

    def health(self, *, detailed: bool = False) -> O.Outcome:
        """``GET /health`` and ``GET /health/detailed`` (O17).

        The detailed read is "an authenticated readiness check ... The response exposes
        status and counts, not config values, credentials, paths, commands, queue
        payloads, or raw errors", and "A degraded result still returns HTTP 200" -- so the
        status *value* is the measurement, not the code.
        """
        return self._read("health_detailed" if detailed else "health")

    def health_v1(self) -> O.Outcome:
        return self._read("health_v1")

    def sessions(self, **params) -> O.Outcome:
        """``GET /api/sessions`` (O17). Documented params only; anything else is refused."""
        return self._read("sessions", params=params or None)

    def session(self, session_id: str) -> O.Outcome:
        """``GET /api/sessions/{id}`` (O17). The id is owner- or run-supplied, never derived."""
        got = self._read("session", path_params={"id": session_id})
        if isinstance(got.data, dict):
            got.data.setdefault("session_id_fingerprint", O.fingerprint(str(session_id)))
        return got

    def session_messages(self, session_id: str, **params) -> O.Outcome:
        """``GET /api/sessions/{id}/messages`` (O17): documented params only."""
        got = self._read("session_messages", path_params={"id": session_id},
                         params=params or None)
        if isinstance(got.data, dict):
            got.data.setdefault("session_id_fingerprint", O.fingerprint(str(session_id)))
        return got

    def run_status(self, run_id: str) -> O.Outcome:
        """``GET /v1/runs/{run_id}`` (O17): the reconciliation path for a run."""
        got = self._read("run", path_params={"run_id": run_id})
        if isinstance(got.data, dict):
            got.data.setdefault("run_id_fingerprint", O.fingerprint(str(run_id)))
        return got

    def run_events(self, run_id: str, *, max_events: int = DEFAULT_MAX_EVENTS) -> O.Outcome:
        """``GET /v1/runs/{run_id}/events`` (O17) — **event names and counts only**.

        O17 says the previews are "passed through forced secret redaction and then
        truncated to 500 characters" on the gateway's side. That is a promise about another
        process's behaviour, so this reader records the event *names* and how many of each
        arrived, the order, and whether anything outside O17's documented vocabulary
        appeared — and stores no ``data:`` payload text at all.
        """
        return self._read("run_events", path_params={"run_id": run_id})

    # -- the one submission ------------------------------------------------
    def submit_run(self, input_text: str, *, idempotency_key: str,
                   consent: bool = False, session_id: Optional[str] = None) -> O.Outcome:
        """``POST /v1/runs`` (O17) — the probe's frozen test run, and nothing else.

        Two gates, both deliberate: the input must be exactly
        :data:`PROBE_TEST_RUN_INPUT`, and the caller must pass ``consent=True`` (the CLI's
        ``--submit-test-run``). A general submission surface is exactly what O17's own
        security note makes dangerous ("gives full access to hermes-agent's toolset,
        including terminal commands"), and PRD §10's draft-only launch default forbids an
        unapproved outbound or executing action.
        """
        if input_text != PROBE_TEST_RUN_INPUT:
            from .hermes_transport import TYPED_STATES
            return self._stamp(O.Outcome.unsupported(
                "refused: this worker creates exactly one kind of run — its own frozen "
                "probe constant — and the input passed was not it. O17 records that the "
                "API server 'gives full access to hermes-agent's toolset, including "
                "terminal commands', so a general submission surface here would be "
                "unapproved remote execution, which PRD §10's draft-only launch default and "
                "§11 forbid.",
                reason="general_run_submission_refused",
                data={"input_text_recorded": False,
                      "input_text_fingerprint": O.fingerprint(input_text or ""),
                      "input_length": len(input_text or ""),
                      "frozen_probe_constant": PROBE_TEST_RUN_INPUT,
                      "requests_made": 0, "no_request_made": True,
                      "general_submission_available": False,
                      "smallest_next_action": TYPED_STATES[
                          "general_run_submission_refused"]["next_action"]}))
        if not consent:
            return self._stamp(O.Outcome.unsupported(
                "refused: creating a Hermes run submits work to a gateway that O17 says "
                "'gives full access to hermes-agent's toolset, including terminal "
                "commands'. This worker creates a run only for its own frozen probe "
                "constant, and only with the explicit --submit-test-run flag.",
                reason="run_submission_not_consented",
                data={"input_text": PROBE_TEST_RUN_INPUT,
                      "input_is_the_frozen_probe_constant": True,
                      "requests_made": 0, "no_request_made": True,
                      "general_submission_available": False},
                next_action=("re-run with --submit-test-run if you consent to one trivial "
                             "run on your gateway; otherwise probe the reads only")))
        body = {"input": input_text}
        if session_id:
            # O17: POST /v1/runs "accepts a simple input string and optional session_id,
            # instructions, conversation_history, or previous_response_id".
            body["session_id"] = session_id
        got = self._read("run_submit", body=body,
                         headers={"Idempotency-Key": idempotency_key})
        if isinstance(got.data, dict):
            got.data.setdefault("idempotency_key_used", idempotency_key)
            got.data.setdefault("idempotency_key_is_ours", True)
            got.data.setdefault("idempotency_key_value_is_a_secret", False)
            got.data.setdefault("input_text", input_text)
            got.data.setdefault("input_text_is_a_frozen_constant", True)
            got.data.setdefault("general_submission_available", False)
        return got

    def stop(self, run_id: str, *, settle_seconds: Optional[float] = None,
             interval: Optional[float] = None) -> O.Outcome:
        """``POST /v1/runs/{run_id}/stop`` (O17), then poll to a terminal status.

        O17: "The endpoint returns immediately with ``{"status": "stopping"}`` ... The run
        stays tracked as ``stopping`` until the executor-backed work exits, then settles as
        ``cancelled``; requesting stop never hides a worker that is still running." So a
        stop is a **request**: this method records the immediate answer, polls
        ``GET /v1/runs/{run_id}``, and reports whether a terminal status was observed and
        how long it took. A stop that has not settled is ``partial`` with
        ``stopping_unsettled`` — never reported as a completed stop.
        """
        asked = self._read("run_stop", path_params={"run_id": run_id})
        data = dict(asked.data or {})
        data.setdefault("run_id_fingerprint", O.fingerprint(str(run_id)))
        data.setdefault("stop_is_a_request_not_a_state_change",
                        "O17: the endpoint returns immediately with status stopping")
        if not asked.usable:
            asked.data = data
            return asked
        immediate = (data.get("document") or {}).get("status") if isinstance(
            data.get("document"), dict) else None
        budget = self.settle_seconds if settle_seconds is None else float(settle_seconds)
        step = self.settle_interval if interval is None else float(interval)
        polls = max(1, int(budget / step)) if step > 0 else 1
        started = time.monotonic()
        sequence: list = []
        terminal = None
        last = None
        for _ in range(polls):
            last = self.run_status(run_id)
            if not last.usable:
                data.update({"status_sequence": sequence,
                             "polls": len(sequence),
                             "poll_failed_with": last.code,
                             "poll_failed_reason": last.reason,
                             "elapsed_ms": int((time.monotonic() - started) * 1000)})
                asked.data = data
                return self._stamp(O.Outcome.partial(
                    data,
                    "the stop was accepted but polling the run failed with "
                    f"{last.code}/{last.reason}, so no terminal status was observed",
                    reason="stopping_unsettled", duration_ms=asked.duration_ms,
                    next_action=("re-poll GET /v1/runs/{run_id} once the gateway is "
                                 "reachable; the stop itself was accepted")))
            document = (last.data or {}).get("document")
            status = document.get("status") if isinstance(document, dict) else None
            if status and (not sequence or sequence[-1] != status):
                sequence.append(status)
            if status in RUN_TERMINAL_STATUSES:
                terminal = status
                break
            if step > 0:
                self._sleep(step)
        elapsed = int((time.monotonic() - started) * 1000)
        data.update({"stop_response_status": immediate,
                     "status_sequence": sequence,
                     "polls": len(sequence),
                     "terminal_status": terminal,
                     "settled": terminal is not None,
                     "elapsed_ms": elapsed,
                     "terminal_statuses_documented": list(RUN_TERMINAL_STATUSES),
                     "settle_budget_s": budget,
                     "settle_budget_is_ours": True,
                     "note": ("O17 states no timeout and no forced-kill path for a stop that "
                              "never settles; this bound and its poll interval are this "
                              "worker's own")})
        if terminal is not None:
            asked.data = data
            return self._stamp(O.Outcome.ok(data, duration_ms=asked.duration_ms))
        asked.data = data
        return self._stamp(O.Outcome.partial(
            data,
            "the run had not settled within this worker's own "
            f"{budget}s poll bound, so no terminal status was observed. O17 states no "
            "timeout for a stop, so this is unmeasured rather than a failure",
            reason="stopping_unsettled", duration_ms=asked.duration_ms,
            next_action=("O17: 'requesting stop never hides a worker that is still "
                         "running' - poll GET /v1/runs/{run_id} again, or read the events "
                         "stream, rather than assuming the stop completed")))

    def approval(self, run_id: str, *, body: Optional[dict] = None) -> O.Outcome:
        """``POST /v1/runs/{run_id}/approval`` (O17) — refused without an owner body.

        O17 names the endpoint and says "The body carries the approval decision"; O20 names
        the decision values for the MCP trust gate (``once`` / ``deny`` in prose) but no
        record in the pack describes the request document. So this refuses rather than
        inventing a shape, and it is never connected to Switchboard's own send approval.
        """
        if not body:
            return self._stamp(O.Outcome.unsupported(
                APPROVAL_REFUSAL, reason=APPROVAL_BODY_SHAPE_REASON,
                data={"endpoint": "/v1/runs/{run_id}/approval",
                      "endpoint_ref": "O17",
                      "run_id_fingerprint": O.fingerprint(str(run_id)),
                      "documented_decisions_in_prose": list(DOCUMENTED_APPROVAL_DECISIONS),
                      "requests_made": 0, "no_request_made": True,
                      "is_the_send_approval": False,
                      "note": ("the decision values once/deny are named in O20's prose for "
                               "the MCP trust gate; no field of the request body is named "
                               "anywhere in the pack, so nothing is sent")},
                next_action=("if the owner wants a pending approval resolved, he resolves "
                             "it himself in the surface that raised it (O20: the prompt, "
                             "or /v1/runs/{id}/approval by hand) and records the outcome; "
                             "this worker will not construct the body")))
        return self._stamp(self.transport.call(
            "run_approval", path_params={"run_id": run_id}, body=body))

    # -- refusals for asks beyond the documented reads ---------------------
    def refused(self, ask: str) -> O.Outcome:
        """A typed refusal for a named ask: a name on O17 with no described purpose, the
        wrong control surface, or anything the pack does not name at all."""
        return self._stamp(refusal_outcome(ask))

    def jobs(self) -> O.Outcome:
        return self.refused("/api/jobs")

    def models(self) -> O.Outcome:
        return self.refused("/v1/models")

    def model_options(self) -> O.Outcome:
        return self.refused("/api/model/options")

    def responses(self, response_id: Optional[str] = None) -> O.Outcome:
        return self.refused("/v1/responses/{id}")

    def chat_completions(self) -> O.Outcome:
        return self.refused("/v1/chat/completions")

    def session_mutation(self, mutation: str = "/api/sessions") -> O.Outcome:
        return self.refused(mutation)

    # -- what a caller may ask about this adapter --------------------------
    def document_summary(self, got: O.Outcome) -> dict:
        """What may be recorded from a document: its top-level key names, never values."""
        document = (got.data or {}).get("document") if isinstance(got.data, dict) else None
        return {"document_kind": type(document).__name__,
                "document_keys": documented_body_keys(document),
                "values_withheld": True}

    def manifest(self, rows: Optional[list] = None) -> dict:
        """A capability manifest for this adapter. Nothing is marked supported pre-probe."""
        caps = {name: {"supported": False, "measured": False} for name in MEASURED_CAPABILITIES}
        if rows:
            for row in rows:
                name = row.get("capability")
                if name in caps:
                    caps[name] = {"supported": bool(row.get("supported")),
                                  "measured": True, "state": row.get("state")}
        return {"manifest_version": MANIFEST_VERSION, "adapter": self.name,
                "adapter_version": self.version, "capabilities": caps,
                "general_run_submission_available": False,
                "run_submission_requires": "the frozen probe constant and "
                                           "--submit-test-run"}


def build_adapter(*, fixture_mode: bool = False, fixture_scenario: str = "healthy",
                  base_url: Optional[str] = None, timeout_s: int = 10,
                  stand_in: bool = False, token_env: Optional[str] = None,
                  profile: Optional[str] = None,
                  settle_seconds: float = SETTLE_SECONDS,
                  settle_interval: float = SETTLE_INTERVAL, sleep=time.sleep,
                  opener=None) -> HermesReadOnlyAdapter:
    """The one place that decides recorded versus real for the Hermes adapter."""
    transport = build_transport(fixture_mode=fixture_mode,
                                fixture_scenario=fixture_scenario, base_url=base_url,
                                timeout_s=timeout_s, stand_in=stand_in,
                                token_env=token_env, profile=profile, opener=opener)
    return HermesReadOnlyAdapter(transport, settle_seconds=settle_seconds,
                                 settle_interval=settle_interval, sleep=sleep)


__all__ = ["HermesReadOnlyAdapter", "build_adapter", "MEASURED_CAPABILITIES",
           "PROBE_TEST_RUN_INPUT", "PROBE_TEST_RUN_INPUT_NOTE", "probe_idempotency_key",
           "APPROVAL_BODY_SHAPE_REASON", "NO_BUILD_VERSION_REASON",
           "DOCUMENTED_APPROVAL_DECISIONS", "NAMESPACE", "MANIFEST_VERSION"]
