"""Grace — Switchboard's headless coordination service foundation (PRD §14 Gate 1).

Modules
-------
contracts   typed adapter outcomes, independent state enums, clock, ids, hashing, labels
store       SQLite connection, schema migration, labelling-enforcing writes, audit
adapters    adapter interface (PRD §11) and visibly-labelled mock adapters
fixtures    deterministic labelled fixture corpus and scripted mock scenarios
ledger      durable job ledger, leases, dedup keys, outbox, idempotent ingest hooks
effects     drafts, versioned approvals, outbound effect ledger and reconciliation
rules       versioned rules, frozen preview, repeat-safe application (PRD §7)
ingest      at-least-once events, idempotent projection, coverage map (PRD §6)
service     application operations used by the CLI and the tests
cli         headless command line with stable JSON output

Nothing in this package talks to Mail, Beeper, Contacts or Hermes. Those are Gate 2/3
work on Randy's Mac; every source it reads is a labelled mock.
"""

from . import contracts  # noqa: F401  (re-exported for convenience)

__all__ = ["contracts"]
__version__ = "0.1.0"
