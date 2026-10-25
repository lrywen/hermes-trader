"""Stage 0 — incremental / partial-IC validation (research validation framework).

Full-review plan (2026-10-25, P0 / §5.2 iron-law #2): a feature that looks
strong on its own must still explain the forward return AFTER the factors we
already know about. If it does not, it is a repackaged beta, not new
information. This module provides:

* ``partial_rank_ic``  — rank-IC of the feature residual vs the forward-return
  residual, after linearising out one or more control factors (a rank-based
  analogue of partial correlation). Controls are coin-mean-demeaned first so a
  pure scale factor cannot absorb residual signal through coin membership.
* ``ic_by_volatility_group`` — within-coin rank-IC split into volatility
  quantiles, so a feature that only "works" in one vol regime is exposed.

Pure standard library; long-form dict rows, same data contract as
``fixed_effect.py``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .fixed_effect import _pearson, _ranks, spearman_ic


def _finite_rows(rows: list[dict], keys: list[str]) -> list[dict]:
    """Keep rows where every numeric key is finite (coin key excluded)."""
    out = []
    for r in rows:
        if all(isinstance(r.get(k), (int, float)) and math.isfinite(r[k])
               for k in keys):
            out.append(r)
    return out


def _ols_residuals(y: list[float], x_cols: list[list[float]]) -> list[float]:
    """Residuals of y on the design matrix [1] + x_cols via normal equations.

    Falls back to a NaN vector when the design is rank-deficient (collinear
    controls), which the caller treats as "not estimable" rather than signal.
    """
    n = len(y)
    cols = [[1.0] * n] + x_cols
    p = len(cols)
    # X^T X and X^T y
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for a in range(p):
        for b in range(a, p):
            s = sum(cols[a][i] * cols[b][i] for i in range(n))
            xtx[a][b] = xtx[b][a] = s
        xty[a] = sum(cols[a][i] * y[i] for i in range(n))
    # Gaussian elimination with partial pivot.
    m = [xtx[i][:] + [xty[i]] for i in range(p)]
    for c in range(p):
        piv = max(range(c, p), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            return [float("nan")] * n
        m[c], m[piv] = m[piv], m[c]
        d = m[c][c]
        m[c] = [v / d for v in m[c]]
        for r in range(p):
            if r == c:
                continue
            f = m[r][c]
            m[r] = [m[r][j] - f * m[c][j] for j in range(p + 1)]
    beta = [m[i][p] for i in range(p)]
    return [y[i] - sum(beta[a] * cols[a][i] for a in range(p)) for i in range(n)]


@dataclass
class PartialICResult:
    n: int
    ic_marginal: float      # feature vs return with no controls
    ic_partial: float       # after residualising feature AND return on controls
    absorbed: bool          # controls removed the edge (|partial| collapses)
    estimable: bool
    reason: str


def partial_rank_ic(
    rows: list[dict],
    feature_key: str,
    ret_key: str,
    control_keys: list[str],
    coin_key: str = "coin",
    survive_fraction: float = 0.5,
) -> PartialICResult:
    """Rank-IC of the feature after removing linear dependence on controls.

    Both the feature and the forward return are residualised on the control
    set (plus coin fixed effects, always added), then Spearman-correlated.
    ``absorbed`` is True when the partial IC keeps less than
    ``survive_fraction`` of the marginal IC magnitude (beta repackaging).
    """
    keys = [feature_key, ret_key] + list(control_keys)
    data = _finite_rows(rows, keys)
    if len(data) < 3 + len(control_keys):
        return PartialICResult(len(data), float("nan"), float("nan"), True,
                               False, "insufficient finite rows")

    coins = sorted({r[coin_key] for r in data})
    coin_idx = {c: i for i, c in enumerate(coins)}
    feat = [float(r[feature_key]) for r in data]
    ret = [float(r[ret_key]) for r in data]
    ic_marginal = spearman_ic(feat, ret)

    if not control_keys:
        return PartialICResult(len(data), ic_marginal, ic_marginal, False, True,
                               "no controls requested")

    # Coin one-hot columns as fixed effects (drop last to avoid collinearity).
    control_cols = [[float(r[k]) for r in data] for k in control_keys]
    for c in coins[:-1]:
        control_cols.append([1.0 if r[coin_key] == c else 0.0 for r in data])

    feat_resid = _ols_residuals(feat, control_cols)
    ret_resid = _ols_residuals(ret, control_cols)
    if any(math.isnan(v) for v in feat_resid + ret_resid):
        return PartialICResult(len(data), ic_marginal, float("nan"), True,
                               False, "rank-deficient controls")
    ic_partial = spearman_ic(feat_resid, ret_resid)

    absorbed = (not math.isfinite(ic_partial)
                or abs(ic_marginal) < 1e-12
                or abs(ic_partial) < survive_fraction * abs(ic_marginal))
    reason = ("partial IC retains >= "
              f"{survive_fraction:.0%} of marginal" if not absorbed
              else "controls absorb most of the marginal IC (beta)")
    return PartialICResult(len(data), ic_marginal, ic_partial, absorbed, True,
                           reason)


@dataclass
class VolGroupIC:
    n: int
    groups: dict[str, float]   # volatility group label -> within-group IC
    consistent: bool           # same sign AND |IC|>=min_abs_ic in every populated group


def _within_coin_demean(
    data: list[dict], feature_key: str, ret_key: str, coin_key: str
) -> tuple[list[float], list[float]]:
    """Subtract each coin's own mean from BOTH feature and return (iron-law #2).

    Mirrors ``fixed_effect_ic`` so a pure per-coin scale/membership factor has
    zero within-coin variation and cannot look predictive inside a bucket.
    Coins with a single finite observation demean to exactly 0.0 on both axes.
    """
    sum_f: dict = {}
    sum_y: dict = {}
    cnt: dict = {}
    cs = [r.get(coin_key) for r in data]
    for c, r in zip(cs, data):
        fv, yv = float(r[feature_key]), float(r[ret_key])
        sum_f[c] = sum_f.get(c, 0.0) + fv
        sum_y[c] = sum_y.get(c, 0.0) + yv
        cnt[c] = cnt.get(c, 0) + 1
    mean_f = {c: sum_f[c] / cnt[c] for c in cnt}
    mean_y = {c: sum_y[c] / cnt[c] for c in cnt}
    f_resid = [float(r[feature_key]) - mean_f[c] for c, r in zip(cs, data)]
    y_resid = [float(r[ret_key]) - mean_y[c] for c, r in zip(cs, data)]
    return f_resid, y_resid


def ic_by_volatility_group(
    rows: list[dict],
    feature_key: str,
    ret_key: str,
    vol_key: str,
    coin_key: str = "coin",
    n_groups: int = 3,
    min_per_group: int = 20,
    min_abs_ic: float = 0.10,
) -> VolGroupIC:
    """Within-coin Spearman IC after bucketing rows by ``vol_key`` quantile.

    Both the feature and the forward return are de-meaned by coin FIRST (the
    fixed-effect control of iron-law #2), then rows are ranked globally by
    volatility and split into ``n_groups`` equal buckets (low/mid/high); IC is
    computed per bucket on the within-coin residuals.

    ``consistent`` requires BOTH (a) the IC sign to agree across every
    populated bucket AND (b) every bucket's ``|IC|`` to clear ``min_abs_ic``
    (default 0.10). Sign agreement alone is uninformative — with three buckets
    a pure-noise feature lands on the same sign by chance ~25% of the time,
    often with |IC| ~ 1e-3. Monte Carlo at n~100/bucket shows a fixed 0.10 floor
    drives the same-sign null pass rate to ~0.5% while still accepting a
    genuine rho~0.35 within-coin edge ~99% of the time. The floor is an
    argument, not a hard-coded constant, so larger-sample studies can tighten
    it. Without the de-meaning a pure scale factor (different coin -> different
    level) returns IC ~= 1 in every bucket and a false "consistent".
    """
    data = _finite_rows(rows, [feature_key, ret_key, vol_key])
    # Fail fast if ANY row entering the calc lacks a usable coin identity: that
    # row would be pooled into a synthetic coin=None group whose "within-coin
    # mean" is just the pooled mean, re-opening the scale-factor leak this gate
    # removes. (A single real coin across every row is a legitimate — if
    # degenerate — explicit input and is allowed.)
    if not data:
        raise ValueError(
            f"ic_by_volatility_group requires finite rows for "
            f"{feature_key!r}/{ret_key!r}/{vol_key!r}; got none.")
    missing = [i for i, r in enumerate(data) if r.get(coin_key) is None]
    if missing:
        raise ValueError(
            f"ic_by_volatility_group requires a usable {coin_key!r} on EVERY "
            f"row for within-coin demeaning; {len(missing)} of {len(data)} rows "
            "are missing it (key absent or None). Pass coin_key= or add the "
            "coin field instead of silently pooling those rows.")
    f_resid, y_resid = _within_coin_demean(data, feature_key, ret_key, coin_key)
    order = sorted(range(len(data)), key=lambda i: data[i][vol_key])
    size = max(1, len(order) // n_groups)
    labels = ["low", "mid", "high"] if n_groups == 3 else [f"g{i}" for i in range(n_groups)]
    groups: dict[str, float] = {}
    for g in range(n_groups):
        lo = g * size
        hi = len(order) if g == n_groups - 1 else (g + 1) * size
        idx = order[lo:hi]
        if len(idx) < min_per_group:
            continue
        f = [f_resid[i] for i in idx]
        y = [y_resid[i] for i in idx]
        groups[labels[g] if g < len(labels) else f"g{g}"] = spearman_ic(f, y)
    finite = [v for v in groups.values() if math.isfinite(v)]
    signs = {1 if v > 0 else -1 for v in finite}
    consistent = (
        len(finite) == len(groups)          # every populated bucket estimable
        and len(finite) >= 2                # need >=2 buckets to call "robust"
        and len(signs) == 1                 # all share one sign
        and all(abs(v) >= min_abs_ic for v in finite)
    )
    return VolGroupIC(len(data), groups, consistent)
