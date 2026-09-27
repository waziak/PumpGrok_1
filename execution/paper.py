"""Paper executor on the shared stop state machine.

No network. Emergency exits record a relaxed minimum-out flag, the way a
live adapter may loosen minSolOut, and still reconcile the actual fill.
"""

from __future__ import annotations

from .engine import LiveTradingDisabled, StopEngine
from .models import SEVERE_IMPACT_PCT, Decision, FillReceipt, StopReason, StopState, qprice


class BlockhashSource:
    def __init__(self) -> None:
        self.issued: list[str] = []

    def __call__(self) -> str:
        value = f"blockhash-{len(self.issued) + 1}"
        self.issued.append(value)
        return value


class PaperBroker:
    def __init__(
        self,
        engine: StopEngine,
        *,
        blockhashes: BlockhashSource | None = None,
        fail_remaining: int = 0,
        slippage_pct: float = 0.0,
        trace: list[str] | None = None,
    ) -> None:
        self.engine = engine
        self.blockhashes = blockhashes or BlockhashSource()
        self.fail_remaining = fail_remaining
        self.slippage_pct = slippage_pct
        self.trace = trace if trace is not None else []
        self.sold_tokens: list[float] = []
        self.last_receipt: FillReceipt | None = None
        self.live_network = False

    def _guard_mode(self) -> None:
        config = self.engine.config
        if config.trading_mode != "paper" or config.real_trades or config.live_network_sends:
            raise LiveTradingDisabled("REAL TRADES=NO; refusing to send")

    def handle(self, decision: Decision, *, event_key: str | None = None) -> str:
        pos = self.engine.get(decision.position_id)
        if pos.stop_state == StopState.STOP_SUBMITTED:
            return "awaiting_receipt"
        if pos.stop_state == StopState.STOP_PENDING:
            return self.retry(decision)
        return self.execute(decision, event_key=event_key)

    def execute(self, decision: Decision, *, event_key: str | None = None, slippage_pct: float | None = None) -> str:
        self._guard_mode()
        pos = self.engine.get(decision.position_id)
        key = event_key or (
            f"{decision.position_id}|{decision.reason}|{decision.stop_trigger_price:.8f}"
        )
        if not self.engine.register_trigger_event(pos.position_id, key):
            return "duplicate"
        if pos.stop_state == StopState.CLOSED:
            return "closed"
        if pos.stop_state == StopState.STOP_SUBMITTED:
            return "duplicate_inflight"
        self._log_impact(decision)
        blocked = self._unexecutable(decision)
        if blocked:
            return blocked
        self.trace.append("exit")
        if pos.stop_state == StopState.OPEN:
            fresh = self.engine.get(pos.position_id)
            if fresh.stop_state == StopState.OPEN:
                raise RuntimeError("stop was not triggered before submit")
        blockhash = self.blockhashes()
        self.engine.mark_submitted(pos.position_id, blockhash)
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            self.engine.mark_submit_failed(pos.position_id, "simulated_tx_failure")
            return "failed"
        return self._fill(decision, slippage_pct)

    def retry(self, decision: Decision, *, slippage_pct: float | None = None) -> str:
        """New attempt after STOP_PENDING. Uses a new blockhash and the same balance cap."""
        self._guard_mode()
        pos = self.engine.get(decision.position_id)
        if pos.stop_state != StopState.STOP_PENDING:
            return f"not_pending:{pos.stop_state}"
        self._log_impact(decision)
        blocked = self._unexecutable(decision)
        if blocked:
            return blocked
        self.trace.append("exit")
        blockhash = self.blockhashes()
        self.engine.mark_submitted(pos.position_id, blockhash)
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            self.engine.mark_submit_failed(pos.position_id, "simulated_tx_failure")
            return "failed"
        return self._fill(decision, slippage_pct)

    def _log_impact(self, decision: Decision) -> None:
        if decision.price_impact_pct < SEVERE_IMPACT_PCT:
            return
        self.engine.log_event(
            decision.position_id,
            StopReason.SEVERE_IMPACT,
            {"price_impact_pct": decision.price_impact_pct},
        )

    def _unexecutable(self, decision: Decision) -> str | None:
        if not decision.pool_exists:
            self.engine.mark_unexecutable(decision.position_id, StopReason.POOL_GONE)
            return StopReason.POOL_GONE
        if not decision.route_available:
            self.engine.mark_unexecutable(decision.position_id, StopReason.NO_ROUTE)
            return StopReason.NO_ROUTE
        if decision.unsellable:
            self.engine.mark_unexecutable(decision.position_id, StopReason.UNSELLABLE)
            return StopReason.UNSELLABLE
        return None

    def _fill(self, decision: Decision, slippage_pct: float | None) -> str:
        if decision.quote_price is None:
            raise RuntimeError("stop fill requires a quote")
        slip = self.slippage_pct if slippage_pct is None else slippage_pct
        quote = decision.quote_price
        actual = qprice(quote * (1.0 - slip / 100.0))
        relaxed = decision.priority == "EMERGENCY_EXIT"
        receipt = self.engine.mark_filled(
            decision.position_id,
            quote=quote,
            actual_fill=actual,
            relaxed_min_out=relaxed,
        )
        self.last_receipt = receipt
        self.sold_tokens.append(receipt.tokens_sold)
        return "filled"
