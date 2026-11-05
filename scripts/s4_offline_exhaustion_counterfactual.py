#!/usr/bin/env python3
"""Offline, read-only reconstruction of the two candle-based S4 exhaustion
counterfactual arms from historical shadow fills + frozen 15m candles.

Faithfully mirrors shadow_book._observe_candle_exhaustion_counterfactuals:
  adx_atr_decay      : last 3 ADX and last 3 ATR all strictly decaying
  volume_stagnation  : volume >= 2x median(prev 20 volumes) and directional
                       bar move <= +10bp

Rows use the exact live schema (observer_only=True). The first observed bar
per (account, position_id, signal) is kept, matching live dedupe.
CVD / phase2-floor arms are NOT reconstructable this way and are left to live
accumulation.

This is a research tool: it is deliberately NOT in scripts/runtime_whitelist.py
and therefore never ships in the runtime image. Output is restricted to a file
directly under /tmp; it never writes /data or the shadow ledgers.

Usage (host, after copying the inputs out of the container):
    docker cp hermes-trader:/data/.shadow-book.json /tmp/shadow_book.json
    docker cp hermes-trader:/data/.binance-candles.json /tmp/binance_candles.json
    PYTHONPATH=. python3 scripts/s4_offline_exhaustion_counterfactual.py \
        --output /tmp/offline_exhaustion_cf.jsonl
    PYTHONPATH=. python3 scripts/s4_exhaustion_paired_eval.py \
        --shadow-book /tmp/shadow_book.json \
        --counterfactual /tmp/offline_exhaustion_cf.jsonl \
        --output /tmp/s4_exhaustion_offline.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

APP_ROOT = Path(os.environ.get("HERMES_APP_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(APP_ROOT))

from hermes_trader.indicators.math import adx as _calculate_adx, atr as _calculate_atr  # noqa: E402
from hermes_trader.models.types import Candle  # noqa: E402

INTERVAL_MS = 15 * 60 * 1000
WARMUP_BARS = 21


def load_bars(path: Path) -> dict[str, list[Candle]]:
    data = json.loads(path.read_text())
    raw = data.get("bars", data)
    out: dict[str, list[Candle]] = {}
    for key, series in raw.items():
        if not key.endswith("|15m"):
            continue
        coin = key.split("|")[0]
        bars = [Candle(t=int(ts), o=float(o), h=float(h), l=float(lo),
                       c=float(c), v=float(v))
                for ts, (o, h, lo, c, v) in series.items()]
        bars.sort(key=lambda b: b.t)
        out[coin] = bars
    return out


def build_positions(book: dict) -> list[dict]:
    positions = []
    for acct_name, acct in (book.get("accounts") or {}).items():
        opens: dict[str, dict] = {}
        for fill in acct.get("fills", []):
            pid = fill.get("position_id")
            if not pid:
                continue
            if fill.get("type") == "open":
                opens[str(pid)] = fill
            elif fill.get("type") == "close" and str(pid) in opens:
                entry = opens[str(pid)]
                positions.append({
                    "account": str(acct_name),
                    "position_id": str(pid),
                    "coin": fill.get("coin") or entry.get("coin"),
                    "side": str(fill.get("side") or entry.get("side") or "").lower(),
                    "open_ts": int(entry.get("ts") or 0),
                    "close_ts": int(fill.get("ts") or 0),
                })
    return positions


def observe(bars: list[Candle], open_ts: int, close_ts: int, side: str,
            account: str, position_id: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    start = max(WARMUP_BARS - 1, 0)
    end = len(bars) - 1
    # first index whose bar closes after the position opened
    for i, b in enumerate(bars):
        if b.t + INTERVAL_MS > open_ts:
            start = max(start, i)
            break
    for i in range(start, end + 1):
        bar = bars[i]
        if bar.t + INTERVAL_MS > close_ts:
            break
        window = bars[: i + 1]
        if len(window) < WARMUP_BARS:
            continue

        if "adx_atr_decay" not in seen:
            adx_values = _calculate_adx(window)
            atr_values = _calculate_atr(window)
            adx_tail = adx_values[-3:]
            atr_tail = atr_values[-3:]
            if (all(v is not None and math.isfinite(v) for v in adx_tail + atr_tail)
                    and adx_tail[0] > adx_tail[1] > adx_tail[2]
                    and atr_tail[0] > atr_tail[1] > atr_tail[2]):
                seen.add("adx_atr_decay")
                rows.append({
                    "ts": bar.t + INTERVAL_MS,
                    "virtual_exit_ts": bar.t + INTERVAL_MS,
                    "account": account,
                    "position_id": position_id,
                    "signal": "adx_atr_decay",
                    "mid": float(bar.c),
                    "reason": "s4_observer:adx_atr_decay",
                    "evidence": {
                        "interval": "15m",
                        "adx_last_3": adx_tail,
                        "atr_last_3": atr_tail,
                        "rule": "three consecutive strictly decaying closed-bar values",
                        "provenance": "offline_reconstructed_from_historical_15m_candles",
                    },
                    "observer_only": True,
                })

        if "volume_stagnation" not in seen:
            baseline = [float(c.v) for c in window[-21:-1]
                        if getattr(c, "v", None) is not None and float(c.v) >= 0]
            if len(baseline) == 20 and bar.o > 0:
                baseline_median = statistics.median(baseline)
                direction = -1 if side == "short" else 1
                directional_bar_bp = direction * (bar.c - bar.o) / bar.o * 10_000.0
                if (baseline_median > 0 and bar.v >= 2.0 * baseline_median
                        and directional_bar_bp <= 10.0):
                    seen.add("volume_stagnation")
                    rows.append({
                        "ts": bar.t + INTERVAL_MS,
                        "virtual_exit_ts": bar.t + INTERVAL_MS,
                        "account": account,
                        "position_id": position_id,
                        "signal": "volume_stagnation",
                        "mid": float(bar.c),
                        "reason": "s4_observer:volume_stagnation",
                        "evidence": {
                            "interval": "15m",
                            "volume": bar.v,
                            "baseline_20_median_volume": baseline_median,
                            "volume_multiple": round(bar.v / baseline_median, 6),
                            "directional_bar_bp": round(directional_bar_bp, 6),
                            "rule": "volume>=2x median20 and directional bar<=10bp",
                            "provenance": "offline_reconstructed_from_historical_15m_candles",
                        },
                        "observer_only": True,
                    })
        if len(seen) == 2:
            break
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--shadow-book", type=Path, default=Path("/tmp/shadow_book.json"))
    p.add_argument("--candles", type=Path, default=Path("/tmp/binance_candles.json"))
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    out = args.output.resolve()
    if out.parent != Path("/tmp"):
        p.error("--output must be a file directly under /tmp")

    bars_by_coin = load_bars(args.candles)
    book = json.loads(args.shadow_book.read_text())
    positions = build_positions(book)

    rows: list[dict] = []
    skipped_no_coin = 0
    skipped_no_window = 0
    for pos in positions:
        bars = bars_by_coin.get(str(pos["coin"]).upper())
        if not bars:
            skipped_no_coin += 1
            continue
        if pos["close_ts"] <= pos["open_ts"] or pos["open_ts"] <= 0:
            skipped_no_window += 1
            continue
        rows.extend(observe(bars, pos["open_ts"], pos["close_ts"],
                            pos["side"], pos["account"], pos["position_id"]))

    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    os.replace(tmp, out)

    print(json.dumps({
        "output": str(out),
        "positions_total": len(positions),
        "positions_with_candles": len(positions) - skipped_no_coin - skipped_no_window,
        "skipped_no_coin": skipped_no_coin,
        "skipped_no_window": skipped_no_window,
        "rows": len(rows),
        "signals": dict(Counter(r["signal"] for r in rows)),
        "accounts": dict(Counter(r["account"] for r in rows)),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
