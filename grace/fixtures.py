"""Deterministic fixture corpus for the labelled mock adapters.

Everything here is invented test data on reserved ``.test`` domains. It is not a
recording of Randy's mailbox and it is not a claim about any real source. Every
record carries ``origin='mock'`` and a ``MOCK:`` label (see contracts.mock_label).

The corpus is deterministic: ids come from contracts.stable_id, and all source
timestamps are fixed constants, so two runs of the test suite see identical data.
"""

from __future__ import annotations

from typing import Any

from .contracts import MOCK, mock_label, stable_id

FIXTURE_SET_VERSION = "fixtures-1.0"
SOURCE_VERSIONS = {"mock_mail": "1.0.0-mock", "mock_beeper": "1.0.0-mock",
                   "mock_contacts": "1.0.0-mock", "mock_hermes": "1.0.0-mock"}

# ----------------------------------------------------------------- scenarios ---
# Named dispatch/reconcile behaviours used to exercise §10 and T14/T15 honestly.
# Each is a visible scripted outcome of a MOCK adapter, never a real send.
SCENARIOS: dict[str, dict[str, Any]] = {
    "happy_pending": {
        "description": "MOCK: source returns a pending message id and the actual routed chat; "
                       "a later reconcile call confirms the send.",
        "dispatch": {"code": "success", "provider_status": "pending",
                     "route_to": "member_chat_confirmed"},
        "reconcile": {"code": "success", "confirmed": True},
        "by_adapter": {
            "mock_mail": {
                "description": "MOCK: Mail accepts the send locally only; no delivery confirmation "
                               "is available, so the receipt stays at provider_accepted (T15).",
                "dispatch": {"code": "success", "provider_status": "accepted",
                             "route_to": "as_requested"},
                "reconcile": {"code": "partial", "confirmed": False,
                              "limitation": "MOCK: Mail send was only locally accepted; delivery "
                                            "state cannot be confirmed"},
            },
        },
    },
    "accepted_only": {
        "description": "MOCK: source accepts the send locally only; reconcile cannot confirm "
                       "delivery, so the receipt stays at provider_accepted.",
        "dispatch": {"code": "success", "provider_status": "accepted", "route_to": "as_requested"},
        "reconcile": {"code": "partial", "confirmed": False,
                      "limitation": "MOCK: local acceptance only; no delivery confirmation available"},
    },
    "pre_submission_failure": {
        "description": "MOCK: transport fails before submission. Nothing reached the source, so a "
                       "retry under the same authorization is allowed.",
        "dispatch": {"code": "retryable_error", "submitted": False,
                     "error_category": "transient_network",
                     "detail": "MOCK: connection refused before the request was submitted"},
        "reconcile": {"code": "unsupported", "detail": "MOCK: nothing to reconcile"},
    },
    "timeout_uncertain": {
        "description": "MOCK: the request timed out after submission. The outcome is unknown; the "
                       "effect must enter reconciliation and must never be retried blindly.",
        "dispatch": {"code": "outcome_unknown", "submitted": True,
                     "error_category": "transient_network",
                     "detail": "MOCK: timeout after submission — source state unknown"},
        "reconcile": {"code": "outcome_unknown",
                      "detail": "MOCK: source lookup inconclusive; operator review required"},
    },
    "timeout_then_found": {
        "description": "MOCK: first dispatch times out after submission; reconciliation later finds "
                       "the message really was sent, so the receipt is confirmed once and only once.",
        "dispatch": {"code": "outcome_unknown", "submitted": True,
                     "detail": "MOCK: timeout after submission — source state unknown"},
        "reconcile": {"code": "success", "confirmed": True,
                      "provider_message_id": "mock-reconciled-1"},
    },
    "duplicate_tap": {
        "description": "MOCK: source honours the idempotency key; a repeated dispatch of the same "
                       "operation returns the original message id instead of sending twice.",
        "dispatch": {"code": "success", "provider_status": "pending", "idempotent_replay": True},
        "reconcile": {"code": "success", "confirmed": True},
    },
    "permission_denied": {
        "description": "MOCK: the account permission for the send operation was revoked.",
        "dispatch": {"code": "permission_denied",
                     "detail": "MOCK: automation permission for this account is denied"},
        "reconcile": {"code": "unsupported"},
    },
}

DEFAULT_SCENARIO = "happy_pending"

# ------------------------------------------------------------------ corpus -----


def _acct(adapter: str, identity: str, display: str, ops: list[str],
          health: str = "current", permission: str = "granted",
          detail: str | None = None) -> dict[str, Any]:
    account_id = stable_id("acct", adapter, identity)
    return {
        "account_id": account_id,
        "adapter": adapter,
        "adapter_version": SOURCE_VERSIONS[adapter],
        "account_identity": identity,
        "host_role": "mini",
        "display_name": display,
        "enabled_operations": ops,
        "health_state": health,
        "health_detail": detail,
        "permission_state": permission,
        "last_success_at": "2026-10-06T21:00:00.000000Z",
        "last_probe_at": "2026-10-06T21:00:00.000000Z",
        "origin": MOCK,
        "mock_label": mock_label(adapter),
    }


READ_OPS = ["manifest", "health", "accounts", "enumerate", "retrieve", "history_poll",
            "change_poll", "identity_evidence", "materialize_attachment"]
SEND_OPS = READ_OPS + ["prepare_draft", "dispatch", "reconcile"]


def build_corpus() -> dict[str, Any]:
    mail = _acct("mock_mail", "randy@example-mail.test", "Mail (MOCK primary)",
                 SEND_OPS)
    beeper = _acct("mock_beeper", "randy@example-beeper.test", "Beeper (MOCK primary)",
                   SEND_OPS)
    contacts = _acct("mock_contacts", "device:mock-mini", "Contacts (MOCK mini device)",
                     READ_OPS, health="permission_denied", permission="denied",
                     detail="MOCK: contacts permission denied in this simulated session")
    hermes = _acct("mock_hermes", "grace:mock-profile", "Hermes (MOCK runtime)", READ_OPS)

    accounts = [mail, beeper, contacts, hermes]
    A = {a["adapter"]: a["account_id"] for a in accounts}

    def conv(adapter: str, key: str, provider_id: str, audience_kind: str, audience: list[str],
             first: str, last: str, *, availability: str = "available",
             reason: str | None = None, meta: dict | None = None,
             merged: bool = False) -> dict[str, Any]:
        return {
            "conv_id": stable_id("conv", adapter, key),
            "account_id": A[adapter], "adapter": adapter,
            "namespaced_id": f"{adapter}:{A[adapter]}:{provider_id}",
            "provider_thread_id": provider_id if adapter == "mock_mail" else None,
            "provider_chat_id": provider_id if adapter == "mock_beeper" else None,
            "audience_kind": audience_kind, "audience": audience,
            "provider_is_merged": merged,
            "revision": "1", "availability": availability, "availability_reason": reason,
            "retrieval_pointer": f"mock://{adapter}/{provider_id}",
            "minimal_metadata": meta or {},
            "source_time_first": first, "source_time_last": last,
            "origin": MOCK, "mock_label": mock_label(adapter),
        }

    conversations = [
        conv("mock_mail", "alex-thread", "thread-alex-1", "direct",
             ["alex.rivera@example-mail.test"], "2026-09-28T08:12:00.000000Z",
             "2026-10-06T07:41:00.000000Z"),
        conv("mock_mail", "invoice-thread", "thread-invoice-4471", "direct",
             ["alex.rivera@example-mail.test"], "2026-10-01T13:00:00.000000Z",
             "2026-10-05T16:20:00.000000Z"),
        conv("mock_mail", "old-message", "thread-supplier-9", "direct",
             ["supplier@example-vendor.test"], "2023-04-02T10:05:00.000000Z",
             "2023-04-02T10:05:00.000000Z", availability="partial",
             reason="MOCK: body not fetched; attachment policy downloads on demand"),
        conv("mock_mail", "deleted-source", "thread-deleted-3", "direct",
             ["ghost@example-mail.test"], "2026-09-30T08:00:00.000000Z",
             "2026-09-30T08:00:00.000000Z", availability="unavailable",
             reason="MOCK: source message deleted upstream; tombstone retained"),
        conv("mock_beeper", "alex-dm", "chat-alex-dm", "direct",
             ["@alex:example-matrix.test"], "2026-10-03T19:02:00.000000Z",
             "2026-10-06T20:15:00.000000Z"),
        conv("mock_beeper", "kestrel-group", "chat-kestrel-group", "group",
             ["@alex:example-matrix.test", "@dana:example-matrix.test",
              "@ravi:example-matrix.test"], "2026-10-04T11:00:00.000000Z",
             "2026-10-06T18:44:00.000000Z", merged=True,
             meta={"merged_member_chats": ["chat-kestrel-dana"]}),
        conv("mock_beeper", "lowprio", "chat-noise", "direct",
             ["@noreply:example-matrix.test"], "2026-10-02T06:00:00.000000Z",
             "2026-10-02T06:00:00.000000Z", availability="partial",
             reason="MOCK: low-priority chat excluded from default search; fetched by explicit poll"),
    ]

    def msg(adapter: str, conv_key: str, provider_id: str, *, sender: dict, recipients: list,
            cc: list | None = None, bcc: list | None = None, source_time: str,
            body_state: str = "fetched", availability: str = "available",
            reason: str | None = None, reply_headers: dict | None = None,
            revision: str = "1", deleted: bool = False,
            metadata: dict | None = None) -> dict[str, Any]:
        conv_rec = next(c for c in conversations if c["namespaced_id"].endswith(provider_id_of(conv_key)))
        return {
            "msg_ref_id": stable_id("msg", adapter, provider_id),
            "conv_id": conv_rec["conv_id"],
            "account_id": conv_rec["account_id"],
            "adapter": adapter,
            "namespaced_id": f"{adapter}:{conv_rec['account_id']}:{provider_id}",
            "provider_message_id": provider_id,
            "sender": sender, "recipients": recipients, "cc": cc or [], "bcc": bcc or [],
            "reply_headers": reply_headers or {},
            "source_time": source_time, "revision": revision,
            "availability": availability, "availability_reason": reason,
            "body_state": body_state,
            "retrieval_pointer": f"mock://{adapter}/{conv_rec['provider_thread_id'] or conv_rec['provider_chat_id']}/{provider_id}",
            "minimal_metadata": metadata or {},
            "deleted_at_source": deleted,
            "origin": MOCK, "mock_label": mock_label(adapter),
        }

    def provider_id_of(conv_key: str) -> str:
        return {
            "alex-thread": "thread-alex-1", "invoice-thread": "thread-invoice-4471",
            "old-message": "thread-supplier-9", "deleted-source": "thread-deleted-3",
            "alex-dm": "chat-alex-dm", "kestrel-group": "chat-kestrel-group",
            "lowprio": "chat-noise",
        }[conv_key]

    AL = {"address": "alex.rivera@example-mail.test", "display_name": "Alex Rivera"}
    RANDY = {"address": "randy@example-mail.test", "display_name": "Randy"}
    AL_BEEPER = {"network_identity": "@alex:example-matrix.test", "display_name": "Alex Rivera"}

    messages = [
        msg("mock_mail", "alex-thread", "mail-alex-1", sender=AL, recipients=[RANDY],
            source_time="2026-09-28T08:12:00.000000Z",
            reply_headers={"message_id": "<a1@example-mail.test>", "references": []},
            metadata={"subject": "Kestrel roll-out", "unread": True}, deleted=False),
        msg("mock_mail", "alex-thread", "mail-alex-2", sender=RANDY,
            recipients=[AL["address"]],
            source_time="2026-09-28T09:30:00.000000Z",
            reply_headers={"message_id": "<a2@example-mail.test>", "references": ["<a1@example-mail.test>"]},
            metadata={"subject": "Re: Kestrel roll-out"}),
        msg("mock_mail", "alex-thread", "mail-alex-3", sender=AL, recipients=[RANDY],
            cc=["dana@example-mail.test"], bcc=["archive@example-mail.test"],
            source_time="2026-10-06T07:41:00.000000Z",
            reply_headers={"message_id": "<a3@example-mail.test>", "references": ["<a1@example-mail.test>"]},
            metadata={"subject": "Re: Kestrel roll-out", "unread": True}),
        msg("mock_mail", "invoice-thread", "mail-inv-1", sender=AL, recipients=[RANDY],
            source_time="2026-10-01T13:00:00.000000Z",
            metadata={"subject": "Invoice #4471", "unread": True}),
        msg("mock_mail", "invoice-thread", "mail-inv-2", sender=AL, recipients=[RANDY],
            source_time="2026-10-05T16:20:00.000000Z",
            metadata={"subject": "Re: Invoice #4471", "unread": True}),
        msg("mock_mail", "old-message", "mail-old-1", sender={"address": "supplier@example-vendor.test",
            "display_name": "Vendor Accounts"}, recipients=[RANDY],
            source_time="2023-04-02T10:05:00.000000Z", body_state="not_fetched",
            availability="partial",
            reason="MOCK: body not fetched and attachment not downloaded locally",
            metadata={"subject": "Statement April 2023"}),
        msg("mock_mail", "deleted-source", "mail-del-1", sender={"address": "ghost@example-mail.test",
            "display_name": "Ghost"}, recipients=[RANDY],
            source_time="2026-09-30T08:00:00.000000Z", body_state="missing",
            availability="unavailable", reason="MOCK: source message deleted upstream",
            deleted=True),
        msg("mock_beeper", "alex-dm", "beeper-alex-1", sender=AL_BEEPER,
            recipients=[{"network_identity": "@randy:example-matrix.test"}],
            source_time="2026-10-03T19:02:00.000000Z",
            metadata={"network": "example-matrix", "subject": "DM"}),
        msg("mock_beeper", "alex-dm", "beeper-alex-2", sender=AL_BEEPER,
            recipients=[{"network_identity": "@randy:example-matrix.test"}],
            source_time="2026-10-06T20:15:00.000000Z",
            metadata={"network": "example-matrix", "subject": "DM", "unread": True}),
        msg("mock_beeper", "kestrel-group", "beeper-grp-1", sender=AL_BEEPER,
            recipients=[{"network_identity": "@randy:example-matrix.test"},
                        {"network_identity": "@dana:example-matrix.test"},
                        {"network_identity": "@ravi:example-matrix.test"}],
            source_time="2026-10-04T11:00:00.000000Z",
            metadata={"network": "example-matrix", "subject": "Project Kestrel"}),
        msg("mock_beeper", "kestrel-group", "beeper-grp-2",
            sender={"network_identity": "@dana:example-matrix.test", "display_name": "Dana Okafor"},
            recipients=[{"network_identity": "@randy:example-matrix.test"},
                        {"network_identity": "@alex:example-matrix.test"},
                        {"network_identity": "@ravi:example-matrix.test"}],
            source_time="2026-10-06T18:44:00.000000Z",
            metadata={"network": "example-matrix", "subject": "Project Kestrel", "unread": True}),
        msg("mock_beeper", "lowprio", "beeper-noise-1",
            sender={"network_identity": "@noreply:example-matrix.test", "display_name": "Alerts"},
            recipients=[{"network_identity": "@randy:example-matrix.test"}],
            source_time="2026-10-02T06:00:00.000000Z", availability="partial",
            reason="MOCK: low-priority conversation, excluded from default search results",
            metadata={"network": "example-matrix", "subject": "Alert", "low_priority": True}),
    ]

    people = [
        {"person_id": stable_id("person", "alex"), "display_name": "Alex Rivera"},
        {"person_id": stable_id("person", "sam-mail"), "display_name": "Sam Patel"},
        {"person_id": stable_id("person", "sam-other"), "display_name": "Sam Patel"},
        {"person_id": stable_id("person", "ravi"), "display_name": "Ravi Menon"},
    ]

    links = [
        # T01: exact normalized address match on two different adapers -> one person
        {"person_id": stable_id("person", "alex"), "kind": "email",
         "normalized_value": "alex.rivera@example-mail.test", "adapter": "mock_mail",
         "evidence": "exact_normalized_address_match", "confidence": 0.98,
         "state": "confirmed", "user_override": 0},
        {"person_id": stable_id("person", "alex"), "kind": "network_identity",
         "normalized_value": "@alex:example-matrix.test", "adapter": "mock_beeper",
         "evidence": "exact_normalized_identity_match", "confidence": 0.9,
         "state": "confirmed", "user_override": 0},
        # T02: same display name, different addresses -> never merged on name alone
        {"person_id": stable_id("person", "sam-mail"), "kind": "email",
         "normalized_value": "sam.patel@example-mail.test", "adapter": "mock_mail",
         "evidence": "exact_normalized_address_match", "confidence": 0.97,
         "state": "confirmed", "user_override": 0},
        {"person_id": stable_id("person", "sam-other"), "kind": "email",
         "normalized_value": "sam.patel@example-other.test", "adapter": "mock_mail",
         "evidence": "exact_normalized_address_match", "confidence": 0.97,
         "state": "confirmed", "user_override": 0},
        # T02: shared alias used by several people -> ambiguous, proposed only
        {"person_id": stable_id("person", "sam-mail"), "kind": "email",
         "normalized_value": "accounts@example-mail.test", "adapter": "mock_mail",
         "evidence": "shared_alias_used_by_multiple_people", "confidence": 0.35,
         "state": "proposed", "user_override": 0},
        {"person_id": stable_id("person", "ravi"), "kind": "phone",
         "normalized_value": "+15550100", "adapter": "mock_contacts",
         "evidence": "native_contact_phone_match", "confidence": 0.8,
         "state": "proposed", "user_override": 0},
    ]

    groups = [
        {"group_id": stable_id("group", "kestrel"), "stable_source_identity": "chat-kestrel-group",
         "account_id": A["mock_beeper"], "title": "Project Kestrel",
         "membership_version": "3",
         "member_identity_json": ["@alex:example-matrix.test", "@dana:example-matrix.test",
                                  "@ravi:example-matrix.test"],
         "audience_json": ["@alex:example-matrix.test", "@dana:example-matrix.test",
                           "@ravi:example-matrix.test"],
         "user_created": 0},
    ]

    attachments = [
        {"attachment_ref_id": stable_id("att", "statement-pdf"),
         "msg_ref_id": stable_id("msg", "mock_mail", "mail-old-1"),
         "namespaced_id": "mock_mail:attachment:statement-2023-04.pdf",
         "filename": "statement-2023-04.pdf", "media_type": "application/pdf",
         "size_bytes": 184320, "content_hash": None, "available": False,
         "download_state": "not_downloaded",
         "limitation": "MOCK: Mail attachment download policy leaves this file off-device"},
        {"attachment_ref_id": stable_id("att", "kestrel-deck"),
         "msg_ref_id": stable_id("msg", "mock_beeper", "beeper-grp-2"),
         "namespaced_id": "mock_beeper:attachment:kestrel-status.pptx",
         "filename": "kestrel-status.pptx", "media_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
         "size_bytes": 2048000, "content_hash": "sha256:mock-kestrel-deck", "available": True,
         "download_state": "downloaded", "limitation": None},
    ]

    return {
        "fixture_set_version": FIXTURE_SET_VERSION,
        "accounts": accounts,
        "conversations": conversations,
        "messages": messages,
        "people": people,
        "identity_links": links,
        "groups": groups,
        "attachments": attachments,
        "scenarios": SCENARIOS,
    }


def scenario_names() -> list[str]:
    return sorted(SCENARIOS)


def scenario(name: str | None) -> dict[str, Any]:
    key = name or DEFAULT_SCENARIO
    if key not in SCENARIOS:
        raise KeyError(f"unknown mock scenario {key!r}; known: {scenario_names()}")
    return {"name": key, **SCENARIOS[key]}
