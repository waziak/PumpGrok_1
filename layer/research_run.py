"""Daily research summary.

The summary may name hypotheses to paper-test. It cannot change hard caps,
promote the desk to live, or record key material.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from layer.config import Thresholds, trading_mode
from layer.db import ResearchDB
from layer.strategy import load_strategies


def run_daily(
    db: ResearchDB,
    thresholds: Thresholds,
    strategies_dir: Path,
    *,
    day: str | None = None,
    out_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mode = trading_mode(env)
    strategies = load_strategies(strategies_dir)
    attempted = []
    for strategy in strategies:
        attempted.extend(strategy.get("attempted_limit_changes") or [])
        db.upsert_strategy(
            {
                "strategy_id": strategy["strategy_id"],
                "version": strategy["version"],
                "hypothesis": strategy["hypothesis"],
                "params": strategy["params"],
                "status": "paper-only",
            }
        )
    summary = {
        "day": day,
        "mode": "paper",
        "requested_mode": mode["requested"],
        "live_refused": True if mode["live_refused"] else mode["requested"] == "live",
        "promote_to_live": False,
        "hard_limits_changed": False,
        "hard_caps": {
            "MAX_BUY_SOL": thresholds.max_buy_sol,
            "MAX_TOTAL_EXPOSURE_SOL": thresholds.max_total_exposure_sol,
            "MIN_SOL_RESERVE": thresholds.min_sol_reserve,
            "PAPER_BUY_SOL": thresholds.paper_buy_sol,
        },
        "attempted_limit_changes": attempted,
        "attempted_live_promotion": any(item.get("attempted_live_promotion") for item in strategies),
        "hypotheses": [
            {
                "strategy_id": item["strategy_id"],
                "version": item["version"],
                "status": "paper-only",
                "claim_status": "hypothesis",
                "source_url": item.get("source_url"),
                "hypothesis": item["hypothesis"],
            }
            for item in strategies
        ],
        "counts": db.counts(),
        "key_material_present": False,
        "real_trades": False,
        "notes": (
            "Hypotheses may be paper-tested. They cannot weaken hard risk limits, "
            "promote the desk to live, or carry key material."
        ),
    }
    # Live was requested: still record the refusal explicitly.
    if mode["live_refused"]:
        summary["live_refused"] = True
        summary["promote_to_live"] = False
    db.upsert_daily(day, summary)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        summary["wrote"] = str(out_path)
    return summary
