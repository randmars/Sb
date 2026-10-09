"""The Beeper Desktop read adapter: what was exercised here, and what was not.

Slice 2 of Gate 2 added a read-only adapter for the Beeper Desktop local API and the four
capability rows it can measure. This is the test file that slice shipped without.

**What runs here.** A local stub HTTP server (stdlib ``http.server``) answers the four
endpoints the probe pack documents -- ``GET /v1/info`` (O11), ``POST /oauth/introspect``
(O11), ``GET /v1/messages/search`` (O06), ``GET /v1/accounts/{accountID}/contacts/list``
(O12) -- so the *wire layer* is exercised for real: real sockets, real urllib, real status
codes, real headers. That is why the adapter needs ``--stand-in-server`` to talk to it on a
host that is not a Mac, and why every document it produces is stamped ``stand_in`` and can
never claim a real source. Four recorded scenarios (``healthy``, ``token_rejected``,
``beeper_down``, ``history_limited``) cover the states a stub cannot honestly produce.

**What this does not prove.** There is no Beeper Desktop on this Linux computer, so nothing
here is an observation of Randy's install: the four capability rows stay
``supported: false``, and only a probe run on his Mac can change that (Gate 2). The stub is
labelled a stub; the recorded scenarios are labelled fixtures.

The properties asserted: the token value never reaches any document, log or error string;
presence/absence is reported without the value; the asks the pack does not answer are
refused with the smallest next action rather than guessed; the documented search limit is
clamped; a foreign cursor is refused instead of resumed; contacts are masked and message
text is fingerprinted; ``beeper_down`` is ``offline`` and a rejected token is
``permission_denied`` with the 401; every fixture-derived row carries its ``FIXTURE:``
label and disclaimer; and no fixture-origin row can be ``supported: true``.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MINI = REPO_ROOT / "mini"
for path in (str(REPO_ROOT), str(MINI)):
    if path not in sys.path:
        sys.path.insert(0, path)

from switchboard_mini import outcomes as O                     # noqa: E402
from switchboard_mini import beeper_transport as T             # noqa: E402
from switchboard_mini.beeper_adapter import build_adapter      # noqa: E402
from switchboard_mini.beeper_adapter import MEASURED_CAPABILITIES  # noqa: E402
from switchboard_mini.probe import run_probe                   # noqa: E402

#: A synthetic credential for the wire tests. Nothing here is a real token: the value only
#: has to be a distinctive string so every assertion "the token is not in this document"
#: can be made by searching for it.
SYNTHETIC_TOKEN = "sb-synthetic-token-do-not-use-2f7a91"
ACCOUNT_ID = "stub-account-A"
CONTACT_EMAIL = "alex.rivera@example.com"
CONTACT_PHONE = "+1 555 0100 001"
CONTACT_IMAGE = "https://example.invalid/avatar/9d3f.png"
CONTACT_ID = "stub-contact-77"
MESSAGE_TEXT = "the quick brown fox jumps over the lazy dog"
SENDER_ID = "stub-network:stub-user-3"


def search_page(*, cursor=None, has_more=True, text=MESSAGE_TEXT, message_id="stub-message-1",
                sender=SENDER_ID, chat_id="stub-chat-1", timestamp="2026-09-28T09:12:44Z"):
    """One recorded-shaped search response, as O06 documents its fields."""
    return {
        "items": [{
            "id": message_id,
            "accountID": ACCOUNT_ID,
            "chatID": chat_id,
            "senderID": sender,
            "senderName": "Alex Rivera",
            "sortKey": f"stub-sort-{message_id}",
            "timestamp": timestamp,
            "text": text,
            "type": "TEXT",
            "attachments": [],
        }],
        "chats": {chat_id: {"id": chat_id, "accountID": ACCOUNT_ID,
                            "network": "Stub Network", "title": "Stub Group",
                            "type": "group"}},
        "hasMore": has_more,
        "newestCursor": "stub-cursor-newer",
        "oldestCursor": cursor or "stub-cursor-older",
    }


def contacts_page():
    return {
        "items": [{
            "id": CONTACT_ID,
            "cannotMessage": False,
            "email": CONTACT_EMAIL,
            "fullName": "Alex Rivera",
            "imgURL": CONTACT_IMAGE,
            "isSelf": False,
            "phoneNumber": CONTACT_PHONE,
            "username": "alex.rivera",
        }],
        "hasMore": False,
        "newestCursor": "stub-contact-newer",
        "oldestCursor": "stub-contact-older",
    }


class StubBeeper:
    """A local stand-in responder for the documented Beeper endpoints.

    It is **not** Beeper Desktop: it exists so the adapter's wire layer can be exercised on
    a host that is not a Mac, and every document produced through it is marked
    ``stand_in``/``responder: stand_in_http_server`` and can never claim a real source. It
    records what it was asked (method, path, whether an Authorization header was present
    and its scheme, the form field *names*) and never the token value.
    """

    def __init__(self, *, introspect_active=True, search_pages=None, contacts=None,
                 info=None, status_overrides=None):
        self.introspect_active = introspect_active
        self.search_pages = search_pages or {"first": search_page()}
        self.contacts = contacts if contacts is not None else contacts_page()
        self.info = info if info is not None else {"server": "stub", "version": "0.0.0-stub",
                                                   "endpointUrls": {"search": "/v1/messages/search"}}
        self.status_overrides = dict(status_overrides or {})
        self.requests: list = []
        self.httpd = None
        self.thread = None
        self.base_url = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "StubBeeper":
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # never echo a request line to stderr
                return

            def _respond(self, status: int, payload) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
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
                body = self.rfile.read(length).decode("utf-8") if length else ""
                authorization = self.headers.get("Authorization") or ""
                stub.requests.append({
                    "method": self.command,
                    "path": parsed.path,
                    "query_fields": sorted(urllib.parse.parse_qs(parsed.query)),
                    "query": urllib.parse.parse_qs(parsed.query),
                    "authorization_present": bool(authorization),
                    "authorization_scheme": authorization.split(" ", 1)[0],
                    "content_type": self.headers.get("Content-Type"),
                    "form_field_names": sorted(urllib.parse.parse_qs(body)),
                })
                override = stub.status_overrides.get(parsed.path)
                if override:
                    return self._respond(int(override), {"error": "stub override"})
                if parsed.path == "/v1/info":
                    return self._respond(200, stub.info)
                if parsed.path == "/oauth/introspect":
                    return self._respond(200, {"active": stub.introspect_active})
                if parsed.path == "/v1/messages/search":
                    query = urllib.parse.parse_qs(parsed.query)
                    page = (query.get("cursor") or ["first"])[0]
                    return self._respond(200, stub.search_pages.get(
                        page, stub.search_pages.get("first", search_page())))
                if "{" in parsed.path:
                    # A documented path *template* reached the wire unsubstituted. That is
                    # a defect in the worker, not a state of the source, so the stub
                    # refuses it loudly rather than answering.
                    return self._respond(400, {"error": "unsubstituted path template"})
                if parsed.path.startswith("/v1/accounts/") and \
                        parsed.path.endswith("/contacts/list"):
                    return self._respond(200, stub.contacts)
                return self._respond(404, {"error": "the stub serves the documented paths only"})

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

    def paths(self) -> list:
        return [r["path"] for r in self.requests]


class BeeperStandInCase(unittest.TestCase):
    """A stub responder plus the environment the worker's own runbook uses."""

    stub_kwargs: dict = {}

    def setUp(self) -> None:
        self._env = {name: os.environ.get(name)
                     for name in (T.TOKEN_ENV, T.BASE_URL_ENV, T.STANDIN_ENV)}
        self.stub = StubBeeper(**self.stub_kwargs).start()
        os.environ[T.BASE_URL_ENV] = self.stub.base_url
        os.environ[T.STANDIN_ENV] = "1"
        os.environ[T.TOKEN_ENV] = SYNTHETIC_TOKEN

    def tearDown(self) -> None:
        self.stub.stop()
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # -- helpers -----------------------------------------------------------
    def adapter(self, **kw):
        """The real adapter pointed at the stub. ``stand_in`` is what opens the host gate."""
        return build_adapter(base_url=self.stub.base_url, stand_in=True, **kw)

    def document(self, outcome) -> str:
        return O.emit(outcome.to_dict())

    def beeper_run(self, adapter, **kw):
        """A Beeper-only probe run: the measured rows plus the pack rows for the rest."""
        run = run_probe([adapter], only_source="beeper", beeper_account_id=ACCOUNT_ID, **kw)
        self.assertTrue(run.ok, run.harness_errors)
        names = [r["capability"] for r in run.rows]
        self.assertEqual(len(names), len(set(names)),
                         "one row per capability key, per run")
        self.assertTrue(set(MEASURED_CAPABILITIES) <= set(names),
                        "every capability this adapter measures is in the run")
        # The five Beeper capabilities no adapter in this worker can measure come back as
        # the documentation read the pack records -- and stay unmeasured.
        for row in run.rows:
            if row["capability"] not in MEASURED_CAPABILITIES:
                with self.subTest(capability=row["capability"], whence="documented only"):
                    self.assertEqual(row["origin"], O.DOCUMENTATION)
                    self.assertEqual(row["state"], O.PROBE_UNMEASURED)
                    self.assertFalse(row["supported"])
                    self.assertTrue(O.is_documentation_label(row["label"]))
        return run

    def beeper_rows(self, adapter, **kw):
        """Just the rows this adapter measures, which is what most tests are about."""
        run = self.beeper_run(adapter, **kw)
        return [r for r in run.rows if r["capability"] in MEASURED_CAPABILITIES]


# ------------------------------------------------- the token never leaks --------

class TestTheTokenValueIsNeverEmitted(BeeperStandInCase):
    def test_no_document_carries_the_token_value(self) -> None:
        adapter = self.adapter()
        outcomes = [adapter.token_status(), adapter.info(), adapter.introspect(),
                    adapter.search(query="stub", limit=5),
                    adapter.contacts(ACCOUNT_ID, limit=5)]
        for outcome in outcomes:
            with self.subTest(operation=outcome.adapter, code=outcome.code):
                document = self.document(outcome)
                self.assertNotIn(SYNTHETIC_TOKEN, document, document[:400])
                self.assertNotIn(SYNTHETIC_TOKEN, outcome.detail or "")
                self.assertNotIn(SYNTHETIC_TOKEN, outcome.next_action or "")
                self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(outcome.data or {}))

    def test_no_probe_row_carries_the_token_value(self) -> None:
        for row in self.beeper_rows(self.adapter()):
            with self.subTest(capability=row["capability"]):
                self.assertNotIn(SYNTHETIC_TOKEN, O.emit(row))

    def test_a_refusal_carries_the_reason_and_not_the_token(self) -> None:
        self.stub.status_overrides["/v1/info"] = 401
        outcome = self.adapter().info()
        self.assertEqual(outcome.code, O.PERMISSION_DENIED)
        self.assertNotIn(SYNTHETIC_TOKEN, self.document(outcome))
        self.assertNotIn(SYNTHETIC_TOKEN, outcome.next_action or "")
        self.assertIn(T.TOKEN_ENV, outcome.next_action or "",
                      "the next action names the variable, never a value")

    def test_the_cli_never_prints_the_token(self) -> None:
        for args in (("beeper", "token"), ("beeper", "introspect"), ("beeper", "search"),
                     ("probe",)):
            with self.subTest(command=args):
                proc = self.run_cli(*args)
                self.assertNotIn(SYNTHETIC_TOKEN, proc.stdout)
                self.assertNotIn(SYNTHETIC_TOKEN, proc.stderr)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_the_cli_never_prints_the_token_on_a_rejected_token(self) -> None:
        self.stub.status_overrides["/v1/messages/search"] = 401
        proc = self.run_cli("beeper", "search")
        self.assertNotIn(SYNTHETIC_TOKEN, proc.stdout)
        self.assertNotIn(SYNTHETIC_TOKEN, proc.stderr)

    def test_the_stub_was_asked_with_the_documented_bearer_header_and_not_the_value(self) -> None:
        adapter = self.adapter()
        adapter.info()
        adapter.introspect()
        first, second = self.stub.requests[0], self.stub.requests[1]
        self.assertTrue(first["authorization_present"])
        self.assertEqual(first["authorization_scheme"], "Bearer")     # O11's literal form
        self.assertEqual(first["query_fields"], [])
        # O11 documents a form-encoded body: the field *names* are recorded, never values.
        self.assertIn("token", second["form_field_names"])
        self.assertEqual(second["content_type"], "application/x-www-form-urlencoded")

    def run_cli(self, *args, fixture=False) -> subprocess.CompletedProcess:
        argv = [sys.executable, "-m", "switchboard_mini"]
        if fixture:
            argv.append("--fixture-mode")
        argv.extend(args)
        env = dict(os.environ, PYTHONPATH=str(MINI))
        env["SWITCHBOARD_MINI_STATE"] = os.path.join(
            tempfile.mkdtemp(prefix="mini-beeper-state-"), "state.json")
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
                              cwd=str(MINI))


# --------------------------------------------------- token present / absent -----

class TestTokenPresenceIsReportedWithoutTheValue(BeeperStandInCase):
    def test_a_present_token_is_reported_as_present(self) -> None:
        outcome = self.adapter().token_status()
        self.assertTrue(outcome.usable, outcome.detail)
        self.assertIs(outcome.data["token_present"], True)
        self.assertIs(outcome.data["token_value_recorded"], False)
        self.assertEqual(outcome.data["token_source"], f"environment {T.TOKEN_ENV}")
        self.assertNotIn(SYNTHETIC_TOKEN, self.document(outcome))

    def test_an_absent_token_refuses_before_any_request_is_made(self) -> None:
        os.environ.pop(T.TOKEN_ENV, None)                 # nothing is configured
        from switchboard_mini.beeper_adapter import BeeperReadOnlyAdapter
        adapter = BeeperReadOnlyAdapter(T.HttpBeeperTransport(
            base_url=self.stub.base_url, token="", stand_in=True))
        status = adapter.token_status()
        self.assertEqual(status.code, O.PERMISSION_DENIED)
        self.assertEqual(status.reason, "token_absent")
        self.assertFalse(status.data["token_present"])
        for outcome in (adapter.info(), adapter.search(query="stub"),
                        adapter.contacts(ACCOUNT_ID)):
            with self.subTest(code=outcome.code):
                self.assertEqual(outcome.code, O.PERMISSION_DENIED)
                self.assertEqual(outcome.reason, "token_absent")
                self.assertNotIn(SYNTHETIC_TOKEN, self.document(outcome))
        self.assertEqual(self.stub.requests, [],
                         "a missing token was reported without contacting anything")


# ------------------------------------------- the asks the pack does not answer ---

class TestUndocumentedAsksAreRefused(BeeperStandInCase):
    def test_accounts_and_chats_refuse_with_the_smallest_next_action(self) -> None:
        adapter = self.adapter()
        for outcome in (adapter.accounts(), adapter.chats()):
            with self.subTest(operation=outcome.adapter):
                self.assertEqual(outcome.code, O.UNSUPPORTED)
                self.assertEqual(outcome.reason, "endpoint_not_in_pack")
                self.assertTrue(outcome.next_action.startswith("on the Mac"))
                self.assertIn("/v1/info", outcome.next_action)
                self.assertIn("will not guess", outcome.next_action)
        self.assertEqual(self.stub.requests, [],
                         "a refusal for an undocumented endpoint contacts nothing")

    def test_the_transport_refuses_an_operation_that_is_not_in_the_pack(self) -> None:
        transport = self.adapter().transport
        for operation in ("send", "chats", "accounts", "events"):
            with self.subTest(operation=operation):
                outcome = transport.call(operation)
                self.assertEqual(outcome.code, O.UNSUPPORTED)
                self.assertEqual(outcome.reason, "endpoint_not_in_pack")
        self.assertEqual(self.stub.paths(), [])


# ------------------------------------------------------------------ search ------

class TestTheBoundedSearch(BeeperStandInCase):
    def test_the_limit_is_clamped_to_the_documented_maximum(self) -> None:
        adapter = self.adapter(max_pages=1)
        asked = adapter.search(query="stub", limit=250)
        self.assertEqual(asked.data["limit_requested"], 250)
        self.assertEqual(asked.data["limit_sent"], 20)      # O06 maximum
        self.assertTrue(asked.data["limit_clamped"])
        self.assertEqual(asked.data["limit_max_documented"], 20)
        sent = [r for r in self.stub.requests if r["path"] == "/v1/messages/search"][0]
        self.assertEqual(sent["query"]["limit"], ["20"],
                         "the clamped value is what went on the wire")

    def test_a_bounded_sweep_reports_partial_history_and_never_completeness(self) -> None:
        pages = {"first": search_page(cursor="stub-cursor-page2", has_more=True),
                 "stub-cursor-page2": search_page(cursor="stub-cursor-page3", has_more=True,
                                                  message_id="stub-message-2")}
        self.stub.search_pages = pages
        outcome = self.adapter(max_pages=1).search(query="stub", limit=20, sweep=True)
        self.assertEqual(outcome.code, O.PARTIAL, outcome.detail)
        self.assertEqual(outcome.data["coverage"]["coverage_state"], O.COVERAGE_PARTIAL_HISTORY)
        self.assertTrue(outcome.data["coverage"]["gap_reason"])
        self.assertFalse(outcome.data["coverage"]["reached_end_of_query"])
        self.assertTrue(outcome.data["has_more"])
        self.assertTrue(outcome.data["next_cursor"])
        self.assertIn("not proof of", outcome.data["coverage"]["note"])

    def test_a_small_limit_is_raised_to_the_documented_minimum(self) -> None:
        outcome = self.adapter().search(query="stub", limit=0)
        self.assertEqual(outcome.data["limit_sent"], 1)
        self.assertTrue(outcome.data["limit_clamped"])

    def test_a_cursor_resumes_the_same_query(self) -> None:
        self.stub.search_pages = {
            "first": search_page(cursor="stub-cursor-page2", has_more=True),
            "stub-cursor-page2": search_page(has_more=False, message_id="stub-message-2"),
        }
        adapter = self.adapter(max_pages=1)
        first = adapter.search(query="stub", limit=20)
        cursor = first.data["next_cursor"]
        self.assertTrue(cursor)
        second = adapter.search(query="stub", limit=20, cursor=cursor)
        self.assertTrue(second.usable, second.detail)
        self.assertTrue(second.data["cursor_used"])
        ids = [i["source_message_id"] for i in second.data["items"]]
        self.assertEqual(ids, ["stub-message-2"])
        resumed = [r for r in self.stub.requests if r["query"].get("cursor")]
        self.assertEqual(len(resumed), 1, "the cursor was resumed exactly once")

    def test_a_cursor_from_another_query_is_refused_not_resumed(self) -> None:
        self.stub.search_pages = {
            "first": search_page(cursor="stub-cursor-page2", has_more=True),
            "stub-cursor-page2": search_page(has_more=False, message_id="stub-message-2"),
        }
        adapter = self.adapter(max_pages=1)
        cursor = adapter.search(query="alpha", limit=20).data["next_cursor"]
        requests_before = len(self.stub.requests)
        outcome = adapter.search(query="beta", limit=20, cursor=cursor)
        self.assertEqual(outcome.code, O.PERMANENT_ERROR, outcome.detail)
        self.assertEqual(outcome.reason, "cursor_scope_mismatch")
        self.assertIn("refusing to resume across scopes", outcome.detail)
        self.assertEqual(len(self.stub.requests), requests_before,
                         "a foreign cursor is refused before anything is asked")

    def test_a_malformed_cursor_is_refused(self) -> None:
        outcome = self.adapter().search(query="stub", cursor="not-a-cursor|at|all|here")
        self.assertEqual(outcome.reason, "malformed_cursor")
        self.assertEqual(outcome.code, O.PERMANENT_ERROR)

    def test_an_undocumented_direction_is_refused(self) -> None:
        outcome = self.adapter().search(query="stub", direction="sideways")
        self.assertEqual(outcome.reason, "bad_direction")
        self.assertIn("after", outcome.detail)


# ----------------------------------------- nothing raw leaves the worker --------

class TestContactsAndMessagesAreMasked(BeeperStandInCase):
    def test_contacts_mask_addresses_and_withhold_image_urls(self) -> None:
        outcome = self.adapter().contacts(ACCOUNT_ID, limit=500)
        self.assertTrue(outcome.usable, outcome.detail)
        self.assertEqual(outcome.data["limit_sent"], 200)          # O12 maximum
        item = outcome.data["items"][0]
        self.assertEqual(item["full_name"], "Alex Rivera")         # the name is not an address
        self.assertNotIn(CONTACT_EMAIL, item["email_masked"])
        self.assertTrue(item["email_masked"].endswith("@example.com"))
        self.assertNotIn(CONTACT_PHONE, item["phone_masked"])
        self.assertTrue(item["phone_masked"].endswith("01"),
                        "the mask keeps the shape, not the number")
        self.assertIs(item["image_url_present"], True)
        self.assertTrue(item["identifier_fingerprint"].startswith("sha256:"))
        document = self.document(outcome)
        for raw in (CONTACT_EMAIL, CONTACT_PHONE, CONTACT_IMAGE, CONTACT_ID):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, document)
        self.assertIs(outcome.data["addresses_masked"], True)
        self.assertIs(outcome.data["image_urls_withheld"], True)

    def test_the_contacts_path_template_is_resolved_before_it_reaches_the_wire(self) -> None:
        """O12's path has a {accountID} placeholder; a literal one on the wire is a defect."""
        outcome = self.adapter().contacts(ACCOUNT_ID, limit=5)
        self.assertTrue(outcome.usable, outcome.detail)
        expected = f"/v1/accounts/{ACCOUNT_ID}/contacts/list"
        self.assertEqual(self.stub.paths(), [expected])
        self.assertNotIn("{accountID}", self.stub.paths()[0])
        self.assertEqual(outcome.data["path_resolved"], expected)
        self.assertEqual(outcome.data["parameters_sent"], ["limit"], 
                         "the account id is a path parameter, not a query field")

    def test_message_text_is_reduced_to_a_length_and_a_fingerprint(self) -> None:
        outcome = self.adapter().search(query="stub", limit=5)
        item = outcome.data["items"][0]
        self.assertEqual(item["text_state"], "present")
        self.assertEqual(item["text_length"], len(MESSAGE_TEXT))
        self.assertTrue(item["text_fingerprint"].startswith("sha256:"))
        self.assertNotIn(MESSAGE_TEXT, item["text_fingerprint"])
        self.assertTrue(item["sender_id_fingerprint"].startswith("sha256:"))
        document = self.document(outcome)
        # Identifiers and timestamps are what a read document carries; the *text* and the
        # sender's own id are not, which is what these two assertions pin.
        for raw in (MESSAGE_TEXT, SENDER_ID):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, document)

    def test_a_stand_in_row_can_never_be_supported(self) -> None:
        rows = {r["capability"]: r for r in self.beeper_rows(self.adapter())}
        for name, row in rows.items():
            with self.subTest(capability=name):
                self.assertFalse(row["supported"])
                self.assertFalse(row["real_source_connected"],
                                 "a stub responder is not the source")
                self.assertFalse(row["values_from_source"])
                self.assertEqual(row["origin"], O.REAL)      # the real adapter spoke
                self.assertIs(row["evidence"]["token_value_recorded"], False)
        reachability = rows["beeper_local_api_reachability"]
        self.assertEqual(reachability["evidence"]["responder"], "stand_in_http_server")
        self.assertEqual(reachability["limitation"] and "responder was 'stand_in_http_server'"
                         in reachability["limitation"], True)


# ------------------------------------------------------ recorded scenarios ------

class TestTheRecordedScenarios(unittest.TestCase):
    """The four recorded scenarios: the states a stub cannot honestly produce."""

    SCENARIOS = ("healthy", "token_rejected", "beeper_down", "history_limited")

    def fixture_adapter(self, scenario):
        return build_adapter(fixture_mode=True, fixture_scenario=scenario)

    def fixture_rows(self, scenario):
        """The four rows the adapter measures. The rest of the run are pack reads."""
        run = run_probe([self.fixture_adapter(scenario)], only_source="beeper",
                        beeper_account_id=ACCOUNT_ID)
        self.assertTrue(run.ok, run.harness_errors)
        measured = [r for r in run.rows if r["capability"] in MEASURED_CAPABILITIES]
        self.assertEqual(len(measured), len(MEASURED_CAPABILITIES), run.rows)
        return measured

    def test_a_fixture_run_still_reports_the_capabilities_it_cannot_measure(self) -> None:
        """Being in a Beeper run is not a measurement: the pack's rows stay pack rows."""
        run = run_probe([self.fixture_adapter("healthy")], only_source="beeper",
                        beeper_account_id=ACCOUNT_ID)
        documented = [r for r in run.rows if r["capability"] not in MEASURED_CAPABILITIES]
        self.assertTrue(documented, "the documented Beeper capabilities are in this run")
        for row in documented:
            with self.subTest(capability=row["capability"]):
                self.assertEqual(row["origin"], O.DOCUMENTATION)
                self.assertEqual(row["state"], O.PROBE_UNMEASURED)
                self.assertFalse(row["supported"])
                self.assertTrue(O.is_documentation_label(row["label"]))
                self.assertTrue(row["citations"])

    def test_every_fixture_row_is_labelled_and_never_supported(self) -> None:
        for scenario in self.SCENARIOS:
            rows = self.fixture_rows(scenario)
            with self.subTest(scenario=scenario):
                self.assertEqual(len(rows), len(MEASURED_CAPABILITIES))
            for row in rows:
                with self.subTest(scenario=scenario, capability=row["capability"]):
                    self.assertEqual(row["origin"], O.FIXTURE)
                    self.assertFalse(row["supported"],
                                     "a recorded scenario is never a measurement")
                    self.assertFalse(row["real_source_connected"])
                    self.assertTrue(O.is_fixture_label(row["label"]), row["label"])
                    self.assertIn("FIXTURE:", row["label"])
                    disclaimer = row["disclaimer"] or ""
                    self.assertIn("No Beeper Desktop was contacted", disclaimer)
                    self.assertIn("not observations of any real Beeper install", disclaimer)

    def test_a_fixture_row_can_never_satisfy_the_precondition_of_support(self) -> None:
        """``supported`` implies a real source answered, which a fixture twin cannot be."""
        for scenario in self.SCENARIOS:
            for row in self.fixture_rows(scenario):
                with self.subTest(scenario=scenario, capability=row["capability"]):
                    if row["supported"]:
                        self.assertTrue(row["real_source_connected"],
                                        "nothing may be supported without a real source")
                    self.assertFalse(row["real_source_connected"],
                                     "a fixture twin never contacted a source")
                    self.assertEqual(row["origin"], O.FIXTURE)

    def test_beeper_down_is_offline(self) -> None:
        adapter = self.fixture_adapter("beeper_down")
        for outcome in (adapter.info(), adapter.search(query="stub"),
                        adapter.contacts(ACCOUNT_ID)):
            with self.subTest(code=outcome.code):
                self.assertEqual(outcome.code, O.OFFLINE)
                self.assertEqual(outcome.reason, "beeper_not_reachable")
                self.assertIn("Beeper Desktop", outcome.next_action or "")
        states = {r["capability"]: r["state"] for r in self.fixture_rows("beeper_down")}
        self.assertEqual(set(states.values()), {O.OFFLINE}, states)

    def test_a_rejected_token_is_permission_denied_with_the_401(self) -> None:
        adapter = self.fixture_adapter("token_rejected")
        for outcome in (adapter.info(), adapter.search(query="stub"),
                        adapter.contacts(ACCOUNT_ID)):
            with self.subTest(code=outcome.code):
                self.assertEqual(outcome.code, O.PERMISSION_DENIED)
                self.assertEqual(outcome.reason, "token_rejected")
                self.assertEqual(outcome.data["http_status"], 401)
        states = {r["capability"]: r["state"] for r in self.fixture_rows("token_rejected")}
        self.assertEqual(set(states.values()), {O.PERMISSION_DENIED}, states)

    def test_a_live_401_from_the_stub_is_recorded_the_same_way(self) -> None:
        stub = StubBeeper(status_overrides={"/v1/info": 401,
                                            "/v1/messages/search": 401,
                                            "/v1/accounts/%s/contacts/list" % ACCOUNT_ID: 401})
        stub.start()
        try:
            from switchboard_mini.beeper_adapter import BeeperReadOnlyAdapter
            adapter = BeeperReadOnlyAdapter(T.HttpBeeperTransport(
                base_url=stub.base_url, token=SYNTHETIC_TOKEN, stand_in=True))
            for outcome in (adapter.info(), adapter.search(query="stub"),
                            adapter.contacts(ACCOUNT_ID)):
                with self.subTest(code=outcome.code):
                    self.assertEqual(outcome.code, O.PERMISSION_DENIED)
                    self.assertEqual(outcome.reason, "token_rejected")
                    self.assertEqual(outcome.data["http_status"], 401)
                    self.assertNotIn(SYNTHETIC_TOKEN, O.emit(outcome.to_dict()))
        finally:
            stub.stop()

    def test_history_limited_never_claims_complete_coverage(self) -> None:
        adapter = self.fixture_adapter("history_limited")
        outcome = adapter.search(limit=20, sweep=True)
        self.assertEqual(outcome.code, O.PARTIAL, outcome.detail)
        coverage = outcome.data["coverage"]
        self.assertEqual(coverage["coverage_state"], O.COVERAGE_PARTIAL_HISTORY)
        self.assertFalse(coverage["reached_end_of_query"])
        self.assertIn("Message history might be limited", coverage["gap_reason"] or "",
                      "the gap reason is O05's own recorded limitation")
        self.assertTrue(outcome.data["has_more"])
        rows = {r["capability"]: r for r in self.fixture_rows("history_limited")}
        history = rows["beeper_history_depth"]
        self.assertFalse(history["supported"])
        self.assertEqual(history["evidence"]["coverage_state"], O.COVERAGE_PARTIAL_HISTORY)

    def test_healthy_records_what_it_saw_and_still_claims_nothing(self) -> None:
        rows = {r["capability"]: r for r in self.fixture_rows("healthy")}
        search = rows["beeper_message_search"]
        self.assertFalse(search["supported"])
        self.assertTrue(search["evidence"]["cursor_offered"])
        self.assertIs(search["evidence"]["message_text_recorded"], False)
        self.assertIs(search["evidence"]["ids_are_fingerprinted"], True)
        # The row names the documentation row it replaces, and quotes no page itself.
        for row in rows.values():
            with self.subTest(capability=row["capability"]):
                self.assertEqual(row["citations"], [])
                self.assertEqual(row["supersedes"]["origin"], O.DOCUMENTATION)
                self.assertTrue(row["supersedes"]["citations"])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
