"""Capabilities of the three sources this worker cannot reach yet, recorded from the
Gate 2 probe pack — **documentation only**.

Why this module exists
----------------------
`probe.py` used to emit rows for Mail only, so Beeper, Contacts and Hermes had no
capability rows at all. A real measurement of Randy's Mac cannot produce rows for a
source the worker has no adapter for, but the *questions* Gate 2 must answer are known:
they were read out of the vendor pages and recorded in
``/home/team/shared/probe-pack/O01..O22`` (see ``00-TEMPLATE.md`` for the record
contract). This module carries those questions as data so the probe can emit one honest
row per capability — and so not one of them can be mistaken for a measurement.

The rules this module obeys (probe-pack §1 "five sentences", rules 2 and 3):

* **A documentation read can never set ``supported: true``.** Every row sourced here is
  ``origin: 'documentation'``, ``supported: false``, ``state: 'unmeasured'``,
  ``real_source_connected: false``, ``values_from_source: false``. ``probe._row``
  refuses to build a documentation row that claims otherwise, and Grace refuses to
  store one (see ``grace/contracts.py::probe_row_supported_claim_allowed``).
* **Never invent a name.** Every endpoint, parameter, response field and API symbol
  written here is copied from the cited record, which copied it from the page. Where the
  page was silent the record says ``not stated``, and the silence is recorded here as an
  explicit **absence** (``absences``) rather than filled in.
* **No secrets, no real data.** No token, vault path, item name, address or message
  content appears here. Credential *mechanisms* are named; their values never are.

The probe pack's correction that matters for this file: PRD line 296 is the
external-side-effects/retry sentence, the secret-handling lines are 291 and 293, and the
fail-closed requirement is the last sentence of **253**. The Hermes rows are written
against those real line numbers.

What only Randy's Mac can settle is in ``procedure`` — the numbered, copy-pasteable,
read-only-first measurement for each row, taken from the pack records. Running it is what
turns an ``unmeasured`` row into an ``unsupported``/``permission_denied``/… row or, if the
capability really works on his build, into a ``supported`` one **on a run that observed
it**.
"""

from __future__ import annotations

# The pack refs this module is allowed to cite. A row citing anything else is a defect,
# and ``tests/test_mini_probe.py`` asserts every citation is in this set.
PACK_REFS = tuple(f"O{n:02d}" for n in range(1, 23))

#: Where each source's documentation lives, for reference in reports.
PACK_PATH = "/home/team/shared/probe-pack"


def _cap(name, title, source, citations, probe_method, probe_assertion, limitation,
         documented=(), absences=(), procedure=()):
    return {
        "name": name,
        "title": title,
        "source": source,
        "citations": tuple(citations),
        "probe_method": probe_method,
        "probe_assertion": probe_assertion,
        "limitation": limitation,
        "documented": tuple(documented),
        "absences": tuple(absences),
        "procedure": tuple(procedure),
    }


DOCUMENTED_CAPABILITIES: tuple = (
    # ------------------------------------------------------------- Beeper ------
    _cap(
        name="beeper_local_api_reachability",
        title="Beeper Desktop local API is reachable and answers read calls",
        source="beeper",
        citations=("O05", "O11"),
        probe_method="GET /v1/info and one account/chats read against the local Beeper "
                     "Desktop API (documented example host http://localhost:23373), with "
                     "Beeper Desktop running",
        probe_assertion="the local API answers a bearer-authenticated read on this "
                        "install: an HTTP status and a JSON body with the documented keys, "
                        "observed from that install",
        limitation="unmeasured on every host so far: this worker has no Beeper adapter, so "
                   "nothing was contacted. The pack is a documentation read (O05, O11) and "
                   "O05 states no base URL or port, no authentication detail and no "
                   "endpoint list",
        documented=(
            "O05: 'Beeper Desktop API is a fully local API for all your chats across "
            "WhatsApp, Instagram, Telegram, ... and more'",
            "O05: 'Beeper Desktop API runs inside Beeper Desktop and requires Beeper "
            "Desktop to be running to be accessible.'",
            "O05: 'By default, it can't be accessed from other devices.'",
            "O11: 'Beeper Desktop API requires an access token for all endpoints.'",
            "O11: header form 'Authorization: Bearer <your_token>'; token created in "
            "Settings -> Integrations under 'Approved connections'",
            "O11: 'GET /v1/info returns metadata about the active Desktop API server and "
            "exposes endpoint URLs for discovery.'",
            "O06/O08/O09/O10/O12 documented example host: http://localhost:23373",
        ),
        absences=(
            "O05 states no base URL and no port (counts on that page: 'localhost' 0, "
            "'23373' 0)",
            "O11 states no scopes, expiry, revocation or rotation, and no "
            "transport-security statement ('https' 0, 'TLS' 0)",
            "no page in the pack states which Desktop release introduced the API "
            "('version' 0 on O05, O08, O09, O10, O11, O12)",
            "no numeric rate limit is documented for the API as a whole (O05 says only "
            "that sending too many messages may cause network-side suspension)",
        ),
        procedure=(
            "Record the installed Beeper Desktop version (About panel) and whether a "
            "Desktop API section appears in Settings -> Integrations (O05 step 1).",
            "Create/confirm a token in the app; record yes/no only — never copy the token "
            "value into any file (O11 step 2).",
            "With Desktop running: curl -sS -o /tmp/beeper-info.json -w \"HTTP:%{http_code}\\n\" "
            "-H \"Authorization: Bearer $BEEPER_ACCESS_TOKEN\" http://localhost:23373/v1/info "
            "— record the status and the JSON *keys* only (O11 step 3).",
            "Repeat without the Authorization header and record the status code; the page "
            "does not state whether an unauthenticated request is refused (O11 step 4).",
            "With Beeper Desktop quit, make one call and record the exact error/behaviour "
            "— that is the 'offline' state the worker must surface (O05 step 6).",
        ),
    ),
    _cap(
        name="beeper_message_search",
        title="Search message history across chats",
        source="beeper",
        citations=("O06",),
        probe_method="GET /v1/messages/search with query/accountIDs/chatIDs/limit, run "
                     "against the local API with a word known to be present in a message",
        probe_assertion="a documented search call returns matching items on this install, "
                        "with the fields the page documents (items, hasMore, newestCursor, "
                        "oldestCursor) observed in the response",
        limitation="unmeasured: no Beeper adapter exists in this worker. O06 documents the "
                   "call but makes no completeness claim, so a search result may never be "
                   "reported as proof of complete ingestion",
        documented=(
            "O06: 'GET /v1/messages/search' — 'Search messages across chats.'",
            "O06 query parameters as written: accountIDs, chatIDs, chatType, cursor, "
            "dateAfter, dateBefore, direction, excludeLowPriority (default true), "
            "includeMuted (default true), limit (maximum 20), mediaTypes, query "
            "(\"Literal word search. Finds messages containing these words in any "
            "order.\"), sender",
            "O06 response fields as written: chats, hasMore, items, newestCursor, "
            "oldestCursor",
            "O06 Message fields as written: id, accountID, chatID, senderID, senderName, "
            "sortKey, timestamp, text, type, attachments, sendStatus",
        ),
        absences=(
            "O06 makes no completeness claim ('complete' 0, 'completeness' 0, 'indexed' 0) "
            "— so PRD line 242's 'do not use a search result as proof of complete "
            "ingestion' is our rule, not a Beeper promise",
            "O06 does not state the default account scope when accountIDs is omitted",
            "O06 states no ranking/relevance semantics and no rate limit or latency",
            "the word 'semantic' does not appear on O06 ('semantic' 0)",
        ),
        procedure=(
            "One search with query set to a word present in a known message, no other "
            "parameters; record HTTP status, items length, and whether the known message "
            "appears (O06 step 1).",
            "Repeat with two words and with a two-word phrase; record whether 'words in "
            "any order' holds (O06 step 2).",
            "Record the default account scope: search a word that exists only in account B "
            "while omitting accountIDs (O06 step 4).",
            "For a merged chat: record the merged chat ID, merge.chatIDs (masked) and "
            "defaultChatID, and whether a search inside the merged chat ID returns "
            "anything (O06 step 5).",
        ),
    ),
    _cap(
        name="beeper_history_depth",
        title="How far back Beeper's local history reaches",
        source="beeper",
        citations=("O05",),
        probe_method="compare the oldest message timestamp the API can reach for one "
                     "account with the oldest message visible for the same account in the "
                     "Beeper Desktop UI",
        probe_assertion="a number: the oldest reachable timestamp from the API, recorded "
                        "beside the oldest visible in the UI for the same account, so a "
                        "history gap is measurable rather than described",
        limitation="unmeasured: no Beeper adapter exists in this worker; O05 states the "
                   "limit only in words, with no number",
        documented=(
            "O05: 'Message history might be limited. Beeper indexes your messages from the "
            "networks in the background, when you first add an account, only recent "
            "messages might be available.'",
            "O05: 'For best results, prefer using On-Device Connections instead of Beeper "
            "Cloud when connecting accounts.'",
            "O05: 'iMessage is only supported on macOS.'",
            "O06/O07 mention no history window; O05 does not state per-network windows",
        ),
        absences=(
            "no page in the pack states a history window per network, an indexing "
            "duration, or how to detect that history is truncated",
        ),
        procedure=(
            "For one account, record the oldest message timestamp the API can reach "
            "versus the oldest visible in the Desktop UI — a number, not a phrase "
            "(O05 step 3).",
            "Record verbatim from the UI whether each account is an On-Device Connection "
            "or Beeper Cloud (O05 step 4).",
            "Record whether an iMessage account exists and is readable (O05 step 5).",
        ),
    ),
    _cap(
        name="beeper_send_message",
        title="Sending a message returns a pending ID and the chat actually routed to",
        source="beeper",
        citations=("O09",),
        probe_method="POST /v1/chats/{chatID}/messages with a trivial text body to a "
                     "consented test chat, recording the response fields only",
        probe_assertion="a send on this install returns both documented response fields — "
                        "pendingMessageID and the chatID the send was actually routed to — "
                        "so a submission is attributable before the network confirms it",
        limitation="unmeasured: this worker has no send path at all and no Beeper adapter, "
                   "so nothing was sent anywhere. The row records what the send response "
                   "must be measured to contain, never that sending works",
        documented=(
            "O09: 'POST /v1/chats/{chatID}/messages' — 'Send a text message to a specific "
            "chat. Supports replying to existing messages. Returns a pending message ID.'",
            "O09 response field chatID: 'Chat the message was actually sent to. When "
            "sending to a merged chat, this is the member chat the send was routed to.'",
            "O09 response field pendingMessageID: 'Pending ID assigned to the message "
            "before the network confirms the send. Pass it to GET /v1/chats/{chatID}/"
            "messages/{messageID} to resolve, or wait for the matching message.upserted "
            "over the WebSocket.'",
            "O09 body parameters as written: attachment (single object with uploadID, "
            "duration, fileName, mimeType, size, type), replyToMessageID, text",
            "O06 merge semantics: 'A merged chat holds no messages of its own - read "
            "messages from the member chats, and send either to a member directly or to "
            "the merged chat ID to route automatically.'",
        ),
        absences=(
            "O09 documents no idempotency key and no deduplication rule for a retried "
            "send; the only pre-confirmation identifier is pendingMessageID, assigned "
            "after the request",
            "O09 states no lifetime for pendingMessageID ('expire' 0, 'timeout' 0, "
            "'retry' 0)",
            "O09 documents no failure model: only a 200 example, no status codes, no error "
            "body shape, no rate-limit response ('error' 0, 'status' 0, '429' 0)",
            "O09 states no per-chat capability check for attachments ('capabilit' 0 on "
            "that page; the model is on O06), and no size or MIME limits",
            "nothing on O09 says a send requires an approval or a durable draft; the "
            "'Draft text' wording describes the body being sent now",
        ),
        procedure=(
            "In a consented test chat, POST /v1/chats/{chatID}/messages with a trivial "
            "text body; record the HTTP status and the JSON *field names* returned "
            "(do not copy message content into any file) (O09 step 1).",
            "Record whether the response carries pendingMessageID and what chatID the "
            "send was routed to, comparing it with the chat ID that was requested "
            "(O09 step 2).",
            "Send to a *merged* chat ID and record whether the returned chatID is a member "
            "chat rather than the merged ID — this is the merged-chat routing trap "
            "(O09 step 6).",
            "Create one throwaway run of each failure shape the page does not document "
            "(no such chat, network offline) and record the exact status/body: only a "
            "measurement can supply the failure model O09 omits.",
        ),
    ),
    _cap(
        name="beeper_send_reconciliation",
        title="Resolving a pending send to a confirmed message",
        source="beeper",
        citations=("O09", "O07"),
        probe_method="reconcile the pendingMessageID from a probe send through "
                     "GET /v1/chats/{chatID}/messages/{messageID}, and separately observe "
                     "whether a matching message.upserted arrives on "
                     "ws://localhost:23373/v1/ws",
        probe_assertion="a pending send on this install resolves to a confirmed message "
                        "through one of the two documented paths, so an uncertain outcome "
                        "can be reconciled instead of retried blindly",
        limitation="unmeasured: nothing has been sent from this worker and no Beeper "
                   "adapter exists, so there is nothing to reconcile. The evidence Gate 2 "
                   "needs is named here; it has to be observed on the Mac",
        documented=(
            "O09 names the two resolution paths: 'GET /v1/chats/{chatID}/messages/"
            "{messageID}' and 'the matching message.upserted over the WebSocket'",
            "O07: 'Beeper Desktop API exposes a live WebSocket endpoint at: "
            "ws://localhost:23373/v1/ws'; 'Use the same token as HTTP requests: "
            "Authorization: Bearer <your_token>'",
            "O07 event fields as written: type, seq ('monotonic per-connection sequence "
            "number'), ts, chatID, ids, entries",
            "O07 control messages as written: ready, subscriptions.updated, error; "
            "subscription command subscriptions.set, with chatIDs ['*'] or an explicit "
            "list",
            "O06 Message field sendStatus as written: {status, timestamp, deliveredToUsers, "
            "internalError, message, reason} with status 'SUCCESS' | 'PENDING' | "
            "'FAIL_RETRIABLE' | 'FAIL_PERMANENT'",
        ),
        absences=(
            "O09 does not say whether pendingMessageID equals the final message ID, how "
            "long it stays valid, or what a caller sees if the network never confirms",
            "O07 documents no reconnect, resume, replay, backlog or catch-up behaviour of "
            "any kind ('reconnect' 0, 'replay' 0, 'backlog' 0, 'recovery' 0, 'durable' 0, "
            "'persist' 0)",
            "O07: message.upserted events are hydrated best-effort and 'If the message is "
            "not yet retrievable, the event is skipped.' — a dropped event is not "
            "documented as detectable",
            "O07 states no edits/deletions history guarantee and no delivery "
            "acknowledgement for domain events",
        ),
        procedure=(
            "Keep the pendingMessageID from the send probe; poll GET /v1/chats/{chatID}/"
            "messages/{messageID} until it resolves or a bounded time elapses; record "
            "whether it resolved, how long it took, and whether the ID changed (O09 "
            "step 5).",
            "Separately, with the WebSocket open on ws://localhost:23373/v1/ws, repeat one "
            "send and record whether a matching message.upserted arrives and what its "
            "seq/ids/entries carry (O07 step 2).",
            "Disconnect deliberately for a few minutes while a consented test message "
            "arrives, reconnect and re-subscribe; record whether the missed event is "
            "delivered at all, then confirm the message is visible through the read API "
            "(O07 step 3) — expected per O07: no backlog, so no replay.",
        ),
    ),
    _cap(
        name="beeper_attachment_materialisation",
        title="Downloading an attachment to a local file",
        source="beeper",
        citations=("O10", "O09", "O06"),
        probe_method="POST /v1/assets/download with a documented mxc:// (or localmxc://) "
                     "URL and record the returned srcURL and whether the file is readable "
                     "by a plain process on the Mac",
        probe_assertion="an attachment referenced by an mxc:// or localmxc:// URL is "
                        "materialised to a local file path by this install, and the path "
                        "is readable by the worker process",
        limitation="unmeasured: this worker has no Beeper adapter and materialises no "
                   "bytes. Attachment *sending* needs an uploadID from an 'uploadAsset' "
                   "endpoint that is not among the Appendix B references at all, so that "
                   "half of the path has no sourced evidence",
        documented=(
            "O10: 'POST /v1/assets/download' — 'Download a file from an mxc:// or "
            "localmxc:// URL to the device running the Beeper Client API and return the "
            "local file URL.'",
            "O10 body parameter url: 'Beeper media URL (mxc:// or localmxc://) for the "
            "file to download.'; response fields error and srcURL ('Local file URL to the "
            "downloaded file.')",
            "O10 method note: this is a POST with a JSON body (Content-Type: "
            "application/json), not a GET with a query string",
            "O09 attachment.uploadID: 'Upload ID from uploadAsset endpoint. Required to "
            "reference uploaded files.'",
            "O06 Attachment field id: 'Attachment identifier, typically an mxc:// URL. "
            "Use the download file endpoint to get a local file path.'",
        ),
        absences=(
            "the 'uploadAsset' endpoint is named on O09/O10 but its path is not stated on "
            "either page, and no Appendix B reference covers it — attachment *upload* is "
            "unsourced",
            "O10 states no size bound and no lifetime for the downloaded file ('maxSize' "
            "0, 'expiry'/'expire' 0, 'ttl' 0)",
            "O10 states no failure taxonomy: 'error' exists as a field but no status codes "
            "and no statement whether a failed download still returns srcURL",
            "O10 documents no transfer path to Grace: no serving endpoint in this "
            "reference set, so a Mini-local path may not be handed to Grace",
        ),
        procedure=(
            "With Desktop running, download one attachment URL from a message the owner "
            "consents to test with; record the HTTP status and whether srcURL is a file: "
            "path (O10 step 1).",
            "Record whether the returned path is readable by a plain non-GUI process and "
            "its byte size class; do not copy bytes or content into any file (O10 "
            "step 2).",
            "Record the same file's mxc:// id and size *class* from the message metadata "
            "so the download can be matched to its attachment (O06/O10 step 3).",
        ),
    ),
    _cap(
        name="beeper_composer_prefill",
        title="Focusing the composer and prefilling text (not a durable draft)",
        source="beeper",
        citations=("O08",),
        probe_method="POST /v1/focus with chatID and draftText in a consented test chat, "
                     "then observe what happens to the prefilled text across a chat switch, "
                     "an app quit and a send",
        probe_assertion="the documented prefill call returns success: true on this "
                        "install and the text is observed in the composer — with what "
                        "survives recorded, since the page states no draft lifecycle",
        limitation="unmeasured: this worker has no Beeper adapter, and prefill is not a "
                   "draft: O08 documents no read, list, update, send or delete operation "
                   "for it, so it cannot back a durable draft or an approval bound to one",
        documented=(
            "O08: 'POST /v1/focus' — 'Focus Beeper Desktop and optionally open a specific "
            "chat, jump to a message, or pre-fill text and an image.'",
            "O08 body parameters as written: chatID, draftAttachmentPath ('Optional local "
            "image path to populate in the message input field.'), draftText ('Optional "
            "plain text to populate in the message input field.'), messageID",
            "O08 response field success: 'Whether the app was successfully opened/"
            "focused.'",
        ),
        absences=(
            "O08 documents no draft lifecycle at all ('durable' 0, 'persist' 0, 'save' 0, "
            "'list' 0, 'delete' 0, 'GET' 0): nothing says prefilled text survives a "
            "restart, a chat switch, an app quit or a send",
            "O08 never says the prefilled text is transmitted; 'populate in the message "
            "input field' describes filling the composer",
            "O08 states no error model: only a 200 example with {\"success\": true}; "
            "success: false is not described",
            "O08 says nothing about who owns the composer afterwards, or about two "
            "callers prefilling it in sequence — PRD §5's execution-context lease has no "
            "vendor statement behind it on this page",
        ),
        procedure=(
            "With Desktop running, POST /v1/focus with chatID and draftText in a consented "
            "test chat; record the status and the response body (O08 step 1).",
            "Switch chats and return; record whether the prefilled text is still there "
            "(O08 step 2).",
            "Quit Beeper Desktop, relaunch, and record whether any prefilled text "
            "survived; then record what happens to prefilled text if a send is performed "
            "(O08 step 3) — 'no durable draft' is confirmed or refuted by this, not "
            "assumed.",
        ),
    ),
    _cap(
        name="beeper_live_event_stream",
        title="Live event stream (experimental) and what it does not guarantee",
        source="beeper",
        citations=("O07",),
        probe_method="connect to ws://localhost:23373/v1/ws with the documented bearer "
                     "token, send subscriptions.set, and record the control frames and "
                     "event frames actually received",
        probe_assertion="the WebSocket endpoint accepts a subscription on this install "
                        "and delivers domain events with the documented fields (type, "
                        "seq, ts, chatID, ids, entries) — and the gap behaviour is "
                        "recorded rather than assumed",
        limitation="unmeasured: no Beeper adapter exists in this worker, so no socket was "
                   "opened. O07 is an experimental surface with no replay, backlog or "
                   "recovery behaviour, so it may never be treated as a durable event log",
        documented=(
            "O07: 'Beeper Desktop API exposes a live WebSocket endpoint at: "
            "ws://localhost:23373/v1/ws'",
            "O07 subscription command: subscriptions.set, 'to fully replace current "
            "subscriptions'; chatIDs ['*'] or an explicit list; ['*'] 'cannot be mixed "
            "with specific chat IDs'",
            "O07 control messages: ready ('{\"type\": \"ready\", \"version\": 1, "
            "\"chatIDs\": []}'), subscriptions.updated, error ('{\"type\": \"error\", "
            "\"requestID\", \"code\", \"message\"}')",
            "O07 domain event fields: type, seq ('monotonic per-connection sequence "
            "number'), ts, chatID, ids, entries ('optional best-effort full payload "
            "objects for message.upserted events')",
            "O07 current event names: chat.upserted, chat.deleted, message.upserted, "
            "message.deleted; 'Mutation types are normalized so both upsert and update "
            "sync mutations are delivered as <resource>.upserted events.'",
        ),
        absences=(
            "O07 documents no reconnect, replay, backlog or catch-up behaviour "
            "('reconnect' 0, 'replay' 0, 'backlog' 0, 'recovery' 0, 'durable' 0, "
            "'persist' 0)",
            "O07: 'message.upserted events are hydrated from the local message endpoint "
            "before emission ... If the message is not yet retrievable, the event is "
            "skipped.' — a dropped event is silent",
            "O07 states no retention, ordering or delivery-acknowledgement guarantee "
            "beyond 'monotonic per-connection'",
        ),
        procedure=(
            "Connect to ws://localhost:23373/v1/ws and send {\"type\":\"subscriptions.set\","
            "\"requestID\":\"probe-1\",\"chatIDs\":[\"*\"]}; record the ready and "
            "subscriptions.updated frames (version and chat-ID *count*, not the IDs) "
            "(O07 step 1).",
            "Record several message.upserted frames: their seq values, whether entries was "
            "present each time, and whether any event arrived without entries (O07 "
            "step 2).",
            "Disconnect for a few minutes while a consented test message arrives, "
            "reconnect, and record whether that event is delivered at all (O07 step 3).",
            "Send {\"type\":\"subscriptions.set\",\"requestID\":\"probe-2\",\"chatIDs\":[]} "
            "and record whether events stop (O07 step 4).",
        ),
    ),
    _cap(
        name="beeper_account_contacts",
        title="Per-account merged contacts (and what is not stated about identity)",
        source="beeper",
        citations=("O12",),
        probe_method="GET /v1/accounts/{accountID}/contacts/list with and without "
                     "cursor/limit/query; record the field-presence matrix and the counts "
                     "only",
        probe_assertion="the documented contacts call returns merged contacts for one "
                        "account on this install, with a stable user id and the "
                        "field-presence of email/phone/fullName observed as counts",
        limitation="unmeasured: no Beeper adapter exists in this worker. O12 scopes contact "
                   "merging per account and states nothing about how contacts from "
                   "different accounts relate to one person, so this cannot be used as an "
                   "identity-linking source without measurement",
        documented=(
            "O12: 'GET /v1/accounts/{accountID}/contacts/list' — 'List merged contacts for "
            "a specific account with cursor-based pagination.'",
            "O12 query parameters as written: cursor, direction, limit (minimum 1, maximum "
            "200), query",
            "O12 response fields as written: hasMore, items (array of User), newestCursor, "
            "oldestCursor",
            "O12 User.id: 'Stable Beeper user ID. Use as the primary key when referencing "
            "a person.'",
            "O12 User fields as written: id, cannotMessage, email ('Not guaranteed "
            "verified.'), fullName, imgURL, isSelf, phoneNumber (E.164), username ('May "
            "be network-specific and not globally unique.')",
        ),
        absences=(
            "O12 states nothing about cross-account identity linkage ('List merged "
            "contacts for a specific account'), no match/normalisation semantics, and "
            "nothing about comparing phone or email values",
            "O12 does not define what the query parameter matches (no 'literal' claim; "
            "counts on that page: 'literal' 0)",
            "O12 states no default page size ('limit' has no stated default) and no "
            "behaviour for an expired or foreign cursor",
            "no avatar durability or size guarantee for imgURL: 'May be temporary or "
            "available only on this device'",
        ),
        procedure=(
            "List accounts; record each account's ID shape (masked) and network name, and "
            "the count (O12 step 1).",
            "Call contacts/list for one account with no parameters; record the status, "
            "items length, hasMore, and the field-presence matrix as counts for email, "
            "phoneNumber, username, imgURL, fullName (O12 step 2).",
            "Page one step with limit, then with cursor + direction; record whether "
            "hasMore/newestCursor/oldestCursor behave as documented (O12 step 3).",
            "For one contact, record whether query finds it by full email, email "
            "local-part, phone with and without '+', and part of the display name (O12 "
            "step 4).",
            "Record whether the same person appears under two accounts and whether id "
            "differs or matches (O12 step 6).",
        ),
    ),
    # ----------------------------------------------------------- Contacts ------
    _cap(
        name="contacts_read_authorization",
        title="Contacts read authorization and a typed denial",
        source="contacts",
        citations=("O13", "O16"),
        probe_method="run the installed helper's Contacts step once with the declaration "
                     "in place, answer 'Don't Allow' first, and record verbatim what the "
                     "call reports and whether it blocked",
        probe_assertion="the declaration ships in the helper's Info.plist and a denial is "
                        "observable as a typed state this worker produces, on this Mac",
        limitation="unmeasured: no Contacts adapter exists in this worker and no helper "
                   "was run on a Mac. No page read in the pack names a queryable "
                   "permission-state API, so typed denial is ours to build and observe, "
                   "not something we inherit",
        documented=(
            "O16: key NSContactsUsageDescription, 'Privacy - Contacts Usage Description', "
            "Type String; 'This key is required if your app uses APIs that access the "
            "user's contacts.'",
            "O13: 'Users can grant or deny access to contact data on a per-app basis. Any "
            "call to CNContactStore blocks the app while asking the user to grant or deny "
            "access.'",
            "O13: 'the user receives a prompt only the first time an app requests access; "
            "all subsequent CNContactStore calls use the existing permissions'",
            "O13: requestAccess(for:completionHandler:) is named as the asynchronous "
            "alternative to the blocking call",
            "O13: NSContactsUsageDescription is the documented declaration key for "
            "Contacts access",
        ),
        absences=(
            "O16 names no runtime authorization call ('authoriz' 0, 'CNContactStore' 0, "
            "'requestAccess' 0) and no way to observe granted/denied",
            "O13 names no authorization-status query ('authoriz' 0, 'status' 0, no "
            "CNAuthorizationStatus, no authorizationStatus(for:)) — the status-query call "
            "is not named on any page read in the pack",
            "O16 never says 'Info.plist' ('Info.plist' 0); the only location signal is the "
            "breadcrumb 'Bundle Resources -> Information Property List'",
            "O16 does not say what happens if the key is missing for a macOS helper (the "
            "crash consequence is stated on O13, and there only for iOS apps linked on or "
            "after iOS 10)",
        ),
        procedure=(
            "Confirm the declaration ships: plutil -p \"$APP/Contents/Info.plist\" | grep -i "
            "-A1 ContactsUsageDescription; record the key name and the string's length only "
            "(O16 step 1).",
            "Record the bundle's CFBundleIdentifier and CFBundleShortVersionString/"
            "CFBundleVersion beside it (O16 step 2).",
            "First run, read-only: run the Contacts step, choose 'Don't Allow', and record "
            "the prompt text, whether the call blocked, and what the helper reports "
            "afterwards; then re-run and record whether the prompt reappears (O16 step 3).",
            "Then allow and repeat; record whether the prompt appears a second time (O16 "
            "step 4).",
            "Record where the permission entry appears (System Settings -> Privacy & "
            "Security -> Contacts) and whether the helper's name/bundle id is listed "
            "(O16 step 5).",
        ),
    ),
    _cap(
        name="contacts_enumerate_contacts",
        title="Enumerating contacts with a minimal key set",
        source="contacts",
        citations=("O13",),
        probe_method="fetch unified contacts from CNContactStore with a minimal keysToFetch "
                     "array on a background queue; record counts, never values",
        probe_assertion="a bounded fetch on this Mac returns contacts and the count of "
                        "contacts carrying a non-empty email address and phone number, "
                        "with only the requested keys fetched",
        limitation="unmeasured: no Contacts adapter exists in this worker, so no fetch has "
                   "run anywhere. O13 states that every fetched object is partial and that "
                   "reading an unfetched property raises, so the key set must be measured "
                   "before it can be trusted",
        documented=(
            "O13: 'You can fetch contacts using the contact store (CNContactStore), which "
            "represents the user's Contacts database.'",
            "O13: 'You can use keysToFetch to limit the contact properties that you "
            "fetch.' with CNKeyDescriptor as the key type",
            "O13 fetch calls named: CNContact.predicateForContacts(matchingName:), "
            "store.unifiedContacts(matching:keysToFetch:), "
            "store.unifiedContact(withIdentifier:keysToFetch:)",
            "O13: 'the Contacts framework doesn't support generic and compound predicates'",
            "O13: 'A partial contact results when the system fetches only some of a "
            "contact object's properties ... If you try to access a property value that "
            "the system didn't fetch, you get an exception.'",
            "O13: 'By default the Contacts framework returns unified contacts.'",
        ),
        absences=(
            "O13 names no required-minimum key set and no 'do not fetch X' list",
            "O13 states no rate limits and no performance figures ('latency' 0, 'rate' 0)",
            "O13 states no OS-version-dependent behaviour table beyond availability strings",
        ),
        procedure=(
            "Build a throwaway helper that fetches with CNContactGivenNameKey, "
            "CNContactFamilyNameKey, CNContactEmailAddressesKey, CNContactPhoneNumbersKey "
            "and CNContactIdentifierKey on a background queue (O13 step 2).",
            "Record whether the call blocked, the prompt's verbatim text, and the "
            "result/exception text (O13 step 2).",
            "Record counts only: total unified contacts, and how many have a non-empty "
            "email address and a non-empty phone number (O13 step 3).",
            "Record how the count changes when a key outside the minimal set is added, so "
            "the smallest sufficient set is measured rather than guessed (O13 step 5).",
        ),
    ),
    _cap(
        name="contacts_identifier_scope",
        title="CNContact identifier scope: device-local, not global",
        source="contacts",
        citations=("O14", "O13"),
        probe_method="record identifier counts, distinct-value counts and identifier "
                     "stability across two launches of the helper; compare a unified "
                     "identifier with the individual identifiers of the same person",
        probe_assertion="the identifiers this Mac returns behave as the page states "
                        "(persistable between app launches, unique on this device), and "
                        "the unified identifier is observed to differ from the "
                        "constituents', so the linking key can be named",
        limitation="unmeasured: no Contacts adapter exists in this worker. Apple states "
                   "twice that an identifier is device-local, so the linking key must be "
                   "(device/source installation, identifier) — never the identifier alone "
                   "— and that key has not been exercised against a real store",
        documented=(
            "O14: 'A value that uniquely identifies a contact on the device.'",
            "O14: 'It is recommended that you use the identifier when re-fetching the "
            "contact. An identifier can be persisted between the app launches. Note that "
            "this identifier only uniquely identifies the contact on the current "
            "device.'",
            "O13: 'Each fetched unified contact object (CNContact) has its own unique "
            "identifier that's different from any individual contact's identifier in the "
            "set of linked contacts. When refetching a unified contact, be sure to use its "
            "identifier.'",
        ),
        absences=(
            "O14 states no global or cross-device uniqueness ('global' 0, 'iCloud' 0, "
            "'sync' 0) — the only scope word is 'device'",
            "O14 states nothing about merge, deletion or rename changing an identifier "
            "('merge' 0, 'delete' 0, 'rename' 0, 'stable' 0, 'change' 0)",
            "O14 gives no format guarantee (declared type String; no UUID/opaque "
            "statement) and no uniqueness-test or resolve-identifier API",
            "O14 states nothing about how identifiers relate between a unified contact "
            "and its constituents ('unified' 0 on that page)",
        ),
        procedure=(
            "Fetch all contacts with the identifier key; record counts only: number of "
            "contacts, number of distinct identifier values, and identifier length "
            "(min/max) — no value that could identify a person (O14 step 1).",
            "Record identifiers for the same contact across two launches of the helper "
            "(quit between them) and how many values are identical (O14 step 2).",
            "Record whether a unified contact's identifier and the individual identifiers "
            "for the same person are all distinct, and whether the unified value appears "
            "among the individual values (O14 step 3).",
            "Store one identifier and resolve it with a refetch call; record 'resolved / "
            "not resolved' (O14 step 4).",
        ),
    ),
    _cap(
        name="contacts_unified_constituents",
        title="Reaching the individual constituent records of a unified contact",
        source="contacts",
        citations=("O13", "O14"),
        probe_method="attempt to enumerate the constituent records behind one unified "
                     "contact on a Mac (for example by fetching with unification turned "
                     "off) and record what the installed SDK names for it",
        probe_assertion="a documented, observable call on this Mac returns the individual "
                        "contact records that make up one unified contact — or the row "
                        "records that no such call was found, which is the honest answer",
        limitation="unmeasured, and deliberately so: no page read in the pack documents how "
                   "to reach an individual constituent record of a unified contact, so "
                   "this cannot be coded against. The name of the call has to come from "
                   "the installed SDK header on the Mac, verbatim",
        documented=(
            "O13: 'A unified contact is an in-memory, temporary view of the set of linked "
            "contacts that the system merges into one contact.'",
            "O13: 'You can automatically link contacts in different accounts that "
            "represent the same person.'",
            "O15: 'CNContactFetchRequest' is named in O13's fetch-request topic list, and "
            "'To fetch individual contact changes, set shouldUnifyResults to NO.' (the "
            "property that turns unification off for a fetch request is not documented on "
            "O13's page)",
        ),
        absences=(
            "O13 never names an API, property or key that returns the constituent set of a "
            "unified contact ('constituent' 0 occurrences)",
            "O13 never says what 'linked' is keyed on",
            "the property that disables unification on a fetch request is not documented "
            "on the pages read; O15 names shouldUnifyResults only for the change-history "
            "request",
        ),
        procedure=(
            "Take one contact that has two addresses from different accounts if one "
            "exists; fetch it with unification on and with unification off (O13 step 4).",
            "Record only: how many values came back, whether they are all distinct, and "
            "whether the unified value appears among the individual values (O13 step 4).",
            "Record the exact SDK name and signature used to turn unification off, taken "
            "from the installed header — the pack does not supply it (O13 step 4).",
        ),
    ),
    _cap(
        name="contacts_change_history",
        title="Change history: token, events, and an observable (not provocable) reset",
        source="contacts",
        citations=("O15",),
        probe_method="perform a change-history fetch with a persisted startingToken, then "
                     "with a token deliberately made invalid, and record the event classes "
                     "and order received",
        probe_assertion="a change-history fetch on this Mac returns the documented event "
                        "classes and a currentHistoryToken, and the drop-everything reset "
                        "sequence is observed when the token is invalid — the observable "
                        "half of T11",
        limitation="unmeasured: no Contacts adapter exists in this worker. A change-history "
                   "reset is observable but not provocable by us: the only documented "
                   "trigger is passing a nil/invalid/expired token, and the pack records "
                   "no way to invalidate a token on demand, so T11's reset leg stays a "
                   "measurement to run, not a behaviour we can assume",
        documented=(
            "O15: 'A change history fetch request efficiently returns a collection of "
            "change history events that describes the contacts and groups added to, "
            "deleted from, and updated in the Contacts database.'",
            "O15: 'If your token has a nil value, is invalid or expired, the fetch request "
            "returns a drop event followed by an add event for every contact and group in "
            "the Contacts database. The drop event indicates that apps should drop cached "
            "information. The token can be persisted between the app launches on the "
            "current device.'",
            "O15 visitor methods: visitDropEverythingEvent:, visitAddContactEvent:, "
            "visitUpdateContactEvent:, visitDeleteContactEvent: (with "
            "visitAddMemberToGroupEvent an example of the optional methods)",
            "O15 configuration defaults: includeGroupChanges default NO; mutableObjects "
            "default NO; shouldUnifyResults default YES",
            "O15: 'The Contacts framework limits the contact properties that you fetch to "
            "the unique identifier when executing a change history fetch request.'",
            "O15 execution: 'Call enumeratorForChangeHistoryFetchRequest:error: on an "
            "instance of CNContactStore to execute the change history fetch request.'",
            "O15: 'inspect the currentHistoryToken property of CNFetchResult ... Your app "
            "should save this token, then use it when fetching the next change history "
            "events.'",
            "O15: 'Transaction authors allow you to filter transactions returned in the "
            "fetched change history events. They don't provide any information about the "
            "author responsible for making a given change.'",
        ),
        absences=(
            "O15 states no way to ask whether a token is valid and no way to force a reset "
            "or a drop ('reset' 0, 'force' 0)",
            "O15 states nothing about what makes a token invalid or expired (no cause "
            "list: no database change, OS update, restore, permission change, container "
            "change or time limit)",
            "O15 documents no merge-specific change event (the T11 'contact is merged' "
            "scenario appears only as whatever add/update/delete combination the system "
            "emits, which the page does not describe)",
            "O15 gives no completeness guarantee for past revisions and no error taxonomy "
            "(only `NSError` in the sample); no notification-based change signal "
            "('notification' 0)",
            "the change-history execution path is documented as Objective-C only ('This "
            "method is unavailable in Swift.'), so the Swift-only helper has to be "
            "measured against the installed SDK",
        ),
        procedure=(
            "Read-only, first run: fetch change history with startingToken nil and record "
            "whether it succeeded, the number and order of events (drop first, then counts "
            "of add/update/delete), whether group events appear with includeGroupChanges "
            "at its default, and the returned token's byte length and a short hash prefix "
            "only (O15 step 1).",
            "Persist the token, quit the helper, relaunch and fetch again with no changes: "
            "record whether the event count was zero and whether any drop event appeared "
            "(O15 step 2).",
            "Make one ordinary, undoable change and fetch again: record which event "
            "classes arrive and in what order, and whether identifier-only events suffice "
            "to identify the changed record (O15 step 3).",
            "Deliberately fetch with a token that cannot be valid (for example a token "
            "from a wiped store) and record whether the drop+add sequence appears; if it "
            "cannot be provoked, record that — the reset is observable, not provocable "
            "(O15 step 4).",
        ),
    ),
    # ------------------------------------------------------------ Hermes -------
    _cap(
        name="hermes_capability_discovery",
        title="Hermes capability discovery: GET /v1/capabilities, /v1/toolsets, /v1/skills",
        source="hermes",
        citations=("O17",),
        probe_method="GET /v1/capabilities with the bearer API_SERVER_KEY against the "
                     "running gateway, then GET /v1/toolsets and GET /v1/skills, recording "
                     "the whole /v1/capabilities body and toolset/skill names",
        probe_assertion="the installed Hermes build serves /v1/capabilities and returns "
                        "the documented envelope with a features map observed on that "
                        "build, so Grace can discover runs/streaming/cancellation/session "
                        "support instead of assuming it",
        limitation="unmeasured: no Hermes adapter exists in this worker and no gateway was "
                   "contacted. O17 says the page describes the current docs surface; "
                   "whether Randy's build serves it and what it returns is a Mac "
                   "measurement and nothing in the pack asserts it",
        documented=(
            "O17: 'GET /v1/capabilities' returns object: hermes.api_server.capabilities, "
            "platform, model, auth: {type: bearer, required: true}, and a features map "
            "whose example shows chat_completions, responses_api, run_submission, "
            "run_status, run_events_sse, run_stop, reasoning_streaming",
            "O17 stated purpose: 'Post ... discover whether the running Hermes version "
            "supports runs, streaming, cancellation, and session continuity without "
            "depending on private Python internals.'",
            "O17: enable with API_SERVER_ENABLED = true and required API_SERVER_KEY in "
            "~/.hermes/.env; port 8642, host 127.0.0.1 by default",
            "O17: 'Bearer token auth via the Authorization header ... Configure the key via "
            "API_SERVER_KEY env var.'",
            "O17: 'The API server gives full access to hermes-agent's toolset, including "
            "terminal commands. API_SERVER_KEY is required for every deployment, "
            "including the default loopback bind on 127.0.0.1.'",
            "O17: discovery of the tool surface over REST: GET /v1/toolsets and "
            "GET /v1/skills (read-only, bearer-gated); toolsets example shape "
            "{\"name\",\"label\",\"description\",\"enabled\",\"configured\",\"tools\"}",
            "O17: GET /health ('{\"status\": \"ok\"}', also /v1/health) and "
            "GET /health/detailed ('Authenticated readiness check ...')",
            "O17: advertised feature names session_key_header ('X-Hermes-Session-Key') and "
            "run_approval",
        ),
        absences=(
            "O17 states no version-to-capability mapping: 'the page describes the current "
            "docs surface only', so version_matches_installed_build stays unknown",
            "O17 states no transaction/effect ledger ('ledger' 0): run statuses are "
            "'retained briefly', unconsumed event buffers expire after five minutes, and "
            "Idempotency-Key retention is 24 hours — none of which is durable recovery of "
            "an external side effect",
        ),
        procedure=(
            "Start the gateway/API server with API_SERVER_ENABLED=true in the profile "
            "Grace will use; record the exact startup line only, never the key (O17 "
            "step 1).",
            "curl -sS -H \"Authorization: Bearer $API_SERVER_KEY\" "
            "http://127.0.0.1:8642/v1/capabilities -> record the whole JSON body verbatim "
            "plus `hermes --version` (O17 step 2).",
            "Repeat for GET /v1/toolsets and GET /v1/skills; record the toolset and skill "
            "names only (O17 step 3).",
            "Record the status codes of /health and the authenticated /health/detailed "
            "with the top-level status/readiness keys only (O17 step 4).",
        ),
    ),
    _cap(
        name="hermes_run_submission_and_status",
        title="Submitting a run and reading its status",
        source="hermes",
        citations=("O17",),
        probe_method="POST /v1/runs with a trivial input, then GET /v1/runs/{run_id}; "
                     "repeat the POST with the same Idempotency-Key and record both "
                     "responses",
        probe_assertion="this build accepts a run, returns a run_id with a documented "
                        "status, reports the terminal status through "
                        "GET /v1/runs/{run_id}, and replays an identical retry under the "
                        "same Idempotency-Key — all observed on that build",
        limitation="unmeasured: no Hermes adapter exists in this worker and no run has "
                   "been submitted. The documented surface is what Gate 3 must be planned "
                   "against; the installed build is what must be measured",
        documented=(
            "O17: 'POST /v1/runs' returns run_id, status 'started'; accepts a simple input "
            "string and optional session_id, instructions, conversation_history, or "
            "previous_response_id",
            "O17: 'GET /v1/runs/{run_id}' -> object: hermes.run with run_id, status, "
            "session_id, model, output, usage, runtime",
            "O17 run statuses named: started, running, stopping, waiting_for_approval, and "
            "terminal completed, failed, cancelled, interrupted; statuses are 'retained "
            "briefly after terminal states'",
            "O17: 'For safely retryable creation, send an Idempotency-Key header (1-255 "
            "visible ASCII characters). Hermes durably reserves the key before starting "
            "work. An identical retry returns the original run_id with HTTP 202 and "
            "Idempotency-Replayed: true ... Reusing the same key with a different JSON "
            "payload returns HTTP 409 with code idempotency_key_conflict. Keys are ... "
            "retained for 24 hours after their last status update.'",
            "O17: 'When the gateway shuts down while a run is active, the run is persisted "
            "as interrupted (error `Gateway shutdown interrupted the run.`, terminal event "
            "run.interrupted) before the agent is asked to stop, so a durable run never "
            "survives a restart as running'",
            "O17: gateway.api_server.max_concurrent_runs default 10; over the cap 'new "
            "run-starting requests are rejected with HTTP 429 Too many concurrent runs "
            "(max N)'",
        ),
        absences=(
            "O17 states nothing about recipients, audiences or approval of outbound "
            "communications ('recipient' 0, 'outbound' 0)",
            "O17 does not claim durable recovery of arbitrary external side effects; the "
            "run-status retention window itself is only 'briefly'",
        ),
        procedure=(
            "POST /v1/runs with a trivial input, twice with the same Idempotency-Key; "
            "record both bodies and whether the second returns 202 with "
            "Idempotency-Replayed: true (O17 step 5).",
            "GET /v1/runs/{run_id} until a terminal status; record the status sequence "
            "observed and the object name returned (O17 step 5).",
            "Record the installed build string (`hermes --version`) beside the observed "
            "status set, so a capability claim can be tied to a build (O17 step 2).",
        ),
    ),
    _cap(
        name="hermes_run_progress_events",
        title="Run progress over SSE (GET /v1/runs/{run_id}/events)",
        source="hermes",
        citations=("O17",),
        probe_method="subscribe to GET /v1/runs/{run_id}/events for a trivial run and "
                     "record which documented event names actually arrive",
        probe_assertion="this build streams the documented progress event names on a run, "
                        "so Grace can show progress from real events rather than polling "
                        "guesses",
        limitation="unmeasured: no Hermes adapter exists in this worker and no event stream "
                   "was opened. Which of the documented event names this build emits is "
                   "exactly what the Mac probe has to record",
        documented=(
            "O17: 'GET /v1/runs/{run_id}/events' -> SSE of tool-call progress, token "
            "deltas and lifecycle events",
            "O17 progress/event vocabulary: tool.started, tool.completed (with error "
            "flag), message.interim, message.delta, run.completed, run.interrupted, "
            "approval.request, subagent.start, subagent.complete (carries "
            "child_session_id, delegation_id), and hermes.tool.progress on Chat "
            "Completions SSE",
            "O17: 'The completion preview is the result text ..., passed through forced "
            "secret redaction and then truncated to 500 characters'",
            "O17: unconsumed event buffers expire after five minutes; 'This expires "
            "transport state only: a run that is still executing remains visible to status "
            "polling, approval, stop control, and concurrency accounting until its "
            "executor work actually exits.'",
        ),
        absences=(
            "O17 states no replay or resume for an expired event buffer beyond the five "
            "minute window",
            "O17 states no delivery-acknowledgement guarantee for SSE consumers",
        ),
        procedure=(
            "Submit a trivial run and subscribe to GET /v1/runs/{run_id}/events; record "
            "the event names, their order, and whether the stream ends with a terminal "
            "event (O17 step 5).",
            "Record what happens to the stream when the client disconnects and reconnects "
            "after the documented five-minute buffer expiry — a measurement, not a "
            "reading (O17 step 5).",
        ),
    ),
    _cap(
        name="hermes_run_stop",
        title="Stopping a run (POST /v1/runs/{run_id}/stop)",
        source="hermes",
        citations=("O17",),
        probe_method="POST /v1/runs/{run_id}/stop for a running throwaway run and poll "
                     "GET /v1/runs/{run_id} until the status settles",
        probe_assertion="this build accepts a stop, answers {\"status\": \"stopping\"} "
                        "immediately, and the run settles as a terminal status observed "
                        "by polling — stop never hiding a worker that is still running",
        limitation="unmeasured: no Hermes adapter exists in this worker and no run was "
                   "started or stopped. The documented response shape is what Gate 3 must "
                   "handle; the observed settling behaviour has to come off the Mac",
        documented=(
            "O17: 'POST /v1/runs/{run_id}/stop'",
            "O17: 'The endpoint returns immediately with {\"status\": \"stopping\"} ... "
            "The run stays tracked as stopping until the executor-backed work exits, then "
            "settles as cancelled; requesting stop never hides a worker that is still "
            "running.'",
        ),
        absences=(
            "O17 states no timeout or forced-kill path if the executor never exits",
        ),
        procedure=(
            "Start a trivial long-ish run, POST /v1/runs/{run_id}/stop, and record the "
            "immediate response body (O17 step 5).",
            "Poll GET /v1/runs/{run_id} and record the status sequence and the final "
            "terminal status, and how long the settling took (O17 step 5).",
        ),
    ),
    _cap(
        name="hermes_session_continuity",
        title="Session identity: session_id, X-Hermes-Session-Key, and the routing key",
        source="hermes",
        citations=("O17", "O18"),
        probe_method="POST /v1/runs with a session_id of an existing session, then read "
                     "back GET /v1/runs/{run_id} and the session list; separately exercise "
                     "/compress and observe whether a continuation session appears",
        probe_assertion="a resumed run on this build keeps its session attribution "
                        "(the returned session_id is echoed unchanged) and the documented "
                        "session-key header is accepted, so a resumed job stays "
                        "attributable to its original conversation",
        limitation="unmeasured: no Hermes adapter exists in this worker. Two different "
                   "'session key' concepts are documented (O17's X-Hermes-Session-Key is "
                   "memory scope; O18's gateway routing key agent:main:<platform>:... maps "
                   "to a session ID), so which his build exposes has to be asked of his "
                   "install, not assumed",
        documented=(
            "O17 section 'Long-term memory scoping ( X-Hermes-Session-Key )': 'Multi-user "
            "frontends like Open WebUI need a stable per-channel identifier for long-term "
            "memory (Honcho, etc.) that is independent of the transcript-scoped "
            "X-Hermes-Session-Id (which rotates on /new).'",
            "O17: 'Pass X-Hermes-Session-Key on /v1/chat/completions, /v1/responses, or "
            "/v1/runs ...'; 'max 256 chars, control characters (\\r, \\n, \\x00) are "
            "rejected, and the value is echoed back on responses (JSON + SSE)'; advertised "
            "as session_key_header",
            "O17 session control over REST: GET/POST /api/sessions, GET/PATCH/DELETE "
            "/api/sessions/{id}, GET /api/sessions/{id}/messages, POST "
            "/api/sessions/{id}/fork, POST /api/sessions/{id}/chat[/stream]",
            "O17: detached-result delivery requires 'an explicit X-Hermes-Session-Id on "
            "Chat Completions, a native /api/sessions/{id}/chat request, or a Runs request "
            "using session history'",
            "O18: 'On messaging platforms, sessions are keyed by a deterministic session "
            "key built from the message source', formats agent:main:telegram:dm:<chat_id>, "
            "agent:main:<platform>:group:<chat_id>:<user_id>, etc.; the gateway_routing "
            "table in ~/.hermes/state.db 'Maps session keys to active session IDs'",
            "O18: 'A gateway chat is designed to be one continuous session ... This holds "
            "across gateway crashes, restarts, and updates.'",
            "O18: 'Auto-Lineage on Compression - When a session's context is compressed "
            "... Hermes creates a new continuation session' with numbered titles",
            "O18: group_sessions_per_user: true by default; session sources include "
            "api-server",
        ),
        absences=(
            "O18 never mentions X-Hermes-Session-Key (that name is on O17 only) and states "
            "no memory-scope key",
            "O18 states no HTTP endpoints ('not stated': no HTTP endpoints on that page; "
            "CLI commands only)",
            "O18 states no version and gives no guidance on which session boundary Randy "
            "should choose — that is his decision, not a documented default",
        ),
        procedure=(
            "Record `hermes --version` and the profile Hermes home in use (no token) "
            "(O18 step 1).",
            "With the API server up, POST /v1/runs with an existing session_id, then GET "
            "/v1/runs/{run_id}; record the returned session_id and whether it is echoed "
            "unchanged (O18 step 4).",
            "Run one long exchange, invoke /compress, then `hermes sessions list --limit "
            "5`; record whether a new numbered session appears and where the parent link "
            "is visible (O18 step 3).",
            "Restart the gateway, send one more message in the same chat, and record "
            "whether the same session ID continues (O18 step 5).",
        ),
    ),
    _cap(
        name="hermes_approval_modes",
        title="Hermes approval modes and fail-closed command defaults (a runtime "
              "safeguard, not our approval contract)",
        source="hermes",
        citations=("O20",),
        probe_method="print the active profile's approvals: block (mode/timeout/deny "
                     "modes and counts only), trigger one benign dangerous-class command "
                     "in an interactive session, let one prompt time out, and repeat in a "
                     "headless/API context",
        probe_assertion="the installed build's approval behaviour for shell commands is "
                        "observed: which mode is configured, whether a timed-out prompt "
                        "denies, and whether an unattended/API surface denies instantly or "
                        "raises approval.request",
        limitation="unmeasured: no Hermes adapter exists in this worker and no command was "
                   "run. This is a runtime safeguard for shell commands; it approves no "
                   "recipient, message or audience, so the immutable send approval Grace "
                   "owes (PRD line 224) remains entirely ours",
        documented=(
            "O20: configuration under approvals: in ~/.hermes/config.yaml — mode (smart | "
            "manual | off, default smart), timeout (default 300 s), cron_mode (default "
            "deny), single_query_mode (default deny), unattended_mode (default deny), "
            "mcp_reload_confirm (default true), destructive_slash_confirm (default true)",
            "O20: unattended_mode covers 'sessions on unattended programmatic platforms "
            "(webhook, msgraph_webhook, api_server)'; 'deny blocks the command instantly'",
            "O20: the documented exception — 'an api_server session whose client can "
            "answer the card (/v1/runs and streaming chat completions, via POST "
            "/v1/runs/{id}/approval) still gets the approval request.'",
            "O20: 'If no response is given within the timeout, the command is denied by "
            "default (fail-closed).'",
            "O20: hardline blocklist 'regardless of' --yolo, approvals.mode: off, cron "
            "approve mode, or a user clicking 'allow always'",
            "O20: approvals.deny glob patterns; command_allowlist ('Commands approved with "
            "always are saved to ~/.hermes/config.yaml'); container backends skip "
            "dangerous-command checks 'because the container itself is the security "
            "boundary'",
            "O20: credential redaction for MCP tool errors replaces ghp_..., sk-..., Bearer "
            "tokens, token=, key=, API_KEY=, password=, secret= with [REDACTED]",
        ),
        absences=(
            "O20 states nothing about outbound communications, recipients or sending "
            "('recipient' 0); every approval mention is about a shell command, a file "
            "write or an MCP trust gate",
            "O20 states no product-level approval contract: nothing about immutable draft "
            "versions, sender identity, recipient snapshots, operation IDs or expiry "
            "('draft' 0 in that sense) — PRD line 224 is nowhere on the page",
            "O20 warns that redaction is not total: 'The session database itself still "
            "holds the command as it was executed.'",
        ),
        procedure=(
            "Print the approvals: block from the active profile's config.yaml with counts "
            "and modes only — never allowlist contents that embed a credential (O20 "
            "step 1).",
            "Trigger one benign dangerous-class command in an interactive CLI session and "
            "record the prompt text and options exactly as the build renders them (O20 "
            "step 2).",
            "Shorten the timeout for one test, let a prompt expire, record whether the "
            "command was denied and the exact message, then restore the timeout (O20 "
            "step 3).",
            "Repeat the same harmless command through POST /v1/runs and record whether it "
            "is denied instantly or whether approval.request appears and POST "
            "/v1/runs/{run_id}/approval resolves it (O20 step 4).",
        ),
    ),
    _cap(
        name="hermes_credential_resolution",
        title="Credential resolution post-check: fail-closed verification is ours to build",
        source="hermes",
        citations=("O21", "O22"),
        probe_method="record `op whoami` account type, run `hermes secrets onepassword "
                     "status`, then deliberately break one throwaway mapping and record "
                     "what Hermes does on startup; repeat from a non-interactive context",
        probe_assertion="the resolution path this deployment will use is named and its "
                        "failure behaviour is observed on this install — including the "
                        "documented fallback — so Grace's own post-resolution check can be "
                        "written against a measurement",
        limitation="unmeasured: no Hermes adapter exists in this worker and nothing was "
                   "contacted. The vendor documents fail-OPEN behaviour ('it never blocks "
                   "startup'), so fail-closed credential checking is ours to build: Grace "
                   "must verify each required credential after resolution and refuse to "
                   "dispatch on a mismatch",
        documented=(
            "O21: map environment-variable names to op:// references in "
            "~/.hermes/config.yaml; 'Every time hermes (or the gateway, or a cron job) "
            "starts, after ~/.hermes/.env has loaded, Hermes runs `op read` for each "
            "reference and sets the resolved values into os.environ.'",
            "O21: 'If `op` is missing, your session is locked, or a reference is wrong, "
            "Hermes prints a one-line warning and continues with whatever credentials "
            "`.env` already had - it never blocks startup.'",
            "O21: 'an empty value is never applied - your existing env var is left intact'",
            "O21: non-interactive use needs a service-account token in "
            "OP_SERVICE_ACCOUNT_TOKEN 'in every process that resolves secrets - including "
            "cron jobs ..., subprocess invocations, CLI runs, macOS launchd agents, and "
            "Docker containers'",
            "O21 config keys: secrets.onepassword.enabled (default false), env, account, "
            "service_account_token_env (default OP_SERVICE_ACCOUNT_TOKEN), binary_path, "
            "cache_ttl_seconds (default 300; 0 disables caching), override_existing "
            "(default true)",
            "O21: resolved values cached at <hermes_home>/cache/op_cache.json, 'written "
            "atomically, mode 0600', storing only resolved secret values",
            "O22: 'We recommend using 1Password Service Accounts to follow the principle "
            "of least privilege.'; methods named: op run, op read, op inject, op plugin "
            "run; reference form op://<vault>/<item>/<field>",
        ),
        absences=(
            "O21 documents no fail-closed secret handling: 'fail closed' 0 and 'fail-"
            "closed' 0 on that page, and the stated behaviour is the opposite",
            "O22 states no failure behaviour at all ('fail' 0): nothing about a missing, "
            "unauthenticated or locked `op`, and nothing about exit codes",
            "O22 does not name OP_SERVICE_ACCOUNT_TOKEN (that name is O21's) and neither "
            "page states a redaction mechanism for diagnostics ('redact' 0 on both)",
            "PRD line 296 is not a secret-handling line: the secret-handling lines are 291 "
            "and 293 and the fail-closed requirement is the last sentence of 253",
        ),
        procedure=(
            "As the owner, run `op whoami`; record success/failure and the account *type* "
            "only (service account vs desktop session) (O21 step 1).",
            "Run `hermes secrets onepassword status`; record which fields report "
            "configured (enabled, binary found, auth state, number of references) — never "
            "the reference values (O21 step 2).",
            "Deliberately break one throwaway mapping (or set enabled: false), start "
            "Hermes, and record: the exact warning shape, whether startup continued (the "
            "fail-open confirmation), and whether a pre-existing .env value was used "
            "(O21 step 3).",
            "Repeat with the token unset in the same non-interactive context the Mini "
            "worker uses (a launchd agent or an env -i style shell) and record what value "
            "sources remain (O21 step 4).",
            "Record which of op run / op read / op inject / op plugin run the existing "
            "approved setup already uses, and whether a vault-scoped service account is "
            "acceptable to the owner (O22 steps 2 and 5).",
        ),
    ),
    _cap(
        name="hermes_execution_modes",
        title="Execution modes named by Hermes — and the computer/GUI mode that is not",
        source="hermes",
        citations=("O19",),
        probe_method="record terminal.backend in the active profile's config.yaml, run "
                     "`hermes tools`, and test the SSH-vs-GUI question read-only with a "
                     "GUI-only command under an ssh backend and under a headless local "
                     "launchd context",
        probe_assertion="the execution backend this install actually uses is recorded, and "
                        "whether an approved computer/desktop action is reachable from it "
                        "is measured — including the case where it is not, which would "
                        "refute our inference",
        limitation="unmeasured: no Hermes adapter exists in this worker and no command was "
                   "run. No page in the pack names a computer/GUI/desktop execution mode, "
                   "so PRD line 251's SSH/GUI privilege sentence is our inference, not "
                   "vendor text, and it is allowed to be refuted by this measurement",
        documented=(
            "O19 terminal backends, verbatim: local ('Run on your machine (default)'), "
            "docker ('Isolated containers'), ssh ('Remote server - Sandboxing, keep agent "
            "away from its own code'), singularity (HPC containers, rootless), modal "
            "(cloud execution), daytona (cloud sandbox workspace), vercel_sandbox (cloud "
            "microVM); configured by terminal.backend",
            "O19: 'Recommended for security - agent can't modify its own code' with "
            "TERMINAL_SSH_HOST, TERMINAL_SSH_USER, TERMINAL_SSH_KEY 'in ~/.hermes/.env'",
            "O19: 'Agent terminal calls run your shell non-interactively - there is no TTY "
            "and no human at the prompt'",
            "O19 tool registry by category (names as written): web_search, web_extract, "
            "x_search, terminal, process, read_file, patch, browser_navigate, "
            "browser_snapshot, browser_vision, vision_analyze, image_generate, "
            "text_to_speech, todo, clarify, execute_code, delegate_task, memory, "
            "session_search, cronjob",
            "O19: 'Outbound delivery is handled by cron's own delivery, the `hermes send` "
            "CLI, and the gateway notifier - not by an agent-callable tool.'",
            "O19 background processes: terminal(..., background=true) returns "
            "{\"session_id\": \"proc_abc123\", \"pid\": 12345}; the process tool has "
            "list, poll, wait, log, kill, write actions; retained receipts are readable "
            "only by the owning conversation",
        ),
        absences=(
            "no page names a computer/GUI/desktop execution mode: O19 counts 'gui' 0, "
            "'computer' 0, 'desktop' 0, 'Accessibility' 0 — the nearest thing is browser "
            "automation, which is not desktop control",
            "O19 states nothing about what SSH does or does not grant ('macOS' 0, "
            "'privacy' 0, 'GUI-session' 0, 'TCC' 0), so the PRD sentence about SSH and "
            "GUI-session access is ours",
            "O17 similarly has no GUI statement (its 'gui' hits are 'distinguishable' and "
            "'guide')",
        ),
        procedure=(
            "Run `hermes tools` (and GET /v1/toolsets with the API server up) and record "
            "the exact tool and toolset names present on the installed build, plus "
            "`hermes --version` (O19 step 1).",
            "Record terminal.backend from the active profile's config.yaml (value only, "
            "no keys) (O19 step 2).",
            "With terminal.backend: ssh configured to a host he owns, run a harmless "
            "read-only GUI-only command on the target and record the exact output or "
            "error; if it succeeds, our inference is wrong and must be corrected (O19 "
            "step 3).",
            "Repeat the same GUI-only probe with terminal.backend: local from a "
            "launchd/headless context, to distinguish 'SSH' from 'headless session' as the "
            "cause (O19 step 4).",
        ),
    ),
)
