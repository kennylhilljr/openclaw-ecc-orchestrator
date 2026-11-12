"""Probe whether this environment lets the tests run `ps`.

Some sandboxes refuse to start `ps`. Tests that need a real process
listing skip themselves there with `ps_permitted()`.
"""

import functools
import os
import subprocess


@functools.lru_cache(maxsize=1)
def ps_permitted():
    """Return True when `ps` runs and exits zero for this process."""
    try:
        res = subprocess.run(["ps", "-o", "pid=", "-p", str(os.getpid())], capture_output=True, check=False)
    except OSError:
        return False
    return res.returncode == 0
