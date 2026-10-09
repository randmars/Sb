"""The Hermes capability rows: what the read adapter actually measured, on this host.

These eight handlers are what turns a Hermes capability row from a *documentation read*
into a *measurement*. Each is bound to a row ``documented_capabilities.py`` records from
the probe pack (``hermes_capability_discovery`` O17, ``hermes_run_submission_and_status``
O17, ``hermes_run_progress_events`` O17, ``hermes_run_stop`` O17,
``hermes_session_continuity`` O17+O18, ``hermes_execution_modes`` O19,
``hermes_approval_modes`` O20, ``hermes_credential_resolution`` O21+O22), and each returns
exactly one row per capability key:

* when the adapter **is** in this probe run, the row is a measured row -- its ``origin`` is
  the adapter's (``real`` on the Mac, ``fixture`` for a recorded scenario), its state and
  evidence are what the reads actually returned, and its ``citations`` are empty because a
  measurement quotes no page. The documentation row it replaces is named in
  ``supersedes``.
* when the adapter is **not** in the run, ``probe.py`` falls back to the documentation row
  for that key (``_probe_documented``).

``supported: true`` is only ever set when the row itself says ``real_source_connected:
true`` -- i.e. the read came from a real Hermes gateway, not from a recorded fixture and
not from a stand-in responder. Grace refuses to store any other kind of supported row, and
the worker refuses to build one.

Four of the eight assertions cannot be fully evaluated by the adapter alone, and those rows
say so rather than rounding up:

* ``hermes_run_submission_and_status`` and ``hermes_run_progress_events`` need a run id the
  owner named, or the explicit ``--submit-test-run`` consent. Without either, the row is a
  typed refusal naming what is missing.
* ``hermes_session_continuity``'s assertion has two halves: the session reads' shape (this
  adapter measures it) and *a resumed run echoing its ``session_id`` plus
  ``X-Hermes-Session-Key`` being accepted* (that needs a run carrying session history, so
  it is the owner's half). Half a measurement is reported as half.
* ``hermes_execution_modes``' tool/toolset-names half comes from ``GET /v1/toolsets``; the
  ``terminal.backend`` half is a value only the owner can read out of his profile, so the
  row stays unsupported until that half arrives.
* ``hermes_approval_modes`` can observe the advertised ``run_approval`` feature flag, but
  the documented trigger is a *dangerous-class command* and O20's own safe example is
  ``bash -c 'echo probe'`` -- which this worker will not submit, because it creates no
  general run. The human-decision half is therefore the owner's observation, and the row
  says exactly that.

``hermes_credential_resolution`` is **not measurable by this adapter at all**, and its own
limitation says why: a 200 on an authenticated read proves only that ``API_SERVER_KEY`` is
valid, never anything about ``op://`` resolution, which O21 documents as fail-open.
"""

from __future__ import annotations

from typing import Optional

from . import outcomes as O
from .probe import _blocked_row, _row

#: O19's toolset names, as written on that page, and O17's documented toolset shape keys.
DOCUMENTED_TOOLSET_KEYS = ("name", "label", "description", "enabled", "configured", "tools")
#: O18's stored session field list, as written on that page.
DOCUMENTED_SESSION_FIELDS = ("Session ID", "Session title", "Parent session ID",
                             "started_at", "ended_at", "source platform", "user ID")
#: O17's documented run document fields.
DOCUMENTED_RUN_FIELDS = ("run_id", "status", "session_id", "model", "output", "usage",
                         "runtime")
#: O20's own harmless example of a command that trips the approval layer. Named here so the
#: row can hand the owner the exact command; never submitted by this worker.
OWNER_APPROVAL_TRIGGER_COMMAND = "bash -c 'echo probe'"
#: The closed set of owner observations for the approval half. Free text would let a row
#: claim support from a sentence; a closed set cannot.
APPROVAL_OBSERVATIONS = ("waiting_for_approval", "instant_deny", "not_reproducible")
#: The closed set of terminal event names for a run (O17).
TERMINAL_EVENT_NAMES = ("run.completed", "run.failed", "run.cancelled", "run.interrupted")


def _adapter(context):
    adapter = context.for_source("hermes")
    if adapter is None:
        raise RuntimeError("the Hermes probe ran without a Hermes adapter in the run")
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
              state_reason: Optional[str] = None,
              values_from_source: Optional[bool] = None,
              observed_version: Optional[str] = None,
              observed_version_reason: Optional[str] = None) -> dict:
    adapter = _adapter(context)
    if values_from_source is None and adapter.adapter_is_real:
        values_from_source = bool(outcome.real_source_connected)
    # A row state stays inside the shared vocabulary Grace validates
    # (``outcomes.ADAPTER_OUTCOMES``); the *named reason* is what makes it specific. The
    # reason and its smallest next action travel in the evidence, so a reader sees which
    # of the documented sub-states was reached without a state Grace would refuse.
    if state_reason:
        evidence = {**evidence,
                    "state_reason": state_reason,
                    "state_reason_meaning": reason_entry(state_reason)["meaning"],
                    "state_reason_next_action": reason_sentence(state_reason)}
    if limitation is None:
        limitation = (
            "measured on this host: the documented assertion for this capability was "
            "evaluated against the response this row's read returned and held"
            if supported else
            "measured on this host, but the documented assertion for this capability was "
            "not evaluated: see state_reason")
    return _row(context, capability, supported=bool(supported),
                state=(state or outcome.code),
                permission_state=O.PERMISSION_GRANTED,
                limitation=limitation, evidence=evidence,
                origin=adapter.origin, adapter_is_real=adapter.adapter_is_real,
                label=adapter._label(),
                citations=(),
                supersedes=_supersedes(capability),
                values_from_source=(None if values_from_source is None
                                    else bool(values_from_source)),
                observed_version=observed_version,
                observed_version_reason=observed_version_reason)


def reason_entry(reason: str) -> dict:
    from .hermes_transport import TYPED_STATES
    return TYPED_STATES.get(reason, {"meaning": "unnamed state"})


def reason_sentence(reason: str) -> str:
    from .hermes_transport import reason_sentence as sentence
    return sentence(reason)


def _not_a_real_measurement(context, capability, outcome, evidence: dict, *,
                            state: Optional[str] = None, note: str = "") -> dict:
    """A read that answered, but not from a real Hermes gateway."""
    adapter = _adapter(context)
    stand_in = bool(adapter.adapter_is_real)
    if stand_in:
        limitation = ("the responder was "
                      f"{(outcome.data or {}).get('responder')!r}, not a real Hermes "
                      "gateway, so this row's own assertion was not evaluated against an "
                      "install. Nothing here is an observation of Randy's Hermes."
                      + (" " + note if note else ""))
    else:
        limitation = ("answered from a recorded fixture, not from a real Hermes gateway: "
                      "this row's assertion was not evaluated against any install."
                      + (" " + note if note else ""))
    return _measured(context, capability, outcome, supported=False,
                     state=state or outcome.code, limitation=limitation,
                     evidence=evidence,
                     values_from_source=(False if stand_in else None),
                     observed_version=_version_for(context),
                     observed_version_reason=_version_reason(context))


def _blocked(context, capability, outcome, evidence: Optional[dict] = None) -> dict:
    """A typed refusal, stamped with the Hermes adapter's provenance (not Mail's).

    The limitation is the *outcome's own* sentence plus its smallest next action, never the
    pack's documentation text: that text says "no Hermes adapter exists in this worker",
    which stopped being true the moment this adapter shipped. A row that quoted it after
    that would be telling a reader something false about the worker.
    """
    adapter = _adapter(context)
    limitation = (outcome.detail or outcome.code)
    if outcome.next_action:
        limitation = f"{limitation} Next: {outcome.next_action}"
    return _blocked_row(context, capability, outcome, evidence,
                        origin=adapter.origin, adapter_is_real=adapter.adapter_is_real,
                        label=adapter._label(), supersedes=_supersedes(capability),
                        limitation=limitation)


def _version_for(context) -> Optional[str]:
    """The version this row may report: the owner's, or nothing at all.

    The documented capability surface exposes ``platform`` and ``model`` and no build
    version (O17 lists no version field), and ``hermes --version`` is a local command this
    adapter cannot run, so ``observed_version`` is ``not_observed`` unless the owner gave
    one -- and then it is recorded as owner-supplied, not as something this row observed.
    """
    return context.hermes_version() or O.VERSION_NOT_OBSERVED


def _version_reason(context) -> str:
    from .hermes_adapter import NO_BUILD_VERSION_REASON
    if context.hermes_version():
        return ("owner-supplied: the owner passed --hermes-version. This row did not "
                "observe it itself; no documented Hermes surface exposes a build version.")
    return NO_BUILD_VERSION_REASON


def _base_evidence(adapter, outcome=None) -> dict:
    from .hermes_transport import DEFAULT_BASE_URL_SOURCE
    data = dict((outcome.data if outcome is not None else {}) or {})
    return {
        "base_url": data.get("base_url") or adapter.base_url,
        "base_url_source": DEFAULT_BASE_URL_SOURCE,
        "profile": data.get("profile") or adapter.profile,
        "token_present": data.get("token_present", None),
        "token_source": data.get("token_source"),
        "token_value_recorded": False,
        "never_read_paths": data.get("never_read_paths"),
        "responder": data.get("responder"),
        "stand_in": data.get("stand_in"),
        "identifier_separation": (
            "O17/O18 keep three identifiers apart and so does this row: the transcript "
            "session_id, the memory-scope header X-Hermes-Session-Key, and the gateway "
            "routing key agent:main:<platform>:..."),
    }


def _document(outcome) -> object:
    return (outcome.data or {}).get("document") if isinstance(outcome.data, dict) else None


def _keys(document) -> list:
    return sorted(document) if isinstance(document, dict) else []


def _names_in(items, key="name") -> list:
    names = []
    for item in items or []:
        if isinstance(item, dict) and isinstance(item.get(key), str):
            names.append(item[key])
        elif isinstance(item, str):
            names.append(item)
    return names


# ------------------------------------------------- discovery (O17) ----------------

def probe_capability_discovery(context, capability) -> dict:
    adapter = _adapter(context)
    got = adapter.capabilities()
    evidence = _base_evidence(adapter, got)
    if not got.usable:
        return _blocked(context, capability, got, evidence)
    document = _document(got)
    features = document.get("features") if isinstance(document, dict) else None
    feature_names = sorted(features) if isinstance(features, dict) else []
    enabled = sorted(name for name, value in (features or {}).items() if value is True) \
        if isinstance(features, dict) else []
    auth = document.get("auth") if isinstance(document, dict) else None
    evidence.update({
        "endpoint": "/v1/capabilities",
        "endpoint_ref": "O17",
        "http_status": (got.data or {}).get("http_status"),
        "document_keys": _keys(document),
        "object_value": document.get("object") if isinstance(document, dict) else None,
        "platform_present": bool(isinstance(document, dict) and document.get("platform")),
        "model_present": bool(isinstance(document, dict) and document.get("model")),
        "auth_type": auth.get("type") if isinstance(auth, dict) else None,
        "auth_required": auth.get("required") if isinstance(auth, dict) else None,
        "feature_names": feature_names,
        "feature_names_enabled_true": enabled,
        "feature_count": len(feature_names),
        "features_is_a_map": isinstance(features, dict),
        "run_approval_advertised": (features.get("run_approval")
                                    if isinstance(features, dict) else None),
        "session_key_header_advertised": (features.get("session_key_header")
                                          if isinstance(features, dict) else None),
        "documentation_note": (
            "O17: the features map is an *example*; the page's own instruction is to call "
            "this endpoint on the installed version rather than assume the list, and an "
            "absent key is not proof that the build lacks the capability"),
        "values_withheld": True,
    })
    # The tool and skill surfaces are part of this row's documented assertion (O17 names
    # /v1/toolsets and /v1/skills as the REST discovery of the tool surface).
    toolsets = adapter.toolsets()
    skills = adapter.skills()
    toolset_doc = _document(toolsets)
    skill_doc = _document(skills)
    toolset_items = (toolset_doc.get("toolsets") if isinstance(toolset_doc, dict) else None)
    if toolset_items is None and isinstance(toolset_doc, list):
        toolset_items = toolset_doc
    evidence.update({
        "toolsets_endpoint_answered": bool(toolsets.usable),
        "toolsets_http_status": (toolsets.data or {}).get("http_status"),
        "toolsets_document_keys": _keys(toolset_doc),
        "toolset_count": len(toolset_items or []) if toolset_items is not None else None,
        "toolset_names": sorted(_names_in(toolset_items))[:50],
        "toolset_field_names_seen": sorted({
            key for item in (toolset_items or []) if isinstance(item, dict) for key in item}),
        "skills_endpoint_answered": bool(skills.usable),
        "skills_http_status": (skills.data or {}).get("http_status"),
        "skills_document_keys": _keys(skill_doc),
        "skill_names": sorted(_names_in(
            (skill_doc.get("skills") if isinstance(skill_doc, dict) else None)
            if isinstance(skill_doc, dict) else (skill_doc if isinstance(skill_doc, list)
                                                 else [])))[:50],
        "tool_and_skill_names_are_capability_names_not_credentials": True,
    })
    connected = bool(got.real_source_connected)
    if not connected:
        return _not_a_real_measurement(
            context, capability, got, evidence,
            note="the assertion for this row is the installed build serving "
                 "/v1/capabilities with the documented envelope and a features map.")
    supported = bool(features is not None and (feature_names or enabled))
    limitation = None
    if not supported:
        limitation = ("the endpoint answered but the capability document carried no "
                      "features map (or an empty one), so this row's assertion -- the "
                      "documented envelope with a features map observed on that build -- "
                      "was not evaluated. O17's own instruction is to call this endpoint "
                      "rather than assume the list, so an empty map is a measurement, not "
                      "a pass")
    return _measured(context, capability, got, supported=supported, limitation=limitation,
                     evidence=evidence, observed_version=_version_for(context),
                     observed_version_reason=_version_reason(context))


# ------------------------------------------- run submission + status (O17) --------

def _run_id_of(outcome) -> Optional[str]:
    document = _document(outcome)
    if isinstance(document, dict) and document.get("run_id"):
        return str(document["run_id"])
    return None


def probe_run_submission_and_status(context, capability) -> dict:
    from .hermes_adapter import PROBE_TEST_RUN_INPUT, probe_idempotency_key
    adapter = _adapter(context)
    key = probe_idempotency_key(PROBE_TEST_RUN_INPUT)
    consent = context.hermes_submit_test_run()
    first = adapter.submit_run(PROBE_TEST_RUN_INPUT, idempotency_key=key,
                               consent=consent)
    evidence = _base_evidence(adapter, first)
    evidence.update({
        "endpoint": "/v1/runs",
        "endpoint_ref": "O17",
        "input_text": PROBE_TEST_RUN_INPUT,
        "input_text_is_a_frozen_constant_of_this_worker": True,
        "general_submission_available": False,
        "consent_flag_supplied": bool(consent),
        "idempotency_key_used": key,
        "idempotency_key_is_ours_and_is_not_a_secret": True,
        "max_concurrent_runs_documented_default": 10,
        "documented_run_statuses": list(O_RUN_STATUSES()),
    })
    if not first.usable:
        return _blocked(context, capability, first, evidence)
    first_doc = _document(first) or {}
    first_id = _run_id_of(first)
    status = first_doc.get("status") if isinstance(first_doc, dict) else None
    evidence.update({
        "submission_http_status": (first.data or {}).get("http_status"),
        "submission_document_keys": _keys(first_doc),
        "submission_status_value": status,
        "submission_status_is_documented": status in O_RUN_STATUSES(),
        "run_id_present": bool(first_id),
        "run_id_fingerprint": O.fingerprint(str(first_id)) if first_id else None,
        "run_id_value_withheld": True,
    })
    # O17: "An identical retry returns the original run_id with HTTP 202 and
    # `Idempotency-Replayed: true`". The retry is the identical payload under the SAME key
    # -- never a fresh key, which would create a second run instead of replaying.
    replay = adapter.submit_run(PROBE_TEST_RUN_INPUT, idempotency_key=key, consent=consent)
    replay_doc = _document(replay) or {}
    replay_id = _run_id_of(replay)
    replay_header = (replay.data or {}).get("idempotency_replayed")
    evidence.update({
        "retry_used_the_same_idempotency_key": True,
        "retry_http_status": (replay.data or {}).get("http_status"),
        "retry_idempotency_replayed_header": replay_header,
        "retry_returned_the_same_run_id": bool(first_id and replay_id
                                              and first_id == replay_id),
        "retry_run_id_fingerprint": (O.fingerprint(str(replay_id)) if replay_id else None),
        "retry_document_keys": _keys(replay_doc),
    })
    # Then the status half: poll to a terminal status within our own bounded window.
    sequence: list = []
    terminal = None
    polls = 0
    if first_id:
        budget = context.hermes_settle_seconds()
        step = context.hermes_settle_interval()
        attempts = max(1, int(budget / step)) if step > 0 else 1
        for _ in range(attempts):
            got = adapter.run_status(first_id)
            polls += 1
            if not got.usable:
                evidence.update({"status_poll_failed_with": got.code,
                                 "status_poll_failed_reason": got.reason})
                break
            document = _document(got) or {}
            value = document.get("status") if isinstance(document, dict) else None
            if value and (not sequence or sequence[-1] != value):
                sequence.append(value)
            if value in O_RUN_TERMINAL_STATUSES():
                terminal = value
                break
            if step > 0:
                context.hermes_sleep(step)
    evidence.update({
        "status_object_name": (_document(first) or {}).get("object")
                              if isinstance(_document(first), dict) else None,
        "status_sequence": sequence,
        "status_polls": polls,
        "terminal_status": terminal,
        "terminal_status_observed": terminal is not None,
        "terminal_statuses_documented": list(O_RUN_TERMINAL_STATUSES()),
        "status_retention_note": ("O17: statuses are 'retained briefly after terminal "
                                  "states', so an unobserved terminal is a measurement "
                                  "window, not a failure"),
    })
    if not first.real_source_connected:
        return _not_a_real_measurement(
            context, capability, first, evidence,
            state=O.PARTIAL,
            note="the assertion for this row is that order of observations on that build: "
                 "accepted, replayed under the same key, and settled.")
    checks = {
        "submission_status_is_documented": status in O_RUN_STATUSES(),
        "run_id_present": bool(first_id),
        "replay_behaviour_matched": bool(replay_header is True
                                         and first_id and replay_id
                                         and first_id == replay_id),
        "terminal_status_observed": terminal is not None,
    }
    supported = all(checks.values())
    limitation = None
    if not supported:
        missing = sorted(name for name, held in checks.items() if not held)
        limitation = ("the run was accepted and observed, but these parts of this row's "
                      "assertion did not hold or were not evaluated: "
                      + ", ".join(missing)
                      + ". O17 documents the replay (202 + Idempotency-Replayed: true "
                        "with the original run_id) and 'retained briefly' terminal "
                        "statuses; a build that differs is a finding to record")
    return _measured(context, capability, replay if replay.usable else first,
                     supported=supported, limitation=limitation, evidence=evidence,
                     observed_version=_version_for(context),
                     observed_version_reason=_version_reason(context))


def O_RUN_STATUSES():
    from .hermes_transport import RUN_STATUSES
    return RUN_STATUSES


def O_RUN_TERMINAL_STATUSES():
    from .hermes_transport import RUN_TERMINAL_STATUSES
    return RUN_TERMINAL_STATUSES


# ------------------------------------------------- progress events (O17) ---------

def probe_run_progress_events(context, capability) -> dict:
    adapter = _adapter(context)
    run_id = context.hermes_run_id()
    if not run_id:
        return _blocked(context, capability, _REFUSE_RUN_ID(
            "hermes_run_progress_events"), {
            "endpoint": "/v1/runs/{run_id}/events", "endpoint_ref": "O17",
            "run_id_supplied": False})
    got = adapter.run_events(run_id)
    evidence = _base_evidence(adapter, got)
    evidence.update({
        "endpoint": "/v1/runs/{run_id}/events",
        "endpoint_ref": "O17",
        "run_id_supplied": True,
        "run_id_fingerprint": O.fingerprint(str(run_id)),
        "documented_event_counts": (got.data or {}).get("documented_event_counts"),
        "undocumented_event_counts": (got.data or {}).get("undocumented_event_counts"),
        "event_names_in_order": (got.data or {}).get("event_names_in_order"),
        "event_count": (got.data or {}).get("event_count"),
        "documented_event_names": list(O_DOCUMENTED_EVENTS()),
        "payload_text_recorded": False,
        "preview_dependence_note": (
            "O17 says the gateway passes completion previews through 'forced secret "
            "redaction and then truncated to 500 characters'. That is a promise about "
            "another process, so this row records event names and counts only and no "
            "payload text at all"),
        "buffer_expiry_note": ("O17: unconsumed event buffers expire after five minutes; "
                               "'a run that is still executing remains visible to status "
                               "polling'"),
    })
    if not got.usable:
        return _blocked(context, capability, got, evidence)
    counts = (got.data or {}).get("documented_event_counts") or {}
    undocumented = (got.data or {}).get("undocumented_event_counts") or {}
    total = (got.data or {}).get("event_count") or 0
    terminal = sorted(name for name in TERMINAL_EVENT_NAMES if name in counts)
    evidence.update({"terminal_event_names_seen": terminal})
    if not first_connected(got):
        return _not_a_real_measurement(
            context, capability, got, evidence,
            note="the assertion for this row is the installed build streaming its "
                 "documented progress event names for a run.")
    if total == 0:
        return _measured(context, capability, got, supported=False,
                         state=O.PROBE_UNMEASURED,
                         state_reason="no_terminal_event_in_window",
                         limitation=("the stream carried no event at all within the "
                                     "window, so this row's assertion (the documented "
                                     "progress event names arrive) was not evaluated. O17: "
                                     "unconsumed event buffers expire after five minutes, "
                                     "so an expired buffer and a silent run look the same "
                                     "here and this is unmeasured, not unsupported"),
                         evidence=evidence, values_from_source=False,
                         observed_version=_version_for(context),
                         observed_version_reason=_version_reason(context))
    if not counts:
        return _measured(context, capability, got, supported=False,
                         state=O.PARTIAL,
                         state_reason="event_names_not_documented",
                         limitation=("events arrived but none carried a name in O17's "
                                     "documented vocabulary "
                                     f"({sorted(undocumented)} instead), so the documented "
                                     "event names were not observed on this build"),
                         evidence=evidence, observed_version=_version_for(context),
                         observed_version_reason=_version_reason(context))
    if not terminal:
        return _measured(context, capability, got, supported=False,
                         state=O.PARTIAL,
                         state_reason="no_terminal_event_in_window",
                         limitation=("documented events arrived but the stream carried no "
                                     "terminal event ("
                                     + ", ".join(TERMINAL_EVENT_NAMES)
                                     + ") within this worker's window, so the stream was "
                                       "observed but not to its end; re-read the stream or "
                                       "poll GET /v1/runs/{run_id} for the settled status"),
                         evidence=evidence, observed_version=_version_for(context),
                         observed_version_reason=_version_reason(context))
    return _measured(context, capability, got, supported=True, limitation=None,
                     evidence=evidence, observed_version=_version_for(context),
                     observed_version_reason=_version_reason(context))


def O_DOCUMENTED_EVENTS():
    from .hermes_transport import DOCUMENTED_EVENT_NAMES
    return DOCUMENTED_EVENT_NAMES


# --------------------------------------------------------------- stop (O17) ------

def probe_run_stop(context, capability) -> dict:
    adapter = _adapter(context)
    run_id = context.hermes_run_id()
    if not run_id:
        return _blocked(context, capability, _REFUSE_RUN_ID("hermes_run_stop"), {
            "endpoint": "/v1/runs/{run_id}/stop", "endpoint_ref": "O17",
            "run_id_supplied": False})
    got = adapter.stop(run_id, settle_seconds=context.hermes_settle_seconds(),
                       interval=context.hermes_settle_interval())
    evidence = _base_evidence(adapter, got)
    evidence.update({
        "endpoint": "/v1/runs/{run_id}/stop",
        "endpoint_ref": "O17",
        "run_id_supplied": True,
        "run_id_fingerprint": O.fingerprint(str(run_id)),
        "stop_response_status": (got.data or {}).get("stop_response_status"),
        "status_sequence": (got.data or {}).get("status_sequence"),
        "polls": (got.data or {}).get("polls"),
        "terminal_status": (got.data or {}).get("terminal_status"),
        "settled": (got.data or {}).get("settled"),
        "elapsed_ms": (got.data or {}).get("elapsed_ms"),
        "settle_budget_s": (got.data or {}).get("settle_budget_s"),
        "settle_budget_is_ours": True,
        "stop_is_a_request_note": ("O17: the endpoint returns immediately with "
                                   "{\"status\": \"stopping\"} and 'requesting stop never "
                                   "hides a worker that is still running', so a stop is "
                                   "recorded as a request plus the settling observed"),
        "no_forced_kill_documented": "O17 states no timeout or forced-kill path",
    })
    if got.code == O.PARTIAL and got.reason == "stopping_unsettled":
        # The request was accepted; the settling was not observed. That is a measurement
        # of a partially-evaluated assertion, not a completed stop.
        if first_connected(got):
            return _measured(context, capability, got, supported=False,
                             state=O.PARTIAL, state_reason="stopping_unsettled",
                             limitation=got.detail,
                             evidence=evidence, values_from_source=True,
                             observed_version=_version_for(context),
                             observed_version_reason=_version_reason(context))
        return _not_a_real_measurement(context, capability, got, evidence,
                                       state=O.PARTIAL,
                                       note="the stop was accepted and not observed to "
                                            "settle.")
    if not got.usable:
        return _blocked(context, capability, got, evidence)
    if not first_connected(got):
        return _not_a_real_measurement(
            context, capability, got, evidence,
            note="the assertion for this row is that the installed build accepted a stop "
                 "and the run settled to a terminal status observed by polling.")
    supported = bool((got.data or {}).get("settled"))
    limitation = None if supported else (
        "the stop was accepted but no terminal status was observed, so this row's "
        "assertion (settling to a terminal status) was not evaluated")
    return _measured(context, capability, got, supported=supported, limitation=limitation,
                     evidence=evidence, observed_version=_version_for(context),
                     observed_version_reason=_version_reason(context))


# ---------------------------------------------------- session continuity (O17/O18) --

def probe_session_continuity(context, capability) -> dict:
    adapter = _adapter(context)
    listing = adapter.sessions(limit=context.hermes_session_limit())
    evidence = _base_evidence(adapter, listing)
    if not listing.usable:
        return _blocked(context, capability, listing, evidence)
    document = _document(listing)
    items = None
    if isinstance(document, dict):
        for key in ("sessions", "items", "results"):
            if isinstance(document.get(key), list):
                items = document[key]
                break
    elif isinstance(document, list):
        items = document
    field_names = sorted({key for item in (items or []) if isinstance(item, dict)
                          for key in item})
    evidence.update({
        "endpoint": "/api/sessions",
        "endpoint_ref": "O17",
        "listing_http_status": (listing.data or {}).get("http_status"),
        "listing_document_keys": _keys(document),
        "listing_is_a_list": isinstance(items, list),
        "session_count": len(items) if isinstance(items, list) else None,
        "session_field_names_seen": field_names,
        "documented_session_fields": list(DOCUMENTED_SESSION_FIELDS),
        "documented_session_fields_seen": sorted(
            name for name in DOCUMENTED_SESSION_FIELDS if name in field_names
            or name.lower().replace(" ", "_") in field_names),
        "session_identifiers_withheld": True,
        "documented_params_sent": ["limit"],
        "params_documented_by_o17": ["limit", "offset", "source", "include_children"],
        "three_identifiers_note": (
            "O17's X-Hermes-Session-Key is memory scope; the transcript session id rotates "
            "on /new; O18's gateway routing key (agent:main:<platform>:...) 'Maps session "
            "keys to active session IDs'. None of the three is used here as another."),
        "compression_lineage_documented": (
            "O18: 'Parent session ID (for compression-triggered session splitting)' plus "
            "numbered continuation titles; the row records whether a parent-shaped field "
            "appears on this build"),
    })
    # An owner-named session (if given) is read explicitly; otherwise the first listed
    # session stands in for the read half. No identifier is ever printed.
    session_id = context.hermes_session_id()
    source = "owner (--session-id)" if session_id else None
    if not session_id and isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                for key in ("id", "session_id", "sessionID", "session id"):
                    if isinstance(item.get(key), str) and item[key]:
                        session_id = item[key]
                        source = f"the first session in the listing (field {key!r})"
                        break
            if session_id:
                break
    evidence.update({"session_id_supplied_by": source})
    if not session_id:
        return _measured(context, capability, listing, supported=False,
                         state=O.PARTIAL,
                         state_reason="session_resume_half_unmeasured",
                         limitation=("the session listing answered but carried no "
                                     "identifier-shaped field for any session, so no "
                                     "single session could be read back and the "
                                     "attribution half of this row's assertion was not "
                                     "evaluated"),
                         evidence=evidence, observed_version=_version_for(context),
                         observed_version_reason=_version_reason(context))
    single = adapter.session(session_id)
    evidence.update({
        "session_read_http_status": (single.data or {}).get("http_status"),
        "session_read_answered": bool(single.usable),
        "session_read_document_keys": _keys(_document(single)),
        "session_id_fingerprint": (single.data or {}).get("session_id_fingerprint"),
        "session_id_value_withheld": True,
    })
    messages = adapter.session_messages(session_id, include_compacted=True)
    message_doc = _document(messages)
    message_items = None
    if isinstance(message_doc, dict):
        for key in ("messages", "items", "results"):
            if isinstance(message_doc.get(key), list):
                message_items = message_doc[key]
                break
    elif isinstance(message_doc, list):
        message_items = message_doc
    evidence.update({
        "messages_endpoint_ref": "O17",
        "messages_http_status": (messages.data or {}).get("http_status"),
        "messages_read_answered": bool(messages.usable),
        "messages_document_keys": _keys(message_doc),
        "message_count": len(message_items) if isinstance(message_items, list) else None,
        "messages_params_sent": ["include_compacted"],
        "message_text_recorded": False,
    })
    if not first_connected(listing):
        return _not_a_real_measurement(
            context, capability, listing, evidence, state=O.PARTIAL,
            note="the read half of this row's assertion (session identity and stored "
                 "history) is what was exercised here.")
    # The row's assertion has two halves and only one of them is reachable without
    # creating a second run carrying session history: this worker creates no run but the
    # probe's own frozen one, and it will not attach that run to the owner's session.
    measured_half = bool(single.usable or messages.usable)
    return _measured(
        context, capability, single if single.usable else listing, supported=False,
        state=O.PARTIAL, state_reason="session_resume_half_unmeasured",
        limitation=("measured half: the session listing and read-back shape on this build "
                    f"(listing answered {bool(listing.usable)}, session read "
                    f"{bool(single.usable)}, messages read {bool(messages.usable)}). "
                    "Unmeasured half: O17's own attribution assertion -- 'a resumed run "
                    "keeps its session attribution (the returned session_id is echoed "
                    "unchanged)' and X-Hermes-Session-Key being accepted -- needs a run "
                    "carrying session history. This worker creates no run but its frozen "
                    "probe constant and will not attach that run to the owner's session, "
                    "so that half stays unmeasured until the owner supplies the "
                    "observation. A row that says partial beats one that claims supported "
                    "on half a measurement"),
        evidence={**evidence, "measured_half_reached": measured_half},
        observed_version=_version_for(context), observed_version_reason=_version_reason(context))


# --------------------------------------------------------- execution modes (O19) --

def probe_execution_modes(context, capability) -> dict:
    adapter = _adapter(context)
    toolsets = adapter.toolsets()
    evidence = _base_evidence(adapter, toolsets)
    terminal_backend = context.hermes_terminal_backend()
    document = _document(toolsets)
    items = None
    if isinstance(document, dict):
        for key in ("toolsets", "items", "results"):
            if isinstance(document.get(key), list):
                items = document[key]
                break
    elif isinstance(document, list):
        items = document
    tool_names = []
    enabled_names = []
    configured_names = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("tools"), list):
            tool_names.extend(name for name in item["tools"] if isinstance(name, str))
        if item.get("enabled") is True:
            enabled_names.append(item.get("name"))
        if item.get("configured") is True:
            configured_names.append(item.get("name"))
    evidence.update({
        "endpoint": "/v1/toolsets",
        "endpoint_ref": "O17",
        "http_status": (toolsets.data or {}).get("http_status"),
        "document_keys": _keys(document),
        "toolset_field_names_seen": sorted({
            key for item in (items or []) if isinstance(item, dict) for key in item}),
        "documented_toolset_keys": list(DOCUMENTED_TOOLSET_KEYS),
        "toolset_names": sorted(name for name in _names_in(items) if name)[:50],
        "toolset_count": len(items or []) if items is not None else None,
        "toolset_names_enabled_true": sorted(name for name in enabled_names if name)[:50],
        "toolset_names_configured_true": sorted(name for name in configured_names
                                                if name)[:50],
        "tool_names_seen": sorted(set(tool_names))[:100],
        "tool_name_count": len(set(tool_names)),
        "terminal_backend_owner_supplied": terminal_backend,
        "terminal_backend_supplied_by": ("the owner (--terminal-backend)"
                                         if terminal_backend else None),
        "documented_terminal_backends": ["local", "docker", "ssh", "singularity", "modal",
                                         "daytona", "vercel_sandbox"],
        "computer_action_mechanism": None,
        "computer_action_note": (
            "no documented or observed computer-action mechanism: O19 counts 'gui' 0, "
            "'computer' 0, 'desktop' 0 and 'Accessibility' 0, and O17 has no GUI statement "
            "either. The nearest named surfaces are browser automation "
            "(browser_navigate/browser_snapshot/browser_vision, O19) and O17's optional "
            "browser-extension control, which 'requires the API-server bearer key' and is "
            "disabled by default. This row does not improvise one"),
        "ssh_no_gui_note": (
            "PRD line 251's sentence that SSH execution does not establish GUI-session "
            "access or macOS privacy permissions is our inference, not vendor text: O19 "
            "states nothing about what SSH grants ('macOS' 0, 'privacy' 0), and a Mac test "
            "is allowed to refute it"),
    })
    if not toolsets.usable:
        return _blocked(context, capability, toolsets, evidence)
    if not first_connected(toolsets):
        return _not_a_real_measurement(
            context, capability, toolsets, evidence, state=O.PARTIAL,
            note="the tool/toolset-names half of this row's assertion is what was exercised "
                 "here.")
    names_half = bool(evidence["toolset_names"] or evidence["tool_names_seen"])
    if not terminal_backend:
        return _measured(
            context, capability, toolsets, supported=False, state=O.PARTIAL,
            state_reason="terminal_backend_half_unmeasured",
            limitation=("half of this row's assertion is measured (the tool and toolset "
                        "names on this build"
                        + ("" if names_half else " -- and even that half came back empty")
                        + ") and half is not: `terminal.backend` is a value in the active "
                          "profile's config.yaml that only the owner can read. Pass "
                          "--terminal-backend to complete this row. Unmeasured half means "
                          "unsupported, not a guess"),
            evidence=evidence, observed_version=_version_for(context),
            observed_version_reason=_version_reason(context))
    return _measured(
        context, capability, toolsets, supported=names_half,
        limitation=(None if names_half else
                    "the owner supplied `terminal.backend`, but the toolset read returned "
                    "no tool or toolset names, so the observed half of this row's "
                    "assertion did not hold"),
        evidence=evidence, observed_version=_version_for(context),
        observed_version_reason=_version_reason(context))


# ------------------------------------------------------------- approvals (O20) ---

def probe_approval_modes(context, capability) -> dict:
    adapter = _adapter(context)
    discovered = adapter.capabilities()
    evidence = _base_evidence(adapter, discovered)
    document = _document(discovered)
    features = document.get("features") if isinstance(document, dict) else None
    advertised = features.get("run_approval") if isinstance(features, dict) else None
    observation = context.hermes_approval_observation()
    evidence.update({
        "endpoint": "/v1/capabilities",
        "endpoint_ref": "O17",
        "http_status": (discovered.data or {}).get("http_status"),
        "run_approval_advertised": advertised,
        "run_approval_absence_is_not_proof": (
            "O17 presents the features map as an example and instructs integrators to call "
            "the endpoint on the installed version; a missing run_approval key is therefore "
            "not evidence that the feature is absent"),
        "approval_observation_supplied_by_owner": observation,
        "approval_observation_allowed_values": list(APPROVAL_OBSERVATIONS),
        "worker_submitted_a_command_to_trip_approval": False,
        "why_not": (
            "O20's trigger list is dangerous-class shell commands and its own harmless "
            "example is `bash -c 'echo probe'`. Submitting that through a run would require "
            "a general run submission surface, which this worker refuses (O17: the API "
            "server 'gives full access to hermes-agent's toolset, including terminal "
            "commands'). The command is named here so the owner can run it by hand in an "
            "interactive session and record what happened"),
        "owner_trigger_command": OWNER_APPROVAL_TRIGGER_COMMAND,
        "is_the_send_approval": False,
        "send_approval_note": (
            "O20: Hermes's approval is a runtime safeguard for shell commands, file writes "
            "and MCP trust gates and 'does not substitute for the product's recipient, "
            "data-disclosure or send approval'. This row is that runtime safeguard; "
            "Switchboard's immutable send approval (PRD line 224) is a different object and "
            "is never wired to this endpoint"),
        "documented_defaults": {"mode": "smart", "timeout_s": 300, "cron_mode": "deny",
                                "single_query_mode": "deny", "unattended_mode": "deny"},
        "unattended_api_server_note": (
            "O20: unattended_mode covers an 'api_server' session and 'deny blocks the "
            "command instantly', with the documented exception that a client which can "
            "answer the card via POST /v1/runs/{id}/approval still gets the request"),
    })
    if not discovered.usable:
        return _blocked(context, capability, discovered, evidence)
    if not first_connected(discovered):
        return _not_a_real_measurement(
            context, capability, discovered, evidence, state=O.PARTIAL,
            note="the advertised-flag half of this row's assertion is what was exercised "
                 "here.")
    if observation in ("waiting_for_approval", "instant_deny"):
        return _measured(
            context, capability, discovered, supported=True, limitation=None,
            evidence={**evidence,
                      "assertion_evaluated_by": ("the owner's observation, recorded as "
                                                 "owner-supplied: the advertised flag "
                                                 "alone is not the assertion")},
            observed_version=_version_for(context), observed_version_reason=_version_reason(context))
    return _measured(
        context, capability, discovered, supported=False, state=O.PARTIAL,
        state_reason="approval_observation_missing",
        limitation=("the advertised `run_approval` flag was "
                    + ("observed" if advertised is not None else "absent")
                    + " and the owner supplied no observation "
                      "(obs**not_reproducible** is a valid answer, not a failure), so this "
                      "row's assertion -- whether a pending approval is raised and resolved "
                      "on this build -- was not evaluated. Run `"
                    + OWNER_APPROVAL_TRIGGER_COMMAND + "` in an interactive session, then "
                      "re-run with --approval-observation waiting_for_approval|"
                      "instant_deny|not_reproducible"),
        evidence=evidence, observed_version=_version_for(context),
        observed_version_reason=_version_reason(context))


# -------------------------------------------------- credential resolution (O21/O22)

def probe_credential_resolution(context, capability) -> dict:
    adapter = _adapter(context)
    # A local check only: it contacts nothing and never reads a file.
    status = adapter.token_status()
    evidence = _base_evidence(adapter, status)
    if not status.usable:
        return _blocked(context, capability, status, evidence)
    # One authenticated read, so the row can say what a 200 does and does not prove.
    discovered = adapter.capabilities()
    evidence.update({
        "endpoint": "/v1/capabilities",
        "endpoint_ref": "O17",
        "authenticated_read_http_status": (discovered.data or {}).get("http_status"),
        "authenticated_read_answered": bool(discovered.usable),
        "authenticated_read_proves": ("only that API_SERVER_KEY is present and valid for "
                                      "this gateway"),
        "authenticated_read_does_not_prove": (
            "nothing about op:// resolution. O21 documents that Hermes resolves op:// "
            "references through an installed, authenticated 1Password CLI at process "
            "startup and that a failure is fail-OPEN: 'If op is missing, your session is "
            "locked, or a reference is wrong, Hermes prints a one-line warning and "
            "continues with whatever credentials .env already had - it never blocks "
            "startup.' A 200 on an authenticated API read cannot observe any of that"),
        "not_measurable_by_this_adapter": True,
        "owner_procedure_refs": ["O21 steps 1-4", "O22 steps 1-2 and 4-5"],
        "fail_closed_is_ours": (
            "PRD line 253's requirement that required credentials fail closed on unexpected "
            "fallback or failed resolution is contradicted by the documented behaviour, so "
            "it is ours to build: Grace must verify each required credential after "
            "resolution and refuse to dispatch on a mismatch"),
        "no_credential_value_recorded": True,
    })
    return _measured(
        context, capability, status, supported=False, state=O.PROBE_UNMEASURED,
        state_reason="credential_path_not_measurable_by_this_adapter",
        limitation=("not measurable by this adapter: a 200 on an authenticated API read "
                    "proves only that API_SERVER_KEY is valid and says nothing about op:// "
                    "resolution, which O21 documents as fail-open. The credential path is "
                    "an owner-side measurement (`op whoami`, `hermes secrets onepassword "
                    "status`, and one deliberately broken throwaway mapping) and this row "
                    "will not claim it"),
        evidence=evidence, values_from_source=False,
        observed_version=_version_for(context), observed_version_reason=_version_reason(context))


def first_connected(outcome) -> bool:
    return bool(getattr(outcome, "real_source_connected", False))


def _REFUSE_RUN_ID(capability_name: str):
    """Refuse a run-scoped row that has no run id: the worker cannot invent one."""
    return O.Outcome.unsupported(
        f"{capability_name} acts on a run the owner names, and no run id was supplied: "
        "this worker creates no run but its own frozen probe constant, so it will not "
        "pick an arbitrary run and report its behaviour as this capability's.",
        reason="run_id_required",
        data={"asked_for": capability_name, "requests_made": 0,
              "no_request_made": True},
        next_action=("start a trivial run yourself (POST /v1/runs with the same "
                     "Idempotency-Key, O17), then re-run with --run-id <the run id>; or "
                     "pass --submit-test-run to let this probe create its own frozen "
                     "constant run"))


HERMES_PROBES = {
    "hermes_capability_discovery": probe_capability_discovery,
    "hermes_run_submission_and_status": probe_run_submission_and_status,
    "hermes_run_progress_events": probe_run_progress_events,
    "hermes_run_stop": probe_run_stop,
    "hermes_session_continuity": probe_session_continuity,
    "hermes_execution_modes": probe_execution_modes,
    "hermes_approval_modes": probe_approval_modes,
    "hermes_credential_resolution": probe_credential_resolution,
}

__all__ = ["HERMES_PROBES", "probe_capability_discovery", "probe_run_submission_and_status",
           "probe_run_progress_events", "probe_run_stop", "probe_session_continuity",
           "probe_execution_modes", "probe_approval_modes", "probe_credential_resolution",
           "APPROVAL_OBSERVATIONS", "OWNER_APPROVAL_TRIGGER_COMMAND",
           "TERMINAL_EVENT_NAMES"]
