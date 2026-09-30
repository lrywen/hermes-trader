"""Funding carry measurement on Hyperliquid (research, perp-only data).

Answers the cheap upstream question BEFORE building any two-leg spot/perp
delta-neutral execution: is the raw funding carry on our universe even thick
and persistent enough to be worth harvesting?

For each coin, page back the hourly ``fundingHistory`` (single response capped
at 500 rows; arbitrary history is reachable by paging). Then:

  * raw carry ceiling: collect funding on every hour it clears a threshold
    (short-only), report annualised carry and time-in-market;
  * funding persistence (lag-1 autocorr) and mean reversion, so we do not
    systematically enter at the funding top;
  * no price leg, no spot hedge, no costs in this step — it is an upper bound
    on the carry, deliberately optimistic. If even this is thin, building the
    hedge infrastructure is pointless.

Read/report only. Never places orders, writes config or flips mode.
"""
from __future__ import annotations

import argparse
import statistics
import time

HOUR_MS = 3_600_000
PAGE = 500


def _default_universe(n: int) -> list[str]:
    from hermes_trader.client.universe import get_universe
    perps = [m for m in get_universe()
             if m["type"] == "perp" and not m["coin"].startswith("@")]
    perps.sort(key=lambda m: m.get("dayNtlVlm", 0), reverse=True)
    return [m["coin"] for m in perps[:n]] or ["BTC", "ETH", "SOL"]


def fetch_all_funding(coin: str, hours: int) -> list[tuple[int, float]]:
    """Page fundingHistory backward until `hours` of history is collected.

    Uses a direct POST rather than hl_client.fetch_funding_history: that
    helper caches by coin alone (window-agnostic, 5-min TTL) for prompt
    context, so paging with different windows would keep returning the first
    cached page and spin forever. The raw endpoint pages correctly.
    """
    import httpx
    now = int(time.time() * 1000)
    target_start = now - hours * HOUR_MS
    out: dict[int, float] = {}
    end = now
    while end > target_start:
        start = max(target_start, end - PAGE * HOUR_MS)
        payload = {"type": "fundingHistory", "coin": coin,
                   "startTime": start, "endTime": end}
        rows = httpx.post("https://api.hyperliquid.xyz/info",
                          json=payload, timeout=30).json()
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            f = r.get("fundingRate")
            if f is not None:
                out[int(r["time"])] = float(f)
        oldest = min(int(r["time"]) for r in rows)
        if len(rows) < PAGE or oldest <= start or oldest >= end:
            break
        end = oldest
    return sorted(out.items())


def _lag1_autocorr(xs: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = statistics.mean(xs)
    den = sum((x - m) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((xs[i] - m) * (xs[i - 1] - m) for i in range(1, n)) / den


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=30)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--thresholds", default="0.0000125,0.00003,0.00005,0.0001")
    args = ap.parse_args()

    thresholds = [float(x) for x in args.thresholds.split(",")]
    universe = _default_universe(args.coins)
    hours = args.days * 24

    # coin -> sorted [(t, funding)]
    series: dict[str, list[tuple[int, float]]] = {}
    for ci, coin in enumerate(universe, 1):
        try:
            rows = fetch_all_funding(coin, hours)
        except Exception as e:
            print(f"[{ci}/{len(universe)}] {coin}: fail ({e}); skip")
            continue
        if len(rows) < 100:
            print(f"[{ci}/{len(universe)}] {coin}: only {len(rows)}; skip")
            continue
        series[coin] = rows
        fs = [f for _, f in rows]
        print(f"[{ci}/{len(universe)}] {coin}: {len(rows)}h "
              f"median={statistics.median(fs)*1e4:+.2f}bps "
              f"mean={statistics.mean(fs)*1e4:+.2f}bps")

    if not series:
        print("no data")
        return 1

    # Build a common timeline grid per coin (hourly). Carry is collected on
    # hour t if funding printed at t clears the threshold (known at t).
    print(f"\n=== raw short-funding carry ceiling | {len(series)} coins, "
          f"~{args.days}d ===")
    print(f"{'threshold':>10} {'coins-harvest':>13} {'time%':>7} "
          f"{'carry/yr':>10} {'avg f when in':>13}")
    for th in thresholds:
        in_hours = 0
        total_hours = 0
        carry_sum = 0.0
        coins_paying = 0
        for coin, rows in series.items():
            fs = [f for _, f in rows]
            picked = [f for f in fs if f >= th]
            if picked:
                coins_paying += 1
            in_hours += len(picked)
            total_hours += len(fs)
            carry_sum += sum(picked)
        time_pct = in_hours / total_hours * 100 if total_hours else 0
        # annualised return on deployed capital: total funding collected /
        # hours-in-position scaled to a year
        apy = (carry_sum / in_hours * 24 * 365 * 100) if in_hours else 0.0
        print(f"{th*1e4:8.2f}bp {coins_paying:13} {time_pct:7.1f} "
              f"{apy:9.1f}% {carry_sum/in_hours*1e4 if in_hours else 0:12.2f}bp")

    # Persistence / mean reversion: per-coin lag-1 autocorr of funding.
    print("\n-- funding persistence (lag-1 autocorr) --")
    autocorrs = []
    for coin, rows in series.items():
        a = _lag1_autocorr([f for _, f in rows])
        autocorrs.append(a)
    print(f"  median across coins: {statistics.median(autocorrs):+.3f} "
          f"(high=persistent, safe to ride; low/neg=mean-reverting, top-entry risk)")

    # Conditional edge: does entering AFTER a funding spike still pay, or does
    # funding revert? Compare next-hour funding given current >= threshold.
    print("\n-- next-hour funding given current >= 5bp --")
    for coin, rows in list(series.items())[:8]:
        fs = [f for _, f in rows]
        nxt = [fs[i + 1] for i in range(len(fs) - 1) if fs[i] >= 0.00005]
        if nxt:
            print(f"  {coin:10} events={len(nxt):4} next-hour median="
                  f"{statistics.median(nxt)*1e4:+.2f}bp")
    # Price leg vs carry (the deciding test): at each hour funding clears a
    # threshold, measure the next-hour SHORT price return (price falling pays
    # the short). If the price leg dwarfs funding, an unhedged carry is a
    # directional bet and a delta-neutral hedge is mandatory — and only worth
    # it if the net (carry - cost) survives.
    print("\n-- next-hour short price leg when funding >= 0.5bp --")
    print(f"{'coin':10} {'events':>6} {'fund_bp':>8} {'price_bp':>9} "
          f"{'net_bp':>8} {'price>fund%':>11}")
    from hermes_trader.client.hl_client import fetch_hl_candles
    th = 0.00005
    agg_f = agg_p = agg_n = 0
    agg_ev = 0
    overwhelmed = 0
    for coin, rows in series.items():
        picks = [(i, f) for i, (_, f) in enumerate(rows[:-1]) if f >= th]
        if not picks:
            continue
        try:
            candles = fetch_hl_candles(coin, "1h", 3000)
        except Exception:
            continue
        cts = [b.t for b in candles]
        import bisect as _bi
        ts = [t for t, _ in rows]

        def bar_near(t: int):
            # funding timestamps carry ms jitter; snap to the enclosing hour bar.
            i = _bi.bisect_left(cts, t)
            cands = [j for j in (i - 1, i) if 0 <= j < len(cts)]
            if not cands:
                return None
            j = min(cands, key=lambda j: abs(cts[j] - t))
            return candles[j] if abs(cts[j] - t) < HOUR_MS else None

        fund_l = []
        price_l = []
        for i, f in picks:
            b = bar_near(ts[i])
            bn = bar_near(ts[i + 1])
            if not b or not bn or b.c <= 0 or bn is b:
                continue
            pr = b.c / bn.c - 1.0  # short's next-hour price return
            fund_l.append(f)
            price_l.append(pr)
        if not fund_l:
            continue
        mf = statistics.mean(fund_l)
        mp = statistics.mean(price_l)
        over = sum(1 for f, p in zip(fund_l, price_l) if abs(p) > f) / len(fund_l) * 100
        print(f"{coin:10} {len(fund_l):6} {mf*1e4:8.2f} {mp*1e4:+9.2f} "
              f"{(mf+mp)*1e4:+8.2f} {over:10.0f}%")
        agg_f += sum(fund_l)
        agg_p += sum(price_l)
        agg_n += len(fund_l)
        agg_ev += len(fund_l)
        overwhelmed += sum(1 for f, p in zip(fund_l, price_l) if abs(p) > f)
    if agg_ev:
        print(f"{'ALL':10} {agg_ev:6} {agg_f/agg_n*1e4:8.2f} "
              f"{agg_p/agg_n*1e4:+9.2f} {(agg_f+agg_p)/agg_n*1e4:+8.2f} "
              f"{overwhelmed/agg_ev*100:10.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
