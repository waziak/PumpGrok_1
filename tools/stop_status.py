#!/usr/bin/env python3
"""Print deterministic stop-loss status for open positions."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.status import main


if __name__ == "__main__":
    raise SystemExit(main())
