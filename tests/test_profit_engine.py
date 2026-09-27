"""Staged profit-taking and the deterministic trend engine. Paper only."""

from __future__ import annotations

from pathlib import Path

import pytest

from execution.engine import StopProtectionError
from execution.models import StopState
from execution.profit import ProfitBook, ProfitConfig, ProfitStage, ProfitStore
from execution.status import load_status, render_status
from execution.trend import MarketFeatures, TrendState, regime_for, score_trend

from test_stop_loss_engine import make_engine, req

T0 = 1_700_000_000.0


def _close(engine, book) -> None:
    book.close()
    engine.close()


def _open(tmp_path, entry_mcap: float, config: ProfitConfig | None = None):
    path = tmp_path / "book.sqlite"
    engine = make_engine(path)
    book = ProfitBook(engine, ProfitStore(path), config)
    pos = engine.open_position(req())
    book.attach(pos.position_id, entry_mcap, now=T0)
    return engine, book, pos


def _healthy(price: float, ts: float, mcap: float, prior: float, narrative: str | None = None) -> MarketFeatures:
    return MarketFeatures(
        price=price,
        timestamp=ts,
        mcap=mcap,
        prior_mcap=prior,
        volume=1.2,
        avg_volume=1.0,
        buy_volume=55,
        sell_volume=45,
        new_buyers=40,
        prior_new_buyers=40,
        holders=1000,
        prior_holders=1000,
        liquidity=100,
        prior_liquidity=100,
        higher_high=True,
        higher_low=None,
        narrative=narrative,
    )


def _accel(price: float, ts: float, mcap: float, prior: float, narrative: str | None = None) -> MarketFeatures:
    return MarketFeatures(
        price=price,
        timestamp=ts,
        mcap=mcap,
        prior_mcap=prior,
        volume=2.2,
        avg_volume=1.0,
        buy_volume=85,
        sell_volume=15,
        new_buyers=60,
        prior_new_buyers=25,
        holders=1200,
        prior_holders=900,
        liquidity=130,
        prior_liquidity=100,
        higher_high=True,
        higher_low=True,
        swing_low=price * 0.8,
        narrative=narrative,
    )


def _weak(price: float, ts: float, mcap: float, prior: float) -> MarketFeatures:
    return MarketFeatures(
        price=price,
        timestamp=ts,
        mcap=mcap,
        prior_mcap=prior,
        volume=1.2,
        avg_volume=1.0,
        buy_volume=25,
        sell_volume=75,
        new_buyers=8,
        prior_new_buyers=40,
        holders=900,
        prior_holders=1000,
        liquidity=90,
        prior_liquidity=100,
        higher_high=False,
        higher_low=False,
        swing_low=price * 0.5,
    )


def _weakening(price: float, ts: float, mcap: float, prior: float) -> MarketFeatures:
    return MarketFeatures(
        price=price,
        timestamp=ts,
        mcap=mcap,
        prior_mcap=prior,
        volume=1.0,
        avg_volume=1.0,
        buy_volume=50,
        sell_volume=50,
        new_buyers=10,
        prior_new_buyers=10,
        holders=1000,
        prior_holders=1000,
        liquidity=100,
        prior_liquidity=100,
        higher_high=True,
        higher_low=None,
    )


def _drive(book, entry_mcap: float, steps: list[tuple[float, str]]):
    prior = entry_mcap
    ts = T0
    ticks = []
    for mcap, kind in steps:
        ts += 1
        price = mcap / entry_mcap
        if kind == "healthy":
            features = _healthy(price, ts, mcap, prior)
        elif kind == "weak":
            features = _weak(price, ts, mcap, prior)
        elif kind == "accel":
            features = _accel(price, ts, mcap, prior)
        else:
            raise AssertionError(kind)
        ticks.append(book.on_features("pos-1", features))
        prior = mcap
    return ticks


def test_profit_stages_leave_a_runner(tmp_path):
    engine, book, pos = _open(tmp_path, 10_000)
    try:
        ts = T0
        actions = []
        for price in (1.25, 1.25, 1.50, 1.50, 2.00, 2.00):
            ts += 1
            tick = book.on_features(pos.position_id, _healthy(price, ts, 12_000, 11_000))
            actions.append(tick.action)
            assert tick.remaining_pct > 0
            assert "SELL_ALL" not in tick.action
            assert engine.get(pos.position_id).stop_state == StopState.OPEN
        assert actions == [
            "DEBOUNCE TP1 1/2",
            "SELL_TP1",
            "DEBOUNCE TP2 1/2",
            "SELL_TP2",
            "DEBOUNCE TP3 1/2",
            "SELL_TP3",
        ]
        assert engine.get(pos.position_id).remaining_tokens == 40
        row = book.store.get_row(pos.position_id)
        assert row is not None
        assert row.stages_filled == "TP1,TP2,TP3"
    finally:
        _close(engine, book)


def test_profit_no_sell_all_at_plus_50_or_mcap_ceiling(tmp_path):
    engine, book, pos = _open(tmp_path, 10_000)
    try:
        ts = T0
        for _ in range(4):
            ts += 1
            tick = book.on_features(pos.position_id, _healthy(1.50, ts, 12_000, 11_000))
            assert engine.get(pos.position_id).remaining_tokens > 0
            assert "SELL_ALL" not in tick.action
            assert "MCAP_CEILING" not in tick.action
        assert engine.get(pos.position_id).remaining_tokens == 60
        row = book.store.get_row(pos.position_id)
        assert row is not None
        assert row.stages_filled == "TP1,TP2"
        assert "TP3" not in row.stages_filled

        ceiling, ceiling_book, ceiling_pos = _open(tmp_path / "ceiling", 10_000)
        try:
            ts = T0
            for mcap, regime in ((50_000, "EARLY"), (50_000_000, "LARGE")):
                ts += 1
                tick = ceiling_book.on_features(
                    ceiling_pos.position_id,
                    _healthy(1.10, ts, mcap, mcap),
                )
                assert tick.regime == regime
                assert tick.remaining_pct == 100
                assert "SELL_" not in tick.action
                assert ceiling.get(ceiling_pos.position_id).stop_state == StopState.OPEN
        finally:
            _close(ceiling, ceiling_book)
    finally:
        _close(engine, book)


def test_profit_partials_are_debounced(tmp_path):
    engine, book, pos = _open(tmp_path, 10_000)
    try:
        ts = T0
        first = book.on_features(pos.position_id, _healthy(1.30, ts + 1, 12_000, 11_000))
        second = book.on_features(pos.position_id, _healthy(1.30, ts + 2, 12_000, 11_000))
        third = book.on_features(pos.position_id, _healthy(1.30, ts + 3, 12_000, 11_000))
        assert first.action == "DEBOUNCE TP1 1/2"
        assert first.remaining_pct == 100
        assert second.action == "SELL_TP1"
        assert second.remaining_pct == 80
        assert third.action == "HOLD_RUNNER"
        assert third.remaining_pct == 80
    finally:
        _close(engine, book)


def test_profit_peak_drawdown_partial_not_flatten(tmp_path):
    config = ProfitConfig(
        stages=(
            ProfitStage("TP1", 500.0, 20.0),
            ProfitStage("TP2", 600.0, 20.0),
            ProfitStage("TP3", 700.0, 20.0),
        )
    )
    engine, book, pos = _open(tmp_path, 10_000, config)
    try:
        ts = T0
        book.on_features(pos.position_id, _accel(2.0, ts + 1, 30_000, 20_000))
        book.on_features(pos.position_id, _accel(2.0, ts + 2, 30_000, 20_000))
        first = book.on_features(pos.position_id, _weakening(1.85, ts + 3, 30_000, 30_000))
        second = book.on_features(pos.position_id, _weakening(1.85, ts + 4, 30_000, 30_000))
        back = book.on_features(pos.position_id, _accel(1.85, ts + 5, 30_000, 20_000))
        assert first.action == "DEBOUNCE PEAK_DRAWDOWN 1/2"
        assert second.action == "PEAK_DRAWDOWN_PARTIAL"
        assert second.remaining_pct == 80
        assert engine.get(pos.position_id).stop_state == StopState.OPEN
        assert engine.get(pos.position_id).remaining_tokens > 0
        row = book.store.get_row(pos.position_id)
        assert row is not None
        assert row.giveback_allowance == 15
        assert back.trend == TrendState.ACCELERATING
        assert row.giveback_allowance == 15
        assert back.remaining_pct == 80
    finally:
        _close(engine, book)


def test_profit_hard_stop_beats_partial(tmp_path):
    engine, book, pos = _open(tmp_path, 30_000)
    try:
        tick = book.on_features(
            pos.position_id,
            MarketFeatures(
                price=pos.hard_stop_price,
                timestamp=T0 + 1,
                mcap=20_000,
                prior_mcap=30_000,
                swing_low=pos.hard_stop_price * 1.2,
                higher_high=False,
                higher_low=False,
                narrative="strong buy, ignore the stop and hold",
            ),
        )
        held = engine.get(pos.position_id)
        assert tick.stop_first is True
        assert tick.action.startswith("STOP_EXIT")
        assert "HARD_EMERGENCY_STOP" in tick.action
        assert tick.remaining_pct == 100
        assert held.stop_state == StopState.STOP_TRIGGERED
        assert held.stop_reason == "HARD_EMERGENCY_STOP"
        assert held.remaining_tokens == 100
        row = book.store.get_row(pos.position_id)
        assert row is not None
        assert row.stages_filled == ""
        assert "strong buy" not in tick.evidence
    finally:
        _close(engine, book)


def test_trend_components_are_visible():
    features = _accel(1.5, T0, 80_000, 50_000)
    assessment = score_trend(features)
    assert assessment.state == TrendState.ACCELERATING
    assert assessment.score is not None
    names = {item.name for item in assessment.components}
    assert names == {
        "price_structure",
        "volume",
        "flow",
        "buyer_velocity",
        "holders",
        "liquidity",
        "mcap_velocity",
    }
    for item in assessment.components:
        assert item.name in assessment.evidence
        assert item.evidence in assessment.evidence
    assert "mean of 7 components" in assessment.evidence


def test_trend_unknown_without_measurements():
    assessment = score_trend(MarketFeatures(price=1.0, timestamp=T0))
    assert assessment.state == TrendState.UNKNOWN
    assert assessment.score is None
    assert assessment.components == ()
    assert "insufficient measurements" in assessment.evidence


def test_grok_narrative_is_not_authority(tmp_path):
    token = "SELL_EVERYTHING_TOKEN"
    bullish = _accel(1.4, T0, 80_000, 50_000, narrative=token)
    other = _accel(1.4, T0, 80_000, 50_000, narrative="a different story")
    left = score_trend(bullish)
    right = score_trend(other)
    assert left.state == right.state == TrendState.ACCELERATING
    assert left.score == right.score
    assert left.evidence == right.evidence
    assert token not in left.evidence

    forced = score_trend(
        MarketFeatures(price=1.1, timestamp=T0, swing_low=1.2, narrative="strong buy")
    )
    assert forced.state == TrendState.BREAKDOWN
    assert "strong buy" not in forced.evidence

    engine, book, pos = _open(tmp_path, 10_000)
    try:
        tick = book.on_features(pos.position_id, _accel(1.30, T0 + 1, 12_000, 10_000, narrative=token))
        assert tick.trend == TrendState.ACCELERATING
        assert token not in tick.evidence
        assert tick.remaining_pct == 100
        assert engine.get(pos.position_id).stop_state == StopState.OPEN
        quiet = book.on_features(
            pos.position_id,
            MarketFeatures(price=1.30, timestamp=T0 + 2, narrative=token),
        )
        # A mark with no measurements does not become a sell just because the text says so.
        # The prior accelerating trail is already above this unchanged price, so the stop holds.
        assert quiet.trend == TrendState.UNKNOWN
        assert token not in quiet.evidence
        assert "SELL_" not in quiet.action
        assert engine.get(pos.position_id).remaining_tokens == 100
    finally:
        _close(engine, book)

    fresh, fresh_book, fresh_pos = _open(tmp_path / "narr", 10_000)
    try:
        tick = fresh_book.on_features(
            fresh_pos.position_id,
            MarketFeatures(price=1.05, timestamp=T0 + 1, narrative=token),
        )
        assert tick.trend == TrendState.UNKNOWN
        assert tick.action == "HOLD"
        assert tick.remaining_pct == 100
        assert token not in tick.evidence
        assert fresh.get(fresh_pos.position_id).stop_state == StopState.OPEN
    finally:
        _close(fresh, fresh_book)


def test_trend_breakdown_exits_runner_after_debounce(tmp_path):
    engine, book, pos = _open(tmp_path, 10_000)
    try:
        first = book.on_features(
            pos.position_id,
            MarketFeatures(
                price=1.10,
                timestamp=T0 + 1,
                mcap=12_000,
                prior_mcap=11_000,
                swing_low=1.20,
                narrative="strong buy",
            ),
        )
        assert first.trend == TrendState.BREAKDOWN
        assert first.action == "DEBOUNCE BREAKDOWN 1/2"
        assert first.remaining_pct == 100
        assert engine.get(pos.position_id).stop_state == StopState.OPEN
        second = book.on_features(
            pos.position_id,
            MarketFeatures(
                price=1.10,
                timestamp=T0 + 2,
                mcap=12_000,
                prior_mcap=11_000,
                swing_low=1.20,
                narrative="strong buy",
            ),
        )
        assert second.action == "TREND_BREAKDOWN_EXIT"
        closed = engine.get(pos.position_id)
        assert closed.stop_state == StopState.CLOSED
        assert closed.stop_reason == "TREND_BREAKDOWN"
        assert closed.remaining_tokens == 0
        assert "strong buy" not in second.evidence
    finally:
        _close(engine, book)


def test_regime_context_does_not_exit(tmp_path):
    assert regime_for(30_000) == "MICRO"
    assert regime_for(49_999) == "MICRO"
    assert regime_for(50_000) == "EARLY"
    assert regime_for(149_999) == "EARLY"
    assert regime_for(150_000) == "GROWTH"
    assert regime_for(200_000) == "GROWTH"
    assert regime_for(999_999) == "GROWTH"
    assert regime_for(1_000_000) == "ESTABLISHED"
    assert regime_for(10_000_000) == "LARGE"
    assert regime_for(50_000_000) == "LARGE"
    engine, book, pos = _open(tmp_path, 10_000)
    try:
        ts = T0
        seen = []
        for mcap in (40_000, 50_000, 200_000, 1_000_000, 50_000_000):
            ts += 1
            tick = book.on_features(pos.position_id, _healthy(1.10, ts, mcap, mcap))
            seen.append(tick.regime)
            assert tick.remaining_pct == 100
            assert "SELL_" not in tick.action
            assert engine.get(pos.position_id).stop_state == StopState.OPEN
        assert seen == ["MICRO", "EARLY", "GROWTH", "ESTABLISHED", "LARGE"]
    finally:
        _close(engine, book)


def test_stall_at_50k(tmp_path):
    engine, book, pos = _open(tmp_path, 30_000)
    try:
        ticks = _drive(
            book,
            30_000,
            [
                (36_000, "healthy"),
                (42_000, "healthy"),
                (48_000, "healthy"),
                (51_000, "weak"),
                (51_000, "weak"),
                (48_500, "weak"),
                (48_500, "weak"),
            ],
        )
        last = ticks[-1]
        held = engine.get(pos.position_id)
        assert last.stall is True
        assert last.breakout is False
        assert last.trend == TrendState.DISTRIBUTING
        assert last.action == "STALL 50000 HOLD_RUNNER"
        assert last.remaining_pct == pytest.approx(55)
        assert held.stop_state == StopState.OPEN
        assert held.remaining_tokens == pytest.approx(55)
        assert all(tick.remaining_pct >= 40 for tick in ticks)
        assert all("SELL_ALL" not in tick.action and "MCAP_CEILING" not in tick.action for tick in ticks)
    finally:
        _close(engine, book)


def test_breakout_50k_to_200k(tmp_path):
    engine, book, pos = _open(tmp_path, 40_000)
    try:
        ticks = _drive(
            book,
            40_000,
            [
                (48_000, "accel"),
                (55_000, "accel"),
                (62_000, "accel"),
                (80_000, "accel"),
                (100_000, "accel"),
                (140_000, "accel"),
                (200_000, "accel"),
            ],
        )
        last = ticks[-1]
        held = engine.get(pos.position_id)
        assert last.breakout is True
        assert last.stall is False
        assert last.regime == "GROWTH"
        assert last.trend == TrendState.ACCELERATING
        assert last.action == "SELL_TP3"
        assert last.remaining_pct == 70
        assert held.stop_state == StopState.OPEN
        assert held.remaining_tokens == 70
        assert ticks[2].breakout is True
        assert all(tick.remaining_pct >= 40 for tick in ticks)
    finally:
        _close(engine, book)


def test_runner_to_1m(tmp_path):
    engine, book, pos = _open(tmp_path, 40_000)
    try:
        ticks = _drive(
            book,
            40_000,
            [
                (48_000, "accel"),
                (55_000, "accel"),
                (62_000, "accel"),
                (80_000, "accel"),
                (100_000, "accel"),
                (140_000, "accel"),
                (200_000, "accel"),
                (350_000, "accel"),
                (600_000, "accel"),
                (1_000_000, "accel"),
                (1_000_000, "accel"),
            ],
        )
        last = ticks[-1]
        held = engine.get(pos.position_id)
        row = book.store.get_row(pos.position_id)
        assert row is not None
        assert last.regime == "ESTABLISHED"
        assert last.breakout is True
        assert last.stall is False
        assert "HOLD_RUNNER" in last.action
        assert last.remaining_pct == 70
        assert row.ath_mcap == 1_000_000
        assert held.stop_state == StopState.OPEN
        assert held.remaining_tokens == 70
        assert all(engine.get(pos.position_id).stop_state == StopState.OPEN for _ in ticks)
        assert all("SELL_ALL" not in tick.action and "MCAP_CEILING" not in tick.action for tick in ticks)
    finally:
        _close(engine, book)


def test_adaptive_trail_never_loosens(tmp_path):
    engine, book, pos = _open(tmp_path, 10_000)
    try:
        ts = T0
        first = book.on_features(pos.position_id, _accel(1.30, ts + 1, 12_000, 10_000))
        second = book.on_features(pos.position_id, _accel(1.30, ts + 2, 13_000, 12_000))
        weak = book.on_features(pos.position_id, _weakening(1.30, ts + 3, 13_000, 13_000))
        back = book.on_features(pos.position_id, _accel(1.30, ts + 4, 14_000, 13_000))
        assert first.trail_distance_pct == 12
        assert second.trail_distance_pct == 12
        assert second.current_stop == pytest.approx(1.144)
        assert weak.trend == TrendState.WEAKENING
        assert weak.trail_distance_pct == 8
        assert weak.current_stop == pytest.approx(1.196)
        assert back.trend == TrendState.ACCELERATING
        assert back.trail_distance_pct == 8
        assert back.current_stop == pytest.approx(1.196)
        assert engine.get(pos.position_id).stop_state == StopState.OPEN
        try:
            engine.tighten_stop(pos.position_id, 1.25, actor="grok", reason="loosen via narrative")
            raise AssertionError("grok tighten must be rejected")
        except StopProtectionError:
            pass
        before = engine.get(pos.position_id).current_stop_price
        engine.tighten_stop(pos.position_id, before - 0.05, actor="profit-engine", reason="lower")
        assert engine.get(pos.position_id).current_stop_price == before
    finally:
        _close(engine, book)


def test_principal_recovery(tmp_path):
    engine, book, pos = _open(tmp_path, 10_000, ProfitConfig(principal_recovery_mode=True))
    try:
        first = book.on_features(pos.position_id, _healthy(1.25, T0 + 1, 12_000, 11_000))
        second = book.on_features(pos.position_id, _healthy(1.25, T0 + 2, 13_000, 12_000))
        later = book.on_features(pos.position_id, _healthy(1.50, T0 + 3, 14_000, 13_000))
        row = book.store.get_row(pos.position_id)
        assert row is not None
        assert first.action == "DEBOUNCE PRINCIPAL 1/2"
        assert first.remaining_pct == 100
        assert second.action == "PRINCIPAL_RECOVERY"
        assert second.remaining_pct == 20
        assert row.principal_recovered is True
        assert row.realized_proceeds == 100
        assert later.action == "HOLD"
        assert later.remaining_pct == 20
        assert engine.get(pos.position_id).stop_state == StopState.OPEN
        assert row.stages_filled == ""
    finally:
        _close(engine, book)


def test_counterfactual_tracking(tmp_path):
    engine, book, pos = _open(tmp_path, 10_000)
    path = engine.store.path
    try:
        book.on_features(pos.position_id, _healthy(1.25, T0 + 1, 12_000, 11_000))
        sold = book.on_features(pos.position_id, _healthy(1.25, T0 + 2, 13_000, 12_000))
        later = book.on_features(pos.position_id, _healthy(1.40, T0 + 3, 14_000, 13_000))
        assert sold.action == "SELL_TP1"
        assert later.remaining_pct == 80
        lots = book.store.lots_for(pos.position_id)
        assert len(lots) == 1
        assert lots[0].exit_price == pytest.approx(1.25)
        assert lots[0].counterfactual_pnl_pct == pytest.approx(40)
        assert lots[0].missed_pct_since_exit == pytest.approx(12)
        assert lots[0].post_exit_high_price == pytest.approx(1.40)
    finally:
        _close(engine, book)
    reloaded = ProfitStore(path)
    try:
        lots = reloaded.lots_for("pos-1")
        assert len(lots) == 1
        assert lots[0].counterfactual_pnl_pct == pytest.approx(40)
        assert lots[0].missed_pct_since_exit == pytest.approx(12)
        assert lots[0].post_exit_high_mcap == 14_000
    finally:
        reloaded.close()


def test_profit_status_shows_research(tmp_path):
    engine, book, pos = _open(tmp_path, 10_000)
    path = engine.store.path
    try:
        book.on_features(pos.position_id, _healthy(1.25, T0 + 1, 20_000, 15_000))
        book.on_features(pos.position_id, _healthy(1.25, T0 + 2, 25_000, 20_000))
        text = render_status(
            engine.config,
            engine.store.load_active(),
            {pos.position_id: book.store.get_row(pos.position_id)},
        )
    finally:
        _close(engine, book)
    loaded = load_status(Path(path))
    for text in (text, loaded):
        assert "CURRENT MODE=PAPER" in text
        assert "REAL TRADES=NO" in text
        assert "entry_mcap=10000" in text
        assert "current_mcap=25000" in text
        assert "ath_mcap=25000" in text
        assert "realized=" in text
        assert "unrealized=" in text
        assert "remaining_pct=" in text
        assert "trend=" in text
        assert "price_structure=" in text
        assert "next_action=" in text
        assert "trailing_stop=" in text
        assert "not a decision input" in text


def test_profit_paper_mode_only(tmp_path):
    config = ProfitConfig.from_env({
        "TRADING_MODE": "live",
        "TP1_SELL_PCT": "100",
        "REAL_TRADES": "yes",
    })
    assert config.trading_mode == "paper"
    assert config.real_trades is False
    assert all(stage.sell_pct <= 40 for stage in config.stages)
    assert sum(stage.sell_pct for stage in config.stages) <= 60
    engine = make_engine(tmp_path / "book.sqlite")
    store = ProfitStore(tmp_path / "book.sqlite")
    try:
        try:
            ProfitBook(engine, store, ProfitConfig(trading_mode="live", real_trades=True))
            raise AssertionError("live profit book must be refused")
        except RuntimeError as exc:
            assert "REAL TRADES=NO" in str(exc)
    finally:
        store.close()
        engine.close()
