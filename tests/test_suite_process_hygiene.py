"""The suite's own process hygiene: it must reach its summary, and it must leave nothing running.

This file exists because of a real investigation, not a suspicion. Three runs of this suite were
observed to print every dot and then never print a summary, which reads as a hang at interpreter
exit and makes every "the suite is green" claim unverifiable. The investigation:

* ``faulthandler.register(SIGUSR1)`` over a full discovery run, with a thread dump at the point
  the dots stop: the interpreter lists the main thread and nothing else, and the run does reach
  its summary (``Ran 515 tests in 72.250s``);
* the same suite driven with a **real pty on file descriptor 0** (the condition a hand-run has,
  and the one the stalled runs had): it returns, rc=1, in 72.0 s, the same as under ``/dev/null``;
* no product code reads standard input at all -- neither ``grace`` nor ``switchboard_mini`` calls
  ``input()`` or touches ``sys.stdin``.

So there is no leaked non-daemon thread, no server left serving and no unjoined child; what the
stalled runs actually shared was a terminal. Every process this suite spawns used to inherit that
terminal on file descriptor 0, and a child that reads it -- now, or after any later change to a
CLI or a shell wrapper a test drives -- blocks the run silently. The fix is at the source: every
spawn in ``tests/`` now passes ``stdin=subprocess.DEVNULL`` (see the comment in
``tests/helpers.py``), and the first test below is the guard that keeps it that way.

**Nothing here contacts a source.** The only processes started are this repository's own CLIs and
a Python one-liner; nothing reads a Mac, a mailbox, a chat or a gateway.

Requirements covered (PRD):

* **R12 / T03** (evidence is reproducible - a claim that cannot be re-run is not evidence) --
  ``TestEverySpawnedProcessIsDetached``: the suite returns however it is launched, and every child
  this suite starts is detached from whatever standard input the suite itself was given.
* **R16** (a functioning slice is only claimable from a running artefact) -- the same class: the
  run's own result is readable only if the run reaches its summary.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: The ``subprocess`` entry points a test may use to start something. ``run``, ``Popen`` and
#: friends all inherit the parent's file descriptor 0 unless told otherwise.
SPAWNERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


class TestEverySpawnedProcessIsDetached(unittest.TestCase):
    """R12/T03: the suite must return, and no child of it may hold the launching terminal."""

    def test_every_spawned_process_in_the_suite_detaches_stdin(self) -> None:
        """R12/T03 (reproducible evidence): a spawn without ``stdin=`` can hold fd 0 of a
        terminal and stall the whole run, so the tree may not contain one.

        Read structurally rather than by grep, so a call spread over several lines, or one whose
        keyword order changed, is still caught. A file that cannot be parsed fails here too --
        a suite whose own sources do not parse is not one whose green result means anything.
        """
        offenders = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr not in SPAWNERS:
                    continue
                if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                    continue
                if not any(keyword.arg == "stdin" for keyword in node.keywords):
                    offenders.append(f"{path.name}:{node.lineno} subprocess.{func.attr}()")
        self.assertEqual(
            offenders, [],
            "these spawns inherit the suite's own standard input, so a child that reads it "
            f"stalls the run: {offenders}")

    def test_a_spawned_child_cannot_read_or_hold_the_launching_terminal(self) -> None:
        """R12/T03: the flag actually does what the comment says it does.

        Driven, not asserted from the source: a child is started exactly the way the suite starts
        its own CLIs and reports what it finds on file descriptor 0. An empty, non-terminal
        standard input is the property that makes the run independent of how it was launched.
        """
        code = "import sys; print(sys.stdin.isatty()); print(repr(sys.stdin.read()))"
        proc = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["False", "''"],
                         f"a detached child must see no terminal and no input: {proc.stdout!r}")
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
