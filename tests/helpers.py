"""Shared test helpers.

Tests import ``grace`` from the repository root as a plain package and drive the real
CLI in a subprocess when the point of the test is a process restart. Nothing here
uses a third-party dependency: stdlib ``unittest`` only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from grace.service import Grace  # noqa: E402

#: Every ``subprocess`` call in ``tests/`` passes ``stdin=subprocess.DEVNULL``, and
#: ``tests/test_suite_process_hygiene.py`` fails the suite if one ever stops doing so.
#: **What leaks without it:** the child inherits the parent's file descriptor 0, which is the
#: terminal whenever a person runs the suite by hand. A child that reads it -- now, or after
#: some later change to a CLI or a shell wrapper this suite spawns -- blocks until that
#: terminal produces input, so the dots stop and the summary is never printed: from the
#: outside that is indistinguishable from the interpreter hanging at exit, and it makes every
#: "the suite is green" claim unverifiable. **Why the fix stops it:** detaching fd 0 removes
#: the shared handle entirely, so a child can neither read nor hold the launching terminal and
#: the suite returns no matter what it was started from. (This was investigated, not assumed:
#: the suite runs to a summary in ~72 s both with ``< /dev/null`` and with a real pty on fd 0,
#: and the lead's SIGUSR1 thread dump at the point the dots stop lists the main thread only.)


def run_cli(db: str, *args: str, expect_ok: bool = True) -> dict:
    """Run the CLI in a fresh process and return its JSON document."""
    cmd = [sys.executable, "-m", "grace", "--db", db, *args]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:  # pragma: no cover - surfaced with the raw output
        raise AssertionError(
            f"CLI did not print JSON for {args}\nexit={proc.returncode}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}")
    if expect_ok:
        assert payload.get("ok") is True, f"{args} failed: {payload}"
    return payload


def run_cli_raw(db: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "grace", "--db", db, *args],
                          cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True)


class GraceTestCase(unittest.TestCase):
    """Base case: a temporary database plus a service bound to it."""

    scenario: str | None = None
    faults: tuple[str, ...] = ()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "grace.sqlite3")
        self.svc = Grace(self.db, scenario=self.scenario, faults=self.faults)

    def tearDown(self) -> None:
        self.svc.close()
        self._tmp.cleanup()

    # -- convenience -------------------------------------------------------
    def seed(self) -> None:
        res = self.svc.seed(reset=True)
        assert res.ok, res.detail

    def alex_ws(self) -> str:
        for item in self.svc.needs_me():
            if item["title"].startswith("Alex"):
                return item["ws_conv_id"]
        raise AssertionError("fixture conversation for Alex Rivera not found")

    def account_id(self, adapter: str) -> str:
        return self.svc.store.scalar("SELECT account_id FROM source_account WHERE adapter = ?",
                                     (adapter,))

    def conv_id(self, adapter: str, provider_id: str) -> str:
        return self.svc.store.scalar(
            "SELECT conv_id FROM source_conversation WHERE account_id = ? AND "
            "(provider_thread_id = ? OR provider_chat_id = ?)",
            (self.account_id(adapter), provider_id, provider_id))

    def assign_and_run(self, *, destination: str | None = None, mode: str = "reply",
                       agent: str = "researcher") -> dict:
        """Seed -> assign -> mock worker pass. Returns {'job_id', 'draft'}."""
        self.seed()
        assigned = self.svc.assign(self.alex_ws(), "Check the order status and draft a reply.",
                                   agent)
        assert assigned.ok, assigned.detail
        job_id = assigned.data["job_id"]
        ran = self.svc.run_job(job_id, destination_conv_id=destination, mode=mode)
        assert ran.ok, ran.detail
        return {"job_id": job_id, "draft": ran.data["draft"]}
