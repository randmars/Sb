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
            self._sync_workspace_for_job(job["ws_conv_id"], to_state, now)
            self.store.audit(actor=actor, operation=f"job:{to_state}", entity_kind="job",
                            entity_id=job_id, reason=reason,
                            version_before=job["state_version"],
                            version_after=job["state_version"] + 1,
                            operation_id=operation_id, details=details, within_tx=True)
        return OpResult(C.OK, reason or f"job is {to_state}",
                        data={"job_id": job_id, "job_state": to_state},
                        current_version=job["state_version"] + 1, operation_id=operation_id)

    def _sync_workspace_for_job(self, ws_conv_id: str, job_state: str, at: str) -> None:
        """Derive the review/queue filter state from job state, leaving source state alone."""
        mapping = {
            JobState.QUEUED: (QueueState.WORKING, ReviewState.NONE, None),
            JobState.RUNNING: (QueueState.WORKING, ReviewState.NONE, None),
            JobState.WAITING_FOR_SOURCE: (QueueState.WORKING, ReviewState.NONE, "waiting_for_source"),
            JobState.WAITING_FOR_USER: (QueueState.NEEDS_ME, ReviewState.AWAITING_INPUT, "question"),
            JobState.WAITING_FOR_APPROVAL: (QueueState.NEEDS_ME, ReviewState.AWAITING_APPROVAL,
                                            "approval"),
            JobState.READY_FOR_REVIEW: (QueueState.NEEDS_ME, ReviewState.AWAITING_REVIEW, "draft"),
            JobState.SUCCEEDED: (QueueState.IDLE, ReviewState.NONE, None),
            JobState.FAILED: (QueueState.NEEDS_ME, ReviewState.BLOCKED, "failure"),
            JobState.CANCELLED: (QueueState.IDLE, ReviewState.NONE, "cancelled"),
            JobState.SUPERSEDED: (QueueState.IDLE, ReviewState.NONE, "superseded"),
        }
        queue_state, review_state, reason = mapping[job_state]
        self.store.update_row("workspace_conversation", {
            "queue_state": queue_state, "review_state": review_state,
            "needs_me_reason": reason, "updated_at": at,
        }, "ws_conv_id = ?", (ws_conv_id,))

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
            self.store.update_row("workspace_conversation", {
                "queue_state": QueueState.WORKING, "review_state": ReviewState.NONE,
                "needs_me_reason": None, "updated_at": now,
            }, "ws_conv_id = ?", (job["ws_conv_id"],))
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
                    "updated_at": now,
                }, "job_id = ?", (job_id,))
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
    def needs_me(self, limit: int = 50) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM workspace_conversation WHERE queue_state = ? "
            "ORDER BY updated_at DESC LIMIT ?", (QueueState.NEEDS_ME, limit))
        return [self._ws_brief(r) for r in rows]

    def working(self, limit: int = 50) -> list[dict]:
        rows = self.store.all(
            "SELECT * FROM workspace_conversation WHERE queue_state = ? "
            "ORDER BY updated_at DESC LIMIT ?", (QueueState.WORKING, limit))
        return [self._ws_brief(r) for r in rows]

    def all_conversations(self, limit: int = 100) -> list[dict]:
        rows = self.store.all("SELECT * FROM workspace_conversation "
                              "ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [self._ws_brief(r) for r in rows]

    def counts(self) -> dict:
        return {
            "needs_me": self.store.scalar("SELECT COUNT(*) FROM workspace_conversation WHERE queue_state=?",
                                          (QueueState.NEEDS_ME,)),
            "working": self.store.scalar("SELECT COUNT(*) FROM workspace_conversation WHERE queue_state=?",
                                         (QueueState.WORKING,)),
            "all": self.store.scalar("SELECT COUNT(*) FROM workspace_conversation"),
            "stalled_jobs": len(self.store.all(
                "SELECT job_id FROM job WHERE job_state = ? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < ?", (JobState.RUNNING, C.now()))),
        }

    def _ws_brief(self, ws: dict) -> dict:
        jobs = self.store.all("SELECT * FROM job WHERE ws_conv_id = ? ORDER BY created_at DESC",
                              (ws["ws_conv_id"],))
        sources = self.store.all(
            "SELECT c.conv_id, c.namespaced_id, c.availability, c.availability_reason, "
            "c.source_time_last, c.audience_kind, c.origin, c.mock_label, l.relevance "
            "FROM workspace_source_link l JOIN source_conversation c ON c.conv_id = l.conv_id "
            "WHERE l.ws_conv_id = ? ORDER BY c.source_time_last DESC", (ws["ws_conv_id"],))
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
