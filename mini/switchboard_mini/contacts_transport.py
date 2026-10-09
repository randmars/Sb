"""Contacts transport: read-only access to macOS Contacts, and its fixture twin.

Why this module exists (Gate 2): the probe pack records five Contacts capability rows
(``contacts_read_authorization``, ``contacts_enumerate_contacts``,
``contacts_identifier_scope``, ``contacts_unified_constituents``,
``contacts_change_history``) and they were all ``origin: documentation`` /
``supported: false`` / ``state: unmeasured``, because the worker shipped no Contacts path
at all. This is that path.

How the read happens, and what is *not* assumed
------------------------------------------------
Contacts is an Objective-C framework, not a scriptable app like Mail, so there is no
AppleScript dictionary to talk to. The Mini worker reaches it through
``/usr/bin/osascript -l JavaScript`` and the bridge that file provides (``ObjC.import``,
``$``, ``Ref``) -- both shipped by macOS, so the worker stays standard-library-only and
needs no compiler and no install step. The helper prints **one** JSON document on stdout,
prefixed with a marker, and every value in it is a fact it observed on that machine.

Two rules this module keeps, both because the alternative would be a lie:

* **``authorization_state`` never asks for permission.** It reads the store's own
  authorization status and stops. Looking up the status does not raise the system consent
  dialog; *fetching* while the status is not authorized can (O13: "Any call to CNContactStore
  blocks the app while asking the user to grant or deny access"). So every read path
  refuses with ``authorization_not_granted`` *before* constructing a fetch, and asking for
  the grant is a separate, explicitly-named operation (``request_access``) that the owner
  runs when he chooses to.
* **No API name is invented.** Every call this module makes is either named by a page in
  the probe pack or by Apple's own documentation for that symbol, and each name's source is
  recorded in :data:`CALL_SOURCES` and in ``mini/CONTACTS_CALL_SOURCES.md``. Where the pack
  explicitly says a name is *not* documented anywhere (the ``CNContactFetchRequest``
  unification toggle, the notes-guarded key symbol), the helper does not guess: it asks the
  installed framework whether the symbol exists (``respondsToSelector``) and reports what it
  found, or it refuses and asks for the name from the SDK header.

The fixture twin (``--fixture-mode``) answers from ``fixtures/contacts/*.json`` and labels
every document ``FIXTURE:``. A fixture is a synthetic recording, never a measurement: the
row it produces is ``supported: false`` and ``values_from_source: false`` by construction.

Nothing here writes to Contacts. The only file this module touches is the change-history
token file (mode 0600), which is the documented place a change-history token lives (O15:
"The token can be persisted between the app launches on the current device").
"""

from __future__ import annotations

import binascii
import json
import os
import sys
from typing import Optional

from . import outcomes as O
from .applescript import ScriptResult, classify
from .version import WORKER_VERSION

NAMESPACE = "contacts"
SCHEMA = "switchboard-mini-contacts-response-1.0"
HELPER = "switchboard-contacts-jxa"
HELPER_VERSION = "1.0"
#: The helper prints exactly one line beginning with this marker; anything else on stdout
#: is osascript chatter and is ignored (and recorded).
RESULT_MARKER = "SWITCHBOARD-CONTACTS-RESPONSE:"

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "contacts")
FIXTURE_SCENARIOS = (
    "authorization_granted",        # granted, contacts present, change history steady
    "authorization_denied",         # denied: every read path refuses, nothing is fetched
    "authorization_not_determined",  # no grant yet: a read must NOT prompt
    "authorization_restricted",     # restricted: the owner cannot grant it here
    "contacts_authorized_user_choice",  # granted, but the open Contacts DB is empty
    "restricted_keys",              # a notes-guarded key symbol refuses, fetch still works
    "change_history_reset",         # a drop event followed by a full add stream
    "change_history_steady",        # token accepted, no events at all
    "bridge_unavailable",           # the JavaScript bridge cannot reach Contacts
)
FIXTURE_LABELS = {
    "contacts_authorized_user_choice": "empty Contacts database",
}

#: Every call this module makes and where its exact name/signature came from. A name with
#: a pack ref was read out of that record; a name marked "Apple docs (this session)" was
#: not in the pack (the pack says so in those cases) and was taken from Apple's own
#: documentation page for that symbol, fetched and recorded in
#: ``mini/CONTACTS_CALL_SOURCES.md``.
CALL_SOURCES: dict = {
    "CNContactStore": ("pack O13 -- https://developer.apple.com/documentation/contacts",
                       "the store class: \"You can fetch contacts using the "
                       "contact store (CNContactStore), which represents the "
                       "user's Contacts database.\""),
    "CNContactStore.authorizationStatusForEntityType:": (
        "Apple docs (this session), not in the pack -- "
        "https://developer.apple.com/documentation/contacts/cncontactstore/"
        "authorizationstatus(for:)",
        "\"Returns the current authorization status to access "
        "the contact data.\" The pack records that no read page names a status query "
        "(O13: 'authoriz' 0, 'status' 0; O16: 'authoriz' 0), so this is the one call in "
        "this module whose name comes from Apple's documentation rather than from a pack "
        "record, and O13's own Mac procedure asks for exactly that: \"take its exact name "
        "and its return values from the installed SDK header\""),
    "CNContactStore.requestAccessForEntityType:completionHandler:": (
        "pack O13 -- https://developer.apple.com/documentation/contacts",
        "the asynchronous request: O13 names requestAccess(for:completionHandler:) as the "
        "documented alternative to the blocking call"),
    "CNEntityTypeContacts": ("Apple docs (this session) -- "
                             "https://developer.apple.com/documentation/contacts/"
                             "cnentitytype",
                             "CNEntityType has one documented case, "
                             "'CNEntityType.contacts -- The user's contacts.'"),
    "CNAuthorizationStatus*": (
        "Apple docs (this session), plus the pack's silence -- "
        "https://developer.apple.com/documentation/contacts/cnauthorizationstatus",
        "the "
        "five documented cases (notDetermined / restricted / denied / authorized / limited) "
        "with Apple's one-line abstract for each. The case *names* are resolved at runtime "
        "from the installed framework (respondsToSelector / constants), never by numeric "
        "assumption"),
    "CNContactFetchRequest": ("pack O13 -- https://developer.apple.com/documentation/contacts",
                              "named in O13's fetch-request topic list (\"Fetch and save "
                              "requests\")"),
    "CNContactFetchRequest.setShouldUnifyResults:": (
        "runtime check only -- NOT documented in the pack -- "
        "https://developer.apple.com/documentation/contacts",
        "O13's own Mac procedure says the property that turns unification off for a fetch "
        "request is undocumented on any page read in this session and that its exact name "
        "must be taken from the installed SDK header. So the helper does not call it blindly: "
        "it asks the installed object with respondsToSelector whether it responds to the "
        "setter and reports the answer. If the installed framework does not respond, the row "
        "says so and names the header to read instead of guessing another spelling"),
    "CNContactStore.enumerateContactsWithFetchRequest:error:usingBlock:": (
        "runtime check only -- NOT documented in the pack -- "
        "https://developer.apple.com/documentation/contacts",
        "the store's fetch-all entry point, undocumented in the pack "
        "-- no page read in the pack names a fetch-all "
        "call (O13 names predicateForContacts(matchingName:), "
        "unifiedContacts(matching:keysToFetch:) and unifiedContact(withIdentifier:"
        "keysToFetch:)). The helper therefore checks respondsToSelector first and refuses "
        "with the exact selector it checked when the installed build does not have it"),
    "CNContactStore.enumeratorForChangeHistoryFetchRequest:error:": (
        "pack O15 -- https://developer.apple.com/documentation/technotes/"
        "tn3149-fetching-change-history-events",
        "\"Call enumeratorForChangeHistoryFetchRequest:error: on an instance of "
        "CNContactStore to execute the change history fetch request.\""),
    "CNChangeHistoryFetchRequest": (
        "pack O15 -- https://developer.apple.com/documentation/technotes/"
        "tn3149-fetching-change-history-events",
        "the request class, with its documented configuration "
        "fields includeGroupChanges (default NO), mutableObjects "
        "(default NO), shouldUnifyResults (default YES) and "
        "startingToken (default nil)"),
    "CNFetchResult.value / currentHistoryToken": (
        "pack O15 -- https://developer.apple.com/documentation/technotes/"
        "tn3149-fetching-change-history-events",
        "\"inspect the currentHistoryToken property of CNFetchResult. This property "
        "provides the history token for the current fetch request.\""),
}

#: The minimal key set. Four of the five are named by O13's Mac procedure step 2; the
#: identifier key is named by O15 ("The request always and primarily returns the
#: CNContactIdentifierKey key").
MINIMAL_KEYS = ("CNContactIdentifierKey", "CNContactGivenNameKey", "CNContactFamilyNameKey",
                "CNContactEmailAddressesKey", "CNContactPhoneNumbersKey")
KEY_SOURCES: dict = {
    "CNContactIdentifierKey": (
        "pack O15 -- https://developer.apple.com/documentation/technotes/"
        "tn3149-fetching-change-history-events",
        "O15's key limit: the identifier key is what a "
        "change-history fetch returns"),
    "CNContactGivenNameKey": ("pack O13 -- https://developer.apple.com/documentation/contacts",
                              "O13 procedure step 2's minimal key list"),
    "CNContactFamilyNameKey": ("pack O13 -- https://developer.apple.com/documentation/contacts",
                               "O13 procedure step 2's minimal key list"),
    "CNContactEmailAddressesKey": (
        "pack O13 -- https://developer.apple.com/documentation/contacts",
        "O13 procedure step 2's minimal key list"),
    "CNContactPhoneNumbersKey": (
        "pack O13 -- https://developer.apple.com/documentation/contacts",
        "O13 procedure step 2's minimal key list"),
}

#: The one thing the pack's silence makes impossible to name. The notes-guarded key's
#: symbol is not on any page read in the pack (``CNContactNoteKey`` 0 occurrences; O13
#: records the entitlement ``com.apple.developer.contacts.notes`` but no key), so the
#: worker only tries a symbol the owner supplies from the installed SDK header.
RESTRICTED_KEY_SYMBOL_ASK = (
    "no notes-guarded key symbol is documented on any page read in the pack, so the worker "
    "will not invent one. Read it from the installed SDK on the Mac: "
    "grep -rn 'NoteKey' \"$(xcrun --show-sdk-path)/System/Library/Frameworks/"
    "Contacts.framework/Headers/CNContact.h\" then pass it with "
    "--contacts-key-symbol NAME")

#: ``CNAuthorizationStatus`` case name -> this product's one permission vocabulary. The
#: names are Apple's (fetched in this session); the values are the shared vocabulary.
AUTHORIZATION_VOCABULARY: dict = {
    "CNAuthorizationStatusAuthorized": O.PERMISSION_GRANTED,
    "CNAuthorizationStatusNotDetermined": O.PERMISSION_NOT_DETERMINED,
    "CNAuthorizationStatusDenied": O.PERMISSION_STATE_DENIED,
    "CNAuthorizationStatusRestricted": O.PERMISSION_RESTRICTED,
}
#: A documented status this product cannot express yet. ``limited`` is a real documented
#: case -- "The app has access to a limited subset of contacts, chosen by the person using
#: the app." -- and it is neither a full grant nor a denial, so the worker refuses to round
#: it to either: the row records the case verbatim and stays unsupported. If a Mac ever
#: reports it, the value is added to the shared vocabulary deliberately, in both
#: ``outcomes.py`` and ``grace/contracts.py``, with the evidence.
AUTHORIZATION_WITHOUT_AN_EQUIVALENT: dict = {
    "CNAuthorizationStatusLimited": "limited access is a documented CNAuthorizationStatus "
                                    "case with no equivalent in this product's permission "
                                    "vocabulary yet",
}
#: The status names the helper checks, in the order Apple documents them.
AUTHORIZATION_STATUS_NAMES = ("CNAuthorizationStatusNotDetermined",
                              "CNAuthorizationStatusRestricted",
                              "CNAuthorizationStatusDenied",
                              "CNAuthorizationStatusAuthorized",
                              "CNAuthorizationStatusLimited")

#: Refusal reason the helper may report -> (typed outcome code, machine-readable reason).
#: The helper reports *facts* (which selector was absent, which status it saw); this table
#: is where the product's vocabulary is applied, so the mapping lives in Python only.
REFUSAL_OUTCOMES: dict = {
    "bridge_unavailable": (O.UNSUPPORTED, "helper_missing"),
    "contacts_class_absent": (O.UNSUPPORTED, "helper_missing"),
    "selector_unavailable": (O.UNSUPPORTED, "selector_not_in_this_sdk"),
    "no_keys_resolved": (O.UNSUPPORTED, "no_documented_key_available"),
    "unification_toggle_absent": (O.UNSUPPORTED, "unify_toggle_not_in_this_sdk"),
    "key_symbol_not_supplied": (O.UNSUPPORTED, "restricted_key_symbol_not_supplied"),
    "key_symbol_absent": (O.UNSUPPORTED, "restricted_key_symbol_not_in_this_sdk"),
    "out_parameter_unavailable": (O.UNSUPPORTED, "script_bridge_out_parameters_unavailable"),
    "change_history_fetch_failed": (O.PERMANENT_ERROR, "change_history_fetch_returned_nil"),
    "token_write_failed": (O.PERMANENT_ERROR, "token_file_not_written"),
    "unknown_operation": (O.UNSUPPORTED, "unknown_helper_operation"),
    "script_error": (O.PERMANENT_ERROR, "unexpected_helper_error"),
}
#: The authorization status we saw -> (typed outcome code, reason) for a *read* path that
#: found the grant missing. A read must never prompt, so it stops here instead.
READ_REFUSALS: dict = {
    "CNAuthorizationStatusDenied": (O.PERMISSION_DENIED, "contacts_access_denied"),
    "CNAuthorizationStatusRestricted": (O.PERMISSION_DENIED, "contacts_access_restricted"),
    "CNAuthorizationStatusNotDetermined": (O.PERMISSION_DENIED,
                                           "contacts_authorization_not_determined"),
}

OPERATIONS = ("authorization_status", "request_access", "enumerate", "restricted_keys",
              "change_history")


def default_token_path() -> str:
    """Where a real run persists the change-history token (O15: it can be persisted)."""
    override = (os.environ.get("SWITCHBOARD_CONTACTS_TOKEN_FILE") or "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".switchboard", "contacts-history-token")


# ------------------------------------------------------------------ the helper ---

#: The JavaScript helper. Standard library only on the Python side; everything ObjC lives
#: in this string, and the only prefix it needs is the injected prologue below. It is
#: written in ES5 on purpose: JXA's JavaScriptCore is older than Node's, and a helper that
#: fails to parse on the Mac would report nothing at all. ``tests/test_mini_contacts.py``
#: executes this text under Node with a stand-in bridge, which proves the script's logic
#: (refusal reasons, the no-prompt guard, the status mapping, the event counting) while
#: proving nothing about the real macOS bridge -- that is what Randy's Mac is for.
JXA_HELPER = r"""
// switchboard-contacts-jxa -- read-only Contacts access for the Switchboard Mini worker.
// Prints ONE JSON document on stdout, prefixed with the marker. Never writes to Contacts.
var REQ = SWITCHBOARD_REQUEST;

function nsString(v) {
  try {
    if (v === null || v === undefined) { return null; }
    if (v.isNil && v.isNil()) { return null; }
    if (typeof v.js === 'string') { return v.js; }
    return String(v);
  } catch (e) { return null; }
}

function errorFields(err) {
  // Returns null when there is no NSError at all (a nil out-parameter, or a JS exception
  // rather than a framework error). Reporting a fabricated {domain: null, code: null,
  // description: null} for "no error" would read as an error object on the row, and the
  // recorded fixture shape for a clean fetch is `"error": null`.
  if (err === null || err === undefined) { return null; }
  try { if (err.isNil && err.isNil()) { return null; } } catch (e) { return null; }
  var out = {domain: null, code: null, description: null};
  try { out.domain = nsString(err.domain); } catch (e) { out.domain_error = String(e); }
  try { out.code = Number(err.code); } catch (e) { out.code_error = String(e); }
  try { out.description = nsString(err.localizedDescription); } catch (e) { out.description_error = String(e); }
  return out;
}

function emit(doc) {
  doc.schema = SWITCHBOARD_SCHEMA;
  doc.helper = SWITCHBOARD_HELPER_NAME;
  doc.helper_version = SWITCHBOARD_HELPER_VERSION;
  doc.operation = REQ.operation;
  console.log(SWITCHBOARD_MARKER + JSON.stringify(doc));
}

function refuse(reason, detail, extra) {
  var doc = {outcome: "refused", source_contacted: false,
             refusal: {reason: reason, detail: String(detail === undefined ? "" : detail)}};
  if (extra) { for (var k in extra) { if (extra.hasOwnProperty(k)) { doc[k] = extra[k]; } } }
  emit(doc);
}

function ok(doc) { doc.outcome = "ok"; doc.source_contacted = true; emit(doc); }

function bridge() {
  try { ObjC.import('Contacts'); }
  catch (e) {
    refuse('bridge_unavailable',
           "ObjC.import('Contacts') threw on this host: " + String(e) +
           ". The Contacts framework cannot be reached from osascript -l JavaScript here.");
    return null;
  }
  if (typeof $.CNContactStore === 'undefined' || $.CNContactStore === null) {
    refuse('contacts_class_absent',
           "the bridge imported Contacts but $.CNContactStore is undefined, so no store " +
           "can be created from this host");
    return null;
  }
  try { return $.CNContactStore.alloc.init; }
  catch (e) { refuse('contacts_class_absent', "CNContactStore.alloc.init threw: " + String(e)); return null; }
}

function entityType() {
  var v = $.CNEntityTypeContacts;
  return {constant_present: (typeof v !== 'undefined'),
          value: (typeof v !== 'undefined' ? v : 0)};
}

function statusDocument(store) {
  var doc = {
    prompt_requested: false,
    selector_checked: 'authorizationStatusForEntityType:',
    selector_present: false,
    constants_resolved: {},
    raw_value: null,
    resolved_name: null
  };
  doc.selector_present = !!store.respondsToSelector(doc.selector_checked);
  if (!doc.selector_present) { return doc; }
  var et = entityType();
  doc.entity_type_constant_present = et.constant_present;
  try {
    doc.raw_value = Number(store.authorizationStatusForEntityType(et.value));
  } catch (e) {
    doc.status_read_error = String(e);
    return doc;
  }
  for (var i = 0; i < SWITCHBOARD_STATUS_NAMES.length; i++) {
    var name = SWITCHBOARD_STATUS_NAMES[i];
    var c = $[name];
    if (typeof c !== 'undefined') {
      doc.constants_resolved[name] = Number(c);
      if (Number(c) === doc.raw_value) { doc.resolved_name = name; }
    }
  }
  return doc;
}

function newErrorRef() {
  try { return Ref(); } catch (e) { return null; }
}

function keyArray(names, doc) {
  var keys = $.NSMutableArray.array;
  for (var i = 0; i < names.length; i++) {
    var k = $[names[i]];
    if (typeof k === 'undefined') { doc.keys_missing_at_runtime.push(names[i]); continue; }
    keys.addObject(k);
    doc.keys_resolved.push(names[i]);
  }
  return keys;
}

function runEnumeration(store, doc, keyNames, limit, unifyOff) {
  var out = {items: [], error: null, raised: null, truncated: false, stop_flag_unavailable: false,
             call_used: null, selector_checked: 'enumerateContactsWithFetchRequest:error:usingBlock:',
             selector_present: false, unify_off_selector_checked: null,
             unify_off_selector_present: null, keys_resolved: [], keys_missing_at_runtime: [],
             key_guard_notes: {}};
  var keys = keyArray(keyNames, out);
  if (out.keys_resolved.length === 0) { return out; }
  var req = null;
  try { req = $.CNContactFetchRequest.alloc.initWithKeysToFetch(keys); }
  catch (e) { out.raised = "CNContactFetchRequest.alloc.initWithKeysToFetch threw: " + String(e); return out; }
  if (unifyOff) {
    out.unify_off_selector_checked = 'setShouldUnifyResults:';
    out.unify_off_selector_present = !!req.respondsToSelector(out.unify_off_selector_checked);
    if (!out.unify_off_selector_present) { return out; }
    try { req.shouldUnifyResults = false; }
    catch (e) { out.raised = "setting shouldUnifyResults=false threw: " + String(e); return out; }
  }
  out.selector_present = !!store.respondsToSelector(out.selector_checked);
  if (!out.selector_present) { return out; }
  out.call_used = out.selector_checked;
  var errorRef = newErrorRef();
  if (errorRef === null) { out.raised = "this bridge provides no out-parameter reference (Ref)"; return out; }
  try {
    store.enumerateContactsWithFetchRequestErrorUsingBlock(req, errorRef, function (contact, stop) {
      if (contact === null || contact === undefined) { return; }
      try { if (contact.isNil && contact.isNil()) { return; } } catch (e) { return; }
      var item = {identifier: null, identifier_length: null, has_name: false,
                  has_email: false, has_phone: false};
      try {
        item.identifier = nsString(contact.identifier);
        if (item.identifier !== null) { item.identifier_length = item.identifier.length; }
      } catch (e) { out.key_guard_notes['identifier'] = "raised: " + String(e); }
      var guard = function (keyName, read) {
        try {
          if (typeof contact.isKeyAvailable === 'function' &&
              !contact.isKeyAvailable($[keyName])) {
            out.key_guard_notes[keyName] = "not fetched";
            return null;
          }
          return read();
        } catch (e) {
          out.key_guard_notes[keyName] = "raised: " + String(e);
          return null;
        }
      };
      var given = guard('CNContactGivenNameKey', function () { return nsString(contact.givenName); });
      var family = guard('CNContactFamilyNameKey', function () { return nsString(contact.familyName); });
      var emails = guard('CNContactEmailAddressesKey', function () { return contact.emailAddresses; });
      var phones = guard('CNContactPhoneNumbersKey', function () { return contact.phoneNumbers; });
      item.has_name = !!((given && given.length) || (family && family.length));
      item.has_email = !!(emails && emails.count && emails.count > 0);
      item.has_phone = !!(phones && phones.count && phones.count > 0);
      // Values stay on the Mac: only presence and counts are reported, never a name,
      // address, phone number or note (O13 procedure step 3: "Record the counts only").
      out.items.push(item);
      if (out.items.length >= limit) {
        try { stop[0] = true; out.truncated = true; }
        catch (e) { out.stop_flag_unavailable = true; }
      }
    });
  } catch (e) {
    out.raised = String(e);
  }
  try { out.error = errorFields(errorRef[0]); } catch (e) { out.error = null; }
  return out;
}

function authorizeForRead(store, doc) {
  var status = statusDocument(store);
  doc.authorization = status;
  if (status.resolved_name === 'CNAuthorizationStatusAuthorized') { return true; }
  refuse('authorization_not_granted',
         "the store reports " + String(status.resolved_name === null
                                       ? ("raw status " + String(status.raw_value) +
                                          " (the installed framework did not resolve it to " +
                                          "a documented case name)")
                                       : status.resolved_name) +
         ", so this read path stops before it constructs a fetch: a fetch while the status " +
         "is not authorized is what raises the system consent dialog (O13: 'Any call to " +
         "CNContactStore blocks the app while asking the user to grant or deny access'), and " +
         "a read-only probe must never raise it. Asking for the grant is a separate command " +
         "(switchboard-mini contacts request-access).",
         doc);
  return false;
}

function authorizationStatus(store) {
  var doc = statusDocument(store);
  if (!doc.selector_present) {
    refuse('selector_unavailable',
           "the store does not respond to " + doc.selector_checked + " on this build, so no " +
           "authorization state could be read without asking for one", doc);
    return;
  }
  if (doc.status_read_error) {
    refuse('script_error', "reading the authorization status threw: " + doc.status_read_error, doc);
    return;
  }
  ok(doc);
}

function requestAccess(store) {
  var doc = {selector_checked: 'requestAccessForEntityType:completionHandler:',
             selector_present: false, prompt_expected: true, prompt_text: null,
             prompt_text_recorded: false, blocked_ms: null, granted: null, error: null,
             completion_handler_called: false,
             prompt_text_note: "the macOS consent dialog's text is not readable by the " +
                               "process it is shown for; the owner records it verbatim " +
                               "(O13 probe step 2)"};
  var before = statusDocument(store);
  doc.status_before = before.resolved_name;
  doc.status_before_raw = before.raw_value;
  doc.selector_present = !!store.respondsToSelector(doc.selector_checked);
  if (!doc.selector_present) {
    refuse('selector_unavailable',
           "the store does not respond to " + doc.selector_checked + " on this build, so " +
           "there is no documented way for this helper to ask for access", doc);
    return;
  }
  var et = entityType();
  var done = false, granted = null, errText = null;
  var started = Date.now();
  try {
    store.requestAccessForEntityTypeCompletionHandler(et.value, function (g, err) {
      granted = !!g;
      if (err !== null && err !== undefined) { errText = errorFields(err); }
      done = true;
    });
  } catch (e) {
    refuse('script_error', "requestAccessForEntityType:completionHandler: threw: " + String(e), doc);
    return;
  }
  var deadline = started + Number(REQ.timeout_ms || 120000);
  while (!done && Date.now() < deadline) {
    try {
      $.NSRunLoop.currentRunLoop.runModeBeforeDate($.NSDefaultRunLoopMode,
                                                  $.NSDate.dateWithTimeIntervalSinceNow(0.25));
    } catch (e) { break; }
  }
  doc.blocked_ms = Date.now() - started;
  doc.granted = granted;
  doc.error = errText;
  doc.completion_handler_called = done;
  var after = statusDocument(store);
  doc.status_after = after.resolved_name;
  doc.status_after_raw = after.raw_value;
  ok(doc);
}

function enumerate(store) {
  var doc = {prompt_requested: false, keys_requested: REQ.keys,
             values_emitted: false, limit: Number(REQ.limit || 25)};
  if (!authorizeForRead(store, doc)) { return; }
  var out = runEnumeration(store, doc, REQ.keys, Number(REQ.limit || 25), !!REQ.unify_off);
  if (out.keys_resolved.length === 0) {
    refuse('no_keys_resolved',
           "none of the documented keys exist on this build (" +
           out.keys_missing_at_runtime.join(', ') + ")", doc);
    return;
  }
  if (out.unify_off_selector_checked && out.unify_off_selector_present === false) {
    for (var k in out) { if (out.hasOwnProperty(k)) { doc[k] = out[k]; } }
    refuse('unification_toggle_absent',
           "the installed framework's " + String(out.unify_off_selector_checked) + " check " +
           "said this build does not respond to the unification toggle, and no page read in " +
           "the pack documents that property (O13 procedure step 4 asks for its exact name " +
           "from the installed SDK header), so individual (un-unified) records were not " +
           "fetched and nothing is claimed about them", doc);
    return;
  }
  if (out.raised !== null) {
    for (var k2 in out) { if (out.hasOwnProperty(k2)) { doc[k2] = out[k2]; } }
    refuse('script_error', out.raised, doc);
    return;
  }
  if (!out.selector_present) {
    for (var k3 in out) { if (out.hasOwnProperty(k3)) { doc[k3] = out[k3]; } }
    refuse('selector_unavailable',
           "the store does not respond to " + out.selector_checked + " on this build, and no " +
           "page read in the pack names a fetch-all call, so this helper will not substitute " +
           "a guess for it", doc);
    return;
  }
  for (var k4 in out) { if (out.hasOwnProperty(k4)) { doc[k4] = out[k4]; } }
  ok(doc);
}

function restrictedKeys(store) {
  var doc = {prompt_requested: false, symbol_supplied: !!REQ.key_symbol,
             symbol: REQ.key_symbol || null, symbol_defined_at_runtime: null,
             plain_fetch_ok: null, guarded_fetch_raised: null, guarded_key_error: null,
             guarded_fetch_key_resolved: null, values_emitted: false,
             symbol_ask: SWITCHBOARD_RESTRICTED_KEY_ASK};
  if (!REQ.key_symbol) {
    refuse('key_symbol_not_supplied', SWITCHBOARD_RESTRICTED_KEY_ASK, doc);
    return;
  }
  if (!authorizeForRead(store, doc)) { return; }
  var sym = $[REQ.key_symbol];
  doc.symbol_defined_at_runtime = (typeof sym !== 'undefined');
  if (!doc.symbol_defined_at_runtime) {
    refuse('key_symbol_absent',
           "the bridge does not define " + REQ.key_symbol + " on this build, so no fetch " +
           "with it was attempted", doc);
    return;
  }
  var plain = runEnumeration(store, doc, SWITCHBOARD_MINIMAL_KEYS, 5, false);
  doc.plain_fetch_ok = (plain.selector_present && plain.raised === null);
  doc.plain_returned = plain.items.length;
  var guarded = runEnumeration(store, doc, SWITCHBOARD_MINIMAL_KEYS.concat([REQ.key_symbol]), 5, false);
  doc.guarded_fetch_key_resolved = guarded.keys_resolved.indexOf(REQ.key_symbol) !== -1;
  doc.guarded_fetch_raised = (guarded.raised !== null);
  doc.guarded_fetch_error = guarded.error;
  doc.guarded_fetch_raised_detail = guarded.raised;
  doc.guarded_returned = guarded.items.length;
  doc.guarded_key_guard_note = guarded.key_guard_notes;
  ok(doc);
}

function changeHistory(store) {
  var doc = {prompt_requested: false, token_file: REQ.token_file || null,
             token_file_requested: !!(REQ.token_file && REQ.token_file.length),
             invalid_token_used: !!REQ.invalid_token, starting_token_present: null,
             include_group_changes: !!REQ.include_group_changes,
             should_unify_results: REQ.should_unify_results === undefined
                                   ? true : !!REQ.should_unify_results,
             mutable_objects: false, fetch_succeeded: false, event_counts: {},
             events_in_order: [], drop_event_first: null, token_length: null,
             token_file_written: false, selector_checked:
             'enumeratorForChangeHistoryFetchRequest:error:',
             selector_present: false, error: null,
             event_classification: "counted by class name; O15's page says deciding an " +
                                   "event's specific class by isKindOfClass-style dispatch " +
                                   "is not recommended, so a compiled helper with a " +
                                   "CNChangeHistoryEventVisitor is the recorded alternative"};
  if (!authorizeForRead(store, doc)) { return; }
  if (typeof $.CNChangeHistoryFetchRequest === 'undefined') {
    refuse('selector_unavailable',
           "the bridge does not define CNChangeHistoryFetchRequest on this build", doc);
    return;
  }
  var req = null;
  try { req = $.CNChangeHistoryFetchRequest.alloc.init; }
  catch (e) { refuse('script_error', "CNChangeHistoryFetchRequest.alloc.init threw: " + String(e), doc); return; }
  try { req.shouldUnifyResults = doc.should_unify_results; }
  catch (e) { doc.should_unify_results_set_error = String(e); }
  try { req.includeGroupChanges = doc.include_group_changes; }
  catch (e) { doc.include_group_changes_set_error = String(e); }
  try { req.mutableObjects = false; }
  catch (e) { doc.mutable_objects_set_error = String(e); }
  if (REQ.invalid_token) {
    // O15's documented trigger, used deliberately and labelled: a token of the correct
    // type whose contents are wrong. This is NOT a genuine reset.
    try {
      var bogus = $.NSString.alloc.initWithUTF8String(SWITCHBOARD_INVALID_TOKEN_BYTES);
      req.startingToken = bogus.dataUsingEncoding($.NSUTF8StringEncoding);
      doc.starting_token_present = (req.startingToken !== null);
      doc.invalid_token_note = "the startingToken handed to the request is the deliberate " +
        "synthetic value " + SWITCHBOARD_INVALID_TOKEN_BYTES + " (correct type, wrong " +
        "contents), so any DropEverything event it produces is an artefact of this trigger " +
        "and is never a genuine reset";
    } catch (e) { doc.invalid_token_set_error = String(e); }
  } else if (REQ.token_file && REQ.token_file.length) {
    try {
      var fileData = $.NSData.dataWithContentsOfFile(REQ.token_file);
      doc.starting_token_present = !!(fileData !== null && fileData !== undefined &&
                                      !(fileData.isNil && fileData.isNil()));
      if (doc.starting_token_present) { req.startingToken = fileData; }
    } catch (e) { doc.token_file_read_error = String(e); doc.starting_token_present = false; }
  }
  doc.selector_present = !!store.respondsToSelector(doc.selector_checked);
  if (!doc.selector_present) {
    refuse('selector_unavailable',
           "the store does not respond to " + doc.selector_checked + " on this build", doc);
    return;
  }
  var errorRef = newErrorRef();
  if (errorRef === null) {
    refuse('out_parameter_unavailable',
           "this bridge provides no out-parameter reference (Ref), which " +
           "enumeratorForChangeHistoryFetchRequest:error: needs", doc);
    return;
  }
  var result = null;
  try { result = store.enumeratorForChangeHistoryFetchRequestError(req, errorRef); }
  catch (e) { doc.error = errorFields(e); doc.fetch_raised = String(e); }
  var nilResult = (result === null || result === undefined);
  if (!nilResult) {
    try { if (result.isNil && result.isNil()) { nilResult = true; } } catch (e) { nilResult = true; }
  }
  if (nilResult) {
    if (doc.error === null) {
      try { doc.error = errorFields(errorRef[0]); }
      catch (e) { doc.error = null; }
    }
    refuse('change_history_fetch_failed',
           "the fetch returned nil, which O15 documents as the failure shape ('If the fetch " +
           "request fails, enumeratorForChangeHistoryFetchRequest:error: returns nil')", doc);
    return;
  }
  try {
    var events = result.value;
    var en = events.objectEnumerator;
    var ev = null;
    while ((ev = en.nextObject) !== null && ev !== undefined) {
      var isNil = false;
      try { isNil = (ev.isNil && ev.isNil()); } catch (e) { isNil = false; }
      if (isNil) { break; }
      var cls = null;
      try { cls = nsString(ev.className); } catch (e) { cls = null; }
      if (cls === null) { cls = "unknown_event_class"; }
      doc.event_counts[cls] = (doc.event_counts[cls] || 0) + 1;
      if (doc.events_in_order.length < 20) { doc.events_in_order.push(cls); }
      ev = null;
    }
  } catch (e) { doc.event_iteration_error = String(e); }
  doc.drop_event_first = (doc.events_in_order.length > 0 &&
                          doc.events_in_order[0] === 'CNChangeHistoryDropEverythingEvent');
  doc.fetch_succeeded = true;
  try {
    var token = result.currentHistoryToken;
    if (token !== null && token !== undefined && !(token.isNil && token.isNil())) {
      doc.token_length = Number(token.length);
      if (REQ.token_file && REQ.token_file.length) {
        doc.token_file_written = !!token.writeToFileAtomically(REQ.token_file, true);
      }
    }
  } catch (e) { doc.token_read_error = String(e); }
  ok(doc);
}

var store = bridge();
if (store !== null) {
  if (REQ.operation === 'authorization_status') { authorizationStatus(store); }
  else if (REQ.operation === 'request_access') { requestAccess(store); }
  else if (REQ.operation === 'enumerate') { enumerate(store); }
  else if (REQ.operation === 'restricted_keys') { restrictedKeys(store); }
  else if (REQ.operation === 'change_history') { changeHistory(store); }
  else { refuse('unknown_operation', "unknown operation " + String(REQ.operation)); }
}
"""

#: A deliberately wrong token body for the documented reset trigger. Synthetic and
#: obviously so: it is never a token from any machine.
INVALID_TOKEN_BYTES = "switchboard-invalid-token-not-a-real-token"


def build_script(request: dict) -> str:
    """The exact text handed to ``osascript -l JavaScript``.

    The prologue is built with ``json.dumps`` so a value can never break out of its
    literal, and the constants are injected rather than duplicated in the helper (one
    vocabulary, one marker, one key list).
    """
    prologue = "\n".join([
        "var SWITCHBOARD_SCHEMA = " + json.dumps(SCHEMA) + ";",
        "var SWITCHBOARD_HELPER_NAME = " + json.dumps(HELPER) + ";",
        "var SWITCHBOARD_HELPER_VERSION = " + json.dumps(HELPER_VERSION) + ";",
        "var SWITCHBOARD_MARKER = " + json.dumps(RESULT_MARKER) + ";",
        "var SWITCHBOARD_STATUS_NAMES = " + json.dumps(list(AUTHORIZATION_STATUS_NAMES)) + ";",
        "var SWITCHBOARD_MINIMAL_KEYS = " + json.dumps(list(MINIMAL_KEYS)) + ";",
        "var SWITCHBOARD_RESTRICTED_KEY_ASK = " + json.dumps(RESTRICTED_KEY_SYMBOL_ASK) + ";",
        "var SWITCHBOARD_INVALID_TOKEN_BYTES = " + json.dumps(INVALID_TOKEN_BYTES) + ";",
        "var SWITCHBOARD_REQUEST = " + json.dumps(request, sort_keys=True) + ";",
        "",
    ])
    return prologue + JXA_HELPER


def extract_response(stdout: str) -> tuple:
    """``(document, other_stdout_lines)`` from the helper's stdout.

    The helper prints one marked line. Anything else is recorded rather than dropped: a
    build that prints a deprecation notice on every run would otherwise look like a clean
    run, and a stray marker line would look like a result.
    """
    document: Optional[dict] = None
    other: list = []
    malformed: list = []
    for line in (stdout or "").splitlines():
        if line.startswith(RESULT_MARKER):
            if document is not None:
                malformed.append("a second marked line was printed")
                continue
            try:
                document = json.loads(line[len(RESULT_MARKER):])
            except ValueError as exc:
                malformed.append(f"the marked line is not JSON: {exc}")
            continue
        if line.strip():
            other.append(line.strip()[:200])
    return document, other, malformed


def permission_state_for(document: Optional[dict]) -> Optional[str]:
    """The shared permission vocabulary for an authorization-status document, or None.

    ``None`` means "this document does not answer with a state this product has a value
    for": the caller must then report ``not_determined`` and say why, never guess.
    """
    name = (document or {}).get("resolved_name")
    return AUTHORIZATION_VOCABULARY.get(name) if name else None


def status_without_an_equivalent(document: Optional[dict]) -> Optional[str]:
    name = (document or {}).get("resolved_name")
    return AUTHORIZATION_WITHOUT_AN_EQUIVALENT.get(name) if name else None


def token_evidence(token_bytes: Optional[bytes]) -> dict:
    """Length and fingerprint of a change-history token. Never the value itself."""
    if not token_bytes:
        return {"token_length": None, "token_fingerprint": None, "token_value_recorded": False}
    return {"token_length": len(token_bytes),
            "token_fingerprint": O.fingerprint(binascii.hexlify(token_bytes).decode("ascii")),
            "token_value_recorded": False}


def compare_identifier_fingerprints(path: Optional[str], current: list, *,
                                    write: bool = True) -> dict:
    """Compare this run's identifier fingerprints with a previous run's (O14 step 2).

    O14 asks for identifier behaviour "across two launches of the helper". The identifier
    *value* is device-local and is never written down, so the comparison is between two
    runs' fingerprint sets, held in a file the owner names. One run cannot answer this, and
    the result says so rather than assuming stability.
    """
    if not path:
        return {"persistence_across_runs": "not evaluated",
                "persistence_unmeasured_reason":
                    "one run cannot show that an identifier survives a relaunch; re-run "
                    "`switchboard-mini contacts enumerate --compare-to <file>` on the Mac "
                    "twice (quitting the worker in between) and the comparison is reported "
                    "(O14 procedure step 2)"}
    previous: Optional[list] = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                previous = [line.strip() for line in handle if line.strip()]
        except OSError as exc:
            return {"persistence_across_runs": "unreadable",
                    "persistence_comparison_file": path,
                    "persistence_error": f"{path}: {exc}"}
    if write:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(current) + "\n")
            os.chmod(path, 0o600)
        except OSError as exc:
            return {"persistence_across_runs": "unwritable",
                    "persistence_comparison_file": path,
                    "persistence_error": f"{path}: {exc}"}
    if previous is None:
        return {"persistence_across_runs": "first run recorded",
                "persistence_comparison_file": path,
                "persistence_unmeasured_reason":
                    "this run wrote the baseline; run the same command again after quitting "
                    "the worker to compare (O14 procedure step 2)"}
    same = len(set(previous) & set(current))
    return {"persistence_across_runs": ("stable" if previous and same == len(previous)
                                        else "changed_or_incomplete"),
            "persistence_comparison_file": path,
            "identical_identifier_fingerprints": same,
            "previous_run_identifiers": len(previous),
            "identifier_values_recorded": False,
            "note": ("identifiers are compared as fingerprints; no identifier value is "
                     "written down (O14: the identifier is device-local and is not recorded "
                     "in the pack)")}


# --------------------------------------------------------------------- fixture ---

def fixture_path(scenario: str) -> str:
    if scenario not in FIXTURE_SCENARIOS:
        raise ValueError(f"unknown Contacts fixture scenario {scenario!r}; known: "
                         f"{', '.join(FIXTURE_SCENARIOS)}")
    return os.path.join(FIXTURE_DIR, f"{scenario}.json")


def load_fixture(scenario: str) -> dict:
    with open(fixture_path(scenario), "r", encoding="utf-8") as handle:
        return json.load(handle)


# ------------------------------------------------------------------ transports --

class ContactsTransport:
    """One method per Contacts read the worker performs. Every one returns an Outcome."""

    adapter = NAMESPACE
    origin = O.REAL
    adapter_is_real = False

    def __init__(self, *, timeout_s: int = 120, token_file: Optional[str] = None,
                 limit: int = 25, key_symbol: Optional[str] = None,
                 unify_off: bool = False, invalid_token: bool = False,
                 include_group_changes: bool = False):
        self.timeout_s = int(timeout_s)
        self.token_file = token_file if token_file is not None else default_token_path()
        self.limit = max(1, int(limit))
        self.key_symbol = key_symbol
        self.unify_off = bool(unify_off)
        self.invalid_token = bool(invalid_token)
        self.include_group_changes = bool(include_group_changes)

    def request(self, operation: str, **extra) -> dict:
        payload = {"operation": operation, "timeout_ms": max(1, self.timeout_s) * 1000,
                   "limit": self.limit, "keys": list(MINIMAL_KEYS),
                   "unify_off": bool(extra.pop("unify_off", self.unify_off))}
        payload.update(extra)
        return payload

    # -- the operations ----------------------------------------------------
    def authorization_status(self) -> O.Outcome:
        return self._call("authorization_status", self.request("authorization_status"))

    def request_access(self) -> O.Outcome:
        return self._call("request_access", self.request("request_access"))

    def enumerate_contacts(self, *, unify_off: Optional[bool] = None) -> O.Outcome:
        return self._call("enumerate",
                          self.request("enumerate",
                                       unify_off=self.unify_off if unify_off is None
                                       else bool(unify_off)))

    def restricted_keys(self, symbol: Optional[str] = None) -> O.Outcome:
        return self._call("restricted_keys",
                          self.request("restricted_keys",
                                       key_symbol=symbol if symbol is not None
                                       else self.key_symbol))

    def change_history(self, *, invalid_token: Optional[bool] = None) -> O.Outcome:
        return self._call("change_history",
                          self.request("change_history", token_file=self.token_file,
                                       invalid_token=(self.invalid_token if invalid_token is None
                                                      else bool(invalid_token)),
                                       include_group_changes=self.include_group_changes,
                                       should_unify_results=True))

    def _call(self, operation: str, request: dict) -> O.Outcome:      # pragma: no cover
        raise NotImplementedError


class JxaContactsTransport(ContactsTransport):
    """The real thing: ``osascript -l JavaScript`` and the Contacts framework."""

    origin = O.REAL
    adapter_is_real = True

    def __init__(self, *, runner=None, **kw):
        super().__init__(**kw)
        if runner is None:
            from .applescript import OsascriptRunner
            runner = OsascriptRunner(language="JavaScript")
        self.runner = runner

    def _host_supported(self) -> Optional[O.Outcome]:
        if sys.platform != "darwin":
            return O.Outcome.unsupported(
                "the Contacts adapter needs macOS with the Contacts framework; this host is "
                f"platform={sys.platform}. No contact store was read.",
                reason="host_not_macos", adapter=self.adapter,
                data={"platform": sys.platform},
                next_action="run the Mini worker on Randy's Mac (Gate 2)")
        if not self.runner.available():
            return O.Outcome.unsupported(
                "osascript is not available at /usr/bin/osascript; no contact store was read.",
                reason="osascript_unavailable", adapter=self.adapter)
        return None

    def _call(self, operation: str, request: dict) -> O.Outcome:
        blocked = self._host_supported()
        if blocked is not None:
            blocked.adapter_is_real = True
            blocked.source_contacted = False
            return blocked
        script = build_script(request)
        result = self.runner.run(script, script_kind=f"contacts_{operation}",
                                 timeout_s=self.timeout_s)
        return interpret(result, operation=operation, adapter=self.adapter,
                         token_file=self.token_file)


class FixtureContactsTransport(ContactsTransport):
    """The recorded twin. Answers from ``fixtures/contacts/*.json`` and says so."""

    origin = O.FIXTURE
    adapter_is_real = False

    def __init__(self, fixture: dict, *, limit: int = 25, key_symbol: Optional[str] = None,
                 unify_off: bool = False, invalid_token: bool = False,
                 include_group_changes: bool = False, token_file: Optional[str] = None):
        super().__init__(limit=limit, key_symbol=key_symbol, unify_off=unify_off,
                         invalid_token=invalid_token,
                         include_group_changes=include_group_changes, token_file=token_file)
        self.fixture = fixture
        self.scenario = fixture.get("scenario", "unknown")
        self.label = fixture.get("label") or O.fixture_label(
            self.adapter, f"recorded Contacts fixture '{self.scenario}'")

    def _response_key(self, operation: str, request: dict) -> str:
        """Recorded responses may key a variant, so one scenario can cover both branches.

        A fixture that carries only ``enumerate`` answers both the unified and the
        un-unified read; one that also carries ``enumerate_unify_off`` (or
        ``change_history_invalid``) answers them separately, which is what lets a recorded
        scenario exercise the unified-vs-individual and reset branches without a Mac.
        """
        if operation == "enumerate" and request.get("unify_off"):
            return "enumerate_unify_off"
        if operation == "change_history" and request.get("invalid_token"):
            return "change_history_invalid"
        return operation

    def _call(self, operation: str, request: dict) -> O.Outcome:
        document = (self.fixture.get("responses") or {}).get(self._response_key(operation, request))
        if document is None:
            return O.Outcome.unsupported(
                f"the recorded fixture {self.scenario!r} carries no response for {operation!r}",
                reason="fixture_scenario_has_no_response", adapter=self.adapter,
                label=self.label, origin=self.origin)
        document = dict(document)
        document.setdefault("operation", operation)
        document.setdefault("schema", SCHEMA)
        document.setdefault("helper", HELPER)
        document.setdefault("helper_version", HELPER_VERSION)
        outcome = interpret_document(document, operation=operation, adapter=self.adapter,
                                     token_file=self.token_file,
                                     fixture_token_base64=(self.fixture.get("token_base64")
                                                           if operation == "change_history"
                                                           else None))
        outcome.origin = O.FIXTURE
        outcome.adapter_is_real = False
        outcome.label = self.label
        # A recorded twin can never have contacted anything, whatever the fixture says.
        outcome.source_contacted = False
        return outcome


def build_transport(*, fixture_mode: bool = False, fixture_scenario: str = "authorization_granted",
                    timeout_s: int = 120, limit: int = 25, key_symbol: Optional[str] = None,
                    unify_off: bool = False, invalid_token: bool = False,
                    include_group_changes: bool = False,
                    token_file: Optional[str] = None, runner=None) -> ContactsTransport:
    if fixture_mode:
        return FixtureContactsTransport(load_fixture(fixture_scenario), limit=limit,
                                        key_symbol=key_symbol, unify_off=unify_off,
                                        invalid_token=invalid_token,
                                        include_group_changes=include_group_changes,
                                        token_file=token_file)
    return JxaContactsTransport(runner=runner, timeout_s=timeout_s, limit=limit,
                                key_symbol=key_symbol, unify_off=unify_off,
                                invalid_token=invalid_token,
                                include_group_changes=include_group_changes,
                                token_file=token_file)


# ------------------------------------------------------------- interpretation --

def interpret(result: ScriptResult, *, operation: str, adapter: str = NAMESPACE,
              token_file: Optional[str] = None) -> O.Outcome:
    """Turn one real ``osascript`` run into a typed outcome.

    A refusal from *osascript itself* (no osascript, a timeout, an Apple event error) is
    classified exactly as the Mail transport classifies it -- the table is shared -- and a
    refusal from *the helper* (no bridge, an absent selector, no grant) is mapped through
    :data:`REFUSAL_OUTCOMES`. The helper's document is passed through untouched so a row
    can quote what it actually observed.
    """
    document, other, malformed = extract_response(result.stdout)
    if result.returncode != 0 or result.timed_out:
        outcome = classify(result, adapter=adapter, application="Contacts")
        data = dict(outcome.data or {})
        data.update({"helper_document": document, "helper_stdout_lines": other,
                     "malformed_marked_lines": malformed})
        outcome.data = data
        outcome.adapter_is_real = True
        outcome.source_contacted = False
        return outcome
    if document is None:
        outcome = O.Outcome.permanent(
            f"the Contacts helper printed no marked result line for {operation!r}, so this "
            "run produced no answer",
            reason=("unparseable_helper_response" if malformed else "helper_printed_no_result"),
            adapter=adapter,
            data={"helper_stdout_lines": other, "malformed_marked_lines": malformed,
                  "returncode": result.returncode, "stderr": (result.stderr or "")[:1000]},
            duration_ms=result.duration_ms,
            next_action="treat this as a worker defect (record the stdout/stderr), not as a "
                        "state of Contacts")
        outcome.adapter_is_real = True
        outcome.source_contacted = False
        return outcome
    outcome = interpret_document(document, operation=operation, adapter=adapter,
                                 token_file=token_file)
    outcome.adapter_is_real = True
    outcome.source_contacted = bool(outcome.usable)
    outcome.duration_ms = result.duration_ms
    if other or malformed:
        data = dict(outcome.data or {})
        data.update({"helper_stdout_lines": other, "malformed_marked_lines": malformed})
        outcome.data = data
    return outcome


def interpret_document(document: dict, *, operation: str, adapter: str = NAMESPACE,
                       token_file: Optional[str] = None,
                       fixture_token_base64: Optional[str] = None) -> O.Outcome:
    """The one place a helper document becomes an outcome -- real and fixture alike."""
    if document.get("outcome") == "refused":
        refusal = document.get("refusal") or {}
        reason = refusal.get("reason") or "unrecognised_helper_refusal"
        if reason == "authorization_not_granted":
            status = (document.get("authorization") or {}).get("resolved_name")
            code, mapped = READ_REFUSALS.get(
                status or "", (O.UNSUPPORTED, "authorization_status_unresolved"))
            if mapped == "authorization_status_unresolved":
                detail = ("the helper refused the read because the authorization status was "
                          "neither a documented case name nor 'authorized' ("
                          + (refusal.get("detail") or "") + ")")
            else:
                detail = refusal.get("detail") or ""
            next_action = ("run `switchboard-mini contacts request-access` if you want to "
                           "grant Contacts access to this helper, then re-run the probe"
                           if code == O.PERMISSION_DENIED else
                           "record the raw status value from the document above on the Gate 2 "
                           "row; it is not a state this product can express yet")
            return O.Outcome(code, detail=detail, reason=mapped, adapter=adapter,
                             data=document, next_action=next_action)
        code, mapped = REFUSAL_OUTCOMES.get(reason, (O.UNSUPPORTED,
                                                     "unrecognised_helper_refusal"))
        if code == O.PERMISSION_DENIED:
            return O.Outcome.permission_denied(refusal.get("detail") or "", reason=mapped,
                                               adapter=adapter, data=document)
        if code == O.PERMANENT_ERROR:
            return O.Outcome.permanent(refusal.get("detail") or "", reason=mapped,
                                       adapter=adapter, data=document)
        next_action = None
        if mapped == "helper_missing":
            next_action = ("this is the typed answer the brief called for: reading Contacts "
                           "from this host needs the ObjC bridge of `osascript -l JavaScript`, "
                           "and this Mac did not provide it. Record the detail verbatim on the "
                           "Gate 2 row; see mini/README.md 'Contacts' for the recorded "
                           "alternatives and do not claim a read")
        elif mapped in ("selector_not_in_this_sdk", "unify_toggle_not_in_this_sdk",
                        "restricted_key_symbol_not_in_this_sdk"):
            next_action = ("read the name from the installed SDK header on the Mac and record "
                           "it verbatim (O13's own Mac procedure asks for exactly this); do "
                           "not invent another spelling")
        return O.Outcome.unsupported(refusal.get("detail") or "", reason=mapped,
                                     adapter=adapter, data=document, next_action=next_action)

    data = dict(document)
    if operation == "change_history":
        data.update(_token_evidence_from_run(document, token_file=token_file,
                                             fixture_token_base64=fixture_token_base64))
        data["token_file_mode"] = _token_file_mode(token_file) if document.get(
            "token_file_written") else None
    if operation == "authorization_status":
        state = permission_state_for(document)
        without = status_without_an_equivalent(document)
        if state is None:
            reason = ("authorization_status_without_an_equivalent" if without
                      else "authorization_status_unresolved")
            detail = (without or
                      ("the installed framework did not resolve the raw status to one of the "
                       f"documented CNAuthorizationStatus case names (raw value "
                       f"{document.get('raw_value')!r})"))
            return O.Outcome.unsupported(detail, reason=reason, adapter=adapter, data=data,
                                         next_action=("record the raw value on the Gate 2 row "
                                                      "and read the case name from the "
                                                      "installed SDK header"))
        data["permission_state"] = state
    if operation == "enumerate" or operation == "restricted_keys":
        data["items"] = _public_items(document)
    return O.Outcome.ok(data, adapter=adapter)


def _public_items(document: dict) -> list:
    """Contacts items with device-local identifiers fingerprinted and values withheld.

    O14 makes the identifier device-local and O13's procedure records counts only, so no
    name, address, phone number or note is carried out of the transport, and the identifier
    is replaced by a fingerprint plus its length and a shape hint.
    """
    items: list = []
    for raw in document.get("items") or []:
        identifier = raw.get("identifier")
        item = {"identifier_fingerprint": O.fingerprint(identifier) if identifier else None,
                "identifier_length": raw.get("identifier_length"),
                "identifier_shape": ("uuid-shaped" if isinstance(identifier, str)
                                     and len(identifier) == 36 and identifier.count("-") == 4
                                     else ("opaque" if identifier else None)),
                "has_name": bool(raw.get("has_name")),
                "has_email": bool(raw.get("has_email")),
                "has_phone": bool(raw.get("has_phone")),
                "device_local_identifier": True}
        items.append(item)
    return items


def _token_evidence_from_run(document: dict, *, token_file: Optional[str],
                             fixture_token_base64: Optional[str]) -> dict:
    """Length and fingerprint of the token, from the file a real run wrote (or the fixture).

    The token value itself is never put in a document: the helper writes it to the token
    file (mode 0600) and reports only its length.
    """
    if fixture_token_base64:
        try:
            return token_evidence(binascii.a2b_base64(fixture_token_base64.encode("ascii")))
        except (binascii.Error, ValueError):
            return {"token_length": None, "token_fingerprint": None,
                    "token_value_recorded": False,
                    "token_error": "the fixture's synthetic token is not valid base64"}
    if document.get("token_file_written") and token_file and os.path.exists(token_file):
        with open(token_file, "rb") as handle:
            return token_evidence(handle.read())
    if document.get("token_length") is not None:
        return {"token_length": document.get("token_length"), "token_fingerprint": None,
                "token_value_recorded": False,
                "token_fingerprint_reason": "the helper reported a length but no readable "
                                            "token file was available to fingerprint (the "
                                            "value itself is never emitted)"}
    return token_evidence(None)


def _token_file_mode(token_file: Optional[str]) -> Optional[str]:
    try:
        return oct(os.stat(token_file).st_mode & 0o777)
    except (OSError, TypeError):
        return None


def harden_token_file(token_file: Optional[str]) -> Optional[str]:
    """Chmod the token file to 0600, and report the mode it now has.

    A change-history token is a handle on the contact database; the file it lives in is
    owner-only. macOS creates it 0644 otherwise, which is more readable than it needs to be.
    """
    if not token_file or not os.path.exists(token_file):
        return None
    try:
        os.chmod(token_file, 0o600)
        return oct(os.stat(token_file).st_mode & 0o777)
    except OSError:
        return None


__all__ = ["NAMESPACE", "SCHEMA", "HELPER", "RESULT_MARKER", "FIXTURE_SCENARIOS",
           "FIXTURE_DIR", "CALL_SOURCES", "KEY_SOURCES", "MINIMAL_KEYS",
           "AUTHORIZATION_VOCABULARY", "AUTHORIZATION_WITHOUT_AN_EQUIVALENT",
           "AUTHORIZATION_STATUS_NAMES", "REFUSAL_OUTCOMES", "READ_REFUSALS",
           "OPERATIONS", "RESTRICTED_KEY_SYMBOL_ASK", "JXA_HELPER", "INVALID_TOKEN_BYTES",
           "WORKER_VERSION", "build_script", "extract_response", "permission_state_for",
           "status_without_an_equivalent", "token_evidence", "interpret",
           "interpret_document", "build_transport", "ContactsTransport",
           "JxaContactsTransport", "FixtureContactsTransport", "load_fixture",
           "fixture_path", "default_token_path", "harden_token_file",
           "compare_identifier_fingerprints"]
