#!/usr/bin/env python3
"""
paper_sim.py – Paper fills that must pass the deterministic stop engine.

Usage (buy, stop required):
  python tools/paper_sim.py --action buy --ticket SOL-20260827-001 \
      --mint <mint> --size-usd 50 --price 1.0 --stop-type FIXED_PERCENT_STOP \
      --stop-pct 20

Usage (sell journal):
  python tools/paper_sim.py --action sell --ticket SOL-20260827-001 \
      --mint <mint> --size-usd 50 --price 1.2 --reason TP

A buy without an explicit stop is rejected and is not journaled.
This tool never sends a live transaction and never reads a private key.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def append_paper_fill(desk: Path, record: Dict[str, Any]) -> str:
    journal_dir = desk / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = journal_dir / f"paper-{day}.md"

    line = (
        f"\n### PAPER {record['action'].upper()} | {record['ticketId']}\n"
        f"- UTC: {record['utc']}\n"
        f"- Mint: {record['mint']}\n"
        f"- Size USD: {record['sizeUsd']}\n"
        f"- Price: {record['price']}\n"
        f"- Slippage bps: {record.get('slippageBps', 'n/a')}\n"
        f"- Reason: {record.get('reason', '')}\n"
        f"- Note: {record.get('note', '')}\n"
        f"- Position: {record.get('positionId', '')}\n"
        f"- Stop: {record.get('currentStopPrice', '')}\n"
        f"- Hard stop: {record.get('hardStopPrice', '')}\n"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return str(path)


def _paper_buy(args: argparse.Namespace) -> Dict[str, Any]:
    from execution.config import StopConfig
    from execution.engine import StopEngine, TradeRejected
    from execution.models import Clock, OpenRequest, StopPolicy
    from execution.status import default_db_path
    from execution.store import StopStore

    if not args.stop_type:
        return {
            "ok": False,
            "mode": "paper",
            "error": "stop_required",
            "realTrades": "NO",
            "privateKeyExposed": "NO",
        }

    # Paper path. Inherit overnight caps from the process env, but force paper mode.
    merged = dict(os.environ)
    merged["TRADING_MODE"] = "paper"
    config = StopConfig.from_env(merged)
    db_path = Path(args.db) if args.db else default_db_path()
    store = StopStore(db_path)
    engine = StopEngine(store, config, Clock())
    try:
        policy = StopPolicy(
            stop_type=args.stop_type,
            stop_loss_pct=args.stop_pct,
            structure_price=args.structure_price,
        )
        tokens = args.tokens if args.tokens is not None else args.size_usd
        request = OpenRequest(
            position_id=args.position_id or args.ticket,
            symbol=args.symbol or args.mint[:8],
            mint=args.mint,
            actual_fill_price=args.price,
            size_tokens=tokens,
            policy=policy,
            signal_price=args.signal_price,
            quote_price=args.price,
            entry_liquidity=args.liquidity,
            size_sol=args.size_sol,
            wallet_sol=args.wallet_sol,
        )
        try:
            position = engine.open_position(request)
        except TradeRejected as exc:
            return {
                "ok": False,
                "mode": "paper",
                "error": exc.reason,
                "realTrades": "NO",
                "privateKeyExposed": "NO",
            }
        return {
            "ok": True,
            "mode": "paper",
            "positionId": position.position_id,
            "stopState": position.stop_state,
            "stopType": position.stop_type,
            "entryFillPrice": position.entry_fill_price,
            "initialStopPrice": position.initial_stop_price,
            "currentStopPrice": position.current_stop_price,
            "hardStopPrice": position.hard_stop_price,
            "sizeSol": position.size_sol,
            "maxBuy": config.max_buy_sol,
            "maxExposure": config.max_exposure_sol,
            "minReserve": config.min_reserve_sol,
            "hardMaxLossPct": config.hard_max_loss_pct,
            "defaultStopLossPct": config.default_stop_loss_pct,
            "realTrades": "NO",
            "privateKeyExposed": "NO",
        }
    finally:
        engine.close()


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="Paper fill logger for PumpGrok")
    parser.add_argument("--desk", default="/workspace/trading-desk")
    parser.add_argument("--action", choices=["buy", "sell"], required=True)
    parser.add_argument("--ticket", required=True, help="Ticket ID e.g. SOL-20260827-001")
    parser.add_argument("--mint", required=True)
    parser.add_argument("--size-usd", type=float, required=True)
    parser.add_argument("--price", type=float, required=True)
    parser.add_argument("--slippage-bps", type=int, default=0)
    parser.add_argument("--reason", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--stop-type", default="")
    parser.add_argument("--stop-pct", type=float, default=None)
    parser.add_argument("--structure-price", type=float, default=None)
    parser.add_argument("--symbol", default="")
    parser.add_argument("--tokens", type=float, default=None)
    parser.add_argument("--liquidity", type=float, default=None)
    parser.add_argument("--position-id", default="")
    parser.add_argument("--signal-price", type=float, default=None)
    parser.add_argument("--size-sol", type=float, default=None)
    parser.add_argument("--wallet-sol", type=float, default=None)
    parser.add_argument("--db", default="")
    args = parser.parse_args(argv)

    record: Dict[str, Any] = {
        "ok": True,
        "mode": "paper",
        "action": args.action,
        "ticketId": args.ticket,
        "mint": args.mint,
        "sizeUsd": args.size_usd,
        "price": args.price,
        "slippageBps": args.slippage_bps,
        "reason": args.reason,
        "note": args.note,
        "utc": datetime.now(timezone.utc).isoformat(),
        "realTrades": "NO",
        "privateKeyExposed": "NO",
    }

    if args.action == "buy":
        gated = _paper_buy(args)
        record.update(gated)
        if not record.get("ok"):
            print(json.dumps(record, indent=2))
            return

    try:
        path = append_paper_fill(Path(args.desk), record)
        record["journalPath"] = path
    except Exception as exc:
        record["ok"] = False
        record["error"] = str(exc)

    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
