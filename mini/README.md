# switchboard-mini — the Mini worker (Gate 2, slice 1)

The Mini role in Switchboard is Randy's always-on Mac: it reads Mail, Beeper and Contacts
and performs approved Mac-bound actions (PRD §1). This directory is slice 1 of that worker:
an installable package, a **read-only Mail.app adapter**, and the **capability probe** whose
rows record what Randy's actual Mac can do (PRD §6, §11).

**Status, stated plainly.** Everything in this directory was executed on the Linux
computer that stands in for Grace, against recorded fixtures and against this host. Mail.app
itself cannot run here. Where a value in this README describes macOS behaviour that has not
been observed on Randy's Mac, it says so and is marked *(confirm on the Mac)*.

---

## What slice 1 does and does not do

| | |
|---|---|
| Reads Mail.app | yes — AppleScript through `/usr/bin/osascript`, read-only |
| Accounts, mailboxes, bounded message listing, historical iteration with a cursor, headers, body, raw source attempt, attachment metadata | yes |
| Sends, drafts, attachment download/materialisation | **no** — `unsupported` with a reason; there is no outbound path in this worker |
| Modifies Mail state | never. No read flag, no move, no delete, no composer |
| Runs without Mail | yes: `--fixture-mode` answers from recorded results and labels every document `FIXTURE:` |

## Install (one command)

```sh
sh mini/install.sh              # links switchboard-mini into ~/.local/bin
sh mini/install.sh --prefix /usr/local    # needs sudo for that directory
sh mini/install.sh --uninstall
```

Nothing is downloaded or compiled. The worker imports the **standard library only** — no
pip, no virtualenv, no third-party package — because macOS ships a system Python without
pip guarantees. `install.sh` checks for a Python 3.9+ interpreter and links the launcher;
if `python3` is missing it says to run `xcode-select --install`.

Run it from the checkout, or with `PYTHONPATH=<repo>/mini python3 -m switchboard_mini ...`.

## Permissions to grant *(confirm every UI path on the Mac — none of these screens has been seen here)*

The worker talks to Mail through Apple events. macOS refuses that until Randy allows it, and
the refusal arrives as Apple event error **-1743** ("Not authorized to send Apple events to
Mail"). The grant lives in **System Settings → Privacy & Security → Automation**: the entry
for the app that runs the worker (Terminal, or whatever launches `switchboard-mini`) must have
**Mail** switched on. *The exact pane and entry name must be confirmed on the Mac in Gate 2;
Apple moves these panes between releases.*

Two things to know before granting anything:

* Run the probe **first**. Its output is what Gate 2 records as evidence, and a denied
  permission is a perfectly good answer. It changes nothing in Mail: no read flag, no move, no
  delete, no composer, no draft, no file. It is **not** inert, though — the `health` row asks the
  Apple event system about Mail.app, and on a Mac where Mail is not running that call is what can
  **launch Mail.app**. Whether `application "Mail" is running` answers without launching it is
  one of the things the probe measures on your Mac, not something this repository can settle.
* This worker never asks for Full Disk Access, never touches Mail's private databases
  (`Envelope Index`, `.emlx`) and never writes to Mail. If Mail's AppleScript terminology
  turns out to be insufficient for something, the row says `unsupported` — that is the
  intended outcome, not a gap to paper over (PRD §11).

## Run the probe

```sh
switchboard-mini probe --out probe-rows.jsonl          # real Mail.app on this Mac
switchboard-mini probe --account "iCloud" --mailbox INBOX --sample 5
switchboard-mini probe --pretty                        # indented JSON, still one row each
switchboard-mini --fixture-mode probe                  # no Mail needed (see below)
```

One JSON object per line, one line per capability — the PRD §11 manifest contract:

```json
{"capability":"account_enumeration","supported":true,"permission_state":"granted",
 "observed_version":"16.0","state":"success","origin":"real","values_from_source":true,
 "probe_method":"AppleScript: repeat over `accounts` of Mail, ...",
 "probe_assertion":"every configured account is returned with at least one ...",
 "limitation":null,"evidence":{"count":3,...}}
```

| Field | Meaning |
|---|---|
| `capability` | stable name of the probed capability |
| `supported` | **true only when this run observed it working.** A capability the probe cannot confirm stays `false` |
| `permission_state` | `granted` / `denied` / `not_determined` / `not_applicable` |
| `observed_version` | the version actually seen (Mail.app's build from its bundle, or the worker itself) |
| `probe_method` | how the worker tried to establish this |
| `probe_assertion` | what `supported: true` claims was observed |
| `limitation` | what is known not to hold, or why the row is unsupported |
| `evidence` | what was actually observed |
| `state` | the typed outcome of this capability: `success`, `partial`, `unsupported`, `permission_denied`, `offline`, `rate_limited`, `retryable_error`, `permanent_error`, `outcome_unknown` — or `unmeasured`, meaning the capability's own assertion was never evaluated (a documentation read, a single-page mailbox, a message with no attachments) |
| `origin` | `real` **only** when this run produced the row on this host; `fixture` means a recorded scenario in this repository; `documentation` means a page read from the Gate 2 probe pack and nothing was contacted |
| `title` | the capability in words, for the review surfaces |
| `source` | which product the capability belongs to (`mail`, `beeper`, `contacts`, `hermes`) |
| `citations` | the probe-pack records behind a `documentation` row (`O01`–`O22`); empty on every measured row |
| `probed_at` | when this row was produced |
| `probe_contract_version` | the row contract this row was written against |
| `observed_version_reason` | why `observed_version` is `not_observed`, when it is |
| `values_from_source` | true only when the capability answered with data (success/partial) rather than a typed refusal |
| `adapter_is_real` | which adapter answered: the real one (`true`) or its fixture twin (`false`) |
| `real_source_connected` | whether a real source was contacted and the value came from it. `false` on every command on any host that did not read a source |
| `label` / `disclaimer` | present whenever `origin` is not `real`: `FIXTURE:` for a recorded scenario, `DOCUMENTATION:<source>(O##)` for a page read |

`supported` says the capability exists; `state` says what this probe saw. A row can be
`supported: true` with `state: offline` — Mail is installed but not running.

**Exit status.** `0` means the probe ran. `3` means the harness itself failed (an unexpected
exception while producing rows — the rows are still printed, and the failure is written to
stderr as `probe_harness_failure`). Unsupported capabilities are a *successful* probe run:
they are the answer, not a failure.

### Reading the output

```sh
jq -r 'select(.supported==false) | "\(.capability): \(.state) — \(.limitation)"' probe-rows.jsonl
jq -r 'select(.values_from_source) | .capability' probe-rows.jsonl
```

### What to do when a probe reports `permission_denied`

The row's `limitation` and `evidence.next_action` carry the Apple event code and the fix.
`-1743` means Automation access to Mail was refused. Grant it (permissions section above) and
re-run the probe; the rows will change to `granted`. Until then every read capability stays
`supported: false` — that is the honest answer, and it is what Gate 2 records.

### What to do when a probe reports `unsupported`

Read `evidence.reason`:

| reason | what it means | what to do |
|---|---|---|
| `host_not_macos` | not running on macOS | run it on the Mac; this is the only host limitation that is not about Mail |
| `event_not_handled_by_mail_terminology` / `raw_source_unsupported` / `attachment_enumeration_unsupported` | this Mail build does not answer that AppleScript | record it as a limitation on the capability row; do **not** invent a workaround, and do not build on private Mail databases (PRD §11) |
| `read_only_slice_1` | a deliberate absence in this slice (send, draft, reconcile, attachment bytes) | nothing to fix here — these arrive in a later slice with their own approval contract |
| `unclassified_automation_error` | an error number not in the mapping table | the RAW code and stderr are in `evidence`; add it to `switchboard_mini/applescript.py` `AE_ERROR_MAP` **with the evidence from that run**. The worker classifies unknown codes as `permanent_error` and records them rather than guessing they are transient |

## Fixture mode (how this was tested off a Mac)

```sh
switchboard-mini --fixture-mode probe
switchboard-mini --fixture-mode --fixture-scenario permission_denied probe
switchboard-mini --fixture-mode --fixture-scenario offline probe
switchboard-mini --fixture-mode --fixture-scenario partial_history probe
switchboard-mini --fixture-mode list --account "FIXTURE Account A" --mailbox INBOX --limit 2
```

The four recorded scenarios live in `switchboard_mini/fixtures/mail/*.json`. They are
**synthetic**: invented data on reserved `.test` domains in the shape the transport returns.
They are not a recording of Randy's mailbox, and every document produced from them carries a
`FIXTURE:` label, `real_source_connected: false` and a disclaimer. The permission-denied and
offline scenarios reproduce the exact `osascript` failure text for `-1743` and `-600`, so the
same classification code that runs on the Mac is exercised by the tests here.

## Commands

| command | what it does |
|---|---|
| `probe` | one JSON row per capability (above). Exit 3 on harness failure |
| `run [--once] [--interval S] [--state FILE]` | foreground poll loop with a durable cursor |
| `health` | Mail.app presence, build version, reachability (typed outcome) |
| `accounts` / `mailboxes --account X` | read-only inventory |
| `list --account X --mailbox INBOX --limit N [--cursor C] [--since T]` | one bounded page plus a resume cursor |
| `fetch --account X --ref mail:<account>:<mailbox>:<id>` | headers, body, attachment metadata for one message |
| `manifest [--probe-result FILE]` | capability manifest; without probe rows every capability is `unverified` |
| `version` | worker and probe-contract versions |

### `run` and launchd

`run` is launchd-shaped: it stays in the **foreground**, writes one JSON document per line to
stdout, never forks and never daemonises. `--once` performs a single poll, which is what the
tests use. `mini/launchd/org.switchboard.mini.plist.template` is a starting point for a
LaunchAgent; its paths and the `ProgramArguments` must be adjusted on the Mac and the plist
itself has **not** been loaded anywhere — treat it as a draft, not a tested artifact.

Cursor discipline, which is the part that matters:

* the cursor is written to the state file (default `~/.switchboard-mini/state.json`,
  `--state` or `$SWITCHBOARD_MINI_STATE` to override) only after a read that **succeeded**;
* a typed failure (`offline`, `permission_denied`, `retryable_error`, …) leaves the stored
  cursor untouched, so nothing is silently skipped;
* if the mailbox changes under a cursor, the adapter reports `partial` with
  `cursor_reset: true` and the consumer must treat the overlap idempotently;
* a cursor from another account or mailbox is refused outright.

## How the Mail adapter reads

AppleScript in `switchboard_mini/mail_transport.py`, one script per operation, run through
`/usr/bin/osascript`:

* **Bounded id scan.** Mail has no server-side cursor, so `message_ids` re-reads the mailbox's
  internal ids with `id of message i`, bounded by `--max-scan` (default 2000), and reports the
  mailbox's true count beside the scanned count. A capped scan is `partial` with
  `coverage.coverage_state = 'partial_history'` and a `gap_reason`: reaching the end of a scan
  is not reaching the end of the mailbox (PRD §6).
* **Metadata window.** One osascript call per contiguous index range reads subject, sender,
  read status, RFC Message-ID, the date (text plus components) and to/cc/bcc counts.
* **Sentinel reads.** `content`, `source` and attachment enumeration are wrapped so that "the
  property could not be read" returns a sentinel rather than an empty string. An unreadable
  body is `partial` with `body_state: "unavailable"`; a genuinely empty message is `success`
  with `body_state: "empty"`. They are never conflated.
* **Addresses are never emitted in full.** Probe evidence and worker output carry masked
  senders and SHA-256 fingerprints; account email addresses are reported as counts. Grace
  needs the real address for identity linking in the Gate 3 ingest slice — that is Randy's
  call to record, and it is flagged, not silently taken.
* **Dates are local, and say so.** Mail renders dates in the Mini's timezone and AppleScript
  returns no offset, so the worker labels the stamp `time_basis: "local_time_on_mini"` with
  `utc_offset_minutes: null` instead of inventing a UTC value.

### What only Randy's Mac can verify (Gate 2, not proven here)

* that this AppleScript terminology works on the installed Mail build — every property above
  is a measurement the probe takes, not a promise (PRD §11: "terminology varies by application
  version");
* the Mail bundle path (`/System/Applications/Mail.app` is the first candidate tried, then
  `/Applications/Mail.app`) and its version string;
* the Automation permission grant and the exact System Settings pane;
* whether `application "Mail" is running` answers without launching Mail;
* that Mail's AppleScript `id`, `sender`, `mime type`, `downloaded`, `content` and `source`
  properties behave as scripted, and what their failure modes look like;
* populated-mailbox latency and whether a bounded id scan is fast enough to poll;
* the ordering of Mail's message indices (the probe samples first/last dates so the ordering
  is measured rather than assumed);
* that `mailbox "X" of account "Y"` resolves unambiguously when two accounts share a mailbox
  name (flagged as an open risk; the addressing strategy is name+account).

## Tests

```sh
python3 -m unittest discover -s tests -t .        # from the repository root
```

The whole suite is **255 tests** in a fresh checkout; the number to trust is whatever that
command prints, so run it rather than believing this line. The Mini tests are
`tests/test_mini_probe.py` (the row contract, the typed states, the labelling of every row by
what it actually contacted, and the citations), `tests/test_mini_mail.py` (the Mail adapter
against the four recorded scenarios) and `tests/test_mini_cli.py` (every command, in both
modes, as a real process). `tests/test_probe_import.py` drives Grace's side: a probe run
imports row by row, and one over-claiming row refuses the whole import. The Mini tests run
entirely off recorded fixtures on any platform and assert the fixture labelling, the typed
failure states, cursor resumption, partial-history reporting, the retrieval-miss distinction,
and that every mutating operation is `unsupported`.

## Recovery notes

| Situation | Action |
|---|---|
| `permission_denied` after an OS update | Automation grants are per-app and can reset. Re-grant, re-run `probe` |
| `offline` | Mail is not running. Launch it; the cursor is untouched, so nothing is missed |
| `retryable_error` / script timeout | Lower `--sample`, raise `--timeout`, or reduce `--max-scan`; the cursor is untouched |
| `unclassified_automation_error` | read `evidence.ae_code` and the raw stderr in `evidence`, add the code to `AE_ERROR_MAP` with that evidence |
| cursor looks wrong | delete the scope entry in the state file; the next poll starts from the beginning and re-reading is idempotent |
| the state file is corrupt | the worker treats it as empty and starts over; it never half-writes (temp file + `os.replace`) |

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
