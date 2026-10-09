"""The capability probe: one JSON row per capability, measured, never guessed.

The row contract (PRD §11 probe matrix, PRD §6 capability manifest):

    capability        stable name of the capability being probed
    supported         true only when *this row's own* ``probe_assertion`` was evaluated
                      and held. Never true for a row whose evidence is a documentation
                      read (see ``documented_capabilities.py``)
    permission_state  one closed vocabulary, shared with Grace:
                      ``granted`` | ``denied`` | ``not_determined`` | ``not_applicable``
                      (``switchboard_mini.outcomes.PERMISSION_STATES``)
    observed_version  the version this row observed from the source it probed, or the
                      literal ``not_observed`` with the reason in
                      ``observed_version_reason``. A row that never touched Mail may not
                      borrow Mail's bundle version: only the ``health`` row reads it, and
                      only the ``manifest`` row reports the worker's own version
    probe_method      how the worker tried to establish this
    probe_assertion   what "supported: true" is claiming to have been observed
    limitation        what is known not to hold, or why the row is unsupported
    evidence          what was actually observed, with mailbox content fingerprinted

plus the labelling fields every worker document carries: ``origin`` (``real`` only when
this run produced the row on this host, ``fixture`` for a recorded scenario,
``documentation`` for a row read out of the Gate 2 probe pack), ``state`` (the typed
outcome, or ``unmeasured``), ``source`` (which source the row is about), ``citations``
(the pack refs ``O01``-``O22`` a documentation row quotes), ``label`` and ``disclaimer``.

Four rules the code now enforces, each because the earlier code could state something it
had not measured:

* A capability the probe cannot confirm comes back ``supported: false`` with a typed
  reason. There is no code path that sets ``supported: true`` without an observation.
* ``supported: true`` requires the row's **own** assertion to have been evaluated. A
  single-page mailbox is not evidence of resumable iteration, a mailbox whose true count
  was never read is not evidence of bounded listing, and a message with no attachments
  is not evidence of attachment enumeration -- those rows are ``unmeasured`` instead.
* ``observed_version`` is reported only by a row that observed that version. Every other
  row carries ``not_observed`` and says why.
* A documentation-origin row can never be ``supported`` (probe-pack scope statement:
  "A documentation read can never set ``supported: true``"). ``_row`` refuses to build
  one, and Grace refuses to store one.

Exit status: the probe process exits non-zero only when the harness itself failed (an
unexpected exception while producing a row). Unsupported capabilities are a successful
probe run -- that is the whole point of probing.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import outcomes as O
from .version import PROBE_CONTRACT_VERSION, WORKER_VERSION

# The row contract. The first eight fields are the required contract; the rest are the
# labelling, provenance and citation fields every worker document carries. Grace's
# ``capability`` table stores the same contract (see ``grace/schema.sql``).
ROW_FIELDS = ("capability", "supported", "permission_state", "observed_version",
              "probe_method", "probe_assertion", "limitation", "evidence")
ROW_LABELLING_FIELDS = ("state", "origin", "label", "disclaimer", "probed_at",
                        "title", "values_from_source", "adapter_is_real",
                        "real_source_connected", "probe_contract_version",
                        "source", "citations", "observed_version_reason", "supersedes")


@dataclass(frozen=True)
class Capability:
    name: str
    title: str
    probe_method: str
    probe_assertion: str
    implemented: bool = True
    limitation: Optional[str] = None
    #: Which source this capability is about: mail | beeper | contacts | hermes.
    source: str = "mail"
    #: Probe-pack refs (O01-O22) a documentation-backed row quotes. Empty for rows this
    #: worker measures itself.
    citations: tuple = ()
    #: Recorded page facts / silences / the Mac procedure, for documented rows.
    documented: tuple = ()
    absences: tuple = ()
    procedure: tuple = ()
    #: True when this row comes from a documentation read and no adapter exists for it.
    documented_only: bool = False


def documented_capabilities() -> tuple:
    """Build one :class:`Capability` per capability recorded in the Gate 2 pack."""
    from .documented_capabilities import DOCUMENTED_CAPABILITIES
    return tuple(Capability(name=entry["name"], title=entry["title"],
                            probe_method=entry["probe_method"],
                            probe_assertion=entry["probe_assertion"],
                            limitation=entry["limitation"], source=entry["source"],
                            citations=entry["citations"], documented=entry["documented"],
                            absences=entry["absences"], procedure=entry["procedure"],
                            documented_only=True)
                 for entry in DOCUMENTED_CAPABILITIES)


CAPABILITIES: tuple = (
    Capability(
        name="manifest",
        title="Versioned capability manifest",
        probe_method="the worker builds its own manifest and counts the capabilities it "
                     "declares",
        probe_assertion="the worker emits a manifest that declares every capability and "
                        "marks each unprobed capability supported=false",
    ),
    Capability(
        name="health",
        title="Mail.app presence, build version and reachability",
        probe_method="read Mail.app's bundle Info.plist (does not launch Mail), then ask "
                     "the Apple event system whether Mail is running",
        probe_assertion="Mail.app's installed build version is observed and the worker can "
                        "tell whether the application is running",
    ),
    Capability(
        name="account_enumeration",
        title="List Mail accounts",
        probe_method="AppleScript: repeat over `accounts` of Mail, reading name, id, "
                     "account type, enabled and email-address count",
        probe_assertion="every configured account is returned with at least one "
                        "non-empty identifying field",
    ),
    Capability(
        name="mailbox_enumeration",
        title="List mailboxes of an account",
        probe_method="AppleScript: repeat over `mailboxes of account` for one account",
        probe_assertion="the mailbox list resolves for the named account and returns at "
                        "least one mailbox with a name",
    ),
    Capability(
        name="bounded_message_listing",
        title="Bounded listing of messages in a mailbox",
        probe_method="AppleScript: `count of messages` plus `id of message i` for a "
                     "bounded index range, then one metadata window for the page",
        probe_assertion="a bounded page of messages is returned with the mailbox's true "
                        "message count, so a capped scan is distinguishable from a "
                        "complete one",
    ),
    Capability(
        name="historical_iteration",
        title="Resumable iteration over mailbox history",
        probe_method="walk a second page using the cursor the first page returned, and "
                     "check the two pages do not overlap",
        probe_assertion="the adapter produces a cursor that resumes the next page and the "
                        "pages are disjoint",
    ),
    Capability(
        name="message_identifiers",
        title="Stable message identifiers",
        probe_method="read `id` and `message id` for the sampled page and re-read the "
                     "same ids on a second scan of the mailbox",
        probe_assertion="each sampled message carries an internal id and an RFC Message-ID "
                        "that are stable across two reads in the same run",
    ),
    Capability(
        name="message_dates",
        title="Message dates",
        probe_method="AppleScript: `date received` rendered as text plus its year, month, "
                     "day, hour, minute and second components",
        probe_assertion="a date is read for every sampled message, with its local-time "
                        "basis recorded and no invented UTC offset",
    ),
    Capability(
        name="sender_recipient_extraction",
        title="Sender and recipient extraction",
        probe_method="AppleScript: `sender` plus counts of `to`, `cc` and `bcc` "
                     "recipients for each sampled message",
        probe_assertion="sender and recipient counts are read for every sampled message, "
                        "with addresses fingerprinted rather than copied",
    ),
    Capability(
        name="body_retrieval",
        title="Message body retrieval",
        probe_method="AppleScript: `content` of one message, wrapped so an unreadable "
                     "body returns a sentinel instead of an empty string",
        probe_assertion="a body is retrieved and reported as fetched or genuinely empty; "
                        "an unreadable body is reported as a typed partial, not as empty",
    ),
    Capability(
        name="raw_source_retrieval",
        title="Raw RFC822 source retrieval",
        probe_method="AppleScript: `source` of one message, wrapped in the same sentinel",
        probe_assertion="the raw source is available on this build, or the row says the "
                        "terminology is not confirmed on it",
    ),
    Capability(
        name="attachment_enumeration",
        title="Attachment enumeration per message",
        probe_method="AppleScript: repeat over `mail attachments` of one message reading "
                     "name, MIME type, file size and downloaded state",
        probe_assertion="attachment metadata resolves per item and the local availability "
                        "of each attachment is reported separately",
    ),
    Capability(
        name="attachment_materialization",
        title="Materialising attachment bytes",
        probe_method="not attempted: this slice has no save/download path",
        probe_assertion="not claimed in this slice",
        implemented=False,
        limitation="attachment bytes cannot be materialised yet: Mail's download/save "
                   "behaviour and per-attachment availability have not been measured "
                   "(PRD §11)",
    ),
    Capability(
        name="draft_preparation",
        title="Preparing a draft in Mail",
        probe_method="the probe calls the adapter's prepare_draft and records the typed "
                     "refusal",
        probe_assertion="not claimed in this slice: the worker refuses to prepare drafts",
        implemented=False,
        limitation="no draft path exists in this worker: nothing in slice 1 writes to a "
                   "Mail composer (PRD §10 draft-only launch default)",
    ),
    Capability(
        name="authorized_send",
        title="Sending through Mail with reply-as-account",
        probe_method="the probe calls the adapter's dispatch and records the typed refusal",
        probe_assertion="not claimed in this slice: the worker refuses every send",
        implemented=False,
        limitation="no outbound path exists in this worker; reply-as-account and iCloud "
                   "sending remain unmeasured (PRD §11, Gate 3)",
    ),
    Capability(
        name="send_reconciliation",
        title="Reconciling a sent message",
        probe_method="the probe calls the adapter's reconcile and records the typed refusal",
        probe_assertion="not claimed in this slice: there is nothing the worker can have "
                        "sent",
        implemented=False,
        limitation="reconciliation is not applicable until a send path exists; a receipt "
                   "must never be marked verified against a real source before then",
    ),
    # ---------------------------------------------------------------------------
    # The three sources this worker cannot reach yet. Each row is a documentation read
    # from the Gate 2 probe pack (see ``documented_capabilities.py``), emitted so that
    # Beeper, Contacts and Hermes have real capability rows with their citations, their
    # documented gaps and the Mac procedure that would measure them. Every one of them is
    # ``supported: false`` and ``state: 'unmeasured'``, and none may ever be supported
    # from a documentation read.
    # ---------------------------------------------------------------------------
) + documented_capabilities()

CAPABILITY_NAMES = tuple(c.name for c in CAPABILITIES)
#: The capabilities whose evidence is a documentation read only.
DOCUMENTED_CAPABILITY_NAMES = tuple(c.name for c in CAPABILITIES if c.documented_only)


# ---------------------------------------------------------------- probe context --

class ProbeContext:
    """Runs each underlying read at most once per probe run and remembers the outcome."""

    def __init__(self, adapter, *, sample: int = 5, account: Optional[str] = None,
                 mailbox: Optional[str] = None, max_scan: int = 2000,
                 beeper_account_id: Optional[str] = None,
                 beeper_ui_oldest_visible: Optional[str] = None):
        # One run may carry several adapters: the Mail adapter measures the Mail rows and
        # the Beeper adapter measures the Beeper rows, so one probe run produces exactly
        # one row per capability key and a documentation row is only ever emitted for a
        # capability no adapter in the run can measure (see ``_handler_for``).
        adapters = list(adapter) if isinstance(adapter, (list, tuple)) else [adapter]
        if not adapters:
            raise ValueError("a probe run needs at least one adapter")
        self.adapters = {getattr(a, "name", type(a).__name__): a for a in adapters}
        self.adapter = adapters[0]
        self.sample = max(1, int(sample))
        self.max_scan = max_scan
        self.origin = self.adapter.origin
        # The provenance of the primary adapter, read once: a fixture twin can never make
        # a row claim a real source, whatever the fixture file says.
        self.adapter_is_real = bool(getattr(self.adapter, "adapter_is_real", False))
        self.label = getattr(self.adapter, "_label", lambda: None)()
        self._account_arg = account
        self._mailbox_arg = mailbox
        self._beeper_account_id = beeper_account_id
        self._beeper_ui_oldest = beeper_ui_oldest_visible
        self._memo: dict = {}
        self.harness_errors: list = []

    # -- the other adapters in this run ------------------------------------
    def for_source(self, source: str):
        """The adapter that measures ``source`` in this run, or None if it is absent."""
        return self.adapters.get(source)

    def beeper_account_id(self) -> Optional[str]:
        return self._beeper_account_id

    def beeper_ui_oldest_visible(self) -> Optional[str]:
        return self._beeper_ui_oldest

    # -- memoized reads ----------------------------------------------------
    def _once(self, key: str, call: Callable[[], Any]) -> Any:
        if key not in self._memo:
            self._memo[key] = call()
        return self._memo[key]

    def identity(self) -> O.Outcome:
        return self._once("identity", lambda: self.adapter.identity())

    def accounts(self) -> O.Outcome:
        return self._once("accounts", lambda: self.adapter.accounts())

    def account(self) -> Optional[str]:
        if self._account_arg:
            return self._account_arg
        got = self.accounts()
        if not got.usable:
            return None
        names = [a["name"] for a in got.data.get("accounts", []) if a.get("name")]
        return names[0] if names else None

    def mailboxes(self) -> O.Outcome:
        account = self.account()
        if not account:
            # No account to ask about. Report *why* accounts could not be read rather
            # than inventing a reason, so a denied or offline source keeps its own state.
            accounts = self.accounts()
            if not accounts.usable:
                return accounts
            return O.Outcome.permanent("Mail reported no account to enumerate mailboxes for",
                                       reason="no_account")
        return self._once(f"mailboxes:{account}",
                          lambda: self.adapter.mailboxes(account))

    def blocking(self) -> Optional[O.Outcome]:
        """The first typed failure that stops every read, if there is one."""
        accounts = self.accounts()
        if not accounts.usable:
            return accounts
        mailed = self.mailboxes()
        if not mailed.usable:
            return mailed
        return None

    def mailbox(self) -> Optional[str]:
        if self._mailbox_arg:
            return self._mailbox_arg
        got = self.mailboxes()
        if not got.usable:
            return None
        names = [m["name"] for m in got.data.get("mailboxes", []) if m.get("name")]
        if "INBOX" in names:
            return "INBOX"
        return names[0] if names else None

    def scope(self) -> Optional[str]:
        from .mail_adapter import scope_ref
        box = self.mailbox()
        return scope_ref(box) if box else None

    def page(self, *, cursor: Optional[str] = None) -> O.Outcome:
        account, scope = self.account(), self.scope()
        if not account or not scope:
            blocked = self.blocking()
            if blocked is not None:
                return blocked
            return O.Outcome.permanent("no account or mailbox to read", reason="no_scope")
        key = f"page:{account}:{scope}:{cursor}"
        return self._once(key, lambda: self.adapter.enumerate(
            account, scope, limit=self.sample, cursor=cursor))

    def first_retrieval(self) -> O.Outcome:
        def fetch() -> O.Outcome:
            page = self.page()
            if not page.usable:
                # Propagate the real typed reason (denied / offline / timeout) instead of
                # reporting an invented "no sample".
                return page
            items = (page.data or {}).get("items") or []
            if not items:
                return O.Outcome.permanent("no message available to retrieve",
                                           reason="no_sample")
            ref = items[0].get("namespaced_id")
            if not ref:
                return O.Outcome.permanent("sampled message has no namespaced reference",
                                           reason="no_reference")
            return self.adapter.retrieve(account, ref)
        account = self.account()
        return self._once("retrieve", fetch)

    def mail_version(self) -> Optional[str]:
        try:
            ident = self.identity()
        except Exception:
            # A row must still be produced when the identity read itself is what broke;
            # that is a harness failure, and it is reported as one, not raised.
            return None
        if not ident.usable:
            return None
        bundle = (ident.data or {}).get("mail_bundle") or {}
        return bundle.get("info_plist_version") or None


# ------------------------------------------------------------------ row helper --

def _row(context: ProbeContext, capability: Capability, *, supported: bool, state: str,
         permission_state: str, limitation: Optional[str], evidence: dict,
         observed_version: Optional[str] = None,
         observed_version_reason: Optional[str] = None,
         values_from_source: Optional[bool] = None,
         origin: Optional[str] = None, adapter_is_real: Optional[bool] = None,
         citations: Optional[tuple] = None, label: Optional[str] = None,
         supersedes: Optional[dict] = None) -> dict:
    """Build one row, enforcing the rules that keep a row from over-claiming.

    ``observed_version`` is **never** defaulted from another row's read: a caller that
    observed a version passes it (with what it came from), and every other row reports
    the literal ``not_observed`` and the reason. ``supported`` is refused outright for a
    documentation-origin row, because a page is not a measurement.
    """
    row_origin = origin or context.origin
    row_cites = tuple(citations if citations is not None else capability.citations)
    if observed_version is None:
        observed_version = O.VERSION_NOT_OBSERVED
        observed_version_reason = observed_version_reason or (
            "this row did not observe a version: it did not read the source's version "
            "information"
            if state in (O.SUCCESS, O.PARTIAL) else
            "no version was observed, because this row never reached the source "
            f"(state {state!r})")
    if origin == O.DOCUMENTATION or (row_origin == O.DOCUMENTATION):
        observed_version = O.VERSION_NOT_OBSERVED
        observed_version_reason = (
            "documentation read only: no source was contacted by this row, so no version "
            "could be observed on any host")
    if not O.probe_row_supported_claim_allowed({
            "origin": row_origin, "supported": bool(supported),
            "values_from_source": bool(values_from_source),
            "real_source_connected": bool(adapter_is_real and values_from_source)}):
        raise ValueError(
            f"probe row {capability.name!r}: a documentation-origin row may never be "
            "supported=true (probe-pack scope statement)")
    row = {
        "capability": capability.name,
        "title": capability.title,
        "source": capability.source,
        "supported": bool(supported),
        "state": state,
        "permission_state": permission_state,
        "observed_version": observed_version,
        "observed_version_reason": observed_version_reason,
        "probe_method": capability.probe_method,
        "probe_assertion": capability.probe_assertion,
        "limitation": limitation or capability.limitation,
        "evidence": evidence,
        "citations": list(row_cites),
        "origin": row_origin,
        "probed_at": O.now(),
        "probe_contract_version": PROBE_CONTRACT_VERSION,
        # ``adapter_is_real``: the real adapter (not a fixture twin) produced this row.
        # ``values_from_source``: the capability answered with data (success or partial)
        # rather than with a typed refusal or a state about the source.
        # ``real_source_connected``: both -- the meaning this field has everywhere else in
        # the product ("a real source was contacted and this value came from it"). On this
        # Linux computer the real adapter answers every row with a typed refusal, so every
        # row is false; only a real probe on a Mac can make it true.
        "adapter_is_real": (bool(context.adapter_is_real)
                            if adapter_is_real is None else bool(adapter_is_real)),
        "values_from_source": (state in (O.SUCCESS, O.PARTIAL)
                               if values_from_source is None
                               else bool(values_from_source)),
        "real_source_connected": bool(
            (context.adapter_is_real if adapter_is_real is None else adapter_is_real)
            and (state in (O.SUCCESS, O.PARTIAL)
                 if values_from_source is None else bool(values_from_source))),
    }
    row["supersedes"] = dict(supersedes) if supersedes else None
    if row["supersedes"] is not None and row["citations"]:
        raise ValueError(
            f"probe row {capability.name!r}: a row that supersedes a documentation read is "
            "a measurement, and a measurement quotes no page — the pack refs belong in "
            "`supersedes.citations`")
    if row["origin"] != O.REAL:
        if row["origin"] == O.DOCUMENTATION:
            row["label"] = O.documentation_label(capability.source, row_cites)
        else:
            row["label"] = label or context.label or O.fixture_label(capability.source)
        row["disclaimer"] = O.disclaimer_for(row["origin"], source=capability.source)
    else:
        row["label"] = None
        row["disclaimer"] = None
    return row


def _permission_for(outcome: O.Outcome) -> str:
    if outcome.code == O.PERMISSION_DENIED:
        return O.PERMISSION_STATE_DENIED
    if outcome.code in (O.UNSUPPORTED, O.PERMANENT_ERROR):
        return O.PERMISSION_NOT_DETERMINED
    if outcome.usable:
        return O.PERMISSION_GRANTED
    return O.PERMISSION_NOT_DETERMINED


def _blocked_row(context: ProbeContext, capability: Capability, outcome: O.Outcome,
                 extra: Optional[dict] = None, *, origin: Optional[str] = None,
                 adapter_is_real: Optional[bool] = None,
                 label: Optional[str] = None,
                 supersedes: Optional[dict] = None) -> dict:
    evidence = dict(outcome.data or {})          # e.g. the Apple event code and message
    evidence.update({
        "outcome_code": outcome.code,
        "reason": outcome.reason,
        "detail": outcome.detail,
        "duration_ms": outcome.duration_ms,
        "next_action": outcome.next_action,
    })
    if extra:
        evidence.update(extra)
    return _row(context, capability, supported=False, state=outcome.code,
                permission_state=_permission_for(outcome),
                limitation=(f"{capability.limitation} — " if capability.limitation else "")
                           + (outcome.detail or outcome.code),
                evidence=evidence, origin=origin, adapter_is_real=adapter_is_real,
                label=label, supersedes=supersedes, citations=())


# ------------------------------------------------------------- per capability --

def _probe_manifest(context: ProbeContext, capability: Capability) -> dict:
    manifest = context.adapter.manifest()
    caps = manifest["capabilities"]
    claimed = sorted(name for name, entry in caps.items() if entry["supported"])
    return _row(context, capability, supported=True, state=O.SUCCESS,
                permission_state=O.PERMISSION_NOT_APPLICABLE,
                limitation=None,
                evidence={
                    "manifest_version": manifest["manifest_version"],
                    "adapter": manifest["adapter"],
                    "adapter_version": manifest["adapter_version"],
                    "capabilities_declared": len(caps),
                    "capabilities_claimed_supported_without_probe": len(claimed),
                    "claimed_without_probe": claimed,
                    "probe_contract_version": PROBE_CONTRACT_VERSION,
                    "note": "the worker refuses to mark a Mail capability supported before "
                            "it has been measured on this Mac",
                },
                observed_version=WORKER_VERSION,
                observed_version_reason=("the worker's own version: this row describes the "
                                         "worker's manifest, not Mail"),
                values_from_source=False)


def _probe_health(context: ProbeContext, capability: Capability) -> dict:
    ident = context.identity()
    if not ident.usable:
        return _blocked_row(context, capability, ident)
    bundle = (ident.data or {}).get("mail_bundle") or {}
    version = bundle.get("info_plist_version")
    running = (ident.data or {}).get("mail_running")
    limitation = None
    if not version:
        limitation = ("Mail.app's bundle version was not read; the installed build's "
                      "version is unknown")
    if running in ("false", False):
        limitation = ("Mail.app is installed but not running; reads report offline until "
                      "it is launched. 'Supported' says the capability exists; 'state' "
                      "says what this probe observed.")
    return _row(context, capability, supported=bool(version),
                state=(O.OFFLINE if running in ("false", False) else ident.code),
                permission_state=O.PERMISSION_GRANTED,
                limitation=limitation,
                evidence={"platform": (ident.data or {}).get("platform"),
                          "mail_bundle": bundle,
                          "mail_bundle_found": bool(bundle.get("path")),
                          "mail_running": running,
                          "duration_ms": ident.duration_ms,
                          "version_observed_from": "Mail.app's installed bundle Info.plist",
                          "probe": (ident.data or {}).get("probe")},
                observed_version=version,
                observed_version_reason=(
                    "observed from Mail.app's installed bundle Info.plist by this row"
                    if version else
                    "Mail.app's bundle version could not be read on this host, so no "
                    "version was observed"))


def _probe_account_enumeration(context: ProbeContext, capability: Capability) -> dict:
    got = context.accounts()
    if not got.usable:
        return _blocked_row(context, capability, got)
    accounts = got.data.get("accounts", [])
    named = [a for a in accounts if a.get("name")]
    return _row(context, capability, supported=bool(named), state=got.code,
                permission_state=O.PERMISSION_GRANTED,
                limitation=None if named else
                "Mail returned no accounts with a usable name; nothing can be read until "
                "an account exists on this Mac",
                evidence={"count": got.data.get("count"),
                          "accounts": [{"name": a["name"],
                                        "account_type": a.get("account_type"),
                                        "enabled": a.get("enabled"),
                                        "address_count": a.get("address_count")}
                                       for a in accounts[:10]],
                          "duration_ms": got.duration_ms,
                          "addresses_emitted": False,
                          "privacy": got.data.get("privacy")})


def _probe_mailbox_enumeration(context: ProbeContext, capability: Capability) -> dict:
    account = context.account()
    if not account:
        blocked = context.blocking()
        if blocked is not None:
            return _blocked_row(context, capability, blocked)
        accounts = context.accounts()
        return _row(context, capability, supported=False, state=O.UNSUPPORTED,
                    permission_state=O.PERMISSION_NOT_DETERMINED,
                    limitation="Mail reported no account to enumerate mailboxes against",
                    evidence={"outcome_code": accounts.code,
                              "detail": accounts.detail or None})
    got = context.mailboxes()
    if not got.usable:
        return _blocked_row(context, capability, got, {"account": account})
    boxes = got.data.get("mailboxes", [])
    return _row(context, capability, supported=bool(boxes), state=got.code,
                permission_state=O.PERMISSION_GRANTED,
                limitation=None if boxes else
                f"account {account!r} returned no mailboxes",
                evidence={"account": account, "count": got.data.get("count"),
                          "mailboxes": [b["name"] for b in boxes[:20]],
                          "duration_ms": got.duration_ms})


def _probe_bounded_message_listing(context: ProbeContext, capability: Capability) -> dict:
    page = context.page()
    if not page.usable:
        return _blocked_row(context, capability, page,
                            {"account": context.account(), "mailbox": context.mailbox()})
    coverage = page.data.get("coverage", {})
    items = page.data.get("items", [])
    # This row's assertion is that a bounded page comes back *with the mailbox's true
    # message count*, so that a capped scan is distinguishable from a complete one. A page
    # whose total count was never read did not evaluate that assertion, and may not claim
    # `supported` -- it is `unmeasured`, with the reason.
    total = coverage.get("mailbox_total_count")
    assertion_evaluated = total is not None
    supported = assertion_evaluated and (bool(items) or total == 0)
    limitation = page.detail if page.code == O.PARTIAL else None
    if not assertion_evaluated:
        limitation = ("the mailbox's true message count was not observed, so a capped scan "
                      "cannot be distinguished from a complete one: this row's assertion "
                      "was not evaluated (unmeasured, not unsupported)")
    return _row(context, capability, supported=supported,
                state=(page.code if assertion_evaluated else O.PROBE_UNMEASURED),
                permission_state=O.PERMISSION_GRANTED,
                limitation=limitation,
                evidence={"account": context.account(), "mailbox": context.mailbox(),
                          "page_size_requested": context.sample,
                          "observed_count": coverage.get("observed_count"),
                          "mailbox_total_count": total,
                          "scan_capped": coverage.get("scan_capped"),
                          "coverage_state": coverage.get("coverage_state"),
                          "assertion_evaluated": assertion_evaluated,
                          "has_next_cursor": bool(page.data.get("next_cursor")),
                          "ordering_sample": page.data.get("ordering_sample"),
                          "first_item_fields_unreadable":
                              sorted(k for k, v in (items[0] if items else {}).items()
                                     if v is None),
                          "duration_ms": page.duration_ms},
                values_from_source=(None if assertion_evaluated else False))


def _probe_historical_iteration(context: ProbeContext, capability: Capability) -> dict:
    page = context.page()
    if not page.usable:
        return _blocked_row(context, capability, page)
    cursor = page.data.get("next_cursor")
    first_ids = [i.get("internal_id") for i in page.data.get("items", [])]
    if not cursor:
        coverage = page.data.get("coverage", {})
        capped = bool(coverage.get("scan_capped"))
        # One page was walked and no cursor was offered, so *this row's assertion* -- that
        # a cursor resumes the next page and the pages are disjoint -- was never
        # evaluated. It cannot be `supported`; the honest state is `unmeasured`.
        return _row(context, capability, supported=False,
                    state=O.PROBE_UNMEASURED,
                    permission_state=O.PERMISSION_GRANTED,
                    limitation=("only one page was available to walk, so no cursor was "
                                "offered and resumption was not exercised: the assertion "
                                "(a cursor resumes the next page and the pages are "
                                "disjoint) was not evaluated"
                                + (" — this mailbox is larger than the bounded scan, so "
                                   "deeper history is unproven" if capped else
                                   " — this mailbox fitted inside one bounded page")),
                    evidence={"pages_walked": 1, "assertion_evaluated": False,
                              "first_page_ids": first_ids,
                              "mailbox_total_count": coverage.get("mailbox_total_count"),
                              "scan_capped": capped,
                              "note": "no cursor was offered because nothing remained "
                                      "inside the scanned window"},
                    values_from_source=False)
    second = context.page(cursor=cursor)
    if not second.usable:
        return _blocked_row(context, capability, second,
                            {"first_page_ids": first_ids, "cursor_present": True})
    second_ids = [i.get("internal_id") for i in second.data.get("items", [])]
    overlap = sorted(set(first_ids) & set(second_ids))
    resumed = bool(second_ids)
    if not resumed:
        # The cursor was offered but returned nothing, so resumption was exercised and
        # produced no page: the assertion is evaluated and does not hold.
        return _row(context, capability, supported=False, state=second.code,
                    permission_state=O.PERMISSION_GRANTED,
                    limitation=("the cursor was offered but the resumed page carried no "
                                "messages, so iteration did not resume"),
                    evidence={"pages_walked": 2, "assertion_evaluated": True,
                              "first_page_ids": first_ids, "second_page_ids": [],
                              "cursor_used": cursor})
    return _row(context, capability, supported=resumed and not overlap,
                state=second.code, permission_state=O.PERMISSION_GRANTED,
                limitation=(second.detail if second.code == O.PARTIAL else None),
                evidence={"pages_walked": 2, "assertion_evaluated": True,
                          "first_page_ids": first_ids,
                          "second_page_ids": second_ids, "overlap_ids": overlap,
                          "cursor_used": cursor,
                          "first_page_duration_ms": page.duration_ms,
                          "second_page_duration_ms": second.duration_ms,
                          "note": "Mail supplies no server-side cursor; the adapter's own "
                                  "cursor is re-validated against the mailbox on resume"})


def _probe_message_identifiers(context: ProbeContext, capability: Capability) -> dict:
    page = context.page()
    if not page.usable:
        return _blocked_row(context, capability, page)
    items = page.data.get("items", [])
    if not items:
        return _row(context, capability, supported=False, state=O.UNSUPPORTED,
                    permission_state=O.PERMISSION_GRANTED,
                    limitation="no message was sampled, so identifier behaviour is "
                               "unproven (the mailbox may be empty)",
                    evidence={"sampled": 0})
    internal = [i.get("internal_id") for i in items]
    with_rfc = [i for i in items if i.get("rfc_message_id")]
    # A second, independent scan of the same mailbox in the same run: ids that move
    # between two reads cannot be used as a cursor or a dedup key.
    rescan = context.adapter.enumerate(context.account(), context.scope(),
                                       limit=context.sample)
    rescan_ids = [i.get("internal_id") for i in (rescan.data or {}).get("items", [])] \
        if rescan.usable else []
    stable = bool(internal) and internal == rescan_ids
    return _row(context, capability, supported=stable,
                state=page.code, permission_state=O.PERMISSION_GRANTED,
                limitation=None if stable else
                "the identifier list changed between two reads in the same run; do not "
                "use these ids as a durable key without further measurement",
                evidence={"internal_ids": internal,
                          "rescan_ids": rescan_ids,
                          "rescan_outcome": rescan.code,
                          "messages_with_rfc_message_id": len(with_rfc),
                          "namespaced_reference_example": items[0].get("namespaced_id"),
                          "rfc_id_present_in_headers": len(with_rfc) > 0,
                          "note": "reported ids are Mail's internal integers and the "
                                  "provider Message-ID; no address is included"})


def _probe_message_dates(context: ProbeContext, capability: Capability) -> dict:
    page = context.page()
    if not page.usable:
        return _blocked_row(context, capability, page)
    items = page.data.get("items", [])
    dated = [i for i in items if i.get("source_time_local")]
    basis = sorted({i.get("time_basis") for i in items if i.get("time_basis")})
    return _row(context, capability, supported=bool(dated) and bool(items),
                state=page.code, permission_state=O.PERMISSION_GRANTED,
                limitation=("dates are rendered in the Mini's local timezone and "
                            "AppleScript returns no UTC offset, so the worker labels the "
                            "stamp local instead of inventing a UTC value"),
                evidence={"sampled": len(items), "dated": len(dated),
                          "oldest_observed": dated[0]["source_time_local"] if dated else None,
                          "newest_observed": dated[-1]["source_time_local"] if dated else None,
                          "raw_text_sample": dated[0].get("source_time_text") if dated else None,
                          "time_basis": basis,
                          "utc_offset_minutes": None})


def _probe_sender_recipient_extraction(context: ProbeContext,
                                       capability: Capability) -> dict:
    page = context.page()
    if not page.usable:
        return _blocked_row(context, capability, page)
    items = page.data.get("items", [])
    with_sender = [i for i in items if i.get("sender_fingerprint")]
    with_counts = [i for i in items if (i.get("recipient_counts") or {}).get("to") is not None]
    supported = bool(items) and len(with_sender) == len(items) and len(with_counts) == len(items)
    return _row(context, capability, supported=supported, state=page.code,
                permission_state=O.PERMISSION_GRANTED,
                limitation=None if supported else
                "sender or recipient counts were unreadable for at least one sampled "
                "message on this build; the terminology needs confirmation (PRD §11)",
                evidence={"sampled": len(items), "sender_readable": len(with_sender),
                          "recipient_counts_readable": len(with_counts),
                          "sender_masked_sample":
                              [i.get("sender_masked") for i in items[:3]],
                          "recipient_counts_sample":
                              [i.get("recipient_counts") for i in items[:3]],
                          "addresses_emitted_in_full": False,
                          "note": "addresses are fingerprinted or masked; the ingest slice "
                                  "will need the real address locally, which is Randy's "
                                  "call to record"})


def _probe_body_retrieval(context: ProbeContext, capability: Capability) -> dict:
    got = context.first_retrieval()
    if not got.usable:
        return _blocked_row(context, capability, got)
    state = (got.data or {}).get("body_state")
    supported = bool(got.usable) and state in ("fetched", "empty")
    return _row(context, capability, supported=supported, state=got.code,
                permission_state=O.PERMISSION_GRANTED,
                limitation=(None if supported else
                            "message content could not be read on this build; the probe "
                            "reports unavailable, never empty"),
                evidence={"reference": _ref_of(context), "body_state": state,
                          "body_length": (got.data or {}).get("body_length"),
                          "unavailable_reason": (got.data or {}).get("unavailable_reason"),
                          "body_content_emitted": False,
                          "note": "body content stays on the Mac; only length and a "
                                  "fingerprint are reported"})


def _probe_raw_source_retrieval(context: ProbeContext, capability: Capability) -> dict:
    account, page = context.account(), context.page()
    if not account or not page.usable:
        return _blocked_row(context, capability, page)
    items = page.data.get("items", [])
    if not items:
        return _row(context, capability, supported=False, state=O.UNSUPPORTED,
                    permission_state=O.PERMISSION_NOT_DETERMINED,
                    limitation="no message was sampled to read raw source from",
                    evidence={"sampled": 0})
    from .mail_adapter import parse_ref
    ref_account, mailbox, internal_id = parse_ref(items[0]["namespaced_id"])
    index = items[0].get("provider_index")
    got = context.adapter.transport.message_source(ref_account, mailbox, index=index)
    if not got.usable:
        return _blocked_row(context, capability, got, {"reference": items[0]["namespaced_id"]})
    return _row(context, capability, supported=True, state=got.code,
                permission_state=O.PERMISSION_GRANTED,
                limitation="raw source can be large; the worker truncates at its output "
                           "bound and reports the length it actually read",
                evidence={"reference": items[0]["namespaced_id"],
                          "source_length": (got.data or {}).get("source_length"),
                          "source_fingerprint": (got.data or {}).get("source_fingerprint"),
                          "source_content_emitted": False})


def _probe_attachment_enumeration(context: ProbeContext, capability: Capability) -> dict:
    account, page = context.account(), context.page()
    if not account or not page.usable:
        return _blocked_row(context, capability, page)
    items = page.data.get("items", [])
    if not items:
        return _row(context, capability, supported=False, state=O.UNSUPPORTED,
                    permission_state=O.PERMISSION_NOT_DETERMINED,
                    limitation="no message was sampled to enumerate attachments from",
                    evidence={"sampled": 0})
    from .mail_adapter import parse_ref
    ref_account, mailbox, _ = parse_ref(items[0]["namespaced_id"])
    got = context.adapter.transport.attachments(ref_account, mailbox,
                                                index=items[0].get("provider_index"))
    if not got.usable:
        return _blocked_row(context, capability, got,
                            {"reference": items[0]["namespaced_id"]})
    data = got.data or {}
    attachments = data.get("attachments") or []
    # This row's assertion is that attachment metadata resolves *per item* and that each
    # attachment's local availability is reported separately. A message with no attachment
    # exercises neither half, so the row is `unmeasured` rather than supported.
    comparable = [a for a in attachments
                  if a.get("filename") and a.get("downloaded") is not None]
    if not attachments:
        return _row(context, capability, supported=False, state=O.PROBE_UNMEASURED,
                    permission_state=O.PERMISSION_GRANTED,
                    limitation=("the sampled message carried no attachment, so this row's "
                                "assertion (metadata resolves per item, availability "
                                "reported separately) was not evaluated: unmeasured, not "
                                "unsupported"),
                    evidence={"reference": items[0]["namespaced_id"],
                              "count": data.get("count"),
                              "assertion_evaluated": False,
                              "note": data.get("note")},
                    values_from_source=False)
    complete_per_item = bool(comparable) and len(comparable) == len(attachments)
    if got.code == O.PARTIAL:
        limitation = got.detail
    elif complete_per_item:
        limitation = None
    else:
        limitation = ("at least one attachment did not report a filename and a downloaded "
                      "state, so per-item enumeration is unproven")
    return _row(context, capability,
                supported=complete_per_item,
                state=got.code, permission_state=O.PERMISSION_GRANTED,
                limitation=limitation,
                evidence={"reference": items[0]["namespaced_id"],
                          "count": data.get("count"),
                          "not_downloaded_locally": data.get("not_downloaded_locally"),
                          "assertion_evaluated": True,
                          "attachments_with_filename_and_availability": len(comparable),
                          "attachments": [{"filename": a.get("filename"),
                                           "mime_type": a.get("mime_type"),
                                           "size_bytes": a.get("size_bytes"),
                                           "downloaded": a.get("downloaded")}
                                          for a in attachments[:10]],
                          "note": data.get("note")})


def _probe_deliberately_absent(context: ProbeContext, capability: Capability) -> dict:
    """Record the adapter's actual refusal for a capability this slice does not build."""
    account = context.account() or "unknown"
    calls = {
        "attachment_materialization":
            lambda: context.adapter.materialize_attachment(account, "mail:x:INBOX:1"),
        "draft_preparation":
            lambda: context.adapter.prepare_draft(account, {"draft_id": "probe"}),
        "authorized_send":
            lambda: context.adapter.dispatch(account, {"idempotency_key": "probe"}),
        "send_reconciliation":
            lambda: context.adapter.reconcile(account, idempotency_key="probe"),
    }
    got = calls[capability.name]()
    return _row(context, capability, supported=False, state=got.code,
                permission_state=O.PERMISSION_NOT_DETERMINED,
                limitation=capability.limitation,
                evidence={"outcome_code": got.code, "reason": got.reason,
                          "detail": got.detail,
                          "demonstrated": ("the adapter was called and refused; this row "
                                           "is an observed refusal, not an assumption"),
                          "next_action": got.next_action})


def _probe_documented(context: ProbeContext, capability: Capability) -> dict:
    """One row per capability the Gate 2 pack documents but this worker cannot measure.

    Nothing is contacted and nothing is claimed: the row is ``origin: documentation``,
    ``supported: false``, ``state: 'unmeasured'``, and it carries the pack refs it quotes,
    the page's own facts, the page's silences and the Mac procedure that would settle it.
    This behaves identically on every host on purpose -- the source has no adapter here or
    on the Mac yet, so there is nothing a different host could measure differently, and
    inventing a host-specific answer would be the one thing this row must not do.
    """
    from .documented_capabilities import PACK_PATH
    evidence = {
        "documentation_only": True,
        "citations": list(capability.citations),
        "pack_path": PACK_PATH,
        "documented_facts": list(capability.documented),
        "documented_absences": list(capability.absences),
        "mac_probe_procedure": list(capability.procedure),
        "why_unmeasured": (
            "this worker has no " + capability.source + " adapter in this slice, so nothing "
            "was read and nothing could be measured on any host. The row records the "
            "documented surface and the procedure that measures it; it never records a "
            "result. A documentation read can never set supported: true"),
        "no_source_contacted": True,
        "observed_version_reason": (
            "documentation read only: no source was contacted by this row, so no version "
            "could be observed"),
    }
    return _row(context, capability, supported=False, state=O.PROBE_UNMEASURED,
                permission_state=O.PERMISSION_NOT_DETERMINED,
                limitation=capability.limitation,
                evidence=evidence, origin=O.DOCUMENTATION, adapter_is_real=False,
                values_from_source=False, citations=capability.citations)


def _ref_of(context: ProbeContext) -> Optional[str]:
    page = context.page()
    items = (page.data or {}).get("items") or [] if page.usable else []
    return items[0].get("namespaced_id") if items else None


_PROBES = {
    "manifest": _probe_manifest,
    "health": _probe_health,
    "account_enumeration": _probe_account_enumeration,
    "mailbox_enumeration": _probe_mailbox_enumeration,
    "bounded_message_listing": _probe_bounded_message_listing,
    "historical_iteration": _probe_historical_iteration,
    "message_identifiers": _probe_message_identifiers,
    "message_dates": _probe_message_dates,
    "sender_recipient_extraction": _probe_sender_recipient_extraction,
    "body_retrieval": _probe_body_retrieval,
    "raw_source_retrieval": _probe_raw_source_retrieval,
    "attachment_enumeration": _probe_attachment_enumeration,
    "attachment_materialization": _probe_deliberately_absent,
    "draft_preparation": _probe_deliberately_absent,
    "authorized_send": _probe_deliberately_absent,
    "send_reconciliation": _probe_deliberately_absent,
}
# Every capability recorded from the Gate 2 probe pack is answered by the documentation
# probe: it contacts nothing, measures nothing and says so.
for _documented in DOCUMENTED_CAPABILITY_NAMES:
    _PROBES[_documented] = _probe_documented

assert set(_PROBES) == set(CAPABILITY_NAMES), "every capability needs a probe"


@dataclass
class ProbeRun:
    rows: list = field(default_factory=list)
    harness_errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.harness_errors

    def summary(self) -> dict:
        supported = [r["capability"] for r in self.rows if r["supported"]]
        unsupported = [r["capability"] for r in self.rows if not r["supported"]]
        states = {}
        for row in self.rows:
            states[row["state"]] = states.get(row["state"], 0) + 1
        permissions = {}
        for row in self.rows:
            permissions[row["permission_state"]] = permissions.get(row["permission_state"], 0) + 1
        sources = {}
        documented = []
        for row in self.rows:
            sources[row.get("source")] = sources.get(row.get("source"), 0) + 1
            if row.get("origin") == O.DOCUMENTATION:
                documented.append(row["capability"])
        superseding = [r["capability"] for r in self.rows if r.get("supersedes")]
        return {"capabilities": len(self.rows), "supported": supported,
                "unsupported": unsupported, "states": states,
                "permission_states": permissions,
                "sources": sources,
                "superseding_rows": superseding,
                "documentation_rows_superseded_by_a_measurement": superseding,
                "documentation_rows": documented,
                "documentation_rows_are_never_supported": not any(
                    r["supported"] for r in self.rows
                    if r.get("origin") == O.DOCUMENTATION),
                "harness_errors": len(self.harness_errors)}


def _handler_for(context: ProbeContext, capability: Capability):
    """Which handler measures this capability in this run?

    An adapter that declares the capability in ``measured_capabilities()`` and provides a
    handler for it wins; otherwise the default registry answers (the Mail probes for the
    Mail rows, ``_probe_documented`` for a capability whose documentation is all the pack
    has). That is the rule that keeps a measured row and its documentation row from both
    being emitted for one capability key.
    """
    for adapter in context.adapters.values():
        measured = getattr(adapter, "measured_capabilities", None)
        handlers = getattr(adapter, "probe_handlers", None)
        if not callable(measured) or not callable(handlers):
            continue
        if capability.name not in set(measured()):
            continue
        handler = (handlers() or {}).get(capability.name)
        if handler is not None:
            return handler
    return _PROBES[capability.name]


def row_coexistence_problems(rows: list) -> list:
    """Why this set of rows may not be imported as a probe run.

    One row per capability key, and a documentation row may never sit beside the
    measurement that superseded it: two rows for one capability with different origins
    would let a reader (or an importer) treat a page read as a measurement, or the other
    way round.
    """
    problems: list = []
    seen: dict = {}
    for row in rows:
        name = row.get("capability") or row.get("name")
        origin = row.get("origin")
        if name in seen:
            problems.append(f"two rows for capability {name!r} in one run: "
                            f"{seen[name]!r} and {origin!r}")
        else:
            seen[name] = origin
        if row.get("supported") and origin == O.DOCUMENTATION:
            problems.append(f"{name}: a documentation row may never be supported=true")
    superseding = {r.get("capability") for r in rows if r.get("supersedes")}
    documented = {r.get("capability") for r in rows if r.get("origin") == O.DOCUMENTATION}
    for name in sorted(superseding & documented):
        problems.append(f"{name}: a measured row and the documentation row it supersedes "
                        "are both in this run")
    return problems


def run_probe(adapter, *, sample: int = 5, account: Optional[str] = None,
              mailbox: Optional[str] = None, max_scan: int = 2000,
              beeper_account_id: Optional[str] = None,
              beeper_ui_oldest_visible: Optional[str] = None,
              only_source: Optional[str] = None) -> ProbeRun:
    """Produce one row per capability. Never raises for an unsupported capability.

    ``adapter`` may be one adapter or a list of them: each capability is measured by the
    adapter that owns its source, and a capability no adapter in the run can measure is
    reported as the documentation read it is.
    """
    context = ProbeContext(adapter, sample=sample, account=account, mailbox=mailbox,
                           max_scan=max_scan, beeper_account_id=beeper_account_id,
                           beeper_ui_oldest_visible=beeper_ui_oldest_visible)
    run = ProbeRun()
    for capability in CAPABILITIES:
        if only_source and capability.source != only_source:
            # `only_source` keeps a single-source run (the `beeper probe` subcommand) from
            # handing another source's rows to this adapter: a Mail row measured by a
            # Beeper adapter would be a fabricated measurement.
            continue
        try:
            run.rows.append(_handler_for(context, capability)(context, capability))
        except Exception as exc:      # harness failure: reported as a row AND a non-zero exit
            trace = traceback.format_exc(limit=4)
            context.harness_errors.append({"capability": capability.name,
                                           "exception": type(exc).__name__,
                                           "message": str(exc)})
            run.harness_errors.append({"capability": capability.name,
                                       "exception": type(exc).__name__,
                                       "message": str(exc)})
            row = _row(context, capability, supported=False, state="harness_error",
                       permission_state=O.PERMISSION_NOT_DETERMINED,
                       limitation=f"the probe harness failed: {type(exc).__name__}: {exc}",
                       evidence={"traceback_tail": trace.splitlines()[-6:]})
            run.rows.append(row)
    for problem in row_coexistence_problems(run.rows):
        # A worker defect, not a source state: reported as a harness failure so the run
        # cannot be imported as a measurement.
        run.harness_errors.append({"capability": "row_coexistence",
                                   "exception": "RowCoexistence", "message": problem})
    return run
