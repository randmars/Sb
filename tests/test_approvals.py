"""Test (c): editing a bound draft field invalidates the approval and blocks dispatch.

Requirement coverage: R10 (owner authorization bound to the sending identity and
recipient set), R12 (approvals persisted independently), R15, PRD §10 draft and
approval contract, T13 ("draft, recipient, group membership or attachment changes
after approval: approval invalidates; dispatch is denied until the changed version is
approved"), T14 ("double-tap approval ... one dispatch decision"), T21 (reply-all and
Bcc risk).
"""

from __future__ import annotations

import unittest

from grace import contracts as C
from tests.helpers import GraceTestCase


class ApprovalBindingTest(GraceTestCase):
    def test_editing_a_bound_field_invalidates_the_approval(self) -> None:
        run = self.assign_and_run()
        draft = run["draft"]
        approval = self.svc.effects.grant_approval(draft["draft_id"], operation_id="op-1")
        self.assertTrue(approval.ok, approval.detail)

        revised = self.svc.effects.revise_draft(draft["draft_id"],
                                                body=draft["body"] + "\nEdited by the owner.")
        self.assertTrue(revised.ok, revised.detail)
        self.assertEqual(2, revised.data["draft"]["version"])
        self.assertEqual([approval.data["approval"]["approval_id"]],
                         revised.data["invalidated_approvals"])

        invalidated = self.svc.effects.approval_view(approval.data["approval"]["approval_id"])
        self.assertEqual(C.ApprovalState.INVALIDATED, invalidated["approval_state"])
        self.assertIn("bound draft version edited", invalidated["invalidation_reason"])

        blocked = self.svc.effects.dispatch(approval.data["approval"]["approval_id"])
        self.assertEqual("denied", blocked.code)
        self.assertEqual(0, self.svc.store.scalar("SELECT COUNT(*) FROM effect_operation"))

        # The changed version can be approved afresh, and only then dispatched.
        second = self.svc.effects.grant_approval(revised.data["draft"]["draft_id"],
                                                 operation_id="op-2")
        self.assertTrue(second.ok)
        dispatched = self.svc.effects.dispatch(second.data["approval"]["approval_id"])
        self.assertTrue(dispatched.ok, dispatched.detail)
        self.assertEqual(1, self.svc.store.scalar("SELECT COUNT(*) FROM effect_operation"))

    def test_changed_source_audience_invalidates_the_approval(self) -> None:
        """A new source message with a different audience must invalidate approval (T13)."""
        run = self.assign_and_run()
        draft = run["draft"]
        approval = self.svc.effects.grant_approval(draft["draft_id"], operation_id="op-3")
        self.assertTrue(approval.ok)
        self.assertTrue(self.svc.effects.revalidate_approval(
            approval.data["approval"]["approval_id"]).ok)

        # Ingest a later message in the same conversation whose sender differs.
        conv = self.svc.store.one("SELECT * FROM source_conversation WHERE conv_id = ?",
                                  (draft["destination"]["conv_id"],))
        corpus_msg = dict(next(m for m in self.svc.corpus["messages"]
                               if m["namespaced_id"].endswith("beeper-alex-2")))
        corpus_msg = dict(corpus_msg)
        corpus_msg.update({
            "msg_ref_id": C.stable_id("msg", "new-arrival"),
            "namespaced_id": conv["namespaced_id"] + ":new-arrival",
            "provider_message_id": "beeper-alex-3",
            "source_time": "2026-10-06T23:59:00.000000Z",
            "sender": {"network_identity": "@alex.someone.else:example-matrix.test",
                       "display_name": "Alex (different identity)"},
        })
        self.svc.ingest.project_message(corpus_msg)

        valid = self.svc.effects.revalidate_approval(approval.data["approval"]["approval_id"])
        self.assertEqual("denied", valid.code)
        self.assertIn("audience changed", valid.detail)
        blocked = self.svc.effects.dispatch(approval.data["approval"]["approval_id"])
        self.assertEqual("denied", blocked.code)
        self.assertEqual(0, self.svc.store.scalar("SELECT COUNT(*) FROM effect_operation"))

    def test_expired_approval_cannot_dispatch(self) -> None:
        run = self.assign_and_run()
        approval = self.svc.effects.grant_approval(run["draft"]["draft_id"],
                                                   operation_id="op-4", ttl_seconds=-5)
        self.assertTrue(approval.ok)
        expired = self.svc.effects.expire_approvals()
        self.assertEqual([approval.data["approval"]["approval_id"]], expired)
        denied = self.svc.effects.dispatch(approval.data["approval"]["approval_id"])
        self.assertEqual("denied", denied.code)
        self.assertEqual(0, self.svc.store.scalar("SELECT COUNT(*) FROM effect_operation"))

    def test_double_tap_produces_exactly_one_dispatch_decision(self) -> None:
        run = self.assign_and_run()
        approval_id = self.svc.effects.grant_approval(run["draft"]["draft_id"],
                                                      operation_id="op-5").data["approval"]["approval_id"]
        first = self.svc.effects.dispatch(approval_id)
        second = self.svc.effects.dispatch(approval_id)
        self.assertTrue(first.ok)
        self.assertEqual("idempotent_replay", second.code)
        self.assertEqual(1, self.svc.store.scalar(
            "SELECT COUNT(*) FROM effect_operation WHERE operation_id = ?", ("op-5",)))
        self.assertEqual(1, self.svc.store.scalar("SELECT COUNT(*) FROM effect_operation"))
        # Attempt history shows the dispatch attempted once, not twice.
        self.assertEqual(1, self.svc.store.scalar(
            "SELECT COUNT(*) FROM effect_attempt WHERE phase <> 'reconciliation'"))
        # Repeating the approval grant is also a single decision, not a second approval.
        again = self.svc.effects.grant_approval(run["draft"]["draft_id"], operation_id="op-5")
        self.assertEqual("idempotent_replay", again.code)
        self.assertEqual(1, self.svc.store.scalar("SELECT COUNT(*) FROM approval"))

    def test_approval_is_bound_to_sender_identity_and_full_recipient_list(self) -> None:
        run = self.assign_and_run(mode="reply_all")
        draft = run["draft"]
        bound = self.svc.effects.grant_approval(draft["draft_id"],
                                                operation_id="op-6").data["approval"]["bound"]
        self.assertEqual(draft["sender_identity"], bound["sender_identity"])
        self.assertEqual(draft["recipients"], bound["recipients"])
        self.assertEqual(draft["hashes"], bound["hashes"])
        # Reply-all never silently includes Bcc: it is disclosed as a risk instead (T21).
        if "bcc_not_included" in draft["recipients"]:
            self.assertEqual([], draft["recipients"]["bcc"])
            self.assertIn("bcc_risk_noted", draft["recipients"])

    def test_approval_blocked_when_a_required_attachment_is_unavailable(self) -> None:
        """An unavailable attachment blocks approval until acknowledged (PRD §10)."""
        self.seed()
        old = self.conv_id("mock_mail", "thread-supplier-9")
        ws = self.svc.store.scalar(
            "SELECT ws_conv_id FROM workspace_source_link WHERE conv_id = ?", (old,))
        job = self.svc.assign(ws, "Reply about the statement.", "accounts_agent")
        ran = self.svc.run_job(job.data["job_id"], destination_conv_id=old)
        draft = ran.data["draft"]
        self.assertIsNotNone(draft["blocking_limitations"])
        denied = self.svc.effects.grant_approval(draft["draft_id"], operation_id="op-7")
        self.assertEqual("denied", denied.code)
        allowed = self.svc.effects.grant_approval(
            draft["draft_id"], operation_id="op-7b",
            acknowledgements=["limitations_acknowledged"])
        self.assertTrue(allowed.ok)


if __name__ == "__main__":
    unittest.main()
