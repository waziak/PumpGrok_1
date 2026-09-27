"""Stop-loss bounds.

Environment values may tighten a cap. They cannot loosen one. The hard
emergency loss percent is not read from the environment at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Frozen in code. No prompt, config file, or env value may raise these.
HARD_MIN_STOP_LOSS_PCT = 5.0
HARD_MAX_STOP_LOSS_PCT = 30.0
HARD_MAX_LOSS_PCT = 25.0
HARD_MAX_LIQUIDITY_DROP_EXIT_PCT = 50.0
HARD_MIN_STALE_SECONDS = 1.0
HARD_MAX_STALE_SECONDS = 120.0

# Names this loader is allowed to read. Anything else in the environment,
# including key material, is ignored.
ENV_ALLOWLIST = (
    "DEFAULT_STOP_LOSS_PCT",
    "MIN_STOP_LOSS_PCT",
    "MAX_STOP_LOSS_PCT",
    "TRAILING_STOP_ENABLED",
    "TRAILING_STOP_ACTIVATION_PCT",
    "TRAILING_STOP_DISTANCE_PCT",
    "LIQUIDITY_DROP_EXIT_PCT",
    "PRICE_STALE_SECONDS",
    "TRADING_MODE",
)

_FALSE = {"0", "false", "no", "off"}


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def _env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class StopConfig:
    """Effective stop configuration after hard caps have been applied."""

    default_stop_loss_pct: float = 20.0
    min_stop_loss_pct: float = HARD_MIN_STOP_LOSS_PCT
    max_stop_loss_pct: float = HARD_MAX_STOP_LOSS_PCT
    hard_max_loss_pct: float = HARD_MAX_LOSS_PCT
    trailing_enabled: bool = True
    trailing_activation_pct: float = 20.0
    trailing_distance_pct: float = 12.0
    liquidity_drop_exit_pct: float = 50.0
    price_stale_seconds: float = 30.0
    trading_mode: str = "paper"
    # These stay false until a future change deliberately wires a sender.
    # Nothing in this package flips them on.
    real_trades: bool = False
    private_key_exposed: bool = False
    live_network_sends: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> StopConfig:
        source = os.environ if env is None else env
        used = {name: source[name] for name in ENV_ALLOWLIST if name in source}

        min_pct = _clamp(
            _env_float(used, "MIN_STOP_LOSS_PCT", HARD_MIN_STOP_LOSS_PCT),
            HARD_MIN_STOP_LOSS_PCT,
            HARD_MAX_STOP_LOSS_PCT,
        )
        max_pct = _clamp(
            _env_float(used, "MAX_STOP_LOSS_PCT", HARD_MAX_STOP_LOSS_PCT),
            min_pct,
            HARD_MAX_STOP_LOSS_PCT,
        )
        default_pct = _clamp(
            _env_float(used, "DEFAULT_STOP_LOSS_PCT", 20.0),
            min_pct,
            max_pct,
        )
        trailing_raw = str(used.get("TRAILING_STOP_ENABLED", "true")).strip().lower()
        trailing = trailing_raw not in _FALSE
        activation = _clamp(
            _env_float(used, "TRAILING_STOP_ACTIVATION_PCT", 20.0),
            0.0,
            500.0,
        )
        distance = _clamp(
            _env_float(used, "TRAILING_STOP_DISTANCE_PCT", 12.0),
            0.1,
            HARD_MAX_STOP_LOSS_PCT,
        )
        liquidity = _env_float(used, "LIQUIDITY_DROP_EXIT_PCT", 50.0)
        # A larger percent waits for a worse collapse, so it cannot exceed the cap.
        if liquidity > HARD_MAX_LIQUIDITY_DROP_EXIT_PCT:
            liquidity = HARD_MAX_LIQUIDITY_DROP_EXIT_PCT
        if liquidity < 1.0:
            liquidity = 1.0
        stale = _clamp(
            _env_float(used, "PRICE_STALE_SECONDS", 30.0),
            HARD_MIN_STALE_SECONDS,
            HARD_MAX_STALE_SECONDS,
        )
        mode = str(used.get("TRADING_MODE", "paper")).strip().lower() or "paper"
        if mode not in ("paper", "live"):
            mode = "paper"
        return cls(
            default_stop_loss_pct=default_pct,
            min_stop_loss_pct=min_pct,
            max_stop_loss_pct=max_pct,
            hard_max_loss_pct=HARD_MAX_LOSS_PCT,
            trailing_enabled=trailing,
            trailing_activation_pct=activation,
            trailing_distance_pct=distance,
            liquidity_drop_exit_pct=liquidity,
            price_stale_seconds=stale,
            trading_mode=mode,
            real_trades=False,
            private_key_exposed=False,
            live_network_sends=False,
        )
