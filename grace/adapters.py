"""Adapter interfaces and visibly-labelled mock adapters.

Interface shape follows PRD §11 ("Integration contracts") and §12 ("Suggested
service operations"): versioned capability manifest, health, account inventory,
bounded enumeration, retrieve-by-reference, history/change polling, attachment
materialisation, optional draft preparation, dispatch and reconciliation — all
returning *typed results*, never exceptions and never a silent empty success.

Every adapter in this file is a MOCK. Each one:
  * sets ``simulated = True`` and reports ``adapter_version`` suffixed ``-mock``;
  * returns payloads carrying ``origin='mock'`` and a ``MOCK:`` label;
  * uses provider identifiers namespaced under ``mock_*`` and ``mock://`` pointers;
  * refuses to be described as a live source in ``describe()``.
The real Mail/Beeper/Contacts/Hermes adapters do not exist yet (Gate 2/3) and no
claim is made that they work.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import contracts as C
from .fixtures import SOURCE_VERSIONS, scenario as load_scenario

CAPABILITY_NAMES = (
    "manifest", "health", "accounts", "enumerate", "retrieve", "history_poll",
    "change_poll", "identity_evidence", "materialize_attachment", "prepare_draft",
    "dispatch", "reconcile",
)

OPTIONAL_CAPABILITIES = ("prepare_draft", "dispatch", "reconcile", "change_poll",
                         "identity_evidence", "materialize_attachment")

MANIFEST_VERSION = "1.0"


@dataclass
class CapabilityEntry:
    supported: bool
    state: str = "ok"                    # ok|unsupported|permission_denied|unverified
    limitation: str | None = None
    probe_method: str | None = None

    def to_dict(self) -> dict:
        return {"supported": self.supported, "state": self.state,
                "limitation": self.limitation, "probe_method": self.probe_method}


@dataclass
class CapabilityManifest:
    """Versioned capability manifest (PRD §6, §11)."""

    adapter: str
    adapter_version: str
    host_role: str
    manifest_version: str = MANIFEST_VERSION
    capabilities: dict[str, CapabilityEntry] = field(default_factory=dict)
    simulated: bool = False
    simulated_label: str | None = None

    def supported(self, name: str) -> bool:
        entry = self.capabilities.get(name)
        return bool(entry and entry.supported)

    def to_dict(self) -> dict:
        payload = {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "manifest_version": self.manifest_version,
            "host_role": self.host_role,
            "simulated": self.simulated,
            "capabilities": {k: v.to_dict() for k, v in sorted(self.capabilities.items())},
            "origin": C.MOCK if self.simulated else C.REAL,
        }
        if self.simulated:
            payload["mock_label"] = self.simulated_label or C.mock_label(self.adapter)
            payload["disclaimer"] = C.MOCK_DISCLAIMER
        C.assert_labelled(payload["origin"], payload.get("mock_label"), "CapabilityManifest")
        return payload


class SourceAdapter(abc.ABC):
    """Contract every source adapter must satisfy. All methods return Outcome."""

    name: str = "adapter"
    version: str = "0.0.0"
    host_role: str = "mini"
    simulated: bool = False

    # -- discovery ---------------------------------------------------------
    @abc.abstractmethod
    def manifest(self) -> CapabilityManifest: ...

    @abc.abstractmethod
    def health(self, account_id: str) -> C.Outcome: ...

    @abc.abstractmethod
    def accounts(self) -> C.Outcome: ...

    # -- reads -------------------------------------------------------------
    @abc.abstractmethod
    def enumerate(self, account_id: str, scope_ref: str, *, limit: int = 25,
                  cursor: str | None = None, since: str | None = None) -> C.Outcome: ...

    @abc.abstractmethod
    def retrieve(self, account_id: str, namespaced_id: str) -> C.Outcome: ...

    @abc.abstractmethod
    def history_poll(self, account_id: str, scope_ref: str, *, cursor: str | None = None,
                     limit: int = 50) -> C.Outcome: ...

    @abc.abstractmethod
    def change_poll(self, account_id: str, *, token: str | None = None) -> C.Outcome: ...

    @abc.abstractmethod
    def materialize_attachment(self, account_id: str, namespaced_id: str, *,
                               max_bytes: int = 10_000_000) -> C.Outcome: ...

    # -- optional mutations, individually declared -------------------------
    def prepare_draft(self, account_id: str, request: dict) -> C.Outcome:
        return C.Outcome.unsupported(
            "prepare_draft is not declared by this adapter", adapter=self.name,
            provenance=C.MOCK if self.simulated else C.REAL,
            label=self._label() if self.simulated else None)

    def dispatch(self, account_id: str, request: dict) -> C.Outcome:
        return C.Outcome.unsupported(
            "dispatch is not declared by this adapter", adapter=self.name,
            provenance=C.MOCK if self.simulated else C.REAL,
            label=self._label() if self.simulated else None)

    def reconcile(self, account_id: str, *, idempotency_key: str,
                  source_operation_id: str | None = None) -> C.Outcome:
        return C.Outcome.unsupported(
            "reconcile is not declared by this adapter", adapter=self.name,
            provenance=C.MOCK if self.simulated else C.REAL,
            label=self._label() if self.simulated else None)

    # -- helpers -----------------------------------------------------------
    def _label(self) -> str:
        return C.mock_label(self.name)

    def _provenance(self) -> str:
        return C.MOCK if self.simulated else C.REAL

    def _out(self, outcome: C.Outcome) -> C.Outcome:
        """Stamp adapter identity and mock labelling on an outgoing result."""
        outcome.adapter = self.name
        outcome.provenance = self._provenance()
        if self.simulated:
            outcome.label = self._label()
        return outcome

    def describe(self) -> dict:
        is_mock = self.simulated
        payload = {
            "adapter": self.name,
            "version": self.version,
            "host_role": self.host_role,
            "simulated": is_mock,
            "origin": C.MOCK if is_mock else C.REAL,
            "real_source_connected": False,
        }
        if is_mock:
            payload["mock_label"] = self._label()
            payload["disclaimer"] = C.MOCK_DISCLAIMER
        C.assert_labelled(payload["origin"], payload.get("mock_label"), "SourceAdapter.describe")
        return payload


# ---------------------------------------------------------------- mock base --


class MockSourceAdapter(SourceAdapter):
    """Labelled mock source built over the deterministic fixture corpus.

    ``faults`` injects honest failure states so the product can be shown telling
    the truth about a broken or unpermitted source (PRD §6 health presentation,
    T10, T20). Faults are always reported as typed outcomes, never as empty
    successes.
    """

    simulated = True
    supported_capabilities: tuple[str, ...] = CAPABILITY_NAMES

    def __init__(self, corpus: dict, *, scenario: str | None = None,
                 faults: Iterable[str] = (), page_size_note: str | None = None,
                 label_detail: str | None = None):
        self.corpus = corpus
        self.scenario = load_scenario(scenario)
        overrides = self.scenario.get("by_adapter", {}).get(self.name, {})
        self.dispatch_spec = {**self.scenario.get("dispatch", {}),
                              **overrides.get("dispatch", {})}
        self.reconcile_spec = {**self.scenario.get("reconcile", {}),
                               **overrides.get("reconcile", {})}
        self.faults = set(faults)
        self.page_size_note = page_size_note
        self._label_detail = label_detail
        self._idempotency_memory: dict[str, dict] = {}
        self._submitted: dict[str, dict] = {}

    # -- labelling ---------------------------------------------------------
    def _label(self) -> str:
        return C.mock_label(self.name, self._label_detail)

    def _mock_rows(self, rows: Iterable[dict]) -> list[dict]:
        """Attach the mock marker to each returned record."""
        out = []
        for row in rows:
            stamped = dict(row)
            stamped["origin"] = C.MOCK
            stamped["mock_label"] = self._label()
            out.append(stamped)
        return out

    def _accounts(self) -> list[dict]:
        return [a for a in self.corpus["accounts"] if a["adapter"] == self.name]

    def _conversations(self, account_id: str | None = None) -> list[dict]:
        convs = [c for c in self.corpus["conversations"] if c["adapter"] == self.name]
        if account_id:
            convs = [c for c in convs if c["account_id"] == account_id]
        return convs

    def _messages(self, account_id: str | None = None) -> list[dict]:
        msgs = [m for m in self.corpus["messages"] if m["adapter"] == self.name]
        if account_id:
            msgs = [m for m in msgs if m["account_id"] == account_id]
        return sorted(msgs, key=lambda m: (m["source_time"], m["namespaced_id"]))

    # -- fault gates -------------------------------------------------------
    def _blocked(self, account_id: str | None = None) -> C.Outcome | None:
        if "offline" in self.faults:
            return self._out(C.Outcome.offline(
                "MOCK: source is offline (injected fault); no data was read"))
        if "permission_denied" in self.faults:
            return self._out(C.Outcome.permission_denied(
                "MOCK: permission for this source is denied (injected fault)"))
        if account_id:
            acct = next((a for a in self._accounts() if a["account_id"] == account_id), None)
            if acct is None:
                return self._out(C.Outcome.permanent(
                    f"unknown account {account_id} for adapter {self.name}"))
            if acct["health_state"] == "permission_denied" and "permission_override" not in self.faults:
                return self._out(C.Outcome.permission_denied(
                    acct.get("health_detail") or "MOCK: permission denied",
                    account_id=account_id))
            if acct["health_state"] == "offline":
                return self._out(C.Outcome.offline("MOCK: account reported offline",
                                                   account_id=account_id))
        return None

    # -- discovery ---------------------------------------------------------
    def manifest(self) -> CapabilityManifest:
        caps = {}
        for cap in CAPABILITY_NAMES:
            supported = cap in self.supported_capabilities
            caps[cap] = CapabilityEntry(
                supported=supported,
                state="ok" if supported else "unsupported",
                limitation=None if supported else "MOCK: not declared by this adapter",
                probe_method="mock-fixture (no probe against a real source)")
        return CapabilityManifest(
            adapter=self.name, adapter_version=self.version, host_role=self.host_role,
            capabilities=caps, simulated=True, simulated_label=self._label(),
        )

    def health(self, account_id: str) -> C.Outcome:
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        acct = next(a for a in self._accounts() if a["account_id"] == account_id)
        return self._out(C.Outcome.ok({
            "account_id": account_id, "health_state": acct["health_state"],
            "detail": acct.get("health_detail"),
            "permission_state": acct["permission_state"],
            "last_success_at": acct["last_success_at"],
            "observed_at": C.now(),
            "freshness": "fixture (MOCK): freshness is a fixed fixture value, not an observation",
            "coverage_state": "partial_history" if "partial_history" in self.faults else "fixture",
            "next_action": None if acct["health_state"] == "current" else "grant permission on Mini",
        }))

    def accounts(self) -> C.Outcome:
        blocked = self._blocked()
        if blocked:
            return blocked
        return self._out(C.Outcome.ok({"accounts": self._mock_rows(self._accounts())}))

    # -- reads -------------------------------------------------------------
    def enumerate(self, account_id: str, scope_ref: str, *, limit: int = 25,
                  cursor: str | None = None, since: str | None = None) -> C.Outcome:
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        msgs = [m for m in self._messages(account_id) if m["conv_id"] in
                {c["conv_id"] for c in self._conversations(account_id)}]
        if since:
            msgs = [m for m in msgs if m["source_time"] >= since]
        start = int(cursor.split(":")[-1]) if cursor else 0
        window = msgs[start:start + limit]
        next_cursor = f"mock-cursor:{start + limit}" if start + limit < len(msgs) else None
        partial = "partial_history" in self.faults
        detail = ("MOCK: injected partial-history fault; this run proves only that the adapter "
                  "reached the end of this query's accessible result set")
        data = {
            "items": self._mock_rows(window),
            "next_cursor": next_cursor,
            "coverage": {
                "observed_count": len(window),
                "returned_total_for_query": len(msgs),
                "coverage_state": "partial_history" if partial else "fixture_scan",
                "gap_reason": detail if partial else None,
                "note": "MOCK: counts describe the fixture corpus, not a real mailbox",
            },
        }
        if partial:
            return self._out(C.Outcome.partial(data, detail, account_id=account_id))
        return self._out(C.Outcome.ok(data, account_id=account_id))

    def retrieve(self, account_id: str, namespaced_id: str) -> C.Outcome:
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        for m in self._messages(account_id):
            if m["namespaced_id"] == namespaced_id:
                if m.get("deleted_at_source"):
                    return self._out(C.Outcome.partial(
                        {"message": None, "tombstone": self._mock_rows([m])[0],
                         "body": None},
                        "MOCK: source message was deleted upstream; only a tombstone exists",
                        account_id=account_id))
                if m["availability"] != "available":
                    return self._out(C.Outcome.partial(
                        {"message": self._mock_rows([m])[0], "body": None,
                         "body_state": m["body_state"], "unavailable_reason": m.get("availability_reason")},
                        "MOCK: content unavailable — " + str(m.get("availability_reason")),
                        account_id=account_id))
                body = (f"[MOCK body — invented fixture text, not a real message]\n"
                        f"Fixture message {m['provider_message_id']} from "
                        f"{m['sender'].get('display_name') or m['sender']}")
                return self._out(C.Outcome.ok({"message": self._mock_rows([m])[0],
                                               "body": body, "body_state": "fetched"},
                                              account_id=account_id))
        return self._out(C.Outcome.permanent(f"unknown message reference {namespaced_id}",
                                             account_id=account_id))

    def history_poll(self, account_id: str, scope_ref: str, *, cursor: str | None = None,
                     limit: int = 50) -> C.Outcome:
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        stage = int(cursor.split(":")[-1]) if cursor else 0
        all_msgs = self._messages(account_id)
        msgs = all_msgs[stage:stage + limit]
        events = [{
            "event_id": f"mock-event:{m['namespaced_id']}:{m['revision']}",
            "kind": "message_upsert",
            "namespaced_id": m["namespaced_id"],
            "revision": m["revision"],
            "account_id": account_id,
            "source_time": m["source_time"],
            "origin": C.MOCK, "mock_label": self._label(),
        } for m in msgs]
        next_stage = stage + limit
        return self._out(C.Outcome.ok({
            "events": events,
            "next_cursor": f"mock-poll:{next_stage}" if next_stage < len(all_msgs) else None,
            "overlap_window": "MOCK: overlap = 1 revision; real adapters must define theirs",
            "note": "MOCK: events are a trigger to retrieve authoritative data, not instructions",
        }, account_id=account_id))

    def change_poll(self, account_id: str, *, token: str | None = None) -> C.Outcome:
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        if self.name != "mock_contacts":
            return self._out(C.Outcome.unsupported(
                "change_poll is only meaningful for the contacts adapter", account_id=account_id))
        if token == "mock-expired-token" or "token_reset" in self.faults:
            return self._out(C.Outcome.partial({
                "changes": [], "next_token": "mock-history-token-2",
                "reset": True,
                "projection_required": "rebuild",
            }, "MOCK: change-history token reset; a projection rebuild is required, which does "
               "not erase unrelated relationship knowledge", account_id=account_id))
        changes = [{
            "kind": "contact_upsert", "namespaced_id": f"mock_contacts:{account_id}:{p['person_id']}",
            "display_name": p["display_name"], "revision": "1",
            "origin": C.MOCK, "mock_label": self._label(),
        } for p in self.corpus["people"]]
        return self._out(C.Outcome.ok({"changes": changes, "next_token": "mock-history-token-1",
                                       "reset": False}, account_id=account_id))

    def materialize_attachment(self, account_id: str, namespaced_id: str, *,
                               max_bytes: int = 10_000_000) -> C.Outcome:
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        att = next((a for a in self.corpus["attachments"] if a["namespaced_id"] == namespaced_id), None)
        if att is None:
            return self._out(C.Outcome.permanent(f"unknown attachment {namespaced_id}"))
        if not att["available"]:
            return self._out(C.Outcome.partial(
                {"attachment": att, "bytes": None},
                "MOCK: attachment is not materialised — " + str(att["limitation"]),
                account_id=account_id))
        if att["size_bytes"] and att["size_bytes"] > max_bytes:
            return self._out(C.Outcome.partial(
                {"attachment": att, "bytes": None},
                f"MOCK: attachment exceeds the size limit ({att['size_bytes']} > {max_bytes})",
                account_id=account_id))
        safe = C.sanitize_filename(att["filename"])
        return self._out(C.Outcome.ok({
            "attachment": att,
            "sanitized_filename": safe,
            "bytes": b"".join((b"MOCK-FIXTURE-BYTES:", att["namespaced_id"].encode())),
            "validation": {"media_type": att["media_type"], "quarantine_state": "none",
                           "executable_blocked": True},
        }, account_id=account_id))

    # -- optional mutations ------------------------------------------------
    def prepare_draft(self, account_id: str, request: dict) -> C.Outcome:
        if "prepare_draft" not in self.supported_capabilities:
            return self._out(C.Outcome.unsupported("MOCK: prepare_draft not declared"))
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        return self._out(C.Outcome.ok({
            "prepared": True, "note": "MOCK: nothing was written to a real composer; reviewable "
                                      "drafts live in this application",
            "application_draft_id": request.get("draft_id"),
        }, account_id=account_id))

    def dispatch(self, account_id: str, request: dict) -> C.Outcome:
        """Scripted MOCK dispatch. Nothing leaves this process."""
        if "dispatch" not in self.supported_capabilities:
            return self._out(C.Outcome.unsupported("MOCK: dispatch not declared"))
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        spec = self.dispatch_spec
        key = request["idempotency_key"]
        code = spec.get("code", "success")
        if code == "permission_denied":
            return self._out(C.Outcome.permission_denied(
                spec.get("detail", "MOCK: send not permitted"), account_id=account_id))
        if code == "retryable_error":
            return self._out(C.Outcome.retryable(
                spec.get("detail", "MOCK: transport failure before submission"),
                account_id=account_id))
        if code == "outcome_unknown":
            self._submitted[key] = {"submitted": True, "request": request}
            return self._out(C.Outcome.uncertain(
                spec.get("detail", "MOCK: outcome unknown after submission"),
                account_id=account_id))
        # success path
        if key in self._idempotency_memory and spec.get("idempotent_replay", True):
            prior = self._idempotency_memory[key]
            return self._out(C.Outcome.ok({
                "provider_message_id": prior["provider_message_id"],
                "provider_status": prior.get("provider_status", "pending"),
                "actual_routed_destination": prior.get("actual_routed_destination"),
                "source_operation_id": prior.get("source_operation_id"),
                "idempotent_replay": True,
                "note": "MOCK: source honoured the idempotency key; no second send occurred",
            }, account_id=account_id))
        provider_id = f"mock-sent-{key[-12:]}"
        routed = request.get("target_provider_id") or request["target_conv_id"]
        if spec.get("route_to") == "member_chat_confirmed":
            routed = routed + " (member chat, MOCK-confirmed routing)"
        record = {
            "provider_message_id": provider_id,
            "provider_status": spec.get("provider_status", "pending"),
            "actual_routed_destination": routed,
            "source_operation_id": f"mock-op-{key[-8:]}",
        }
        self._idempotency_memory[key] = record
        return self._out(C.Outcome.ok({
            **record,
            "note": "MOCK send: no real message exists and none was transmitted",
        }, account_id=account_id))

    def reconcile(self, account_id: str, *, idempotency_key: str,
                  source_operation_id: str | None = None) -> C.Outcome:
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        spec = self.reconcile_spec
        code = spec.get("code", "unsupported")
        if code == "unsupported":
            return self._out(C.Outcome.unsupported(
                spec.get("detail", "MOCK: reconciliation not supported by this adapter"),
                account_id=account_id))
        if code == "outcome_unknown":
            return self._out(C.Outcome.uncertain(
                spec.get("detail", "MOCK: reconciliation inconclusive"),
                account_id=account_id))
        found = bool(spec.get("confirmed", code == "success"))
        payload = {
            "found": found,
            "provider_message_id": spec.get("provider_message_id") if found else None,
            "idempotency_key": idempotency_key,
            "source_operation_id": source_operation_id,
            "note": "MOCK reconciliation against fixture state only",
        }
        if code == "partial":
            return self._out(C.Outcome.partial(
                payload, spec.get("limitation", "MOCK: partial reconciliation"),
                account_id=account_id))
        return self._out(C.Outcome.ok(payload, account_id=account_id))


class MockMailAdapter(MockSourceAdapter):
    name = "mock_mail"
    version = SOURCE_VERSIONS["mock_mail"]
    host_role = "mini"
    supported_capabilities = ("manifest", "health", "accounts", "enumerate", "retrieve",
                              "history_poll", "materialize_attachment", "prepare_draft",
                              "dispatch", "reconcile")


class MockBeeperAdapter(MockSourceAdapter):
    name = "mock_beeper"
    version = SOURCE_VERSIONS["mock_beeper"]
    host_role = "mini"
    # No change_poll / identity_evidence: an adapter declares only what it supports,
    # and unsupported operations must be reported as such (PRD §6).
    supported_capabilities = ("manifest", "health", "accounts", "enumerate", "retrieve",
                              "history_poll", "materialize_attachment", "prepare_draft",
                              "dispatch", "reconcile")


class MockContactsAdapter(MockSourceAdapter):
    name = "mock_contacts"
    version = SOURCE_VERSIONS["mock_contacts"]
    host_role = "mini"
    supported_capabilities = ("manifest", "health", "accounts", "enumerate", "retrieve",
                              "change_poll", "identity_evidence")

    def identity_evidence(self, account_id: str) -> C.Outcome:
        blocked = self._blocked(account_id)
        if blocked:
            return blocked
        return self._out(C.Outcome.ok({
            "identities": self._mock_rows(self.corpus["identity_links"]),
            "note": "MOCK: contact identifiers are device-local in reality; namespaced here",
        }, account_id=account_id))


class MockHermesAdapter(SourceAdapter):
    """Labelled MOCK of the Hermes runtime boundary (PRD §9).

    Only used to exercise the application's own session/run bookkeeping: session
    identity, run identity, progress, stop. It proves nothing about the installed
    Hermes build; that is Gate 2 work on Randy's machines.
    """

    name = "mock_hermes"
    version = SOURCE_VERSIONS["mock_hermes"]
    host_role = "grace"
    simulated = True

    def __init__(self, corpus: dict, *, scenario: str | None = None, faults: Iterable[str] = ()):
        self.corpus = corpus
        self.scenario = load_scenario(scenario)
        self.faults = set(faults)
        self.runs: dict[str, dict] = {}

    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            adapter=self.name, adapter_version=self.version, host_role=self.host_role,
            capabilities={c: CapabilityEntry(supported=True, state="ok",
                                             probe_method="mock-fixture")
                          for c in ("manifest", "health", "accounts", "sessions", "runs",
                                    "progress", "stop")},
            simulated=True, simulated_label=self._label())

    def health(self, account_id: str) -> C.Outcome:
        if "offline" in self.faults:
            return self._out(C.Outcome.offline("MOCK: Hermes runtime unreachable (injected fault)"))
        return self._out(C.Outcome.ok({"health_state": "current",
                                       "note": "MOCK runtime; the installed build is unknown"},
                                      account_id=account_id))

    def accounts(self) -> C.Outcome:
        return self._out(C.Outcome.ok({"accounts": self._mock_rows(
            [a for a in self.corpus["accounts"] if a["adapter"] == self.name])}))

    def enumerate(self, account_id: str, scope_ref: str, *, limit: int = 25,
                  cursor: str | None = None, since: str | None = None) -> C.Outcome:
        return self._out(C.Outcome.unsupported("MOCK: Hermes has sessions and runs, not enumeration"))

    def retrieve(self, account_id: str, namespaced_id: str) -> C.Outcome:
        run = self.runs.get(namespaced_id)
        if run is None:
            return self._out(C.Outcome.permanent(f"unknown run {namespaced_id}"))
        return self._out(C.Outcome.ok({"run": run}, account_id=account_id))

    def history_poll(self, account_id: str, scope_ref: str, *, cursor: str | None = None,
                     limit: int = 50) -> C.Outcome:
        return self._out(C.Outcome.unsupported("MOCK: use run status, not history polling"))

    def change_poll(self, account_id: str, *, token: str | None = None) -> C.Outcome:
        return self._out(C.Outcome.unsupported("MOCK: not applicable"))

    def materialize_attachment(self, account_id: str, namespaced_id: str, *,
                               max_bytes: int = 10_000_000) -> C.Outcome:
        return self._out(C.Outcome.unsupported("MOCK: not applicable"))

    # sessions/runs --------------------------------------------------------
    def start_run(self, *, session_key: str, job_id: str, instruction: str,
                  profile: str = "mock-profile") -> C.Outcome:
        if "offline" in self.faults:
            return self._out(C.Outcome.offline("MOCK: runtime unreachable; run not started"))
        run_id = C.stable_id("mockrun", job_id, session_key)
        self.runs[run_id] = {
            "run_id": run_id, "session_key": session_key, "job_id": job_id,
            "profile": profile, "run_state": "running", "started_at": C.now(),
            "instruction_hash": C.text_hash(instruction),
            "origin": C.MOCK, "mock_label": self._label(),
        }
        return self._out(C.Outcome.ok({"run": self.runs[run_id]}))

    def run_status(self, run_id: str) -> C.Outcome:
        run = self.runs.get(run_id)
        if run is None:
            return self._out(C.Outcome.permanent(f"unknown run {run_id}"))
        return self._out(C.Outcome.ok({"run": run}))

    def stop_run(self, run_id: str, *, reason: str = "cancelled") -> C.Outcome:
        run = self.runs.get(run_id)
        if run is None:
            return self._out(C.Outcome.permanent(f"unknown run {run_id}"))
        run["run_state"] = "stopped"
        run["stop_reason"] = reason
        return self._out(C.Outcome.ok({"run": run,
                                       "note": "MOCK: an already-submitted external effect is not "
                                               "undone by stopping a run"}))


def default_adapters(corpus: dict, *, scenario: str | None = None,
                     faults: Iterable[str] = ()) -> dict[str, SourceAdapter]:
    return {
        "mock_mail": MockMailAdapter(corpus, scenario=scenario, faults=faults),
        "mock_beeper": MockBeeperAdapter(corpus, scenario=scenario, faults=faults),
        "mock_contacts": MockContactsAdapter(corpus, scenario=scenario, faults=faults),
        "mock_hermes": MockHermesAdapter(corpus, scenario=scenario, faults=faults),
    }
