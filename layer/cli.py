"""CLIs: research, scan, paper, status, test.

The process prints JSON and does not sign or send.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from layer.config import REPO_ROOT, load_thresholds, trading_mode
from layer.cycle import EXECUTION_CLASS, PUBLIC_WALLET, percentile, run_cycle
from layer.db import ResearchDB
from layer.paper import PaperEngine
from layer.research_run import run_daily
from layer.scan import paper_path, scan_path
from layer.strategy import load_strategies


def default_db() -> Path:
    return REPO_ROOT / "data" / "pumpgrok-research.sqlite"


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_status(args: argparse.Namespace) -> int:
    thresholds = load_thresholds()
    mode = trading_mode()
    db = ResearchDB(args.db)
    try:
        health = db.read_health() or {}
        durations = db.pipeline_durations("cycle")
        cash = db.simulated_cash(thresholds.paper_wallet_sol)
        strategies = []
        for item in load_strategies(Path(args.strategies)):
            strategies.append(
                {
                    "strategy_id": item["strategy_id"],
                    "execution_class": EXECUTION_CLASS.get(item["strategy_id"], "RESEARCH"),
                    "active": True,
                    "status": item["status"],
                }
            )
        wallet = health.get("wallet") if isinstance(health.get("wallet"), dict) else {}
        payload = {
            "ok": True,
            "mode": mode["effective"],
            "requested_mode": mode["requested"],
            "live_refused": mode["live_refused"],
            "real_trades": False,
            "private_key_exposed": False,
            "live_send_disabled": True,
            "caps": {
                "MAX_BUY_SOL": thresholds.max_buy_sol,
                "MAX_TOTAL_EXPOSURE_SOL": thresholds.max_total_exposure_sol,
                "MIN_SOL_RESERVE": thresholds.min_sol_reserve,
                "PAPER_BUY_SOL": thresholds.paper_buy_sol,
            },
            "counts": db.counts(),
            "open_positions": [
                {
                    "position_id": row["position_id"],
                    "mint": row["mint"],
                    "status": row["status"],
                    "remaining_fraction": row["remaining_fraction"],
                    "size_sol": row["size_sol"],
                    "strategy_id": row.get("strategy_id"),
                }
                for row in db.open_positions()
            ],
            "open_paper_positions": len(db.open_positions()),
            "open_exposure_sol": db.open_exposure_sol(),
            "simulated_cash_sol": cash,
            "paper_trades": db.counts()["paper_trades"],
            "paper_exits": db.counts()["trade_exits"],
            "paper_net_pnl_sol": round(cash - thresholds.paper_wallet_sol, 9),
            "strategies": strategies,
            "latency_ms": {
                "p50": percentile(durations, 0.50),
                "p95": percentile(durations, 0.95),
                "samples": len(durations),
            },
            "grok": {
                "available": bool(health.get("grok_available")),
                "reason": health.get("grok_reason"),
            },
            "scanner": health.get("scanner") or {},
            "tokens_observed_last_cycle": health.get("tokens_observed"),
            "qualified_entries_last_cycle": health.get("qualified_entries"),
            "execution": "gated",
            "execution_engine": "complete",
            "transaction_build": "plan_only",
            "wire_ready": False,
            "local_signing": "gated",
            "jito": "implemented_default_transport_disabled",
            "bot_wallet": PUBLIC_WALLET,
            "bot_balance_lamports": wallet.get("balanceLamports"),
            "keypair_path_documented": "SOLANA_KEYPAIR_PATH",
            "signal": health.get("signal"),
        }
    finally:
        db.close()
    _print(payload)
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    raw = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
    if not raw.strip():
        _print({"ok": False, "error": "input_required", "real_trades": False})
        return 1
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _print({"ok": False, "error": "malformed_cycle", "real_trades": False})
        return 1
    db = ResearchDB(args.db)
    try:
        result = run_cycle(db, load_thresholds(), load_strategies(Path(args.strategies)), payload)
    finally:
        db.close()
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_scan(args: argparse.Namespace) -> int:
    if not args.input:
        _print({"ok": False, "error": "input_required"})
        return 1
    db = ResearchDB(args.db)
    try:
        strategy = None
        if args.strategy:
            matches = [
                item
                for item in load_strategies(Path(args.strategies))
                if item["strategy_id"] == args.strategy
            ]
            if not matches:
                _print({"ok": False, "error": "unknown_strategy", "strategy": args.strategy})
                return 1
            strategy = matches[0]
        result = scan_path(
            db,
            Path(args.input),
            load_thresholds(),
            evaluate_risk=not args.no_risk,
            strategy=strategy,
        )
    finally:
        db.close()
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_paper(args: argparse.Namespace) -> int:
    thresholds = load_thresholds()
    db = ResearchDB(args.db)
    strategy = None
    if args.strategy:
        matches = [
            item
            for item in load_strategies(Path(args.strategies))
            if item["strategy_id"] == args.strategy
        ]
        if not matches:
            _print({"ok": False, "error": "unknown_strategy", "strategy": args.strategy})
            db.close()
            return 1
        strategy = matches[0]
    engine = PaperEngine(db, thresholds, desk=Path(args.desk) if args.desk else None)
    try:
        if args.exit:
            if not args.position_id or not args.snapshot:
                _print({"ok": False, "error": "exit_requires_position_and_snapshot"})
                return 1
            snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
            result = engine.exit(args.position_id, snapshot, family=args.family)
        else:
            if not args.input:
                _print({"ok": False, "error": "input_required"})
                return 1
            result = paper_path(
                db,
                Path(args.input),
                thresholds,
                strategy=strategy,
                desk=Path(args.desk) if args.desk else None,
            )
    finally:
        db.close()
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_research(args: argparse.Namespace) -> int:
    db = ResearchDB(args.db)
    try:
        summary = run_daily(
            db,
            load_thresholds(),
            Path(args.strategies),
            day=args.day,
            out_path=Path(args.out) if args.out else None,
        )
    finally:
        db.close()
    _print(summary)
    return 0


def cmd_test(_args: argparse.Namespace) -> int:
    import unittest

    suite = unittest.defaultTestLoader.discover(str(REPO_ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PumpGrok research and paper layer")
    parser.add_argument("--db", default=str(default_db()))
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status")
    status.add_argument("--strategies", default=str(REPO_ROOT / "research" / "strategies"))
    status.set_defaults(func=cmd_status)

    cycle = sub.add_parser("cycle")
    cycle.add_argument("--input")
    cycle.add_argument("--strategies", default=str(REPO_ROOT / "research" / "strategies"))
    cycle.set_defaults(func=cmd_cycle)

    scan = sub.add_parser("scan")
    scan.add_argument("--input", required=True)
    scan.add_argument("--no-risk", action="store_true")
    scan.add_argument("--strategy")
    scan.add_argument("--strategies", default=str(REPO_ROOT / "research" / "strategies"))
    scan.set_defaults(func=cmd_scan)

    paper = sub.add_parser("paper")
    paper.add_argument("--input")
    paper.add_argument("--strategy")
    paper.add_argument("--strategies", default=str(REPO_ROOT / "research" / "strategies"))
    paper.add_argument("--desk")
    paper.add_argument("--exit", action="store_true")
    paper.add_argument("--position-id")
    paper.add_argument("--snapshot")
    paper.add_argument("--family")
    paper.set_defaults(func=cmd_paper)

    research = sub.add_parser("research")
    research.add_argument("--strategies", default=str(REPO_ROOT / "research" / "strategies"))
    research.add_argument("--day")
    research.add_argument("--out")
    research.set_defaults(func=cmd_research)

    test = sub.add_parser("test")
    test.set_defaults(func=cmd_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
