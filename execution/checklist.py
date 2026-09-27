"""Run the stop-loss suite and print the checklist."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    import pytest

    root = Path(__file__).resolve().parents[1]
    suite = root / "tests"
    return int(pytest.main(["-q", str(suite)]))


if __name__ == "__main__":
    sys.exit(main())
