"""Deterministic stop-loss execution for PumpGrok.

Paper and future live trading share this state machine. Stop decisions are
code in this package. Grok, CHIEF, and other prompts cannot disable, widen,
or remove a stop after entry.

A configured percent is a trigger threshold, not a guaranteed maximum loss.
Slippage, liquidity collapse, and failed transactions can make the realized
loss worse than that percent.
"""

from .config import HARD_MAX_LOSS_PCT, StopConfig
from .engine import EntryDeferred, StopEngine, StopProtectionError, TradeRejected
from .models import STOP_IS_TRIGGER_NOT_GUARANTEE

__all__ = [
    "HARD_MAX_LOSS_PCT",
    "STOP_IS_TRIGGER_NOT_GUARANTEE",
    "EntryDeferred",
    "StopConfig",
    "StopEngine",
    "StopProtectionError",
    "TradeRejected",
]
