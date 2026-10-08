"""Test package bootstrap.

``mini/switchboard_mini`` is an installable, standard-library-only package that runs on
Randy's Mac without this repository around it, so the repository root does not put
``mini/`` on ``sys.path``. The tests need both the repository root and ``mini/``, so they
are added here once, for every test module discovered with ``-t .``.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_HERE)
MINI_DIR = os.path.join(REPO_ROOT, "mini")

for _path in (REPO_ROOT, MINI_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)
