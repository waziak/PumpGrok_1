"""Deterministic stop-loss state machine.

States
------
OPEN
    Protective stop is armed from the actual fill. No exit is in flight.
STOP_TRIGGERED
    A hard emergency stop or strategy stop has fired. The position is not closed.
STOP_SUBMITTED
    A sell has been handed to the executor. Waiting for a receipt.
STOP_PENDING
    The sell failed or the receipt never arrived. Retry with a new blockhash.
    The position stays open and is not marked closed.
EXIT_FAILED_NO_ROUTE
    The exit cannot be executed (pool gone, no route, or unsellable). Tokens
    remain. The position is not marked closed.
CLOSED
    The sell receipt reconciled. Terminal.

Exit priority, highest first: EMERGENCY EXIT, STOP LOSS, NORMAL STRATEGY
EXIT, NEW ENTRY. A new buy is not accepted while a stop sell is outstanding.

Stops are measured from ``actual_fill_price``. Signal and quote prices are
stored and ignored for the threshold. After entry, no caller can disable,
widen, or remove the stop. The engine itself may only tighten it (trail up,
or break-even after a partial).

The configured percent is a trigger, not a guaranteed max loss.
"""

from __future__ import annotations

from .config import HARD_MAX_LOSS_PCT, StopConfig
from .models import (
    BLOCKING_STATES,
    STOP_IS_TRIGGER_NOT_GUARANTEE,
    Clock,
    Decision,
    ExitPriority,
    FillReceipt,
    MarketView,
    OpenRequest,
    Quote,
    StopPolicy,
    StopPosition,
    StopReason,
    StopState,
    StopType,
    iso,
    qprice,
)
from .store import StopStore

_LLM_ACTORS = frozenset({
    "grok",
    "llm",
    "chief",
    "prompt",
    "model",
    "agent",
})


class TradeRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StopProtectionError(Exception):
    pass


class EntryDeferred(Exception):
    """A new entry would run before an outstanding stop exit."""


class ExitPriorityError(Exception):
    """A normal strategy exit must not outrank a fired stop."""


class LiveTradingDisabled(Exception):
    pass


def _is_llm_actor(actor: str) -> bool:
    lowered = actor.strip().lower()
    if lowered in _LLM_ACTORS:
        return True
    return any(token in lowered for token in _LLM_ACTORS)


def _clamp_pct(raw: float, config: StopConfig) -> float:
    pct = float(raw)
    if pct < config.min_stop_loss_pct:
        pct = config.min_stop_loss_pct
    if pct > config.max_stop_loss_pct:
        pct = config.max_stop_loss_pct
    return pct


def _fresh(quote: Quote | None, now: float, window: float) -> bool:
    if quote is None or quote.price <= 0:
        return False
    return (now - quote.timestamp) <= window + 1e-9


def resolve_quote(
    primary: Quote | None,
    fallbacks: tuple[Quote, ...],
    now: float,
    window: float,
) -> tuple[Quote | None, bool, str]:
    """Pick a mark. Stale primary data falls through to a fresh provider.

    When every provider is stale, the newest quote is returned with
    ``stale=True``. Callers must not treat that position as safe.
    """
    if _fresh(primary, now, window):
        assert primary is not None
        return primary, False, primary.source or "primary"
    for fallback in fallbacks:
        if _fresh(fallback, now, window):
            return fallback, False, fallback.source or "fallback"
    candidates = [quote for quote in (primary, *fallbacks) if quote is not None and quote.price > 0]
    if not candidates:
        return None, True, ""
    newest = max(candidates, key=lambda quote: quote.timestamp)
    return newest, True, newest.source or "stale"


class StopEngine:
    """Shared by paper execution and any future live adapter."""

    def __init__(self, store: StopStore, config: StopConfig, clock: Clock | None = None) -> None:
        self.store = store
        self.config = config
        self.clock = clock or Clock()
        self.private_key_exposed = False
        self.real_trades = False

    def close(self) -> None:
        self.store.close()

    def get(self, position_id: str) -> StopPosition:
        pos = self.store.get(position_id)
        if pos is None:
            raise KeyError(position_id)
        return pos

    def _now(self, now: float | None) -> float:
        return self.clock() if now is None else now

    def _stamp(self, pos: StopPosition, now: float) -> None:
        pos.updated_at = iso(now)

    def save(self, pos: StopPosition, now: float | None = None) -> None:
        self._stamp(pos, self._now(now))
        self.store.save(pos)

    def log_event(self, position_id: str, event: str, detail: object, now: float | None = None) -> None:
        self.store.add_event(position_id, event, detail, iso(self._now(now)))

    def entries_blocked(self) -> bool:
        return any(pos.stop_state in BLOCKING_STATES for pos in self.store.load_active())

    def request_entry(self, request: OpenRequest) -> StopPosition:
        if self.entries_blocked():
            raise EntryDeferred("stop exit outranks a new entry")
        return self.open_position(request)

    def open_position(self, request: OpenRequest, now: float | None = None) -> StopPosition:
        """Arm a position. Rejects the trade when no explicit stop is provided."""
        if self.config.real_trades or self.config.live_network_sends:
            raise LiveTradingDisabled("REAL TRADES=NO; live network sends are disabled")
        if request.actual_fill_price is None or request.actual_fill_price <= 0:
            raise TradeRejected("fill_price_required")
        if request.size_tokens <= 0:
            raise TradeRejected("size_required")
        if not request.position_id:
            raise TradeRejected("position_id_required")
        if self.store.get(request.position_id) is not None:
            raise TradeRejected("position_exists")

        stop_type, stop_pct, initial_stop = self._normalize_policy(
            request.policy, request.actual_fill_price
        )
        moment = self._now(request.opened_at if request.opened_at is not None else now)
        stamp = iso(moment)
        fill = qprice(request.actual_fill_price)
        # Hard emergency uses the code constant, not a mutable config field.
        hard = qprice(fill * (1.0 - HARD_MAX_LOSS_PCT / 100.0))
        symbol = request.symbol.strip() or request.mint[:8]
        pos = StopPosition(
            position_id=request.position_id,
            symbol=symbol,
            mint=request.mint,
            trading_mode=self.config.trading_mode,
            stop_state=StopState.OPEN,
            stop_type=stop_type,
            entry_fill_price=fill,
            signal_price=request.signal_price,
            entry_quote_price=request.quote_price,
            size_tokens=qprice(request.size_tokens),
            remaining_tokens=qprice(request.size_tokens),
            entry_liquidity=request.entry_liquidity,
            current_liquidity=request.entry_liquidity,
            stop_loss_pct=stop_pct,
            initial_stop_price=initial_stop,
            current_stop_price=initial_stop,
            hard_stop_price=hard,
            high_water_price=fill,
            trailing_enabled=bool(self.config.trailing_enabled),
            trailing_activation_pct=self.config.trailing_activation_pct,
            trailing_distance_pct=self.config.trailing_distance_pct,
            trailing_active=False,
            break_even_active=False,
            liquidity_drop_exit_pct=self.config.liquidity_drop_exit_pct,
            stop_triggered_at=None,
            stop_submitted_at=None,
            stop_fill_price=None,
            stop_reason=None,
            stop_trigger_price=None,
            exit_quote_price=None,
            exit_actual_fill_price=None,
            slippage=None,
            realized_loss=None,
            realized_loss_pct=None,
            last_mark_price=None,
            last_mark_at=None,
            price_data_stale=False,
            safe=False,
            pending_sell_tokens=0.0,
            submitted_blockhash=None,
            attempt_count=0,
            duplicate_events=0,
            last_error=None,
            liquidity_status=None,
            opened_at=stamp,
            updated_at=stamp,
            closed_at=None,
        )
        self.store.save(pos)
        self.log_event(
            pos.position_id,
            "OPENED",
            {
                "entry_fill_price": pos.entry_fill_price,
                "signal_price": pos.signal_price,
                "stop_type": pos.stop_type,
                "stop_loss_pct": pos.stop_loss_pct,
                "initial_stop_price": pos.initial_stop_price,
                "hard_stop_price": pos.hard_stop_price,
                "notice": STOP_IS_TRIGGER_NOT_GUARANTEE,
            },
            moment,
        )
        return pos

    def _normalize_policy(self, policy: StopPolicy | None, fill: float) -> tuple[str, float, float]:
        if policy is None or not str(policy.stop_type or "").strip():
            raise TradeRejected("stop_required")
        stop_type = policy.stop_type.strip()
        if stop_type not in StopType.ALL:
            raise TradeRejected("stop_required")

        if stop_type == StopType.STRUCTURE_STOP:
            structure = policy.structure_price
            if structure is None:
                raise TradeRejected("stop_required")
            if structure <= 0 or structure >= fill:
                raise TradeRejected("structure_stop_not_protective")
            raw_pct = (fill - structure) / fill * 100.0
            pct = _clamp_pct(raw_pct, self.config)
            if abs(raw_pct - pct) <= 1e-9:
                price = qprice(structure)
            else:
                price = qprice(fill * (1.0 - pct / 100.0))
            return stop_type, pct, price

        if policy.stop_loss_pct is None:
            if stop_type != StopType.TRAILING_STOP:
                raise TradeRejected("stop_required")
            raw_pct = self.config.default_stop_loss_pct
        else:
            raw_pct = policy.stop_loss_pct
        pct = _clamp_pct(raw_pct, self.config)
        price = qprice(fill * (1.0 - pct / 100.0))
        return stop_type, pct, price

    def mutate(
        self,
        position_id: str,
        *,
        actor: str,
        action: str,
        new_stop_price: float | None = None,
        new_stop_pct: float | None = None,
        now: float | None = None,
    ) -> None:
        """Reject every external attempt to loosen or clear a stop.

        Trail-up and break-even go through engine methods, not this door.
        Grok, CHIEF, LLM, and prompt actors are rejected even for a tighten.
        """
        pos = self.get(position_id)
        moment = self._now(now)
        loosening = action in {"disable", "remove", "widen"}
        if new_stop_price is not None and new_stop_price < pos.current_stop_price - 1e-12:
            loosening = True
        if new_stop_pct is not None and new_stop_pct > pos.stop_loss_pct + 1e-12:
            loosening = True
        self.log_event(
            position_id,
            "MUTATION_REJECTED",
            {"actor": actor, "action": action, "llm": _is_llm_actor(actor), "loosening": loosening},
            moment,
        )
        if _is_llm_actor(actor):
            raise StopProtectionError(
                "LLM/CHIEF/Grok/prompt cannot disable, widen, or remove a stop after entry"
            )
        raise StopProtectionError(
            "stop cannot be disabled, widened, or removed after entry"
        )

    def apply_partial_take_profit(
        self,
        position_id: str,
        tokens: float,
        fill_price: float,
        *,
        to_breakeven: bool = True,
        now: float | None = None,
    ) -> StopPosition:
        """Reduce size. The remainder keeps a stop, optionally at break-even."""
        pos = self.get(position_id)
        moment = self._now(now)
        if pos.stop_state != StopState.OPEN:
            raise StopProtectionError("partial take-profit is only valid while OPEN")
        if tokens <= 0 or tokens >= pos.remaining_tokens:
            raise TradeRejected("partial size must leave a protected remainder")
        pos.remaining_tokens = qprice(pos.remaining_tokens - tokens)
        if pos.remaining_tokens <= 0 or not pos.stop_type or pos.current_stop_price <= 0:
            raise StopProtectionError("remainder would be unprotected")
        if to_breakeven and pos.entry_fill_price > pos.current_stop_price:
            pos.current_stop_price = pos.entry_fill_price
            pos.break_even_active = True
        elif to_breakeven:
            pos.break_even_active = True
        self.log_event(
            position_id,
            "PARTIAL_TAKE_PROFIT",
            {
                "tokens_sold": tokens,
                "fill_price": fill_price,
                "remaining_tokens": pos.remaining_tokens,
                "current_stop_price": pos.current_stop_price,
                "break_even": pos.break_even_active,
            },
            moment,
        )
        self.save(pos, moment)
        return pos

    def tighten_stop(
        self,
        position_id: str,
        new_stop_price: float,
        *,
        actor: str,
        reason: str,
        now: float | None = None,
    ) -> StopPosition:
        """Raise a stop. A lower price is ignored so the trail cannot loosen."""
        if actor != "profit-engine":
            raise StopProtectionError("only the profit engine may tighten a stop")
        pos = self.get(position_id)
        moment = self._now(now)
        if pos.stop_state != StopState.OPEN:
            return pos
        if new_stop_price <= pos.current_stop_price + 1e-12:
            return pos
        pos.current_stop_price = qprice(new_stop_price)
        if pos.current_stop_price >= pos.entry_fill_price:
            pos.break_even_active = True
        self.log_event(
            position_id,
            "STOP_TIGHTENED",
            {"stop": pos.current_stop_price, "reason": reason},
            moment,
        )
        self.save(pos, moment)
        return pos

    def close_for_trend_breakdown(
        self,
        position_id: str,
        fill_price: float,
        *,
        actor: str,
        now: float | None = None,
    ) -> float:
        """Flatten a runner after confirmed trend breakdown. Not a profit target."""
        if actor != "profit-engine":
            raise StopProtectionError("only the profit engine may close for trend breakdown")
        pos = self.get(position_id)
        moment = self._now(now)
        if pos.stop_state != StopState.OPEN:
            raise StopProtectionError("trend breakdown close is only valid while OPEN")
        sold = pos.remaining_tokens
        if sold <= 0:
            return 0.0
        pos.remaining_tokens = 0.0
        pos.stop_state = StopState.CLOSED
        pos.stop_reason = StopReason.TREND_BREAKDOWN
        pos.closed_at = iso(moment)
        pos.exit_quote_price = qprice(fill_price)
        pos.exit_actual_fill_price = qprice(fill_price)
        pos.last_mark_price = qprice(fill_price)
        pos.last_mark_at = iso(moment)
        self.log_event(
            position_id,
            StopReason.TREND_BREAKDOWN,
            {"tokens_sold": sold, "fill_price": fill_price},
            moment,
        )
        self.save(pos, moment)
        return sold

    def on_mark(self, view: MarketView, now: float | None = None) -> Decision:
        """Apply one mark. Does not call an LLM and does not send an order."""
        pos = self.get(view.position_id)
        moment = self._now(now)
        if pos.stop_state == StopState.CLOSED:
            return self._decision(pos, False, ExitPriority.HOLD, "HOLD", None, None, False, False, "")

        quote, stale, source = resolve_quote(
            view.primary,
            view.fallbacks,
            moment,
            self.config.price_stale_seconds,
        )
        codes: list[str] = []
        if quote is None:
            pos.price_data_stale = True
            pos.safe = False
            self.log_event(pos.position_id, StopReason.PRICE_DATA_STALE, {"source": "none"}, moment)
            self.save(pos, moment)
            return self._decision(
                pos, False, ExitPriority.HOLD, StopReason.PRICE_DATA_STALE,
                None, None, False, True, "",
            )

        pos.last_mark_price = qprice(quote.price)
        pos.last_mark_at = iso(moment)
        if quote.liquidity is not None:
            pos.current_liquidity = quote.liquidity
        pos.price_data_stale = stale
        if stale:
            codes.append(StopReason.PRICE_DATA_STALE)
            self.log_event(
                pos.position_id,
                StopReason.PRICE_DATA_STALE,
                {"source": source, "price": quote.price, "timestamp": quote.timestamp},
                moment,
            )
        if not stale:
            self._update_trailing(pos, quote.price)

        should, priority, reason, trigger = self._evaluate(pos, quote)
        # A fired stop stays armed until the receipt or a hard no-route.
        # A later print back above the stop does not cancel the exit.
        if pos.stop_state in (StopState.STOP_TRIGGERED, StopState.STOP_PENDING) and not should:
            should = True
            reason = pos.stop_reason or StopReason.FIXED_PERCENT_STOP
            trigger = (
                pos.stop_trigger_price
                if pos.stop_trigger_price is not None
                else pos.current_stop_price
            )
            if reason in (
                StopReason.HARD_EMERGENCY_STOP,
                StopReason.LIQUIDITY_DROP_EXIT,
                StopReason.POOL_GONE,
                StopReason.NO_ROUTE,
                StopReason.UNSELLABLE,
            ):
                priority = ExitPriority.EMERGENCY_EXIT
            else:
                priority = ExitPriority.STOP_LOSS
        # A stale print that has not breached is not a clean bill of health.
        pos.safe = (not stale) and (not should)
        if should and pos.stop_state == StopState.OPEN:
            pos.stop_state = StopState.STOP_TRIGGERED
            pos.stop_triggered_at = iso(moment)
            pos.stop_reason = reason
            pos.stop_trigger_price = trigger
            self.log_event(
                pos.position_id,
                "STOP_TRIGGERED",
                {
                    "reason": reason,
                    "stop_trigger_price": trigger,
                    "quote_price": quote.price,
                    "priority": priority,
                },
                moment,
            )
        elif should and pos.stop_state == StopState.STOP_TRIGGERED:
            pos.stop_reason = pos.stop_reason or reason
            if pos.stop_trigger_price is None:
                pos.stop_trigger_price = trigger
        if (
            pos.stop_state == StopState.EXIT_FAILED_NO_ROUTE
            and should
            and quote.route_available
            and quote.pool_exists
            and not quote.unsellable
        ):
            pos.stop_state = StopState.STOP_PENDING
            pos.last_error = None

        if reason == StopReason.LIQUIDITY_DROP_EXIT:
            self.log_event(
                pos.position_id,
                StopReason.LIQUIDITY_DROP_EXIT,
                {
                    "entry_liquidity": pos.entry_liquidity,
                    "current_liquidity": quote.liquidity,
                },
                moment,
            )
        self.save(pos, moment)
        return self._decision(
            pos,
            should,
            priority,
            reason if should else ExitPriority.HOLD,
            trigger if should else pos.current_stop_price,
            quote.price,
            pos.safe,
            stale,
            source,
            route_available=quote.route_available,
            pool_exists=quote.pool_exists,
            unsellable=quote.unsellable,
            price_impact_pct=quote.price_impact_pct,
            log_codes=tuple(codes),
        )

    def _evaluate(
        self, pos: StopPosition, quote: Quote
    ) -> tuple[bool, str, str, float | None]:
        price = quote.price
        if price <= pos.hard_stop_price:
            return True, ExitPriority.EMERGENCY_EXIT, StopReason.HARD_EMERGENCY_STOP, pos.hard_stop_price
        if (
            pos.entry_liquidity
            and pos.entry_liquidity > 0
            and quote.liquidity is not None
        ):
            drop = (pos.entry_liquidity - quote.liquidity) / pos.entry_liquidity * 100.0
            if drop + 1e-9 >= pos.liquidity_drop_exit_pct:
                return True, ExitPriority.EMERGENCY_EXIT, StopReason.LIQUIDITY_DROP_EXIT, qprice(price)
        if price <= pos.current_stop_price:
            if pos.trailing_active:
                reason = StopReason.TRAILING_STOP
            elif pos.stop_type == StopType.TRAILING_STOP:
                reason = StopReason.FIXED_PERCENT_STOP
            else:
                reason = pos.stop_type
            return True, ExitPriority.STOP_LOSS, reason, pos.current_stop_price
        return False, ExitPriority.HOLD, ExitPriority.HOLD, None

    def _update_trailing(self, pos: StopPosition, price: float) -> None:
        if not pos.trailing_enabled:
            return
        if price > pos.high_water_price:
            pos.high_water_price = qprice(price)
        activation = pos.entry_fill_price * (1.0 + pos.trailing_activation_pct / 100.0)
        if not pos.trailing_active and pos.high_water_price + 1e-12 >= activation:
            pos.trailing_active = True
        if not pos.trailing_active:
            return
        trail = qprice(pos.high_water_price * (1.0 - pos.trailing_distance_pct / 100.0))
        # High-water only rises, and the trailed stop only tightens.
        if trail > pos.current_stop_price:
            pos.current_stop_price = trail

    def _decision(
        self,
        pos: StopPosition,
        should: bool,
        priority: str,
        reason: str,
        trigger: float | None,
        quote_price: float | None,
        safe: bool,
        stale: bool,
        source: str,
        route_available: bool = True,
        pool_exists: bool = True,
        unsellable: bool = False,
        price_impact_pct: float = 0.0,
        log_codes: tuple[str, ...] = (),
    ) -> Decision:
        return Decision(
            position_id=pos.position_id,
            should_exit=should,
            priority=priority,
            reason=reason,
            stop_trigger_price=trigger,
            quote_price=quote_price,
            safe=safe,
            stale=stale,
            state=pos.stop_state,
            source=source,
            route_available=route_available,
            pool_exists=pool_exists,
            unsellable=unsellable,
            price_impact_pct=price_impact_pct,
            log_codes=log_codes,
        )

    def register_trigger_event(self, position_id: str, event_key: str, now: float | None = None) -> bool:
        moment = self._now(now)
        if self.store.insert_event_key(event_key, position_id, iso(moment)):
            return True
        pos = self.get(position_id)
        pos.duplicate_events += 1
        self.save(pos, moment)
        self.log_event(position_id, "DUPLICATE_STOP_EVENT", {"event_key": event_key}, moment)
        return False

    def mark_submitted(self, position_id: str, blockhash: str, now: float | None = None) -> StopPosition:
        pos = self.get(position_id)
        moment = self._now(now)
        if pos.stop_state not in (StopState.STOP_TRIGGERED, StopState.STOP_PENDING):
            raise StopProtectionError(f"cannot submit stop from {pos.stop_state}")
        if pos.pending_sell_tokens > 0:
            raise StopProtectionError("sell already in flight")
        if pos.remaining_tokens <= 0:
            raise StopProtectionError("no balance")
        sell = pos.remaining_tokens
        if sell > pos.remaining_tokens:
            sell = pos.remaining_tokens
        pos.pending_sell_tokens = qprice(sell)
        pos.stop_state = StopState.STOP_SUBMITTED
        pos.submitted_blockhash = blockhash
        pos.stop_submitted_at = iso(moment)
        pos.attempt_count += 1
        self.save(pos, moment)
        self.store.add_attempt(
            position_id,
            pos.attempt_count,
            blockhash,
            pos.pending_sell_tokens,
            "submitted",
            iso(moment),
        )
        return pos

    def mark_submit_failed(self, position_id: str, error: str, now: float | None = None) -> StopPosition:
        """Failed transaction: stay open in STOP_PENDING. Do not mark closed."""
        pos = self.get(position_id)
        moment = self._now(now)
        if pos.stop_state != StopState.STOP_SUBMITTED:
            raise StopProtectionError(f"cannot fail submit from {pos.stop_state}")
        attempt_no = pos.attempt_count
        pos.pending_sell_tokens = 0.0
        pos.stop_state = StopState.STOP_PENDING
        pos.last_error = error
        pos.closed_at = None
        self.save(pos, moment)
        self.store.set_attempt_status(position_id, attempt_no, "failed")
        self.log_event(position_id, "STOP_SUBMIT_FAILED", {"error": error, "attempt": attempt_no}, moment)
        return pos

    def mark_unexecutable(self, position_id: str, code: str, now: float | None = None) -> StopPosition:
        pos = self.get(position_id)
        moment = self._now(now)
        if pos.stop_state == StopState.CLOSED:
            raise StopProtectionError("already closed")
        if pos.stop_state == StopState.STOP_SUBMITTED:
            self.store.set_attempt_status(position_id, pos.attempt_count, "failed")
        pos.pending_sell_tokens = 0.0
        pos.stop_state = StopState.EXIT_FAILED_NO_ROUTE
        pos.last_error = code
        pos.liquidity_status = code
        pos.stop_reason = pos.stop_reason or code
        pos.closed_at = None
        self.save(pos, moment)
        self.log_event(position_id, code, {"code": code}, moment)
        return pos

    def mark_filled(
        self,
        position_id: str,
        *,
        quote: float,
        actual_fill: float,
        relaxed_min_out: bool,
        now: float | None = None,
    ) -> FillReceipt:
        pos = self.get(position_id)
        moment = self._now(now)
        if pos.stop_state != StopState.STOP_SUBMITTED:
            raise StopProtectionError(f"cannot fill from {pos.stop_state}")
        tokens = pos.pending_sell_tokens
        if tokens <= 0 or tokens > pos.remaining_tokens + 1e-9:
            raise StopProtectionError("refusing to sell more tokens than balance")
        tokens = min(tokens, pos.remaining_tokens)
        slippage = 0.0 if quote == 0 else (quote - actual_fill) / quote
        loss_pct = (pos.entry_fill_price - actual_fill) / pos.entry_fill_price * 100.0
        loss_abs = max(0.0, (pos.entry_fill_price - actual_fill) * tokens)
        pos.remaining_tokens = qprice(pos.remaining_tokens - tokens)
        if pos.remaining_tokens <= 1e-9:
            pos.remaining_tokens = 0.0
        pos.pending_sell_tokens = 0.0
        pos.exit_quote_price = qprice(quote)
        pos.exit_actual_fill_price = qprice(actual_fill)
        pos.stop_fill_price = qprice(actual_fill)
        pos.slippage = slippage
        pos.realized_loss = qprice(loss_abs)
        pos.realized_loss_pct = loss_pct
        pos.last_mark_price = qprice(actual_fill)
        pos.last_mark_at = iso(moment)
        attempt_no = pos.attempt_count
        blockhash = pos.submitted_blockhash or ""
        if pos.remaining_tokens == 0:
            pos.stop_state = StopState.CLOSED
            pos.closed_at = iso(moment)
        self.save(pos, moment)
        self.store.set_attempt_status(position_id, attempt_no, "filled")
        receipt = FillReceipt(
            position_id=position_id,
            tokens_sold=qprice(tokens),
            stop_trigger_price=pos.stop_trigger_price,
            quote_price=qprice(quote),
            actual_fill_price=qprice(actual_fill),
            slippage=slippage,
            realized_loss=qprice(loss_abs),
            realized_loss_pct=loss_pct,
            blockhash=blockhash,
            relaxed_min_out=relaxed_min_out,
            trading_mode="paper",
        )
        self.log_event(
            position_id,
            "STOP_FILLED",
            {
                "stop_trigger_price": receipt.stop_trigger_price,
                "quote_price": receipt.quote_price,
                "actual_fill_price": receipt.actual_fill_price,
                "slippage": receipt.slippage,
                "realized_loss": receipt.realized_loss,
                "realized_loss_pct": receipt.realized_loss_pct,
                "tokens_sold": receipt.tokens_sold,
                "relaxed_min_out": relaxed_min_out,
                "notice": STOP_IS_TRIGGER_NOT_GUARANTEE,
            },
            moment,
        )
        return receipt

    def request_strategy_exit(
        self,
        position_id: str,
        quote_price: float,
        now: float | None = None,
    ) -> Decision:
        """Normal strategy exit. Refuses to run when a stop is already due."""
        moment = self._now(now)
        view = MarketView(position_id, Quote(price=quote_price, timestamp=moment))
        decision = self.on_mark(view, moment)
        if decision.should_exit or self.get(position_id).stop_state in BLOCKING_STATES:
            raise ExitPriorityError("stop loss outranks a normal strategy exit")
        pos = self.get(position_id)
        if pos.stop_state != StopState.OPEN:
            raise ExitPriorityError("stop loss outranks a normal strategy exit")
        if not pos.stop_type or pos.current_stop_price <= 0:
            raise StopProtectionError("refusing to touch a position with no stop")
        pos.remaining_tokens = 0.0
        pos.stop_state = StopState.CLOSED
        pos.stop_reason = StopReason.NORMAL_STRATEGY_EXIT
        pos.closed_at = iso(moment)
        pos.exit_quote_price = qprice(quote_price)
        pos.exit_actual_fill_price = qprice(quote_price)
        self.save(pos, moment)
        self.log_event(position_id, StopReason.NORMAL_STRATEGY_EXIT, {"quote_price": quote_price}, moment)
        return decision

    def reconcile_unknown_submit(self, position_id: str, now: float | None = None) -> StopPosition:
        """A submit with no receipt is not a close. Return the position to STOP_PENDING."""
        pos = self.get(position_id)
        moment = self._now(now)
        if pos.stop_state != StopState.STOP_SUBMITTED:
            return pos
        pos.stop_state = StopState.STOP_PENDING
        pos.pending_sell_tokens = 0.0
        pos.last_error = "reconciled_no_receipt"
        pos.closed_at = None
        self.save(pos, moment)
        self.log_event(position_id, "RECONCILED_NO_RECEIPT", {"attempt": pos.attempt_count}, moment)
        return pos

    def recover(self, now: float | None = None) -> list[StopPosition]:
        """Reload active stops. Does not reset prices, trails, or reasons."""
        moment = self._now(now)
        loaded: list[StopPosition] = []
        for pos in self.store.load_active():
            if pos.stop_state == StopState.STOP_SUBMITTED:
                pos = self.reconcile_unknown_submit(pos.position_id, moment)
            loaded.append(pos)
        return loaded
