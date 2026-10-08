"""Typed outcomes and the labelling rule for the Mini worker.

The Mini worker installs onto Randy's Mac *on its own*: standard library only, no
third-party imports, no virtualenv, and no dependency on the Grace repository.
This module therefore restates the typed-outcome vocabulary that ``grace/contracts.py``
defines rather than importing it, and ``tests/test_mini_contract.py`` asserts that the
two vocabularies have not drifted apart.

Labelling rule (never optional). Every value the worker emits carries an explicit
``origin``:

* ``real``    -- produced by this run, against Mail.app on this Mac. Only the code
                 path that actually invoked ``osascript`` against the real
                 application may set it.
* ``fixture`` -- answered from a recorded result shipped in this repository
                 (``--fixture-mode``) or supplied by a test. It always carries a
                 ``FIXTURE:`` label and a disclaimer, and always reports
                 ``real_source_connected: false``.

Nothing in this worker is a live mock adapter: a fixture read is a synthetic
recording, and it says so in every document it produces.

``partial_history``: Grace expresses "the adapter reached the end of this query's
accessible result set, which is not the same as the end of the mailbox" as
``code='partial'`` plus ``data.coverage.coverage_state='partial_history'`` with a
``gap_reason`` (see ``grace/adapters.py``). The Mini worker uses exactly that shape so
Grace can consume these rows without translation.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# --------------------------------------------------------------------- labels --

REAL = "real"
FIXTURE = "fixture"
FIXTURE_LABEL_PREFIX = "FIXTURE:"

REAL_DISCLAIMER = None
FIXTURE_DISCLAIMER = (
    "FIXTURE — answered from a recorded result shipped in this repository in "
    "--fixture-mode. No Mail.app was contacted and no mailbox was read; these are not "
    "observations of any real mailbox or account."
)


def fixture_label(adapter: str, detail: Optional[str] = None) -> str:
    label = f"{FIXTURE_LABEL_PREFIX}{adapter}"
    if detail:
        label = f"{label}({detail})"
    return label


def is_fixture_label(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(FIXTURE_LABEL_PREFIX)


# ----------------------------------------------------------------- typed codes --
# Mirrors grace/contracts.py ADAPTER_OUTCOMES exactly; tests assert the equality.

SUCCESS = "success"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"
PERMISSION_DENIED = "permission_denied"
OFFLINE = "offline"
RATE_LIMITED = "rate_limited"
RETRYABLE_ERROR = "retryable_error"
PERMANENT_ERROR = "permanent_error"
OUTCOME_UNKNOWN = "outcome_unknown"

ADAPTER_OUTCOMES = (
    SUCCESS, PARTIAL, UNSUPPORTED, PERMISSION_DENIED, OFFLINE,
    RATE_LIMITED, RETRYABLE_ERROR, PERMANENT_ERROR, OUTCOME_UNKNOWN,
)

# Coverage states (PRD §6). ``partial_history`` never appears as an outcome code; it is
# carried in ``data.coverage.coverage_state`` next to a ``gap_reason``.
COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL_HISTORY = "partial_history"
COVERAGE_UNKNOWN = "unknown"


# ------------------------------------------------------------ permission states --

PERMISSION_GRANTED = "granted"
PERMISSION_STATE_DENIED = "denied"
PERMISSION_NOT_DETERMINED = "not_determined"
PERMISSION_NOT_APPLICABLE = "not_applicable"

PERMISSION_STATES = (PERMISSION_GRANTED, PERMISSION_STATE_DENIED,
                     PERMISSION_NOT_DETERMINED, PERMISSION_NOT_APPLICABLE)
# NOTE: the outcome code for a denied operation is ``PERMISSION_DENIED``
# ("permission_denied", above). PERMISSION_STATE_DENIED is the *permission_state* value
# reported on a probe row, which is the state of the macOS Automation grant itself.


# ---------------------------------------------------------------------- clock ---


def now() -> str:
    """UTC ISO-8601 with microseconds, lexicographically sortable (matches Grace)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def fingerprint(value: str, *, length: int = 16) -> str:
    """Stable, non-reversible fingerprint.

    Probe evidence and worker output must not carry mailbox content or addresses. Every
    identifier the worker reports is either an opaque fingerprint or a masked form, so a
    probe transcript can be pasted into a report without leaking Randy's mail.
    """
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def mask_address(value: str) -> str:
    """``randy@example.com`` -> ``r***y@example.com``; never the full local part.

    ``Alex Rivera <alex.rivera@example.com>`` keeps its display name and masks the
    address, because a Mail ``sender`` field is usually both.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if "<" in text and text.endswith(">"):
        name, _, address = text.partition("<")
        return f"{name.strip()} <{_mask_local_part(address[:-1])}>"
    return _mask_local_part(text)


def _mask_local_part(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if "@" not in text:
        return text[:1] + "***"
    local, _, domain = text.partition("@")
    if len(local) <= 2:
        masked_local = local[:1] + "***"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


@dataclass
class Outcome:
    """Typed adapter result. Never an exception, never a silent empty success."""

    code: str
    detail: str = ""
    reason: str = ""
    data: Optional[dict] = None
    origin: str = REAL
    label: Optional[str] = None
    adapter: Optional[str] = None
    account_id: Optional[str] = None
    submitted: bool = False
    partial: bool = False
    retry_after: Optional[str] = None
    next_action: Optional[str] = None
    duration_ms: Optional[int] = None

    # -- constructors ------------------------------------------------------
    @classmethod
    def ok(cls, data: dict, **kw: Any) -> "Outcome":
        return cls(SUCCESS, data=data, **kw)

    @classmethod
    def partial(cls, data: dict, detail: str, **kw: Any) -> "Outcome":
        return cls(PARTIAL, data=data, detail=detail, partial=True, **kw)

    @classmethod
    def unsupported(cls, detail: str, **kw: Any) -> "Outcome":
        return cls(UNSUPPORTED, detail=detail, **kw)

    @classmethod
    def permission_denied(cls, detail: str, **kw: Any) -> "Outcome":
        return cls(PERMISSION_DENIED, detail=detail, **kw)

    @classmethod
    def offline(cls, detail: str, **kw: Any) -> "Outcome":
        return cls(OFFLINE, detail=detail, **kw)

    @classmethod
    def rate_limited(cls, detail: str, **kw: Any) -> "Outcome":
        return cls(RATE_LIMITED, detail=detail, **kw)

    @classmethod
    def retryable(cls, detail: str, **kw: Any) -> "Outcome":
        return cls(RETRYABLE_ERROR, detail=detail, **kw)

    @classmethod
    def permanent(cls, detail: str, **kw: Any) -> "Outcome":
        return cls(PERMANENT_ERROR, detail=detail, **kw)

    @classmethod
    def uncertain(cls, detail: str, **kw: Any) -> "Outcome":
        return cls(OUTCOME_UNKNOWN, detail=detail, submitted=True, **kw)

    # -- helpers -----------------------------------------------------------
    @property
    def succeeded(self) -> bool:
        return self.code == SUCCESS

    @property
    def usable(self) -> bool:
        """Data may be used, but the caller must also surface the partial state."""
        return self.code in (SUCCESS, PARTIAL)

    @property
    def is_real(self) -> bool:
        return self.origin == REAL

    @property
    def retryable_as_read(self) -> bool:
        return self.code in (OFFLINE, RATE_LIMITED, RETRYABLE_ERROR, PARTIAL)

    def to_dict(self) -> dict:
        payload = {
            "code": self.code,
            "detail": self.detail,
            "reason": self.reason,
            "submitted": self.submitted,
            "partial": self.partial,
            "data": self.data,
            "adapter": self.adapter,
            "account_id": self.account_id,
            "origin": self.origin,
            "real_source_connected": self.is_real,
            "fixture_mode": not self.is_real,
            "retry_after": self.retry_after,
            "next_action": self.next_action,
            "duration_ms": self.duration_ms,
        }
        if not self.is_real:
            payload["label"] = self.label or fixture_label(self.adapter or "mini")
            payload["disclaimer"] = FIXTURE_DISCLAIMER
        if self.origin not in (REAL, FIXTURE):
            raise AssertionError(f"Outcome.to_dict: origin must be {REAL!r} or {FIXTURE!r}, "
                                 f"got {self.origin!r}")
        if not self.is_real and not is_fixture_label(payload.get("label")):
            raise AssertionError("Outcome.to_dict: a non-real value is not labelled")
        return payload


# ------------------------------------------------------------------ reporting ---


def emit(document: dict, *, pretty: bool = False) -> str:
    """Canonical JSON. Keys sorted so two runs are diffable."""
    if pretty:
        return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False)
    return json.dumps(document, sort_keys=True, ensure_ascii=False)


def python_supported() -> bool:
    """The worker targets the macOS system Python, which is 3.9 or newer."""
    return sys.version_info >= (3, 9)
