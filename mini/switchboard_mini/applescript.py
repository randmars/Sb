"""Running AppleScript and turning Apple events errors into typed outcomes.

Every OS- or automation-level failure has to arrive at the caller as one of the typed
outcomes in :mod:`outcomes` -- never as an exception, and never as an empty success.
That mapping lives here, in one place, so the real transport and the fixture transport
classify identically and the tests exercise the same code path that runs on the Mac.

The numeric codes below are Apple event / TCC error numbers that ``osascript`` prints
in its ``execution error: ...`` message, e.g.::

    execution error: Not authorized to send Apple events to Mail. (-1743)

Anything that does not parse is classified ``permanent_error`` with
``reason='unclassified_automation_error'``: the worker refuses to guess that an unknown
error is transient, and records the raw text so the code can be added to the table.
Apple's own documentation warns that Mail's scripting terminology varies by application
version (PRD §11), so this table is a measurement aid, not a promise: the probe records
the raw code and message it actually saw.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

from . import outcomes as O

OSASCRIPT = "/usr/bin/osascript"

# Apple event error numbers -> (typed outcome code, machine-readable reason).
AE_ERROR_MAP = {
    -1743: (O.PERMISSION_DENIED, "not_authorized_to_send_apple_events"),
    -600: (O.OFFLINE, "mail_not_running"),
    -609: (O.OFFLINE, "connection_to_mail_invalid"),
    -1712: (O.RETRYABLE_ERROR, "apple_event_timed_out"),
    -1708: (O.UNSUPPORTED, "event_not_handled_by_mail_terminology"),
    -1700: (O.PERMANENT_ERROR, "coercion_failed_terminology_mismatch"),
    -1728: (O.PERMANENT_ERROR, "object_not_found"),
    -1729: (O.PERMANENT_ERROR, "object_not_found_index_out_of_range"),
    -1741: (O.UNSUPPORTED, "terminology_not_available_for_this_application"),
}

_CODE_RE = re.compile(r"\((-?\d+)\)\s*$")
_EXECUTION_ERROR_RE = re.compile(r"execution error:\s*(.*?)(?:\s*\(-?\d+\))?\s*$", re.S)


@dataclass
class ScriptResult:
    """The raw result of one ``osascript`` invocation, before classification."""

    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    script_kind: str
    timed_out: bool = False
    argv: Optional[list] = None


class ScriptRunner:
    """Runs a script. The real one shells out to ``osascript``; tests replace it."""

    def run(self, script: str, *, script_kind: str, timeout_s: int = 120) -> ScriptResult:
        raise NotImplementedError


class OsascriptRunner(ScriptRunner):
    """The real runner. Requires macOS; elsewhere it returns an ``unsupported`` outcome.

    ``language`` names the interpreter osascript is asked for. Mail is driven by AppleScript
    (the default, no flag), and the Contacts helper by ``JavaScript`` -- the only route to a
    framework with no scripting dictionary, and still standard library on this side.
    """

    def __init__(self, *, executable: str = OSASCRIPT, max_output_bytes: int = 4_000_000,
                 language: Optional[str] = None):
        self.executable = executable
        self.max_output_bytes = max_output_bytes
        self.language = language

    def available(self) -> bool:
        import os

        return sys.platform == "darwin" and os.path.exists(self.executable)

    def run(self, script: str, *, script_kind: str, timeout_s: int = 120) -> ScriptResult:
        argv = [self.executable]
        if self.language:
            argv += ["-l", self.language]
        argv += ["-e", script]
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return ScriptResult(
                returncode=-1,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=f"osascript did not return within {timeout_s}s",
                duration_ms=elapsed, script_kind=script_kind, timed_out=True)
        except FileNotFoundError:
            elapsed = int((time.monotonic() - started) * 1000)
            return ScriptResult(
                returncode=-2, stdout="",
                stderr=f"{self.executable}: not found",
                duration_ms=elapsed, script_kind=script_kind)
        elapsed = int((time.monotonic() - started) * 1000)
        stdout = proc.stdout or ""
        if len(stdout) > self.max_output_bytes:
            stdout = stdout[: self.max_output_bytes]
        return ScriptResult(returncode=proc.returncode, stdout=stdout,
                            stderr=(proc.stderr or ""), duration_ms=elapsed,
                            script_kind=script_kind)


def parse_ae_code(stderr: str) -> Optional[int]:
    """Extract the Apple event error number ``osascript`` printed, if it printed one."""
    if not stderr:
        return None
    for line in reversed(stderr.strip().splitlines()):
        match = _CODE_RE.search(line.strip())
        if match:
            return int(match.group(1))
    return None


def parse_ae_message(stderr: str) -> str:
    match = _EXECUTION_ERROR_RE.search((stderr or "").strip())
    if match:
        return match.group(1).strip()
    return (stderr or "").strip()[:400]


def classify(result: ScriptResult, *, adapter: str, account_id: Optional[str] = None,
             application: str = "Mail") -> O.Outcome:
    """Turn a failed script result into a typed outcome. Success returns ``ok`` with the
    stdout, which the caller then parses.

    ``application`` names the source the automation was aimed at, so the same table serves
    Mail (Apple events) and Contacts (the JavaScript bridge) without one source's failures
    being described in the other's words.
    """
    if result.returncode == 0 and not result.timed_out:
        return O.Outcome.ok({"stdout": result.stdout}, adapter=adapter,
                            account_id=account_id, duration_ms=result.duration_ms)
    if result.timed_out:
        return O.Outcome.retryable(
            f"osascript timed out after {result.duration_ms} ms while probing "
            f"{result.script_kind}; the read did not complete",
            reason="script_timeout", adapter=adapter, account_id=account_id,
            duration_ms=result.duration_ms,
            next_action="retry the read; a populated mailbox may need a longer timeout")
    if result.returncode == -2:
        return O.Outcome.unsupported(
            f"{OSASCRIPT} is not available on this host (platform={sys.platform}). "
            f"The Mini worker needs macOS with Mail.app installed.",
            reason="osascript_unavailable", adapter=adapter, account_id=account_id,
            duration_ms=result.duration_ms)
    code = parse_ae_code(result.stderr)
    message = parse_ae_message(result.stderr)
    if code is not None and code in AE_ERROR_MAP:
        outcome_code, reason = AE_ERROR_MAP[code]
        detail = (f"{application} automation error {code} ({reason}) during {result.script_kind}: "
                  f"{message}")
        data = {"ae_code": code, "ae_message": message, "script_kind": result.script_kind}
        if outcome_code == O.PERMISSION_DENIED:
            return O.Outcome.permission_denied(
                detail, reason=reason, data=data, adapter=adapter, account_id=account_id,
                duration_ms=result.duration_ms,
                next_action=f"grant this worker Automation access to {application} in System Settings "
                            "(see mini/README.md); the capability stays unsupported until then")
        if outcome_code == O.OFFLINE:
            return O.Outcome.offline(detail, reason=reason, data=data, adapter=adapter,
                                     account_id=account_id, duration_ms=result.duration_ms,
                                     next_action=f"launch {application} and retry")
        if outcome_code == O.RETRYABLE_ERROR:
            return O.Outcome.retryable(detail, reason=reason, data=data, adapter=adapter,
                                       account_id=account_id, duration_ms=result.duration_ms,
                                       next_action="retry the read with a longer timeout")
        if outcome_code == O.UNSUPPORTED:
            return O.Outcome.unsupported(
                detail, reason=reason, data=data, adapter=adapter, account_id=account_id,
                duration_ms=result.duration_ms,
                next_action="record this on the Gate 2 capability row; do not assume the "
                            "operation exists")
        return O.Outcome.permanent(detail, reason=reason, data=data, adapter=adapter,
                                   account_id=account_id, duration_ms=result.duration_ms)
    raw = (message or result.stderr or "").strip()[:400]
    return O.Outcome.permanent(
        f"unclassified {application} automation failure during {result.script_kind}: {raw}",
        reason="unclassified_automation_error", adapter=adapter, account_id=account_id,
        data={"ae_code": code, "ae_message": message, "script_kind": result.script_kind,
              "returncode": result.returncode, "stderr": (result.stderr or "")[:1000]},
        duration_ms=result.duration_ms,
        next_action="add this error number to switchboard_mini/applescript.py AE_ERROR_MAP "
                    "with evidence from Randy's Mac; it is recorded, not guessed")
