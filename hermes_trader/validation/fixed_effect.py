"""Stage 0 — fixed-effect IC decomposition (research validation framework).

The full-review plan (2026-10-25, P0 / §5.2 iron-law #1) requires that every
candidate feature pass a coin fixed-effect decomposition before it is treated
as a real signal:

* ``IC_all``    — pooled rank-IC over every (coin, day) row. Easy to inflate:
                  a feature that merely encodes "large/liquid coin vs small
                  coin" looks predictive when big coins happen to outperform
                  over the window (the ``total_depth_log`` failure mode:
                  IC_all +0.20 but within-coin IC ~ -0.02).
* ``IC_within`` — rank-IC after subtracting each coin's own mean from BOTH the
                  feature and the forward return. Cross-sectional scale is
                  removed; only "feature high for THIS coin vs its own history
                  -> this coin outperforms later" remains.

Rejection rule (frozen in the plan): ``IC_within / IC_all < 0.3`` (or a sign
flip) classifies the feature as a scale/coin-membership factor and rejects it,
regardless of how strong IC_all is.

Pure standard library. Inputs are plain long-form rows so this stays agnostic
to the data source (book-raw replay, candle cache, shadow book).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def _ranks(values: list[float]) -> list[float]:
    """Average ranks (1-based) with tie handling; NaN gets rank NaN."""
    n = len(values)
    order = sorted(range(n), key=lambda i: (math.isnan(values[i]), values[i]))
    ranks = [float("nan")] * n
    i = 0
    while i < n:
        if math.isnan(values[order[i]]):
            break  # NaNs sort last; leave their ranks NaN
        j = i
        while j + 1 < n and not math.isnan(values[order[j + 1]]) and \
                values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    """Pearson correlation over finite paired values; NaN if undefined."""
    pairs = [(a, b) for a, b in zip(x, y)
             if math.isfinite(a) and math.isfinite(b)]
    n = len(pairs)
    if n < 3:
        return float("nan")
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    sxx = syy = sxy = 0.0
    for a, b in pairs:
        dx, dy = a - mx, b - my
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def spearman_ic(feature: list[float], forward_ret: list[float]) -> float:
    """Rank (Spearman) information coefficient over paired rows."""
    return _pearson(_ranks(feature), _ranks(forward_ret))


@dataclass
class FixedEffectResult:
    n: int
    n_coins: int
    ic_all: float
    ic_within: float
    ratio: float          # ic_within / ic_all when defined
    sign_flip: bool       # ic_all and ic_within have opposite signs
    is_scale_factor: bool # plan iron-law #1 rejection verdict
    reason: str


def fixed_effect_ic(
    rows: list[dict],
    feature_key: str,
    ret_key: str,
    coin_key: str = "coin",
    ratio_threshold: float = 0.3,
) -> FixedEffectResult:
    """Decompose pooled IC into coin fixed-effect and within-coin components.

    Each row: ``{coin_key, feature_key, ret_key}`` (one observation per
    coin-time). ``ic_within`` is computed by residualising both the feature and
    the forward return against coin means before ranking.
    """
    feats = [float(r[feature_key]) for r in rows]
    rets = [float(r[ret_key]) for r in rows]
    coins = [r[coin_key] for r in rows]
    finite = [i for i in range(len(rows))
              if math.isfinite(feats[i]) and math.isfinite(rets[i])]
    if len(finite) < 3:
        return FixedEffectResult(len(finite), len(set(coins)), float("nan"),
                                 float("nan"), float("nan"), False, True,
                                 "insufficient finite rows")

    f = [feats[i] for i in finite]
    y = [rets[i] for i in finite]
    cs = [coins[i] for i in finite]
    ic_all = spearman_ic(f, y)

    # Coin means for within residualisation.
    sum_f: dict[str, float] = {}
    sum_y: dict[str, float] = {}
    cnt: dict[str, int] = {}
    for c, fv, yv in zip(cs, f, y):
        sum_f[c] = sum_f.get(c, 0.0) + fv
        sum_y[c] = sum_y.get(c, 0.0) + yv
        cnt[c] = cnt.get(c, 0) + 1
    mean_f = {c: sum_f[c] / cnt[c] for c in cnt}
    mean_y = {c: sum_y[c] / cnt[c] for c in cnt}
    f_resid = [fv - mean_f[c] for c, fv in zip(cs, f)]
    y_resid = [yv - mean_y[c] for c, yv in zip(cs, y)]
    ic_within = spearman_ic(f_resid, y_resid)

    sign_flip = (math.isfinite(ic_all) and math.isfinite(ic_within)
                 and ic_all != 0 and ic_all * ic_within < 0)
    ratio = (ic_within / ic_all) if (math.isfinite(ic_all)
                                     and ic_all != 0
                                     and math.isfinite(ic_within)) else float("nan")

    is_scale = False
    reason = "within-coin IC survives fixed-effect removal"
    if sign_flip:
        is_scale, reason = True, "sign flips after coin fixed-effect removal"
    elif not math.isfinite(ratio):
        is_scale, reason = True, "undefined within/all ratio"
    elif abs(ratio) < ratio_threshold:
        is_scale = True
        reason = (f"|IC_within/IC_all|={abs(ratio):.3f} < {ratio_threshold}: "
                  f"pooled edge is mostly coin-membership/scale")
    return FixedEffectResult(len(finite), len(cnt), ic_all, ic_within, ratio,
                             sign_flip, is_scale, reason)
