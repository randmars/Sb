"""Headless CLI for the Grace service foundation.

Every command prints one stable JSON document. Keys are sorted, the structure of a
result never changes based on state, and any output that includes mocked values
carries ``"mocked": true`` plus the MOCK disclaimer, so a mock can never be read as
a real send or a real read.

Typical walk (see README):

    python3 -m grace seed --reset
    python3 -m grace needs-me
    python3 -m grace assign --ws <id> --instruction "..." --agent researcher
    python3 -m grace run --job <id>
    python3 -m grace approve --draft <id> --operation-id op-1
    python3 -m grace dispatch --approval <id>
    python3 -m grace reconcile --effect <id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import contracts as C
from .effects import InjectedFault
from .fixtures import scenario_names
from .ingest import (PROBE_DOCUMENT_WRAP, import_probe_rows,  # noqa: F401
                     probe_document_refusal, read_probe_document)
from .service import Grace
from .store import DEFAULT_DB

# Set once per CLI invocation: whether this deployment holds any mock source. Used by
# emit() so that even a list view without per-row origin columns declares itself mocked.
_STORE_CONTEXT: dict[str, Any] = {"has_mock_source": False}


def emit(command: str, *, data: Any = None, message: str = "", ok: bool = True,
         mocked: Optional[bool] = None, label: Optional[str] = None,
         extra: Optional[dict] = None) -> dict:
    if mocked is None:
        mocked = _contains_mock(data)
    if not mocked and _STORE_CONTEXT.get("has_mock_source"):
        # The output does not repeat the origin of every source row (e.g. list views), so
        # the deployment itself declares that its data came from mock sources.
        mocked = True
        label = label or C.mock_label("store")
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "command": command,
        "message": message,
        "mocked": bool(mocked),
        "data": data,
    }
    if extra:
        payload.update(extra)
    if mocked:
        payload["mock_label"] = label or C.mock_label("cli")
        payload["mock_disclaimer"] = C.MOCK_DISCLAIMER
    problems = C.find_unlabelled_mock(payload)
    if problems:
        # A mock value without a MOCK label is a labelling defect: fail loudly.
        payload["ok"] = False
        payload["labelling_problems"] = problems
        payload["message"] = (payload["message"] + " | LABELLING DEFECT: unlabelled mock value")
    return payload


def _contains_mock(obj: Any) -> bool:
    if isinstance(obj, dict):
        if obj.get("origin") == C.MOCK or obj.get("mocked") is True:
            return True
        return any(_contains_mock(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_contains_mock(v) for v in obj)
    return False


def _print(payload: dict, pretty: bool) -> int:
    print(json.dumps(payload, sort_keys=True, indent=2 if pretty else None,
                     ensure_ascii=False, default=str))
    return 0 if payload.get("ok") else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grace", description="Switchboard Grace service foundation")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite ledger path")
    p.add_argument("--scenario", default=None, help=f"mock scenario {scenario_names()}")
    p.add_argument("--fault", action="append", default=[],
                   help="inject an honest source fault: offline | permission_denied | "
                        "partial_history | token_reset (repeatable)")
    p.add_argument("--pretty", action="store_true", help="indent the JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="load the deterministic labelled fixtures")
    s.add_argument("--reset", action="store_true", help="delete existing rows first")

    sub.add_parser("needs-me", help="Needs me queue (untriaged, returned work, blocked, failures)")
    sub.add_parser("working", help="Working queue")
    sub.add_parser("all", help="All conversations")
    sub.add_parser("counts", help="independent queue counts")
    sub.add_parser("health", help="service and source health")
    sub.add_parser("coverage", help="per-account coverage map")
    sub.add_parser("source-health", help="accounts, capabilities and honest states")
    sub.add_parser("scenarios", help="list labelled mock scenarios")
    sub.add_parser("reap", help="expire stalled job leases and make them visible")
    sub.add_parser("outbox", help="publish pending outbox messages (crash recovery)")

    a = sub.add_parser("assign", help="assign an instruction to a named agent")
    a.add_argument("--ws", required=True)
    a.add_argument("--instruction", required=True)
    a.add_argument("--agent", required=True)
    a.add_argument("--operation-id", default=None)

    j = sub.add_parser("job", help="inspect a job")
    j.add_argument("--job", required=True)

    jl = sub.add_parser("jobs", help="list jobs")
    jl.add_argument("--state", default=None)
    jl.add_argument("--limit", type=int, default=50)

    r = sub.add_parser("run", help="MOCK worker pass over a job")
    r.add_argument("--job", required=True)
    r.add_argument("--worker", default="mock-worker-1")
    r.add_argument("--lease-seconds", type=int, default=30)
    r.add_argument("--destination", default=None)
    r.add_argument("--mode", default="reply", choices=["reply", "reply_all", "new"])
    r.add_argument("--body", default=None)
    r.add_argument("--question", default=None)
    r.add_argument("--fail-with", default=None)
    r.add_argument("--stall", action="store_true",
                   help="claim the lease and stop without renewing (stall demo)")

    d = sub.add_parser("draft", help="show a draft")
    d.add_argument("--draft", required=True)

    dr = sub.add_parser("draft-revise", help="create a new immutable draft version")
    dr.add_argument("--draft", required=True)
    dr.add_argument("--body", default=None)
    dr.add_argument("--subject", default=None)
    dr.add_argument("--mode", default=None, choices=["reply", "reply_all", "new"])
    dr.add_argument("--expected-version", type=int, default=None)

    ap = sub.add_parser("approve", help="bind an approval to the current draft version")
    ap.add_argument("--draft", required=True)
    ap.add_argument("--operation-id", required=True)
    ap.add_argument("--ttl", type=int, default=900)
    ap.add_argument("--ack-limitations", action="store_true")

    di = sub.add_parser("dispatch", help="dispatch an approved draft through a mock adapter")
    di.add_argument("--approval", required=True)
    di.add_argument("--inject-crash-after", default=None,
                    choices=["commit", "submission"],
                    help="fault injection to demonstrate recovery from a crash window")

    e = sub.add_parser("effect", help="show an outbound operation and its receipts")
    e.add_argument("--effect", required=True)

    rc = sub.add_parser("reconcile", help="reconcile an uncertain or pending effect")
    rc.add_argument("--effect", required=True)

    rt = sub.add_parser("retry", help="retry a definite pre-submission failure only")
    rt.add_argument("--effect", required=True)

    ce = sub.add_parser("cancel-effect", help="cancel or mark an outbound operation unknown")
    ce.add_argument("--effect", required=True)
    ce.add_argument("--reason", default="owner cancelled")

    cj = sub.add_parser("cancel", help="cancel a job")
    cj.add_argument("--job", required=True)
    cj.add_argument("--reason", default="owner cancelled")

    an = sub.add_parser("answer", help="answer a waiting job (not an approval)")
    an.add_argument("--job", required=True)
    an.add_argument("--text", required=True)

    au = sub.add_parser("audit", help="recent audit events")
    au.add_argument("--limit", type=int, default=20)

    sy = sub.add_parser("sync", help="poll one account, project events idempotently, advance the "
                                     "checkpoint")
    sy.add_argument("--adapter", required=True)
    sy.add_argument("--account", required=True)
    sy.add_argument("--scope-ref", default=None)
    sy.add_argument("--limit", type=int, default=50)

    ex = sub.add_parser("export-fixtures",
                        help="write the labelled fixture corpus as JSON for other components")
    ex.add_argument("--out", default="fixtures")
    pi = sub.add_parser(
        "probe-import",
        help="store one Mini capability-probe run (Gate 2) against an account, refusing any "
             "row that over-claims")
    pi.add_argument("--file", required=True,
                    help="a JSON document with a 'rows' list. The Mini worker writes "
                         "JSONL, one row per line: `switchboard-mini probe --account "
                         "<label> --out rows.jsonl` — there is no `--json` flag. Wrap it "
                         f"before importing: `{PROBE_DOCUMENT_WRAP} rows.jsonl > rows.json`. "
                         "Anything else (an unwrapped JSONL run, an empty file, a bare "
                         "array, a non-JSON file) is refused with a typed reason and nothing "
                         "is stored")
    pi.add_argument("--account", required=True, help="the source_account_id these rows describe")
    pi.add_argument("--actor", default="probe-import")

    rs = sub.add_parser("rule-save", help="save a sender rule (no send authority)")
    rs.add_argument("--sender", required=True)
    rs.add_argument("--agent", default="accounts_agent")
    rs.add_argument("--instruction", default="Check the order status and draft a reply")
    rs.add_argument("--subject-contains", default=None)
    rs.add_argument("--rule-id", default=None)

    rp = sub.add_parser("rule-preview", help="freeze a preview of existing matches")
    rp.add_argument("--rule-id", required=True)
    rp.add_argument("--version", type=int, required=True)
    rp.add_argument("--limit", type=int, default=50)

    ra = sub.add_parser("rule-apply", help="apply a frozen preview (repeat-safe)")
    ra.add_argument("--rule-run", required=True)
    ra.add_argument("--stop-after", type=int, default=None)

    rc2 = sub.add_parser("rule-cancel", help="cancel a historical run")
    rc2.add_argument("--rule-run", required=True)
    rc2.add_argument("--reason", default="owner cancelled")

    rd = sub.add_parser("rule-disable", help="disable a rule version")
    rd.add_argument("--rule-id", required=True)
    rd.add_argument("--version", type=int, required=True)
    rd.add_argument("--cancel-pending", action="store_true")

    dm = sub.add_parser("demo", help="walk instruction -> job -> draft -> approval -> mock "
                                     "dispatch -> reconciled receipt")
    dm.add_argument("--include-mail-leg", action="store_true", default=True)

    sv = sub.add_parser("serve", help="serve the authenticated Gate 1 review client (PRD §5 "
                                      "surfaces) over HTTP")
    sv.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1: reachable from this computer only; "
                         "use 0.0.0.0 to reach it from Randy's phone on the same network)")
    sv.add_argument("--port", type=int, default=8088)
    sv.add_argument("--token", default=None,
                    help="owner token, 16+ characters. Falls back to $SWITCHBOARD_WEB_TOKEN. There "
                         "is no unauthenticated mode: without a token the server refuses to start.")
    sv.add_argument("--print-url", action="store_true",
                    help="print the one-time landing URL including the token. Sensitive: it appears "
                         "in your terminal scrollback; the token is never logged by the server.")

    # `--pretty` is accepted in both positions. Argparse would otherwise reject the flag
    # after the subcommand, because the top-level parser owns it (README documented the
    # after-subcommand form, which exited 2). default=SUPPRESS keeps the top-level value
    # when the flag is not repeated after the subcommand.
    for sub_parser in {id(p): p for p in sub.choices.values()}.values():
        if any(a.dest == "pretty" for a in sub_parser._actions):
            continue
        sub_parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                                help="indent the JSON output (also accepted before the subcommand)")
    return p


def _svc(args: argparse.Namespace) -> Grace:
    return Grace(args.db, scenario=args.scenario, faults=args.fault)


def _job_brief(job: dict) -> dict:
    return {k: job.get(k) for k in ("job_id", "ws_conv_id", "job_state", "state_version",
                                    "agent", "instruction", "lease", "attempt_count",
                                    "created_at", "updated_at", "superseded_by")}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    pretty = bool(getattr(args, "pretty", False))
    cmd = args.cmd
    if cmd == "scenarios":
        from .fixtures import SCENARIOS
        return _print(emit(cmd, data={k: {"description": v["description"],
                                          "mock_label": C.mock_label(k)}
                                      for k, v in sorted(SCENARIOS.items())},
                          mocked=True, label=C.mock_label("scenarios"),
                          message="labelled mock scenarios"), pretty)
    if cmd == "serve":
        # Not a one-shot command: it blocks, and it owns its own database connection
        # inside the serving thread, so no service is opened here.
        return _serve(args, pretty)
    svc = _svc(args)
    _STORE_CONTEXT["has_mock_source"] = bool(svc.store.scalar(
        "SELECT COUNT(*) FROM source_account WHERE origin = 'mock'"))
    try:
        return _dispatch(svc, args, cmd, pretty)
    finally:
        svc.close()


def _dispatch(svc: Grace, args: argparse.Namespace, cmd: str, pretty: bool) -> int:
    if cmd == "seed":
        res = svc.seed(reset=args.reset)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok,
                           mocked=True, label=C.mock_label("fixtures")), pretty)
    if cmd == "needs-me":
        rows = svc.needs_me()
        return _print(emit(cmd, data={"items": rows, "counts": svc.ledger.counts()},
                           message=f"{len(rows)} item(s) need attention"), pretty)
    if cmd == "working":
        rows = svc.working()
        return _print(emit(cmd, data={"items": rows, "counts": svc.ledger.counts()},
                           message=f"{len(rows)} job(s) in flight"), pretty)
    if cmd == "all":
        rows = svc.all_conversations()
        return _print(emit(cmd, data={"items": rows, "counts": svc.ledger.counts()},
                           message=f"{len(rows)} conversation(s)"), pretty)
    if cmd == "counts":
        return _print(emit(cmd, data=svc.ledger.counts()), pretty)
    if cmd == "health":
        health = svc.health()
        return _print(emit(cmd, data=health["data"], mocked=health["mocked"],
                           label=health["mock_label"],
                           message="service health (mock adapters only)"), pretty)
    if cmd == "coverage":
        return _print(emit(cmd, data={"checkpoints": svc.ingest.coverage()}), pretty)
    if cmd == "source-health":
        return _print(emit(cmd, data={"sources": svc.ingest.source_health()}), pretty)
    if cmd == "probe-import":
        # The document is read (and a malformed one refused, typed) before anything is
        # built, opened or stored: a bad file is a refusal, never a traceback.
        loaded = read_probe_document(args.file)
        if not loaded["ok"]:
            refusal = probe_document_refusal(loaded)
            return _print(emit(cmd, ok=False, data=refusal, message=(
                f"refused: {loaded['reason']}, nothing stored — {loaded['problem']} "
                f"Smallest next action: {loaded['next_action']}")), pretty)
        rows = loaded["rows"]
        # Above the store: a malformed or over-claiming document is a typed refusal, never a
        # partial import and never an exception.
        result = import_probe_rows(svc.store, args.account, rows, actor=args.actor)
        superseded = result.get("supersessions") or []
        if result["ok"]:
            svc.store.audit(actor=args.actor, operation="probe_import",
                            entity_kind="source_account", entity_id=args.account,
                            reason=f"{result['imported']} capability row(s) imported",
                            details={"imported": result["imported"],
                                     "provenance": result["provenance"],
                                     "supersessions": [s["capability"] for s in superseded]})
            message = f"{result['imported']} probe row(s) stored for {args.account}"
            if superseded:
                # Reported, never silent: a measurement took the place of a stored
                # documentation read for these capability keys.
                message += (f"; superseded the stored documentation row for "
                            f"{', '.join(s['capability'] for s in superseded)}")
        else:
            refusals = result.get("refusals") or []
            message = (f"refused: {len(result['problems'])} problem(s), nothing stored")
            if refusals:
                reasons = sorted({r["reason"] for r in refusals})
                message += (f"; {len(refusals)} documentation row(s) would replace a stored "
                            f"measurement ({', '.join(reasons)}). Smallest next action: "
                            f"{refusals[0]['next_action']}")
        return _print(emit(cmd, ok=result["ok"], data=result, message=message), pretty)
    if cmd == "reap":
        reaped = svc.ledger.reap_expired_leases()
        return _print(emit(cmd, data={"reaped": reaped, "counts": svc.ledger.counts()},
                           message=f"{len(reaped)} stalled job(s) returned to a recoverable "
                                   f"state"), pretty)
    if cmd == "outbox":
        res = svc.ledger.publish_outbox()
        return _print(emit(cmd, data=res.data, message=res.detail), pretty)
    if cmd == "assign":
        res = svc.assign(args.ws, args.instruction, args.agent,
                         operation_id=args.operation_id)
        data = res.data
        if res.ok and data:
            data = {**data, "job": _job_brief(svc.ledger.job_detail(data["job_id"]))}
        return _print(emit(cmd, data=data, message=res.detail, ok=res.ok,
                           mocked=res.mocked, label=res.label), pretty)
    if cmd == "job":
        detail = svc.ledger.job_detail(args.job)
        if detail is None:
            return _print(emit(cmd, ok=False, message=f"unknown job {args.job}"), pretty)
        return _print(emit(cmd, data=_job_brief(detail) | {
            "capability_plan": detail["capability_plan"],
            "transitions": detail["transitions"], "attempts": detail["attempts"],
            "inputs": detail["inputs"], "results": detail["results"],
            "effects": detail["effects"], "session_bindings": detail["session_bindings"],
            "checkpoint": detail["checkpoint"], "stall_count": detail["stall_count"],
        }), pretty)
    if cmd == "jobs":
        sql = "SELECT * FROM job"
        params: tuple = ()
        if args.state:
            sql += " WHERE job_state = ?"
            params = (args.state,)
        sql += " ORDER BY created_at LIMIT ?"
        rows = svc.store.all(sql, params + (args.limit,))
        return _print(emit(cmd, data={"jobs": [_job_brief(r) for r in rows]}), pretty)
    if cmd == "run":
        res = svc.run_job(args.job, worker=args.worker, lease_seconds=args.lease_seconds,
                          destination_conv_id=args.destination, mode=args.mode,
                          draft_body=args.body, question=args.question,
                          fail_with=args.fail_with, stall=args.stall)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok,
                           mocked=True, label=res.label), pretty)
    if cmd == "draft":
        view = svc.effects.draft_view(args.draft)
        if not view:
            return _print(emit(cmd, ok=False, message=f"unknown draft {args.draft}"), pretty)
        return _print(emit(cmd, data=view), pretty)
    if cmd == "draft-revise":
        res = svc.effects.revise_draft(args.draft, body=args.body, subject=args.subject,
                                       mode=args.mode, expected_version=args.expected_version)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok), pretty)
    if cmd == "approve":
        acks = ["limitations_acknowledged"] if args.ack_limitations else []
        res = svc.effects.grant_approval(args.draft, operation_id=args.operation_id,
                                          ttl_seconds=args.ttl, acknowledgements=acks)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok,
                           extra={"code": res.code}), pretty)
    if cmd == "dispatch":
        try:
            res = svc.effects.dispatch(args.approval, inject_crash_after=args.inject_crash_after)
        except InjectedFault as fault:
            data = {"injected_fault": str(fault), "where": fault.where,
                    "recovery": "re-run 'effect' or 'reconcile' — the operation is persisted and "
                                "marked as requiring reconciliation"}
            return _print(emit(cmd, data=data, ok=False,
                               message="INJECTED FAULT (test hook): process stopped inside the "
                                       "dispatch window"), pretty)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok,
                           extra={"code": res.code}), pretty)
    if cmd == "effect":
        view = svc.effects.effect_view(args.effect)
        if not view:
            return _print(emit(cmd, ok=False, message=f"unknown effect {args.effect}"), pretty)
        return _print(emit(cmd, data=view), pretty)
    if cmd == "reconcile":
        res = svc.effects.reconcile(args.effect)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok,
                           extra={"code": res.code}), pretty)
    if cmd == "retry":
        res = svc.effects.retry(args.effect)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok,
                           extra={"code": res.code}), pretty)
    if cmd == "cancel-effect":
        res = svc.effects.cancel(args.effect, reason=args.reason)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok,
                           extra={"code": res.code}), pretty)
    if cmd == "cancel":
        res = svc.cancel_job(args.job, args.reason)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok), pretty)
    if cmd == "answer":
        res = svc.answer(args.job, args.text)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok), pretty)
    if cmd == "audit":
        rows = svc.store.all("SELECT * FROM audit_event ORDER BY at DESC LIMIT ?", (args.limit,))
        return _print(emit(cmd, data={"events": rows}), pretty)
    if cmd == "sync":
        res = svc.ingest.sync(args.adapter, account_id=args.account, scope_ref=args.scope_ref,
                              limit=args.limit)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok,
                           mocked=res.mocked, label=res.label), pretty)
    if cmd == "export-fixtures":
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        corpus = svc.corpus
        files: dict[str, str] = {}
        corpus_bytes = json.dumps(corpus, indent=2, sort_keys=True, default=str)
        (out_dir / "corpus.json").write_text(corpus_bytes + "\n")
        files["corpus.json"] = hashlib.sha256((corpus_bytes + "\n").encode()).hexdigest()
        for adapter_name in sorted({a["adapter"] for a in corpus["accounts"]}):
            messages = [m for m in corpus["messages"] if m["adapter"] == adapter_name]
            msg_ref_ids = {m["msg_ref_id"] for m in messages}
            attachments = [dict(a, adapter=adapter_name) for a in corpus["attachments"]
                           if a["msg_ref_id"] in msg_ref_ids]
            subset = {
                "adapter": adapter_name,
                "mock_label": C.mock_label(adapter_name),
                "disclaimer": C.MOCK_DISCLAIMER,
                "accounts": [a for a in corpus["accounts"] if a["adapter"] == adapter_name],
                "conversations": [c for c in corpus["conversations"]
                                  if c["adapter"] == adapter_name],
                "messages": messages,
                "attachments": attachments,
            }
            name = f"{adapter_name}.json"
            body = json.dumps(subset, indent=2, sort_keys=True, default=str)
            (out_dir / name).write_text(body + "\n")
            files[name] = hashlib.sha256((body + "\n").encode()).hexdigest()
        manifest = {
            "mock_label": C.mock_label("fixtures"),
            "disclaimer": C.MOCK_DISCLAIMER,
            "note": "Deterministic recorded fixtures for components that cannot reach a real "
                    "source (the macOS Mini worker, Gate 2 probes). Every record is labelled "
                    "origin='mock'; nothing here was captured from Mail, Beeper or Contacts.",
            "files": files,
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return _print(emit(cmd, data={"out": str(out_dir), "files": sorted(files),
                                      "hashes": files},
                           mocked=True, label=C.mock_label("fixtures"),
                           message="labelled fixtures exported"), pretty)
    if cmd == "rule-save":
        scope: dict = {"account_ids": [], "audience_kinds": []}
        conds = [{"kind": "sender_identity", "value": args.sender}]
        if args.subject_contains:
            conds.append({"kind": "subject_contains", "value": args.subject_contains})
        explanation = (f"For messages from {args.sender}"
                       + (f" whose subject contains {args.subject_contains!r}"
                          if args.subject_contains else "")
                       + f", ask {args.agent} to prepare a draft. Drafting only: this rule grants "
                         f"no permission to send.")
        res = svc.rules.save(rule_id=args.rule_id, owner="owner", scope=scope,
                             conditions={"all": conds}, explanation=explanation,
                             action="prepare_draft", agent=args.agent,
                             instruction_template=args.instruction,
                             origin=C.MOCK, mock_label=C.mock_label("fixtures"))
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok), pretty)
    if cmd == "rule-preview":
        res = svc.rules.preview(args.rule_id, args.version, bounds={"limit": args.limit})
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok), pretty)
    if cmd == "rule-apply":
        res = svc.rules.apply(args.rule_run, stop_after=args.stop_after)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok), pretty)
    if cmd == "rule-cancel":
        res = svc.rules.cancel_run(args.rule_run, reason=args.reason)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok), pretty)
    if cmd == "rule-disable":
        res = svc.rules.disable(args.rule_id, args.version, reason="owner disabled the rule",
                                cancel_pending=args.cancel_pending)
        return _print(emit(cmd, data=res.data, message=res.detail, ok=res.ok), pretty)
    if cmd == "demo":
        return _print(_demo(svc, include_mail_leg=args.include_mail_leg), pretty)
    return _print(emit(cmd, ok=False, message=f"unhandled command {cmd}"), pretty)


# --------------------------------------------------------------------- serve ---

TOKEN_ENV = "SWITCHBOARD_WEB_TOKEN"
MIN_TOKEN_LENGTH = 16


def _serve(args: argparse.Namespace, pretty: bool) -> int:
    """Serve the authenticated review client. Refuses to start without a token."""
    import os

    from .web import WebApp

    token = args.token or os.environ.get(TOKEN_ENV) or ""
    if len(token) < MIN_TOKEN_LENGTH:
        payload = emit("serve", ok=False, mocked=False,
                       message=("refusing to start: this surface has no unauthenticated mode. Pass "
                                f"--token <16+ characters> or set ${TOKEN_ENV}."),
                       data={"token_required": True, "min_length": MIN_TOKEN_LENGTH,
                             "env_var": TOKEN_ENV})
        _print(payload, pretty)
        return 2
    try:
        app = WebApp(args.db, token, host=args.host, port=args.port,
                     scenario=args.scenario, faults=args.fault)
    except (OSError, ValueError) as exc:
        _print(emit("serve", ok=False, mocked=False, message=str(exc)), pretty)
        return 2
    url = app.base_url() + "/"
    landing = url + "?token=" + token
    payload = emit("serve", mocked=True, label=C.mock_label("web"),
                   message="serving the Gate 1 review client on labelled mocks",
                   data={
                       "url": url,
                       "host": args.host,
                       "port": app.port,
                       "db": app.db_path,
                       "scenario": app.scenario,
                       "faults": sorted(app.faults),
                       "landing_url": landing if args.print_url else None,
                       "token_redacted": not args.print_url,
                       "auth": ("every path — including / and the assets — requires the owner token; "
                                "an unauthenticated request gets 401 with no data and the token is "
                                "never logged"),
                       "how_to_open": ("append ?token=<your token> to the URL on your phone, once. "
                                       "The client trades it for an HttpOnly session cookie and "
                                       "removes it from the address bar."
                                       if not args.print_url else
                                       "open the landing_url below; it carries the token once and "
                                       "the client then removes it from the address bar"),
                       "reach_from_phone": (f"mocked surfaces only. To reach this from Randy's phone, "
                                            f"both devices must be on the same network and the "
                                            f"server must bind {args.host}. Nothing in this "
                                            f"deployment has been exercised against a real source."),
                   })
    _print(payload, pretty)
    try:
        app.serve()
    except KeyboardInterrupt:
        return 0
    return 0




def _demo(svc: Grace, *, include_mail_leg: bool = True) -> dict:
    """Walk the Gate 1 flow end to end against labelled mocks, recording each step."""
    steps: list[dict] = []
    svc.seed(reset=True)
    steps.append({"step": 0, "action": "seed fixtures",
                  "command_equivalent": "python3 -m grace seed --reset",
                  "result": {"counts": svc.ledger.counts()}})
    needs = svc.needs_me()
    target = next((w for w in needs if w["title"].startswith("Alex")), needs[0] if needs else None)
    if target is None:
        return emit("demo", ok=False, message="no fixture conversation to work with")
    steps.append({"step": 1, "action": "read Needs me",
                  "command_equivalent": "python3 -m grace needs-me",
                  "result": {"title": target["title"], "reason": target["needs_me_reason"],
                             "counts": svc.ledger.counts(),
                             "untriaged_source_read_state": target["latest_message"]["read_state"]}})
    instruction = ("Check the order status and draft a reply with the current delivery date "
                   "(MOCK fixture instruction).")
    assigned = svc.assign(target["ws_conv_id"], instruction, "researcher",
                          operation_id="demo-op-assign-1")
    job_id = assigned.data["job_id"]
    steps.append({"step": 2, "action": "assign instruction to a named agent",
                  "command_equivalent": f"python3 -m grace assign --ws {target['ws_conv_id']} "
                                        f"--instruction '...' --agent researcher",
                  "result": {"job_id": job_id, "job_state": "queued",
                             "outbox_pending": len(svc.ledger.pending_outbox()),
                             "counts_after_assignment": svc.ledger.counts()}})
    published = svc.ledger.publish_outbox()
    steps.append({"step": 3, "action": "publish the persisted job to the worker queue",
                  "command_equivalent": "python3 -m grace outbox",
                  "result": {"published": published.data["published"],
                             "note": "persisted before publication, so a crash between the two "
                                     "recovers by republishing"}})
    destination = None
    prefer = svc.store.one(
        "SELECT c.conv_id, c.adapter FROM workspace_source_link l "
        "JOIN source_conversation c ON c.conv_id = l.conv_id "
        "WHERE l.ws_conv_id = ? AND l.relevance = 'primary' "
        "ORDER BY (c.adapter = 'mock_beeper') DESC, c.source_time_last DESC LIMIT 1",
        (target["ws_conv_id"],))
    destination = prefer["conv_id"] if prefer else None
    ran = svc.run_job(job_id, destination_conv_id=destination, mode="reply_all")
    draft = ran.data["draft"]
    steps.append({"step": 4, "action": "MOCK worker pass produces a draft",
                  "command_equivalent": f"python3 -m grace run --job {job_id}",
                  "result": {"job_state_after": svc.store.one(
                      "SELECT job_state FROM job WHERE job_id = ?", (job_id,))["job_state"],
                      "draft_id": draft["draft_id"],
                      "recipients": draft["recipients"],
                      "hashes": draft["hashes"],
                      "labelled": draft["mock_label"]}})
    revised = svc.effects.revise_draft(draft["draft_id"],
                                       body=draft["body"] + "\n\n(Mock edit by the owner.)")
    new_draft = revised.data["draft"]
    steps.append({"step": 5, "action": "owner edits the draft (new immutable version)",
                  "command_equivalent": f"python3 -m grace draft-revise --draft {draft['draft_id']}",
                  "result": {"new_version": new_draft["version"],
                             "superseded_versions": revised.data["superseded_versions"],
                             "invalidated_approvals": revised.data["invalidated_approvals"]}})
    approval = svc.effects.grant_approval(new_draft["draft_id"], operation_id="demo-op-send-1",
                                          ttl_seconds=900)
    steps.append({"step": 6, "action": "owner approves the bound version",
                  "command_equivalent": "python3 -m grace approve --draft "
                                        f"{new_draft['draft_id']} --operation-id demo-op-send-1",
                  "result": {"approval_id": approval.data["approval"]["approval_id"],
                             "bound": approval.data["approval"]["bound"],
                             "expires_at": approval.data["approval"]["expires_at"]}})
    disp = svc.effects.dispatch(approval.data["approval"]["approval_id"])
    effect_id = disp.data["effect"]["effect_id"]
    steps.append({"step": 7, "action": "dispatch through the labelled mock adapter",
                  "command_equivalent": f"python3 -m grace dispatch --approval "
                                        f"{approval.data['approval']['approval_id']}",
                  "result": {"effect_state": disp.data["effect"]["effect_state"],
                             "provider_status": disp.data["effect"]["provider_status"],
                             "provider_message_id": disp.data["effect"]["provider_message_id"],
                             "actual_routed_destination":
                                 disp.data["effect"]["actual_routed_destination"],
                             "receipt": disp.data["effect"]["receipts"][-1],
                             "job_state": svc.store.one(
                                 "SELECT job_state FROM job WHERE job_id = ?",
                                 (job_id,))["job_state"]}})
    rec = svc.effects.reconcile(effect_id)
    steps.append({"step": 8, "action": "reconcile the pending send",
                  "command_equivalent": f"python3 -m grace reconcile --effect {effect_id}",
                  "result": {"effect_state": rec.data["effect"]["effect_state"],
                             "provider_message_id": rec.data["effect"]["provider_message_id"],
                             "receipt": rec.data["effect"]["receipts"][-1],
                             "job_state": svc.store.one(
                                 "SELECT job_state FROM job WHERE job_id = ?",
                                 (job_id,))["job_state"]}})
    double = svc.effects.dispatch(approval.data["approval"]["approval_id"])
    steps.append({"step": 9, "action": "double tap the approval (must resolve to one decision)",
                  "command_equivalent": f"python3 -m grace dispatch --approval "
                                        f"{approval.data['approval']['approval_id']}",
                  "result": {"code": double.code, "detail": double.detail,
                             "effects_for_operation": svc.store.scalar(
                                 "SELECT COUNT(*) FROM effect_operation WHERE operation_id = ?",
                                 ("demo-op-send-1",))}})
    if include_mail_leg:
        mail_leg = _demo_mail_leg(svc, target["ws_conv_id"])
        steps.append({"step": 10, "action": "second leg: Mail-shaped adapter accepts locally only",
                      "command_equivalent": "python3 -m grace run / approve / dispatch / reconcile",
                      "result": mail_leg})
    return emit("demo", data={
        "steps": steps,
        "final_counts": svc.ledger.counts(),
        "notes": [
            "Every value above came from a labelled mock adapter (origin='mock', MOCK: label).",
            "No Mail, Beeper, Contacts or Hermes source exists in this process: the send and the "
            "receipt are simulated.",
            "Verification levels are those the evidence supports: provider_pending/accepted are "
            "not 'delivered', and a confirmed_sent receipt came from a reconciliation read-back.",
        ],
    }, message="Gate 1 mock walk completed", mocked=True, label=C.mock_label("demo"))


def _demo_mail_leg(svc: Grace, ws_conv_id: str) -> dict:
    job = svc.assign(ws_conv_id, "Draft a reply to the invoice thread (MOCK fixture).",
                     "accounts_agent", operation_id="demo-op-assign-2")
    job_id = job.data["job_id"]
    svc.ledger.publish_outbox()
    mail = svc.store.one(
        "SELECT c.conv_id FROM workspace_source_link l JOIN source_conversation c "
        "ON c.conv_id = l.conv_id WHERE l.ws_conv_id = ? AND c.adapter = 'mock_mail' "
        "AND l.relevance = 'primary' ORDER BY c.source_time_last DESC LIMIT 1", (ws_conv_id,))
    ran = svc.run_job(job_id, destination_conv_id=mail["conv_id"], mode="reply_all")
    draft = ran.data["draft"]
    appr = svc.effects.grant_approval(draft["draft_id"], operation_id="demo-op-send-2")
    disp = svc.effects.dispatch(appr.data["approval"]["approval_id"])
    effect_id = disp.data["effect"]["effect_id"]
    rec = svc.effects.reconcile(effect_id)
    return {
        "draft_recipients": draft["recipients"],
        "effect_state": rec.data["effect"]["effect_state"],
        "receipt": rec.data["effect"]["receipts"][-1],
        "honesty": "the Mail-shaped mock only accepts locally; the product does not claim "
                   "delivery and does not retry",
    }


if __name__ == "__main__":
    sys.exit(main())
