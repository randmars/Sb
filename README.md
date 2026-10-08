# Switchboard — Grace service foundation (Gate 1)

This repository is the **Grace** side of Switchboard: the headless coordination service that
holds Randy's conversations, jobs, drafts, approvals and outbound operations in a durable local
ledger, drives independent work in parallel, and refuses to invent results it cannot verify.

It is built to the product brief *Personal AI communications workspace — Product requirements and
implementation brief*, v1.0 (7 Oct 2026). Requirement IDs (`R01`–`R16`), acceptance tests
(`T01`–`T22`) and gates (1–4) used throughout this repository come from that brief.

**This is Gate 1 work: contracts plus a demonstrable prototype, exercised against visibly
labelled mocks.** Nothing in this repository talks to Mail, Beeper, Contacts or Hermes. Every
mocked value says so. Read [What was actually executed](#what-was-actually-executed-here) before
quoting any number from this repository.

---

## Quickstart

```bash
cd /home/team/shared/switchboard          # or the checkout on your machine
python3 -m grace --db /tmp/sb.sqlite3 seed --reset      # deterministic labelled fixtures
python3 -m grace --db /tmp/sb.sqlite3 needs-me
python3 -m grace --db /tmp/sb.sqlite3 assign --ws <ws_conv_id> \
        --instruction "Check the order status and draft a reply." --agent researcher
python3 -m grace --db /tmp/sb.sqlite3 run --job <job_id>          # MOCK worker pass
python3 -m grace --db /tmp/sb.sqlite3 approve --draft <draft_id> --operation-id op-1
python3 -m grace --db /tmp/sb.sqlite3 dispatch --approval <appr_id>   # labelled mock adapter
python3 -m grace --db /tmp/sb.sqlite3 reconcile --effect <effect_id>
```

Or the whole walk in one command, with the command equivalents printed:

```bash
python3 -m grace --db /tmp/sb.sqlite3 demo --pretty
```

Requirements: Python 3.10+ with `sqlite3`. **No third-party packages, no network access, no
build step.** Tests use the standard library `unittest`:

```bash
python3 -m unittest discover -s tests -t .
```

The CLI prints one JSON document per command. Keys are sorted and every output that contains
mocked data carries `"mocked": true`, a `MOCK:` label and the disclaimer — so a mock send can
never be read as a real one.

---

## What was actually executed here

This team's Linux computer stands in for **Grace** (PRD §1 deployment roles). The macOS **Mini**
worker, Apple Mail, Contacts and Beeper Desktop cannot run here, and **Hermes was not contacted**.
The brief's rule is absolute: *anything presented as working has been exercised against the real
source; where it has not, the product shows an explicit typed state.*

| Component | Status in this repository | Must be verified on Randy's Mac |
|---|---|---|
| Grace service, ledger, approvals, effects, rules, ingest | **Executed for real here** — real SQLite, real transactions, real process restarts | — |
| CLI and its JSON output | **Executed for real here** | — |
| Test suite (`tests/`, 142 tests) | **Executed for real here** (all pass) | — |
| Mail / Beeper / Contacts / Hermes adapters | **Mock only** (`MockMailAdapter`, `MockBeeperAdapter`, `MockContactsAdapter`, `MockHermesAdapter`), labelled `MOCK:` | The real adapters, the capability manifests they return, permissions, versions, latency (PRD §11, §14 Gate 2) |
| Agent runs (the "MOCK worker pass") | **Simulated**; produces a labelled draft, never an external message | Real Hermes sessions/runs/progress/stop (Gate 3) |
| Sends, dispatch results, receipts | **Simulated end to end.** `verified_against_real_source = 0` on every receipt | Real outbound operation and reconciled receipt (Gate 3) |
| Review web client, phone surface | **Built and exercised for real here** — `grace serve` over HTTP on this computer, driven in a real browser at iPhone viewport (Needs me → assign → mock worker run → review → approve-and-bind → dispatch → receipt). Served content is labelled mock (`MOCK:`, `mocked: true`) | Randy validating triage and approval on his own phone, and how his phone reaches the client (Gate 3/4) |
| Mini worker (`mini/`, `switchboard-mini`) | **Executed for real here** against recorded fixtures only; the four `--fixture-mode` probe scenarios and the cursor/retrieval behaviour are tested. Every row says `origin: fixture` | The real Mail.app probe rows, the Automation permission, the installed build's version and its actual AppleScript behaviour (Gate 2). See `mini/README.md` |

Consequences you can see in the data: every receipt records
`verified_against_real_source = 0`; every adapter's `describe()` returns
`real_source_connected: false`; `grace health` reports
`capability_honesty.real_sources_connected = false`. There is no code path in this repository
that can produce a receipt claiming a real provider confirmed delivery.

---

## Layout

```
grace/
  schema.sql     logical model, PRD §12 record lists (SQLite)
  contracts.py   typed outcomes, state machines, labelling rules, UTC time, hashing
  store.py       connection, migration, label-enforcing writes, re-entrant transactions
  adapters.py    SourceAdapter protocol + capability manifest + the four MOCK adapters
  fixtures.py    deterministic labelled fixture corpus + scripted mock scenarios
  ledger.py      durable job/attempt ledger, leases, transitions, dedup, outbox, checkpoints
  effects.py     drafts, versioned approvals, outbound effect ledger, dispatch, reconciliation
  rules.py       versioned rules, frozen previews, repeat-safe application
  ingest.py      deterministic seeding, at-least-once events, idempotent projection, sync
  service.py     Grace application object used by the CLI and the tests
  cli.py         headless CLI, stable JSON output
tests/           stdlib unittest suite (see below)
fixtures/        exported labelled corpus JSON (regenerated by `export-fixtures`)
```

### Requirement mapping

| PRD | Where it lives |
|---|---|
| §4 model (person/identity/group/source conversation/workspace conversation) | `schema.sql`, `ingest.py` (`_associate`), `effects.audience_snapshot` |
| §5 independent states (read ≠ review ≠ queue ≠ job ≠ rule) | separate columns/tables; `ledger._ws_brief`, `store` |
| §6 coverage, freshness, unavailable content | `ingest.py` (checkpoints, coverage map), `source_account.health_state` |
| §7 rules: versioned, repeat-safe, no send authority | `rules.py` |
| §9 job lifecycle, leases, restart | `ledger.py` |
| §10 draft/approval/effect contract | `effects.py` |
| §11 source capability reality | `adapters.py` manifests, `grace source-health` |
| §12 data contracts | `schema.sql` (`LABELLED_TABLES` in `store.py`) |
| §13 recovery/idempotency, honest failure | `ledger.py` (dedup, outbox, checkpoints), typed outcomes everywhere |
| R01–R03 (single owner, durable assignment, parallel work) | `ledger.create_job`, `cli assign` |
| R04–R06 (identity evidence, reversible corrections) | `person`/`identity_link`/`person_merge`, `ingest.project_message` |
| R07–R08 (rule preview, per-item keys) | `rules.preview`/`apply`, `rule_run_item.evaluation_key` |
| R09–R11 (honest outbound, approval binding, draft-only) | `effects.py` |
| R12–R16 (ledger, reversibility, inspectable rules, uncertain effects, draft-only launch) | `ledger.py`, `effects.py`, `rules.py` |

---

## Data model (PRD §12)

`grace/schema.sql` implements the record lists with these deliberate properties:

* **Independent states stay independent.** A source message carries its own `read_state`,
  `hidden_state` and `mute_state`; a workspace conversation carries `queue_state`,
  `assignment_state` and `review_state`; jobs, rule runs, approvals and effects each have their
  own state column and their own transition table. Binding a message to a job never writes to
  `read_state` (see `tests/test_restart_recovery.py`).
* **Source time is not ingestion time.** Every projected row keeps `source_time` (from the
  provider) separately from `ingested_at` (Grace's clock). Both are UTC ISO-8601 with `Z`;
  `contracts.now()` and `contracts.to_utc()` are the only clock helpers.
* **Versions are immutable.** `draft` rows are append-only versions; `rule` versions are
  superseded, not edited; `message_ref` keeps `revision` so an edited source message is a new
  version rather than an overwrite.
* **Provider identifiers are namespaced.** `namespaced_id = '<adapter>:<account>:<provider id>'`;
  aggregation into a person or group view never rewrites it (PRD §4).
* **Every table that can hold mocked data has `origin` + `mock_label`**, and `store.py` refuses a
  write that claims `origin='mock'` without a `MOCK:` label.

---

## Adapter interface (`grace/adapters.py`)

`SourceAdapter` is an ABC — the Python form of the PRD §11 interface — with:

| Method | Purpose |
|---|---|
| `manifest()` | versioned capability manifest: per capability `supported`, `permission_required`, `permission_state`, `limit`, `latency_ms`, `rate_limit`, `probe_method`, `probe_assertion`, `limitation`. A capability that is not declared is never simulated. |
| `health(account_id)` | typed health: connected, permission denied, offline, degraded, partial history |
| `accounts()` | account inventory with owner identity, enabled operations, health |
| `enumerate(account, scope, cursor, limit)` | bounded enumeration with coverage state and a cursor |
| `retrieve(account, namespaced_ref)` | retrieve by reference (full body, headers, tombstone) |
| `history(account, since, limit)` / `change_poll(account, token)` | history and change polling; `token_reset` is reported as a reset, not as "no changes" |
| `materialize_attachment(account, namespaced_ref)` | attachment bytes or an explicit unavailable state |
| `prepare_draft(account, intent)` *(optional)* | optional source-side draft preparation |
| `dispatch(account, request)` | submit an outbound operation; returns a provider id/status or a typed failure |
| `reconcile(account, request)` | read back what actually happened for an operation |

**Typed outcomes, never exceptions.** Every method returns `Outcome` with one of
`success, partial, unsupported, permission_denied, offline, rate_limited, retryable_error,
permanent_error, outcome_unknown`, plus `detail`, `data`, `retry_after`, `provenance`,
`mock_label` and a `disclaimer`. Adapter faults (`offline`, `permission_denied`,
`partial_history`, `token_reset`) are injectable and produce exactly those typed states — see
`tests/test_honest_failure_states.py`.

The capability manifest carries `probe_method` / `probe_assertion` strings precisely so that
Gate 2 can be executed as a checklist on Randy's Mac rather than as a code change: a capability
stays `supported: false` until a probe on the installed version confirms it.

---

## Mock labelling rules

1. **In code:** mock adapters are named `Mock*Adapter`, subclass `MockSourceAdapter`, and return
   `origin='mock'` with a `MOCK:` label on every outcome and every record.
2. **In stored data:** every labelled table row has `origin`/`mock_label`; `store.insert_row` and
   `store.update_row` raise `AssertionError` if a mock row has no label.
3. **In output:** the CLI declares `"mocked": true`, a `MOCK:` label and the disclaimer whenever
   the payload contains mock values *or* the deployment has any mock source. `emit()` also runs
   `contracts.find_unlabelled_mock()` over the payload and reports `labelling_problems` if it
   ever finds an unlabelled mock value.
4. **In receipts:** every receipt says whether it was verified against a real source
   (`verified_against_real_source`, which is `0` here) and what the evidence actually was.
5. **Enforced by tests:** `tests/test_mock_labelling.py` walks every labelled table, every CLI
   command and every adapter result, and proves the guard fires by trying to store an unlabelled
   mock value (`T20`, PRD §14 Gate 1).

---

## Job ledger (PRD §9, R02, R12)

States: `queued, running, waiting_for_source, waiting_for_user, waiting_for_approval,
ready_for_review, succeeded, failed, cancelled, superseded`.

* **Persisted before launch.** `ledger.create_job()` writes the job, its inputs, its first
  transition and an outbox row in one transaction *before* anything is published to a worker.
* **Transitions** record actor, time, reason, `from`/`to` version and an `expected_version`
  guard, so a stale writer conflicts instead of overwriting.
* **Claims are leases.** `claim()` takes a short lease (`lease_expires_at`, `lease_owner`);
  `renew_lease()` extends it. No database transaction is ever held across the work itself.
* **Stalled work becomes visible.** `reap_expired_leases()` turns an expired lease into a
  visible state change (`lease expired — job returned to a recoverable state`), increments
  `stall_count` and closes the interrupted attempt with the typed outcome
  `outcome_unknown` — never a success.
* **Cancellation and supersession** are recorded with lineage: superseding a job links
  `superseded_by`, and cancelling keeps the previous attempts readable.
* **Restart recovery** is what `tests/test_restart_recovery.py` proves: the job, its lease and
  its attempt history survive the death of the process that was working on it.

---

## Drafts, approvals and outbound effects (PRD §10, R10, R11, R15)

Effects move `prepared → approved → dispatching → provider_accepted | provider_pending →
confirmed_sent | failed | cancelled | outcome_unknown`. Every step is an `effect_attempt` row
with `phase`, `submitted`, `outcome_code`, `error_category`, `retry_allowed` and the provider
identifiers seen.

* **Draft-only launch.** Nothing is sent until the owner explicitly approves; there is no
  standing send authority anywhere in this repository, and `rules.py` cannot create one.
* **An approval binds:** owner, immutable draft version, sender identity, recipient/audience
  snapshot (including group membership version), destination, body hash, attachment hashes,
  purpose, operation ID, issue time and expiry.
* **Editing any bound field invalidates the approval**, and `revalidate_approval()` re-reads the
  source before dispatch, so a change to the draft *or* to the source audience (a new message
  with a different sender, changed membership) blocks dispatch until the changed version is
  approved (`T13`).
* **Double taps and concurrent clients resolve to exactly one decision.** The approval row is
  consumed with a conditional update inside the dispatch transaction; the second caller gets
  `idempotent_replay` and no second `effect_operation` (`T14`).
* **Retry policy.** Only a definite *pre-submission* failure (`submitted = 0`) may be retried,
  under the same authorization and the same idempotency key. Anything uncertain —
  `outcome_unknown`, a crash inside the dispatch window, a pending provider state — goes to
  reconciliation and is never retried blindly (`T14`, `T15`, `R15`).
* **Reconciliation reads back** and reports what the evidence supports: `confirmed_sent`,
  `provider_accepted`, `provider_pending`, `unverified`, `none`. Absence of a record after a
  crash is reported as `failed` with `verification_level = none`, not as a clean stop.
* **Receipts state their limitations.** "The source recorded the send locally; delivery
  confirmation is unavailable from this source" is a real receipt in the mock Mail scenario —
  the product does not upgrade `accepted` into `delivered`.

## Idempotency, ordering and recovery (PRD §6, §13, R08, R14)

* **Durable dedup keys.** `event_dedup(dedup_key, projection_state, seen_count)` makes ingest
  at-least-once; replaying an event increments the counter and changes nothing else (`T08`).
* **Idempotent projection.** Re-projecting a message preserves independently-owned state
  (read/hidden/mute) and updates only what the event actually carries.
* **Cursors advance only after their updates commit.** `ingest.sync()` projects first, then
  writes the checkpoint; a failure leaves the cursor where it was, so the next poll re-reads and
  dedups rather than losing events.
* **Outbox.** Job creation and job publication are separate: a crash between commit and worker
  notification is recovered by `grace outbox`, which republishes pending rows exactly once.
* **Rule evaluation keys** make rule-driven work repeat-safe: the same rule version plus message
  version cannot create a second job (`T06`, `T07`).

---

## CLI reference

| Command | Purpose |
|---|---|
| `seed [--reset]` | load deterministic labelled fixtures |
| `needs-me`, `working`, `all`, `counts` | the three queues and independent counts (R02, §14) |
| `health`, `source-health`, `coverage` | honest service/source health and coverage (`R09`, `T09`, `T10`) |
| `assign --ws --instruction [--agent]` | assign an instruction to a named agent |
| `job --job`, `jobs [--state]` | inspect jobs, attempts, leases, transitions |
| `run --job [--stall] [--lease-seconds] [--worker]` | **MOCK** worker pass (or a deliberate stall for recovery tests) |
| `draft --draft`, `draft-revise --draft [--body] [--to]…` | drafts and new immutable versions |
| `approve --draft --operation-id [--ttl-seconds]`, `dispatch --approval` | approval and outbound operation |
| `effect --effect`, `reconcile --effect`, `retry --effect`, `cancel-effect --effect` | outbound ledger, reconciliation, policy-limited retry |
| `cancel --job`, `answer --job --text` | cancel or answer a waiting job (not an approval) |
| `sync --adapter --account [--limit]` | poll, project idempotently, advance the checkpoint |
| `rule-save`, `rule-preview`, `rule-apply`, `rule-cancel`, `rule-disable` | versioned rules with frozen preview and repeat-safe apply |
| `reap`, `outbox`, `audit` | lease reaping, publication recovery, audit trail |
| `scenarios`, `export-fixtures`, `demo` | mock scenario list, fixture export, full labelled walk |
| `serve [--host H] [--port P] [--token T] [--print-url]` | the authenticated Gate 1 review client over HTTP (see below) |

All commands accept `--db <path>` (default `.grace/grace.sqlite3`), `--scenario <name>` and
`--fault <fault>` (repeatable), so any scenario can be reproduced from the command line.

### Review client (`grace serve`)

```bash
python3 -m grace serve --port 3000 --print-url     # prints the URL to open
```

`serve` runs the Gate 1 review client — the four surfaces (Needs me, Working, All
conversations, Rules and source health) — over HTTP from the same process, against the same
SQLite database `.grace/grace.sqlite3` the CLI writes, so a decision made in the browser and
one made on the command line land in the same ledger. It is authenticated: the client needs
the bearer token the server prints/accepts, and it refuses to serve without one, because it
is a phone surface and never a public page. Drive it at a phone viewport; every document it
renders carries the mock labels (`MOCK:`, `mocked: true`) while the adapters behind it are
mock adapters.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .           # 142 tests, no third-party dependency
```

The five required scenarios:

| Required check | Test |
|---|---|
| (a) an in-flight job survives a process restart | `tests/test_restart_recovery.py` (real subprocesses: seed → assign → stalled worker → new process shows the lease → reap → requeue) |
| (b) a duplicate source event creates no duplicate job | `tests/test_duplicate_events.py` (`T08`, `T06`) |
| (c) editing a bound draft field invalidates the approval and blocks dispatch | `tests/test_approvals.py` (`T13`, `T14`, `T21`) |
| (d) `outcome_unknown` enters reconciliation and is not retried blindly | `tests/test_unreconciled_effects.py` (`T14`, `T15`, `T22`) |
| (e) every mocked value is labelled | `tests/test_mock_labelling.py` (`T20`) |

Also covered: honest disconnected/permission-denied/partial-history/token-reset states and
missing attachments (`tests/test_honest_failure_states.py`, `T09`, `T10`), crash windows inside
dispatch, cancel-after-submission, expired approvals, blocked approval on unavailable
attachments, and that no fixture contains a secret or a real endpoint.

---

## Recovery runbook

| Situation | Action |
|---|---|
| A worker died mid-job | `grace reap` (expires the lease, records the stall, closes the attempt as `outcome_unknown`); the job returns to `queued` and can be claimed again |
| Crash between job commit and worker publication | `grace outbox` republishes pending rows exactly once |
| Dispatch crashed or timed out | `grace effect <id>` to see the state; `grace reconcile --effect <id>` (never `dispatch` again for the same approval — the operation ID makes that a replay) |
| Definite pre-submission failure | `grace retry --effect <id>` (same authorization, same idempotency key); if the approved content changed, the retry is refused and must be re-approved |
| Approval invalid, expired or superseded | `grace draft-revise` then `grace approve` again — the old approval stays visible in the ledger with its invalidation reason |
| A source is offline or denied | `grace source-health` shows the typed state and the last success; `grace sync` reports the typed outcome and does not advance the cursor |
| Full restart of the service | Nothing to do: the SQLite ledger is the source of truth. Jobs, approvals, effects, receipts, dedup keys and checkpoints are all durable. |

---

## Deliberately not in this build

* No real Mail, Beeper, Contacts or Hermes adapter; no probe results (Gate 2), no real send
  (Gate 3), no phone client (Gate 3/4).
* No unattended send, no standing send authority, no mass messaging.
* No full-history mirroring of source mail; only bounded enumeration plus retrieval by reference.
* No every-account/every-channel coverage, no dashboards beyond the four surfaces, no multi-user
  or team features (PRD "deliberately out of this release").

## What Gate 2 must confirm before any of this can leave the mock

Mail: reply-as-account from a configured iCloud account, sent/draft scripting and attachment
download policy. Beeper: local API authentication, account/chat/message read, search, focus,
send and asset download on the installed build. Contacts: `CNContactStore` access plus
change-history tokens and the reset behaviour. Hermes: the installed build's API server,
sessions, runs, progress and stop. Each of those must be observed on Randy's machine and
recorded against the matching capability manifest entry before the corresponding claim moves
from `mock` to `real`.

## Provenance vocabulary (correction, 2026-10-08)

Two different claims, two different fields -- do not conflate them:

* `adapter_is_real` -- *which adapter answered*: the real one (`true`) or its recorded
  fixture twin (`false`). On this Linux computer the real adapter is selected and can
  read nothing, so real-mode documents legitimately carry `adapter_is_real: true`.
* `real_source_connected` -- *whether a real source was contacted and the reported value
  came from it*. Same meaning as everywhere else in the product (Grace's health output,
  receipts, the mock-labelling rules). It is `false` on every command, in both modes,
  whenever no source was actually read -- which is always, on this computer.

Before this correction the manifest overloaded `real_source_connected` to mean "this is
the real adapter", so `manifest` in real mode claimed `real_source_connected: true` on a
host where Mail was never contacted. That claim is withdrawn. The probe rows and the
manifest now carry both fields with the meanings above.

Also: the global flags (`--fixture-mode`, `--fixture-scenario`, `--pretty`,
`--summary-to-stderr`) may be given **before or after** the subcommand -- `probe
--fixture-mode` and `--fixture-mode probe` mean the same thing. A flag given before the
subcommand is never reset by the subcommand parser, and a test covers both positions.

If any command cannot describe itself, it prints a typed `harness_failure` document on
stdout with exit 3 -- never a stack trace.
