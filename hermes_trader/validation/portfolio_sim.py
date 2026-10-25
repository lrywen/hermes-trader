"""Stage 0 — daily cross-sectional portfolio simulation (validation framework).

Full-review plan (2026-10-25, P0 / §5.2 iron-law #3): a pooled quantile table
or a statistically significant IC is NOT evidence of tradability. A candidate
must survive a daily cross-sectional selection where, each day, we pick at most
one signal per coin, hold for one horizon, and report net bps after the full
round-trip cost for portfolio sizes N = 1/2/3/5. If N=1 and N=5 point in
opposite directions the feature is judged unstable (the ``spread_bps``
failure mode: significant IC, yet long-top-1 was -75 bps after cost while
N=5 turned positive — concentration in a few losers).

Inputs are long-form rows keyed by ``day``/``coin`` with a signal score and a
forward net-of-spot return (fraction). Costs are applied per round trip, in
bps. Pure standard library; deterministic day-block bootstrap for the CI.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class PortfolioResult:
    N: int
    n_entries: int
    n_days: int
    gross_bps: float        # mean per-entry gross bps
    cost_bps: float         # round-trip cost charged
    net_bps: float          # mean per-entry net bps (the headline number)
    net_ci_low: float       # day-block bootstrap 95% CI
    net_ci_high: float
    top1_concentration: float  # share of total gross from the single best entry
    per_day_net: dict[str, float] = field(default_factory=dict)


def _day_block_bootstrap_ci(
    day_means: list[tuple[str, float]],
    B: int,
    seed: int,
) -> tuple[float, float]:
    """95% CI of the pooled mean by resampling whole days (block = 1 day)."""
    if not day_means:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(day_means)
    boots = []
    for _ in range(B):
        idx = [rng.randrange(n) for _ in range(n)]
        boots.append(sum(day_means[i][1] for i in idx) / n)
    boots.sort()
    lo = boots[int(0.025 * (B - 1))]
    hi = boots[int(0.975 * (B - 1))]
    return lo, hi


def simulate_size(
    rows: list[dict],
    N: int,
    feature_key: str,
    ret_key: str,
    day_key: str = "day",
    coin_key: str = "coin",
    cost_bps: float = 9.47,
    long_only: bool = True,
    B: int = 2000,
    seed: int = 20261025,
) -> PortfolioResult:
    """Pick the top-N coins by feature each day; equal weight; charge cost.

    Exactly one observation per (day, coin) is REQUIRED. If a duplicate pair is
    present the call fails fast with ``ValueError``: silently keeping the first
    row in input order made the result order-dependent (shuffling the same rows
    could flip the headline sign). The caller must choose an aggregation policy
    upstream (e.g. last signal of the day, or a mean) and de-duplicate before
    calling. Forward returns are fractions (0.001 = 10 bps). When fewer than N
    coins signal on a day, the available ones are used.
    """
    # Group, enforcing exactly one row per (day, coin) with fail-fast.
    by_day: dict[str, dict[str, float]] = {}
    for r in rows:
        f, y = r.get(feature_key), r.get(ret_key)
        if not (isinstance(f, (int, float)) and isinstance(y, (int, float))):
            continue
        if not (math.isfinite(f) and math.isfinite(y)):
            continue
        d, c = r[day_key], r[coin_key]
        by_day.setdefault(d, {})
        if c in by_day[d]:
            raise ValueError(
                f"duplicate observation for ({day_key}={d!r}, {coin_key}={c!r}): "
                "simulate_size requires exactly one row per (day, coin). "
                "De-duplicate/aggregate upstream with an explicit policy "
                "instead of relying on input order.")
        by_day[d][c] = (float(f), float(y))

    entries = []          # (day, gross_bps)
    day_gross: dict[str, list[float]] = {}
    for d in sorted(by_day):
        cands = sorted(by_day[d].items(), key=lambda kv: kv[1][0],
                       reverse=long_only)
        chosen = cands[:N]
        for _coin, (_f, y) in chosen:
            g = y * 1e4
            entries.append((d, g))
            day_gross.setdefault(d, []).append(g)

    n_entries = len(entries)
    gross = sum(g for _, g in entries) / n_entries if n_entries else float("nan")
    net = gross - cost_bps if n_entries else float("nan")

    # Pooled net per day for a day-block CI (each day's mean net bps).
    day_means = []
    for d, gs in day_gross.items():
        day_means.append((d, sum(gs) / len(gs) - cost_bps))
    lo, hi = _day_block_bootstrap_ci(day_means, B, seed)

    best = max((g for _, g in entries), default=0.0)
    tot = sum(g for _, g in entries)
    concentration = (best / tot) if tot > 0 else float("nan")

    return PortfolioResult(
        N=N, n_entries=n_entries, n_days=len(day_gross),
        gross_bps=gross, cost_bps=cost_bps, net_bps=net,
        net_ci_low=lo, net_ci_high=hi,
        top1_concentration=concentration,
        per_day_net={d: v for d, v in day_means},
    )


def simulate_sizes(
    rows: list[dict],
    feature_key: str,
    ret_key: str,
    sizes: tuple[int, ...] = (1, 2, 3, 5),
    cost_bps: float = 9.47,
    **kw,
) -> dict[int, PortfolioResult]:
    """Run the cross-sectional sim for every requested N."""
    return {N: simulate_size(rows, N, feature_key, ret_key,
                             cost_bps=cost_bps, **kw)
            for N in sizes}


def tradability_verdict(results: dict[int, PortfolioResult]) -> tuple[bool, str]:
    """Plan iron-law #3: N=1 and N=5 opposite sign => unstable/not tradable.

    Also requires the headline top-1 (N=1) net mean to be positive after cost.
    """
    r1, r5 = results.get(1), results.get(5)
    if r1 is None or r5 is None:
        return (False, "N=1 and N=5 results required")
    if math.isnan(r1.net_bps) or r1.n_entries == 0:
        return (False, "no top-1 entries")
    if r1.net_bps * r5.net_bps < 0:
        return (False,
                f"N=1 net {r1.net_bps:+.1f} vs N=5 net {r5.net_bps:+.1f} "
                "have opposite signs -> unstable")
    if r1.net_bps <= 0:
        return (False, f"top-1 net {r1.net_bps:+.1f} bps after cost <= 0")
    return (True, f"signs agree; top-1 net {r1.net_bps:+.1f} bps after cost")
