"""Core types for the unified backtest kernel.

The kernel drives the SAME production exit engine
(:class:`hermes_trader.agents.dsl_exit.DSLTracker`) that live trading uses,
wrapped by a point-in-time (PIT) bar adapter in :mod:`hermes_trader.backtest.exit_dsl`.
These types are the narrow contract between the driver, the cost model and the
exit adapter — no pandas, no file format assumptions. Bars themselves stay as
:class:`hermes_trader.models.types.Candle`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

Side = Literal["long", "short"]


class ExitReason(str, Enum):
    """Normalized exit reason.

    Production verdict strings carry human-readable suffixes
    (e.g. ``"max_loss (2.50% spot ...)"``) and the legacy research scripts use
    several aliases (``trailing_stop`` / ``end_of_window``); everything collapses
    onto this stable token so analytics don't parse prose.
    """

    MAX_LOSS = "max_loss"
    FLOOR_BREACH = "floor_breach"
    HARD_TIMEOUT = "hard_timeout"
    STALE_FLAT_TIMEOUT = "stale_flat_timeout"
    TIME_SCRATCH = "time_scratch"
    END_OF_DATA = "end_of_data"


_PREFIX_MAP = (
    ("max_loss", ExitReason.MAX_LOSS),
    ("stale_flat_timeout", ExitReason.STALE_FLAT_TIMEOUT),
    ("time_scratch", ExitReason.TIME_SCRATCH),
    ("hard_timeout", ExitReason.HARD_TIMEOUT),
    # Legacy script alias for a phase-2 trailing exit.
    ("trailing_stop", ExitReason.FLOOR_BREACH),
    ("floor_breach", ExitReason.FLOOR_BREACH),
    # Legacy backtest_logged sentinels.
    ("end_of_window", ExitReason.END_OF_DATA),
    ("end_of_data", ExitReason.END_OF_DATA),
    ("no_data", ExitReason.END_OF_DATA),
)


def normalize_reason(raw: str) -> ExitReason:
    """Map a production/script reason string onto the stable ``ExitReason``."""
    for prefix, reason in _PREFIX_MAP:
        if raw.startswith(prefix):
            return reason
    raise ValueError(f"unknown exit reason: {raw!r}")


@dataclass(frozen=True)
class Signal:
    """An entry decision made at the CLOSE of ``bar_index``.

    PIT contract: the order fills at the OPEN of ``bar_index + 1`` — a strategy
    can only act on information available once bar ``bar_index`` has closed.

    ``entry_atr_pct`` / ``entry_regime`` carry the per-signal context the
    production DSL tracker receives at registration (4h ATR% and the BTC-proxy
    regime at the decision instant). When left at their defaults the driver's
    run-level values apply — heuristic runs use one constant context, replays
    attach the real per-entry values logged at decision time.
    """

    bar_index: int
    side: Side
    entry_atr_pct: float = 0.0
    entry_regime: str = ""


@dataclass(frozen=True)
class ExitEvent:
    """Raw exit decision emitted by the bar adapter (pre exit-slippage).

    ``ref_px`` is the economic fill reference: the stop floor for stop/floor
    exits (gap-filled against the bar open by the adapter), or the bar close
    for timeout exits. The cost model applies slippage on top.
    """

    bar_index: int
    reason: ExitReason
    ref_px: float


@dataclass(frozen=True)
class Trade:
    """One completed round trip with both raw references and filled prices."""

    coin: str
    side: Side
    entry_bar: int
    exit_bar: int
    entry_time_ms: int
    exit_time_ms: int
    entry_ref_px: float
    entry_fill_px: float
    exit_ref_px: float
    exit_fill_px: float
    reason: ExitReason
    notional_usd: float
    fee_usd: float
    pnl_gross_usd: float
    pnl_net_usd: float
    #: RFT-01：入场时判定的 regime 与出场选档标签（trend_ride/scalp）。
    entry_regime: str = ""
    exit_label: str = ""

    @property
    def hold_bars(self) -> int:
        return self.exit_bar - self.entry_bar
