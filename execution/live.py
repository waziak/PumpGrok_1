"""Live sender stub. Network sends stay off.

The stop state machine does not live here. This type exists so a future
adapter can sit behind the same decisions without being selectable today.
"""

from __future__ import annotations

from .engine import LiveTradingDisabled


class LiveSender:
    """Refuses every send. Does not read keys and does not open a socket."""

    live_network = False
    real_trades = False

    def send(self, intent: object, **_ignored: object) -> None:
        raise LiveTradingDisabled("REAL TRADES=NO; live network sends are disabled")
