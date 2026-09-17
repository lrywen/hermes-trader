#!/usr/bin/env python3
"""5m-resolution short re-enable backtest — production signal chain replay.

Supersedes the 1h proxy (backtest_short_regime.py): the live perception scan
runs on 5m bars (scan.candleInterval="5m", candleCount=100), so the intraday
volume/momentum edge the desk trades is INVISIBLE on 1h aggregates. This
script replays the EXACT production trigger stack on closed 5m bars:

  - the 12 triggers from hermes_trader.indicators.triggers, called with the
    live thresholds (trigger_thresholds_params) and weighted with the live
    camelCase weights (trigger_weights_params) — same dicts perception injects
  - squeeze-breakout coupling (perception._apply_squeeze_breakout_coupling)
  - candlestick reversal patterns (live: enabled)
  - surfacing rule: score >= minCompositeScore (54) OR momentumBurst OR
    trend bypass (downtrendMomentum fired AND own-1h regime != "chop",
    classified with market_regime.classify_candles on the last 48 closed 1h
    bars — the same window the scan fetches) OR bearishReversalCandle
  - SHORT direction proxy (the LLM verdict is not replayable): a candidate
    needs bearish directional evidence = downtrendMomentum fired OR
    bearishReversalCandle fired OR a DOWN momentumBurst
  - live ta_late_entry short-mirror hard gate on closed 4h bars
    (mtf_enabled=false in live config -> the 15m sub-check is inert, so
    candles_15m=None is behaviourally identical to production)

Variants (ALL already enforce surfacing + ta_late):
  A  baseline     : production 5m short candidate
  B  + own trend  : the coin's own 4h is in a downtrend (EMA8 < EMA21)
  C  + macro down : BTC macro regime == "down" (production trend_from_closes
                    math with the live regime_classifier params)
  D  + both       : B AND C
  E  + composite  : D with composite >= MIN_SCORE, swept over SCAN_SCORES to
                    calibrate runner_entry_gate.min_short_composite.

Anti-look-ahead: decisions on 5m bar i close, fill at bar i+1 open; 1h/4h
series sliced to bars fully closed by the decision instant; macro regime uses
only BTC 1h bars closed by then.

Performance: full trigger stack only runs on bars passing a strict superset
pre-filter of the directional triggers (72-bar move <= -3.5% OR 2-bar move
<= -2.4% OR 6-bar advance >= +0.9% into the bar). Every bar that could
produce a short candidate passes the filter (margins are 60-70% of the live
thresholds), so results are exact, just faster.

Caveats: AI verdict / min_short_confidence (LLM outputs) CANNOT be replayed —
the composite stands in for min_short_composite only; $50M volume floor uses
TODAY's dayNtlVlm snapshot (survivorship bias); one open position per coin;
equity held constant; DSL modelled with the base 3-param ladder.

Usage:
    HERMES_AGENT_CONFIG_FILE=/tmp/hermes-agent-config.json \
        python3 scripts/backtest_short_5m.py --days 180 --coins 7
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

os.environ["HERMES_BACKTEST"] = "1"

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            # Never let .env.local inject a live signing key into a backtest.
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

import backtest as bt  # noqa: E402  (Trade/DSL/cost constants/_split_metrics/...)
from hermes_trader.agents.config_store import cfg_get, read_agent_config  # noqa: E402
from hermes_trader.agents.market_regime import classify_candles  # noqa: E402
from hermes_trader.agents.ta_filter import late_entry_check  # noqa: E402
from hermes_trader.client.universe import get_universe  # noqa: E402
from hermes_trader.data import historical_candles as hc  # noqa: E402
from hermes_trader.indicators import math as ind  # noqa: E402
from hermes_trader.indicators import triggers as trig  # noqa: E402
from hermes_trader.models.types import Candle  # noqa: E402

SCAN_SCORES = (20.0, 25.0, 30.0, 40.0)  # variant-E min_short_composite sweep
W4_WINDOW = 60     # trailing 4h suffix fed to late_entry_check
H1_WINDOW = 48     # production scan fetches 48 1h bars (slow-burn + chop)
CHUNK_MS = 60 * 86_400_000   # fetch_candle_range caps at 20k bars/request

# --- Binance spot klines (data-api.binance.vision) -------------------------
# HyperLiquid only retains ~5000 5m bars (~17 days). Binance spot serves the
# full history, enabling a true 180d replay of the production 5m chain.
# Price basis differs slightly from HL perp (spot vs perp) — acceptable for
# a signal-shape replay; noted in the caveats.
_BINANCE_BASE = "https://data-api.binance.vision"
_BN_CACHE: Dict[str, List[list]] = {}
_BN_CACHE_PATH: Optional[str] = None
_BN_SYMBOL_MEMO: Dict[str, Optional[str]] = {}


def _bn_cache_load(path: str) -> None:
    global _BN_CACHE, _BN_CACHE_PATH
    _BN_CACHE_PATH = path
    try:
        _BN_CACHE = json.loads(Path(path).read_text())
    except Exception:
        _BN_CACHE = {}


def _bn_cache_flush() -> None:
    if not _BN_CACHE_PATH:
        return
    try:
        Path(_BN_CACHE_PATH).write_text(json.dumps(_BN_CACHE))
    except Exception:
        pass


def _binance_klines(symbol: str, interval: str, start_ms: int,
                    end_ms: int) -> Optional[List[list]]:
    """Paginated klines; None if Binance spot doesn't list the symbol."""
    out: List[list] = []
    step = hc._interval_ms(interval)
    t = start_ms
    while t <= end_ms:
        qs = urllib.parse.urlencode({
            "symbol": symbol, "interval": interval,
            "startTime": t, "endTime": end_ms, "limit": 1000})
        req = urllib.request.Request(
            f"{_BINANCE_BASE}/api/v3/klines?{qs}",
            headers={"User-Agent": "hermes-backtest"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return None  # invalid symbol
            raise
        if not data:
            break
        out.extend(data)
        if len(data) < 1000:
            break
        t = int(data[-1][0]) + step
        time.sleep(0.12)
    return out


def _binance_symbol(coin: str) -> Optional[str]:
    """Map an HL perp coin to a Binance spot symbol (kPEPE -> PEPEUSDT)."""
    if coin in _BN_SYMBOL_MEMO:
        return _BN_SYMBOL_MEMO[coin]
    tries = [f"{coin}USDT"]
    if coin.startswith("k") and len(coin) > 1:
        tries.append(f"{coin[1:]}USDT")
    found = None
    for s in tries:
        qs = urllib.parse.urlencode({"symbol": s, "interval": "5m", "limit": 1})
        req = urllib.request.Request(
            f"{_BINANCE_BASE}/api/v3/klines?{qs}",
            headers={"User-Agent": "hermes-backtest"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if json.loads(r.read().decode()):
                    found = s
                    break
        except Exception:
            pass
        time.sleep(0.05)
    _BN_SYMBOL_MEMO[coin] = found
    return found


def _fetch_span_binance(coin: str, interval: str, start_ms: int,
                        end_ms: int) -> List[Candle]:
    """Binance spot klines with a persistent disk cache; [] if not listed."""
    symbol = _binance_symbol(coin)
    if symbol is None:
        return []
    step = hc._interval_ms(interval)
    key = f"{symbol}:{interval}"
    cached = _BN_CACHE.get(key) or []
    if cached and cached[0][0] <= start_ms and cached[-1][0] >= end_ms - step:
        rows = [r for r in cached if start_ms <= r[0] <= end_ms]
    else:
        raw = _binance_klines(symbol, interval, start_ms, end_ms) or []
        rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                 float(k[4]), float(k[5])] for k in raw]
        merged = {r[0]: r for r in cached}
        for r in rows:
            merged[r[0]] = r
        _BN_CACHE[key] = [merged[t] for t in sorted(merged)]
    return [Candle(t=r[0], o=r[1], h=r[2], l=r[3], c=r[4], v=r[5])
            for r in rows]


def _fetch_span(coin: str, interval: str, start_ms: int, end_ms: int) -> List[Candle]:
    """Chunked fetch_candle_range over [start_ms, end_ms] (bar-open times)."""
    step = hc._interval_ms(interval)
    out: List[Candle] = []
    t = start_ms - (start_ms % step)
    while t <= end_ms:
        out.extend(hc.fetch_candle_range(coin, interval, t,
                                         min(t + CHUNK_MS - 1, end_ms)))
        t += CHUNK_MS
    seen = set()
    deduped: List[Candle] = []
    for c in sorted(out, key=lambda c: c.t):
        if c.t not in seen:
            seen.add(c.t)
            deduped.append(c)
    return deduped


def _macro_regime_series(closes: List[float], fast_p: int, slow_p: int,
                         slope_up: float, lookback: int) -> List[str]:
    """trend_from_closes replayed at every bar index (no look-ahead)."""
    n = len(closes)
    out = ["neutral"] * n
    if n < slow_p:
        return out
    fast = ind.ema(closes, fast_p)
    slow = ind.ema(closes, slow_p)
    for i in range(n):
        if i < slow_p - 1 or i < lookback:
            continue
        f_now, s_now, f_prev = fast[i], slow[i], fast[i - lookback]
        if not all(math.isfinite(v) for v in (f_now, s_now, f_prev)):
            continue
        if f_prev == 0:
            continue
        slope = (f_now - f_prev) / abs(f_prev)
        if f_now > s_now and slope > slope_up:
            out[i] = "up"
        elif f_now < s_now and slope < -slope_up:
            out[i] = "down"
    return out


def _squeeze_coupling(hits: List[Dict[str, Any]]) -> None:
    """perception._apply_squeeze_breakout_coupling, copied verbatim."""
    squeeze_fired = any(
        (h.get("name") == "rangeCompression") and h.get("fired") for h in hits
    )
    if not squeeze_fired:
        return
    for h in hits:
        if h.get("name") == "breakout" and h.get("fired"):
            h["score"] = min(10.0, float(h.get("score", 0)) + 2.0)
            reason = h.get("reason") or ""
            if "[squeeze-resolved]" not in reason:
                h["reason"] = f"{reason} [squeeze-resolved]".strip()


def _scan_candidates(coin: str, candles: List[Candle],
                     candles_1h: List[Candle], candles_4h: List[Candle],
                     macro_close_ts: List[int], macro_regime: List[str],
                     weights: Dict[str, float], thresholds: Dict[str, Any],
                     cp: Dict[str, Any], min_score: float, candle_count: int,
                     trend_surface_enabled: bool,
                     le_params: Dict[str, Any], i0: int, sim_ms: int,
                     tm_lookback: int, tm_pct: float,
                     mom_lookback: int, mom_pct: float,
                     ctx_lookback: int, ctx_pct: float) -> tuple:
    """Pass 1 (run ONCE per coin): production 5m trigger-stack replay.

    Returns (cands, stats): every bar where the production surfacing rule
    passes AND bearish directional evidence exists AND the live ta_late short
    mirror does not veto. Records per-variant filter inputs."""
    closes = [c.c for c in candles]
    t1 = [c.t + 3_600_000 for c in candles_1h]   # 1h close times
    t4 = [c.t for c in candles_4h]
    closes4 = [c.c for c in candles_4h]
    ema8_4 = ind.ema(closes4, 8) if len(closes4) >= 8 else []
    ema21_4 = ind.ema(closes4, 21) if len(closes4) >= 21 else []

    # Strict-superset pre-filter of the directional triggers (see module doc).
    pre: List[bool] = [False] * len(candles)
    for i in range(i0, len(candles) - 1):
        c_i = closes[i]
        base72 = closes[i - tm_lookback] if i >= tm_lookback else 0.0
        if base72 and (c_i - base72) / base72 * 100 <= -tm_pct * 0.7:
            pre[i] = True
            continue
        base_m = closes[i - mom_lookback] if i >= mom_lookback else 0.0
        if base_m and (c_i - base_m) / base_m * 100 <= -mom_pct * 0.6:
            pre[i] = True
            continue
        # bearish reversal: advance over ctx_lookback bars INTO bar i
        # (ctx computed on candles[:-1] -> close[i-1] vs close[i-1-ctx_lb])
        if i >= 1 + ctx_lookback:
            base_ctx = closes[i - 1 - ctx_lookback]
            if base_ctx and (closes[i - 1] - base_ctx) / base_ctx * 100 >= ctx_pct * 0.6:
                pre[i] = True

    cands: List[Dict[str, Any]] = []
    stats = {"prefilter": 0, "fired": 0, "surfaced": 0, "directional": 0,
             "late_veto": 0}
    for i in range(i0, len(candles) - 1):
        if not pre[i]:
            continue
        stats["prefilter"] += 1
        bar = candles[i]
        decision_ms = bar.t + sim_ms
        window = candles[i + 1 - candle_count: i + 1]
        # Last H1_WINDOW 1h bars closed by the decision instant.
        j1 = bisect.bisect_right(t1, decision_ms)
        c1h = candles_1h[max(0, j1 - H1_WINDOW): j1]

        th = thresholds
        hits = [
            trig.pct_move_spike(window, th["sigmaThreshold"]),
            trig.volume_spike(window, th["sigmaThreshold"]),
            trig.breakout(
                window,
                th["breakoutLookback"],
                min_rvol=th.get("breakoutMinRvol", 1.5),
                rvol_window=th.get("breakoutRvolWindow", 20),
                atr_score_mult=th.get("breakoutAtrScoreMult", 3.0),
                confirm_bars=th.get("breakoutConfirmBars", 2),
            ),
            trig.range_compression(window, th["bbLength"], th["bbStdDev"]),
            trig.trend_strength(window, th["adxPeriod"]),
            trig.momentum_burst(window, th["momentumLookback"], th["momentumPct"]),
            trig.volume_buildup_1h(c1h, th.get("volBuildupRatio", 2.5)),
            trig.trend_flip_1h(c1h, th.get("trendFlipBars", 3)),
            trig.higher_lows_1h(c1h, th.get("higherLowsRequired", 4)),
            trig.uptrend_momentum(window, th.get("trendMomentumLookback", 72),
                                  th.get("trendMomentumPct", 5.0)),
            trig.downtrend_momentum(window, th.get("trendMomentumLookback", 72),
                                    th.get("trendMomentumPct", 5.0)),
        ]
        _squeeze_coupling(hits)
        # momentum_continuation disabled in live config -> not appended.
        if cp.get("enabled"):
            _wbr = cp.get("wick_body_ratio", 2.0)
            _ctx_lb = int(cp.get("context_lookback", 6))
            _ctx_pct = float(cp.get("context_pct", 1.5))
            hits.append(trig.bearish_reversal_candle(window, _wbr, _ctx_lb, _ctx_pct))
            hits.append(trig.bullish_reversal_candle(window, _wbr, _ctx_lb, _ctx_pct))
        # runner_mover_surface disabled in live config -> dailyMover never fires.
        hits.append({"name": "dailyMover", "score": 0,
                     "reason": "disabled in live config", "fired": False})

        if not any(h["fired"] for h in hits):
            continue
        stats["fired"] += 1
        score = trig.composite_score(hits, weights)

        burst_fired = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
        dt_fired = any(h["name"] == "downtrendMomentum" and h["fired"] for h in hits)
        bearish_pat = bool(cp.get("enabled")) and any(
            h["name"] == "bearishReversalCandle" and h["fired"] for h in hits)
        # Chop suppression on the trend bypass (production classify_candles).
        trend_chop = False
        if trend_surface_enabled and dt_fired and c1h:
            try:
                trend_chop = classify_candles(c1h) == "chop"
            except Exception:
                trend_chop = False  # production falls back to surfacing
        trend_bypass = trend_surface_enabled and dt_fired and not trend_chop

        surfaced = (score >= min_score or burst_fired or trend_bypass
                    or bearish_pat)
        if not surfaced:
            continue
        stats["surfaced"] += 1

        # Direction proxy for the (non-replayable) LLM verdict: need bearish
        # evidence — downtrend, bearish reversal, or a DOWN burst.
        burst_down = False
        if burst_fired and mom_lookback >= 1 and i >= mom_lookback:
            burst_down = closes[i] < closes[i - mom_lookback]
        if not (dt_fired or bearish_pat or burst_down):
            continue
        stats["directional"] += 1

        # Live ta_late_entry hard gate (short mirror), closed 4h bars only.
        # mtf_enabled=false in live config -> candles_15m=None is identical.
        if le_params and candles_4h:
            w4 = bt._closed_slice(candles_4h, t4, decision_ms, bt._MS_PER["4h"])
            if w4:
                w4 = w4[-W4_WINDOW:]
            le = late_entry_check(w4, None, "short", le_params)
            if le.get("block"):
                stats["late_veto"] += 1
                continue

        # Variant-B input: own 4h downtrend on closed bars (EMA8 < EMA21).
        j4 = bisect.bisect_right(t4, decision_ms - bt._MS_PER["4h"])
        own_down = False
        if j4 >= 22 and ema8_4 and ema21_4:
            e8, e21 = ema8_4[j4 - 1], ema21_4[j4 - 1]
            own_down = math.isfinite(e8) and math.isfinite(e21) and e8 < e21
        # Variant-C input: macro regime from the last CLOSED BTC 1h bar.
        jm = bisect.bisect_right(macro_close_ts, decision_ms) - 1
        macro = macro_regime[jm] if 0 <= jm < len(macro_regime) else "neutral"
        cands.append({"bar": i, "score": score, "own_down": own_down,
                      "macro": macro})
    return cands, stats


def _simulate_shorts(coin: str, candles: List[Candle],
                     cands: List[Dict[str, Any]], max_lev: int, *,
                     equity: float, equity_fraction: float, lev_ceiling: int,
                     max_loss_pct: float, protect_pct: float,
                     retrace_threshold: float,
                     admit: Callable[[Dict[str, Any]], bool],
                     entry_slip_bps: float, exit_slip_bps: float,
                     stop_delay_slip_bps: float) -> List[bt.Trade]:
    """Pass 2: DSL position management over the admitted candidates. Mirrors
    backtest._simulate's short branch exactly (entry at next bar open with
    adverse slip, H-7 exit costs, ROUND_TRIP_FEE_BPS)."""
    by_bar = {c["bar"]: c for c in cands}
    fee_pct = bt.ROUND_TRIP_FEE_BPS / 10000.0

    def _fill(px: float, is_buy: bool, bps: float) -> float:
        adj = px * bps / 10000.0
        return px + adj if is_buy else px - adj

    trades: List[bt.Trade] = []
    open_t: Optional[bt.Trade] = None
    open_dsl: Optional[bt.DSL] = None
    for i in range(len(candles) - 1):
        bar = candles[i]
        if open_t is not None and open_dsl is not None:
            done, exit_ref, reason = open_dsl.check_bar(i, bar)
            if done:
                is_stop = reason.startswith("max_loss")
                slip = exit_slip_bps + (stop_delay_slip_bps if is_stop else 0.0)
                exit_px = _fill(exit_ref, True, slip)  # closing a short = BUY
                gross = (open_t.entry_px - exit_px) / open_t.entry_px
                open_t.exit_bar = i
                open_t.exit_px = exit_px
                open_t.pnl_usd = open_t.notional * (gross - fee_pct)
                open_t.exit_reason = reason
                trades.append(open_t)
                open_t = open_dsl = None
                continue
        if open_t is None and i in by_bar and admit(by_bar[i]):
            next_bar = candles[i + 1]
            lev = min(lev_ceiling, max_lev)
            notional = equity * equity_fraction * lev
            entry_px = _fill(next_bar.o, False, entry_slip_bps)  # SELL below
            open_t = bt.Trade(coin=coin, side="short", entry_bar=i + 1,
                              entry_px=entry_px, notional=notional,
                              margin=equity * equity_fraction, leverage=lev)
            open_dsl = bt.DSL(side="short", entry_px=entry_px, entry_bar=i + 1,
                              peak_px=entry_px, max_loss_pct=max_loss_pct,
                              protect_pct=protect_pct,
                              retrace_threshold=retrace_threshold)
    return trades


def _variant_admit(name: str, min_score: float = 0.0
                   ) -> Callable[[Dict[str, Any]], bool]:
    if name == "A":
        return lambda c: True
    if name == "B":
        return lambda c: bool(c["own_down"])
    if name == "C":
        return lambda c: c["macro"] == "down"
    if name == "D":
        return lambda c: bool(c["own_down"]) and c["macro"] == "down"
    if name == "E":
        return lambda c: (bool(c["own_down"]) and c["macro"] == "down"
                          and c["score"] >= min_score)
    raise ValueError(name)


def _window_metrics(trades: List[bt.Trade], candles: List[Candle],
                    days: int, equity: float) -> Dict[str, Any]:
    """Stats restricted to trades ENTERED within the trailing `days` window."""
    cutoff = candles[-1].t - days * 86_400_000
    sub = [t for t in trades if candles[t.entry_bar].t >= cutoff]
    m = bt._split_metrics(sub, equity)
    reasons: Dict[str, int] = {}
    for t in sub:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    m["reasons"] = reasons
    return m


def _merge_metrics(per_coin: List[Dict[str, Any]], equity: float) -> Dict[str, Any]:
    """Merge per-coin _window_metrics dicts into portfolio-level stats."""
    n = sum(m.get("n", 0) for m in per_coin)
    out: Dict[str, Any] = {"n": n}
    if n == 0:
        return out
    wins = sum(m.get("wins", 0) for m in per_coin)
    pnl = sum(m.get("pnl", 0.0) for m in per_coin)
    out.update(n=n, wins=wins, win_rate=wins / n * 100, pnl=pnl,
               expectancy=pnl / n, pnl_pct=pnl / equity * 100,
               max_dd=sum(m.get("max_dd", 0.0) for m in per_coin))
    reasons: Dict[str, int] = {}
    for m in per_coin:
        for k, v in (m.get("reasons") or {}).items():
            reasons[k] = reasons.get(k, 0) + v
    out["reasons"] = reasons
    return out


def _print_variant(label: str, desc: str, merged: Dict[int, Dict[str, Any]],
                   windows: tuple) -> None:
    print(f"\n--- Variant {label}: {desc} ---")
    hdr = f"  {'window':<7s} {'n':>4s} {'win%':>6s} {'exp/trade':>10s} " \
          f"{'PnL':>9s} {'ret%':>7s} {'maxDD':>8s}  exits"
    print(hdr)
    for d in windows:
        m = merged.get(d) or {}
        if not m.get("n"):
            print(f"  {d}d{'':<4s} {'0':>4s} {'-':>6s} {'-':>10s} "
                  f"{'-':>9s} {'-':>7s} {'-':>8s}  -")
            continue
        print(f"  {d}d{'':<4s} {m['n']:>4d} {m['win_rate']:>5.1f}% "
              f"${m['expectancy']:>+8.3f} ${m['pnl']:>+8.2f} "
              f"{m['pnl_pct']:>+6.1f}% ${m['max_dd']:>7.2f}  {m['reasons']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=180,
                    help="history to pull (results are also split at 90d)")
    ap.add_argument("--coins", type=int, default=7)
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--min-vol", type=float, default=50e6,
                    help="24h notional volume floor for the universe (USD)")
    ap.add_argument("--entry-slip-bps", type=float, default=bt.DEFAULT_ENTRY_SLIP_BPS)
    ap.add_argument("--exit-slip-bps", type=float, default=bt.DEFAULT_EXIT_SLIP_BPS)
    ap.add_argument("--stop-delay-slip-bps", type=float,
                    default=bt.DEFAULT_STOP_DELAY_SLIP_BPS)
    ap.add_argument("--cache-file", default="/tmp/hermes_5m_backtest_cache.json",
                    help="historical_candles disk cache (reused across runs)")
    ap.add_argument("--source", choices=("hyperliquid", "binance"),
                    default="hyperliquid",
                    help="candle data source; binance = spot klines from "
                         "data-api.binance.vision (full 180d 5m history, "
                         "HL only retains ~17d of 5m)")
    ap.add_argument("--bn-cache-file", default="/tmp/hermes_5m_binance_cache.json",
                    help="Binance klines disk cache (reused across runs)")
    args = ap.parse_args()

    live = read_agent_config()
    live_dsl = live.get("dsl_exit", {}) or {}
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.10))
    lev_ceiling = int(cfg_get("leverage", config=live) or 5)
    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=live_dsl) or 2.5)
    protect = float(cfg_get("dsl_exit.protect_pct", config=live_dsl) or 1.5)
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=live_dsl) or 0.30)

    le_params = dict(live.get("ta_late_entry") or {})
    le_params.pop("mode", None)
    le_params.pop("shadow_log_path", None)

    rc = live.get("regime_classifier") or {}
    fast_p = int(rc.get("fast_ema", 20))
    slow_p = int(rc.get("slow_ema", 30))
    slope_up = float(rc.get("slope_threshold", 0.002))
    lookback = int(rc.get("slope_lookback", 8))
    gate = live.get("runner_entry_gate") or {}
    live_min_short_comp = float(gate.get("min_short_composite", 40))
    live_min_short_conf = float(gate.get("min_short_confidence", 0.68))

    # Production scan block + trigger params (SAME helpers perception uses).
    scan = live.get("scan") or {}
    min_score = float(scan.get("minCompositeScore", 54))
    candle_count = int(scan.get("candleCount", 100))
    from hermes_trader.agents.config import (
        trigger_thresholds_params, trigger_weights_params)
    weights = trigger_weights_params(config=live)
    thresholds = trigger_thresholds_params(config=live)
    cp = live.get("candlestick_patterns") or {}
    trend_surface_enabled = bool(live.get("trend_surface_enabled", True))

    tm_lookback = int(thresholds.get("trendMomentumLookback", 72))
    tm_pct = float(thresholds.get("trendMomentumPct", 5.0))
    mom_lookback = int(thresholds.get("momentumLookback", 2))
    mom_pct = float(thresholds.get("momentumPct", 4.0))
    ctx_lookback = int(cp.get("context_lookback", 6))
    ctx_pct = float(cp.get("context_pct", 1.5))

    interval = "5m"
    sim_ms = bt._MS_PER[interval]
    now_ms = int(time.time() * 1000)
    last_closed_open = now_ms - (now_ms % sim_ms) - sim_ms
    window_start = last_closed_open - args.days * 86_400_000
    warmup_bars = candle_count + tm_lookback + 10
    fetch_start = window_start - warmup_bars * sim_ms
    need_1h_start = window_start - (H1_WINDOW + 30) * 3_600_000
    need_4h_start = window_start - (W4_WINDOW + 30) * bt._MS_PER["4h"]

    hc.set_cache_file(args.cache_file)
    fetch = _fetch_span if args.source == "hyperliquid" else _fetch_span_binance
    if args.source == "binance":
        _bn_cache_load(args.bn_cache_file)

    print("=== hermes-trader SHORT re-enable backtest (5m, production chain) ===")
    print(f"period: {args.days}d (also split at 90d)   interval: {interval}   "
          f"source: {args.source}   "
          f"universe: top-{args.coins} perps with 24h vol >= ${args.min_vol / 1e6:.0f}M")
    print(f"equity: ${args.equity:.0f}   fraction: {equity_fraction:.0%}   "
          f"leverage ceiling: {lev_ceiling}x")
    print(f"scan: minCompositeScore={min_score:g}  candleCount={candle_count}  "
          f"trend_surface_enabled={trend_surface_enabled}  "
          f"candlestick_patterns={'on' if cp.get('enabled') else 'off'}")
    print(f"DSL: max_loss={max_loss}%  protect={protect}%  retrace={retrace}")
    print(f"ta_late_entry: ENFORCED (rsi {le_params.get('rsi_ob')}/{le_params.get('rsi_os')}, "
          f"ext ±{le_params.get('ext_ob')}, trend-relax ADX>={le_params.get('adx_trend_threshold')}, "
          f"mtf={le_params.get('mtf_enabled')} -> 15m sub-check inert, passed as None)")
    print(f"macro regime: BTC EMA{fast_p}/{slow_p} slope±{slope_up} over {lookback} bars "
          f"(live regime_classifier params)")
    print(f"cost model (H-7): {bt.ROUND_TRIP_FEE_BPS:.1f}bps RT fee + "
          f"{args.entry_slip_bps:.1f}bps entry / {args.exit_slip_bps:.1f}bps exit slip + "
          f"{args.stop_delay_slip_bps:.1f}bps stop-out delay")
    print(f"live short gates for reference: min_short_confidence={live_min_short_conf} "
          f"(NOT replayable — LLM output), min_short_composite={live_min_short_comp}")

    # Macro regime series from BTC 1h closes (production classifier params).
    print("\nfetching BTC 1h for macro regime series ...")
    btc = fetch("BTC", "1h", need_1h_start, last_closed_open)
    btc_closes = [c.c for c in btc]
    macro_close_ts = [c.t + 3_600_000 for c in btc]
    macro_regime = _macro_regime_series(btc_closes, fast_p, slow_p, slope_up,
                                        lookback)
    n_down = sum(1 for r in macro_regime if r == "down")
    n_up = sum(1 for r in macro_regime if r == "up")
    print(f"BTC 1h bars: {len(btc)}   regime mix: up {n_up / len(btc) * 100:.0f}% / "
          f"down {n_down / len(btc) * 100:.0f}% / "
          f"neutral {(len(btc) - n_up - n_down) / len(btc) * 100:.0f}%")

    universe = get_universe()
    perps = [m for m in universe
             if m["type"] == "perp" and not m["coin"].startswith("@")
             and float(m.get("dayNtlVlm", 0) or 0) >= args.min_vol]
    ranked = sorted(perps, key=lambda m: float(m.get("dayNtlVlm", 0) or 0),
                    reverse=True)
    if args.source == "binance":
        # Keep only coins Binance spot lists (HYPE/PONS etc. are HL-only).
        coins, skipped = [], []
        for m in ranked:
            if len(coins) >= args.coins:
                break
            if _binance_symbol(m["coin"]) is not None:
                coins.append(m)
            else:
                skipped.append(m["coin"])
        if skipped:
            print(f"not on Binance spot, skipped: {', '.join(skipped)}")
    else:
        coins = ranked[: args.coins]
    print(f"universe: {len(perps)} perps pass the volume floor; "
          f"simulating top {len(coins)}: "
          f"{', '.join(m['coin'] for m in coins)}\n")

    variant_labels = ["A", "B", "C", "D"] + [f"E>={s:g}" for s in SCAN_SCORES]
    trades_by_variant: Dict[str, List[bt.Trade]] = {v: [] for v in variant_labels}
    candles_by_coin: Dict[str, List[Candle]] = {}
    total_stats = {"prefilter": 0, "fired": 0, "surfaced": 0, "directional": 0,
                   "late_veto": 0}

    for m in coins:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        try:
            t0 = time.time()
            candles = fetch(coin, interval, fetch_start, last_closed_open)
            if len(candles) < warmup_bars + 200:
                print(f"  {coin}: only {len(candles)} bars — skipped")
                continue
            candles_1h = fetch(coin, "1h", need_1h_start, last_closed_open)
            candles_4h = fetch(coin, "4h", need_4h_start, last_closed_open)
        except Exception as e:
            print(f"  {coin}: fetch failed: {e} — skipped")
            continue
        candles_by_coin[coin] = candles
        ts = [c.t for c in candles]
        i0 = max(bisect.bisect_left(ts, window_start), candle_count,
                 tm_lookback + 1)
        cands, stats = _scan_candidates(
            coin, candles, candles_1h, candles_4h, macro_close_ts, macro_regime,
            weights, thresholds, cp, min_score, candle_count,
            trend_surface_enabled, le_params, i0, sim_ms,
            tm_lookback, tm_pct, mom_lookback, mom_pct, ctx_lookback, ctx_pct)
        for k in total_stats:
            total_stats[k] += stats[k]
        for label in variant_labels:
            if label.startswith("E"):
                ms = float(label.split(">=")[1])
                admit = _variant_admit("E", ms)
            else:
                admit = _variant_admit(label)
            trades_by_variant[label].extend(_simulate_shorts(
                coin, candles, cands, max_lev, equity=args.equity,
                equity_fraction=equity_fraction, lev_ceiling=lev_ceiling,
                max_loss_pct=max_loss, protect_pct=protect, retrace_threshold=retrace,
                admit=admit, entry_slip_bps=args.entry_slip_bps,
                exit_slip_bps=args.exit_slip_bps,
                stop_delay_slip_bps=args.stop_delay_slip_bps))
        print(f"  {coin:8s} bars={len(candles)}  candidates={len(cands)} "
              f"(prefilter {stats['prefilter']} → fired {stats['fired']} → "
              f"surfaced {stats['surfaced']} → directional {stats['directional']}, "
              f"late-veto {stats['late_veto']})  [{time.time() - t0:.0f}s]")

    print(f"\ncandidate funnel (all coins): prefilter {total_stats['prefilter']} → "
          f"fired {total_stats['fired']} → surfaced {total_stats['surfaced']} → "
          f"directional {total_stats['directional']} → late-vetoed "
          f"{total_stats['late_veto']} → admitted to variant filters")

    try:
        hc.flush_disk_cache()
        if args.source == "binance":
            _bn_cache_flush()
    except Exception:
        pass

    windows = (90, args.days) if args.days != 90 else (90,)
    descs = {
        "A": "baseline: production 5m surfacing + bearish-directional + ta_late",
        "B": "+ own 4h downtrend (EMA8<EMA21)",
        "C": "+ macro down (BTC regime)",
        "D": "+ BOTH own-4h downtrend AND macro down",
    }
    for s in SCAN_SCORES:
        descs[f"E>={s:g}"] = f"D + composite>={s:g} (min_short_composite candidate)"

    print("\n=== VARIANT RESULTS (net of H-7 costs) ===")
    verdict_rows = []
    for label in variant_labels:
        merged: Dict[int, Dict[str, Any]] = {}
        for d in windows:
            per_coin = []
            for coin, candles in candles_by_coin.items():
                ct = [t for t in trades_by_variant[label] if t.coin == coin]
                per_coin.append(_window_metrics(ct, candles, d, args.equity))
            merged[d] = _merge_metrics(per_coin, args.equity)
        _print_variant(label, descs[label], merged, windows)
        for d in windows:
            m = merged[d]
            verdict_rows.append((label, d, m.get("n", 0), m.get("win_rate", 0.0),
                                 m.get("expectancy", 0.0), m.get("pnl", 0.0),
                                 m.get("max_dd", 0.0)))

    print("\n=== DECISION GRID (expectancy is per-trade USD on $"
          f"{args.equity:.0f} equity) ===")
    print(f"  {'variant':<8s} {'win':>5s} {'n':>4s} {'win%':>6s} "
          f"{'exp/trade':>10s} {'PnL':>9s} {'maxDD':>8s}")
    for label, d, n, wr, exp, pnl, mdd in verdict_rows:
        flag = " <-- n>=30 & exp>0" if n >= 30 and exp > 0 else ""
        print(f"  {label:<8s} {d}d{'':<2s} {n:>4d} {wr:>5.1f}% "
              f"${exp:>+8.3f} ${pnl:>+8.2f} ${mdd:>7.2f}{flag}")

    print("\nCaveats:")
    print("  - AI verdict substituted with a deterministic bearish-directional "
          "rule (downtrendMomentum / bearishReversalCandle / down-burst); "
          "min_short_confidence (LLM) NOT replayed — only min_short_composite "
          "is calibrated here.")
    print("  - Score-path surfacing without directional evidence (e.g. a lone "
          "-2sigma bar in an uptrend) is excluded — production would likely "
          "bounce it off the counter-regime gate anyway.")
    print("  - Volume floor uses TODAY's dayNtlVlm snapshot (survivorship bias).")
    print("  - DSL modelled with the base 3-param ladder; live phase2_tiers / "
          "regime_aware / breakeven refinements not replayed.")
    print("  - One open position per coin; equity constant; no cooldown; "
          "max_concurrent cap NOT enforced across coins.")
    print("  - 100-bar 5m / 48-bar 1h / 60-bar 4h windows = production fetch "
          "sizes (exact, not truncated).")
    print("  - Past performance does NOT imply future results.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
