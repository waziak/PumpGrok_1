"""Staged profit-taking on top of the stop-loss engine.

There is no market-cap ceiling and no "sell all at +50%". Three partials
leave a runner. Trend strength changes partial size and how tight the
trail is. The trail and the peak-profit giveback allowance only tighten.

A hard emergency stop, evaluated first by the stop engine, still flattens
the position. Grok narrative is stored and never scored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from .engine import ExitPriority, StopEngine
from .models import MarketView, Quote, iso, qprice
from .trend import (
    POSITIVE_TRENDS,
    WATCH_LEVELS,
    WEAK_TRENDS,
    MarketFeatures,
    TrendAssessment,
    TrendState,
    mcap_velocity,
    regime_for,
    score_trend,
)

PROFIT_ACTOR = "profit-engine"

# Distance under the high-water mark. Smaller means a tighter trail.
_TRAIL_DISTANCE = {
    TrendState.ACCELERATING: 12.0,
    TrendState.STRONG: 12.0,
    TrendState.HEALTHY: 10.0,
    TrendState.WEAKENING: 8.0,
    TrendState.DISTRIBUTING: 6.0,
    TrendState.BREAKDOWN: 4.0,
}

# How much peak profit (percentage points) may be given back before a
# defensive partial. Smaller means tighter protection.
_GIVEBACK = {
    TrendState.ACCELERATING: 45.0,
    TrendState.STRONG: 35.0,
    TrendState.HEALTHY: 25.0,
    TrendState.WEAKENING: 15.0,
    TrendState.DISTRIBUTING: 10.0,
    TrendState.BREAKDOWN: 5.0,
}

# Multiplier on the configured partial. Accelerating sells less.
_PARTIAL_MULTIPLIER = {
    TrendState.ACCELERATING: 0.50,
    TrendState.STRONG: 0.75,
    TrendState.HEALTHY: 1.00,
    TrendState.WEAKENING: 1.00,
    TrendState.DISTRIBUTING: 1.25,
    TrendState.BREAKDOWN: 1.00,
}

_MAX_STAGE_SELL_PCT = 40.0
_MAX_STAGED_SELL_SUM = 60.0
_MIN_DEBOUNCE = 2


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def _env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class ProfitStage:
    name: str
    trigger_pct: float
    sell_pct: float


@dataclass(frozen=True)
class ProfitConfig:
    """Research defaults. Env values cannot turn partials into a full exit."""

    stages: tuple[ProfitStage, ...] = (
        ProfitStage("TP1", 25.0, 20.0),
        ProfitStage("TP2", 50.0, 20.0),
        ProfitStage("TP3", 100.0, 20.0),
    )
    runner_floor_pct: float = 40.0
    principal_recovery_mode: bool = False
    principal_recovery_trigger_pct: float = 25.0
    principal_runner_floor_pct: float = 20.0
    debounce_marks: int = 2
    acceptance_marks: int = 2
    peak_activation_pct: float = 25.0
    trading_mode: str = "paper"
    real_trades: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ProfitConfig:
        source = {} if env is None else env
        raw_stages = []
        defaults = (("TP1", "TP1_PCT", 25.0, "TP1_SELL_PCT", 20.0),
                    ("TP2", "TP2_PCT", 50.0, "TP2_SELL_PCT", 20.0),
                    ("TP3", "TP3_PCT", 100.0, "TP3_SELL_PCT", 20.0))
        for name, pct_key, pct_default, sell_key, sell_default in defaults:
            trigger = max(0.1, _env_float(source, pct_key, pct_default))
            sell = _clamp(_env_float(source, sell_key, sell_default), 1.0, _MAX_STAGE_SELL_PCT)
            raw_stages.append(ProfitStage(name, trigger, sell))
        ordered = tuple(sorted(raw_stages, key=lambda stage: stage.trigger_pct))
        total_sell = sum(stage.sell_pct for stage in ordered)
        if total_sell > _MAX_STAGED_SELL_SUM:
            scale = _MAX_STAGED_SELL_SUM / total_sell
            ordered = tuple(
                ProfitStage(stage.name, stage.trigger_pct, round(stage.sell_pct * scale, 4))
                for stage in ordered
            )
        mode_raw = str(source.get("PRINCIPAL_RECOVERY_MODE", "false")).strip().lower()
        principal = mode_raw in {"1", "true", "yes", "on"}
        debounce = int(_clamp(_env_float(source, "PARTIAL_DEBOUNCE_MARKS", 2), _MIN_DEBOUNCE, 5))
        floor = _clamp(_env_float(source, "RUNNER_FLOOR_PCT", 40.0), 20.0, 70.0)
        return cls(
            stages=ordered,
            runner_floor_pct=floor,
            principal_recovery_mode=principal,
            debounce_marks=debounce,
            trading_mode="paper",
            real_trades=False,
        )


@dataclass
class ProfitRow:
    position_id: str
    entry_mcap: float | None
    current_mcap: float | None
    ath_mcap: float | None
    peak_pnl_pct: float
    realized_pnl: float
    realized_proceeds: float
    trend_state: str
    trend_score: float | None
    trend_evidence: str
    regime: str
    stall: bool
    breakout: bool
    stall_level: float | None
    breakout_level: float | None
    next_action: str
    trail_distance_pct: float | None
    giveback_allowance: float | None
    principal_recovery_mode: bool
    principal_recovered: bool
    stages_filled: str
    debounce_key: str
    debounce_count: int
    accepted_levels: str
    level_streaks: str
    narrative: str
    updated_at: str


@dataclass
class ClosedLot:
    lot_id: int | None
    position_id: str
    tokens: float
    exit_price: float
    exit_mcap: float | None
    exit_pnl_pct: float
    reason: str
    exited_at: str
    post_exit_high_price: float
    post_exit_high_mcap: float | None
    last_price: float
    last_mcap: float | None
    counterfactual_pnl_pct: float
    missed_pct_since_exit: float


@dataclass
class TickResult:
    position_id: str
    action: str
    trend: str
    regime: str
    stall: bool
    breakout: bool
    remaining_tokens: float
    remaining_pct: float
    stop_first: bool
    evidence: str
    next_action: str
    trail_distance_pct: float | None
    current_stop: float
    entry_mcap: float | None
    current_mcap: float | None
    ath_mcap: float | None
    realized_pnl: float
    unrealized_pnl: float
    narrative: str


def _split_floats(text: str) -> set[float]:
    if not text.strip():
        return set()
    return {float(part) for part in text.split(",") if part}


def _join_floats(values: set[float]) -> str:
    return ",".join(str(int(value) if value.is_integer() else value) for value in sorted(values))


def _parse_streaks(text: str) -> dict[float, int]:
    streaks: dict[float, int] = {}
    if not text.strip():
        return streaks
    for part in text.split(","):
        level, _, count = part.partition("=")
        if level:
            streaks[float(level)] = int(count or "0")
    return streaks


def _format_streaks(streaks: dict[float, int]) -> str:
    return ",".join(
        f"{int(level) if float(level).is_integer() else level}={count}"
        for level, count in sorted(streaks.items())
    )


class ProfitStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profit_positions (
                position_id TEXT PRIMARY KEY,
                entry_mcap REAL,
                current_mcap REAL,
                ath_mcap REAL,
                peak_pnl_pct REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                realized_proceeds REAL NOT NULL DEFAULT 0,
                trend_state TEXT NOT NULL,
                trend_score REAL,
                trend_evidence TEXT NOT NULL DEFAULT '',
                regime TEXT NOT NULL,
                stall INTEGER NOT NULL DEFAULT 0,
                breakout INTEGER NOT NULL DEFAULT 0,
                stall_level REAL,
                breakout_level REAL,
                next_action TEXT NOT NULL DEFAULT '',
                trail_distance_pct REAL,
                giveback_allowance REAL,
                principal_recovery_mode INTEGER NOT NULL DEFAULT 0,
                principal_recovered INTEGER NOT NULL DEFAULT 0,
                stages_filled TEXT NOT NULL DEFAULT '',
                debounce_key TEXT NOT NULL DEFAULT '',
                debounce_count INTEGER NOT NULL DEFAULT 0,
                accepted_levels TEXT NOT NULL DEFAULT '',
                level_streaks TEXT NOT NULL DEFAULT '',
                narrative TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profit_lots (
                lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                tokens REAL NOT NULL,
                exit_price REAL NOT NULL,
                exit_mcap REAL,
                exit_pnl_pct REAL NOT NULL,
                reason TEXT NOT NULL,
                exited_at TEXT NOT NULL,
                post_exit_high_price REAL NOT NULL,
                post_exit_high_mcap REAL,
                last_price REAL NOT NULL,
                last_mcap REAL,
                counterfactual_pnl_pct REAL NOT NULL,
                missed_pct_since_exit REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save_row(self, row: ProfitRow) -> None:
        self._conn.execute(
            """
            INSERT INTO profit_positions (
                position_id, entry_mcap, current_mcap, ath_mcap, peak_pnl_pct,
                realized_pnl, realized_proceeds, trend_state, trend_score, trend_evidence,
                regime, stall, breakout, stall_level, breakout_level, next_action,
                trail_distance_pct, giveback_allowance, principal_recovery_mode,
                principal_recovered, stages_filled, debounce_key, debounce_count,
                accepted_levels, level_streaks, narrative, updated_at
            ) VALUES (
                :position_id, :entry_mcap, :current_mcap, :ath_mcap, :peak_pnl_pct,
                :realized_pnl, :realized_proceeds, :trend_state, :trend_score, :trend_evidence,
                :regime, :stall, :breakout, :stall_level, :breakout_level, :next_action,
                :trail_distance_pct, :giveback_allowance, :principal_recovery_mode,
                :principal_recovered, :stages_filled, :debounce_key, :debounce_count,
                :accepted_levels, :level_streaks, :narrative, :updated_at
            )
            ON CONFLICT(position_id) DO UPDATE SET
                entry_mcap=excluded.entry_mcap,
                current_mcap=excluded.current_mcap,
                ath_mcap=excluded.ath_mcap,
                peak_pnl_pct=excluded.peak_pnl_pct,
                realized_pnl=excluded.realized_pnl,
                realized_proceeds=excluded.realized_proceeds,
                trend_state=excluded.trend_state,
                trend_score=excluded.trend_score,
                trend_evidence=excluded.trend_evidence,
                regime=excluded.regime,
                stall=excluded.stall,
                breakout=excluded.breakout,
                stall_level=excluded.stall_level,
                breakout_level=excluded.breakout_level,
                next_action=excluded.next_action,
                trail_distance_pct=excluded.trail_distance_pct,
                giveback_allowance=excluded.giveback_allowance,
                principal_recovery_mode=excluded.principal_recovery_mode,
                principal_recovered=excluded.principal_recovered,
                stages_filled=excluded.stages_filled,
                debounce_key=excluded.debounce_key,
                debounce_count=excluded.debounce_count,
                accepted_levels=excluded.accepted_levels,
                level_streaks=excluded.level_streaks,
                narrative=excluded.narrative,
                updated_at=excluded.updated_at
            """,
            self._row_params(row),
        )
        self._conn.commit()

    def get_row(self, position_id: str) -> ProfitRow | None:
        found = self._conn.execute(
            "SELECT * FROM profit_positions WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        if found is None:
            return None
        return self._row_from(found)

    def all_rows(self) -> list[ProfitRow]:
        return [self._row_from(row) for row in self._conn.execute("SELECT * FROM profit_positions")]

    def insert_lot(self, lot: ClosedLot) -> ClosedLot:
        cursor = self._conn.execute(
            """
            INSERT INTO profit_lots (
                position_id, tokens, exit_price, exit_mcap, exit_pnl_pct, reason, exited_at,
                post_exit_high_price, post_exit_high_mcap, last_price, last_mcap,
                counterfactual_pnl_pct, missed_pct_since_exit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lot.position_id, lot.tokens, lot.exit_price, lot.exit_mcap, lot.exit_pnl_pct,
                lot.reason, lot.exited_at, lot.post_exit_high_price, lot.post_exit_high_mcap,
                lot.last_price, lot.last_mcap, lot.counterfactual_pnl_pct, lot.missed_pct_since_exit,
            ),
        )
        self._conn.commit()
        return replace(lot, lot_id=int(cursor.lastrowid))

    def lots_for(self, position_id: str) -> list[ClosedLot]:
        rows = self._conn.execute(
            "SELECT * FROM profit_lots WHERE position_id = ? ORDER BY lot_id ASC",
            (position_id,),
        ).fetchall()
        return [self._lot_from(row) for row in rows]

    def save_lot(self, lot: ClosedLot) -> None:
        if lot.lot_id is None:
            raise ValueError("lot_id required")
        self._conn.execute(
            """
            UPDATE profit_lots SET
                last_price=?, last_mcap=?, post_exit_high_price=?, post_exit_high_mcap=?,
                counterfactual_pnl_pct=?, missed_pct_since_exit=?
            WHERE lot_id=?
            """,
            (
                lot.last_price, lot.last_mcap, lot.post_exit_high_price, lot.post_exit_high_mcap,
                lot.counterfactual_pnl_pct, lot.missed_pct_since_exit, lot.lot_id,
            ),
        )
        self._conn.commit()

    @staticmethod
    def _row_params(row: ProfitRow) -> dict[str, object]:
        return {
            "position_id": row.position_id,
            "entry_mcap": row.entry_mcap,
            "current_mcap": row.current_mcap,
            "ath_mcap": row.ath_mcap,
            "peak_pnl_pct": row.peak_pnl_pct,
            "realized_pnl": row.realized_pnl,
            "realized_proceeds": row.realized_proceeds,
            "trend_state": row.trend_state,
            "trend_score": row.trend_score,
            "trend_evidence": row.trend_evidence,
            "regime": row.regime,
            "stall": 1 if row.stall else 0,
            "breakout": 1 if row.breakout else 0,
            "stall_level": row.stall_level,
            "breakout_level": row.breakout_level,
            "next_action": row.next_action,
            "trail_distance_pct": row.trail_distance_pct,
            "giveback_allowance": row.giveback_allowance,
            "principal_recovery_mode": 1 if row.principal_recovery_mode else 0,
            "principal_recovered": 1 if row.principal_recovered else 0,
            "stages_filled": row.stages_filled,
            "debounce_key": row.debounce_key,
            "debounce_count": row.debounce_count,
            "accepted_levels": row.accepted_levels,
            "level_streaks": row.level_streaks,
            "narrative": row.narrative,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _row_from(row: sqlite3.Row) -> ProfitRow:
        return ProfitRow(
            position_id=row["position_id"],
            entry_mcap=row["entry_mcap"],
            current_mcap=row["current_mcap"],
            ath_mcap=row["ath_mcap"],
            peak_pnl_pct=row["peak_pnl_pct"],
            realized_pnl=row["realized_pnl"],
            realized_proceeds=row["realized_proceeds"],
            trend_state=row["trend_state"],
            trend_score=row["trend_score"],
            trend_evidence=row["trend_evidence"],
            regime=row["regime"],
            stall=bool(row["stall"]),
            breakout=bool(row["breakout"]),
            stall_level=row["stall_level"],
            breakout_level=row["breakout_level"],
            next_action=row["next_action"],
            trail_distance_pct=row["trail_distance_pct"],
            giveback_allowance=row["giveback_allowance"],
            principal_recovery_mode=bool(row["principal_recovery_mode"]),
            principal_recovered=bool(row["principal_recovered"]),
            stages_filled=row["stages_filled"],
            debounce_key=row["debounce_key"],
            debounce_count=row["debounce_count"],
            accepted_levels=row["accepted_levels"],
            level_streaks=row["level_streaks"],
            narrative=row["narrative"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _lot_from(row: sqlite3.Row) -> ClosedLot:
        return ClosedLot(
            lot_id=row["lot_id"],
            position_id=row["position_id"],
            tokens=row["tokens"],
            exit_price=row["exit_price"],
            exit_mcap=row["exit_mcap"],
            exit_pnl_pct=row["exit_pnl_pct"],
            reason=row["reason"],
            exited_at=row["exited_at"],
            post_exit_high_price=row["post_exit_high_price"],
            post_exit_high_mcap=row["post_exit_high_mcap"],
            last_price=row["last_price"],
            last_mcap=row["last_mcap"],
            counterfactual_pnl_pct=row["counterfactual_pnl_pct"],
            missed_pct_since_exit=row["missed_pct_since_exit"],
        )


class ProfitBook:
    """Paper profit manager. Uses the shared stop engine and never sends live."""

    def __init__(self, engine: StopEngine, store: ProfitStore, config: ProfitConfig | None = None) -> None:
        if engine.config.real_trades or engine.config.live_network_sends:
            raise RuntimeError("REAL TRADES=NO")
        self.engine = engine
        self.store = store
        self.config = config or ProfitConfig.from_env({})
        if self.config.real_trades or self.config.trading_mode != "paper":
            raise RuntimeError("REAL TRADES=NO")

    def close(self) -> None:
        self.store.close()

    def attach(self, position_id: str, entry_mcap: float | None, now: float | None = None) -> ProfitRow:
        position = self.engine.get(position_id)
        moment = self.engine.clock() if now is None else now
        row = ProfitRow(
            position_id=position_id,
            entry_mcap=entry_mcap,
            current_mcap=entry_mcap,
            ath_mcap=entry_mcap,
            peak_pnl_pct=0.0,
            realized_pnl=0.0,
            realized_proceeds=0.0,
            trend_state=TrendState.UNKNOWN,
            trend_score=None,
            trend_evidence="no mark yet",
            regime=regime_for(entry_mcap),
            stall=False,
            breakout=False,
            stall_level=None,
            breakout_level=None,
            next_action="HOLD",
            trail_distance_pct=None,
            giveback_allowance=None,
            principal_recovery_mode=self.config.principal_recovery_mode,
            principal_recovered=False,
            stages_filled="",
            debounce_key="",
            debounce_count=0,
            accepted_levels="",
            level_streaks="",
            narrative="",
            updated_at=iso(moment),
        )
        del position
        self.store.save_row(row)
        return row

    def on_features(self, position_id: str, features: MarketFeatures, now: float | None = None) -> TickResult:
        moment = features.timestamp if now is None else now
        position = self.engine.get(position_id)
        row = self.store.get_row(position_id)
        if row is None:
            row = self.attach(position_id, features.mcap, moment)

        view = MarketView(
            position_id,
            Quote(
                price=features.price,
                timestamp=moment,
                liquidity=features.liquidity,
                source="profit-mark",
            ),
        )
        decision = self.engine.on_mark(view, moment)
        position = self.engine.get(position_id)
        # Narrative is recorded after scoring so it cannot leak into the score.
        assessment = score_trend(features)
        self._remember_context(row, position.entry_fill_price, features, assessment, moment)

        if decision.should_exit and decision.priority in (
            ExitPriority.EMERGENCY_EXIT,
            ExitPriority.STOP_LOSS,
        ):
            row.next_action = f"STOP_EXIT {decision.reason}"
            row.debounce_key = ""
            row.debounce_count = 0
            self.store.save_row(row)
            return self._tick(position_id, row, action=row.next_action, stop_first=True)

        if decision.stale or position.stop_state != "OPEN":
            row.next_action = "HOLD_STALE" if decision.stale else "HOLD"
            self.store.save_row(row)
            return self._tick(position_id, row, action=row.next_action, stop_first=False)

        self._tighten_trail(position_id, row, assessment)
        self._update_lots(position_id, features)
        action = self._maybe_act(position_id, row, features, assessment, moment)
        self.store.save_row(row)
        return self._tick(position_id, row, action=action, stop_first=False)

    def _remember_context(
        self,
        row: ProfitRow,
        entry: float,
        features: MarketFeatures,
        assessment: TrendAssessment,
        moment: float,
    ) -> None:
        pnl = (features.price - entry) / entry * 100.0 if entry > 0 else 0.0
        row.peak_pnl_pct = max(row.peak_pnl_pct, pnl)
        row.current_mcap = features.mcap
        if features.mcap is not None:
            row.ath_mcap = features.mcap if row.ath_mcap is None else max(row.ath_mcap, features.mcap)
        row.trend_state = assessment.state
        row.trend_score = assessment.score
        row.trend_evidence = assessment.evidence
        row.regime = regime_for(features.mcap)
        row.narrative = features.narrative or ""
        row.updated_at = iso(moment)
        if assessment.state in _GIVEBACK:
            proposed = _GIVEBACK[assessment.state]
            row.giveback_allowance = (
                proposed if row.giveback_allowance is None else min(row.giveback_allowance, proposed)
            )
        self._update_levels(row, features, assessment)
        velocity = mcap_velocity(features)
        stall_level, breakout_level = _stall_breakout(
            ath=row.ath_mcap,
            mcap=features.mcap,
            trend=assessment.state,
            accepted=_split_floats(row.accepted_levels),
        )
        # A live breakout above a level clears a stall at that same level.
        if breakout_level is not None and stall_level is not None and breakout_level >= stall_level:
            stall_level = None
        row.stall_level = stall_level
        row.breakout_level = breakout_level
        row.stall = stall_level is not None
        row.breakout = breakout_level is not None
        del velocity

    def _update_levels(self, row: ProfitRow, features: MarketFeatures, assessment: TrendAssessment) -> None:
        if features.mcap is None:
            return
        streaks = _parse_streaks(row.level_streaks)
        accepted = _split_floats(row.accepted_levels)
        velocity = mcap_velocity(features)
        positive = assessment.state in POSITIVE_TRENDS and velocity is not None and velocity >= 0
        for level in WATCH_LEVELS:
            if features.mcap >= level and positive:
                streaks[level] = streaks.get(level, 0) + 1
            else:
                streaks[level] = 0
            if streaks[level] >= self.config.acceptance_marks:
                accepted.add(level)
        row.level_streaks = _format_streaks(streaks)
        row.accepted_levels = _join_floats(accepted)

    def _tighten_trail(self, position_id: str, row: ProfitRow, assessment: TrendAssessment) -> None:
        if assessment.state not in _TRAIL_DISTANCE:
            return
        proposed = _TRAIL_DISTANCE[assessment.state]
        distance = proposed if row.trail_distance_pct is None else min(row.trail_distance_pct, proposed)
        row.trail_distance_pct = distance
        position = self.engine.get(position_id)
        trail = position.high_water_price * (1.0 - distance / 100.0)
        if trail > position.current_stop_price:
            self.engine.tighten_stop(
                position_id,
                trail,
                actor=PROFIT_ACTOR,
                reason=f"adaptive trail {distance:.1f}% ({assessment.state})",
            )

    def _maybe_act(
        self,
        position_id: str,
        row: ProfitRow,
        features: MarketFeatures,
        assessment: TrendAssessment,
        moment: float,
    ) -> str:
        position = self.engine.get(position_id)
        entry = position.entry_fill_price
        pnl = (features.price - entry) / entry * 100.0 if entry > 0 else 0.0
        planned = self._plan(row, position.size_tokens, position.remaining_tokens, pnl, assessment)
        if planned is None:
            row.debounce_key = ""
            row.debounce_count = 0
            if row.stall:
                row.next_action = f"STALL {int(row.stall_level or 0)} HOLD_RUNNER"
            elif row.breakout:
                row.next_action = f"BREAKOUT {int(row.breakout_level or 0)} HOLD_RUNNER"
            else:
                row.next_action = "HOLD_RUNNER" if row.stages_filled else "HOLD"
            return row.next_action
        key, kind, reason, sell_pct = planned
        if row.debounce_key != key:
            row.debounce_key = key
            row.debounce_count = 1
        else:
            row.debounce_count += 1
        if row.debounce_count < self.config.debounce_marks:
            row.next_action = f"DEBOUNCE {key} {row.debounce_count}/{self.config.debounce_marks}"
            return row.next_action
        row.debounce_count = 0
        row.debounce_key = ""
        if kind == "breakdown":
            sold = self.engine.close_for_trend_breakdown(
                position_id, features.price, actor=PROFIT_ACTOR, now=moment
            )
            self._record_lot(row, sold, features, entry, reason, moment)
            row.next_action = reason
            return reason
        tokens = self._tokens_for(position_id, row, kind, sell_pct, features.price, assessment.state)
        if tokens <= 0:
            row.next_action = "HOLD_RUNNER"
            return row.next_action
        self.engine.apply_partial_take_profit(
            position_id, tokens, features.price, to_breakeven=True, now=moment
        )
        self._record_lot(row, tokens, features, entry, reason, moment)
        if kind == "principal":
            row.principal_recovered = True
        elif kind == "stage":
            filled = [part for part in row.stages_filled.split(",") if part]
            filled.append(key)
            row.stages_filled = ",".join(filled)
        row.next_action = reason
        return reason

    def _plan(
        self,
        row: ProfitRow,
        size: float,
        remaining: float,
        pnl: float,
        assessment: TrendAssessment,
    ) -> tuple[str, str, str, float] | None:
        del size
        if remaining <= 0 or assessment.state == TrendState.UNKNOWN:
            return None
        if assessment.state == TrendState.BREAKDOWN:
            return ("BREAKDOWN", "breakdown", "TREND_BREAKDOWN_EXIT", 0.0)
        allowance = row.giveback_allowance
        giveback = row.peak_pnl_pct - pnl
        if (
            allowance is not None
            and row.peak_pnl_pct >= self.config.peak_activation_pct
            and giveback + 1e-9 >= allowance
        ):
            return ("PEAK_DRAWDOWN", "drawdown", "PEAK_DRAWDOWN_PARTIAL", 20.0)
        if (
            row.principal_recovery_mode
            and not row.principal_recovered
            and pnl + 1e-9 >= self.config.principal_recovery_trigger_pct
        ):
            return ("PRINCIPAL", "principal", "PRINCIPAL_RECOVERY", 0.0)
        if row.principal_recovered:
            return None
        filled = {part for part in row.stages_filled.split(",") if part}
        for stage in self.config.stages:
            if stage.name in filled:
                continue
            if pnl + 1e-9 >= stage.trigger_pct:
                # One stage at a time. A jump through +50% does not flatten.
                return (stage.name, "stage", f"SELL_{stage.name}", stage.sell_pct)
        return None

    def _tokens_for(
        self,
        position_id: str,
        row: ProfitRow,
        kind: str,
        sell_pct: float,
        price: float,
        trend: str,
    ) -> float:
        position = self.engine.get(position_id)
        size = position.size_tokens
        remaining = position.remaining_tokens
        if kind == "principal":
            floor = size * (self.config.principal_runner_floor_pct / 100.0)
            shortfall = size * position.entry_fill_price - row.realized_proceeds
            if shortfall <= 0 or price <= 0:
                row.principal_recovered = True
                return 0.0
            want = shortfall / price
        else:
            floor = size * (self.config.runner_floor_pct / 100.0)
            multiplier = _PARTIAL_MULTIPLIER.get(trend, 1.0)
            want = size * (sell_pct / 100.0) * multiplier
        room = remaining - floor
        if room <= size * 0.01:
            return 0.0
        sell = min(want, room)
        if sell >= remaining:
            sell = remaining * 0.99
        if sell <= 0:
            return 0.0
        return qprice(sell)

    def _record_lot(
        self,
        row: ProfitRow,
        tokens: float,
        features: MarketFeatures,
        entry: float,
        reason: str,
        moment: float,
    ) -> None:
        if tokens <= 0:
            return
        proceeds = tokens * features.price
        row.realized_proceeds += proceeds
        row.realized_pnl += proceeds - tokens * entry
        exit_pnl = (features.price - entry) / entry * 100.0 if entry else 0.0
        self.store.insert_lot(
            ClosedLot(
                lot_id=None,
                position_id=row.position_id,
                tokens=tokens,
                exit_price=features.price,
                exit_mcap=features.mcap,
                exit_pnl_pct=exit_pnl,
                reason=reason,
                exited_at=iso(moment),
                post_exit_high_price=features.price,
                post_exit_high_mcap=features.mcap,
                last_price=features.price,
                last_mcap=features.mcap,
                counterfactual_pnl_pct=exit_pnl,
                missed_pct_since_exit=0.0,
            )
        )

    def _update_lots(self, position_id: str, features: MarketFeatures) -> None:
        position = self.engine.get(position_id)
        entry = position.entry_fill_price
        for lot in self.store.lots_for(position_id):
            lot.last_price = features.price
            lot.last_mcap = features.mcap
            if features.price > lot.post_exit_high_price:
                lot.post_exit_high_price = features.price
                lot.post_exit_high_mcap = features.mcap
            lot.counterfactual_pnl_pct = (features.price - entry) / entry * 100.0 if entry else 0.0
            lot.missed_pct_since_exit = (
                (features.price - lot.exit_price) / lot.exit_price * 100.0 if lot.exit_price else 0.0
            )
            self.store.save_lot(lot)

    def _tick(self, position_id: str, row: ProfitRow, *, action: str, stop_first: bool) -> TickResult:
        position = self.engine.get(position_id)
        entry = position.entry_fill_price
        price = position.last_mark_price or entry
        unrealized = (price - entry) * position.remaining_tokens
        remaining_pct = (
            position.remaining_tokens / position.size_tokens * 100.0 if position.size_tokens else 0.0
        )
        return TickResult(
            position_id=position_id,
            action=action,
            trend=row.trend_state,
            regime=row.regime,
            stall=row.stall,
            breakout=row.breakout,
            remaining_tokens=position.remaining_tokens,
            remaining_pct=remaining_pct,
            stop_first=stop_first,
            evidence=row.trend_evidence,
            next_action=row.next_action,
            trail_distance_pct=row.trail_distance_pct,
            current_stop=position.current_stop_price,
            entry_mcap=row.entry_mcap,
            current_mcap=row.current_mcap,
            ath_mcap=row.ath_mcap,
            realized_pnl=row.realized_pnl,
            unrealized_pnl=unrealized,
            narrative=row.narrative,
        )


def _stall_breakout(
    *,
    ath: float | None,
    mcap: float | None,
    trend: str,
    accepted: set[float],
) -> tuple[float | None, float | None]:
    if mcap is None or ath is None:
        return None, None
    breakout_level = None
    for level in sorted(WATCH_LEVELS, reverse=True):
        if level in accepted and mcap + 1e-9 >= level:
            breakout_level = level
            break
    stall_level = None
    if trend in WEAK_TRENDS:
        for level in sorted(WATCH_LEVELS, reverse=True):
            if ath + 1e-9 >= level and mcap < level and level not in accepted:
                stall_level = level
                break
    return stall_level, breakout_level
