"""Gate 1 review client served by Grace: the four PRD §5 surfaces over HTTP.

This module is the *client-facing* half of Grace. It renders the same ledger the CLI
renders, over a tiny authenticated HTTP surface, with vanilla HTML/CSS/JS and no
third-party dependency (Python standard library ``http.server`` only). It exists so
Randy can triage from a phone without a desktop: PRD §5 minimum surfaces and
"Mobile and accessibility", §10 draft/approval contract, R11 (one-tap delegation and
a review return path) and R16 (vertical slice before dashboards).

Honesty rules enforced here, not merely displayed:

* **No unauthenticated surface at all.** Every path — including ``/``, the stylesheet
  and the script — requires the owner's token. An unauthenticated request gets a
  fixed 401 body that contains no ledger value. Comparison is constant-time
  (``hmac.compare_digest``) and the token is never written to a log line: the request
  logger records the method and the path *without* the query string.
* **Labelled mocks.** Every payload carries ``mocked``, a ``MOCK:`` label and the
  disclaimer whenever the deployment holds any mock source, exactly like the CLI.
* **No invented send state.** ``send_state`` is computed from the effect ledger and
  the receipt's ``verified_against_real_source`` column. ``is_green_sent`` can only be
  true when the ledger says ``confirmed_sent`` *and* the receipt was verified against a
  real source. On this deployment it is therefore always false.
* **The source is never touched by assignment.** Assignment changes the application
  queue filter only (``workspace_conversation.queue_state``); source ``read_state``,
  ``hidden_state``, ``mute_state`` and ``availability`` are read-only here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import contracts as C
from .contracts import ApprovalState, EffectState, JobState, QueueState
from .effects import InjectedFault
from .ingest import Ingest
from .ledger import Ledger, _job_brief
from .service import MOCK_WORKER_NOTE, Grace

#: What an archived or deleted item is, said the same way on every surface. It is the
#: ledger's own wording so the client and the ledger cannot drift apart (PRD §8, R08).
RETENTION_NOTE = Ledger.RETENTION_NOTE


WEBUI_DIR = Path(__file__).with_name("webui")
TOKEN_HEADER = "X-Switchboard-Token"
COOKIE_NAME = "sb_session"
SESSION_SALT = b"switchboard-web-session-v1"
MAX_BODY_BYTES = 1 << 20  # 1 MiB: no large uploads on this surface

STATIC_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}

# -----------------------------------------------------------------------------
# The agent picker's list (PRD §5 "a visible assign control opening the agent
# picker"; the ledger refuses an empty agent name). Grace has no Hermes runtime on
# this computer, so this deployment can only *declare* a catalogue: it is not a
# capability probe and it is labelled as a mock value. Agents already named in the
# ledger (by a rule or an earlier assignment) are always added to the list, and the
# endpoint reports both sources so the UI can say where each name came from.
# CONTRACT GAP: the adapter capability manifest has no "available agents" entry, so
# Gate 2/3 cannot yet replace this catalogue with discovered Hermes agents.
# -----------------------------------------------------------------------------
AGENT_CATALOGUE: tuple[dict[str, str], ...] = (
    {"name": "researcher", "description": "Look up the facts the instruction needs, then draft a reply."},
    {"name": "accounts_agent", "description": "Check order, invoice and account state, then draft a reply."},
    {"name": "web_agent", "description": "Carry out a website or API task and report the evidence found."},
)
AGENT_CATALOGUE_NOTE = (
    "MOCK: this deployment has no Hermes runtime, so this catalogue is declared, not "
    "discovered. Gate 2 must probe the installed build for the agents it actually offers."
)

# -----------------------------------------------------------------------------
# Typed state -> the smallest next action (PRD §6 "Health presentation": include the
# affected capability, retry state and the smallest next action; R15: expose
# unsupported capabilities and failures). The keys are the contract's own vocabulary.
# -----------------------------------------------------------------------------
NEXT_ACTIONS: dict[str, str] = {
    "unsupported":
        "Nothing to do here: the adapter does not declare this capability. Record a probe "
        "result on Randy's Mac (Gate 2) before the feature can be offered.",
    "permission_denied":
        "Grant the permission in macOS System Settings ▸ Privacy & Security, then re-run the "
        "Gate 2 probe for this capability.",
    "offline":
        "Bring the source back online (start the desktop app or restore the network), then "
        "run `grace sync` for this account. Grace keeps the previous cursor, so nothing is lost.",
    "rate_limited":
        "Wait for the source's limit to clear, then retry the read with backoff. No writes are "
        "queued behind it.",
    "retryable_error":
        "Retry the read; it is a bounded read-only operation and is safe to repeat.",
    "permanent_error":
        "Fix the underlying content or configuration; repeating the operation will not help.",
    "partial":
        "Coverage is partial: read the stated gap reason, then continue bounded enumeration or "
        "accept the recorded bounds. Partial history is not an empty result.",
    "outcome_unknown":
        "Open the outbound operation and reconcile it against the source. Never dispatch it again.",
    "partial_history":
        "Coverage is partial: the adapter reached the end of one query, not the end of history. "
        "See the recorded gap reason before treating this list as complete.",
    "token_reset":
        "The source's change token was reset. Run a full re-enumeration for this scope; unrelated "
        "relationship data is kept.",
    "unknown":
        "Run the Gate 2 probe for this capability to establish its real state.",
}

DRAFT_STATE_LABELS = {
    "awaiting_review": "Result ready — review the draft",
    "awaiting_input": "Question returned — needs information",
    "awaiting_approval": "Approved — awaiting dispatch",
    "blocked": "Blocked",
}


def next_action_for(state: Optional[str], *, detail: Optional[str] = None) -> Optional[str]:
    if not state:
        return None
    return NEXT_ACTIONS.get(state)


# ---------------------------------------------------------------------- helpers --


def session_value(token: str) -> str:
    """Cookie value derived from the token, so the raw token is not stored client-side."""
    return hmac.new(token.encode("utf-8"), SESSION_SALT, hashlib.sha256).hexdigest()


def _mt(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _seconds_since(iso_ts: Optional[str]) -> Optional[int]:
    if not iso_ts:
        return None
    try:
        return max(0, int((C.now_dt() - C.parse_iso(iso_ts)).total_seconds()))
    except (ValueError, TypeError):
        return None


def human_age(iso_ts: Optional[str]) -> str:
    secs = _seconds_since(iso_ts)
    if secs is None:
        return "unknown"
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def send_state_view(effect: Optional[dict], receipt: Optional[dict]) -> dict:
    """What the effect ledger actually supports — never a hopeful green 'sent'."""
    if effect is None:
        return {"state": "none", "label": "No outbound operation", "is_green_sent": False,
                "requires_reconciliation": False, "verified_against_real_source": False,
                "detail": "Nothing has been dispatched for this conversation."}
    state = effect.get("effect_state")
    verification = (receipt or {}).get("verification_level")
    verified_real = bool((receipt or {}).get("verified_against_real_source"))
    labels = {
        EffectState.PREPARED: "Prepared — not submitted",
        EffectState.APPROVED: "Approved — not submitted",
        EffectState.DISPATCHING: "Submitting — outcome not yet known",
        EffectState.PROVIDER_ACCEPTED: "Accepted by the source (recorded locally)",
        EffectState.PROVIDER_PENDING: "Pending at the source",
        EffectState.CONFIRMED_SENT: "Confirmed recorded by the source",
        EffectState.FAILED: "Failed",
        EffectState.CANCELLED: "Cancelled",
        EffectState.OUTCOME_UNKNOWN: "Outcome unknown — reconcile before retrying",
    }
    green = (state == EffectState.CONFIRMED_SENT
             and verification == C.VerificationLevel.CONFIRMED_SENT
             and verified_real)
    detail = {
        EffectState.CONFIRMED_SENT: (
            "The source recorded the send and a read-back confirmed it."
            if verified_real else
            "The mock source recorded the send locally. This is not independent delivery "
            "evidence: verified_against_real_source is false."),
        EffectState.PROVIDER_PENDING: (
            "The source returned a pending id. Delivery is not claimed until reconciliation "
            "confirms it."),
        EffectState.PROVIDER_ACCEPTED: (
            "The source accepted the submission. No delivery confirmation is available from "
            "this source, and Grace does not upgrade acceptance into delivery."),
        EffectState.OUTCOME_UNKNOWN: (
            "Grace cannot tell whether the source received this. It must be reconciled; it is "
            "never retried blindly."),
        EffectState.DISPATCHING: (
            "Submission started and no outcome was recorded (for example a crash inside the "
            "dispatch window). Reconcile it."),
        EffectState.FAILED: "The source rejected or could not accept this operation.",
    }.get(state, "")
    return {
        "state": state,
        "label": labels.get(state, str(state)),
        "detail": detail,
        "is_green_sent": green,
        "requires_reconciliation": bool(effect.get("requires_reconciliation")),
        "verification_level": verification,
        "verified_against_real_source": verified_real,
        "provider_message_id": effect.get("provider_message_id"),
        "provider_status": effect.get("provider_status"),
        "actual_routed_destination": effect.get("actual_routed_destination"),
        "last_error_category": effect.get("last_error_category"),
        "updated_at": effect.get("updated_at"),
        "age": human_age(effect.get("updated_at")),
        "receipt_limitations": (receipt or {}).get("limitations"),
        "receipt_evidence": _json_list((receipt or {}).get("evidence_json")),
        "origin": effect.get("origin"),
        "mock_label": effect.get("mock_label"),
    }


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


# ------------------------------------------------------------------ web server --


class WebServer:
    """Wraps the Grace service in an authenticated HTTP surface.

    Single-threaded on purpose: one owner, one durable SQLite connection, and no
    request may interleave with another inside the ledger. Requests are fast local
    reads, so serializing them costs nothing and removes a whole class of races.
    """

    def __init__(self, db_path: str, token: str, *, host: str = "127.0.0.1", port: int = 8088,
                 scenario: Optional[str] = None, faults: Iterable[str] = (),
                 owner: str = "owner") -> None:
        if not token or len(token) < 16:
            raise ValueError(
                "refusing to start: a token of at least 16 characters is required "
                "(this surface has no unauthenticated mode)")
        self.db_path = str(db_path)
        self.token = token
        self._session = session_value(token)
        self.host = host
        self.port = int(port)
        self.scenario = scenario
        self.faults = tuple(faults)
        self.owner = owner
        self.ready = threading.Event()
        self.svc: Optional[Grace] = None
        self.httpd: Optional[HTTPServer] = None
        self.started_at: Optional[str] = None
        self.requests_served = 0

    # -- lifecycle ---------------------------------------------------------
    def serve(self) -> None:
        """Create the service *in this thread* and serve until shutdown.

        sqlite3 connections are single-thread, so the Grace instance is created by the
        same thread that runs the request loop.
        """
        self.svc = Grace(self.db_path, scenario=self.scenario, faults=self.faults,
                         owner=self.owner)
        self.started_at = C.now()
        handler = _make_handler(self)
        self.httpd = HTTPServer((self.host, self.port), handler)
        self.port = self.httpd.server_address[1]
        self.ready.set()
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        finally:
            self.httpd.server_close()
            if self.svc is not None:
                self.svc.close()
                self.svc = None

    def start_background(self) -> "WebServer":
        """Run ``serve()`` on a daemon thread and wait until it is accepting requests."""
        thread = threading.Thread(target=self.serve, name="grace-web", daemon=True)
        thread.start()
        self._thread = thread
        if not self.ready.wait(timeout=15):
            raise RuntimeError("web server did not become ready")
        return self

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(timeout=10)

    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _make_handler(app: WebServer) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to one WebServer instance."""
    _application = app  # a class body cannot close over a name it also assigns

    class Handler(BaseHTTPRequestHandler):
        server_version = "SwitchboardGrace/1.0"
        sys_version = ""
        # HTTP/1.0 deliberately: one request per connection, so a stalled or
        # half-closed socket can never leave a request waiting on a keep-alive loop
        # (the phone reconnects per view anyway, and this is a single-owner surface).
        protocol_version = "HTTP/1.0"
        app = _application

        # -- logging: never the query string (it can carry the token) ------
        def log_request(self, code: Any = "-", size: Any = "-") -> None:  # noqa: D102
            self._log(f"{self.command} {self._clean_path()} -> {code}")

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
            # Deliberately drops args: BaseHTTPRequestHandler would log the raw
            # request line, which may contain ?token=...
            self._log(self._clean_path())

        def log_error(self, fmt: str, *args: Any) -> None:  # noqa: D102
            self._log(self._clean_path())

        def _clean_path(self) -> str:
            return urlparse(self.path).path or "/"

        @staticmethod
        def _log(line: str) -> None:
            sys.stderr.write(f"[switchboard-web] {line}\n")
            sys.stderr.flush()

        # -- auth ----------------------------------------------------------
        def _cookie_token_ok(self) -> bool:
            raw = self.headers.get("Cookie", "") or ""
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME and value:
                    return hmac.compare_digest(value, self.app._session)
            return False

        def _header_token_ok(self) -> bool:
            supplied = self.headers.get(TOKEN_HEADER)
            if not supplied:
                auth = self.headers.get("Authorization", "") or ""
                if auth.lower().startswith("bearer "):
                    supplied = auth[7:].strip()
            return bool(supplied) and hmac.compare_digest(supplied, self.app.token)

        def _query_token_ok(self, query: dict) -> bool:
            supplied = (query.get("token") or [""])[0]
            return bool(supplied) and hmac.compare_digest(supplied, self.app.token)

        def _same_origin(self) -> bool:
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site in ("same-origin", "none"):
                return True
            origin = self.headers.get("Origin")
            if not origin:
                return False
            host = self.headers.get("Host") or ""
            return urlparse(origin).netloc == host

        def _authorised(self, query: dict, *, mutation: bool) -> bool:
            if self._header_token_ok():
                return True
            if self._cookie_token_ok() and (not mutation or self._same_origin()):
                return True
            if not mutation and self._query_token_ok(query):
                return True
            return False

        # -- responses -----------------------------------------------------
        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")

        def _send(self, code: int, body: bytes, ctype: str, *, extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self.app.requests_served += 1

        def _json(self, code: int, payload: Any, *, extra: Optional[dict] = None) -> None:
            body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
            self._send(code, body, "application/json; charset=utf-8", extra=extra)

        def _unauthorised(self) -> None:
            # One fixed body for every unauthorised case: missing token, wrong token,
            # expired session. It carries no ledger value of any kind.
            self._json(401, {
                "ok": False,
                "error": "unauthorized",
                "detail": ("This surface requires the owner's token. Open the URL printed by "
                           "`grace serve --print-url`, or send the token as a "
                           f"{TOKEN_HEADER} / Authorization: Bearer header."),
            }, extra={"WWW-Authenticate": 'Bearer realm="switchboard"'})

        def _error(self, code: int, reason: str, detail: str = "") -> None:
            self._json(code, {"ok": False, "error": reason, "detail": detail})

        # -- HTTP verbs ----------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle("HEAD")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._error(405, "method_not_allowed", "Only GET and POST are served here.")

        def do_DELETE(self) -> None:  # noqa: N802
            self._error(405, "method_not_allowed", "Only GET and POST are served here.")

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            mutation = method == "POST"
            if not self._authorised(query, mutation=mutation):
                self._unauthorised()
                return
            try:
                if method in ("GET", "HEAD"):
                    self._route_get(path, query)
                else:
                    self._route_post(path)
            except InjectedFault as fault:
                self._json(200, {
                    "ok": False, "error": "injected_fault", "where": fault.where,
                    "detail": str(fault),
                    "recovery": ("The operation row is persisted and requires reconciliation. "
                                 "Open the effect and reconcile it; do not dispatch again."),
                })
            except Exception as exc:  # pragma: no cover - defensive; surfaced honestly
                self._log(f"error handling {method} {path}: {type(exc).__name__}: {exc}")
                self._error(500, "internal_error", "Grace could not complete this request.")

        # -- GET routing ---------------------------------------------------
        def _route_get(self, path: str, query: dict) -> None:
            if path in STATIC_ROUTES:
                self._static(path, query)
                return
            if path == "/api/ping":
                self._json(200, {"ok": True, "service": "grace-web"})
                return
            if path == "/api/overview":
                self._json(200, self.app.page_overview())
                return
            if path == "/api/needs-me":
                self._json(200, self.app.page_view("needs_me"))
                return
            if path == "/api/working":
                self._json(200, self.app.page_view("working"))
                return
            if path == "/api/all":
                self._json(200, self.app.page_view(
                    "all", q=(query.get("q") or [""])[0],
                    state=(query.get("state") or ["all"])[0]))
                return
            if path == "/api/rules-source-health":
                self._json(200, self.app.page_rules_health())
                return
            match = re.fullmatch(r"/api/conversation/([^/]+)", path)
            if match:
                # ?job=<job_id> scopes the review page to ONE work item. A conversation may
                # host several concurrent jobs and each has its own review page (T04/T05).
                job_id = (query.get("job") or [""])[0] or None
                payload = self.app.page_conversation(match.group(1), job_id)
                if payload is None:
                    self._error(404, "not_found",
                                "No such workspace conversation, or no such job in it.")
                else:
                    self._json(200, payload)
                return
            self._error(404, "not_found", "No such path.")

        def _static(self, path: str, query: dict) -> None:
            name, ctype = STATIC_ROUTES[path]
            asset = WEBUI_DIR / name
            if not asset.exists():  # pragma: no cover - packaging error
                self._error(500, "asset_missing", f"{name} is not installed")
                return
            body = asset.read_bytes()
            extra: dict[str, str] = {}
            if path in ("/", "/index.html") and self._query_token_ok(query):
                # Exchange the one-time ?token=... for a session cookie so the token does
                # not stay in the address bar, history or the Referer header.
                extra["Set-Cookie"] = (
                    f"{COOKIE_NAME}={self.app._session}; Path=/; HttpOnly; SameSite=Strict")
            self._send(200, body, ctype, extra=extra)

        # -- POST routing --------------------------------------------------
        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype not in ("application/json", ""):
                raise ValueError(f"unsupported content type {ctype!r}")
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"malformed JSON body: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("body must be a JSON object")
            return parsed

        def _route_post(self, path: str) -> None:
            try:
                body = self._body()
            except ValueError as exc:
                self._error(400, "bad_request", str(exc))
                return
            if path == "/api/seed":
                self._json(200, self.app.action_seed(body))
                return
            if path == "/api/assign":
                self._json(200, self.app.action_assign(body))
                return
            match = re.fullmatch(r"/api/jobs/([^/]+)/(run|input|cancel)", path)
            if match:
                job_id, action = match.group(1), match.group(2)
                self._json(200, getattr(self.app, f"action_job_{action}")(job_id, body))
                return
            match = re.fullmatch(r"/api/drafts/([^/]+)/(revise|approve|request-changes|reject)",
                                 path)
            if match:
                draft_id, action = match.group(1), match.group(2).replace("-", "_")
                self._json(200, getattr(self.app, f"action_draft_{action}")(draft_id, body))
                return
            match = re.fullmatch(r"/api/approvals/([^/]+)/dispatch", path)
            if match:
                self._json(200, self.app.action_dispatch(match.group(1), body))
                return
            match = re.fullmatch(r"/api/effects/([^/]+)/(reconcile|retry)" , path)
            if match:
                effect_id, action = match.group(1), match.group(2)
                self._json(200, getattr(self.app, f"action_effect_{action}")(effect_id, body))
                return
            # Archive is a state, not a delete: these routes change a state in the ledger and
            # never remove a source row (PRD §8, R08).
            match = re.fullmatch(r"/api/conversations/([^/]+)/(archive|unarchive|delete|restore)",
                                 path)
            if match:
                ws_id, action = match.group(1), match.group(2)
                self._json(200, getattr(self.app, f"action_conversation_{action}")(ws_id, body))
                return
            match = re.fullmatch(r"/api/jobs/([^/]+)/(archive|unarchive|delete|restore)", path)
            if match:
                job_id, action = match.group(1), match.group(2)
                self._json(200, getattr(self.app, f"action_job_{action}")(job_id, body))
                return
            self._error(404, "not_found", "No such path.")

    return Handler


# --------------------------------------------------------------- app: surfaces --


class _AppMixin:
    """Presentation layer over the Grace service. Mixed into WebServer below."""

    svc: Optional[Grace]

    # -- shared envelope ---------------------------------------------------
    def _envelope(self) -> dict:
        svc = self.svc
        assert svc is not None
        status = svc.health()
        return {
            "mocked": bool(status["mocked"]),
            "mock_label": status["mock_label"] or C.mock_label("web"),
            "mock_disclaimer": C.MOCK_DISCLAIMER,
            "verified_against_real_source": False,
            "labelling_note": ("Every value in this client came from the labelled mock adapters in "
                               "this repository, or from the application's own ledger. No Mail, "
                               "Beeper, Contacts or Hermes source was contacted."),
        }

    def _wrapped(self, payload: dict) -> dict:
        problems = C.find_unlabelled_mock({"_envelope": self._envelope(), "data": payload})
        if problems:
            return {"ok": False, "error": "labelling_defect", "labelling_problems": problems,
                    "detail": ("A mock value reached the client without a MOCK: label. The client "
                               "refuses to render it.")}
        return {"ok": True, **self._envelope(), "data": payload}

    # ------------------------------------------------------------- overview -
    def page_overview(self) -> dict:
        svc = self.svc
        assert svc is not None
        counts = svc.ledger.counts()
        sources = svc.ingest.source_health()
        states = self._disconnected_states(sources)
        return self._wrapped({
            "counts": counts,
            "count_meaning": {
                "needs_me": "Conversations with an application queue filter of needs_me: untriaged, "
                            "a returned question, a draft waiting for review, a blocked job or a "
                            "failure. Counted independently of Working and All (PRD §5).",
                "working": "Conversations with a durable assignment in flight. Independent count.",
                "all": "Every workspace conversation, including completed and application-hidden "
                       "items. Independent count.",
                "stalled_jobs": "Jobs whose lease expired while still marked running (they are "
                                "visible,  not silently running).",
            },
            "schema": svc.store.health(),
            "sources": sources,
            "coverage": svc.ingest.coverage(),
            "disconnected_states": states,
            "degraded": bool(states),
            "capability_honesty": {
                "real_sources_connected": False,
                "explanation": ("This deployment has no Mail, Beeper, Contacts or Hermes connection. "
                                "Those integrations are Gate 2/3 work on Randy's Mac and have not "
                                "been exercised here."),
            },
            "worker": {
                "kind": "mock",
                "note": MOCK_WORKER_NOTE,
                "label": C.mock_label("worker"),
            },
            "surfaces": ["needs_me", "working", "all", "rules_source_health"],
        })

    def _disconnected_states(self, sources: list[dict]) -> list[dict]:
        """Every source condition that needs the owner's attention, with its reason.

        Two things are reported side by side, because they are not the same fact:

        * the state **observed just now** by the adapter's health probe (``health_state``,
          with what was stored kept in ``stored_health_state``), and
        * the state **last recorded in the ledger**, when it differs and is itself a
          condition the owner should see — a revoked permission stays revoked while a
          source is offline, and neither fact may hide the other (PRD §6, §13, R09, T20).
        """
        out: list[dict] = []
        healthy = {"connected", "syncing", "current"}
        permission_ok = ("granted", "not_required")
        for account in sources:
            observed = account.get("health_state")
            stored = account.get("stored_health_state")
            conditions: list[tuple[str, str, str]] = []
            if observed not in healthy or account.get("permission_state") not in permission_ok:
                conditions.append((
                    observed,
                    account.get("health_detail") or f"source reports {observed}",
                    "observed just now (health probe of this run)"))
            if stored and stored != observed and (
                    stored not in healthy or account.get("permission_state") not in permission_ok):
                conditions.append((
                    stored,
                    account.get("stored_health_detail") or f"the last recorded state was {stored}",
                    "last state recorded in the ledger (not this run's probe)"))
            for state, reason, basis in conditions:
                out.append({
                    "kind": "source",
                    "adapter": account.get("adapter"),
                    "account_id": account.get("account_id"),
                    "display_name": account.get("display_name"),
                    "account_identity": account.get("account_identity"),
                    "state": state,
                    "reason": reason,
                    "basis": basis,
                    "observed_state": observed,
                    "stored_state": stored,
                    "next_action": next_action_for(state),
                    "permission_state": account.get("permission_state"),
                    "last_success_at": account.get("last_success_at"),
                    "freshness": human_age(account.get("last_success_at")),
                    "origin": account.get("origin"),
                    "mock_label": account.get("mock_label"),
                })
            if not conditions:
                continue
            for capability in account.get("capabilities", []):
                if capability.get("state") in ("ok",):
                    continue
                out.append({
                    "kind": "capability",
                    "adapter": account.get("adapter"),
                    "account_id": account.get("account_id"),
                    "display_name": account.get("display_name"),
                    "capability": capability.get("name"),
                    "state": capability.get("state"),
                    "reason": capability.get("limitation") or f"capability is {capability.get('state')}",
                    "next_action": next_action_for(capability.get("state")),
                    "probe_method": capability.get("probe_method"),
                    "origin": account.get("origin"),
                    "mock_label": account.get("mock_label"),
                })
        return out

    # ----------------------------------------------------------- the queues -
    def _items(self, rows: list[dict]) -> list[dict]:
        return [self._item(row, row.get("job")) for row in rows]

    def _item(self, ws: dict, job: Optional[dict] = None) -> dict:
        """One review item, scoped to ONE work item.

        ``job`` is the work item this row is about. Drafts, results, the latest outbound
        operation and its approval are all read through that job, so a conversation
        carrying two jobs shows two items with their own drafts instead of one item that
        can only ever show the newest (PRD §5, §12; T04/T05/T12).
        """
        svc = self.svc
        assert svc is not None
        ws_id = ws["ws_conv_id"]
        job_id = (job or {}).get("job_id")
        meta = _mt((ws.get("latest_message") or {}).get("minimal_metadata"))
        links = [self._source_link(link) for link in ws.get("source_links", [])]
        drafts = self._drafts_for(ws_id, job_id)
        draft = next((d for d in drafts if not d.get("superseded_by")), None)
        approval = self._approval_for(ws_id, draft, job_id=job_id)
        scope = "AND job_id = ? " if job_id else ""
        args: tuple = (ws_id, job_id) if job_id else (ws_id,)
        effect_row = svc.store.one(
            "SELECT * FROM effect_operation WHERE ws_conv_id = ? " + scope
            + "ORDER BY created_at DESC LIMIT 1", args)
        receipt = None
        if effect_row is not None:
            receipt = svc.store.one(
                "SELECT * FROM receipt WHERE effect_id = ? ORDER BY observed_at DESC LIMIT 1",
                (effect_row["effect_id"],))
        results = svc.store.all(
            "SELECT * FROM result WHERE ws_conv_id = ? " + scope + "ORDER BY version DESC LIMIT 3",
            args)
        latest_job = job if job is not None else ((ws.get("jobs") or [None])[0] or None)
        work_item_id = ws.get("work_item_id") or (
            f"{ws_id}::{job_id}" if job_id else f"{ws_id}::conversation")
        item = {
            "ws_conv_id": ws_id,
            "work_item_id": work_item_id,
            "job_id": job_id,
            "title": ws["title"],
            "association": ws["association"],
            "queue_state": ws["queue_state"],
            "review_state": ws["review_state"],
            "assignment_state": ws["assignment_state"],
            "needs_me_reason": ws["needs_me_reason"],
            "review_label": DRAFT_STATE_LABELS.get(ws["review_state"]),
            "reason": self._reason_text(ws, latest_job, draft, results, effect_row),
            "current_input_revision": ws["current_input_revision"],
            "updated_at": ws["updated_at"],
            "age": human_age(ws["updated_at"]),
            # Archive and deletion are two different facts, and both travel with every
            # item so a client can never render one as the other (PRD §8, R08).
            "archive_state": ws.get("archive_state") or C.ArchiveState.ACTIVE,
            "archived_at": ws.get("archived_at"),
            "archive_reason": ws.get("archive_reason"),
            "deletion_state": ws.get("deletion_state") or C.DeletionState.RETAINED,
            "deleted_at": ws.get("deleted_at"),
            "deletion_reason": ws.get("deletion_reason"),
            "retention_note": (RETENTION_NOTE
                               if (ws.get("archived_at") or ws.get("deleted_at")) else None),
            "source_links": links,
            "source_accounts": [link["account"] for link in links],
            "audience": [link["audience"] for link in links],
            "provider_boundaries_preserved": True,
            "latest_message": self._message_view((ws.get("latest_message") or {}), meta),
            "jobs": [self._job_brief(job) for job in ws.get("jobs", [])],
            "job": self._job_brief(latest_job) if latest_job else None,
            "latest_result": self._result_view(results[0]) if results else None,
            "results": [self._result_view(r) for r in results],
            "draft": draft,
            "drafts": drafts,
            "approval": approval,
            "effect": effect_row,
            "send_state": send_state_view(effect_row, receipt),
        }
        item["origin"] = ws.get("origin")
        item["mock_label"] = ws.get("mock_label")
        return item

    def _reason_text(self, ws: dict, job: Optional[dict], draft: Optional[dict],
                     results: list[dict], effect: Optional[dict]) -> str:
        reason = ws.get("needs_me_reason")
        if reason == "untriaged_message":
            return "Untriaged source message — no instruction has been given yet."
        if reason == "question":
            question = next((r for r in results if r["kind"] == "question"), None)
            return ("The agent returned a question before drafting: "
                    + (question["summary"] if question else "information needed"))
        if reason == "draft":
            return "A draft is ready for review."
        if reason == "approval":
            return "Approved and waiting to be dispatched."
        if reason == "failure":
            detail = next((j for j in [job] if j), None)
            return "Work failed and needs attention: " + (
                (job or {}).get("job_state", "failed"))
        if reason == "blocked":
            return "Blocked — the job cannot proceed without a decision."
        if reason == "uncertain_effect":
            return "An outbound operation's outcome is unknown; it needs reconciliation."
        if effect is not None and effect.get("requires_reconciliation"):
            return "An outbound operation still requires reconciliation."
        if ws["queue_state"] == QueueState.WORKING and job:
            return f"Assigned to {job.get('agent')} — {job.get('job_state')}"
        if draft and not draft.get("superseded_by"):
            return "A draft exists for this conversation."
        if ws["queue_state"] == QueueState.IDLE:
            return "No open work. Kept for context and history."
        return "No application state requires attention."

    def page_view(self, view: str, *, q: str = "", state: str = "all") -> dict:
        svc = self.svc
        assert svc is not None
        if view == "needs_me":
            rows = svc.needs_me()
            note = ("Items leave this queue only when an assignment succeeds (an application "
                    "queue filter). No source message is marked read, archived, muted or deleted.")
        elif view == "working":
            rows = svc.working()
            note = ("Assigned jobs with their state, agent, latest progress and age. Inspect, add "
                    "information or cancel — all recorded in the ledger.")
        else:
            rows = svc.all_conversations(limit=200)
            note = ("People, groups and preserved source threads, including completed and "
                    "application-hidden items. Hidden is not deleted; every provider id, account "
                    "and audience is preserved. Archived items are listed here and labelled "
                    "archived — archive is a state, not a deletion.")
        if view == "all" and state == "archived":
            rows = [r for r in rows if r.get("archived_at")]
        elif view == "all" and state != "all":
            rows = [r for r in rows if r["queue_state"] == state]
        items = self._items(rows)
        if view == "all" and q.strip():
            items = [item for item in items if self._matches(item, q)]
        counts = svc.ledger.counts()
        # Archived items are counted separately: they are outside needs_me/working by design,
        # and folding them into those totals would make the counts tell a different story.
        archived = svc.ledger.archived_count()
        sources = svc.ingest.source_health()
        return self._wrapped({
            "view": view,
            "note": note,
            "query": q,
            "state_filter": state,
            "items": items,
            "shown": len(items),
            "counts": counts,
            "counts_independent": True,
            "archived_counts": archived,
            "review_summary": self._review_summary(items),
            "disconnected_states": self._disconnected_states(sources),
            "empty_is_a_real_result": ("An empty list here comes from the durable ledger. If a source "
                                       "is unhealthy its typed state is listed beside the list rather "
                                       "than being rendered as an empty success (PRD §6)."),
        })

    @staticmethod
    def _review_summary(items: list[dict]) -> dict:
        summary = {"needs_me": 0, "drafts_ready": 0, "questions": 0, "blocked": 0,
                   "uncertain": 0, "untriaged": 0}
        for item in items:
            if item["queue_state"] == QueueState.NEEDS_ME:
                summary["needs_me"] += 1
            reason = item.get("needs_me_reason")
            if reason == "draft":
                summary["drafts_ready"] += 1
            elif reason == "question":
                summary["questions"] += 1
            elif reason == "uncertain_effect":
                summary["uncertain"] += 1
            elif reason == "untriaged_message":
                summary["untriaged"] += 1
            if item["review_state"] == "blocked" or item["job"] and item["job"]["job_state"] == "failed":
                summary["blocked"] += 1
            if item["send_state"]["requires_reconciliation"]:
                summary["uncertain"] += 1
        return summary

    @staticmethod
    def _matches(item: dict, q: str) -> bool:
        needle = q.strip().lower()
        haystack = [item["title"], item["association"]["id"], item["ws_conv_id"],
                    str(item.get("reason") or "")]
        for link in item["source_links"]:
            haystack += [link["namespaced_id"], link["adapter"], link["audience_text"],
                         link["account"]["account_identity"], link["account"]["display_name"]]
        message = item.get("latest_message") or {}
        haystack += [str(message.get("subject") or ""), str(message.get("sender_text") or ""),
                     str(message.get("snippet") or "")]
        for draft in item.get("drafts") or []:
            haystack += [str(draft.get("subject") or ""), str(draft.get("body") or "")[:400]]
        return any(needle in str(value).lower() for value in haystack if value)

    # ------------------------------------------------------------ fragments -
    def _source_link(self, link: dict) -> dict:
        svc = self.svc
        assert svc is not None
        account = svc.store.one(
            "SELECT a.* FROM source_conversation c JOIN source_account a "
            "ON a.account_id = c.account_id WHERE c.conv_id = ?", (link["conv_id"],))
        conv = svc.store.one("SELECT * FROM source_conversation WHERE conv_id = ?",
                             (link["conv_id"],))
        audience = _json_list(conv["audience_json"]) if conv else []
        return {
            "conv_id": link["conv_id"],
            "namespaced_id": link["namespaced_id"],
            "adapter": (conv or {}).get("adapter"),
            "relevance": link["relevance"],
            "audience_kind": link["audience_kind"],
            "audience": audience,
            "audience_text": ", ".join(str(a) for a in audience) or "(audience not recorded)",
            "provider_thread_id": (conv or {}).get("provider_thread_id"),
            "provider_chat_id": (conv or {}).get("provider_chat_id"),
            "provider_is_merged": bool((conv or {}).get("provider_is_merged")),
            "availability": link["availability"],
            "availability_reason": link["availability_reason"],
            "source_time_first": (conv or {}).get("source_time_first"),
            "source_time_last": link["source_time_last"],
            "freshness": human_age(link["source_time_last"]),
            "retrieval_pointer": (conv or {}).get("retrieval_pointer"),
            "account": {
                "account_id": (account or {}).get("account_id"),
                "account_identity": (account or {}).get("account_identity"),
                "display_name": (account or {}).get("display_name"),
                "adapter": (account or {}).get("adapter"),
                "host_role": (account or {}).get("host_role"),
                "health_state": (account or {}).get("health_state"),
                "health_detail": (account or {}).get("health_detail"),
                "permission_state": (account or {}).get("permission_state"),
                "last_success_at": (account or {}).get("last_success_at"),
                "freshness": human_age((account or {}).get("last_success_at")),
                "next_action": next_action_for((account or {}).get("health_state")),
            },
            "origin": link.get("origin"),
            "mock_label": link.get("mock_label"),
        }

    def _message_view(self, row: dict, meta: Optional[dict] = None) -> Optional[dict]:
        if not row:
            return None
        meta = meta if meta is not None else _mt(row.get("minimal_metadata"))
        sender = _mt(row.get("sender_json"))
        return {
            "msg_ref_id": row.get("msg_ref_id"),
            "source_time": row.get("source_time"),
            "ingested_at": row.get("ingested_at"),
            "read_state": row.get("read_state"),
            "hidden_state": row.get("hidden_state"),
            "mute_state": row.get("mute_state"),
            "availability": row.get("availability"),
            "availability_reason": row.get("availability_reason"),
            "body_state": row.get("body_state"),
            "subject": meta.get("subject"),
            "network": meta.get("network"),
            "low_priority": meta.get("low_priority"),
            "unread": meta.get("unread"),
            "sender": sender,
            "sender_text": sender.get("display_name") or sender.get("address") \
                or sender.get("network_identity") or "unknown sender",
            "snippet": meta.get("preview") or meta.get("snippet"),
            "body_note": ("Bodies are fetched on demand (PRD §8): Grace stores a retrieval "
                          "reference, not a permanent copy. body_state="
                          + str(row.get("body_state"))),
            "origin": row.get("origin"),
            "mock_label": row.get("mock_label"),
        }

    @staticmethod
    def _job_brief(job: Optional[dict]) -> Optional[dict]:
        if not job:
            return None
        brief = dict(job)
        brief["age"] = human_age(job.get("updated_at"))
        brief["age_created"] = human_age(job.get("created_at"))
        brief["lease"] = job.get("lease") or {}
        lease = brief["lease"]
        if lease.get("stalled"):
            brief["progress"] = ("Lease expired — the worker stopped renewing it. The job is "
                                 "visible and recoverable, not invisibly running.")
        else:
            brief["progress"] = f"state {job.get('job_state')}, attempt " \
                                f"{job.get('attempt_count', 0)}, lease " \
                                f"{lease.get('state', 'none')}"
        return brief

    @staticmethod
    def _result_view(row: dict) -> dict:
        return {
            "result_id": row["result_id"],
            "version": row["version"],
            "kind": row["kind"],
            "summary": row["summary"],
            "detail": _mt(row.get("detail_json")),
            "evidence": _json_list(row.get("evidence_json")),
            "created_at": row["created_at"],
            "age": human_age(row["created_at"]),
            "origin": row.get("origin"),
            "mock_label": row.get("mock_label"),
        }

    def _drafts_for(self, ws_id: str, job_id: Optional[str] = None) -> list[dict]:
        svc = self.svc
        assert svc is not None
        if job_id:
            # This work item's own drafts, not every draft in the conversation.
            rows = svc.store.all(
                "SELECT draft_id FROM draft WHERE ws_conv_id = ? AND job_id = ? "
                "ORDER BY version DESC, created_at DESC LIMIT 6", (ws_id, job_id))
        else:
            rows = svc.store.all(
                "SELECT draft_id FROM draft WHERE ws_conv_id = ? ORDER BY version DESC, created_at DESC "
                "LIMIT 6", (ws_id,))
        return [self._draft_view(row["draft_id"]) for row in rows]

    def _draft_view(self, draft_id: str) -> dict:
        svc = self.svc
        assert svc is not None
        view = svc.effects.draft_view(draft_id)
        if not view:
            return {}
        conv = svc.store.one("SELECT * FROM source_conversation WHERE conv_id = ?",
                             (view["destination"]["conv_id"],))
        account = svc.store.one("SELECT * FROM source_account WHERE account_id = ?",
                                (view["account_id"],))
        snapshot = view.get("recipients") or {}
        view["destination_view"] = {
            "conv_id": view["destination"]["conv_id"],
            "provider_id": view["destination"]["provider_id"],
            "provider_thread_id": (conv or {}).get("provider_thread_id"),
            "provider_chat_id": (conv or {}).get("provider_chat_id"),
            "adapter": (conv or {}).get("adapter"),
            "audience_kind": (conv or {}).get("audience_kind"),
            "namespaced_id": (conv or {}).get("namespaced_id"),
        }
        view["sender_account"] = {
            "account_id": view["account_id"],
            "identity": view["sender_identity"],
            "display_name": (account or {}).get("display_name"),
            "adapter": (account or {}).get("adapter"),
            "host_role": (account or {}).get("host_role"),
            "health_state": (account or {}).get("health_state"),
            "permission_state": (account or {}).get("permission_state"),
        }
        # Every recipient, never truncated (PRD §5 "Mobile and accessibility").
        view["recipient_lines"] = self._recipient_lines(snapshot)
        view["audience_snapshot"] = snapshot
        view["mode_label"] = {"reply": "Reply to sender",
                              "reply_all": "Reply all",
                              "new": "New message"}.get(view.get("mode"), view.get("mode"))
        view["approval_effect"] = None
        approval = svc.store.one(
            "SELECT * FROM approval WHERE draft_id = ? ORDER BY issued_at DESC LIMIT 1", (draft_id,))
        if approval is not None:
            view["approval_effect"] = {
                "approval_id": approval["approval_id"],
                "approval_state": approval["approval_state"],
                "invalidated_at": approval["invalidated_at"],
                "invalidation_reason": approval["invalidation_reason"],
                "expires_at": approval["expires_at"],
                "consumed_at": approval["consumed_at"],
            }
        return view

    @staticmethod
    def _recipient_lines(snapshot: dict) -> list[dict]:
        """Flatten the audience snapshot into one untruncated line per recipient."""
        lines: list[dict] = []

        def add(role: str, entry: Any) -> None:
            if isinstance(entry, str):
                lines.append({"role": role, "text": entry, "address": entry})
                return
            if isinstance(entry, dict):
                text = (entry.get("display_name") or entry.get("address")
                        or entry.get("network_identity") or entry.get("identity")
                        or json.dumps(entry, sort_keys=True, default=str))
                if entry.get("display_name") and (entry.get("address")
                                                  or entry.get("network_identity")):
                    text = f"{entry['display_name']} <{entry.get('address') or entry.get('network_identity')}>"
                lines.append({"role": role, "text": text,
                              "address": entry.get("address") or entry.get("network_identity"),
                              "detail": entry})

        for key, role in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc"),
                          ("members", "Group member"), ("audience", "Audience"),
                          ("participants", "Participant")):
            value = snapshot.get(key)
            if isinstance(value, list):
                for entry in value:
                    add(role, entry)
            elif value:
                add(role, value)
        if not lines:
            for key, value in sorted(snapshot.items()):
                if key in ("membership_version", "reviewer_note"):
                    continue
                if isinstance(value, list):
                    for entry in value:
                        add(key, entry)
                elif value:
                    add(key, value)
        return lines

    def _approval_for(self, ws_id: str, draft: Optional[dict],
                      *, job_id: Optional[str] = None) -> Optional[dict]:
        svc = self.svc
        assert svc is not None
        row = None
        if draft:
            row = svc.store.one(
                "SELECT * FROM approval WHERE draft_id = ? ORDER BY issued_at DESC LIMIT 1",
                (draft["draft_id"],))
        if row is None:
            if job_id:
                row = svc.store.one(
                    "SELECT a.* FROM approval a JOIN draft d ON d.draft_id = a.draft_id "
                    "WHERE d.ws_conv_id = ? AND d.job_id = ? ORDER BY a.issued_at DESC LIMIT 1",
                    (ws_id, job_id))
            else:
                row = svc.store.one(
                    "SELECT a.* FROM approval a JOIN draft d ON d.draft_id = a.draft_id "
                    "WHERE d.ws_conv_id = ? ORDER BY a.issued_at DESC LIMIT 1", (ws_id,))

        if row is None:
            return None
        view = svc.effects.approval_view(row["approval_id"])
        # The review screen shows the *whole* binding, so the approval's immutable draft
        # version key and operation ID travel with the bound block, and the audience
        # hash is named for what it hashes: the recipient snapshot (PRD §10, T13).
        bound = dict(view.get("bound") or {})
        hashes = dict(bound.get("hashes") or {})
        hashes.setdefault("recipients", hashes.get("audience"))
        bound["hashes"] = hashes
        bound.setdefault("draft_version_key", view.get("draft_version_key"))
        bound.setdefault("operation_id", view.get("operation_id"))
        bound["approval_id"] = view.get("approval_id")
        bound["owner"] = view.get("owner")
        bound["scope"] = view.get("scope")
        view["bound"] = bound
        view["state_binding"] = {
            "invalid": row["approval_state"] not in (ApprovalState.GRANTED,),
            "reason": row["invalidation_reason"],
            "expires_at": row["expires_at"],
            "expired": C.is_past(row["expires_at"]),
        }
        view["recipient_lines"] = self._recipient_lines(view.get("bound", {}).get("recipients") or {})
        return view

    # -------------------------------------------------------- conversation --
    def page_conversation(self, ws_id: str, job_id: Optional[str] = None) -> Optional[dict]:
        """One conversation's review page, scoped to ONE work item when a job is named.

        A conversation may host several concurrent jobs (T04). Without ``job_id`` the page
        describes the conversation's aggregate state and lists every job; with ``job_id``
        the drafts, results, effect and approval shown are that job's own. An unknown or
        foreign job id is refused rather than silently falling back to the newest job.
        """
        svc = self.svc
        assert svc is not None
        ws_row = svc.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?",
                               (ws_id,))
        if ws_row is None:
            return None
        ws = svc.ledger._ws_brief(ws_row)
        job_row = None
        if job_id:
            job_row = svc.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
            if job_row is None or job_row["ws_conv_id"] != ws_id:
                return None
            ws["queue_state"] = job_row["queue_state"]
            ws["review_state"] = job_row["review_state"]
            ws["needs_me_reason"] = job_row["needs_me_reason"]
            ws["work_item_id"] = f"{ws_id}::{job_id}"
        item = self._item(ws, _job_brief(job_row, ws_row) if job_row else None)

        source_conversations = []
        for link in ws["source_links"]:
            conv = svc.store.one("SELECT * FROM source_conversation WHERE conv_id = ?",
                                 (link["conv_id"],))
            messages = svc.store.all(
                "SELECT * FROM message_ref WHERE conv_id = ? ORDER BY source_time", (link["conv_id"],))
            source_conversations.append({
                **self._source_link(link),
                "messages": [self._message_view(m) for m in messages],
                "message_count": len(messages),
                "minimal_metadata": _mt((conv or {}).get("minimal_metadata")),
                "revision": (conv or {}).get("revision"),
            })
        attachments = svc.store.all(
            "SELECT * FROM attachment_ref WHERE msg_ref_id IN "
            "(SELECT msg_ref_id FROM message_ref WHERE conv_id IN "
            "(SELECT conv_id FROM workspace_source_link WHERE ws_conv_id = ?))", (ws_id,))
        jobs = []
        for job in ws["jobs"]:
            detail = svc.ledger.job_detail(job["job_id"]) or {}
            jobs.append({
                **self._job_brief(job),
                "capability_plan": detail.get("capability_plan"),
                "checkpoint": detail.get("checkpoint"),
                "stall_count": detail.get("stall_count"),
                "inputs": detail.get("inputs", []),
                "results": [self._result_view(r) for r in detail.get("results", [])],
                "transitions": detail.get("transitions", []),
                "attempts": detail.get("attempts", []),
                "session_bindings": detail.get("session_bindings", []),
                "delivered_to_worker": any(i.get("delivered_to_worker_at")
                                           for i in detail.get("inputs", [])),
            })
        effect = None
        receipt = None
        if item["effect"] is not None:
            effect = svc.effects.effect_view(item["effect"]["effect_id"])
            receipt = effect["receipts"][-1] if effect.get("receipts") else None
        return self._wrapped({
            "conversation": item,
            "retention": {
                "storage_state": "retained" if item["deletion_state"] == "retained" else "retained_tombstone",
                "archive_state": item["archive_state"],
                "deletion_state": item["deletion_state"],
                "note": item["retention_note"] or (
                    "Active: not archived and not deleted. Nothing here removes or rewrites "
                    "anything in the source apps."),
                "source_rows_deleted": False,
                "recoverable": True,
            },
            "review_item": {"work_item_id": item["work_item_id"], "job_id": item["job_id"],
                            "queue_state": item["queue_state"],
                            "review_state": item["review_state"],
                            "needs_me_reason": item["needs_me_reason"],
                            "note": ("This review page is scoped to this work item. Other jobs in "
                                     "this conversation keep their own state and their own drafts."
                                     if item["job_id"] else
                                     "No job was named, so this page shows the conversation's own "
                                     "aggregate state and every job it hosts.")},
            "source_conversations": source_conversations,
            "source_attachments": attachments,
            "jobs": jobs,
            "drafts": item["drafts"],
            "approval": item["approval"],
            "effect": effect,
            "send_state": send_state_view(item["effect"], receipt),
            "source_evidence": self._source_evidence(item, source_conversations, jobs),
            "operational_history": self._operational_history(ws_id),
            "agents": self._agents(),
            "low_level_log": self._low_level_log(ws_id),
            "privacy_note": ("This history records what the application and the ledger did. Grace "
                             "stores no private model reasoning and shows none here."),
        })

    def _source_evidence(self, item: dict, sources: list[dict], jobs: list[dict]) -> list[dict]:
        evidence: list[dict] = []
        for source in sources:
            evidence.append({
                "kind": "source_conversation",
                "ref": source["namespaced_id"],
                "detail": (f"{source['adapter']} via {source['account']['account_identity']} "
                           f"({source['account']['display_name']}), audience "
                           f"{source['audience_kind']}: {source['audience_text']}"),
                "observed_at": source["source_time_last"],
                "availability": source["availability"],
                "limitation": source["availability_reason"],
                "origin": source["origin"],
                "mock_label": source["mock_label"],
            })
        for job in jobs:
            for result in job.get("results", []):
                for entry in result.get("evidence", []):
                    evidence.append({"kind": "result_evidence", "ref": result["result_id"],
                                     "detail": entry if isinstance(entry, str)
                                               else json.dumps(entry, sort_keys=True, default=str),
                                     "observed_at": result["created_at"],
                                     "origin": result.get("origin"),
                                     "mock_label": result.get("mock_label")})
        return evidence

    OPERATION_TEXT = {
        "create_job": "Instruction stored and a job created",
        "grant_approval": "Owner approved an immutable draft version",
        "revise_draft": "Owner edited the draft — bound approvals invalidated",
        "create_draft": "Draft version created",
        "effect_dispatching": "Dispatch started (approval consumed)",
        "lease_expired": "Lease expired — stalled job made visible",
        "attempt_finished": "Worker attempt closed",
        "approval_invalidated": "An approval stopped being valid — dispatch is refused until re-approval",
        "effect_reconcile": "Outbound operation reconciled against the source",
        "effect_retry": "Owner retried a definite pre-submission failure",
        "request_changes": "Owner asked the agent to change the draft",
        "draft_rejected": "Owner rejected the draft — nothing was sent",
        "duplicate_event": "Duplicate source event ignored (dedup key already recorded)",
    }

    # Audit rows that belong to this workspace conversation. An audit row's entity is
    # the row the operation touched, so it can be a job, a draft, an approval or an
    # effect; all four are reachable from the workspace conversation.
    _WS_AUDIT_SQL = (
        "SELECT * FROM audit_event WHERE entity_id = ? "
        "OR entity_id IN (SELECT job_id FROM job WHERE ws_conv_id = ?) "
        "OR entity_id IN (SELECT draft_id FROM draft WHERE ws_conv_id = ?) "
        "OR entity_id IN (SELECT a.approval_id FROM approval a JOIN draft d ON d.draft_id = "
        "a.draft_id WHERE d.ws_conv_id = ?) "
        "OR entity_id IN (SELECT effect_id FROM effect_operation WHERE ws_conv_id = ?) "
        "ORDER BY at")

    def _ws_audit_args(self, ws_id: str) -> tuple:
        return (ws_id,) * 5

    def _operational_history(self, ws_id: str) -> list[dict]:
        svc = self.svc
        assert svc is not None
        history: list[dict] = []
        for row in svc.store.all(
                "SELECT * FROM job_transition WHERE job_id IN "
                "(SELECT job_id FROM job WHERE ws_conv_id = ?) ORDER BY at", (ws_id,)):
            history.append({
                "at": row["at"], "kind": "job_state", "actor": row["actor"],
                "text": (f"Job {'→'.join(x for x in (row['from_state'], row['to_state']) if x)}: "
                         f"{row['reason']}"),
                "ref": row["job_id"], "version": row["resulting_version"],
            })
        for row in svc.store.all(
                "SELECT * FROM effect_attempt WHERE effect_id IN "
                "(SELECT effect_id FROM effect_operation WHERE ws_conv_id = ?) ORDER BY started_at",
                (ws_id,)):
            submitted = "after submission" if row["submitted"] else "before submission"
            history.append({
                "at": row["started_at"], "kind": "effect_attempt", "actor": "service",
                "text": (f"Dispatch attempt {row['attempt_no']} ({submitted}) → "
                         f"{row['outcome_code']}"
                         + (f" [{row['error_category']}]" if row["error_category"] else "")
                         + (" — retry allowed under the same authorization"
                            if row["retry_allowed"] else "")),
                "ref": row["effect_id"],
            })
        for row in svc.store.all(self._WS_AUDIT_SQL, self._ws_audit_args(ws_id)):
            if row["operation"] not in self.OPERATION_TEXT:
                continue
            history.append({
                "at": row["at"], "kind": "audit", "actor": row["actor"],
                "text": self.OPERATION_TEXT[row["operation"]],
                "ref": row["entity_id"], "reason": row["reason"],
                "detail": _mt(row.get("details_json")),
            })
        for row in svc.store.all(
                "SELECT * FROM receipt WHERE effect_id IN "
                "(SELECT effect_id FROM effect_operation WHERE ws_conv_id = ?) "
                "ORDER BY observed_at", (ws_id,)):
            history.append({
                "at": row["observed_at"], "kind": "receipt", "actor": "service",
                # The column is a 0/1 flag in the ledger; the review screen says the word, so
                # the sentence is readable and unambiguous ("this receipt was not verified
                # against a real source") rather than a bare "=0" (PRD §10, T15).
                "text": (f"Receipt: state {row['effect_state']}, verification "
                         f"{row['verification_level']}, delivery {row['delivery_state']}, "
                         f"verified_against_real_source="
                         f"{bool(row['verified_against_real_source'])}"),
                "ref": row["effect_id"], "limitations": row["limitations"],
            })
        history.sort(key=lambda entry: entry["at"])
        return history[-40:]

    def _low_level_log(self, ws_id: str) -> list[dict]:
        """Raw ledger rows, rendered collapsed *after* the human-readable summary."""
        svc = self.svc
        assert svc is not None
        rows = svc.store.all(
            "SELECT audit_id, at, actor, operation, entity_kind, entity_id, reason, "
            "version_before, version_after, operation_id, details_json, secret_redacted "
            "FROM audit_event WHERE entity_id = ? "
            "OR entity_id IN (SELECT job_id FROM job WHERE ws_conv_id = ?) "
            "OR entity_id IN (SELECT draft_id FROM draft WHERE ws_conv_id = ?) "
            "OR entity_id IN (SELECT a.approval_id FROM approval a JOIN draft d ON d.draft_id = "
            "a.draft_id WHERE d.ws_conv_id = ?) "
            "OR entity_id IN (SELECT effect_id FROM effect_operation WHERE ws_conv_id = ?) "
            "ORDER BY at DESC LIMIT 60", self._ws_audit_args(ws_id))
        return rows

    def _agents(self) -> dict:
        svc = self.svc
        assert svc is not None
        observed = set()
        for table, column in (("job", "agent"), ("rule", "agent"), ("draft", "author")):
            for row in svc.store.all(
                    f"SELECT DISTINCT {column} AS name FROM {table} "
                    f"WHERE {column} IS NOT NULL AND {column} <> ''"):
                observed.add(row["name"])
        catalogue = {entry["name"]: dict(entry, source="declared_catalogue")
                     for entry in AGENT_CATALOGUE}
        for name in sorted(observed):
            entry = catalogue.setdefault(name, {"name": name, "description": ""})
            entry["source"] = "named_in_ledger"
        return {
            "items": [catalogue[name] for name in sorted(catalogue)],
            "note": AGENT_CATALOGUE_NOTE,
            "mock_label": C.mock_label("agents"),
            "labelled": True,
        }

    # ------------------------------------------------ rules and source health -
    def page_rules_health(self) -> dict:
        svc = self.svc
        assert svc is not None
        rules = svc.store.all("SELECT * FROM rule ORDER BY rule_id, version DESC")
        rule_views = []
        for rule in rules:
            runs = svc.store.all(
                "SELECT * FROM rule_run WHERE rule_id = ? AND rule_version = ? "
                "ORDER BY created_at DESC", (rule["rule_id"], rule["version"]))
            run_views = []
            for run in runs:
                items = svc.store.all(
                    "SELECT * FROM rule_run_item WHERE rule_run_id = ? ORDER BY evaluated_at",
                    (run["rule_run_id"],))
                outcomes: dict[str, int] = {}
                for item in items:
                    outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + 1
                run_views.append({
                    **run,
                    "preview_bounds": _mt(run.get("preview_bounds")),
                    "items": items[:100],
                    "item_count_shown": min(len(items), 100),
                    "outcomes": outcomes,
                })
            rule_views.append({
                **rule,
                "scope": _mt(rule.get("scope_json")),
                "conditions": _mt(rule.get("conditions_json")),
                "authorization": rule.get("authorization_ref") or "none — drafting only, no send authority",
                "versions": [r["version"] for r in rules if r["rule_id"] == rule["rule_id"]],
                "runs": run_views,
                "send_authority": False,
            })
        sources = svc.ingest.source_health()
        return self._wrapped({
            "rules": rule_views,
            "rule_count": len(rule_views),
            "rule_note": ("Rules are versioned and inspectable. A rule can create drafting work; it "
                          "cannot grant standing send authority (R10, R14, PRD §7)."),
            "sources": sources,
            "health_axes": {
                "order": list(Ingest.AXES),
                "labels": {
                    "transport": "Transport — can this source be reached at all?",
                    "freshness": "Freshness — when was it last observed?",
                    "coverage": "Coverage — is the history complete, and if not, why not?",
                },
                "healthy_states": {k: list(v) for k, v in Ingest.AXIS_HEALTHY.items()},
                "note": ("Three separate axes, each with its own typed state and its own reason. An "
                         "axis whose value is unknown reads 'unknown' and is never green; no axis "
                         "borrows another's good news. The state last written to the ledger is shown "
                         "as a further labelled axis, never blended into these three (PRD §6, R09)."),
                "severities": ["ok", "warn", "danger", "unknown"],
                "unknown_is_not_green": True,
            },
            "coverage": svc.ingest.coverage(),
            "disconnected_states": self._disconnected_states(sources),
            "archived_counts": svc.ledger.archived_count(),
            "freshness_note": ("Observed freshness is the source's own last success time as recorded "
                               "by this deployment. The expected polling cadence is not declared by "
                               "the capability contract, so this client shows observed freshness "
                               "only rather than inventing a schedule."),
            "adapters": [a.describe() for a in svc.adapters.values()],
            "manifest_note": ("A capability stays unsupported until a probe on Randy's Mac confirms "
                              "it (PRD §11, Gate 2). Nothing here has been probed."),
        })

    # ----------------------------------------------------------- mutations --
    def _result_payload(self, res: Any, *, extra: Optional[dict] = None) -> dict:
        payload = {"ok": bool(res.ok), "code": res.code, "detail": res.detail,
                   "result": res.to_dict()}
        if extra:
            payload.update(extra)
        if not res.ok:
            payload["mocked"] = bool(getattr(res, "mocked", False))
            payload["mock_label"] = getattr(res, "label", None) or C.mock_label("service")
        return payload

    def action_seed(self, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        res = svc.seed(reset=True)
        return self._result_payload(res, extra={
            "mocked": True, "mock_label": C.mock_label("fixtures"),
            "data": {"counts": svc.ledger.counts(), "fixtures": res.data},
            "note": "Deterministic labelled fixtures reloaded. Nothing was read from a real source.",
        })

    def action_assign(self, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        ws_conv_id = str(body.get("ws_conv_id") or "")
        instruction = str(body.get("instruction") or "")
        agent = str(body.get("agent") or "")
        operation_id = body.get("operation_id") or None
        before = svc.store.one(
            "SELECT msg_ref_id, read_state, hidden_state, mute_state, availability FROM message_ref "
            "WHERE conv_id IN (SELECT conv_id FROM workspace_source_link WHERE ws_conv_id = ?) "
            "ORDER BY source_time DESC LIMIT 1", (ws_conv_id,))
        res = svc.assign(ws_conv_id, instruction, agent, operation_id=operation_id)
        after = svc.store.one(
            "SELECT msg_ref_id, read_state, hidden_state, mute_state, availability FROM message_ref "
            "WHERE conv_id IN (SELECT conv_id FROM workspace_source_link WHERE ws_conv_id = ?) "
            "ORDER BY source_time DESC LIMIT 1", (ws_conv_id,))
        published = svc.ledger.publish_outbox() if res.ok else None
        extra = {
            "source_untouched": before == after,
            "source_state_before": before,
            "source_state_after": after,
            "source_note": ("Assignment is an application queue filter only (PRD §5): the source "
                            "message is never marked read, never archived, never muted, never "
                            "deleted and never otherwise mutated — the provider's own state is "
                            "read-only here."),
            "published_outbox": published.data["published"] if published else [],
        }
        if res.ok and res.data:
            extra["data"] = {
                **res.data,
                "job": self._job_brief(svc.ledger.job_detail(res.data["job_id"])),
                "counts": svc.ledger.counts(),
            }
        return self._result_payload(res, extra=extra)

    def action_job_run(self, job_id: str, body: dict) -> dict:
        """The simulated worker pass. Labelled MOCK everywhere it appears."""
        svc = self.svc
        assert svc is not None
        res = svc.run_job(job_id,
                          mode=str(body.get("mode") or "reply"),
                          destination_conv_id=body.get("destination_conv_id") or None,
                          draft_body=body.get("draft_body") or None,
                          question=body.get("question") or None,
                          fail_with=body.get("fail_with") or None,
                          stall=bool(body.get("stall")))
        return self._result_payload(res, extra={
            "mocked": True, "mock_label": res.label or C.mock_label("worker"),
            "data": res.data,
            "worker_note": MOCK_WORKER_NOTE,
            "disclaimer": C.MOCK_DISCLAIMER,
        })

    def action_job_input(self, job_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        kind = str(body.get("kind") or "information")
        content = str(body.get("content") or "")
        if kind not in ("answer", "follow_up", "information"):
            return {"ok": False, "code": "invalid",
                    "detail": "kind must be answer, follow_up or information"}
        if not content.strip():
            return {"ok": False, "code": "invalid", "detail": "nothing to add"}
        res = svc.ledger.append_input(job_id, kind=kind, content=content,
                                      operation_id=body.get("operation_id") or None)
        return self._result_payload(res, extra={
            "note": ("Added as a new input version. A prompt that is already executing is never "
                     "mutated (PRD §5)." if res.ok else ""),
            "job": self._job_brief(svc.ledger.job_detail(job_id)),
        })

    def action_job_cancel(self, job_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        res = svc.cancel_job(job_id, str(body.get("reason") or "owner cancelled in the review client"))
        return self._result_payload(res, extra={"job": self._job_brief(svc.ledger.job_detail(job_id))})

    def action_draft_revise(self, draft_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        res = svc.effects.revise_draft(
            draft_id, body=body.get("body"), subject=body.get("subject"),
            mode=body.get("mode"), expected_version=body.get("expected_version"))
        extra = {"invalidates_approval": True,
                 "note": ("Editing any bound field creates a new immutable version and invalidates "
                          "any approval of the previous version (PRD §10, T13).")}
        if res.ok:
            extra["data"] = {**res.data, "draft": self._draft_view(res.data["draft"]["draft_id"])}
        return self._result_payload(res, extra=extra)

    def action_draft_approve(self, draft_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        acks = ["limitations_acknowledged"] if body.get("acknowledge_limitations") else []
        res = svc.effects.grant_approval(
            draft_id, operation_id=str(body.get("operation_id") or C.new_id("op")),
            ttl_seconds=int(body.get("ttl_seconds") or 900), acknowledgements=acks)
        return self._result_payload(res, extra={
            "binding": ("Bound to the owner, this immutable draft version, the sender identity, the "
                        "recipient snapshot, the destination, body and attachment hashes and the "
                        "operation ID (PRD §10).")})

    def action_draft_request_changes(self, draft_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        draft = svc.store.one("SELECT * FROM draft WHERE draft_id = ?", (draft_id,))
        if draft is None:
            return {"ok": False, "code": "not_found", "detail": "unknown draft"}
        note = str(body.get("note") or "please revise this draft")
        res = svc.ledger.append_input(draft["job_id"], kind="follow_up",
                                      content=f"Requested changes to draft v{draft['version']}: {note}"
                                              if draft["job_id"] else note,
                                      operation_id=body.get("operation_id") or None)
        requeued = False
        if res.ok and draft["job_id"]:
            current = svc.store.one("SELECT job_state FROM job WHERE job_id = ?", (draft["job_id"],))
            if current and current["job_state"] in (JobState.READY_FOR_REVIEW,
                                                    JobState.WAITING_FOR_APPROVAL):
                requeued = svc.ledger.transition(
                    draft["job_id"], JobState.QUEUED, actor="owner",
                    reason="owner requested changes to the draft").ok
        svc.store.audit(actor="owner", operation="request_changes", entity_kind="draft",
                        entity_id=draft_id, reason=note,
                        details={"draft_version": draft["version"], "requeued": requeued},
                        origin=draft["origin"], mock_label=draft["mock_label"])
        return self._result_payload(res, extra={
            "requeued": requeued,
            "note": ("Recorded as a follow-up turn on the host job; the draft stays visible until a "
                     "new version is produced. Nothing was sent."),
        })

    def action_draft_reject(self, draft_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        draft = svc.store.one("SELECT * FROM draft WHERE draft_id = ?", (draft_id,))
        if draft is None:
            return {"ok": False, "code": "not_found", "detail": "unknown draft"}
        reason = str(body.get("reason") or "owner rejected the draft in the review client")
        revoked = svc.effects.revoke_approvals_for_job(
            draft["job_id"], f"draft rejected: {reason}") if draft["job_id"] else []
        cancelled = None
        if draft["job_id"]:
            cancelled = svc.cancel_job(draft["job_id"], reason)
        svc.store.audit(actor="owner", operation="draft_rejected", entity_kind="draft",
                        entity_id=draft_id, reason=reason,
                        details={"draft_version": draft["version"],
                                 "revoked_approvals": revoked},
                        origin=draft["origin"], mock_label=draft["mock_label"])
        return {
            "ok": True, "code": "ok",
            "detail": "Draft rejected. Nothing was sent and no approval remains live.",
            "revoked_approvals": revoked,
            "job": self._job_brief(svc.ledger.job_detail(draft["job_id"]))
                   if draft["job_id"] else None,
            "job_cancelled": bool(cancelled and cancelled.ok),
            "note": ("Rejection is recorded in the operational history. Grace has no separate "
                     "draft-rejection record; it revokes the approval, stops the host job and "
                     "leaves the immutable draft readable. Reported as a contract gap."),
        }

    def action_dispatch(self, approval_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        res = svc.effects.dispatch(approval_id,
                                   inject_crash_after=body.get("inject_crash_after") or None)
        extra = {"mocked": True, "mock_label": C.mock_label("dispatch")}
        if res.data and res.data.get("effect"):
            effect = res.data["effect"]
            receipt = effect["receipts"][-1] if effect.get("receipts") else None
            extra["send_state"] = send_state_view(effect, receipt)
            extra["data"] = res.data
        return self._result_payload(res, extra=extra)

    def action_effect_reconcile(self, effect_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        res = svc.effects.reconcile(effect_id)
        extra = {"mocked": True, "mock_label": C.mock_label("reconcile")}
        if res.data and res.data.get("effect"):
            effect = res.data["effect"]
            receipt = effect["receipts"][-1] if effect.get("receipts") else None
            extra["send_state"] = send_state_view(effect, receipt)
            extra["data"] = res.data
        return self._result_payload(res, extra=extra)

    def action_effect_retry(self, effect_id: str, body: dict) -> dict:
        svc = self.svc
        assert svc is not None
        res = svc.effects.retry(effect_id)
        return self._result_payload(res, extra={
            "note": ("Only a definite pre-submission failure may be retried, under the same "
                     "authorization and the same idempotency key. An uncertain outcome is refused "
                     "and must be reconciled (PRD §10, R15).")})

    # -- archive and deletion: states, never row removals (PRD §8, R08) ----
    def _retention_payload(self, res: Any, body: dict, *, what: str) -> dict:
        extra = {
            "mocked": True, "mock_label": C.mock_label("retention"),
            "retention_note": RETENTION_NOTE,
            "data": res.data,
            "what": what,
            "reason": str(body.get("reason") or ""),
            "note": ("Nothing was removed from storage and nothing was changed in the source "
                     "apps. An archived item stays retrievable; a deleted one is an application "
                     "tombstone that is listed only when explicitly asked for."),
        }
        if res.ok and res.data:
            extra["archive_state"] = res.data.get("archive_state")
            extra["deletion_state"] = res.data.get("deletion_state")
        return self._result_payload(res, extra=extra)

    def _retention_run(self, action: str, ws_conv_id: Optional[str], job_id: Optional[str],
                       body: dict, *, what: str) -> dict:
        svc = self.svc
        assert svc is not None
        method = {
            "archive": svc.ledger.archive_item,
            "unarchive": svc.ledger.unarchive_item,
            "delete": svc.ledger.delete_item,
            "restore": svc.ledger.restore_item,
        }[action]
        res = method(ws_conv_id=ws_conv_id, job_id=job_id,
                     reason=str(body.get("reason") or ""), actor="owner")
        return self._retention_payload(res, body, what=what)

    def action_conversation_archive(self, ws_id: str, body: dict) -> dict:
        return self._retention_run("archive", ws_id, None, body,
                                   what=f"workspace conversation {ws_id}")

    def action_conversation_unarchive(self, ws_id: str, body: dict) -> dict:
        return self._retention_run("unarchive", ws_id, None, body,
                                   what=f"workspace conversation {ws_id}")

    def action_conversation_delete(self, ws_id: str, body: dict) -> dict:
        return self._retention_run("delete", ws_id, None, body,
                                   what=f"workspace conversation {ws_id}")

    def action_conversation_restore(self, ws_id: str, body: dict) -> dict:
        return self._retention_run("restore", ws_id, None, body,
                                   what=f"workspace conversation {ws_id}")

    def action_job_archive(self, job_id: str, body: dict) -> dict:
        return self._retention_run("archive", None, job_id, body, what=f"job {job_id}")

    def action_job_unarchive(self, job_id: str, body: dict) -> dict:
        return self._retention_run("unarchive", None, job_id, body, what=f"job {job_id}")

    def action_job_delete(self, job_id: str, body: dict) -> dict:
        return self._retention_run("delete", None, job_id, body, what=f"job {job_id}")

    def action_job_restore(self, job_id: str, body: dict) -> dict:
        return self._retention_run("restore", None, job_id, body, what=f"job {job_id}")



class WebApp(_AppMixin, WebServer):
    """The served client: WebServer wiring plus the presentation layer."""


def serve_forever(db_path: str, token: str, *, host: str = "127.0.0.1", port: int = 8088,
                  scenario: Optional[str] = None, faults: Iterable[str] = (),
                  owner: str = "owner") -> int:
    """Blocking entry point used by the ``serve`` CLI subcommand."""
    app = WebApp(db_path, token, host=host, port=port, scenario=scenario, faults=faults,
                 owner=owner)
    app.serve()
    return 0
