#!/usr/bin/env python3
"""Event-time replay: turn historical PIT signals into SYNTHETIC closed trades.

The forward-return analysis (bf / ch) measured signal-point mark quality with no
stops or costs. This replay drives the PRODUCTION exit state machine
(dsl_exit.DSLTracker + the same config-derived ExitPolicy the shadow book uses)
over historical 15m candles to produce synthetic taker closed trades with:

  * entry  = first 15m bar OPEN after the signal bar closes (PIT, no lookahead),
  * exit   = first subsequent bar on which DSLTracker.check exits, filled at the
             returned floor for a floor_breach / at the adverse bar extreme for a
             max_loss stop (gap-through bounded by that bar), or at the bar close
             for a timeout exit,
  * cost   = taker fee each side (default 0.045%), leverage from config,
  * labels = surface channel / composite band / AI side / exit reason / MFE.

Bars are fed adverse-tick-first within each 15m bar (long: low then high;
short: high then low) so stop fills are conservative. The tracker's wall clock
is pinned to each bar's event time (dsl_exit.time.time / time.monotonic) and
persistence is disabled, so thousands of sims write nothing. index_px=None
fails open to mid exactly as the shadow book does without an oracle feed.

This is a SIMULATION, not evidence the live engine would have filled identically
(15m intrabar path is approximated; funding omitted; maker fills ignored —
taker only). Use for go/no-go and ranking; real tagged fills remain the gate.

Offline (CH_OFFLINE=1): reads HERMES_HIST_CANDLE_CACHE [o,h,l,c,v] keyed
coin|15m and CH_SIGNALS (the ch_signal_extract json). Writes CH_TRADES jsonl and
CH_OUT summary json.
"""
from __future__ import annotations

import json
import math
import os
import sys
import datetime as dt
from collections import defaultdict

STEP = 15 * 60_000
# bars of 4h aggregated from 15m for the Wilder ATR(14) entry stop width
ATR_BARS = 16          # 16 x 15m = 4h
ATR_PERIOD = 14
MAX_HOLD_BARS = int(os.environ.get("REPLAY_MAX_HOLD_BARS", str(96 * 4)))  # ~8d cap

TAKER_FEE_PCT = float(os.environ.get("REPLAY_TAKER_FEE_PCT", "0.045")) / 100.0
DEFAULT_LEV = int(os.environ.get("REPLAY_LEV", "3"))


def wilder_atr_pct(bars_4h, *, i_entry, period=ATR_PERIOD, entry_px=0.0):
    """Wilder ATR(period) on CLOSED 4h bars up to but excluding the entry bar,
    returned as % of entry_px. bars_4h items are (t,o,h,l,c). Mirrors
    exchange.get_hl_atr (true range vs prior close, Wilder smoothing)."""
    closed = bars_4h[:i_entry]          # never use the forming/entry bar
    if len(closed) < period + 1 or entry_px <= 0:
        return 0.0
    tr = []
    for j in range(1, len(closed)):
        _, o, h, l, c = closed[j]
        pc = closed[j - 1][4]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(tr) < period:
        return 0.0
    atr = sum(tr[:period]) / period
    for j in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[j]) / period
    return atr / entry_px * 100.0


def aggregate_4h(bars):
    """Aggregate ascending 15m (t,o,h,l,c,v) into aligned 4h bars."""
    out = []
    bucket = 4 * 60 * 60_000
    cur = None
    for t, o, h, l, c, *_ in bars:
        key = (t // bucket) * bucket
        if cur is None or cur[0] != key:
            if cur is not None:
                out.append(cur)
            cur = [key, o, h, l, c]
        else:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
    if cur is not None:
        out.append(cur)
    return out


def _ignition_bar(bars, i, *, is_long, entry_idx, vol_mult=2.0, lookback=20):
    """Replicate dsl_exit.detect_ignition for ONE bar at index i on 15m.

    The production launch gate (one of ignite_periods is 15m) requires a
    CLOSED post-entry bar with volume >= vol_mult x the mean of the prior
    ``lookback`` bars AND a close beyond their range (high for long / low for
    short). Bars are (t,o,h,l,c,v). Price-only caches carry v=0 -> never
    ignites, which then transparently falls through to the timeouts (the
    conservative no-launch read)."""
    if i <= entry_idx:
        return False
    b = bars[i]
    if len(b) < 6:
        return False
    v = b[5]
    prior = bars[max(0, i - lookback):i]
    if len(prior) < lookback:
        return False
    vols = [p[5] if len(p) > 5 else 0 for p in prior]
    avg = sum(vols) / len(vols)
    if avg <= 0 or v < vol_mult * avg:
        return False
    c = b[4]
    if is_long:
        return c > max(p[2] for p in prior)
    return c < min(p[3] for p in prior)


def simulate_trade(*, bars, i_entry, side, entry_atr_pct, policy, leverage,
                   fee_pct, entry_time_s, dsl_module, tracker_cls,
                   max_hold=MAX_HOLD_BARS, use_ignition=True):
    """Run one synthetic position. ``bars`` are ascending (t,o,h,l,c[,v]);
    entry is the OPEN of bars[i_entry]. Returns a closed-trade dict or None if
    it cannot be opened / never resolves inside the series."""
    if not (0 <= i_entry < len(bars)):
        return None
    entry_px = float(bars[i_entry][1])
    if not (entry_px > 0 and math.isfinite(entry_px)):
        return None
    is_long = side == "LONG"
    tracker = tracker_cls(
        coin="R", side="long" if is_long else "short", entry_px=entry_px,
        entry_time=entry_time_s, policy=policy, leverage=leverage,
        entry_atr_pct=float(entry_atr_pct), entry_regime="")

    # Swap ONLY the ``time`` NAME on the dsl module for a shim; the stdlib
    # ``time`` module is a process-wide singleton, so reassigning
    # ``dsl_module.time.time`` would freeze the clock for every other module
    # (and never get restored here), leaking the synthetic bar epoch into
    # unrelated tests under pytest-xdist. Restored in ``finally`` below.
    import types
    real_time_mod = dsl_module.time

    end = min(len(bars), i_entry + 1 + max_hold)
    try:
        for j in range(i_entry + 1, end):
            t, o, h, l, c = bars[j][:5]
            bar_time_s = t / 1000.0 + STEP / 1000.0   # moment this bar has closed
            # Pin the DSL clock to this bar's close for the whole bar (wall +
            # monotonic share the same synthetic instant), then run the ticks.
            dsl_module.time = types.SimpleNamespace(
                time=lambda bt=bar_time_s: bt,
                monotonic=lambda bt=bar_time_s: bt,
                sleep=real_time_mod.sleep)
            # Mark a 15m volume+breakout launch BEFORE the stop check, exactly as
            # production refresh_ignition_states does between scan ticks.
            if use_ignition and not tracker.ignited and _ignition_bar(
                    bars, j, is_long=is_long, entry_idx=i_entry):
                try:
                    tracker.mark_ignited(t / 1000.0, period="15m")
                except Exception:
                    pass
            # feed adverse extreme first (conservative stop trigger order)
            ticks = (l, h) if is_long else (h, l)
            verdict = None
            trig_px = None
            for px in ticks:
                v = tracker.check(float(px), None)
                if getattr(v, "exit", False):
                    verdict = v
                    # floor stops fill at the floor; cap gap-through to this bar's
                    # adverse extreme so we never invent a fill outside the bar range
                    floor = getattr(v, "floor_price", None)
                    if floor and floor > 0:
                        trig_px = min(floor, h) if is_long else max(floor, l)
                        if is_long and trig_px < l:
                            trig_px = l
                        if not is_long and trig_px > h:
                            trig_px = h
                    else:
                        trig_px = px          # timeout / non-floor exit at the tick
                    break
            if verdict is not None:
                exit_px = float(trig_px)
                gross = (exit_px / entry_px - 1.0) if is_long \
                    else (1.0 - exit_px / entry_px)
                cost = 2.0 * fee_pct
                net = gross - cost
                return {
                    "entry_idx": i_entry, "exit_idx": j,
                    "bars_held": j - i_entry,
                    "entry_px": entry_px, "exit_px": exit_px,
                    "side": side, "gross_pct": round(100 * gross, 4),
                    "fee_roundtrip_pct": round(100 * cost, 4),
                    "roe_pct": round(100 * net * leverage, 3),
                    "ret_pct": round(100 * net, 4),
                    "reason": str(getattr(verdict, "reason", "") or "").split(" ", 1)[0],
                    "mfe_pct": round(100 * float(getattr(verdict, "mfe_pct", 0) or 0), 3),
                }
        return None  # never exited within available series (right-censored)
    finally:
        dsl_module.time = real_time_mod


def _load_cache(path, interval="15m"):
    raw = json.load(open(path, encoding="utf-8"))
    series = {}
    for key, b in raw.items():
        if "|" not in key:
            continue
        coin, iv = key.split("|", 1)
        if iv != interval:
            continue
        rows = sorted((int(t), float(v[0]), float(v[1]), float(v[2]),
                       float(v[3]), float(v[4]) if len(v) > 4 else 0.0)
                      for t, v in b.items())
        series[coin] = rows
    return series


def build_policy():
    """Same policy the shadow book builds: config base overlaid with the
    neutral-regime exit params. Fail back to base ExitPolicy."""
    from hermes_trader.agents.dsl_exit import _policy_from_config, ExitPolicy
    try:
        from hermes_trader.agents.config_store import read_agent_config
        from hermes_trader.agents.regime_exit_common import overlay_regime_exit
        dsl = read_agent_config().get("dsl_exit", {}) or {}
        pol, _ = overlay_regime_exit(_policy_from_config(), dsl, "neutral")
        return pol
    except Exception:
        return _policy_from_config() if callable(_policy_from_config) else ExitPolicy()


def main() -> int:
    import bisect
    from hermes_trader.agents import dsl_exit as dsl_module
    # silence persistence during the batch
    dsl_module._request_save = lambda force=False: None

    signals = json.load(open(os.environ["CH_SIGNALS"], encoding="utf-8"))
    series = _load_cache(os.environ.get("HERMES_HIST_CANDLE_CACHE",
                                        "/tmp/ch_candles.json"))
    policy = build_policy()
    leverage = DEFAULT_LEV

    trades = []
    censored = 0
    for s in signals:
        v = s.get("verdict")
        if v not in ("LONG", "SHORT"):
            continue
        coin = s["coin"]
        bars = series.get(coin)
        if not bars:
            continue
        ts = [b[0] for b in bars]
        # entry = open of first 15m bar that STARTS at/after the signal (the
        # signal bar may still be forming; bisect_left on bar-open gives the
        # next fully-closed-then-opened bar — conservative one-bar lag).
        i = bisect.bisect_left(ts, s["ts"])
        if i <= 0 or i >= len(bars) - 2:
            continue
        bars4 = aggregate_4h(bars)
        # index in 4h series of the entry 15m bar's containing 4h bucket
        bucket = (bars[i][0] // (4 * 3600_000)) * (4 * 3600_000)
        i4 = next((k for k, b in enumerate(bars4) if b[0] == bucket), len(bars4))
        atr_pct = wilder_atr_pct(
            bars4, i_entry=i4, entry_px=float(bars[i][1]))
        tr = simulate_trade(
            bars=bars, i_entry=i, side=v, entry_atr_pct=atr_pct,
            policy=policy, leverage=leverage, fee_pct=TAKER_FEE_PCT,
            entry_time_s=bars[i][0] / 1000.0, dsl_module=dsl_module,
            tracker_cls=dsl_module.DSLTracker)
        if tr is None:
            censored += 1
            continue
        tr.update({"coin": coin, "ts": s["ts"], "channel": s.get("channel"),
                   "band": s.get("band"), "composite": s.get("composite"),
                   "conf": s.get("conf"), "entry_atr_pct": round(atr_pct, 3)})
        trades.append(tr)

    out_tr = os.environ.get("CH_TRADES", "/tmp/ch_trades.jsonl")
    with open(out_tr, "w", encoding="utf-8") as f:
        for tr in trades:
            f.write(json.dumps(tr, separators=(",", ":")) + "\n")
    print("WROTE_TRADES", out_tr, len(trades), "censored", censored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
