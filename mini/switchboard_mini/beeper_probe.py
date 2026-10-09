"""The Beeper capability rows: what the read adapter actually measured, on this host.

These four handlers are what turns a capability row from a *documentation read* into a
*measurement*. Each one is bound to a row that ``documented_capabilities.py`` records from
the probe pack (``beeper_local_api_reachability`` O05/O11, ``beeper_message_search`` O06,
``beeper_history_depth`` O05, ``beeper_account_contacts`` O12), and each returns exactly
one row per capability key:

* when the adapter **is** in this probe run, the row is a measured row -- its ``origin``
  is the adapter's (``real`` on the Mac, ``fixture`` for a recorded scenario), its state
  and evidence are what the read actually returned, and its ``citations`` are empty
  because a measurement quotes no page. The documentation row it replaces is named in
  ``supersedes``, so the ledger's import can report the supersession instead of holding
  two disagreeing rows for one capability key.
* when the adapter is **not** in the run, ``probe.py`` falls back to the documentation row
  for that key (``_probe_documented``), exactly as before this slice.

``supported: true`` is only ever set when the row itself says
``real_source_connected: true`` -- i.e. the read came from a real Beeper Desktop API, not
from a recorded fixture and not from a stand-in responder. Grace refuses to store any
other kind of supported row, and the worker refuses to build one.

Two of the four assertions cannot be fully evaluated by the adapter alone, and those rows
say so rather than rounding up:

* ``beeper_history_depth`` asserts *a comparison*: the oldest timestamp the API can reach
  beside the oldest visible in the Desktop UI for the same account (O05 step 3). The
  adapter records the API half as a number; the row stays unsupported until the owner's
  half is supplied (``--beeper-ui-oldest-visible`` for the probe command).
* ``beeper_account_contacts`` cannot list accounts -- no page in the pack names an
  accounts endpoint -- so it needs an account id, and says that plainly when it has none.
"""

from __future__ import annotations

from typing import Optional

from . import outcomes as O
from .probe import _blocked_row, _row


def _adapter(context):
    adapter = context.for_source("beeper")
    if adapter is None:
        raise RuntimeError("the Beeper probe ran without a Beeper adapter in the run")
    return adapter


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
                 "documentation record in the probe pack; the pack row is superseded by "
                 "this one and must not be stored beside it (grace probe-import reports "
                 "the supersession, and refuses a documentation row that would replace a "
                 "stored real measurement)"),
    }


def _measured(context, capability, outcome, *, supported: bool, limitation: str,
              evidence: dict, state: Optional[str] = None,
              values_from_source: Optional[bool] = None) -> dict:
    adapter = _adapter(context)
    if values_from_source is None and adapter.adapter_is_real:
        # A real adapter's own provenance decides: a stand-in responder is not the source,
        # so a row it produced may not claim a source value. A fixture twin leaves this to
        # probe._row, which follows the state -- and is barred from `supported` anyway
        # because adapter_is_real is false.
        values_from_source = bool(outcome.real_source_connected)
    return _row(context, capability, supported=bool(supported),
                state=(state or outcome.code),
                permission_state=O.PERMISSION_GRANTED,
                limitation=limitation, evidence=evidence,
                origin=adapter.origin, adapter_is_real=adapter.adapter_is_real,
                label=adapter._label(),
                citations=(),
                supersedes=_supersedes(capability),
                values_from_source=(None if values_from_source is None
                                    else bool(values_from_source)))


def _not_a_real_measurement(context, capability, outcome, evidence: dict, *,
                            state: Optional[str] = None, note: str = "") -> dict:
    """A read that answered, but not from a real Beeper install.

    Two shapes, and they are not the same shape:

    * a **recorded fixture** (``adapter_is_real`` false): the row reports the state the
      recorded read produced and ``values_from_source`` follows that state, exactly as the
      Mail fixture rows do -- but ``real_source_connected`` is false by construction and so
      ``supported`` must be false. This is what lets fixture mode exercise the whole path.
    * a **stand-in responder** on a real adapter: the wire layer really ran, so
      ``values_from_source`` is forced false, otherwise the row would be able to claim
      ``real_source_connected`` from a responder that is not Beeper Desktop.
    """
    adapter = _adapter(context)
    stand_in = bool(adapter.adapter_is_real)
    if stand_in:
        limitation = ("the responder was "
                      f"{(outcome.data or {}).get('responder')!r}, not a real Beeper "
                      "Desktop API, so this row's own assertion was not evaluated against "
                      "an install. Nothing here is an observation of Randy's Mac."
                      + (" " + note if note else ""))
    else:
        limitation = ("answered from a recorded fixture, not from a real Beeper install: "
                      "this row's assertion was not evaluated against any Mac."
                      + (" " + note if note else ""))
    return _measured(context, capability, outcome, supported=False,
                     state=state or outcome.code, limitation=limitation,
                     evidence=evidence,
                     values_from_source=(False if stand_in else None))


def _blocked(context, capability, outcome, evidence: Optional[dict] = None) -> dict:
    """A typed refusal, stamped with the Beeper adapter's provenance (not Mail's)."""
    adapter = _adapter(context)
    return _blocked_row(context, capability, outcome, evidence,
                        origin=adapter.origin, adapter_is_real=adapter.adapter_is_real,
                        label=adapter._label(), supersedes=_supersedes(capability))


def _base_evidence(adapter, outcome=None) -> dict:
    data = dict((outcome.data if outcome is not None else {}) or {})
    from .beeper_transport import DEFAULT_BASE_URL_SOURCE
    return {
        "base_url": data.get("base_url") or adapter.base_url,
        "base_url_source": DEFAULT_BASE_URL_SOURCE,
        "token_present": data.get("token_present", None),
        "token_value_recorded": False,
        "responder": data.get("responder"),
        "stand_in": data.get("stand_in"),
    }


# ------------------------------------------------------- reachability (O05, O11) --

def probe_local_api_reachability(context, capability) -> dict:
    adapter = _adapter(context)
    # The host guard lives inside the transport, so the read itself is what answers "can
    # this host reach Beeper at all": a local token check first would report
    # `token_absent` on a host that could never have contacted anything.
    got = adapter.info()
    evidence = _base_evidence(adapter, got)
    evidence.update({
        "http_status": (got.data or {}).get("http_status"),
        "endpoint": (got.data or {}).get("endpoint"),
        "endpoint_ref": (got.data or {}).get("endpoint_ref"),
        "server_metadata_keys": (got.data or {}).get("keys"),
        "document_keys_recorded": (got.data or {}).get("document_keys_recorded"),
        "values_withheld": True,
        "endpoint_urls_present": (got.data or {}).get("endpoint_urls_present"),
        "duration_ms": got.duration_ms,
        "auth_header": "Authorization: Bearer <token> (O11); the token value is never "
                       "recorded",
    })
    if not got.usable:
        return _blocked(context, capability, got, evidence)
    connected = bool(got.real_source_connected)
    keys = (got.data or {}).get("keys") or []
    if connected and keys:
        return _measured(context, capability, got, supported=True, limitation=None,
                         evidence=evidence)
    if not connected:
        return _not_a_real_measurement(
            context, capability, got, evidence,
            note="the assertion for this row is a bearer-authenticated read observed from "
                 "that install.")
    return _measured(
        context, capability, got, supported=False, state=O.PROBE_UNMEASURED,
        limitation=("the API answered 200 but the server metadata document carried no "
                    "readable key at all, so reachability was not confirmed by anything "
                    "this row could observe"),
        evidence=evidence)


# ---------------------------------------------------------- search (O06) ----------

def _ids(items: list) -> list:
    return [i.get("source_message_id") for i in items if i.get("source_message_id")]


def probe_message_search(context, capability) -> dict:
    adapter = _adapter(context)
    from .beeper_transport import SEARCH_LIMIT_MAX
    first = adapter.search(limit=SEARCH_LIMIT_MAX)
    evidence = _base_evidence(adapter, first)
    if not first.usable:
        return _blocked(context, capability, first, evidence)
    data = first.data or {}
    first_ids = _ids(data.get("items") or [])
    cursor = data.get("next_cursor")
    fields_seen = sorted({f for item in (data.get("items") or [])
                          for f in item.get("documented_fields_seen", [])})
    undocumented = sorted({f for item in (data.get("items") or [])
                           for f in item.get("undocumented_fields_seen", [])})
    evidence.update({
        "endpoint": "/v1/messages/search",
        "endpoint_ref": "O06",
        "http_status": None,
        "limit_requested": data.get("limit_requested"),
        "limit_sent": data.get("limit_sent"),
        "limit_max_documented": SEARCH_LIMIT_MAX,
        "limit_clamped": data.get("limit_clamped"),
        "pages_read": data.get("pages_read"),
        "item_counts": [data.get("item_count")],
        "message_id_fingerprints": [O.fingerprint(str(i)) for i in first_ids][:10],
        "chat_count": data.get("chat_count"),
        "has_more": data.get("has_more"),
        "cursor_offered": bool(cursor),
        "documented_fields_seen": fields_seen,
        "undocumented_fields_seen": undocumented,
        "exclude_low_priority_sent": data.get("exclude_low_priority_sent"),
        "oldest_observed": data.get("oldest_observed"),
        "newest_observed": data.get("newest_observed"),
        "coverage_state": (data.get("coverage") or {}).get("coverage_state"),
        "message_text_recorded": False,
        "ids_are_fingerprinted": True,
    })
    connected = bool(first.real_source_connected)
    if not connected:
        return _not_a_real_measurement(
            context, capability, first, evidence,
            note="the assertion for this row is a real bounded page that resumes with the "
                 "opaque cursor the source returned.")
    if not cursor:
        return _measured(
            context, capability, first, supported=False, state=O.PROBE_UNMEASURED,
            limitation=("the first page offered no cursor (hasMore false or no cursor in "
                        "the response), so resumption was not exercised and this row's "
                        "assertion — a bounded page with a resumable, opaque cursor — was "
                        "not evaluated"),
            evidence=evidence)
    second = adapter.search(cursor=cursor, limit=SEARCH_LIMIT_MAX)
    if not second.usable:
        evidence.update({"resume_outcome": second.code, "resume_detail": second.detail})
        return _measured(context, capability, second, supported=False,
                         state=second.code,
                         limitation=("the cursor was offered but resuming it returned "
                                     f"{second.code}: {second.detail} — the assertion (a "
                                     "cursor resumes the next page) did not hold"),
                         evidence=evidence)
    second_data = second.data or {}
    second_ids = _ids(second_data.get("items") or [])
    overlap = sorted(set(first_ids) & set(second_ids))
    evidence.update({
        "pages_read": 2,
        "item_counts": [data.get("item_count"), second_data.get("item_count")],
        "resumed_page_cursor_used": True,
        "resumed_page_item_count": second_data.get("item_count"),
        "overlap_id_fingerprints": [O.fingerprint(str(i)) for i in overlap],
        "resumed_page_documented_fields_seen": sorted({
            f for item in (second_data.get("items") or [])
            for f in item.get("documented_fields_seen", [])}),
    })
    resumed = bool(second_ids)
    supported = resumed and not overlap and bool(fields_seen)
    limitation = None
    if not supported:
        limitation = ("resumption was exercised and "
                      + ("the resumed page carried no messages" if not resumed
                         else "the two pages overlap on at least one message id"
                         if overlap else
                         "no documented message field name was seen in the response, so "
                         "the documented shape was not confirmed"))
    return _measured(context, capability, second, supported=supported,
                     limitation=limitation, evidence=evidence)


# ------------------------------------------------------ history depth (O05) -------

def probe_history_depth(context, capability) -> dict:
    adapter = _adapter(context)
    from .beeper_transport import SEARCH_LIMIT_MAX
    sweep = adapter.search(limit=SEARCH_LIMIT_MAX, sweep=True)
    evidence = _base_evidence(adapter, sweep)
    if not sweep.usable:
        return _blocked(context, capability, sweep, evidence)
    data = sweep.data or {}
    coverage = data.get("coverage") or {}
    ui_oldest = context.beeper_ui_oldest_visible()
    api_oldest = data.get("oldest_observed") if coverage.get("reached_end_of_query") else None
    evidence.update({
        "pages_read": data.get("pages_read"),
        "sweep_bounded_at_pages": coverage.get("sweep_bounded_at_pages"),
        "api_oldest_observed": data.get("oldest_observed"),
        "api_newest_observed": data.get("newest_observed"),
        "api_oldest_reachable": api_oldest,
        "reached_end_of_query": coverage.get("reached_end_of_query"),
        "coverage_state": coverage.get("coverage_state"),
        "gap_reason": coverage.get("gap_reason"),
        "ui_oldest_visible": ui_oldest,
        "ui_oldest_visible_supplied_by": ("the owner (--beeper-ui-oldest-visible)"
                                          if ui_oldest else None),
        "comparison_made": bool(api_oldest and ui_oldest),
        "o05_limitation": ("O05: Message history might be limited; only recent messages "
                           "might be available. O05 states no history window per network "
                           "and no way to detect truncation."),
        "on_device_vs_cloud": ("not recorded by this row: O05 recommends On-Device "
                               "Connections over Beeper Cloud, and which one an account "
                               "uses is a value only the Desktop UI shows"),
        "ids_are_fingerprinted": True,
    })
    connected = bool(sweep.real_source_connected)
    if not connected:
        return _not_a_real_measurement(
            context, capability, sweep, evidence, state=O.PARTIAL,
            note="no history number here is a number from Randy's install.")
    if api_oldest and ui_oldest:
        # Both halves of the documented assertion are present: a number from the API and
        # the owner's number for the same account.
        limitation = (None if api_oldest >= ui_oldest else
                      "the API's oldest reachable timestamp is older than the oldest "
                      "message visible in the Desktop UI, which needs the owner's "
                      "explanation before it is treated as a gap")
        return _measured(context, capability, sweep, supported=True,
                         limitation=limitation, evidence=evidence)
    limitation = ("the API half of this assertion is recorded as a number "
                  f"({coverage.get('coverage_state')}), but the second half — the oldest "
                  "message visible in Beeper Desktop's UI for the same account — is the "
                  "owner's half and is not in this row, so the comparison the row asserts "
                  "was not evaluated. Pass it with --beeper-ui-oldest-visible to complete "
                  "this row (O05 step 3).")
    return _measured(context, capability, sweep, supported=False, state=O.PARTIAL,
                     limitation=limitation, evidence=evidence)


# --------------------------------------------------- account contacts (O12) ------

def probe_account_contacts(context, capability) -> dict:
    adapter = _adapter(context)
    account_id = context.beeper_account_id()
    gate = adapter.host_gate()
    if gate is not None:
        # Nothing can be requested from this host at all, so say that rather than naming a
        # missing account id a reader could mistake for the only obstacle.
        return _blocked(context, capability, gate,
                        {**_base_evidence(adapter, gate),
                         "account_id_supplied": bool(account_id),
                         "endpoint": "/v1/accounts/{accountID}/contacts/list",
                         "endpoint_ref": "O12"})
    if not account_id:
        outcome = adapter.contacts("")
        evidence = _base_evidence(adapter, outcome)
        evidence.update({"account_id_supplied": False, "endpoint_ref": "O12",
                         "endpoint": "/v1/accounts/{accountID}/contacts/list"})
        return _blocked(context, capability, outcome, evidence)
    got = adapter.contacts(account_id)
    evidence = _base_evidence(adapter, got)
    if not got.usable:
        evidence.update({"account_id_supplied": True, "account_id_fingerprint":
                         O.fingerprint(account_id)})
        return _blocked(context, capability, got, evidence)
    data = got.data or {}
    items = data.get("items") or []
    presence = {field: sum(1 for item in items if field in (item.get("documented_fields_seen")
                                                            or []))
                for field in ("id", "cannotMessage", "email", "fullName", "imgURL",
                              "isSelf", "phoneNumber", "username")}
    with_identity = [i for i in items
                     if i.get("identifier_fingerprint")
                     and (i.get("full_name") or i.get("username") or i.get("email_masked")
                          or i.get("phone_masked"))]
    evidence.update({
        "account_id_supplied": True,
        "account_id_fingerprint": O.fingerprint(account_id),
        "endpoint": "/v1/accounts/{accountID}/contacts/list",
        "endpoint_ref": "O12",
        "item_count": data.get("item_count"),
        "items_without_a_usable_identity_field":
            data.get("items_without_a_usable_identity_field"),
        "field_presence_counts": presence,
        "has_more": data.get("has_more"),
        "limit_requested": data.get("limit_requested"),
        "limit_sent": data.get("limit_sent"),
        "limit_clamped": data.get("limit_clamped"),
        "addresses_masked": True,
        "image_urls_withheld": True,
        "contact_identifier_fingerprints": [i["identifier_fingerprint"] for i in items][:10],
        "identifier_is_device_local": ("O14: a Contacts/Beeper identifier is device-local, "
                                       "so a later identity link keeps the device beside "
                                       "the identifier"),
    })
    connected = bool(got.real_source_connected)
    supported = connected and bool(items) and len(with_identity) == len(items)
    limitation = None
    if not connected:
        return _not_a_real_measurement(
            context, capability, got, evidence,
            note="the assertion for this row is merged contacts for one account observed "
                 "on that install.")
    if not items:
        limitation = ("the call answered with no contacts for this account, so the "
                      "documented identity fields were not observed (O12 scopes merging "
                      "per account; an empty list is a result, not a defect)")
    elif not supported:
        limitation = ("at least one returned contact carried no usable identity field "
                      "(no id, name, username, email or phone), so the documented identity "
                      "shape was not confirmed for every item")
    return _measured(context, capability, got, supported=supported, limitation=limitation,
                     evidence=evidence)


BEEPER_PROBES = {
    "beeper_local_api_reachability": probe_local_api_reachability,
    "beeper_message_search": probe_message_search,
    "beeper_history_depth": probe_history_depth,
    "beeper_account_contacts": probe_account_contacts,
}

__all__ = ["BEEPER_PROBES", "probe_local_api_reachability", "probe_message_search",
           "probe_history_depth", "probe_account_contacts"]
