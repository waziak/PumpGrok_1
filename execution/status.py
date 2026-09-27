"""Operator status for open stop-loss positions."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import sqlite3

from .config import StopConfig, effective_hard_loss_pct
from .models import STOP_IS_TRIGGER_NOT_GUARANTEE, StopPosition
from .profit import ProfitRow, ProfitStore
from .store import StopStore


def default_db_path() -> Path:
    override = os.environ.get("STOP_LOSS_DB", "").strip()
    if override:
        return Path(override)
    desk = os.environ.get("PUMPGROK_DESK", "").strip()
    if not desk:
        desk = "/workspace/trading-desk" if Path("/workspace").is_dir() else "trading-desk"
    return Path(desk) / "state" / "stop_loss.sqlite"


def _mcap(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f}"


def format_research(pos: StopPosition, row: ProfitRow) -> str:
    entry = pos.entry_fill_price
    price = pos.last_mark_price if pos.last_mark_price is not None else entry
    unrealized = (price - entry) * pos.remaining_tokens
    remaining_pct = pos.remaining_tokens / pos.size_tokens * 100.0 if pos.size_tokens else 0.0
    distance = row.trail_distance_pct
    distance_s = "n/a" if distance is None else f"{distance:.1f}%"
    narrative = row.narrative or "(none)"
    return "\n".join((
        (
            f"entry_mcap={_mcap(row.entry_mcap)} current_mcap={_mcap(row.current_mcap)} "
            f"ath_mcap={_mcap(row.ath_mcap)}"
        ),
        (
            f"realized={row.realized_pnl:.4f} unrealized={unrealized:.4f} "
            f"remaining_pct={remaining_pct:.2f}"
        ),
        row.trend_evidence,
        (
            f"regime={row.regime} stall={str(row.stall).lower()} "
            f"breakout={str(row.breakout).lower()}"
        ),
        f"next_action={row.next_action}",
        f"trailing_stop={pos.current_stop_price:.4f} trail_distance={distance_s}",
        f"narrative_context={narrative} (not a decision input)",
    ))


def render_status(
    config: StopConfig,
    positions: list[StopPosition],
    research: dict[str, ProfitRow] | None = None,
) -> str:
    mode = config.trading_mode if config.trading_mode in ("paper", "live") else "paper"
    lines = [
        f"TRADING_MODE={mode}",
        "CURRENT MODE=PAPER" if mode == "paper" else f"CURRENT MODE={mode.upper()}",
        "PRIVATE KEY EXPOSED=NO",
        "REAL TRADES=NO",
        "LIVE READY=FAIL",
        f"DEFAULT_STOP_LOSS_PCT={config.default_stop_loss_pct:.2f}",
        f"HARD_MAX_LOSS_PCT={effective_hard_loss_pct(config.hard_max_loss_pct):.2f}",
        f"MAX_BUY={config.max_buy_sol:.4f}",
        f"MAX_EXPOSURE={config.max_exposure_sol:.4f}",
        f"MIN_RESERVE={config.min_reserve_sol:.4f}",
        STOP_IS_TRIGGER_NOT_GUARANTEE,
        "symbol entry current pnl_pct stop distance_to_stop trailing state",
    ]
    if not positions:
        lines.append("(no open positions)")
        return "\n".join(lines)
    for pos in positions:
        current = pos.last_mark_price
        entry = pos.entry_fill_price
        stop = pos.current_stop_price
        if current is not None and entry > 0:
            pnl = (current - entry) / entry * 100.0
            pnl_s = f"{pnl:+.2f}"
            cur_s = f"{current:.4f}"
            dist = (current - stop) / current * 100.0 if current else 0.0
            dist_s = f"{dist:.2f}"
        else:
            pnl_s = "n/a"
            cur_s = "n/a"
            dist_s = "n/a"
        trailing = "active" if pos.trailing_active else "inactive"
        lines.append(
            f"{pos.symbol} {entry:.4f} {cur_s} {pnl_s} {stop:.4f} {dist_s} {trailing} {pos.stop_state}"
        )
        row = None if research is None else research.get(pos.position_id)
        if row is not None:
            lines.append(format_research(pos, row))
    return "\n".join(lines)


def read_research(path: Path) -> dict[str, ProfitRow]:
    if not path.exists():
        return {}
    conn = sqlite3.connect(path)
    try:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='profit_positions'"
        ).fetchone()
        if found is None:
            return {}
    finally:
        conn.close()
    store = ProfitStore(path)
    try:
        return {row.position_id: row for row in store.all_rows()}
    finally:
        store.close()


def load_status(path: Path | None = None, config: StopConfig | None = None) -> str:
    cfg = config or StopConfig.from_env()
    db = path or default_db_path()
    if not db.exists():
        return render_status(cfg, [])
    store = StopStore(db)
    try:
        positions = store.load_active()
    finally:
        store.close()
    return render_status(cfg, positions, read_research(db))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PumpGrok stop-loss status")
    parser.add_argument("--db", default="", help="SQLite path (default: desk state)")
    args = parser.parse_args(argv)
    path = Path(args.db) if args.db else None
    print(load_status(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
