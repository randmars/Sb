"""``mini/install.sh``: which prefix the installer actually uses.

The installer is the first thing Randy runs, and its one argument decides *where* the worker
lands. It used to consume the value of ``--prefix DIR`` with a bare ``shift`` inside
``for arg in "$@"`` (which drops it) and then recover it only when ``--prefix`` happened to be
``$1`` -- so ``sh mini/install.sh --uninstall --prefix DIR`` silently used
``$HOME/.local`` instead of ``DIR``. Every check below therefore drives the shipped script,
in both argument forms and in both orders, against a temporary prefix.

The whole ``mini/`` tree is **copied** first and the copy's ``install.sh`` is the one that
runs: the installer legitimately does ``chmod +x`` on the launcher next to it, and the
repository checkout must stay exactly as it was.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from switchboard_mini.version import WORKER_VERSION

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINI_DIR = os.path.join(REPO_ROOT, "mini")


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="switchboard-install-")
        # A pristine copy of the tree, so nothing here touches the checkout.
        self.tree = os.path.join(self.tmp, "mini")
        shutil.copytree(MINI_DIR, self.tree,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.installer = os.path.join(self.tree, "install.sh")
        self.launcher = os.path.join(self.tree, "bin", "switchboard-mini")
        self.home = os.path.join(self.tmp, "home")          # a HOME with nothing in it
        os.makedirs(os.path.join(self.home, ".local", "bin"), exist_ok=True)
        self.default_link = os.path.join(self.home, ".local", "bin", "switchboard-mini")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers ---
    def env(self):
        env = dict(os.environ)
        env["HOME"] = self.home
        return env

    def run_installer(self, *args):
        return subprocess.run(["sh", self.installer] + list(args), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60, env=self.env())

    def prefix(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(path, "bin"), exist_ok=True)
        return path

    def link_in(self, prefix):
        return os.path.join(prefix, "bin", "switchboard-mini")

    # ------------------------------------------------------------------- checks ---
    def test_the_prefix_argument_directory_form_installs_into_that_prefix(self):
        """`--prefix DIR`: the form the runbook tells Randy to use, value included."""
        prefix = self.prefix("optdir")
        proc = self.run_installer("--prefix", prefix)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        link = self.link_in(prefix)
        self.assertTrue(os.path.islink(link), f"nothing was linked into {link}: {proc.stdout}")
        self.assertEqual(os.path.realpath(link), os.path.realpath(self.launcher))
        self.assertFalse(os.path.lexists(self.default_link),
                         "an explicit prefix must not also write into $HOME/.local")

    def test_the_prefix_argument_equals_form_installs_into_that_prefix(self):
        prefix = self.prefix("optdir")
        proc = self.run_installer(f"--prefix={prefix}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.islink(self.link_in(prefix)), proc.stdout)
        self.assertFalse(os.path.lexists(self.default_link))

    def test_the_installed_launcher_is_executable_and_answers(self):
        prefix = self.prefix("optdir")
        self.assertEqual(self.run_installer("--prefix", prefix).returncode, 0)
        link = self.link_in(prefix)
        self.assertTrue(os.access(link, os.X_OK), "the installer must make the launcher runnable")
        proc = subprocess.run([link, "version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                              env=self.env(), cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(WORKER_VERSION, proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_uninstall_honours_a_prefix_that_is_not_the_first_argument(self):
        """The regression guard: this is the case the old parsing got wrong.

        `--uninstall --prefix DIR` puts the value at `$2`. The old loop shifted it away and
        the post-loop recovery looked at `${1}`/`${2}` -- `${1}` was `--uninstall` -- so the
        default prefix was used: DIR's link survived and the default prefix was the one
        touched. Both halves are asserted here, with a canary in the default prefix.
        """
        prefix = self.prefix("optdir")
        self.assertEqual(self.run_installer("--prefix", prefix).returncode, 0)
        link = self.link_in(prefix)
        self.assertTrue(os.path.islink(link))
        with open(self.default_link, "w", encoding="utf-8") as handle:
            handle.write("canary: not the worker, and not ours to remove\n")

        proc = self.run_installer("--uninstall", "--prefix", prefix)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.lexists(link),
                         f"the link in the requested prefix survived: {proc.stdout}")
        self.assertTrue(os.path.exists(self.default_link),
                        "the default prefix was touched even though --prefix named another")

    def test_uninstall_honours_the_prefix_in_either_order(self):
        prefix = self.prefix("optdir")
        self.assertEqual(self.run_installer("--prefix", prefix).returncode, 0)
        proc = self.run_installer("--prefix", prefix, "--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.lexists(self.link_in(prefix)))

    def test_a_prefix_with_no_directory_is_refused_with_the_working_form(self):
        proc = self.run_installer("--prefix")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--prefix DIR", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_an_empty_prefix_is_refused(self):
        proc = self.run_installer("--prefix=")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--prefix DIR", proc.stderr)

    def test_the_usage_names_the_forms_the_parser_honours(self):
        proc = self.run_installer("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--prefix DIR", proc.stdout)
        self.assertIn("--prefix=DIR", proc.stdout)

    def test_running_it_twice_is_safe(self):
        prefix = self.prefix("optdir")
        first = self.run_installer("--prefix", prefix)
        second = self.run_installer("--prefix", prefix)
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        link = self.link_in(prefix)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(self.launcher))

    def test_the_default_prefix_is_still_home_local_when_none_is_given(self):
        proc = self.run_installer()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.islink(self.default_link), proc.stdout)

    def test_the_installer_leaves_the_tree_it_runs_from_alone_apart_from_the_launcher(self):
        """It may chmod its own launcher (documented in the runbook); nothing else moves."""
        before = {}
        for name in ("bin/switchboard-mini", "install.sh"):
            path = os.path.join(self.tree, name)
            with open(path, "rb") as handle:
                before[name] = (handle.read(), os.stat(path).st_mode)
        self.assertEqual(self.run_installer("--prefix", self.prefix("optdir")).returncode, 0)
        for name, (data, mode) in before.items():
            path = os.path.join(self.tree, name)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), data, f"{name} was rewritten")
            if name == "install.sh":
                self.assertEqual(os.stat(path).st_mode, mode)


if __name__ == "__main__":
    unittest.main()
