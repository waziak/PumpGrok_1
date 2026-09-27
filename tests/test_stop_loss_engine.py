"""Deterministic stop-loss engine. Paper and live share this state machine."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from execution.config import ENV_ALLOWLIST, HARD_MAX_LOSS_PCT, StopConfig
from execution.engine import (
    EntryDeferred,
    ExitPriorityError,
    StopEngine,
    StopProtectionError,
    TradeRejected,
)
from execution.live import LiveSender
from execution.models import (
    STOP_IS_TRIGGER_NOT_GUARANTEE,
    Clock,
    MarketView,
    OpenRequest,
    Quote,
    StopPolicy,
    StopReason,
    StopState,
    StopType,
)
from execution.paper import PaperBroker
from execution.risk import ExecutionCycle, build_executor
from execution.status import render_status
from execution.store import StopStore
from execution.monitor import StopMonitor

ROOT = Path(__file__).resolve().parents[1]
_DEFAULT = object()


def make_engine(path: Path, **overrides) -> StopEngine:
    config = StopConfig.from_env({})
    if overrides:
        config = replace(config, **overrides)
    return StopEngine(StopStore(path), config, Clock())


def req(
    pid: str = "pos-1",
    *,
    policy: object = _DEFAULT,
    fill: float = 1.0,
    signal: float | None = None,
    quote: float | None = None,
    tokens: float = 100.0,
    symbol: str = "BONK",
    liq: float | None = None,
    mint: str = "mint",
) -> OpenRequest:
    chosen: StopPolicy | None
    if policy is _DEFAULT:
        chosen = StopPolicy(StopType.FIXED_PERCENT_STOP, stop_loss_pct=20)
    else:
        chosen = policy  # type: ignore[assignment]
    return OpenRequest(
        position_id=pid,
        symbol=symbol,
        mint=mint,
        actual_fill_price=fill,
        size_tokens=tokens,
        policy=chosen,
        signal_price=signal,
        quote_price=quote,
        entry_liquidity=liq,
    )


def mark(engine: StopEngine, pid: str, price: float, **quote_kw):
    timestamp = quote_kw.pop("timestamp", engine.clock.now)
    source = quote_kw.pop("source", "primary")
    fallbacks = quote_kw.pop("fallbacks", ())
    scanner_up = quote_kw.pop("scanner_up", True)
    advisor = quote_kw.pop("advisor", None)
    quote = Quote(price=price, timestamp=timestamp, source=source, **quote_kw)
    view = MarketView(
        pid,
        quote,
        fallbacks=tuple(fallbacks),
        scanner_up=scanner_up,
        advisor=advisor,
    )
    return engine.on_mark(view, timestamp)


def test_reject_trade_without_stop(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        with pytest.raises(TradeRejected) as missing:
            engine.open_position(req(policy=None))
        assert missing.value.reason == "stop_required"
        with pytest.raises(TradeRejected):
            engine.open_position(req(policy=StopPolicy(stop_type="")))
        with pytest.raises(TradeRejected):
            engine.open_position(
                req(policy=StopPolicy(StopType.FIXED_PERCENT_STOP, stop_loss_pct=None))
            )
        with pytest.raises(TradeRejected):
            engine.open_position(
                req(policy=StopPolicy(StopType.STRUCTURE_STOP, structure_price=None))
            )
        assert engine.store.count() == 0
    finally:
        engine.close()


def test_fixed_20_percent_stop_uses_actual_fill_not_signal(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req(signal=1.5, quote=1.2, fill=1.0))
        assert pos.entry_fill_price == pytest.approx(1.0)
        assert pos.signal_price == pytest.approx(1.5)
        assert pos.initial_stop_price == pytest.approx(0.80)
        assert pos.current_stop_price == pytest.approx(0.80)
        assert pos.hard_stop_price == pytest.approx(0.75)
        assert pos.initial_stop_price != pytest.approx(1.5 * 0.8)
        assert pos.initial_stop_price != pytest.approx(1.2 * 0.8)
        held = mark(engine, pos.position_id, pos.current_stop_price + 0.01)
        assert held.should_exit is False
        assert engine.get(pos.position_id).stop_state == StopState.OPEN
        fired = mark(engine, pos.position_id, pos.current_stop_price)
        assert fired.should_exit is True
        assert fired.reason == StopReason.FIXED_PERCENT_STOP
        assert fired.priority == "STOP_LOSS"
        assert fired.stop_trigger_price == pytest.approx(0.80)
        assert engine.get(pos.position_id).stop_state == StopState.STOP_TRIGGERED
    finally:
        engine.close()


def test_full_exit_on_fixed_stop_breach(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req(tokens=100))
        decision = mark(engine, pos.position_id, pos.current_stop_price)
        broker = PaperBroker(engine)
        assert broker.execute(decision) == "filled"
        closed = engine.get(pos.position_id)
        assert closed.stop_state == StopState.CLOSED
        assert closed.remaining_tokens == 0
        assert broker.sold_tokens == [pytest.approx(100)]
        assert broker.last_receipt is not None
        assert broker.last_receipt.tokens_sold == pytest.approx(100)
    finally:
        engine.close()


def test_strategy_stop_clamped_never_wider_than_max(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        wide = engine.open_position(
            req("wide", policy=StopPolicy(StopType.FIXED_PERCENT_STOP, stop_loss_pct=50))
        )
        tight = engine.open_position(
            req("tight", policy=StopPolicy(StopType.FIXED_PERCENT_STOP, stop_loss_pct=2))
        )
        assert wide.stop_loss_pct == pytest.approx(30)
        assert wide.current_stop_price == pytest.approx(0.70)
        assert wide.current_stop_price > 0.50
        assert wide.hard_stop_price == pytest.approx(0.75)
        assert tight.stop_loss_pct == pytest.approx(5)
        assert tight.current_stop_price == pytest.approx(0.95)
        config = StopConfig.from_env({
            "MAX_STOP_LOSS_PCT": "80",
            "MIN_STOP_LOSS_PCT": "1",
            "DEFAULT_STOP_LOSS_PCT": "50",
            "HARD_MAX_LOSS_PCT": "90",
            "LIQUIDITY_DROP_EXIT_PCT": "90",
            "PRICE_STALE_SECONDS": "99999",
        })
        assert config.max_stop_loss_pct == pytest.approx(30)
        assert config.min_stop_loss_pct == pytest.approx(5)
        assert config.default_stop_loss_pct == pytest.approx(30)
        assert config.hard_max_loss_pct == pytest.approx(HARD_MAX_LOSS_PCT)
        assert config.liquidity_drop_exit_pct == pytest.approx(50)
        assert config.price_stale_seconds == pytest.approx(120)
    finally:
        engine.close()


def test_llm_cannot_disable_widen_or_remove_stop(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req())
        before = (
            pos.current_stop_price,
            pos.initial_stop_price,
            pos.hard_stop_price,
            pos.stop_type,
            pos.stop_state,
        )
        for actor in ("grok", "Grok", "CHIEF", "llm", "prompt"):
            for action in ("disable", "widen", "remove"):
                with pytest.raises(StopProtectionError):
                    engine.mutate(
                        pos.position_id,
                        actor=actor,
                        action=action,
                        new_stop_price=0.10,
                        new_stop_pct=90,
                    )
        with pytest.raises(StopProtectionError):
            engine.mutate(
                pos.position_id,
                actor="strategy",
                action="widen",
                new_stop_price=0.50,
                new_stop_pct=40,
            )
        after = engine.get(pos.position_id)
        assert (
            after.current_stop_price,
            after.initial_stop_price,
            after.hard_stop_price,
            after.stop_type,
            after.stop_state,
        ) == before
        assert after.stop_state == StopState.OPEN
    finally:
        engine.close()


def test_structure_stop(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(
            req(policy=StopPolicy(StopType.STRUCTURE_STOP, structure_price=0.90))
        )
        assert pos.stop_type == StopType.STRUCTURE_STOP
        assert pos.current_stop_price == pytest.approx(0.90)
        assert pos.hard_stop_price == pytest.approx(0.75)
        held = mark(engine, pos.position_id, 0.91)
        assert held.should_exit is False
        fired = mark(engine, pos.position_id, pos.current_stop_price)
        assert fired.should_exit is True
        assert fired.reason == StopReason.STRUCTURE_STOP
        widened = engine.open_position(
            req("tight-structure", policy=StopPolicy(StopType.STRUCTURE_STOP, structure_price=0.96))
        )
        assert widened.stop_loss_pct == pytest.approx(5)
        assert widened.current_stop_price == pytest.approx(0.95)
        clamped = engine.open_position(
            req("wide-structure", policy=StopPolicy(StopType.STRUCTURE_STOP, structure_price=0.40))
        )
        assert clamped.stop_loss_pct == pytest.approx(30)
        assert clamped.current_stop_price == pytest.approx(0.70)
        assert clamped.hard_stop_price == pytest.approx(0.75)
    finally:
        engine.close()


def test_partial_profit_retains_stop_and_moves_to_breakeven(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req(tokens=100))
        hard = pos.hard_stop_price
        updated = engine.apply_partial_take_profit(
            pos.position_id, tokens=40, fill_price=1.4, to_breakeven=True
        )
        assert updated.remaining_tokens == pytest.approx(60)
        assert updated.stop_state == StopState.OPEN
        assert updated.stop_type == StopType.FIXED_PERCENT_STOP
        assert updated.current_stop_price == pytest.approx(1.0)
        assert updated.break_even_active is True
        assert updated.hard_stop_price == pytest.approx(hard)
        assert updated.initial_stop_price == pytest.approx(0.80)
        held = mark(engine, pos.position_id, 1.01)
        assert held.should_exit is False
        fired = mark(engine, pos.position_id, 1.0)
        assert fired.should_exit is True
        broker = PaperBroker(engine)
        assert broker.execute(fired) == "filled"
        closed = engine.get(pos.position_id)
        assert closed.remaining_tokens == 0
        assert closed.stop_state == StopState.CLOSED
        assert broker.sold_tokens == [pytest.approx(60)]
    finally:
        engine.close()


def test_exit_priority_over_new_entry(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        first = engine.open_position(req("first"))
        trace: list[str] = []
        broker = PaperBroker(engine, trace=trace)
        cycle = ExecutionCycle(engine, StopMonitor(engine), broker)
        breach = MarketView(
            first.position_id,
            Quote(price=first.current_stop_price, timestamp=engine.clock.now),
        )
        _acted, opened = cycle.run([breach], [req("second")], now=engine.clock.now)
        assert trace == ["exit", "entry"]
        assert engine.get("first").stop_state == StopState.CLOSED
        assert [pos.position_id for pos in opened] == ["second"]

        trace.clear()
        broker.fail_remaining = 1
        second = engine.get("second")
        breach_second = MarketView(
            second.position_id,
            Quote(price=second.current_stop_price, timestamp=engine.clock.now),
        )
        _acted, blocked = cycle.run([breach_second], [req("third")], now=engine.clock.now)
        assert trace == ["exit"]
        assert blocked == []
        assert engine.get("second").stop_state == StopState.STOP_PENDING
        with pytest.raises(EntryDeferred):
            engine.request_entry(req("third"))
        with pytest.raises(ExitPriorityError):
            engine.request_strategy_exit("second", quote_price=0.5)
    finally:
        engine.close()


def test_stop_during_grok_and_scanner_outage(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req())
        calls = {"n": 0}

        def advisor():
            calls["n"] += 1
            raise RuntimeError("grok down")

        view = MarketView(
            pos.position_id,
            Quote(price=pos.current_stop_price, timestamp=engine.clock.now),
            scanner_up=False,
            advisor=advisor,
        )
        broker = PaperBroker(engine)
        cycle = ExecutionCycle(engine, StopMonitor(engine), broker)
        cycle.run([view], now=engine.clock.now)
        assert calls["n"] == 0
        assert engine.get(pos.position_id).stop_state == StopState.CLOSED
        assert broker.sold_tokens == [pytest.approx(100)]
    finally:
        engine.close()


def test_hard_25_percent_emergency_stop(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(
            req(policy=StopPolicy(StopType.FIXED_PERCENT_STOP, stop_loss_pct=30))
        )
        assert pos.stop_loss_pct == pytest.approx(30)
        assert pos.current_stop_price == pytest.approx(0.70)
        assert pos.hard_stop_price == pytest.approx(0.75)
        held = mark(engine, pos.position_id, pos.hard_stop_price + 0.01)
        assert held.should_exit is False
        fired = mark(engine, pos.position_id, pos.hard_stop_price)
        assert fired.should_exit is True
        assert fired.reason == StopReason.HARD_EMERGENCY_STOP
        assert fired.priority == "EMERGENCY_EXIT"
        assert fired.stop_trigger_price == pytest.approx(0.75)
        broker = PaperBroker(engine)
        assert broker.execute(fired) == "filled"
        receipt = broker.last_receipt
        assert receipt is not None and receipt.relaxed_min_out is True
        assert engine.get(pos.position_id).stop_state == StopState.CLOSED
        assert engine.get(pos.position_id).remaining_tokens == 0
    finally:
        engine.close()


def test_hard_max_loss_ignores_override_attempts(tmp_path):
    config = replace(StopConfig.from_env({"HARD_MAX_LOSS_PCT": "90"}), hard_max_loss_pct=90)
    engine = StopEngine(StopStore(tmp_path / "stops.sqlite"), config, Clock())
    try:
        assert engine.config.hard_max_loss_pct == pytest.approx(90)
        pos = engine.open_position(req(policy=StopPolicy(StopType.FIXED_PERCENT_STOP, 30)))
        assert pos.hard_stop_price == pytest.approx(1.0 * (1.0 - HARD_MAX_LOSS_PCT / 100.0))
        assert pos.hard_stop_price == pytest.approx(0.75)
        with pytest.raises(StopProtectionError):
            engine.mutate(
                pos.position_id,
                actor="grok",
                action="widen",
                new_stop_price=0.10,
            )
        assert engine.get(pos.position_id).hard_stop_price == pytest.approx(0.75)
    finally:
        engine.close()


def test_trailing_activates_moves_up_and_never_down(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(
            req(policy=StopPolicy(StopType.TRAILING_STOP, stop_loss_pct=20))
        )
        assert pos.trailing_active is False
        assert pos.current_stop_price == pytest.approx(0.80)
        early = mark(engine, pos.position_id, 1.10)
        assert early.should_exit is False
        armed = engine.get(pos.position_id)
        assert armed.trailing_active is False
        assert armed.current_stop_price == pytest.approx(0.80)
        assert armed.high_water_price == pytest.approx(1.10)

        activated = mark(engine, pos.position_id, 1.20)
        assert activated.should_exit is False
        live = engine.get(pos.position_id)
        assert live.trailing_active is True
        assert live.current_stop_price == pytest.approx(1.20 * 0.88)
        assert live.high_water_price == pytest.approx(1.20)

        higher = mark(engine, pos.position_id, 1.50)
        assert higher.should_exit is False
        peaked = engine.get(pos.position_id)
        assert peaked.high_water_price == pytest.approx(1.50)
        assert peaked.current_stop_price == pytest.approx(1.50 * 0.88)

        pulled = mark(engine, pos.position_id, 1.40)
        assert pulled.should_exit is False
        held = engine.get(pos.position_id)
        assert held.high_water_price == pytest.approx(1.50)
        assert held.current_stop_price == pytest.approx(1.32)

        fired = mark(engine, pos.position_id, held.current_stop_price)
        assert fired.should_exit is True
        assert fired.reason == StopReason.TRAILING_STOP
        broker = PaperBroker(engine)
        assert broker.execute(fired) == "filled"
        assert engine.get(pos.position_id).stop_state == StopState.CLOSED
    finally:
        engine.close()


def test_crash_after_trigger_restart_executes_below_stop(tmp_path):
    path = tmp_path / "stops.sqlite"
    engine = make_engine(path)
    try:
        pos = engine.open_position(req())
        now = engine.clock.now
        decision = engine.on_mark(
            MarketView(pos.position_id, Quote(price=0.79, timestamp=now)),
            now,
        )
        assert decision.should_exit is True
        saved = engine.get(pos.position_id)
        assert saved.stop_state == StopState.STOP_TRIGGERED
        snapshot = (
            saved.entry_fill_price,
            saved.initial_stop_price,
            saved.current_stop_price,
            saved.hard_stop_price,
            saved.high_water_price,
            saved.stop_type,
            saved.stop_triggered_at,
        )
    finally:
        engine.close()

    restarted = make_engine(path)
    try:
        loaded = restarted.recover(now)
        assert len(loaded) == 1
        assert loaded[0].stop_state == StopState.STOP_TRIGGERED
        assert (
            loaded[0].entry_fill_price,
            loaded[0].initial_stop_price,
            loaded[0].current_stop_price,
            loaded[0].hard_stop_price,
            loaded[0].high_water_price,
            loaded[0].stop_type,
            loaded[0].stop_triggered_at,
        ) == snapshot
        broker = PaperBroker(restarted)
        again = restarted.on_mark(
            MarketView(pos.position_id, Quote(price=0.70, timestamp=now)),
            now,
        )
        assert again.should_exit is True
        still = restarted.get(pos.position_id)
        assert still.current_stop_price == pytest.approx(snapshot[2])
        assert still.hard_stop_price == pytest.approx(snapshot[3])
        assert still.high_water_price == pytest.approx(snapshot[4])
        assert broker.handle(again) == "filled"
        assert restarted.get(pos.position_id).stop_state == StopState.CLOSED
        assert restarted.get(pos.position_id).remaining_tokens == 0
    finally:
        restarted.close()


def test_restart_does_not_reset_stops(tmp_path):
    path = tmp_path / "stops.sqlite"
    engine = make_engine(path)
    try:
        pos = engine.open_position(req(policy=StopPolicy(StopType.TRAILING_STOP, 20)))
        now = engine.clock.now
        engine.on_mark(MarketView(pos.position_id, Quote(price=1.50, timestamp=now)), now)
        saved = engine.get(pos.position_id)
        assert saved.trailing_active is True
        assert saved.current_stop_price == pytest.approx(1.32)
        assert saved.high_water_price == pytest.approx(1.50)
        snapshot = (
            saved.entry_fill_price,
            saved.initial_stop_price,
            saved.current_stop_price,
            saved.hard_stop_price,
            saved.high_water_price,
            saved.trailing_active,
            saved.stop_state,
        )
    finally:
        engine.close()

    restarted = make_engine(path)
    try:
        loaded = restarted.recover(now)
        assert len(loaded) == 1
        got = loaded[0]
        assert (
            got.entry_fill_price,
            got.initial_stop_price,
            got.current_stop_price,
            got.hard_stop_price,
            got.high_water_price,
            got.trailing_active,
            got.stop_state,
        ) == snapshot
        assert got.current_stop_price > got.initial_stop_price
        assert not hasattr(restarted, "reset_stops")
    finally:
        restarted.close()


def test_failed_stop_tx_safe_retry_and_duplicate_event(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req(tokens=80))
        decision = mark(engine, pos.position_id, pos.current_stop_price)
        broker = PaperBroker(engine, fail_remaining=1)
        assert broker.execute(decision, event_key="evt-1") == "failed"
        pending = engine.get(pos.position_id)
        assert pending.stop_state == StopState.STOP_PENDING
        assert pending.remaining_tokens == pytest.approx(80)
        assert pending.pending_sell_tokens == 0
        assert pending.closed_at is None
        assert pending.attempt_count == 1
        assert broker.execute(decision, event_key="evt-1") == "duplicate"
        duplicated = engine.get(pos.position_id)
        assert duplicated.remaining_tokens == pytest.approx(80)
        assert duplicated.attempt_count == 1
        assert duplicated.duplicate_events == 1
        assert duplicated.stop_state == StopState.STOP_PENDING
        again = mark(engine, pos.position_id, pos.current_stop_price)
        assert broker.retry(again) == "filled"
        closed = engine.get(pos.position_id)
        assert closed.stop_state == StopState.CLOSED
        assert closed.remaining_tokens == 0
        assert sum(broker.sold_tokens) == pytest.approx(80)
        assert len(broker.sold_tokens) == 1
        attempts = engine.store.attempts_for(pos.position_id)
        assert [row["status"] for row in attempts] == ["failed", "filled"]
        assert attempts[0]["blockhash"] != attempts[1]["blockhash"]
        assert attempts[0]["size_tokens"] == pytest.approx(80)
        assert attempts[1]["size_tokens"] == pytest.approx(80)
        assert broker.blockhashes.issued[0] != broker.blockhashes.issued[1]
    finally:
        engine.close()


def test_paper_stop_cycle_and_receipt(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        assert engine.config.trading_mode == "paper"
        assert isinstance(build_executor(engine), PaperBroker)
        pos = engine.open_position(req())
        broker = build_executor(engine)
        cycle = ExecutionCycle(engine, StopMonitor(engine), broker)
        view = MarketView(
            pos.position_id,
            Quote(price=pos.current_stop_price, timestamp=engine.clock.now),
        )
        cycle.run([view], now=engine.clock.now)
        closed = engine.get(pos.position_id)
        receipt = broker.last_receipt
        assert receipt is not None
        assert closed.stop_state == StopState.CLOSED
        assert closed.trading_mode == "paper"
        assert receipt.stop_trigger_price == pytest.approx(0.80)
        assert receipt.quote_price == pytest.approx(0.80)
        assert receipt.actual_fill_price == pytest.approx(0.80)
        assert receipt.slippage == pytest.approx(0.0)
        assert receipt.realized_loss_pct == pytest.approx(20.0)
        assert receipt.trading_mode == "paper"
        assert "trigger" in receipt.notice.lower()
        assert closed.exit_quote_price == pytest.approx(0.80)
        assert closed.exit_actual_fill_price == pytest.approx(0.80)
        assert broker.live_network is False
    finally:
        engine.close()


def test_liquidity_collapse_exit(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req(liq=1000))
        held = mark(engine, pos.position_id, 0.95, liquidity=600)
        assert held.should_exit is False
        assert engine.get(pos.position_id).stop_state == StopState.OPEN
        fired = mark(engine, pos.position_id, 0.95, liquidity=500)
        assert fired.should_exit is True
        assert fired.reason == StopReason.LIQUIDITY_DROP_EXIT
        assert fired.priority == "EMERGENCY_EXIT"
        broker = PaperBroker(engine)
        assert broker.execute(fired) == "filled"
        closed = engine.get(pos.position_id)
        assert closed.stop_state == StopState.CLOSED
        assert closed.remaining_tokens == 0
        events = [row["event"] for row in engine.store.events_for(pos.position_id)]
        assert StopReason.LIQUIDITY_DROP_EXIT in events
    finally:
        engine.close()


def test_no_route_pool_gone_and_unsellable(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        cases = (
            ("pool", {"pool_exists": False}, StopReason.POOL_GONE),
            ("route", {"route_available": False}, StopReason.NO_ROUTE),
            ("unsellable", {"unsellable": True}, StopReason.UNSELLABLE),
        )
        for pid, flags, code in cases:
            pos = engine.open_position(req(pid))
            decision = mark(engine, pid, pos.current_stop_price, **flags)
            broker = PaperBroker(engine)
            assert broker.execute(decision, event_key=f"{pid}-event") == code
            got = engine.get(pid)
            assert got.stop_state == StopState.EXIT_FAILED_NO_ROUTE
            assert got.remaining_tokens == pytest.approx(100)
            assert got.closed_at is None
            events = [row["event"] for row in engine.store.events_for(pid)]
            assert code in events
        codes = []
        for pid, _flags, code in cases:
            codes.append(code)
        assert codes == [StopReason.POOL_GONE, StopReason.NO_ROUTE, StopReason.UNSELLABLE]
    finally:
        engine.close()


def test_stale_price_fallback_and_not_safe(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        now = engine.clock.now
        window = engine.config.price_stale_seconds
        breached = engine.open_position(req("fallback"))
        primary = Quote(price=1.0, timestamp=now - window - 10, source="primary")
        fallback = Quote(price=breached.current_stop_price, timestamp=now, source="fallback-feed")
        decision = engine.on_mark(
            MarketView(breached.position_id, primary, fallbacks=(fallback,)),
            now,
        )
        assert decision.stale is False
        assert decision.source == "fallback-feed"
        assert decision.quote_price == pytest.approx(breached.current_stop_price)
        assert decision.should_exit is True
        assert decision.safe is False

        stale_safe_print = engine.open_position(req("stale-high"))
        stale = engine.on_mark(
            MarketView(
                stale_safe_print.position_id,
                Quote(price=5.0, timestamp=now - window - 10, source="primary"),
            ),
            now,
        )
        assert stale.should_exit is False
        assert stale.safe is False
        assert stale.stale is True
        held = engine.get("stale-high")
        assert held.stop_state == StopState.OPEN
        assert held.high_water_price == pytest.approx(1.0)
        assert held.trailing_active is False
        assert held.price_data_stale is True
        events = [row["event"] for row in engine.store.events_for("stale-high")]
        assert StopReason.PRICE_DATA_STALE in events

        stale_breach = engine.open_position(req("stale-low"))
        forced = engine.on_mark(
            MarketView(
                stale_breach.position_id,
                Quote(price=0.50, timestamp=now - window - 10, source="primary"),
            ),
            now,
        )
        assert forced.should_exit is True
        assert forced.safe is False
        assert engine.get("stale-low").stop_state == StopState.STOP_TRIGGERED

        blind = engine.open_position(req("blind"))
        missing = engine.on_mark(MarketView("blind", None), now)
        assert missing.should_exit is False
        assert missing.safe is False
        assert engine.get("blind").stop_state == StopState.OPEN
    finally:
        engine.close()


def test_severe_slippage_worse_than_configured_stop(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req(tokens=10))
        decision = mark(
            engine,
            pos.position_id,
            pos.current_stop_price,
            price_impact_pct=80,
        )
        broker = PaperBroker(engine, slippage_pct=37.5)
        assert broker.execute(decision) == "filled"
        closed = engine.get(pos.position_id)
        receipt = broker.last_receipt
        assert receipt is not None
        assert receipt.stop_trigger_price == pytest.approx(0.80)
        assert receipt.quote_price == pytest.approx(0.80)
        assert receipt.actual_fill_price == pytest.approx(0.50)
        assert receipt.slippage == pytest.approx(0.375)
        assert receipt.realized_loss_pct == pytest.approx(50.0)
        assert receipt.realized_loss_pct > pos.stop_loss_pct
        assert closed.realized_loss == pytest.approx(0.50 * 10)
        assert closed.stop_state == StopState.CLOSED
        events = [row["event"] for row in engine.store.events_for(pos.position_id)]
        assert StopReason.SEVERE_IMPACT in events
        filled = next(row for row in engine.store.events_for(pos.position_id) if row["event"] == "STOP_FILLED")
        assert "trigger threshold" in filled["detail"]
    finally:
        engine.close()


def test_status_shows_open_position(tmp_path):
    engine = make_engine(tmp_path / "stops.sqlite")
    try:
        pos = engine.open_position(req(symbol="BONK"))
        mark(engine, pos.position_id, 0.90)
        text = render_status(engine.config, engine.store.load_active())
        assert "TRADING_MODE=paper" in text
        assert "PRIVATE KEY EXPOSED=NO" in text
        assert "REAL TRADES=NO" in text
        assert "LIVE READY=FAIL" in text
        assert "BONK" in text
        assert "1.0000" in text
        assert "0.9000" in text
        assert "0.8000" in text
        assert "inactive" in text
        assert "OPEN" in text
        assert "-10.00" in text
        assert STOP_IS_TRIGGER_NOT_GUARANTEE in text
    finally:
        engine.close()


def test_live_sends_disabled(tmp_path):
    secret = "do-not-log-this-token"
    sender = LiveSender()
    with pytest.raises(Exception) as denied:
        sender.send({"side": "sell"}, key_material=secret)
    assert secret not in str(denied.value)
    assert sender.real_trades is False
    assert sender.live_network is False

    config = replace(StopConfig.from_env({}), trading_mode="live")
    engine = StopEngine(StopStore(tmp_path / "stops.sqlite"), config, Clock())
    try:
        pos = engine.open_position(req())
        decision = mark(engine, pos.position_id, pos.current_stop_price)
        assert decision.should_exit is True
        assert engine.get(pos.position_id).stop_state == StopState.STOP_TRIGGERED
        assert type(engine) is StopEngine
        with pytest.raises(Exception) as blocked:
            build_executor(engine)
        assert secret not in str(blocked.value)
        with pytest.raises(Exception):
            PaperBroker(engine).execute(decision)
        assert engine.get(pos.position_id).stop_state == StopState.STOP_TRIGGERED
        assert engine.get(pos.position_id).remaining_tokens == pytest.approx(100)
        assert engine.real_trades is False
        assert engine.config.real_trades is False
        assert engine.config.live_network_sends is False
    finally:
        engine.close()


def test_private_key_not_exposed(tmp_path):
    secret = "not-a-real-key-material-xyz"
    config = StopConfig.from_env({
        "TRADING_MODE": "paper",
        "WALLET_PRIVATE_KEY": secret,
        "GROKBOT_WALLET_PRIVATE_KEY": secret,
    })
    path = tmp_path / "stops.sqlite"
    engine = StopEngine(StopStore(path), config, Clock())
    try:
        engine.open_position(req())
        text = render_status(config, engine.store.load_active())
        assert secret not in text
        assert secret not in str(config)
        assert config.private_key_exposed is False
        assert engine.private_key_exposed is False
        assert "WALLET_PRIVATE_KEY" not in ENV_ALLOWLIST
    finally:
        engine.close()
    blob = path.read_bytes()
    assert secret.encode() not in blob
    source = "\n".join(item.read_text(encoding="utf-8") for item in (ROOT / "execution").rglob("*.py"))
    for banned in (
        "import socket",
        "import http",
        "urllib",
        "import requests",
        "from requests",
        "base58",
        "solders",
        "wallet_private",
    ):
        assert banned not in source


def test_paper_sim_rejects_buy_without_stop(tmp_path):
    script = ROOT / "tools" / "paper_sim.py"
    desk = tmp_path / "desk"
    db = tmp_path / "stops.sqlite"
    base = [
        sys.executable,
        str(script),
        "--action",
        "buy",
        "--ticket",
        "SOL-20260927-001",
        "--mint",
        "mint",
        "--size-usd",
        "25",
        "--price",
        "1",
        "--desk",
        str(desk),
        "--db",
        str(db),
    ]
    rejected = subprocess.run(base, check=False, capture_output=True, text=True)
    payload = json.loads(rejected.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "stop_required"
    assert payload["realTrades"] == "NO"
    assert payload["privateKeyExposed"] == "NO"
    assert not db.exists()

    accepted = subprocess.run(
        base + ["--stop-type", "FIXED_PERCENT_STOP", "--stop-pct", "20", "--symbol", "BONK"],
        check=False,
        capture_output=True,
        text=True,
    )
    opened = json.loads(accepted.stdout)
    assert opened["ok"] is True
    assert opened["mode"] == "paper"
    assert opened["stopState"] == "OPEN"
    assert opened["initialStopPrice"] == pytest.approx(0.8)
    assert opened["hardStopPrice"] == pytest.approx(0.75)
    assert opened["realTrades"] == "NO"
    assert db.exists()
