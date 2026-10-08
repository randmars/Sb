"""Durable job ledger, leases, idempotent ingest, dedup keys, outbox, audit.

Implements PRD §9 (job lifecycle, claims/leases, restart and recovery), §12
(transactional persistence before publication, durable dedup keys, UTC times,
source times separate from ingestion times) and §6 (at-least-once ingest with
idempotent projection and cursors committed with their updates).

Never holds a transaction across work: ``Store.tx()`` blocks are short and contain
no source calls.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Optional

from . import contracts as C
from .contracts import JOB_TRANSITIONS, JobState, OpResult, QueueState, ReviewState
from .store import Store


# ------------------------------------------------------------------ helpers ---


def _job_brief(job: dict, ws: dict | None = None) -> dict:
    return {
        "job_id": job["job_id"],
        "ws_conv_id": job["ws_conv_id"],
        "agent": job["agent"],
        "job_state": job["job_state"],
        # The triage/review state is the JOB's own, not the conversation's: two jobs in one
        # conversation each keep their own (PRD §5, §12).
        "queue_state": job.get("queue_state"),
        "review_state": job.get("review_state"),
        "needs_me_reason": job.get("needs_me_reason"),
        "archive_state": C.ArchiveState.ARCHIVED if job.get("archived_at")
                         else C.ArchiveState.ACTIVE,
        "archived_at": job.get("archived_at"),
        "archive_reason": job.get("archive_reason"),
        "deletion_state": C.DeletionState.DELETED if job.get("deleted_at")
                          else C.DeletionState.RETAINED,
        "deleted_at": job.get("deleted_at"),
        "state_version": job["state_version"],
        "attempt_count": job["attempt_count"],
        "instruction": job["instruction"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "superseded_by": job["superseded_by"],
        "rule_id": job["rule_id"],
        "rule_version": job["rule_version"],
        "lease": lease_state(job),
        "origin": job["origin"],
        "mock_label": job["mock_label"],
        "workspace": None if ws is None else {
            "title": ws["title"], "queue_state": ws["queue_state"],
            "review_state": ws["review_state"], "assignment_state": ws["assignment_state"],
        },
    }


def lease_state(job: dict) -> dict:
    """Derived, never stored: lease health is computed so a stalled job is visible."""
    if not job.get("lease_expires_at"):
        return {"held": False, "state": "none", "owner": None, "expires_at": None,
                "stalled": False}
    expired = C.parse_iso(job["lease_expires_at"]) <= C.now_dt()
    return {
        "held": True,
        "state": "expired" if expired else "active",
        "owner": job.get("lease_owner"),
        "expires_at": job["lease_expires_at"],
        "heartbeat_at": job.get("lease_heartbeat_at"),
        "stalled": bool(expired and job["job_state"] == JobState.RUNNING),
    }


class Ledger:
    def __init__(self, store: Store, *, actor_default: str = "service"):
        self.store = store
        self.actor_default = actor_default

    # ------------------------------------------------------------ job create --
    def create_job(self, *, ws_conv_id: str, instruction: str, agent: str,
                   rule_id: str | None = None, rule_version: int | None = None,
                   dedup_key: str | None = None, capability_plan: dict | None = None,
                   dependencies: Iterable[str] = (),
                   operation_id: str | None = None, actor: str = "owner",
                   origin: str = C.REAL, mock_label: str | None = None) -> OpResult:
        """Persist the job *and* the publication intent in one transaction.

        ``dedup_key`` makes this repeat-safe: a replayed source event reuses the
        existing job instead of creating a second one (PRD §12, T08).
        """
        if not instruction.strip():
            return OpResult(C.INVALID, "instruction must not be empty")
        if not agent.strip():
            return OpResult(C.INVALID, "an agent must be named (no generic auto-assignment)")
        if dedup_key:
            prior = self.store.one("SELECT * FROM outbox WHERE dedup_key = ?", (dedup_key,))
            if prior is not None:
                job_id = (json.loads(prior["payload_json"]) or {}).get("job_id")
                if job_id:
                    return OpResult(C.IDEMPOTENT_REPLAY,
                                    "duplicate event: reusing the existing job",
                                    data={"job_id": job_id, "dedup_key": dedup_key})
        job_id = C.new_id("job")
        at = C.now()
        ws = self.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?", (ws_conv_id,))
        if ws is None:
            return OpResult(C.NOT_FOUND, f"unknown workspace conversation {ws_conv_id}")
        with self.store.tx():
            self.store.insert_row("job", {
                "job_id": job_id, "ws_conv_id": ws_conv_id, "instruction_version": 1,
                "instruction": instruction, "agent": agent, "job_state": JobState.QUEUED,
                # A job starts working, and it carries its own triage state from the start.
                "queue_state": QueueState.WORKING, "review_state": ReviewState.NONE,
                "needs_me_reason": None,
                "state_version": 1, "capability_plan": C.canonical_json(capability_plan or {
                    "systems": [], "target": None, "requested_effect": "prepare_draft",
                    "authorization": "owner_instruction", "verification": "application_receipt",
                }),
                "dependencies_json": C.canonical_json(list(dependencies)),
                "source_context_revision": None, "attempt_count": 0,
                "lease_owner": None, "lease_expires_at": None, "lease_heartbeat_at": None,
                "stall_count": 0, "last_stall_at": None, "checkpoint": "{}",
                "cancellation_requested_at": None, "superseded_by": None, "supersedes": None,
                "rule_id": rule_id, "rule_version": rule_version,
                "origin": origin, "mock_label": mock_label,
                "created_at": at, "updated_at": at,
            })
            self.store.insert_row("job_input", {
                "job_input_id": C.new_id("jin"), "job_id": job_id, "version": 1,
                "kind": "instruction", "content": instruction, "author": actor, "at": at,
                "delivered_to_worker_at": None, "superseded_by": None,
                "origin": origin, "mock_label": mock_label,
            })
            self.store.insert_row("job_transition", {
                "transition_id": C.new_id("tr"), "job_id": job_id, "from_state": "",
                "to_state": JobState.QUEUED, "actor": actor, "at": at,
                "reason": "job persisted before launch", "expected_version": 0,
                "resulting_version": 1,
                "details_json": C.canonical_json({"agent": agent, "dedup_key": dedup_key}),
            })
            # Independent states: assignment moves the item out of the Needs me
            # filter; it does not touch source read/hidden/mute state (PRD §5, §12).
            self.store.update_row("workspace_conversation", {
                "queue_state": QueueState.WORKING, "assignment_state": "assigned",
                "updated_at": at,
            }, "ws_conv_id = ?", (ws_conv_id,))
            self.store.insert_row("outbox", {
                "outbox_id": C.new_id("obx"),
                "dedup_key": dedup_key or f"job:{job_id}",
                "topic": "job.queued",
                "payload_json": C.canonical_json({"job_id": job_id, "ws_conv_id": ws_conv_id,
                                                  "agent": agent}),
                "outbox_state": "pending", "attempts": 0, "last_error": None,
                "created_at": at, "committed_at": at, "published_at": None,
                "origin": origin, "mock_label": mock_label,
            })
            self.store.audit(actor=actor, operation="create_job", entity_kind="job",
                            entity_id=job_id, reason="owner instruction accepted",
                            version_before=0, version_after=1, operation_id=operation_id,
                            details={"ws_conv_id": ws_conv_id, "agent": agent,
                                     "rule_id": rule_id},
                            origin=origin, mock_label=mock_label, within_tx=True)
        return OpResult(C.OK, "job persisted before launch",
                        data={"job_id": job_id, "job_state": JobState.QUEUED,
                              "outbox_topic": "job.queued", "outbox_pending": 1},
                        operation_id=operation_id, provenance=origin, label=mock_label,
                        mocked=(origin == C.MOCK))

    # ------------------------------------------------------------ transitions --
    def transition(self, job_id: str, to_state: str, *, actor: str | None = None,
                   reason: str = "", expected_version: int | None = None,
                   details: dict | None = None, operation_id: str | None = None) -> OpResult:
        if to_state not in JobState.ALL:
            return OpResult(C.INVALID, f"unknown job state {to_state!r}")
        actor = actor or self.actor_default
        with self.store.tx():
            job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
            if job is None:
                return OpResult(C.NOT_FOUND, f"unknown job {job_id}")
            if expected_version is not None and expected_version != job["state_version"]:
                return OpResult(C.CONFLICT,
                                "state version changed; returning the current version for "
                                "explicit reconciliation",
                                current_version=job["state_version"],
                                data={"job_state": job["job_state"]})
            if to_state not in JOB_TRANSITIONS[job["job_state"]]:
                return OpResult(C.INVALID,
                                f"illegal transition {job['job_state']} -> {to_state}",
                                current_version=job["state_version"])
            now = C.now()
            updates: dict[str, Any] = {"job_state": to_state,
                                      "state_version": job["state_version"] + 1,
                                      "updated_at": now}
            if to_state != JobState.RUNNING:
                updates.update({"lease_owner": None, "lease_expires_at": None})
            self.store.update_row("job", updates, "job_id = ?", (job_id,))
            self.store.insert_row("job_transition", {
                "transition_id": C.new_id("tr"), "job_id": job_id,
                "from_state": job["job_state"], "to_state": to_state, "actor": actor,
                "at": now, "reason": reason,
                "expected_version": job["state_version"],
                "resulting_version": job["state_version"] + 1,
                "details_json": C.canonical_json(details or {}),
            })
            self._sync_workspace_for_job(job["ws_conv_id"], to_state, now, job_id=job_id)
            self.store.audit(actor=actor, operation=f"job:{to_state}", entity_kind="job",
                            entity_id=job_id, reason=reason,
                            version_before=job["state_version"],
                            version_after=job["state_version"] + 1,
                            operation_id=operation_id, details=details, within_tx=True)
        return OpResult(C.OK, reason or f"job is {to_state}",
                        data={"job_id": job_id, "job_state": to_state},
                        current_version=job["state_version"] + 1, operation_id=operation_id)

    def _sync_workspace_for_job(self, ws_conv_id: str, job_state: str, at: str,
                               job_id: str | None = None) -> None:
        """Derive the triage filter state from job state — per JOB, then aggregate.

        PRD §5 keeps the queue filter an application-owned state that is independent of the
        source message. Deriving it per *conversation* was the Gate 1 defect: a second job
        in the same conversation overwrote the first job's queue/review/reason, so one job's
        draft vanished from Needs me and from the client. Now the job carries its own state
        (:data:`contracts.JOB_FILTER_STATES`) and the conversation shows the aggregate of the
        jobs it hosts.
        """
        queue_state, review_state, reason = C.JOB_FILTER_STATES[job_state]
        if job_id:
            self.store.update_row("job", {
                "queue_state": queue_state, "review_state": review_state,
                "needs_me_reason": reason, "updated_at": at,
            }, "job_id = ?", (job_id,))
        self.store.recompute_conversation_filter_state(ws_conv_id, at=at)


    # ------------------------------------------------------------------ leases --
    def claim(self, job_id: str, *, worker: str, lease_seconds: int = 30,
              expected_version: int | None = None) -> OpResult:
        """Short-lived claim. The DB transaction covers bookkeeping only."""
        with self.store.tx():
            job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
            if job is None:
                return OpResult(C.NOT_FOUND, f"unknown job {job_id}")
            if job["job_state"] not in (JobState.QUEUED,):
                return OpResult(C.ALREADY_IN_STATE,
                                f"job is {job['job_state']}, not claimable",
                                current_version=job["state_version"])
            if expected_version is not None and expected_version != job["state_version"]:
                return OpResult(C.CONFLICT, "state version changed", current_version=job["state_version"])
            now = C.now()
            attempt_no = job["attempt_count"] + 1
            self.store.update_row("job", {
                "job_state": JobState.RUNNING, "state_version": job["state_version"] + 1,
                "attempt_count": attempt_no, "lease_owner": worker,
                "lease_expires_at": C.plus(lease_seconds), "lease_heartbeat_at": now,
                # Claiming moves THIS job into the working filter; it must not overwrite
                # another job's needs-me state in the same conversation.
                "queue_state": C.JOB_FILTER_STATES[JobState.RUNNING][0],
                "review_state": C.JOB_FILTER_STATES[JobState.RUNNING][1],
                "needs_me_reason": C.JOB_FILTER_STATES[JobState.RUNNING][2],
                "updated_at": now,
            }, "job_id = ?", (job_id,))
            self.store.insert_row("attempt", {
                "attempt_id": C.new_id("att"), "job_id": job_id, "attempt_no": attempt_no,
                "started_at": now, "ended_at": None, "outcome_code": None,
                "outcome_detail": None, "lease_owner": worker, "checkpoint": job["checkpoint"],
                "host_run_ref": None, "origin": job["origin"], "mock_label": job["mock_label"],
            })
            self.store.insert_row("job_transition", {
                "transition_id": C.new_id("tr"), "job_id": job_id,
                "from_state": job["job_state"], "to_state": JobState.RUNNING, "actor": worker,
                "at": now, "reason": f"claimed with a {lease_seconds}s lease",
                "expected_version": job["state_version"],
                "resulting_version": job["state_version"] + 1,
                "details_json": C.canonical_json({"lease_seconds": lease_seconds}),
            })
            self.store.recompute_conversation_filter_state(job["ws_conv_id"], at=now)
        return OpResult(C.OK, "claimed", data={"job_id": job_id, "attempt_no": attempt_no,
                                              "lease_expires_at": C.plus(lease_seconds)})

    def renew_lease(self, job_id: str, *, worker: str, lease_seconds: int = 30) -> OpResult:
        with self.store.tx():
            job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
            if job is None:
                return OpResult(C.NOT_FOUND, f"unknown job {job_id}")
            if job["lease_owner"] != worker:
                return OpResult(C.CONFLICT, "lease is held by another worker",
                                current_version=job["state_version"],
                                data={"lease": lease_state(job)})
            if job["lease_expires_at"] and C.is_past(job["lease_expires_at"]):
                return OpResult(C.CONFLICT, "lease already expired; re-claim before continuing",
                                current_version=job["state_version"],
                                data={"lease": lease_state(job)})
            self.store.update_row("job", {"lease_expires_at": C.plus(lease_seconds),
                                         "lease_heartbeat_at": C.now(),
                                         "updated_at": C.now()},
                                 "job_id = ?", (job_id,))
        return OpResult(C.OK, "lease renewed", data={"job_id": job_id})

    def reap_expired_leases(self, *, actor: str = "service") -> list[dict]:
        """A stalled job becomes visible and recoverable instead of running forever."""
        now = C.now()
        stale = self.store.all(
            "SELECT job_id FROM job WHERE job_state = ? AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at < ?", (JobState.RUNNING, now))
        reaped = []
        for row in stale:
            job_id = row["job_id"]
            with self.store.tx():
                job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
                if job is None or job["job_state"] != JobState.RUNNING:
                    continue
                self.store.update_row("job", {
                    "job_state": JobState.QUEUED, "state_version": job["state_version"] + 1,
                    "lease_owner": None, "lease_expires_at": None,
                    "stall_count": job["stall_count"] + 1, "last_stall_at": now,
                    # The job returns to the recoverable queue state, and only THIS job.
                    "queue_state": C.JOB_FILTER_STATES[JobState.QUEUED][0],
                    "review_state": C.JOB_FILTER_STATES[JobState.QUEUED][1],
                    "needs_me_reason": C.JOB_FILTER_STATES[JobState.QUEUED][2],
                    "updated_at": now,
                }, "job_id = ?", (job_id,))
                self.store.recompute_conversation_filter_state(job["ws_conv_id"], at=now)
                self.store.insert_row("job_transition", {
                    "transition_id": C.new_id("tr"), "job_id": job_id,
                    "from_state": JobState.RUNNING, "to_state": JobState.QUEUED,
                    "actor": actor, "at": now,
                    "reason": "lease expired — job returned to a recoverable state",
                    "expected_version": job["state_version"],
                    "resulting_version": job["state_version"] + 1,
                    "details_json": C.canonical_json({"expired_lease": job["lease_expires_at"],
                                                      "stall_count": job["stall_count"] + 1}),
                })
                attempt = self.store.one(
                    "SELECT * FROM attempt WHERE job_id = ? ORDER BY attempt_no DESC LIMIT 1",
                    (job_id,))
                if attempt and attempt["ended_at"] is None:
                    self.store.update_row("attempt", {
                        "ended_at": now, "outcome_code": C.OUTCOME_UNKNOWN,
                        "outcome_detail": "worker did not renew its lease",
                    }, "attempt_id = ?", (attempt["attempt_id"],))
                self.store.audit(actor=actor, operation="lease_expired", entity_kind="job",
                                entity_id=job_id, reason="stalled worker made visible",
                                version_before=job["state_version"],
                                version_after=job["state_version"] + 1, within_tx=True)
            reaped.append({"job_id": job_id, "reason": "lease_expired"})
        return reaped

    def finish_attempt(self, job_id: str, *, outcome_code: str, detail: str = "",
                       checkpoint: dict | None = None, host_run_ref: str | None = None,
                       actor: str | None = None) -> OpResult:
        """Attach the worker outcome to the in-flight attempt, not to a job state change."""
        with self.store.tx():
            attempt = self.store.one(
                "SELECT * FROM attempt WHERE job_id = ? AND ended_at IS NULL "
                "ORDER BY attempt_no DESC LIMIT 1", (job_id,))
            if attempt is None:
                return OpResult(C.NOT_FOUND, "no open attempt for this job")
            if outcome_code not in C.ADAPTER_OUTCOMES:
                return OpResult(C.INVALID, f"outcome_code must be a typed outcome, got {outcome_code!r}")
            now = C.now()
            self.store.update_row("attempt", {
                "ended_at": now, "outcome_code": outcome_code, "outcome_detail": detail,
                "host_run_ref": host_run_ref,
            }, "attempt_id = ?", (attempt["attempt_id"],))
            if checkpoint is not None:
                job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
                self.store.update_row("job", {"checkpoint": C.canonical_json(checkpoint),
                                             "updated_at": now}, "job_id = ?", (job_id,))
            self.store.audit(actor=actor or self.actor_default, operation="attempt_finished",
                            entity_kind="job", entity_id=job_id, reason=detail,
                            details={"outcome_code": outcome_code}, within_tx=True)
        return OpResult(C.OK, "attempt closed", data={"job_id": job_id, "outcome_code": outcome_code})

    # ----------------------------------------------------------------- cancel --
    def request_cancellation(self, job_id: str, *, reason: str, actor: str = "owner") -> OpResult:
        job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
        if job is None:
            return OpResult(C.NOT_FOUND, f"unknown job {job_id}")
        if job["job_state"] in JobState.TERMINAL:
            return OpResult(C.ALREADY_IN_STATE, f"job already {job['job_state']}",
                            current_version=job["state_version"])
        effects = self.store.all(
            "SELECT effect_id, effect_state, provider_status FROM effect_operation "
            "WHERE job_id = ? AND effect_state NOT IN (?, ?, ?)",
            (job_id, "confirmed_sent", "failed", "cancelled"))
        with self.store.tx():
            self.store.update_row("job", {"cancellation_requested_at": C.now()},
                                  "job_id = ?", (job_id,))
        result = self.transition(job_id, JobState.CANCELLED, actor=actor, reason=reason,
                                 details={"effects_not_stoppable": effects,
                                          "note": "a submitted effect cannot be recalled; it is "
                                                  "reconciled separately"})
        if result.ok:
            result.data = {**(result.data or {}), "effects_not_stoppable": effects}
        return result

    def mark_superseded(self, job_id: str, *, by_job_id: str, reason: str,
                        actor: str = "owner") -> OpResult:
        with self.store.tx():
            self.store.update_row("job", {"superseded_by": by_job_id}, "job_id = ?", (job_id,))
        return self.transition(job_id, JobState.SUPERSEDED, actor=actor, reason=reason,
                               details={"superseded_by": by_job_id})

    # ------------------------------------------------------------------ inputs --
    def append_input(self, job_id: str, *, kind: str, content: str, author: str = "owner",
                     operation_id: str | None = None) -> OpResult:
        """New input is a new version/turn; a prompt already executing is never mutated."""
        job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
        if job is None:
            return OpResult(C.NOT_FOUND, f"unknown job {job_id}")
        if kind not in ("answer", "follow_up", "information"):
            return OpResult(C.INVALID, f"unknown input kind {kind!r}")
        if job["job_state"] in JobState.TERMINAL:
            return OpResult(C.INVALID, f"job is {job['job_state']}; work with a new instruction instead")
        with self.store.tx():
            version = int(self.store.scalar(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM job_input WHERE job_id = ?", (job_id,)))
            now = C.now()
            self.store.insert_row("job_input", {
                "job_input_id": C.new_id("jin"), "job_id": job_id, "version": version,
                "kind": kind, "content": content, "author": author, "at": now,
                "delivered_to_worker_at": None, "superseded_by": None,
                "origin": job["origin"], "mock_label": job["mock_label"],
            })
            self.store.update_row("job", {"updated_at": now}, "job_id = ?", (job_id,))
            self.store.audit(actor=author, operation=f"job_input:{kind}", entity_kind="job",
                            entity_id=job_id, reason="new input version",
                            version_before=version - 1, version_after=version,
                            operation_id=operation_id, within_tx=True)
        resumed = False
        if kind == "answer" and job["job_state"] == JobState.WAITING_FOR_USER:
            resumed = self.transition(job_id, JobState.QUEUED,
                                      actor=author,
                                      reason="answer persisted; the waiting job resumes").ok
        return OpResult(C.OK, "input appended as a new version",
                        data={"job_id": job_id, "version": version, "answer_delivered_to_worker": False,
                              "job_resumed": resumed, "note": "an informational answer is not an "
                                                              "approval of a separate external action"})

    # -------------------------------------------------------------- sessions ----
    def bind_session(self, *, ws_conv_id: str, job_id: str | None, profile: str,
                     session_key: str, session_id: str | None = None,
                     run_id: str | None = None, successor_of: str | None = None,
                     parent_binding_id: str | None = None,
                     compression_lineage: str | None = None,
                     origin: str = C.REAL, mock_label: str | None = None) -> OpResult:
        """PRD §9: persist exact profile/session/run ids; never infer a "latest session"."""
        existing = self.store.one(
            "SELECT * FROM session_binding WHERE hermes_profile = ? AND session_key = ?",
            (profile, session_key))
        now = C.now()
        with self.store.tx():
            if existing is None:
                binding_id = C.new_id("bind")
                self.store.insert_row("session_binding", {
                    "binding_id": binding_id, "ws_conv_id": ws_conv_id, "job_id": job_id,
                    "hermes_profile": profile, "session_key": session_key,
                    "session_id": session_id, "run_id": run_id,
                    "successor_of": successor_of, "parent_binding_id": parent_binding_id,
                    "compression_lineage": compression_lineage,
                    "created_at": now, "updated_at": now,
                    "origin": origin, "mock_label": mock_label,
                })
            else:
                binding_id = existing["binding_id"]
                self.store.update_row("session_binding", {
                    "session_id": session_id or existing["session_id"],
                    "run_id": run_id or existing["run_id"],
                    "successor_of": successor_of or existing["successor_of"],
                    "compression_lineage": compression_lineage or existing["compression_lineage"],
                    "updated_at": now,
                }, "binding_id = ?", (binding_id,))
        return OpResult(C.OK, "session binding persisted",
                        data={"binding_id": binding_id, "session_key": session_key,
                              "session_id": session_id, "run_id": run_id})

    # ---------------------------------------------------------------- outbox ----
    def pending_outbox(self, limit: int = 100) -> list[dict]:
        return self.store.all("SELECT * FROM outbox WHERE outbox_state = 'pending' "
                              "ORDER BY created_at LIMIT ?", (limit,))

    def publish_outbox(self, limit: int = 100) -> OpResult:
        """Worker notification step. Recovery: whatever is still 'pending' is republished."""
        rows = self.pending_outbox(limit)
        for row in rows:
            with self.store.tx():
                self.store.update_row("outbox", {
                    "outbox_state": "published", "published_at": C.now(),
                    "attempts": row["attempts"] + 1,
                }, "outbox_id = ?", (row["outbox_id"],))
        return OpResult(C.OK, f"published {len(rows)} outbox message(s)",
                        data={"published": [r["outbox_id"] for r in rows]})

    # ------------------------------------------------------------------ views ---
    #: A work item is only on a surface while it is live: an archived or deleted job, or a
    #: job inside an archived or deleted conversation, is out of the default triage
    #: surfaces. Nothing is removed from storage to achieve that.
    LIVE_JOB = "j.archived_at IS NULL AND j.deleted_at IS NULL"
    LIVE_WS = "w.archived_at IS NULL AND w.deleted_at IS NULL"

    #: What an archived (or deleted) item is, stated once, for every surface.
    RETENTION_NOTE = (
        "Archived: out of the default triage surfaces, still stored and directly "
        "retrievable. Archive is not deletion — no source conversation, message, job, draft "
        "or result row is removed. A deleted item is an application tombstone: also still in "
        "storage, listed only when explicitly asked for, and never a source-app deletion.")

    def _work_items(self, state: str, limit: int) -> list[dict]:
        """One item per JOB in that filter state, plus conversations with no live job.

        A conversation may carry several concurrent jobs (`T04`): each is its own item with
        its own state, so a second job can no longer overwrite the first one's.
        """
        rows = self.store.all(
            f"SELECT j.job_id, j.ws_conv_id FROM job j JOIN workspace_conversation w "
            f"ON w.ws_conv_id = j.ws_conv_id "
            f"WHERE j.queue_state = ? AND {self.LIVE_JOB} AND {self.LIVE_WS} "
            f"ORDER BY w.updated_at DESC, j.created_at DESC LIMIT ?", (state, limit))
        items: list[dict] = []
        for row in rows:
            ws = self.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?",
                                (row["ws_conv_id"],))
            job = self.store.one("SELECT * FROM job WHERE job_id = ?", (row["job_id"],))
            items.append(self._work_item_brief(ws, job))
        covered = {item["ws_conv_id"] for item in items}
        for ws in self.store.all(
                f"SELECT * FROM workspace_conversation w WHERE w.queue_state = ? AND {self.LIVE_WS} "
                f"ORDER BY w.updated_at DESC LIMIT ?", (state, limit)):
            if ws["ws_conv_id"] in covered:
                continue
            if self.store.scalar(
                    f"SELECT COUNT(*) FROM job j WHERE j.ws_conv_id = ? AND {self.LIVE_JOB}",
                    (ws["ws_conv_id"],)):
                # The conversation has live jobs of its own; they are listed individually
                # and the conversation row's state is only their aggregate.
                continue
            items.append(self._work_item_brief(ws, None))
        return items[:limit]

    def _work_item_brief(self, ws: dict, job: dict | None) -> dict:
        """A conversation brief carrying THIS work item's own state."""
        brief = self._ws_brief(ws)
        brief["job_id"] = job["job_id"] if job else None
        brief["work_item_id"] = (f"{ws['ws_conv_id']}::{job['job_id']}" if job
                                 else f"{ws['ws_conv_id']}::conversation")
        brief["job"] = _job_brief(job, ws) if job else None
        if job is not None:
            brief["queue_state"] = job["queue_state"]
            brief["review_state"] = job["review_state"]
            brief["needs_me_reason"] = job["needs_me_reason"]
        return brief

    def needs_me(self, limit: int = 50) -> list[dict]:
        return self._work_items(QueueState.NEEDS_ME, limit)

    def working(self, limit: int = 50) -> list[dict]:
        return self._work_items(QueueState.WORKING, limit)

    def all_conversations(self, limit: int = 100, *, include_deleted: bool = False) -> list[dict]:
        sql = ("SELECT * FROM workspace_conversation "
               + ("" if include_deleted else "WHERE deleted_at IS NULL ")
               + "ORDER BY updated_at DESC LIMIT ?")
        return [self._ws_brief(r) for r in self.store.all(sql, (limit,))]

    def counts(self) -> dict:
        """Independent counts (PRD §5): work items for the two queues, conversations for `all`.

        needs_me/working count *items*, because that is what those surfaces list; `all`
        counts conversations, because that is what it lists. Each is computed on its own.
        """
        needs_me_jobs = self.store.scalar(
            f"SELECT COUNT(*) FROM job j JOIN workspace_conversation w "
            f"ON w.ws_conv_id = j.ws_conv_id WHERE j.queue_state = ? AND {self.LIVE_JOB} "
            f"AND {self.LIVE_WS}", (QueueState.NEEDS_ME,))
        needs_me_conversations = self.store.scalar(
            f"SELECT COUNT(*) FROM workspace_conversation w WHERE w.queue_state = ? "
            f"AND {self.LIVE_WS} AND NOT EXISTS (SELECT 1 FROM job j WHERE "
            f"j.ws_conv_id = w.ws_conv_id AND {self.LIVE_JOB})", (QueueState.NEEDS_ME,))
        working_jobs = self.store.scalar(
            f"SELECT COUNT(*) FROM job j JOIN workspace_conversation w "
            f"ON w.ws_conv_id = j.ws_conv_id WHERE j.queue_state = ? AND {self.LIVE_JOB} "
            f"AND {self.LIVE_WS}", (QueueState.WORKING,))
        working_conversations = self.store.scalar(
            f"SELECT COUNT(*) FROM workspace_conversation w WHERE w.queue_state = ? "
            f"AND {self.LIVE_WS} AND NOT EXISTS (SELECT 1 FROM job j WHERE "
            f"j.ws_conv_id = w.ws_conv_id AND {self.LIVE_JOB})", (QueueState.WORKING,))
        return {
            "needs_me": needs_me_jobs + needs_me_conversations,
            "working": working_jobs + working_conversations,
            "all": self.store.scalar("SELECT COUNT(*) FROM workspace_conversation "
                                     "WHERE deleted_at IS NULL"),
            "stalled_jobs": len(self.store.all(
                "SELECT job_id FROM job WHERE job_state = ? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < ?", (JobState.RUNNING, C.now()))),
        }

    def archived_count(self) -> dict:
        """Archived items are counted separately; they are outside the triage totals."""
        return {
            "conversations": self.store.scalar(
                "SELECT COUNT(*) FROM workspace_conversation WHERE archived_at IS NOT NULL "
                "AND deleted_at IS NULL"),
            "jobs": self.store.scalar(
                "SELECT COUNT(*) FROM job WHERE archived_at IS NOT NULL AND deleted_at IS NULL"),
            "deleted_conversations": self.store.scalar(
                "SELECT COUNT(*) FROM workspace_conversation WHERE deleted_at IS NOT NULL"),
        }

    def _ws_brief(self, ws: dict) -> dict:
        jobs = self.store.all("SELECT * FROM job WHERE ws_conv_id = ? ORDER BY created_at DESC",
                              (ws["ws_conv_id"],))
        sources = self.store.all(
            "SELECT c.conv_id, c.namespaced_id, c.availability, c.availability_reason, "
            "c.source_time_last, c.audience_kind, c.origin, c.mock_label, l.relevance "
            "FROM workspace_source_link l JOIN source_conversation c ON c.conv_id = l.conv_id "
            "WHERE l.ws_conv_id = ? "
            # The order of a conversation's sources is part of the contract the client
            # relies on: most recent source activity first, with the provider's namespaced
            # id as a stable tiebreak so two threads with the same timestamp never swap
            # places between reads.
            "ORDER BY c.source_time_last DESC, c.namespaced_id ASC", (ws["ws_conv_id"],))
        latest = self.store.one(
            "SELECT msg_ref_id, source_time, ingested_at, read_state, hidden_state, availability, "
            "body_state, sender_json, minimal_metadata, origin, mock_label "
            "FROM message_ref WHERE conv_id IN "
            "(SELECT conv_id FROM workspace_source_link WHERE ws_conv_id = ?) "
            "ORDER BY source_time DESC LIMIT 1", (ws["ws_conv_id"],))
        return {
            "ws_conv_id": ws["ws_conv_id"],
            "association": {"kind": ws["association_kind"], "id": ws["association_id"]},
            "title": ws["title"],
            "queue_state": ws["queue_state"],
            "review_state": ws["review_state"],
            "assignment_state": ws["assignment_state"],
            "needs_me_reason": ws["needs_me_reason"],
            # Archive is a state, not a delete: both facts travel together everywhere so a
            # reader can never mistake one for the other.
            "archive_state": C.ArchiveState.ARCHIVED if ws["archived_at"]
                             else C.ArchiveState.ACTIVE,
            "archived_at": ws["archived_at"],
            "archive_reason": ws["archive_reason"],
            "deletion_state": C.DeletionState.DELETED if ws["deleted_at"]
                              else C.DeletionState.RETAINED,
            "deleted_at": ws["deleted_at"],
            "deletion_reason": ws["deletion_reason"],
            "current_input_revision": ws["current_input_revision"],
            "updated_at": ws["updated_at"],
            "source_links": sources,
            "latest_message": latest,
            "jobs": [_job_brief(j, ws) for j in jobs],
        }


    def job_detail(self, job_id: str) -> Optional[dict]:
        job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
        if job is None:
            return None
        ws = self.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?",
                            (job["ws_conv_id"],))
        return {
            **_job_brief(job, ws),
            "instruction_version": job["instruction_version"],
            "capability_plan": json.loads(job["capability_plan"]),
            "dependencies": json.loads(job["dependencies_json"]),
            "checkpoint": json.loads(job["checkpoint"]),
            "cancellation_requested_at": job["cancellation_requested_at"],
            "supersedes": job["supersedes"],
            "stall_count": job["stall_count"],
            "last_stall_at": job["last_stall_at"],
            "attempts": self.store.all(
                "SELECT * FROM attempt WHERE job_id = ? ORDER BY attempt_no", (job_id,)),
            "transitions": self.store.all(
                "SELECT * FROM job_transition WHERE job_id = ? ORDER BY at", (job_id,)),
            "inputs": self.store.all(
                "SELECT * FROM job_input WHERE job_id = ? ORDER BY version", (job_id,)),
            "results": self.store.all("SELECT * FROM result WHERE job_id = ? ORDER BY version",
                                      (job_id,)),
            "effects": self.store.all(
                "SELECT * FROM effect_operation WHERE job_id = ? ORDER BY created_at", (job_id,)),
            "session_bindings": self.store.all(
                "SELECT * FROM session_binding WHERE job_id = ?", (job_id,)),
        }

    # ------------------------------------------------------- archive / delete --
    def archive_item(self, *, ws_conv_id: str | None = None, job_id: str | None = None,
                     reason: str = "", actor: str = "owner") -> OpResult:
        """Archive a conversation or one job: out of triage, still stored. Not a delete."""
        return self._retention_op(ws_conv_id=ws_conv_id, job_id=job_id, archive=True,
                                  reason=reason, actor=actor, operation="archive_item")

    def unarchive_item(self, *, ws_conv_id: str | None = None, job_id: str | None = None,
                       reason: str = "", actor: str = "owner") -> OpResult:
        return self._retention_op(ws_conv_id=ws_conv_id, job_id=job_id, archive=False,
                                  reason=reason, actor=actor, operation="unarchive_item")

    def delete_item(self, *, ws_conv_id: str | None = None, job_id: str | None = None,
                    reason: str = "", actor: str = "owner") -> OpResult:
        """Tombstone an item. It leaves every list, but no source row is removed (R08)."""
        return self._retention_op(ws_conv_id=ws_conv_id, job_id=job_id, delete=True,
                                  reason=reason, actor=actor, operation="delete_item")

    def restore_item(self, *, ws_conv_id: str | None = None, job_id: str | None = None,
                     reason: str = "", actor: str = "owner") -> OpResult:
        return self._retention_op(ws_conv_id=ws_conv_id, job_id=job_id, delete=False,
                                  reason=reason, actor=actor, operation="restore_item")

    def _retention_op(self, *, ws_conv_id: str | None, job_id: str | None,
                      archive: bool | None = None, delete: bool | None = None,
                      reason: str, actor: str, operation: str) -> OpResult:
        """Archive/delete/restore one conversation or one job.

        Archive and deletion are *states* in the ledger, never a row removal: the source
        stays authoritative in its own app (PRD §8, R08), and a deleted item is an
        application tombstone that stays in storage and stays retrievable by identity.
        """
        if ws_conv_id and job_id:
            return OpResult(C.INVALID, "name either a workspace conversation or a job, not both")
        if job_id:
            table, key_col, key = "job", "job_id", job_id
        elif ws_conv_id:
            table, key_col, key = "workspace_conversation", "ws_conv_id", ws_conv_id
        else:
            return OpResult(C.INVALID, "a workspace conversation or a job must be named")
        row = self.store.one(f"SELECT * FROM {table} WHERE {key_col} = ?", (key,))
        if row is None:
            return OpResult(C.NOT_FOUND, f"unknown {table} {key}")
        at = C.now()
        fields: dict[str, Any] = {"updated_at": at}
        if archive is not None:
            fields["archived_at"] = at if archive else None
            fields["archive_reason"] = ((reason or "archived by the owner") if archive else None)
        if delete is not None:
            fields["deleted_at"] = at if delete else None
            fields["deletion_reason"] = ((reason or "deleted by the owner") if delete else None)
        with self.store.tx():
            self.store.update_row(table, fields, f"{key_col} = ?", (key,))
            if table == "job":
                self.store.recompute_conversation_filter_state(row["ws_conv_id"], at=at)
        fresh = self.store.one(f"SELECT * FROM {table} WHERE {key_col} = ?", (key,))
        data = self._retention_view(fresh)
        self.store.audit(actor=actor, operation=operation, entity_kind=table, entity_id=key,
                        reason=reason or operation,
                        details={"archive_state": data["archive_state"],
                                 "deletion_state": data["deletion_state"],
                                 "source_rows_deleted": False},
                        origin=row.get("origin") or C.REAL,
                        mock_label=row.get("mock_label"))
        return OpResult(C.OK, f"{key}: archive={data['archive_state']} "
                              f"deletion={data['deletion_state']}; nothing was removed "
                              "from storage", data=data, provenance=row.get("origin") or C.REAL,
                        label=row.get("mock_label"), mocked=(row.get("origin") == C.MOCK))

    @staticmethod
    def _retention_view(row: dict) -> dict:
        """Archive and deletion travel together so neither can be mistaken for the other."""
        return {
            "ws_conv_id": row.get("ws_conv_id"),
            "job_id": row.get("job_id"),
            "archive_state": (C.ArchiveState.ARCHIVED if row.get("archived_at")
                              else C.ArchiveState.ACTIVE),
            "archived_at": row.get("archived_at"),
            "archive_reason": row.get("archive_reason"),
            "deletion_state": (C.DeletionState.DELETED if row.get("deleted_at")
                               else C.DeletionState.RETAINED),
            "deleted_at": row.get("deleted_at"),
            "deletion_reason": row.get("deletion_reason"),
            "source_rows_deleted": False,
            "recoverable": True,
            "retention_note": Ledger.RETENTION_NOTE,
        }

    # ---------------------------------------------------------------- ingest ----

    def record_event(self, *, account_id: str, event_kind: str, dedup_key: str,
                     trigger_hash: str, project: Callable[[Any, dict], dict],
                     origin: str = C.REAL, mock_label: str | None = None) -> OpResult:
        """At-least-once ingest with a durable dedup key and idempotent projection.

        The dedup record, the projection and any cursor advance all commit in one
        transaction, so a crash can never leave a cursor ahead of its updates and
        a replay can never double-apply (PRD §6, §12, T08).
        """
        with self.store.tx():
            seen = self.store.one("SELECT * FROM event_dedup WHERE dedup_key = ?", (dedup_key,))
            if seen is not None:
                self.store.update_row("event_dedup", {
                    "seen_count": seen["seen_count"] + 1, "last_seen_at": C.now(),
                }, "dedup_key = ?", (dedup_key,))
                self.store.audit(actor="ingest", operation="duplicate_event",
                                entity_kind="event", entity_id=dedup_key,
                                reason="duplicate event ignored; projection not repeated",
                                within_tx=True, origin=origin, mock_label=mock_label)
                return OpResult(C.IDEMPOTENT_REPLAY, "duplicate source event",
                                data={"dedup_key": dedup_key, "projection": "already_applied"})
            now = C.now()
            self.store.insert_row("event_dedup", {
                "dedup_key": dedup_key, "account_id": account_id, "event_kind": event_kind,
                "trigger_hash": trigger_hash, "first_seen_at": now, "last_seen_at": now,
                "seen_count": 1, "projection_state": "applied", "projected_at": now,
            })
            outcome = project(self.store, {}) or {}
            self.store.audit(actor="ingest", operation=f"event:{event_kind}",
                            entity_kind="event", entity_id=dedup_key,
                            reason="projection applied", details=outcome,
                            within_tx=True, origin=origin, mock_label=mock_label)
        return OpResult(C.OK, "event projected once",
                        data={"dedup_key": dedup_key, **outcome})

    def set_checkpoint(self, *, account_id: str, scope: str, scope_ref: str,
                       cursor: str | None, coverage: dict, coverage_state: str,
                       overlap_state: str | None = None, gap_reason: str | None = None,
                       oldest_observed_time: str | None = None,
                       origin: str = C.REAL, mock_label: str | None = None) -> OpResult:
        now = C.now()
        row = {
            "checkpoint_id": C.stable_id("ckpt", account_id, scope, scope_ref),
            "account_id": account_id, "scope": scope, "scope_ref": scope_ref,
            "cursor_value": cursor, "overlap_state": overlap_state,
            "oldest_observed_time": oldest_observed_time, "last_success_at": now,
            "last_attempt_at": now, "pagination_state": "idle",
            "coverage_state": coverage_state, "coverage_json": C.canonical_json(coverage),
            "gap_reason": gap_reason, "committed_updates": 1,
            "updated_at": now, "origin": origin, "mock_label": mock_label,
        }
        with self.store.tx():
            existing = self.store.one("SELECT * FROM sync_checkpoint WHERE checkpoint_id = ?",
                                      (row["checkpoint_id"],))
            if existing:
                row["committed_updates"] = existing["committed_updates"] + 1
                self.store.update_row("sync_checkpoint", row, "checkpoint_id = ?",
                                      (row["checkpoint_id"],))
            else:
                self.store.insert_row("sync_checkpoint", row)
        return OpResult(C.OK, "checkpoint committed with its updates",
                        data={"checkpoint_id": row["checkpoint_id"], "cursor": cursor,
                              "coverage_state": coverage_state})
