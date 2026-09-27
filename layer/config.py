"""Hard caps and configurable thresholds.

SOL ceilings and the reserve floor are code constants. Environment values and
strategy files may only tighten them. A higher buy cap, a higher exposure cap,
or a lower reserve is clamped back to the hard value.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parent.parent

# Immutable ceilings. Do not raise these from config, strategies, or agents.
HARD_MAX_BUY_SOL = 0.005
HARD_MAX_TOTAL_EXPOSURE_SOL = 0.03
HARD_MIN_SOL_RESERVE = 0.02
HARD_PAPER_BUY_SOL = 0.005

SUPPORTED_PROGRAMS = frozenset({"pump", "pumpswap", "jupiter"})

PROGRAM_ALIASES = {
    "pump": "pump",
    "pump.fun": "pump",
    "bonding_curve": "pump",
    "bonding-curve": "pump",
    "pumpswap": "pumpswap",
    "pump_swap": "pumpswap",
    "pump-swap": "pumpswap",
    "pump-amm": "pumpswap",
    "jupiter": "jupiter",
    "jup": "jupiter",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sol_round(value: float) -> float:
    return round(float(value), 9)


def exceeds(value: float, limit: float) -> bool:
    return sol_round(value) - sol_round(limit) > 1e-12


@dataclass(frozen=True)
class Thresholds:
    max_buy_sol: float = HARD_MAX_BUY_SOL
    max_total_exposure_sol: float = HARD_MAX_TOTAL_EXPOSURE_SOL
    min_sol_reserve: float = HARD_MIN_SOL_RESERVE
    paper_buy_sol: float = HARD_PAPER_BUY_SOL
    max_top_holder_pct: float = 0.15
    max_top10_holder_pct: float = 0.55
    min_liquidity_sol: float = 5.0
    max_candidate_age_sec: int = 180
    max_price_impact_bps: int = 300
    max_slippage_bps: int = 150
    paper_wallet_sol: float = 0.1
    clamped: tuple[str, ...] = ()

    def as_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["clamped"] = list(self.clamped)
        data["hard_caps_immutable"] = True
        return data


def _env_float(env: Mapping[str, str], key: str) -> float | None:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def load_thresholds(env: Mapping[str, str] | None = None) -> Thresholds:
    """Load thresholds and clamp them so hard caps cannot be weakened."""
    env = os.environ if env is None else env
    file_caps = _load_json(REPO_ROOT / "config" / "hard-caps.json")
    defaults = _load_json(REPO_ROOT / "config" / "risk-defaults.json")
    ceilings = defaults.get("ceilings") or {}
    clamped: list[str] = []

    def tighten_max(name: str, hard: float, file_value: float, env_key: str) -> float:
        chosen = min(hard, float(file_value))
        override = _env_float(env, env_key)
        if override is not None:
            if override > hard:
                clamped.append(env_key)
            chosen = min(chosen, override, hard)
        if float(file_value) > hard:
            clamped.append(f"file:{env_key}")
        return chosen

    def raise_floor(name: str, hard: float, env_key: str) -> float:
        chosen = hard
        override = _env_float(env, env_key)
        if override is not None:
            if override < hard:
                clamped.append(env_key)
            chosen = max(hard, override)
        return chosen

    max_buy = tighten_max(
        "max_buy", HARD_MAX_BUY_SOL, float(file_caps["MAX_BUY_SOL"]), "MAX_BUY_SOL"
    )
    max_exposure = tighten_max(
        "exposure",
        HARD_MAX_TOTAL_EXPOSURE_SOL,
        float(file_caps["MAX_TOTAL_EXPOSURE_SOL"]),
        "MAX_TOTAL_EXPOSURE_SOL",
    )
    paper_buy = tighten_max(
        "paper_buy",
        HARD_PAPER_BUY_SOL,
        float(file_caps["PAPER_BUY_SOL"]),
        "PAPER_BUY_SOL",
    )
    paper_buy = min(paper_buy, max_buy)
    reserve = raise_floor("reserve", HARD_MIN_SOL_RESERVE, "MIN_SOL_RESERVE")
    file_reserve = float(file_caps["MIN_SOL_RESERVE"])
    if file_reserve < HARD_MIN_SOL_RESERVE:
        clamped.append("file:MIN_SOL_RESERVE")
    reserve = max(reserve, file_reserve, HARD_MIN_SOL_RESERVE)

    def capped_pct(key: str, default: float, ceiling: float) -> float:
        chosen = float(defaults.get(key, default))
        override = _env_float(env, key.upper())
        if override is not None:
            chosen = override
        if chosen > ceiling:
            clamped.append(key)
            chosen = ceiling
        if chosen <= 0:
            clamped.append(key)
            chosen = default
        return chosen

    top1 = capped_pct("max_top_holder_pct", 0.15, float(ceilings["max_top_holder_pct"]))
    top10 = capped_pct(
        "max_top10_holder_pct", 0.55, float(ceilings["max_top10_holder_pct"])
    )

    liq_floor = float(ceilings["min_liquidity_sol_floor"])
    liq = float(defaults.get("min_liquidity_sol", 5.0))
    liq_override = _env_float(env, "MIN_LIQUIDITY_SOL")
    if liq_override is not None:
        liq = liq_override
    if liq < liq_floor:
        clamped.append("min_liquidity_sol")
        liq = liq_floor

    age_cap = int(ceilings["max_candidate_age_sec"])
    age = int(defaults.get("max_candidate_age_sec", 180))
    age_override = _env_float(env, "MAX_CANDIDATE_AGE_SEC")
    if age_override is not None:
        age = int(age_override)
    if age > age_cap or age <= 0:
        clamped.append("max_candidate_age_sec")
        age = min(age_cap, int(defaults.get("max_candidate_age_sec", 180)))

    impact_cap = int(ceilings["max_price_impact_bps"])
    impact = int(defaults.get("max_price_impact_bps", 300))
    impact_override = _env_float(env, "MAX_PRICE_IMPACT_BPS")
    if impact_override is not None:
        impact = int(impact_override)
    if impact > impact_cap or impact <= 0:
        clamped.append("max_price_impact_bps")
        impact = min(impact_cap, int(defaults.get("max_price_impact_bps", 300)))

    slip_cap = int(ceilings["max_slippage_bps"])
    slip = int(defaults.get("max_slippage_bps", 150))
    slip_override = _env_float(env, "MAX_SLIPPAGE_BPS")
    if slip_override is not None:
        slip = int(slip_override)
    if slip > slip_cap or slip <= 0:
        clamped.append("max_slippage_bps")
        slip = min(slip_cap, int(defaults.get("max_slippage_bps", 150)))

    wallet = float(defaults.get("paper_wallet_sol", 0.1))
    wallet_override = _env_float(env, "PAPER_WALLET_SOL")
    if wallet_override is not None and wallet_override > 0:
        wallet = wallet_override

    return Thresholds(
        max_buy_sol=max_buy,
        max_total_exposure_sol=max_exposure,
        min_sol_reserve=reserve,
        paper_buy_sol=paper_buy,
        max_top_holder_pct=top1,
        max_top10_holder_pct=top10,
        min_liquidity_sol=liq,
        max_candidate_age_sec=age,
        max_price_impact_bps=impact,
        max_slippage_bps=slip,
        paper_wallet_sol=wallet,
        clamped=tuple(clamped),
    )


def trading_mode(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """This Python process is always paper. A live request is recorded and refused."""
    env = os.environ if env is None else env
    requested = str(env.get("TRADING_MODE", "paper") or "paper").strip().lower()
    if requested not in {"paper", "research", "live"}:
        requested = "paper"
    return {
        "requested": requested,
        "effective": "paper",
        "live_refused": requested == "live",
        "real_trades": False,
        "reason": (
            "research layer refuses live trading"
            if requested == "live"
            else "TRADING_MODE paper"
        ),
    }
