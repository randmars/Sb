"""The Hermes transport's wire and security surface. HALF ONE of two test files.

This file is deliberately the *transport* half, because the last two sessions ran out of
budget before the whole of it could be written. **Half two** (not written yet, see the
closing note of the module and the delegation report) is the probe-row half: the eight
Hermes capability rows, `probe --submit-test-run`'s row evidence, the session/execution/
approval/credential rows and `grace probe-import`.

**What is exercised here.** The *shipped* transport (``switchboard_mini.hermes_transport``)
against a **labelled loopback stand-in** — a real ``http.server`` socket on 127.0.0.1 that
answers only the paths a pack record describes. It is not Hermes, and there is no Hermes
gateway on this Linux computer: every document a transport produces against it carries
``responder: stand_in_http_server`` and ``source_contacted: false``, and the tests assert
that, so nothing here can be read as an observation of Randy's install. The recorded
fixtures under ``mini/switchboard_mini/fixtures/hermes/`` are exercised too, where the
transport's own narrowing must hold for a recorded answer as well.

**What only Randy's Mac can settle** (stated here so no test is mistaken for it): whether his
installed gateway answers these endpoints at all, what his ``max_concurrent_runs`` and
version are, and whether a real 409/429/403 arrives the way O17 describes. No test in this
file contacted a gateway, and none of them may claim to.

Requirements covered (from the PRD; O-numbers are Gate 2 probe-pack records):

* **R14 / T12** — every agent-authored external communication needs the owner's explicit
  approval, and the credential half of it: the bearer key never leaves the process
  environment. ``TestTheBearerKeyNeverLeavesTheEnvironment``.
* **T14** — a source that is not reachable is reported as a socket fact, never as a Mac
  reason. ``TestUnreachabilityIsSocketShapedNeverAMacReason``.
* **T15** — a failure state is named only when a record describes it.
  ``TestAStatusNoRecordDescribesIsUnexpectedStatus``.
* **R01/R15** — reads are built only against documented paths, parameters and purposes.
  ``TestOnlyDocumentedPathsAreEverSent``.
* **R14 (no unapproved outbound or executing action)** — there is no general run path.
  ``TestThereIsNoGeneralRunSubmission``.
* **R14/R15 (bounded work)** — a caller's bound is applied, and a template that would put a
  literal placeholder on the wire is refused before any socket work.
  ``TestBoundsAreAppliedAndPlaceholdersAreRefusedBeforeAnySocketWork``.
* **R14 (a refusal says whose policy it is)** — the echo-check floor is ours and says so.
  ``TestTheEchoCheckFloorIsOursAndIsNamedAsOurs``.
* **Labelling rule (never optional)** — a stand-in or recorded answer is labelled and can
  never claim a contacted source. ``TestEveryStandInAndFixtureOutcomeIsLabelled``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

import http.server

from switchboard_mini import cli, outcomes as O
from switchboard_mini import hermes_transport as T
from switchboard_mini.hermes_adapter import (PROBE_TEST_RUN_INPUT, HermesReadOnlyAdapter,
                                             build_adapter, probe_idempotency_key)

#: Repo root and the worker package directory, for the subprocess-level checks.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINI = os.path.join(REPO_ROOT, "mini")

#: A **synthetic** stand-in bearer value. Not a credential, not a secret, never read from
#: the owner: it exists so the token gate passes and the echo check has a string to look
#: for. Distinctive on purpose, so "the key is not in this document" is a meaningful
#: assertion rather than a coincidence check.
STANDIN_TOKEN = "synthetic-stand-in-bearer-4a7f9c1e2d7b"

#: Exactly the echo-check floor (8 characters) and one character below it. Syntactically a
#: key and nothing like real entropy: these are about the gate's arithmetic, not a credential.
FLOOR_KEY = "q7zk1p9x"
BELOW_FLOOR_KEY = "k7qz1p9"

#: The stand-in's run id, its session id, and the one run this worker may ever create.
STUB_RUN_ID = "run-stub-1"
STUB_SESSION_ID = "session-stub-1"
STUB_CREATED_RUN_ID = "run-stub-created-1"

#: How many events the stand-in's recorded stream carries. More than any cap a test asks
#: for, so "the cap was applied" and "the cap is inert" are distinguishable.
STUB_EVENT_COUNT = 12
STUB_EVENT_NAMES = ("tool.started", "message.delta", "tool.completed", "run.completed")


def sse_body(count: int = STUB_EVENT_COUNT) -> bytes:
    """A documented-shaped SSE body: ``event:`` lines and matching ``data:`` objects."""
    blocks = []
    for index in range(count):
        name = STUB_EVENT_NAMES[index % len(STUB_EVENT_NAMES)]
        blocks.append(f"event: {name}\ndata: {json.dumps({'type': name, 'index': index})}")
    return ("\n\n".join(blocks) + "\n").encode("utf-8")


def closed_port_base_url() -> str:
    """A loopback URL nothing is listening on.

    Bound and released rather than guessed, so the port really is free. This is the only
    meaning T14 needs: a *socket* that refuses, not a host that is the wrong platform.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    finally:
        probe.close()
    return f"http://127.0.0.1:{port}"


class StubHermesGateway:
    """A labelled loopback stand-in for the Hermes API server's documented reads.

    It is **not** Hermes and it is not a gateway. There is no Hermes install on this Linux
    computer, so the shipped transport's real wire layer — the socket, the headers, the
    status mapper, the body reader — can only be exercised against a responder that is not
    the source. Every document produced through it is marked ``stand_in: true`` and
    ``responder: stand_in_http_server`` by the transport itself, and the tests assert that.

    What it records, and what it deliberately does not:

    * the method, the resolved path, the query *names and values* (a query string is not a
      credential channel and the tests need to prove the key is not in one)
    * the ``Authorization`` header's **presence and scheme only** — never its value
    * the request body text (this worker's only POST bodies are its frozen probe constant
      and an owner-supplied approval body; the tests assert the bearer key is in neither)

    A path that still holds a ``{placeholder}`` is answered 400 and recorded, because an
    unsubstituted template reaching the wire is a defect in the worker, not a state of the
    source — that is exactly the ``{accountID}`` defect the Beeper slice once shipped.
    """

    def __init__(self, *, status_overrides: dict | None = None,
                 body_overrides: dict | None = None,
                 content_type_overrides: dict | None = None):
        self.status_overrides = dict(status_overrides or {})
        self.body_overrides = dict(body_overrides or {})
        self.content_type_overrides = dict(content_type_overrides or {})
        self.requests: list = []
        self.httpd = None
        self.thread = None
        self.base_url = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "StubHermesGateway":
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # never echo a request line anywhere
                return

            def _respond(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):                    # noqa: N802 - http.server's naming
                self._handle()

            def do_POST(self):                   # noqa: N802
                self._handle()

            def _handle(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                authorization = self.headers.get("Authorization") or ""
                stub.requests.append({
                    "method": self.command,
                    "path": parsed.path,
                    "query": urllib.parse.parse_qs(parsed.query),
                    "authorization_present": bool(authorization),
                    "authorization_scheme": authorization.split(" ", 1)[0],
                    "content_type": self.headers.get("Content-Type"),
                    "idempotency_key_present": bool(self.headers.get("Idempotency-Key")),
                    "body_text": body,
                    "body_keys": sorted(json.loads(body)) if body.startswith("{") else [],
                })
                status, content_type, payload = stub.answer(self.command, parsed.path,
                                                            urllib.parse.parse_qs(parsed.query))
                self._respond(status, content_type, payload)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    # -- what it answers ---------------------------------------------------
    def answer(self, method: str, path: str, query: dict) -> tuple:
        """``(status, content_type, body)`` for the paths a pack record describes."""
        if "{" in path or "}" in path:
            return (400, "application/json",
                    json.dumps({"error": "a path template reached the wire unsubstituted"})
                    .encode("utf-8"))
        if path in self.body_overrides:
            raw = self.body_overrides[path]
            body = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
            return (int(self.status_overrides.get(path, 200)),
                    self.content_type_overrides.get(path, "application/json"), body)
        if path in self.status_overrides:
            return (int(self.status_overrides[path]), "application/json",
                    json.dumps({"stub": "status override", "path": path}).encode("utf-8"))
        if path in ("/health", "/v1/health", "/health/detailed"):
            return (200, "application/json", json.dumps({"status": "ok"}).encode("utf-8"))
        if path == "/v1/capabilities":
            return (200, "application/json", json.dumps({
                "object": "hermes.api_server.capabilities", "platform": "stand-in",
                "model": "stand-in-model",
                "auth": {"type": "bearer", "required": True},
                "features": {"runs": True, "run_approval": True}}).encode("utf-8"))
        if path == "/v1/toolsets":
            return (200, "application/json", json.dumps([
                {"name": "terminal", "label": "Terminal", "description": "stand-in",
                 "enabled": True, "configured": True, "tools": ["shell"]}]).encode("utf-8"))
        if path == "/v1/skills":
            return (200, "application/json",
                    json.dumps([{"name": "stand-in-skill"}]).encode("utf-8"))
        if path == "/api/sessions":
            return (200, "application/json", json.dumps({
                "sessions": [{"id": STUB_SESSION_ID, "source": "api"}],
                "total": 1}).encode("utf-8"))
        if path == f"/api/sessions/{STUB_SESSION_ID}/messages":
            return (200, "application/json",
                    json.dumps({"messages": [{"role": "user", "content": "stand-in"}]})
                    .encode("utf-8"))
        if path == f"/api/sessions/{STUB_SESSION_ID}":
            return (200, "application/json",
                    json.dumps({"id": STUB_SESSION_ID, "source": "api"}).encode("utf-8"))
        if path == f"/v1/runs/{STUB_RUN_ID}/events":
            return (200, "text/event-stream", sse_body())
        if method == "POST" and path == f"/v1/runs/{STUB_RUN_ID}/stop":
            return (200, "application/json", json.dumps({"status": "stopping"}).encode("utf-8"))
        if method == "POST" and path == f"/v1/runs/{STUB_RUN_ID}/approval":
            return (200, "application/json", json.dumps({"status": "approved"}).encode("utf-8"))
        if method == "POST" and path == "/v1/runs":
            return (202, "application/json", json.dumps({
                "run_id": STUB_CREATED_RUN_ID, "status": "started"}).encode("utf-8"))
        if path == f"/v1/runs/{STUB_RUN_ID}":
            return (200, "application/json", json.dumps({
                "run_id": STUB_RUN_ID, "status": "completed",
                "session_id": STUB_SESSION_ID}).encode("utf-8"))
        return (404, "application/json",
                json.dumps({"error": "the stand-in serves the documented paths only"}).encode())


class _CaseHelpers:
    """Assertions shared by the cases that use the stand-in and the one that does not."""

    def document(self, outcome: O.Outcome) -> str:
        return O.emit(outcome.to_dict())

    def assertNoTraceback(self, text: str) -> None:
        self.assertNotIn("Traceback", text, text[:600])


class HermesStandInCase(_CaseHelpers, unittest.TestCase):
    """The stand-in gateway, plus the environment the worker's own runbook uses."""

    stub_kwargs: dict = {}
    token: str = STANDIN_TOKEN

    def setUp(self) -> None:
        self._env = {name: os.environ.get(name)
                     for name in (T.TOKEN_ENV, T.BASE_URL_ENV, T.STANDIN_ENV)}
        self.stub = StubHermesGateway(**self.stub_kwargs).start()
        os.environ[T.BASE_URL_ENV] = self.stub.base_url
        os.environ[T.STANDIN_ENV] = "1"
        os.environ[T.TOKEN_ENV] = self.token

    def tearDown(self) -> None:
        self.stub.stop()
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # -- construction ------------------------------------------------------
    def adapter(self, **kw) -> HermesReadOnlyAdapter:
        """The real adapter pointed at the stand-in. ``stand_in`` is the honest flag."""
        return build_adapter(base_url=self.stub.base_url, stand_in=True, **kw)

    def transport(self, **kw) -> T.HttpHermesTransport:
        """The real transport pointed at the stand-in."""
        return T.HttpHermesTransport(base_url=self.stub.base_url, stand_in=True, **kw)

    # -- assertions helpers ------------------------------------------------
    def read_sweep(self, adapter: HermesReadOnlyAdapter) -> list:
        """Every read the adapter offers, so a leak in any one of them is caught."""
        return [
            adapter.token_status(),
            adapter.capabilities(),
            adapter.toolsets(),
            adapter.skills(),
            adapter.health(),
            adapter.health(detailed=True),
            adapter.health_v1(),
            adapter.sessions(),
            adapter.session(STUB_SESSION_ID),
            adapter.session_messages(STUB_SESSION_ID),
            adapter.run_status(STUB_RUN_ID),
            adapter.run_events(STUB_RUN_ID),
            adapter.jobs(), adapter.models(), adapter.model_options(),
            adapter.responses(), adapter.chat_completions(), adapter.session_mutation(),
            adapter.refused("/v1/widgets"),
            adapter.approval(STUB_RUN_ID),
        ]


# ---------------------------------------------------------------------------------
# R14 / T12 -- the bearer key never leaves the process environment
# ---------------------------------------------------------------------------------


class TestTheBearerKeyNeverLeavesTheEnvironment(HermesStandInCase):
    """R14/T12 (the credential half of the approval contract): the key never leaves the
    process environment -- not into a log line, an evidence value, a ``repr()``, a URL or a
    file the worker writes."""

    def test_no_outcome_of_a_full_sweep_carries_the_key(self) -> None:
        """R14/T12: no read's document, detail, next action, evidence or repr has the key."""
        adapter = self.adapter()
        outcomes = self.read_sweep(adapter)
        self.assertTrue(any(o.usable for o in outcomes),
                        "the sweep must include reads the stand-in answered, or the "
                        "absence of a leak proves nothing")
        for outcome in outcomes:
            with self.subTest(operation=outcome.reason or outcome.code, code=outcome.code):
                emitted = self.document(outcome)
                self.assertNotIn(STANDIN_TOKEN, emitted)
                self.assertNotIn(STANDIN_TOKEN, outcome.detail or "")
                self.assertNotIn(STANDIN_TOKEN, outcome.next_action or "")
                self.assertNotIn(STANDIN_TOKEN, json.dumps(outcome.data or {}))
                self.assertNotIn(STANDIN_TOKEN, repr(outcome))
                self.assertNotIn(STANDIN_TOKEN, str(outcome))
                self.assertNotIn(STANDIN_TOKEN, repr(vars(outcome)))

    def test_the_transport_and_the_adapter_are_not_printable_in_a_way_that_leaks(self) -> None:
        """R14/T12: ``repr()``, ``str()``, f-strings and a dump of ``__dict__`` are safe.

        This is the assertion that fails the day somebody adds ``print(self.token)``: the
        value is held in a wrapper whose every printable form is a placeholder, so printing
        the attribute, the object, ``vars()`` of the object or a traceback of a frame that
        holds it all show the placeholder.
        """
        transport = self.transport()
        adapter = HermesReadOnlyAdapter(transport)
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            print(transport)
            print(adapter)
            print(transport._token)                  # the naive debug print
            print(vars(transport))
            print(transport.__dict__)
            print(f"{transport} {adapter} {transport._token!r} {transport._token}")
        for label, text in (("repr(transport)", repr(transport)),
                            ("str(transport)", str(transport)),
                            ("format(transport)", "{}".format(transport)),
                            ("repr(vars(transport))", repr(vars(transport))),
                            ("str(transport.__dict__)", str(transport.__dict__)),
                            ("repr(vars(adapter))", repr(vars(adapter))),
                            ("repr(vars(outcome))", repr(vars(adapter.token_status()))),
                            ("captured print()", printed.getvalue())):
            with self.subTest(view=label):
                self.assertNotIn(STANDIN_TOKEN, text, text[:400])
        self.assertIn(T._Secret.PLACEHOLDER, repr(transport))
        self.assertIn(T._Secret.PLACEHOLDER, repr(vars(transport)))
        self.assertEqual(str(transport._token), T._Secret.PLACEHOLDER)
        self.assertEqual(transport._token.value, STANDIN_TOKEN,
                         "the value is still reachable where the code needs it")
        self.assertNotIsInstance(vars(transport)["_token"], str,
                                 "a held key must not be a bare str attribute")

    def test_the_key_is_read_from_the_process_environment_and_no_file_is_opened(self) -> None:
        """R14/T12: the value comes from the environment, never from ``~/.hermes/.env`` or
        ``~/.hermes/config.yaml`` (O21) -- and no read path opens a file at all."""
        opened: list = []
        real_open = open

        def recording_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", recording_open):
            with mock.patch.object(T.os.environ, "get", wraps=T.os.environ.get) as asked:
                self.assertEqual(T.token_value(), STANDIN_TOKEN)
                adapter = self.adapter()
                for outcome in self.read_sweep(adapter):
                    self.document(outcome)
        asked_for = {call.args[0] for call in asked.call_args_list if call.args}
        self.assertIn(T.TOKEN_ENV, asked_for,
                      "the key must be looked for in the environment under O17's name")
        self.assertFalse([path for path in opened if ".hermes" in path],
                         f"a read path opened a Hermes credential file: {opened}")
        self.assertEqual(
            [os.path.join(name) for name in T.NEVER_READ_PATHS],
            ["~/.hermes/.env", "~/.hermes/config.yaml"],
            "the two files this worker refuses to open are recorded, not merely unopened")
        document = T.token_status_document()
        self.assertIs(document["token_read_from_file"], False)
        self.assertIs(document["token_value_recorded"], False)
        self.assertEqual(document["never_read_paths"], list(T.NEVER_READ_PATHS))
        self.assertIn("process environment " + T.TOKEN_ENV, document["token_source"])
        # Nothing is inherited by accident: a different variable name reads nothing.
        self.assertEqual(T.token_value("SWITCHBOARD_NO_SUCH_KEY"), "")

    def test_no_log_line_and_no_file_the_worker_writes_carries_the_key(self) -> None:
        """R14/T12: the key is in no log line, no stdout/stderr and no file the worker writes.

        In-process for the whole read family, then through the real CLI entry point (``hermes
        probe --out``) for the file the worker is able to write.
        """
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            adapter = self.adapter()
            for outcome in self.read_sweep(adapter):
                cli._print(outcome.to_dict(), False)
        self.assertNotIn(STANDIN_TOKEN, out.getvalue())
        self.assertNotIn(STANDIN_TOKEN, err.getvalue())

        scratch = tempfile.mkdtemp(prefix="hermes-transport-")
        rows_path = os.path.join(scratch, "rows.jsonl")
        state_path = os.path.join(scratch, "state.json")
        proc = subprocess.run(
            [sys.executable, "-m", "switchboard_mini", "hermes", "probe",
             "--out", rows_path],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=MINI,
            env=dict(os.environ, PYTHONPATH=MINI,
                     SWITCHBOARD_MINI_STATE=state_path,
                     SWITCHBOARD_HERMES_STANDIN="1"))
        self.assertNoTraceback(proc.stdout)
        self.assertNoTraceback(proc.stderr)
        written = {}
        for name in sorted(os.listdir(scratch)):
            with open(os.path.join(scratch, name), "r", encoding="utf-8") as handle:
                written[name] = handle.read()
        self.assertIn("rows.jsonl", written, "the probe's --out file was not written")
        for name, text in written.items():
            with self.subTest(file=name):
                self.assertNotIn(STANDIN_TOKEN, text)
        for label, text in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            with self.subTest(stream=label):
                self.assertNotIn(STANDIN_TOKEN, text)

    def test_the_key_is_never_in_a_url_and_only_ever_in_the_bearer_header(self) -> None:
        """R14/T12: the documented header form carries the key (O17) and a URL never does."""
        adapter = self.adapter()
        for outcome in self.read_sweep(adapter):
            self.document(outcome)
        self.assertTrue(self.stub.requests, "the sweep contacted the stand-in at least once")
        for request in self.stub.requests:
            with self.subTest(path=request["path"]):
                self.assertNotIn(STANDIN_TOKEN, request["path"])
                self.assertNotIn(STANDIN_TOKEN, urllib.parse.urlencode(request["query"]))
                self.assertNotIn(STANDIN_TOKEN, request["body_text"])
        authed = [r for r in self.stub.requests if r["authorization_present"]]
        self.assertTrue(authed)
        for request in authed:
            with self.subTest(path=request["path"]):
                self.assertEqual(request["authorization_scheme"], "Bearer")   # O17's form

    def test_a_response_that_echoes_the_key_is_refused_and_its_body_discarded(self) -> None:
        """R14/T12: the echo check runs on the value and the body is thrown away, not stored.

        A gateway promising redaction is not an observation, so the check is positive: the
        recorded body is searched for the key. A body that carries it produces
        ``token_echoed_in_response`` and **no** document.
        """
        self.stub.body_overrides["/v1/capabilities"] = json.dumps({
            "object": "hermes.api_server.capabilities",
            "leaked": f"Authorization: Bearer {STANDIN_TOKEN}"})
        outcome = self.adapter().capabilities()
        self.assertEqual(outcome.code, O.PERMANENT_ERROR)
        self.assertEqual(outcome.reason, "token_echoed_in_response")
        self.assertNotIn("document", outcome.data or {},
                         "a body carrying the key must not be recorded")
        self.assertNotIn(STANDIN_TOKEN, self.document(outcome))


# ---------------------------------------------------------------------------------
# T14 -- unreachability is a socket fact, never a platform reason
# ---------------------------------------------------------------------------------


class TestUnreachabilityIsSocketShapedNeverAMacReason(_CaseHelpers, unittest.TestCase):
    """T14: with nothing listening, the answer is ``offline`` / ``hermes_not_reachable`` --
    never a Mac-only reason and never a traceback. Unlike Beeper (an app's loopback port
    reached through an app on the Mac) Hermes reachability is a socket question only."""

    def setUp(self) -> None:
        self._env = {name: os.environ.get(name)
                     for name in (T.TOKEN_ENV, T.BASE_URL_ENV, T.STANDIN_ENV)}
        self.base_url = closed_port_base_url()
        os.environ[T.BASE_URL_ENV] = self.base_url
        os.environ.pop(T.STANDIN_ENV, None)
        os.environ[T.TOKEN_ENV] = STANDIN_TOKEN

    def tearDown(self) -> None:
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_every_documented_operation_answers_the_socket_reason(self) -> None:
        """T14: every read the transport can make reports ``offline``/``hermes_not_reachable``."""
        transport = T.HttpHermesTransport(base_url=self.base_url, timeout_s=5)
        params_for = {"session": {"id": STUB_SESSION_ID},
                      "session_messages": {"id": STUB_SESSION_ID},
                      "run": {"run_id": STUB_RUN_ID},
                      "run_events": {"run_id": STUB_RUN_ID},
                      "run_stop": {"run_id": STUB_RUN_ID},
                      "run_approval": {"run_id": STUB_RUN_ID}}
        for operation in sorted(T.ENDPOINTS):
            with self.subTest(operation=operation):
                outcome = transport.call(
                    operation, path_params=params_for.get(operation),
                    body={"input": PROBE_TEST_RUN_INPUT} if operation == "run_submit" else None)
                self.assertEqual(outcome.code, O.OFFLINE, outcome.detail)
                self.assertEqual(outcome.reason, "hermes_not_reachable")
                self.assertNotIn("host_not_macos", self.document(outcome))
                self.assertFalse(outcome.source_contacted)
                self.assertFalse(outcome.real_source_connected)
                self.assertEqual((outcome.data or {}).get("responder"), "nothing answered")
                self.assertIs((outcome.data or {}).get("requests_made"), 1)
                self.assertIn("refused", outcome.detail)

    def test_every_hermes_cli_command_answers_the_socket_reason(self) -> None:
        """T14: the whole ``hermes`` command family, driven through ``cli.main``.

        Each row is ``(label, argv after ``hermes``, expected reason)``; ``None`` means the
        command contacts nothing by design (a local token check, or a refusal the pack
        forces), so its own typed reason is what must appear instead.
        """
        commands = (
            ("token", ("token",), None, None),
            ("capabilities", ("capabilities",), O.OFFLINE, "hermes_not_reachable"),
            ("toolsets", ("toolsets",), O.OFFLINE, "hermes_not_reachable"),
            ("skills", ("skills",), O.OFFLINE, "hermes_not_reachable"),
            ("health", ("health",), O.OFFLINE, "hermes_not_reachable"),
            ("health-v1", ("health-v1",), O.OFFLINE, "hermes_not_reachable"),
            ("sessions", ("sessions",), O.OFFLINE, "hermes_not_reachable"),
            ("session", ("session", "--session-id", STUB_SESSION_ID),
             O.OFFLINE, "hermes_not_reachable"),
            ("session-messages", ("session-messages", "--session-id", STUB_SESSION_ID),
             O.OFFLINE, "hermes_not_reachable"),
            ("run-status", ("run-status", "--run-id", STUB_RUN_ID),
             O.OFFLINE, "hermes_not_reachable"),
            ("events", ("events", "--run-id", STUB_RUN_ID),
             O.OFFLINE, "hermes_not_reachable"),
            ("stop", ("stop", "--run-id", STUB_RUN_ID),
             O.OFFLINE, "hermes_not_reachable"),
            ("approval", ("approval", "--run-id", STUB_RUN_ID),
             O.UNSUPPORTED, "approval_body_shape_not_documented"),
            ("jobs", ("jobs",), O.UNSUPPORTED, "endpoint_not_in_pack"),
            ("models", ("models",), O.UNSUPPORTED, "endpoint_not_in_pack"),
            ("model-options", ("model-options",), O.UNSUPPORTED, "endpoint_not_in_pack"),
            ("responses", ("responses",), O.UNSUPPORTED, "endpoint_not_in_pack"),
            ("chat-completions", ("chat-completions",), O.UNSUPPORTED,
             "not_the_control_surface"),
            ("session-mutations", ("session-mutations",), O.UNSUPPORTED,
             "not_the_control_surface"),
            ("probe", ("probe",), None, None),
        )
        for label, argv, expected_code, expected_reason in commands:
            with self.subTest(command=label):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(["hermes", *argv])
                payload = out.getvalue()
                self.assertEqual(code, 0, f"{label}: exit {code}\n{payload}\n{err.getvalue()}")
                self.assertNoTraceback(payload)
                self.assertNoTraceback(err.getvalue())
                self.assertNotIn("host_not_macos", payload + err.getvalue())
                if label == "probe":
                    self.assertProbeRowsAreSocketShaped(payload)
                    continue
                document = json.loads(payload)
                if expected_reason is None:
                    self.assertNotEqual(document["reason"], "hermes_not_reachable",
                                        f"{label} contacts nothing, so it cannot report a "
                                        "socket state")
                    self.assertIs(document["data"].get("no_request_made", True), True)
                else:
                    self.assertEqual(document["reason"], expected_reason)
                    self.assertEqual(document["code"], expected_code)
                    self.assertIs(document["real_source_connected"], False)

    def assertProbeRowsAreSocketShaped(self, payload: str) -> None:
        """T14 (the probe's own rows): a row that could only be settled by a socket answers
        the socket reason, every Hermes row stays ``supported: false`` and claims no
        contacted source, and no row anywhere reports a Mac reason. The rows that need a run
        (or that an authenticated read cannot settle at all) report their own typed state --
        which this asserts, rather than assuming every row is the socket state."""
        rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
        hermes_rows = [r for r in rows if r.get("source") == "hermes"]
        self.assertTrue(hermes_rows, "the probe emitted no Hermes rows")
        socket_rows = []
        for row in hermes_rows:
            with self.subTest(capability=row["capability"]):
                self.assertIs(row["supported"], False)
                self.assertIs(row["real_source_connected"], False)
                self.assertNotEqual(row["state"], O.SUCCESS)
                self.assertNotIn("host_not_macos", json.dumps(row))
                evidence = row.get("evidence") or {}
                if row["state"] == O.OFFLINE:
                    socket_rows.append(row["capability"])
                    self.assertEqual(evidence["reason"], "hermes_not_reachable")
                    self.assertNotEqual(evidence["responder"], "hermes_gateway_api")
                else:
                    self.assertEqual(evidence.get("requests_made", 0), 0,
                                     "a row that did not report the socket state must not "
                                     "have made a request")
        self.assertGreaterEqual(
            len(socket_rows), 4,
            "the rows that can only be settled by reaching the gateway must report offline")

    def test_the_real_entry_point_shows_no_traceback_and_no_mac_reason(self) -> None:
        """T14: through the installed entry point (``python3 -m switchboard_mini``) too."""
        proc = subprocess.run(
            [sys.executable, "-m", "switchboard_mini", "hermes", "capabilities"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=MINI,
            env=dict(os.environ, PYTHONPATH=MINI))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNoTraceback(proc.stderr)
        self.assertNotIn("host_not_macos", proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["reason"], "hermes_not_reachable")


# ---------------------------------------------------------------------------------
# T15 -- a status no record describes is the honest unnamed state
# ---------------------------------------------------------------------------------


class TestAStatusNoRecordDescribesIsUnexpectedStatus(HermesStandInCase):
    """T15: a named failure state may only be used where a record names it for *that*
    operation. O17 records 409 for an Idempotency-Key reused with a different payload on a
    run-starting POST, and 429 for a new run-starting request; nothing in the pack describes
    a 403 anywhere, and O17 itself says another profile's run id returns "404, never 403"."""

    def test_a_409_on_a_get_is_unexpected_status_not_an_idempotency_conflict(self) -> None:
        """T15: 409 on a discovery path is ``unexpected_status`` with the record's scope noted."""
        self.stub.status_overrides["/v1/capabilities"] = 409
        outcome = self.adapter().capabilities()
        self.assertEqual(outcome.code, O.PERMANENT_ERROR)
        self.assertEqual(outcome.reason, "unexpected_status")
        self.assertEqual(outcome.data["http_status"], 409)
        self.assertIs(outcome.data["status_has_no_documented_meaning_for_this_operation"], True)
        self.assertIn("Idempotency-Key", outcome.data["status_described_elsewhere"])
        self.assertIn("not a run-starting request", outcome.data["status_described_elsewhere"])

    def test_a_429_on_a_get_is_not_a_rate_limit(self) -> None:
        """T15: O17 records 429 for a new *run-starting* request; a GET 429 is unnamed."""
        self.stub.status_overrides["/api/sessions"] = 429
        outcome = self.adapter().sessions()
        self.assertEqual(outcome.code, O.PERMANENT_ERROR)
        self.assertNotEqual(outcome.code, O.RATE_LIMITED)
        self.assertEqual(outcome.reason, "unexpected_status")
        self.assertEqual(outcome.data["http_status"], 429)
        self.assertIn("run-starting", outcome.data["status_described_elsewhere"])

    def test_a_403_has_no_named_state_anywhere(self) -> None:
        """T15: no record describes a 403 for any path this worker reads, so it is unnamed."""
        self.stub.status_overrides["/v1/capabilities"] = 403
        outcome = self.adapter().capabilities()
        self.assertEqual(outcome.code, O.PERMANENT_ERROR)
        self.assertEqual(outcome.reason, "unexpected_status")
        self.assertEqual(outcome.data["http_status"], 403)
        self.assertIn("403", outcome.data["status_described_elsewhere"])
        self.assertNotIn("forbidden_by_hermes", T.TYPED_STATES)
        self.assertNotEqual(outcome.code, O.PERMISSION_DENIED)

    def test_a_409_on_a_run_post_without_the_header_is_also_unnamed(self) -> None:
        """T15: the documented 409 needs the documented header; without it the status is new."""
        self.stub.status_overrides["/v1/runs"] = 409
        outcome = self.adapter().transport.call("run_submit", body={"input": "x"})
        self.assertEqual(outcome.code, O.PERMANENT_ERROR)
        self.assertEqual(outcome.reason, "unexpected_status")
        self.assertIn("carried no Idempotency-Key",
                      outcome.data["status_described_elsewhere"])

    def test_the_documented_409_and_429_are_still_named_on_the_run_post(self) -> None:
        """T15: narrowing is scoped, not a blanket removal -- the recorded case still works."""
        self.stub.status_overrides["/v1/runs"] = 409
        conflicted = self.transport().call(
            "run_submit", body={"input": "x"}, headers={"Idempotency-Key": "probe-key-1"})
        self.assertEqual(conflicted.code, O.PERMANENT_ERROR)
        self.assertEqual(conflicted.reason, "idempotency_key_conflict")
        self.assertFalse(conflicted.data.get("status_has_no_documented_meaning_for_this_operation"))

        self.stub.status_overrides["/v1/runs"] = 429
        throttled = self.transport().call(
            "run_submit", body={"input": "x"}, headers={"Idempotency-Key": "probe-key-2"})
        self.assertEqual(throttled.code, O.RATE_LIMITED)
        self.assertEqual(throttled.reason, "rate_limited")
        self.assertEqual(throttled.data["http_status"], 429)

    def test_a_recorded_scenario_gets_the_same_narrowing(self) -> None:
        """T15: a recorded answer cannot make a GET look rate-limited either (fixture parity)."""
        recorded = T.RecordedHermesTransport({
            "scenario": "synthetic-transport-test", "token_present": True,
            "responses": {"sessions": {"status": 429}, "capabilities": {"status": 403},
                          "run_submit": {"status": 409}}})
        for operation, path_params in (("sessions", None), ("capabilities", None),
                                       ("run_submit", None)):
            with self.subTest(operation=operation):
                outcome = recorded.call(operation, path_params=path_params)
                self.assertEqual(outcome.code, O.PERMANENT_ERROR)
                self.assertEqual(outcome.reason, "unexpected_status")
                self.assertEqual(outcome.origin, O.FIXTURE)
                self.assertTrue(O.is_fixture_label(outcome.label))
                self.assertFalse(outcome.source_contacted)
        self.assertEqual(recorded.call("capabilities").data["http_status"], 403)


# ---------------------------------------------------------------------------------
# only documented paths, and nothing else, ever reaches the wire
# ---------------------------------------------------------------------------------


class TestOnlyDocumentedPathsAreEverSent(HermesStandInCase):
    """R01/R15 (the pack is the only source of surface): every endpoint this transport will
    call is one a record describes; anything else is refused, and no request is made."""

    def test_an_invented_operation_is_refused_with_no_request(self) -> None:
        """R15: a path no record names is ``endpoint_not_in_pack`` and never sent."""
        outcome = self.transport().call("invented_operation")
        self.assertEqual(outcome.code, O.UNSUPPORTED)
        self.assertEqual(outcome.reason, "endpoint_not_in_pack")
        self.assertIs(outcome.data["no_request_made"], True)
        self.assertEqual(outcome.data["requests_made"], 0)
        self.assertEqual(self.stub.requests, [])

    def test_every_refused_ask_contacts_nothing(self) -> None:
        """R15: the named-but-undescribed and mutation asks are refused before any socket work."""
        adapter = self.adapter()
        refused = [adapter.jobs(), adapter.models(), adapter.model_options(),
                   adapter.responses(), adapter.chat_completions(),
                   adapter.session_mutation(), adapter.refused("/v1/widgets"),
                   adapter.refused("/v1/responses/{id}")]
        for outcome in refused:
            with self.subTest(ask=(outcome.data or {}).get("asked_for")):
                self.assertEqual(outcome.code, O.UNSUPPORTED)
                self.assertIn(outcome.reason,
                              ("endpoint_not_in_pack", "not_the_control_surface"))
                self.assertEqual(outcome.data["requests_made"], 0)
                self.assertIs(outcome.data["no_request_made"], True)
                self.assertTrue(outcome.next_action)
                self.assertNotIn("http://", self.document(outcome))
        self.assertEqual(self.stub.requests, [],
                         "a refused ask must not have reached the stand-in at all")

    def test_the_endpoint_table_is_the_whole_surface_and_names_a_pack_record(self) -> None:
        """R15: every operation the transport will call is backed by an O-record, GET or POST."""
        for operation, meta in sorted(T.ENDPOINTS.items()):
            with self.subTest(operation=operation):
                self.assertTrue(meta["ref"].startswith("O"), meta)
                self.assertIn(meta["method"], ("GET", "POST"))
                self.assertTrue(meta["path"].startswith("/"))
                self.assertTrue(meta["described_as"])

    def test_an_undocumented_query_parameter_is_refused_with_no_request(self) -> None:
        """R15: only the parameters O17 names for a path are sent (nothing is invented)."""
        outcome = self.adapter().transport.call("sessions", params={"invented": "1"})
        self.assertEqual(outcome.reason, "parameter_not_documented")
        self.assertEqual(outcome.data["documented"],
                         list(T.DOCUMENTED_PARAMS["sessions"]))
        self.assertEqual(outcome.data["requests_made"], 0)
        self.assertEqual(self.stub.requests, [])

    def test_a_refused_ask_does_not_reach_the_wire_even_when_the_gateway_is_up(self) -> None:
        """R15: the stand-in is reachable and still receives nothing for a refused ask."""
        adapter = self.adapter()
        self.assertTrue(adapter.capabilities().usable,
                        "the stand-in must be answering, or this proves nothing")
        served = len(self.stub.requests)
        for outcome in (adapter.jobs(), adapter.refused("/api/sessions/abc/fork"),
                        adapter.refused("/v1/chat/completions")):
            self.assertEqual(outcome.data["requests_made"], 0)
        self.assertEqual(len(self.stub.requests), served)


# ---------------------------------------------------------------------------------
# there is no general run submission
# ---------------------------------------------------------------------------------


class TestThereIsNoGeneralRunSubmission(HermesStandInCase):
    """R14 (no unapproved outbound or executing action): O17 says the API server "gives full
    access to hermes-agent's toolset, including terminal commands", so a general run command
    would be unapproved remote execution. The only run this worker ever creates is its own
    frozen trivial probe constant, behind the explicit consent flag."""

    def test_an_arbitrary_input_is_refused_before_any_socket_work_even_with_consent(self) -> None:
        """R14: consent cannot unlock a general submission; the input must be the constant."""
        adapter = self.adapter()
        for probe_input in ("run a shell command", "bash -c 'echo hi'", "", "  ",
                            PROBE_TEST_RUN_INPUT + " ", "reply with ready"):
            with self.subTest(input=probe_input):
                outcome = adapter.submit_run(probe_input,
                                             idempotency_key="probe-key-1", consent=True)
                self.assertEqual(outcome.code, O.UNSUPPORTED)
                self.assertEqual(outcome.reason, "general_run_submission_refused")
                self.assertEqual(outcome.data["requests_made"], 0)
                self.assertIs(outcome.data["general_submission_available"], False)
                self.assertFalse([r for r in self.stub.requests if r["method"] == "POST"])
        self.assertEqual(self.stub.requests, [])

    def test_without_the_consent_flag_no_run_is_created_even_when_the_source_answers(self) -> None:
        """R14: a reachable source and the frozen constant are still not enough without consent."""
        adapter = self.adapter()
        self.assertTrue(adapter.health().usable, "the stand-in must be answering")
        outcome = adapter.submit_run(PROBE_TEST_RUN_INPUT, idempotency_key="probe-key-1",
                                     consent=False)
        self.assertEqual(outcome.code, O.UNSUPPORTED)
        self.assertEqual(outcome.reason, "run_submission_not_consented")
        self.assertEqual(outcome.data["requests_made"], 0)
        self.assertIs(outcome.data["input_is_the_frozen_probe_constant"], True)
        self.assertFalse([r for r in self.stub.requests if r["method"] == "POST"],
                         "no run may be created without the explicit consent flag")

    def test_with_consent_exactly_one_frozen_run_is_created(self) -> None:
        """R14: with the flag, the one permitted run is sent -- and it is the frozen constant."""
        adapter = self.adapter()
        key = probe_idempotency_key(PROBE_TEST_RUN_INPUT)
        outcome = adapter.submit_run(PROBE_TEST_RUN_INPUT, idempotency_key=key, consent=True)
        self.assertEqual(outcome.code, O.SUCCESS, outcome.detail)
        posts = [r for r in self.stub.requests if r["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["path"], "/v1/runs")
        self.assertEqual(json.loads(posts[0]["body_text"]),
                         {"input": PROBE_TEST_RUN_INPUT})
        self.assertTrue(posts[0]["idempotency_key_present"])
        self.assertEqual(outcome.data["idempotency_key_used"], key)
        self.assertIs(outcome.data["general_submission_available"], False)
        self.assertIs(outcome.data["input_text_is_a_frozen_constant"], True)

    def test_the_adapter_and_the_cli_expose_no_other_run_door(self) -> None:
        """R14: the module offers no general ``run`` surface and the CLI parses no such command."""
        adapter = self.adapter()
        for name in ("run", "submit", "execute", "chat", "run_input"):
            self.assertFalse(hasattr(adapter, name), name)
        proc = subprocess.run(
            [sys.executable, "-m", "switchboard_mini", "hermes", "run", "--input", "hi"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=MINI, env=dict(os.environ, PYTHONPATH=MINI))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid choice", proc.stderr)
        self.assertEqual(self.stub.requests, [])


# ---------------------------------------------------------------------------------
# bounds are applied; a placeholder never reaches the wire
# ---------------------------------------------------------------------------------


class TestBoundsAreAppliedAndPlaceholdersAreRefusedBeforeAnySocketWork(HermesStandInCase):
    """R14/R15 (bounded, addressable work): a caller's ``max_events`` bound is applied while
    parsing rather than accepted and dropped, and a path template that would carry a literal
    ``{run_id}`` to the gateway is refused *before* any socket work -- the ``{accountID}``
    defect the Beeper slice once shipped."""

    def test_max_events_is_applied_by_the_transport(self) -> None:
        """R15: the caller's window is the one applied, and it is recorded as ours."""
        adapter = self.adapter()
        capped = adapter.run_events(STUB_RUN_ID, max_events=2)
        self.assertEqual(capped.code, O.SUCCESS, capped.detail)
        self.assertEqual(capped.data["event_limit_applied"], 2)
        self.assertIs(capped.data["event_limit_came_from_caller"], True)
        self.assertIs(capped.data["event_limit_is_ours"], True)
        self.assertEqual(len(capped.data["event_names_in_order"]), 2)
        self.assertGreater(capped.data["event_count"], 2,
                           "the stand-in's stream must be longer than the cap, or an inert "
                           "cap would look identical")
        self.assertIs(capped.data["payload_text_recorded"], False)
        self.assertNotIn(STANDIN_TOKEN, self.document(capped))

    def test_the_default_window_is_used_when_the_caller_names_none(self) -> None:
        """R15: with no caller bound the worker's own default applies -- and is labelled ours."""
        default = self.transport().call("run_events", path_params={"run_id": STUB_RUN_ID})
        self.assertEqual(default.data["event_limit_applied"], T.DEFAULT_MAX_EVENTS)
        self.assertIs(default.data["event_limit_came_from_caller"], False)
        self.assertIs(default.data["event_limit_is_ours"], True)
        self.assertEqual(len(default.data["event_names_in_order"]), STUB_EVENT_COUNT)
        self.assertEqual(default.data["event_count"], STUB_EVENT_COUNT)
        # The adapter is itself a caller: it passes its own default explicitly, and says so.
        through_adapter = self.adapter().run_events(STUB_RUN_ID)
        self.assertEqual(through_adapter.data["event_limit_applied"], T.DEFAULT_MAX_EVENTS)
        self.assertIs(through_adapter.data["event_limit_came_from_caller"], True)
        self.assertIs(through_adapter.data["event_limit_is_ours"], True)

    def test_a_missing_path_parameter_is_refused_with_no_request(self) -> None:
        """R15: a template asked for without its parameter never goes on the wire."""
        transport = self.transport()
        for operation, path_params in (("run", None), ("run", {}), ("session", {"id": ""}),
                                       ("run_events", {"run_id": None})):
            with self.subTest(operation=operation, path_params=path_params):
                outcome = transport.call(operation, path_params=path_params)
                self.assertEqual(outcome.code, O.UNSUPPORTED)
                self.assertEqual(outcome.reason, "missing_path_parameter")
                self.assertEqual(outcome.data["requests_made"], 0)
                self.assertIs(outcome.data["no_request_made"], True)
        self.assertEqual(self.stub.requests, [])

    def test_a_leftover_placeholder_is_refused_even_if_resolution_leaves_one(self) -> None:
        """R15: the last-line template guard fires, and no socket work happens.

        Driven with the substitution deliberately broken and the earlier missing-parameter
        check bypassed, so what is under test is the guard itself rather than the check in
        front of it.
        """
        transport = self.transport()
        with mock.patch.dict(T.REQUIRED_PATH_PARAMS, {}, clear=True), \
                mock.patch.object(T, "resolve_path", side_effect=lambda path, params: path):
            outcome = transport.call("run", path_params=None)
        self.assertEqual(outcome.reason, "missing_path_parameter")
        self.assertEqual(outcome.data["requests_made"], 0)
        self.assertEqual(outcome.data["missing"], ["run_id"])
        self.assertIs(outcome.data["no_request_made"], True)
        self.assertEqual(self.stub.requests, [],
                         "a literal {run_id} must never reach a gateway")
        self.assertEqual(T.unsubstituted_placeholders("/v1/runs/{run_id}"), ["run_id"])
        self.assertEqual(T.unsubstituted_placeholders("/v1/runs/run-stub-1"), [])

    def test_with_its_parameter_the_same_call_does_reach_the_wire(self) -> None:
        """R15 (positive control): the refusals above are specific, not a blanket refusal."""
        outcome = self.transport().call("run", path_params={"run_id": STUB_RUN_ID})
        self.assertEqual(outcome.code, O.SUCCESS, outcome.detail)
        self.assertEqual(outcome.data["path_resolved"], f"/v1/runs/{STUB_RUN_ID}")
        self.assertEqual([r["path"] for r in self.stub.requests],
                         [f"/v1/runs/{STUB_RUN_ID}"])

    def test_a_malformed_idempotency_key_is_refused_before_any_request(self) -> None:
        """R15: O17's 1-255 visible ASCII rule is enforced locally, so nothing malformed is sent."""
        for key in ("", "x" * 256, "has space", "unicode-\u00e9"):
            with self.subTest(key=key[:12]):
                outcome = self.transport().call("run_submit", body={"input": "x"},
                                                headers={"Idempotency-Key": key})
                self.assertEqual(outcome.reason, "idempotency_key_not_documented_shape")
                self.assertEqual(outcome.code, O.PERMANENT_ERROR)
        self.assertEqual(self.stub.requests, [],
                         "a malformed Idempotency-Key must never reach the wire")


# ---------------------------------------------------------------------------------
# the echo-check floor is ours, and says so
# ---------------------------------------------------------------------------------


class TestTheEchoCheckFloorIsOursAndIsNamedAsOurs(HermesStandInCase):
    """R14 (a refusal states whose rule it is): O17 states no minimum bearer-key length, so
    the floor this worker needs before it can run its response echo check is this worker's
    own policy. A key below it is refused at the gate -- named as ours -- rather than used
    for a check that would silently pass on ordinary prose."""

    def test_a_key_below_the_floor_is_refused_as_our_own_policy(self) -> None:
        """R14: the refusal names the floor as ours and makes no request."""
        transport = self.transport(token=BELOW_FLOOR_KEY)
        self.assertLess(len(BELOW_FLOOR_KEY), T.MIN_TOKEN_LEN_FOR_ECHO_CHECK)
        for operation, path_params in (("capabilities", None), ("run", {"run_id": STUB_RUN_ID})):
            with self.subTest(operation=operation):
                outcome = transport.call(operation, path_params=path_params)
                self.assertEqual(outcome.code, O.PERMANENT_ERROR)
                self.assertEqual(outcome.reason, "token_too_short_for_the_echo_check")
                self.assertIs(outcome.data["refusal_is_ours"], True)
                self.assertIs(outcome.data["floor_is_ours"], True)
                self.assertIsNone(outcome.data["documented_minimum_key_length"])
                self.assertEqual(outcome.data["requests_made"], 0)
                self.assertIn("O17 states no minimum key length", outcome.detail)
                self.assertIn("this floor is this worker's own policy", outcome.detail)
                self.assertIn(str(T.MIN_TOKEN_LEN_FOR_ECHO_CHECK), outcome.detail)
                self.assertNotIn(BELOW_FLOOR_KEY, self.document(outcome))
        self.assertEqual(self.stub.requests, [],
                         "a key below the floor is refused before any request")

    def test_the_status_document_reports_the_floor_without_the_key(self) -> None:
        """R14: the gate's own report says the floor is ours and never prints the key.

        It must also report the key *this transport holds*: a status document that answered
        from the environment while the gate was passing a constructor-supplied key said "no
        bearer key is configured" about a transport that was authorising requests with one.
        """
        transport = self.transport(token=BELOW_FLOOR_KEY)
        status = transport.token_status()
        self.assertEqual(status.code, O.SUCCESS)
        self.assertIs(status.data["echo_check_min_length_is_ours"], True)
        self.assertEqual(status.data["echo_check_min_length"],
                         T.MIN_TOKEN_LEN_FOR_ECHO_CHECK)
        self.assertIs(status.data["token_present"], True)
        self.assertIs(status.data["token_below_the_echo_check_floor"], True)
        self.assertIs(status.data["token_value_recorded"], False)
        self.assertIs(status.data["token_read_from_file"], False)
        self.assertIn("constructor argument", status.data["token_source"])
        self.assertIn(T.TOKEN_ENV, status.data["token_source"])
        self.assertNotIn(BELOW_FLOOR_KEY, self.document(status))
        # And a transport holding a long constructor key does not report the environment's
        # (empty) state either.
        self.assertIs(self.transport().token_status().data["token_present"], True)

    def test_a_key_exactly_at_the_floor_still_runs_the_echo_check(self) -> None:
        """R14: at the floor the gate passes and the echo check actually runs on the value."""
        self.assertEqual(len(FLOOR_KEY), T.MIN_TOKEN_LEN_FOR_ECHO_CHECK)
        self.stub.body_overrides["/v1/capabilities"] = json.dumps({
            "object": "hermes.api_server.capabilities", "echo": FLOOR_KEY})
        adapter = HermesReadOnlyAdapter(self.transport(token=FLOOR_KEY))
        outcome = adapter.capabilities()
        self.assertEqual([r["path"] for r in self.stub.requests], ["/v1/capabilities"],
                         "the gate must have passed: the echo check only means something "
                         "if a response really arrived")
        self.assertEqual(outcome.code, O.PERMANENT_ERROR)
        self.assertEqual(outcome.reason, "token_echoed_in_response")
        self.assertNotIn("document", outcome.data or {})
        self.assertNotIn(FLOOR_KEY, self.document(outcome))

    def test_the_floor_is_a_floor_for_the_check_not_a_licence_for_a_short_key(self) -> None:
        """R14: ``body_carries_token`` cannot be fooled into a false positive by a short string."""
        self.assertLess(len(BELOW_FLOOR_KEY), T.MIN_TOKEN_LEN_FOR_ECHO_CHECK)
        self.assertFalse(T.body_carries_token("prose containing " + BELOW_FLOOR_KEY,
                                              BELOW_FLOOR_KEY))
        self.assertTrue(T.body_carries_token("prose containing " + FLOOR_KEY, FLOOR_KEY))
        self.assertFalse(T.body_carries_token("prose containing nothing", FLOOR_KEY))


# ---------------------------------------------------------------------------------
# nothing here may look like an observation of a real source
# ---------------------------------------------------------------------------------


class TestEveryStandInAndFixtureOutcomeIsLabelled(HermesStandInCase):
    """The labelling rule (never optional): a stand-in or recorded answer is visibly labelled
    and can never claim a contacted source. Nothing in this file is an observation of any
    Hermes install -- there is none on this computer."""

    def test_every_stand_in_outcome_says_it_is_a_stand_in_and_contacted_nothing(self) -> None:
        """Labelling rule: every document through the loopback stand-in is marked, and no
        document claims ``source_contacted``/``real_source_connected``."""
        adapter = self.adapter()
        marked = 0
        for outcome in self.read_sweep(adapter):
            with self.subTest(operation=outcome.reason or outcome.code):
                self.assertFalse(outcome.source_contacted)
                self.assertFalse(outcome.real_source_connected)
                self.assertEqual(outcome.origin, O.REAL,
                                 "the real *adapter* is what ran; it read nothing from a source")
                responder = (outcome.data or {}).get("responder")
                if responder is not None:
                    marked += 1
                    self.assertEqual(responder, "stand_in_http_server")
                    self.assertIs((outcome.data or {}).get("stand_in"), True)
                self.assertNotIn("hermes_gateway_api", json.dumps(outcome.data or {}))
        self.assertGreater(marked, 0,
                           "some outcome must have reached the stand-in, or this proves nothing")
        self.assertTrue([r for r in self.stub.requests if r["authorization_present"]],
                        "the sweep must really have spoken to the stand-in")

    def test_a_fixture_outcome_is_labelled_and_names_the_source_it_did_not_read(self) -> None:
        """Labelling rule: a recorded answer carries the FIXTURE label and the Hermes waiver."""
        recorded = T.build_transport(fixture_mode=True, fixture_scenario="healthy")
        outcome = recorded.call("capabilities")
        self.assertEqual(outcome.origin, O.FIXTURE)
        self.assertTrue(O.is_fixture_label(outcome.label), outcome.label)
        self.assertFalse(outcome.source_contacted)
        self.assertFalse(outcome.real_source_connected)
        document = outcome.to_dict()
        self.assertIn("label", document)
        self.assertIn("disclaimer", document)
        self.assertIn("Hermes", document["disclaimer"])
        self.assertIn("No Hermes gateway was contacted", document["disclaimer"])
        self.assertIn("stand_in", json.dumps(outcome.data))
        self.assertIs(outcome.data["stand_in"], False)

    def test_an_injected_opener_is_a_stand_in_by_construction(self) -> None:
        """Labelling rule: replacing the responder *is* being a stand-in, flag or no flag.

        A caller who injects an opener has replaced the real responder by construction, so
        the transport must not mint a document that claims ``source_contacted``.
        """
        class _Response:
            status = 200
            headers: dict = {}

            def read(self):
                return json.dumps({"object": "hermes.api_server.capabilities"}).encode()

        transport = T.HttpHermesTransport(token=STANDIN_TOKEN,
                                          opener=lambda request, timeout: _Response())
        self.assertTrue(transport.stand_in,
                        "an injected opener makes the responder a stand-in")
        outcome = transport.call("capabilities")
        self.assertEqual(outcome.code, O.SUCCESS)
        self.assertFalse(outcome.source_contacted)
        self.assertFalse(outcome.real_source_connected)
        self.assertEqual((outcome.data or {}).get("responder"), "stand_in_http_server")

    def test_no_outcome_in_this_file_can_import_as_a_real_measurement(self) -> None:
        """Labelling rule: with the loopback stand-in, every row says nothing was measured."""
        adapter = self.adapter()
        reached_the_wire = 0
        for outcome in self.read_sweep(adapter):
            document = outcome.to_dict()
            with self.subTest(operation=outcome.reason or outcome.code):
                self.assertIs(document["real_source_connected"], False)
                self.assertIs(document["source_contacted"], False)
                if "responder" in json.dumps(document):
                    reached_the_wire += 1
                    self.assertIn("stand_in_http_server", json.dumps(document))
        self.assertGreater(reached_the_wire, 0)


# ---------------------------------------------------------------------------------
# HALF TWO -- deliberately not in this file
# ---------------------------------------------------------------------------------
#
# The following are the *probe-row* half and are NOT covered here; they are the next
# delegation's file (``tests/test_mini_hermes_probe_rows.py`` or its accepted name):
#
# * the eight Hermes capability rows: their ``origin``/``state``/``supported`` shape, the
#   ``hermes_execution_modes`` mechanism derived from ``tool_names_seen`` /
#   ``toolset_names``, the ``state: partial`` narrowing, and that no row claims a contacted
#   source while every Hermes row stays ``unmeasured`` until a gateway answers;
# * ``probe --submit-test-run``'s row evidence: the frozen constant quoted where a reader
#   can see what was sent, the deterministic ``Idempotency-Key``, and that the row cannot
#   claim a measurement from the stand-in;
# * the session-continuity, terminal-backend, approval-observation and
#   credential-resolution half-measured rows and their next actions;
# * ``grace probe-import`` consuming a Hermes run: a stand-in row must not import as a
#   measurement, and a documentation row still may never be ``supported: true``.
#
# The transport coverage those rows depend on (the status mapper, the consent gate, the
# bounds, the refusals, the labelling) is what this file establishes, so half two can build
# on it rather than re-deriving it.


class TestThisFileIsHalfOneOnly(unittest.TestCase):
    """Scope guard: this file must not quietly become the probe-row half."""

    def test_this_file_names_the_requirements_it_covers(self) -> None:
        """R14/T12 (coverage traceability): every test docstring in this file names an
        R-number, an acceptance test, or the labelling rule, so a claim in a report can be
        traced to a test that made it."""
        module = sys.modules[__name__]
        missing = []
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase) \
                    and obj is not unittest.TestCase:
                for attribute in vars(obj):
                    if attribute.startswith("test_"):
                        text = getattr(obj, attribute).__doc__ or ""
                        if not any(token in text for token in ("R0", "R1", "T0", "T1", "Labelling")):
                            missing.append(f"{obj.__name__}.{attribute}")
        self.assertEqual(missing, [], f"tests without a named requirement: {missing}")


if __name__ == "__main__":
    unittest.main()
