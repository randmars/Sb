"""The Contacts read adapter: the five Contacts capability rows, measured for real.

This is the third source adapter in the Mini worker (after Mail and Beeper). It exists so
that the pack's five Contacts rows can stop being ``origin: documentation`` and become
measurements on Randy's Mac, and so the probe's Contacts steps are runnable in one sitting
instead of being recorded as "not runnable".

What it reuses rather than reinventing
--------------------------------------
* the **one permission vocabulary** (``outcomes.PERMISSION_STATES``), including the
  ``restricted`` value macOS Contacts genuinely reports and this product could not express
  before this slice;
* the **one healthy-source definition** (``HEALTHY_SOURCE_STATES`` below), restated here
  because the Mini worker installs on its own and cannot import Grace, and asserted equal to
  Grace's copy by ``tests/test_shared_vocabulary.py``;
* the **one typed-outcome table** (``outcomes.ADAPTER_OUTCOMES``) and the shared
  AppleScript/JXA classification in ``applescript.py``.

What it will not do: write to Contacts, fetch while the authorization status is not
``authorized`` (that is what raises the system consent dialog), or report a value it did not
read. Every method below returns a typed :class:`~switchboard_mini.outcomes.Outcome`.
"""

from __future__ import annotations

from typing import Optional

from . import outcomes as O
from .contacts_transport import (NAMESPACE, OPERATIONS, FixtureContactsTransport,
                                 JxaContactsTransport, build_transport, load_fixture,
                                 permission_state_for, status_without_an_equivalent)
from .version import WORKER_VERSION

#: The healthy-source definition, shared with Grace
#: (``grace/ingest.py::Ingest.HEALTHY_SOURCE_STATES`` = ``("connected", "syncing",
#: "current")``). Restated, not reinvented: the Mini worker installs onto a Mac on its own
#: and imports nothing from the Grace package, so the copy has to exist here -- and
#: ``tests/test_shared_vocabulary.py`` fails if the two ever drift.
HEALTHY_SOURCE_STATES = ("connected", "syncing", "current")
#: The one state a successful Contacts read path reports. ``current`` is in the shared list
#: above and is the honest word here: the store answered when asked, so the projection is as
#: current as the store is.
HEALTHY_READ_STATE = "current"

MEASURED_CAPABILITIES = (
    "contacts_read_authorization",
    "contacts_enumerate_contacts",
    "contacts_identifier_scope",
    "contacts_unified_constituents",
    "contacts_change_history",
)


class ContactsReadOnlyAdapter:
    """A ``SourceAdapter`` in the Grace sense, restricted to the documented Contacts reads."""

    name = NAMESPACE
    version = WORKER_VERSION
    host_role = "mini"
    simulated = False

    def __init__(self, transport, *, max_items: int = 25):
        self.transport = transport
        self.max_items = max(1, int(max_items))

    # -- provenance --------------------------------------------------------
    @property
    def origin(self) -> str:
        return self.transport.origin

    @property
    def adapter_is_real(self) -> bool:
        return bool(getattr(self.transport, "adapter_is_real", False))

    def _label(self) -> Optional[str]:
        return getattr(self.transport, "label", None)

    @property
    def key_symbol(self) -> Optional[str]:
        return getattr(self.transport, "key_symbol", None)

    @property
    def unify_off(self) -> bool:
        return bool(getattr(self.transport, "unify_off", False))

    def _stamp(self, outcome: O.Outcome, *, from_read: Optional[O.Outcome] = None) -> O.Outcome:
        outcome.adapter = self.name
        outcome.origin = self.origin
        outcome.adapter_is_real = self.adapter_is_real
        if from_read is not None:
            outcome.adapter_is_real = bool(from_read.adapter_is_real or self.adapter_is_real)
            outcome.source_contacted = bool(from_read.source_contacted)
        if not outcome.is_real:
            outcome.label = self._label() or O.fixture_label(self.name)
        return outcome

    def probe_handlers(self) -> dict:
        """The probe rows this adapter measures, keyed by capability name."""
        from .contacts_probe import CONTACTS_PROBES        # local import avoids a cycle
        return dict(CONTACTS_PROBES)

    @staticmethod
    def measured_capabilities() -> tuple:
        return MEASURED_CAPABILITIES

    # -- the reads ---------------------------------------------------------
    def authorization(self) -> O.Outcome:
        """The store's own authorization status. **Never asks for permission.**

        Reading the status does not raise the consent prompt; a fetch while the status is
        not ``authorized`` can. This method therefore exists on its own, and every fetch
        method calls it first and refuses before constructing a fetch.
        """
        return self._stamp(self.transport.authorization_status())

    def request_access(self) -> O.Outcome:
        """Ask the system for Contacts access. The prompt is the point of this call.

        Separate from every read on purpose: a probe must be able to report the permission
        state without ever triggering a dialog, and this is the only command that may.
        """
        return self._stamp(self.transport.request_access())

    def enumerate_contacts(self, *, unify_off: Optional[bool] = None) -> O.Outcome:
        return self._stamp(self.transport.enumerate_contacts(unify_off=unify_off))

    def restricted_keys(self, symbol: Optional[str] = None) -> O.Outcome:
        return self._stamp(self.transport.restricted_keys(symbol))

    def change_history(self, *, invalid_token: Optional[bool] = None) -> O.Outcome:
        return self._stamp(self.transport.change_history(invalid_token=invalid_token))

    def health(self) -> O.Outcome:
        """One health document on the shared vocabulary, built on the authorization read.

        A granted, readable store is ``current`` (a word from the shared healthy-source
        definition). Anything else returns the typed refusal unchanged: the store's own
        condition (``permission_denied`` with reason ``contacts_access_denied`` /
        ``contacts_access_restricted`` / ``contacts_authorization_not_determined``) is what
        the surfaces render, so nothing here invents a health word for a blocked source.
        """
        got = self.transport.authorization_status()
        if not got.usable:
            return self._stamp(got)
        data = dict(got.data or {})
        state = permission_state_for(data)
        data.update({"health_state": HEALTHY_READ_STATE if state == O.PERMISSION_GRANTED
                     else None,
                     "permission_state": state or O.PERMISSION_NOT_DETERMINED,
                     "healthy_source_states": list(HEALTHY_SOURCE_STATES),
                     "note": ("a healthy read state is not completeness and not write "
                              "capability: this adapter holds no write path at all (PRD §10, "
                              "O13: contact writing is out of scope)")})
        return self._stamp(O.Outcome.ok(data))


def build_adapter(*, fixture_mode: bool = False,
                  fixture_scenario: str = "authorization_granted",
                  timeout_s: int = 120, limit: int = 25,
                  key_symbol: Optional[str] = None, unify_off: bool = False,
                  invalid_token: bool = False, include_group_changes: bool = False,
                  token_file: Optional[str] = None, runner=None) -> ContactsReadOnlyAdapter:
    transport = build_transport(fixture_mode=fixture_mode, fixture_scenario=fixture_scenario,
                               timeout_s=timeout_s, limit=limit, key_symbol=key_symbol,
                               unify_off=unify_off, invalid_token=invalid_token,
                               include_group_changes=include_group_changes,
                               token_file=token_file, runner=runner)
    return ContactsReadOnlyAdapter(transport, max_items=limit)


__all__ = ["ContactsReadOnlyAdapter", "build_adapter", "MEASURED_CAPABILITIES",
           "HEALTHY_SOURCE_STATES", "HEALTHY_READ_STATE", "NAMESPACE", "OPERATIONS",
           "JxaContactsTransport", "FixtureContactsTransport", "load_fixture"]
