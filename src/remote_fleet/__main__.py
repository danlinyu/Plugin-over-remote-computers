"""Entrypoint for `python3 -m remote_fleet`."""

import sys
from remote_fleet.cli import main

if __name__ == "__main__":
    sys.exit(main())
