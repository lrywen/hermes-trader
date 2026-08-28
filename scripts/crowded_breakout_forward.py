#!/usr/bin/env python3
"""E2c: Forward-PnL for breakout signals blocked by the with-crowd
LONG_CROWDED veto (confidence < 0.80), to quantify the cost of that gate."""
from __future__ import annotations
import json, os, sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "/app")
from hermes_trader.client.hl_client import _http_post  # noqa

SESSION_LOG = os.environ.get("SESSION_LOG_PATH", "/data/session-log.jsonl")
HORIZON_BARS = 72
SL_MULT, TP_MULT = 1.5, 1.0
IV = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}


def fetch_candles_at(coin, interval, count, end_ms):
    step = IV[interval]
    payload = {"type": "candleSnapshot",
               "req": {"coin": coin, "interval": interval,
                       "startTime": end_ms - step * count, "endTime": end_ms}}
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


def fetch_window(coin, interval, start_ms, end_ms):
    step = IV[interval]
    count = int((end_ms - start_ms) / step) + 2
    return fetch_candles_at(coin, interval, count, end_ms)


def atr14(candles):
    if len(candles) < 15:
        return None
    trs, prev_c = [], candles[0][4]
    for t, o, h, l, c, v in candles[1:]:
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        prev_c = c
    if len(trs) < 14:
        return None
    a = sum(trs[:14]) / 14
    for tr in trs[14:]:
        a = (a * 13 + tr) / 14
    return a


def simulate(coin, ts):
    atr = atr14(fetch_candles_at(coin, "4h", 50, ts))
    if not atr or atr <= 0:
        return None
    c5 = [b for b in fetch_window(coin, "5m", ts, ts + HORIZON_BARS*IV["5m"])
          if b[0] >= ts]
    if len(c5) < 2:
        return None
    entry = c5[0][1]
    stop, target = entry - SL_MULT*atr, entry + TP_MULT*atr
    risk = entry - stop
    if risk <= 0:
        return None
    bars, mfe, mae = 0, 0.0, 0.0
    exit_px, reason = None, "timeout"
    for i, (t, o, h, l, c, v) in enumerate(c5):
        bars = i + 1
        mfe = max(mfe, h - entry); mae = min(mae, l - entry)
        if l <= stop:
            exit_px, reason = stop, "stop"; break
        if h >= target:
            exit_px, reason = target, "target"; break
    if exit_px is None:
        exit_px = c5[-1][4]
    return {"atr": atr, "entry": entry, "exit": exit_px, "reason": reason,
            "r": (exit_px-entry)/risk, "mfe": mfe/risk, "mae": mae/risk,
            "bars": bars}


def main():
    latest_scan, latest_research = {}, {}
    crowded_breakouts = []
    with open(SESSION_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            et, ts = ev.get("event"), ev.get("ts") or ev.get("timestamp")
            if not ts:
                continue
            if et == "scan":
                for cs in ev.get("coin_scores", []) or []:
                    c = cs.get("coin")
                    if c:
                        latest_scan[c] = set(cs.get("triggers", []) or [])
            elif et == "research":
                latest_research[ev.get("coin")] = {
                    "conf": float(ev.get("confidence", 0) or 0),
                    "verdict": ev.get("verdict")}
            elif et == "execute" and ev.get("side") == "long":
                c = ev.get("coin")
                if not c or c not in latest_scan:
                    continue
                bb = ev.get("blocked_by")
                bb = "; ".join(bb) if isinstance(bb, list) else str(bb or "")
                if "LONG_CROWDED" not in bb.upper():
                    continue
                trg = latest_scan[c]
                if "breakout" not in trg:
                    continue
                # dedup 15m
                if crowded_breakouts and crowded_breakouts[-1]["coin"] == c \
                        and ts - crowded_breakouts[-1]["ts"] < 900_000:
                    continue
                crowded_breakouts.append({
                    "coin": c, "ts": ts,
                    "conf": latest_research.get(c, {}).get("conf", 0.0),
                    "verdict": latest_research.get(c, {}).get("verdict", "?")})

    print(f"Crowded-breakout signals blocked by LONG_CROWDED veto: "
          f"{len(crowded_breakouts)}\n")
    confs = [s["conf"] for s in crowded_breakouts]
    if confs:
        confs.sort()
        print(f"confidence: min={confs[0]:.2f} med={confs[len(confs)//2]:.2f} "
              f"max={confs[-1]:.2f}  (gate requires >= 0.80)")
        print(f"  in [0.70,0.80): {sum(1 for x in confs if 0.70 <= x < 0.80)}")
        print(f"  in [0.75,0.80): {sum(1 for x in confs if 0.75 <= x < 0.80)}")
    print()

    print(f"{'coin':<10} {'time(UTC)':<12} {'conf':>5} {'verdict':<8} "
          f"{'R':>7} {'MFE':>6} {'MAE':>6} {'bars':>4}  out")
    print("-" * 80)
    rows = []
    for s in crowded_breakouts:
        try:
            r = simulate(s["coin"], s["ts"])
        except Exception as e:
            print(f"  {s['coin']} error: {e}", file=sys.stderr); continue
        if not r:
            print(f"  {s['coin']} no data"); continue
        rows.append((s, r))
        t = datetime.fromtimestamp(s["ts"]/1000, tz=timezone.utc).strftime("%m-%d %H:%M")
        print(f"{s['coin']:<10} {t:<12} {s['conf']:5.2f} {str(s['verdict'])[:8]:<8} "
              f"{r['r']:+7.2f} {r['mfe']:+6.2f} {r['mae']:+6.2f} "
              f"{r['bars']:4d}  {r['reason']}")

    n = len(rows)
    if n:
        w = sum(1 for _, r in rows if r["r"] > 0)
        tot = sum(r["r"] for _, r in rows)
        print("-" * 80)
        print(f"\nn={n} wins={w} winrate={w/n*100:.0f}% total_R={tot:+.2f} "
              f"avg_R={tot/n:+.2f}")
        # subset conf >= 0.70 (already > runner min_conf 0.70)
        sub = [(s, r) for s, r in rows if s["conf"] >= 0.70]
        if sub:
            w2 = sum(1 for _, r in sub if r["r"] > 0)
            t2 = sum(r["r"] for _, r in sub)
            print(f"sub conf>=0.70: n={len(sub)} winrate={w2/len(sub)*100:.0f}% "
                  f"total_R={t2:+.2f} avg_R={t2/len(sub):+.2f}")


if __name__ == "__main__":
    main()
