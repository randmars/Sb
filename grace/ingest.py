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

    # -------------------------------------------------------------- coverage ---
    def coverage(self, account_id: str | None = None) -> list[dict]:
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
            row["mock"] = row["origin"] == C.MOCK
            row["coverage_disclosure"] = (
                "MOCK: coverage describes fixture data only. A completed pagination run proves "
                "only that the adapter reached the end of that query's accessible result set.")
        return rows

    def source_health(self) -> list[dict]:
        rows = self.store.all("SELECT * FROM source_account ORDER BY adapter, account_id")
        for row in rows:
            row["capabilities"] = self.store.all(
                "SELECT name, supported, state, limitation, probe_method FROM capability "
                "WHERE account_id = ? ORDER BY name", (row["account_id"],))
            row["enabled_operations"] = json.loads(row["enabled_operations"])
            row["mock"] = row["origin"] == C.MOCK
            row["disclosure"] = ("MOCK: this is a labelled mock account. It proves nothing about "
                                 "the installed Mail, Beeper, Contacts or Hermes on Randy's Mac.")
        return rows
