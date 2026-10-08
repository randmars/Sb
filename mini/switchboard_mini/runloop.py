"""Foreground run loop with durable, resumable cursors.

Shaped for launchd on purpose: it runs in the foreground, logs one JSON document per
line to stdout, never forks and never daemonises, so launchd can supervise it directly
(a `.plist` template is in ``mini/launchd/``). It is equally usable from a terminal with
``--once``, which is what the tests drive.

Cursor discipline (the part that matters):

* The cursor advances only after a read that succeeded (``success``) or a read that
  returned a usable partial page. It is written to disk before the next poll.
* A typed failure -- offline, permission denied, timeout, unclassified automation error
  -- leaves the stored cursor untouched, so nothing is silently skipped after a failure.
* The state file is written atomically (temp file plus ``os.replace``) so a crash during
  a poll cannot leave a half-written cursor behind.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
from typing import Optional

from . import outcomes as O

STATE_VERSION = 1


class CursorStore:
    """Durable per-scope cursor state in a single JSON file."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._state = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return {"version": STATE_VERSION, "scopes": {}}
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return {"version": STATE_VERSION, "scopes": {}}
        state.setdefault("scopes", {})
        return state

    def _save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             prefix=".cursor-", suffix=".tmp",
                                             delete=False)
        try:
            json.dump(self._state, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, self.path)

    def scope_key(self, account: str, scope: str) -> str:
        return f"{account}|{scope}"

    def get(self, account: str, scope: str) -> Optional[str]:
        entry = self._state["scopes"].get(self.scope_key(account, scope)) or {}
        return entry.get("cursor")

    def record(self, account: str, scope: str, *, code: str, cursor: Optional[str],
               detail: str = "", advanced: bool) -> None:
        key = self.scope_key(account, scope)
        entry = self._state["scopes"].setdefault(key, {})
        entry["last_outcome"] = code
        entry["last_detail"] = detail
        entry["last_poll_at"] = O.now()
        entry["polls"] = int(entry.get("polls", 0)) + 1
        if advanced:
            entry["cursor"] = cursor
            entry["cursor_updated_at"] = O.now()
            entry["caught_up"] = cursor is None
        self._save()

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._state))


def default_state_path() -> str:
    override = os.environ.get("SWITCHBOARD_MINI_STATE")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".switchboard-mini", "state.json")


def run_once(adapter, *, account: str, mailbox: str, limit: int,
             store: Optional[CursorStore] = None) -> dict:
    """One poll: read one bounded page, report it, advance the cursor only if usable."""
    from .mail_adapter import scope_ref

    scope = scope_ref(mailbox)
    cursor = store.get(account, scope) if store else None
    outcome = adapter.enumerate(account, scope, limit=limit, cursor=cursor)
    if outcome.usable:
        next_cursor = (outcome.data or {}).get("next_cursor")
        if store:
            store.record(account, scope, code=outcome.code, cursor=next_cursor,
                         detail=outcome.detail, advanced=True)
    else:
        if store:
            store.record(account, scope, code=outcome.code, cursor=cursor,
                         detail=outcome.detail, advanced=False)
    return {
        "event": "poll",
        "account": account,
        "mailbox": mailbox,
        "cursor_used": cursor,
        "cursor_advanced": bool(outcome.usable),
        "outcome": outcome.to_dict(),
    }


def run_loop(adapter, *, account: Optional[str] = None, mailbox: Optional[str] = None,
             limit: int = 25, interval: float = 30.0, once: bool = False,
             state_path: Optional[str] = None, out=sys.stdout) -> int:
    """Foreground poll loop. Returns a process exit status."""
    store = CursorStore(state_path or default_state_path())

    if not account:
        accounts = adapter.accounts()
        if not accounts.usable:
            _write(out, {"event": "startup_failed",
                         "reason": "no account could be listed to poll",
                         "outcome": accounts.to_dict()})
            return 2
        names = [a["name"] for a in accounts.data.get("accounts", []) if a.get("name")]
        if not names:
            _write(out, {"event": "startup_failed",
                         "reason": "Mail reported no accounts on this Mac",
                         "outcome": accounts.to_dict()})
            return 2
        account = names[0]
    if not mailbox:
        boxes = adapter.mailboxes(account)
        names = [b["name"] for b in (boxes.data or {}).get("mailboxes", [])] \
            if boxes.usable else []
        mailbox = "INBOX" if "INBOX" in names else (names[0] if names else None)
        if not mailbox:
            _write(out, {"event": "startup_failed",
                         "reason": f"no mailbox could be listed for account {account!r}",
                         "outcome": boxes.to_dict()})
            return 2

    _write(out, {"event": "worker_start",
                 "account": account, "mailbox": mailbox, "interval_s": interval,
                 "once": once, "state_path": store.path,
                 "adapter": adapter.name, "adapter_version": adapter.version,
                 "origin": adapter.origin,
                 "adapter_is_real": bool(getattr(adapter, "adapter_is_real", False)),
                 # Nothing has been read yet at this point, so this is false by
                 # construction: each poll document below carries its own value.
                 "real_source_connected": False,
                 "source_contacted": False,
                 "note": ("foreground loop; launchd-shaped (no fork, no daemon). Reads "
                          "only — this worker holds no send path.")})

    stopping = {"flag": False}

    def _handle(signum, _frame):            # pragma: no cover - exercised via signal test
        stopping["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):       # not the main thread / unsupported platform
            pass

    while not stopping["flag"]:
        batch = run_once(adapter, account=account, mailbox=mailbox, limit=limit, store=store)
        _write(out, batch)
        if once:
            break
        if batch["outcome"]["code"] not in (O.SUCCESS, O.PARTIAL):
            _write(out, {"event": "backing_off",
                         "reason": batch["outcome"]["code"],
                         "detail": batch["outcome"]["detail"],
                         "note": "the cursor was not advanced; the next poll retries the "
                                 "same page"})
        deadline = time.monotonic() + max(0.0, float(interval))
        while not stopping["flag"] and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    _write(out, {"event": "worker_stop", "reason": "signal or single pass complete",
                 "state": store.snapshot()})
    return 0


def _write(out, document: dict) -> None:
    out.write(O.emit(document) + "\n")
    out.flush()
