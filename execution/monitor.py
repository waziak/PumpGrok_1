"""Market marks to exit decisions. No model calls."""

from __future__ import annotations

from .engine import StopEngine
from .models import PRIORITY_RANK, Decision, ExitPriority, MarketView


class StopMonitor:
    """Evaluate stops from marks. Independent of Grok and of the scanner."""

    def __init__(self, engine: StopEngine) -> None:
        self.engine = engine

    def run_once(self, views: list[MarketView], now: float | None = None) -> list[Decision]:
        moment = self.engine.clock() if now is None else now
        decisions: list[Decision] = []
        for view in views:
            # view.advisor and view.scanner_up are intentionally unused.
            # A model outage or a scanner outage must not block this loop.
            decisions.append(self.engine.on_mark(view, moment))
        decisions.sort(key=lambda item: PRIORITY_RANK.get(item.priority, PRIORITY_RANK[ExitPriority.HOLD]))
        return decisions
