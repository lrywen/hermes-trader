"""Signal entry points for the unified backtest kernel.

Two ways to feed :class:`~hermes_trader.backtest.types.Signal` sequences to
:func:`hermes_trader.backtest.driver.run`, matching the two research workflows
the project already uses:

* :func:`heuristic_signals` — re-runs the SAME pure production trigger library
  (:mod:`hermes_trader.indicators.triggers`) the live scanner uses, then
  substitutes the LLM verdict with a deterministic rule (the exact stand-in
  ``scripts/backtest.py`` uses). It measures the mechanical TA edge; it does
  NOT replay AI judgment.
* :func:`replay_signals` — replays the LONG/SHORT verdicts an agent actually
  logged (``.agent-memory.json`` analyses joined to their perception snapshot),
  aligned to bar indices by ``created_at``. Admission modes mirror
  ``scripts/backtest_logged.py`` (ai / lowconf / force / sidestep).

Both are pure: bars and parsed memory go in, signals come out. No network, no
LLM, no clock. File loading / candle fetching stays in the thin CLI layer.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from hermes_trader.indicators import math as ind
from hermes_trader.indicators import triggers as trig
from hermes_trader.models.types import Candle

from .types import Side, Signal

#: Default signal cadence: 5-minute bars (the live scanner interval).
BAR_MS_5M = 5 * 60_000

# ── heuristic mode ──────────────────────────────────────────────────────────

#: Verdict stand-in gates, verbatim from scripts/backtest.py.
HEURISTIC_MIN_SCORE = 25.0
HEURISTIC_MIN_ATR_PCT = 0.4

#: Minimum closed bars before the directional EMA/ATR/ADX read is trusted.
_MIN_TREND_BARS = 30


@dataclass(frozen=True)
class HeuristicConfig:
    """Knobs for the deterministic verdict stand-in.

    ``thresholds`` / ``weights`` default to the production trigger config so a
    heuristic run scores setups exactly like the live scanner; tests and
    parameter sweeps may pass a reduced config.
    """

    thresholds: dict[str, Any]
    weights: dict[str, float]
    warmup: int = 100
    min_score: float = HEURISTIC_MIN_SCORE
    min_atr_pct: float = HEURISTIC_MIN_ATR_PCT
    require_ta_confirm: bool = True


def default_heuristic_config(*, warmup: int = 100) -> HeuristicConfig:
    """Build a :class:`HeuristicConfig` from the live trigger config."""
    from hermes_trader.agents.config import get_config

    cfg = get_config()
    return HeuristicConfig(
        thresholds=dict(cfg["thresholds"]),
        weights=dict(cfg["weights"]),
        warmup=warmup,
    )


def evaluate_window(
    window: Sequence[Candle], th: dict[str, Any], weights: dict[str, float]
) -> tuple[float, list[dict[str, Any]]]:
    """Run the six live triggers + composite score on a trailing window.

    Mirrors ``scripts/backtest.py::_evaluate``.
    """
    hits = [
        trig.pct_move_spike(window, th["sigmaThreshold"]),
        trig.volume_spike(window, th["sigmaThreshold"]),
        trig.breakout(
            window, th["breakoutLookback"],
            min_rvol=th.get("breakoutMinRvol", 1.5),
            rvol_window=th.get("breakoutRvolWindow", 20),
            atr_score_mult=th.get("breakoutAtrScoreMult", 3.0),
        ),
        trig.range_compression(window, th["bbLength"], th["bbStdDev"]),
        trig.trend_strength(window, th["adxPeriod"]),
        trig.momentum_burst(window, th["momentumLookback"], th["momentumPct"]),
    ]
    return trig.composite_score(hits, weights), hits


def trend_and_atr_pct(
    window: Sequence[Candle],
) -> tuple[Optional[bool], Optional[float], Optional[float]]:
    """EMA8/21 direction, ATR% of price, ADX(14). ``None`` if data is thin.

    Mirrors ``scripts/backtest.py::_trend_and_atr_pct``.
    """
    closes = [c.c for c in window]
    if len(closes) < _MIN_TREND_BARS:
        return None, None, None
    e8 = ind.ema(closes, 8)[-1]
    e21 = ind.ema(closes, 21)[-1]
    if not (math.isfinite(e8) and math.isfinite(e21)):
        return None, None, None
    a = ind.atr(window, 14)[-1]
    if not math.isfinite(a) or closes[-1] == 0:
        return None, None, None
    atr_pct = a / closes[-1] * 100
    adx14 = ind.adx(window, 14)[-1]
    return e8 > e21, atr_pct, (adx14 if math.isfinite(adx14) else None)


def heuristic_verdict(
    score: float, hits: list[dict[str, Any]],
    bullish: Optional[bool], atr_pct: Optional[float],
    *, min_score: float = HEURISTIC_MIN_SCORE,
    min_atr_pct: float = HEURISTIC_MIN_ATR_PCT,
) -> Optional[Side]:
    """Deterministic LLM stand-in: score gate OR directional trend+ATR OR burst.

    Mirrors ``scripts/backtest.py::_heuristic_verdict`` (returns the kernel
    ``"long"``/``"short"`` side token instead of ``"LONG"``/``"SHORT"``).
    """
    if bullish is None:
        return None
    burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
    score_ok = score >= min_score
    trend_ok = atr_pct is not None and atr_pct >= min_atr_pct
    if not (score_ok or trend_ok or burst):
        return None
    return "long" if bullish else "short"


def ta_confirmed(
    bullish: Optional[bool], atr_pct: Optional[float],
    adx14: Optional[float], composite: float,
) -> bool:
    """Local proxy for ta_filter's CONFIRMED gate (score >= 45).

    Mirrors ``scripts/backtest.py::_ta_confirmed``.
    """
    if bullish is None or atr_pct is None:
        return False
    s = 20  # trend present
    if 30 < (atr_pct * 10) < 700:
        s += 15
    if atr_pct >= 0.5:
        s += 15
    if adx14 is not None and adx14 >= 25:
        s += 15
    s += min(15, composite / 100 * 15)
    return s >= 45


#: Optional decision-time context resolver: bar close ms -> (atr_pct, regime).
#: Used to attach the per-entry 4h ATR% / BTC-proxy regime the production DSL
#: tracker receives. ``None`` skips enrichment (constant/zero context).
ContextFn = Callable[[int], tuple[float, str]]


def heuristic_signals(
    bars: Sequence[Candle],
    config: Optional[HeuristicConfig] = None,
    *,
    context_fn: Optional[ContextFn] = None,
    bar_ms: int = BAR_MS_5M,
) -> list[Signal]:
    """Scan every closed bar PIT and emit entry signals at bar closes.

    A signal on bar ``i`` (decided from ``bars[:i+1]``) fills at the open of
    bar ``i+1`` — no signal is ever emitted on the last bar. ``context_fn``,
    when supplied, is called with the bar's CLOSE ms (``t + bar_ms``) to attach
    ``entry_atr_pct`` / ``entry_regime``. ``bar_ms`` only timestamps that
    context lookup; pass the real interval (15m/1h/4h/1d) for non-5m feeds.
    """
    cfg = config or default_heuristic_config()
    signals: list[Signal] = []
    n = len(bars)
    for i in range(cfg.warmup, n - 1):
        window = bars[: i + 1]
        score, hits = evaluate_window(window, cfg.thresholds, cfg.weights)
        bullish, atr_pct, adx14 = trend_and_atr_pct(window)
        side = heuristic_verdict(
            score, hits, bullish, atr_pct,
            min_score=cfg.min_score, min_atr_pct=cfg.min_atr_pct,
        )
        if side is None:
            continue
        burst = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
        if cfg.require_ta_confirm and not ta_confirmed(
            bullish, atr_pct, adx14, score
        ) and not burst:
            continue
        atr_pct_ctx, regime = (0.0, "")
        if context_fn is not None:
            atr_pct_ctx, regime = context_fn(bars[i].t + bar_ms)
        signals.append(Signal(i, side, atr_pct_ctx, regime))
    return signals


# ── replay mode ─────────────────────────────────────────────────────────────

#: Admission modes, verbatim from scripts/backtest_logged.py.
ReplayMode = str  # "ai" | "lowconf" | "force" | "sidestep"


@dataclass(frozen=True)
class ReplayConfig:
    """Admission knobs for a logged-verdict replay."""

    mode: ReplayMode = "ai"
    min_ai_conf: float = 0.6
    min_conf: float = 0.0
    force_bar: float = 45.0
    sidestep_min_slow_burn: int = 1
    long_only: bool = False
    dedup_ms: int = 0


def _burst_fired(analysis: dict[str, Any], triggers: list[dict[str, Any]]) -> bool:
    return (
        any(t.get("name") == "momentumBurst" and t.get("fired") for t in triggers)
        or bool(analysis.get("momentum_burst_fired", False))
    )


def _slow_count(analysis: dict[str, Any], triggers: list[dict[str, Any]]) -> int:
    count = sum(
        1 for t in triggers
        if t.get("name") in ("volumeBuildup1h", "trendFlip1h", "higherLows1h")
        and t.get("fired")
    )
    if count <= 0:
        count = int(analysis.get("slow_burn_count", 0) or 0)
    return count


def admit_analysis(
    analysis: dict[str, Any],
    perception: Optional[dict[str, Any]],
    cfg: ReplayConfig,
) -> Optional[tuple[Side, float, bool, bool]]:
    """Apply the mode-aware admission gate to one logged analysis.

    Returns ``(side, effective_conf, forced, sidestep_override)`` or ``None``
    when the record is skipped. Mirrors the admission block in
    ``scripts/backtest_logged.py``.
    """
    verdict = analysis.get("verdict")
    conf = float(analysis.get("confidence", 0) or 0)
    composite = float(
        (perception or {}).get("composite_score", analysis.get("composite_score", 0))
        or 0
    )
    triggers = (perception or {}).get("triggers", []) or []
    burst = _burst_fired(analysis, triggers)
    slow = _slow_count(analysis, triggers)
    ta_conf = (
        composite >= cfg.force_bar
        or burst
        or slow >= max(1, cfg.sidestep_min_slow_burn)
    )

    ai_ls = verdict in ("LONG", "SHORT")
    side_of = lambda: ("long" if verdict == "LONG" else "short")
    admit = False
    side: Optional[Side] = None
    forced = False
    sidestep = False

    if cfg.mode == "ai":
        admit = ai_ls and conf >= cfg.min_ai_conf
        side = side_of() if ai_ls else None
    elif cfg.mode == "lowconf":
        admit = ai_ls and conf >= cfg.min_conf
        side = side_of() if ai_ls else None
    elif cfg.mode == "force":
        if ai_ls and conf >= cfg.min_ai_conf:
            admit, side = True, side_of()
        elif composite >= cfg.force_bar:
            admit, side, forced = True, "long", True
            conf = max(conf, cfg.min_ai_conf)
    elif cfg.mode == "sidestep":
        if ta_conf:
            admit, side, forced, sidestep = True, "long", (not ai_ls), True
            conf = max(conf, cfg.min_ai_conf)
        elif ai_ls and conf >= cfg.min_ai_conf:
            admit, side = True, side_of()
    else:
        raise ValueError(f"unknown replay mode: {cfg.mode!r}")

    if not admit or side is None:
        return None
    if cfg.long_only and side == "short":
        return None
    return side, conf, forced, sidestep


def replay_signals(
    bars: Sequence[Candle],
    analyses: Sequence[dict[str, Any]],
    perceptions_by_id: Optional[dict[str, dict[str, Any]]] = None,
    cfg: Optional[ReplayConfig] = None,
    *,
    coin: Optional[str] = None,
    context_fn: Optional[ContextFn] = None,
    bar_ms: int = BAR_MS_5M,
) -> list[Signal]:
    """Align logged verdicts onto ``bars`` for one coin.

    Each admitted analysis is mapped to the last bar CLOSED at/ before its
    ``created_at``; the signal then fills at the NEXT bar's open (the first bar
    whose open is at/after ``created_at`` — the same alignment
    ``backtest_logged.py`` uses). Same-coin dedup is by decision ms. Records
    with no forward bar, no matching coin, or a rejected admission are dropped.

    ``analyses`` is expected in ascending ``created_at`` order (as loaded by
    the CLI): when two records map onto the same decision bar the earliest one
    wins.
    """
    cfg = cfg or ReplayConfig()
    perceptions_by_id = perceptions_by_id or {}
    if not bars:
        return []
    open_times = [b.t for b in bars]

    # (decision_ms, side, signal_bar); one entry per decision bar survives.
    selected: list[tuple[int, Side, int]] = []
    seen_bars: set[int] = set()
    last_decision_ms: Optional[int] = None
    for a in analyses:
        a_coin = a.get("coin")
        if coin is not None and a_coin != coin:
            continue
        ts = int(a.get("created_at", 0) or 0)
        if ts == 0:
            continue
        perc = perceptions_by_id.get(a.get("perception_id"))
        decision = admit_analysis(a, perc, cfg)
        if decision is None:
            continue
        side, _conf, _forced, _sidestep = decision
        if cfg.dedup_ms > 0 and last_decision_ms is not None:
            if ts - last_decision_ms < cfg.dedup_ms:
                continue
        # First bar whose OPEN is at/after the decision: that is the fill bar.
        # The signal is decided at the previous bar's close.
        fill_bar = _first_ge(open_times, ts)
        if fill_bar is None or fill_bar >= len(bars):
            continue
        signal_bar = fill_bar - 1
        if signal_bar < 0:
            continue
        # At most one decision per closed bar: two verdicts inside one 5m bar
        # cannot both act on the same next open. Earliest verdict wins.
        if signal_bar in seen_bars:
            continue
        seen_bars.add(signal_bar)
        selected.append((ts, side, signal_bar))
        last_decision_ms = ts

    signals: list[Signal] = []
    for ts, side, signal_bar in selected:
        atr_pct_ctx, regime = (0.0, "")
        if context_fn is not None:
            # Decision instant ~ signal bar close.
            atr_pct_ctx, regime = context_fn(bars[signal_bar].t + bar_ms)
        signals.append(Signal(signal_bar, side, atr_pct_ctx, regime))
    # Stable chronological order (same-bar duplicates already collapsed above).
    signals.sort(key=lambda s: s.bar_index)
    return signals


def _first_ge(sorted_vals: Sequence[int], target: int) -> Optional[int]:
    """Index of the first value >= target (bisect_left), or None."""
    lo, hi = 0, len(sorted_vals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(sorted_vals) else None
