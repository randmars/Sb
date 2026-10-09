"""Ingestion: deterministic fixture seeding, at-least-once events, idempotent projection.

Implements PRD §6 (common source contract, ingest at least once, idempotent
projection, cursors committed with their updates, honest coverage) and the parts of
§4/§12 that decide how a message becomes a person/group-associated conversation
without merging audiences.

Everything in this module operates on data returned by the labelled mock adapters.
The projection function does not care whether its input came from a mock or a real
adapter; it only requires the input to be labelled, which ``_assert_source_valued``
enforces before anything is written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from . import contracts as C
from .adapters import SourceAdapter
from .contracts import JobState, OpResult, QueueState, ReviewState
from .ledger import Ledger
from .rules import Rules
from .store import Store

UNTRIAGED = "untriaged_message"


class Ingest:
    def __init__(self, store: Store, ledger: Ledger, adapters: dict[str, SourceAdapter],
                 rules: Optional[Rules] = None):
        self.store = store
        self.ledger = ledger
        self.adapters = adapters
        self.rules = rules

    # ------------------------------------------------------------- guarding ----
    @staticmethod
    def _assert_source_valued(record: dict, where: str) -> None:
        """A record about a source must declare where it came from, and a mock must
        say so. This is the gate that keeps unlabelled mock data out of the store."""
        origin = record.get("origin")
        if origin not in (C.MOCK, C.REAL):
            raise AssertionError(f"{where}: source record must declare origin mock|real")
        C.assert_labelled(origin, record.get("mock_label"), where)

    # --------------------------------------------------------------- seeding ---
    def seed(self, corpus: dict, *, actor: str = "seed") -> OpResult:
        """Load the deterministic fixture corpus. Idempotent: re-running changes nothing."""
        created = {"accounts": 0, "capabilities": 0, "people": 0, "links": 0, "groups": 0,
                   "conversations": 0, "messages": 0, "attachments": 0, "checkpoints": 0}
        for acct in corpus["accounts"]:
            self._assert_source_valued(acct, "seed.account")
            adapter = self.adapters.get(acct["adapter"])
            with self.store.tx():
                self.store.upsert_row("source_account", {
                    "account_id": acct["account_id"], "adapter": acct["adapter"],
                    "adapter_version": acct["adapter_version"],
                    "account_identity": acct["account_identity"],
                    "host_role": acct["host_role"], "display_name": acct["display_name"],
                    "enabled_operations": C.canonical_json(acct["enabled_operations"]),
                    "health_state": acct["health_state"], "health_detail": acct.get("health_detail"),
                    "permission_state": acct["permission_state"],
                    "last_success_at": acct["last_success_at"], "last_probe_at": acct["last_probe_at"],
                    "origin": acct["origin"], "mock_label": acct["mock_label"],
                }, ["account_id"])
            created["accounts"] += 1
            if adapter is not None:
                manifest = adapter.manifest()
                for name, entry in manifest.capabilities.items():
                    with self.store.tx():
                        self.store.upsert_row("capability", {
                            "account_id": acct["account_id"], "name": name,
                            "supported": 1 if entry.supported else 0, "state": entry.state,
                            "limitation": entry.limitation, "probe_method": entry.probe_method,
                            "observed_at": C.now(), "origin": acct["origin"],
                            "mock_label": acct["mock_label"],
                        }, ["account_id", "name"])
                    created["capabilities"] += 1
        for person in corpus["people"]:
            with self.store.tx():
                self.store.upsert_row("person", {
                    "person_id": person["person_id"], "display_name": person["display_name"],
                    "notes": None, "user_override": 0, "created_at": C.now(),
                    "origin": C.MOCK, "mock_label": C.mock_label("fixtures"),
                }, ["person_id"])
            created["people"] += 1
        for link in corpus["identity_links"]:
            source = corpus["accounts"]
            acct = next((a for a in source if a["adapter"] == link["adapter"]), None)
            with self.store.tx():
                self.store.upsert_row("identity_link", {
                    "link_id": C.stable_id("ilink", link["adapter"], link["normalized_value"]),
                    "person_id": link["person_id"], "kind": link["kind"],
                    "normalized_value": link["normalized_value"].lower(), "adapter": link["adapter"],
                    "account_id": acct["account_id"] if acct else None,
                    "source_namespaced_id": f"{link['adapter']}:{link['normalized_value']}",
                    "evidence": link["evidence"], "confidence": link["confidence"],
                    "actor": "identity_resolver", "evidence_time": C.now(),
                    "user_override": link["user_override"], "state": link["state"],
                    "reversible": 1, "superseded_by": None, "created_at": C.now(),
                    "revoked_at": None, "revoke_reason": None,
                    "origin": C.MOCK, "mock_label": C.mock_label("fixtures"),
                }, ["link_id"])
            created["links"] += 1
        for group in corpus["groups"]:
            with self.store.tx():
                self.store.upsert_row("grp", {
                    "group_id": group["group_id"],
                    "stable_source_identity": group["stable_source_identity"],
                    "account_id": group["account_id"], "title": group["title"],
                    "membership_version": group["membership_version"],
                    "member_identity_json": C.canonical_json(group["member_identity_json"]),
                    "audience_json": C.canonical_json(group["audience_json"]),
                    "user_created": group["user_created"], "created_at": C.now(),
                    "origin": C.MOCK, "mock_label": C.mock_label("fixtures"),
                }, ["group_id"])
            created["groups"] += 1
        for conv in corpus["conversations"]:
            self._assert_source_valued(conv, "seed.conversation")
            self.project_conversation(conv, actor=actor)
            created["conversations"] += 1
        for msg in corpus["messages"]:
            self._assert_source_valued(msg, "seed.message")
            self.ledger.record_event(
                account_id=msg["account_id"], event_kind="message_upsert",
                dedup_key=self.event_key(msg["namespaced_id"], msg.get("revision", "1")),
                trigger_hash=C.text_hash(msg["namespaced_id"] + str(msg.get("revision"))),
                project=lambda conn, _ctx: self.project_message(msg, actor=actor),
                origin=msg["origin"], mock_label=msg["mock_label"])
            created["messages"] += 1
        for att in corpus["attachments"]:
            with self.store.tx():
                self.store.upsert_row("attachment_ref", {
                    "attachment_ref_id": att["attachment_ref_id"], "draft_id": None,
                    "job_id": None, "msg_ref_id": att["msg_ref_id"],
                    "namespaced_id": att["namespaced_id"], "filename": att["filename"],
                    "media_type": att["media_type"], "size_bytes": att.get("size_bytes"),
                    "content_hash": att.get("content_hash"),
                    "download_state": att["download_state"],
                    "availability": "available" if att["available"] else "unavailable",
                    "limitation": att.get("limitation"), "quarantine_state": "none",
                    "created_at": C.now(), "origin": C.MOCK,
                    "mock_label": C.mock_label("fixtures"),
                }, ["attachment_ref_id"])
            created["attachments"] += 1
        for acct in corpus["accounts"]:
            if acct["adapter"] not in ("mock_mail", "mock_beeper"):
                continue
            convs = [c for c in corpus["conversations"] if c["account_id"] == acct["account_id"]]
            for conv in convs:
                res = self.ledger.set_checkpoint(
                    account_id=acct["account_id"], scope="mailbox", scope_ref=conv["conv_id"],
                    cursor="mock-checkpoint:0",
                    coverage={"observed_count": len([
                        m for m in corpus["messages"] if m["conv_id"] == conv["conv_id"]]),
                        "missing_bodies": len([m for m in corpus["messages"]
                                               if m["conv_id"] == conv["conv_id"]
                                               and m["body_state"] not in ("fetched",)]),
                        "note": "MOCK fixture checkpoint"},
                    coverage_state="fixture_scan",
                    overlap_state="overlap=1 revision (MOCK)",
                    gap_reason=None,
                    oldest_observed_time=conv["source_time_first"],
                    origin=C.MOCK, mock_label=C.mock_label("fixtures"))
                created["checkpoints"] += 1
        return OpResult(C.OK, "fixtures seeded (deterministic, labelled MOCK)",
                        data={"created": created,
                              "counts": self.ledger.counts()}, mocked=False)

    @staticmethod
    def event_key(namespaced_id: str, revision: str) -> str:
        return f"evt:{namespaced_id}:{revision}"

    # ------------------------------------------------------------ projection ---
    def project_conversation(self, conv: dict, *, actor: str = "ingest") -> dict:
        existing = self.store.one("SELECT * FROM source_conversation WHERE namespaced_id = ?",
                                  (conv["namespaced_id"],))
        row = {
            "conv_id": existing["conv_id"] if existing else conv["conv_id"],
            "account_id": conv["account_id"], "adapter": conv["adapter"],
            "namespaced_id": conv["namespaced_id"],
            "provider_thread_id": conv.get("provider_thread_id"),
            "provider_chat_id": conv.get("provider_chat_id"),
            "audience_kind": conv["audience_kind"],
            "audience_json": C.canonical_json(conv.get("audience", [])),
            "provider_is_merged": 1 if conv.get("provider_is_merged") else 0,
            "revision": conv.get("revision"),
            "availability": conv["availability"],
            "availability_reason": conv.get("availability_reason"),
            "retrieval_pointer": conv.get("retrieval_pointer"),
            "minimal_metadata": C.canonical_json(conv.get("minimal_metadata", {})),
            "source_time_first": conv.get("source_time_first"),
            "source_time_last": conv.get("source_time_last"),
            "ingested_at": C.now(),
            "origin": conv["origin"], "mock_label": conv["mock_label"],
        }
        with self.store.tx():
            if existing:
                self.store.update_row("source_conversation", row, "conv_id = ?",
                                      (row["conv_id"],))
            else:
                self.store.insert_row("source_conversation", row)
        return {"conv_id": row["conv_id"], "created": existing is None}

    def project_message(self, msg: dict, *, actor: str = "ingest") -> dict:
        """Idempotent projection of one source message.

        On replay the *source* fields are refreshed and the independent application
        states (read/hidden/mute) are left exactly as the owner left them (PRD §12).
        """
        conv = self.store.one("SELECT * FROM source_conversation WHERE namespaced_id = ?",
                              (f"{msg['adapter']}:{msg['account_id']}:"
                               + (msg["conv_provider_id"] if msg.get("conv_provider_id") else ""),))
        if conv is None:
            conv = self.store.one("SELECT * FROM source_conversation WHERE conv_id = ?",
                                  (msg["conv_id"],))
        if conv is None:
            return {"projected": False, "reason": "conversation not projected yet"}
        existing = self.store.one("SELECT * FROM message_ref WHERE namespaced_id = ?",
                                  (msg["namespaced_id"],))
        read_state = existing["read_state"] if existing else (
            "unread" if (msg.get("minimal_metadata") or {}).get("unread") else "unread")
        hidden = existing["hidden_state"] if existing else "visible"
        mute = existing["mute_state"] if existing else "unmuted"
        row = {
            "msg_ref_id": existing["msg_ref_id"] if existing else msg["msg_ref_id"],
            "conv_id": conv["conv_id"], "account_id": msg["account_id"],
            "namespaced_id": msg["namespaced_id"],
            "provider_message_id": msg["provider_message_id"],
            "reply_headers_json": C.canonical_json(msg.get("reply_headers", {})),
            "sender_json": C.canonical_json(msg["sender"]),
            "recipients_json": C.canonical_json(msg.get("recipients", [])),
            "cc_json": C.canonical_json(msg.get("cc", [])),
            "bcc_json": C.canonical_json(msg.get("bcc", [])),
            "audience_kind": conv["audience_kind"],
            "source_time": msg["source_time"], "ingested_at": C.now(),
            "revision": msg.get("revision", "1"), "availability": msg["availability"],
            "availability_reason": msg.get("availability_reason"),
            "body_state": msg["body_state"], "retrieval_pointer": msg.get("retrieval_pointer"),
            "minimal_metadata": C.canonical_json(msg.get("minimal_metadata", {})),
            "read_state": read_state, "hidden_state": hidden, "mute_state": mute,
            "deleted_at_source": 1 if msg.get("deleted_at_source") else 0,
            "origin": msg["origin"], "mock_label": msg["mock_label"],
        }
        with self.store.tx():
            if existing:
                self.store.update_row("message_ref", row, "msg_ref_id = ?", (row["msg_ref_id"],))
            else:
                self.store.insert_row("message_ref", row)
        association = self._associate(row, msg, actor=actor)
        return {"projected": True, "msg_ref_id": row["msg_ref_id"],
                "replayed": existing is not None, **association}

    def _associate(self, msg_row: dict, msg: dict, *, actor: str) -> dict:
        """Link the message to a person (or a group) and to a host workspace conversation."""
        sender = msg["sender"]
        value = (sender.get("address") or sender.get("network_identity") or "").lower()
        display = sender.get("display_name") or value or "Unknown sender"
        person_id = None
        if value:
            link = self.store.one(
                "SELECT * FROM identity_link WHERE normalized_value = ? AND state IN "
                "('confirmed','proposed') ORDER BY CASE state WHEN 'confirmed' THEN 0 ELSE 1 END, "
                "confidence DESC", (value,))
            if link is not None:
                person_id = link["person_id"]
        if person_id is None and value:
            # Unknown senders stay readable and actionable without a forced contact merge.
            person_id = C.stable_id("person", "observed", value)
            with self.store.tx():
                self.store.upsert_row("person", {
                    "person_id": person_id, "display_name": display, "notes": None,
                    "user_override": 0, "created_at": C.now(),
                    "origin": msg_row["origin"], "mock_label": msg_row["mock_label"],
                }, ["person_id"])
                self.store.upsert_row("identity_link", {
                    "link_id": C.stable_id("ilink", msg_row["account_id"], value),
                    "person_id": person_id, "kind": ("network_identity" if "@" not in value
                                                     or value.startswith("@") else "email"),
                    "normalized_value": value, "adapter": msg["adapter"],
                    "account_id": msg_row["account_id"],
                    "source_namespaced_id": f"{msg['adapter']}:{value}",
                    "evidence": "observed_sender_address", "confidence": 0.5,
                    "actor": actor, "evidence_time": C.now(), "user_override": 0,
                    "state": "proposed", "reversible": 1, "superseded_by": None,
                    "created_at": C.now(), "revoked_at": None, "revoke_reason": None,
                    "origin": msg_row["origin"], "mock_label": msg_row["mock_label"],
                }, ["link_id"])
        conv = self.store.one("SELECT * FROM source_conversation WHERE conv_id = ?",
                              (msg_row["conv_id"],))
        # Primary association follows the audience: a group chat belongs to the group view,
        # never silently to a private person view (PRD §4).
        if conv["audience_kind"] == "group":
            group = self.store.one(
                "SELECT * FROM grp WHERE stable_source_identity = ? AND account_id = ?",
                (conv["provider_chat_id"], conv["account_id"]))
            if group is None:
                group_id = C.stable_id("group", conv["account_id"], conv["provider_chat_id"])
                with self.store.tx():
                    self.store.insert_row("grp", {
                        "group_id": group_id,
                        "stable_source_identity": conv["provider_chat_id"],
                        "account_id": conv["account_id"], "title": "Group conversation",
                        "membership_version": "1",
                        "member_identity_json": "[]", "audience_json": conv["audience_json"],
                        "user_created": 0, "created_at": C.now(),
                        "origin": msg_row["origin"], "mock_label": msg_row["mock_label"],
                    })
            else:
                group_id = group["group_id"]
            ws_primary = self._ensure_workspace("group", group_id, group["title"] if group
                                                else "Group conversation", msg_row)
            ws_related = self._ensure_workspace("person", person_id, display, msg_row)
        else:
            ws_primary = self._ensure_workspace("person", person_id, display, msg_row)
            ws_related = None
        with self.store.tx():
            self.store.upsert_row("workspace_source_link", {
                "ws_conv_id": ws_primary, "conv_id": conv["conv_id"],
                "relevance": "primary", "added_at": C.now(),
            }, ["ws_conv_id", "conv_id"])
            if ws_related:
                self.store.upsert_row("workspace_source_link", {
                    "ws_conv_id": ws_related, "conv_id": conv["conv_id"],
                    "relevance": "related_context", "added_at": C.now(),
                }, ["ws_conv_id", "conv_id"])
        unread = bool((msg.get("minimal_metadata") or {}).get("unread")) and not \
            msg.get("deleted_at_source")
        if unread:
            self._mark_needs_me(ws_primary, UNTRIAGED)
        return {"person_id": person_id, "ws_conv_id": ws_primary,
                "ws_conv_related": ws_related, "needs_me": unread,
                "source_time": msg_row["source_time"], "ingested_at": msg_row["ingested_at"]}

    def _ensure_workspace(self, kind: str, association_id: str, title: str,
                          msg_row: dict) -> str:
        ws_conv_id = C.stable_id("ws", kind, association_id)
        existing = self.store.one("SELECT ws_conv_id FROM workspace_conversation WHERE ws_conv_id = ?",
                                  (ws_conv_id,))
        if existing is None:
            now = C.now()
            with self.store.tx():
                self.store.insert_row("workspace_conversation", {
                    "ws_conv_id": ws_conv_id, "association_kind": kind,
                    "association_id": association_id, "title": title,
                    "queue_state": QueueState.IDLE, "assignment_state": "unassigned",
                    "review_state": ReviewState.NONE, "current_input_revision": 0,
                    "needs_me_reason": None, "created_at": now, "updated_at": now,
                    "origin": msg_row["origin"], "mock_label": msg_row["mock_label"],
                })
        return ws_conv_id

    def _mark_needs_me(self, ws_conv_id: str, reason: str) -> None:
        with self.store.tx():
            self.store.update_row("workspace_conversation", {
                "queue_state": QueueState.NEEDS_ME, "needs_me_reason": reason,
                "updated_at": C.now(),
            }, "ws_conv_id = ?", (ws_conv_id,))

    # ------------------------------------------------------------------ sync ---
    def sync(self, adapter_name: str, *, account_id: str, scope_ref: str | None = None,
             limit: int = 50, apply_future_rules: bool = True) -> OpResult:
        """Resumable poll: events -> authoritative retrieval -> idempotent projection."""
        adapter = self.adapters.get(adapter_name)
        if adapter is None:
            return OpResult(C.NOT_FOUND, f"unknown adapter {adapter_name}")
        scope_ref = scope_ref or account_id
        checkpoint = self.store.one(
            "SELECT * FROM sync_checkpoint WHERE account_id = ? AND scope_ref = ? "
            "AND scope = 'mailbox'", (account_id, scope_ref))
        cursor = checkpoint["cursor_value"] if checkpoint else None
        poll = adapter.history_poll(account_id, scope_ref, cursor=cursor, limit=limit)
        payload = poll.to_dict()
        if not poll.usable:
            # An honest typed failure; nothing is projected and the cursor does not move.
            return OpResult(C.OK if poll.code != C.PERMANENT_ERROR else C.INVALID,
                            f"source not read: {poll.code} ({poll.detail})",
                            data={"outcome": payload, "projected": 0,
                                  "cursor_advanced": False}, mocked=poll.mocked,
                            label=poll.label)
        events = (poll.data or {}).get("events", [])
        projected, duplicates, unreachable = 0, 0, []
        highest_cursor = cursor
        for event in events:
            ref = event["namespaced_id"]
            got = adapter.retrieve(account_id, ref)
            if not got.usable:
                unreachable.append({"namespaced_id": ref, "code": got.code, "detail": got.detail})
                continue
            data = got.data or {}
            if not data.get("message"):
                unreachable.append({"namespaced_id": ref, "code": got.code,
                                    "detail": got.detail or "no retrievable message"})
                continue
            message = self._merge_event_with_source(event, data["message"])
            res = self.ledger.record_event(
                account_id=account_id, event_kind=event["kind"],
                dedup_key=self.event_key(ref, event.get("revision", "1")),
                trigger_hash=C.text_hash(C.canonical_json(event)),
                project=lambda conn, _ctx: self.project_message(message, actor="ingest"),
                origin=event.get("origin", C.REAL), mock_label=event.get("mock_label"))
            if res.code == C.IDEMPOTENT_REPLAY:
                duplicates += 1
            else:
                projected += 1
                highest_cursor = (poll.data or {}).get("next_cursor")
                if apply_future_rules and self.rules is not None:
                    row = self.store.one("SELECT * FROM message_ref WHERE namespaced_id = ?", (ref,))
                    if row is not None:
                        self.rules.evaluate_arrival(row)
        if poll.code == C.SUCCESS and (poll.data or {}).get("next_cursor"):
            highest_cursor = (poll.data or {}).get("next_cursor")
        self.ledger.set_checkpoint(
            account_id=account_id, scope="mailbox", scope_ref=scope_ref,
            cursor=highest_cursor,
            coverage={"observed_count": len(events), "projected": projected,
                      "duplicates": duplicates,
                      "unreachable": unreachable,
                      "note": (poll.data or {}).get("coverage", {}).get("note")},
            coverage_state=("partial_history" if poll.code == C.PARTIAL else "verified_scan"),
            overlap_state=(poll.data or {}).get("overlap_window"),
            gap_reason=poll.detail if poll.code == C.PARTIAL else None,
            origin=C.MOCK if poll.mocked else C.REAL, mock_label=poll.label)
        return OpResult(C.OK,
                        f"polled {len(events)} event(s): {projected} projected, "
                        f"{duplicates} duplicate(s) ignored",
                        data={"outcome": payload, "projected": projected, "duplicates": duplicates,
                              "unreachable": unreachable,
                              "cursor": highest_cursor,
                              "cursor_advanced": highest_cursor != cursor},
                        mocked=poll.mocked, label=poll.label)

    @staticmethod
    def _merge_event_with_source(event: dict, message: dict) -> dict:
        """The event is a trigger; the authoritative data is the retrieved message."""
        merged = dict(message)
        merged.setdefault("adapter", event["origin"] and message.get("adapter"))
        merged["conv_provider_id"] = None
        merged["revision"] = event.get("revision", message.get("revision", "1"))
        return merged

    # ---------------------------------------------------------- observations ---
    #: A source counts as healthy only when its own adapter says so in these terms. Any
    #: other typed outcome (offline, permission_denied, unsupported, ...) is a condition
    #: the owner must be able to see.
    #:
    #: There is ONE definition of "healthy/current" for a source, and it is the next two
    #: constants. The web layer used to keep its own narrower copy
    #: (``{"connected", "syncing", "current"}``) while this module used a wider one that
    #: also admitted the raw probe codes ``success``/``ok``; a source the ingest layer read
    #: as healthy could then be reported to the owner as disconnected. The web layer now
    #: imports ``HEALTHY_SOURCE_STATES`` from here instead of restating it.
    HEALTHY_SOURCE_STATES = ("connected", "syncing", "current")
    #: The same fact expressed as raw adapter outcome codes (what ``observe_sources``
    #: returns and what an adapter's ``health()`` can answer).
    HEALTHY_OBSERVED_STATES = ("success", "ok") + HEALTHY_SOURCE_STATES

    #: Coverage states that mean "this ledger view is as complete as the source allowed".
    #: ``complete`` is the state ``_coverage_axis`` itself produces for a fully scanned
    #: scope and the one ``AXIS_HEALTHY["coverage"]`` treats as green; leaving it out made a
    #: complete coverage declaration score as ``partial_history`` (the mis-scoring this
    #: list was fixed for), because the guard is "not in COMPLETE_COVERAGE_STATES".
    COMPLETE_COVERAGE_STATES = ("complete", "current", "fixture", "fixture_scan",
                                "verified_scan")

    # ---------------------------------------------------------------------------
    # Source health is three separate axes, not one blended value. Transport asks "can
    # this source be reached at all?", freshness asks "when was it last observed?", and
    # coverage asks "is this history complete, and if not, why not?" (PRD §6 "Health
    # presentation", §13, R09, T09/T10/T20). Each axis reports its own typed state, its
    # own reason and its own basis, and an axis whose value is unknown says ``unknown``
    # rather than borrowing the good news from another axis. The state last written to
    # the ledger stays a separate, labelled axis of its own (see ``health_state_basis``).
    # ---------------------------------------------------------------------------
    AXES = ("transport", "freshness", "coverage")

    #: The probe outcome -> the transport axis state.
    TRANSPORT_STATES = {
        "success": "connected", "ok": "connected", "current": "connected",
        "connected": "connected", "syncing": "connected", "partial": "partial",
        "unsupported": "unsupported", "permission_denied": "permission_denied",
        "offline": "offline", "rate_limited": "rate_limited",
        "retryable_error": "error", "permanent_error": "error",
        "outcome_unknown": "outcome_unknown", "token_reset": "error",
    }
    TRANSPORT_SEVERITY = {
        "connected": "ok", "partial": "warn", "unsupported": "warn",
        "rate_limited": "warn", "outcome_unknown": "warn",
        "permission_denied": "danger", "offline": "danger", "error": "danger",
        "unknown": "unknown",
    }
    #: The states that make each axis positively healthy. Nothing else is ever green, and
    #: `unknown` is not in any of these lists.
    AXIS_HEALTHY = {
        "transport": ("connected",),
        "freshness": ("observed_now",),
        "coverage": ("complete",),
    }

    @classmethod
    def _axis(cls, name: str, state: str, reason: str, basis: str, *, severity: str,
              **extra: Any) -> dict:
        axis = {
            "axis": name,
            "state": state,
            "severity": severity,
            "green": state in cls.AXIS_HEALTHY[name],
            "reason": reason,
            "basis": basis,
        }
        axis.update(extra)
        return axis

    def _transport_axis(self, sample: dict, account: dict) -> dict:
        """Can the source be reached at all? Answered by this run's typed probe outcome."""
        code = (sample or {}).get("code")
        state = self.TRANSPORT_STATES.get(code or "", "unknown")
        basis = ("observed just now: the adapter's own health probe of this run "
                 f"(outcome {code!r})" if code else
                 "unknown: no health probe ran for this account in this deployment")
        if state == "unknown":
            reason = (account.get("health_detail") or
                      "the source's reachability was not observed in this run, so it is "
                      "reported as unknown rather than assumed healthy")
        else:
            reason = (sample.get("detail") or
                      f"the adapter answered {code!r} for this account")
        return self._axis("transport", state, reason, basis,
                          severity=self.TRANSPORT_SEVERITY[state],
                          probe_outcome=code, probe_at=sample.get("observed_at"),
                          stored_health_state=account.get("health_state"),
                          stored_health_detail=account.get("health_detail"),
                          permission_state=account.get("permission_state"))

    def _freshness_axis(self, sample: dict, account: dict) -> dict:
        """When was this source last observed? A missing observation is unknown, not fresh."""
        last = account.get("last_success_at")
        probed_ok = bool(sample) and sample.get("code") in self.HEALTHY_OBSERVED_STATES
        if not last:
            state, severity = "unknown", "unknown"
            reason = ("no successful read of this account has ever been recorded in this "
                      "deployment, so its freshness is unknown"
                      + ("; this run's health probe did answer, but that is not a read "
                         "observation" if probed_ok else ""))
        elif probed_ok:
            state, severity = "observed_now", "ok"
            reason = (f"last observed successfully at {last}, and the source answered this "
                      "run's health probe")
        else:
            state, severity = "observed", "warn"
            reason = (f"last observed successfully at {last}; the source did not confirm a "
                      "healthy answer to this run's probe, so this is a recorded past "
                      "observation rather than a current one")
        seconds = None
        if last:
            seconds = max(0, int((C.now_dt() - C.parse_iso(last)).total_seconds()))
        return self._axis("freshness", state, reason,
                          "the account's own last_success_at plus this run's probe",
                          severity=severity, last_success_at=last, last_probe_at=account.get("last_probe_at"),
                          age_seconds=seconds, probe_observed=bool(sample))

    def _coverage_axis(self, account: dict, checkpoints: list[dict], declared: dict,
                       sample: dict) -> dict:
        """Is the history read complete, partial or unproven? Never 'complete' by default."""
        declared_state = declared.get("coverage_state")
        gap_reason = declared.get("gap_reason")
        partial = [c for c in checkpoints
                   if c.get("coverage_state") and c["coverage_state"] not in self.COMPLETE_COVERAGE_STATES]
        if not gap_reason and partial:
            gap_reason = partial[0].get("gap_reason") or (
                f"the checkpoint records coverage_state={partial[0].get('coverage_state')!r}")
        scopes = [f"{c.get('scope')}:{c.get('scope_ref')}" for c in checkpoints]
        if declared_state and declared_state not in self.COMPLETE_COVERAGE_STATES:
            state, severity = "partial_history", "warn"
            reason = ("the adapter declares this account's coverage to be partial: "
                      + (gap_reason or declared_state))
        elif partial:
            state, severity = "partial_history", "warn"
            reason = ("a recorded checkpoint shows this account's history is partial: "
                      + (gap_reason or "see the checkpoint's gap reason"))
        elif checkpoints:
            state, severity = "complete", "ok"
            reason = ("every recorded checkpoint for this account reports a complete bounded "
                      f"scan of its scope ({', '.join(scopes) or 'no scope recorded'})")
        else:
            state, severity = "unknown", "unknown"
            reason = ("no sync checkpoint has ever been recorded for this account, so the "
                      "completeness of its history is unproven — partial history is not an "
                      "empty result (PRD §6)")
            gap_reason = gap_reason or reason
        return self._axis("coverage", state, reason,
                          "this deployment's own sync checkpoints for the account"
                          + (" plus the adapter's coverage declaration" if declared else ""),
                          severity=severity, gap_reason=gap_reason, scopes=scopes,
                          checkpoint_count=len(checkpoints),
                          declared_coverage_state=declared_state,
                          declared_limitation=declared.get("limitation"))

    def health_axes(self, account: dict, sample: dict, checkpoints: list[dict]) -> dict:
        """The three axes for one account, computed from three different sources of truth."""
        declared = self._coverage_declaration(account)
        return {
            "transport": self._transport_axis(sample, account),
            "freshness": self._freshness_axis(sample, account),
            "coverage": self._coverage_axis(account, checkpoints, declared, sample),
        }

    def observe_sources(self) -> dict[str, dict]:
        """Ask every account's adapter what its state is *now*, keyed by account id.

        The stored ``source_account`` row records what was last written (a fixture load or
        an earlier probe); it cannot know that a source has since gone offline, that the
        Mini app closed or that a permission was revoked. Health presentation is therefore
        built from the adapter's own typed outcome, so a source that cannot answer is shown
        as that failure with its reason instead of staying green (PRD §6 "Health
        presentation"; §13 "a source that has stopped syncing should not remain green
        indefinitely"; R09, R15; T10, T20).

        This is a health probe only: nothing is read, no cursor moves and no stored row is
        written. A probe that raises is reported as ``unknown`` rather than breaking a read
        surface — an unobserved source is not a healthy one.
        """
        observed: dict[str, dict] = {}
        rows = self.store.all("SELECT account_id, adapter FROM source_account "
                              "ORDER BY adapter, account_id")
        for row in rows:
            base = {"account_id": row["account_id"], "adapter": row["adapter"],
                    "observed_at": C.now()}
            adapter = self.adapters.get(row["adapter"])
            if adapter is None or not hasattr(adapter, "health"):
                observed[row["account_id"]] = {
                    **base, "code": "unknown", "data": None, "mocked": True,
                    "label": C.mock_label(row["adapter"]),
                    "detail": (f"adapter {row['adapter']!r} declares no health operation, so this "
                               "account's state could not be observed")}
                continue
            try:
                outcome = adapter.health(row["account_id"])
                payload = outcome.to_dict()
                observed[row["account_id"]] = {
                    **base, "code": outcome.code, "detail": outcome.detail or "",
                    "mocked": bool(outcome.mocked), "label": payload.get("mock_label"),
                    "data": outcome.data if outcome.usable else None}
            except Exception as exc:  # noqa: BLE001 - a probe must never break a surface
                observed[row["account_id"]] = {
                    **base, "code": "unknown", "data": None, "mocked": True,
                    "label": C.mock_label(row["adapter"]),
                    "detail": (f"health probe failed: {exc.__class__.__name__}: {exc}")}
        return observed

    def _coverage_declaration(self, account: dict) -> dict:
        """The adapter's own statement about coverage for one account (may be empty).

        Optional by design: an adapter that has nothing to say about coverage answers
        ``unsupported``, and one that does not implement the operation leaves the recorded
        checkpoints to speak for themselves.
        """
        adapter = self.adapters.get(account["adapter"])
        if adapter is None or not hasattr(adapter, "coverage_declaration"):
            return {}
        try:
            outcome = adapter.coverage_declaration(account["account_id"])
        except Exception:  # noqa: BLE001 - a declaration must never break a read surface
            return {}
        if not outcome.usable or not outcome.data:
            return {}
        return dict(outcome.data)

    # -------------------------------------------------------------- coverage ---
    def coverage(self, account_id: str | None = None) -> list[dict]:
        observed = self.observe_sources()
        sql = ("SELECT c.*, a.display_name, a.adapter, a.health_state, a.permission_state, "
               "a.last_success_at AS account_last_success FROM sync_checkpoint c "
               "JOIN source_account a ON a.account_id = c.account_id")
        args: tuple = ()
        if account_id:
            sql += " WHERE c.account_id = ?"
            args = (account_id,)
        rows = self.store.all(sql + " ORDER BY c.account_id, c.scope_ref", args)
        for row in rows:
            row["coverage"] = json.loads(row["coverage_json"])
            row["note"] = (row["coverage"] or {}).get("note")
            row["mock"] = row["origin"] == C.MOCK
            row["coverage_disclosure"] = (
                "MOCK: coverage describes fixture data only. A completed pagination run proves "
                "only that the adapter reached the end of that query's accessible result set.")
        # A checkpoint only tells the reader where the last successful scan stopped. The
        # adapter can additionally declare that its coverage is partial (an injected
        # partial-history fault here; on a real source, a bounded query or a reset change
        # token). That statement must not be lost, or a partial view would read as a
        # complete one (PRD §6, T09/T20).
        for account in self.store.all(
                "SELECT account_id, adapter, display_name, health_state, permission_state, "
                "last_success_at FROM source_account ORDER BY adapter, account_id"):
            if account_id and account["account_id"] != account_id:
                continue
            sample = observed.get(account["account_id"]) or {}
            declared = self._coverage_declaration(account)
            state = declared.get("coverage_state") \
                or (sample.get("data") or {}).get("coverage_state")
            if not state or state in self.COMPLETE_COVERAGE_STATES:
                continue
            mocked = bool(sample.get("mocked"))
            gap = declared.get("gap_reason") or sample.get("detail") or (
                f"the adapter reports coverage_state={state} for this account: the "
                "checkpoints below are not a complete history")
            rows.append({
                "checkpoint_id": C.stable_id("obs-ckpt", account["account_id"], state),
                "account_id": account["account_id"], "scope": "health",
                "scope_ref": "(account observation)",
                "display_name": account["display_name"], "adapter": account["adapter"],
                "health_state": account["health_state"],
                "permission_state": account["permission_state"],
                "cursor_value": None, "coverage_state": state, "gap_reason": gap,
                "note": declared.get("note") or (
                    "Observed coverage, not a stored checkpoint: the adapter declares this "
                    "account's coverage partial for this run."),
                "coverage": sample.get("data"), "observed": True,
                "observed_at": sample.get("observed_at"),
                "last_success_at": account["last_success_at"],
                "account_last_success": account["last_success_at"],
                "mock": mocked, "origin": C.MOCK if mocked else C.REAL,
                "mock_label": sample.get("label") if mocked else None,
                "coverage_disclosure": (
                    "MOCK: coverage describes fixture data only. A completed pagination run "
                    "proves only that the adapter reached the end of that query's accessible "
                    "result set.") if mocked else (
                    "Observed coverage: the adapter reached the end of one query's accessible "
                    "result set, which is not the end of history."),
            })
        return rows

    def source_health(self) -> list[dict]:
        rows = self.store.all("SELECT * FROM source_account ORDER BY adapter, account_id")
        observed = self.observe_sources()
        for row in rows:
            row["capabilities"] = [_capability_row(c) for c in self.store.all(
                "SELECT * FROM capability WHERE account_id = ? ORDER BY name",
                (row["account_id"],))]
            row["enabled_operations"] = json.loads(row["enabled_operations"])
            row["mock"] = row["origin"] == C.MOCK
            row["disclosure"] = ("MOCK: this is a labelled mock account. It proves nothing about "
                                 "the installed Mail, Beeper, Contacts or Hermes on Randy's Mac.")
            sample = observed.get(row["account_id"]) or {}
            row["stored_health_state"] = row["health_state"]
            row["stored_health_detail"] = row["health_detail"]
            row["observed_health_state"] = sample.get("code")
            row["observed_at"] = sample.get("observed_at")
            row["observed_label"] = sample.get("label")
            # Three axes, three sources of truth: the live probe (transport), the account's
            # own last observation (freshness) and this deployment's checkpoints plus the
            # adapter's declaration (coverage). None of them borrows another's good news.
            checkpoints = self.store.all(
                "SELECT scope, scope_ref, coverage_state, gap_reason, coverage_json, "
                "last_success_at, oldest_observed_time FROM sync_checkpoint WHERE account_id = ? "
                "ORDER BY scope, scope_ref", (row["account_id"],))
            row["health_axes"] = self.health_axes(row, sample, checkpoints)
            if sample and sample["code"] not in self.HEALTHY_OBSERVED_STATES:
                # The observed failure replaces the stored state: what is true now wins over
                # what was last written.
                row["health_state"] = sample["code"]
                row["health_detail"] = sample.get("detail") or (
                    f"the adapter reports {sample['code']} for this account")
                row["health_state_basis"] = "observed just now (health probe of this run)"
            else:
                row["health_state_basis"] = (
                    "stored ledger state; the source answered healthy to this run's probe")
        return rows

# ------------------------------------------------------- Gate 2 probe records --
# The Mini worker's capability probe is the instrument that will measure Randy's Mac
# (Gate 2). Its rows are the contract in `probe-pack/00-TEMPLATE.md`; they are imported
# here so a real measurement lands in the ledger and can be served to the review client
# instead of dying in a report.

#: Contract version the Mini worker writes into every row. An import from a different
#: version is refused rather than silently reinterpreted.
PROBE_CONTRACT_VERSION = "1.0"   # = switchboard_mini.version.PROBE_CONTRACT_VERSION


def _capability_row(stored: dict) -> dict:
    """One capability row as the review surfaces need it, from the stored columns."""
    return {
        "name": stored["name"],
        "supported": bool(stored["supported"]),
        "state": stored["state"],
        "limitation": stored.get("limitation"),
        "probe_method": stored.get("probe_method"),
        "probe_assertion": stored.get("probe_assertion"),
        "observed_version": stored.get("observed_version"),
        "permission": stored.get("permission"),
        "observed_at": stored.get("observed_at"),
        "origin": stored.get("origin"),
        "label": stored.get("mock_label"),
        "source": stored.get("source") or stored.get("adapter"),
        # The three honesty flags, read straight back out of the ledger. ``real_source_connected``
        # is the one that decides whether "no source was contacted" may be said at all.
        "values_from_source": bool(stored.get("values_from_source")),
        "real_source_connected": bool(stored.get("real_source_connected")),
        # What this row measured: 'source' for every row about Mail/Beeper/Contacts/Hermes,
        # 'worker' for the Mini worker's own manifest row (a self-measurement that contacts
        # nothing). Defect fix, 2026-10-09.
        "measurement_target": (stored.get("measurement_target")
                               or C.MEASUREMENT_SOURCE),
        "sourced_refs": json.loads(stored["sourced_refs"]) if stored.get("sourced_refs")
                        else [],
    }


def probe_row_problems(row: dict) -> list[str]:
    """Every reason this probe row may not be stored. Empty list means it may.

    ``probe-pack/00-TEMPLATE.md`` fixes the row contract; this is where Grace holds it.
    The two rules that matter most, both from the pack's scope statement:
    a documentation-origin row may never be ``supported``, and a row may not claim it
    answered from a source unless it says a real source was connected.
    """
    problems: list[str] = []
    if not isinstance(row, dict):
        # A 'rows' list holding something that is not a row is a malformed document, so it
        # is a typed refusal like every other one -- not an AttributeError (2026-10-09).
        return [f"row is a JSON {_json_kind(row)}, not a capability row"]
    name = row.get("capability") or row.get("name")
    if not name:
        problems.append("row has no capability name")
    origin = row.get("origin")
    if origin not in ("real", "fixture", "mock", "documentation"):
        problems.append(f"{name}: origin {origin!r} is not real|fixture|documentation")
    if "supported" not in row:
        problems.append(f"{name}: row does not say whether the capability is supported")
    state = row.get("state")
    if state not in C.PROBE_ROW_STATES:
        problems.append(f"{name}: state {state!r} is not one of {C.PROBE_ROW_STATES}")
    permission = row.get("permission_state", row.get("permission"))
    if permission not in C.PERMISSION_STATES:
        problems.append(f"{name}: permission {permission!r} is not one of "
                        f"{C.PERMISSION_STATES}")
    if not row.get("probe_assertion"):
        problems.append(f"{name}: row does not state what its supported flag asserts")
    version = row.get("observed_version")
    if not version:
        problems.append(f"{name}: row reports no observed_version (use "
                        f"{C.VERSION_NOT_OBSERVED!r} when nothing was observed)")
    contract = row.get("probe_contract_version")
    if contract and contract != PROBE_CONTRACT_VERSION:
        problems.append(f"{name}: row was written by probe contract {contract!r}, this "
                        f"service stores {PROBE_CONTRACT_VERSION!r}")
    # The scope-statement rule, and the flag consistency it depends on.
    if not C.probe_row_supported_claim_allowed(row):
        problems.append(f"{name}: origin 'documentation' may never be supported=true "
                        f"(probe-pack scope statement)")
    target = row.get("measurement_target", C.MEASUREMENT_SOURCE)
    if target not in C.MEASUREMENT_TARGETS:
        problems.append(f"{name}: measurement_target {target!r} is not one of "
                        f"{list(C.MEASUREMENT_TARGETS)}")
    if target == C.MEASUREMENT_WORKER and (row.get("values_from_source")
                                           or row.get("real_source_connected")):
        problems.append(f"{name}: measurement_target 'worker' says this row measured the "
                        f"worker itself, so it cannot carry source values "
                        f"(values_from_source={bool(row.get('values_from_source'))}, "
                        f"real_source_connected={bool(row.get('real_source_connected'))})")
    if row.get("supported") and origin == "real" and not row.get("real_source_connected") \
            and not C.probe_row_supported_without_a_source_allowed(row):
        problems.append(f"{name}: origin 'real' claims the capability is supported but "
                        f"real_source_connected=false — no source answered this row (a row "
                        f"that read no source may only be supported when it says it measured "
                        f"the worker: measurement_target 'worker' for "
                        f"{sorted(C.SELF_MEASURED_CAPABILITIES)})")
    if row.get("values_from_source") and row.get("state") not in ("success", "partial"):
        problems.append(f"{name}: values_from_source=true with state {state!r} — the row "
                        f"has no source values to have come from")
    if row.get("real_source_connected") and not row.get("values_from_source"):
        problems.append(f"{name}: real_source_connected=true while values_from_source=false")
    return problems


# ------------------------------------------------------- the probe handover ----
# One probe run emits one row per capability key, and the capability key is what the
# ledger stores. So two things have to be explicit when rows cross from the worker to
# Grace, and neither may happen silently:
#
#   * a **measured** row (``real``/``fixture``/``mock``) that arrives for a capability
#     whose stored row is a documentation read **supersedes** it -- that is the handover
#     the worker names in the row's own ``supersedes`` field, and it is reported;
#   * a **documentation** row that arrives for a capability the ledger already holds a
#     *measurement* for is **refused**, because a page read may never replace an
#     observation. Nothing is written and the refusal names its reason and the smallest
#     next action -- the two ways of "keeping both as disagreeing rows" (storing the
#     page read beside the measurement, or overwriting the measurement with it) are both
#     closed here.

#: Origins that are evidence of a read rather than of a page.
MEASURED_PROBE_ORIGINS = ("real", "fixture", "mock")

#: The typed refusals, one per kind of stored measurement a documentation read would
#: replace. Named so a person or a script can act on the reason alone.
DOCUMENTATION_REPLACES_REAL_MEASUREMENT = "documentation_would_replace_a_real_measurement"
DOCUMENTATION_REPLACES_MEASUREMENT = "documentation_would_replace_a_measurement"

NEXT_ACTION_REAL_MEASUREMENT = (
    "keep the stored real measurement: re-import the run that measured this capability on "
    "the Mac (the Mini worker's `probe` re-emits the measured row), or remove that stored "
    "row deliberately before importing a documentation read — a page read may not replace "
    "an observation")
NEXT_ACTION_MEASUREMENT = (
    "re-run the probe with the adapter that measured this capability in the run (the run "
    "that produced this documentation row had no adapter for it), or import only the rows "
    "the run measured")


def probe_row_handover(store: Store, account_id: str, rows: Iterable[dict]) -> dict:
    """What this import would supersede, and what it may not replace.

    Pure read: nothing is written here. Returns the supersessions (informational, applied
    by the import) and the refusals (nothing is applied at all if there are any).
    """
    supersessions: list[dict] = []
    refusals: list[dict] = []
    problems: list[str] = []
    for row in rows:
        name = row.get("capability") or row.get("name")
        if not name:
            continue                    # probe_row_problems already refuses this row
        stored = store.one(
            "SELECT name, origin, state, supported, observed_at, real_source_connected "
            "FROM capability WHERE account_id = ? AND name = ?", (account_id, name))
        if stored is None:
            continue                    # first row for this capability key: nothing to hand over
        stored_origin = stored.get("origin")
        if stored_origin not in MEASURED_PROBE_ORIGINS:
            # The stored row is itself a documentation read (or an origin the schema does
            # not know). A measured row supersedes it; a documentation re-read just
            # replaces a page read with a page read.
            if (row.get("origin") in MEASURED_PROBE_ORIGINS
                    and row.get("origin") != stored_origin):
                supersessions.append({
                    "capability": name,
                    "superseded_origin": stored_origin,
                    "superseded_state": stored.get("state"),
                    "superseded_supported": bool(stored.get("supported")),
                    "superseded_observed_at": stored.get("observed_at"),
                    "superseding_origin": row.get("origin"),
                    "superseding_supported": bool(row.get("supported")),
                    "superseding_real_source_connected":
                        bool(row.get("real_source_connected")),
                    "note": ("the stored documentation row for this capability is replaced "
                             "by this measured row: a measurement supersedes a page read. "
                             "This is reported rather than silent, and the two rows never "
                             "coexist in the ledger"),
                })
            continue
        if row.get("origin") != "documentation":
            continue                    # measurement replaces measurement: the usual update
        real = (stored_origin == "real" or bool(stored.get("real_source_connected")))
        reason = (DOCUMENTATION_REPLACES_REAL_MEASUREMENT if real
                  else DOCUMENTATION_REPLACES_MEASUREMENT)
        next_action = (NEXT_ACTION_REAL_MEASUREMENT if real else NEXT_ACTION_MEASUREMENT)
        refusals.append({
            "capability": name,
            "reason": reason,
            "reason_text": (
                "a documentation row for this capability would replace the stored real "
                "measurement already in this ledger" if real else
                "a documentation row for this capability would replace the stored "
                "measurement already in this ledger"),
            "stored_origin": stored_origin,
            "stored_state": stored.get("state"),
            "stored_supported": bool(stored.get("supported")),
            "stored_observed_at": stored.get("observed_at"),
            "offending_origin": row.get("origin"),
            "offending_state": row.get("state"),
            "next_action": next_action,
        })
        problems.append(
            f"{name}: {reason} — a documentation read may never replace a stored "
            f"measurement (stored origin {stored_origin!r}, state "
            f"{stored.get('state')!r}). Nothing was imported. Smallest next action: "
            f"{next_action}")
    return {"supersessions": supersessions, "refusals": refusals,
            "problems": sorted(problems)}


def _worker_self_clause(worker_self: list[dict]) -> str:
    """``; 1 row measures the worker itself (manifest), not a source``.

    Count-correct for one row and for many, and the capability name is read off the rows
    (``capability`` on a probe row, ``name`` on a stored one). A row that names nothing is
    described without a name: this sentence is shown to the owner, and ``(?)`` is not a
    capability. Defect fix, 2026-10-09.
    """
    names = sorted({(row.get("capability") or row.get("name") or "").strip()
                    for row in worker_self} - {""})
    count = len(worker_self)
    noun, verb = ("row", "measures") if count == 1 else ("rows", "measure")
    named = f" ({', '.join(names)})" if names else ""
    return f"; {count} {noun} {verb} the worker itself{named}, not a source"


def probe_provenance(rows: list[dict]) -> dict:
    """What a set of probe rows honestly says about where its values came from.

    Every word of the statement is derived from the rows themselves, so it cannot go on
    saying "no source was contacted" after a row has connected to one. This is the single
    source of that claim for the review client and the health payload.
    """
    total = len(rows)
    if not total:
        return {"rows": 0, "real_source_connected": 0, "documentation_rows": 0,
                "fixture_rows": 0, "supported": 0, "unmeasured": 0,
                "worker_self_measurements": 0,
                "no_source_contacted": True,
                "statement": ("No capability rows have been measured: no source has been "
                              "contacted, and no capability is claimed.")}
    connected = [r for r in rows if r.get("real_source_connected")]
    documentation = [r for r in rows if r.get("origin") == "documentation"]
    fixture = [r for r in rows if r.get("origin") in ("fixture", "mock")]
    supported = [r for r in rows if r.get("supported")]
    unmeasured = [r for r in rows if r.get("state") == C.PROBE_UNMEASURED]
    # A supported row that contacted nothing is only allowed when it says it measured the
    # worker itself (the Mini worker's manifest row). Say so rather than letting it look
    # like a source claim. Defect fix, 2026-10-09.
    worker_self = [r for r in rows if r.get("measurement_target") == C.MEASUREMENT_WORKER]
    if connected:
        statement = (f"{len(connected)} of {total} capability rows answered from a real "
                     f"source; the rest did not.")
    else:
        statement = (f"No source was contacted by any of the {total} capability rows: "
                     f"{len(unmeasured)} are unmeasured"
                     + (f", {len(documentation)} are documented-only reads"
                        if documentation else "")
                     + (f", {len(fixture)} came from recorded fixtures" if fixture else "")
                     + (_worker_self_clause(worker_self) if worker_self else "")
                     + ". Nothing here is an observation of Randy's Mac.")
    return {"rows": total, "real_source_connected": len(connected),
            "documentation_rows": len(documentation), "fixture_rows": len(fixture),
            "supported": len(supported), "unmeasured": len(unmeasured),
            "worker_self_measurements": len(worker_self),
            "no_source_contacted": not connected, "statement": statement,
            "sources": sorted({r.get("source") for r in rows if r.get("source")})}


# ------------------------------------------- the document a probe run arrives in --
# ``switchboard-mini probe --out run.jsonl`` writes **JSONL**: one row per line. Grace reads
# one JSON *document* with a ``rows`` list, and now says so for every shape that is not that
# document, instead of handing the whole file to ``json.loads``.
#
# Defect fixed 2026-10-09: importing the natural artefact of ``probe --out`` used to crash
# with ``json.decoder.JSONDecodeError: Extra data: line 2 column 1 (char 1135)`` -- exit
# status 1 with a raw traceback, which is not a refusal and tells the owner nothing he can
# act on. Each shape below is now a typed reason code plus the smallest next action, in the
# same shape as the command's other refusals, and nothing is stored.

PROBE_DOCUMENT_UNREADABLE = "probe_document_unreadable"
PROBE_DOCUMENT_EMPTY = "probe_document_empty"
PROBE_DOCUMENT_NOT_JSON = "probe_document_not_json"
PROBE_DOCUMENT_IS_JSONL = "probe_document_is_jsonl"
PROBE_DOCUMENT_WRONG_SHAPE = "probe_document_wrong_shape"
PROBE_DOCUMENT_NO_ROWS = "probe_document_no_rows"

#: Every typed reason a malformed probe document can be refused with, in one place so a
#: script (or a test) can enumerate them.
PROBE_DOCUMENT_REASONS = (PROBE_DOCUMENT_UNREADABLE, PROBE_DOCUMENT_EMPTY,
                          PROBE_DOCUMENT_NOT_JSON, PROBE_DOCUMENT_IS_JSONL,
                          PROBE_DOCUMENT_WRONG_SHAPE, PROBE_DOCUMENT_NO_ROWS)

#: The documented wrap, as the readiness pack and ``probe-import --help`` both give it.
PROBE_DOCUMENT_WRAP = "jq -s '{rows: .}'"
#: A bare JSON array of probe rows: the rows list without its key. ``jq '{rows: .}'``.
PROBE_DOCUMENT_NAME_ROWS = "jq '{rows: .}'"
#: One probe row on its own, wrapped as the one-row run it is: ``jq '{rows: [.]}'``.
#: Not ``-s``: slupring first makes the whole file the one element and nests the row in a
#: list inside the list, which Grace then refuses. Run, not assumed (2026-10-09).
PROBE_DOCUMENT_NAME_ONE_ROW = "jq '{rows: [.]}'"
#: The run itself, as the pack takes it. Every refusal that prints a command to take a run
#: prints this one, and it was run before it was written down.
PROBE_DOCUMENT_TAKE_RUN = ("bash mini/bin/switchboard-mini probe --account <label> "
                           "--out run.jsonl")

# Advice fixed 2026-10-09. Every refusal used to close with the JSONL wrap applied to the
# file at hand, which works only for a JSONL run. Applied to a bare array, an object without
# ``rows`` or a file that is not JSON it produced a document Grace refuses again -- a "smallest
# next action" that led nowhere. A refusal now names the transform that fits *that* shape, or
# the run to take instead.


def probe_document_wrap_target(source: str) -> str:
    """Where a wrap of ``source`` lands: ``run.jsonl`` -> ``run.json``, else ``+.json``."""
    return (re.sub(r"\.jsonl$", ".json", source) if source.endswith(".jsonl")
            else source + ".json")


def probe_document_wrap_next_action(source: str) -> str:
    """``jq -s '{rows: .}' run.jsonl > run.json`` for a real path: the smallest next action."""
    return f"{PROBE_DOCUMENT_WRAP} {source} > {probe_document_wrap_target(source)}"


def probe_document_run_next_action(source: str) -> str:
    """A file that is not a probe run: take one. No wrap of this file can help."""
    return (f"this file is not a probe run and no wrap of it can make it one: take a run "
            f"with `{PROBE_DOCUMENT_TAKE_RUN}`, wrap it "
            f"(`{PROBE_DOCUMENT_WRAP} run.jsonl > run.json`) and import the file that writes")


def probe_document_rows_list_next_action(source: str) -> str:
    """A bare JSON array of probe rows is the rows list without its key: name it as one."""
    return (f"this file is the 'rows' list without its key: name it "
            f"(`{PROBE_DOCUMENT_NAME_ROWS} {source} > "
            f"{probe_document_wrap_target(source)}`) and import the file that writes")


def probe_document_single_row_next_action(source: str) -> str:
    """One probe row on its own: a run of one row, wrapped as such."""
    return (f"this file is one probe row, not a run: wrap it as the one-row run it is "
            f"(`{PROBE_DOCUMENT_NAME_ONE_ROW} {source} > "
            f"{probe_document_wrap_target(source)}`) and import the file that writes")


def probe_document_take_run_next_action() -> str:
    """Nothing was measured: take a run that measures something and import its output."""
    return (f"take a run that measures something (`{PROBE_DOCUMENT_TAKE_RUN}`) and import "
            f"the rows it emits")


def _json_kind(value) -> str:
    """How to name a JSON value in a sentence, without Python's type names leaking out."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    return "object"


def _looks_like_probe_row(value) -> bool:
    """A probe row names the capability it is about; nothing else is a row."""
    return isinstance(value, dict) and bool(value.get("capability") or value.get("name"))


def _is_list_of_probe_rows(value) -> bool:
    """A bare JSON array of probe rows: the ``rows`` list without its key."""
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, dict) for item in value)
            and any(_looks_like_probe_row(item) for item in value))


def _refused_probe_document(reason: str, problem: str, next_action: str,
                            source: str) -> dict:
    return {"ok": False, "reason": reason, "problem": problem,
            "next_action": next_action, "source": source}


def _looks_like_jsonl(text: str) -> bool:
    """Several JSON values, one per line: what ``probe --out`` writes."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    for line in lines:
        try:
            json.loads(line)
        except ValueError:
            return False
    return True


def load_probe_document(text: str, *, source: str = "run.jsonl") -> dict:
    """Read one probe document. Returns the rows, or a typed refusal -- never an exception.

    ``{"ok": True, "rows": [...], "source": ...}`` or
    ``{"ok": False, "reason": <one of PROBE_DOCUMENT_REASONS>, "problem": ...,
    "next_action": ..., "source": ...}``.
    """
    wrap = probe_document_wrap_next_action(source)
    if not text.strip():
        return _refused_probe_document(
            PROBE_DOCUMENT_EMPTY,
            "the file is empty: the probe wrote no rows, so there is nothing to import",
            probe_document_take_run_next_action(), source)
    try:
        document = json.loads(text)
    except ValueError as err:
        if "Extra data" in str(err) or _looks_like_jsonl(text):
            return _refused_probe_document(
                PROBE_DOCUMENT_IS_JSONL,
                f"this file is JSONL (one JSON row per line, which is what "
                f"`switchboard-mini probe --out` writes), not the JSON document Grace "
                f"reads: {err}",
                wrap, source)
        return _refused_probe_document(
            PROBE_DOCUMENT_NOT_JSON,
            f"this file is not JSON, so it is not a probe run -- and a wrap cannot turn "
            f"text into JSON: {err}",
            probe_document_run_next_action(source), source)
    if not isinstance(document, dict):
        if _is_list_of_probe_rows(document):
            return _refused_probe_document(
                PROBE_DOCUMENT_WRONG_SHAPE,
                "the document is a JSON array of probe rows, not the object with a 'rows' "
                "list that Grace reads",
                probe_document_rows_list_next_action(source), source)
        return _refused_probe_document(
            PROBE_DOCUMENT_WRONG_SHAPE,
            f"the document is a JSON {_json_kind(document)}, not the object with a 'rows' "
            f"list that Grace reads",
            probe_document_run_next_action(source), source)
    if "rows" not in document:
        if _looks_like_probe_row(document):
            return _refused_probe_document(
                PROBE_DOCUMENT_WRONG_SHAPE,
                "the document is a single probe row, not the object with a 'rows' list that "
                "Grace reads: a run is what Grace stores, not one row",
                probe_document_single_row_next_action(source), source)
        return _refused_probe_document(
            PROBE_DOCUMENT_WRONG_SHAPE,
            "the document has no 'rows' list: an object without 'rows' is not a probe run, "
            "and wrapping it would only put that object inside a 'rows' key",
            probe_document_run_next_action(source), source)
    rows = document["rows"]
    if not isinstance(rows, list):
        return _refused_probe_document(
            PROBE_DOCUMENT_WRONG_SHAPE,
            f"'rows' is a JSON {_json_kind(rows)}, not a list of capability rows",
            probe_document_run_next_action(source), source)
    if not rows:
        return _refused_probe_document(
            PROBE_DOCUMENT_NO_ROWS,
            "the document has an empty 'rows' list: nothing was measured, so storing it "
            "would report a successful import of nothing",
            probe_document_take_run_next_action(), source)
    return {"ok": True, "rows": rows, "source": source}


def read_probe_document(path: str) -> dict:
    """Read a probe document from disk. A missing or unreadable file is a typed refusal."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        return _refused_probe_document(
            PROBE_DOCUMENT_UNREADABLE,
            f"the file could not be read: {err.strerror or err}",
            f"check the path -- the Mini worker writes it with `switchboard-mini probe "
            f"--out {path}` -- then re-run the import", str(path))
    return load_probe_document(text, source=str(path))


def probe_document_refusal(loaded: dict) -> dict:
    """A refused document, in the same shape as every other ``probe-import`` refusal."""
    return {"imported": 0, "refused": 0, "problems": [loaded["problem"]],
            "reason": loaded["reason"], "next_action": loaded["next_action"],
            "provenance": probe_provenance([]), "supersessions": [], "refusals": [],
            "ok": False}


def _provenance_row(row) -> dict:
    """One row, in the shape :func:`probe_provenance` needs to describe it.

    The capability name travels with the row: without it the statement described a
    self-measurement it could not name and printed "(?)" at the owner (defect fix,
    2026-10-09). A "row" that is not an object at all carries nothing to describe.
    """
    if not isinstance(row, dict):
        return {"origin": None, "supported": None, "state": None,
                "real_source_connected": None, "measurement_target": None,
                "capability": None, "source": None}
    return {"origin": row.get("origin"), "supported": row.get("supported"),
            "state": row.get("state"),
            "real_source_connected": row.get("real_source_connected"),
            "measurement_target": row.get("measurement_target", C.MEASUREMENT_SOURCE),
            "capability": row.get("capability") or row.get("name"),
            "source": row.get("source")}


def import_probe_rows(store: Store, account_id: str, rows: Iterable[dict], *,
                      actor: str = "probe-import") -> dict:
    """Store one probe run's rows. Refuses the whole import if any row over-claims.

    All-or-nothing on purpose: a partially imported probe would leave the review surfaces
    describing a machine that was never measured. Returns a result dict; raises nothing for
    bad input (an import of a malformed document is a typed refusal, not a crash).

    Two handover rules decide what may happen to a row already stored for the same
    capability key (see ``probe_row_handover``): a measured row supersedes a stored
    documentation row, and that supersession is reported in ``supersessions``; a
    documentation row may never replace a stored measurement, so it refuses the import
    (type ``refusals``, each with its reason and the smallest next action). A row that
    over-claims still refuses everything; nothing is ever silently overwritten and two
    disagreeing rows for one capability never coexist.
    """
    rows = list(rows)
    problems = [p for row in rows for p in probe_row_problems(row)]
    provenance = probe_provenance([_provenance_row(row) for row in rows])
    refused_result = {"imported": 0, "refused": len(rows), "provenance": provenance,
                      "supersessions": [], "refusals": [], "ok": False}
    if problems:
        return {**refused_result, "problems": sorted(problems)}
    if not store.one("SELECT account_id FROM source_account WHERE account_id = ?",
                     (account_id,)):
        return {**refused_result,
                "problems": [f"no source_account {account_id!r} in this ledger"]}
    # The handover with what is already stored: a measurement supersedes a stored
    # documentation row (reported), and a documentation row may never replace a stored
    # measurement (refused, typed, with the smallest next action). Read-only.
    handover = probe_row_handover(store, account_id, rows)
    if handover["refusals"]:
        return {**refused_result, "problems": handover["problems"],
                "refusals": handover["refusals"]}
    with store.tx():
        for row in rows:
            name = row.get("capability") or row.get("name")
            store.upsert_row("capability", {
                "account_id": account_id,
                "name": name,
                "supported": 1 if row.get("supported") else 0,
                "state": row.get("state"),
                "limitation": row.get("limitation"),
                "probe_method": row.get("probe_method"),
                "observed_at": row.get("probed_at") or C.now(),
                "origin": row.get("origin"),
                "mock_label": row.get("label") or row.get("mock_label"),
                "observed_version": row.get("observed_version"),
                "permission": row.get("permission_state", row.get("permission")),
                "probe_assertion": row.get("probe_assertion"),
                "evidence": C.canonical_json(row.get("evidence") or {}),
                "values_from_source": 1 if row.get("values_from_source") else 0,
                "real_source_connected": 1 if row.get("real_source_connected") else 0,
                # What this row measured: a source, or (for the worker's own manifest row)
                # the worker itself. Stored so the ledger can say why a supported row
                # contacted nothing. Defect fix, 2026-10-09.
                "measurement_target": (row.get("measurement_target")
                                       or C.MEASUREMENT_SOURCE),
                "sourced_refs": C.canonical_json(list(row.get("citations") or [])),
            }, ["account_id", "name"])
    return {"imported": len(rows), "refused": 0, "problems": [], "provenance": provenance,
            "supersessions": handover["supersessions"], "refusals": [], "ok": True}

def stored_probe_rows(store: Store) -> list[dict]:
    """Every capability row in the ledger, shaped for :func:`probe_provenance`."""
    return [_capability_row(row) for row in store.all(
        "SELECT c.*, a.adapter AS source FROM capability c "
        "JOIN source_account a ON a.account_id = c.account_id "
        "ORDER BY c.account_id, c.name")]
