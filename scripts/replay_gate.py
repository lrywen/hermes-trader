#!/usr/bin/env python3
"""M1.5 (plan G1): offline REPLAY of the REAL runner-entry gate over historical
candles, producing schema-compatible decision rows tagged replay=true.

Why: live runner_gate_shadow.jsonl only retains ~90 daily siblings going forward
and real verdicts exist only from 2026-10-24. To accrue multi-month samples we
reconstruct the gate's structural inputs from closed candles and call the SAME
production function executor._runner_entry_block_reason_impl (not a rewrite).

What IS reconstructed from candles (deterministic):
  build_hits triggers + composite_score, each *_fired flag, momentum_burst_dir,
  slow_burn_count = #fired of {volumeBuildup1h,trendFlip1h,higherLows1h},
  breakout_onset_ms, 4h rsi/adx/ema8/ema21/atr/close for the late-entry screen,
  mid/mid_price (=closed 5m close), daily_mover via 24h close-to-close + volume.

What is NOT knowable from candles and is therefore NEUTRALIZED + TAGGED:
  * AI confidence (LLM output) -> set to pass; rows carry conf_neutralized=true
    and conf_assumed. The replay therefore measures the STRUCTURAL gate
    (fresh-impulse / score / slow-burn / daily-mover / late-entry / weak-trend),
    not whether the model's conviction would have cleared its own bar.
  * whale signal, binary news, account/exposure/cooldown state -> absent/False.

Replay rows are evidence-grade below live verdicts and must be calibrated
against the real 2026-10-24+ subset before use. Read-only; never orders.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import time
from typing import Any, Optional

from hermes_trader.agents import executor as ex
from hermes_trader.agents.config import (
    trigger_thresholds_params, trigger_weights_params)
from hermes_trader.indicators import triggers as T
from hermes_trader.indicators import math as M

BAR5 = 5 * 60_000

# Liquid Hermes-universe coins that also trade on Binance USDT spot (offline
# Binance deep-history replay only). HIP-3/k-prefix/exclusive/delisted names
# omitted: no 1:1 Binance symbol.
_DEFAULT_BASKET = [
    "AAVE","ADA","AERO","ASTER","AVAX","BTC","CHIP","CRV","ENA","ETH",
    "ETHFI","GRAM","HEMI","HYPE","INIT","INJ","JTO","JUP","LINK","LTC",
    "NEAR","ONDO","PENGU","PUMP","PYTH","REZ","SOL","SUI","TAO","TRUMP",
    "UNI","WLD","XPL","ZEC","ZRO","ALT","ANIME","BABY","BIGTIME","BLUR",
    "COMP","DASH","GMX","MEGA","NIL","SOPH","TIA","WCT"]
_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
_SLOW_BURN = {"volumeBuildup1h", "trendFlip1h", "higherLows1h"}


def _candles(rows: list[tuple[int, list[float]]]):
    # Return the real Candle model (triggers access .c/.h attributes, not dict keys).
    from hermes_trader.models.types import Candle
    out = []
    for t, v in rows:
        try:
            o, h, l, c, vol = (float(v[0]), float(v[1]), float(v[2]),
                               float(v[3]), float(v[4]))
        except Exception:
            continue
        if all(math.isfinite(x) for x in (o, h, l, c, vol)) and h >= l and c > 0:
            out.append(Candle(t=int(t), o=o, h=h, l=l, c=c, v=vol))
    return out


class Data:
    def __init__(self, cache_path: str = "", use_network: bool = True,
                 source: str = "hyperliquid"):
        self.source = source
        self.cache: dict[str, Any] = {}
        if cache_path and source == "hyperliquid" and os.path.exists(cache_path):
            try:
                self.cache = json.load(open(cache_path))
            except Exception:
                self.cache = {}
        self.use_network = use_network
        self._mem: dict[str, Optional[list]] = {}

    def candles(self, coin: str, interval: str,
                start_ms: int, end_ms: int):
        key = f"{coin}|{interval}"
        if key not in self._mem:
            series = None
            if self.source == "binance":
                if self.use_network:
                    try:
                        from hermes_trader.data import binance_candles as B
                        if B.to_binance_symbol(coin) is None:
                            series = None  # HIP-3 / no 1:1 symbol
                        else:
                            series = list(B.fetch_candle_range(
                                coin, interval, start_ms, end_ms,
                                use_cache=True))
                    except Exception:
                        series = None
            else:
                d = self.cache.get(key)
                if d:
                    series = _candles(sorted((int(t), v) for t, v in d.items()))
                elif self.use_network:
                    try:
                        from hermes_trader.data.historical_candles import fetch_candle_range
                        series = list(fetch_candle_range(
                            coin, interval, start_ms, end_ms, use_cache=True))
                    except Exception:
                        series = None
            self._mem[key] = series
        s = self._mem[key]
        if not s:
            return None
        return [c for c in s if start_ms <= c.t <= end_ms]

    def quote_volume_24h(self, coin: str, t5_ms: int) -> Optional[float]:
        """Trailing-24h USDT notional ending at t5_ms (binance only)."""
        if self.source != "binance":
            return None
        try:
            from hermes_trader.data import binance_candles as B
            tot = 0.0
            found = False
            for k in range(288):
                qv = B.quote_volume(coin, "5m", t5_ms - k * BAR5)
                if qv is not None:
                    tot += qv
                    found = True
            return tot if found else None
        except Exception:
            return None


def _resample_4h(c5) -> list:
    """Aggregate 5m closed candles into 4h bars aligned to epoch grid."""
    from hermes_trader.models.types import Candle
    grid = 4 * 60 * 60_000
    buckets: dict[int, dict] = {}
    order = []
    for c in c5:
        g = c.t - (c.t % grid)
        b = buckets.get(g)
        if b is None:
            b = {"t": g, "o": c.o, "h": c.h, "l": c.l, "c": c.c, "v": 0.0}
            buckets[g] = b
            order.append(g)
        else:
            b["h"] = max(b["h"], c.h)
            b["l"] = min(b["l"], c.l)
            b["c"] = c.c
            b["v"] += c.v
    # first bar of a grid carries its open; add volume of current candle
    for c in c5:
        buckets[c.t - (c.t % grid)]["v"] += c.v
    return [Candle(t=buckets[g]["t"], o=buckets[g]["o"], h=buckets[g]["h"],
                   l=buckets[g]["l"], c=buckets[g]["c"], v=buckets[g]["v"])
            for g in sorted(buckets)]


def build_analysis(data: Data, coin: str, i5: int, c5: list,
                   c15: list, c1h: list,
                   thresholds: dict, weights: dict,
                   mover_cfg: dict) -> Optional[dict]:
    """Reconstruct the runner-gate analysis dict at closed 5m bar index i5."""
    win5 = c5[max(0, i5 - 499):i5 + 1]
    if len(win5) < 200:
        return None
    now_t = c5[i5].t
    # PIT: higher-timeframe bars must be CLOSED as of this 5m bar, else the 1h
    # slow-burn triggers leak future bars (and dilute firing at the live bar).
    c1h_p = [c for c in c1h if c.t + 3_600_000 <= now_t][-200:]
    c15_p = [c for c in c15 if c.t + 900_000 <= now_t][-200:]
    hits, sw = T.build_hits(win5, thresholds, weights,
                            candles_1h=c1h_p, candles_15m=c15_p)
    fired = {h["name"]: h for h in hits if h.get("fired")}
    score = T.composite_score(hits, sw)

    def f(name):
        return name in fired

    burst_hit = fired.get("momentumBurst") or {}
    burst_dir = str(burst_hit.get("dir") or burst_hit.get("direction") or "")
    slow_count = sum(1 for n in _SLOW_BURN if n in fired)

    # breakout onset: this bar's timestamp if breakout fires (replay samples at
    # closed bars; recent-breakout window handled by the gate only if onset set).
    breakout_onset_ms = win5[-1].t if f("breakout") else None

    # 4h late-entry screen features from resampled closed bars
    c4 = _resample_4h(win5)
    rsi4 = adx4 = ema8_4 = ema21_4 = atr4 = close4 = None
    if len(c4) >= 40:
        try:
            rsi4 = float(M.rsi([b.c for b in c4], 14)[-1])
            adx4 = float(M.adx(c4, 14)[-1])
            ema8_4 = float(M.ema([b.c for b in c4], 8)[-1])
            ema21_4 = float(M.ema([b.c for b in c4], 21)[-1])
            atr4 = float(M.atr(c4, 14)[-1])
            close4 = float(c4[-1].c)
        except Exception:
            pass

    # daily mover: 24h (288 x 5m) closed-to-close % and trailing-day notional
    daily_mover_fired = False
    if i5 - 288 >= 0 and mover_cfg.get("enabled", False):
        prev = c5[i5 - 288].c
        cur = c5[i5].c
        qv24 = data.quote_volume_24h(coin, c5[i5].t)
        vol24 = (qv24 if qv24 is not None
                 else sum(c.v * c.c for c in c5[i5 - 288:i5 + 1]))
        move = (cur - prev) / prev * 100 if prev > 0 else 0.0
        if (abs(move) >= float(mover_cfg.get("min_crypto_24h_pct", 10.0))
                and vol24 >= float(mover_cfg.get("min_volume_usd", 5e6))):
            daily_mover_fired = True

    mid = float(c5[i5].c)
    return {
        "coin": coin, "side": "long",
        "composite_score": float(score),
        "confidence": None, "ai_confidence_raw": None,  # neutralized below
        "volume_spike_fired": f("volumeSpike"),
        "breakout_fired": f("breakout"),
        "breakout_onset_ms": breakout_onset_ms,
        "momentum_burst_fired": f("momentumBurst"),
        "momentum_burst_dir": burst_dir,
        "daily_mover_fired": daily_mover_fired,
        "uptrend_momentum_fired": f("uptrendMomentum"),
        "downtrend_momentum_fired": f("downtrendMomentum"),
        "slow_burn_count": slow_count,
        "whale_signal": False,
        "reasoning": "",
        "rsi4h": rsi4, "adx4h": adx4, "ema8_4h": ema8_4,
        "ema21_4h": ema21_4, "atr4h": atr4, "close4h": close4,
        "mid": mid, "mid_price": mid, "price": mid,
        "_fired_names": sorted(fired.keys()),
    }


def replay_coin(data: Data, coin: str, start_ms: int, end_ms: int,
                step_bars: int, thresholds, weights, config: dict,
                conf_assumed: float) -> list[dict]:
    c5 = data.candles(coin, "5m", start_ms, end_ms)
    if not c5 or len(c5) < 600:
        return []
    c15 = data.candles(coin, "15m", start_ms, end_ms) or []
    c1h = data.candles(coin, "1h", start_ms, end_ms) or []
    mover_cfg = (config.get("runner_mover_surface") or {})
    rows = []
    for i in range(500, len(c5) - 1, step_bars):
        an = build_analysis(data, coin, i, c5, c15, c1h,
                            thresholds, weights, mover_cfg)
        if an is None:
            continue
        # neutralize AI confidence (not candle-knowable)
        an["confidence"] = conf_assumed
        an["ai_confidence_raw"] = conf_assumed
        # call the REAL production gate core
        try:
            decision = ex._runner_entry_block_reason_impl(an, config)
        except Exception as e:  # a replay failure must not abort the batch
            decision = f"replay_error:{type(e).__name__}"
        admitted = (decision == "")
        feats = getattr(ex._runner_gate_ctx, "feats", None) or {}
        rows.append({
            "ts": c5[i].t, "coin": coin, "side": "long",
            "admitted": admitted, "rule": "" if admitted else str(decision),
            "composite_score": round(an["composite_score"], 3),
            "min_composite": float((config.get("runner_entry_gate") or {})
                                   .get("min_composite", 45)),
            "passes40": an["composite_score"] >= 40,
            "passes45": an["composite_score"] >= 45,
            "confidence": conf_assumed, "conf_neutralized": True,
            "conf_assumed": conf_assumed,
            "slow_burn_count": an["slow_burn_count"],
            "fresh_impulse": bool(feats.get("fresh_impulse")),
            "fresh_src": feats.get("fresh_src", ""),
            "runner_carrier": feats.get("runner_carrier", ""),
            "vol": an["volume_spike_fired"], "brk": an["breakout_fired"],
            "burst": an["momentum_burst_fired"],
            "dMover": an["daily_mover_fired"],
            "up": an["uptrend_momentum_fired"],
            "down": an["downtrend_momentum_fired"],
            "whale": False, "forced": False, "is_hip3": ":" in coin,
            "mid_price": an["mid"],
            "fired_names": an["_fired_names"],
            "replay": True,
            "data_source": getattr(data, "source", "hyperliquid"),
            "replay_note": ("structural gate from candles; AI confidence "
                            "neutralized; whale/news/account absent"),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(_DATA_DIR, ".agent-config.json"))
    ap.add_argument("--cache", default=os.path.join(_DATA_DIR, "ch_hist_candles.json"))
    ap.add_argument("--coins", default="", help="comma list; default = cache coins")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--step-bars", type=int, default=10)
    ap.add_argument("--conf", type=float, default=0.9)
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--source", choices=["hyperliquid", "binance"],
                    default="hyperliquid")
    ap.add_argument("--out", default=os.path.join(_DATA_DIR, "decision_shadow_replay.jsonl"))
    ap.add_argument("--max-coins", type=int, default=0)
    args = ap.parse_args()

    config = json.load(open(args.config))
    thresholds = trigger_thresholds_params(config=config)
    weights = trigger_weights_params(config=config)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86_400_000
    data = Data(args.cache, use_network=not args.no_network,
                source=args.source)

    if args.coins:
        coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    elif args.source == "binance":
        # Binance has no local cache seed; default to the liquid shared basket.
        coins = _DEFAULT_BASKET
    else:
        coins = sorted({k[:-3] for k in data.cache if k.endswith("|5m")})
    if args.max_coins:
        coins = coins[:args.max_coins]

    n = 0
    if args.source == "binance":
        try:
            from hermes_trader.data import binance_candles as _B
        except Exception:
            _B = None
    tmp_out = args.out + ".tmp"
    with open(tmp_out, "w") as f:
        for ci, coin in enumerate(coins):
            rows = replay_coin(data, coin, start_ms, end_ms, args.step_bars,
                               thresholds, weights, config, args.conf)
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + chr(10))
            n += len(rows)
            print(f"[{ci+1}/{len(coins)}] {coin}: {len(rows)}", flush=True)
    if args.source == "binance" and _B is not None:
        _B.flush_disk()
    os.replace(tmp_out, args.out)
    adm = 0
    all_ts = []
    for line in open(args.out):
        r = json.loads(line)
        if r.get("admitted"):
            adm += 1
        all_ts.append(r["ts"])
    span = None
    if all_ts:
        span_days = (max(all_ts) - min(all_ts)) / 86_400_000
        _notes = {
            "hyperliquid": ("HL serves 5m candles only back ~17d (hard "
                            "exchange retention)."),
            "binance": ("Binance public spot klines (offline-only source; "
                        "never used by live trading). Symbols without a 1:1 "
                        "USDT spot listing (HIP-3/k-prefix/delisted) omitted."),
        }
        span = {"requested_days": args.days,
                "actual_first_utc": time.strftime(
                    "%Y-%m-%d", time.gmtime(min(all_ts) / 1000)),
                "actual_last_utc": time.strftime(
                    "%Y-%m-%d", time.gmtime(max(all_ts) / 1000)),
                "actual_span_days": round(span_days, 1),
                "base_interval": "5m", "data_source": args.source,
                "note": _notes.get(args.source, "")}
        meta_out = args.out.replace(".jsonl", "_meta.json")
        json.dump({"rows": n, "coins": len(coins), "admitted": adm,
                   "span": span}, open(meta_out, "w"), indent=2)
    if n:
        print(f"replay rows={n} admitted={adm} ({100*adm/n:.2f}%) "
              f"coins={len(coins)} span={span['actual_span_days']}d "
              f"-> {args.out}")
    else:
        print("replay rows=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
