"""Stop-loss records shared by paper and future live execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Logged with every fill and printed by status. The percent is a trigger.
STOP_IS_TRIGGER_NOT_GUARANTEE = (
    "Configured stop is a trigger threshold, not a guaranteed maximum loss. "
    "Slippage, liquidity collapse, and failed transactions can make realized "
    "loss worse than the configured percent."
)

# Impact at or above this percent is logged as its own condition. The exit
# still reconciles the actual fill; it does not pretend the trigger was the fill.
SEVERE_IMPACT_PCT = 30.0


class StopState:
    OPEN = "OPEN"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    STOP_SUBMITTED = "STOP_SUBMITTED"
    STOP_PENDING = "STOP_PENDING"
    CLOSED = "CLOSED"
    EXIT_FAILED_NO_ROUTE = "EXIT_FAILED_NO_ROUTE"


class StopType:
    FIXED_PERCENT_STOP = "FIXED_PERCENT_STOP"
    STRUCTURE_STOP = "STRUCTURE_STOP"
    TRAILING_STOP = "TRAILING_STOP"

    ALL = frozenset({FIXED_PERCENT_STOP, STRUCTURE_STOP, TRAILING_STOP})


class StopReason:
    FIXED_PERCENT_STOP = "FIXED_PERCENT_STOP"
    STRUCTURE_STOP = "STRUCTURE_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    HARD_EMERGENCY_STOP = "HARD_EMERGENCY_STOP"
    LIQUIDITY_DROP_EXIT = "LIQUIDITY_DROP_EXIT"
    POOL_GONE = "POOL_GONE"
    NO_ROUTE = "NO_ROUTE"
    SEVERE_IMPACT = "SEVERE_IMPACT"
    UNSELLABLE = "UNSELLABLE"
    PRICE_DATA_STALE = "PRICE_DATA_STALE"
    NORMAL_STRATEGY_EXIT = "NORMAL_STRATEGY_EXIT"
    TREND_BREAKDOWN = "TREND_BREAKDOWN"


class ExitPriority:
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    STOP_LOSS = "STOP_LOSS"
    NORMAL_STRATEGY_EXIT = "NORMAL_STRATEGY_EXIT"
    NEW_ENTRY = "NEW_ENTRY"
    HOLD = "HOLD"


PRIORITY_RANK = {
    ExitPriority.EMERGENCY_EXIT: 0,
    ExitPriority.STOP_LOSS: 1,
    ExitPriority.NORMAL_STRATEGY_EXIT: 2,
    ExitPriority.NEW_ENTRY: 3,
    ExitPriority.HOLD: 4,
}

BLOCKING_STATES = frozenset({
    StopState.STOP_TRIGGERED,
    StopState.STOP_SUBMITTED,
    StopState.STOP_PENDING,
    StopState.EXIT_FAILED_NO_ROUTE,
})


def qprice(value: float) -> float:
    return round(float(value), 12)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class StopPolicy:
    """Explicit protective stop supplied with an entry. Missing policy is rejected."""

    stop_type: str
    stop_loss_pct: float | None = None
    structure_price: float | None = None


@dataclass
class OpenRequest:
    position_id: str
    symbol: str
    mint: str
    actual_fill_price: float
    size_tokens: float
    policy: StopPolicy | None
    signal_price: float | None = None
    quote_price: float | None = None
    entry_liquidity: float | None = None
    opened_at: float | None = None
    size_sol: float | None = None
    wallet_sol: float | None = None


@dataclass
class Quote:
    price: float
    timestamp: float
    liquidity: float | None = None
    route_available: bool = True
    pool_exists: bool = True
    price_impact_pct: float = 0.0
    unsellable: bool = False
    source: str = "primary"


@dataclass
class MarketView:
    """One position's market marks. `advisor` is never called by the monitor."""

    position_id: str
    primary: Quote | None
    fallbacks: tuple[Quote, ...] = ()
    scanner_up: bool = True
    advisor: object | None = None


@dataclass
class Decision:
    position_id: str
    should_exit: bool
    priority: str
    reason: str
    stop_trigger_price: float | None
    quote_price: float | None
    safe: bool
    stale: bool
    state: str
    source: str = ""
    route_available: bool = True
    pool_exists: bool = True
    unsellable: bool = False
    price_impact_pct: float = 0.0
    log_codes: tuple[str, ...] = ()


@dataclass
class FillReceipt:
    position_id: str
    tokens_sold: float
    stop_trigger_price: float | None
    quote_price: float
    actual_fill_price: float
    slippage: float
    realized_loss: float
    realized_loss_pct: float
    blockhash: str
    relaxed_min_out: bool
    trading_mode: str
    notice: str = STOP_IS_TRIGGER_NOT_GUARANTEE


@dataclass
class StopPosition:
    position_id: str
    symbol: str
    mint: str
    trading_mode: str
    stop_state: str
    stop_type: str
    entry_fill_price: float
    signal_price: float | None
    entry_quote_price: float | None
    size_tokens: float
    size_sol: float
    remaining_tokens: float
    entry_liquidity: float | None
    current_liquidity: float | None
    stop_loss_pct: float
    initial_stop_price: float
    current_stop_price: float
    hard_stop_price: float
    high_water_price: float
    trailing_enabled: bool
    trailing_activation_pct: float
    trailing_distance_pct: float
    trailing_active: bool
    break_even_active: bool
    liquidity_drop_exit_pct: float
    stop_triggered_at: str | None
    stop_submitted_at: str | None
    stop_fill_price: float | None
    stop_reason: str | None
    stop_trigger_price: float | None
    exit_quote_price: float | None
    exit_actual_fill_price: float | None
    slippage: float | None
    realized_loss: float | None
    realized_loss_pct: float | None
    last_mark_price: float | None
    last_mark_at: str | None
    price_data_stale: bool
    safe: bool
    pending_sell_tokens: float
    submitted_blockhash: str | None
    attempt_count: int
    duplicate_events: int
    last_error: str | None
    liquidity_status: str | None
    opened_at: str
    updated_at: str
    closed_at: str | None

    def pnl_pct(self) -> float | None:
        if self.last_mark_price is None or self.entry_fill_price <= 0:
            return None
        return (self.last_mark_price - self.entry_fill_price) / self.entry_fill_price * 100.0

    def distance_to_stop_pct(self) -> float | None:
        if self.last_mark_price is None or self.last_mark_price <= 0:
            return None
        return (self.last_mark_price - self.current_stop_price) / self.last_mark_price * 100.0


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now
