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


def run_cli(db: str, *args: str, expect_ok: bool = True) -> dict:
    """Run the CLI in a fresh process and return its JSON document."""
    cmd = [sys.executable, "-m", "grace", "--db", db, *args]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
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
                          cwd=REPO_ROOT, capture_output=True, text=True)


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
