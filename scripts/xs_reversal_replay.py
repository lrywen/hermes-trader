"""xs_reversal cross-sectional replay on OUR data + universe.

Reproduces pathiel's W-XSR1 specification faithfully, then reports whether it
holds on Hyperliquid data fetched through this repo's own client:

  * at each snapshot, rank coins cross-sectionally by trailing-3d return
    (never pooled across time);
  * short the top decile;
  * gate: funding spent off HL's 1.25e-05 baseline for >= 67% of trailing 7d;
  * outcome quoted from the SHORT side over next 24h: price move for the
    short + funding collected - 25bps round trip (never negated from a long);
  * significance by bootstrap clustered on the SNAPSHOT (coins in one snapshot
    share one market and are not independent).

Research tool (not shipped in the runtime image). Read/report only: never
writes config, flips mode or places orders.
"""
from __future__ import annotations

import argparse
import bisect
import statistics

from hermes_trader.client.hl_client import fetch_funding_history, fetch_hl_candles
from hermes_trader.validation.significance import DAY_MS

HOUR_MS = 3_600_000
BASELINE_F = 1.25e-05
COST_RT = 0.0025  # 25bps round trip, charged on the short side


def _default_universe(n: int) -> list[str]:
    from hermes_trader.client.universe import get_universe
    perps = [m for m in get_universe()
             if m["type"] == "perp" and not m["coin"].startswith("@")]
    perps.sort(key=lambda m: m.get("dayNtlVlm", 0), reverse=True)
    coins = [m["coin"] for m in perps[:n]]
    return coins or ["BTC", "ETH", "SOL"]


def _awake_fraction(funds: list[tuple[int, float]], now_ms: int,
                    days: float) -> float | None:
    """Share of trailing window spent off the venue baseline. Trailing only."""
    cutoff = now_ms - int(days * DAY_MS)
    vals = [f for (t, f) in funds if t >= cutoff]
    if len(vals) < 8:
        return None
    return sum(1 for f in vals if abs(f - BASELINE_F) > 1e-9) / len(vals)


def _bootstrap_cluster(snap_means: list[float], boot: int, seed: int) -> tuple[float, float]:
    """Bootstrap the per-snapshot mean trade return, resampling snapshots."""
    import random
    n = len(snap_means)
    if n < 2:
        raise ValueError("need >=2 snapshots")
    rng = random.Random(seed)
    vals = sorted(
        statistics.mean([snap_means[rng.randrange(n)] for _ in range(n)])
        for _ in range(boot)
    )
    return vals[int(boot * 0.025)], vals[int(boot * 0.975)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", type=int, default=40)
    ap.add_argument("--span-days", type=int, default=60,
                    help="snapshot panel length (pathiel panel ~70d)")
    ap.add_argument("--step-hours", type=int, default=8,
                    help="snapshot spacing")
    ap.add_argument("--lookback-d", type=float, default=3.0)
    ap.add_argument("--awake-d", type=float, default=7.0)
    ap.add_argument("--awake-min", type=float, default=0.67)
    ap.add_argument("--hold-hours", type=float, default=24.0)
    ap.add_argument("--top-pct", type=float, default=90.0)
    ap.add_argument("--min-vol", type=float, default=1_000_000.0)
    ap.add_argument("--min-universe", type=int, default=20)
    ap.add_argument("--show-gated", action="store_true")
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20260928)
    args = ap.parse_args()

    universe = _default_universe(args.coins)

    # Need: awake(7d) + lookback(3d) history before the first snapshot, plus
    # span + hold after. Fetch hourly candles & hourly funding once per coin.
    total_d = args.span_days + args.hold_hours / 24 + max(
        args.awake_d, args.lookback_d) + 1
    bars_n = int(total_d * 24) + 5
    start_pad_d = max(args.awake_d, args.lookback_d)

    panel: dict[str, dict] = {}
    for ci, coin in enumerate(universe, 1):
        try:
            candles = fetch_hl_candles(coin, "1h", bars_n)
        except Exception as e:
            print(f"[{ci}/{len(universe)}] {coin}: candles fail ({e}); skip")
            continue
        if len(candles) < bars_n * 0.6:
            print(f"[{ci}/{len(universe)}] {coin}: only {len(candles)} bars; skip")
            continue
        t0 = candles[0].t
        try:
            funds_raw = fetch_funding_history(coin, t0, candles[-1].t)
        except Exception as e:
            print(f"[{ci}/{len(universe)}] {coin}: funding fail ({e}); skip")
            continue
        funds = sorted((int(r["time"]), float(r["fundingRate"]))
                       for r in funds_raw if r.get("fundingRate") is not None)
        ts = [b.t for b in candles]
        closes = [b.c for b in candles]
        # rolling-24h notional volume proxy: v is base volume * close
        dollar_v = [b.v * b.c for b in candles]
        panel[coin] = {"ts": ts, "c": closes, "dv": dollar_v, "f": funds}
        print(f"[{ci}/{len(universe)}] {coin}: {len(candles)} bars, "
              f"{len(funds)} funding")

    if not panel:
        print("no data")
        return 1

    # Snapshot timeline: bounded by every coin having enough prior bars.
    latest_start = max(p["ts"][0] for p in panel.values())
    global_end = min(p["ts"][-1] for p in panel.values())
    first_snap = latest_start + int(start_pad_d * DAY_MS)
    step = args.step_hours * HOUR_MS
    hold_ms = int(args.hold_hours * HOUR_MS)
    snaps = list(range(first_snap, global_end - hold_ms, step))
    if not snaps:
        print("no usable snapshots (history too short)")
        return 1

    def price_at(coin: str, t: int) -> float | None:
        p = panel[coin]
        i = bisect.bisect_right(p["ts"], t) - 1
        return p["c"][i] if i >= 0 else None

    def vol24(coin: str, t: int) -> float:
        p = panel[coin]
        i1 = bisect.bisect_right(p["ts"], t)
        i0 = max(0, i1 - 24)
        return sum(p["dv"][i0:i1])

    # Per-snapshot candidate returns, gated and ungated, clustered by snapshot.
    rows: list[dict] = []
    for snap in snaps:
        scored = []
        for coin in panel:
            p0 = price_at(coin, snap - int(args.lookback_d * DAY_MS))
            p1 = price_at(coin, snap)
            pe = price_at(coin, snap + hold_ms)
            if not p0 or not p1 or not pe:
                continue
            if vol24(coin, snap) < args.min_vol:
                continue
            mom = p1 / p0 - 1.0
            awake = _awake_fraction(panel[coin]["f"], snap, args.awake_d)
            if awake is None:
                continue
            scored.append((coin, mom, awake, p1, pe))
        if len(scored) < args.min_universe:
            continue
        scored.sort(key=lambda x: x[1])
        n = len(scored)
        for rank, (coin, mom, awake, p1, pe) in enumerate(scored):
            pct = rank / (n - 1) * 100.0
            if pct < args.top_pct:
                continue
            # short-side return: price drop gains, funding collected while short.
            price_ret = p1 / pe - 1.0  # >0 when price falls
            # funding over the hold (funding rate is per-hour on HL)
            fi1 = bisect.bisect_right(panel[coin]["ts"], snap + hold_ms)
            fund = sum(f for (t, f) in panel[coin]["f"]
                       if snap < t <= snap + hold_ms)
            ret = price_ret + fund - COST_RT
            rows.append({"snap": snap, "coin": coin, "ret": ret,
                         "awake": awake, "mom": mom})

    if not rows:
        print("no top-decile candidates")
        return 1

    def report(label: str, subset: list[dict]) -> None:
        if not subset:
            print(f"{label:16} no trades")
            return
        by_snap: dict[int, list[float]] = {}
        for r in subset:
            by_snap.setdefault(r["snap"], []).append(r["ret"])
        snap_means = [statistics.mean(v) for v in by_snap.values()]
        mean_ret = statistics.mean(r["ret"] for r in subset)
        win = sum(1 for r in subset if r["ret"] > 0) / len(subset) * 100
        lo, hi = _bootstrap_cluster(snap_means, args.boot, args.seed)
        print(f"{label:16} n={len(subset):5} snaps={len(snap_means):3} "
              f"ret/trade={mean_ret*100:+7.3f}% win={win:5.1f}% "
              f"CI=[{lo*100:+.3f},{hi*100:+.3f}]% "
              f"{'PASS' if lo > 0 else ''}")

    print(f"\n=== xs_reversal replay | {len(snaps)} snapshots, "
          f"universe~{len(panel)}, step {args.step_hours}h ===")
    gated = [r for r in rows if r["awake"] >= args.awake_min]
    if args.show_gated:
        import datetime
        print("\n-- gated trades (sorted by snap) --")
        for r in sorted(gated, key=lambda x: x["snap"]):
            d = datetime.datetime.utcfromtimestamp(r["snap"] / 1000).strftime("%m-%d %H:%M")
            print(f"  {d} {r['coin']:10} ret={r['ret']*100:+7.2f}% "
                  f"mom3d={r['mom']*100:+6.1f}% awake={r['awake']:.2f}")
    report("ungated", rows)
    report("awake>=67%", gated)

    # Time quartiles of the gated leg (pathiel's OOS-across-time self-check).
    if gated:
        print("\n-- gated time quartiles --")
        gated.sort(key=lambda r: r["snap"])
        q = len(gated) // 4
        for qi in range(4):
            seg = gated[qi * q:] if qi == 3 else gated[qi * q:(qi + 1) * q]
            if seg:
                m = statistics.mean(r["ret"] for r in seg)
                print(f"  Q{qi+1}: n={len(seg):3} ret/trade={m*100:+7.3f}%")

    # De-overlapped robustness: 3h snapshots with 24h holds reuse one position
    # up to 8x. Keep only snapshots >=24h apart so each trade is non-overlapping.
    if gated:
        kept: list[dict] = []
        last_t = -10**15
        for r in sorted(gated, key=lambda x: x["snap"]):
            if r["snap"] - last_t >= 24 * HOUR_MS:
                kept.append(r)
                last_t = r["snap"]
        print("\n-- non-overlapping gated trades (>=24h apart) --")
        if len(kept) >= 2:
            by_snap: dict[int, list[float]] = {}
            for r in kept:
                by_snap.setdefault(r["snap"], []).append(r["ret"])
            sm = [statistics.mean(v) for v in by_snap.values()]
            lo, hi = _bootstrap_cluster(sm, args.boot, args.seed)
            m = statistics.mean(r["ret"] for r in kept)
            print(f"  n={len(kept):3} ret/trade={m*100:+7.3f}% "
                  f"CI=[{lo*100:+.3f},{hi*100:+.3f}]% {'PASS' if lo > 0 else ''}")

    # awake monotonicity bands (the mechanism claim)
    print("\n-- awake bands (all top-decile) --")
    for lo_b, hi_b, name in ((0.0, 0.0001, "dead(0%)"),
                             (0.0001, 0.34, "faint(1-33%)"),
                             (0.34, 0.67, "moderate"),
                             (0.67, 1.01, "awake")):
        band = [r for r in rows if lo_b <= r["awake"] <= hi_b]
        if band:
            m = statistics.mean(r["ret"] for r in band)
            w = sum(1 for r in band if r["ret"] > 0) / len(band) * 100
            print(f"  {name:12} n={len(band):5} ret/trade={m*100:+7.3f}% win={w:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
