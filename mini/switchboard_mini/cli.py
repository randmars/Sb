"""``switchboard-mini`` — the Mini worker for Switchboard (PRD §1, §11).

Runs on Randy's Mac, reads Mail.app, and answers a capability probe. Standard library
only: macOS ships a system Python without pip guarantees, so this package imports
nothing from outside the standard library and needs no virtualenv.

Subcommands: ``probe``, ``run``, ``accounts``, ``mailboxes``, ``list``, ``fetch``,
``health``, ``manifest``, ``version``, the read-only Beeper family
``beeper token|info|introspect|search|messages|contacts|accounts|chats|probe|health``, and
the read-only Contacts family
``contacts authorization|request-access|enumerate|restricted-keys|change-history|probe|health``.

Nothing in this worker sends, drafts or modifies Mail. ``--fixture-mode`` answers from
recorded results in this repository and labels every document ``FIXTURE:``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import outcomes as O
from .beeper_adapter import build_adapter as build_beeper_adapter
from .beeper_transport import (BASE_URL_ENV, DEFAULT_BASE_URL,
                               FIXTURE_SCENARIOS as BEEPER_FIXTURE_SCENARIOS)
from .contacts_adapter import build_adapter as build_contacts_adapter
from .contacts_transport import (FIXTURE_SCENARIOS as CONTACTS_FIXTURE_SCENARIOS,
                                 compare_identifier_fingerprints, harden_token_file)
from .mail_adapter import build_adapter
from .mail_transport import FIXTURE_SCENARIOS
from .probe import CAPABILITY_NAMES, run_probe
from .runloop import default_state_path, run_loop
from .version import PROBE_CONTRACT_VERSION, WORKER_VERSION

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_HARNESS_FAILURE = 3


def _print(document: dict, pretty: bool) -> None:
    sys.stdout.write(O.emit(document, pretty=pretty) + "\n")
    sys.stdout.flush()


def _line(document: dict) -> None:
    sys.stdout.write(O.emit(document) + "\n")


def _add_global_flags_to_subparsers(sub, *, beeper_scenarios=None) -> None:
    """Accept the global flags after the subcommand as well as before it.

    Argparse rejects `probe --fixture-mode` when the top-level parser owns the flag --
    the same ordering quirk that made the documented `grace demo --pretty` exit 2. The
    runbook must not trip Randy on it, so every subcommand takes them too.
    ``default=argparse.SUPPRESS`` is what keeps this honest: the subparser only sets the
    attribute when the flag is actually present after the subcommand, so a flag written
    before the subcommand still applies when it is not repeated.
    """
    choices = list(beeper_scenarios) if beeper_scenarios else list(FIXTURE_SCENARIOS)
    for sub_parser in {id(p): p for p in sub.choices.values()}.values():
        nested = next((action for action in getattr(sub_parser, "_actions", [])
                       if isinstance(action, argparse._SubParsersAction)), None)
        if nested is not None:
            # The Beeper family nests one level deeper and takes the Beeper fixture
            # scenarios, so the same flags mean the same thing inside it -- including in
            # the `beeper info --fixture-scenario ...` position, which PR #2's flag-order
            # tolerance promises to accept.
            is_beeper = "beeper" in (getattr(sub_parser, "prog", "") or "")
            is_contacts = "contacts" in (getattr(sub_parser, "prog", "") or "")
            if is_contacts and beeper_scenarios is None:
                # The Contacts family nests too, and its scenarios are its own: offering Mail
                # scenario names inside `contacts ...` would let a Mail recording be read as a
                # Contacts one.
                _add_global_flags_to_subparsers(nested,
                                                beeper_scenarios=CONTACTS_FIXTURE_SCENARIOS)
                continue
            _add_global_flags_to_subparsers(
                nested, beeper_scenarios=(beeper_scenarios if beeper_scenarios is not None
                                          else (BEEPER_FIXTURE_SCENARIOS if is_beeper
                                                else None)))
        # Checked by dest *and* by option string: a parser may bind a differently-named
        # dest to the same flag name (the Beeper family's own --fixture-scenario), and
        # adding the flag twice is an argparse conflict.
        existing = {a.dest for a in sub_parser._actions}
        existing_opts = {opt for a in sub_parser._actions for opt in a.option_strings}
        if "fixture_mode" not in existing \
                and "--fixture-mode" not in existing_opts:
            sub_parser.add_argument("--fixture-mode", action="store_true",
                                    default=argparse.SUPPRESS,
                                    help="answer from recorded results (also accepted "
                                         "before the subcommand)")
        if "fixture_scenario" not in existing \
                and "--fixture-scenario" not in existing_opts:
            sub_parser.add_argument("--fixture-scenario", default=argparse.SUPPRESS,
                                    choices=choices,
                                    help="which recorded result to answer from (also "
                                         "accepted before the subcommand)")
        if "max_scan" not in existing and "--max-scan" not in existing_opts:
            sub_parser.add_argument("--max-scan", type=int, default=argparse.SUPPRESS,
                                    help="bounded id-scan window per mailbox (also "
                                         "accepted before the subcommand)")
        if "timeout" not in existing and "--timeout" not in existing_opts:
            sub_parser.add_argument("--timeout", type=int, default=argparse.SUPPRESS,
                                    help="osascript timeout in seconds (also accepted "
                                         "before the subcommand)")
        if "pretty" not in existing and "--pretty" not in existing_opts:
            sub_parser.add_argument("--pretty", action="store_true",
                                    default=argparse.SUPPRESS,
                                    help="indent JSON output (also accepted before the "
                                         "subcommand)")


def harness_failure_document(command: str, exc: BaseException, *,
                             fixture_mode: bool = False) -> dict:
    """A typed document for a failure that should never happen -- never a traceback.

    The worker answers with typed states, so an unexpected exception is itself a
    documented outcome: it says the run is void and no source value is claimed. It must
    not be silently retried or built on.
    """
    paths = getattr(exc, "paths", None)
    serialisation = isinstance(exc, O.SerializationError)
    return {
        "event": "harness_failure",
        "command": command,
        "state": "harness_error",
        "reason": "document_not_serialisable" if serialisation else "unexpected_exception",
        "failure_type": type(exc).__name__,
        "detail": str(exc),
        "unserialisable_paths": list(paths) if paths else None,
        "origin": O.FIXTURE if fixture_mode else O.REAL,
        "fixture_mode": bool(fixture_mode),
        "adapter_is_real": not fixture_mode,
        "source_contacted": False,
        "real_source_connected": False,
        "note": ("the worker failed to produce a well-formed answer, so this run proves "
                 "nothing about Mail and claims nothing from it. This is a defect in the "
                 "worker, not a state of the source."),
        "next_action": ("re-run the same command with --fixture-mode to see whether the "
                        "defect is host-independent, and record it with this command "
                        "line; do not build on this run"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchboard-mini",
        description="Switchboard Mini worker: read-only Mail.app access and capability probe")
    parser.add_argument("--fixture-mode", action="store_true",
                        help="answer from recorded results in this repository instead of "
                             "contacting Mail. Every document is labelled FIXTURE:")
    parser.add_argument("--fixture-scenario",
                        default="granted",
                        choices=list(FIXTURE_SCENARIOS),
                        help="which recorded result to answer from in --fixture-mode")
    parser.add_argument("--max-scan", type=int, default=2000,
                        help="bounded id-scan window per mailbox (default 2000)")
    parser.add_argument("--timeout", type=int, default=120,
                        help="osascript timeout in seconds (default 120)")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("version", help="worker version and probe contract version")

    probe = sub.add_parser("probe", help="emit one JSON row per capability")
    probe.add_argument("--account", default=None, help="Mail account label to probe")
    probe.add_argument("--mailbox", default=None, help="mailbox to probe (default INBOX)")
    probe.add_argument("--sample", type=int, default=5,
                       help="messages to sample per page (default 5)")
    probe.add_argument("--out", default=None, help="also write the rows to this file")
    probe.add_argument("--summary-to-stderr", action="store_true",
                       help="print a summary document to stderr as well")
    probe.add_argument("--mail-only", action="store_true",
                       help="measure the Mail rows only (the Beeper rows then fall back "
                            "to their documentation rows)")
    probe.add_argument("--beeper-fixture-scenario", default="healthy",
                       choices=list(BEEPER_FIXTURE_SCENARIOS),
                       help="which recorded Beeper scenario the Beeper adapter answers "
                            "from in --fixture-mode (default healthy)")
    probe.add_argument("--beeper-account-id", default=None,
                       help="Beeper accountID for the contacts row (O12); no endpoint in "
                            "the pack lists accounts, so it cannot be discovered here")
    probe.add_argument("--beeper-ui-oldest-visible", default=None,
                       help="the oldest message timestamp visible in Beeper Desktop's UI "
                            "for that account (O05 step 3): the owner's half of the "
                            "history-depth assertion")

    run = sub.add_parser("run", help="foreground poll loop with a durable cursor")
    run.add_argument("--account", default=None)
    run.add_argument("--mailbox", default=None)
    run.add_argument("--limit", type=int, default=25)
    run.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    run.add_argument("--once", action="store_true", help="one poll, then exit")
    run.add_argument("--state", default=None,
                     help=f"cursor state file (default {default_state_path()})")

    sub.add_parser("health", help="Mail.app presence, build version and reachability")
    sub.add_parser("accounts", help="enumerate Mail accounts (read-only)")

    boxes = sub.add_parser("mailboxes", help="enumerate mailboxes of an account")
    boxes.add_argument("--account", required=True)

    listing = sub.add_parser("list", help="bounded message enumeration with a cursor")
    listing.add_argument("--account", required=True)
    listing.add_argument("--mailbox", default="INBOX")
    listing.add_argument("--limit", type=int, default=5)
    listing.add_argument("--cursor", default=None)
    listing.add_argument("--since", default=None,
                         help="filter the fetched page by local timestamp (Mail has no "
                              "server-side date filter)")

    fetch = sub.add_parser("fetch", help="retrieve one message by its mail: reference")
    fetch.add_argument("--account", required=True)
    fetch.add_argument("--ref", required=True,
                       help="namespaced reference mail:<account>:<mailbox>:<internal id>")

    manifest = sub.add_parser("manifest", help="capability manifest (unprobed by default)")
    manifest.add_argument("--probe-result", default=None,
                          help="JSONL file of probe rows to fold into the manifest")

    beeper = sub.add_parser(
        "beeper", help="read-only Beeper Desktop local API (documented endpoints only)")
    # SUPPRESS on the shared dest so a flag given *before* `beeper` is not reset by the
    # copy on the subcommand (PR #2's flag-order tolerance).
    beeper.add_argument("--fixture-mode", action="store_true", default=argparse.SUPPRESS,
                        help="answer from a recorded Beeper scenario; every document is "
                             "labelled FIXTURE:")
    beeper.add_argument("--fixture-scenario", dest="beeper_fixture_scenario",
                        default="healthy", choices=list(BEEPER_FIXTURE_SCENARIOS),
                        help="which recorded Beeper scenario to answer from (the Beeper "
                             "scenarios differ from the Mail ones)")
    beeper.add_argument("--base-url", default=None,
                        help=f"API base URL (default the documented example host "
                             f"{DEFAULT_BASE_URL}; override with {BASE_URL_ENV})")
    beeper.add_argument("--timeout", type=int, default=argparse.SUPPRESS,
                        help="HTTP timeout in seconds")
    beeper.add_argument("--stand-in-server", action="store_true",
                        help="contact a local stand-in responder from this host to "
                             "exercise the wire layer. Every document is marked "
                             "stand_in and can never claim a real source")
    beeper.add_argument("--account-id", default=None,
                        help="Beeper accountID (required by the contacts path, O12)")
    beeper.add_argument("--limit", type=int, default=20,
                        help="page size (search max 20, O06; contacts max 200, O12)")
    beeper.add_argument("--cursor", default=None,
                        help="the opaque cursor a previous page returned")
    beeper.add_argument("--direction", default="before", choices=["after", "before"],
                        help="cursor direction (O06/O12: after = newer, before = older)")
    beeper.add_argument("--sender", default=None, help="search filter: sender (O06)")
    beeper.add_argument("--chat-id", default=None,
                        help="restrict to one chat via the documented chatIDs filter (O06)")
    beeper.add_argument("--ui-oldest-visible", default=None,
                        help="the oldest message timestamp visible in Beeper Desktop's UI "
                             "for this account (O05 step 3): the owner's half of the "
                             "history-depth assertion")
    beeper.add_argument("--include-low-priority", action="store_true",
                        help="send excludeLowPriority=false (O06 documents the default as "
                             "true, so it is only sent when asked for)")
    beeper.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                        help="indent JSON output")
    beeper_sub = beeper.add_subparsers(dest="beeper_command")
    beeper_sub.add_parser("token", help="is a token configured? (contacts nothing)")
    beeper_sub.add_parser("info", help="GET /v1/info: reachability and server metadata")
    beeper_sub.add_parser("health", help="one health document built on /v1/info")
    beeper_sub.add_parser("introspect", help="POST /oauth/introspect: is the token active?")
    beeper_sub.add_parser("accounts", help="refused: no accounts endpoint in the pack")
    beeper_sub.add_parser("chats", help="refused: no chats endpoint in the pack")
    b_search = beeper_sub.add_parser("search", help="GET /v1/messages/search (O06)")
    b_search.add_argument("--query", default=None, help="literal-word search terms (O06)")
    b_search.add_argument("--sweep", action="store_true",
                          help="walk pages with the returned cursor until the end or the "
                               "bounded page budget is reached")
    b_messages = beeper_sub.add_parser(
        "messages", help="message listing with a bounded resumable cursor (search-based)")
    b_messages.add_argument("--query", default=None)
    b_messages.add_argument("--sweep", action="store_true")
    b_contacts = beeper_sub.add_parser(
        "contacts", help="GET /v1/accounts/{accountID}/contacts/list (O12)")
    b_contacts.add_argument("--query", default=None)
    b_probe = beeper_sub.add_parser("probe", help="emit the Beeper capability rows")
    b_probe.add_argument("--out", default=None, help="also write the rows to this file")
    b_probe.add_argument("--summary-to-stderr", action="store_true")
    contacts = sub.add_parser(
        "contacts", help="read-only Contacts access (the macOS Contacts framework)")
    # ``default=argparse.SUPPRESS``: if this carried a default it would shadow the same flag
    # written *after* the action (`contacts probe --fixture-scenario ...`), which argparse
    # attaches to the nested parser under ``dest='fixture_scenario'`` -- and every Contacts
    # command would silently answer from the default scenario instead.
    contacts.add_argument("--fixture-scenario", dest="contacts_fixture_scenario",
                          default=argparse.SUPPRESS, choices=list(CONTACTS_FIXTURE_SCENARIOS),
                          help="recorded scenario to answer from in --fixture-mode")
    contacts.add_argument("--token-file", dest="contacts_token_file", default=None,
                          help="where the change-history token lives; the documented "
                               "persistence point for a change-history token (O15). Default "
                               "~/.switchboard/contacts-history-token, mode 0600")
    contacts.add_argument("--key-symbol", dest="contacts_key_symbol", default=None,
                          help="a notes-guarded key symbol read from the installed SDK header "
                               "(no page in the pack names one, so the worker will not)")
    contacts.add_argument("--limit", dest="contacts_limit", type=int, default=25,
                          help="most contacts one bounded fetch returns (default 25)")
    contacts.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                          help="indent JSON output")
    contacts_sub = contacts.add_subparsers(dest="contacts_command")
    contacts_sub.add_parser(
        "authorization", help="the store's authorization status; never raises the consent "
                              "dialog a fetch can raise")
    contacts_sub.add_parser(
        "request-access", help="ask macOS for Contacts access (this command is the prompt)")
    contacts_sub.add_parser(
        "health", help="one health document on the shared healthy-source vocabulary")
    c_enumerate = contacts_sub.add_parser(
        "enumerate", help="bounded contact enumeration with device-local identifiers")
    c_enumerate.add_argument("--unify-off", action="store_true",
                             help="also turn unification off, if the installed framework has "
                                  "the toggle (it is checked, never assumed)")
    c_enumerate.add_argument("--limit", dest="contacts_limit", type=int, default=argparse.SUPPRESS,
                             help="most contacts one bounded fetch returns (default 25)")
    c_enumerate.add_argument("--compare-to", dest="contacts_compare", default=None,
                             help="compare this run's identifier fingerprints with a previous "
                                  "run's, to measure whether identifiers persist (mode 0600)")
    c_restricted = contacts_sub.add_parser(
        "restricted-keys", help="attempt the notes-guarded key symbol the owner read from the "
                                "SDK header, and record what happened")
    c_restricted.add_argument("--key-symbol", dest="contacts_key_symbol", default=argparse.SUPPRESS,
                              help="a notes-guarded key symbol read from the installed SDK header")
    c_history = contacts_sub.add_parser(
        "change-history", help="change-history fetch: event classes, the token, and the "
                               "documented invalid-token trigger")
    c_history.add_argument("--invalid-token", action="store_true",
                           help="use the documented reset trigger (a deliberately invalid "
                                "token) -- never reported as a genuine reset")
    c_history.add_argument("--token-file", dest="contacts_token_file", default=argparse.SUPPRESS,
                           help="where the change-history token lives (mode 0600)")
    c_history.add_argument("--include-group-changes", action="store_true",
                           help="include group changes (O15 documents the default as NO)")
    c_probe = contacts_sub.add_parser("probe", help="emit the five Contacts capability rows")
    c_probe.add_argument("--limit", dest="contacts_limit", type=int, default=argparse.SUPPRESS,
                         help="most contacts one bounded fetch returns (default 25)")
    c_probe.add_argument("--out", default=None, help="also write the rows to this file")
    c_probe.add_argument("--summary-to-stderr", action="store_true")
    _add_global_flags_to_subparsers(sub)
    return parser


def _adapter(args):
    return build_adapter(fixture_mode=args.fixture_mode,
                         fixture_scenario=args.fixture_scenario,
                         max_scan=args.max_scan, timeout_s=args.timeout)


def _beeper_adapter(args):
    """The Beeper read adapter, in fixture mode or for real. One place decides."""
    return build_beeper_adapter(
        fixture_mode=bool(getattr(args, "fixture_mode", False)),
        # Only the Beeper scenarios are valid here: `--fixture-scenario` before the
        # subcommand carries a Mail scenario name, and silently reinterpreting it as a
        # Beeper one would either crash or answer from the wrong recording.
        fixture_scenario=getattr(args, "beeper_fixture_scenario", None) or "healthy",
        base_url=getattr(args, "base_url", None),
        timeout_s=int(getattr(args, "timeout", 10) or 10),
        stand_in=bool(getattr(args, "stand_in_server", False)))


def _contacts_scenario(args) -> str:
    """The Contacts fixture scenario, tolerating the flag argparse may have nested.

    ``--fixture-scenario`` is added to subcommands that lack it (with ``dest``
    ``fixture_scenario``); a Contacts subcommand can carry either name. Only a value that is
    actually a Contacts scenario is honoured, so a Mail scenario name can never be
    reinterpreted as a Contacts one.
    """
    for name in ("contacts_fixture_scenario", "fixture_scenario"):
        value = getattr(args, name, None)
        if value in CONTACTS_FIXTURE_SCENARIOS:
            return value
    return "authorization_granted"


def _contacts_options(args) -> dict:
    return {"token_file": getattr(args, "contacts_token_file", None),
            "key_symbol": getattr(args, "contacts_key_symbol", None),
            "identifier_file": getattr(args, "contacts_compare", None),
            "limit": getattr(args, "contacts_limit", 25)}


def _contacts_adapter(args):
    """The Contacts read adapter, in fixture mode or for real. One place decides."""
    options = _contacts_options(args)
    return build_contacts_adapter(
        fixture_mode=bool(getattr(args, "fixture_mode", False)),
        fixture_scenario=_contacts_scenario(args),
        timeout_s=int(getattr(args, "timeout", 120) or 120),
        limit=int(options["limit"] or 25),
        key_symbol=options["key_symbol"],
        token_file=options["token_file"])


def _probe_adapters(args):
    """Every source adapter this worker ships, for one probe run.

    One run, one row per capability key: the Mail adapter measures the Mail rows, the
    Beeper adapter measures the five Beeper rows it can, the Contacts adapter measures the
    five Contacts rows it can, and a capability none of them can measure is reported as the
    documentation read the pack records.
    """
    adapters = [_adapter(args), _beeper_adapter(args), _contacts_adapter(args)]
    if getattr(args, "mail_only", False):
        return adapters[:1]
    return adapters


def _read_probe_rows(path: str) -> list:
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                import json
                rows.append(json.loads(line))
    return rows


def cmd_probe(args) -> int:
    adapters = _probe_adapters(args)
    adapter = adapters[0]
    run = run_probe(adapters, sample=args.sample, account=args.account,
                    mailbox=args.mailbox, max_scan=args.max_scan,
                    beeper_account_id=getattr(args, "beeper_account_id", None),
                    beeper_ui_oldest_visible=getattr(args, "beeper_ui_oldest_visible",
                                                     None),
                    contacts=_contacts_options(args))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for row in run.rows:
                handle.write(O.emit(row) + "\n")
    for row in run.rows:
        _line(row)
    if args.summary_to_stderr:
        sys.stderr.write(O.emit({
            "event": "probe_summary", "origin": adapter.origin,
            "adapter_is_real": bool(adapter.adapter_is_real),
            "fixture_mode": bool(args.fixture_mode),
            "label": getattr(adapter, "_label", lambda: None)(),
            "adapters_in_run": [
                {"adapter": getattr(a, "name", type(a).__name__),
                 "origin": a.origin, "adapter_is_real": bool(a.adapter_is_real),
                 "label": getattr(a, "_label", lambda: None)()} for a in adapters],
            **run.summary()}) + "\n")
    if not run.ok:
        sys.stderr.write(O.emit({
            "event": "probe_harness_failure",
            "detail": "the probe harness failed while producing some rows; the rows above "
                      "are still reported, but the run is not a valid measurement",
            "errors": run.harness_errors}) + "\n")
        return EXIT_HARNESS_FAILURE
    return EXIT_OK


def cmd_run(args) -> int:
    adapter = _adapter(args)
    return run_loop(adapter, account=args.account, mailbox=args.mailbox,
                    limit=args.limit, interval=args.interval, once=args.once,
                    state_path=args.state)


def cmd_health(args) -> int:
    adapter = _adapter(args)
    outcome = adapter.health(None)
    _print(outcome.to_dict(), args.pretty)
    return EXIT_OK if outcome.usable else EXIT_OK     # a typed state is a valid answer


def cmd_accounts(args) -> int:
    adapter = _adapter(args)
    _print(adapter.accounts().to_dict(), args.pretty)
    return EXIT_OK


def cmd_mailboxes(args) -> int:
    adapter = _adapter(args)
    _print(adapter.mailboxes(args.account).to_dict(), args.pretty)
    return EXIT_OK


def cmd_list(args) -> int:
    from .mail_adapter import scope_ref

    adapter = _adapter(args)
    outcome = adapter.enumerate(args.account, scope_ref(args.mailbox), limit=args.limit,
                                cursor=args.cursor, since=args.since)
    _print(outcome.to_dict(), args.pretty)
    return EXIT_OK


def cmd_fetch(args) -> int:
    adapter = _adapter(args)
    _print(adapter.retrieve(args.account, args.ref).to_dict(), args.pretty)
    return EXIT_OK


def cmd_manifest(args) -> int:
    adapter = _adapter(args)
    rows = _read_probe_rows(args.probe_result) if args.probe_result else None
    _print(adapter.manifest(rows), args.pretty)
    return EXIT_OK


def cmd_contacts(args) -> int:
    """The Contacts read family. Read-only: nothing here writes to Contacts.

    Every method behind these commands refuses before constructing a fetch when the
    authorization status is not ``authorized``, so a read can never raise the consent dialog;
    ``request-access`` is the one command whose whole purpose is that dialog.
    """
    adapter = _contacts_adapter(args)
    action = getattr(args, "contacts_command", None)
    if action is None:
        sys.stderr.write("usage: switchboard-mini contacts "
                         "authorization|request-access|enumerate|restricted-keys|"
                         "change-history|probe|health\n")
        return EXIT_USAGE
    if action == "authorization":
        _print(adapter.authorization().to_dict(), args.pretty)
        return EXIT_OK
    if action == "request-access":
        _print(adapter.request_access().to_dict(), args.pretty)
        return EXIT_OK
    if action == "health":
        _print(adapter.health().to_dict(), args.pretty)
        return EXIT_OK
    if action == "enumerate":
        outcome = adapter.enumerate_contacts(unify_off=bool(getattr(args, "unify_off", False)))
        data = dict(outcome.data or {})
        outcome.data = data
        if outcome.usable:
            current = [item.get("identifier_fingerprint") for item in data.get("items") or []
                       if item.get("identifier_fingerprint")]
            path = getattr(args, "contacts_compare", None)
            data["identifier_comparison"] = compare_identifier_fingerprints(
                path, current, write=bool(path and adapter.adapter_is_real))
            if not adapter.adapter_is_real:
                data["identifier_comparison"].setdefault(
                    "persistence_comparison_file", path)
        _print(outcome.to_dict(), args.pretty)
        return EXIT_OK
    if action == "restricted-keys":
        _print(adapter.restricted_keys(getattr(args, "contacts_key_symbol", None)).to_dict(),
               args.pretty)
        return EXIT_OK
    if action == "change-history":
        outcome = adapter.change_history(
            invalid_token=bool(getattr(args, "invalid_token", False)))
        data = dict(outcome.data or {})
        if outcome.usable:
            # A real run wrote the token; it is a handle on the contact database, so it is
            # owner-only. The value is never printed, only its length and fingerprint.
            mode = harden_token_file(getattr(args, "contacts_token_file", None)
                                     or data.get("token_file"))
            if mode:
                data["token_file_mode"] = mode
        outcome.data = data
        _print(outcome.to_dict(), args.pretty)
        return EXIT_OK
    if action == "probe":
        run = run_probe([adapter], only_source="contacts",
                        contacts={"token_file": getattr(args, "contacts_token_file", None),
                                  "key_symbol": getattr(args, "contacts_key_symbol", None),
                                  "identifier_file": getattr(args, "contacts_compare", None),
                                  "limit": getattr(args, "contacts_limit", 25)})
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                for row in run.rows:
                    handle.write(O.emit(row) + "\n")
        for row in run.rows:
            _line(row)
        if getattr(args, "summary_to_stderr", False):
            sys.stderr.write(O.emit({
                "event": "probe_summary", "origin": adapter.origin,
                "adapter_is_real": bool(adapter.adapter_is_real),
                "adapter": adapter.name,
                "label": adapter._label(), **run.summary()}) + "\n")
        if not run.ok:
            sys.stderr.write(O.emit({"event": "probe_harness_errors",
                                     "errors": run.harness_errors}) + "\n")
            return EXIT_HARNESS_FAILURE
        return EXIT_OK
    return EXIT_USAGE


def cmd_beeper(args) -> int:
    """The Beeper read family. Read-only: nothing here sends, drafts or focuses."""
    adapter = _beeper_adapter(args)
    action = args.beeper_command
    if action == "token":
        _print(adapter.token_status().to_dict(), args.pretty)
        return EXIT_OK
    if action == "info":
        _print(adapter.info().to_dict(), args.pretty)
        return EXIT_OK
    if action == "health":
        _print(adapter.health().to_dict(), args.pretty)
        return EXIT_OK
    if action == "introspect":
        _print(adapter.introspect().to_dict(), args.pretty)
        return EXIT_OK
    if action in ("search", "messages"):
        scope = {"query": getattr(args, "query", None),
                 "limit": args.limit,
                 "cursor": getattr(args, "cursor", None),
                 "direction": args.direction,
                 "sender": getattr(args, "sender", None),
                 "include_low_priority": args.include_low_priority,
                 "sweep": getattr(args, "sweep", False)}
        chat_id = getattr(args, "chat_id", None)
        if chat_id:
            outcome = adapter.messages_in_chat(chat_id, **scope)
        else:
            outcome = adapter.search(**scope)
        _print(outcome.to_dict(), args.pretty)
        return EXIT_OK
    if action == "contacts":
        _print(adapter.contacts(args.account_id, limit=args.limit,
                                cursor=getattr(args, "cursor", None),
                                direction=args.direction,
                                query=getattr(args, "query", None)).to_dict(),
               args.pretty)
        return EXIT_OK
    if action == "accounts":
        _print(adapter.accounts().to_dict(), args.pretty)
        return EXIT_OK
    if action == "chats":
        _print(adapter.chats().to_dict(), args.pretty)
        return EXIT_OK
    if action == "probe":
        run = run_probe([adapter], beeper_account_id=args.account_id,
                        beeper_ui_oldest_visible=getattr(args, "ui_oldest_visible", None),
                        only_source="beeper")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                for row in run.rows:
                    handle.write(O.emit(row) + "\n")
        for row in run.rows:
            _line(row)
        if getattr(args, "summary_to_stderr", False):
            sys.stderr.write(O.emit({
                "event": "probe_summary", "origin": adapter.origin,
                "adapter_is_real": bool(adapter.adapter_is_real),
                "adapter": adapter.name,
                "label": adapter._label(), **run.summary()}) + "\n")
        if not run.ok:
            sys.stderr.write(O.emit({
                "event": "probe_harness_failure",
                "detail": "the Beeper probe harness failed while producing some rows; the "
                          "rows above are still reported, but the run is not a valid "
                          "measurement",
                "errors": run.harness_errors}) + "\n")
            return EXIT_HARNESS_FAILURE
        return EXIT_OK
    return EXIT_USAGE


def main(argv: Optional[list] = None) -> int:
    if not O.python_supported():
        sys.stderr.write("switchboard-mini needs Python 3.9 or newer; this is "
                         f"{sys.version.split()[0]}\n")
        return EXIT_USAGE
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    if args.command == "version":
        _print({"worker": "switchboard-mini", "worker_version": WORKER_VERSION,
                "probe_contract_version": PROBE_CONTRACT_VERSION,
                "capabilities": list(CAPABILITY_NAMES),
                "python": sys.version.split()[0],
                "standard_library_only": True,
                "fixture_scenarios": list(FIXTURE_SCENARIOS),
                "beeper_fixture_scenarios": list(BEEPER_FIXTURE_SCENARIOS),
                "contacts_fixture_scenarios": list(CONTACTS_FIXTURE_SCENARIOS)}, args.pretty)
        return EXIT_OK
    handlers = {"probe": cmd_probe, "run": cmd_run, "health": cmd_health,
                "accounts": cmd_accounts, "mailboxes": cmd_mailboxes, "list": cmd_list,
                "fetch": cmd_fetch, "manifest": cmd_manifest, "beeper": cmd_beeper,
                "contacts": cmd_contacts}
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception as exc:
        # The worker answers with typed states, so an unexpected exception is reported as
        # one: exit 3 (harness failure), a well-formed JSON document on stdout naming the
        # defect, and never a traceback. Nothing here claims a source value.
        return _report_harness_failure(args, exc)


def _report_harness_failure(args, exc: BaseException) -> int:
    document = harness_failure_document(args.command, exc,
                                       fixture_mode=bool(getattr(args, "fixture_mode",
                                                                 False)))
    try:
        _print(document, bool(getattr(args, "pretty", False)))
    except Exception:                 # the guard must not fail in its own right
        sys.stdout.write(json.dumps({
            "event": "harness_failure",
            "command": str(getattr(args, "command", "")),
            "state": "harness_error",
            "reason": "harness_failure_document_not_serialisable",
            "failure_type": type(exc).__name__,
            "detail": "the worker could not describe its own failure; see stderr",
            "real_source_connected": False,
        }, sort_keys=True) + "\n")
    sys.stderr.write(
        f"switchboard-mini {getattr(args, 'command', '')}: harness failure "
        f"({type(exc).__name__}): {exc}\n"
        "This is a defect in the worker, not a state of Mail. The JSON document above "
        "records it; no source value was read.\n")
    return EXIT_HARNESS_FAILURE


__all__ = ["main", "build_parser", "CAPABILITY_NAMES", "WORKER_VERSION",
           "harness_failure_document", "FIXTURE_SCENARIOS", "BEEPER_FIXTURE_SCENARIOS",
           "cmd_beeper"]
