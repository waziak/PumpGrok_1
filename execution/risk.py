"""Execution risk gate.

Entries are rejected without an explicit stop. Exit work is drained before
any new buy. The factory never returns a live network sender.
"""

from __future__ import annotations

from .engine import (
    EntryDeferred,
    ExitPriorityError,
    LiveTradingDisabled,
    StopEngine,
    StopProtectionError,
    TradeRejected,
)
from .live import LiveSender
from .models import OpenRequest
from .monitor import StopMonitor
from .paper import PaperBroker


def build_executor(engine: StopEngine) -> PaperBroker:
    """Paper broker only. Live mode is the same state machine with sends disabled."""
    if engine.config.trading_mode != "paper":
        raise LiveTradingDisabled("REAL TRADES=NO; live network sends are disabled")
    if engine.config.real_trades or engine.config.live_network_sends:
        raise LiveTradingDisabled("REAL TRADES=NO; live network sends are disabled")
    return PaperBroker(engine)


class ExecutionCycle:
    """One pass: marks, stop exits, then entries that are still allowed."""

    def __init__(self, engine: StopEngine, monitor: StopMonitor, broker: PaperBroker) -> None:
        self.engine = engine
        self.monitor = monitor
        self.broker = broker

    def run(
        self,
        views: list,
        entry_requests: list[OpenRequest] | tuple[OpenRequest, ...] = (),
        now: float | None = None,
    ) -> tuple[list, list]:
        decisions = self.monitor.run_once(list(views), now=now)
        acted = []
        for decision in decisions:
            pos = self.engine.get(decision.position_id)
            actionable = decision.should_exit and pos.stop_state in (
                "OPEN",
                "STOP_TRIGGERED",
                "STOP_PENDING",
            )
            if not actionable:
                continue
            self.broker.handle(decision)
            acted.append(decision)
        if self.engine.entries_blocked():
            return acted, []
        opened = []
        for request in entry_requests:
            self.broker.trace.append("entry")
            opened.append(self.engine.request_entry(request))
        return acted, opened


__all__ = [
    "EntryDeferred",
    "ExecutionCycle",
    "ExitPriorityError",
    "LiveSender",
    "LiveTradingDisabled",
    "PaperBroker",
    "StopEngine",
    "StopMonitor",
    "StopProtectionError",
    "TradeRejected",
    "build_executor",
]
