"""Drafts, versioned approvals and the outbound effect ledger.

Implements PRD §10 (draft and approval contract, outbound operation ledger,
browser/computer concurrency rules that apply to external effects) and the parts
of §12/§13 that govern external side effects.

Invariants this module exists to hold:

* An approval is bound to an immutable draft version, sender identity, recipient
  and audience snapshot, destination, body and attachment hashes, purpose,
  operation ID, issuance time and expiry. Editing any bound field invalidates it.
* Revalidation happens immediately before dispatch; a changed route, audience,
  rule state or authorization denies dispatch rather than proceeding silently.
* Exactly one dispatch decision per approval: the approval row is consumed by a
  conditional UPDATE inside one immediate transaction, so double taps and
  concurrent clients cannot produce two sends.
* A definite pre-submission failure may be retried under the same authorization if
  scope is unchanged. Anything uncertain (failure after submission, a timeout, an
  unknown state) enters reconciliation and is never retried blindly.
* No green "Sent" is ever inferred from a successful call: the receipt records what
  the source actually said, at the verification level that evidence supports.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from . import contracts as C
from .adapters import SourceAdapter
from .contracts import (
    ApprovalState, EffectState, JobState, OpResult, QueueState, ReviewState, VerificationLevel,
)
from .ledger import Ledger
from .store import Store


class InjectedFault(RuntimeError):
    """Fault injection used by tests and the demo to reproduce crash windows.

    Always labelled: it exists only to prove that recovery works, and it never
    fabricates a successful outcome.
    """

    def __init__(self, where: str, detail: str):
        super().__init__(f"INJECTED-FAULT[{where}]: {detail}")
        self.where = where


# --------------------------------------------------------------- audiences ----


def audience_snapshot(store: Store, conv_id: str, mode: str) -> dict:
    """Frozen recipient snapshot for a draft (PRD §4/§10).

    The reply audience is taken from the most recent *inbound* message, so a reply goes
    to the counterparty rather than back to the owner. ``reply`` keeps that sender; the
    ``reply_all`` mode adds the Cc list from that same message but never silently adds
    Bcc — original Bcc is recorded as a risk note instead, so the audience cannot be
    widened by accident (T21).
    """
    conv = store.one("SELECT * FROM source_conversation WHERE conv_id = ?", (conv_id,))
    if conv is None:
        return {"to": [], "cc": [], "bcc": [], "audience_kind": "unknown",
                "membership_version": None, "unavailable": "no source conversation"}
    acct = store.one("SELECT * FROM source_account WHERE account_id = ?", (conv["account_id"],))
    owner_identity = (acct["account_identity"] if acct else "").lower()
    messages = store.all("SELECT * FROM message_ref WHERE conv_id = ? "
                         "AND deleted_at_source = 0 ORDER BY source_time DESC", (conv_id,))
    inbound = None
    for row in messages:
        sender = json.loads(row["sender_json"] or "{}")
        value = (sender.get("address") or sender.get("network_identity") or "").lower()
        if value and value != owner_identity:
            inbound = row
            break
    latest = inbound or (messages[0] if messages else None)
    if latest is None:
        return {"to": [], "cc": [], "bcc": [], "audience_kind": "unknown",
                "membership_version": None, "unavailable": "no source message"}
    sender = json.loads(latest["sender_json"] or "{}")
    to = [sender] if sender else json.loads(latest["recipients_json"])
    cc = _json_list(store, latest["msg_ref_id"], "cc_json")
    bcc = _json_list(store, latest["msg_ref_id"], "bcc_json")
    membership_version = None
    if conv["audience_kind"] == "group":
        grp = store.one("SELECT * FROM grp WHERE stable_source_identity = ? AND account_id = ?",
                        (conv["provider_chat_id"], conv["account_id"]))
        membership_version = grp["membership_version"] if grp else None
    snapshot = {
        "to": to,
        "cc": cc if mode == "reply_all" else [],
        "bcc": [],
        "audience_kind": conv["audience_kind"],
        "membership_version": membership_version,
        "reply_all": mode == "reply_all",
        "derived_from_message": latest["namespaced_id"],
        "derived_from_source_time": latest["source_time"],
    }
    if bcc:
        snapshot["bcc_not_included"] = bcc
        snapshot["bcc_risk_noted"] = (
            "the source message had Bcc recipients; they are not added to this draft and must be "
            "reviewed explicitly before any reply-all")
    if latest["availability"] != "available":
        snapshot["source_availability"] = {"state": latest["availability"],
                                           "reason": latest["availability_reason"]}
    return snapshot


def _json_list(store: Store, msg_ref_id: str, column: str) -> list:
    row = store.conn.execute(
        f"SELECT {column} FROM message_ref WHERE msg_ref_id = ?", (msg_ref_id,)).fetchone()
    if row is None:
        return []
    return json.loads(row[0] or "[]")


def audience_hash(snapshot: dict) -> str:
    return C.sha256_hex(snapshot)


def _attachment_digest(refs: Iterable[dict]) -> str:
    material = sorted(
        ({"namespaced_id": r.get("namespaced_id"), "filename": r.get("filename"),
          "media_type": r.get("media_type"), "size_bytes": r.get("size_bytes"),
          "content_hash": r.get("content_hash"), "availability": r.get("availability")}
         for r in refs), key=lambda d: (d["namespaced_id"] or ""))
    return C.sha256_hex(material)


class Effects:
    """Drafts, approvals and outbound operations."""

    def __init__(self, store: Store, ledger: Ledger, adapters: dict[str, SourceAdapter],
                 *, owner: str = "owner"):
        self.store = store
        self.ledger = ledger
        self.adapters = adapters
        self.owner = owner

    # ------------------------------------------------------------- drafts -----
    def create_draft(self, *, ws_conv_id: str, job_id: str | None, destination_conv_id: str,
                     mode: str, subject: str | None, body: str, purpose: str,
                     version: int | None = None, attachment_refs: Iterable[dict] = (),
                     sender_identity: str | None = None, author: str = "agent",
                     blocking_limitations: str | None = None,
                     origin: str = C.REAL, mock_label: str | None = None) -> OpResult:
        conv = self.store.one("SELECT * FROM source_conversation WHERE conv_id = ?",
                              (destination_conv_id,))
        if conv is None:
            return OpResult(C.NOT_FOUND, f"unknown destination conversation {destination_conv_id}")
        if mode not in ("reply", "reply_all", "new"):
            return OpResult(C.INVALID, f"unknown draft mode {mode!r}")
        acct = self.store.one("SELECT * FROM source_account WHERE account_id = ?",
                              (conv["account_id"],))
        sender = sender_identity or acct["account_identity"]
        snapshot = audience_snapshot(self.store, destination_conv_id, mode)
        a_hash = audience_hash(snapshot)
        refs = [dict(r) for r in attachment_refs]
        att_hash = _attachment_digest(refs)
        body_hash = C.text_hash(body)
        if version is None:
            version = int(self.store.scalar(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM draft WHERE ws_conv_id = ? "
                "AND COALESCE(job_id,'') = COALESCE(?, '')", (ws_conv_id, job_id)) or 1)
        content_hash = C.sha256_hex({
            "account_id": conv["account_id"], "channel": conv["adapter"],
            "destination_conv_id": destination_conv_id, "mode": mode, "subject": subject,
            "body_hash": body_hash, "audience_hash": a_hash, "attachment_hash": att_hash,
            "purpose": purpose, "sender_identity": sender, "version": version,
        })
        draft_id = C.new_id("draft")
        now = C.now()
        with self.store.tx():
            self.store.insert_row("draft", {
                "draft_id": draft_id, "ws_conv_id": ws_conv_id, "job_id": job_id,
                "version": version, "parent_version": None, "immutable": 1,
                "account_id": conv["account_id"], "channel": conv["adapter"],
                "destination_conv_id": destination_conv_id,
                "destination_provider_id": conv["provider_thread_id"] or conv["provider_chat_id"],
                "destination_resolved": 1, "mode": mode, "subject": subject, "body": body,
                "recipient_snapshot": C.canonical_json(snapshot), "audience_hash": a_hash,
                "membership_version": snapshot.get("membership_version"),
                "body_hash": body_hash, "attachment_hash": att_hash,
                "content_hash": content_hash, "purpose": purpose, "sender_identity": sender,
                "source_refs_json": C.canonical_json([destination_conv_id]),
                "author": author, "superseded_by": None, "invalidated_at": None,
                "invalidation_reason": None, "blocking_limitations": blocking_limitations,
                "created_at": now, "origin": origin, "mock_label": mock_label,
            })
            for i, ref in enumerate(refs, start=1):
                ref_id = ref.get("attachment_ref_id")
                if ref_id and self.store.one(
                        "SELECT attachment_ref_id FROM attachment_ref WHERE attachment_ref_id = ?",
                        (ref_id,)):
                    # The source attachment record already exists (fixture or earlier ingest);
                    # this draft needs its own reference row.
                    ref_id = None
                self.store.insert_row("attachment_ref", {
                    "attachment_ref_id": ref_id or C.new_id("att"),
                    "draft_id": draft_id, "job_id": job_id, "msg_ref_id": ref.get("msg_ref_id"),
                    "namespaced_id": ref["namespaced_id"], "filename": ref["filename"],
                    "media_type": ref["media_type"], "size_bytes": ref.get("size_bytes"),
                    "content_hash": ref.get("content_hash"),
                    "download_state": ref.get("download_state", "not_downloaded"),
                    "availability": ref.get("availability", "unknown"),
                    "limitation": ref.get("limitation"),
                    "quarantine_state": ref.get("quarantine_state", "none"),
                    "created_at": now, "origin": origin, "mock_label": mock_label,
                })
            self.store.audit(actor=author, operation="create_draft", entity_kind="draft",
                            entity_id=draft_id, reason=f"draft v{version} persisted before review",
                            version_before=0, version_after=version,
                            details={"content_hash": content_hash, "audience_hash": a_hash,
                                     "attachment_hash": att_hash,
                                     "blocking_limitations": blocking_limitations},
                            origin=origin, mock_label=mock_label, within_tx=True)
        if job_id:
            # Outside the transaction: no DB transaction is ever held across another unit of work.
            self.ledger.transition(job_id, JobState.READY_FOR_REVIEW, actor=author,
                                   reason="draft ready for review",
                                   details={"draft_id": draft_id, "version": version})
        return OpResult(C.OK, f"draft v{version} created (immutable)",
                        data=self.draft_view(draft_id), provenance=origin, label=mock_label,
                        mocked=(origin == C.MOCK))

    def draft_view(self, draft_id: str) -> dict:
        row = self.store.one("SELECT * FROM draft WHERE draft_id = ?", (draft_id,))
        if row is None:
            return {}
        return self._draft_dict(row)

    def _draft_dict(self, row: dict) -> dict:
        attachments = self.store.all("SELECT * FROM attachment_ref WHERE draft_id = ?",
                                     (row["draft_id"],))
        return {
            "draft_id": row["draft_id"], "version": row["version"], "immutable": bool(row["immutable"]),
            "ws_conv_id": row["ws_conv_id"], "job_id": row["job_id"],
            "draft_version_key": f"{row['draft_id']}@v{row['version']}",
            "account_id": row["account_id"], "channel": row["channel"],
            "sender_identity": row["sender_identity"],
            "destination": {"conv_id": row["destination_conv_id"],
                            "provider_id": row["destination_provider_id"],
                            "resolved": bool(row["destination_resolved"])},
            "mode": row["mode"], "subject": row["subject"], "purpose": row["purpose"],
            "body": row["body"],
            "recipients": json.loads(row["recipient_snapshot"]),
            "hashes": {"audience": row["audience_hash"], "body": row["body_hash"],
                       "attachment": row["attachment_hash"], "content": row["content_hash"]},
            "attachments": attachments,
            "blocking_limitations": row["blocking_limitations"],
            "superseded_by": row["superseded_by"],
            "invalidated_at": row["invalidated_at"],
            "invalidation_reason": row["invalidation_reason"],
            "author": row["author"], "created_at": row["created_at"],
            "origin": row["origin"], "mock_label": row["mock_label"],
        }

    def latest_draft(self, ws_conv_id: str, job_id: str | None = None) -> Optional[dict]:
        row = self.store.one(
            "SELECT * FROM draft WHERE ws_conv_id = ? AND COALESCE(job_id,'') = COALESCE(?, '') "
            "AND superseded_by IS NULL ORDER BY version DESC LIMIT 1", (ws_conv_id, job_id))
        return self._draft_dict(row) if row else None

    def revise_draft(self, draft_id: str, *, expected_version: int | None = None,
                     body: str | None = None, subject: str | None = None,
                     mode: str | None = None, recipients_note: str | None = None,
                     actor: str = "owner", operation_id: str | None = None) -> OpResult:
        """Editing a bound field creates a NEW immutable version and invalidates approvals."""
        row = self.store.one("SELECT * FROM draft WHERE draft_id = ?", (draft_id,))
        if row is None:
            return OpResult(C.NOT_FOUND, f"unknown draft {draft_id}")
        if row["superseded_by"]:
            return OpResult(C.INVALID, "this draft version is superseded; edit the current version",
                            current_version=row["version"])
        if expected_version is not None and expected_version != row["version"]:
            return OpResult(C.CONFLICT, "draft was revised by someone else; returning current "
                                        "version for explicit reconciliation",
                            current_version=row["version"])
        new_body = row["body"] if body is None else body
        new_subject = row["subject"] if subject is None else subject
        new_mode = row["mode"] if mode is None else mode
        snapshot = json.loads(row["recipient_snapshot"])
        if recipients_note:
            snapshot["reviewer_note"] = recipients_note
        a_hash = audience_hash(snapshot)
        att_hash = row["attachment_hash"]
        body_hash = C.text_hash(new_body)
        content_hash = C.sha256_hex({
            "account_id": row["account_id"], "channel": row["channel"],
            "destination_conv_id": row["destination_conv_id"], "mode": new_mode,
            "subject": new_subject, "body_hash": body_hash, "audience_hash": a_hash,
            "attachment_hash": att_hash, "purpose": row["purpose"],
            "sender_identity": row["sender_identity"], "version": row["version"] + 1,
        })
        new_id = C.new_id("draft")
        now = C.now()
        changed = []
        if body is not None and body != row["body"]:
            changed.append("body")
        if subject is not None and subject != row["subject"]:
            changed.append("subject")
        if mode is not None and mode != row["mode"]:
            changed.append("mode")
        if recipients_note:
            changed.append("recipient_snapshot")
        with self.store.tx():
            self.store.insert_row("draft", {
                **{k: row[k] for k in ("ws_conv_id", "job_id", "account_id", "channel",
                                       "destination_conv_id", "destination_provider_id",
                                       "destination_resolved", "purpose", "sender_identity",
                                       "source_refs_json", "origin", "mock_label")},
                "draft_id": new_id, "version": row["version"] + 1, "parent_version": row["version"],
                "immutable": 1, "mode": new_mode, "subject": new_subject, "body": new_body,
                "recipient_snapshot": C.canonical_json(snapshot), "audience_hash": a_hash,
                "membership_version": snapshot.get("membership_version"),
                "body_hash": body_hash, "attachment_hash": att_hash, "content_hash": content_hash,
                "author": actor, "superseded_by": None, "invalidated_at": None,
                "invalidation_reason": None, "blocking_limitations": row["blocking_limitations"],
                "created_at": now,
            })
            self.store.update_row("draft", {"superseded_by": new_id}, "draft_id = ?", (draft_id,))
            invalidated = self.store.all(
                "SELECT approval_id FROM approval WHERE draft_id = ? AND approval_state = ?",
                (draft_id, ApprovalState.GRANTED))
            for appr in invalidated:
                self.store.update_row("approval", {
                    "approval_state": ApprovalState.INVALIDATED,
                    "invalidated_at": now,
                    "invalidation_reason": "bound draft version edited ("
                                           + ", ".join(changed or ["no field changed"]) + ")",
                }, "approval_id = ?", (appr["approval_id"],))
            self.store.audit(actor=actor, operation="revise_draft", entity_kind="draft",
                            entity_id=new_id, reason="draft revised; approval invalidated",
                            version_before=row["version"], version_after=row["version"] + 1,
                            operation_id=operation_id,
                            details={"changed_fields": changed,
                                     "invalidated_approvals": [a["approval_id"] for a in invalidated]},
                            origin=row["origin"], mock_label=row["mock_label"], within_tx=True)
        return OpResult(C.OK,
                        "new draft version created; any approval of the previous version is invalid",
                        data={"draft": self.draft_view(new_id),
                              "superseded_versions": [f"{draft_id}@v{row['version']}"],
                              "invalidated_approvals": [a["approval_id"] for a in invalidated],
                              "changed_fields": changed},
                        current_version=row["version"] + 1, operation_id=operation_id)

    # ---------------------------------------------------------- approvals -----
    def grant_approval(self, draft_id: str, *, operation_id: str, ttl_seconds: int = 900,
                       acknowledgements: Iterable[str] = (), owner: str | None = None,
                       scope: dict | None = None) -> OpResult:
        row = self.store.one("SELECT * FROM draft WHERE draft_id = ?", (draft_id,))
        if row is None:
            return OpResult(C.NOT_FOUND, f"unknown draft {draft_id}")
        if row["superseded_by"]:
            return OpResult(C.INVALID, "cannot approve a superseded draft version")
        existing = self.store.one("SELECT * FROM approval WHERE operation_id = ?", (operation_id,))
        if existing is not None:
            return OpResult(C.IDEMPOTENT_REPLAY,
                            "an approval already exists for this operation ID; no second decision",
                            data={"approval": self.approval_view(existing["approval_id"])},
                            operation_id=operation_id)
        if row["blocking_limitations"] and "limitations_acknowledged" not in set(acknowledgements):
            return OpResult(C.DENIED,
                            "approval blocked: " + row["blocking_limitations"]
                            + " (pass an explicit acknowledgement to proceed)")
        owner = owner or self.owner
        approval_id = C.new_id("appr")
        now = C.now()
        with self.store.tx():
            self.store.insert_row("approval", {
                "approval_id": approval_id, "owner": owner, "operation_id": operation_id,
                "draft_id": draft_id, "draft_version": row["version"],
                "bound_account_id": row["account_id"],
                "bound_sender_identity": row["sender_identity"],
                "bound_destination": row["destination_conv_id"],
                "bound_mode": row["mode"],
                "bound_recipient_snapshot": row["recipient_snapshot"],
                "bound_audience_hash": row["audience_hash"],
                "bound_body_hash": row["body_hash"],
                "bound_attachment_hash": row["attachment_hash"],
                "bound_content_hash": row["content_hash"],
                "bound_purpose": row["purpose"],
                "scope_json": C.canonical_json(scope or {
                    "kind": "one_off_send", "recipient": row["destination_conv_id"],
                    "purpose": row["purpose"], "data_category": "conversation reply",
                    "standing_authority": False,
                }),
                "approval_state": ApprovalState.GRANTED, "issued_at": now,
                "expires_at": C.iso(C.now_dt() + C.timedelta(seconds=ttl_seconds)),
                "consumed_at": None, "invalidated_at": None, "invalidation_reason": None,
                "superseded_by": None,
                "origin": row["origin"], "mock_label": row["mock_label"],
            })
            self.store.audit(actor=owner, operation="grant_approval", entity_kind="approval",
                            entity_id=approval_id, reason="one-off send authorization",
                            version_before=0, version_after=1, operation_id=operation_id,
                            details={"draft_version_key": f"{draft_id}@v{row['version']}",
                                     "content_hash": row["content_hash"]},
                            origin=row["origin"], mock_label=row["mock_label"], within_tx=True)
        if row["job_id"]:
            self.ledger.transition(row["job_id"], JobState.WAITING_FOR_APPROVAL,
                                   actor=owner, reason="owner approved the draft",
                                   details={"approval_id": approval_id})
        return OpResult(C.OK, "approval bound to this immutable draft version",
                        data={"approval": self.approval_view(approval_id)},
                        operation_id=operation_id)

    def approval_view(self, approval_id: str) -> dict:
        row = self.store.one("SELECT * FROM approval WHERE approval_id = ?", (approval_id,))
        if row is None:
            return {}
        return {
            "approval_id": row["approval_id"], "owner": row["owner"],
            "operation_id": row["operation_id"],
            "draft_version_key": f"{row['draft_id']}@v{row['draft_version']}",
            "bound": {
                "account_id": row["bound_account_id"],
                "sender_identity": row["bound_sender_identity"],
                "destination": row["bound_destination"], "mode": row["bound_mode"],
                "recipients": json.loads(row["bound_recipient_snapshot"]),
                "purpose": row["bound_purpose"],
                "hashes": {"audience": row["bound_audience_hash"], "body": row["bound_body_hash"],
                           "attachment": row["bound_attachment_hash"],
                           "content": row["bound_content_hash"]},
            },
            "scope": json.loads(row["scope_json"]),
            "approval_state": row["approval_state"], "issued_at": row["issued_at"],
            "expires_at": row["expires_at"], "consumed_at": row["consumed_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidation_reason": row["invalidation_reason"],
            "origin": row["origin"], "mock_label": row["mock_label"],
        }

    def revalidate_approval(self, approval_id: str, *, actor: str = "service") -> OpResult:
        """Revalidate binding and current source route immediately before dispatch (T13)."""
        appr = self.store.one("SELECT * FROM approval WHERE approval_id = ?", (approval_id,))
        if appr is None:
            return OpResult(C.NOT_FOUND, f"unknown approval {approval_id}")
        if appr["approval_state"] == ApprovalState.INVALIDATED:
            return OpResult(C.DENIED, "approval was invalidated: "
                            + str(appr["invalidation_reason"]))
        if appr["approval_state"] == ApprovalState.CONSUMED:
            return OpResult(C.ALREADY_IN_STATE, "approval already consumed",
                            data={"consumed_at": appr["consumed_at"]})
        if appr["approval_state"] != ApprovalState.GRANTED:
            return OpResult(C.DENIED, f"approval is {appr['approval_state']}")
        if C.is_past(appr["expires_at"]):
            with self.store.tx():
                self.store.update_row("approval", {"approval_state": ApprovalState.EXPIRED,
                                                   "invalidated_at": C.now(),
                                                   "invalidation_reason": "approval expired"},
                                      "approval_id = ?", (approval_id,))
            return OpResult(C.DENIED, "approval expired before dispatch")
        draft = self.store.one("SELECT * FROM draft WHERE draft_id = ?", (appr["draft_id"],))
        if draft is None:
            return self._deny(approval_id, "bound draft no longer exists", actor)
        if draft["version"] != appr["draft_version"] or draft["superseded_by"]:
            return self._deny(approval_id,
                              "bound draft version was edited or superseded", actor)
        mismatches = []
        pairs = (("bound_account_id", draft["account_id"], "account"),
                 ("bound_sender_identity", draft["sender_identity"], "sender identity"),
                 ("bound_destination", draft["destination_conv_id"], "destination"),
                 ("bound_mode", draft["mode"], "reply mode"),
                 ("bound_recipient_snapshot", draft["recipient_snapshot"], "recipient snapshot"),
                 ("bound_audience_hash", draft["audience_hash"], "audience hash"),
                 ("bound_body_hash", draft["body_hash"], "body hash"),
                 ("bound_attachment_hash", draft["attachment_hash"], "attachment hash"),
                 ("bound_content_hash", draft["content_hash"], "content hash"),
                 ("bound_purpose", draft["purpose"], "purpose"))
        for bound_key, current, human in pairs:
            if appr[bound_key] != current:
                mismatches.append(human)
        if mismatches:
            return self._deny(approval_id, "bound field changed: " + ", ".join(mismatches), actor)
        # live audience check: a new recipient, changed membership or a changed route invalidates
        fresh = audience_snapshot(self.store, draft["destination_conv_id"], draft["mode"])
        if audience_hash(fresh) != appr["bound_audience_hash"]:
            return self._deny(approval_id,
                              "source audience changed since approval (recipients, membership or "
                              "routing); the changed version must be approved", actor)
        return OpResult(C.OK, "approval still valid", data={"approval_id": approval_id})

    def _deny(self, approval_id: str, reason: str, actor: str) -> OpResult:
        appr = self.store.one("SELECT * FROM approval WHERE approval_id = ?", (approval_id,))
        with self.store.tx():
            self.store.update_row("approval", {
                "approval_state": ApprovalState.INVALIDATED, "invalidated_at": C.now(),
                "invalidation_reason": reason,
            }, "approval_id = ?", (approval_id,))
            self.store.audit(actor=actor, operation="approval_invalidated", entity_kind="approval",
                            entity_id=approval_id, reason=reason, within_tx=True,
                            origin=appr["origin"] if appr else C.REAL,
                            mock_label=appr["mock_label"] if appr else None)
        return OpResult(C.DENIED, "dispatch denied: " + reason,
                        data={"approval_id": approval_id})

    def expire_approvals(self) -> list[str]:
        expired = []
        for row in self.store.all("SELECT * FROM approval WHERE approval_state = ?",
                                  (ApprovalState.GRANTED,)):
            if C.is_past(row["expires_at"]):
                with self.store.tx():
                    self.store.update_row("approval", {
                        "approval_state": ApprovalState.EXPIRED, "invalidated_at": C.now(),
                        "invalidation_reason": "expiry passed without approval",
                    }, "approval_id = ?", (row["approval_id"],))
                expired.append(row["approval_id"])
        return expired

    def revoke_approvals_for_job(self, job_id: str, reason: str,
                                actor: str = "owner") -> list[str]:
        job = self.store.one("SELECT job_id FROM job WHERE job_id = ?", (job_id,))
        if job is None:
            return []
        revoked = []
        for row in self.store.all(
                "SELECT a.approval_id FROM approval a JOIN draft d ON d.draft_id = a.draft_id "
                "WHERE d.job_id = ? AND a.approval_state = ?", (job_id, ApprovalState.GRANTED)):
            with self.store.tx():
                self.store.update_row("approval", {
                    "approval_state": ApprovalState.REVOKED, "invalidated_at": C.now(),
                    "invalidation_reason": reason,
                }, "approval_id = ?", (row["approval_id"],))
            revoked.append(row["approval_id"])
        return revoked

    # ------------------------------------------------------------ dispatch ----
    def dispatch(self, approval_id: str, *, adapter_name: str | None = None,
                 actor: str = "owner", inject_crash_after: str | None = None) -> OpResult:
        """Consume the approval exactly once, then talk to the source outside any transaction."""
        valid = self.revalidate_approval(approval_id, actor=actor)
        if not valid.ok:
            if valid.code == C.ALREADY_IN_STATE:
                appr = self.store.one("SELECT operation_id FROM approval WHERE approval_id = ?",
                                      (approval_id,))
                existing = self.store.one("SELECT * FROM effect_operation WHERE operation_id = ?",
                                          (appr["operation_id"],))
                if existing:
                    return OpResult(C.IDEMPOTENT_REPLAY,
                                    "this approval already produced exactly one dispatch decision",
                                    data={"effect": self.effect_view(existing["effect_id"])})
            return OpResult(C.DENIED, valid.detail, data=valid.data)
        appr = self.store.one("SELECT * FROM approval WHERE approval_id = ?", (approval_id,))
        draft = self.store.one("SELECT * FROM draft WHERE draft_id = ?", (appr["draft_id"],))
        adapter = self.adapters.get(adapter_name or draft["channel"])
        if adapter is None:
            return OpResult(C.NOT_FOUND, f"no adapter named {adapter_name or draft['channel']}")
        request = self._dispatch_request(appr, draft)
        idempotency_key = self._idempotency_key(appr, draft)
        effect_id = C.new_id("eff")
        now = C.now()
        with self.store.tx():
            existing = self.store.one("SELECT * FROM effect_operation WHERE operation_id = ?",
                                      (appr["operation_id"],))
            if existing is not None:
                return OpResult(C.IDEMPOTENT_REPLAY,
                                "operation ID already dispatched; no second send",
                                data={"effect": self.effect_view(existing["effect_id"])})
            consumed = self.store.conn.execute(
                "UPDATE approval SET approval_state = ?, consumed_at = ? WHERE approval_id = ? "
                "AND approval_state = ? AND consumed_at IS NULL",
                (ApprovalState.CONSUMED, now, approval_id, ApprovalState.GRANTED)).rowcount
            if consumed != 1:
                # Another client consumed it in the same instant: one dispatch decision only.
                other = self.store.one("SELECT * FROM effect_operation WHERE operation_id = ?",
                                       (appr["operation_id"],))
                if other is not None:
                    return OpResult(C.IDEMPOTENT_REPLAY,
                                    "a concurrent client already dispatched this operation",
                                    data={"effect": self.effect_view(other["effect_id"])})
                return OpResult(C.CONFLICT, "approval was consumed or invalidated concurrently")
            self.store.insert_row("effect_operation", {
                "effect_id": effect_id, "operation_id": appr["operation_id"],
                "job_id": draft["job_id"], "ws_conv_id": draft["ws_conv_id"],
                "approval_id": approval_id, "adapter": adapter.name,
                "account_id": draft["account_id"],
                "target_conv_id": draft["destination_conv_id"],
                "target_provider_id": draft["destination_provider_id"],
                "purpose": draft["purpose"], "idempotency_key": idempotency_key,
                "request_hash": draft["content_hash"],
                "effect_state": EffectState.PREPARED, "attempt_count": 1,
                "provider_message_id": None, "source_operation_id": None,
                "actual_routed_destination": None, "provider_status": None,
                "requires_reconciliation": 1, "last_error_category": None,
                "created_at": now, "updated_at": now,
                "origin": draft["origin"], "mock_label": draft["mock_label"],
            })
            # Record the documented progression prepared -> approved -> dispatching so the
            # ledger shows the authorization step separately from the submission attempt.
            self.store.update_row("effect_operation", {"effect_state": EffectState.APPROVED,
                                                      "updated_at": C.now()},
                                  "effect_id = ?", (effect_id,))
            self.store.update_row("effect_operation", {"effect_state": EffectState.DISPATCHING,
                                                      "updated_at": C.now()},
                                  "effect_id = ?", (effect_id,))
            self.store.audit(actor=actor, operation="effect_dispatching", entity_kind="effect",
                            entity_id=effect_id, reason="approval consumed; operation persisted "
                                                        "before the source call",
                            version_before=0, version_after=1,
                            operation_id=appr["operation_id"],
                            details={"idempotency_key": idempotency_key,
                                     "target": draft["destination_conv_id"]},
                            within_tx=True, origin=draft["origin"],
                            mock_label=draft["mock_label"])
        if inject_crash_after == "commit":
            raise InjectedFault("dispatch.commit", "process died after the operation row committed "
                                                   "and before the source call")
        outcome = adapter.dispatch(draft["account_id"], request)
        if inject_crash_after == "submission":
            # The adapter already returned (the source may hold a message); we die before
            # recording it. The ledger must therefore look uncertain, not successful.
            raise InjectedFault("dispatch.submission",
                                "process died after the source call and before the outcome was "
                                "recorded")
        return self.record_outcome(effect_id, outcome, actor=actor,
                                   note="dispatch", authorization_ref=approval_id)

    def _dispatch_request(self, appr: dict, draft: dict) -> dict:
        return {
            "idempotency_key": self._idempotency_key(appr, draft),
            "account_id": draft["account_id"],
            "target_conv_id": draft["destination_conv_id"],
            "target_provider_id": draft["destination_provider_id"],
            "mode": draft["mode"], "subject": draft["subject"],
            "recipient_snapshot": json.loads(draft["recipient_snapshot"]),
            "purpose": draft["purpose"], "content_hash": draft["content_hash"],
            "operation_id": appr["operation_id"],
            "application_label": ("MOCK dispatch request from a labelled mock scenario"
                                  if draft["origin"] == C.MOCK else "dispatch request"),
        }

    @staticmethod
    def _idempotency_key(appr: dict, draft: dict) -> str:
        # Source idempotency key where available: stable for this authorization and content,
        # so a retry of the same operation cannot become a second message (PRD §10).
        return "idem-" + C.sha256_hex({"operation_id": appr["operation_id"],
                                       "content_hash": draft["content_hash"]})[:40]

    def record_outcome(self, effect_id: str, outcome: C.Outcome, *, actor: str = "service",
                       note: str = "", authorization_ref: str | None = None) -> OpResult:
        """Persist what the source actually said; never invent a green state."""
        effect = self.store.one("SELECT * FROM effect_operation WHERE effect_id = ?", (effect_id,))
        if effect is None:
            return OpResult(C.NOT_FOUND, f"unknown effect {effect_id}")
        new_state, error_category, retry_allowed, requires_recon = self._classify(outcome, effect)
        phase = "post_submission" if outcome.submitted else "pre_submission"
        submitted = 1 if outcome.submitted else 0
        data = outcome.data or {}
        provider_message_id = data.get("provider_message_id") or effect["provider_message_id"]
        routed = data.get("actual_routed_destination") or effect["actual_routed_destination"]
        source_op = data.get("source_operation_id") or effect["source_operation_id"]
        now = C.now()
        with self.store.tx():
            attempt_no = int(self.store.scalar(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM effect_attempt WHERE effect_id = ?",
                (effect_id,)))
            self.store.insert_row("effect_attempt", {
                "effect_attempt_id": C.new_id("eat"), "effect_id": effect_id,
                "attempt_no": attempt_no, "phase": phase, "submitted": submitted,
                "started_at": now, "ended_at": now, "outcome_code": outcome.code,
                "error_category": error_category,
                "provider_message_id": data.get("provider_message_id"),
                "actual_routed_destination": data.get("actual_routed_destination"),
                "retry_allowed": 1 if retry_allowed else 0,
                "authorization_ref": authorization_ref or effect["approval_id"],
                "note": note or outcome.detail,
                "origin": effect["origin"], "mock_label": effect["mock_label"],
            })
            self.store.update_row("effect_operation", {
                "effect_state": new_state, "attempt_count": attempt_no,
                "provider_message_id": provider_message_id,
                "source_operation_id": source_op,
                "actual_routed_destination": routed,
                "provider_status": data.get("provider_status"),
                "requires_reconciliation": 1 if requires_recon else 0,
                "last_error_category": error_category, "updated_at": now,
            }, "effect_id = ?", (effect_id,))
            self.store.audit(actor=actor, operation=f"effect:{outcome.code}", entity_kind="effect",
                            entity_id=effect_id, reason=outcome.detail or note or new_state,
                            version_before=attempt_no - 1, version_after=attempt_no,
                            operation_id=effect["operation_id"],
                            details={"effect_state": new_state, "phase": phase,
                                     "error_category": error_category,
                                     "requires_reconciliation": requires_recon},
                            within_tx=True, origin=effect["origin"],
                            mock_label=effect["mock_label"])
        receipt_id = self._write_receipt(effect_id, outcome, new_state)
        self._settle_job(effect_id, new_state, outcome)
        result = self.effect_view(effect_id)
        return OpResult(C.OK,
                        f"dispatch outcome: {outcome.code} -> {new_state}",
                        data={"effect": result, "outcome": outcome.to_dict(),
                              "receipt_id": receipt_id},
                        operation_id=effect["operation_id"])

    def _classify(self, outcome: C.Outcome, effect: dict) -> tuple[str, str | None, bool, bool]:
        """(effect_state, error_category, retry_allowed, requires_reconciliation)"""
        if outcome.code == C.SUCCESS:
            status = (outcome.data or {}).get("provider_status")
            if status == "pending":
                return EffectState.PROVIDER_PENDING, None, False, True
            return EffectState.PROVIDER_ACCEPTED, None, False, True
        if outcome.code == C.OUTCOME_UNKNOWN:
            return EffectState.OUTCOME_UNKNOWN, C.ErrorCategory.UNKNOWN, False, True
        if outcome.code == C.PARTIAL:
            return EffectState.PROVIDER_PENDING, None, False, True
        if outcome.code == C.PERMISSION_DENIED:
            return EffectState.FAILED, C.ErrorCategory.PERMISSION_REVOKED, False, False
        if outcome.code == C.UNSUPPORTED:
            return EffectState.FAILED, C.ErrorCategory.UNSUPPORTED, False, False
        if outcome.code == C.PERMANENT_ERROR:
            return EffectState.FAILED, C.ErrorCategory.PERMANENT_CONTENT, False, False
        if outcome.code == C.RATE_LIMITED:
            return EffectState.FAILED, C.ErrorCategory.RATE_LIMITED, True, False
        if outcome.code in C.RETRYABLE_EFFECT_OUTCOMES:
            category = (C.ErrorCategory.RATE_LIMITED if outcome.code == C.RATE_LIMITED
                        else C.ErrorCategory.TRANSIENT_NETWORK)
            return EffectState.FAILED, category, True, False
        return EffectState.OUTCOME_UNKNOWN, C.ErrorCategory.UNKNOWN, False, True

    def _write_receipt(self, effect_id: str, outcome: C.Outcome, state: str) -> str | None:
        effect = self.store.one("SELECT * FROM effect_operation WHERE effect_id = ?", (effect_id,))
        data = outcome.data or {}
        levels = {
            EffectState.CONFIRMED_SENT: VerificationLevel.CONFIRMED_SENT,
            EffectState.PROVIDER_ACCEPTED: VerificationLevel.PROVIDER_ACCEPTED,
            EffectState.PROVIDER_PENDING: VerificationLevel.PROVIDER_PENDING,
            EffectState.OUTCOME_UNKNOWN: VerificationLevel.UNVERIFIED,
            EffectState.FAILED: VerificationLevel.NONE,
        }
        level = levels.get(state)
        if level is None:
            return None
        limitations = data.get("limitation") or {
            EffectState.PROVIDER_PENDING: "source returned a pending identifier; delivery is not yet "
                                          "confirmed and no delivery/read state is claimed",
            EffectState.PROVIDER_ACCEPTED: "source recorded the send locally; delivery confirmation "
                                           "is unavailable from this source",
            EffectState.OUTCOME_UNKNOWN: "outcome unknown: reconcile before retrying; review required",
            EffectState.FAILED: "no message was accepted by the source for this attempt",
            EffectState.CONFIRMED_SENT: "confirmed by source read-back at the identified destination",
        }.get(state)
        evidence = data.get("evidence") or [
            {"kind": "provider_response", "code": outcome.code,
             "provider_message_id": data.get("provider_message_id"),
             "detail": outcome.detail},
        ]
        receipt_id = C.new_id("rcpt")
        now = C.now()
        with self.store.tx():
            self.store.insert_row("receipt", {
                "receipt_id": receipt_id, "effect_id": effect_id, "result_id": None,
                "effect_state": state,
                "provider_message_id": data.get("provider_message_id") or effect["provider_message_id"],
                "source_operation_id": data.get("source_operation_id") or effect["source_operation_id"],
                "actual_routed_destination": data.get("actual_routed_destination")
                or effect["actual_routed_destination"],
                "verification_level": level,
                "delivery_state": "unknown" if level != VerificationLevel.CONFIRMED_SENT else "accepted",
                "read_state": "unknown",
                "verified_against_real_source": 0,
                "limitations": limitations,
                "evidence_json": C.canonical_json(evidence),
                "observed_at": now, "origin": effect["origin"], "mock_label": effect["mock_label"],
            })
        return receipt_id

    def _settle_job(self, effect_id: str, state: str, outcome: C.Outcome) -> None:
        effect = self.store.one("SELECT * FROM effect_operation WHERE effect_id = ?", (effect_id,))
        job_id = effect["job_id"]
        if not job_id:
            return
        job = self.store.one("SELECT * FROM job WHERE job_id = ?", (job_id,))
        if state == EffectState.CONFIRMED_SENT:
            self.ledger.transition(job_id, JobState.SUCCEEDED, actor="service",
                                   reason="outbound operation confirmed by the source",
                                   details={"effect_id": effect_id})
        elif state in (EffectState.PROVIDER_ACCEPTED, EffectState.PROVIDER_PENDING):
            self.ledger.transition(job_id, JobState.WAITING_FOR_SOURCE, actor="service",
                                   reason="send recorded by the source; delivery not yet confirmed "
                                          "— reconcile for a stronger receipt",
                                   details={"effect_id": effect_id, "state": state})
        elif state == EffectState.OUTCOME_UNKNOWN:
            self.ledger.transition(job_id, JobState.WAITING_FOR_SOURCE, actor="service",
                                   reason="dispatch outcome unknown; reconciliation required before "
                                          "any retry",
                                   details={"effect_id": effect_id, "uncertain": True})
            self._flag_review(effect["ws_conv_id"], "uncertain_effect",
                              "outbound outcome unknown — reconcile or review")
        elif state == EffectState.FAILED:
            if outcome.code in C.RETRYABLE_EFFECT_OUTCOMES:
                self.ledger.transition(job_id, JobState.WAITING_FOR_USER, actor="service",
                                       reason="send failed before submission; a retry under the same "
                                              "authorization or an edit is needed",
                                       details={"effect_id": effect_id})
                self._flag_review(effect["ws_conv_id"], "dispatch_failed_retryable",
                                  "send failed before submission (nothing was sent)")
            else:
                self.ledger.transition(job_id, JobState.FAILED, actor="service",
                                       reason=f"dispatch failed: {outcome.code}",
                                       details={"effect_id": effect_id})
                self._flag_review(effect["ws_conv_id"], "dispatch_failed",
                                  f"send failed: {outcome.code}")

    def _flag_review(self, ws_conv_id: str, reason: str, note: str) -> None:
        with self.store.tx():
            self.store.update_row("workspace_conversation", {
                "queue_state": QueueState.NEEDS_ME, "needs_me_reason": reason,
                "updated_at": C.now(),
            }, "ws_conv_id = ?", (ws_conv_id,))
        self.add_result(ws_conv_id=ws_conv_id, job_id=None, kind="failure", summary=note,
                        detail={"reason": reason})

    # -------------------------------------------------------- reconciliation --
    def reconcile(self, effect_id: str, *, actor: str = "service") -> OpResult:
        effect = self.store.one("SELECT * FROM effect_operation WHERE effect_id = ?", (effect_id,))
        if effect is None:
            return OpResult(C.NOT_FOUND, f"unknown effect {effect_id}")
        if effect["effect_state"] == EffectState.CONFIRMED_SENT:
            return OpResult(C.ALREADY_IN_STATE, "already confirmed sent",
                            data={"effect": self.effect_view(effect_id)})
        if effect["effect_state"] not in (EffectState.DISPATCHING, EffectState.PROVIDER_ACCEPTED,
                                          EffectState.PROVIDER_PENDING, EffectState.OUTCOME_UNKNOWN):
            return OpResult(C.ALREADY_IN_STATE,
                            f"nothing to reconcile from state {effect['effect_state']}",
                            data={"effect": self.effect_view(effect_id)})
        adapter = self.adapters.get(effect["adapter"])
        if adapter is None:
            return OpResult(C.NOT_FOUND, f"no adapter named {effect['adapter']}")
        outcome = adapter.reconcile(effect["account_id"], idempotency_key=effect["idempotency_key"],
                                    source_operation_id=effect["source_operation_id"])
        with self.store.tx():
            attempt_no = int(self.store.scalar(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM effect_attempt WHERE effect_id = ?",
                (effect_id,)))
            self.store.insert_row("effect_attempt", {
                "effect_attempt_id": C.new_id("eat"), "effect_id": effect_id,
                "attempt_no": attempt_no, "phase": "reconciliation", "submitted": 0,
                "started_at": C.now(), "ended_at": C.now(), "outcome_code": outcome.code,
                "error_category": None, "provider_message_id": (outcome.data or {}).get("provider_message_id"),
                "actual_routed_destination": None, "retry_allowed": 0,
                "authorization_ref": effect["approval_id"],
                "note": "reconciliation attempt",
                "origin": effect["origin"], "mock_label": effect["mock_label"],
            })
            self.store.audit(actor=actor, operation="effect_reconcile", entity_kind="effect",
                            entity_id=effect_id, reason=outcome.detail or outcome.code,
                            version_before=attempt_no - 1, version_after=attempt_no,
                            operation_id=effect["operation_id"],
                            details={"reconcile_code": outcome.code,
                                     "found": (outcome.data or {}).get("found")},
                            within_tx=True, origin=effect["origin"],
                            mock_label=effect["mock_label"])
        data = outcome.data or {}
        if outcome.code == C.SUCCESS and data.get("found"):
            now = C.now()
            with self.store.tx():
                self.store.update_row("effect_operation", {
                    "effect_state": EffectState.CONFIRMED_SENT, "requires_reconciliation": 0,
                    "provider_message_id": data.get("provider_message_id")
                    or effect["provider_message_id"],
                    "actual_routed_destination": data.get("actual_routed_destination")
                    or effect["actual_routed_destination"],
                    "provider_status": "confirmed", "updated_at": now,
                }, "effect_id = ?", (effect_id,))
            receipt_id = self._write_receipt(
                effect_id,
                C.Outcome(C.SUCCESS, data={"provider_message_id": data.get("provider_message_id")
                                           or effect["provider_message_id"],
                                           "note": "confirmed by reconciliation"},
                          provenance=outcome.provenance, label=outcome.label),
                EffectState.CONFIRMED_SENT)
            self._settle_job(effect_id, EffectState.CONFIRMED_SENT, outcome)
            return OpResult(C.OK, "reconciliation confirmed the send once",
                            data={"effect": self.effect_view(effect_id), "receipt_id": receipt_id})
        if outcome.code == C.SUCCESS and not data.get("found"):
            # A definitive "the source has no record of this" is not uncertainty: nothing was sent.
            now = C.now()
            with self.store.tx():
                self.store.update_row("effect_operation", {
                    "effect_state": EffectState.FAILED, "requires_reconciliation": 0,
                    "last_error_category": C.ErrorCategory.SOURCE_REJECTED, "updated_at": now,
                }, "effect_id = ?", (effect_id,))
            receipt_id = self._write_receipt(
                effect_id,
                C.Outcome(C.PERMANENT_ERROR,
                          data={"limitation": "reconciliation proved the source has no record of "
                                              "this send; a fresh approval and dispatch is required"},
                          detail="source has no record of the operation",
                          provenance=outcome.provenance, label=outcome.label),
                EffectState.FAILED)
            self._settle_job(effect_id, EffectState.FAILED,
                             C.Outcome(C.PERMANENT_ERROR, provenance=outcome.provenance))
            return OpResult(C.OK, "reconciliation proved nothing was sent",
                            data={"effect": self.effect_view(effect_id), "receipt_id": receipt_id})
        if outcome.code == C.PARTIAL:
            receipt_id = self._write_receipt(effect_id, outcome, effect["effect_state"])
            return OpResult(C.OK, "reconciliation partial: still not confirmed, not retried",
                            data={"effect": self.effect_view(effect_id), "receipt_id": receipt_id})
        # unsupported / outcome_unknown / anything else: reconcile is unavailable or inconclusive
        now = C.now()
        with self.store.tx():
            self.store.update_row("effect_operation", {
                "effect_state": EffectState.OUTCOME_UNKNOWN, "requires_reconciliation": 1,
                "updated_at": now,
            }, "effect_id = ?", (effect_id,))
        receipt_id = self._write_receipt(
            effect_id,
            C.Outcome(C.OUTCOME_UNKNOWN,
                      data={"limitation": outcome.detail or
                            "reconciliation is unavailable for this source",
                            "next_action": "review in Needs me; do not retry blindly"},
                      detail=outcome.detail, provenance=outcome.provenance, label=outcome.label),
            EffectState.OUTCOME_UNKNOWN)
        self._flag_review(effect["ws_conv_id"], "uncertain_effect",
                          "send state could not be reconciled — operator review required")
        return OpResult(C.OK, "reconciliation inconclusive: outcome unknown, retry refused",
                        data={"effect": self.effect_view(effect_id), "receipt_id": receipt_id})

    # ---------------------------------------------------------------- retry ---
    def retry(self, effect_id: str, *, actor: str = "owner") -> OpResult:
        """Retry only a *definite* pre-submission failure under the same authorization."""
        effect = self.store.one("SELECT * FROM effect_operation WHERE effect_id = ?", (effect_id,))
        if effect is None:
            return OpResult(C.NOT_FOUND, f"unknown effect {effect_id}")
        last = self.store.one(
            "SELECT * FROM effect_attempt WHERE effect_id = ? AND phase <> 'reconciliation' "
            "ORDER BY attempt_no DESC LIMIT 1", (effect_id,))
        if last is None:
            return OpResult(C.INVALID, "no dispatch attempt recorded for this effect")
        if last["submitted"] or effect["effect_state"] == EffectState.OUTCOME_UNKNOWN \
                or effect["requires_reconciliation"]:
            return OpResult(C.RETRY_REFUSED_UNCERTAIN,
                            "the source may already hold this message; reconcile instead of "
                            "retrying",
                            data={"effect": self.effect_view(effect_id),
                                  "next_action": "reconcile"})
        if effect["effect_state"] != EffectState.FAILED or last["outcome_code"] not in C.RETRYABLE_EFFECT_OUTCOMES:
            return OpResult(C.INVALID,
                            f"retry is not allowed from {effect['effect_state']} / "
                            f"{last['outcome_code']}",
                            data={"effect": self.effect_view(effect_id)})
        appr = self.store.one("SELECT * FROM approval WHERE approval_id = ?",
                              (effect["approval_id"],))
        draft = self.store.one("SELECT * FROM draft WHERE draft_id = ?",
                               (appr["draft_id"],)) if appr else None
        if appr is None or draft is None:
            return OpResult(C.DENIED, "the authorization for this operation is no longer available")
        if draft["version"] != appr["draft_version"] or draft["superseded_by"] \
                or draft["content_hash"] != appr["bound_content_hash"]:
            return OpResult(C.DENIED,
                            "the approved content changed; a new approval is required before any "
                            "retry",
                            data={"draft_version_key": f"{draft['draft_id']}@v{draft['version']}"})
        adapter = self.adapters.get(effect["adapter"])
        if adapter is None:
            return OpResult(C.NOT_FOUND, f"no adapter named {effect['adapter']}")
        with self.store.tx():
            self.store.update_row("effect_operation", {
                "effect_state": EffectState.DISPATCHING, "requires_reconciliation": 1,
                "updated_at": C.now(),
            }, "effect_id = ?", (effect_id,))
            self.store.audit(actor=actor, operation="effect_retry",
                            entity_kind="effect", entity_id=effect_id,
                            reason="definite pre-submission failure retried under the same "
                                   "authorization",
                            operation_id=effect["operation_id"],
                            details={"previous_outcome": last["outcome_code"],
                                     "authorization_ref": appr["approval_id"]},
                            within_tx=True, origin=effect["origin"],
                            mock_label=effect["mock_label"])
        outcome = adapter.dispatch(effect["account_id"], self._dispatch_request(appr, draft))
        result = self.record_outcome(effect_id, outcome, actor=actor,
                                    note="retry after pre-submission failure",
                                    authorization_ref=appr["approval_id"])
        result.detail = "retried under the same authorization: " + result.detail
        return result

    def cancel(self, effect_id: str, *, reason: str, actor: str = "owner") -> OpResult:
        effect = self.store.one("SELECT * FROM effect_operation WHERE effect_id = ?", (effect_id,))
        if effect is None:
            return OpResult(C.NOT_FOUND, f"unknown effect {effect_id}")
        if effect["effect_state"] in (EffectState.PREPARED, EffectState.APPROVED):
            with self.store.tx():
                self.store.update_row("effect_operation", {
                    "effect_state": EffectState.CANCELLED, "requires_reconciliation": 0,
                    "updated_at": C.now()}, "effect_id = ?", (effect_id,))
            return OpResult(C.OK, "operation cancelled before dispatch",
                            data={"effect": self.effect_view(effect_id)})
        if effect["effect_state"] == EffectState.CONFIRMED_SENT:
            return OpResult(C.INVALID, "already confirmed sent; a send cannot be recalled",
                            data={"effect": self.effect_view(effect_id)})
        now = C.now()
        with self.store.tx():
            self.store.update_row("effect_operation", {
                "effect_state": EffectState.OUTCOME_UNKNOWN, "requires_reconciliation": 1,
                "updated_at": now}, "effect_id = ?", (effect_id,))
        self._write_receipt(
            effect_id,
            C.Outcome(C.OUTCOME_UNKNOWN,
                      data={"limitation": "cancel requested after submission; the message may "
                                          "already exist — reconciliation required"},
                      provenance=effect["origin"], label=effect["mock_label"],
                      adapter=effect["adapter"]),
            EffectState.OUTCOME_UNKNOWN)
        self._flag_review(effect["ws_conv_id"], "uncertain_effect",
                          "cancel requested while the operation may already be submitted")
        return OpResult(C.OK, "cannot be stopped at this stage: marked outcome unknown and queued "
                              "for reconciliation",
                        data={"effect": self.effect_view(effect_id)})

    # ---------------------------------------------------------------- views ---
    def effect_view(self, effect_id: str) -> dict:
        row = self.store.one("SELECT * FROM effect_operation WHERE effect_id = ?", (effect_id,))
        if row is None:
            return {}
        return {
            "effect_id": row["effect_id"], "operation_id": row["operation_id"],
            "job_id": row["job_id"], "ws_conv_id": row["ws_conv_id"],
            "approval_id": row["approval_id"], "adapter": row["adapter"],
            "account_id": row["account_id"], "purpose": row["purpose"],
            "target": {"conv_id": row["target_conv_id"], "provider_id": row["target_provider_id"]},
            "actual_routed_destination": row["actual_routed_destination"],
            "effect_state": row["effect_state"], "provider_status": row["provider_status"],
            "provider_message_id": row["provider_message_id"],
            "source_operation_id": row["source_operation_id"],
            "idempotency_key": row["idempotency_key"], "request_hash": row["request_hash"],
            "attempt_count": row["attempt_count"],
            "requires_reconciliation": bool(row["requires_reconciliation"]),
            "last_error_category": row["last_error_category"],
            "attempts": self.store.all(
                "SELECT * FROM effect_attempt WHERE effect_id = ? ORDER BY attempt_no", (effect_id,)),
            "receipts": self.store.all(
                "SELECT * FROM receipt WHERE effect_id = ? ORDER BY observed_at", (effect_id,)),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "origin": row["origin"], "mock_label": row["mock_label"],
        }

    def add_result(self, *, ws_conv_id: str, job_id: str | None, kind: str, summary: str,
                   detail: dict | None = None, evidence: Optional[list] = None,
                   origin: str = C.REAL, mock_label: str | None = None) -> OpResult:
        with self.store.tx():
            version = int(self.store.scalar(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM result WHERE ws_conv_id = ? "
                "AND COALESCE(job_id,'') = COALESCE(?, '')", (ws_conv_id, job_id)) or 1)
            result_id = C.new_id("res")
            self.store.insert_row("result", {
                "result_id": result_id, "job_id": job_id, "ws_conv_id": ws_conv_id,
                "version": version, "kind": kind, "summary": summary,
                "detail_json": C.canonical_json(detail or {}),
                "evidence_json": C.canonical_json(evidence or []),
                "created_at": C.now(), "origin": origin, "mock_label": mock_label,
            })
        return OpResult(C.OK, "result recorded", data={"result_id": result_id, "version": version})
