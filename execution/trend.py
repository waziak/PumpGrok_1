"""Deterministic trend and market-cap regime classification.

Every state is the mean of the measured components below. Narrative text is
not an input. Missing measurements produce UNKNOWN, not a guessed score.
"""

from __future__ import annotations

from dataclasses import dataclass

# Market-cap bands are context for stall and breakout detection.
# They are not sell triggers.
MICRO_MAX = 50_000.0
EARLY_MAX = 150_000.0
GROWTH_MAX = 1_000_000.0
ESTABLISHED_MAX = 10_000_000.0

WATCH_LEVELS = (50_000.0, 150_000.0, 250_000.0, 1_000_000.0, 10_000_000.0)

POSITIVE_TRENDS = frozenset({"ACCELERATING", "STRONG", "HEALTHY"})
WEAK_TRENDS = frozenset({"WEAKENING", "DISTRIBUTING", "BREAKDOWN"})


class TrendState:
    ACCELERATING = "ACCELERATING"
    STRONG = "STRONG"
    HEALTHY = "HEALTHY"
    WEAKENING = "WEAKENING"
    DISTRIBUTING = "DISTRIBUTING"
    BREAKDOWN = "BREAKDOWN"
    UNKNOWN = "UNKNOWN"


class McapRegime:
    MICRO = "MICRO"
    EARLY = "EARLY"
    GROWTH = "GROWTH"
    ESTABLISHED = "ESTABLISHED"
    LARGE = "LARGE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ComponentScore:
    name: str
    score: float
    evidence: str


@dataclass(frozen=True)
class TrendAssessment:
    state: str
    score: float | None
    components: tuple[ComponentScore, ...]
    evidence: str


@dataclass
class MarketFeatures:
    """Measurable structure for one mark. `narrative` is stored elsewhere."""

    price: float
    timestamp: float
    mcap: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    buy_volume: float | None = None
    sell_volume: float | None = None
    new_buyers: float | None = None
    prior_new_buyers: float | None = None
    holders: float | None = None
    prior_holders: float | None = None
    liquidity: float | None = None
    prior_liquidity: float | None = None
    prior_mcap: float | None = None
    higher_high: bool | None = None
    higher_low: bool | None = None
    swing_low: float | None = None
    narrative: str | None = None


def clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def regime_for(mcap: float | None) -> str:
    if mcap is None or mcap <= 0:
        return McapRegime.UNKNOWN
    if mcap < MICRO_MAX:
        return McapRegime.MICRO
    if mcap < EARLY_MAX:
        return McapRegime.EARLY
    if mcap < GROWTH_MAX:
        return McapRegime.GROWTH
    if mcap < ESTABLISHED_MAX:
        return McapRegime.ESTABLISHED
    return McapRegime.LARGE


def _structure(features: MarketFeatures) -> ComponentScore | None:
    if features.swing_low is not None and features.price < features.swing_low:
        return ComponentScore(
            "price_structure",
            -1.0,
            f"price {features.price:.4f} is below swing low {features.swing_low:.4f}",
        )
    high = features.higher_high
    low = features.higher_low
    if high is None and low is None:
        return None
    if high and low:
        return ComponentScore("price_structure", 1.0, "higher high and higher low")
    if high and low is False:
        return ComponentScore("price_structure", 0.30, "higher high, lower low")
    if high is False and low:
        return ComponentScore("price_structure", -0.20, "lower high, higher low")
    if high is False and low is False:
        return ComponentScore("price_structure", -0.45, "lower high and lower low")
    if high:
        return ComponentScore("price_structure", 0.60, "higher high")
    return ComponentScore("price_structure", 0.20, "higher low")


def _volume(features: MarketFeatures) -> ComponentScore | None:
    if features.volume is None or features.avg_volume is None or features.avg_volume <= 0:
        return None
    ratio = features.volume / features.avg_volume
    if ratio >= 1.5:
        score = 1.0
    elif ratio >= 1.1:
        score = 0.40
    elif ratio <= 0.7:
        score = -0.50
    else:
        score = 0.0
    return ComponentScore("volume", score, f"volume/avg {ratio:.2f}")


def _flow(features: MarketFeatures) -> ComponentScore | None:
    buy = features.buy_volume
    sell = features.sell_volume
    if buy is None or sell is None or (buy + sell) <= 0:
        return None
    ratio = buy / (buy + sell)
    score = clamp((ratio - 0.5) * 2.0)
    return ComponentScore("flow", score, f"buy share {ratio:.2f}")


def _buyer_velocity(features: MarketFeatures) -> ComponentScore | None:
    current = features.new_buyers
    prior = features.prior_new_buyers
    if current is None or prior is None:
        return None
    if prior <= 0:
        score = 1.0 if current > 0 else 0.0
        return ComponentScore("buyer_velocity", score, f"new buyers {current:.0f} from {prior:.0f}")
    change = current / prior - 1.0
    return ComponentScore(
        "buyer_velocity",
        clamp(change),
        f"new buyers {current:.0f} vs {prior:.0f} ({change:+.0%})",
    )


def _change_component(
    name: str,
    current: float | None,
    prior: float | None,
    *,
    scale: float,
    noun: str,
) -> ComponentScore | None:
    if current is None or prior is None or prior <= 0:
        return None
    change = (current - prior) / prior
    return ComponentScore(name, clamp(change / scale), f"{noun} {change:+.0%}")


def score_trend(features: MarketFeatures) -> TrendAssessment:
    """Score structure only. `features.narrative` is intentionally unused."""
    parts = [
        item
        for item in (
            _structure(features),
            _volume(features),
            _flow(features),
            _buyer_velocity(features),
            _change_component(
                "holders", features.holders, features.prior_holders, scale=0.20, noun="holders"
            ),
            _change_component(
                "liquidity",
                features.liquidity,
                features.prior_liquidity,
                scale=0.20,
                noun="liquidity",
            ),
            _change_component(
                "mcap_velocity",
                features.mcap,
                features.prior_mcap,
                scale=0.15,
                noun="mcap",
            ),
        )
        if item is not None
    ]
    by_name = {item.name: item.score for item in parts}
    if not parts:
        return TrendAssessment(
            TrendState.UNKNOWN,
            None,
            (),
            "insufficient measurements (0 components); no score assigned",
        )
    score = sum(item.score for item in parts) / len(parts)
    state = _classify(score, by_name, len(parts))
    detail = "; ".join(f"{item.name}={item.score:+.2f} ({item.evidence})" for item in parts)
    evidence = (
        f"trend={state} score={score:+.2f} = mean of {len(parts)} components; {detail}"
    )
    return TrendAssessment(state, score, tuple(parts), evidence)


def _classify(score: float, parts: dict[str, float], count: int) -> str:
    structure = parts.get("price_structure")
    flow = parts.get("flow")
    velocity = parts.get("mcap_velocity")
    liquidity = parts.get("liquidity")
    if structure is not None and structure <= -0.80:
        return TrendState.BREAKDOWN
    if (
        liquidity is not None
        and liquidity <= -0.75
        and structure is not None
        and structure < 0
    ):
        return TrendState.BREAKDOWN
    if count < 2:
        return TrendState.UNKNOWN
    if flow is not None and flow <= -0.35 and (velocity is None or velocity < 0) and score < 0.15:
        return TrendState.DISTRIBUTING
    if (
        score >= 0.72
        and (velocity or 0.0) >= 0.40
        and (structure or 0.0) >= 0.40
        and (flow or 0.0) > 0
    ):
        return TrendState.ACCELERATING
    if score >= 0.45:
        return TrendState.STRONG
    if score >= 0.15:
        return TrendState.HEALTHY
    if score >= -0.15:
        return TrendState.WEAKENING
    return TrendState.DISTRIBUTING


def mcap_velocity(features: MarketFeatures) -> float | None:
    if features.mcap is None or features.prior_mcap is None or features.prior_mcap <= 0:
        return None
    return (features.mcap - features.prior_mcap) / features.prior_mcap
