"""Cost model — the single H-7 cost/fill contract for every backtest path.

Unifies the three legacy research scripts (backtest.py, backtest_logged.py,
backtest_majors_surge.py), which each reimplemented fees/slippage slightly
differently. Canonical rules:

* Round-trip fee charged ONCE as a fraction of notional (default 5 bps).
* Entry fill: raw price adjusted adversely by ``entry_slip_bps`` (default 5).
* Exit fill: adjusted adversely by ``exit_slip_bps`` (default 15), PLUS
  ``stop_delay_slip_bps`` (default 10) ONLY for hard max-loss exits.
* Favorable = +sgn: longs buy higher / sell lower on exits (cover is a buy).

Prices are raw fill REFERENCES in and FILLED prices out, so both can be
recorded on the :class:`~hermes_trader.backtest.types.Trade`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .types import ExitReason

# Canonical defaults — copied verbatim from scripts/backtest.py L87/L99-105.
DEFAULT_ROUND_TRIP_FEE_BPS = 5.0
DEFAULT_ENTRY_SLIP_BPS = 5.0
DEFAULT_EXIT_SLIP_BPS = 15.0
DEFAULT_STOP_DELAY_SLIP_BPS = 10.0


@dataclass(frozen=True)
class CostModel:
    """Fees + adverse slippage in basis points. ``bps <= 0`` disables a leg."""

    round_trip_fee_bps: float = DEFAULT_ROUND_TRIP_FEE_BPS
    entry_slip_bps: float = DEFAULT_ENTRY_SLIP_BPS
    exit_slip_bps: float = DEFAULT_EXIT_SLIP_BPS
    stop_delay_slip_bps: float = DEFAULT_STOP_DELAY_SLIP_BPS

    @staticmethod
    def _adjust(px: float, is_buy: bool, bps: float) -> float:
        """Adverse fill: buys pay more, sells receive less."""
        if bps <= 0:
            return px
        delta = px * bps / 1e4
        return px + delta if is_buy else px - delta

    def fill_entry(self, raw_px: float, side: str) -> float:
        return self._adjust(raw_px, is_buy=(side == "long"),
                            bps=self.entry_slip_bps)

    def fill_exit(self, raw_px: float, side: str, reason: ExitReason) -> float:
        bps = self.exit_slip_bps
        if reason is ExitReason.MAX_LOSS:
            bps += self.stop_delay_slip_bps
        # Closing a short is a buy; closing a long is a sell.
        return self._adjust(raw_px, is_buy=(side == "short"), bps=bps)

    def fee_usd(self, notional_usd: float) -> float:
        return notional_usd * self.round_trip_fee_bps / 1e4

    def pnl_usd(self, side: str, entry_fill_px: float, exit_fill_px: float,
                notional_usd: float) -> tuple[float, float]:
        """Return ``(pnl_gross, pnl_net)`` in USD; fee charged once per round trip."""
        sgn = 1.0 if side == "long" else -1.0
        gross = sgn * (exit_fill_px - entry_fill_px) / entry_fill_px * notional_usd
        return gross, gross - self.fee_usd(notional_usd)
