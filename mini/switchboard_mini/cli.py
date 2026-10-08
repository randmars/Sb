"""``switchboard-mini`` — the Mini worker for Switchboard (PRD §1, §11).

Runs on Randy's Mac, reads Mail.app, and answers a capability probe. Standard library
only: macOS ships a system Python without pip guarantees, so this package imports
nothing from outside the standard library and needs no virtualenv.

Subcommands: ``probe``, ``run``, ``accounts``, ``mailboxes``, ``list``, ``fetch``,
``health``, ``manifest``, ``version``.

Nothing in this worker sends, drafts or modifies Mail. ``--fixture-mode`` answers from
recorded results in this repository and labels every document ``FIXTURE:``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import outcomes as O
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


def _add_global_flags_to_subparsers(sub) -> None:
    """Accept the global flags after the subcommand as well as before it.

    Argparse rejects `probe --fixture-mode` when the top-level parser owns the flag --
    the same ordering quirk that made the documented `grace demo --pretty` exit 2. The
    runbook must not trip Randy on it, so every subcommand takes them too.
    ``default=argparse.SUPPRESS`` is what keeps this honest: the subparser only sets the
    attribute when the flag is actually present after the subcommand, so a flag written
    before the subcommand still applies when it is not repeated.
    """
    for sub_parser in {id(p): p for p in sub.choices.values()}.values():
        existing = {a.dest for a in sub_parser._actions}
        if "fixture_mode" not in existing:
            sub_parser.add_argument("--fixture-mode", action="store_true",
                                    default=argparse.SUPPRESS,
                                    help="answer from recorded results (also accepted "
                                         "before the subcommand)")
        if "fixture_scenario" not in existing:
            sub_parser.add_argument("--fixture-scenario", default=argparse.SUPPRESS,
                                    choices=list(FIXTURE_SCENARIOS),
                                    help="which recorded result to answer from (also "
                                         "accepted before the subcommand)")
        if "max_scan" not in existing:
            sub_parser.add_argument("--max-scan", type=int, default=argparse.SUPPRESS,
                                    help="bounded id-scan window per mailbox (also "
                                         "accepted before the subcommand)")
        if "timeout" not in existing:
            sub_parser.add_argument("--timeout", type=int, default=argparse.SUPPRESS,
                                    help="osascript timeout in seconds (also accepted "
                                         "before the subcommand)")
        if "pretty" not in existing:
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
    _add_global_flags_to_subparsers(sub)
    return parser


def _adapter(args):
    return build_adapter(fixture_mode=args.fixture_mode,
                         fixture_scenario=args.fixture_scenario,
                         max_scan=args.max_scan, timeout_s=args.timeout)


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
    adapter = _adapter(args)
    run = run_probe(adapter, sample=args.sample, account=args.account,
                    mailbox=args.mailbox, max_scan=args.max_scan)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for row in run.rows:
                handle.write(O.emit(row) + "\n")
    for row in run.rows:
        _line(row)
    if args.summary_to_stderr:
        sys.stderr.write(O.emit({"event": "probe_summary", "origin": adapter.origin,
                                 "adapter_is_real": bool(adapter.adapter_is_real),
                                 "fixture_mode": bool(args.fixture_mode),
                                 "label": getattr(adapter, "_label", lambda: None)(),
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
                "fixture_scenarios": list(FIXTURE_SCENARIOS)}, args.pretty)
        return EXIT_OK
    handlers = {"probe": cmd_probe, "run": cmd_run, "health": cmd_health,
                "accounts": cmd_accounts, "mailboxes": cmd_mailboxes, "list": cmd_list,
                "fetch": cmd_fetch, "manifest": cmd_manifest}
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
           "harness_failure_document", "FIXTURE_SCENARIOS"]
