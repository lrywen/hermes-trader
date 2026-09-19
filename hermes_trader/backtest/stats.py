"""Trade statistics and walk-forward (in-sample / out-of-sample) analysis.

Port of the metrics ``scripts/backtest.py::_split_metrics`` computed on the
unified kernel :class:`~hermes_trader.backtest.types.Trade`, with one
improvement: the Sharpe annualization span uses the trades' real entry/exit
timestamps instead of a mean-bar-count proxy.

Classification rule (O-7): a trade belongs to the segment its ENTRY bar lies
in — a position entered before the split that exits after it is still
in-sample, because the entry decision used only in-sample information.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .types import ExitReason, Trade

_MS_PER_DAY = 86_400_000.0


@dataclass(frozen=True)
class Stats:
    """Aggregate performance of one trade segment."""

    n: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    pnl_net_usd: float = 0.0
    expectancy_usd: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    max_dd_usd: float = 0.0
    sharpe: float = 0.0
    pnl_pct_equity: float | None = None
    by_reason: dict[str, int] = field(default_factory=dict)


def trade_stats(trades: Sequence[Trade], *, equity: float | None = None) -> Stats:
    """Compute aggregate stats over ``trades`` (any chronology is accepted).

    The cumulative-PnL path for max drawdown orders trades by
    ``(exit_time_ms, coin)`` — a close approximation of the realized equity
    curve under the kernel's constant-equity, one-position-per-coin model.
    """
    n = len(trades)
    if n == 0:
        return Stats()

    pnls = [t.pnl_net_usd for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    total = sum(pnls)
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]

    mean = total / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    sharpe = 0.0
    if sd > 0:
        first_entry = min(t.entry_time_ms for t in trades)
        last_exit = max(t.exit_time_ms for t in trades)
        span_days = max(1.0, (last_exit - first_entry) / _MS_PER_DAY)
        sharpe = mean / sd * math.sqrt(365.0 / span_days)

    ordered = sorted(trades, key=lambda t: (t.exit_time_ms, t.coin))
    peak = cum = mdd = 0.0
    for t in ordered:
        cum += t.pnl_net_usd
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)

    by_reason: dict[str, int] = {}
    for t in trades:
        key = t.reason.value if isinstance(t.reason, ExitReason) else str(t.reason)
        by_reason[key] = by_reason.get(key, 0) + 1

    return Stats(
        n=n, wins=wins, losses=losses,
        win_rate_pct=wins / n * 100.0,
        pnl_net_usd=total, expectancy_usd=mean,
        avg_win_usd=(sum(win_pnls) / len(win_pnls) if win_pnls else 0.0),
        avg_loss_usd=(sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0),
        max_dd_usd=mdd, sharpe=sharpe,
        pnl_pct_equity=(total / equity * 100.0 if equity else None),
        by_reason=by_reason,
    )


def oos_split_index(n_bars: int, warmup: int, oos_frac: float) -> int:
    """Bar index at which the out-of-sample window starts.

    Only bars in ``[warmup, n_bars)`` can generate entries, so the split is
    placed ``oos_frac`` of the way through THAT tradeable window. Trades with
    ``entry_bar >=`` the returned index are out-of-sample. The fixed warmup
    prefix is always in-sample (indicators on OOS bars read only past bars).
    """
    if not 0.0 <= oos_frac < 1.0:
        raise ValueError("oos_frac must be in [0, 1)")
    if n_bars <= warmup:
        return n_bars
    return int(warmup + (n_bars - warmup) * (1.0 - oos_frac))


def split_trades(
    trades: Sequence[Trade], split_bar: int
) -> tuple[list[Trade], list[Trade]]:
    """Partition trades by ENTRY bar against ``split_bar`` (O-7 rule)."""
    in_sample = [t for t in trades if t.entry_bar < split_bar]
    out_of_sample = [t for t in trades if t.entry_bar >= split_bar]
    return in_sample, out_of_sample


@dataclass(frozen=True)
class WalkForward:
    """Paired IS/OOS stats for one run."""

    split_bar: int
    in_sample: Stats
    out_of_sample: Stats

    @property
    def edge_held_oos(self) -> bool:
        """True only when the OOS expectancy stays positive."""
        return self.out_of_sample.n > 0 and self.out_of_sample.expectancy_usd > 0


def walk_forward(
    trades: Sequence[Trade], split_bar: int, *, equity: float | None = None
) -> WalkForward:
    """Split trades by entry bar and score both segments."""
    is_tr, oos_tr = split_trades(trades, split_bar)
    return WalkForward(
        split_bar=split_bar,
        in_sample=trade_stats(is_tr, equity=equity),
        out_of_sample=trade_stats(oos_tr, equity=equity),
    )
