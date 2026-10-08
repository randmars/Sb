"""switchboard-mini — the Switchboard Mini worker (read-only Mail.app source).

Standard library only, Python 3.9+, installable without pip (see ``mini/install.sh``).
See ``mini/README.md`` for the install, probe and permission runbook.
"""

from .version import PROBE_CONTRACT_VERSION, WORKER_VERSION

__all__ = ["WORKER_VERSION", "PROBE_CONTRACT_VERSION"]
