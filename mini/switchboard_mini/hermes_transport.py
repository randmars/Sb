"""The Hermes read transport (the gateway's OpenAI-compatible HTTP API). Stdlib only.

Every endpoint, parameter, header, status code, limit and reason string in this module is
copied from a record in the Gate 2 probe pack, and each one says which record it came
from. Nothing is inferred from a name that happens to look plausible.

* ``GET /v1/capabilities``, ``GET /v1/toolsets``, ``GET /v1/skills`` -- **O17**:
  "Returns a machine-readable description of the API server's stable surface for external
  UIs, orchestrators, and plugin bridges."; toolsets/skills are "read-only, bearer-gated".
* ``GET /health`` / ``GET /v1/health`` / ``GET /health/detailed`` -- **O17** (the detailed
  read is "an authenticated readiness check ... exposes status and counts, not config
  values, credentials, paths, commands, queue payloads, or raw errors").
* ``GET /api/sessions``, ``GET /api/sessions/{id}``, ``GET /api/sessions/{id}/messages`` --
  **O17** (the Sessions API table; ``limit``, ``offset``, ``source``, ``include_children``
  on the list list, ``include_compacted``/``inline_images`` on messages).
* ``POST /v1/runs``, ``GET /v1/runs/{run_id}``, ``GET /v1/runs/{run_id}/events``,
  ``POST /v1/runs/{run_id}/stop``, ``POST /v1/runs/{run_id}/approval`` -- **O17**.
* ``Idempotency-Key`` (1-255 visible ASCII; identical retry -> 202 +
  ``Idempotency-Replayed: true``; a different payload under the same key -> 409
  ``idempotency_key_conflict``; retained 24 hours) -- **O17**.
* the default host/port ``127.0.0.1:8642`` and ``API_SERVER_ENABLED=true`` +
  required ``API_SERVER_KEY`` -- **O17**; the bearer header form
  ``Authorization: Bearer <token>`` -- **O17** ("Bearer token auth via the
  ``Authorization`` header ... Configure the key via ``API_SERVER_KEY`` env var.").
* the optional ``/p/<profile>/...`` prefix and per-profile keys -- **O17**
  ("Runs are per-profile scoped: ... another profile's run id returns 404, never 403").

**The token is read from the process environment only** (``API_SERVER_KEY``, overridable
with ``--token-env NAME``). O21 documents that ``~/.hermes/.env`` can hold a
service-account token that "can read every secret the account has access to" and that
``~/.hermes/config.yaml`` is where the gateway key is configured, so this worker never
opens either file: :data:`NEVER_READ_PATHS` is recorded in every token document as a
statement of what was deliberately not read. The value is never logged, echoed,
persisted, put in a URL or written into an error string; only ``token_present`` /
``token_source`` / ``token_value_recorded: false`` are ever reported. After every
response the recorded body is checked **positively** for the token value; if it occurs
there the row is refused (``token_echoed_in_response``) and no body is emitted. Any
recorded string under a key matching :data:`SECRET_KEY_PATTERN` is scrubbed first.
The value is read from the environment or, for a caller that constructs this transport
directly (a test, or the CLI's fixture-free path), handed in as an argument: neither is a
file read, and the status document names which of the two the value came from instead of
claiming the environment for a key that never passed through it.

**No macOS guard.** Beeper's transport refuses to make a request off macOS because the
Desktop API is an app's loopback port reached through an app on the Mac. Hermes
reachability is a socket question and nothing else: a refused connection is ``offline`` /
``hermes_not_reachable``, nothing answered, and no request is claimed to have been served.
A ``host_not_macos``-shaped reason here would assert a fact no record in the pack states.

Typed states (:mod:`switchboard_mini.outcomes`, plus the Hermes-specific reason constants
below):

* no token in the environment -> ``permission_denied`` / ``token_absent``
* a configured key too short to run the echo check on -> ``permanent_error`` /
  ``token_too_short_for_the_echo_check``. The floor is **ours**, and the refusal says so:
  O17 states no minimum key length, but this worker will not run its echo check on a key
  short enough to occur in ordinary prose, because a body echoing such a key would be
  recorded and printed as if it had been checked.
* HTTP 401 -> ``permission_denied`` / ``token_rejected``. A 403 is deliberately **not**
  given a named state: no record in the pack describes a 403 for any path in this worker's
  table (O17 says another profile's run id returns ``404, never 403``), so it is reported as
  ``permanent_error`` / ``unexpected_status`` with the status recorded in the evidence,
  rather than under a name borrowed from an operation no page describes it for
* HTTP 404 on a discovery/health path -> ``unsupported`` /
  ``endpoint_not_in_installed_build``; on a run or session path -> ``unsupported`` /
  ``run_or_session_unknown_here`` (O17: another profile's run id returns 404, never 403)
* HTTP 429 -> ``rate_limited`` / ``rate_limited`` (O17: over the 10-run cap the server
  "rejected with HTTP 429", i.e. it rejects rather than queues -- the ledger owns the wait).
  **Only on a run-starting request**: O17 records the 429 for "new run-starting requests",
  so a 429 on any other operation is ``unexpected_status`` with the status recorded, never a
  rate-limit borrowed from the record of another operation
* a request this worker timed out on -> ``retryable_error`` / ``request_timeout`` (our own
  bound; no page in the pack names a timeout) -- **except for a POST**, where the request
  had already been written and whether the gateway acted is unknown: that is
  ``outcome_unknown`` / ``submission_not_confirmed`` (defect fix, audit finding 3)
* connection refused -> ``offline`` / ``hermes_not_reachable``
* a connection dropped after the request was written -> ``outcome_unknown`` /
  ``submission_not_confirmed`` (the only honest reading: re-send the **identical** payload
  under the **same** ``Idempotency-Key``, never a fresh key, never a guess that it failed)
* a body that is not parsable -> ``permanent_error`` / ``unexpected_document_shape``
* a POST whose response body could not be read at all -> ``outcome_unknown`` /
  ``submission_not_confirmed`` for the same reason as a POST timeout (defect fix, audit
  finding 3): a broken read of one's own response is not evidence that nothing happened
* 409 -> ``permanent_error`` / ``idempotency_key_conflict`` -- **only on a run-starting POST
  that actually carried an ``Idempotency-Key``**, which is the one request O17 documents a 409
  for ("Reusing the same key with a different JSON payload returns HTTP 409"). On any other
  operation a 409 is ``permanent_error`` / ``unexpected_status`` with the status recorded

``stand_in`` exists for one purpose: exercising this transport's wire layer on a host that
is not Randy's Mac (there is no Hermes gateway on this Linux computer). A stand-in run
marks every document ``responder: stand_in_http_server`` and forces
``source_contacted: false``, so a row it produces can never claim
``real_source_connected: true`` and can never import into Grace as a measurement.
"""

from __future__ import annotations

import abc
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from . import outcomes as O

#: Where the owner's bearer key is read from. O17: "Configure the key via ``API_SERVER_KEY``
#: env var." Overridable with ``--token-env NAME`` (a profile may use its own name).
TOKEN_ENV = "API_SERVER_KEY"
#: Optional override for the API base URL. O17's documented default is the default here.
BASE_URL_ENV = "HERMES_API_BASE_URL"
#: Opt-in that points this transport at a local stand-in responder. Never a measurement.
STANDIN_ENV = "SWITCHBOARD_HERMES_STANDIN"

DEFAULT_BASE_URL = "http://127.0.0.1:8642"
DEFAULT_BASE_URL_SOURCE = (
    "O17: 'Enable with API_SERVER_ENABLED = true and required API_SERVER_KEY'; port 8642, "
    "host 127.0.0.1 by default. The pack records the default bind, not a verified address "
    "of Randy's install -- his profile can set API_SERVER_HOST / API_SERVER_PORT, so this "
    "is a default the operator overrides, and every document records which base URL was "
    "used")

#: Paths this worker refuses to open by design. O21 documents that ``~/.hermes/.env`` can
#: hold a service-account token that "can read every secret the account has access to" and
#: that the gateway key is configured in ``~/.hermes/config.yaml``. Recorded in every token
#: document so the refusal is visible, not just absent from the code.
NEVER_READ_PATHS = ("~/.hermes/.env", "~/.hermes/config.yaml")

#: The documented read operations, each with the pack ref that documents it. The ``kind``
#: is what decides a 404's meaning: O17 documents discovery/health paths and the run and
#: session paths separately, and "another profile's run id returns 404, never 403".
ENDPOINTS: dict = {
    "capabilities": {
        "method": "GET", "path": "/v1/capabilities", "ref": "O17", "kind": "discovery",
        "described_as": "a machine-readable description of the API server's stable surface "
                        "for external UIs, orchestrators, and plugin bridges"},
    "toolsets": {
        "method": "GET", "path": "/v1/toolsets", "ref": "O17", "kind": "discovery",
        "described_as": "read-only, bearer-gated discovery of the tool surface; example "
                        "shape {\"name\",\"label\",\"description\",\"enabled\","
                        "\"configured\",\"tools\"}"},
    "skills": {
        "method": "GET", "path": "/v1/skills", "ref": "O17", "kind": "discovery",
        "described_as": "read-only, bearer-gated discovery of the skill surface"},
    "health": {
        "method": "GET", "path": "/health", "ref": "O17", "kind": "health",
        "described_as": "'{\"status\": \"ok\"}'"},
    "health_v1": {
        "method": "GET", "path": "/v1/health", "ref": "O17", "kind": "health",
        "described_as": "the same health document as /health"},
    "health_detailed": {
        "method": "GET", "path": "/health/detailed", "ref": "O17", "kind": "health",
        "described_as": "an authenticated readiness check; 'exposes status and counts, not "
                        "config values, credentials, paths, commands, queue payloads, or "
                        "raw errors'"},
    "sessions": {
        "method": "GET", "path": "/api/sessions", "ref": "O17", "kind": "session",
        "described_as": "session listing, gated by API_SERVER_KEY"},
    "session": {
        "method": "GET", "path": "/api/sessions/{id}", "ref": "O17", "kind": "session",
        "described_as": "one session"},
    "session_messages": {
        "method": "GET", "path": "/api/sessions/{id}/messages", "ref": "O17",
        "kind": "session",
        "described_as": "one session's stored messages"},
    "run": {
        "method": "GET", "path": "/v1/runs/{run_id}", "ref": "O17", "kind": "run",
        "described_as": "object: hermes.run with run_id, status, session_id, model, "
                        "output, usage, runtime"},
    "run_events": {
        "method": "GET", "path": "/v1/runs/{run_id}/events", "ref": "O17", "kind": "run",
        "described_as": "SSE of tool-call progress, token deltas and lifecycle events"},
    "run_submit": {
        "method": "POST", "path": "/v1/runs", "ref": "O17", "kind": "run",
        "described_as": "returns run_id, status 'started'; accepts a simple input string "
                        "and optional session_id, instructions, conversation_history or "
                        "previous_response_id"},
    "run_stop": {
        "method": "POST", "path": "/v1/runs/{run_id}/stop", "ref": "O17", "kind": "run",
        "described_as": "returns immediately with {\"status\": \"stopping\"}"},
    "run_approval": {
        "method": "POST", "path": "/v1/runs/{run_id}/approval", "ref": "O17", "kind": "run",
        "described_as": "resolve a pending approval for a run that is waiting on a human "
                        "decision (a tool call gated behind an approval policy)"},
}

#: Names that appear on O17 but whose purpose that page does not describe, or that are the
#: wrong control surface. Copied from the page, refused here: a name in an endpoint table
#: is not a documented purpose, and a guessed purpose would be a fabricated capability.
REFUSED_ASKS: dict = {
    "/api/jobs": (
        "endpoint_not_in_pack",
        "O17 lists /api/jobs* in its endpoint table and describes no purpose for them, so "
        "this worker will not guess what a jobs surface means for the owner's queue: the "
        "application-owned job ledger is Grace's (PRD R12), not Hermes's."),
    "/v1/models": (
        "endpoint_not_in_pack",
        "O17 names GET /v1/models and describes no purpose for it, so no read is built "
        "against it."),
    "/api/model/options": (
        "endpoint_not_in_pack",
        "O17 names GET /api/model/options and describes no purpose for it, so no read is "
        "built against it."),
    "/v1/responses/{id}": (
        "endpoint_not_in_pack",
        "O17 names GET and DELETE /v1/responses/{id} and describes no purpose for either, "
        "so neither is built: a delete in particular is a mutation this worker will not "
        "perform on a guess."),
    "/v1/chat/completions": (
        "not_the_control_surface",
        "O17 records Chat Completions as 'Stateless - the full conversation is included in "
        "each request via the messages array' and names the Runs API as the streaming-"
        "friendly run-control surface, so this is not a run-control surface and nothing "
        "here submits work through it."),
    "/v1/responses": (
        "not_the_control_surface",
        "O17 names POST /v1/responses without describing it as the control surface and "
        "documents the Runs API for run control; this worker does not submit work here."),
    "/api/sessions": (
        "not_the_control_surface",
        "O17's Sessions API is read here (GET only); creating, patching, deleting, forking "
        "or chatting into a session is a mutation of the owner's Hermes state and this "
        "worker is read-only (PRD §9 forbids writing into Hermes-owned stores)."),
    "/api/sessions/{id}/fork": (
        "not_the_control_surface",
        "O17 documents POST /api/sessions/{id}/fork as branching the session via SessionDB "
        "lineage - a mutation of Hermes state, refused here."),
    "/api/sessions/{id}/chat": (
        "not_the_control_surface",
        "O17 documents POST /api/sessions/{id}/chat as 'one synchronous turn' - a run-like "
        "submission whose control surface is the Runs API (O17 documents the Runs API as "
        "the streaming-friendly alternative), and refused here."),
    "/api/sessions/{id}/chat/stream": (
        "not_the_control_surface",
        "O17 documents POST /api/sessions/{id}/chat/stream as an SSE wrapper over one "
        "synchronous turn - a run-like submission, and its control surface is the Runs API; "
        "refused here."),
}

UNLISTED_ASK_NEXT_ACTION = (
    "read the endpoint's own section on the record that names it, add it to the pack, and "
    "only then build a read for it - this worker will not guess a path, a parameter or a "
    "purpose (Gate 2 probe pack O17; see /home/team/shared/probe-pack)")

#: The path params each documented template needs. A template built without them would put
#: a literal ``{run_id}``/``{id}`` on the wire -- the defect the Beeper slice shipped once
#: (the literal ``{accountID}`` reached the stub). Refused, never sent.
REQUIRED_PATH_PARAMS: dict = {
    "session": ("id",), "session_messages": ("id",),
    "run": ("run_id",), "run_events": ("run_id",), "run_stop": ("run_id",),
    "run_approval": ("run_id",),
}

#: Documented query parameters, by operation. Anything else is refused
#: (``parameter_not_documented``): O17 names these and only these.
DOCUMENTED_PARAMS: dict = {
    "sessions": ("limit", "offset", "source", "include_children"),
    "session_messages": ("include_compacted", "inline_images"),
}

#: Documented limits and vocabulary.
IDEMPOTENCY_KEY_MAX = 255          # O17: "1-255 visible ASCII characters"
IDEMPOTENCY_KEY_MIN = 1
MAX_CONCURRENT_RUNS_DEFAULT = 10   # O17: gateway.api_server.max_concurrent_runs default 10
RUN_STATUSES = ("started", "running", "stopping", "waiting_for_approval",
                "completed", "failed", "cancelled", "interrupted")  # O17
RUN_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")  # O17
#: O17's progress/event vocabulary, as written on the page.
DOCUMENTED_EVENT_NAMES = (
    "tool.started", "tool.completed", "message.interim", "message.delta",
    "run.completed", "run.interrupted", "approval.request", "subagent.start",
    "subagent.complete", "hermes.tool.progress")
#: The terminal event names of a run: O17's own run lifecycle (``completed``, ``failed``,
#: ``cancelled``, ``interrupted``). **This is the one truth for terminal detection** (defect
#: fix, audit finding 4): terminal detection used to read only
#: :data:`DOCUMENTED_EVENT_NAMES`, which names ``run.completed``/``run.interrupted`` and
#: omits ``run.failed``/``run.cancelled``, so a stream ending in one of the two omitted
#: names reported "no terminal event in window" -- understating what the worker actually
#: saw. Detection counts from this tuple; whether a name is *documented* is derived from
#: :data:`DOCUMENTED_EVENT_NAMES` and recorded separately, so an undocumented name is never
#: silently upgraded into a documented one.
TERMINAL_EVENT_NAMES = ("run.completed", "run.failed", "run.cancelled", "run.interrupted")
#: Which of the terminal names O17's documented event vocabulary actually names. Derived,
#: never written out a second time.
DOCUMENTED_TERMINAL_EVENT_NAMES = tuple(name for name in TERMINAL_EVENT_NAMES
                                        if name in DOCUMENTED_EVENT_NAMES)
#: Our own bounds. No page names a timeout or a page cap; these are ours and are recorded
#: as ours everywhere they appear.
DEFAULT_TIMEOUT_S = 10
DEFAULT_MAX_EVENTS = 200
DEFAULT_MAX_EVENT_BYTES = 262144
#: The shortest bearer key this worker will run the echo check against. **This floor is
#: ours**: O17 states no minimum key length, and the audit found that a 1-7 character key
#: silently skipped :func:`body_carries_token`, so an echoed body would have been recorded
#: and printed as if it had been checked. A key below this floor is refused at the gate,
#: with the refusal labelled as this worker's own policy rather than as a documented rule.
MIN_TOKEN_LEN_FOR_ECHO_CHECK = 8
#: The one operation a 409 or a 429 may be reported as its named state on: O17 documents
#: both for run-starting requests (``POST /v1/runs`` and no other path).
RUN_STARTING_OPERATIONS = ("run_submit",)
#: Statuses the pack describes for one operation only, with the record's own scope. Reaching
#: one of these anywhere else is a measurement with no documented meaning: it is reported as
#: ``unexpected_status`` carrying this note, never under a name borrowed from the operation
#: the page describes (honesty narrowing: a named state may not assert an evaluation the
#: record does not cover).
STATUSES_DESCRIBED_ELSEWHERE = {
    403: ("no page in the pack describes a 403 for any path in this worker's table (O17 "
          "says another profile's run id returns 404, never 403)"),
    409: ("O17 records 409 for reusing an Idempotency-Key with a different payload on a "
          "run-starting POST"),
    429: "O17 records 429 for a new run-starting request over the max_concurrent_runs cap",
}
#: Our own stop-settling poll bound (seconds and interval). O17 states no timeout and no
#: forced-kill path, so the wait is bounded by us and reported when it is exceeded.
DEFAULT_SETTLE_SECONDS = 20.0
DEFAULT_SETTLE_INTERVAL = 0.5

#: A recorded string under a key matching this is scrubbed before it is stored anywhere.
SECRET_KEY_PATTERN = re.compile(r"(key|token|secret|password)", re.IGNORECASE)
SCRUBBED_PLACEHOLDER = "[redacted: key name matched /key|token|secret|password/i]"
#: Every named Hermes state this slice can report, with the outcome code it travels as and
#: the smallest next action for each. The row states themselves stay inside the shared
#: vocabulary (``outcomes.ADAPTER_OUTCOMES``); the *named reason* is what makes a row
#: specific, and Grace validates the state, not the reason. A reason that is not in this
#: table would be a state nobody has thought about, so the tests assert the table is the
#: whole vocabulary and that each entry carries a next action a human can run.
TYPED_STATES = {
    "token_absent": {
        "code": O.PERMISSION_DENIED,
        "meaning": "no bearer key in this process's environment, so no request was made",
        "next_action": f"export the gateway key as {TOKEN_ENV} (O17) and re-run"},
    "token_rejected": {
        "code": O.PERMISSION_DENIED,
        "meaning": "HTTP 401: the gateway rejected the bearer key",
        "next_action": "use the key the API server was started with (API_SERVER_KEY, O17)"},
    "token_too_short_for_the_echo_check": {
        "code": O.PERMANENT_ERROR,
        "meaning": ("the configured bearer key is shorter than "
                    f"{MIN_TOKEN_LEN_FOR_ECHO_CHECK} characters, so this worker refuses to "
                    "run its echo check against it: O17 states no minimum key length, and "
                    "this floor is ours"),
        "next_action": ("put a key of at least "
                        f"{MIN_TOKEN_LEN_FOR_ECHO_CHECK} characters into the environment "
                        "variable this worker reads, or record that this install's key is "
                        "shorter and treat the echo check as unavailable"),
    },
    "token_echoed_in_response": {
        "code": O.PERMANENT_ERROR,
        "meaning": ("the response body contained the bearer key value itself, so the body "
                    "was discarded and no row was built from it"),
        "next_action": "treat the gateway as leaking credentials, fix it, rotate the key"},
    "hermes_not_reachable": {
        "code": O.OFFLINE,
        "meaning": "the connection to the API server was refused: nothing is listening",
        "next_action": ("start the gateway with API_SERVER_ENABLED=true (O17) and check "
                        "its host/port")},
    "request_timeout": {
        "code": O.RETRYABLE_ERROR,
        "meaning": "this worker's own timeout elapsed; no record names an API timeout",
        "next_action": "raise --timeout and re-run"},
    "request_failed": {
        "code": O.RETRYABLE_ERROR,
        "meaning": "the request could not be completed",
        "next_action": "retry the read"},
    "transport_error": {
        "code": O.RETRYABLE_ERROR,
        "meaning": "a transport-level failure below HTTP",
        "next_action": "retry the read"},
    "body_unreadable": {
        "code": O.RETRYABLE_ERROR,
        "meaning": "a response arrived whose body could not be read",
        "next_action": "retry the read"},
    "server_error": {
        "code": O.RETRYABLE_ERROR,
        "meaning": "HTTP 5xx from the gateway",
        "next_action": "retry the read"},
    "rate_limited": {
        "code": O.RATE_LIMITED,
        "meaning": ("HTTP 429: too many concurrent runs (O17 documents the rejection; the "
                    "server does not queue)"),
        "next_action": "let the job ledger own the wait and retry later"},
    "submission_not_confirmed": {
        "code": O.OUTCOME_UNKNOWN,
        "meaning": ("the connection dropped after the request was written and before a "
                    "response arrived, so whether the gateway accepted it is unknown"),
        "next_action": ("re-send the identical payload with the same Idempotency-Key; "
                        "never a fresh key and never a guess that it failed")},
    "run_or_session_unknown_here": {
        "code": O.UNSUPPORTED,
        "meaning": ("HTTP 404 on a run or session path: no such id on this profile (O17: "
                    "another profile's run id returns 404, never 403)"),
        "next_action": "use an id from the profile Grace will use"},
    "endpoint_not_in_installed_build": {
        "code": O.UNSUPPORTED,
        "meaning": "HTTP 404 on a documented discovery/health path on this build",
        "next_action": ("record `hermes --version` and this status beside the row; the "
                        "missing endpoint blocks the release claim rather than being "
                        "simulated")},
    "endpoint_not_in_pack": {
        "code": O.UNSUPPORTED,
        "meaning": "the ask is a name on O17 with no described purpose, or unlisted",
        "next_action": ("read the endpoint's own section on the record that names it, add "
                        "it to the pack, and only then build a read for it")},
    "not_the_control_surface": {
        "code": O.UNSUPPORTED,
        "meaning": ("the path exists but is not the run-control surface (or is a mutation "
                    "this read-only worker will not perform)"),
        "next_action": ("use POST /v1/runs for run control and GET /v1/runs/{run_id} for "
                        "run state (O17)")},
    "missing_path_parameter": {
        "code": O.UNSUPPORTED,
        "meaning": ("a documented path template was asked for without its parameter, so a "
                    "literal placeholder would have gone on the wire"),
        "next_action": "pass the id (--run-id <id> or --session-id <id>)"},
    "parameter_not_documented": {
        "code": O.UNSUPPORTED,
        "meaning": "a query parameter the pack does not name for that path",
        "next_action": ("use a documented parameter, or add the parameter to the pack with "
                        "the page that names it")},
    "idempotency_key_not_documented_shape": {
        "code": O.PERMANENT_ERROR,
        "meaning": "an Idempotency-Key outside O17's 1-255 visible ASCII characters",
        "next_action": "send 1-255 visible ASCII characters in Idempotency-Key (O17)"},
    "idempotency_key_conflict": {
        "code": O.PERMANENT_ERROR,
        "meaning": ("HTTP 409: the same Idempotency-Key was reused with a different "
                    "payload (O17)"),
        "next_action": "use a fresh key for new work, the same key only for an identical "
                       "retry"},
    "unexpected_status": {
        "code": O.PERMANENT_ERROR,
        "meaning": "an HTTP status no record in the pack describes for that path",
        "next_action": "record the status and the path; it is a new observation"},
    "unexpected_document_shape": {
        "code": O.PERMANENT_ERROR,
        "meaning": "the body is not the JSON document the pack describes",
        "next_action": "record the status, byte count and key names; the shape is a finding"},
    "empty_body": {
        "code": O.PARTIAL,
        "meaning": "a success status with an empty body",
        "next_action": "re-run the read and record the status beside it"},
    "run_submission_not_consented": {
        "code": O.UNSUPPORTED,
        "meaning": ("the probe's frozen test run was not submitted because the explicit "
                    "--submit-test-run consent was not given"),
        "next_action": ("re-run with --submit-test-run, or probe the reads only")},
    "general_run_submission_refused": {
        "code": O.UNSUPPORTED,
        "meaning": ("an input other than this worker's frozen probe constant was refused: "
                    "the API server exposes the agent's whole toolset including terminal "
                    "commands (O17)"),
        "next_action": ("run a real instruction from a Switchboard job with the owner's "
                        "approval contract (PRD §10), not from this worker")},
    "run_id_required": {
        "code": O.UNSUPPORTED,
        "meaning": ("a run-scoped read was asked for with no run id, and this worker will "
                    "not pick an arbitrary run"),
        "next_action": "pass --run-id <id>, or --submit-test-run to create the probe's own"},
    "stopping_unsettled": {
        "code": O.PARTIAL,
        "meaning": ("the stop was accepted (O17: {\"status\": \"stopping\"}) but no "
                    "terminal status was observed within this worker's own poll bound"),
        "next_action": ("poll GET /v1/runs/{run_id} again rather than assuming the stop "
                        "completed; O17 states no timeout for a stop")},
    "no_terminal_event_in_window": {
        "code": O.PARTIAL,
        "meaning": ("documented progress events arrived but the stream carried no terminal "
                    "event within this worker's window"),
        "next_action": ("re-read the stream, or poll GET /v1/runs/{run_id} for the settled "
                        "status")},
    "event_names_not_documented": {
        "code": O.PARTIAL,
        "meaning": "events arrived whose names are not in O17's documented vocabulary",
        "next_action": ("record the names: the vocabulary is a measurement on this build, "
                        "not a reading")},
    "terminal_event_name_not_documented": {
        "code": O.PARTIAL,
        "meaning": ("the stream ended in a terminal run event whose name O17's documented "
                    "event vocabulary does not name (run.failed / run.cancelled are the two "
                    "gap names in it), so the run was observed to end but not under a "
                    "documented terminal name"),
        "next_action": ("record the name beside the row: the vocabulary on this build is a "
                        "measurement, not a reading (O17 names run.completed and "
                        "run.interrupted only)"),
    },
    "not_in_recorded_scenario": {
        "code": O.UNSUPPORTED,
        "meaning": "the recorded fixture has no answer for this operation",
        "next_action": "record that operation on the Mac and add it to the scenario"},
    "recorded_fixture_fault": {
        "code": O.PERMANENT_ERROR,
        "meaning": "the recorded fixture encodes a fault for this operation",
        "next_action": "read the fixture's own note; this is a recorded scenario, not a "
                       "live source"},
    "approval_body_shape_not_documented": {
        "code": O.UNSUPPORTED,
        "meaning": ("no record names a field of the approval request body, so none was "
                    "constructed"),
        "next_action": ("the owner resolves a pending approval himself and records the "
                        "outcome")},
    "session_resume_half_unmeasured": {
        "code": O.PARTIAL,
        "meaning": ("the session reads' shape was measured but O17's attribution assertion "
                    "(a resumed run echoing its session_id, and X-Hermes-Session-Key being "
                    "accepted) was not: it needs a run carrying session history"),
        "next_action": ("submit a run that resumes a session yourself and record whether "
                        "the returned session_id is echoed unchanged")},
    "terminal_backend_half_unmeasured": {
        "code": O.PARTIAL,
        "meaning": ("the tool/toolset names half was measured from GET /v1/toolsets; the "
                    "`terminal.backend` half is a value in the owner's config.yaml"),
        "next_action": "pass --terminal-backend local|docker|ssh|<what yours says>"},
    "approval_observation_missing": {
        "code": O.PARTIAL,
        "meaning": ("the advertised run_approval flag was read, but the human-decision half "
                    "needs a dangerous-class command this worker will not submit"),
        "next_action": ("run `bash -c 'echo probe'` in an interactive session, then re-run "
                        "with --approval-observation "
                        "waiting_for_approval|instant_deny|not_reproducible")},
    "credential_path_not_measurable_by_this_adapter": {
        "code": O.PROBE_UNMEASURED,
        "meaning": ("the credential-resolution capability cannot be measured by an "
                    "authenticated API read: a 200 proves only that API_SERVER_KEY is "
                    "valid, and O21 documents op:// resolution as fail-open"),
        "next_action": ("run the owner-side credential procedure (O21 steps 1-4 / O22) and "
                        "record it, or leave this row unmeasured")},
}


def reason_sentence(reason: str) -> str:
    """The smallest next action for a named state, or a plain fallback if it is new."""
    entry = TYPED_STATES.get(reason)
    if entry is None:
        return ("this reason is not in the worker's typed-state table: treat the row as a "
                "finding and add the state to hermes_transport.TYPED_STATES")
    return entry["next_action"]


def token_env_name(override: Optional[str] = None) -> str:
    """Which environment variable holds the bearer key. Default ``API_SERVER_KEY`` (O17)."""
    return (override or "").strip() or TOKEN_ENV


def token_value(override: Optional[str] = None) -> str:
    """The key value, read from the process environment only. Never from a file."""
    return (os.environ.get(token_env_name(override)) or "").strip()


def token_present(override: Optional[str] = None) -> bool:
    """Is a key configured? The value itself is never returned by this function."""
    return bool(token_value(override))


def token_status_document(override: Optional[str] = None) -> dict:
    """What may be said about the key without saying the key."""
    name = token_env_name(override)
    value = token_value(override)
    return {
        "token_present": bool(value),
        "token_source": f"process environment {name}",
        "token_env_var": name,
        "token_value_recorded": False,
        "token_read_from_file": False,
        "echo_check_min_length": MIN_TOKEN_LEN_FOR_ECHO_CHECK,
        "echo_check_min_length_is_ours": True,
        "token_below_the_echo_check_floor": bool(value) and len(value) < MIN_TOKEN_LEN_FOR_ECHO_CHECK,
        "echo_check_floor_note": (
            "O17 states no minimum key length: the floor above is this worker's own, and a "
            "key below it is refused at the gate rather than used for a check it could not "
            "make (a 1-7 character key would occur in ordinary prose, so an echo would go "
            "undetected and the body would be recorded and printed)"),
        "never_read_paths": list(NEVER_READ_PATHS),
        "note": ("the key is read from the process environment only. O21 documents that "
                 "~/.hermes/.env can hold a 1Password service-account token ('can read "
                 "every secret the account has access to') and that the gateway key lives "
                 "in ~/.hermes/config.yaml, so this worker opens neither file. The value is "
                 "never logged, echoed, persisted or put in a URL."),
    }


def base_url_from_env() -> str:
    return (os.environ.get(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL


def standin_requested() -> bool:
    return bool((os.environ.get(STANDIN_ENV) or "").strip())


def profile_prefix(profile: Optional[str]) -> str:
    """The optional multi-profile routing prefix. O17: ``/p/<profile>/...``."""
    name = (profile or "").strip()
    if not name:
        return ""
    return "/p/" + urllib.parse.quote(name, safe="")


def resolve_path(path: str, path_params: Optional[dict]) -> str:
    """Substitute a documented path template's parameters. Never leaves a placeholder."""
    resolved = path
    for key, value in (path_params or {}).items():
        resolved = resolved.replace("{" + key + "}", urllib.parse.quote(str(value), safe=""))
    return resolved


def unsubstituted_placeholders(path: str) -> list:
    return sorted(set(re.findall(r"\{([^{}]*)\}", path)))


def validate_idempotency_key(key: str) -> Optional[str]:
    """Is this a documented ``Idempotency-Key``? Returns a problem sentence or None.

    O17: "send an Idempotency-Key header (1-255 visible ASCII characters)". Anything else
    (empty, too long, control characters, non-ASCII) is refused before it goes on the wire.
    """
    text = key or ""
    if not (IDEMPOTENCY_KEY_MIN <= len(text) <= IDEMPOTENCY_KEY_MAX):
        return (f"the Idempotency-Key must be {IDEMPOTENCY_KEY_MIN}-"
                f"{IDEMPOTENCY_KEY_MAX} characters (O17); this one is {len(text)}")
    bad = [ch for ch in text if not (0x21 <= ord(ch) <= 0x7E)]
    if bad:
        return ("the Idempotency-Key must be 1-255 visible ASCII characters (O17); it "
                "carries a character outside that range")
    return None


def scrub_secrets(value: Any, *, path: str = "$") -> tuple:
    """Scrub any recorded string under a key matching :data:`SECRET_KEY_PATTERN`.

    Returns ``(scrubbed_value, scrubbed_paths)``. Defensive and deliberately broad: a
    response this worker did not write could carry a secret under any name, and the cost of
    redacting a harmless value is a slightly poorer evidence field, while the cost of
    recording a real one is a leak in a probe transcript.
    """
    scrubbed: list = []
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            here = f"{path}.{key}"
            if isinstance(item, str) and SECRET_KEY_PATTERN.search(str(key)):
                out[key] = SCRUBBED_PLACEHOLDER
                scrubbed.append(here)
                continue
            new_item, deeper = scrub_secrets(item, path=here)
            out[key] = new_item
            scrubbed.extend(deeper)
        return out, scrubbed
    if isinstance(value, list):
        out_list = []
        for index, item in enumerate(value):
            new_item, deeper = scrub_secrets(item, path=f"{path}[{index}]")
            out_list.append(new_item)
            scrubbed.extend(deeper)
        return out_list, scrubbed
    return value, scrubbed


def body_carries_token(text: str, token: str) -> bool:
    """The positive echo check: does the recorded body contain the key value?

    O17 documents that ``X-Hermes-Session-Key`` is "echoed back on responses (JSON + SSE)"
    and that tool-event previews pass "forced secret redaction" -- but a *promise* about
    redaction is not an observation, so every response is checked rather than trusted.

    The length floor is ours (see :data:`MIN_TOKEN_LEN_FOR_ECHO_CHECK`). It is a floor for
    the *check*, not a licence to run with a short key: the gate refuses a key below it, so
    this branch is a defence in depth and never the reason a short key goes unchecked.
    """
    if not token or len(token) < MIN_TOKEN_LEN_FOR_ECHO_CHECK:
        return False
    return token in (text or "")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class _Secret:
    """A bearer key held so that no printable view of its holder can publish the value.

    R14/T12: the key never leaves the process environment, and "never" has to survive the
    accidents as well as the deliberate paths. A plain ``str`` attribute is published by
    every one of these, none of which is a decision anybody makes:

    * ``repr(transport)`` / ``str(transport)`` / ``f"{transport}"``
    * ``print(vars(transport))``, ``print(transport.__dict__)``, an f-string of either
    * a traceback or a debugger rendering of an object whose locals hold the transport
    * ``repr(outcome)``, and anything that walks the outcome's graph

    ``__repr__``, ``__str__`` and ``__format__`` each answer
    :data:`_Secret.PLACEHOLDER`, so every one of the above prints the placeholder. The
    value is reachable only through the explicit ``.value`` attribute, which is the one
    place a reader is meant to look and which never reaches a document, a log line, a URL
    or a refusal sentence.
    """

    __slots__ = ("_value",)

    #: What every printable form of a held key says instead of the key.
    PLACEHOLDER = "[redacted: the bearer key value is never printable]"

    def __init__(self, value: Optional[str] = None) -> None:
        self._value = (value or "").strip()

    @property
    def value(self) -> str:
        """The key itself. Called only for the header, the echo check and the
        emptiness/length gate -- never to build a sentence or a document."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return self.PLACEHOLDER

    def __str__(self) -> str:
        return self.PLACEHOLDER

    def __format__(self, format_spec: str) -> str:
        return self.PLACEHOLDER


def parse_event_names(text: str, *, limit: int = DEFAULT_MAX_EVENTS) -> dict:
    """Event **names and counts** from an SSE body. Never payload text.

    O17 documents the event vocabulary (``tool.started``, ``run.completed``,
    ``approval.request``, ...). This records which of those names arrived, how many times,
    and whether anything unrecognised appeared -- and records no ``data:`` line at all,
    because the previews are only "passed through forced secret redaction and then
    truncated to 500 characters" on the *gateway's* side and this worker will not depend on
    that promise for the text it stores.
    """
    counts: dict = {}
    undocumented: dict = {}
    order: list = []
    data_objects = 0
    disagreements: list = []
    # An SSE block usually carries BOTH an `event:` line and a `data:` object whose
    # `type` repeats it. Counting each line separately reported every progress event
    # twice -- found by driving a recorded stream rather than by reading the code, and
    # fixed here: a block contributes one name, the `event:` line first, and the data
    # object's `type` only when the `event:` line is missing. Where the two disagree the
    # disagreement is recorded, because that is a fact about the stream worth reporting.
    pending_event_line: Optional[str] = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            pending_event_line = line[len("event:"):].strip() or None
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            data_objects += 1
            data_type = None
            try:
                parsed = json.loads(payload)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                for key in ("type", "event", "name"):
                    candidate = parsed.get(key)
                    if isinstance(candidate, str) and candidate:
                        data_type = candidate
                        break
            if pending_event_line and data_type and data_type != pending_event_line:
                disagreements.append({"event_line": pending_event_line,
                                      "data_type": data_type})
            name = pending_event_line or data_type
            pending_event_line = None
        elif line:
            continue
        else:
            pending_event_line = None
            continue
        if not name:
            continue
        if len(order) < limit:
            order.append(name)
        if name in DOCUMENTED_EVENT_NAMES:
            counts[name] = counts.get(name, 0) + 1
        else:
            undocumented[name] = undocumented.get(name, 0) + 1
    return {
        "documented_event_counts": counts,
        "undocumented_event_counts": undocumented,
        "event_names_in_order": order[:limit],
        "event_count": sum(counts.values()) + sum(undocumented.values()),
        "data_lines_seen": data_objects,
        "event_line_and_data_type_disagreements": disagreements,
        "counts_one_per_event_not_one_per_line": True,
        "payload_text_recorded": False,
    }


# --------------------------------------------------------------------- interface --


class HermesTransport(abc.ABC):
    """One method per operation the Hermes adapter performs. All return ``Outcome``."""

    name = "hermes"
    origin = O.REAL
    adapter_is_real = False
    label: Optional[str] = None
    base_url = DEFAULT_BASE_URL
    profile: Optional[str] = None
    stand_in = False
    token_env: Optional[str] = None

    @abc.abstractmethod
    def token_status(self) -> O.Outcome: ...

    @abc.abstractmethod
    def call(self, operation: str, *, params: Optional[dict] = None,
             body: Optional[dict] = None, path_params: Optional[dict] = None,
             headers: Optional[dict] = None, max_bytes: Optional[int] = None,
             max_events: Optional[int] = None) -> O.Outcome: ...


# ------------------------------------------------------------------- the real one --


class HttpHermesTransport(HermesTransport):
    """Read-only talk to the Hermes gateway's HTTP API.

    **Stand-in honesty rests on construction, not on a flag a caller can forget** (defect
    fix, audit finding 1). Three things make a transport a stand-in, and any one of them is
    enough: the caller asked for it (``stand_in=True``), the environment asked for it
    (``SWITCHBOARD_HERMES_STANDIN``), or **an ``opener`` was injected**. The injected opener
    is the supported way to drive this transport against a stub gateway, and a caller who
    injects one has, by construction, replaced the real responder -- so ``opener is not
    None`` *is* "not the gateway". Deriving ``stand_in`` from the explicit flag alone meant
    a caller who stubbed the socket and forgot ``stand_in=True`` minted documents with
    ``source_contacted: true``, which the probe turns into ``origin: real`` /
    ``real_source_connected: true`` / a ``supported: true`` row that Grace would import as a
    real measurement of Randy's Mac. The flag is a courtesy; the construction is the fact.
    """

    origin = O.REAL
    adapter_is_real = True

    def __init__(self, *, base_url: Optional[str] = None, token: Optional[str] = None,
                 token_env: Optional[str] = None, profile: Optional[str] = None,
                 timeout_s: int = DEFAULT_TIMEOUT_S, stand_in: bool = False,
                 opener: Optional[Callable[..., Any]] = None):
        self.token_env = token_env
        self.base_url = (base_url or base_url_from_env()).rstrip("/")
        # Read the key once and hold it wrapped, so no printable view of this object can
        # publish the value (see :class:`_Secret`): repr(), a debug print of __dict__ and
        # a traceback rendering locals all show the placeholder, not the key.
        self._token = _Secret(token if token is not None else token_value(token_env))
        #: Did this value come from the constructor rather than the environment? Recorded so
        #: ``held_token_document`` can say which, without recording the value.
        self._token_from_argument = token is not None
        self.profile = profile
        self.timeout_s = int(timeout_s)
        # See the class docstring: an injected opener is a stand-in by construction.
        self.stand_in = bool(stand_in or standin_requested() or opener is not None)
        self._opener = opener

    def __repr__(self) -> str:
        """Never the key value: ``_token`` prints as :data:`_Secret.PLACEHOLDER`."""
        return (f"<HttpHermesTransport base_url={self.base_url!r} "
                f"profile={self.profile!r} stand_in={self.stand_in} "
                f"token={self._token!r}>")

    # -- preconditions -----------------------------------------------------
    def held_token_document(self) -> dict:
        """The token status document for the key *this transport holds*.

        ``token_status_document`` answers from the process environment, and this transport
        can hold a key that never passed through it (the constructor seam used by tests and
        by the CLI's own construction). Answering from the environment in that case said
        "no bearer key is configured" while the gate was passing and requests were being
        authorised with one -- a status document contradicting the transport's own
        behaviour. Presence and the echo-check floor are reported from the held key; which
        of the two places it came from is named; and the value is never recorded either way.
        """
        document = token_status_document(self.token_env)
        document["token_present"] = bool(self._token)
        document["token_below_the_echo_check_floor"] = (
            bool(self._token) and len(self._token) < MIN_TOKEN_LEN_FOR_ECHO_CHECK)
        if self._token_from_argument:
            document["token_source"] = (
                "an explicit constructor argument (not read from the environment variable "
                + token_env_name(self.token_env) + ")")
        document["token_value_recorded"] = False
        document["token_read_from_file"] = False
        return document

    def _token_gate(self) -> Optional[O.Outcome]:
        if not self._token:
            name = token_env_name(self.token_env)
            return O.Outcome.permission_denied(
                f"no Hermes bearer key is configured: {name} is empty or unset, so no "
                "request was made and nothing was read.",
                reason="token_absent", adapter=self.name,
                data=self.held_token_document(),
                next_action=(f"put the gateway key the owner configured "
                             f"(API_SERVER_ENABLED / API_SERVER_KEY, O17) into this "
                             f"process's environment as {name} and re-run"))
        if len(self._token) < MIN_TOKEN_LEN_FOR_ECHO_CHECK:
            # Our own policy, named as ours (audit finding: the 8-character floor in
            # ``body_carries_token`` let a 1-7 character key skip the echo check silently, so
            # an echoed body would have been recorded and printed as if it had been checked).
            return O.Outcome.permanent(
                f"the configured Hermes bearer key is {len(self._token)} characters long, "
                f"below the {MIN_TOKEN_LEN_FOR_ECHO_CHECK}-character floor this worker needs "
                "to run its response echo check, so no request was made. O17 states no "
                "minimum key length: this floor is this worker's own policy, not a documented "
                "rule, and a key shorter than it would match ordinary prose, so an echoed "
                "body would be recorded and printed undetected.",
                reason="token_too_short_for_the_echo_check", adapter=self.name,
                data={**self.held_token_document(),
                      "token_length_recorded": False,
                      "token_length_class": ("below the worker's echo-check floor"),
                      "refusal_is_ours": True,
                      "floor_is_ours": True,
                      "documented_minimum_key_length": None,
                      "requests_made": 0, "no_request_made": True},
                next_action=(f"use a key of at least {MIN_TOKEN_LEN_FOR_ECHO_CHECK} "
                             "characters, or record that this install's key is shorter and "
                             "treat the echo check as unavailable"))
        return None

    def host_gate(self) -> Optional[O.Outcome]:
        """The precondition that would block *any* request, or None."""
        blocked = self._token_gate()
        return None if blocked is None else self._blocked_outcome(blocked)

    def token_status(self) -> O.Outcome:
        # A local check only: it contacts nothing.
        document = self.held_token_document()
        if not self._token:
            return O.Outcome.permission_denied(
                f"no Hermes bearer key is present: {document['token_env_var']} "
                "is empty or unset. No request was made.",
                reason="token_absent", adapter=self.name, data=document,
                next_action=("export the gateway key (API_SERVER_KEY, O17) into this "
                             "process's environment, then re-run"))
        return O.Outcome.ok(document, adapter=self.name)

    # -- requests ----------------------------------------------------------
    def _blocked_outcome(self, outcome: O.Outcome) -> O.Outcome:
        """Stamp a refusal this transport built before touching the network."""
        outcome.adapter = self.name
        outcome.origin = O.REAL
        outcome.adapter_is_real = True
        outcome.source_contacted = False
        return outcome

    def _request(self, operation: str, params: Optional[dict], body: Optional[dict],
                 resolved_path: str, headers: Optional[dict]):
        meta = ENDPOINTS[operation]
        path = profile_prefix(self.profile) + resolved_path
        url = self.base_url + path
        if params:
            url = url + "?" + urllib.parse.urlencode(params, doseq=True)
        data = None
        if meta["method"] in ("POST", "PATCH", "PUT"):
            payload = dict(body or {})
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=meta["method"])
        # O17: "Bearer token auth via the Authorization header".
        request.add_header("Authorization", "Bearer " + self._token.value)
        request.add_header("Accept", "application/json, text/event-stream")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if self._opener is not None:
            return self._opener(request, self.timeout_s)
        return urllib.request.urlopen(request, timeout=self.timeout_s)

    def call(self, operation: str, *, params: Optional[dict] = None,
             body: Optional[dict] = None, path_params: Optional[dict] = None,
             headers: Optional[dict] = None,
             max_bytes: Optional[int] = None,
             max_events: Optional[int] = None) -> O.Outcome:
        if operation not in ENDPOINTS:
            return self._blocked_outcome(refusal_outcome(operation))
        missing = [name for name in REQUIRED_PATH_PARAMS.get(operation, ())
                   if not (path_params or {}).get(name)]
        if missing:
            return self._blocked_outcome(missing_path_parameter(operation, missing))
        blocked = self._token_gate()
        if blocked is not None:
            return self._blocked_outcome(blocked)
        extra_params = sorted(set(params or {}) - set(DOCUMENTED_PARAMS.get(operation, ())))
        if extra_params:
            return self._blocked_outcome(undocumented_parameter(operation, extra_params))
        idem = (headers or {}).get("Idempotency-Key")
        if idem is not None:
            problem = validate_idempotency_key(idem)
            if problem:
                return self._blocked_outcome(O.Outcome.permanent(
                    problem, reason="idempotency_key_not_documented_shape",
                    next_action="send 1-255 visible ASCII characters in Idempotency-Key "
                                "(O17)"))
        resolved_path = resolve_path(ENDPOINTS[operation]["path"], path_params)
        # The last-line guard, wired in (audit finding: ``unsubstituted_placeholders`` was
        # defined and exported but never called, so a template that still held a parameter
        # after resolution would have gone on the wire -- the literal ``{accountID}`` the
        # Beeper slice once shipped). Refused before the request, never sent.
        leftover = unsubstituted_placeholders(resolved_path)
        if leftover:
            return self._blocked_outcome(missing_path_parameter(operation, leftover))
        started = time.monotonic()
        try:
            response = self._request(operation, params, body, resolved_path, headers)
        except urllib.error.HTTPError as exc:
            outcome = self._from_response(operation, _replay_for(exc), started, max_bytes,
                                          idempotency_key=idem, body=body,
                                          max_events=max_events)
        except urllib.error.URLError as exc:
            outcome = self._from_url_error(operation, exc, started,
                                           idempotency_key=idem, body=body)
        except socket.timeout as exc:
            outcome = self._timeout_outcome(
                operation, started=started,
                detail=f"this worker's own {self.timeout_s}s timeout elapsed waiting for "
                       f"{operation}",
                idempotency_key=idem, body=body)
        except TimeoutError as exc:
            outcome = self._timeout_outcome(
                operation, started=started,
                detail=f"the request to {operation} timed out after {self.timeout_s}s: "
                       f"{type(exc).__name__}",
                idempotency_key=idem, body=body)
        except (OSError, ValueError) as exc:
            outcome = self._stamp(O.Outcome.retryable(
                f"the request to {operation} could not be completed: "
                f"{type(exc).__name__}: {exc}",
                reason="request_failed", duration_ms=_ms(started),
                next_action="retry the read"))
        else:
            outcome = self._from_response(operation, response, started, max_bytes,
                                          idempotency_key=idem, body=body,
                                          max_events=max_events)
        if isinstance(outcome.data, dict):
            outcome.data.setdefault("path_resolved", resolved_path)
            outcome.data.setdefault("path_params_supplied",
                                    sorted(k for k, v in (path_params or {}).items() if v))
            if self.profile:
                outcome.data.setdefault("profile", self.profile)
            data, scrubbed = scrub_secrets(outcome.data)
            data.setdefault("scrubbed_paths", scrubbed)
            outcome.data = data
        return outcome

    # -- responses ---------------------------------------------------------
    def _stamp(self, outcome: O.Outcome, *, extra: Optional[dict] = None) -> O.Outcome:
        outcome.adapter = self.name
        outcome.origin = O.REAL
        outcome.adapter_is_real = True
        # A stand-in responder is not the source; a real response is. A refusal that
        # reached the network but carried no usable value keeps source_contacted False.
        outcome.source_contacted = bool(outcome.usable) and not self.stand_in
        payload = dict(outcome.data or {})
        payload.setdefault("base_url", self.base_url)
        payload.setdefault("responder", "stand_in_http_server" if self.stand_in
                           else "hermes_gateway_api")
        payload.setdefault("stand_in", self.stand_in)
        payload.setdefault("token_present", bool(self._token))
        payload.setdefault("token_source", f"process environment "
                                           f"{token_env_name(self.token_env)}")
        payload.setdefault("token_value_recorded", False)
        if extra:
            payload.update(extra)
        if outcome.data is not None or extra:
            outcome.data = payload
        return outcome

    # -- the uncertain-outcome shape, reused wherever a POST's answer was lost -----------
    def _timeout_outcome(self, operation: str, *, started: float, detail: str,
                         idempotency_key: Optional[str] = None,
                         body: Optional[dict] = None) -> O.Outcome:
        """Our own timeout: a retryable read, unless it was a POST (audit finding 3).

        For a GET, nothing was changed by the request and re-running the read is the whole
        fix. For a POST the request had already been written when the clock ran out, so
        whether the gateway acted is unknown and the row says so.
        """
        if ENDPOINTS[operation]["method"] == "POST":
            return self._post_uncertain(
                operation, started=started,
                detail=(detail + ", and the request had already been written, so it is "
                        "unknown whether the gateway accepted it"),
                idempotency_key=idempotency_key, body=body,
                extra={"timeout_s": self.timeout_s, "timeout_is_ours": True})
        return self._stamp(O.Outcome.retryable(
            detail, reason="request_timeout", duration_ms=_ms(started), data={
                "endpoint": ENDPOINTS[operation]["path"],
                "requests_made": 1,
                "timeout_s": self.timeout_s,
                "timeout_is_ours": True,
                "note": ("no page in the pack names an API-server timeout: this bound "
                         "is this worker's own"),
            },
            next_action="raise --timeout and re-run"))


    def _post_uncertain(self, operation: str, *, started: float, detail: str,
                        idempotency_key: Optional[str] = None,
                        body: Optional[dict] = None,
                        extra: Optional[dict] = None) -> O.Outcome:
        """The honest state for a POST whose answer never arrived (audit finding 3).

        The gateway may have created the run, so a timeout on a POST, or a response this
        worker could not read, is **not** a failed read and **not** a retryable one: the
        row says ``outcome_unknown`` / ``submission_not_confirmed`` and asks for the
        identical payload under the same ``Idempotency-Key`` (O17: an identical retry
        returns the original run_id with 202 + ``Idempotency-Replayed: true``). A fresh key
        would create a second run instead of replaying the first.

        The key value also travels in the ``next_action`` text, not only in
        ``data['idempotency_key_used']``: this transport scrubs any recorded *value* under a
        key name matching :data:`SECRET_KEY_PATTERN` (deliberately broad, and it matches
        "key"), so the field the reader acts on is the one in the sentence -- exactly as the
        already-correct dropped-connection branch does it.
        """
        payload = json.dumps(dict(body or {}), sort_keys=True)
        data = {"requests_made": 1, "duration_ms": _ms(started),
                "endpoint": ENDPOINTS[operation]["path"], "method": "POST",
                "request_body_bytes": len(payload.encode("utf-8")),
                "request_body_fingerprint": O.fingerprint(payload),
                "resend_requires": ("the identical payload under the same Idempotency-Key "
                                    "(O17: identical retry -> 202 + Idempotency-Replayed)"),
                "assumption_of_failure": False}
        data.update(extra or {})
        if idempotency_key:
            # Ours, and not a secret: the module docstring says so, and the retry is only
            # safe because the reader can see which key to reuse.
            data["idempotency_key_used"] = idempotency_key
            data["idempotency_key_is_ours_and_is_not_a_secret"] = True
        return self._stamp(O.Outcome.uncertain(
            detail, reason="submission_not_confirmed", data=data,
            next_action=("re-send the **identical** payload with the **same** "
                         "Idempotency-Key"
                         + (f" ({idempotency_key})" if idempotency_key else "")
                         + ": O17 documents that an identical retry returns the original "
                           "run_id with 202 + Idempotency-Replayed: true. Never a fresh key "
                           "and never a guess that it failed")))

    def _from_response(self, operation: str, response, started: float,
                       max_bytes: Optional[int], *,
                       idempotency_key: Optional[str] = None,
                       body: Optional[dict] = None,
                       max_events: Optional[int] = None) -> O.Outcome:
        status = getattr(response, "status", None) or getattr(response, "code", 0) or 0
        try:
            raw = response.read() if hasattr(response, "read") else b""
        except Exception as exc:                      # a body we could not read at all
            if ENDPOINTS[operation]["method"] == "POST":
                # The response arrived but this worker could not read it. That is not a
                # failed read: the gateway may have created the run, so the honest state is
                # the uncertain one (audit finding 3).
                return self._post_uncertain(
                    operation, started=started,
                    detail=("the response to " + ENDPOINTS[operation]["path"] + " arrived "
                            "but its body could not be read (" + type(exc).__name__ + "), "
                            "so it is unknown whether the gateway accepted the request"),
                    idempotency_key=idempotency_key, body=body,
                    extra={"http_status": status})
            return self._stamp(O.Outcome.retryable(
                f"the response body for {operation} could not be read: "
                f"{type(exc).__name__}", reason="body_unreadable",
                duration_ms=_ms(started), next_action="retry the read"),
                extra={"http_status": status, "requests_made": 1})
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", "replace")
            byte_count = len(raw)
        else:
            text = raw or ""
            byte_count = len(text.encode("utf-8"))
        headers = {k: v for k, v in (getattr(response, "headers", {}) or {}).items()}
        replay_header = headers.get("Idempotency-Replayed")
        base = {"http_status": status, "body_bytes": byte_count,
                "duration_ms": _ms(started),
                "endpoint": ENDPOINTS[operation]["path"],
                "endpoint_ref": ENDPOINTS[operation]["ref"],
                "idempotency_replayed": (replay_header or "").lower() == "true"
                                        if replay_header is not None else None}

        # The positive echo check runs before anything is recorded from the body.
        if body_carries_token(text, self._token.value):
            return self._stamp(O.Outcome.permanent(
                "the gateway answered " + ENDPOINTS[operation]["path"] + " with a body "
                "that contains the bearer key value itself, so the body was discarded and "
                "no row is built from it.",
                reason="token_echoed_in_response", data=base,
                next_action=("treat the running gateway as leaking credentials: check its "
                             "logging and response construction before probing again, and "
                             "rotate the key (O21: a leaked token is 'revoke + regenerate')"
                             )), extra=base)

        document = None
        parse_problem = None
        if text.strip():
            try:
                document = json.loads(text)
            except ValueError as exc:
                parse_problem = str(exc)

        if status == 401:
            return self._stamp(O.Outcome.permission_denied(
                "the Hermes gateway answered 401 for " + ENDPOINTS[operation]["path"]
                + ": the bearer key was rejected. No data was returned.",
                reason="token_rejected", data=base,
                next_action=("use the key the profile's API server was started with "
                             "(API_SERVER_KEY, O17); a named profile with no key of its "
                             "own 'fails closed' (O17)")), extra=base)
        # A 403 gets no named state (audit finding): O17 documents 404 for another profile's
        # run id and "never 403", and no page in the pack describes a 403 for any path this
        # worker reads, so the old ``forbidden_by_hermes`` row asserted "the key is valid" for
        # a status no record covers. It falls through to ``unexpected_status`` below with the
        # status recorded, and ``STATUSES_DESCRIBED_ELSEWHERE`` carries the record's scope.
        if status == 404:
            if ENDPOINTS[operation]["kind"] in ("run", "session"):
                return self._stamp(O.Outcome.unsupported(
                    f"the Hermes gateway answered 404 for "
                    f"{ENDPOINTS[operation]['path']}: no such run or session id on this "
                    "profile. O17: 'another profile's run id returns 404, never 403', so "
                    "this is an unknown-here id, not a missing endpoint.",
                    reason="run_or_session_unknown_here", data=base,
                    next_action=("check the id belongs to this profile (O17: runs are "
                                 "per-profile scoped) and re-run with the id from the "
                                 "profile Grace will use")), extra=base)
            return self._stamp(O.Outcome.unsupported(
                f"the Hermes gateway answered 404 for {ENDPOINTS[operation]['path']}: this "
                "installed build does not serve that documented path. O17 states no "
                "version-to-capability mapping, so this is a measurement, not a defect.",
                reason="endpoint_not_in_installed_build", data=base,
                next_action=("record `hermes --version` and this status beside the row; a "
                             "missing required endpoint blocks the corresponding release "
                             "claim rather than being simulated")), extra=base)
        # 409 and 429 are named states O17 records for *run-starting* requests, so they are
        # reported as those states only on the operation the record describes, and (for 409)
        # only when the request actually carried the header the record talks about. Anything
        # else is ``unexpected_status``: a named state borrowed from another operation's
        # record would assert an evaluation no page covers (audit finding: honesty narrowing).
        run_starting = operation in RUN_STARTING_OPERATIONS
        if status == 409 and run_starting and idempotency_key is not None:
            return self._stamp(O.Outcome.permanent(
                "the Hermes gateway answered 409 for " + ENDPOINTS[operation]["path"]
                + ": this is O17's documented idempotency_key_conflict — the same "
                "Idempotency-Key was reused with a different payload.",
                reason="idempotency_key_conflict", data=base,
                next_action=("do not re-send: a different payload under a reserved key is "
                             "refused by design (O17). Use a fresh key for new work, and "
                             "the same key only for an identical retry")), extra=base)
        if status == 429 and run_starting:
            return self._stamp(O.Outcome.rate_limited(
                "the Hermes gateway answered 429 for " + ENDPOINTS[operation]["path"]
                + ": too many concurrent runs. O17 names the default cap "
                f"(max_concurrent_runs default {MAX_CONCURRENT_RUNS_DEFAULT}) and says "
                "run-starting requests are 'rejected' rather than queued.",
                reason="rate_limited", data=base,
                next_action=("let the job ledger own the wait and retry later: this server "
                             "rejects rather than queues, so a retry loop here would just "
                             "re-hit the cap")), extra=base)
        if status in STATUSES_DESCRIBED_ELSEWHERE and status not in (200, 202):
            note = STATUSES_DESCRIBED_ELSEWHERE[status]
            if status in (409, 429):
                note = (note + ", and this request (" + operation + ") does not match that "
                        "description: it is "
                        + ("a run-starting request that carried no Idempotency-Key"
                           if run_starting else "not a run-starting request"))
            base = {**base, "status_has_no_documented_meaning_for_this_operation": True,
                    "status_described_elsewhere": note}
        if 500 <= status:
            return self._stamp(O.Outcome.retryable(
                f"the Hermes gateway answered {status} for {ENDPOINTS[operation]['path']}",
                reason="server_error", data=base, next_action="retry the read"), extra=base)
        if status not in (200, 202):
            return self._stamp(O.Outcome.permanent(
                f"the Hermes gateway answered unexpected status {status} for "
                f"{ENDPOINTS[operation]['path']}",
                reason="unexpected_status", data=base), extra=base)
        # SSE is not JSON: an event stream answers 200 with text/event-stream and is parsed
        # by name/count only (never payload text). Checked before the JSON-shape refusal,
        # because a correctly-formed event stream is *expected* not to be a JSON document.
        if document is None and text.strip() and operation == "run_events":
            # The caller's cap, not an inert keyword (audit finding): ``run_events(max_events)``
            # used to accept a bound it never passed on, so the transport always parsed with
            # ``DEFAULT_MAX_EVENTS``. The cap is ours (no page names one) and is recorded.
            applied_limit = int(max_events or DEFAULT_MAX_EVENTS)
            parsed = parse_event_names(text, limit=applied_limit)
            return self._stamp(O.Outcome.ok(
                {**base, "event_stream": True, **parsed,
                 "event_limit_applied": applied_limit,
                 "event_limit_is_ours": True,
                 "event_limit_came_from_caller": bool(max_events),
                 "body_bytes_read": byte_count,
                 "http_status_note": ("O17 documents this endpoint as SSE of tool-call "
                                      "progress, token deltas and lifecycle events")}),
                extra=base)
        if parse_problem is not None:
            return self._stamp(O.Outcome.permanent(
                f"the Hermes gateway answered {status} for {ENDPOINTS[operation]['path']} "
                f"with a body that is not JSON ({parse_problem}); nothing could be read "
                "from it.",
                reason="unexpected_document_shape", data=base), extra=base)
        if document is None and not text.strip():
            return self._stamp(O.Outcome.partial(
                {**base, "document": None, "empty_body": True},
                "the gateway answered " + str(status) + " for "
                + ENDPOINTS[operation]["path"] + " with an empty body",
                reason="empty_body"), extra=base)
        return self._stamp(O.Outcome.ok(
            {**base, "document": document,
             "replayed_response": base["idempotency_replayed"] is True},
            duration_ms=base["duration_ms"]),
            extra={"idempotency_header_seen": replay_header is not None})

    def _from_url_error(self, operation: str, exc, started: float, *,
                        idempotency_key: Optional[str] = None,
                        body: Optional[dict] = None) -> O.Outcome:
        reason = getattr(exc, "reason", None)
        detail = f"{type(reason).__name__ if reason else type(exc).__name__}: {reason or exc}"
        refused = isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower()
        data = {"base_url": self.base_url, "endpoint": ENDPOINTS[operation]["path"],
                "url_error": detail, "requests_made": 1, "duration_ms": _ms(started),
                "responder": "nothing answered"}
        if isinstance(reason, (socket.timeout, TimeoutError)) \
                or "timed out" in str(reason or exc).lower():
            # urllib wraps a socket timeout in URLError, so it arrives here rather than at
            # the `except socket.timeout` branch below. No page in the pack names a
            # timeout: this bound is this worker's own and the state says so.
            if ENDPOINTS[operation]["method"] == "POST":
                # ... and for a POST the request was already written, so the timeout is not
                # evidence that nothing happened (audit finding 3).
                return self._post_uncertain(
                    operation, started=started,
                    detail=(f"this worker's own {self.timeout_s}s timeout elapsed waiting "
                            f"for {ENDPOINTS[operation]['path']} ({detail}), and the request "
                            "had already been written, so it is unknown whether the gateway "
                            "accepted it"),
                    idempotency_key=idempotency_key, body=body,
                    extra={**data, "timeout_s": self.timeout_s, "timeout_is_ours": True})
            return self._stamp(O.Outcome.retryable(
                f"this worker's own {self.timeout_s}s timeout elapsed waiting for "
                f"{ENDPOINTS[operation]['path']}: {detail}",
                reason="request_timeout",
                data={**data, "timeout_s": self.timeout_s, "timeout_is_ours": True},
                next_action="raise --timeout and re-run"), extra=data)
        if refused:
            return self._stamp(O.Outcome.offline(
                f"nothing is listening at {self.base_url} "
                f"({ENDPOINTS[operation]['path']}): the connection was refused. No Hermes "
                "gateway is reachable at that address, so nothing was read and no request "
                "was served.",
                reason="hermes_not_reachable", data=data,
                next_action=("start the gateway with API_SERVER_ENABLED=true in the "
                             "profile Grace will use (O17), check its host/port "
                             "(API_SERVER_HOST / API_SERVER_PORT), then re-run")), extra=data)
        dropped = isinstance(reason, (ConnectionResetError, ConnectionAbortedError,
                                      BrokenPipeError)) \
            or "reset" in str(reason).lower() or "aborted" in str(reason).lower() \
            or "disconnect" in str(reason).lower()
        if dropped and ENDPOINTS[operation]["method"] == "POST":
            # The request was written and the answer never arrived. Nothing may be assumed
            # about whether the gateway acted, and the Idempotency-Key is what makes the
            # retry safe -- the SAME key, the SAME payload.
            return self._stamp(O.Outcome.uncertain(
                "the connection to " + self.base_url + " dropped after the request to "
                + ENDPOINTS[operation]["path"] + " was written and before a response "
                "arrived, so it is unknown whether the gateway accepted it.",
                reason="submission_not_confirmed", data=data,
                next_action=("re-send the **identical** payload with the **same** "
                             "Idempotency-Key"
                             + (f" ({idempotency_key})" if idempotency_key else "")
                             + ": O17 documents that an identical retry returns the "
                               "original run_id with 202 + Idempotency-Replayed: true. "
                               "Never a fresh key and never a guess that it failed")),
                extra=data)
        return self._stamp(O.Outcome.retryable(
            f"the request to {self.base_url} failed: {detail}",
            reason="transport_error", data=data, next_action="retry the read"), extra=data)


class _Replay:
    """Wrap an ``HTTPError`` as a response-shaped object so one mapper handles both.

    ``HTTPError`` *is* a response (it carries a status, headers and a readable body), so
    the same status mapper that handles a 200 handles a 404 body too. The wrapper exists
    only so a body that cannot be read at all is an empty string rather than a second
    exception during error handling.
    """

    def __init__(self, exc):
        self.status = exc.code
        self.code = exc.code
        self.headers = dict(getattr(exc, "headers", {}) or {})
        self._exc = exc

    def read(self):
        try:
            return self._exc.read()
        except Exception:
            return b""


def _replay_for(exc) -> _Replay:
    return _Replay(exc)


# ------------------------------------------------------------------ refusals -------


def refusal_outcome(ask: str) -> O.Outcome:
    """A typed refusal for an ask the pack does not document, or forbids by design."""
    key = (ask or "").strip()
    for name, (reason, detail) in REFUSED_ASKS.items():
        if key == name or key.startswith(name.rstrip("/") + "/") or key == name.rstrip("/"):
            return O.Outcome.unsupported(
                f"{key} is refused: {detail}", reason=reason,
                data={"asked_for": key, "refused_by": "switchboard-mini hermes",
                      "requests_made": 0, "no_request_made": True},
                next_action=(UNLISTED_ASK_NEXT_ACTION if reason == "endpoint_not_in_pack"
                             else ("use POST /v1/runs for run control (O17 documents it as "
                                   "the control surface) and read run state with GET "
                                   "/v1/runs/{run_id}") ))
    return O.Outcome.unsupported(
        f"{key!r} is not a documented Hermes read: the pack (O17) names "
        f"{sorted(ENDPOINTS)} and nothing else, and a purpose for {key!r} appears on no "
        "record.",
        reason="endpoint_not_in_pack",
        data={"asked_for": key, "requests_made": 0, "no_request_made": True},
        next_action=UNLISTED_ASK_NEXT_ACTION)


def missing_path_parameter(operation: str, missing: list) -> O.Outcome:
    return O.Outcome.unsupported(
        f"{operation} needs the path parameter(s) {missing}: "
        f"{ENDPOINTS.get(operation, {}).get('path')} is a template, and sending it "
        "unsubstituted would put a literal placeholder on the wire.",
        reason="missing_path_parameter",
        data={"operation": operation, "path_template": ENDPOINTS.get(operation, {}).get(
            "path"), "missing": missing, "requests_made": 0, "no_request_made": True},
        next_action=("pass the id (for example --run-id <id> or --session-id <id>) — the "
                     "worker will not send a request it cannot address"))


def undocumented_parameter(operation: str, extra: list) -> O.Outcome:
    return O.Outcome.unsupported(
        f"{operation} does not document the parameter(s) {extra}: O17 names "
        f"{list(DOCUMENTED_PARAMS.get(operation, ()))} for this path and nothing else, and "
        "an invented parameter would be a fabricated request.",
        reason="parameter_not_documented",
        data={"operation": operation, "undocumented": extra,
              "documented": list(DOCUMENTED_PARAMS.get(operation, ())),
              "requests_made": 0, "no_request_made": True},
        next_action=("use a documented parameter, or add the parameter to the pack (O17) "
                     "with the page that names it before it is used"))


# ------------------------------------------------------------------ the fixture ---

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "hermes")
FIXTURE_SCENARIOS = ("healthy", "token_rejected", "gateway_down", "endpoint_absent",
                     "rate_limited", "idempotency_replay", "events_without_terminal")

FIXTURE_DISCLAIMER = (
    "FIXTURE — answered from a recorded result shipped in this repository in "
    "--fixture-mode. No Hermes gateway was contacted and no run, session, event or "
    "capability document was read; these are not observations of any real Hermes install."
)


def fixture_path(scenario: str) -> str:
    if scenario not in FIXTURE_SCENARIOS:
        raise ValueError(f"unknown Hermes fixture scenario {scenario!r}; known: "
                         f"{', '.join(FIXTURE_SCENARIOS)}")
    return os.path.join(FIXTURE_DIR, f"{scenario}.json")


def load_fixture(scenario: str) -> dict:
    with open(fixture_path(scenario), "r", encoding="utf-8") as handle:
        return json.load(handle)


class RecordedHermesTransport(HermesTransport):
    """Answers the read interface from a recorded scenario. Never touches a gateway."""

    origin = O.FIXTURE
    adapter_is_real = False

    def __init__(self, fixture: dict, *, name: str = "hermes"):
        self.name = name
        self.fixture = fixture
        self.scenario = fixture.get("scenario", "unknown")
        self.base_url = fixture.get("base_url") or DEFAULT_BASE_URL
        self.profile = fixture.get("profile")
        self.stand_in = False
        self.token_env = fixture.get("token_env")
        self.label = fixture.get("label") or O.fixture_label(
            name, f"recorded Hermes fixture '{self.scenario}'")
        # A recorded scenario states whether the recorded run had a key; it never holds one.
        self.token_configured = bool(fixture.get("token_present"))
        self._fault = fixture.get("fault") or None
        self._counts: dict = {}

    # -- helpers -----------------------------------------------------------
    def _out(self, outcome: O.Outcome, *, extra: Optional[dict] = None) -> O.Outcome:
        outcome.adapter = self.name
        outcome.origin = O.FIXTURE
        outcome.label = self.label
        outcome.adapter_is_real = False
        outcome.source_contacted = False
        payload = dict(outcome.data or {})
        payload.setdefault("fixture_scenario", self.scenario)
        payload.setdefault("responder", "recorded_fixture")
        payload.setdefault("stand_in", False)
        payload.setdefault("token_value_recorded", False)
        if extra:
            payload.update(extra)
        if outcome.data is not None or extra:
            outcome.data = payload
        return outcome

    def token_status(self) -> O.Outcome:
        document = {**token_status_document(self.token_env),
                    "token_present": self.token_configured,
                    "token_source": f"recorded scenario {self.scenario!r} (no value stored)",
                    "fixture_scenario": self.scenario, "responder": "recorded_fixture"}
        if not self.token_configured:
            return self._out(O.Outcome.permission_denied(
                "FIXTURE: the recorded scenario has no bearer key configured, so no request "
                "was made.", reason="token_absent", data=document,
                next_action=f"export {token_env_name(self.token_env)} and re-run"))
        return self._out(O.Outcome.ok(document))

    def call(self, operation: str, *, params: Optional[dict] = None,
             body: Optional[dict] = None, path_params: Optional[dict] = None,
             headers: Optional[dict] = None, max_bytes: Optional[int] = None,
             max_events: Optional[int] = None) -> O.Outcome:
        if operation not in ENDPOINTS:
            return self._out(refusal_outcome(operation))
        missing = [name for name in REQUIRED_PATH_PARAMS.get(operation, ())
                   if not (path_params or {}).get(name)]
        if missing:
            return self._out(missing_path_parameter(operation, missing))
        meta = ENDPOINTS[operation]
        if not self.token_configured:
            return self._out(O.Outcome.permission_denied(
                "FIXTURE: the recorded scenario has no bearer key configured, so no request "
                "was made.", reason="token_absent",
                data={"fixture_scenario": self.scenario,
                      **token_status_document(self.token_env)},
                next_action=f"export {token_env_name(self.token_env)} and re-run"))
        extra_params = sorted(set(params or {}) - set(DOCUMENTED_PARAMS.get(operation, ())))
        if extra_params:
            return self._out(undocumented_parameter(operation, extra_params))
        idem = (headers or {}).get("Idempotency-Key")
        if idem is not None:
            problem = validate_idempotency_key(idem)
            if problem:
                return self._out(O.Outcome.permanent(
                    problem, reason="idempotency_key_not_documented_shape"))
        fault = self._fault
        if fault and (fault.get("applies_to", "*") in ("*", operation)):
            return self._faulted(operation, fault, meta)
        recorded = (self.fixture.get("responses") or {}).get(operation)
        if recorded is None:
            return self._out(O.Outcome.unsupported(
                f"FIXTURE: the recorded scenario has no response for {operation!r} "
                f"({meta['path']}); a recorded answer cannot invent one.",
                reason="not_in_recorded_scenario",
                next_action=f"record a {operation} response on the Mac and add it to the "
                            f"scenario"), extra={"http_status": None})
        if isinstance(recorded, list):
            # A scenario may record a sequence (the idempotency replay: first call, then the
            # replay). The counter is per operation and clamps at the last entry.
            index = min(self._counts.get(operation, 0), len(recorded) - 1)
            self._counts[operation] = self._counts.get(operation, 0) + 1
            recorded = recorded[index]
        status = int(recorded.get("status", 200))
        response_headers = dict(recorded.get("headers") or {})
        payload = recorded.get("json")
        stream = recorded.get("stream")
        base = {"http_status": status, "endpoint": meta["path"], "endpoint_ref": meta["ref"],
                "duration_ms": recorded.get("duration_ms", 3),
                "idempotency_replayed": (
                    (response_headers.get("Idempotency-Replayed") or "").lower() == "true"
                    if "Idempotency-Replayed" in response_headers else None)}
        if status == 401:
            return self._out(O.Outcome.permission_denied(
                "FIXTURE: the recorded scenario answers 401 for " + meta["path"]
                + " — the bearer key was rejected.", reason="token_rejected", data=base,
                next_action="use the key the profile's API server was started with "
                            "(API_SERVER_KEY, O17)"), extra=base)
        # Same narrowing as the live transport (audit finding): a recorded 429/409 is that
        # named state only on the operation O17 records it for, so a scenario cannot make a
        # GET look rate-limited or an unknown 409 look like an idempotency conflict.
        fixture_run_starting = operation in RUN_STARTING_OPERATIONS
        if status == 429 and fixture_run_starting:
            return self._out(O.Outcome.rate_limited(
                "FIXTURE: the recorded scenario answers 429 for " + meta["path"]
                + " — too many concurrent runs (O17: the server rejects rather than "
                  "queues)", reason="rate_limited", data=base,
                next_action="let the job ledger own the wait and retry later"), extra=base)
        if status == 404:
            if meta["kind"] in ("run", "session"):
                return self._out(O.Outcome.unsupported(
                    f"FIXTURE: the recorded scenario answers 404 for {meta['path']} — an "
                    "unknown-here run or session id (O17: another profile's run id returns "
                    "404, never 403)", reason="run_or_session_unknown_here", data=base),
                    extra=base)
            return self._out(O.Outcome.unsupported(
                f"FIXTURE: the recorded scenario answers 404 for {meta['path']} — the "
                "installed build does not serve it",
                reason="endpoint_not_in_installed_build", data=base), extra=base)
        if status == 409 and fixture_run_starting and idem is not None:
            return self._out(O.Outcome.permanent(
                "FIXTURE: the recorded scenario answers 409 for " + meta["path"]
                + " — idempotency_key_conflict (O17)", reason="idempotency_key_conflict",
                data=base, next_action="use the same key only for an identical retry"),
                extra=base)
        if status in STATUSES_DESCRIBED_ELSEWHERE and status not in (200, 202):
            note = STATUSES_DESCRIBED_ELSEWHERE[status]
            if status in (409, 429):
                note = (note + ", and this recorded request (" + operation + ") does not "
                        "match that description: it is "
                        + ("a run-starting request that carried no Idempotency-Key"
                           if fixture_run_starting else "not a run-starting request"))
            base = {**base, "status_has_no_documented_meaning_for_this_operation": True,
                    "status_described_elsewhere": note}
        if status not in (200, 202):
            return self._out(O.Outcome.permanent(
                f"FIXTURE: the recorded scenario answers status {status} for {meta['path']}",
                reason="unexpected_status", data=base), extra=base)
        if stream is not None:
            # The caller's cap wins over the scenario's recorded default (audit finding: the
            # adapter's ``max_events`` argument used to be inert).
            applied_limit = int(max_events
                                or self.fixture.get("max_events", DEFAULT_MAX_EVENTS))
            parsed = parse_event_names(stream, limit=applied_limit)
            return self._out(O.Outcome.ok({**base, "event_stream": True, **parsed,
                                           "event_limit_applied": applied_limit,
                                           "event_limit_is_ours": True,
                                           "event_limit_came_from_caller": bool(max_events)}),
                             extra={**base, "idempotency_header_seen":
                                    "Idempotency-Replayed" in response_headers})
        return self._out(O.Outcome.ok({**base, "document": payload,
                                       "replayed_response":
                                           base["idempotency_replayed"] is True}),
                         extra={**base, "idempotency_header_seen":
                                "Idempotency-Replayed" in response_headers})

    def _faulted(self, operation: str, fault: dict, meta: dict) -> O.Outcome:
        kind = fault.get("kind")
        detail = fault.get("detail") or f"FIXTURE: recorded {kind} during {operation}"
        if kind == "connection_refused":
            data = {"base_url": self.base_url, "endpoint": meta["path"],
                    "http_status": None, "requests_made": 1,
                    "responder": "nothing answered",
                    "fixture_scenario": self.scenario}
            return self._out(O.Outcome.offline(
                "FIXTURE: nothing was listening at the recorded base URL "
                f"({meta['path']}): the connection was refused.",
                reason="hermes_not_reachable", data=data,
                next_action="start the gateway with API_SERVER_ENABLED=true (O17)"), extra=data)
        if kind == "connection_dropped":
            data = {"base_url": self.base_url, "endpoint": meta["path"],
                    "http_status": None, "requests_made": 1,
                    "responder": "connection dropped", "fixture_scenario": self.scenario}
            if meta["method"] == "POST":
                return self._out(O.Outcome.uncertain(
                    "FIXTURE: the recorded connection dropped after the request was "
                    f"written ({meta['path']}) and before a response arrived.",
                    reason="submission_not_confirmed", data=data,
                    next_action="re-send the identical payload with the same "
                                "Idempotency-Key"), extra=data)
            return self._out(O.Outcome.retryable(detail, reason="transport_error"), extra=data)
        if kind == "timeout":
            return self._out(O.Outcome.retryable(detail, reason="request_timeout",
                                                 next_action="retry with a longer timeout"))
        if kind == "token_echo":
            return self._out(O.Outcome.permanent(
                "FIXTURE: the recorded body contains the bearer key value, so the body was "
                "discarded and no row is built from it.",
                reason="token_echoed_in_response",
                data={"base_url": self.base_url, "endpoint": meta["path"],
                      "http_status": None, "requests_made": 1,
                      "fixture_scenario": self.scenario},
                next_action="treat the gateway as leaking credentials and rotate the key"))
        return self._out(O.Outcome.permanent(detail, reason="recorded_fixture_fault"))


def build_transport(*, fixture_mode: bool = False, fixture_scenario: str = "healthy",
                    base_url: Optional[str] = None, timeout_s: int = DEFAULT_TIMEOUT_S,
                    stand_in: bool = False, token_env: Optional[str] = None,
                    profile: Optional[str] = None,
                    opener: Optional[Callable[..., Any]] = None) -> HermesTransport:
    """The one place that decides recorded versus real for the Hermes adapter."""
    if fixture_mode:
        return RecordedHermesTransport(load_fixture(fixture_scenario))
    return HttpHermesTransport(base_url=base_url, timeout_s=timeout_s,
                               stand_in=stand_in, token_env=token_env, profile=profile,
                               opener=opener)


__all__ = [
    "HermesTransport", "HttpHermesTransport", "RecordedHermesTransport", "build_transport",
    "load_fixture", "fixture_path", "FIXTURE_SCENARIOS", "FIXTURE_DISCLAIMER", "ENDPOINTS",
    "REFUSED_ASKS", "TOKEN_ENV", "BASE_URL_ENV", "STANDIN_ENV", "DEFAULT_BASE_URL",
    "DEFAULT_BASE_URL_SOURCE", "NEVER_READ_PATHS", "DOCUMENTED_PARAMS",
    "REQUIRED_PATH_PARAMS", "IDEMPOTENCY_KEY_MAX", "MAX_CONCURRENT_RUNS_DEFAULT",
    "RUN_STATUSES", "RUN_TERMINAL_STATUSES", "DOCUMENTED_EVENT_NAMES",
    "TERMINAL_EVENT_NAMES", "DOCUMENTED_TERMINAL_EVENT_NAMES", "DEFAULT_MAX_EVENTS",
    "MIN_TOKEN_LEN_FOR_ECHO_CHECK", "RUN_STARTING_OPERATIONS", "STATUSES_DESCRIBED_ELSEWHERE",
    "DEFAULT_SETTLE_SECONDS", "token_env_name", "token_value", "token_present",
    "token_status_document", "base_url_from_env", "standin_requested", "profile_prefix",
    "resolve_path", "unsubstituted_placeholders", "validate_idempotency_key", "scrub_secrets",
    "body_carries_token", "parse_event_names", "refusal_outcome", "missing_path_parameter",
    "undocumented_parameter",
]
