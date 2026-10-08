"""The read-only Mail.app adapter: namespacing, bounded enumeration, retrieval.

This is the Mini worker's real Mail source. It is deliberately *read-only*:

* ``prepare_draft``, ``dispatch`` and ``reconcile`` return ``unsupported`` with a reason,
  so no outbound path exists in this slice (PRD §10 draft-only launch default; R12–R16).
* ``materialize_attachment`` returns ``unsupported``: downloading attachment bytes needs
  Mail's own download/save verb, which has not been measured on Randy's build (PRD §11).
* Nothing here marks a message read, moves it, or changes mailbox state — PRD §6 forbids
  exactly that.

Every method returns a typed :class:`~switchboard_mini.outcomes.Outcome`. A mailbox that
cannot be reached reports ``offline``; a denied automation permission reports
``permission_denied``; a scan that stops short reports ``partial`` with
``coverage.coverage_state = 'partial_history'`` and a ``gap_reason``. None of those is an
empty successful list.

``retrieve`` is careful about the difference between "not there" and "not in the window I
looked at": if the id scan was capped, a miss is reported as ``partial`` (it may exist
deeper), and only an uncapped scan earns a ``permanent_error``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional
from urllib.parse import quote, unquote

from . import outcomes as O
from .mail_transport import (DEFAULT_MAX_SCAN, MailTransport, RecordedMailTransport,
                             AppleScriptMailTransport, load_fixture)
from .version import WORKER_VERSION

NAMESPACE = "mail"
MANIFEST_VERSION = "1.0"
CURSOR_VERSION = "mail1"
MAX_LIMIT = 500


def account_key(name: str) -> str:
    """Stable, non-reversible key for a Mail account label."""
    return "acct_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def scope_ref(mailbox: str) -> str:
    return "mailbox:" + quote(mailbox, safe="")


def mailbox_of(scope: str) -> str:
    if not scope.startswith("mailbox:"):
        raise ValueError(f"unsupported scope reference {scope!r}; expected 'mailbox:<name>'")
    return unquote(scope[len("mailbox:"):])


def make_ref(account: str, mailbox: str, internal_id: str) -> str:
    return ":".join([NAMESPACE, quote(account, safe=""), quote(mailbox, safe=""),
                     quote(str(internal_id), safe="")])


def parse_ref(ref: str) -> tuple:
    parts = (ref or "").split(":")
    if len(parts) != 4 or parts[0] != NAMESPACE:
        raise ValueError(f"not a {NAMESPACE} reference: {ref!r}")
    return unquote(parts[1]), unquote(parts[2]), unquote(parts[3])


def encode_cursor(account: str, mailbox: str, last_index: int, last_id: str) -> str:
    return "|".join([CURSOR_VERSION, quote(account, safe=""), quote(mailbox, safe=""),
                     str(last_index), quote(str(last_id), safe="")])


def decode_cursor(cursor: str) -> tuple:
    parts = (cursor or "").split("|")
    if len(parts) != 5 or parts[0] != CURSOR_VERSION:
        raise ValueError(f"malformed cursor {cursor!r}")
    last_index = int(parts[3])
    return unquote(parts[1]), unquote(parts[2]), last_index, unquote(parts[4])


class MailReadOnlyAdapter:
    """A ``SourceAdapter`` in the Grace sense, restricted to reads."""

    name = NAMESPACE
    version = WORKER_VERSION
    host_role = "mini"
    simulated = False                     # the *worker* is real; fixture mode is labelled

    def __init__(self, transport: MailTransport, *, max_scan: int = DEFAULT_MAX_SCAN):
        self.transport = transport
        self.max_scan = max_scan

    # -- provenance --------------------------------------------------------
    @property
    def origin(self) -> str:
        return self.transport.origin

    @property
    def adapter_is_real(self) -> bool:
        """The real adapter answered, not a fixture twin. Not a contact claim."""
        return bool(getattr(self.transport, "adapter_is_real", False))

    def _label(self) -> Optional[str]:
        return getattr(self.transport, "label", None)

    def _stamp(self, outcome: O.Outcome, *, account: Optional[str] = None,
               from_read: Optional[O.Outcome] = None) -> O.Outcome:
        """Stamp adapter identity, and the provenance of the read this outcome came from.

        ``from_read`` is the transport outcome the adapter derived this document from, so
        the derived document keeps the fact that Mail was really reached (and keeps it
        absent when it was not). An outcome the adapter builds on its own -- a local
        rejection, an unsupported capability, a command that contacts nothing -- passes
        nothing and stays uncontacted.
        """
        outcome.adapter = self.name
        outcome.origin = self.origin
        outcome.adapter_is_real = self.adapter_is_real
        if from_read is not None:
            outcome.adapter_is_real = bool(from_read.adapter_is_real
                                           or self.adapter_is_real)
            outcome.source_contacted = bool(from_read.source_contacted)
        if account:
            outcome.account_id = account_key(account)
        if not outcome.is_real:
            outcome.label = self._label() or O.fixture_label(self.name)
        return outcome


    # -- discovery ---------------------------------------------------------
    def identity(self) -> O.Outcome:
        return self._stamp(self.transport.identity())

    def health(self, account: Optional[str] = None) -> O.Outcome:
        ident = self.identity()
        if not ident.usable:
            return ident
        running = ident.data.get("mail_running")
        installed = bool(ident.data.get("mail_bundle", {}).get("info_plist_version"))
        if running is False:
            return self._stamp(O.Outcome.offline(
                "Mail.app is not running on this Mac; no mailbox can be read until it is",
                reason="mail_not_running", data=ident.data,
                account_id=account_key(account) if account else None,
                next_action="launch Mail.app and retry"))
        return self._stamp(O.Outcome.ok({
            "health_state": "current" if installed else "unknown",
            "mail_running": running,
            "mail_bundle": ident.data.get("mail_bundle"),
            "permission_state": O.PERMISSION_GRANTED,
            "note": ("connected is not completeness and not send capability: this worker "
                     "holds no send path at all"),
        }, account_id=account_key(account) if account else None), account=account,
                           from_read=ident)

    def accounts(self) -> O.Outcome:
        return self._stamp(self.transport.accounts())

    def mailboxes(self, account: str) -> O.Outcome:
        return self._stamp(self.transport.mailboxes(account), account=account)

    def manifest(self, probe_rows: Optional[list] = None) -> dict:
        """Versioned capability manifest (PRD §6, §11).

        Without probe results every capability is declared ``supported: false`` with
        ``state='unverified'``: the worker will not claim a Mail capability it has not
        measured on this Mac. Pass the rows from ``switchboard-mini probe`` to have the
        manifest reflect what was actually observed.
        """
        from .probe import CAPABILITIES          # local import avoids a cycle

        measured = {row["capability"]: row for row in (probe_rows or [])}
        capabilities = {}
        for capability in CAPABILITIES:
            row = measured.get(capability.name)
            if row is None:
                capabilities[capability.name] = {
                    "supported": False,
                    "state": "unverified",
                    "permission_state": O.PERMISSION_NOT_DETERMINED,
                    "observed_version": None,
                    "limitation": ("not yet probed on this host — run `switchboard-mini "
                                   "probe` and record its output (Gate 2)"),
                    "probe_method": capability.probe_method,
                    "probe_assertion": capability.probe_assertion,
                }
            else:
                capabilities[capability.name] = {
                    "supported": bool(row["supported"]),
                    "state": row["state"],
                    "permission_state": row["permission_state"],
                    "observed_version": row["observed_version"],
                    "limitation": row["limitation"],
                    "probe_method": row["probe_method"],
                    "probe_assertion": row["probe_assertion"],
                }
        manifest = {
            "adapter": self.name,
            "adapter_version": self.version,
            "manifest_version": MANIFEST_VERSION,
            "host_role": self.host_role,
            "origin": self.origin,
            "adapter_is_real": self.adapter_is_real,
            # The manifest command contacts nothing at all: it is the worker's own
            # declaration plus any probe rows folded in. `real_source_connected` therefore
            # stays false here even with the real adapter selected -- what Mail could do
            # is what the *rows* say, not what this document can claim.
            "real_source_connected": False,
            "source_contacted": False,
            "probed": bool(measured),
            "folded_probe_rows": {
                "count": len(measured),
                "any_from_real_source": bool(any(
                    row.get("real_source_connected") for row in (probe_rows or []))),
                "note": ("a folded row carries its own origin, label and "
                         "real_source_connected value; folding it in does not make this "
                         "document a measurement"),
            },
            "capabilities": capabilities,
        }
        if self.origin != O.REAL:
            manifest["label"] = self._label() or O.fixture_label(self.name)
            manifest["disclaimer"] = O.FIXTURE_DISCLAIMER
        return manifest

    # -- reads -------------------------------------------------------------
    def _scan(self, account: str, mailbox: str) -> O.Outcome:
        return self._stamp(self.transport.message_ids(account, mailbox,
                                                      max_scan=self.max_scan),
                           account=account)

    def _window(self, account: str, mailbox: str, start: int, end: int) -> O.Outcome:
        return self._stamp(self.transport.message_window(account, mailbox, start=start,
                                                         end=end), account=account)

    def enumerate(self, account: str, scope: str, *, limit: int = 25,
                  cursor: Optional[str] = None, since: Optional[str] = None) -> O.Outcome:
        try:
            mailbox = mailbox_of(scope)
        except ValueError as exc:
            return self._stamp(O.Outcome.permanent(
                f"unusable scope reference: {exc}", reason="bad_scope",
                adapter=self.name, account_id=account_key(account)), account=account)
        limit = max(1, min(int(limit), MAX_LIMIT))
        scan = self._scan(account, mailbox)
        if not scan.usable:
            return scan
        entries = scan.data.get("entries") or []
        capped = bool(scan.data.get("capped"))

        start = 0
        cursor_reset = False
        reset_reason = None
        if cursor:
            try:
                c_account, c_mailbox, last_index, last_id = decode_cursor(cursor)
            except ValueError as exc:
                return self._stamp(O.Outcome.permanent(
                    f"cursor rejected: {exc}", reason="malformed_cursor",
                    adapter=self.name, account_id=account_key(account)),
                    account=account)
            if c_account != account or c_mailbox != mailbox:
                return self._stamp(O.Outcome.permanent(
                    f"cursor belongs to a different scope ({c_mailbox!r} in account "
                    f"{c_account!r}); refusing to resume across scopes",
                    reason="cursor_scope_mismatch", adapter=self.name,
                    account_id=account_key(account)), account=account)
            start = max(0, last_index)
            if start > 0:
                previous = entries[start - 1]["internal_id"] if start - 1 < len(entries) \
                    else None
                if previous != last_id:
                    cursor_reset = True
                    reset_reason = (
                        "the message that ended the previous page is no longer at that "
                        "position in the mailbox; the mailbox changed under the cursor, so "
                        "this page restarts from the beginning and the consumer must treat "
                        "the overlap idempotently")
                    start = 0

        window = entries[start:start + limit]
        items = []
        if window:
            # Mail message indices are 1-based and follow the id order we just read.
            got = self._window(account, mailbox, window[0]["index"], window[-1]["index"])
            if got.usable:
                windowed = {item["internal_id"]: item for item in got.data["items"]}
                for entry in window:
                    item = dict(windowed.get(entry["internal_id"], {}))
                    item.setdefault("internal_id", entry["internal_id"])
                    item["provider_index"] = entry["index"]
                    item["namespaced_id"] = make_ref(account, mailbox, entry["internal_id"])
                    item["retrieval_pointer"] = "mail:" + "/".join(
                        [quote(account, safe=""), quote(mailbox, safe=""),
                         quote(str(entry["internal_id"]), safe="")])
                    items.append(item)
            else:
                # The ids are real, the metadata fetch was not: report the gap instead of
                # returning rows with invented fields.
                for entry in window:
                    items.append({
                        "internal_id": entry["internal_id"],
                        "provider_index": entry["index"],
                        "namespaced_id": make_ref(account, mailbox, entry["internal_id"]),
                        "metadata_state": "unavailable",
                        "unavailable_reason": got.detail or got.code,
                    })
        last = window[-1] if window else None
        next_cursor = None
        if last is not None and (start + len(window)) < len(entries):
            next_cursor = encode_cursor(account, mailbox, start + len(window),
                                        last["internal_id"])
        if since:
            kept = [i for i in items
                    if (i.get("source_time_local") or "") >= since]
            dropped = len(items) - len(kept)
            items = kept
        else:
            dropped = 0

        reachable_end = not next_cursor
        coverage_state = (O.COVERAGE_COMPLETE
                          if (reachable_end and not capped) else O.COVERAGE_PARTIAL_HISTORY)
        gap_reason = None
        if capped:
            gap_reason = (scan.detail or
                          f"the id scan stopped at {scan.data.get('scanned_count')} of "
                          f"{scan.data.get('total_count')} messages")
        elif next_cursor:
            gap_reason = ("more messages remain after this page; this page proves only "
                          "that the adapter reached the end of the requested window")
        data = {
            "items": items,
            "next_cursor": next_cursor,
            "cursor_reset": cursor_reset,
            "since_filter": {"applied": bool(since), "value": since,
                             "dropped_from_page": dropped,
                             "note": "Mail has no server-side date filter; `since` selects "
                                     "within the fetched page only"},
            "coverage": {
                "observed_count": len(items),
                "returned_total_for_query": len(entries),
                "mailbox_total_count": scan.data.get("total_count"),
                "scan_capped": capped,
                "coverage_state": coverage_state,
                "gap_reason": gap_reason,
                "oldest_observed": items[0].get("source_time_local") if items else None,
                "newest_observed": items[-1].get("source_time_local") if items else None,
                "note": ("reaching the end of this query's accessible result set is not "
                         "the end of the mailbox history (PRD §6)"),
            },
        }
        if cursor_reset:
            return self._stamp(O.Outcome.partial(data, reset_reason,
                                                 reason="cursor_reset",
                                                 account_id=account_key(account)),
                               account=account, from_read=scan)
        if capped or next_cursor:
            return self._stamp(O.Outcome.partial(data, gap_reason or "partial page",
                                                 reason="partial_history",
                                                 account_id=account_key(account)),
                               account=account, from_read=scan)
        return self._stamp(O.Outcome.ok(data, account_id=account_key(account)),
                           account=account, from_read=scan)

    def retrieve(self, account: str, namespaced_id: str) -> O.Outcome:
        try:
            ref_account, mailbox, internal_id = parse_ref(namespaced_id)
        except ValueError as exc:
            return self._stamp(O.Outcome.permanent(
                f"unusable reference: {exc}", reason="bad_reference",
                account_id=account_key(account)), account=account)
        if ref_account != account:
            return self._stamp(O.Outcome.permanent(
                f"reference belongs to account {ref_account!r}, not {account!r}",
                reason="account_mismatch", account_id=account_key(account)),
                account=account)
        scan = self._scan(account, mailbox)
        if not scan.usable:
            return scan
        entries = scan.data.get("entries") or []
        index = None
        for entry in entries:
            if str(entry["internal_id"]) == str(internal_id):
                index = entry["index"]
                break
        if index is None:
            if scan.data.get("capped"):
                return self._stamp(O.Outcome.partial(
                    {"message": None, "scanned_count": scan.data.get("scanned_count"),
                     "mailbox_total_count": scan.data.get("total_count")},
                    f"{internal_id} is not inside the first {scan.data.get('scanned_count')} "
                    f"messages of {mailbox}; the scan was capped, so this run proves only "
                    f"that the message is not in the scanned window",
                    reason="not_in_scanned_window", account_id=account_key(account)),
                    account=account, from_read=scan)
            return self._stamp(O.Outcome.permanent(
                f"unknown message reference {namespaced_id}: not present in {mailbox}",
                reason="unknown_message_reference",
                data={"mailbox_total_count": scan.data.get("total_count"),
                      "scanned_all": True},
                account_id=account_key(account)), account=account, from_read=scan)

        got = self._window(account, mailbox, index, index)
        if not got.usable:
            return got
        items = got.data.get("items") or []
        if not items:
            return self._stamp(O.Outcome.permanent(
                f"message {internal_id} disappeared between the id scan and the read",
                reason="message_vanished", account_id=account_key(account)),
                account=account, from_read=got)
        message = dict(items[0])
        message["namespaced_id"] = namespaced_id
        message["retrieval_pointer"] = "mail:" + "/".join(
            [quote(account, safe=""), quote(mailbox, safe=""), quote(str(internal_id), safe="")])

        body = self._stamp(self.transport.message_body(account, mailbox, index=index),
                           account=account)
        attachments = self._stamp(
            self.transport.attachments(account, mailbox, index=index), account=account)

        # A partial body result means no content was read. It is NOT an empty message and
        # it is not usable text: only `success` carries a body (PRD §6).
        body_read = body.succeeded
        data = {
            "message": message,
            "body": (body.data or {}).get("body") if body_read else None,
            "body_state": ((body.data or {}).get("body_state") if body_read
                           else "unavailable"),
            "body_length": (body.data or {}).get("body_length") if body_read else None,
            "attachments": (attachments.data or {}).get("attachments", [])
            if attachments.usable else [],
            "attachment_state": attachments.code,
            "unavailable_reason": (None if body_read else (body.detail or body.code)),
        }
        if not body_read:
            return self._stamp(O.Outcome.partial(
                data,
                f"headers retrieved but message content is unavailable "
                f"({body.code}: {body.detail}); this is not an empty message",
                reason="content_unavailable", account_id=account_key(account)),
                account=account, from_read=got)
        if not attachments.usable:
            return self._stamp(O.Outcome.partial(
                data,
                f"headers and body retrieved; attachment metadata is unavailable "
                f"({attachments.code}: {attachments.detail})",
                reason="attachment_metadata_unavailable",
                account_id=account_key(account)), account=account, from_read=got)
        return self._stamp(O.Outcome.ok(data, account_id=account_key(account)),
                           account=account, from_read=got)

    def history_poll(self, account: str, scope: str, *, cursor: Optional[str] = None,
                     limit: int = 50) -> O.Outcome:
        """Mail has no event feed; resumable polling is the reliable baseline (PRD §11)."""
        out = self.enumerate(account, scope, limit=limit, cursor=cursor)
        if out.usable and out.data is not None:
            out.data["poll_basis"] = ("bounded re-scan with the consumer's own cursor; "
                                      "Mail supplies no server-side change feed")
        return out

    def change_poll(self, account: str, *, token: Optional[str] = None) -> O.Outcome:
        return self._stamp(O.Outcome.unsupported(
            "Mail exposes no change-history token; the reliable baseline is resumable "
            "polling with overlap (PRD §11)",
            reason="no_change_feed", next_action="use history_poll"),
            account=account)

    def coverage_declaration(self, account: str, scope: str) -> O.Outcome:
        """What this adapter can say about the completeness of what it has read."""
        try:
            mailbox = mailbox_of(scope)
        except ValueError as exc:
            return self._stamp(O.Outcome.permanent(
                f"unusable scope reference: {exc}", reason="bad_scope",
                adapter=self.name, account_id=account_key(account)), account=account)
        scan = self._scan(account, mailbox)
        if not scan.usable:
            return scan
        capped = bool(scan.data.get("capped"))
        gap = (f"the id scan stopped at {scan.data.get('scanned_count')} of "
               f"{scan.data.get('total_count')} messages; deeper history is unproven"
               if capped else None)
        return self._stamp(O.Outcome.ok({
            "account": account, "mailbox": mailbox,
            "coverage_state": O.COVERAGE_PARTIAL_HISTORY if capped else "scan_reached_end",
            "gap_reason": gap,
            "scanned_count": scan.data.get("scanned_count"),
            "mailbox_total_count": scan.data.get("total_count"),
            "note": ("a declaration about one query against one mailbox, not a statement "
                     "about the account's history"),
        }, account_id=account_key(account)), account=account, from_read=scan)

    def materialize_attachment(self, account: str, namespaced_id: str, *,
                               max_bytes: int = 10_000_000) -> O.Outcome:
        return self._stamp(O.Outcome.unsupported(
            "materialising attachment bytes is not implemented in this read-only slice: "
            "Mail's download/save verb and per-attachment download state have not been "
            "measured on Randy's build (PRD §11)",
            reason="materialize_not_implemented",
            next_action="measure Mail's attachment save behaviour on the Mac in Gate 2"),
            account=account)

    # -- optional mutations: none exist in this worker ---------------------
    def prepare_draft(self, account: str, request: dict) -> O.Outcome:
        return self._stamp(O.Outcome.unsupported(
            "this worker has no draft path: nothing in slice 1 writes to a Mail composer",
            reason="read_only_slice_1"), account=account)

    def dispatch(self, account: str, request: dict) -> O.Outcome:
        return self._stamp(O.Outcome.unsupported(
            "no outbound path exists in this worker: the Mini adapter is read-only in "
            "slice 1, so nothing can be sent from it (PRD §10, R12–R16)",
            reason="read_only_slice_1",
            next_action="an outbound Mail path is Gate 3 work and needs its own approval "
                        "contract and reconciliation"), account=account)

    def reconcile(self, account: str, *, idempotency_key: str,
                  source_operation_id: Optional[str] = None) -> O.Outcome:
        return self._stamp(O.Outcome.unsupported(
            "nothing can have been sent by this worker, so there is nothing to reconcile",
            reason="read_only_slice_1"), account=account)


# ------------------------------------------------------------------ factories --

def build_adapter(*, fixture_mode: bool = False, fixture_scenario: str = "granted",
                  max_scan: int = DEFAULT_MAX_SCAN, timeout_s: int = 120) -> MailReadOnlyAdapter:
    """The one place that decides real versus fixture. Nothing else guesses."""
    if fixture_mode:
        transport: MailTransport = RecordedMailTransport(load_fixture(fixture_scenario))
    else:
        transport = AppleScriptMailTransport(timeout_s=timeout_s)
    return MailReadOnlyAdapter(transport, max_scan=max_scan)
