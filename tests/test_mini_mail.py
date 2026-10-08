"""Mini worker: the Mail adapter's reads, its cursors and its read-only guarantee.

Everything here runs against recorded fixtures on this Linux computer. What this file
proves: the adapter's paging, cursor and typed-failure behaviour is correct *given a
transport*, and that the whole worker is wired together. What it does not prove: that
Mail.app answers the AppleScript those tests never run. That is Gate 2 on Randy's Mac.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from switchboard_mini import outcomes as O
from switchboard_mini.applescript import (AE_ERROR_MAP, ScriptResult, classify,
                                          parse_ae_code)
from switchboard_mini.mail_adapter import (MailReadOnlyAdapter, account_key,
                                           build_adapter, decode_cursor, encode_cursor,
                                           make_ref, mailbox_of, parse_ref, scope_ref)
from switchboard_mini.mail_transport import (FIXTURE_SCENARIOS, load_fixture,
                                            fixture_path)
from switchboard_mini.runloop import CursorStore, run_once

ACCOUNT = "FIXTURE Account A"
MAILBOX = "INBOX"
REPO_MINI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "mini")


def adapter_for(scenario: str = "granted", **kw) -> MailReadOnlyAdapter:
    return build_adapter(fixture_mode=True, fixture_scenario=scenario, **kw)


class TestNamespacing(unittest.TestCase):
    def test_reference_round_trip(self) -> None:
        ref = make_ref(ACCOUNT, "Mailbox With Spaces", "105")
        self.assertEqual(parse_ref(ref), (ACCOUNT, "Mailbox With Spaces", "105"))
        self.assertTrue(ref.startswith("mail:"))

    def test_scope_reference_round_trip(self) -> None:
        self.assertEqual(mailbox_of(scope_ref("Work Stuff")), "Work Stuff")

    def test_bad_references_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_ref("beeper:chat:1")
        with self.assertRaises(ValueError):
            mailbox_of("chat:1")


class TestEnumeration(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = adapter_for()

    def test_a_complete_page_reports_complete_coverage(self) -> None:
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=10)
        self.assertTrue(out.succeeded)
        self.assertEqual(out.data["coverage"]["coverage_state"], O.COVERAGE_COMPLETE)
        self.assertEqual(out.data["coverage"]["mailbox_total_count"], 5)
        self.assertEqual(out.data["coverage"]["observed_count"], 5)
        self.assertIsNone(out.data["next_cursor"])
        self.assertIsNone(out.data["coverage"]["gap_reason"])
        for item in out.data["items"]:
            self.assertTrue(item["namespaced_id"].startswith("mail:"))
            self.assertEqual(item["time_basis"], "local_time_on_mini")
            self.assertIsNone(item["utc_offset_minutes"])

    def test_limits_are_bounded_and_reported(self) -> None:
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=0)
        self.assertEqual(out.data["coverage"]["observed_count"], 1)
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=99999)
        self.assertLessEqual(out.data["coverage"]["observed_count"], 500)

    def test_addresses_are_masked_not_copied(self) -> None:
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=5)
        for item in out.data["items"]:
            sender = item["sender_masked"]
            self.assertNotIn("alex.rivera@", sender)
            self.assertIn("***", sender)

    def test_an_unknown_scope_is_a_typed_error_not_an_exception(self) -> None:
        out = self.adapter.enumerate(ACCOUNT, "chat:1")
        self.assertFalse(out.usable)
        self.assertEqual(out.code, O.PERMANENT_ERROR)


class TestResumableCursor(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = adapter_for()

    def _ids(self, out) -> list:
        return [i["internal_id"] for i in out.data["items"]]

    def test_walking_the_mailbox_yields_every_message_once(self) -> None:
        seen: list = []
        cursor = None
        pages = 0
        while True:
            out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2, cursor=cursor)
            self.assertTrue(out.usable, out.detail)
            page_ids = self._ids(out)
            self.assertEqual(set(seen) & set(page_ids), set(), "pages must not overlap")
            seen.extend(page_ids)
            pages += 1
            cursor = out.data["next_cursor"]
            if cursor is None:
                break
            self.assertLess(pages, 10, "the cursor did not terminate")
        self.assertEqual(seen, ["101", "102", "103", "104", "105"])

    def test_the_last_page_is_complete_and_the_intermediate_ones_are_partial(self) -> None:
        first = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2)
        self.assertEqual(first.code, O.PARTIAL)
        self.assertEqual(first.data["coverage"]["coverage_state"], O.COVERAGE_PARTIAL_HISTORY)
        self.assertIn("more messages remain", first.data["coverage"]["gap_reason"])
        last = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2,
                                      cursor=first.data["next_cursor"])
        third = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2,
                                       cursor=last.data["next_cursor"])
        self.assertTrue(third.succeeded)
        self.assertIsNone(third.data["next_cursor"])
        self.assertEqual(self._ids(third), ["105"])

    def test_a_cursor_survives_a_restart_of_the_worker(self) -> None:
        first = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2)
        fresh = adapter_for()                       # a new process, in effect
        resumed = fresh.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2,
                                  cursor=first.data["next_cursor"])
        self.assertEqual(self._ids(resumed), ["103", "104"])

    def test_a_malformed_cursor_is_refused_without_reading_anything(self) -> None:
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2, cursor="junk")
        self.assertEqual(out.code, O.PERMANENT_ERROR)
        self.assertEqual(out.reason, "malformed_cursor")
        self.assertIsNone(out.data)

    def test_a_cursor_from_another_scope_is_refused(self) -> None:
        foreign = encode_cursor("Other Account", MAILBOX, 1, "101")
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2, cursor=foreign)
        self.assertEqual(out.code, O.PERMANENT_ERROR)
        self.assertEqual(out.reason, "cursor_scope_mismatch")

    def test_a_cursor_the_mailbox_moved_under_is_reported_not_ignored(self) -> None:
        stale = encode_cursor(ACCOUNT, MAILBOX, 2, "999999")
        self.assertEqual(decode_cursor(stale)[2], 2)
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=2, cursor=stale)
        self.assertEqual(out.code, O.PARTIAL)
        self.assertEqual(out.reason, "cursor_reset")
        self.assertTrue(out.data["cursor_reset"])
        self.assertIn("overlap", out.detail)
        self.assertEqual([i["internal_id"] for i in out.data["items"]], ["101", "102"])
        self.assertEqual(out.data["coverage"]["observed_count"], 2)


class TestPartialHistory(unittest.TestCase):
    """Reaching the end of the scan is not reaching the end of the mailbox (PRD §6)."""

    def setUp(self) -> None:
        self.adapter = adapter_for("partial_history")

    def test_a_capped_scan_is_partial_with_a_gap_reason(self) -> None:
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=10)
        self.assertEqual(out.code, O.PARTIAL)
        coverage = out.data["coverage"]
        self.assertEqual(coverage["coverage_state"], O.COVERAGE_PARTIAL_HISTORY)
        self.assertTrue(coverage["scan_capped"])
        self.assertEqual(coverage["mailbox_total_count"], 9)
        self.assertEqual(coverage["observed_count"], 4)
        self.assertTrue(coverage["gap_reason"])
        self.assertIn("4 of 9", coverage["gap_reason"])
        # the four messages that were read are still usable: a partial outage must not
        # suppress data that did arrive
        self.assertEqual(len(out.data["items"]), 4)

    def test_the_adapter_declares_its_own_coverage_state(self) -> None:
        declared = self.adapter.coverage_declaration(ACCOUNT, scope_ref(MAILBOX))
        self.assertTrue(declared.usable)
        self.assertEqual(declared.data["coverage_state"], O.COVERAGE_PARTIAL_HISTORY)
        self.assertEqual(declared.data["mailbox_total_count"], 9)
        self.assertTrue(declared.data["gap_reason"])

    def test_progress_beyond_the_scan_window_is_not_invented(self) -> None:
        out = self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX), limit=10)
        self.assertIsNone(out.data["next_cursor"],
                          "the scan ended at the cap; there is nothing honest to point "
                          "the next page at")


class TestRetrieval(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = adapter_for()

    def test_a_full_retrieval_carries_headers_body_and_attachments(self) -> None:
        out = self.adapter.retrieve(ACCOUNT, make_ref(ACCOUNT, MAILBOX, "101"))
        self.assertTrue(out.succeeded)
        self.assertIsNotNone(out.data["body"])
        self.assertEqual(out.data["body_state"], "fetched")
        self.assertEqual(out.data["message"]["internal_id"], "101")
        self.assertEqual(out.data["message"]["rfc_message_id"],
                         "<fixture-101@example-mail.test>")
        self.assertEqual(len(out.data["attachments"]), 1)
        self.assertFalse(out.data["attachments"][0]["downloaded"])

    def test_unavailable_body_is_not_an_empty_message(self) -> None:
        unavailable = self.adapter.retrieve(ACCOUNT, make_ref(ACCOUNT, MAILBOX, "102"))
        self.assertEqual(unavailable.code, O.PARTIAL)
        self.assertEqual(unavailable.reason, "content_unavailable")
        self.assertIsNone(unavailable.data["body"])
        self.assertEqual(unavailable.data["body_state"], "unavailable")
        self.assertTrue(unavailable.data["unavailable_reason"])
        self.assertIn("not an empty message", unavailable.detail)

    def test_a_genuinely_empty_body_is_reported_as_empty(self) -> None:
        empty = self.adapter.retrieve(ACCOUNT, make_ref(ACCOUNT, MAILBOX, "104"))
        self.assertTrue(empty.succeeded)
        self.assertEqual(empty.data["body_state"], "empty")
        self.assertEqual(empty.data["body"], "")

    def test_an_unavailable_attachment_is_reported_per_item(self) -> None:
        out = self.adapter.retrieve(ACCOUNT, make_ref(ACCOUNT, MAILBOX, "101"))
        self.assertEqual(out.data["attachment_state"], O.PARTIAL)
        self.assertEqual(out.data["attachments"][0]["downloaded"], False)

    def test_a_retrieval_miss_after_a_complete_scan_is_permanent(self) -> None:
        out = self.adapter.retrieve(ACCOUNT, make_ref(ACCOUNT, MAILBOX, "999999"))
        self.assertEqual(out.code, O.PERMANENT_ERROR)
        self.assertEqual(out.reason, "unknown_message_reference")
        self.assertTrue(out.data["scanned_all"])
        self.assertEqual(out.data["mailbox_total_count"], 5)

    def test_a_retrieval_miss_inside_a_capped_scan_claims_nothing(self) -> None:
        capped = adapter_for("partial_history")
        out = capped.retrieve(ACCOUNT, make_ref(ACCOUNT, MAILBOX, "900"))
        self.assertEqual(out.code, O.PARTIAL)
        self.assertEqual(out.reason, "not_in_scanned_window")
        self.assertIn("proves only", out.detail)
        self.assertEqual(out.data["mailbox_total_count"], 9)

    def test_a_reference_for_another_account_is_refused(self) -> None:
        out = self.adapter.retrieve(ACCOUNT, make_ref("Other Account", MAILBOX, "101"))
        self.assertEqual(out.code, O.PERMANENT_ERROR)
        self.assertEqual(out.reason, "account_mismatch")

    def test_a_malformed_reference_is_refused(self) -> None:
        out = self.adapter.retrieve(ACCOUNT, "not-a-reference")
        self.assertEqual(out.code, O.PERMANENT_ERROR)
        self.assertEqual(out.reason, "bad_reference")


class TestPermissionDeniedAndOffline(unittest.TestCase):
    def test_permission_denied_is_typed_at_every_read(self) -> None:
        adapter = adapter_for("permission_denied")
        for out in (adapter.accounts(), adapter.mailboxes(ACCOUNT),
                    adapter.enumerate(ACCOUNT, scope_ref(MAILBOX)),
                    adapter.retrieve(ACCOUNT, make_ref(ACCOUNT, MAILBOX, "101")),
                    adapter.health(ACCOUNT)):
            self.assertEqual(out.code, O.PERMISSION_DENIED, out.detail)
            self.assertEqual(out.data.get("ae_code"), -1743)

    def test_offline_is_typed_and_no_cursor_could_advance(self) -> None:
        adapter = adapter_for("offline")
        out = adapter.enumerate(ACCOUNT, scope_ref(MAILBOX))
        self.assertEqual(out.code, O.OFFLINE)
        self.assertFalse(out.usable)
        # the typed outcome may carry evidence, but it must never carry a page of data
        self.assertNotIn("items", out.data or {})
        self.assertEqual(out.data.get("ae_code"), -600)
        self.assertEqual(adapter.health(ACCOUNT).code, O.OFFLINE)


class TestReadOnlyGuarantee(unittest.TestCase):
    """No outbound path exists in this worker, and nothing here is submitted."""

    def setUp(self) -> None:
        self.adapter = adapter_for()

    def test_every_mutating_operation_is_unsupported(self) -> None:
        ref = make_ref(ACCOUNT, MAILBOX, "101")
        calls = {
            "prepare_draft": self.adapter.prepare_draft(ACCOUNT, {"draft_id": "d1"}),
            "dispatch": self.adapter.dispatch(ACCOUNT, {"idempotency_key": "k1"}),
            "reconcile": self.adapter.reconcile(ACCOUNT, idempotency_key="k1"),
            "materialize_attachment": self.adapter.materialize_attachment(ACCOUNT, ref),
        }
        for name, out in calls.items():
            self.assertEqual(out.code, O.UNSUPPORTED, f"{name} must be unsupported")
            self.assertFalse(out.submitted, f"{name} must not report a submission")
            self.assertTrue(out.detail)
            self.assertTrue(out.to_dict()["adapter"] == "mail")

    def test_no_read_ever_reports_a_submitted_side_effect(self) -> None:
        outcomes = [self.adapter.accounts(), self.adapter.health(ACCOUNT),
                    self.adapter.enumerate(ACCOUNT, scope_ref(MAILBOX)),
                    self.adapter.retrieve(ACCOUNT, make_ref(ACCOUNT, MAILBOX, "101")),
                    self.adapter.history_poll(ACCOUNT, scope_ref(MAILBOX)),
                    self.adapter.change_poll(ACCOUNT),
                    self.adapter.coverage_declaration(ACCOUNT, scope_ref(MAILBOX))]
        for out in outcomes:
            self.assertFalse(out.submitted)

    def test_change_poll_is_unsupported_because_mail_has_no_change_feed(self) -> None:
        out = self.adapter.change_poll(ACCOUNT)
        self.assertEqual(out.code, O.UNSUPPORTED)
        self.assertEqual(out.reason, "no_change_feed")

    def test_history_poll_says_what_it_is(self) -> None:
        out = self.adapter.history_poll(ACCOUNT, scope_ref(MAILBOX), limit=3)
        self.assertTrue(out.usable)
        self.assertIn("no server-side change feed", out.data["poll_basis"])


class TestRealHostBehaviour(unittest.TestCase):
    """On a non-macOS host the real adapter reports unsupported. It never raises."""

    def test_real_adapter_is_typed_unsupported_here(self) -> None:
        if sys.platform == "darwin":     # pragma: no cover - this computer is Linux
            self.skipTest("this computer is the Linux stand-in for Grace")
        adapter = build_adapter(fixture_mode=False)
        out = adapter.accounts()
        self.assertEqual(out.code, O.UNSUPPORTED)
        self.assertEqual(out.reason, "host_not_macos")
        self.assertEqual(out.origin, O.REAL)
        # The real adapter is selected, but nothing was contacted: no osascript ran, no
        # mailbox was read. `real_source_connected` must not be overloaded to mean "this
        # is the real adapter" (it means "a real source was contacted", everywhere).
        payload = out.to_dict()
        self.assertTrue(payload["adapter_is_real"])
        self.assertFalse(payload["source_contacted"])
        self.assertFalse(payload["real_source_connected"])
        self.assertFalse(payload["fixture_mode"])
        self.assertIn("linux", out.detail)

    def test_the_fixture_path_is_never_reported_as_real(self) -> None:
        adapter = adapter_for()
        self.assertEqual(adapter.accounts().origin, O.FIXTURE)
        self.assertFalse(adapter.accounts().to_dict()["real_source_connected"])


class TestAppleEventClassification(unittest.TestCase):
    """One mapping table, used by the real transport and the recorded one alike."""

    def _classify(self, returncode: int, stderr: str) -> str:
        result = ScriptResult(returncode=returncode, stdout="", stderr=stderr,
                              duration_ms=1, script_kind="message_window")
        return classify(result, adapter="mail").code

    def test_permission_denied(self) -> None:
        self.assertEqual(
            self._classify(1, 'execution error: Not authorized to send Apple events to '
                              'Mail. (-1743)'), O.PERMISSION_DENIED)

    def test_mail_not_running_is_offline(self) -> None:
        self.assertEqual(
            self._classify(1, "execution error: Mail got an error: Application isn't "
                              "running. (-600)"), O.OFFLINE)

    def test_apple_event_timeout_is_retryable(self) -> None:
        self.assertEqual(
            self._classify(1, "execution error: The Apple event timed out. (-1712)"),
            O.RETRYABLE_ERROR)

    def test_unhandled_event_is_unsupported(self) -> None:
        self.assertEqual(
            self._classify(1, "execution error: Mail got an error: event not handled "
                              "(-1708)"), O.UNSUPPORTED)

    def test_missing_object_is_permanent(self) -> None:
        self.assertEqual(
            self._classify(1, "execution error: Mail got an error: Can't get message 9. "
                              "(-1728)"), O.PERMANENT_ERROR)

    def test_an_unknown_code_is_recorded_not_guessed_as_transient(self) -> None:
        result = ScriptResult(returncode=1, stdout="",
                              stderr="execution error: something new (-4242)",
                              duration_ms=1, script_kind="accounts")
        out = classify(result, adapter="mail")
        self.assertEqual(out.code, O.PERMANENT_ERROR)
        self.assertEqual(out.reason, "unclassified_automation_error")
        self.assertEqual(out.data["ae_code"], -4242)
        self.assertIn("add this error number", out.next_action)

    def test_a_timeout_result_is_retryable(self) -> None:
        result = ScriptResult(returncode=-1, stdout="", stderr="osascript did not return",
                              duration_ms=1000, script_kind="message_ids", timed_out=True)
        out = classify(result, adapter="mail")
        self.assertEqual(out.code, O.RETRYABLE_ERROR)
        self.assertEqual(out.reason, "script_timeout")

    def test_success_is_not_an_error(self) -> None:
        result = ScriptResult(returncode=0, stdout="ok", stderr="", duration_ms=3,
                              script_kind="accounts")
        self.assertTrue(classify(result, adapter="mail").succeeded)

    def test_every_mapped_code_produces_a_typed_outcome(self) -> None:
        for code in AE_ERROR_MAP:
            outcome_code = self._classify(1, f"execution error: mapped -{abs(code)} ({code})")
            self.assertIn(outcome_code, O.ADAPTER_OUTCOMES)

    def test_error_code_parsing(self) -> None:
        self.assertEqual(parse_ae_code("execution error: nope (-1743)"), -1743)
        self.assertEqual(parse_ae_code("execution error: nope (-1743)\n"), -1743)
        self.assertIsNone(parse_ae_code("no code here"))


class TestRunLoop(unittest.TestCase):
    """The foreground loop advances its cursor only after a usable read."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._tmp.name, "state.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_successful_poll_persists_a_resumable_cursor(self) -> None:
        store = CursorStore(self.state)
        adapter = adapter_for()
        batch = run_once(adapter, account=ACCOUNT, mailbox=MAILBOX, limit=2, store=store)
        self.assertEqual(batch["outcome"]["code"], O.PARTIAL)
        self.assertTrue(batch["cursor_advanced"])
        cursor = store.get(ACCOUNT, scope_ref(MAILBOX))
        self.assertTrue(cursor)
        with open(self.state, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["version"], 1)

    def test_the_second_poll_resumes_where_the_first_stopped(self) -> None:
        store = CursorStore(self.state)
        adapter = adapter_for()
        first = run_once(adapter, account=ACCOUNT, mailbox=MAILBOX, limit=2, store=store)
        second = run_once(adapter, account=ACCOUNT, mailbox=MAILBOX, limit=2,
                          store=CursorStore(self.state))
        first_ids = [i["internal_id"] for i in first["outcome"]["data"]["items"]]
        second_ids = [i["internal_id"] for i in second["outcome"]["data"]["items"]]
        self.assertEqual(first_ids, ["101", "102"])
        self.assertEqual(second_ids, ["103", "104"])
        self.assertNotEqual(first["cursor_used"], second["cursor_used"])

    def test_a_failed_poll_leaves_the_cursor_untouched(self) -> None:
        store = CursorStore(self.state)
        good = adapter_for()
        run_once(good, account=ACCOUNT, mailbox=MAILBOX, limit=2, store=store)
        before = store.get(ACCOUNT, scope_ref(MAILBOX))

        offline = adapter_for("offline")
        batch = run_once(offline, account=ACCOUNT, mailbox=MAILBOX, limit=2, store=store)
        self.assertEqual(batch["outcome"]["code"], O.OFFLINE)
        self.assertFalse(batch["cursor_advanced"])
        self.assertEqual(store.get(ACCOUNT, scope_ref(MAILBOX)), before,
                         "a typed failure must never advance the cursor")
        entry = store.snapshot()["scopes"][f"{ACCOUNT}|{scope_ref(MAILBOX)}"]
        self.assertEqual(entry["last_outcome"], O.OFFLINE)
        self.assertEqual(entry["cursor"], before)

    def test_caught_up_is_recorded(self) -> None:
        store = CursorStore(self.state)
        adapter = adapter_for()
        batch = run_once(adapter, account=ACCOUNT, mailbox=MAILBOX, limit=50, store=store)
        self.assertTrue(batch["outcome"]["code"] == O.SUCCESS)
        self.assertIsNone(store.get(ACCOUNT, scope_ref(MAILBOX)))
        entry = store.snapshot()["scopes"][f"{ACCOUNT}|{scope_ref(MAILBOX)}"]
        self.assertTrue(entry["caught_up"])

    def test_a_corrupt_state_file_does_not_stop_the_worker(self) -> None:
        with open(self.state, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        store = CursorStore(self.state)
        self.assertIsNone(store.get(ACCOUNT, scope_ref(MAILBOX)))
        batch = run_once(adapter_for(), account=ACCOUNT, mailbox=MAILBOX, limit=2,
                         store=store)
        self.assertTrue(batch["cursor_advanced"])

    def test_labelled_state_is_reported_by_the_run_loop_cli(self) -> None:
        env = dict(os.environ, SWITCHBOARD_MINI_STATE=self.state,
                   PYTHONPATH=REPO_MINI)
        proc = subprocess.run(
            [sys.executable, "-m", "switchboard_mini", "--fixture-mode", "run", "--once",
             "--limit", "2", "--account", ACCOUNT, "--mailbox", MAILBOX],
            capture_output=True, text=True, env=env, cwd=REPO_MINI)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        events = [d["event"] for d in documents]
        self.assertEqual(events, ["worker_start", "poll", "worker_stop"])
        poll = documents[1]
        self.assertEqual(poll["outcome"]["origin"], O.FIXTURE)
        self.assertFalse(poll["outcome"]["real_source_connected"])
        self.assertTrue(O.is_fixture_label(poll["outcome"]["label"]))
        self.assertEqual(poll["outcome"]["code"], O.PARTIAL)


class TestWorkerContract(unittest.TestCase):
    """The worker's vocabulary must not drift from the one Grace consumes."""

    def test_outcome_codes_match_grace(self) -> None:
        from grace import contracts as C
        self.assertEqual(O.ADAPTER_OUTCOMES, C.ADAPTER_OUTCOMES)
        self.assertEqual(O.SUCCESS, C.SUCCESS)
        self.assertEqual(O.PARTIAL, C.PARTIAL)
        self.assertEqual(O.OUTCOME_UNKNOWN, C.OUTCOME_UNKNOWN)

    def test_every_fixture_scenario_exists_and_is_labelled(self) -> None:
        for scenario in FIXTURE_SCENARIOS:
            self.assertTrue(os.path.exists(fixture_path(scenario)))
            fixture = load_fixture(scenario)
            self.assertEqual(fixture["scenario"], scenario)
            self.assertEqual(fixture["origin"], O.FIXTURE)
            self.assertIn("not a recording", fixture["note"].lower())

    def test_no_fixture_contains_a_real_address_or_secret(self) -> None:
        for scenario in FIXTURE_SCENARIOS:
            with open(fixture_path(scenario), "r", encoding="utf-8") as handle:
                text = handle.read()
            for forbidden in ("password", "token", "api_key", "secret", "BEGIN PRIVATE"):
                self.assertNotIn(forbidden, text.lower(),
                                 f"{scenario} must not carry credentials")
            for address in ("@gmail.com", "@icloud.com", "@outlook.com"):
                self.assertNotIn(address, text)

    def test_account_keys_are_stable_and_not_the_account_name(self) -> None:
        key = account_key(ACCOUNT)
        self.assertEqual(key, account_key(ACCOUNT))
        self.assertNotIn("FIXTURE", key)
        self.assertTrue(key.startswith("acct_"))


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
