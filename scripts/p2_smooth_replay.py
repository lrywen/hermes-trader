#!/usr/bin/env python3
"""P2 smooth-transition TICK-LEVEL A/B replay (READ-ONLY — no orders, no writes).

Question: at the phase1->phase2 arm instant the floor SNAPS from the hard
max-loss stop (below entry) up to the full trailing floor (~protect*(1-retrace)
ABOVE entry). Does that snap cause premature exits on a noise wick right at the
protect boundary that the smoother ramp (dsl_exit.ExitPolicy.smooth_transition_*)
would avoid — and does avoiding them IMPROVE captured PnL, net of the winners
the smooth floor then lets round-trip?

1m OHLCV can't resolve it because the arm+exit can happen INSIDE one candle. So
we drive TWO copies of the REAL exit engine (dsl_exit.DSLTracker.check) over the
SAME Binance spot aggTrades print path (ms timestamps), differing ONLY by policy
(baseline = smooth OFF, variant = smooth ON). Feeding mimics the live loop:

  * trailing / breach floors are evaluated once per ~60s wall-clock mark — the
    last traded print in each 60s bucket (the loop polls ~60s and sees a price
    near the end of its scan), and
  * the hard max-loss stop is a SERVER-SIDE bracket level that fires intra-bar,
    so it is tested against the FIRST print that trades through the hard floor
    (with a sub-poll hard-stop confirm of one print, i.e. effectively instant,
    matching the exchange backup SL). breach_confirm / hard_stop_confirm clock
    gates are set to 0 because the ~60s discrete sampling already encodes the
    live "only see prices at poll boundaries" resolution.

Candidate runs are found cheaply on Binance 1m klines (a peak that rose >=
protect_pct from a run base then retraced), and only the aggTrades around
entry->arm->retrace are pulled. No lookahead: entry is fixed before any forward
tick is replayed, and both engines see the identical print sequence.

Usage:
  python3 scripts/p2_smooth_replay.py                 # default coins, last --hours
  python3 scripts/p2_smooth_replay.py BTC ETH SOL --hours 12 --runs 3
  HTTPS_PROXY=http://192.168.124.65:7890 python3 scripts/p2_smooth_replay.py BTC
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_trader.agents.dsl_exit import DSLTracker, ExitPolicy, RetraceTier

KLINES = "https://api.binance.com/api/v3/klines"
AGG = "https://api.binance.com/api/v3/aggTrades"

# Binance spot symbol map (mirrors crypto_whale.binance_symbol; only majors the
# live system trades that also have deep spot books — no ":" synthetics).
_SYMBOL_OVERRIDE = {}
_DEFAULT_COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]


def _proxy_opener():
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


_OPENER = _proxy_opener()


def _get_json(url, tries=4, timeout=12):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 — retry any transient network error
            last = e
            time.sleep(0.8 * (k + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}: {last}")


def binance_symbol(coin):
    if ":" in coin:
        return None
    return _SYMBOL_OVERRIDE.get(coin, f"{coin.upper()}USDT")


# ── Candle (1m) run detection ────────────────────────────────────────────────
def fetch_klines(symbol, start_ms, end_ms):
    """1m klines in [start,end]; paged by startTime (1000/page)."""
    out = []
    cur = start_ms
    while cur < end_ms:
        batch = _get_json(
            f"{KLINES}?symbol={symbol}&interval=1m&startTime={cur}"
            f"&endTime={end_ms}&limit=1000")
        if not batch:
            break
        out.extend(batch)
        last_open = int(batch[-1][0])
        if last_open <= cur or len(batch) < 1000:
            break
        cur = last_open + 60_000
    # [open_t, o, h, l, c, ...]
    return [{"t": int(b[0]), "o": float(b[1]), "h": float(b[2]),
             "l": float(b[3]), "c": float(b[4])} for b in out]


def find_runs(candles, protect_pct, min_rise_pct=1.4, max_rise_pct=25.0,
              forward_bars=45):
    """Find peaks that rose >=protect from a base then retraced.

    Returns a list of dicts {entry_px, entry_t, peak_t, peak_px, end_t}.
    A peak bar i qualifies if: some later bar within `forward_bars` closes back
    down >= protect-ish from the peak (a retrace actually happened), and the
    rise from the run base (min low over the prior `lookback` bars) to the peak
    high is in [min_rise_pct, max_rise_pct]. Entry is anchored at the base low
    and entry_t at the most recent bar at/before the peak whose low touches it.
    No lookahead beyond choosing the peak (we replay entry->forward in order).
    """
    runs = []
    lookback = 120
    n = len(candles)
    used_entries = set()
    for i in range(lookback, n - 3):
        peak = candles[i]["h"]
        base = min(candles[j]["l"] for j in range(max(0, i - lookback), i + 1))
        rise = (peak - base) / base * 100
        if rise < min_rise_pct or rise > max_rise_pct:
            continue
        # require a real retrace after the peak (close falls >= protect_pct off peak)
        window = candles[i + 1: min(n, i + 1 + forward_bars)]
        if not window:
            continue
        min_close_after = min(w["c"] for w in window)
        pullback = (peak - min_close_after) / peak * 100
        if pullback < protect_pct * 0.7:  # need a meaningful give-back to stress the floor
            continue
        # entry anchor: last bar at/before i whose low touches `base`
        entry_i = i
        for j in range(i, max(0, i - lookback) - 1, -1):
            if candles[j]["l"] <= base * 1.0005:
                entry_i = j
                break
        entry_t = candles[entry_i]["t"]
        if entry_t in used_entries:
            continue
        used_entries.add(entry_t)
        end_t = window[-1]["t"] + 60_000
        runs.append({"entry_px": base, "entry_t": entry_t,
                     "peak_t": candles[i]["t"], "peak_px": peak,
                     "end_t": end_t, "rise_pct": rise,
                     "pullback_pct": pullback})
    # keep the strongest, most separated runs
    runs.sort(key=lambda r: r["pullback_pct"], reverse=True)
    return runs


# ── aggTrades tick path ──────────────────────────────────────────────────────
def fetch_aggtrades(symbol, start_ms, end_ms):
    """All aggTrades prints in [start,end] as (ts_ms, price) in time order."""
    prints = []
    cursor = None
    while True:
        if cursor is None:
            url = (f"{AGG}?symbol={symbol}&startTime={start_ms}&endTime={end_ms}"
                   f"&limit=1000")
        else:
            url = f"{AGG}?symbol={symbol}&fromId={cursor}&limit=1000"
        batch = _get_json(url)
        if not batch:
            break
        for t in batch:
            ts = int(t["T"])
            if start_ms <= ts <= end_ms:
                prints.append((ts, float(t["p"])))
        last_t = int(batch[-1]["T"])
        if len(batch) < 1000 or last_t >= end_ms:
            break
        cursor = int(batch[-1]["a"]) + 1
    prints.sort(key=lambda x: x[0])
    return prints


# ── Policy (mirrors live dsl_exit config; clock gates off for discrete replay) ─
def _base_kwargs():
    """Live-like exit params. Clock confirm gates set to 0: the ~60s sampling
    already models poll-boundary resolution; a sub-poll wick that the loop never
    sees must not be allowed to 'confirm' a stop the real loop couldn't act on."""
    return dict(
        max_loss_pct=0.4, max_loss_roe_pct=5.0,
        protect_pct=1.25, retrace_threshold=0.20,
        hard_timeout_minutes=1e9, stale_flat_timeout_minutes=0.0,
        breach_confirm_sec=0.0, hard_stop_confirm_sec=0.0,
        phase2_tiers=[RetraceTier(8.0, 0.35), RetraceTier(15.0, 0.40)],
    )


def make_policy(smooth_on, smooth_band_pct):
    kw = _base_kwargs()
    kw["smooth_transition_enabled"] = bool(smooth_on)
    kw["smooth_band_pct"] = float(smooth_band_pct)
    return ExitPolicy(**kw)


# ── Single-run tick replay of one engine ─────────────────────────────────────
def replay_engine(policy, prints, entry_px, entry_t_ms, leverage):
    """Feed prints to one DSLTracker.

    Returns dict with exit info (or None if no exit in window). Hard stop is the
    server-side bracket: the FIRST print trading through the effective hard-stop
    floor fires it (intra-bucket). The trailing/breach floor is evaluated once
    per 60s wall-clock bucket on the LAST print in that bucket.
    """
    tr = DSLTracker("P2", "long", entry_px, entry_time=entry_t_ms / 1000.0,
                    policy=policy, leverage=leverage, entry_atr_pct=0.0)
    eff_loss = tr._effective_max_loss()
    hard_floor = tr._hard_stop_floor(eff_loss)

    # Group prints into 60s wall-clock buckets (bucket = floor(ts/60000)).
    buckets = {}
    for ts, px in prints:
        buckets.setdefault(ts // 60_000, []).append((ts, px))
    bucket_keys = sorted(buckets)

    # Pre-compute, per bucket, the first print that trades through the hard
    # floor (server bracket fires intra-bucket) — checked before the bucket's
    # trailing mark in time order.
    last_ts, last_mark = (prints[0][0], prints[0][1]) if prints else (None, None)
    for bk in bucket_keys:
        rows = buckets[bk]
        # hard stop: first print at/below the hard floor (long)
        for ts, px in rows:
            if px <= hard_floor:
                return {
                    "exit": True, "kind": "hard_stop", "ts": ts, "px": px,
                    "reason": f"hard_stop (first print {px:.6g} <= {hard_floor:.6g})",
                    "unrl_pct": (px - entry_px) / entry_px * 100,
                    "peak_pct": tr._peak_profit_pct(),
                    "hard_floor": hard_floor,
                }
        # trailing engine: evaluate on the LAST print in the bucket (the mark the
        # 60s loop would act on), with time injected to that print's wall clock.
        ts, mark = rows[-1]
        last_ts, last_mark = ts, mark
        import hermes_trader.agents.dsl_exit as dmod
        _real_time = dmod.time.time
        dmod.time.time = (lambda t=ts: t / 1000.0)
        try:
            v = tr.check(mark)
        finally:
            dmod.time.time = _real_time
        if v.exit:
            return {
                "exit": True, "kind": "trailing", "ts": ts, "px": mark,
                "reason": v.reason, "unrl_pct": v.unrealized_pct,
                "peak_pct": tr._peak_profit_pct(),
                "floor": v.floor_price,
            }
    # No exit within the tick window: report the OPEN position marked at the
    # last print so a still-held runner isn't silently dropped from the compare.
    return {"exit": False, "kind": "open", "ts": last_ts, "px": last_mark,
            "reason": "still open at window end (marked at last print)",
            "unrl_pct": ((last_mark - entry_px) / entry_px * 100
                         if last_mark is not None else None),
            "peak_pct": tr._peak_profit_pct()}


def pnl_pct(exit_px, entry_px):
    return None if exit_px is None else (exit_px - entry_px) / entry_px * 100


def run_one(coin, run, leverage, smooth_band_pct, tick_end_ms, tick_hours=4.0):
    sym = binance_symbol(coin)
    # Pull prints from entry through the extended tick window so a smooth engine
    # that survives the first pull-back gets to keep trailing (or stop) rather
    # than being truncated before its trade resolves.
    t_end = int(min(tick_end_ms, run["entry_t"] + tick_hours * 3600 * 1000))
    prints = fetch_aggtrades(sym, int(run["entry_t"]), t_end)
    if len(prints) < 200:
        return None
    pol_b = make_policy(False, smooth_band_pct)
    pol_s = make_policy(True, smooth_band_pct)
    base = replay_engine(pol_b, prints, run["entry_px"], run["entry_t"], leverage)
    smth = replay_engine(pol_s, prints, run["entry_px"], run["entry_t"], leverage)
    return {
        "coin": coin, "sym": sym, "n_prints": len(prints),
        "entry_px": run["entry_px"], "rise_pct": run["rise_pct"],
        "pullback_pct": run["pullback_pct"],
        "window_min": (t_end - run["entry_t"]) / 60_000,
        "baseline": base, "smooth": smth,
        "base_pnl": pnl_pct(base["px"], run["entry_px"]),
        "smth_pnl": pnl_pct(smth["px"], run["entry_px"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("coins", nargs="*", default=_DEFAULT_COINS)
    ap.add_argument("--hours", type=float, default=8.0,
                    help="lookback window of 1m candles to scan for runs")
    ap.add_argument("--runs", type=int, default=3,
                    help="max qualifying runs per coin to tick-replay")
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--protect", type=float, default=1.25)
    ap.add_argument("--smooth-band", type=float, default=1.0,
                    help="smooth_band_pct (peak-profit ramp width) for the variant")
    ap.add_argument("--tick-hours", type=float, default=4.0,
                    help="hours of aggTrades after entry to tick-replay per run")
    args = ap.parse_args()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.hours * 3600 * 1000)
    leverage = args.leverage

    results = []
    for coin in args.coins:
        sym = binance_symbol(coin)
        if sym is None:
            continue
        try:
            candles = fetch_klines(sym, start_ms, end_ms)
        except Exception as e:  # noqa: BLE001
            print(f"[{coin}] klines failed: {e}")
            continue
        runs = find_runs(candles, protect_pct=args.protect)
        if not runs:
            print(f"[{coin}] no qualifying runs in last {args.hours}h "
                  f"({len(candles)} candles)")
            continue
        print(f"[{coin}] {len(candles)} candles, {len(runs)} candidate runs; "
              f"replaying top {min(args.runs, len(runs))}")
        for run in runs[: args.runs]:
            try:
                r = run_one(coin, run, leverage, args.smooth_band, end_ms,
                            tick_hours=args.tick_hours)
            except Exception as e:  # noqa: BLE001
                print(f"  !! run@{run['entry_px']:.6g} failed: {e}")
                continue
            if r is None:
                print(f"  run@{run['entry_px']:.6g}: too few prints, skipped")
                continue
            results.append(r)
            b, s = r["baseline"], r["smooth"]
            bp, sp = r["base_pnl"], r["smth_pnl"]
            bps = f"{bp:+.3f}%" if bp is not None else "  n/a "
            sps = f"{sp:+.3f}%" if sp is not None else "  n/a "
            delta = (sp - bp) if (bp is not None and sp is not None) else None
            dstr = f"{delta:+.3f}%" if delta is not None else "n/a"
            print(f"  @{r['entry_px']:.6g} rise={r['rise_pct']:.1f}% "
                  f"pb={r['pullback_pct']:.1f}% prints={r['n_prints']} "
                  f"({r['window_min']:.0f}m)")
            print(f"      base : {b['kind']:<9} pnl={bps} peak={b['peak_pct']:.2f}%  {b['reason'][:70]}")
            print(f"      smooth: {s['kind']:<9} pnl={sps} peak={s['peak_pct']:.2f}%  {s['reason'][:70]}")
            print(f"      delta(smooth-base) = {dstr}")

    # ── Aggregate ──────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print(f"SUMMARY  ({len(results)} runs, leverage={leverage}x, "
          f"smooth_band_pct={args.smooth_band})")
    both = [r for r in results if r["base_pnl"] is not None and r["smth_pnl"] is not None]
    if both:
        nb_exit = sum(1 for r in both if r["baseline"]["exit"])
        ns_exit = sum(1 for r in both if r["smooth"]["exit"])
        nb_open = len(both) - nb_exit
        ns_open = len(both) - ns_exit
        avg_b = sum(r["base_pnl"] for r in both) / len(both)
        avg_s = sum(r["smth_pnl"] for r in both) / len(both)
        wins = sum(1 for r in both if r["smth_pnl"] > r["base_pnl"] + 1e-9)
        losses = sum(1 for r in both if r["smth_pnl"] < r["base_pnl"] - 1e-9)
        ties = len(both) - wins - losses
        # reason mix (hard_stop / trailing / open)
        def mix(key):
            d = {}
            for r in both:
                k = r[key]["kind"]
                d[k] = d.get(k, 0) + 1
            return d
        print(f"  runs resolved (both engines): {len(both)}")
        print(f"  baseline: exited={nb_exit} open(end-of-window)={nb_open}")
        print(f"  smooth  : exited={ns_exit} open(end-of-window)={ns_open}")
        print(f"  avg resolved PnL  baseline={avg_b:+.3f}%   smooth={avg_s:+.3f}%   "
              f"(smooth-base)={avg_s-avg_b:+.3f}%")
        print(f"    ('open' = still holding at window end, marked at last print)")
        print(f"  smooth vs baseline: better={wins} worse={losses} tie={ties}")
        print(f"  baseline kinds: {mix('baseline')}")
        print(f"  smooth   kinds: {mix('smooth')}")
    else:
        print("  no runs with both engines exiting in-window.")


if __name__ == "__main__":
    main()
