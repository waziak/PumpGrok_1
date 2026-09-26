"""SQLite research store. Values that were not observed are stored as UNKNOWN."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DuplicateCandidate(Exception):
    def __init__(self, candidate_id: str) -> None:
        super().__init__(candidate_id)
        self.candidate_id = candidate_id


class DuplicateReceipt(Exception):
    def __init__(self, receipt_id: str) -> None:
        super().__init__(receipt_id)
        self.receipt_id = receipt_id


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ResearchDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        self.conn.executescript(schema)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def insert_candidate(self, row: dict[str, Any]) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO candidates (
                  candidate_id, mint, symbol, name, source, program,
                  market_cap_usd, observed_at, payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["candidate_id"],
                    row["mint"],
                    row.get("symbol"),
                    row.get("name"),
                    row.get("source"),
                    row.get("program"),
                    row.get("market_cap_usd"),
                    row.get("observed_at"),
                    row["payload_json"],
                    row["status"],
                    row.get("created_at") or utc_now(),
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateCandidate(row["candidate_id"]) from exc

    def upsert_features(self, candidate_id: str, features: list[dict[str, Any]]) -> None:
        for feature in features:
            self.conn.execute(
                """
                INSERT INTO candidate_features (
                  candidate_id, feature_key, feature_value, source, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id, feature_key) DO UPDATE SET
                  feature_value = excluded.feature_value,
                  source = excluded.source,
                  observed_at = excluded.observed_at
                """,
                (
                    candidate_id,
                    feature["feature_key"],
                    feature["feature_value"],
                    feature.get("source"),
                    feature.get("observed_at"),
                ),
            )
        self.conn.commit()

    def add_review(self, candidate_id: str, review: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO agent_reviews (candidate_id, agent, verdict, review_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                str(review.get("agent") or "UNKNOWN"),
                None if review.get("verdict") is None else str(review.get("verdict")),
                json.dumps(review, sort_keys=True),
                utc_now(),
            ),
        )
        self.conn.commit()

    def add_risk_decision(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO risk_decisions (
              candidate_id, decision, reasons_json, thresholds_json,
              override_attempt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["candidate_id"],
                row["decision"],
                json.dumps(row["reasons"]),
                json.dumps(row["thresholds"]),
                1 if row.get("override_attempt") else 0,
                row.get("created_at") or utc_now(),
            ),
        )
        self.conn.commit()

    def add_paper_trade(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_trades (
              trade_id, candidate_id, mint, side, size_sol, price_sol,
              costs_json, fill_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["trade_id"],
                row["candidate_id"],
                row["mint"],
                row["side"],
                row["size_sol"],
                row.get("price_sol"),
                json.dumps(row["costs"]),
                json.dumps(row["fill"]),
                row.get("created_at") or utc_now(),
            ),
        )
        self.conn.commit()

    def upsert_position(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO positions (
              position_id, candidate_id, mint, size_sol, tokens, entry_price,
              remaining_fraction, status, opened_at, updated_at, high_water_price,
              strategy_id, state_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
              remaining_fraction = excluded.remaining_fraction,
              status = excluded.status,
              updated_at = excluded.updated_at,
              high_water_price = excluded.high_water_price,
              tokens = excluded.tokens,
              state_json = excluded.state_json
            """,
            (
                row["position_id"],
                row["candidate_id"],
                row["mint"],
                row["size_sol"],
                row.get("tokens"),
                row.get("entry_price"),
                row["remaining_fraction"],
                row["status"],
                row["opened_at"],
                row["updated_at"],
                row.get("high_water_price"),
                row.get("strategy_id"),
                json.dumps(row["state"]),
            ),
        )
        self.conn.commit()

    def add_exit(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO trade_exits (
              exit_id, position_id, family, fraction, proceeds_sol,
              costs_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["exit_id"],
                row["position_id"],
                row["family"],
                row["fraction"],
                row.get("proceeds_sol"),
                json.dumps(row["costs"]),
                row.get("reason"),
                row.get("created_at") or utc_now(),
            ),
        )
        self.conn.commit()

    def add_receipt(self, row: dict[str, Any]) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO execution_receipts (
                  receipt_id, candidate_id, mode, status, signature, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["receipt_id"],
                    row.get("candidate_id"),
                    row["mode"],
                    row["status"],
                    row.get("signature"),
                    json.dumps(row.get("payload") or {}),
                    row.get("created_at") or utc_now(),
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateReceipt(row["receipt_id"]) from exc

    def upsert_daily(self, day: str, summary: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO daily_research (day, summary_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
              summary_json = excluded.summary_json,
              created_at = excluded.created_at
            """,
            (day, json.dumps(summary), utc_now()),
        )
        self.conn.commit()

    def upsert_strategy(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO strategy_versions (
              strategy_id, version, hypothesis, params_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, version) DO UPDATE SET
              hypothesis = excluded.hypothesis,
              params_json = excluded.params_json,
              status = excluded.status,
              created_at = excluded.created_at
            """,
            (
                row["strategy_id"],
                row["version"],
                row["hypothesis"],
                json.dumps(row["params"]),
                row["status"],
                row.get("created_at") or utc_now(),
            ),
        )
        self.conn.commit()

    def candidate_exists(self, candidate_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        return row is not None

    def set_status(self, candidate_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE candidates SET status = ? WHERE candidate_id = ?",
            (status, candidate_id),
        )
        self.conn.commit()

    def get_position(self, position_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        return _position_dict(row) if row else None

    def open_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'partial') ORDER BY opened_at"
        ).fetchall()
        return [_position_dict(row) for row in rows]

    def open_exposure_sol(self) -> float:
        total = 0.0
        for position in self.open_positions():
            total += float(position["size_sol"]) * float(position["remaining_fraction"])
        return round(total, 9)

    def simulated_cash(self, starting_sol: float) -> float:
        spent = self.conn.execute(
            "SELECT COALESCE(SUM(size_sol), 0) FROM paper_trades WHERE side = 'buy'"
        ).fetchone()[0]
        buy_costs = 0.0
        for row in self.conn.execute("SELECT costs_json FROM paper_trades WHERE side = 'buy'"):
            costs = json.loads(row[0])
            buy_costs += float(costs.get("fixed_sol") or 0)
        proceeds = self.conn.execute(
            "SELECT COALESCE(SUM(COALESCE(proceeds_sol, 0)), 0) FROM trade_exits"
        ).fetchone()[0]
        # proceeds_sol is already net of exit fixed costs.
        return round(float(starting_sol) - float(spent) - buy_costs + float(proceeds), 9)

    def counts(self) -> dict[str, int]:
        tables = (
            "candidates",
            "candidate_features",
            "agent_reviews",
            "risk_decisions",
            "paper_trades",
            "positions",
            "trade_exits",
            "execution_receipts",
            "daily_research",
            "strategy_versions",
        )
        out: dict[str, int] = {}
        for table in tables:
            out[table] = int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out

    def feature_map(self, candidate_id: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT feature_key, feature_value FROM candidate_features WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchall()
        return {row["feature_key"]: row["feature_value"] for row in rows}


def _position_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["state"] = json.loads(data.pop("state_json"))
    return data
