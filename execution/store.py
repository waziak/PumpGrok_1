"""SQLite persistence for stop-loss positions.

OPEN, STOP_TRIGGERED, STOP_SUBMITTED, STOP_PENDING, and
EXIT_FAILED_NO_ROUTE rows are reloaded after a restart. CLOSED rows stay
on disk for the audit trail and are not armed again.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields
from pathlib import Path

from .models import StopPosition

BOOL_FIELDS = frozenset({
    "trailing_enabled",
    "trailing_active",
    "break_even_active",
    "price_data_stale",
    "safe",
})

INT_FIELDS = frozenset({
    "attempt_count",
    "duplicate_events",
})

TEXT_FIELDS = frozenset({
    "position_id",
    "symbol",
    "mint",
    "trading_mode",
    "stop_state",
    "stop_type",
    "stop_triggered_at",
    "stop_submitted_at",
    "stop_reason",
    "last_mark_at",
    "submitted_blockhash",
    "last_error",
    "liquidity_status",
    "opened_at",
    "updated_at",
    "closed_at",
})

COLUMNS = [item.name for item in fields(StopPosition)]


def _affinity(name: str) -> str:
    if name in BOOL_FIELDS or name in INT_FIELDS:
        return "INTEGER NOT NULL DEFAULT 0"
    if name in TEXT_FIELDS:
        if name in ("position_id", "symbol", "mint", "trading_mode", "stop_state", "stop_type"):
            return "TEXT NOT NULL"
        if name in ("opened_at", "updated_at"):
            return "TEXT NOT NULL"
        return "TEXT"
    return "REAL"


def _create_sql() -> str:
    parts = []
    for name in COLUMNS:
        decl = _affinity(name)
        if name == "position_id":
            decl += " PRIMARY KEY"
        parts.append(f"{name} {decl}")
    return "CREATE TABLE IF NOT EXISTS stop_positions (\n  " + ",\n  ".join(parts) + "\n)"


def position_from_row(row: sqlite3.Row) -> StopPosition:
    data: dict[str, object] = {}
    for name in COLUMNS:
        value = row[name]
        if name in BOOL_FIELDS:
            value = bool(value)
        if name == "size_sol" and value is None:
            value = 0.0
        data[name] = value
    return StopPosition(**data)  # type: ignore[arg-type]


def position_to_params(pos: StopPosition) -> dict[str, object]:
    params: dict[str, object] = {}
    for name in COLUMNS:
        value = getattr(pos, name)
        if name in BOOL_FIELDS:
            value = 1 if value else 0
        params[name] = value
    return params


class StopStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute(_create_sql())
        self._ensure_columns()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stop_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stop_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                blockhash TEXT NOT NULL,
                size_tokens REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(position_id, attempt_no)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stop_event_keys (
                event_key TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stop_state ON stop_positions(stop_state)"
        )
        self._conn.commit()

    def _ensure_columns(self) -> None:
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(stop_positions)")}
        for name in COLUMNS:
            if name in have:
                continue
            self._conn.execute(f"ALTER TABLE stop_positions ADD COLUMN {name} {_affinity(name)}")

    def close(self) -> None:
        self._conn.close()

    def save(self, pos: StopPosition) -> None:
        params = position_to_params(pos)
        columns = ", ".join(COLUMNS)
        placeholders = ", ".join(f":{name}" for name in COLUMNS)
        updates = ", ".join(f"{name}=excluded.{name}" for name in COLUMNS if name != "position_id")
        sql = (
            f"INSERT INTO stop_positions ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(position_id) DO UPDATE SET {updates}"
        )
        self._conn.execute(sql, params)
        self._conn.commit()

    def get(self, position_id: str) -> StopPosition | None:
        row = self._conn.execute(
            "SELECT * FROM stop_positions WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        if row is None:
            return None
        return position_from_row(row)

    def load_active(self) -> list[StopPosition]:
        rows = self._conn.execute(
            "SELECT * FROM stop_positions WHERE stop_state != ? ORDER BY opened_at ASC",
            ("CLOSED",),
        ).fetchall()
        return [position_from_row(row) for row in rows]

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM stop_positions").fetchone()
        return int(row["n"])

    def add_event(self, position_id: str, event: str, detail: object, ts: str) -> None:
        payload = detail if isinstance(detail, str) else json.dumps(detail, sort_keys=True)
        self._conn.execute(
            "INSERT INTO stop_events (position_id, ts, event, detail) VALUES (?, ?, ?, ?)",
            (position_id, ts, event, payload),
        )
        self._conn.commit()

    def events_for(self, position_id: str) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT ts, event, detail FROM stop_events WHERE position_id = ? ORDER BY id ASC",
            (position_id,),
        ).fetchall()
        return [{"ts": row["ts"], "event": row["event"], "detail": row["detail"]} for row in rows]

    def insert_event_key(self, event_key: str, position_id: str, ts: str) -> bool:
        try:
            self._conn.execute(
                "INSERT INTO stop_event_keys (event_key, position_id, created_at) VALUES (?, ?, ?)",
                (event_key, position_id, ts),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def add_attempt(
        self,
        position_id: str,
        attempt_no: int,
        blockhash: str,
        size_tokens: float,
        status: str,
        ts: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO stop_attempts (
                position_id, attempt_no, blockhash, size_tokens, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (position_id, attempt_no, blockhash, size_tokens, status, ts),
        )
        self._conn.commit()

    def set_attempt_status(self, position_id: str, attempt_no: int, status: str) -> None:
        self._conn.execute(
            "UPDATE stop_attempts SET status = ? WHERE position_id = ? AND attempt_no = ?",
            (status, position_id, attempt_no),
        )
        self._conn.commit()

    def attempts_for(self, position_id: str) -> list[dict[str, object]]:
        rows = self._conn.execute(
            """
            SELECT attempt_no, blockhash, size_tokens, status, created_at
            FROM stop_attempts WHERE position_id = ? ORDER BY attempt_no ASC
            """,
            (position_id,),
        ).fetchall()
        return [
            {
                "attempt_no": row["attempt_no"],
                "blockhash": row["blockhash"],
                "size_tokens": row["size_tokens"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
