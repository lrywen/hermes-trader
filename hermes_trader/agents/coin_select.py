"""Continuous coin-selection scoring (strategy-paradigm P1-2, Forager-inspired).

Hermes currently partitions the scanner budget into hard buckets — top-N by
24h notional volume plus top-M by |24h change|, with an optional rotating
sweep — rather than ranking every eligible market on one continuous quality
score. Passivbot's *Forager* instead ranks symbols on three factors:

  1. short-timeframe log-range volatility (moves worth trading)
  2. turnover / volume (liquidity so the move is capturable)
  3. EMA readiness (price aligned to act on)

The selection happens *before* candles are fetched, so the short-timeframe
volatility and EMA factors are not available to every symbol cheaply. This
module therefore provides two layers:

  * ``pre_candle_score`` — a continuous score over the fields ALREADY present
    in the ``metaAndAssetCtxs`` universe snapshot (24h turnover, |24h change|,
    funding/OI structure). This is the zero-extra-fetch Forager-style ranking
    that replaces the hard buckets in shadow/enforce comparison.
  * ``post_candle_readiness`` — an optional short-timeframe factor computed
    only for markets whose candles were already fetched (log-range volatility
    + EMA readiness), folded in without any additional data request.

Everything is pure: no network, no clock reads, no input mutation. Weights are
configurable via the ``coin_selection`` block.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


# ---- pre-candle normalisers ------------------------------------------------

def turnover_term(m: Mapping[str, Any]) -> float:
    """0–1 log-scaled 24h turnover (higher = more liquid)."""
    v = max(0.0, _f(m.get("dayNtlVlm")))
    if v <= 0:
        return 0.0
    # log10: $1e5 -> 0, $1e9 -> 1 (covers the realistic perp range).
    return max(0.0, min(1.0, (math.log10(v) - 5.0) / 4.0))


def momentum_term(m: Mapping[str, Any], cur_px: float | None = None) -> float:
    """0–1 magnitude of the 24h move (direction-agnostic).

    Uses an explicit current price when given (this cycle's fresh mid), else
    the universe ``markPx``; compares to ``prevDayPx``.
    """
    prev = _f(m.get("prevDayPx"))
    cur = _f(cur_px) if cur_px is not None else _f(m.get("markPx"))
    if prev <= 0 or cur <= 0:
        return 0.0
    pct = abs((cur - prev) / prev)
    # 2% -> ~0.2, 10% -> 1.0; a big move is what a momentum scanner wants.
    return max(0.0, min(1.0, pct / 0.10))


def funding_oi_term(m: Mapping[str, Any]) -> float:
    """0–1 mild contrarian/structure read from funding and open interest.

    Extreme funding is crowded (risk of a squeeze unwind); moderate funding is
    healthy. Open interest presence confirms a real tradeable market. This is a
    small tie-breaker, never the dominant factor.
    """
    oi = max(0.0, _f(m.get("openInterest") or m.get("oi")))
    fund = _f(m.get("funding"))
    oi_ok = 1.0 if oi > 0 else 0.0
    # Crowding penalty rises with |funding|; 0.1% funding -> fully crowded.
    crowd = min(1.0, abs(fund) / 0.001)
    return oi_ok * (1.0 - 0.5 * crowd)


DEFAULT_PRE_WEIGHTS: Mapping[str, float] = {
    "turnover": 0.5,
    "momentum": 0.35,
    "funding_oi": 0.15,
}


def pre_candle_score(
    m: Mapping[str, Any],
    *,
    cur_px: float | None = None,
    weights: Mapping[str, float] | None = None,
) -> float:
    """0–100 continuous pre-candle selection score for one market. Pure."""
    w = _normalise(weights or DEFAULT_PRE_WEIGHTS,
                   tuple(DEFAULT_PRE_WEIGHTS.keys()))
    val = (
        w["turnover"] * turnover_term(m)
        + w["momentum"] * momentum_term(m, cur_px)
        + w["funding_oi"] * funding_oi_term(m)
    )
    return 100.0 * max(0.0, min(1.0, val))


def _normalise(weights: Mapping[str, float], keys: tuple[str, ...]
               ) -> dict[str, float]:
    w = {k: max(0.0, _f(weights.get(k))) for k in keys}
    total = sum(w.values())
    if total <= 0:
        return {k: float(DEFAULT_PRE_WEIGHTS[k]) for k in keys}
    return {k: v / total for k, v in w.items()}


# ---- post-candle readiness (Forager factors 1 & 3) ------------------------

def log_range_volatility(closes: Sequence[float]) -> float:
    """0–1 short-timeframe log-range volatility from fetched closes.

    Uses the std-dev of log returns (a scale-free volatility estimate); mapped
    so ~1% bar-to-bar log vol -> 1.0. Pure and defensive: needs >=2 points.
    """
    px = [_f(x) for x in closes]
    px = [x for x in px if x > 0]
    if len(px) < 2:
        return 0.0
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    sd = math.sqrt(var)
    return max(0.0, min(1.0, sd / 0.01))


def ema_readiness(closes: Sequence[float], period: int = 20) -> float:
    """0–1 alignment of price to a short EMA (Forager "EMA span ready").

    Returns 1 when price is at/just above a rising EMA (long-ready in Hermes'
    long-biased book), decaying below it or when the EMA is falling.
    """
    px = [_f(x) for x in closes]
    px = [x for x in px if x > 0]
    if len(px) < 2:
        return 0.0
    p = max(2, min(period, len(px)))
    alpha = 2.0 / (p + 1.0)
    ema = px[0]
    for x in px[1:]:
        ema = alpha * x + (1 - alpha) * ema
    last = px[-1]
    rising = ema > px[max(0, len(px) - p)]  # EMA higher than p bars ago
    rel = (last - ema) / ema
    if rel >= 0:
        # At/above EMA up to +3% -> fully ready (price aligned, not overextended).
        # Beyond +3% an overextended spike starts fading back toward 0 by +6%.
        if rel <= 0.03:
            base = 1.0
        else:
            base = max(0.0, 1.0 - (rel - 0.03) / 0.03)
    else:
        # below EMA: readiness falls with the gap (-3% -> 0).
        base = max(0.0, 1.0 + rel / 0.03)
    return max(0.0, min(1.0, base * (1.0 if rising else 0.4)))


def post_candle_readiness(closes: Sequence[float]) -> float:
    """0–1 blended short-timeframe readiness (volatility + EMA). Pure."""
    return 0.5 * log_range_volatility(closes) + 0.5 * ema_readiness(closes)


# ---- ranking ---------------------------------------------------------------

def rank_pool(
    pool: Sequence[Mapping[str, Any]],
    *,
    cur_prices: Mapping[str, float] | None = None,
    weights: Mapping[str, float] | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """Rank markets best-first on the pre-candle score. Deterministic."""
    out: list[tuple[dict[str, Any], float]] = []
    for m in pool:
        cp = cur_prices.get(m.get("coin")) if cur_prices is not None else None
        out.append((dict(m), pre_candle_score(m, cur_px=cp, weights=weights)))
    out.sort(key=lambda item: (-item[1], str(item[0].get("coin", ""))))
    return out


def top_k(pool: Sequence[Mapping[str, Any]], k: int,
          *, cur_prices: Mapping[str, float] | None = None,
          weights: Mapping[str, float] | None = None) -> list[dict[str, Any]]:
    """Return the k highest-scoring markets (k>=len -> all; k<=0 -> none)."""
    ranked = rank_pool(pool, cur_prices=cur_prices, weights=weights)
    if k <= 0:
        return []
    return [m for m, _ in ranked[:k]]


def xs_momentum_rank(
    pool: Sequence[Mapping[str, Any]],
    *,
    cur_prices: Mapping[str, float] | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """Cross-sectional DIRECTIONAL (signed) 24h momentum, strongest-first.

    Unlike rank_pool (direction-agnostic activity), this keeps the SIGN of the
    move so Top-N selection concentrates in actual up-leaders. Uses the cycle
    mid when present, else universe markPx vs prevDayPx. Invalid markets drop.
    """
    out: list[tuple[dict[str, Any], float]] = []
    for m in pool:
        coin = m.get("coin")
        cp = cur_prices.get(coin) if cur_prices is not None else None
        prev = _f(m.get("prevDayPx"))
        cur = _f(cp) if cp is not None else _f(m.get("markPx"))
        if prev <= 0.0 or cur <= 0.0:
            continue
        out.append((dict(m), (cur - prev) / prev))
    out.sort(key=lambda item: (-item[1], str(item[0].get("coin", ""))))
    return out


def xs_top_n_long(
    pool: Sequence[Mapping[str, Any]],
    n: int,
    *,
    cur_prices: Mapping[str, float] | None = None,
    require_positive: bool = True,
) -> list[dict[str, Any]]:
    """Top-N strongest-UP markets by signed cross-sectional momentum."""
    ranked = xs_momentum_rank(pool, cur_prices=cur_prices)
    if require_positive:
        ranked = [x for x in ranked if x[1] > 0.0]
    if n <= 0:
        return []
    return [m for m, _ in ranked[:n]]
