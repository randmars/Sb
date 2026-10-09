"""Typed outcomes and the labelling rule for the Mini worker.

The Mini worker installs onto Randy's Mac *on its own*: standard library only, no
third-party imports, no virtualenv, and no dependency on the Grace repository.
This module therefore restates the typed-outcome vocabulary that ``grace/contracts.py``
defines rather than importing it, and ``tests/test_mini_contract.py`` asserts that the
two vocabularies have not drifted apart.

Labelling rule (never optional). Every value the worker emits carries an explicit
``origin``:

* ``real``    -- answered by the *real* adapter rather than by a recorded fixture. It
                 says which twin answered; it is **not** a claim that Mail was
                 contacted. A real adapter on a host with no Mail (this Linux computer,
                 or a Mac where automation was refused) is still ``origin: real`` while
                 nothing was read.
* ``fixture`` -- answered from a recorded result shipped in this repository
                 (``--fixture-mode``) or supplied by a test. It always carries a
                 ``FIXTURE:`` label and a disclaimer, and always reports
                 ``real_source_connected: false``.

Two further fields keep the provenance honest, and they are deliberately separate:

* ``adapter_is_real`` -- the adapter behind this document is the real one (AppleScript
  against Mail.app) rather than a fixture twin. This is the meaning ``origin`` alone used
  to be asked to carry.
* ``source_contacted`` -- this run actually obtained *this* result from the source. Set
  only by the transport that performed the read; a fixture twin, a refusal that never
  reached the source (``host_not_macos``, no ``osascript``), and a command that contacts
  nothing (``manifest``) all leave it false.
* ``real_source_connected`` (derived, never set directly) -- ``adapter_is_real and
  source_contacted``. It means "a real source was contacted and this value came from it",
  which is the same meaning Grace's health output, the receipts and the mock-labelling
  rules give it. It is false whenever no source was contacted, on every command, in both
  modes.

Serialization is not optional either: every document this module hands to ``emit`` is
validated, and a document that is not JSON-safe raises :class:`SerializationError` naming
the offending key path instead of letting ``json.dumps`` fail somewhere in the caller.
(That check exists because of a real defect: a dataclass field whose name was shadowed by
a constructor of the same name silently became a *bound method* on every instance, and
``accounts``/``health``/``run`` printed a ``TypeError`` traceback instead of a document.
See the note on ``is_partial`` below.)

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
DOCUMENTATION = "documentation"
FIXTURE_LABEL_PREFIX = "FIXTURE:"
DOCUMENTATION_LABEL_PREFIX = "DOCUMENTATION:"

REAL_DISCLAIMER = None
FIXTURE_DISCLAIMER = (
    "FIXTURE — answered from a recorded result shipped in this repository in "
    "--fixture-mode. No Mail.app was contacted and no mailbox was read; these are not "
    "observations of any real mailbox or account."
)
#: A recorded read is labelled for the source it would have read. Every source the probe
#: knows has its own entry, so no source can inherit another's wording by falling back:
#: a Mail fixture must not claim no *Beeper Desktop* was contacted, and — the defect this
#: map grew a ``hermes`` entry for — a Hermes fixture must not claim no *Mail.app* was
#: contacted. ``FIXTURE_DISCLAIMER`` remains the default only for a source this map does
#: not know, and ``test_mini_hermes`` asserts the map covers every source ``CAPABILITIES``
#: declares, so the next source added cannot quietly land on the Mail wording.
FIXTURE_DISCLAIMER_BY_SOURCE = {
    "mail": FIXTURE_DISCLAIMER,
    "beeper": (
        "FIXTURE — answered from a recorded result shipped in this repository in "
        "--fixture-mode. No Beeper Desktop was contacted and no chat, message or contact "
        "was read; these are not observations of any real Beeper install."
    ),
    "contacts": (
        "FIXTURE — answered from a recorded result shipped in this repository in "
        "--fixture-mode. No Contacts database was read and no contact, identifier, key or "
        "change-history token was observed; these are not observations of any Mac."
    ),
    "hermes": (
        "FIXTURE — answered from a recorded result shipped in this repository in "
        "--fixture-mode. No Hermes gateway was contacted and no capability, run, session or "
        "event stream was observed; no token value is recorded here, and these are not "
        "observations of any Hermes install."
    ),
}

#: A **labelled loopback stand-in** answered, not the source. This is the third kind of
#: responder the worker can be pointed at, and it is the one that used to be invisible in the
#: label column: ``SWITCHBOARD_HERMES_STANDIN`` / ``--stand-in-server`` drives the *real*
#: transport class against a local HTTP responder, so the row was ``origin: real`` with a
#: NULL label and the only tell sat inside ``evidence``. A reader filtering on the label or
#: on ``origin`` alone saw a real-origin row for a run that contacted no gateway at all.
#: Every row answered by a stand-in now carries this label and the disclaimer below.
STAND_IN_LABEL_PREFIX = "STAND-IN:"

REAL_DISCLAIMER = None
STAND_IN_DISCLAIMER = (
    "STAND-IN — a labelled loopback stand-in HTTP responder answered this row, not the "
    "source. It is not Hermes, not Mail.app, not Beeper Desktop and not Contacts: it is a "
    "local test server in this repository, so nothing here is an observation of Randy's "
    "machine or of any install, and this row may never be read as a measurement."
)
DOCUMENTATION_DISCLAIMER = (
    "DOCUMENTATION — recorded from a public reference page in the Gate 2 probe pack, not "
    "from this machine or Randy's. No source was contacted by this row and no capability "
    "is claimed: it carries the documented surface, the documented silences and the "
    "procedure that would measure it."
)


def fixture_label(adapter: str, detail: Optional[str] = None) -> str:
    label = f"{FIXTURE_LABEL_PREFIX}{adapter}"
    if detail:
        label = f"{label}({detail})"
    return label


def is_fixture_label(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(FIXTURE_LABEL_PREFIX)


def documentation_label(source: str, citations=()) -> str:
    """``DOCUMENTATION:beeper(O05,O11)`` — what a documentation-backed row is labelled."""
    label = f"{DOCUMENTATION_LABEL_PREFIX}{source}"
    if citations:
        label = f"{label}({','.join(citations)})"
    return label


def is_documentation_label(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(DOCUMENTATION_LABEL_PREFIX)


def stand_in_label(source: str, detail: Optional[str] = None) -> str:
    """``STAND-IN:hermes`` — what a row answered by a labelled loopback stand-in is called.

    The label column is where the labelling rule lives, so a stand-in row says it *there*
    and not only inside ``evidence``. ``origin`` stays whatever the vocabulary honestly
    supports (a stand-in is driven through the real transport class, so ``origin`` is
    ``real``: "the real adapter produced this row", which is not a claim about the source).
    """
    label = f"{STAND_IN_LABEL_PREFIX}{source}"
    if detail:
        label = f"{label}({detail})"
    return label


def is_stand_in_label(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(STAND_IN_LABEL_PREFIX)


def fixture_disclaimer(source: Optional[str] = None) -> str:
    """The fixture disclaimer for a source: it must name the source it did *not* contact."""
    return FIXTURE_DISCLAIMER_BY_SOURCE.get(source or "", FIXTURE_DISCLAIMER)


def stand_in_disclaimer(source: Optional[str] = None) -> str:
    """The disclaimer for a stand-in row. It names what answered, and what that is not."""
    return STAND_IN_DISCLAIMER


def disclaimer_for(origin: str, source: Optional[str] = None,
                   stand_in: bool = False) -> Optional[str]:
    """The one disclaimer for each non-real origin. Never a real-source claim.

    ``stand_in`` is checked first because it outranks the origin: a stand-in row is
    ``origin: real`` (the real adapter produced it) while no source answered it, and the
    disclaimer is the one place that has to say so.
    """
    if stand_in:
        return stand_in_disclaimer(source)
    if origin == FIXTURE:
        return fixture_disclaimer(source)
    if origin == DOCUMENTATION:
        return DOCUMENTATION_DISCLAIMER
    return REAL_DISCLAIMER


def probe_row_supported_claim_allowed(row: dict) -> bool:
    """May this probe row carry ``supported: true``?

    One rule, and it is the probe pack's scope statement: **a documentation read can
    never set ``supported: true``**. A row whose evidence is a page is a question, not a
    measurement, so it is ``unmeasured`` until something observes it.

    And one addition from the stand-in defect (2026-10-09): a row answered by a **labelled
    loopback stand-in** may not be ``supported`` either. The stand-in is not the source, so
    its answer is not an observation of anything, and support means an observation held.

    Grace holds the identical rule at write time
    (``grace/contracts.py::probe_row_supported_claim_allowed``); ``tests/
    test_shared_vocabulary.py`` asserts the two agree.
    """
    if not row.get("supported"):
        return True
    if row.get("stand_in"):
        return False
    if row.get("origin") == DOCUMENTATION:
        return False
    return True


#: What a probe row is a measurement *of* (defect fix, 2026-10-09). Every row is about a
#: source (Mail, Beeper, Contacts, Hermes) except the worker's own ``manifest`` row, which
#: measures **the worker**: it says whether the manifest the worker emits declares every
#: capability and marks each unprobed one ``supported: false``. That row contacts no
#: source, so it may neither claim a source value nor be read as one -- but it is a real
#: measurement of something, and this field is how it says which.
MEASUREMENT_SOURCE = "source"
MEASUREMENT_WORKER = "worker"
MEASUREMENT_TARGETS = (MEASUREMENT_SOURCE, MEASUREMENT_WORKER)

#: The frozen set of capabilities that are a measurement of the worker itself. Frozen on
#: purpose: it is the whole width of the exception below, so a row for any other
#: capability cannot borrow it. Grace holds the same set in ``grace/contracts.py`` and
#: ``tests/test_probe_manifest_self_measurement.py`` asserts the two agree.
SELF_MEASURED_CAPABILITIES = frozenset({"manifest"})


def probe_row_supported_without_a_source_allowed(row: dict) -> bool:
    """May a row that contacted no source still carry ``supported: true``?

    Only for a row that says so in its own contract field -- ``measurement_target:
    'worker'`` -- and only for one of the frozen :data:`SELF_MEASURED_CAPABILITIES`.
    Everything else is the ordinary rule: support means a source answered this row
    (``real_source_connected``), and a row that never touched a source may not claim it.

    This is deliberately *not* a bypass: the capability name alone is not enough (a row
    with the marker on a source capability is still refused), a documentation row can
    never use it (a page is not a measurement of anything, including the worker), and a
    self-measurement may carry no source values.

    Grace holds the identical rule
    (``grace/contracts.py::probe_row_supported_without_a_source_allowed``); ``tests/
    test_probe_manifest_self_measurement.py`` asserts the two agree case by case.
    """
    if row.get("origin") == DOCUMENTATION:
        return False
    if row.get("measurement_target") != MEASUREMENT_WORKER:
        return False
    return (row.get("capability") or row.get("name")) in SELF_MEASURED_CAPABILITIES


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

# ---------------------------------------------------------------- probe rows ----
#: A probe row's typed state vocabulary: the adapter outcome codes above, plus
#: ``unmeasured`` for a capability whose own assertion was never evaluated (a
#: documentation read, a single-page mailbox, a message with no attachments). ``unmeasured``
#: is deliberately neither ``supported`` nor a claim about the source: the question is
#: open, which is different from ``unsupported``.
PROBE_UNMEASURED = "unmeasured"
PROBE_ROW_STATES = ADAPTER_OUTCOMES + (PROBE_UNMEASURED,)

#: ``observed_version`` reports this literal when the row did not observe a version. A
#: version is never borrowed from another row's read (see ``probe._row``).
VERSION_NOT_OBSERVED = "not_observed"

#: Row origins. ``real`` = produced on this host by the real adapter; ``fixture`` =
#: answered from a recorded result; ``documentation`` = read out of the Gate 2 probe pack,
#: which may never carry ``supported: true``.
PROBE_ROW_ORIGINS = (REAL, FIXTURE, DOCUMENTATION)


# ------------------------------------------------------------ permission states --

PERMISSION_GRANTED = "granted"
PERMISSION_STATE_DENIED = "denied"
PERMISSION_NOT_DETERMINED = "not_determined"
PERMISSION_NOT_APPLICABLE = "not_applicable"
# NOTE (2026-10-09, Gate 2 Contacts slice): a fifth value, and the reason it is not "denied".
# macOS Contacts reports its own authorization status as ``CNAuthorizationStatusRestricted``
# -- Apple: "The application is not authorized to access contact data. The user cannot
# change this application's status, possibly due to active restrictions such as parental
# controls being in place." That is a different condition from ``denied`` (which the owner
# can reverse in System Settings by granting access) and from ``not_determined`` (where
# nothing has been asked yet): a restricted grant cannot be granted from that pane at all,
# so telling Randy to "grant the permission in System Settings" would be wrong advice.
# It was added here and in ``grace/contracts.py`` (one shared vocabulary, both sides) rather
# than mapped onto an existing word, and ``tests/test_shared_vocabulary.py`` asserts the two
# lists still agree.
PERMISSION_RESTRICTED = "restricted"

PERMISSION_STATES = (PERMISSION_GRANTED, PERMISSION_STATE_DENIED,
                     PERMISSION_NOT_DETERMINED, PERMISSION_NOT_APPLICABLE,
                     PERMISSION_RESTRICTED)
# NOTE (2026-10-08): this is the **single closed permission vocabulary** for the whole
# product. Grace carries the same four values (``grace/contracts.py::PERMISSION_STATES``)
# and ``tests/test_shared_vocabulary.py`` fails if the two ever drift. The probe used to
# emit ``not_applicable`` while Grace's schema comment and web layer expected
# ``not_required``/``unknown``, so a granted-by-absence source was read as a problem;
# ``not_applicable`` is the one spelling that survives.
# The outcome code for a denied operation is a different thing: ``PERMISSION_DENIED``
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
    # NOTE (defect fix, 2026-10-08): this field is ``is_partial`` and NOT ``partial``.
    # The class also exposes a ``partial()`` constructor, and a dataclass field whose name
    # is shadowed by a later class attribute takes that attribute as its *default* -- so
    # every instance that did not pass ``partial=`` explicitly carried a bound method in
    # this field, and ``to_dict()``/``emit()`` blew up with
    # "TypeError: Object of type method is not JSON serializable". That is what made
    # `accounts`, `health` and `run` crash on every platform. Keep the two names distinct.
    # The JSON key stays ``partial`` for consumers (Grace reads it).
    is_partial: bool = False
    retry_after: Optional[str] = None
    next_action: Optional[str] = None
    duration_ms: Optional[int] = None
    # Provenance, kept deliberately separate -- see the module docstring.
    adapter_is_real: bool = False      # the real adapter answered, not a fixture twin
    source_contacted: bool = False     # this run actually read this value from the source

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
    def is_real(self) -> bool:
        """The real adapter answered (as opposed to a fixture twin).

        This is *not* "a real source was contacted": see ``real_source_connected``.
        """
        return self.origin == REAL

    @property
    def real_source_connected(self) -> bool:
        """True only when this document carries usable data read from the real source.

        All three parts matter: the real adapter answered, this run actually reached the
        source, and the document is a usable read (success or partial). A refusal over a
        connection that exists (permission denied, offline, a timeout) is therefore still
        false -- it carries no source value.
        """
        return bool(self.adapter_is_real and self.source_contacted and self.usable)

    @property
    def retryable_as_read(self) -> bool:
        return self.code in (OFFLINE, RATE_LIMITED, RETRYABLE_ERROR, PARTIAL)

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
            "origin": self.origin,
            "adapter_is_real": bool(self.adapter_is_real),
            "source_contacted": bool(self.source_contacted),
            "real_source_connected": self.real_source_connected,
            "fixture_mode": not self.is_real,
            "retry_after": self.retry_after,
            "next_action": self.next_action,
            "duration_ms": self.duration_ms,
        }
        if not self.is_real:
            payload["label"] = self.label or fixture_label(self.adapter or "mini")
            payload["disclaimer"] = fixture_disclaimer(self.adapter)
        if self.origin not in (REAL, FIXTURE):
            raise AssertionError(f"Outcome.to_dict: origin must be {REAL!r} or {FIXTURE!r}, "
                                 f"got {self.origin!r}")
        if not self.is_real and not is_fixture_label(payload.get("label")):
            raise AssertionError("Outcome.to_dict: a non-real value is not labelled")
        return payload


# ------------------------------------------------------------------ reporting ---


class SerializationError(ValueError):
    """A document that is not well-formed JSON, reported as a typed failure.

    Carries the offending key paths so the caller can say *where* the document went
    wrong without printing a stack trace or inventing a value.
    """

    def __init__(self, paths):
        self.paths = list(paths)
        super().__init__(
            "document is not JSON-serialisable at: " + ", ".join(self.paths))


def json_problem_paths(value, path: str = "$", out=None) -> list:
    """Every path in ``value`` that ``json.dumps`` could not serialise.

    Types that JSON cannot express (a bound method, a set, a datetime) are recorded
    instead of raised on, so the failure message can name all of them at once.
    """
    if out is None:
        out = []
    if value is None or isinstance(value, (str, bool, int)):
        return out
    if isinstance(value, float):
        return out
    if isinstance(value, list) or isinstance(value, tuple):
        for i, item in enumerate(value):
            json_problem_paths(item, f"{path}[{i}]", out)
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                out.append(f"{path}.<key {key!r}>")
            json_problem_paths(item, f"{path}.{key}", out)
        return out
    out.append(f"{path} ({type(value).__name__})")
    return out


def emit(document: dict, *, pretty: bool = False) -> str:
    """Canonical JSON. Keys sorted so two runs are diffable.

    Validated first: a document that is not JSON-safe raises
    :class:`SerializationError` naming the offending paths, so the worker reports the
    defect as a typed failure instead of dying with a ``TypeError`` traceback.
    """
    problems = json_problem_paths(document)
    if problems:
        raise SerializationError(problems)
    if pretty:
        return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False)
    return json.dumps(document, sort_keys=True, ensure_ascii=False)


def python_supported() -> bool:
    """The worker targets the macOS system Python, which is 3.9 or newer."""
    return sys.version_info >= (3, 9)
