"""`python3 -m openclaw_ecc_orchestrator <subcommand>`: the operator CLI."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
