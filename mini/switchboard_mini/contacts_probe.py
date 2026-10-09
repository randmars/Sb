"""The five Contacts capability rows, measured on Randy's Mac.

Before this slice, all five Contacts rows in ``documented_capabilities.py`` were
``origin: documentation`` / ``supported: false`` / ``state: unmeasured``, and the pack's own
Contacts procedure was recorded as *not runnable* because the worker shipped no Contacts
path. Each handler here binds one of those five capability keys to a real read through
:mod:`switchboard_mini.contacts_adapter`, exactly as ``beeper_probe.py`` binds the Beeper
rows and ``probe.py`` binds the Mail rows:

* when the Contacts adapter **is** in the probe run, the row is a measurement: its origin is
  the adapter's (``real`` on the Mac, ``fixture`` for a recorded scenario), its state and
  evidence are what the read returned, its ``citations`` are empty because a measurement
  quotes no page, and the documentation row it replaces is named in ``supersedes`` so
  Grace's import reports the supersession rather than holding two rows for one key;
* when the adapter is **not** in the run, ``probe.py`` falls back to the documentation row
  for that key, exactly as before this slice.

Three rules, and each one is the reason a row is not simply ``supported: true``:

* ``supported`` means **this row's own assertion was evaluated and held**. A status read
  that was not a denial does not evaluate the "a denial is observable" half; a single
  Contacts database read does not evaluate "identifiers are stable across launches"; a
  unification toggle the installed framework does not have does not evaluate "the unified
  identifier differs from the constituents'".
* Where a half was not evaluated, the row is ``unmeasured`` with the missing half named and
  the command that would evaluate it -- never rounded up to supported.
* A *fixture* row can never be supported: a recorded scenario is not an observation of any
  Mac (``real_source_connected`` is false by construction, whatever the file says).

One more thing, recorded rather than smoothed over: this row set includes
``contacts_read_authorization``, whose assertion as recorded requires the usage-declaration
to ship in "the helper's Info.plist". The Mini worker is a command-line tool, not an app
bundle, so it has no Info.plist and the TCC grant is attributed to the responsible process
(Terminal, or whatever launched it). That half therefore cannot hold as written, and the row
says so and stays unsupported while reporting everything else it *did* observe. Amending
that assertion is the lead's call, not something to be papered over here.
"""

from __future__ import annotations

import os
from typing import Optional

from . import outcomes as O
from .contacts_transport import (AUTHORIZATION_STATUS_NAMES, CALL_SOURCES,
                                 AUTHORIZATION_WITHOUT_AN_EQUIVALENT, MINIMAL_KEYS,
                                 RESTRICTED_KEY_SYMBOL_ASK, permission_state_for,
                                 status_without_an_equivalent)
from .probe import _blocked_row, _row

#: The event classes O15 names as required visitor methods, plus the other
#: ``CNChangeHistory*`` classes O13's topic list names. An event outside this set is recorded
#: as an unknown class rather than folded into a documented one.
DOCUMENTED_EVENT_CLASSES = (
    "CNChangeHistoryDropEverythingEvent",
    "CNChangeHistoryAddContactEvent",
    "CNChangeHistoryUpdateContactEvent",
    "CNChangeHistoryDeleteContactEvent",
    "CNChangeHistoryAddGroupEvent",
    "CNChangeHistoryAddMemberToGroupEvent",
    "CNChangeHistoryAddSubgroupToGroupEvent",
    "CNChangeHistoryDeleteGroupEvent",
    "CNChangeHistoryRemoveMemberFromGroupEvent",
    "CNChangeHistoryRemoveSubgroupFromGroupEvent",
    "CNChangeHistoryUpdateGroupEvent",
)

#: The declaration half of ``contacts_read_authorization`` as recorded, and why it cannot
#: hold for this worker. Kept as data so the row, the runbook and this report all quote one
#: sentence.
DECLARATION_GAP = (
    "this row's assertion also requires the usage declaration to ship in \"the helper's "
    "Info.plist\" (O16: 'This key is required if your app uses APIs that access the user's "
    "contacts'). The Mini worker is a command-line tool, not an app bundle: it has no "
    "Info.plist, and macOS attributes a TCC grant to the *responsible process* that launched "
    "it, so the declaration half cannot hold as written and the row is honestly unsupported "
    "until that assertion is amended deliberately. Everything the row *did* observe (the "
    "status the store reported, and whether a non-granted status was typed) is in evidence"
)


def _adapter(context):
    adapter = context.for_source("contacts")
    if adapter is None:
        raise RuntimeError("the Contacts probe ran without a Contacts adapter in the run")
    return adapter


def _document(outcome) -> dict:
    return dict(outcome.data or {})


def _supersedes(capability) -> Optional[dict]:
    """The documentation row this measurement replaces, named explicitly."""
    if not capability.citations:
        return None
    return {
        "origin": O.DOCUMENTATION,
        "capability": capability.name,
        "citations": list(capability.citations),
        "documented_state": O.PROBE_UNMEASURED,
        "note": ("this row is the measurement for a capability key that also has a "
                 "documentation record in the probe pack; the pack row is superseded by this "
                 "one and must not be stored beside it (grace probe-import reports the "
                 "supersession, and refuses a documentation row that would replace a stored "
                 "real measurement)"),
    }


def _base_evidence(context, adapter) -> dict:
    from .contacts_transport import SCHEMA
    return {
        "adapter": adapter.name,
        "schema": SCHEMA,
        "prompt_never_requested_by_a_read": True,
        "call_sources": {name: {"source": src, "quote": quote}
                         for name, (src, quote) in CALL_SOURCES.items()},
        "minimal_keys": list(MINIMAL_KEYS),
        "pack_refs_for_the_documented_half": ["O13", "O14", "O15", "O16"],
    }


def _status_document(document: dict) -> dict:
    """The authorization block: a refusal carries it at the top level, a read nested."""
    nested = document.get("authorization")
    if isinstance(nested, dict) and nested:
        return nested
    return document


def _permission_state_for_document(document: dict) -> str:
    source = _status_document(document)
    state = permission_state_for(source)
    if state is not None:
        return state
    # A documented status this product has no word for (``limited``): the honest report is
    # ``not_determined`` -- a state exists, and this product cannot name it -- and the
    # caller's limitation says which status it was. The value is never rounded to granted.
    return O.PERMISSION_NOT_DETERMINED


#: What a *measured* row says about its own limitation. It is never the pack's
#: documentation text: that text was written when no Contacts adapter existed ("unmeasured: no
#: Contacts adapter exists in this worker"), and repeating it on a row that just measured the
#: thing would be a stale claim in the one place a reader looks for the caveat.
HELD_NOTE = ("this row's assertion was evaluated against the source itself on this Mac and "
             "held. The pack's documentation text for this capability is deliberately not "
             "repeated here: it was written while the worker had no Contacts read path at "
             "all, and the measurement supersedes it")
#: The fixture sentence. A recorded scenario is not an observation of any Mac.
FIXTURE_NOTE = ("answered from a recorded fixture, not from a real Contacts store: this "
                "row's assertion was not evaluated against any Mac")


def _measured(context, capability, outcome, *, supported: bool,
              limitation: Optional[str], evidence: dict, state: Optional[str] = None,
              values_from_source: Optional[bool] = None) -> dict:
    """Build a measured row, with the two rules that stop it over-claiming applied here.

    * A row whose adapter is not the real one -- the fixture twin -- is ``supported: false``
      and says so, whatever state the recorded response produced. Fixture mode still runs
      the whole row path, so it is the thing that keeps this honest that is checked here.
    * A row that held its assertion carries :data:`HELD_NOTE` rather than the pack's
      documentation limitation, so the caveat a reader sees is true of *this* measurement.
    """
    adapter = _adapter(context)
    if not adapter.adapter_is_real:
        supported = False
        values_from_source = None
        limitation = FIXTURE_NOTE + ((" " + limitation) if limitation else "")
    elif supported and not limitation:
        limitation = HELD_NOTE
    if values_from_source is None and adapter.adapter_is_real:
        values_from_source = bool(outcome.real_source_connected)
    return _row(context, capability, supported=bool(supported),
                state=(state or outcome.code),
                permission_state=_permission_state_for_document(_document(outcome)),
                limitation=limitation, evidence=evidence,
                origin=adapter.origin, adapter_is_real=adapter.adapter_is_real,
                label=adapter._label(), citations=(),
                supersedes=_supersedes(capability),
                values_from_source=values_from_source)


#: The reasons that mean "a real Mac answered, and the call is not there", as opposed to
#: "this host never looked". Only the former may be reported as the row's accepted negative.
NEGATIVE_BRANCH_REASONS = ("unify_toggle_not_in_this_sdk", "selector_not_in_this_sdk")


def _blocked(context, capability, outcome, evidence: Optional[dict] = None) -> dict:
    """A typed refusal, stamped with the Contacts adapter's provenance (not Mail's).

    Two things are done here that the shared helper does not do by default:

    * the permission state is taken from the status the helper actually reported when the
      refusal carries one, so ``restricted`` stays ``restricted`` instead of collapsing into
      the ``denied`` that ``permission_denied`` would imply;
    * the limitation is this read's own sentence, not the pack's documentation text. That
      text says "no Contacts adapter exists in this worker", which was true when it was
      written and is not true now -- repeating it in the one place a reader looks for the
      caveat would be a stale claim on a row that is already reporting a refusal.
    """
    adapter = _adapter(context)
    document = _document(outcome)
    state = permission_state_for(_status_document(document))
    detail = outcome.detail or outcome.code
    if adapter.adapter_is_real:
        limitation = (detail + " (the pack's documentation text for this capability is not "
                               "repeated here: it was written while the worker had no "
                               "Contacts read path at all)")
    else:
        limitation = FIXTURE_NOTE + " " + detail
    return _blocked_row(context, capability, outcome, evidence,
                        origin=adapter.origin, adapter_is_real=adapter.adapter_is_real,
                        label=adapter._label(), supersedes=_supersedes(capability),
                        permission_state=state, limitation=limitation)


# ------------------------------------------------------- the memoized reads -----

def _authorization(context):
    adapter = _adapter(context)
    return context._once("contacts:authorization", adapter.authorization)


def _enumerate(context):
    adapter = _adapter(context)
    return context._once("contacts:enumerate",
                         lambda: adapter.enumerate_contacts(unify_off=False))


def _individual(context):
    """The same fetch with unification turned off, if the installed build allows it."""
    adapter = _adapter(context)
    return context._once("contacts:enumerate_individual",
                         lambda: adapter.enumerate_contacts(unify_off=True))


def _restricted_keys(context):
    adapter = _adapter(context)
    return context._once("contacts:restricted_keys",
                         lambda: adapter.restricted_keys(context.contacts_key_symbol()))


def _change_history(context, *, invalid_token: bool = False):
    adapter = _adapter(context)
    key = "contacts:change_history_invalid" if invalid_token else "contacts:change_history"
    return context._once(key, lambda: adapter.change_history(invalid_token=invalid_token))


def _ids(document: dict) -> list:
    return [i.get("identifier_fingerprint") for i in (document.get("items") or [])
            if i.get("identifier_fingerprint")]


def _counts(document: dict) -> dict:
    items = document.get("items") or []
    return {
        "returned": len(items),
        "with_identifier": sum(1 for i in items if i.get("identifier_fingerprint")),
        "with_name": sum(1 for i in items if i.get("has_name")),
        "with_email": sum(1 for i in items if i.get("has_email")),
        "with_phone": sum(1 for i in items if i.get("has_phone")),
        "without_any_identity_field": sum(1 for i in items
                                          if not (i.get("has_name") or i.get("has_email")
                                                  or i.get("has_phone"))),
    }


def _identifier_shape(document: dict) -> dict:
    lengths = [i.get("identifier_length") for i in (document.get("items") or [])
               if i.get("identifier_length")]
    shapes = sorted({i.get("identifier_shape") for i in (document.get("items") or [])
                     if i.get("identifier_shape")})
    identifiers = _ids(document)
    return {
        "identifier_length_min": min(lengths) if lengths else None,
        "identifier_length_max": max(lengths) if lengths else None,
        "identifier_shape_classes": shapes,
        "distinct_identifiers": len(set(identifiers)),
        "duplicate_identifier_values_in_one_read": len(identifiers) - len(set(identifiers)),
        "device_local_identifiers": True,
        "identifier_values_recorded": False,
    }


def _persistence_evidence(context, document: dict, adapter=None) -> dict:
    """The persistence half, delegated to the shared comparison in the transport."""
    from .contacts_transport import compare_identifier_fingerprints
    path = context.contacts_identifier_file()
    current = _ids(document)
    if path and adapter is not None and not getattr(adapter, "adapter_is_real", False):
        return {"persistence_across_runs": "not evaluated (recorded fixture)",
                "persistence_unmeasured_reason":
                    "a recorded fixture is not an observation of any Mac, so no comparison "
                    "file is written and nothing is claimed about identifier stability"}
    return compare_identifier_fingerprints(path, current, write=bool(path))


def _unification_evidence(context) -> dict:
    """The unified-vs-individual half, from the same run when the build allows it."""
    individual = _individual(context)
    document = _document(individual)
    unified_ids = set(_ids(_document(_enumerate(context))))
    evidence = {
        "unify_off_requested": True,
        "unify_off_selector_checked": document.get("unify_off_selector_checked"),
        "unify_off_selector_present": document.get("unify_off_selector_present"),
        "unify_off_outcome": individual.code,
        "unify_off_reason": individual.reason,
        "unify_off_detail": individual.detail,
    }
    if not individual.usable:
        evidence["unification_half_evaluated"] = False
        evidence["unification_half_note"] = (
            "the individual-record fetch did not answer, so nothing is claimed about the "
            "constituents of a unified contact")
        return evidence
    individual_ids = set(_ids(document))
    evidence.update({
        "unification_half_evaluated": bool(individual_ids and unified_ids),
        "individual_returned": len(document.get("items") or []),
        "individual_distinct_identifiers": len(individual_ids),
        "identifiers_in_both_reads": len(unified_ids & individual_ids),
        "unified_identifier_equals_an_individual_identifier": bool(unified_ids & individual_ids),
        "identifier_values_recorded": False,
        "note": ("O13: 'Each fetched unified contact object (CNContact) has its own unique "
                 "identifier that's different from any individual contact's identifier in "
                 "the set of linked contacts.' This row measures whether that difference is "
                 "observable on this Mac"),
    })
    return evidence


def _restricted_key_evidence(context) -> dict:
    got = _restricted_keys(context)
    document = _document(got)
    return {
        "restricted_key_outcome": got.code,
        "restricted_key_reason": got.reason,
        "restricted_key_detail": got.detail,
        "restricted_key_symbol_supplied": document.get("symbol_supplied", False),
        "restricted_key_symbol_defined_at_runtime": document.get(
            "symbol_defined_at_runtime"),
        "restricted_key_plain_fetch_ok": document.get("plain_fetch_ok"),
        "restricted_key_guarded_fetch_raised": document.get("guarded_fetch_raised"),
        "restricted_key_guarded_fetch_error": document.get("guarded_fetch_error"),
        "restricted_key_guarded_key_guard_note": document.get("guarded_key_guard_note"),
        "restricted_key_ask": RESTRICTED_KEY_SYMBOL_ASK,
        "note": ("O13 records the notes entitlement com.apple.developer.contacts.notes but "
                 "names no note key, so the symbol is the owner's to read from the installed "
                 "SDK header; this evidence records what happened when he did"),
    }


# ----------------------------------------------------------- the five rows ------

def probe_read_authorization(context, capability) -> dict:
    adapter = _adapter(context)
    got = _authorization(context)
    document = _document(got)
    evidence = _base_evidence(context, adapter)
    if not got.usable:
        evidence.update({"refusal": document.get("refusal"), "fixture_scenario":
                         document.get("scenario")})
        return _blocked(context, capability, got, evidence)
    status_name = document.get("resolved_name")
    state_word = permission_state_for(document)
    without = status_without_an_equivalent(document)
    denial_observed = status_name == "CNAuthorizationStatusDenied"
    non_granted_typed = bool(state_word) and state_word != O.PERMISSION_GRANTED
    evidence.update({
        "status_case_name": status_name,
        "status_raw_value": document.get("raw_value"),
        "constants_resolved_on_this_build": document.get("constants_resolved"),
        "status_selector_checked": document.get("selector_checked"),
        "status_selector_present": document.get("selector_present"),
        "permission_state": state_word or O.PERMISSION_NOT_DETERMINED,
        "permission_vocabulary": list(O.PERMISSION_STATES),
        "denial_observed": denial_observed,
        "non_granted_status_typed_as_a_typed_state": non_granted_typed,
        "typed_outcome_for_a_read_without_a_grant": (
            "permission_denied (the read path refuses before constructing a fetch, so no "
            "consent dialog is raised)"),
        "declaration_in_an_info_plist": None,
        "declaration_gap": DECLARATION_GAP,
        "responsible_process": document.get("responsible_process"),
        "mac_probe_steps_not_yet_run": [
            "the consent dialog's own text (not readable by the process it is shown for)",
            "whether the prompt appears a second time after a grant (O13: it should not)",
            "the System Settings entry's name and whether the helper's process appears in it",
            "first-run blocking duration (--contacts-request-access reports blocked_ms)",
        ],
    })
    if without:
        limitation = (f"{without}. The status is recorded verbatim and the row stays "
                      "unsupported rather than being rounded to granted or denied.")
        return _measured(context, capability, got, supported=False, state=O.UNSUPPORTED,
                         limitation=limitation, evidence=evidence)
    limitation = DECLARATION_GAP
    if state_word == O.PERMISSION_GRANTED:
        limitation += (" Additionally, a granted store cannot evaluate this row's denial half: "
                       "run `switchboard-mini contacts request-access` and choose \"Don't "
                       "Allow\" to measure that half (it is a deliberate, reversible test).")
    return _measured(context, capability, got, supported=False, state=got.code,
                     limitation=limitation, evidence=evidence)


def probe_enumerate_contacts(context, capability) -> dict:
    adapter = _adapter(context)
    got = _enumerate(context)
    document = _document(got)
    evidence = _base_evidence(context, adapter)
    if not got.usable:
        evidence.update(_restricted_key_evidence(context))
        return _blocked(context, capability, got, evidence)
    counts = _counts(document)
    missing_keys = document.get("keys_missing_at_runtime") or []
    guard_notes = document.get("key_guard_notes") or {}
    guarded_keys = sorted(k for k in guard_notes if k in (document.get("keys_resolved") or []))
    evidence.update({
        "call_used": document.get("call_used"),
        "selector_checked": document.get("selector_checked"),
        "selector_present": document.get("selector_present"),
        "keys_requested": document.get("keys_requested"),
        "keys_resolved": document.get("keys_resolved"),
        "keys_missing_at_runtime": missing_keys,
        "key_guard_notes": guard_notes,
        "counts": counts,
        "limit": document.get("limit"),
        "enumeration_truncated_by_limit": document.get("truncated"),
        "stop_flag_unavailable": document.get("stop_flag_unavailable"),
        "fetch_error": document.get("error"),
        "values_emitted": False,
        "note": ("counts only leave the Mac: no name, address, phone number or note is "
                 "reported (O13 procedure step 3)"),
    })
    evidence.update(_restricted_key_evidence(context))
    if counts["returned"] == 0:
        return _measured(
            context, capability, got, supported=False, state=O.PROBE_UNMEASURED,
            limitation=("the fetch answered and returned no contact at all, so this row's "
                        "assertion -- a bounded fetch returns contacts, with the counts of "
                        "those carrying an email address and a phone number -- was not "
                        "evaluated. An empty Contacts database is a result, not a defect"),
            evidence={**evidence, "assertion_evaluated": False})
    assertion_evaluated = (counts["with_identifier"] == counts["returned"]
                           and not guarded_keys and not missing_keys)
    limitation = None
    if not assertion_evaluated:
        reasons = []
        if counts["with_identifier"] != counts["returned"]:
            reasons.append(f"{counts['returned'] - counts['with_identifier']} returned item(s) "
                           "carried no readable identifier")
        if missing_keys:
            reasons.append("documented key(s) absent on this build: " + ", ".join(missing_keys))
        if guarded_keys:
            reasons.append("key(s) reported as not fetched for at least one contact: "
                           + ", ".join(guarded_keys))
        limitation = ("the fetch returned contacts but this row's assertion was only partly "
                      "evaluated: " + "; ".join(reasons))
    return _measured(context, capability, got, supported=assertion_evaluated,
                     limitation=limitation,
                     evidence={**evidence, "assertion_evaluated": assertion_evaluated})


def probe_identifier_scope(context, capability) -> dict:
    adapter = _adapter(context)
    got = _enumerate(context)
    document = _document(got)
    evidence = _base_evidence(context, adapter)
    if not got.usable:
        return _blocked(context, capability, got, evidence)
    shape = _identifier_shape(document)
    evidence.update(shape)
    evidence.update(_persistence_evidence(context, document, adapter))
    evidence.update(_unification_evidence(context))
    counts = _counts(document)
    persistence = evidence.get("persistence_across_runs")
    evidence["persistence_half_evaluated"] = persistence in ("stable", "changed_or_incomplete")
    evidence["unification_half_evaluated"] = bool(evidence.get("unification_half_evaluated"))
    unified_difference_observed = bool(
        evidence.get("unification_half_evaluated")
        and evidence.get("individual_distinct_identifiers")
        and not evidence.get("unified_identifier_equals_an_individual_identifier"))
    evidence["unified_identifier_differs_from_the_constituents_identifiers"] = (
        unified_difference_observed)
    if counts["returned"] == 0:
        return _measured(
            context, capability, got, supported=False, state=O.PROBE_UNMEASURED,
            limitation=("no contact was returned, so no identifier was observed and this "
                        "row's assertion (device-local identifiers that persist, and a "
                        "unified identifier that differs from its constituents') was not "
                        "evaluated"),
            evidence={**evidence, "assertion_evaluated": False})
    missing = []
    if not evidence["persistence_half_evaluated"]:
        missing.append("identifier stability across two runs was not compared "
                       "(pass --contacts-compare FILE and run the command twice)")
    if not evidence["unification_half_evaluated"]:
        missing.append("the unified-vs-individual comparison was not evaluated "
                       "(the installed build's unification toggle: "
                       f"{evidence.get('unify_off_selector_checked')} present="
                       f"{evidence.get('unify_off_selector_present')})")
    duplicates = shape["duplicate_identifier_values_in_one_read"]
    if duplicates:
        missing.append(f"{duplicates} duplicate identifier value(s) appeared in one read")
    supported = (not missing and counts["with_identifier"] == counts["returned"]
                 and unified_difference_observed)
    limitation = None
    if not supported:
        limitation = ("this row's assertion was only partly evaluated: " + "; ".join(missing)
                      if missing else
                      "the unified identifier coincided with an individual identifier, which "
                      "O13 says it should not")
    return _measured(context, capability, got, supported=supported, limitation=limitation,
                     evidence={**evidence, "assertion_evaluated": not missing})


def probe_unified_constituents(context, capability) -> dict:
    adapter = _adapter(context)
    individual = _individual(context)
    document = _document(individual)
    evidence = _base_evidence(context, adapter)
    unified_ids = set(_ids(_document(_enumerate(context))))
    evidence.update({
        "unify_off_selector_checked": document.get("unify_off_selector_checked"),
        "unify_off_selector_present": document.get("unify_off_selector_present"),
        "call_used": document.get("call_used"),
        "keys_resolved": document.get("keys_resolved"),
        "keys_missing_at_runtime": document.get("keys_missing_at_runtime"),
        "individual_distinct_identifiers": len(set(_ids(document))),
        "individual_returned": len(document.get("items") or []),
        "unified_distinct_identifiers": len(unified_ids),
        "identifiers_in_both_reads": len(unified_ids & set(_ids(document))),
        "identifier_values_recorded": False,
        "pack_says_no_such_call_is_documented": (
            "O13's own Mac procedure step 4: the property that turns unification off for a "
            "fetch request is not documented on any page read in the pack, so its exact name "
            "has to come from the installed SDK header, and this row records what the "
            "installed framework actually answered"),
    })
    if not individual.usable and individual.reason not in NEGATIVE_BRANCH_REASONS:
        # This host never ran the read (not macOS, no bridge, no grant): that is a blocked
        # row, not the accepted negative. Reporting "no such call was found" here would be
        # claiming a search that did not happen.
        evidence.update({"assertion_branch": "read_refused",
                         "refusal_reason": individual.reason})
        return _blocked(context, capability, individual, evidence)
    if not individual.usable:
        # The accepted negative: the row's assertion explicitly allows "no such call was
        # found" to be the answer, and a real Mac answered that the call is not there.
        # Record it as the negative branch -- and do not call it supported, because nothing
        # was observed to return constituents.
        evidence["assertion_branch"] = "negative_recorded"
        return _measured(
            context, capability, individual, supported=False, state=O.PROBE_UNMEASURED,
            limitation=("no call in the installer's reachable surface enumerates the "
                        "individual contact records behind a unified contact: "
                        f"{individual.detail or individual.code} ({individual.reason}). The "
                        "row's assertion allows this negative answer, and it is recorded as "
                        "that -- the negative -- and not as a working capability"),
            evidence=evidence)
    individual_ids = set(_ids(document))
    overlap = unified_ids & individual_ids
    returned = len(document.get("items") or [])
    evidence["assertion_branch"] = "individual_records_returned"
    supported = bool(returned and individual_ids and not overlap)
    limitation = None
    if not supported:
        limitation = ("the un-unified fetch answered but "
                      + ("carried no record at all" if not returned else
                         "returned identifiers that also appear in the unified read, so the "
                         "two record kinds were not observed to be distinct"))
    return _measured(context, capability, individual, supported=supported,
                     limitation=limitation, evidence=evidence)


def probe_change_history(context, capability) -> dict:
    adapter = _adapter(context)
    got = _change_history(context)
    document = _document(got)
    evidence = _base_evidence(context, adapter)
    if not got.usable:
        evidence.update({"fetch_selector_checked": document.get("selector_checked")})
        return _blocked(context, capability, got, evidence)
    invalid = _change_history(context, invalid_token=True)
    invalid_document = _document(invalid)
    classes = sorted(document.get("event_counts") or {})
    invalid_classes = sorted(invalid_document.get("event_counts") or {})
    unknown = sorted(set(classes + invalid_classes) - set(DOCUMENTED_EVENT_CLASSES))
    evidence.update({
        "fetch_selector_checked": document.get("selector_checked"),
        "fetch_selector_present": document.get("selector_present"),
        "fetch_succeeded": document.get("fetch_succeeded"),
        "event_classification": document.get("event_classification"),
        "event_counts": document.get("event_counts"),
        "events_in_order_sample": document.get("events_in_order"),
        "documented_event_classes": list(DOCUMENTED_EVENT_CLASSES),
        "undocumented_event_classes_seen": unknown,
        "token_returned": bool(document.get("token_length")),
        "token_length": document.get("token_length"),
        "token_fingerprint": document.get("token_fingerprint"),
        "token_value_recorded": False,
        "token_file": document.get("token_file"),
        "token_file_written": document.get("token_file_written"),
        "token_file_mode": document.get("token_file_mode"),
        "starting_token_present": document.get("starting_token_present"),
        "include_group_changes": document.get("include_group_changes"),
        "should_unify_results": document.get("should_unify_results"),
        "reset_attempt": {
            "trigger": "a deliberately invalid startingToken (the documented trigger)",
            "not_a_genuine_reset": True,
            "note": ("O15's only documented reset trigger is passing a token that is nil, "
                     "invalid or expired, and the pack records no way to invalidate a token "
                     "on demand ('reset' 0, 'force' 0). This is that trigger used "
                     "deliberately, and it must never be reported as a genuine reset"),
            "fetch_usable": invalid.usable,
            "outcome": invalid.code,
            "event_counts": invalid_document.get("event_counts"),
            "events_in_order_sample": invalid_document.get("events_in_order"),
            "drop_event_first": invalid_document.get("drop_event_first"),
            "error": invalid_document.get("error"),
            "detail": invalid.detail,
        },
    })
    halves = {
        "fetch_returned_a_token": bool(document.get("token_length")),
        "reset_sequence_observed": bool(invalid.usable and invalid_document.get(
            "drop_event_first")),
        "documented_event_classes_only": not unknown,
    }
    missing = [name for name, held in halves.items() if not held]
    supported = not missing
    limitation = None
    if missing:
        limitation = ("this row's assertion was only partly evaluated: " + "; ".join(
            {"fetch_returned_a_token": "the fetch returned no currentHistoryToken",
             "reset_sequence_observed":
                 "the drop-everything reset sequence was not observed "
                 f"({invalid.detail or invalid.code})",
             "documented_event_classes_only":
                 "event class(es) outside the CNChangeHistory* set the pack names were seen: "
                 + ", ".join(unknown)}[name] for name in missing))
    return _measured(context, capability, got, supported=supported, limitation=limitation,
                     evidence={**evidence, "assertion_halves": halves})


CONTACTS_PROBES = {
    "contacts_read_authorization": probe_read_authorization,
    "contacts_enumerate_contacts": probe_enumerate_contacts,
    "contacts_identifier_scope": probe_identifier_scope,
    "contacts_unified_constituents": probe_unified_constituents,
    "contacts_change_history": probe_change_history,
}

__all__ = ["CONTACTS_PROBES", "DOCUMENTED_EVENT_CLASSES", "DECLARATION_GAP",
           "probe_read_authorization", "probe_enumerate_contacts",
           "probe_identifier_scope", "probe_unified_constituents",
           "probe_change_history", "AUTHORIZATION_STATUS_NAMES",
           "AUTHORIZATION_WITHOUT_AN_EQUIVALENT"]
