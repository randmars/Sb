"""Mail.app transport: the AppleScript that talks to Mail, and its fixture twin.

Two implementations of one interface:

* :class:`AppleScriptMailTransport` -- the real thing. It builds AppleScript, runs it
  through ``/usr/bin/osascript`` and returns typed outcomes.
* :class:`RecordedMailTransport` -- answers the same calls from a recorded result in
  ``switchboard_mini/fixtures/mail/*.json`` so the whole worker is testable off a Mac
  (``--fixture-mode``). Its outcomes are labelled ``FIXTURE:`` and report
  ``real_source_connected: false``.

Both classify failures with :func:`applescript.classify`, so the mapping an operator
sees on the Mac is the mapping the tests exercise here.

Honesty notes that the probe and the README repeat:

* Mail's scripting terminology varies by application version (PRD §11). Every property
  read below is wrapped in an AppleScript ``try`` and its failure is *recorded*, not
  swallowed: a call that could return "nothing" returns an explicit sentinel instead, so
  an unavailable body can never be read as an empty message.
* Mail offers no server-side cursor. ``message_ids`` re-reads the mailbox's internal
  ids (bounded by ``max_scan``) and the caller keeps its own cursor. When the scan is
  capped the result is ``partial`` with ``coverage_state='partial_history'`` -- reaching
  the end of the scan proves the end of the scan, not the end of the mailbox (PRD §6).
* Reads never write. No Mail.app data, mailbox state, read flag or composer is touched.
"""

from __future__ import annotations

import abc
import json
import os
import subprocess
import sys
from typing import Any, Iterable, Optional
from urllib.parse import quote, unquote

from . import outcomes as O
from .applescript import OsascriptRunner, ScriptRunner, classify

MAIL_BUNDLE_CANDIDATES = (
    "/System/Applications/Mail.app",
    "/Applications/Mail.app",
)
DEFAULT_MAX_SCAN = 2000


# ------------------------------------------------------------- applescript text --

def _as_text(value: str) -> str:
    """Quote a Python string for an AppleScript string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def script_accounts() -> str:
    return """tell application "Mail"
  set out to ""
  repeat with a in accounts
    set aName to ""
    set aId to ""
    set aType to ""
    set aEnabled to ""
    set aCount to 0
    try
      set aName to (name of a) as text
    end try
    try
      set aId to (id of a) as text
    end try
    try
      set aType to (account type of a) as text
    end try
    try
      set aEnabled to (enabled of a) as text
    end try
    try
      set aCount to (count of email addresses of a)
    end try
    set out to out & aName & tab & aId & tab & aType & tab & aEnabled & tab & (aCount as text) & linefeed
  end repeat
  return out
end tell"""


def script_mailboxes(account: Optional[str]) -> str:
    target = "mailboxes" if not account else f"mailboxes of account {_as_text(account)}"
    return f"""tell application "Mail"
  set out to ""
  repeat with mb in {target}
    set mbName to ""
    set mbUnread to 0
    set mbId to ""
    try
      set mbName to (name of mb) as text
    end try
    try
      set mbUnread to (unread count of mb)
    end try
    try
      set mbId to (id of mb) as text
    end try
    set out to out & mbName & tab & (mbUnread as text) & tab & mbId & linefeed
  end repeat
  return out
end tell"""


def script_message_ids(account: str, mailbox: str, max_scan: int) -> str:
    """Bounded internal-id scan of one mailbox.

    The header line reports the mailbox's true message count, so a caller can tell a
    complete scan from a capped one. The first and last dates are sampled so the probe
    can measure whether Mail's index order is chronological on this mailbox.
    """
    return f"""tell application "Mail"
  set theBox to mailbox {_as_text(mailbox)} of account {_as_text(account)}
  set n to (count of messages of theBox)
  set upper to n
  if upper > {max_scan} then set upper to {max_scan}
  set firstDate to ""
  set lastDate to ""
  try
    if upper > 0 then set firstDate to (date received of message 1 of theBox) as text
    if upper > 0 then set lastDate to (date received of message upper of theBox) as text
  end try
  set out to "meta" & tab & (n as text) & tab & (upper as text) & tab & firstDate & tab & lastDate & linefeed
  repeat with i from 1 to upper
    set m to message i of theBox
    set mId to ""
    try
      set mId to (id of m) as text
    end try
    set out to out & mId & tab & (i as text) & linefeed
  end repeat
  return out
end tell"""


def script_message_window(account: str, mailbox: str, start: int, end: int) -> str:
    """Metadata for a contiguous index range, in one osascript call."""
    return f"""tell application "Mail"
  set theBox to mailbox {_as_text(mailbox)} of account {_as_text(account)}
  set out to ""
  repeat with i from {start} to {end}
    set m to message i of theBox
    set mId to ""
    set mSubject to ""
    set mSender to ""
    set mRead to ""
    set mMsgId to ""
    set mDate to ""
    set mYear to ""
    set mMonth to ""
    set mDay to ""
    set mHour to ""
    set mMin to ""
    set mSec to ""
    set mTo to -1
    set mCc to -1
    set mBcc to -1
    try
      set mId to (id of m) as text
    end try
    try
      set mSubject to (subject of m) as text
    end try
    try
      set mSender to (sender of m) as text
    end try
    try
      set mRead to (read status of m) as text
    end try
    try
      set mMsgId to (message id of m) as text
    end try
    try
      set d to date received of m
      set mDate to d as text
      set mYear to (year of d) as text
      set mMonth to (month of d as integer) as text
      set mDay to (day of d) as text
      set mHour to (hours of d) as text
      set mMin to (minutes of d) as text
      set mSec to (seconds of d) as text
    end try
    try
      set mTo to (count of to recipients of m)
    end try
    try
      set mCc to (count of cc recipients of m)
    end try
    try
      set mBcc to (count of bcc recipients of m)
    end try
    set out to out & mId & tab & mSubject & tab & mSender & tab & mRead & tab & mMsgId & tab & mDate & tab & mYear & tab & mMonth & tab & mDay & tab & mHour & tab & mMin & tab & mSec & tab & (mTo as text) & tab & (mCc as text) & tab & (mBcc as text) & linefeed
  end repeat
  return out
end tell"""


def script_message_body(account: str, mailbox: str, index: int) -> str:
    return f"""tell application "Mail"
  set theBox to mailbox {_as_text(mailbox)} of account {_as_text(account)}
  set m to message {index} of theBox
  set ok to "0"
  set c to ""
  try
    set c to content of m
    set ok to "1"
  end try
  return ok & linefeed & c
end tell"""


def script_message_source(account: str, mailbox: str, index: int) -> str:
    return f"""tell application "Mail"
  set theBox to mailbox {_as_text(mailbox)} of account {_as_text(account)}
  set m to message {index} of theBox
  set ok to "0"
  set s to ""
  try
    set s to source of m
    set ok to "1"
  end try
  return ok & linefeed & s
end tell"""


def script_attachments(account: str, mailbox: str, index: int) -> str:
    return f"""tell application "Mail"
  set theBox to mailbox {_as_text(mailbox)} of account {_as_text(account)}
  set m to message {index} of theBox
  set ok to "0"
  set out to ""
  try
    repeat with att in mail attachments of m
      set aName to ""
      set aMime to ""
      set aSize to ""
      set aDown to ""
      try
        set aName to (name of att) as text
      end try
      try
        set aMime to (MIME type of att) as text
      end try
      try
        set aSize to (file size of att) as text
      end try
      try
        set aDown to (downloaded of att) as text
      end try
      set out to out & aName & tab & aMime & tab & aSize & tab & aDown & linefeed
    end repeat
    set ok to "1"
  end try
  return ok & linefeed & out
end tell"""


# --------------------------------------------------------------------- parsing --

def clean_field(value: str) -> str:
    """Flatten separators that would break the tab/linefeed wire format."""
    return (value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def split_rows(stdout: str) -> list:
    """Tab-separated rows, one per output line; blank lines dropped."""
    rows = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        rows.append([clean_field(cell) for cell in line.split("\t")])
    return rows


def parse_sentinel(stdout: str) -> tuple:
    """Split the ``ok`` sentinel line from a payload: returns (ok, payload)."""
    text = stdout or ""
    if "\n" not in text:
        return (text.strip() == "1", "")
    head, _, rest = text.partition("\n")
    return (head.strip() == "1", rest)


def parse_date_parts(year: str, month: str, day: str, hour: str, minute: str,
                     second: str) -> Optional[str]:
    """Render Mail's local date components as an ISO string with no timezone claim.

    Mail reports the date in the Mini's local timezone and AppleScript will not hand
    back the offset. Pretending otherwise would put a wrong UTC stamp into Grace, so the
    worker returns a local-time string and marks it as such.
    """
    try:
        return "%04d-%02d-%02dT%02d:%02d:%02d" % (int(year), int(month), int(day),
                                                  int(hour), int(minute), int(second))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- interface --

class MailTransport(abc.ABC):
    """One method per Mail operation the worker performs. All return ``Outcome``."""

    origin = O.REAL

    def __init__(self, *, adapter: str = "mail", account_id: Optional[str] = None):
        self.adapter = adapter
        self._account_id = account_id

    @property
    def account_id(self) -> Optional[str]:
        return self._account_id

    @abc.abstractmethod
    def identity(self) -> O.Outcome: ...

    @abc.abstractmethod
    def accounts(self) -> O.Outcome: ...

    @abc.abstractmethod
    def mailboxes(self, account: str) -> O.Outcome: ...

    @abc.abstractmethod
    def message_ids(self, account: str, mailbox: str, *,
                    max_scan: int = DEFAULT_MAX_SCAN) -> O.Outcome: ...

    @abc.abstractmethod
    def message_window(self, account: str, mailbox: str, *, start: int,
                       end: int) -> O.Outcome: ...

    @abc.abstractmethod
    def message_body(self, account: str, mailbox: str, *, index: int) -> O.Outcome: ...

    @abc.abstractmethod
    def message_source(self, account: str, mailbox: str, *, index: int) -> O.Outcome: ...

    @abc.abstractmethod
    def attachments(self, account: str, mailbox: str, *, index: int) -> O.Outcome: ...


# ------------------------------------------------------------- the real thing --

class AppleScriptMailTransport(MailTransport):
    """Read-only Mail.app access through AppleScript."""

    origin = O.REAL

    def __init__(self, *, runner: Optional[ScriptRunner] = None, adapter: str = "mail",
                 account_id: Optional[str] = None, timeout_s: int = 120,
                 bundle_candidates: Iterable[str] = MAIL_BUNDLE_CANDIDATES):
        super().__init__(adapter=adapter, account_id=account_id)
        self.runner = runner or OsascriptRunner()
        self.timeout_s = timeout_s
        self.bundle_candidates = tuple(bundle_candidates)

    # -- helpers -----------------------------------------------------------
    def _host_supported(self) -> Optional[O.Outcome]:
        if sys.platform != "darwin":
            return O.Outcome.unsupported(
                "the Mail adapter needs macOS with Mail.app; this host is "
                f"platform={sys.platform}. No mailbox was read.",
                reason="host_not_macos", adapter=self.adapter,
                account_id=self.account_id,
                data={"platform": sys.platform},
                next_action="run the Mini worker on Randy's Mac (Gate 2)")
        if not self.runner.available():
            return O.Outcome.unsupported(
                "osascript is not available at /usr/bin/osascript; no mailbox was read.",
                reason="osascript_unavailable", adapter=self.adapter,
                account_id=self.account_id)
        return None

    def _run(self, script: str, kind: str) -> O.Outcome:
        blocked = self._host_supported()
        if blocked is not None:
            return blocked
        result = self.runner.run(script, script_kind=kind, timeout_s=self.timeout_s)
        return classify(result, adapter=self.adapter, account_id=self.account_id)

    def mail_bundle(self) -> dict:
        """Mail.app's version, read from its bundle. This does not launch Mail."""
        for path in self.bundle_candidates:
            info = os.path.join(path, "Contents", "Info.plist")
            if not os.path.exists(info):
                continue
            version = None
            try:
                proc = subprocess.run(
                    ["/usr/bin/defaults", "read", info, "CFBundleShortVersionString"],
                    capture_output=True, text=True, timeout=20)
                if proc.returncode == 0:
                    version = (proc.stdout or "").strip() or None
            except (OSError, subprocess.SubprocessError):
                version = None
            return {"path": path, "info_plist_version": version}
        return {"path": None, "info_plist_version": None}

    # -- operations --------------------------------------------------------
    def identity(self) -> O.Outcome:
        blocked = self._host_supported()
        if blocked is not None:
            return blocked
        bundle = self.mail_bundle()
        running = self._run("application \"Mail\" is running", "is_running")
        data = {
            "platform": sys.platform,
            "mail_bundle": bundle,
            "mail_running": (running.data or {}).get("stdout", "").strip().lower()
            if running.usable else None,
            "probe": "read Mail.app's bundle Info.plist without launching it, then ask "
                     "the Apple event system whether Mail is running",
        }
        if not running.usable:
            data["running_check_outcome"] = running.code
            return O.Outcome.permanent(
                "could not determine whether Mail is running: " + (running.detail or ""),
                reason=running.reason or "identity_probe_failed", adapter=self.adapter,
                account_id=self.account_id, data=data,
                duration_ms=running.duration_ms)
        if not bundle["info_plist_version"]:
            return O.Outcome.partial(
                data, "Mail.app's bundle version could not be read; the observed version "
                      "of the installed build is unknown",
                reason="version_unread", adapter=self.adapter,
                account_id=self.account_id, duration_ms=running.duration_ms)
        return O.Outcome.ok(data, adapter=self.adapter, account_id=self.account_id,
                            duration_ms=running.duration_ms)

    def accounts(self) -> O.Outcome:
        out = self._run(script_accounts(), "accounts")
        if not out.usable:
            return out
        rows = split_rows((out.data or {}).get("stdout", ""))
        accounts = []
        for row in rows:
            row = (row + [""] * 5)[:5]
            name, acct_id, acct_type, enabled, addr_count = row
            accounts.append({
                "name": name,
                "provider_account_id_fingerprint": O.fingerprint(acct_id) if acct_id else None,
                "account_type": acct_type or None,
                "enabled": (enabled.lower() == "true") if enabled else None,
                "address_count": int(addr_count) if addr_count.isdigit() else 0,
            })
        return O.Outcome.ok({
            "accounts": accounts,
            "count": len(accounts),
            "privacy": ("account email addresses are not emitted by this worker; only names "
                        "and counts. Grace receives addresses in the Gate 3 ingest slice, "
                        "which is Randy's call to record."),
        }, adapter=self.adapter, account_id=self.account_id, duration_ms=out.duration_ms)

    def mailboxes(self, account: str) -> O.Outcome:
        out = self._run(script_mailboxes(account), "mailboxes")
        if not out.usable:
            return out
        rows = split_rows((out.data or {}).get("stdout", ""))
        boxes = []
        for row in rows:
            row = (row + [""] * 3)[:3]
            name, unread, mb_id = row
            boxes.append({"name": name,
                          "unread_count": int(unread) if unread.isdigit() else None,
                          "provider_mailbox_id_fingerprint":
                              O.fingerprint(mb_id) if mb_id else None})
        return O.Outcome.ok({"account": account, "mailboxes": boxes, "count": len(boxes)},
                            adapter=self.adapter, account_id=self.account_id,
                            duration_ms=out.duration_ms)

    def message_ids(self, account: str, mailbox: str, *,
                    max_scan: int = DEFAULT_MAX_SCAN) -> O.Outcome:
        out = self._run(script_message_ids(account, mailbox, max_scan), "message_ids")
        if not out.usable:
            return out
        rows = split_rows((out.data or {}).get("stdout", ""))
        if not rows or rows[0][0] != "meta":
            return O.Outcome.permanent(
                "Mail returned no id-scan header; the mailbox reference or the "
                "scripting terminology did not resolve",
                reason="id_scan_unparsable", adapter=self.adapter,
                account_id=self.account_id,
                data={"first_row": rows[0] if rows else None, "row_count": len(rows)},
                duration_ms=out.duration_ms)
        meta = (rows[0] + [""] * 5)[:5]
        total = int(meta[1]) if meta[1].isdigit() else None
        scanned = int(meta[2]) if meta[2].isdigit() else len(rows) - 1
        entries = []
        for row in rows[1:]:
            row = (row + [""] * 2)[:2]
            if not row[0]:
                continue
            entries.append({"internal_id": row[0],
                            "index": int(row[1]) if row[1].isdigit() else None})
        capped = bool(total is not None and scanned < total)
        data = {
            "account": account, "mailbox": mailbox,
            "total_count": total, "scanned_count": scanned, "capped": capped,
            "max_scan": max_scan,
            "entries": entries,
            "ordering_sample": {"first_index_date_text": meta[3] or None,
                                "last_index_date_text": meta[4] or None},
            "note": ("Mail exposes no server-side cursor; this is a bounded re-scan of "
                     "the mailbox's internal ids. Reading the end of the scan is not "
                     "reading the end of the mailbox."),
        }
        if capped:
            return O.Outcome.partial(
                data,
                f"scan capped at {scanned} of {total} messages in {mailbox}; "
                f"coverage beyond that point is unproven",
                reason="scan_capped", adapter=self.adapter, account_id=self.account_id,
                duration_ms=out.duration_ms)
        return O.Outcome.ok(data, adapter=self.adapter, account_id=self.account_id,
                            duration_ms=out.duration_ms)

    def message_window(self, account: str, mailbox: str, *, start: int,
                       end: int) -> O.Outcome:
        out = self._run(script_message_window(account, mailbox, start, end),
                        "message_window")
        if not out.usable:
            return out
        rows = split_rows((out.data or {}).get("stdout", ""))
        items = []
        for row in rows:
            row = (row + [""] * 15)[:15]
            (m_id, subject, sender, read, msg_id, date_text, year, month, day, hour,
             minute, second, to_count, cc_count, bcc_count) = row
            local_iso = parse_date_parts(year, month, day, hour, minute, second)
            items.append({
                "internal_id": m_id or None,
                "subject": subject or None,
                "sender_masked": O.mask_address(sender) if sender else None,
                "sender_fingerprint": O.fingerprint(sender) if sender else None,
                "read_status": (read.lower() == "true") if read else None,
                "rfc_message_id": msg_id or None,
                "source_time_local": local_iso,
                "source_time_text": date_text or None,
                "time_basis": "local_time_on_mini",
                "utc_offset_minutes": None,
                "recipient_counts": {
                    "to": int(to_count) if to_count.lstrip("-").isdigit() and int(to_count) >= 0 else None,
                    "cc": int(cc_count) if cc_count.lstrip("-").isdigit() and int(cc_count) >= 0 else None,
                    "bcc": int(bcc_count) if bcc_count.lstrip("-").isdigit() and int(bcc_count) >= 0 else None,
                },
            })
        missing = sorted({k for item in items for k, v in item.items() if v is None})
        return O.Outcome.ok({"items": items, "count": len(items),
                             "fields_unreadable": missing,
                             "date_note": ("Mail reports dates in the Mini's local timezone "
                                           "and AppleScript returns no offset; the stamp is "
                                           "labelled local, never converted to UTC by guess")},
                            adapter=self.adapter, account_id=self.account_id,
                            duration_ms=out.duration_ms)

    def message_body(self, account: str, mailbox: str, *, index: int) -> O.Outcome:
        out = self._run(script_message_body(account, mailbox, index), "message_body")
        if not out.usable:
            return out
        ok, payload = parse_sentinel((out.data or {}).get("stdout", ""))
        if not ok:
            return O.Outcome.partial(
                {"body": None, "body_state": "unavailable",
                 "unavailable_reason": "Mail did not return message content for this item"},
                "message content could not be read; this is not an empty message",
                reason="content_unavailable", adapter=self.adapter,
                account_id=self.account_id, duration_ms=out.duration_ms)
        text = payload.rstrip("\n")
        return O.Outcome.ok({
            "body": text,
            "body_state": "empty" if text == "" else "fetched",
            "body_length": len(text),
            "body_fingerprint": O.fingerprint(text),
        }, adapter=self.adapter, account_id=self.account_id, duration_ms=out.duration_ms)

    def message_source(self, account: str, mailbox: str, *, index: int) -> O.Outcome:
        out = self._run(script_message_source(account, mailbox, index), "message_source")
        if not out.usable:
            return out
        ok, payload = parse_sentinel((out.data or {}).get("stdout", ""))
        if not ok:
            return O.Outcome.unsupported(
                "this Mail build did not return the raw source of the message; the "
                "terminology is not confirmed on this version (PRD §11)",
                reason="raw_source_unsupported", adapter=self.adapter,
                account_id=self.account_id, duration_ms=out.duration_ms)
        text = payload.rstrip("\n")
        return O.Outcome.ok({
            "source": text, "source_length": len(text),
            "source_fingerprint": O.fingerprint(text),
        }, adapter=self.adapter, account_id=self.account_id, duration_ms=out.duration_ms)

    def attachments(self, account: str, mailbox: str, *, index: int) -> O.Outcome:
        out = self._run(script_attachments(account, mailbox, index), "attachments")
        if not out.usable:
            return out
        ok, payload = parse_sentinel((out.data or {}).get("stdout", ""))
        if not ok:
            return O.Outcome.unsupported(
                "attachment enumeration failed on this Mail build; the attachment "
                "terminology is not confirmed (PRD §11)",
                reason="attachment_enumeration_unsupported", adapter=self.adapter,
                account_id=self.account_id, duration_ms=out.duration_ms)
        atts = []
        for row in split_rows(payload):
            row = (row + [""] * 4)[:4]
            name, mime, size, downloaded = row
            atts.append({
                "filename": name or None,
                "mime_type": mime or None,
                "size_bytes": int(size) if size.isdigit() else None,
                "downloaded": (downloaded.lower() == "true") if downloaded else None,
            })
        not_local = [a for a in atts if a["downloaded"] is False]
        data = {"attachments": atts, "count": len(atts),
                "not_downloaded_locally": len(not_local),
                "note": ("an attachment Mail has not downloaded cannot be materialised "
                         "from the Mini; availability is reported per item (PRD §11)")}
        if not_local:
            return O.Outcome.partial(
                data,
                f"{len(not_local)} of {len(atts)} attachments are not present on this Mac; "
                f"their content is unavailable until Mail downloads them",
                reason="attachment_not_local", adapter=self.adapter,
                account_id=self.account_id, duration_ms=out.duration_ms)
        return O.Outcome.ok(data, adapter=self.adapter, account_id=self.account_id,
                            duration_ms=out.duration_ms)


# ------------------------------------------------------------------ the fixture --

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mail")
FIXTURE_SCENARIOS = ("granted", "permission_denied", "offline", "partial_history")


def fixture_path(scenario: str) -> str:
    if scenario not in FIXTURE_SCENARIOS:
        raise ValueError(f"unknown fixture scenario {scenario!r}; known: "
                         f"{', '.join(FIXTURE_SCENARIOS)}")
    return os.path.join(FIXTURE_DIR, f"{scenario}.json")


def load_fixture(scenario: str) -> dict:
    with open(fixture_path(scenario), "r", encoding="utf-8") as handle:
        return json.load(handle)


class RecordedMailTransport(MailTransport):
    """Answers the Mail interface from a recorded result. Never touches Mail."""

    origin = O.FIXTURE

    def __init__(self, fixture: dict, *, adapter: str = "mail",
                 account_id: Optional[str] = None):
        super().__init__(adapter=adapter, account_id=account_id)
        self.fixture = fixture
        self.scenario = fixture.get("scenario", "unknown")
        self.label = fixture.get("label") or O.fixture_label(
            adapter, f"recorded mailbox fixture '{self.scenario}'")
        self._fault = fixture.get("fault") or None

    # -- helpers -----------------------------------------------------------
    def _out(self, outcome: O.Outcome) -> O.Outcome:
        outcome.adapter = self.adapter
        outcome.origin = O.FIXTURE
        outcome.label = self.label
        return outcome

    def _faulted(self, operation: str) -> Optional[O.Outcome]:
        fault = self._fault
        if not fault:
            return None
        applies = fault.get("applies_to", "*")
        if applies != "*" and operation not in applies:
            return None
        code = fault.get("ae_code")
        message = fault.get("message", "")
        detail = (fault.get("detail") or
                  f"FIXTURE: recorded Mail automation error {code} during {operation}: "
                  f"{message}")
        kw = {"reason": fault.get("reason", "recorded_fixture_fault"),
              "data": {"ae_code": code, "ae_message": message, "script_kind": operation},
              "adapter": self.adapter, "account_id": self.account_id}
        if code == -1743:
            kw["next_action"] = ("grant this worker Automation access to Mail in System "
                                 "Settings (see mini/README.md); the capability stays "
                                 "unsupported until then")
            return self._out(O.Outcome.permission_denied(detail, **kw))
        if code in (-600, -609):
            kw["next_action"] = "launch Mail.app and retry"
            return self._out(O.Outcome.offline(detail, **kw))
        if code == -1712:
            kw["next_action"] = "retry the read with a longer timeout"
            return self._out(O.Outcome.retryable(detail, **kw))
        if code == -1708:
            kw["next_action"] = ("record this on the Gate 2 capability row; do not assume "
                                 "the operation exists")
            return self._out(O.Outcome.unsupported(detail, **kw))
        return self._out(O.Outcome.permanent(detail, **kw))

    def _messages(self, account: str, mailbox: str) -> list:
        return list((self.fixture.get("messages") or {}).get(f"{account}|{mailbox}", []))

    def _sorted(self, account: str, mailbox: str) -> list:
        return sorted(self._messages(account, mailbox), key=lambda m: int(m["index"]))

    # -- operations --------------------------------------------------------
    def identity(self) -> O.Outcome:
        faulted = self._faulted("identity")
        if faulted is not None:
            return faulted
        bundle = self.fixture.get("mail_bundle") or {}
        data = {
            "platform": self.fixture.get("platform", "fixture-macos"),
            "mail_bundle": {"path": bundle.get("path"),
                            "info_plist_version": bundle.get("short_version")},
            "mail_running": self.fixture.get("mail_running"),
            "probe": "FIXTURE: recorded identity result; nothing on this host was probed",
            "fixture_scenario": self.scenario,
        }
        return self._out(O.Outcome.ok(data, adapter=self.adapter))

    def accounts(self) -> O.Outcome:
        faulted = self._faulted("accounts")
        if faulted is not None:
            return faulted
        accounts = []
        for entry in self.fixture.get("accounts") or []:
            accounts.append({
                "name": entry["name"],
                "provider_account_id_fingerprint": O.fingerprint(entry["name"]),
                "account_type": entry.get("account_type"),
                "enabled": entry.get("enabled"),
                "address_count": entry.get("address_count", 0),
            })
        return self._out(O.Outcome.ok({"accounts": accounts, "count": len(accounts),
                                       "privacy": "FIXTURE: recorded, contains no addresses"},
                                      adapter=self.adapter))

    def mailboxes(self, account: str) -> O.Outcome:
        faulted = self._faulted("mailboxes")
        if faulted is not None:
            return faulted
        boxes = list((self.fixture.get("mailboxes") or {}).get(account, []))
        return self._out(O.Outcome.ok(
            {"account": account, "mailboxes": boxes, "count": len(boxes)},
            adapter=self.adapter))

    def message_ids(self, account: str, mailbox: str, *,
                    max_scan: int = DEFAULT_MAX_SCAN) -> O.Outcome:
        faulted = self._faulted("message_ids")
        if faulted is not None:
            return faulted
        messages = self._sorted(account, mailbox)
        total = self.fixture.get("total_count", len(messages))
        cap = min(int(self.fixture.get("scan_cap", max_scan)), max_scan)
        scanned = min(total, cap)
        entries = [{"internal_id": str(m["internal_id"]), "index": int(m["index"])}
                   for m in messages[:scanned]]
        sample = self.fixture.get("ordering_sample") or {}
        capped = scanned < total
        data = {
            "account": account, "mailbox": mailbox,
            "total_count": total, "scanned_count": scanned, "capped": capped,
            "max_scan": max_scan,
            "entries": entries,
            "ordering_sample": {
                "first_index_date_text": sample.get("first_index_date_text"),
                "last_index_date_text": sample.get("last_index_date_text"),
            },
            "note": "FIXTURE: recorded id scan; no mailbox was read",
        }
        if capped:
            return self._out(O.Outcome.partial(
                data,
                f"FIXTURE: scan capped at {scanned} of {total} messages in {mailbox}; "
                f"coverage beyond that point is unproven",
                reason="scan_capped", adapter=self.adapter))
        return self._out(O.Outcome.ok(data, adapter=self.adapter))

    def message_window(self, account: str, mailbox: str, *, start: int,
                       end: int) -> O.Outcome:
        faulted = self._faulted("message_window")
        if faulted is not None:
            return faulted
        items = []
        for m in self._sorted(account, mailbox):
            if not (start <= int(m["index"]) <= end):
                continue
            parts = m.get("date_parts") or [None] * 6
            local_iso = parse_date_parts(*[str(p) for p in parts]) if all(
                p is not None for p in parts) else None
            items.append({
                "internal_id": str(m["internal_id"]),
                "subject": m.get("subject"),
                "sender_masked": O.mask_address(m.get("sender", "")),
                "sender_fingerprint": O.fingerprint(m.get("sender", "")),
                "read_status": m.get("read"),
                "rfc_message_id": m.get("rfc_message_id"),
                "source_time_local": local_iso,
                "source_time_text": m.get("date_text"),
                "time_basis": "local_time_on_mini",
                "utc_offset_minutes": None,
                "recipient_counts": m.get("recipient_counts",
                                          {"to": None, "cc": None, "bcc": None}),
            })
        missing = sorted({k for item in items for k, v in item.items() if v is None})
        return self._out(O.Outcome.ok(
            {"items": items, "count": len(items), "fields_unreadable": missing,
             "date_note": "FIXTURE: recorded date rendering"},
            adapter=self.adapter))

    def _by_index(self, account: str, mailbox: str, index: int) -> Optional[dict]:
        for m in self._messages(account, mailbox):
            if int(m["index"]) == int(index):
                return m
        return None

    def message_body(self, account: str, mailbox: str, *, index: int) -> O.Outcome:
        faulted = self._faulted("message_body")
        if faulted is not None:
            return faulted
        m = self._by_index(account, mailbox, index)
        if m is None or not m.get("body_available", True):
            return self._out(O.Outcome.partial(
                {"body": None, "body_state": "unavailable",
                 "unavailable_reason": "FIXTURE: recorded result has no retrievable content"},
                "FIXTURE: message content could not be read; this is not an empty message",
                reason="content_unavailable", adapter=self.adapter))
        text = m.get("body") or ""
        return self._out(O.Outcome.ok({
            "body": text,
            "body_state": "empty" if text == "" else "fetched",
            "body_length": len(text), "body_fingerprint": O.fingerprint(text),
        }, adapter=self.adapter))

    def message_source(self, account: str, mailbox: str, *, index: int) -> O.Outcome:
        faulted = self._faulted("message_source")
        if faulted is not None:
            return faulted
        m = self._by_index(account, mailbox, index)
        if m is None or not m.get("source_available", True):
            return self._out(O.Outcome.unsupported(
                "FIXTURE: recorded result did not expose the raw source on this build",
                reason="raw_source_unsupported", adapter=self.adapter))
        text = m.get("source") or ""
        return self._out(O.Outcome.ok({
            "source": text, "source_length": len(text),
            "source_fingerprint": O.fingerprint(text)}, adapter=self.adapter))

    def attachments(self, account: str, mailbox: str, *, index: int) -> O.Outcome:
        faulted = self._faulted("attachments")
        if faulted is not None:
            return faulted
        m = self._by_index(account, mailbox, index)
        if m is None or not m.get("attachments_available", True):
            return self._out(O.Outcome.unsupported(
                "FIXTURE: attachment enumeration failed on the recorded build",
                reason="attachment_enumeration_unsupported", adapter=self.adapter))
        atts = [dict(a) for a in (m.get("attachments") or [])]
        not_local = [a for a in atts if a.get("downloaded") is False]
        data = {"attachments": atts, "count": len(atts),
                "not_downloaded_locally": len(not_local),
                "note": "FIXTURE: recorded attachment metadata"}
        if not_local:
            return self._out(O.Outcome.partial(
                data,
                f"FIXTURE: {len(not_local)} of {len(atts)} attachments are not present on "
                f"this Mac; their content is unavailable until Mail downloads them",
                reason="attachment_not_local", adapter=self.adapter))
        return self._out(O.Outcome.ok(data, adapter=self.adapter))
