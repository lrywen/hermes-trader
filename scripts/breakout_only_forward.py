#!/usr/bin/env python3
"""Forward-PnL analysis for breakout-only signals newly admitted by the
runner_gate fresh_impulse change (D1).

These signals were BLOCKED by the old formula (volume AND (breakout OR burst))
but are ADMITTED by the new formula (breakout alone qualifies). They never
traded live, so we reconstruct entry/exit from Hyperliquid candles:

  entry  = open of the first 5m bar at/after the gate decision
  stop   = entry - 1.5 * ATR14(4h)   (server.py default bracket)
  target = entry + 1.0 * ATR14(4h)   (server.py default bracket)
  horizon = 6h (72 x 5m bars); intrabar stop/tp resolution, stop-first on
           same-bar touch (conservative).

Outputs per signal: coin, signal time (UTC), score, entry, stop, target,
exit reason, bars held, R multiple, and MFE/MAE in R.
"""
from __future__ import annotations
import json, os, sys, math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, "/app")

from hermes_trader.client.hl_client import _http_post  # noqa: E402

SESSION_LOG = os.environ.get("SESSION_LOG_PATH", "/data/session-log.jsonl")
MIN_SCORE = 30.0          # runner_gate min_composite
HORIZON_BARS = 72         # 6h of 5m bars
SL_MULT = 1.5             # server.py default sl_atr_mult
TP_MULT = 1.0             # server.py default tp_atr_mult
_INTERVAL_MS = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}


def fetch_candles_at(coin: str, interval: str, count: int, end_ms: int):
    step = _INTERVAL_MS[interval]
    start_ms = end_ms - step * count
    payload = {"type": "candleSnapshot",
               "req": {"coin": coin, "interval": interval,
                       "startTime": start_ms, "endTime": end_ms}}
    raw = _http_post("/info", payload)
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        try:
            out.append((int(c["t"]), float(c["o"]), float(c["h"]),
                        float(c["l"]), float(c["c"]), float(c.get("v", 0))))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def fetch_window(coin: str, interval: str, start_ms: int, end_ms: int):
    step = _INTERVAL_MS[interval]
    count = int((end_ms - start_ms) / step) + 2
    return fetch_candles_at(coin, interval, count, end_ms)


def atr14(candles) -> Optional[float]:
    if len(candles) < 15:
        return None
    trs = []
    prev_c = candles[0][4]
    for t, o, h, l, c, v in candles[1:]:
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
    # Wilder smoothing, seeded on first 14 TRs
    period = 14
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def load_newly_admitted():
    """Replay session-log; return list of LONG breakout-only signals that are
    newly admitted under the new fresh_impulse formula. Matches each execute
    event to the nearest preceding scan for that coin's triggers/score."""
    latest_scan: Dict[str, dict] = {}
    signals = []
    with open(SESSION_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = ev.get("event")
            ts = ev.get("ts") or ev.get("timestamp")
            if et == "scan" and ts:
                for cs in ev.get("coin_scores", []) or []:
                    coin = cs.get("coin")
                    if not coin:
                        continue
                    latest_scan[coin] = {
                        "ts": int(ts),
                        "score": float(cs.get("score", 0) or 0),
                        "triggers": set(cs.get("triggers", []) or []),
                    }
            elif et == "execute" and ts and ev.get("side") == "long":
                coin = ev.get("coin")
                if not coin or coin not in latest_scan:
                    continue
                sc = latest_scan[coin]
                trg = sc["triggers"]
                volume = "volumeSpike" in trg
                breakout = "breakout" in trg
                burst = "momentumBurst" in trg
                score = sc["score"]
                old = (volume and (breakout or burst)) or (burst and score >= MIN_SCORE)
                new = breakout or (volume and burst) or (burst and score >= MIN_SCORE)
                if new and not old:
                    signals.append({
                        "coin": coin,
                        "ts": int(ts),
                        "score": score,
                        "triggers": sorted(trg),
                        "executed": bool(ev.get("executed")),
                        "blocked_by": ev.get("blocked_by"),
                    })
    # Dedup: same coin within 15 min counts as one signal
    signals.sort(key=lambda s: s["ts"])
    deduped = []
    for s in signals:
        if deduped and deduped[-1]["coin"] == s["coin"] and \
                (s["ts"] - deduped[-1]["ts"]) < 900_000:
            continue
        deduped.append(s)
    return deduped


def simulate(sig):
    coin, ts = sig["coin"], sig["ts"]
    # 4h ATR as of signal time
    c4h = fetch_candles_at(coin, "4h", 50, ts)
    atr = atr14(c4h)
    if not atr or atr <= 0:
        return None
    # Forward 5m window: entry at first bar open at/after ts
    end_ms = ts + HORIZON_BARS * _INTERVAL_MS["5m"] + 60_000
    c5 = fetch_window(coin, "5m", ts, end_ms)
    forward = [b for b in c5 if b[0] >= ts]
    if len(forward) < 2:
        return None
    entry = forward[0][1]  # open of first bar
    stop = entry - SL_MULT * atr
    target = entry + TP_MULT * atr
    risk = entry - stop
    if risk <= 0:
        return None
    bars_held = 0
    mfe = 0.0
    mae = 0.0
    exit_px = None
    reason = "timeout"
    for i, (t, o, h, l, c, v) in enumerate(forward):
        bars_held = i + 1
        mfe = max(mfe, h - entry)
        mae = min(mae, l - entry)
        hit_tp = h >= target
        hit_sl = l <= stop
        if hit_sl and hit_tp:
            # conservative: assume stop first
            exit_px, reason = stop, "stop(+tp same bar)"
            break
        if hit_sl:
            exit_px, reason = stop, "stop"
            break
        if hit_tp:
            exit_px, reason = target, "target"
            break
    if exit_px is None:
        exit_px = forward[-1][4]
        reason = f"timeout@{bars_held}b"
    r_pnl = (exit_px - entry) / risk
    return {
        "entry": entry, "stop": stop, "target": target, "atr": atr,
        "exit_px": exit_px, "reason": reason, "bars_held": bars_held,
        "r_pnl": r_pnl, "mfe_r": mfe / risk, "mae_r": mae / risk,
    }


def main():
    sigs = load_newly_admitted()
    print(f"Found {len(sigs)} newly-admitted breakout-only LONG signals "
          f"(after 15m dedup)\n")
    rows = []
    for sig in sigs:
        try:
            res = simulate(sig)
        except Exception as e:
            print(f"  {sig['coin']} error: {e}", file=sys.stderr)
            res = None
        if res is None:
            print(f"  {sig['coin']} @ {utc(sig['ts'])} — skipped (no data)")
            continue
        rows.append((sig, res))

    print(f"{'coin':<10} {'time(UTC)':<16} {'score':>5} {'entry':>10} "
          f"{'R':>7} {'MFE_R':>6} {'MAE_R':>6} {'bars':>4}  {'outcome'}")
    print("-" * 95)
    wins = losses = 0
    sum_r = 0.0
    for sig, r in rows:
        outcome = r["reason"]
        if r["r_pnl"] > 0:
            wins += 1
        elif r["r_pnl"] < 0:
            losses += 1
        sum_r += r["r_pnl"]
        print(f"{sig['coin']:<10} {utc(sig['ts']):<16} {sig['score']:5.0f} "
              f"{r['entry']:10.5f} {r['r_pnl']:+7.2f} {r['mfe_r']:+6.2f} "
              f"{r['mae_r']:+6.2f} {r['bars_held']:4d}  {outcome}")
    n = len(rows)
    print("-" * 95)
    if n:
        wr = wins / n * 100
        print(f"\nSimulated: {n} signals | wins={wins} losses={losses} "
              f"winrate={wr:.0f}% | total_R={sum_r:+.2f} | avg_R={sum_r/n:+.2f}")
        # By reason breakdown
        by_reason = {}
        for _, r in rows:
            key = r["reason"].split("@")[0].split("(")[0]
            by_reason.setdefault(key, []).append(r["r_pnl"])
        print("\nExit breakdown:")
        for k, vals in sorted(by_reason.items()):
            print(f"  {k:<10} n={len(vals):<3} sum_R={sum(vals):+.2f} "
                  f"avg_R={sum(vals)/len(vals):+.2f}")
    # dump json for report
    out = "/tmp/breakout_forward_results.json"
    with open(out, "w") as f:
        json.dump([{"coin": s["coin"], "ts": s["ts"], "time_utc": utc(s["ts"]),
                    "score": s["score"], "triggers": s["triggers"], **r}
                   for s, r in rows], f, indent=2)
    print(f"\nJSON written to {out}")


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


if __name__ == "__main__":
    main()
