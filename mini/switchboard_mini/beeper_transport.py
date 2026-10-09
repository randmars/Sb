"""The Beeper Desktop local-API read transport. Standard library only.

Every endpoint, parameter, header and query limit in this module is copied from a
record in the Gate 2 probe pack, and the record says which page it came from:

* ``GET /v1/info`` -- O11: "GET /v1/info returns metadata about the active Desktop API
  server and exposes endpoint URLs for discovery."
* ``POST /oauth/introspect`` -- O11: "OAuth token introspection is available via POST
  /oauth/introspect and accepts form-encoded payloads" (``token=<your_token>
  &token_type_hint=access_token``), returning ``active``.
* ``GET /v1/messages/search`` -- O06, with the documented parameters and response fields
  named in ``beeper_adapter``.
* ``GET /v1/accounts/{accountID}/contacts/list`` -- O12.
* header -- O11: "All requests to Beeper Desktop API ... support the Authorization header
  with the Bearer token for authentication", literal form
  ``Authorization: Bearer <your_token>``.
* the default base URL -- ``http://localhost:23373``, which appears on O11 inside the OAuth
  metadata URL and in the example calls recorded on O06 and O12. The pack is explicit that
  this is a **documented example host, not a verified base URL of Randy's install** (O11
  ``do_not_use``), so it is a default the operator can override and every response this
  module produces records which base URL was used and where that default came from.

What is deliberately **not** here, because no page in the pack names it: an accounts
listing endpoint, a chats listing endpoint, and a "messages of a chat" endpoint. The
adapter answers those asks with ``unsupported``/``endpoint_not_in_pack`` and the smallest
next action (read the discovery URLs ``GET /v1/info`` exposes on the Mac). A guessed path
would be a fabricated measurement, which is the one thing this product may not do.

Typed states (the shared vocabulary in :mod:`switchboard_mini.outcomes`):

* no token in the environment -> ``permission_denied`` / ``token_absent``
* this host is not macOS and the stand-in guard was not opened -> ``unsupported`` /
  ``host_not_macos`` (nothing is contacted: no request is made off-Mac)
* connection refused / no listener -> ``offline`` / ``beeper_not_reachable``
* HTTP 401 -> ``permission_denied`` / ``token_rejected``; 403 ->
  ``permission_denied`` / ``forbidden_by_desktop``
* HTTP 404 -> ``unsupported`` / ``endpoint_not_found``
* HTTP 429 -> ``rate_limited``; 5xx -> ``retryable_error``; 400 -> ``permanent_error``
* a body that is not JSON -> ``permanent_error`` / ``non_json_response``

The token value is never logged, echoed, persisted, put in a URL, or written into an
error string. Only its presence and where it was read from are ever reported
(``token_present`` / ``token_source`` / ``token_value_recorded: false``).

``stand_in`` exists for one purpose: exercising this transport's wire layer on a host that
is not Randy's Mac (there is no Beeper Desktop on this Linux computer, and the Mini role
runs on the Mac). A stand-in run marks every document ``responder: stand_in_http_server``
and forces ``source_contacted: false``, so a row it produces can never claim
``real_source_connected: true`` and can never import into Grace as a measurement.
"""

from __future__ import annotations

import abc
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from . import outcomes as O

#: Where the owner's token is read from. O11 step 3 of the pack's Mac procedure uses
#: exactly this environment variable in its example ``curl`` call.
TOKEN_ENV = "BEEPER_ACCESS_TOKEN"
#: Optional override for the API base URL. The documented example host is the default.
BASE_URL_ENV = "BEEPER_API_BASE_URL"
#: Opt-in that opens the non-macOS guard for wire-layer testing. Never a real measurement.
STANDIN_ENV = "SWITCHBOARD_BEEPER_STANDIN"

DEFAULT_BASE_URL = "http://localhost:23373"
DEFAULT_BASE_URL_SOURCE = (
    "documented example host http://localhost:23373, recorded on O11 (inside the OAuth "
    "metadata URL), O06 and O12 (their example calls). The pack records it as a name, not "
    "as a verified base URL of Randy's install (O11 'do not use')")

#: The read operations this transport can perform, each with the pack ref that documents
#: it. Nothing else may be requested.
ENDPOINTS: dict = {
    "info": {"method": "GET", "path": "/v1/info", "ref": "O11",
             "described_as": "returns metadata about the active Desktop API server and "
                             "exposes endpoint URLs for discovery"},
    "introspect": {"method": "POST", "path": "/oauth/introspect", "ref": "O11",
                   "described_as": "OAuth token introspection; form-encoded body "
                                   "token=<token>&token_type_hint=access_token"},
    "search": {"method": "GET", "path": "/v1/messages/search", "ref": "O06",
               "described_as": "Search messages across chats."},
    "contacts": {"method": "GET", "path": "/v1/accounts/{accountID}/contacts/list",
                 "ref": "O12",
                 "described_as": "List merged contacts for a specific account with "
                                 "cursor-based pagination."},
}

#: Asks the pack does not answer. Kept as data so the refusal message can name the ask and
#: the smallest next action instead of inventing a path.
UNDOCUMENTED_ASKS: dict = {
    "accounts": ("no accounts-listing endpoint is named by any page in the pack: O06 "
                 "documents 'accounts listing' as not_addressed, and O05 states no endpoint "
                 "list at all"),
    "chats": ("no chats-listing endpoint is named by any page in the pack: O06's 'chats' "
              "map is 'chats referenced in items', which that record explicitly says must "
              "not be cited as a chats listing"),
    "chat_messages": ("no per-chat message endpoint is named by any page in the pack: O06 "
                      "documents message *search* pagination only"),
}
UNDOCUMENTED_NEXT_ACTION = (
    "on the Mac, call GET /v1/info (documented on O11 as exposing endpoint URLs for "
    "discovery), record the paths it advertises, and add them to the pack before they are "
    "used — this adapter will not guess one")

#: Documented limits (O06: ``limit`` "minimum 0 exclusiveMinimum maximum 20").
SEARCH_LIMIT_MAX = 20
SEARCH_LIMIT_MIN = 1
#: Documented limits (O12: ``limit`` "minimum 1", "maximum 200").
CONTACTS_LIMIT_MAX = 200
CONTACTS_LIMIT_MIN = 1
#: O06 documents these cursor directions.
DIRECTIONS = ("after", "before")


def token_present() -> bool:
    """Is a token configured? The value itself is never read out of this function."""
    return bool((os.environ.get(TOKEN_ENV) or "").strip())


def token_status_document() -> dict:
    """What may be said about the token without saying the token."""
    return {
        "token_present": token_present(),
        "token_source": f"environment {TOKEN_ENV}",
        "token_value_recorded": False,
        "note": ("the token value is never logged, echoed, persisted or put in an error "
                 "string; only its presence and its source are reported (probe pack O11 "
                 "'No secret leaves your secret store')"),
    }


def base_url_from_env() -> str:
    return (os.environ.get(BASE_URL_ENV) or "").strip() or DEFAULT_BASE_URL


def standin_requested() -> bool:
    return bool((os.environ.get(STANDIN_ENV) or "").strip())


# --------------------------------------------------------------------- interface --


class BeeperTransport(abc.ABC):
    """One method per read the Beeper adapter performs. All return ``Outcome``."""

    name = "beeper"
    origin = O.REAL
    adapter_is_real = False
    label: Optional[str] = None
    base_url = DEFAULT_BASE_URL
    stand_in = False

    @abc.abstractmethod
    def token_status(self) -> O.Outcome: ...

    @abc.abstractmethod
    def call(self, operation: str, *, params: Optional[dict] = None,
             form: Optional[dict] = None) -> O.Outcome: ...

    def host_gate(self) -> Optional[O.Outcome]:
        """The precondition that would block *any* request from this host, or None.

        A read path that refuses before it would otherwise make a request (a missing
        account id, say) asks this first, so the row names the host-level truth instead of
        a narrower reason that would be misleading on a host that cannot reach Beeper at
        all.
        """
        return None


# ------------------------------------------------------------------- the real one --

class HttpBeeperTransport(BeeperTransport):
    """Read-only talk to the Beeper Desktop API over loopback HTTP.

    ``opener`` is injectable so a test can drive the wire layer with a recorded response
    without a socket; the default opener makes a real request. ``stand_in`` marks the
    responder as *not* the source and is never a measurement.
    """

    origin = O.REAL
    adapter_is_real = True

    def __init__(self, *, base_url: Optional[str] = None, token: Optional[str] = None,
                 timeout_s: int = 10, stand_in: bool = False,
                 opener: Optional[Callable[[urllib.request.Request, int], Any]] = None):
        self.base_url = (base_url or base_url_from_env()).rstrip("/")
        # Read the token once, hold it in memory, never expose it.
        self._token = token if token is not None else (os.environ.get(TOKEN_ENV) or "")
        self._token = self._token.strip()
        self.timeout_s = int(timeout_s)
        self.stand_in = bool(stand_in or standin_requested())
        self._opener = opener

    # -- preconditions -----------------------------------------------------
    def _host_supported(self) -> Optional[O.Outcome]:
        if sys.platform != "darwin" and not self.stand_in:
            return O.Outcome.unsupported(
                "the Mini worker's Beeper read adapter is installed for the Mini role on "
                f"Randy's Mac: this host is platform={sys.platform} and no Beeper Desktop "
                "API request was made from it. Nothing was contacted.",
                reason="host_not_macos", adapter=self.name, data={
                    "platform": sys.platform,
                    "base_url": self.base_url,
                    "requests_made": 0,
                    "stand_in_allowed": False,
                    "note": (f"{STANDIN_ENV}=1 (or --stand-in-server) opens a local "
                             "stand-in responder for wire-layer testing only; every "
                             "document it produces is marked stand_in and can never claim "
                             "a real source"),
                },
                next_action=("run the Mini worker on Randy's Mac with Beeper Desktop "
                             "running (Gate 2), or set " + STANDIN_ENV + "=1 to exercise "
                             "the wire layer against a stand-in responder"))
        return None

    def _token_gate(self) -> Optional[O.Outcome]:
        if not self._token:
            return O.Outcome.permission_denied(
                f"no Beeper token is configured: {TOKEN_ENV} is empty or unset, so no "
                "request was made and nothing was read.",
                reason="token_absent", adapter=self.name,
                data=token_status_document(),
                next_action=(f"create a token in Beeper Desktop -> Settings -> Integrations "
                             f"-> '+' next to 'Approved connections' (O11) and export it as "
                             f"{TOKEN_ENV} in this process's environment, then re-run"))
        return None

    def host_gate(self) -> Optional[O.Outcome]:
        blocked = self._host_supported() or self._token_gate()
        return None if blocked is None else self._blocked_outcome(blocked)

    def token_status(self) -> O.Outcome:
        # A local check only: it contacts nothing, so the host guard does not apply.
        document = token_status_document()
        if not token_present() and not self._token:
            return O.Outcome.permission_denied(
                f"no Beeper token is present: {TOKEN_ENV} is empty or unset. No request was "
                "made.",
                reason="token_absent", adapter=self.name, data=document,
                next_action=(f"export {TOKEN_ENV} from your secret store (Beeper Desktop -> "
                             "Settings -> Integrations -> 'Approved connections', O11), "
                             "then re-run"))
        return O.Outcome.ok(document, adapter=self.name)

    # -- requests ----------------------------------------------------------
    def _blocked_outcome(self, outcome: O.Outcome) -> O.Outcome:
        """Stamp a refusal this transport built before touching the network.

        ``adapter_is_real`` is true (this is the real adapter speaking) while
        ``source_contacted`` stays false: nothing answered this document.
        """
        outcome.adapter = self.name
        outcome.origin = O.REAL
        outcome.adapter_is_real = True
        outcome.source_contacted = False
        return outcome

    def _request(self, operation: str, params: Optional[dict], form: Optional[dict]):
        meta = ENDPOINTS[operation]
        path = meta["path"]
        url = self.base_url + path
        if params:
            url = url + "?" + urllib.parse.urlencode(params, doseq=True)
        data = None
        if meta["method"] == "POST":
            payload = dict(form or {})
            if operation == "introspect":
                # O11 documents a form-encoded body: token=<your_token>
                # &token_type_hint=access_token. The token goes here and in the header; it
                # is never copied into any document, log or error string.
                payload["token"] = self._token
            data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=meta["method"])
        # O11: the literal header form Authorization: Bearer <your_token>.
        request.add_header("Authorization", "Bearer " + self._token)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        if self._opener is not None:
            return self._opener(request, self.timeout_s)
        return urllib.request.urlopen(request, timeout=self.timeout_s)

    def call(self, operation: str, *, params: Optional[dict] = None,
             form: Optional[dict] = None) -> O.Outcome:
        if operation not in ENDPOINTS:
            return self._blocked_outcome(O.Outcome.unsupported(
                f"{operation!r} is not a documented Beeper read: the pack names "
                f"{sorted(ENDPOINTS)} and nothing else.",
                reason="endpoint_not_in_pack", next_action=UNDOCUMENTED_NEXT_ACTION))
        blocked = self._host_supported() or self._token_gate()
        if blocked is not None:
            return self._blocked_outcome(blocked)
        started = time.monotonic()
        try:
            response = self._request(operation, params, form)
        except urllib.error.HTTPError as exc:
            return self._from_http_error(operation, exc, started)
        except urllib.error.URLError as exc:
            return self._from_url_error(operation, exc, started)
        except (OSError, ValueError) as exc:
            return self._stamp(O.Outcome.retryable(
                f"the request to {operation} could not be completed: "
                f"{type(exc).__name__}: {exc}",
                reason="request_failed", next_action="retry the read",
                duration_ms=_ms(started)))
        return self._from_response(operation, response, started)

    # -- responses ---------------------------------------------------------
    def _stamp(self, outcome: O.Outcome, *, extra: Optional[dict] = None) -> O.Outcome:
        outcome.adapter = self.name
        outcome.origin = O.REAL
        outcome.adapter_is_real = True
        # A stand-in responder is not the source; a real response is. A refusal that
        # reached the network but carried no usable value keeps source_contacted False,
        # exactly as the Mail transport does.
        outcome.source_contacted = bool(outcome.usable) and not self.stand_in
        payload = dict(outcome.data or {})
        payload.setdefault("base_url", self.base_url)
        payload.setdefault("responder", "stand_in_http_server" if self.stand_in
                           else "beeper_desktop_api")
        payload.setdefault("stand_in", self.stand_in)
        payload.setdefault("token_value_recorded", False)
        if extra:
            payload.update(extra)
        if outcome.data is not None or extra:
            outcome.data = payload
        return outcome

    def _from_response(self, operation: str, response, started: float) -> O.Outcome:
        status = getattr(response, "status", None) or getattr(response, "code", 0) or 0
        raw = response.read() if hasattr(response, "read") else b""
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        document = None
        parse_problem = None
        if text:
            try:
                document = json.loads(text)
            except ValueError as exc:
                parse_problem = str(exc)
        base = {"http_status": status, "body_bytes": len(raw or ""),
                "duration_ms": _ms(started),
                "endpoint": ENDPOINTS[operation]["path"],
                "endpoint_ref": ENDPOINTS[operation]["ref"]}
        if status == 401:
            return self._stamp(O.Outcome.permission_denied(
                "the Beeper API answered 401 for " + ENDPOINTS[operation]["path"]
                + ": the token was rejected. No data was returned.",
                reason="token_rejected", data=base,
                next_action=("create or refresh the token in Beeper Desktop -> Settings -> "
                             "Integrations -> 'Approved connections' (O11), export it as "
                             + TOKEN_ENV + ", then re-run")), extra=base)
        if status == 403:
            return self._stamp(O.Outcome.permission_denied(
                f"the Beeper API answered 403 for {ENDPOINTS[operation]['path']}: the token "
                "is valid but this request is not permitted. No data was returned.",
                reason="forbidden_by_desktop", next_action=("check the token's scope in "
                                                            "Beeper Desktop (O11 names no "
                                                            "scope model) and record the "
                                                            "status")), extra=base)
        if status == 404:
            return self._stamp(O.Outcome.unsupported(
                f"the Beeper API answered 404 for {ENDPOINTS[operation]['path']}: this "
                "install does not serve that documented path (O11 names no API version or "
                "minimum Desktop release, so this is a measurement, not a defect).",
                reason="endpoint_not_found", data=base,
                next_action="record the installed Beeper Desktop version and this status"),
                extra=base)
        if status == 429:
            return self._stamp(O.Outcome.rate_limited(
                "the Beeper API answered 429 for " + ENDPOINTS[operation]["path"],
                reason="rate_limited", data=base, next_action="retry more slowly"),
                extra=base)
        if 500 <= status:
            return self._stamp(O.Outcome.retryable(
                f"the Beeper API answered {status} for {ENDPOINTS[operation]['path']}",
                reason="server_error", data=base, next_action="retry the read"), extra=base)
        if status == 400:
            return self._stamp(O.Outcome.permanent(
                f"the Beeper API answered 400 for {ENDPOINTS[operation]['path']}: the "
                "request was rejected. No data was returned.",
                reason="bad_request", data=base,
                next_action="check the parameter names against O06/O12"), extra=base)
        if status != 200:
            return self._stamp(O.Outcome.permanent(
                f"the Beeper API answered unexpected status {status} for "
                f"{ENDPOINTS[operation]['path']}",
                reason="unexpected_status", data=base), extra=base)
        if parse_problem is not None:
            return self._stamp(O.Outcome.permanent(
                f"the Beeper API answered 200 for {ENDPOINTS[operation]['path']} with a "
                f"body that is not JSON ({parse_problem}); nothing could be read from it.",
                reason="non_json_response", data=base), extra=base)
        return self._stamp(O.Outcome.ok({**base, "document": document},
                                        duration_ms=base["duration_ms"]), extra=base)

    def _from_http_error(self, operation: str, exc, started: float) -> O.Outcome:
        class _Replay:
            status = exc.code
            def read(self):
                try:
                    return exc.read()
                except Exception:
                    return b""
        return self._from_response(operation, _Replay(), started)

    def _from_url_error(self, operation: str, exc, started: float) -> O.Outcome:
        reason = getattr(exc, "reason", None)
        detail = f"{type(reason).__name__ if reason else type(exc).__name__}: {reason or exc}"
        refused = isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower()
        data = {"base_url": self.base_url, "endpoint": ENDPOINTS[operation]["path"],
                "url_error": detail, "requests_made": 1, "duration_ms": _ms(started),
                "responder": "nothing answered",
                "note": ("O05: the Desktop API runs inside Beeper Desktop and requires "
                         "Beeper Desktop to be running to be accessible")}
        if refused:
            return self._stamp(O.Outcome.offline(
                f"nothing is listening at {self.base_url} "
                f"({ENDPOINTS[operation]['path']}): the connection was refused. Beeper "
                "Desktop is probably not running (O05).",
                reason="beeper_not_reachable", data=data,
                next_action="start Beeper Desktop (its API needs it running, O05), then "
                            "re-run"), extra=data)
        return self._stamp(O.Outcome.retryable(
            f"the request to {self.base_url} failed: {detail}",
            reason="transport_error", data=data, next_action="retry the read"), extra=data)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ------------------------------------------------------------------ the fixture ---

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "beeper")
FIXTURE_SCENARIOS = ("healthy", "token_rejected", "beeper_down", "history_limited")

FIXTURE_DISCLAIMER = (
    "FIXTURE — answered from a recorded result shipped in this repository in "
    "--fixture-mode. No Beeper Desktop was contacted and no chat, message or contact was "
    "read; these are not observations of any real Beeper install."
)


def fixture_path(scenario: str) -> str:
    if scenario not in FIXTURE_SCENARIOS:
        raise ValueError(f"unknown Beeper fixture scenario {scenario!r}; known: "
                         f"{', '.join(FIXTURE_SCENARIOS)}")
    return os.path.join(FIXTURE_DIR, f"{scenario}.json")


def load_fixture(scenario: str) -> dict:
    with open(fixture_path(scenario), "r", encoding="utf-8") as handle:
        return json.load(handle)


class RecordedBeeperTransport(BeeperTransport):
    """Answers the read interface from a recorded scenario. Never touches Beeper."""

    origin = O.FIXTURE
    adapter_is_real = False

    def __init__(self, fixture: dict, *, name: str = "beeper"):
        self.name = name
        self.fixture = fixture
        self.scenario = fixture.get("scenario", "unknown")
        self.base_url = fixture.get("base_url") or DEFAULT_BASE_URL
        self.stand_in = False
        self.label = fixture.get("label") or O.fixture_label(
            name, f"recorded Beeper fixture '{self.scenario}'")
        # A recorded scenario states whether the recorded run had a token; it never holds
        # one. There is no key in any fixture that can carry a token value.
        self.token_configured = bool(fixture.get("token_present"))
        self._fault = fixture.get("fault") or None

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
        document = {**token_status_document(), "token_present": self.token_configured,
                    "token_source": f"recorded scenario {self.scenario!r} (no value stored)",
                    "fixture_scenario": self.scenario, "responder": "recorded_fixture"}
        if not self.token_configured:
            return self._out(O.Outcome.permission_denied(
                "FIXTURE: the recorded scenario has no token configured, so no request was "
                "made.", reason="token_absent", data=document,
                next_action=f"export {TOKEN_ENV} and re-run"))
        return self._out(O.Outcome.ok(document))

    def call(self, operation: str, *, params: Optional[dict] = None,
             form: Optional[dict] = None) -> O.Outcome:
        if operation not in ENDPOINTS:
            return self._out(O.Outcome.unsupported(
                f"{operation!r} is not a documented Beeper read: the pack names "
                f"{sorted(ENDPOINTS)} and nothing else.",
                reason="endpoint_not_in_pack", next_action=UNDOCUMENTED_NEXT_ACTION))
        meta = ENDPOINTS[operation]
        if not self.token_configured:
            return self._out(O.Outcome.permission_denied(
                "FIXTURE: the recorded scenario has no token configured, so no request was "
                "made.", reason="token_absent",
                data={"fixture_scenario": self.scenario, **token_status_document()},
                next_action=f"export {TOKEN_ENV} and re-run"))
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
                            f"scenario"),
                extra={"http_status": None})
        if "first" in recorded or "cursor" in recorded:
            # A scenario may record a second page for a cursor resume. Without one the
            # first page is returned again, which is itself a measurement (the adapter
            # detects the repeated page rather than pretending it advanced).
            page = "cursor" if (params or {}).get("cursor") else "first"
            recorded = recorded.get(page) or recorded.get("first") or {}
        status = int(recorded.get("status", 200))
        body = recorded.get("json")
        base = {"http_status": status, "endpoint": meta["path"], "endpoint_ref": meta["ref"],
                "duration_ms": recorded.get("duration_ms", 3)}
        if status == 401:
            return self._out(O.Outcome.permission_denied(
                "FIXTURE: the recorded scenario answers 401 for " + meta["path"]
                + " — the token was rejected.", reason="token_rejected", data=base,
                next_action=("create or refresh the token in Beeper Desktop -> Settings -> "
                             "Integrations -> 'Approved connections' (O11)")), extra=base)
        if status == 404:
            return self._out(O.Outcome.unsupported(
                f"FIXTURE: the recorded scenario answers 404 for {meta['path']}",
                reason="endpoint_not_found", data=base), extra=base)
        if status != 200:
            return self._out(O.Outcome.permanent(
                f"FIXTURE: the recorded scenario answers status {status} for {meta['path']}",
                reason="unexpected_status", data=base), extra=base)
        return self._out(O.Outcome.ok({**base, "document": body}), extra=base)

    def _faulted(self, operation: str, fault: dict, meta: dict) -> O.Outcome:
        kind = fault.get("kind")
        detail = fault.get("detail") or (
            f"FIXTURE: recorded {kind} during {operation}")
        if kind == "connection_refused":
            data = {"base_url": self.base_url, "endpoint": meta["path"],
                    "http_status": None, "requests_made": 1,
                    "note": ("O05: the Desktop API runs inside Beeper Desktop and requires "
                             "Beeper Desktop to be running to be accessible"),
                    "fixture_scenario": self.scenario}
            return self._out(O.Outcome.offline(
                "FIXTURE: nothing was listening at the recorded base URL "
                f"({meta['path']}): the connection was refused.",
                reason="beeper_not_reachable", data=data,
                next_action="start Beeper Desktop (its API needs it running, O05)"),
                extra=data)
        if kind == "timeout":
            return self._out(O.Outcome.retryable(detail, reason="timeout",
                                                 next_action="retry with a longer timeout"))
        return self._out(O.Outcome.permanent(detail, reason="recorded_fixture_fault"))


def build_transport(*, fixture_mode: bool = False, fixture_scenario: str = "healthy",
                    base_url: Optional[str] = None, timeout_s: int = 10,
                    stand_in: bool = False,
                    opener: Optional[Callable[..., Any]] = None) -> BeeperTransport:
    """The one place that decides recorded versus real for the Beeper adapter."""
    if fixture_mode:
        return RecordedBeeperTransport(load_fixture(fixture_scenario))
    return HttpBeeperTransport(base_url=base_url, timeout_s=timeout_s, stand_in=stand_in,
                               opener=opener)


__all__ = ["BeeperTransport", "HttpBeeperTransport", "RecordedBeeperTransport",
           "build_transport", "load_fixture", "fixture_path", "FIXTURE_SCENARIOS",
           "FIXTURE_DISCLAIMER", "ENDPOINTS", "UNDOCUMENTED_ASKS", "TOKEN_ENV",
           "BASE_URL_ENV", "STANDIN_ENV", "DEFAULT_BASE_URL", "DEFAULT_BASE_URL_SOURCE",
           "SEARCH_LIMIT_MAX", "CONTACTS_LIMIT_MAX", "DIRECTIONS", "token_present",
           "token_status_document", "base_url_from_env", "standin_requested"]
