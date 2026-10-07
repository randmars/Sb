"""Test (d): an uncertain or outcome_unknown dispatch enters reconciliation and is never
retried blindly.

Requirement coverage: R15 ("reconcile uncertain side effects instead of blindly
retrying them"), PRD §10 outbound operation ledger, PRD §13 retry policy,
T14 ("crash or timeout before or after source submission: one dispatch decision;
reconcile uncertain result; no blind duplicate resend"),
T15 ("pending ID and routed chat ... accurate accepted/pending/sent/unknown state"),
T22 ("cancel or supersede while an action may already be submitted").
"""

from __future__ import annotations

import unittest

from grace import contracts as C
from grace.effects import InjectedFault
from grace.service import Grace
from tests.helpers import GraceTestCase


class UncertainOutcomeTest(GraceTestCase):
    scenario = "timeout_uncertain"

    def test_uncertain_dispatch_requires_reconciliation_and_refuses_retry(self) -> None:
        run = self.assign_and_run()
        approval = self.svc.effects.grant_approval(run["draft"]["draft_id"], operation_id="op-u1")
        dispatch = self.svc.effects.dispatch(approval.data["approval"]["approval_id"])
        effect = dispatch.data["effect"]
        self.assertEqual(C.EffectState.OUTCOME_UNKNOWN, effect["effect_state"])
        self.assertTrue(effect["requires_reconciliation"])
        self.assertEqual(C.VerificationLevel.UNVERIFIED, effect["receipts"][-1]["verification_level"])
        self.assertEqual("unknown", effect["receipts"][-1]["delivery_state"])

        # No retry is allowed while the outcome is unknown.
        retry = self.svc.effects.retry(effect["effect_id"])
        self.assertEqual("retry_refused_uncertain", retry.code)
        self.assertEqual(1, self.svc.store.scalar(
            "SELECT COUNT(*) FROM effect_attempt WHERE phase <> 'reconciliation'"))

        # The item returns to Needs me with the reason and a next action, not an audit-only note.
        ws = self.svc.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?",
                                (effect["ws_conv_id"],))
        self.assertEqual("needs_me", ws["queue_state"])
        self.assertEqual("uncertain_effect", ws["needs_me_reason"])

        # Reconciliation is inconclusive in this scenario: the state stays unknown.
        recon = self.svc.effects.reconcile(effect["effect_id"])
        self.assertTrue(recon.ok)
        self.assertEqual(C.EffectState.OUTCOME_UNKNOWN, recon.data["effect"]["effect_state"])
        self.assertTrue(recon.data["effect"]["requires_reconciliation"])
        self.assertEqual("unverified", recon.data["effect"]["receipts"][-1]["verification_level"])
        still_refused = self.svc.effects.retry(effect["effect_id"])
        self.assertEqual("retry_refused_uncertain", still_refused.code)
        self.assertEqual(1, self.svc.store.scalar("SELECT COUNT(*) FROM effect_operation"))

    def test_cancel_after_submission_marks_unknown_rather_than_stopping_cleanly(self) -> None:
        run = self.assign_and_run()
        approval = self.svc.effects.grant_approval(run["draft"]["draft_id"], operation_id="op-u2")
        effect_id = self.svc.effects.dispatch(
            approval.data["approval"]["approval_id"]).data["effect"]["effect_id"]
        cancelled = self.svc.effects.cancel(effect_id, reason="owner changed their mind")
        self.assertTrue(cancelled.ok)
        self.assertEqual(C.EffectState.OUTCOME_UNKNOWN,
                         cancelled.data["effect"]["effect_state"])
        self.assertIn("cannot be stopped", cancelled.detail)


class PreSubmissionFailureTest(GraceTestCase):
    scenario = "pre_submission_failure"

    def test_definite_pre_submission_failure_may_be_retried_under_same_authorization(self) -> None:
        run = self.assign_and_run()
        approval = self.svc.effects.grant_approval(run["draft"]["draft_id"], operation_id="op-p1")
        approval_id = approval.data["approval"]["approval_id"]
        dispatch = self.svc.effects.dispatch(approval_id)
        effect = dispatch.data["effect"]
        self.assertEqual(C.EffectState.FAILED, effect["effect_state"])
        self.assertFalse(effect["requires_reconciliation"])
        attempt = effect["attempts"][-1]
        self.assertEqual("pre_submission", attempt["phase"])
        self.assertEqual(0, attempt["submitted"])
        self.assertEqual(1, attempt["retry_allowed"])

        retried = self.svc.effects.retry(effect["effect_id"])
        self.assertTrue(retried.ok, retried.detail)
        attempts = self.svc.store.all(
            "SELECT * FROM effect_attempt WHERE effect_id = ? AND phase <> 'reconciliation' "
            "ORDER BY attempt_no", (effect["effect_id"],))
        self.assertEqual(2, len(attempts))
        # The retry used the SAME authorization and the SAME idempotency key.
        self.assertEqual(approval_id, attempts[1]["authorization_ref"])
        self.assertEqual(1, self.svc.store.scalar("SELECT COUNT(*) FROM approval"))

        # Editing the approved content takes the authorization away: a new approval is needed.
        revised = self.svc.effects.revise_draft(run["draft"]["draft_id"], body="completely different")
        self.assertTrue(revised.ok)
        refused = self.svc.effects.retry(effect["effect_id"])
        self.assertEqual("denied", refused.code)
        self.assertIn("approved content changed", refused.detail)

    def test_permission_denial_is_terminal_and_visible(self) -> None:
        svc = Grace(self.db + "-perm", scenario="permission_denied")
        try:
            svc.seed(reset=True)
            ws = next(i["ws_conv_id"] for i in svc.needs_me() if i["title"].startswith("Alex"))
            job = svc.assign(ws, "Draft a reply.", "researcher")
            ran = svc.run_job(job.data["job_id"])
            approval = svc.effects.grant_approval(ran.data["draft"]["draft_id"],
                                                  operation_id="op-perm")
            dispatch = svc.effects.dispatch(approval.data["approval"]["approval_id"])
            effect = dispatch.data["effect"]
            self.assertEqual(C.EffectState.FAILED, effect["effect_state"])
            self.assertEqual(C.ErrorCategory.PERMISSION_REVOKED, effect["last_error_category"])
            self.assertFalse(effect["requires_reconciliation"])
            retry = svc.effects.retry(effect["effect_id"])
            self.assertEqual("invalid", retry.code)
            ws_row = svc.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?",
                                   (ws,))
            self.assertEqual("dispatch_failed", ws_row["needs_me_reason"])
        finally:
            svc.close()


class CrashWindowTest(GraceTestCase):
    """A crash inside the dispatch window must leave an uncertain, reconcilable state."""

    scenario = "timeout_then_found"

    def test_crash_after_submission_recovers_by_reconciliation(self) -> None:
        run = self.assign_and_run()
        approval_id = self.svc.effects.grant_approval(
            run["draft"]["draft_id"], operation_id="op-c1").data["approval"]["approval_id"]
        with self.assertRaises(InjectedFault) as caught:
            self.svc.effects.dispatch(approval_id, inject_crash_after="submission")
        self.assertEqual("dispatch.submission", caught.exception.where)

        effect = self.svc.store.one("SELECT * FROM effect_operation")
        self.assertEqual(C.EffectState.DISPATCHING, effect["effect_state"])
        self.assertTrue(effect["requires_reconciliation"])
        # No attempt row claims success: the process died before it could record one.
        self.assertEqual(0, self.svc.store.scalar(
            "SELECT COUNT(*) FROM effect_attempt WHERE phase <> 'reconciliation'"))
        # The approval is already consumed, so the operation cannot be dispatched twice.
        replay = self.svc.effects.dispatch(approval_id)
        self.assertEqual("idempotent_replay", replay.code)
        self.assertEqual(1, self.svc.store.scalar("SELECT COUNT(*) FROM effect_operation"))

        recon = self.svc.effects.reconcile(effect["effect_id"])
        self.assertEqual(C.EffectState.CONFIRMED_SENT, recon.data["effect"]["effect_state"])
        self.assertEqual("confirmed_sent",
                         recon.data["effect"]["receipts"][-1]["verification_level"])
        self.assertEqual(1, self.svc.store.scalar(
            "SELECT COUNT(*) FROM receipt WHERE verification_level = 'confirmed_sent'"))
        self.assertEqual(0, self.svc.store.scalar(
            "SELECT requires_reconciliation FROM effect_operation WHERE effect_id = ?",
            (effect["effect_id"],)))
        job = self.svc.store.one("SELECT * FROM job WHERE job_id = ?", (run["job_id"],))
        self.assertEqual(C.JobState.SUCCEEDED, job["job_state"])

    def test_crash_before_the_source_call_leaves_a_recoverable_operation(self) -> None:
        run = self.assign_and_run()
        approval_id = self.svc.effects.grant_approval(
            run["draft"]["draft_id"], operation_id="op-c2").data["approval"]["approval_id"]
        with self.assertRaises(InjectedFault):
            self.svc.effects.dispatch(approval_id, inject_crash_after="commit")
        effect = self.svc.store.one("SELECT * FROM effect_operation")
        self.assertEqual(C.EffectState.DISPATCHING, effect["effect_state"])
        recon = self.svc.effects.reconcile(effect["effect_id"])
        # The source does have a record in this scenario, and reconcile says so once.
        self.assertEqual(C.EffectState.CONFIRMED_SENT, recon.data["effect"]["effect_state"])

    def test_reconciled_absence_is_not_uncertainty(self) -> None:
        """If reconciliation proves nothing was sent, the operation fails and is not left unknown."""
        svc = Grace(self.db + "-absent", scenario="timeout_then_found")
        try:
            svc.seed(reset=True)
            ws = next(i["ws_conv_id"] for i in svc.needs_me() if i["title"].startswith("Alex"))
            job = svc.assign(ws, "Draft a reply.", "researcher")
            ran = svc.run_job(job.data["job_id"])
            approval = svc.effects.grant_approval(ran.data["draft"]["draft_id"],
                                                  operation_id="op-absent")
            effect_id = svc.effects.dispatch(
                approval.data["approval"]["approval_id"]).data["effect"]["effect_id"]
            # Force the "definitive absence" branch of the mock reconciler.
            svc.adapters["mock_beeper"].reconcile_spec = {"code": "success", "confirmed": False}
            recon = svc.effects.reconcile(effect_id)
            self.assertEqual(C.EffectState.FAILED, recon.data["effect"]["effect_state"])
            self.assertFalse(recon.data["effect"]["requires_reconciliation"])
            self.assertEqual("none", recon.data["effect"]["receipts"][-1]["verification_level"])
        finally:
            svc.close()


class VerifiedStateHonestyTest(GraceTestCase):
    scenario = "happy_pending"

    def test_pending_then_confirmed_receipt_is_reported_at_the_right_level(self) -> None:
        run = self.assign_and_run()
        approval = self.svc.effects.grant_approval(run["draft"]["draft_id"], operation_id="op-h1")
        dispatch = self.svc.effects.dispatch(approval.data["approval"]["approval_id"])
        effect = dispatch.data["effect"]
        self.assertEqual(C.EffectState.PROVIDER_PENDING, effect["effect_state"])
        self.assertEqual("provider_pending", effect["receipts"][-1]["verification_level"])
        self.assertIn("not yet confirmed", effect["receipts"][-1]["limitations"])
        self.assertEqual(0, effect["receipts"][-1]["verified_against_real_source"])
        recon = self.svc.effects.reconcile(effect["effect_id"])
        self.assertEqual("confirmed_sent", recon.data["effect"]["receipts"][-1]["verification_level"])
        # The actual routed destination is recorded, not the requested one.
        self.assertIn("member chat", recon.data["effect"]["actual_routed_destination"])


if __name__ == "__main__":
    unittest.main()
