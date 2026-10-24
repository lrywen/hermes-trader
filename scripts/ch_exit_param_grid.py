#!/usr/bin/env python3
"""Exit-parameter grid replay over historical PIT signals (h1, redundancy plan).

Drives the PRODUCTION dsl_exit.DSLTracker with the same bar-by-bar adverse-tick
machinery as ch_event_replay, but replays each LONG signal under several
ExitPolicy variants to answer the B-class questions the new per-exit shadow
writer would otherwise need ~30+ live fills per arm to judge:

  * baseline            : neutral-regime policy the shadow book uses today
  * protect 1.5 -> 1.0  : lower the phase-2 arming threshold (more winners arm)
  * protect 1.5 -> 0.75 : aggressive
  * breakeven 2.5 -> 1.0: add a guaranteed-profit lock at +1.0% peak
  * protect 1.0 + BE1.0 : combined

For every variant we record, on the SAME entries (so it is a paired comparison):
  reason mix, median/mean taker ROE net of fees, win rate, median bars held,
  fraction that EVER armed phase-2 (peak>=protect), fraction ignited, true
  bar-based MFE/MAE, and the double-peak exit signature (max_loss cluster vs
  floor_breach cluster).

The same entries + same intrabar path are used for every variant; only the
policy differs, so differences are causal to the knobs (simulation fidelity
notwithstanding). Offline, network-free.

Env:
  CH_TRADES    surviving ch_event_replay trades (provides signal coin/ts/side/
               channel/band/composite/conf) [/tmp/v2/ch_trades.jsonl]
  CH_DEEP      deep 15m candle cache coin|15m          [/tmp/v2/ch_deep_candles.json]
  CH_GRID_OUT  summary json                          [/tmp/v2/ch_grid_result.json]
"""
from __future__ import annotations

import bisect
import dataclasses
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "ch_event_replay", os.path.join(_HERE, "ch_event_replay.py"))
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)  # type: ignore

STEP = rp.STEP
TAKER_FEE_PCT = rp.TAKER_FEE_PCT
LEV = rp.DEFAULT_LEV


def _load_signals(path):
    out = []
    for line in open(path, encoding="utf-8"):
        t = json.loads(line)
        if t.get("side") != "LONG":
            continue
        out.append(t)
    return out


def _variants(base):
    """Construct named ExitPolicy variants from the neutral base."""
    def mk(name, **kw):
        return name, dataclasses.replace(base, **kw)
    v = [("baseline", base)]
    v.append(mk("protect_1.0", protect_pct=1.0))
    v.append(mk("protect_0.75", protect_pct=0.75))
    # add a breakeven ratchet (lock floor at +0.2% spot once peak clears 1.0%)
    v.append(mk("breakeven_1.0",
                breakeven_trigger_pct=1.0, breakeven_lock_pct=0.2))
    v.append(mk("protect1.0+BE1.0",
                protect_pct=1.0, breakeven_trigger_pct=1.0,
                breakeven_lock_pct=0.2))
    return v


def replay_one(*, bars, i_entry, policy, dsl_module, tracker_cls,
               leverage=LEV, fee_pct=TAKER_FEE_PCT, max_hold=rp.MAX_HOLD_BARS):
    """Same as rp.simulate_trade but ALSO returns true bar MFE/MAE and
    phase-2/ignite arming flags, independent of the (unit-odd) verdict mfe."""
    entry_px = float(bars[i_entry][1])
    tracker = tracker_cls(
        coin="R", side="long", entry_px=entry_px,
        entry_time=bars[i_entry][0] / 1000.0, policy=policy,
        leverage=leverage, entry_atr_pct=0.0, entry_regime="")
    # Swap ONLY the ``time`` NAME on the dsl module for a shim; the stdlib
    # ``time`` module is a process-wide singleton, so reassigning
    # ``dsl_module.time.time`` would freeze the clock for every other module
    # (and never get restored here), leaking the synthetic bar epoch into
    # unrelated tests under pytest-xdist. Restored in ``finally`` below.
    import types
    real_time_mod = dsl_module.time
    end = min(len(bars), i_entry + 1 + max_hold)
    mfe = mae = 0.0
    armed_protect = False
    ignited = False
    try:
        for j in range(i_entry + 1, end):
            t, o, h, l, c = bars[j][:5]
            bar_time_s = t / 1000.0 + STEP / 1000.0
            # Pin the DSL clock to this bar's close (wall + monotonic share
            # the same synthetic instant) before the ticks.
            dsl_module.time = types.SimpleNamespace(
                time=lambda bt=bar_time_s: bt,
                monotonic=lambda bt=bar_time_s: bt,
                sleep=real_time_mod.sleep)
            # true excursion from the bar extremes (long)
            mfe = max(mfe, (h / entry_px - 1.0) * 100.0)
            mae = min(mae, (l / entry_px - 1.0) * 100.0)
            if mfe >= float(policy.protect_pct):
                armed_protect = True
            if not ignited and rp._ignition_bar(bars, j, is_long=True,
                                                entry_idx=i_entry):
                try:
                    tracker.mark_ignited(t / 1000.0, period="15m")
                    ignited = True
                except Exception:
                    pass
            verdict = None
            trig_px = None
            for px in (l, h):  # adverse first
                vv = tracker.check(float(px), None)
                if getattr(vv, "exit", False):
                    verdict = vv
                    floor = getattr(vv, "floor_price", None)
                    if floor and floor > 0:
                        trig_px = min(floor, h)
                        if trig_px < l:
                            trig_px = l
                    else:
                        trig_px = px
                    break
            if verdict is not None:
                exit_px = float(trig_px)
                gross = exit_px / entry_px - 1.0
                net = gross - 2.0 * fee_pct
                return {
                    "reason": str(getattr(verdict, "reason", "") or "").split(" ", 1)[0],
                    "roe": 100.0 * net * leverage,
                    "ret": 100.0 * net,
                    "bars": j - i_entry,
                    "mfe": round(mfe, 3), "mae": round(mae, 3),
                    "armed_protect": armed_protect, "ignited": ignited,
                }
        return None
    finally:
        dsl_module.time = real_time_mod


def _summ(rows):
    roes = [r["roe"] for r in rows]
    wins = [r for r in rows if r["roe"] > 0]
    reasons = {}
    for r in rows:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    n = len(rows) or 1
    return {
        "n": len(rows),
        "roe_median": round(st.median(roes), 3),
        "roe_mean": round(sum(roes) / len(roes), 4),
        "win_rate": round(100.0 * len(wins) / n, 2),
        "bars_median": st.median([r["bars"] for r in rows]),
        "armed_protect_pct": round(100.0 * sum(r["armed_protect"] for r in rows) / n, 2),
        "ignited_pct": round(100.0 * sum(r["ignited"] for r in rows) / n, 2),
        "mfe_median": round(st.median([r["mfe"] for r in rows]), 3),
        "mae_median": round(st.median([r["mae"] for r in rows]), 3),
        "reasons": {k: round(100.0 * v / n, 1) for k, v in
                    sorted(reasons.items(), key=lambda kv: -kv[1])},
    }


def main() -> int:
    from hermes_trader.agents import dsl_exit as dsl_module
    dsl_module._request_save = lambda force=False: None

    signals = _load_signals(os.environ.get(
        "CH_TRADES", "/tmp/v2/ch_trades.jsonl"))
    series = rp._load_cache(os.environ.get(
        "CH_DEEP", "/tmp/v2/ch_deep_candles.json"))
    base = rp.build_policy()
    variants = _variants(base)

    # pre-group signals by coin so bars/4h are prepared once per coin
    by_coin = {}
    for s in signals:
        by_coin.setdefault(s["coin"], []).append(s)

    # variant -> list of result rows
    results = {name: [] for name, _ in variants}
    # also keep per-channel results for baseline + the combined candidate
    chan_rows = {"baseline": {}, "protect1.0+BE1.0": {}}
    censored = 0
    done = 0
    for coin, ss in by_coin.items():
        bars = series.get(coin)
        if not bars:
            continue
        ts = [b[0] for b in bars]
        bars4 = rp.aggregate_4h(bars)
        for s in ss:
            i = bisect.bisect_left(ts, s["ts"])
            if i <= 0 or i >= len(bars) - 2:
                continue
            bucket = (bars[i][0] // (4 * 3600_000)) * (4 * 3600_000)
            i4 = next((k for k, b in enumerate(bars4) if b[0] == bucket),
                      len(bars4))
            for name, pol in variants:
                r = replay_one(bars=bars, i_entry=i, policy=pol,
                               dsl_module=dsl_module,
                               tracker_cls=dsl_module.DSLTracker)
                if r is None:
                    if name == "baseline":
                        censored += 1
                    continue
                r["channel"] = s.get("channel")
                results[name].append(r)
                if name in chan_rows:
                    chan_rows[name].setdefault(
                        s.get("channel"), []).append(r)
            done += 1

    summary = {"entries_long": done, "censored": censored,
               "variants": {}, "by_channel": {}}
    for name, _ in variants:
        summary["variants"][name] = _summ(results[name])
    for name, ch in chan_rows.items():
        summary["by_channel"][name] = {
            c: _summ(rows) for c, rows in sorted(ch.items())}

    out = os.environ.get("CH_GRID_OUT", "/tmp/v2/ch_grid_result.json")
    json.dump(summary, open(out, "w"), indent=2)
    print("WROTE_GRID", out, "entries", done, "censored", censored)
    for name, s in summary["variants"].items():
        print(f"  {name:16s} medROE={s['roe_median']:>7} mean={s['roe_mean']:>7} "
              f"win={s['win_rate']:>5}% arm={s['armed_protect_pct']:>5}% "
              f"ign={s['ignited_pct']:>5}% n={s['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
