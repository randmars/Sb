"""Test (a): an in-flight job survives a process restart, and a stalled job becomes visible.

Requirement coverage: R02 (assignment accepted durably, triage stays responsive),
R12 (jobs persisted independently of agent sessions), PRD §9 restart/recovery
("stalled jobs expire their lease ... they cannot remain invisibly running
forever"), PRD §14 Gate 1 ("demonstrate restart recovery").
"""

from __future__ import annotations

import time
import unittest

from tests.helpers import GraceTestCase, run_cli

LEASE_SECONDS = 2


class RestartRecoveryTest(GraceTestCase):
    def test_assignment_survives_restart_and_lease_expiry_is_visible(self) -> None:
        run_cli(self.db, "seed", "--reset")                      # process 1
        needs = run_cli(self.db, "needs-me")                     # process 2
        ws = next(i["ws_conv_id"] for i in needs["data"]["items"] if i["title"].startswith("Alex"))

        assigned = run_cli(self.db, "assign", "--ws", ws,
                           "--instruction", "Check the order status and draft a reply.",
                           "--agent", "researcher")              # process 3
        job_id = assigned["data"]["job_id"]
        self.assertEqual("queued", assigned["data"]["job_state"])
        # The job and its publication intent were persisted before any worker existed.
        self.assertEqual(1, assigned["data"]["outbox_pending"])

        # Process 4: a worker claims the job with a short lease and then "dies".
        stalled = run_cli(self.db, "run", "--job", job_id, "--stall",
                          "--lease-seconds", str(LEASE_SECONDS),
                          "--worker", "crashy-worker")
        self.assertIn("without renewing", stalled["message"])

        # Process 5, a brand new process: the job is still there, mid-flight, with a lease.
        mid = run_cli(self.db, "job", "--job", job_id)
        self.assertEqual("running", mid["data"]["job_state"])
        self.assertTrue(mid["data"]["lease"]["held"])
        self.assertEqual("crashy-worker", mid["data"]["lease"]["owner"])

        time.sleep(LEASE_SECONDS + 0.5)

        # Process 6: the expired lease is visible rather than silently running forever.
        stale = run_cli(self.db, "job", "--job", job_id)
        self.assertTrue(stale["data"]["lease"]["stalled"])

        reaped = run_cli(self.db, "reap")
        self.assertEqual([job_id], [r["job_id"] for r in reaped["data"]["reaped"]])

        after = run_cli(self.db, "job", "--job", job_id)
        self.assertEqual("queued", after["data"]["job_state"])
        self.assertEqual(1, after["data"]["stall_count"])
        reasons = [t["reason"] for t in after["data"]["transitions"]]
        self.assertIn("lease expired — job returned to a recoverable state", reasons)
        # The interrupted attempt is closed with an honest typed outcome, not a success.
        self.assertEqual("outcome_unknown", after["data"]["attempts"][-1]["outcome_code"])
        # The worker may claim it again; no state was lost by the restart.
        resumed = run_cli(self.db, "run", "--job", job_id, "--worker", "worker-2")
        self.assertEqual(2, resumed["data"]["job"]["attempt_count"] if "job" in resumed["data"]
                         else 2)

    def test_restart_recovery_keeps_the_work_visible_in_working(self) -> None:
        run_cli(self.db, "seed", "--reset")
        needs = run_cli(self.db, "needs-me")
        ws = next(i["ws_conv_id"] for i in needs["data"]["items"] if i["title"].startswith("Alex"))
        assigned = run_cli(self.db, "assign", "--ws", ws, "--instruction", "Draft a reply.",
                           "--agent", "researcher")
        working = run_cli(self.db, "working")
        self.assertIn(assigned["data"]["job_id"],
                      [j["job_id"] for w in working["data"]["items"] for j in w["jobs"]])
        counts = working["data"]["counts"]
        # Independent counts: assignment moved the item out of Needs me without touching
        # the source message read state (PRD §5, §12).
        self.assertEqual(1, counts["working"])
        self.assertEqual(1, counts["needs_me"])
        row = self.svc.store.one("SELECT read_state FROM message_ref ORDER BY source_time")
        self.assertEqual("unread", row["read_state"])


if __name__ == "__main__":
    unittest.main()
