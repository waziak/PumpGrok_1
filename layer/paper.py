"""Paper fills and exit families.

Costs are assumptions used so a simulated fill is not free. They are not a
live Jupiter, Pump.fun, or Jito quote, and nothing here is broadcast.

Exit families: structure invalidation, fixed stop, trailing stop, partial
profit, momentum loss, liquidity deterioration, volume breakdown, and fixed
R-multiple. A raw 2x price print is not an exit by itself.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from layer.config import Thresholds, sol_round
from layer.db import ResearchDB, utc_now
from layer.risk import (
    UNKNOWN_TEXT,
    RESERVE_FEE_SOL,
    RiskResult,
    evaluate,
    feature_rows,
    normalize_candidate,
)


# Documented paper assumptions. Pump fees on the live programs are dynamic
# (fee program pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ). 100 bps is a
# conservative stand-in, not a claim about the current tier.
PAPER_SLIPPAGE_BPS = 150
PAPER_PUMP_FEE_BPS = 100
BASE_SIGNATURE_FEE_LAMPORTS = 5_000
PRIORITY_FEE_LAMPORTS = 10_000
JITO_TIP_LAMPORTS = 10_000  # above the documented 1_000 lamport minimum; not sent
FILL_DELAY_SEC = 2

EXIT_FAMILIES = (
    "structure_invalidation",
    "fixed_stop",
    "trailing_stop",
    "partial_profit",
    "momentum_loss",
    "liquidity_deterioration",
    "volume_breakdown",
    "fixed_r_multiple",
)

DEFAULT_EXIT_PARAMS: dict[str, Any] = {
    "fixed_stop_pct": 0.25,
    "trail_pct": 0.20,
    "partial_r": 1.0,
    "partial_fraction": 0.50,
    "target_r": 2.0,
    "momentum_volume_ratio": 0.40,
    "liquidity_ratio": 0.50,
    "volume_floor_sol_5m": 0.5,
    "blind_2x": False,
    "ceiling_adaptive": False,
}

FULL_EXIT_ORDER = (
    "structure_invalidation",
    "liquidity_deterioration",
    "fixed_stop",
    "volume_breakdown",
    "momentum_loss",
    "trailing_stop",
    "fixed_r_multiple",
)


def cost_model(size_sol: float, slippage_bps: int = PAPER_SLIPPAGE_BPS) -> dict[str, Any]:
    pump_fee = sol_round(size_sol * PAPER_PUMP_FEE_BPS / 10_000)
    slippage = sol_round(size_sol * slippage_bps / 10_000)
    fixed = sol_round(
        (BASE_SIGNATURE_FEE_LAMPORTS + PRIORITY_FEE_LAMPORTS + JITO_TIP_LAMPORTS) / 1_000_000_000
    )
    effective = sol_round(size_sol - pump_fee - slippage)
    return {
        "slippage_bps": slippage_bps,
        "slippage_sol": slippage,
        "pump_fee_bps": PAPER_PUMP_FEE_BPS,
        "pump_fee_sol": pump_fee,
        "pump_fee_model": "assumption_100bps_dynamic_live_fees_differ",
        "network_fee_sol": sol_round(BASE_SIGNATURE_FEE_LAMPORTS / 1_000_000_000),
        "priority_fee_sol": sol_round(PRIORITY_FEE_LAMPORTS / 1_000_000_000),
        "jito_tip_sol": sol_round(JITO_TIP_LAMPORTS / 1_000_000_000),
        "jito_tip_sent": False,
        "fixed_sol": fixed,
        "effective_sol": effective,
        "delay_sec": FILL_DELAY_SEC,
        "reserve_fee_sol": RESERVE_FEE_SOL,
    }


class PaperEngine:
    def __init__(self, db: ResearchDB, thresholds: Thresholds, desk: Path | None = None) -> None:
        self.db = db
        self.thresholds = thresholds
        self.desk = desk

    def buy(
        self,
        payload: Any,
        *,
        now: datetime | None = None,
        strategy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        candidate, error = normalize_candidate(payload)
        if candidate is None:
            return {"ok": False, "error": error, "decision": "REJECT", "real_trades": False}

        if self.db.candidate_exists(candidate["candidate_id"]):
            return {
                "ok": False,
                "error": "duplicate_candidate",
                "candidate_id": candidate["candidate_id"],
                "real_trades": False,
            }

        wallet = self.db.simulated_cash(self.thresholds.paper_wallet_sol)
        exposure = self.db.open_exposure_sol()
        result = evaluate(
            candidate,
            self.thresholds,
            now=now,
            open_exposure_sol=exposure,
            wallet_sol=wallet,
            execution_delay_sec=FILL_DELAY_SEC,
        )
        strategy_note = None
        if result.passed and strategy is not None:
            from layer.strategy import match_strategy

            strategy_note = match_strategy(strategy, candidate["features"], candidate.get("program"))
            if not strategy_note["matched"]:
                result = RiskResult(
                    decision="REJECT",
                    reasons=["strategy_mismatch"],
                    override_attempt=result.override_attempt,
                    size_sol=None,
                    program=result.program,
                )

        status = "paper_open" if result.passed else "rejected"
        self._store_candidate(candidate, status)
        self.db.add_risk_decision(
            {
                "candidate_id": candidate["candidate_id"],
                "decision": result.decision,
                "reasons": result.reasons,
                "thresholds": self.thresholds.as_public_dict(),
                "override_attempt": result.override_attempt,
            }
        )
        if not result.passed or result.size_sol is None:
            return {
                "ok": True,
                "traded": False,
                "decision": "REJECT",
                "reasons": result.reasons,
                "override_attempt": result.override_attempt,
                "candidate_id": candidate["candidate_id"],
                "strategy": strategy_note,
                "real_trades": False,
                "mode": "paper",
            }

        price = float(candidate["features"]["price_sol"])
        slippage = candidate.get("slippage_bps") or PAPER_SLIPPAGE_BPS
        slippage = min(int(slippage), self.thresholds.max_slippage_bps)
        costs = cost_model(result.size_sol, slippage)
        if costs["effective_sol"] <= 0:
            return {"ok": False, "error": "costs_exceed_size", "real_trades": False}
        tokens = sol_round(costs["effective_sol"] / price)
        effective_entry = sol_round(result.size_sol / tokens)
        volume = candidate["features"].get("volume_sol_5m")
        liquidity = candidate["features"].get("liquidity_sol")
        position_id = f"pos-{candidate['candidate_id']}"
        trade_id = f"paper-{uuid.uuid4().hex[:12]}"
        receipt_id = f"paper:{trade_id}"
        opened = utc_now()
        state = {
            "tokens_initial": tokens,
            "remaining_tokens": tokens,
            "quote_price": price,
            "effective_entry_price": effective_entry,
            "entry_volume_sol_5m": volume if isinstance(volume, float) else None,
            "entry_liquidity_sol": liquidity if isinstance(liquidity, float) else None,
            "entry_market_cap_usd": candidate.get("market_cap_usd"),
            "fixed_costs_sol": costs["fixed_sol"],
            "realized_sol": 0.0,
            "simulated_wallet": True,
            "exit_params": _exit_params(strategy),
            "blind_2x": False,
        }
        fill = {
            "tokens": tokens,
            "effective_entry_price": effective_entry,
            "delay_sec": FILL_DELAY_SEC,
            "mode": "paper",
            "sent": False,
        }
        self.db.add_paper_trade(
            {
                "trade_id": trade_id,
                "candidate_id": candidate["candidate_id"],
                "mint": candidate["mint"],
                "side": "buy",
                "size_sol": result.size_sol,
                "price_sol": price,
                "costs": costs,
                "fill": fill,
            }
        )
        self.db.upsert_position(
            {
                "position_id": position_id,
                "candidate_id": candidate["candidate_id"],
                "mint": candidate["mint"],
                "size_sol": result.size_sol,
                "tokens": tokens,
                "entry_price": effective_entry,
                "remaining_fraction": 1.0,
                "status": "open",
                "opened_at": opened,
                "updated_at": opened,
                "high_water_price": price,
                "strategy_id": (strategy or {}).get("strategy_id") or candidate.get("strategy_id"),
                "state": state,
            }
        )
        self.db.add_receipt(
            {
                "receipt_id": receipt_id,
                "candidate_id": candidate["candidate_id"],
                "mode": "paper",
                "status": "simulated",
                "signature": None,
                "payload": {"trade_id": trade_id, "sent": False},
            }
        )
        journal = self._journal(candidate, result.size_sol, price, "buy", "paper open")
        return {
            "ok": True,
            "traded": True,
            "decision": "PASS",
            "mode": "paper",
            "real_trades": False,
            "sent": False,
            "candidate_id": candidate["candidate_id"],
            "position_id": position_id,
            "trade_id": trade_id,
            "receipt_id": receipt_id,
            "size_sol": result.size_sol,
            "tokens": tokens,
            "costs": costs,
            "journal": journal,
            "strategy": strategy_note,
        }

    def exit(
        self,
        position_id: str,
        snapshot: dict[str, Any],
        *,
        now: datetime | None = None,
        family: str | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        position = self.db.get_position(position_id)
        if position is None:
            return {"ok": False, "error": "unknown_position", "real_trades": False}
        if position["status"] == "closed":
            return {"ok": False, "error": "position_closed", "position_id": position_id, "real_trades": False}

        mark = _num(snapshot.get("price_sol"))
        if mark is not None and (position["high_water_price"] is None or mark > float(position["high_water_price"])):
            position["high_water_price"] = mark

        params = dict(position["state"].get("exit_params") or DEFAULT_EXIT_PARAMS)
        params = _maybe_tighten_ceiling(params, snapshot, position)
        signals = evaluate_exits(position, snapshot, params)
        chosen = _choose_signal(signals, family)
        self.db.upsert_position(_position_row(position, utc_now()))
        if chosen is None:
            return {
                "ok": True,
                "exited": False,
                "position_id": position_id,
                "signals": signals,
                "real_trades": False,
                "mode": "paper",
                "note": "no exit family fired; UNKNOWN inputs were not treated as signals",
            }

        fraction = float(chosen["fraction"])
        remaining = float(position["remaining_fraction"])
        sell_fraction_of_original = remaining * fraction
        tokens_initial = position["state"].get("tokens_initial")
        tokens_sold = None
        if isinstance(tokens_initial, (int, float)) and mark is not None:
            tokens_sold = sol_round(float(tokens_initial) * sell_fraction_of_original)
        costs = cost_model(0.0)
        proceeds = None
        if tokens_sold is not None and mark is not None:
            gross = tokens_sold * mark
            net = gross * (1 - costs["slippage_bps"] / 10_000) * (1 - PAPER_PUMP_FEE_BPS / 10_000)
            costs["gross_sol"] = sol_round(gross)
            costs["proceeds_before_fixed_sol"] = sol_round(net)
            proceeds = sol_round(net - costs["fixed_sol"])
        exit_id = f"exit-{uuid.uuid4().hex[:12]}"
        self.db.add_exit(
            {
                "exit_id": exit_id,
                "position_id": position_id,
                "family": chosen["family"],
                "fraction": fraction,
                "proceeds_sol": proceeds,
                "costs": costs,
                "reason": chosen["family"],
            }
        )
        new_remaining = sol_round(remaining * (1 - fraction))
        position["remaining_fraction"] = new_remaining
        if isinstance(tokens_initial, (int, float)):
            position["state"]["remaining_tokens"] = sol_round(float(tokens_initial) * new_remaining)
        if proceeds is not None:
            position["state"]["realized_sol"] = sol_round(
                float(position["state"].get("realized_sol") or 0) + proceeds
            )
        closed = new_remaining <= 1e-9
        position["status"] = "closed" if closed else "partial"
        if closed:
            position["remaining_fraction"] = 0.0
        self.db.upsert_position(_position_row(position, utc_now()))
        if closed:
            self.db.set_status(position["candidate_id"], "closed")
        receipt_id = f"paper-exit:{exit_id}"
        self.db.add_receipt(
            {
                "receipt_id": receipt_id,
                "candidate_id": position["candidate_id"],
                "mode": "paper",
                "status": "simulated",
                "signature": None,
                "payload": {"exit_id": exit_id, "family": chosen["family"], "sent": False},
            }
        )
        return {
            "ok": True,
            "exited": True,
            "full": closed,
            "partial": not closed,
            "family": chosen["family"],
            "fraction": fraction,
            "proceeds_sol": proceeds,
            "proceeds_unknown": proceeds is None,
            "position_id": position_id,
            "exit_id": exit_id,
            "receipt_id": receipt_id,
            "status": position["status"],
            "remaining_fraction": position["remaining_fraction"],
            "costs": costs,
            "real_trades": False,
            "sent": False,
            "mode": "paper",
            "blind_2x": False,
        }

    def _store_candidate(self, candidate: dict[str, Any], status: str) -> None:
        payload = dict(candidate["payload"])
        self.db.insert_candidate(
            {
                "candidate_id": candidate["candidate_id"],
                "mint": candidate["mint"],
                "symbol": candidate["symbol"],
                "name": candidate["name"],
                "source": candidate["source"],
                "program": candidate["program"],
                "market_cap_usd": candidate["market_cap_usd"],
                "observed_at": candidate["observed_at"],
                "payload_json": json.dumps(payload, sort_keys=True),
                "status": status,
            }
        )
        self.db.upsert_features(
            candidate["candidate_id"],
            feature_rows(candidate, candidate["source"]),
        )
        for review in candidate["agent_reviews"]:
            self.db.add_review(candidate["candidate_id"], review)

    def _journal(
        self,
        candidate: dict[str, Any],
        size_sol: float,
        price: float,
        action: str,
        reason: str,
    ) -> str | None:
        if self.desk is None:
            return None
        import importlib.util

        path = Path(__file__).resolve().parent.parent / "tools" / "paper_sim.py"
        spec = importlib.util.spec_from_file_location("pumpgrok_paper_sim", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        record = {
            "action": action,
            "ticketId": f"PAPER-{candidate['candidate_id']}",
            "mint": candidate["mint"],
            "sizeUsd": "n/a",
            "sizeSol": size_sol,
            "price": price,
            "slippageBps": PAPER_SLIPPAGE_BPS,
            "reason": reason,
            "note": "paper layer simulated fill; not a live send",
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return module.append_paper_fill(self.desk, record)


def evaluate_exits(
    position: dict[str, Any],
    snapshot: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return fired signals. Missing inputs do not fire."""
    params = {**DEFAULT_EXIT_PARAMS, **(params or {})}
    if params.get("blind_2x") is True:
        # Strategies are not allowed to install a blind 2x rule.
        params["blind_2x"] = False
    signals: list[dict[str, Any]] = []
    price = _num(snapshot.get("price_sol"))
    entry = _num(position.get("entry_price"))
    high = _num(position.get("high_water_price"))
    r_multiple = _r_multiple(price, entry, float(params["fixed_stop_pct"]))

    structure = snapshot.get("structure_valid", UNKNOWN_TEXT)
    if structure is False:
        signals.append(_signal("structure_invalidation", 1.0))

    entry_liq = _num((position.get("state") or {}).get("entry_liquidity_sol"))
    liq = _num(snapshot.get("liquidity_sol"))
    if entry_liq is not None and liq is not None and entry_liq > 0:
        if liq < entry_liq * float(params["liquidity_ratio"]):
            signals.append(_signal("liquidity_deterioration", 1.0))

    if price is not None and entry is not None:
        if price <= entry * (1 - float(params["fixed_stop_pct"])):
            signals.append(_signal("fixed_stop", 1.0))

    entry_vol = _num((position.get("state") or {}).get("entry_volume_sol_5m"))
    volume = _num(snapshot.get("volume_sol_5m"))
    floor = float(params["volume_floor_sol_5m"])
    if entry_vol is not None and volume is not None and entry_vol >= floor and volume < floor:
        signals.append(_signal("volume_breakdown", 1.0))

    if (
        entry_vol is not None
        and volume is not None
        and entry_vol > 0
        and price is not None
        and high is not None
        and volume / entry_vol < float(params["momentum_volume_ratio"])
        and price < high
    ):
        signals.append(_signal("momentum_loss", 1.0))

    trail = float(params["trail_pct"])
    if price is not None and high is not None and entry is not None and high > entry:
        if price <= high * (1 - trail):
            signals.append(_signal("trailing_stop", 1.0))

    if r_multiple is not None and r_multiple >= float(params["target_r"]):
        signals.append(_signal("fixed_r_multiple", 1.0, extra={"r_multiple": r_multiple}))

    remaining = float(position.get("remaining_fraction") or 0)
    already_partial = remaining < 0.999
    if (
        r_multiple is not None
        and r_multiple >= float(params["partial_r"])
        and not already_partial
        and r_multiple < float(params["target_r"])
    ):
        signals.append(
            _signal(
                "partial_profit",
                float(params["partial_fraction"]),
                extra={"r_multiple": r_multiple},
            )
        )
    return signals


def _r_multiple(price: float | None, entry: float | None, stop_pct: float) -> float | None:
    if price is None or entry is None or entry <= 0 or stop_pct <= 0:
        return None
    risk_unit = entry * stop_pct
    if risk_unit <= 0:
        return None
    return (price - entry) / risk_unit


def _signal(family: str, fraction: float, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"family": family, "fraction": fraction, "fired": True}
    if extra:
        row.update(extra)
    return row


def _choose_signal(signals: list[dict[str, Any]], forced: str | None) -> dict[str, Any] | None:
    by_family = {item["family"]: item for item in signals}
    if forced:
        if forced not in EXIT_FAMILIES:
            return None
        return by_family.get(forced)
    for family in FULL_EXIT_ORDER:
        if family in by_family:
            return by_family[family]
    return by_family.get("partial_profit")


def _exit_params(strategy: dict[str, Any] | None) -> dict[str, Any]:
    params = dict(DEFAULT_EXIT_PARAMS)
    if not strategy:
        return params
    exits = strategy.get("exits")
    if isinstance(exits, dict):
        for key in DEFAULT_EXIT_PARAMS:
            if key in exits and key != "blind_2x":
                params[key] = exits[key]
        if "ceiling_adaptive" in exits:
            params["ceiling_adaptive"] = bool(exits.get("ceiling_adaptive"))
        if "market_cap_usd_max" in (strategy.get("entry") or {}):
            params["market_cap_ceiling"] = strategy["entry"]["market_cap_usd_max"]
    params["blind_2x"] = False
    return params


def _maybe_tighten_ceiling(
    params: dict[str, Any], snapshot: dict[str, Any], position: dict[str, Any]
) -> dict[str, Any]:
    if not params.get("ceiling_adaptive"):
        return params
    ceiling = _num(params.get("market_cap_ceiling"))
    mcap = _num(snapshot.get("market_cap_usd"))
    if ceiling is None or mcap is None:
        return params
    if mcap >= ceiling * 0.9:
        tightened = dict(params)
        tightened["trail_pct"] = float(params["trail_pct"]) * 0.5
        return tightened
    return params


def _position_row(position: dict[str, Any], updated: str) -> dict[str, Any]:
    return {
        "position_id": position["position_id"],
        "candidate_id": position["candidate_id"],
        "mint": position["mint"],
        "size_sol": position["size_sol"],
        "tokens": position.get("tokens"),
        "entry_price": position.get("entry_price"),
        "remaining_fraction": position["remaining_fraction"],
        "status": position["status"],
        "opened_at": position["opened_at"],
        "updated_at": updated,
        "high_water_price": position.get("high_water_price"),
        "strategy_id": position.get("strategy_id"),
        "state": position["state"],
    }


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return float(value)

