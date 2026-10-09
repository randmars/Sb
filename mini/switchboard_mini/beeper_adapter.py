"""The read-only Beeper Desktop adapter: bounded, resumable, honest about its gaps.

Read-only by construction: there is no send path, no composer/focus call, no asset
download and no event stream in this slice. Every one of those asks returns
``unsupported`` with a reason, so a consumer cannot reach a Beeper mutation through this
adapter at all (PRD §10 draft-only launch default; R12–R16).

The four reads it does perform are exactly the ones the probe pack documents:

``info``      ``GET /v1/info`` (O11) — reachability and the server metadata document.
``search``    ``GET /v1/messages/search`` (O06) — the documented filters, ``limit`` ≤ 20
              and the opaque ``newestCursor``/``oldestCursor`` pagination, wrapped in this
              adapter's own scope-checked cursor.
``contacts``  ``GET /v1/accounts/{accountID}/contacts/list`` (O12) — cursor pagination,
              identity fields, addresses masked and image URLs reduced to a presence flag
              (O12: ``imgURL`` "may be temporary or available only on this device").
``introspect`` ``POST /oauth/introspect`` (O11) — whether the token is active.

Asks the pack does not answer — an accounts listing, a chats listing, per-chat messages
— return ``unsupported``/``endpoint_not_in_pack`` with the smallest next action (read the
discovery URLs ``GET /v1/info`` exposes on the Mac). No path is guessed.

Three honesty rules the code enforces, each because the opposite is easy to write by
accident:

* **A page that cannot be shown to be the end is not the end.** ``limit`` is capped at the
  documented 20, and a page is only reported ``complete`` when the response said
  ``hasMore: false`` for that query — with the note that this is the end of *this query's*
  result set, which O06 nowhere claims is complete ingestion (PRD line 242).
* **Coverage is partial by design.** O05 says history "might be limited" and only recent
  messages may be available, so an empty or short page is reported with
  ``coverage.coverage_state = partial_history`` and O05's own words as the gap reason,
  never as "this chat has no messages".
* **A stand-in responder is not the source.** ``source_contacted`` is only ever true for a
  response read from the documented endpoint on the Mini Mac, so a row produced against a
  local stand-in server can never claim ``real_source_connected``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional
from urllib.parse import quote, unquote

from . import outcomes as O
from .beeper_transport import (CONTACTS_LIMIT_MAX, CONTACTS_LIMIT_MIN, DIRECTIONS,
                               SEARCH_LIMIT_MAX, SEARCH_LIMIT_MIN, BeeperTransport,
                               build_transport)
from .version import WORKER_VERSION

NAMESPACE = "beeper"
MANIFEST_VERSION = "1.0"
CURSOR_VERSION = "beeper1"
DEFAULT_MAX_PAGES = 4

#: The capability rows the Beeper adapter can measure on a host. Each name is a row in
#: ``documented_capabilities.py`` (the pack's record of the documented surface); this
#: adapter is what turns such a row from a documentation read into a measurement.
MEASURED_CAPABILITIES = (
    "beeper_local_api_reachability",
    "beeper_message_search",
    "beeper_history_depth",
    "beeper_account_contacts",
)

#: The documented field names of O06's ``Message`` and of O12's ``User``. A page's own
#: field names are compared against these lists to report what the install actually sent.
DOCUMENTED_MESSAGE_FIELDS = ("id", "accountID", "chatID", "senderID", "senderName",
                             "sortKey", "timestamp", "text", "type", "attachments",
                             "sendStatus")
DOCUMENTED_CHAT_FIELDS = ("id", "accountID", "network", "participants", "title", "type",
                          "unreadCount", "capabilities", "lastActivity",
                          "lastReadMessageSortKey", "localChatID", "merge",
                          "mergedIntoChatID", "messageExpirySeconds", "reminder", "snooze")
DOCUMENTED_CONTACT_FIELDS = ("id", "cannotMessage", "email", "fullName", "imgURL", "isSelf",
                             "phoneNumber", "username")
DOCUMENTED_SEARCH_PARAMS = ("accountIDs", "chatIDs", "chatType", "cursor", "dateAfter",
                            "dateBefore", "direction", "excludeLowPriority", "includeMuted",
                            "limit", "mediaTypes", "query", "sender")

HISTORY_LIMITATION = ("O05: \"Message history might be limited. Beeper indexes your "
                      "messages from the networks in the background, when you first add an "
                      "account, only recent messages might be available.\"")
NO_COMPLETENESS_CLAIM = ("reaching the end of this query's result set is not proof of "
                         "complete ingestion (PRD line 242; O06 states no completeness "
                         "claim: 'complete', 'completeness' and 'indexed' occur 0 times on "
                         "that page)")


def scope_digest(params: dict) -> str:
    """A stable digest of the parameters that scope a paginated query.

    O06 and O12 both say the cursor is "Opaque string; do not inspect", and neither says
    what happens with a foreign or expired cursor. This adapter therefore refuses to
    resume a cursor across a different query rather than guessing.
    """
    payload = {key: params.get(key) for key in sorted(params) if params.get(key) is not None}
    return hashlib.sha256(O.emit(payload).encode("utf-8")).hexdigest()[:16]


def encode_cursor(direction: str, digest: str, raw: str) -> str:
    return "|".join([CURSOR_VERSION, quote(direction, safe=""), digest, quote(raw, safe="")])


def decode_cursor(cursor: str) -> tuple:
    parts = (cursor or "").split("|")
    if len(parts) != 4 or parts[0] != CURSOR_VERSION:
        raise ValueError(f"malformed Beeper cursor {cursor!r}")
    return unquote(parts[1]), parts[2], unquote(parts[3])


def mask_phone(value: Any) -> Optional[str]:
    """``+1 555 0100 001`` -> ``+*******01``: the shape is kept, the number is not."""
    text = str(value or "").strip()
    if not text:
        return None
    return "*" * max(0, len(text) - 2) + text[-2:]


class BeeperReadOnlyAdapter:
    """A ``SourceAdapter`` in the Grace sense, restricted to the documented reads."""

    name = NAMESPACE
    version = WORKER_VERSION
    host_role = "mini"
    simulated = False

    def __init__(self, transport: BeeperTransport, *, max_pages: int = DEFAULT_MAX_PAGES):
        self.transport = transport
        self.max_pages = max(1, int(max_pages))

    # -- provenance --------------------------------------------------------
    @property
    def origin(self) -> str:
        return self.transport.origin

    @property
    def adapter_is_real(self) -> bool:
        return bool(getattr(self.transport, "adapter_is_real", False))

    @property
    def base_url(self) -> str:
        return getattr(self.transport, "base_url", "")

    def _label(self) -> Optional[str]:
        return getattr(self.transport, "label", None)

    def _stamp(self, outcome: O.Outcome, *, from_read: Optional[O.Outcome] = None,
               account_id: Optional[str] = None) -> O.Outcome:
        outcome.adapter = self.name
        outcome.origin = self.origin
        outcome.adapter_is_real = self.adapter_is_real
        if from_read is not None:
            outcome.adapter_is_real = bool(from_read.adapter_is_real
                                           or self.adapter_is_real)
            outcome.source_contacted = bool(from_read.source_contacted)
        if account_id:
            outcome.account_id = account_id
        if not outcome.is_real:
            outcome.label = self._label() or O.fixture_label(self.name)
        return outcome

    def probe_handlers(self) -> dict:
        """The probe rows this adapter measures, keyed by capability name."""
        from .beeper_probe import BEEPER_PROBES          # local import avoids a cycle
        return dict(BEEPER_PROBES)

    @staticmethod
    def measured_capabilities() -> tuple:
        return MEASURED_CAPABILITIES

    # -- local checks ------------------------------------------------------
    def token_status(self) -> O.Outcome:
        return self._stamp(self.transport.token_status())

    def host_gate(self) -> Optional[O.Outcome]:
        """The host/token precondition, or None when this adapter may make a request."""
        outcome = self.transport.host_gate()
        return None if outcome is None else self._stamp(outcome)

    # -- documented reads --------------------------------------------------
    def info(self) -> O.Outcome:
        """``GET /v1/info`` (O11): is the API there, and what does it say about itself?

        The pack records no field names for this document, so only the *key names* that
        actually came back are reported (which is exactly what the pack's own Mac
        procedure prescribes: "record the JSON keys only (not values)"). No field name is
        read out of it as if it were documented.
        """
        got = self._stamp(self.transport.call("info"))
        if not got.usable:
            return got
        document = (got.data or {}).get("document")
        keys = sorted(document) if isinstance(document, dict) else []
        if not isinstance(document, dict):
            return self._stamp(O.Outcome.partial(
                {**(got.data or {}), "document_kind": type(document).__name__, "keys": []},
                "the /v1/info body is not a JSON object, so no server metadata key could be "
                "read", reason="unexpected_document_shape"),
                from_read=got)
        got.data.update({
            "keys": keys,
            "document_keys_recorded": len(keys),
            "values_withheld": True,
            "endpoint_urls_present": any(
                isinstance(value, (list, str)) and "http" in json.dumps(value)
                for value in document.values()),
            "note": ("the pack records no field names for /v1/info, so this document "
                     "reports the key names it observed and no values (O11's own Mac "
                     "procedure: 'record the JSON keys only (not values)')"),
        })
        del got.data["document"]
        return self._stamp(O.Outcome.ok(got.data), from_read=got)

    def introspect(self) -> O.Outcome:
        """``POST /oauth/introspect`` (O11): ``active: true|false`` for the token."""
        got = self._stamp(self.transport.call("introspect", form={
            # The documented form-encoded payload. The token is taken from the transport
            # and never copied into a document.
            "token_type_hint": "access_token"}))
        if not got.usable:
            return got
        document = (got.data or {}).get("document")
        active = document.get("active") if isinstance(document, dict) else None
        data = {k: v for k, v in (got.data or {}).items() if k != "document"}
        data["active"] = active
        data["documented_field"] = "active"
        del got.data["document"]
        if active is False:
            return self._stamp(O.Outcome.permission_denied(
                "Beeper's own introspection reports active=false: the configured token is "
                "not active. No data was returned.",
                reason="token_inactive", data=data,
                next_action=("create or refresh the token in Beeper Desktop -> Settings -> "
                             "Integrations -> 'Approved connections' (O11)")), from_read=got)
        if active is None:
            return self._stamp(O.Outcome.partial(
                data, "the introspection document carries no 'active' field, so token "
                      "activity is unknown", reason="active_field_absent"), from_read=got)
        return self._stamp(O.Outcome.ok(data), from_read=got)

    def _search_params(self, *, query: Optional[str], account_ids, chat_ids, sender,
                       limit: int, cursor: Optional[str], direction: str,
                       include_low_priority: bool, chat_type: Optional[str] = None) -> dict:
        params: dict = {}
        if query:
            params["query"] = query
        if account_ids:
            params["accountIDs"] = list(account_ids)
        if chat_ids:
            params["chatIDs"] = list(chat_ids)
        if sender:
            params["sender"] = sender
        if chat_type:
            params["chatType"] = chat_type
        params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
            params["direction"] = direction
        if include_low_priority:
            # O06: excludeLowPriority "Default: true. Set to false to include all." The
            # parameter is only sent when the caller asks for the documented override; the
            # default is never asserted as if it had been observed.
            params["excludeLowPriority"] = "false"
        return params

    def search(self, *, query: Optional[str] = None, account_ids=(), chat_ids=(),
               sender: Optional[str] = None, limit: int = SEARCH_LIMIT_MAX,
               cursor: Optional[str] = None, direction: str = "before",
               include_low_priority: bool = False, chat_type: Optional[str] = None,
               sweep: bool = False) -> O.Outcome:
        """A bounded, resumable page of ``GET /v1/messages/search`` (O06)."""
        if direction not in DIRECTIONS:
            return self._stamp(O.Outcome.permanent(
                f"direction {direction!r} is not documented: O06 names "
                f"{list(DIRECTIONS)} only", reason="bad_direction"))
        requested_limit = int(limit)
        limit = max(SEARCH_LIMIT_MIN, min(requested_limit, SEARCH_LIMIT_MAX))
        scope = {"query": query, "accountIDs": list(account_ids),
                 "chatIDs": list(chat_ids), "sender": sender, "chatType": chat_type}
        digest = scope_digest(scope)
        raw_cursor = None
        if cursor:
            try:
                got_direction, got_digest, raw_cursor = decode_cursor(cursor)
            except ValueError as exc:
                return self._stamp(O.Outcome.permanent(
                    f"cursor rejected: {exc}", reason="malformed_cursor"))
            if got_digest != digest:
                return self._stamp(O.Outcome.permanent(
                    "cursor belongs to a different query; refusing to resume across "
                    "scopes (O06 says the cursor is opaque and does not say what a foreign "
                    "cursor does)", reason="cursor_scope_mismatch"))
            direction = got_direction
        pages = 0
        items: list = []
        chats: dict = {}
        has_more = None
        last_next = None
        gap = None
        outcome_code = O.SUCCESS
        while True:
            params = self._search_params(query=query, account_ids=account_ids,
                                         chat_ids=chat_ids, sender=sender, limit=limit,
                                         cursor=raw_cursor, direction=direction,
                                         include_low_priority=include_low_priority,
                                         chat_type=chat_type)
            got = self.transport.call("search", params=params)
            if not got.usable:
                if pages:
                    # Part of the sweep answered; report what it held and why it stopped
                    # rather than discarding the pages that did answer.
                    gap = f"{got.code}: {got.detail}"
                    outcome_code = O.PARTIAL
                    break
                return self._stamp(got)
            pages += 1
            document = (got.data or {}).get("document") or {}
            page_items = document.get("items") or []
            for item in page_items:
                items.append(self._project_message(item if isinstance(item, dict) else {}))
            page_chats = document.get("chats") or {}
            if not isinstance(page_chats, dict):
                page_chats = {}
            for chat_id, chat in page_chats.items():
                chats[str(chat_id)] = self._project_chat(chat if isinstance(chat, dict)
                                                         else {})
            has_more = bool(document.get("hasMore"))
            raw_newest = document.get("newestCursor")
            raw_oldest = document.get("oldestCursor")
            raw_cursor = (raw_newest if direction == "after" else raw_oldest) or raw_oldest \
                or raw_newest
            last_next = raw_cursor
            if not has_more:
                break
            if not sweep or pages >= self.max_pages:
                break
        reached_end = has_more is False
        next_cursor = (encode_cursor(direction, digest, last_next)
                       if (has_more and last_next) else None)
        if not reached_end and gap is None:
            gap = HISTORY_LIMITATION
        data = {
            "items": items,
            "item_count": len(items),
            "chats": chats,
            "chat_count": len(chats),
            "has_more": bool(has_more),
            "pages_read": pages,
            "next_cursor": next_cursor,
            "cursor_used": bool(cursor),
            "direction": direction,
            "limit_requested": requested_limit,
            "limit_sent": limit,
            "limit_clamped": limit != requested_limit,
            "limit_max_documented": SEARCH_LIMIT_MAX,
            "parameters_sent": sorted(self._search_params(
                query=query, account_ids=account_ids, chat_ids=chat_ids, sender=sender,
                limit=limit, cursor=None, direction=direction,
                include_low_priority=include_low_priority, chat_type=chat_type)),
            "exclude_low_priority_sent": include_low_priority,
            "low_priority_note": ("O06 documents excludeLowPriority with 'Default: true'; "
                                  "the parameter is not sent unless the caller asks for the "
                                  "documented override, and this document never asserts the "
                                  "default as an observation"),
            "messages": "identifiers, timestamps and fingerprints only — no message text "
                        "is included in a read document",
            "coverage": {
                "coverage_state": ("complete" if reached_end else O.COVERAGE_PARTIAL_HISTORY),
                "gap_reason": gap,
                "reached_end_of_query": reached_end,
                "sweep_bounded_at_pages": self.max_pages,
                "note": NO_COMPLETENESS_CLAIM,
            },
            "oldest_observed": min([i["timestamp"] for i in items if i.get("timestamp")],
                                   default=None),
            "newest_observed": max([i["timestamp"] for i in items if i.get("timestamp")],
                                   default=None),
            "source_scope": {"query": query, "account_ids": list(account_ids),
                             "chat_ids": list(chat_ids), "sender": sender},
            "documented_parameters": list(DOCUMENTED_SEARCH_PARAMS),
        }
        if outcome_code == O.PARTIAL:
            return self._stamp(O.Outcome.partial(data, gap or "partial sweep",
                                                reason="partial_history",
                                                account_id=None))
        if not reached_end:
            return self._stamp(O.Outcome.partial(data, gap, reason="partial_history"))
        return self._stamp(O.Outcome.ok(data))

    # -- the asks the pack does not answer ---------------------------------
    def accounts(self) -> O.Outcome:
        from .beeper_transport import UNDOCUMENTED_ASKS, UNDOCUMENTED_NEXT_ACTION
        return self._stamp(O.Outcome.unsupported(
            UNDOCUMENTED_ASKS["accounts"] + ". This adapter will not invent one.",
            reason="endpoint_not_in_pack", next_action=UNDOCUMENTED_NEXT_ACTION))

    def chats(self) -> O.Outcome:
        from .beeper_transport import UNDOCUMENTED_ASKS, UNDOCUMENTED_NEXT_ACTION
        return self._stamp(O.Outcome.unsupported(
            UNDOCUMENTED_ASKS["chats"] + ". Use chats_from_search for the chats a search "
            "page actually referenced.", reason="endpoint_not_in_pack",
            next_action=UNDOCUMENTED_NEXT_ACTION))

    def messages_in_chat(self, chat_id: str, **kw: Any) -> O.Outcome:
        if not chat_id:
            return self._stamp(O.Outcome.permanent(
                "no chat id given: the documented search filter is chatIDs (O06)",
                reason="no_chat_id"))
        return self.search(chat_ids=[chat_id], **kw)

    def list_messages(self, **kw: Any) -> O.Outcome:
        """Message listing with a bounded, resumable cursor: search with no query."""
        return self.search(**kw)

    def chats_from_search(self, **kw: Any) -> O.Outcome:
        """The documented ``chats`` map from one search page (O06).

        Named for what it is: O06 says the map is "chats referenced in items" and its own
        ``do_not_use`` forbids citing it as a chats listing.
        """
        got = self.search(**kw)
        if not got.usable:
            return got
        data = dict(got.data or {})
        data["what_this_is"] = ("the 'chats' map from one search response, i.e. chats "
                                "referenced in that page's items (O06), not a chats "
                                "listing")
        return got

    def contacts(self, account_id: str, *, limit: int = 50, cursor: Optional[str] = None,
                 direction: str = "before", query: Optional[str] = None) -> O.Outcome:
        """``GET /v1/accounts/{accountID}/contacts/list`` (O12)."""
        if not account_id:
            return self._stamp(O.Outcome.unsupported(
                "the documented contacts path needs an accountID (O12: GET "
                "/v1/accounts/{accountID}/contacts/list); no Beeper account id was given. "
                "The pack names no endpoint that lists accounts, so this adapter cannot "
                "discover one for you.",
                reason="no_account_id",
                next_action=("read the discovery URLs GET /v1/info advertises (O11) on the "
                             "Mac, or pass the account id from Beeper Desktop's own UI")))
        if direction not in DIRECTIONS:
            return self._stamp(O.Outcome.permanent(
                f"direction {direction!r} is not documented: O12 names \"after\"/\"before\"",
                reason="bad_direction"))
        requested_limit = int(limit)
        limit = max(CONTACTS_LIMIT_MIN, min(requested_limit, CONTACTS_LIMIT_MAX))
        params: dict = {"limit": limit}
        if query:
            params["query"] = query
        if cursor:
            params["cursor"] = cursor
            params["direction"] = direction
        # O12's path is a template: the account id is a *path* parameter, not a query
        # parameter (putting it in ``params`` would send an undocumented query field and
        # still leave the placeholder on the wire).
        got = self.transport.call("contacts", params=params,
                                  path_params={"accountID": account_id})
        if not got.usable:
            return self._stamp(got, account_id=account_id)
        document = (got.data or {}).get("document") or {}
        raw_items = document.get("items") or []
        items = [self._project_contact(item if isinstance(item, dict) else {})
                 for item in raw_items]
        unnamed = [i for i in items if not (i.get("full_name") or i.get("username")
                                            or i.get("identifier_fingerprint"))]
        data = {k: v for k, v in (got.data or {}).items() if k != "document"}
        data.update({
            "account_id": account_id,
            "items": items,
            "item_count": len(items),
            "items_without_a_usable_identity_field": len(unnamed),
            "has_more": bool(document.get("hasMore")),
            "newest_cursor": bool(document.get("newestCursor")),
            "oldest_cursor": bool(document.get("oldestCursor")),
            "limit_requested": requested_limit,
            "limit_sent": limit,
            "limit_clamped": limit != requested_limit,
            "parameters_sent": sorted(params),
            "documented_parameters": ["cursor", "direction", "limit", "query"],
            "addresses_masked": True,
            "image_urls_withheld": True,
            "note": ("O12 documents the identity fields; addresses are masked and image "
                     "URLs are reduced to a presence flag because O12 says imgURL \"may be "
                     "temporary or available only on this device\""),
        })
        if document.get("hasMore"):
            return self._stamp(O.Outcome.partial(
                data, "more contacts remain (hasMore true); this page is not the whole "
                      "list", reason="partial_history", account_id=account_id),
                from_read=got)
        return self._stamp(O.Outcome.ok(data, account_id=account_id), from_read=got)

    # -- projections -------------------------------------------------------
    @staticmethod
    def _project_message(item: dict) -> dict:
        text = item.get("text")
        return {
            "source_message_id": item.get("id"),
            "account_id": item.get("accountID"),
            "chat_id": item.get("chatID"),
            "sender_id_fingerprint": (O.fingerprint(str(item["senderID"]))
                                      if item.get("senderID") else None),
            "sender_name": item.get("senderName"),
            "sort_key": item.get("sortKey"),
            "timestamp": item.get("timestamp"),
            "type": item.get("type"),
            "text_state": ("present" if isinstance(text, str) and text
                           else ("empty" if isinstance(text, str) else "absent")),
            "text_length": len(text) if isinstance(text, str) else None,
            "text_fingerprint": (O.fingerprint(text) if isinstance(text, str) and text
                                 else None),
            "attachment_count": len(item.get("attachments") or []),
            "send_status": ((item.get("sendStatus") or {}).get("status")
                            if isinstance(item.get("sendStatus"), dict) else None),
            "documented_fields_seen": sorted(k for k in DOCUMENTED_MESSAGE_FIELDS
                                             if k in item),
            "undocumented_fields_seen": sorted(k for k in item
                                               if k not in DOCUMENTED_MESSAGE_FIELDS),
        }

    @staticmethod
    def _project_chat(chat: dict) -> dict:
        merge = chat.get("merge") if isinstance(chat.get("merge"), dict) else None
        return {
            "source_chat_id": chat.get("id"),
            "account_id": chat.get("accountID"),
            "network": chat.get("network"),
            "title": chat.get("title"),
            "type": chat.get("type"),
            "unread_count": chat.get("unreadCount"),
            "local_chat_id_present": bool(chat.get("localChatID")),
            "is_merged": bool(merge),
            "merged_chat_ids_count": len((merge or {}).get("chatIDs") or []) if merge else 0,
            "merged_into_chat_id_present": bool(chat.get("mergedIntoChatID")),
            "documented_fields_seen": sorted(k for k in DOCUMENTED_CHAT_FIELDS if k in chat),
            "note": ("a merged chat holds no messages of its own (O06): read the member "
                     "chats, and record the chat id addressed as well as the chat that "
                     "answered"),
        }

    @staticmethod
    def _project_contact(item: dict) -> dict:
        identifier = item.get("id")
        return {
            "identifier_fingerprint": (O.fingerprint(str(identifier))
                                       if identifier else None),
            "identifier_shape_recorded": False,
            "full_name": item.get("fullName"),
            "username": item.get("username"),
            "email_masked": O.mask_address(str(item["email"])) if item.get("email") else None,
            "phone_masked": mask_phone(item.get("phoneNumber")),
            "is_self": item.get("isSelf"),
            "cannot_message": item.get("cannotMessage"),
            "image_url_present": bool(item.get("imgURL")),
            "documented_fields_seen": sorted(k for k in DOCUMENTED_CONTACT_FIELDS
                                             if k in item),
            "note": ("O12 documents these identity fields; O14 records that a Contacts "
                     "identifier is device-local, so a later identity link must keep the "
                     "device alongside the identifier"),
        }

    # -- manifest / health -------------------------------------------------
    def manifest(self, probe_rows: Optional[list] = None) -> dict:
        from .probe import CAPABILITIES
        measured = {row["capability"]: row for row in (probe_rows or [])}
        capabilities = {}
        for capability in CAPABILITIES:
            if capability.source != self.name:
                continue
            row = measured.get(capability.name)
            if row is not None:
                capabilities[capability.name] = {
                    "supported": bool(row["supported"]), "state": row["state"],
                    "permission_state": row["permission_state"],
                    "observed_version": row["observed_version"],
                    "limitation": row["limitation"], "probe_method": row["probe_method"],
                    "probe_assertion": row["probe_assertion"], "source": capability.source,
                    "citations": list(row.get("citations", ())),
                    "origin": row.get("origin"),
                }
                continue
            if capability.source == self.name:
                capabilities[capability.name] = {
                    "supported": False, "state": "unverified",
                    "permission_state": O.PERMISSION_NOT_DETERMINED,
                    "observed_version": O.VERSION_NOT_OBSERVED,
                    "limitation": ("not yet probed on this host — run `switchboard-mini "
                                   "probe` and record its output (Gate 2)"),
                    "probe_method": capability.probe_method,
                    "probe_assertion": capability.probe_assertion,
                    "source": capability.source, "citations": list(capability.citations),
                }
        return {
            "adapter": self.name, "adapter_version": self.version,
            "manifest_version": MANIFEST_VERSION, "host_role": self.host_role,
            "origin": self.origin, "adapter_is_real": self.adapter_is_real,
            "real_source_connected": False, "source_contacted": False,
            "base_url": self.base_url,
            "measured_capabilities": list(MEASURED_CAPABILITIES),
            "capabilities_not_in_this_adapter": (
                "sending, drafting, focus/composer, asset download and the live event "
                "stream are not implemented in this read-only slice and are refused by "
                "this adapter"),
            "capabilities": capabilities,
        }

    def health(self) -> O.Outcome:
        got = self.info()
        if not got.usable:
            return got
        data = dict(got.data or {})
        data.update({"health_state": "current",
                     "note": ("reachable is not complete and not send capability: this "
                              "adapter holds no send path at all (PRD §10)")})
        return self._stamp(O.Outcome.ok(data), from_read=got)

    # -- deliberately absent: the mutations ---------------------------------
    def _absent(self, operation: str, detail: str) -> O.Outcome:
        return self._stamp(O.Outcome.unsupported(
            detail, reason="not_in_this_read_only_slice",
            next_action=(f"{operation} is a later Gate 3 slice with its own approval "
                         "contract and reconciliation (PRD §10, R12–R16)")))

    def prepare_draft(self, account: str, request: dict) -> O.Outcome:
        return self._absent("prepare_draft",
                            "this slice has no draft path: nothing here writes to Beeper "
                            "Desktop")

    def dispatch(self, account: str, request: dict) -> O.Outcome:
        return self._absent("dispatch",
                            "no outbound path exists in this adapter: sending through "
                            "Beeper is not implemented, so nothing can be sent from it")

    def reconcile(self, account: str, *, idempotency_key: str,
                  source_operation_id: Optional[str] = None) -> O.Outcome:
        return self._absent("reconcile",
                            "nothing can have been sent by this adapter, so there is "
                            "nothing to reconcile (O09's returned pending message id is a "
                            "later slice)")

    def materialize_attachment(self, account: str, namespaced_id: str, **kw: Any) -> O.Outcome:
        return self._absent("materialize_attachment",
                            "downloading attachment bytes needs the upload/download "
                            "endpoint that Appendix B does not include (O10)")

    def focus_composer(self, *args: Any, **kw: Any) -> O.Outcome:
        return self._absent("focus_composer", "the focus/composer surface (O08) is not "
                                             "implemented in this read-only slice")

    def events(self, *args: Any, **kw: Any) -> O.Outcome:
        return self._absent("events", "the live event stream (O07) is not implemented in "
                                      "this read-only slice")


# ------------------------------------------------------------------ factories --

def build_adapter(*, fixture_mode: bool = False, fixture_scenario: str = "healthy",
                  base_url: Optional[str] = None, timeout_s: int = 10,
                  stand_in: bool = False, max_pages: int = DEFAULT_MAX_PAGES,
                  opener=None) -> BeeperReadOnlyAdapter:
    """The one place that decides real versus recorded for the Beeper adapter."""
    transport = build_transport(fixture_mode=fixture_mode,
                                fixture_scenario=fixture_scenario, base_url=base_url,
                                timeout_s=timeout_s, stand_in=stand_in, opener=opener)
    return BeeperReadOnlyAdapter(transport, max_pages=max_pages)


__all__ = ["BeeperReadOnlyAdapter", "build_adapter", "MEASURED_CAPABILITIES",
           "DOCUMENTED_MESSAGE_FIELDS", "DOCUMENTED_CONTACT_FIELDS", "mask_phone",
           "encode_cursor", "decode_cursor", "scope_digest", "NAMESPACE"]
