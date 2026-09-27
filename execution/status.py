"""Operator status for open stop-loss positions."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config import StopConfig
from .models import STOP_IS_TRIGGER_NOT_GUARANTEE, StopPosition
from .store import StopStore


def default_db_path() -> Path:
    override = os.environ.get("STOP_LOSS_DB", "").strip()
    if override:
        return Path(override)
    desk = os.environ.get("PUMPGROK_DESK", "").strip()
    if not desk:
        desk = "/workspace/trading-desk" if Path("/workspace").is_dir() else "trading-desk"
    return Path(desk) / "state" / "stop_loss.sqlite"


def render_status(config: StopConfig, positions: list[StopPosition]) -> str:
    mode = config.trading_mode if config.trading_mode in ("paper", "live") else "paper"
    lines = [
        f"TRADING_MODE={mode}",
        "PRIVATE KEY EXPOSED=NO",
        "REAL TRADES=NO",
        "LIVE READY=FAIL",
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
    return "\n".join(lines)


def load_status(path: Path | None = None, config: StopConfig | None = None) -> str:
    cfg = config or StopConfig.from_env()
    db = path or default_db_path()
    if not db.exists():
        return render_status(cfg, [])
    store = StopStore(db)
    try:
        return render_status(cfg, store.load_active())
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PumpGrok stop-loss status")
    parser.add_argument("--db", default="", help="SQLite path (default: desk state)")
    args = parser.parse_args(argv)
    path = Path(args.db) if args.db else None
    print(load_status(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
