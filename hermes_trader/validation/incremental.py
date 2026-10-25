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
    consistent: bool           # same sign in all populated groups


def ic_by_volatility_group(
    rows: list[dict],
    feature_key: str,
    ret_key: str,
    vol_key: str,
    n_groups: int = 3,
    min_per_group: int = 20,
) -> VolGroupIC:
    """Within-group Spearman IC after bucketing rows by ``vol_key`` quantile.

    Rows are ranked globally by volatility then split into ``n_groups`` equal
    buckets (low/mid/high); IC is computed per bucket. ``consistent`` requires
    the IC sign to agree across every populated bucket.
    """
    data = _finite_rows(rows, [feature_key, ret_key, vol_key])
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
        f = [data[i][feature_key] for i in idx]
        y = [data[i][ret_key] for i in idx]
        groups[labels[g] if g < len(labels) else f"g{g}"] = spearman_ic(f, y)
    signs = [1 if v > 0 else -1 for v in groups.values() if math.isfinite(v) and v != 0]
    consistent = len(set(signs)) == 1 and len(signs) == len(groups)
    return VolGroupIC(len(data), groups, consistent)
