"""Core contracts: typed outcomes, independent state enums, clock, ids, hashing.

Everything in Grace speaks in *typed results* rather than exceptions for expected
failure modes (PRD §11/§12 "Use typed results such as success, partial, unsupported,
permission_denied, offline, rate_limited, retryable_error, permanent_error and
outcome_unknown"). Exceptions are reserved for programming errors.

Label rule (PRD §12, Gate 1 "Mocks must be visibly labeled"): any value that came
from a mock adapter is tagged ``origin='mock'`` and carries a ``mock_label`` that
starts with ``MOCK:``. ``assert_labelled()`` is the single enforcement point, used
by the store on write and by the CLI on output.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

# --------------------------------------------------------------------- labels --

MOCK = "mock"
REAL = "real"
MOCK_LABEL_PREFIX = "MOCK:"
MOCK_DISCLAIMER = (
    "MOCK — produced by a labelled mock adapter in this repository. No real Mail, "
    "Beeper, Contacts or Hermes source was contacted; no external message exists."
)


def mock_label(adapter: str, detail: str | None = None) -> str:
    """Human-readable label attached to every mocked value."""
    label = f"{MOCK_LABEL_PREFIX}{adapter}"
    if detail:
        label = f"{label}({detail})"
    return label


def is_mock_label(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MOCK_LABEL_PREFIX)


def assert_labelled(origin: str, label: Any, where: str) -> None:
    """Enforce the labelling invariant. Raises AssertionError (a bug, not a state)."""
    if origin not in (MOCK, REAL):
        raise AssertionError(f"{where}: origin must be 'mock' or 'real', got {origin!r}")
    if origin == MOCK and not is_mock_label(label):
        raise AssertionError(f"{where}: mocked value is not labelled (mock_label={label!r})")


def find_unlabelled_mock(obj: Any, path: str = "$") -> list[str]:
    """Recursively report mock-tagged values that lack a mock label."""
    problems: list[str] = []
    if isinstance(obj, Mapping):
        origin = obj.get("origin", obj.get("source_kind"))
        if origin == MOCK and not (
            is_mock_label(obj.get("mock_label")) or is_mock_label(obj.get("label"))
        ):
            problems.append(f"{path}: origin=mock without a MOCK: label")
        for key, value in obj.items():
            problems.extend(find_unlabelled_mock(value, f"{path}.{key}"))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            problems.extend(find_unlabelled_mock(value, f"{path}[{i}]"))
    return problems


# ---------------------------------------------------------------------- time ---


def now() -> str:
    """UTC ISO-8601 timestamp with microseconds; lexicographically sortable."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def plus(seconds: int | float) -> str:
    return iso(now_dt() + timedelta(seconds=seconds))


def is_past(value: Optional[str]) -> bool:
    if not value:
        return False
    return parse_iso(value) <= now_dt()


# ----------------------------------------------------------------------- ids ---


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic id for fixtures: same inputs, same id, every run."""
    seed = "|".join(str(p) for p in parts)
    digest = uuid.uuid5(uuid.NAMESPACE_URL, "switchboard:" + seed).hex[:16]
    return f"{prefix}_{digest}"


# ------------------------------------------------------------------- hashing ---


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------ adapter states --

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

# Read-only / demonstrably idempotent operations may be retried with bounded
# backoff. Post-submission uncertainty never enters this set (PRD §13, §10).
RETRYABLE_READ_OUTCOMES = frozenset({OFFLINE, RATE_LIMITED, RETRYABLE_ERROR, PARTIAL})
# Failures that prove nothing was submitted and therefore may be retried under
# the same authorization if scope is unchanged (PRD §13).
RETRYABLE_EFFECT_OUTCOMES = frozenset({OFFLINE, RATE_LIMITED, RETRYABLE_ERROR})

HONEST_EMPTY_ALLOWED = True  # a successful read may legitimately return nothing


@dataclass
class Outcome:
    """Typed adapter result. Never an exception, never a silent empty success."""

    code: str
    detail: str = ""
    reason: str = ""
    data: Optional[dict] = None
    provenance: str = REAL                 # 'mock' | 'real' — where the value came from
    label: Optional[str] = None
    adapter: Optional[str] = None
    account_id: Optional[str] = None
    submitted: bool = False                # may a side effect already have reached the source?
    # NOTE (defect fix, 2026-10-08): this field is ``is_partial`` and NOT ``partial``,
    # for the same reason as in ``mini/switchboard_mini/outcomes.py``. A dataclass field
    # whose name is later shadowed by the ``partial()`` constructor takes that method as
    # its default, so ``to_dict()`` handed a bound method to json.dumps and every
    # serialisation of a non-partial Outcome raised
    # "TypeError: Object of type method is not JSON serializable".
    # The JSON key stays ``partial`` for consumers.
    is_partial: bool = False
    retry_after: Optional[str] = None
    next_action: Optional[str] = None

    # -- constructors ------------------------------------------------------
    @classmethod
    def ok(cls, data: dict, **kw: Any) -> "Outcome":
        return cls(SUCCESS, data=data, **kw)

    @classmethod
    def partial(cls, data: dict, detail: str, **kw: Any) -> "Outcome":
        return cls(PARTIAL, data=data, detail=detail, is_partial=True, **kw)

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
    def mocked(self) -> bool:
        return self.provenance == MOCK

    @property
    def retryable_as_read(self) -> bool:
        return self.code in RETRYABLE_READ_OUTCOMES

    def to_dict(self) -> dict:
        payload = {
            "code": self.code,
            "detail": self.detail,
            "reason": self.reason,
            "submitted": self.submitted,
            "partial": bool(self.is_partial),
            "data": self.data,
            "adapter": self.adapter,
            "account_id": self.account_id,
            "origin": self.provenance,
            "mocked": self.mocked,
            "retry_after": self.retry_after,
            "next_action": self.next_action,
        }
        if self.provenance == MOCK:
            payload["mock_label"] = self.label or mock_label(self.adapter or "unknown")
            payload["disclaimer"] = MOCK_DISCLAIMER
        assert_labelled(self.provenance, payload.get("mock_label"), "Outcome.to_dict")
        return payload
    # NOTE: Outcome.to_dict() of a REAL outcome has no mock_label by design; the
    # CLI adds a top-level "mocked": false marker for real runs.


# --------------------------------------------------------------- job states ---


class JobState:
    """PRD §9 lifecycle. Exhaustive; no 'unknown' bucket that hides work."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_SOURCE = "waiting_for_source"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    READY_FOR_REVIEW = "ready_for_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"

    ALL = (QUEUED, RUNNING, WAITING_FOR_SOURCE, WAITING_FOR_USER, WAITING_FOR_APPROVAL,
           READY_FOR_REVIEW, SUCCEEDED, FAILED, CANCELLED, SUPERSEDED)
    ACTIVE = (QUEUED, RUNNING, WAITING_FOR_SOURCE, WAITING_FOR_USER, WAITING_FOR_APPROVAL,
              READY_FOR_REVIEW)
    TERMINAL = (SUCCEEDED, FAILED, CANCELLED, SUPERSEDED)


JOB_TRANSITIONS: dict[str, tuple[str, ...]] = {
    JobState.QUEUED: (JobState.RUNNING, JobState.WAITING_FOR_SOURCE, JobState.WAITING_FOR_USER,
                      JobState.WAITING_FOR_APPROVAL, JobState.READY_FOR_REVIEW, JobState.FAILED,
                      JobState.CANCELLED, JobState.SUPERSEDED),
    JobState.RUNNING: (JobState.WAITING_FOR_SOURCE, JobState.WAITING_FOR_USER,
                       JobState.WAITING_FOR_APPROVAL, JobState.READY_FOR_REVIEW,
                       JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
                       JobState.SUPERSEDED, JobState.QUEUED),
    JobState.WAITING_FOR_SOURCE: (JobState.QUEUED, JobState.RUNNING, JobState.WAITING_FOR_USER,
                                  JobState.READY_FOR_REVIEW, JobState.SUCCEEDED,
                                  JobState.FAILED, JobState.CANCELLED, JobState.SUPERSEDED),
    JobState.WAITING_FOR_USER: (JobState.QUEUED, JobState.RUNNING, JobState.FAILED,
                                JobState.CANCELLED, JobState.SUPERSEDED),
    JobState.WAITING_FOR_APPROVAL: (JobState.QUEUED, JobState.RUNNING, JobState.READY_FOR_REVIEW,
                                    JobState.WAITING_FOR_SOURCE, JobState.WAITING_FOR_USER,
                                    JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
                                    JobState.SUPERSEDED),
    JobState.READY_FOR_REVIEW: (JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED,
                                JobState.WAITING_FOR_APPROVAL, JobState.FAILED,
                                JobState.CANCELLED, JobState.SUPERSEDED),
    JobState.SUCCEEDED: (JobState.SUPERSEDED,),
    JobState.FAILED: (JobState.QUEUED, JobState.CANCELLED, JobState.SUPERSEDED),
    JobState.CANCELLED: (JobState.SUPERSEDED,),
    JobState.SUPERSEDED: (),
}


class EffectState:
    """PRD §10 outbound operation ledger states."""

    PREPARED = "prepared"
    APPROVED = "approved"
    DISPATCHING = "dispatching"
    PROVIDER_ACCEPTED = "provider_accepted"
    PROVIDER_PENDING = "provider_pending"
    CONFIRMED_SENT = "confirmed_sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"

    ALL = (PREPARED, APPROVED, DISPATCHING, PROVIDER_ACCEPTED, PROVIDER_PENDING,
           CONFIRMED_SENT, FAILED, CANCELLED, OUTCOME_UNKNOWN)
    TERMINAL = (CONFIRMED_SENT, FAILED, CANCELLED, OUTCOME_UNKNOWN)


EFFECT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    EffectState.PREPARED: (EffectState.APPROVED, EffectState.CANCELLED, EffectState.FAILED),
    EffectState.APPROVED: (EffectState.DISPATCHING, EffectState.CANCELLED, EffectState.FAILED),
    EffectState.DISPATCHING: (EffectState.PROVIDER_ACCEPTED, EffectState.PROVIDER_PENDING,
                              EffectState.CONFIRMED_SENT, EffectState.FAILED,
                              EffectState.OUTCOME_UNKNOWN),
    EffectState.PROVIDER_ACCEPTED: (EffectState.CONFIRMED_SENT, EffectState.OUTCOME_UNKNOWN,
                                    EffectState.FAILED),
    EffectState.PROVIDER_PENDING: (EffectState.CONFIRMED_SENT, EffectState.OUTCOME_UNKNOWN,
                                   EffectState.FAILED),
    EffectState.CONFIRMED_SENT: (),
    EffectState.FAILED: (EffectState.OUTCOME_UNKNOWN, EffectState.CANCELLED),
    EffectState.CANCELLED: (),
    EffectState.OUTCOME_UNKNOWN: (EffectState.CONFIRMED_SENT, EffectState.FAILED,
                                  EffectState.CANCELLED),
}


class ApprovalState:
    GRANTED = "granted"
    CONSUMED = "consumed"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    REVOKED = "revoked"

    ALL = (GRANTED, CONSUMED, INVALIDATED, EXPIRED, REVOKED)
    OPEN = (GRANTED,)


class VerificationLevel:
    """How strong the outbound evidence is. Never upgrade this without evidence."""

    PROVIDER_ACCEPTED = "provider_accepted"     # source recorded the submission
    PROVIDER_PENDING = "provider_pending"       # source returned a pending id
    CONFIRMED_SENT = "confirmed_sent"           # confirmed by a read-back/reconcile
    UNVERIFIED = "unverified"                   # outcome unknown, review required
    NONE = "none"


class ReviewState:
    """Independent of job state and of source read state (PRD §12, §5)."""

    NONE = "none"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"

    ALL = (NONE, AWAITING_REVIEW, AWAITING_INPUT, AWAITING_APPROVAL, BLOCKED)


class QueueState:
    NEEDS_ME = "needs_me"
    WORKING = "working"
    IDLE = "idle"

    ALL = (NEEDS_ME, WORKING, IDLE)


# --------------------------------------------------------- derived job state ---

#: job_state -> (queue_state, review_state, needs_me_reason).
#:
#: This is the single, pure derivation of the triage filter state from the lifecycle
#: state. It is applied **per job** (``job.queue_state`` and friends) and never per
#: conversation directly: a conversation's own state is the aggregate of its jobs
#: (``ledger._recompute_workspace_state``), so two jobs in one conversation cannot
#: overwrite each other's state (PRD §5 independent states, §12; T04/T05/T12).
JOB_FILTER_STATES: dict[str, tuple[str, str, "Optional[str]"]] = {
    JobState.QUEUED: (QueueState.WORKING, ReviewState.NONE, None),
    JobState.RUNNING: (QueueState.WORKING, ReviewState.NONE, None),
    JobState.WAITING_FOR_SOURCE: (QueueState.WORKING, ReviewState.NONE, "waiting_for_source"),
    JobState.WAITING_FOR_USER: (QueueState.NEEDS_ME, ReviewState.AWAITING_INPUT, "question"),
    JobState.WAITING_FOR_APPROVAL: (QueueState.NEEDS_ME, ReviewState.AWAITING_APPROVAL,
                                    "approval"),
    JobState.READY_FOR_REVIEW: (QueueState.NEEDS_ME, ReviewState.AWAITING_REVIEW, "draft"),
    JobState.SUCCEEDED: (QueueState.IDLE, ReviewState.NONE, None),
    JobState.FAILED: (QueueState.NEEDS_ME, ReviewState.BLOCKED, "failure"),
    JobState.CANCELLED: (QueueState.IDLE, ReviewState.NONE, "cancelled"),
    JobState.SUPERSEDED: (QueueState.IDLE, ReviewState.NONE, "superseded"),
}

#: Which needs-me reason wins when one conversation carries several of them. Most urgent
#: first: an uncertain side effect or a failure must never be hidden behind a draft.
NEEDS_ME_REASON_PRIORITY: tuple[str, ...] = (
    "untriaged_message", "uncertain_effect", "failure", "blocked", "approval",
    "question", "draft", "waiting_for_source", "cancelled", "superseded",
)


def most_urgent_reason(reasons: "Iterable[str]") -> Optional[str]:
    """The most urgent of several needs-me reasons, or None when there is none."""
    present = [r for r in NEEDS_ME_REASON_PRIORITY if r in set(reasons)]
    if present:
        return present[0]
    remaining = sorted({r for r in reasons if r})
    return remaining[0] if remaining else None


class ArchiveState:
    """Archive is a state, not a delete (PRD §8, §12)."""

    ACTIVE = "active"
    ARCHIVED = "archived"

    ALL = (ACTIVE, ARCHIVED)


class DeletionState:
    """Application tombstone. Deleting here never removes a source row (R08)."""

    RETAINED = "retained"
    DELETED = "deleted"

    ALL = (RETAINED, DELETED)


class ErrorCategory:
    """Classified failure reason recorded on effect attempts (PRD §13)."""

    TRANSIENT_NETWORK = "transient_network"
    RATE_LIMITED = "rate_limited"
    AUTH_EXPIRED = "auth_expired"
    PERMISSION_REVOKED = "permission_revoked"
    UNSUPPORTED = "unsupported"
    PERMANENT_CONTENT = "permanent_content"
    SOURCE_REJECTED = "source_rejected"
    UNKNOWN = "unknown"


# -------------------------------------------------------------- app results ---

OK = "ok"
CONFLICT = "conflict"
NOT_FOUND = "not_found"
INVALID = "invalid"
DENIED = "denied"
IDEMPOTENT_REPLAY = "idempotent_replay"
ALREADY_IN_STATE = "already_in_state"
RETRY_REFUSED_UNCERTAIN = "retry_refused_uncertain"
INTERNAL = "internal"


@dataclass
class OpResult:
    """Result of an application operation. Conflicts return the current version."""

    code: str
    detail: str = ""
    data: Optional[dict] = None
    current_version: Optional[int] = None
    operation_id: Optional[str] = None
    provenance: str = REAL
    label: Optional[str] = None
    mocked: bool = False

    @property
    def ok(self) -> bool:
        return self.code in (OK, IDEMPOTENT_REPLAY, ALREADY_IN_STATE)

    def to_dict(self) -> dict:
        payload = {
            "code": self.code,
            "detail": self.detail,
            "data": self.data,
            "current_version": self.current_version,
            "operation_id": self.operation_id,
            "origin": self.provenance,
            "mocked": self.mocked,
        }
        if self.mocked:
            payload["mock_label"] = self.label or mock_label("service")
            payload["disclaimer"] = MOCK_DISCLAIMER
            assert_labelled(MOCK, payload["mock_label"], "OpResult.to_dict")
        return payload


# ------------------------------------------------------------------ utilities --

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str) -> str:
    """Attachment materialisation must sanitize names (PRD §6)."""
    cleaned = _SAFE_NAME.sub("_", name).lstrip(".") or "unnamed"
    return cleaned[:120]


def bounded(items: Iterable[Any], limit: int) -> list:
    return list(items)[: max(0, limit)]
