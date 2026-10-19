"""Run the hook entry point directly: `python -m lanekeeper.hooks <agent> <event>`."""

import sys

from lanekeeper.hooks import main

raise SystemExit(main(sys.argv[1:]))
