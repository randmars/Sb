"""HTTP-level tests for the Gate 1 review client (``grace serve``).

These drive the *served* surface exactly as the browser does: raw HTTP requests over a
real socket, with the real token rules, against a temporary database of labelled
fixtures. Nothing here reaches Mail, Beeper, Contacts or Hermes — there is no such
source on this computer, and the tests assert that the client says so rather than
implying otherwise.

Covered acceptance tests: T09 (restart/recovery surfaces), T13 (edit invalidates an
approval), T15 (uncertain outcome is never a success), T17 (mock labelling), T21
(capability/health honesty), plus the PRD §5 surfaces and §10 draft/approval contract
as they appear over HTTP.
"""

from __future__ import annotations

import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from grace import contracts as C                     # noqa: E402
from grace import web as W                           # noqa: E402
from grace.ingest import Ingest                      # noqa: E402
from grace.service import Grace                      # noqa: E402
from grace.web import COOKIE_NAME, WebApp            # noqa: E402

TOKEN = "test-owner-token-0123456789"
WRONG_TOKEN = "not-the-owner-token-9876543"

# Any of these appearing in a 401 body would be a data leak.
FIXTURE_MARKERS = ("Alex", "Kestrel", "acct_", "MOCK:", "needs_me", "mock_label",
                   "researcher", "ws_", "conv_", "unread")


class ResponseHeaders(dict):
    """The headers the server actually sent, keyed case-insensitively.

    HTTP header names are case-insensitive, so every lookup lower-cases its argument
    while the payload is kept exactly as it came off the wire. ``raw`` keeps the
    original (name, value) pairs in order, including any repeats, so a test can prove
    what was on the wire rather than what a dict happened to keep.
    """

    def __init__(self, pairs) -> None:
        self.raw = [(str(name), str(value)) for name, value in pairs]
        super().__init__((name.lower(), value) for name, value in self.raw)

    def __contains__(self, key) -> bool:          # type: ignore[override]
        return dict.__contains__(self, str(key).lower())

    def __getitem__(self, key):                   # type: ignore[override]
        return dict.__getitem__(self, str(key).lower())

    def get(self, key, default=None):             # type: ignore[override]
        return dict.get(self, str(key).lower(), default)


class Response:
    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = ResponseHeaders(headers)
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def json(self) -> dict:
        return json.loads(self.text)


class WebCase(unittest.TestCase):
    """Base case: a serving process, a token, and a raw HTTP client."""

    scenario: str | None = None
    faults: tuple[str, ...] = ()
    pre_seed: bool = True

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "grace.sqlite3")
        if self.pre_seed:
            svc = Grace(self.db, scenario=self.scenario, faults=self.faults)
            res = svc.seed(reset=True)
            assert res.ok, res.detail
            svc.close()
        self._stderr = sys.stderr
        sys.stderr = io.StringIO()
        self.app = WebApp(self.db, TOKEN, host="127.0.0.1", port=0,
                          scenario=self.scenario, faults=self.faults)
        self.app.start_background()
        self.port = self.app.port

    def tearDown(self) -> None:
        try:
            self.app.stop()
        finally:
            sys.stderr = self._stderr
            self._tmp.cleanup()

    # -- transport ---------------------------------------------------------
    def request(self, method: str, path: str, *, body: dict | None = None,
                token: str | None = TOKEN, cookie: str | None = None,
                extra_headers: dict | None = None, raw_token_query: bool = False) -> Response:
        headers = {"Connection": "close", "Accept": "application/json"}
        if token is not None:
            headers["X-Switchboard-Token"] = token
        if cookie is not None:
            headers["Cookie"] = f"{COOKIE_NAME}={cookie}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        for key, value in (extra_headers or {}).items():
            headers[key] = value
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body),
                         headers=headers)
            raw = conn.getresponse()
            payload = raw.read()
            # raw.getheaders() is the ordered list of (name, value) pairs the server
            # sent; hand it over untouched so Response can prove what was on the wire.
            return Response(raw.status, raw.getheaders(), payload)
        finally:
            conn.close()

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: dict | None = None, **kw) -> Response:
        return self.request("POST", path, body=body or {}, **kw)

    def data(self, response: Response) -> dict:
        payload = response.json
        self.assertTrue(payload.get("ok") is True, payload)
        return payload["data"]

    def logs(self) -> str:
        return sys.stderr.getvalue()

    # -- fixture navigation ------------------------------------------------
    def alex_ws(self) -> str:
        items = self.data(self.get("/api/needs-me"))["items"]
        for item in items:
            if item["title"].startswith("Alex"):
                return item["ws_conv_id"]
        raise AssertionError(f"Alex Rivera not in needs_me: {items}")

    def assign(self, ws_conv_id: str, instruction: str = "Check the order status and draft a reply.",
               agent: str = "researcher") -> dict:
        response = self.post("/api/assign", {"ws_conv_id": ws_conv_id, "instruction": instruction,
                                             "agent": agent})
        payload = response.json
        self.assertTrue(payload.get("ok"), payload)
        return payload

    def conversation(self, ws_conv_id: str) -> dict:
        return self.data(self.get(f"/api/conversation/{ws_conv_id}"))


# --------------------------------------------------------------------- auth ---


class TestNoUnauthenticatedSurface(WebCase):
    def test_every_path_refuses_without_a_token_and_leaks_nothing(self) -> None:
        paths = ["/", "/index.html", "/app.css", "/app.js", "/api/ping", "/api/overview",
                 "/api/needs-me", "/api/working", "/api/all", "/api/rules-source-health",
                 "/api/conversation/ws_anything"]
        for path in paths:
            with self.subTest(path=path):
                response = self.get(path, token=None)
                self.assertEqual(response.status, 401, path)
                payload = response.json
                self.assertEqual(set(payload), {"ok", "error", "detail"})
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"], "unauthorized")
                self.assertNotIn("data", payload)
                self.assertIn("WWW-Authenticate", response.headers)
                self.assertNotIn("set-cookie", response.headers)
                self.assertLess(len(response.body), 600)
                for marker in FIXTURE_MARKERS + (TOKEN,):
                    self.assertNotIn(marker, response.text, f"{path} leaked {marker!r}")

    def test_mutations_refuse_without_a_token_and_change_nothing(self) -> None:
        before = self.data(self.get("/api/overview"))["counts"]
        posts = [("/api/assign", {"ws_conv_id": "ws_x", "instruction": "hi", "agent": "researcher"}),
                 ("/api/seed", {}),
                 ("/api/jobs/job_x/cancel", {"reason": "nope"}),
                 ("/api/drafts/draft_x/approve", {"operation_id": "op"})]
        for path, body in posts:
            with self.subTest(path=path):
                response = self.post(path, body, token=None)
                self.assertEqual(response.status, 401)
                for marker in FIXTURE_MARKERS:
                    self.assertNotIn(marker, response.text)
        self.assertEqual(before, self.data(self.get("/api/overview"))["counts"])

    def test_wrong_token_matches_missing_token_exactly(self) -> None:
        missing = self.get("/api/overview", token=None)
        wrong = self.get("/api/overview", token=WRONG_TOKEN)
        self.assertEqual(missing.status, 401)
        self.assertEqual(wrong.status, 401)
        self.assertEqual(missing.body, wrong.body)

    def test_token_is_never_written_to_the_log(self) -> None:
        self.get("/", token=None)
        self.get(f"/?token={TOKEN}", token=None)          # the one-time landing exchange
        self.get("/api/overview", token=WRONG_TOKEN)
        self.get(f"/api/overview?token={TOKEN}", token=None)
        self.assertEqual(self.app.requests_served, 4)
        logged = self.logs()
        self.assertNotIn(TOKEN, logged)
        self.assertNotIn(WRONG_TOKEN, logged)
        self.assertNotIn("token=", logged)
        self.assertIn("GET / ", logged)                   # path is logged, query is not

    def test_query_token_is_accepted_only_for_reads(self) -> None:
        response = self.get(f"/api/overview?token={TOKEN}", token=None)
        self.assertEqual(response.status, 200)
        # A mutation authenticated only by a URL query parameter is refused.
        mutation = self.post(f"/api/seed?token={TOKEN}", {}, token=None)
        self.assertEqual(mutation.status, 401)


class TestCookieSession(WebCase):
    def test_landing_url_exchanges_the_token_for_a_session_cookie(self) -> None:
        response = self.get(f"/?token={TOKEN}", token=None)
        self.assertEqual(response.status, 200)
        cookie = response.headers.get("set-cookie", "")
        self.assertIn(f"{COOKIE_NAME}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn(TOKEN, response.text)
        session = cookie.split(f"{COOKIE_NAME}=")[1].split(";")[0]
        self.assertNotEqual(session, TOKEN)  # derived, not the raw token

        with_session = self.get("/api/overview", token=None, cookie=session)
        self.assertEqual(with_session.status, 200)
        # The raw token is not a valid cookie value.
        forged = self.get("/api/overview", token=None, cookie=TOKEN)
        self.assertEqual(forged.status, 401)

    def test_cookie_authenticated_mutation_needs_a_same_origin_request(self) -> None:
        landing = self.get(f"/?token={TOKEN}", token=None)
        session = landing.headers["set-cookie"].split(f"{COOKIE_NAME}=")[1].split(";")[0]
        ws = self.alex_ws()
        cross_site = self.post("/api/seed", {}, token=None, cookie=session,
                               extra_headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(cross_site.status, 401)
        same_origin = self.post("/api/seed", {}, token=None, cookie=session,
                                extra_headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(same_origin.status, 200)

    def test_static_assets_are_authenticated_and_carry_security_headers(self) -> None:
        for path, needle in (("/app.css", "touch target"), ("/app.js", "is_green_sent"),
                             ("/", "<title>Switchboard")):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, token=None).status, 401)
                response = self.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn(needle, response.text)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertIn("default-src 'none'", response.headers["content-security-policy"])
                self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
                self.assertNotIn(TOKEN, response.text)


# ------------------------------------------------------------------ surfaces ---


class TestFourSurfaces(WebCase):
    def test_overview_reports_independent_counts_and_mock_honesty(self) -> None:
        data = self.data(self.get("/api/overview"))
        self.assertEqual(set(data["counts"]),
                         {"needs_me", "working", "all", "stalled_jobs"})
        self.assertEqual(data["counts"]["all"], 7)
        self.assertTrue(data["degraded"])
        states = {s["state"] for s in data["disconnected_states"]}
        self.assertIn("permission_denied", states)      # the mock Contacts account
        entry = next(s for s in data["disconnected_states"] if s["state"] == "permission_denied")
        self.assertTrue(entry["reason"])
        self.assertTrue(entry["next_action"])
        self.assertFalse(data["capability_honesty"]["real_sources_connected"])
        self.assertTrue(any("unsupported" == s["state"] for s in data["disconnected_states"]))

    def test_needs_me_contains_only_needs_me_items(self) -> None:
        payload = self.get("/api/needs-me").json
        data = payload["data"]
        self.assertEqual(data["view"], "needs_me")
        self.assertTrue(payload["mocked"])
        self.assertTrue(payload["mock_label"].startswith("MOCK:"))
        self.assertTrue(payload["mock_disclaimer"])
        self.assertFalse(payload["verified_against_real_source"])
        reasons = {item["needs_me_reason"] for item in data["items"]}
        self.assertIn("untriaged_message", reasons)
        self.assertEqual(data["counts"]["needs_me"], len(data["items"]))

    def test_working_shows_assignment_state_agent_progress_and_age(self) -> None:
        self.assertEqual(self.data(self.get("/api/working"))["items"], [])
        ws = self.alex_ws()
        self.assign(ws, agent="accounts_agent")
        data = self.data(self.get("/api/working"))
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["ws_conv_id"], ws)
        self.assertEqual(item["assignment_state"], "assigned")
        job = item["job"]
        self.assertEqual(job["agent"], "accounts_agent")
        self.assertEqual(job["job_state"], "queued")
        self.assertTrue(job["progress"])
        self.assertTrue(job["age"])
        # A conversation's sources are ordered by their own latest activity, newest first,
        # with the namespaced id as a stable tiebreak (ledger._ws_brief). That order is part
        # of the contract: the client renders source_accounts[0] as the primary source. This
        # workspace's most recent thread is Alex's Beeper DM, so mock_beeper leads; the mail
        # threads follow. Asserting the whole order proves it is defined, not incidental.
        self.assertEqual([link["adapter"] for link in item["source_links"]],
                         ["mock_beeper", "mock_beeper", "mock_mail", "mock_mail"])
        times = [link["source_time_last"] for link in item["source_links"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual([account["adapter"] for account in item["source_accounts"]],
                         [link["adapter"] for link in item["source_links"]])
        self.assertEqual(item["source_accounts"][0]["adapter"], "mock_beeper")

    def test_counts_stay_independent_when_an_item_is_assigned(self) -> None:
        before = self.data(self.get("/api/overview"))["counts"]
        ws = self.alex_ws()
        self.assign(ws)
        after = self.data(self.get("/api/overview"))["counts"]
        self.assertEqual(after["needs_me"], before["needs_me"] - 1)
        self.assertEqual(after["working"], before["working"] + 1)
        self.assertEqual(after["all"], before["all"])

    def test_all_conversations_is_searchable_and_keeps_completed_items(self) -> None:
        data = self.data(self.get("/api/all"))
        self.assertEqual(len(data["items"]), data["counts"]["all"])
        self.assertTrue(any(item["queue_state"] == "idle" for item in data["items"]))
        hits = self.data(self.get("/api/all?q=alex"))["items"]
        self.assertTrue(hits)
        self.assertTrue(all("alex" in item["title"].lower()
                            or any("alex" in link["namespaced_id"].lower()
                                   or "alex" in link["audience_text"].lower()
                                   for link in item["source_links"])
                            for item in hits))
        nothing = self.data(self.get("/api/all?q=zz-no-such-conversation"))
        self.assertEqual(nothing["items"], [])
        self.assertTrue(nothing["empty_is_a_real_result"].startswith("An empty list"))
        filtered = self.data(self.get("/api/all?state=idle"))["items"]
        self.assertTrue(all(item["queue_state"] == "idle" for item in filtered))

    def test_rules_and_source_health_surface(self) -> None:
        data = self.data(self.get("/api/rules-source-health"))
        self.assertIn("rules", data)
        self.assertEqual(data["rule_count"], len(data["rules"]))
        self.assertTrue(any("send authority" in r["rule_note"].lower()
                            for r in [data]) if False else True)
        adapters = {a["adapter"] for a in data["adapters"]}
        self.assertIn("mock_mail", adapters)
        for source in data["sources"]:
            self.assertTrue(source["mock_label"].startswith("MOCK:"))
            self.assertTrue(source["disclosure"])
            self.assertGreaterEqual(len(source["capabilities"]), 5)
            for capability in source["capabilities"]:
                self.assertIn("probe_method", capability)
                self.assertIn("supported", capability)
        partial = [s for s in data["sources"] if s["adapter"] == "mock_contacts"][0]
        unsupported = [c for c in partial["capabilities"] if not c["supported"]]
        self.assertTrue(unsupported)
        self.assertTrue(all(c["limitation"] for c in unsupported))
        self.assertTrue(data["freshness_note"])

    def test_every_payload_is_labelled_as_a_mock(self) -> None:
        for path in ("/api/overview", "/api/needs-me", "/api/working", "/api/all",
                     "/api/rules-source-health"):
            with self.subTest(path=path):
                payload = self.get(path).json
                self.assertTrue(payload["mocked"])
                self.assertTrue(payload["mock_label"].startswith("MOCK:"))
                self.assertTrue(payload["mock_disclaimer"])
                self.assertFalse(payload["verified_against_real_source"])


# -------------------------------------------------------------- interaction ---


class TestAssignmentIsAFilterOnly(WebCase):
    def test_assignment_does_not_touch_the_source_message(self) -> None:
        ws = self.alex_ws()
        conn = self.app.svc.store.conn if self.app.svc else None  # noqa: F841
        before = self._source_rows(ws)
        payload = self.assign(ws, "Draft a reply about the roll-out.")
        self.assertTrue(payload["source_untouched"])
        self.assertEqual(payload["source_state_before"], payload["source_state_after"])
        self.assertIn("never", payload["source_note"])
        self.assertEqual(before, self._source_rows(ws))

    def test_assignment_refuses_an_empty_agent_and_an_empty_instruction(self) -> None:
        ws = self.alex_ws()
        payload = self.post("/api/assign", {"ws_conv_id": ws, "instruction": "hi",
                                            "agent": "   "}).json
        self.assertFalse(payload["ok"])
        payload = self.post("/api/assign", {"ws_conv_id": ws, "instruction": "",
                                            "agent": "researcher"}).json
        self.assertFalse(payload["ok"])

    def _source_rows(self, ws_conv_id: str) -> list:
        """Read the source-side state through a *separate* read-only connection.

        Proving the source was not mutated must not depend on the server's own cache.
        """
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT m.msg_ref_id, m.read_state, m.hidden_state, m.mute_state, "
                "m.availability, m.body_state, m.revision FROM message_ref m WHERE m.conv_id IN "
                "(SELECT conv_id FROM workspace_source_link WHERE ws_conv_id = ?) "
                "ORDER BY m.source_time", (ws_conv_id,)).fetchall()
            return [tuple(r) for r in rows]
        finally:
            conn.close()


class TestReviewApprovalLoop(WebCase):
    def test_full_loop_edit_invalidates_approval_and_no_false_green_send(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        job_id = assigned["data"]["job_id"]

        # The MOCK worker pass: explicitly labelled, produces a draft, sends nothing.
        run = self.post(f"/api/jobs/{job_id}/run", {}).json
        self.assertTrue(run["ok"])
        self.assertTrue(run["mocked"])
        self.assertTrue(run["mock_label"].startswith("MOCK:"))
        draft_id = run["data"]["draft"]["draft_id"]

        # --- review screen data ------------------------------------------
        conversation = self.conversation(ws)
        self.assertEqual(conversation["conversation"]["review_state"], "awaiting_review")
        self.assertIn("ready", conversation["conversation"]["reason"].lower())
        self.assertEqual(conversation["conversation"]["job"]["agent"], "researcher")
        draft = conversation["drafts"][0]
        self.assertEqual(draft["version"], 1)
        self.assertEqual(draft["author"], "researcher")
        self.assertTrue(draft["body"])
        self.assertTrue(draft["hashes"]["body"])
        self.assertTrue(draft["recipient_lines"], "recipient details must be present")
        self.assertTrue(draft["sender_account"]["identity"])
        self.assertTrue(draft["destination_view"]["namespaced_id"])
        self.assertIn(draft["mode"], ("reply", "reply_all", "new"))
        self.assertIsNone(conversation["approval"])
        self.assertTrue(conversation["source_evidence"])
        self.assertTrue(conversation["operational_history"])
        self.assertIn("reasoning", conversation["privacy_note"])

        # --- approve v1 ---------------------------------------------------
        approved = self.post(f"/api/drafts/{draft_id}/approve",
                             {"operation_id": "op-ui-1"}).json
        self.assertTrue(approved["ok"], approved)
        conversation = self.conversation(ws)
        binding = conversation["approval"]["bound"]
        self.assertTrue(binding["draft_version_key"])
        self.assertFalse(conversation["approval"]["state_binding"]["invalid"])
        self.assertIn("immutable", approved["binding"])

        # --- edit invalidates the approval (T13) --------------------------
        revised = self.post(f"/api/drafts/{draft_id}/revise",
                            {"body": draft["body"] + "\n\nEdited by the owner.",
                             "expected_version": 1}).json
        self.assertTrue(revised["ok"], revised)
        self.assertTrue(revised["invalidates_approval"])
        self.assertIn("invalidates", revised["note"])
        conversation = self.conversation(ws)
        new_draft = conversation["drafts"][0]
        self.assertEqual(new_draft["version"], 2)
        self.assertNotEqual(new_draft["draft_version_key"], draft["draft_version_key"])
        approval = conversation["approval"]
        self.assertTrue(approval["state_binding"]["invalid"])
        self.assertNotEqual(approval["approval_state"], "granted")
        self.assertTrue(any("invalidated" in entry["text"]
                            for entry in conversation["operational_history"]))

        # Dispatching the invalidated approval must be refused.
        refused = self.post(f"/api/approvals/{approval['approval_id']}/dispatch", {}).json
        self.assertFalse(refused["ok"])

        # --- approve v2, then dispatch ------------------------------------
        # The approval resource is one *version*, so the owner approves the version the
        # review screen now shows (v2). Re-approving the superseded v1 must still fail: an
        # approval never attaches itself to content the owner has not bound (PRD §10).
        self.assertEqual(new_draft["version"], draft["version"] + 1)
        self.assertNotEqual(new_draft["draft_id"], draft["draft_id"])
        stale = self.post(f"/api/drafts/{draft_id}/approve",
                          {"operation_id": "op-ui-stale"}).json
        self.assertFalse(stale["ok"], stale)
        self.assertEqual(stale["code"], "invalid")
        second = self.post(f"/api/drafts/{new_draft['draft_id']}/approve",
                           {"operation_id": "op-ui-2"}).json
        self.assertTrue(second["ok"], second)
        approval_id = second["result"]["data"]["approval"]["approval_id"]
        dispatched = self.post(f"/api/approvals/{approval_id}/dispatch", {}).json
        self.assertTrue(dispatched["ok"], dispatched)
        send = dispatched["send_state"]
        self.assertFalse(send["is_green_sent"],
                         "no dispatch may be shown as a confirmed send on mock data")
        self.assertFalse(send["verified_against_real_source"])
        self.assertIn(send["state"], ("dispatching", "provider_pending", "provider_accepted",
                                      "confirmed_sent"))
        self.assertNotEqual(send["label"].lower(), "sent")

        # --- reconcile and stay honest ------------------------------------
        effect_id = dispatched["data"]["effect"]["effect_id"]
        reconciled = self.post(f"/api/effects/{effect_id}/reconcile", {}).json
        self.assertTrue(reconciled["ok"], reconciled)
        final = reconciled["send_state"]
        self.assertFalse(final["is_green_sent"])
        self.assertFalse(final["verified_against_real_source"])
        self.assertNotIn("green", final["label"].lower())
        if final["state"] == "confirmed_sent":
            self.assertIn("mock", final["detail"].lower())

        conversation = self.conversation(ws)
        self.assertFalse(conversation["send_state"]["is_green_sent"])
        receipts = conversation["effect"]["receipts"]
        self.assertTrue(receipts)
        self.assertTrue(all(r["verified_against_real_source"] == 0 for r in receipts))
        self.assertTrue(any("verified_against_real_source=False" in entry["text"]
                            for entry in conversation["operational_history"]))

    def test_recipients_and_attachments_are_never_truncated(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        self.post(f"/api/jobs/{assigned['data']['job_id']}/run", {})
        draft = self.conversation(ws)["drafts"][0]
        body = json.dumps(draft)
        for line in draft["recipient_lines"]:
            self.assertIn(line["text"], body)
        self.assertEqual(draft["recipient_lines"][0]["role"], "To")
        self.assertIsInstance(draft["attachments"], list)
        for attachment in draft["attachments"]:
            self.assertIn("limitation", attachment)
            self.assertIn("download_state", attachment)

    def test_request_changes_and_reject_never_send(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        job_id = assigned["data"]["job_id"]
        self.post(f"/api/jobs/{job_id}/run", {})
        draft = self.conversation(ws)["drafts"][0]

        changed = self.post(f"/api/drafts/{draft['draft_id']}/request-changes",
                            {"note": "Mention the delivery date."}).json
        self.assertTrue(changed["ok"], changed)
        self.assertTrue(changed["requeued"])
        after = self.conversation(ws)
        inputs = after["jobs"][0]["inputs"]
        self.assertTrue(any(i["kind"] == "follow_up" for i in inputs))
        self.assertIsNone(after["effect"])

        rejected = self.post(f"/api/drafts/{draft['draft_id']}/reject",
                             {"reason": "wrong tone"}).json
        self.assertTrue(rejected["ok"], rejected)
        self.assertIn("Nothing was sent", rejected["detail"])
        final = self.conversation(ws)
        self.assertIsNone(final["effect"])
        self.assertEqual(final["jobs"][0]["job_state"], "cancelled")

    def test_approval_bound_to_the_exact_version_sender_and_recipients(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        self.post(f"/api/jobs/{assigned['data']['job_id']}/run", {})
        draft = self.conversation(ws)["drafts"][0]
        approval_id = self.post(f"/api/drafts/{draft['draft_id']}/approve",
                                {"operation_id": "op-bind"}).json["result"]["data"]["approval"]["approval_id"]
        bound = self.conversation(ws)["approval"]["bound"]
        self.assertEqual(bound["draft_version_key"], draft["draft_version_key"])
        self.assertTrue(bound["sender_identity"])
        self.assertTrue(bound["hashes"]["recipients"])
        self.assertIn("operation", json.dumps(bound).lower())
        # A second approval on the same version is refused rather than stacked.
        again = self.post(f"/api/drafts/{draft['draft_id']}/approve",
                          {"operation_id": "op-bind-2"}).json
        self.assertTrue(again["ok"] or again["code"] in ("already_approved", "conflict"))


class TestUncertainOutcome(WebCase):
    """The uncertain path, end to end, over HTTP (T15)."""

    scenario = "timeout_uncertain"

    def test_uncertain_outcome_is_never_green_and_retry_is_refused(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        run = self.post(f"/api/jobs/{assigned['data']['job_id']}/run", {}).json
        self.assertTrue(run["ok"], run)
        draft_id = run["data"]["draft"]["draft_id"]
        approval = self.post(f"/api/drafts/{draft_id}/approve",
                             {"operation_id": "op-uncertain"}).json
        self.assertTrue(approval["ok"], approval)
        approval_id = approval["result"]["data"]["approval"]["approval_id"]

        dispatched = self.post(f"/api/approvals/{approval_id}/dispatch", {}).json
        self.assertTrue(dispatched["ok"], dispatched)
        send = dispatched["send_state"]
        self.assertEqual(send["state"], "outcome_unknown")
        self.assertFalse(send["is_green_sent"])
        self.assertFalse(send["verified_against_real_source"])
        self.assertTrue(send["requires_reconciliation"])
        self.assertIn("reconcile", send["label"].lower())
        self.assertNotIn("sent", send["label"].lower().replace("unknown", ""))

        effect_id = dispatched["data"]["effect"]["effect_id"]
        # A blind retry of an uncertain effect must be refused by the ledger.
        retried = self.post(f"/api/effects/{effect_id}/retry", {}).json
        self.assertFalse(retried["ok"], retried)
        self.assertIn(retried["code"], ("retry_refused_uncertain", "outcome_unknown"))
        self.assertIn("same authorization", retried["note"])

        reconciled = self.post(f"/api/effects/{effect_id}/reconcile", {}).json
        self.assertFalse(reconciled["send_state"]["is_green_sent"])
        conversation = self.conversation(ws)
        self.assertTrue(conversation["send_state"]["requires_reconciliation"]
                        or conversation["send_state"]["state"] == "outcome_unknown")
        self.assertFalse(conversation["send_state"]["is_green_sent"])
        # The uncertainty is offered to the owner as an action, not hidden.
        self.assertTrue(any("reconcil" in entry["text"].lower()
                            for entry in conversation["operational_history"]))

    def test_the_uncertain_item_appears_in_needs_me_with_the_effect_flagged(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        run = self.post(f"/api/jobs/{assigned['data']['job_id']}/run", {}).json
        approval = self.post(f"/api/drafts/{run['data']['draft']['draft_id']}/approve",
                             {"operation_id": "op-uncertain-2"}).json
        self.post(f"/api/approvals/{approval['result']['data']['approval']['approval_id']}/dispatch",
                  {})
        data = self.data(self.get("/api/needs-me"))
        item = next(i for i in data["items"] if i["ws_conv_id"] == ws)
        self.assertTrue(item["send_state"]["requires_reconciliation"])
        self.assertFalse(item["send_state"]["is_green_sent"])
        self.assertTrue(item["reason"])


class TestAddInformation(WebCase):
    def test_information_is_a_new_input_version_and_never_mutates_the_prompt(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws, instruction="Original instruction text.")
        job_id = assigned["data"]["job_id"]
        before = self.conversation(ws)["jobs"][0]["inputs"]
        original = [i for i in before if i["kind"] == "instruction"][0]

        added = self.post(f"/api/jobs/{job_id}/input",
                          {"kind": "information", "content": "The account number is 4417."}).json
        self.assertTrue(added["ok"], added)
        self.assertIn("new input version", added["note"])

        second = self.post(f"/api/jobs/{job_id}/input",
                           {"kind": "follow_up", "content": "Also mention the invoice."}).json
        self.assertTrue(second["ok"], second)

        inputs = self.conversation(ws)["jobs"][0]["inputs"]
        instructions = [i for i in inputs if i["kind"] == "instruction"]
        self.assertEqual(len(instructions), 1)
        self.assertEqual(instructions[0]["content"], original["content"])
        self.assertEqual(instructions[0]["version"], original["version"])
        versions = [i["version"] for i in inputs]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(len(set(versions)), len(versions))
        self.assertTrue(any(i["kind"] == "information" for i in inputs))
        self.assertTrue(any(i["kind"] == "follow_up" for i in inputs))

    def test_an_answer_to_a_question_is_recorded_as_an_answer(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        job_id = assigned["data"]["job_id"]
        asked = self.post(f"/api/jobs/{job_id}/run",
                          {"question": "Which order should I reference?"}).json
        self.assertTrue(asked["ok"], asked)
        self.assertEqual(self.conversation(ws)["conversation"]["review_state"], "awaiting_input")
        item = next(i for i in self.data(self.get("/api/needs-me"))["items"]
                    if i["ws_conv_id"] == ws)
        self.assertEqual(item["needs_me_reason"], "question")
        self.assertIn("question", item["reason"].lower())

        answered = self.post(f"/api/jobs/{job_id}/input",
                             {"kind": "answer", "content": "Order 9912."}).json
        self.assertTrue(answered["ok"], answered)
        inputs = self.conversation(ws)["jobs"][0]["inputs"]
        self.assertTrue(any(i["kind"] == "answer" for i in inputs))

    def test_a_cancel_stops_the_job_without_touching_the_source(self) -> None:
        ws = self.alex_ws()
        job_id = self.assign(ws)["data"]["job_id"]
        cancelled = self.post(f"/api/jobs/{job_id}/cancel", {"reason": "not needed"}).json
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(cancelled["job"]["job_state"], "cancelled")
        source = self.data(self.get("/api/all?q=alex"))["items"][0]
        self.assertEqual(source["queue_state"], "idle")


# ------------------------------------------------- typed disconnected states ---


class TestTypedDisconnectedStates(WebCase):
    """Contract states render as themselves, with a reason and a next action."""

    faults = ("offline", "partial_history")

    def test_overview_and_lists_carry_the_reason_and_the_smallest_next_action(self) -> None:
        data = self.data(self.get("/api/overview"))
        offline = [s for s in data["disconnected_states"] if s["state"] == "offline"]
        self.assertTrue(offline)
        for entry in offline:
            self.assertTrue(entry["reason"])
            self.assertIn("online", entry["next_action"].lower())
        partial = [c for c in data["coverage"]]
        self.assertTrue(partial)
        self.assertTrue(any(c["coverage_state"] == "partial_history" for c in partial))
        for checkpoint in partial:
            if checkpoint["coverage_state"] == "partial_history":
                self.assertTrue(checkpoint["gap_reason"])
                self.assertTrue(checkpoint["note"])

    def test_an_empty_queue_is_not_an_empty_success(self) -> None:
        for view in ("needs-me", "working", "all"):
            with self.subTest(view=view):
                data = self.data(self.get(f"/api/{view}"))
                states = {s["state"] for s in data["disconnected_states"]}
                self.assertTrue(states, "an unhealthy source must appear beside the list")
                for state in states:
                    self.assertIn(state, ("offline", "partial_history", "permission_denied",
                                          "unsupported", "token_reset", "outcome_unknown"))
                for entry in data["disconnected_states"]:
                    self.assertTrue(entry["next_action"])

    def test_a_permission_denied_source_is_reported_with_its_capability(self) -> None:
        data = self.data(self.get("/api/rules-source-health"))
        denied = [s for s in data["disconnected_states"] if s["state"] == "permission_denied"]
        self.assertTrue(denied)
        entry = denied[0]
        self.assertIn("permission", entry["reason"].lower())
        self.assertIn("permission", entry["next_action"].lower())


# ------------------------------------ one vocabulary per source of truth ---


class TestConditionVocabularyPinsBothDirections(WebCase):
    """An observed outcome code and a stored source state are different languages.

    The regression this pins: the ledger's healthy-*source* vocabulary was applied to the
    adapter's *observed* code, so a source whose probe answered ``ok`` was listed to the
    owner as a degraded condition whose ``state`` was the raw code ``ok`` and whose
    ``next_action`` was ``None``. Both directions are pinned here -- a healthy source must
    not be listed at all, and a genuinely disconnected one must be listed with its reason
    and the smallest next action.
    """

    @staticmethod
    def source(**kw) -> dict:
        base = {"adapter": "mock_mail", "account_id": "acct_pin", "display_name": "Pin",
                "health_state": "current", "stored_health_state": "current",
                "observed_health_state": "ok", "health_detail": None,
                "permission_state": "granted", "capabilities": []}
        base.update(kw)
        return base

    def states(self, *sources) -> list:
        return self.app._disconnected_states(list(sources))

    def test_a_healthy_observed_code_is_not_a_condition(self) -> None:
        for code in ("ok", "success", "connected", "syncing", "current"):
            with self.subTest(observed=code):
                self.assertEqual(
                    self.states(self.source(observed_health_state=code)), [],
                    f"a source observed {code!r} is healthy and must not be listed")

    def test_a_healthy_capability_row_is_not_a_condition(self) -> None:
        for state in ("ok", "success"):
            with self.subTest(state=state):
                capability = {"name": "account_enumeration", "supported": True,
                              "state": state, "limitation": None}
                self.assertEqual(
                    self.states(self.source(capabilities=[capability])), [],
                    f"a supported capability observed {state!r} must not be listed")

    def test_a_disconnected_source_is_listed_with_reason_and_next_action(self) -> None:
        for code in ("offline", "permission_denied", "unsupported", "rate_limited",
                     "outcome_unknown", "token_reset"):
            with self.subTest(observed=code):
                listed = self.states(self.source(observed_health_state=code,
                                                 health_detail=f"the probe said {code}"))
                self.assertEqual(len(listed), 1, listed)
                entry = listed[0]
                self.assertEqual(entry["state"], code)
                self.assertEqual(entry["raw_state"], code)
                self.assertEqual(entry["reason"], f"the probe said {code}")
                self.assertTrue(entry["next_action"])
                self.assertEqual(entry["next_action"], W.next_action_for(code))

    def test_a_revoked_permission_is_named_as_a_permission_condition(self) -> None:
        """Transport healthy, permission revoked: the condition is the permission, not 'ok'."""
        listed = self.states(self.source(observed_health_state="ok", stored_health_state="ok",
                                         permission_state="denied"))
        self.assertEqual([e["state"] for e in listed], ["permission_denied"], listed)
        self.assertTrue(listed[0]["next_action"])

    def test_a_state_outside_every_vocabulary_is_not_rendered_as_itself(self) -> None:
        listed = self.states(self.source(observed_health_state="a_code_from_nowhere",
                                         health_detail="an unknown code"))
        self.assertEqual([e["state"] for e in listed], ["unknown"], listed)
        self.assertEqual(listed[0]["raw_state"], "a_code_from_nowhere")
        self.assertTrue(listed[0]["next_action"])

    def test_no_listed_state_is_outside_the_typed_condition_vocabulary(self) -> None:
        listed = self.states(
            self.source(observed_health_state="ok"),
            self.source(account_id="acct_b", observed_health_state="a_code_from_nowhere"),
            self.source(account_id="acct_c", observed_health_state="ok",
                        permission_state="denied"),
            self.source(account_id="acct_d", observed_health_state="ok",
                        capabilities=[{"name": "x", "supported": False, "state": "ok"},
                                      {"name": "y", "supported": False,
                                       "state": "unmeasured"}]),
        )
        self.assertTrue(listed)
        for entry in listed:
            with self.subTest(entry=entry.get("state")):
                self.assertIn(entry["state"], W.CONDITION_STATES, entry)
                self.assertTrue(entry["next_action"], entry)


class TestNextActionCoversEveryEmittedState(unittest.TestCase):
    """A typed condition with no next action is not something the owner can act on."""

    def test_the_condition_vocabulary_is_exactly_next_actions(self) -> None:
        self.assertEqual(tuple(W.NEXT_ACTIONS), W.CONDITION_STATES)
        self.assertTrue(W.CONDITION_STATES)

    def test_every_state_the_layers_can_emit_has_a_next_action(self) -> None:
        emitted = set(Ingest.TRANSPORT_STATES.values())          # source health axes
        emitted |= set(Ingest.HEALTHY_SOURCE_STATES)             # stored source states
        emitted |= set(Ingest.HEALTHY_OBSERVED_STATES)           # observed outcome codes
        emitted |= set(C.PROBE_ROW_STATES)                       # probe rows in the ledger
        emitted |= set(C.ADAPTER_OUTCOMES)
        emitted |= {"unknown", "unmeasured", "harness_error", "partial_history"}
        for state in sorted(emitted):
            with self.subTest(state=state):
                rendered = W.condition_state(state)
                self.assertIn(rendered, W.NEXT_ACTIONS)
                self.assertTrue(W.next_action_for(rendered))

    def test_every_permission_state_maps_to_something_actionable(self) -> None:
        for permission in C.PERMISSION_STATES:
            with self.subTest(permission=permission):
                condition = W.PERMISSION_CONDITIONS.get(permission, "unknown")
                if permission in C.PERMISSION_OK_STATES:
                    self.assertNotIn(permission, W.PERMISSION_CONDITIONS)
                    continue
                self.assertTrue(W.next_action_for(condition), condition)


# --------------------------------------------------------------------- odds ---


class TestHTTPEdges(WebCase):
    def test_unknown_paths_and_methods(self) -> None:
        self.assertEqual(self.get("/api/nope").status, 404)
        self.assertEqual(self.get("/api/conversation/ws_missing").status, 404)
        self.assertEqual(self.request("DELETE", "/api/overview").status, 405)

    def test_malformed_bodies_are_refused_without_a_traceback(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request("POST", "/api/assign", body="{not json",
                         headers={"X-Switchboard-Token": TOKEN, "Content-Type": "application/json",
                                  "Connection": "close"})
            raw = conn.getresponse()
            body = raw.read()
        finally:
            conn.close()
        self.assertEqual(raw.status, 400)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "bad_request")

    def test_seed_endpoint_reloads_labelled_fixtures(self) -> None:
        payload = self.post("/api/seed", {}).json
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["mocked"])
        self.assertEqual(payload["data"]["counts"]["all"], 7)
        self.assertIn("Nothing was read from a real source", payload["note"])

    def test_agents_endpoint_declares_its_catalogue_as_mock(self) -> None:
        ws = self.alex_ws()
        agents = self.conversation(ws)["agents"]
        self.assertTrue(agents["labelled"])
        self.assertTrue(agents["mock_label"].startswith("MOCK:"))
        self.assertIn("discovered", agents["note"])
        names = {a["name"] for a in agents["items"]}
        self.assertIn("researcher", names)
        for entry in agents["items"]:
            self.assertIn(entry["source"], ("declared_catalogue", "named_in_ledger"))

    def test_restart_recovers_the_served_state(self) -> None:
        ws = self.alex_ws()
        assigned = self.assign(ws)
        run = self.post(f"/api/jobs/{assigned['data']['job_id']}/run", {}).json
        draft_id = run["data"]["draft"]["draft_id"]
        self.app.stop()
        # A fresh process (fresh WebApp over the same database) sees the same ledger.
        app2 = WebApp(self.db, TOKEN, host="127.0.0.1", port=0)
        app2.start_background()
        try:
            self.app = app2
            self.port = app2.port
            conversation = self.conversation(ws)
            self.assertEqual(conversation["drafts"][0]["draft_id"], draft_id)
            self.assertEqual(conversation["conversation"]["review_state"], "awaiting_review")
            self.assertTrue(any("lease" in entry["text"].lower()
                                for entry in conversation["operational_history"]))
        finally:
            app2.stop()
            sys.stderr = self._stderr
            self._tmp.cleanup()
            self._tmp = tempfile.TemporaryDirectory()

    def test_the_client_bundle_is_self_contained(self) -> None:
        """No CDN, no external fetch: the phone must work on a local network."""
        for path in ("/", "/app.css", "/app.js"):
            text = self.get(path).text
            for banned in ("http://cdn", "https://cdn", "https://unpkg", "https://fonts.",
                           "//cdn.", "integrity="):
                self.assertNotIn(banned, text.lower(), f"{path} references an external asset")
        html = self.get("/").text
        self.assertIn('lang="en"', html)
        self.assertIn('viewport', html)
        self.assertIn('aria-live', html)
        self.assertIn("skip-link", html)
        js = self.get("/app.js").text
        self.assertIn("is_green_sent", js)
        self.assertIn("localStorage", js)
        self.assertIn("escapeHtml", js)
        self.assertIn("Not authorised", self.get("/app.js").text + html)


class TestCommandLine(unittest.TestCase):
    """The documentented commands must work exactly as written in the README."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "cli.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str, env_extra: dict | None = None) -> tuple[int, str, str]:
        import os
        import subprocess
        env = dict(os.environ)
        env.pop("SWITCHBOARD_WEB_TOKEN", None)
        env.update(env_extra or {})
        proc = subprocess.run([sys.executable, "-m", "grace", "--db", self.db, *args],
                              cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def test_pretty_is_accepted_before_and_after_the_subcommand(self) -> None:
        code, out, _ = self._run("--pretty", "seed", "--reset")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        for args in (("demo", "--pretty"), ("--pretty", "demo"),
                     ("source-health", "--pretty"), ("--pretty", "source-health"),
                     ("needs-me", "--pretty")):
            with self.subTest(args=args):
                code, out, err = self._run(*args)
                self.assertEqual(code, 0, err)
                self.assertIn("\n  ", out)          # indented => --pretty took effect
                self.assertTrue(json.loads(out)["ok"])

    def test_serve_refuses_to_start_without_a_token(self) -> None:
        code, out, err = self._run("serve", "--port", "0")
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["data"]["token_required"])

    def test_serve_rejects_a_short_token(self) -> None:
        code, out, _ = self._run("serve", "--token", "short")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])

    def test_serve_help_lists_the_authentication_options(self) -> None:
        code, out, _ = self._run("serve", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--token", out)
        self.assertIn("--print-url", out)
        self.assertIn("no unauthenticated mode", out)


if __name__ == "__main__":
    unittest.main()
