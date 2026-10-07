"""Grace application service: the operations the CLI and the tests drive.

This is the application-owned layer of PRD §12/§13: every mutation carries an
operation ID and an expected current version, conflicts return the current version
for explicit reconciliation rather than overwriting newer edits, and the durable
ledger (not any agent session) is the source of truth.

The "worker" step is a *simulated* agent pass. It is explicitly labelled: on this
Linux computer there is no Hermes runtime, so ``run_job`` does not claim to execute
an agent. It exercises the ledger, the lease, the draft and the approval contract
using the labelled mock adapters only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from . import contracts as C
from .adapters import SourceAdapter, default_adapters
from .contracts import JobState, OpResult, QueueState, ReviewState
from .effects import Effects, InjectedFault
from .fixtures import build_corpus, scenario_names
from .ingest import Ingest
from .ledger import Ledger
from .rules import Rules
from .store import DEFAULT_DB, Store

MOCK_WORKER_NOTE = ("MOCK worker pass: no agent, model or Hermes run was executed. This step "
                    "simulates the application-owned part of a job (lease, draft, review state) "
                    "so the ledger can be exercised here.")


class Grace:
    def __init__(self, db_path: str | Path = DEFAULT_DB, *, scenario: str | None = None,
                 faults: Iterable[str] = (), owner: str = "owner"):
        self.corpus = build_corpus()
        self.adapters: dict[str, SourceAdapter] = default_adapters(
            self.corpus, scenario=scenario, faults=faults)
        self.store = Store(db_path)
        self.ledger = Ledger(self.store)
        self.rules = Rules(self.store, self.ledger)
        self.ingest = Ingest(self.store, self.ledger, self.adapters, self.rules)
        self.effects = Effects(self.store, self.ledger, self.adapters, owner=owner)
        self.scenario_name = scenario
        self.faults = set(faults)

    def close(self) -> None:
        self.store.close()

    # ---------------------------------------------------------------- seed ----
    def seed(self, *, reset: bool = False) -> OpResult:
        if reset:
            for table in ("outbox", "audit_event", "event_dedup", "sync_checkpoint",
                          "rule_run_item", "rule_run", "rule", "receipt", "result",
                          "effect_attempt", "effect_operation", "approval", "attachment_ref",
                          "draft", "session_binding", "attempt", "job_input", "job_transition",
                          "job", "workspace_source_link", "workspace_conversation",
                          "relation", "memory_fact", "person_merge", "identity_link", "person",
                          "grp", "message_ref", "source_conversation", "capability",
                          "source_account"):
                with self.store.tx():
                    self.store.conn.execute(f"DELETE FROM {table}")
        result = self.ingest.seed(self.corpus)
        return result

    # ------------------------------------------------------------ assignment --
    def assign(self, ws_conv_id: str, instruction: str, agent: str, *,
               operation_id: str | None = None,
               source_refs: Iterable[str] = ()) -> OpResult:
        """Store the instruction and create a job; acknowledge without waiting for a model."""
        ws = self.store.one("SELECT * FROM workspace_conversation WHERE ws_conv_id = ?",
                            (ws_conv_id,))
        if ws is None:
            return OpResult(C.NOT_FOUND, f"unknown workspace conversation {ws_conv_id}")
        origin, label = ws["origin"], ws["mock_label"]
        plan = {
            "systems": ["mock_mail", "mock_beeper"],
            "target": None,
            "requested_effect": "prepare_draft",
            "required_data": ["selected source conversation", "person context"],
            "authorization": "owner_instruction (drafting only; sending needs its own approval)",
            "verification": "application receipt for drafts; source reconciliation for any send",
        }
        result = self.ledger.create_job(
            ws_conv_id=ws_conv_id, instruction=instruction, agent=agent,
            capability_plan=plan, dependencies=list(source_refs), operation_id=operation_id,
            origin=origin, mock_label=label)
        if result.ok and result.data:
            with self.store.tx():
                version = int(self.store.scalar(
                    "SELECT COALESCE(MAX(current_input_revision), 0) + 1 FROM "
                    "workspace_conversation WHERE ws_conv_id = ?", (ws_conv_id,)) or 1)
                self.store.update_row("workspace_conversation",
                                      {"current_input_revision": version},
                                      "ws_conv_id = ?", (ws_conv_id,))
        return result

    def answer(self, job_id: str, content: str) -> OpResult:
        """Persist the answer and resume the exact waiting job; never an approval."""
        return self.ledger.append_input(job_id, kind="answer", content=content, author="owner")

    def cancel_job(self, job_id: str, reason: str) -> OpResult:
        job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
        if job is None:
            return OpResult(C.NOT_FOUND, f"unknown job {job_id}")
        result = self.ledger.request_cancellation(job_id, reason=reason)
        if result.ok:
            for effect in self.store.all(
                    "SELECT effect_id FROM effect_operation WHERE job_id = ? AND effect_state "
                    "IN ('prepared','approved','dispatching','provider_accepted','provider_pending')",
                    (job_id,)):
                self.effects.cancel(effect["effect_id"],
                                    reason=f"job cancelled: {reason}")
        return result

    def supersede(self, job_id: str, *, new_job_id: str, reason: str) -> OpResult:
        revoked = self.effects.revoke_approvals_for_job(
            job_id, "run superseded; unconsumed approvals are revoked")
        result = self.ledger.mark_superseded(job_id, by_job_id=new_job_id, reason=reason)
        if result.data is not None:
            result.data["revoked_approvals"] = revoked
        return result

    # ------------------------------------------------------ simulated worker --
    def run_job(self, job_id: str, *, worker: str = "mock-worker-1", lease_seconds: int = 30,
                destination_conv_id: str | None = None, mode: str = "reply",
                draft_body: str | None = None, question: str | None = None,
                fail_with: str | None = None, stall: bool = False,
                renew_once: bool = True) -> OpResult:
        """Simulated agent pass over the labelled mocks. Clearly labelled as a mock.

        ``stall=True`` claims the lease and stops without renewing: the point is that a
        stalled job expires its lease and becomes visible (PRD §9).
        """
        claim = self.ledger.claim(job_id, worker=worker, lease_seconds=lease_seconds)
        if not claim.ok:
            return claim
        job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
        if stall:
            return OpResult(C.OK,
                            "MOCK worker claimed the job and stopped without renewing its lease",
                            data={"job_id": job_id, "worker": worker,
                                  "lease_expires_at": claim.data["lease_expires_at"],
                                  "note": MOCK_WORKER_NOTE}, mocked=True,
                            label=job["mock_label"])
        if renew_once:
            self.ledger.renew_lease(job_id, worker=worker, lease_seconds=lease_seconds)
        hermes = self.adapters.get("mock_hermes")
        binding = None
        if hermes is not None and hasattr(hermes, "start_run"):
            run = hermes.start_run(session_key=f"ws:{job['ws_conv_id']}", job_id=job_id,
                                   instruction=job["instruction"])
            if run.succeeded:
                run_id = run.data["run"]["run_id"]
                binding = self.ledger.bind_session(
                    ws_conv_id=job["ws_conv_id"], job_id=job_id, profile="mock-profile",
                    session_key=f"ws:{job['ws_conv_id']}", session_id=f"mock-session-{run_id}",
                    run_id=run_id, origin=job["origin"], mock_label=job["mock_label"])
                self.ledger.finish_attempt(job_id, outcome_code=C.SUCCESS,
                                           host_run_ref=run_id,
                                           checkpoint={"stage": "run_started"})
        if fail_with:
            self.ledger.finish_attempt(job_id, outcome_code=fail_with,
                                       detail="MOCK: injected worker failure")
            target = JobState.FAILED
            if fail_with in (C.OFFLINE, C.RATE_LIMITED, C.RETRYABLE_ERROR):
                target = JobState.WAITING_FOR_USER
            return self.ledger.transition(job_id, target, actor=worker,
                                          reason=f"MOCK worker reported {fail_with}")
        if question:
            self.effects.add_result(ws_conv_id=job["ws_conv_id"], job_id=job_id, kind="question",
                                    summary=question,
                                    detail={"question": question,
                                            "missing": "order status (simulated)"},
                                    origin=job["origin"], mock_label=job["mock_label"])
            self.ledger.finish_attempt(job_id, outcome_code=C.SUCCESS,
                                       detail="question returned to the owner")
            return self.ledger.transition(job_id, JobState.WAITING_FOR_USER, actor=worker,
                                          reason="MOCK agent needs information before drafting")
        destination = destination_conv_id or self._default_destination(job["ws_conv_id"])
        if destination is None:
            self.ledger.finish_attempt(job_id, outcome_code=C.PARTIAL,
                                       detail="no source conversation available for drafting")
            return self.ledger.transition(job_id, JobState.WAITING_FOR_SOURCE, actor=worker,
                                          reason="no source conversation available")
        attachments, blocking = self._attachments_for(destination)
        body = draft_body or (
            f"[MOCK DRAFT — invented fixture text, no real message exists]\n\n"
            f"Hi,\n\nRe: your last message — {job['instruction'].strip()}\n"
            f"Requested action: {job['agent']} (simulated pass)\n\nThanks,\nRandy")
        draft = self.effects.create_draft(
            ws_conv_id=job["ws_conv_id"], job_id=job_id, destination_conv_id=destination,
            mode=mode, subject=self._subject_for(destination, mode), body=body,
            purpose="reply to the owner's instruction in this conversation",
            attachment_refs=attachments, author=job["agent"],
            blocking_limitations=blocking,
            origin=job["origin"], mock_label=job["mock_label"])
        self.ledger.finish_attempt(job_id, outcome_code=C.SUCCESS,
                                  detail="MOCK pass produced a draft for review")
        result = self.effects.add_result(
            ws_conv_id=job["ws_conv_id"], job_id=job_id, kind="draft",
            summary="Draft prepared for review (MOCK)", detail={"draft_id": draft.data["draft_id"]},
            origin=job["origin"], mock_label=job["mock_label"])
        return OpResult(C.OK, "MOCK worker pass finished: draft ready for review",
                        data={"job_id": job_id, "draft": draft.data, "result_id": result.data,
                              "session_binding": binding.data if binding else None,
                              "note": MOCK_WORKER_NOTE, "worker": worker,
                              "lease_seconds": lease_seconds},
                        mocked=True, label=job["mock_label"])

    def _default_destination(self, ws_conv_id: str) -> Optional[str]:
        rows = self.store.all(
            "SELECT c.conv_id, c.source_time_last FROM workspace_source_link l "
            "JOIN source_conversation c ON c.conv_id = l.conv_id "
            "WHERE l.ws_conv_id = ? AND l.relevance = 'primary' "
            "ORDER BY c.source_time_last DESC", (ws_conv_id,))
        return rows[0]["conv_id"] if rows else None

    def _subject_for(self, conv_id: str, mode: str) -> Optional[str]:
        conv = self.store.one("SELECT * FROM source_conversation WHERE conv_id = ?", (conv_id,))
        latest = self.store.one("SELECT * FROM message_ref WHERE conv_id = ? "
                                "ORDER BY source_time DESC LIMIT 1", (conv_id,))
        if conv is None or latest is None:
            return None
        meta = json.loads(latest["minimal_metadata"] or "{}")
        subject = meta.get("subject")
        if not subject or conv["audience_kind"] != "direct" or conv["adapter"] != "mock_mail":
            return subject
        return subject if subject.lower().startswith("re:") else f"Re: {subject}"

    def _attachments_for(self, conv_id: str) -> tuple[list[dict], Optional[str]]:
        latest = self.store.one("SELECT * FROM message_ref WHERE conv_id = ? "
                                "ORDER BY source_time DESC LIMIT 1", (conv_id,))
        if latest is None:
            return [], None
        rows = self.store.all("SELECT * FROM attachment_ref WHERE msg_ref_id = ?",
                              (latest["msg_ref_id"],))
        if not rows:
            return [], None
        blocking = None
        if any(r["availability"] != "available" for r in rows):
            blocking = ("an attachment needed to review this communication is not available "
                        "off-device; approval is blocked until it is downloaded or the owner "
                        "acknowledges the limitation (PRD §10)")
        return rows, blocking

    # ------------------------------------------------------------- surfaces ---
    def needs_me(self, limit: int = 50) -> list[dict]:
        return self.ledger.needs_me(limit)

    def working(self, limit: int = 50) -> list[dict]:
        return self.ledger.working(limit)

    def all_conversations(self, limit: int = 100) -> list[dict]:
        return self.ledger.all_conversations(limit)

    def health(self) -> dict:
        mocked = any(a.simulated for a in self.adapters.values())
        data = {
            "schema": self.store.health(),
            "queues": self.ledger.counts(),
            "adapters": [a.describe() for a in self.adapters.values()],
            "sources": self.ingest.source_health(),
            "capability_honesty": {
                "real_sources_connected": False,
                "explanation": ("This deployment has no Mail, Beeper, Contacts or Hermes connection. "
                                "Those integrations are Gate 2/3 work on Randy's Mac and have not "
                                "been exercised here."),
                "unsupported_states_are_explicit": True,
            },
        }
        return {"data": data, "mocked": mocked,
                "mock_label": C.mock_label("adapters") if mocked else None,
                "disclaimer": C.MOCK_DISCLAIMER if mocked else None}
