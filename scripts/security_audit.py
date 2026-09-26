#!/usr/bin/env python3
"""Scan the tree for populated key material. Prints PRIVATE KEY EXPOSED: NO or YES."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "data", "dist"}
KEYPAIR_ARRAY = re.compile(r"\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]")
PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
ASSIGNMENT = re.compile(
    r"(PRIVATE[\s_-]*KEY|SECRET[\s_-]*KEY|MNEMONIC|SEED[\s_-]*PHRASE)\s*[:=]\s*(\S+)",
    re.IGNORECASE,
)
PLACEHOLDERS = ("your", "path", "absolute", "example", "changeme", "todo", "redacted", "подставьте")


def exposed_line(line: str) -> bool:
    if PEM.search(line):
        return True
    if KEYPAIR_ARRAY.search(line):
        return True
    match = ASSIGNMENT.search(line)
    if not match:
        return False
    value = match.group(2).strip().strip("\"'`")
    if not value or value in {"=", "''", '""'}:
        return False
    lowered = value.lower()
    if any(token in lowered for token in PLACEHOLDERS):
        return False
    if value.startswith("/") or value.startswith("$") or "${" in value:
        return False
    return len(value) >= 16


def main() -> int:
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if exposed_line(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}")
    if hits:
        print("PRIVATE KEY EXPOSED: YES")
        print("\n".join(hits))
        return 1
    print("PRIVATE KEY EXPOSED: NO")
    print(f"scanned: {ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
