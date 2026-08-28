#!/usr/bin/env python3
"""Real-time drift monitor: production regime vs backtest 5-component score.

For each monitored symbol this fetches the SAME 100 1h candles the live
detector uses, then runs two classifiers on the identical window:

  * production  — `hermes_trader.agents.market_regime._classify_candles`
                  (EMA20/30 cross + 8-bar slope + ADX chop overlay)
  * backtest    — `compare_regime_architectures.backtest_label`
                  (5-component weighted score: ADX/ATR/EMA-gap/price-ext/OBV)

Every disagreement is logged as an anomaly to both stderr and a JSONL file.
Drift rates (false-trend, missed-trend, direction-conflict) are tracked on a
rolling window and surfaced at WARNING when they exceed configurable thresholds,
so an operator is alerted the moment the production classifier degrades relative
to the calibrated score — without waiting for the weekly recalibration.

Anomaly types:
  false_trend     prod says up/down, backtest says NEUTRAL/CHOP
  missed_trend    prod says neutral/chop, backtest says TREND
  direction       prod says up but backtest says down (or vice versa)
  weak_aligned    prod says up/down AND agrees direction with backtest but the
                  score is below --min-score (the entry overlay would demote it)

Single-shot (cron / one-off check):
    python3 scripts/monitor_regime_drift.py --coins 20

Continuous (daemon; polls every 5 min, logs every 15 min):
    nohup python3 scripts/monitor_regime_drift.py --coins 20 --loop \
        --interval 300 --summary-interval 900 \
        >> /tmp/regime_drift.log 2>&1 &
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from hermes_trader.agents.market_regime import (  # noqa: E402
    CRYPTO_PROXY,
    EQUITY_PROXY,
    _classify_candles,
    classify_asset,
)
from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from compare_regime_architectures import backtest_label  # noqa: E402

WINDOW = 100  # same count=100 the live detector fetches

TREND, NEUTRAL, CHOP = "TREND", "NEUTRAL", "CHOP"

# Score thresholds (mirror backtest_ab_compare._regime_score buckets).
STRONG_TREND = 0.70
TREND_MIN = 0.55
NEUTRAL_MIN = 0.40


def _bt_strength(raw: str) -> str:
    if raw in ("STRONG_TREND", "TREND"):
        return TREND
    if raw == "CHOP":
        return CHOP
    return NEUTRAL


def _prod_strength(regime: str) -> str:
    if regime in ("up", "down"):
        return TREND
    if regime == "chop":
        return CHOP
    return NEUTRAL


def _bt_is_bullish(common: str) -> Optional[bool]:
    if common == "trend_up":
        return True
    if common == "trend_down":
        return False
    return None


def _classify_anomaly(prod: str, common: str, score: float,
                      min_score: float) -> List[str]:
    """Return the set of anomaly labels for this (prod, backtest) pair."""
    anomalies: List[str] = []
    p_strength = _prod_strength(prod)
    b_strength = _bt_strength(
        "STRONG_TREND" if common in ("trend_up", "trend_down") else
        ("CHOP" if common == "chop" else "NEUTRAL"))

    if p_strength == TREND and b_strength != TREND:
        anomalies.append("false_trend")
    if p_strength != TREND and b_strength == TREND:
        anomalies.append("missed_trend")

    prod_bull = prod == "up"
    bt_bull = _bt_is_bullish(common)
    if bt_bull is not None and p_strength == TREND and prod_bull != bt_bull:
        anomalies.append("direction")

    if p_strength == TREND and score < min_score:
        anomalies.append("weak_aligned")

    return anomalies


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _emit_log(log_fh, record: Dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    print(line, file=log_fh, flush=True)


def _resolve_proxies(coins: List[str]) -> List[str]:
    """Map coins to the proxy production actually uses, deduped. Crypto coins
    collapse to CRYPTO_PROXY; equities to EQUITY_PROXY; commodities keep own
    ticker. The two proxies are always included even if no coin maps to them."""
    proxies = set()
    for coin in coins:
        klass = classify_asset(coin)
        if klass == "crypto":
            proxies.add(CRYPTO_PROXY)
        elif klass == "equity":
            proxies.add(EQUITY_PROXY)
        else:
            proxies.add(coin.upper())
    proxies.add(CRYPTO_PROXY)
    proxies.add(EQUITY_PROXY)
    return sorted(proxies)


def check_symbol(proxy: str, min_score: float) -> Optional[Dict]:
    """Fetch candles for one proxy, run both classifiers, return a sample dict
    (or None on fetch/insufficient-data failure)."""
    try:
        candles = fetch_hl_candles(proxy, "1h", WINDOW)
    except Exception as e:
        return {"proxy": proxy, "error": f"fetch failed: {e}"}
    if not candles or len(candles) < WINDOW:
        return {"proxy": proxy, "error": "insufficient candles"}

    closes = [float(c.c) for c in candles]
    prod = _classify_candles(candles)
    common, raw, score = backtest_label(candles, closes)
    anomalies = _classify_anomaly(prod, common, score, min_score)
    return {
        "proxy": proxy,
        "prod": prod,
        "bt_common": common,
        "bt_raw": raw,
        "score": round(score, 3),
        "anomalies": anomalies,
        "close": closes[-1],
        "ts": candles[-1].t if hasattr(candles[-1], "t") else None,
    }


def run_once(proxies: List[str], min_score: float, log_fh,
             history: Deque[Dict]) -> Tuple[int, int, Counter]:
    """One poll across all proxies. Returns (n_ok, n_alert, type_counter)."""
    n_ok = n_alert = 0
    type_counter: Counter = Counter()
    for proxy in proxies:
        sample = check_symbol(proxy, min_score)
        if sample is None:
            continue
        if "error" in sample:
            rec = {"ts": _now_iso(), "level": "ERROR",
                   "event": "regime_drift_error", **sample}
            _emit_log(log_fh, rec)
            print(f"[{rec['ts']}] ERROR {proxy}: {sample['error']}",
                  file=sys.stderr)
            continue
        n_ok += 1
        if sample["anomalies"]:
            n_alert += 1
            for a in sample["anomalies"]:
                type_counter[a] += 1
            rec = {"ts": _now_iso(), "level": "WARNING",
                   "event": "regime_drift_anomaly", **sample}
            _emit_log(log_fh, rec)
            print(f"[{rec['ts']}] WARN  {proxy}: prod={sample['prod']} "
                  f"bt={sample['bt_raw']}({sample['bt_common']}) "
                  f"score={sample['score']} anomalies={sample['anomalies']}",
                  file=sys.stderr)
        else:
            rec = {"ts": _now_iso(), "level": "INFO",
                   "event": "regime_drift_ok", "proxy": proxy,
                   "prod": sample["prod"], "bt_raw": sample["bt_raw"],
                   "score": sample["score"]}
            _emit_log(log_fh, rec)
        history.append(sample)
    return n_ok, n_alert, type_counter


def _rolling_rates(history: Deque[Dict]) -> Dict[str, float]:
    """Compute drift rates over the rolling history."""
    n = len(history)
    if n == 0:
        return {"samples": 0}
    counts = Counter()
    for s in history:
        if "error" in s:
            continue
        for a in s.get("anomalies", []):
            counts[a] += 1
    return {
        "samples": n,
        "false_trend_rate": round(counts["false_trend"] / n, 4),
        "missed_trend_rate": round(counts["missed_trend"] / n, 4),
        "direction_conflict_rate": round(counts["direction"] / n, 4),
        "weak_aligned_rate": round(counts["weak_aligned"] / n, 4),
        "anomaly_rate": round(
            sum(1 for s in history
                if "error" not in s and s.get("anomalies")) / n, 4),
    }


def _print_summary(rates: Dict, thresholds: Dict[str, float]) -> None:
    if rates.get("samples", 0) == 0:
        return
    flags = []
    for key, thr in thresholds.items():
        rate = rates.get(key, 0.0)
        mark = " !!" if rate >= thr else ""
        flags.append(f"{key}={rate*100:.1f}%{mark}")
    print(f"[{_now_iso()}] SUMMARY " + " ".join(flags)
          + f" samples={rates['samples']}", file=sys.stderr, flush=True)


def _pick_coins(n: int, explicit: str) -> List[str]:
    if explicit:
        return [c.strip() for c in explicit.split(",") if c.strip()]
    from hermes_trader.client.universe import get_universe
    uni = get_universe(include_hip3=False)
    uni.sort(key=lambda m: float(m.get("dayNtlVlm", 0) or 0), reverse=True)
    return [m["coin"] for m in uni[:n]]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", type=int, default=20,
                    help="top-N crypto coins by volume (their proxies are "
                         "deduped; BTC + SP500 always included)")
    ap.add_argument("--coin-list", type=str, default="",
                    help="comma-separated explicit coins (overrides --coins)")
    ap.add_argument("--min-score", type=float, default=0.55,
                    help="score below which a prod=trend bar is logged as "
                         "weak_aligned (matches risk-gate min_trend_score)")
    ap.add_argument("--log-file", type=str,
                    default="/tmp/regime_drift.jsonl",
                    help="JSONL anomaly/info log path")
    ap.add_argument("--loop", action="store_true",
                    help="run continuously instead of once")
    ap.add_argument("--interval", type=int, default=300,
                    help="poll interval in seconds (loop mode)")
    ap.add_argument("--summary-interval", type=int, default=900,
                    help="rolling-summary print interval in seconds (loop)")
    ap.add_argument("--history", type=int, default=288,
                    help="rolling window size for drift rates (default 288 "
                         "= 24h of 5-minute samples)")
    ap.add_argument("--false-trend-alert", type=float, default=0.30,
                    help="rolling false-trend rate that triggers a summary "
                         "alert (fraction; 0.30 = 30%%)")
    ap.add_argument("--missed-trend-alert", type=float, default=0.30,
                    help="rolling missed-trend rate alert threshold")
    ap.add_argument("--direction-alert", type=float, default=0.05,
                    help="rolling direction-conflict rate alert threshold")
    ap.add_argument("--anomaly-alert", type=float, default=0.40,
                    help="overall anomaly-rate alert threshold")
    args = ap.parse_args()

    coins = _pick_coins(args.coins, args.coin_list)
    proxies = _resolve_proxies(coins)

    Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(args.log_file, "a", encoding="utf-8")

    thresholds = {
        "false_trend_rate": args.false_trend_alert,
        "missed_trend_rate": args.missed_trend_alert,
        "direction_conflict_rate": args.direction_alert,
        "anomaly_rate": args.anomaly_alert,
    }

    print(f"[{_now_iso()}] regime drift monitor starting: "
          f"proxies={proxies} min_score={args.min_score} "
          f"log={args.log_file} loop={args.loop}", file=sys.stderr, flush=True)

    history: Deque[Dict] = deque(maxlen=args.history)

    if not args.loop:
        n_ok, n_alert, _ = run_once(proxies, args.min_score, log_fh, history)
        rates = _rolling_rates(history)
        _print_summary(rates, thresholds)
        print(f"[{_now_iso()}] single-shot complete: "
              f"ok={n_ok} anomalies={n_alert}", file=sys.stderr, flush=True)
        log_fh.close()
        return 1 if n_ok == 0 else 0

    last_summary = time.time()
    try:
        while True:
            try:
                run_once(proxies, args.min_score, log_fh, history)
            except Exception as e:
                rec = {"ts": _now_iso(), "level": "ERROR",
                       "event": "regime_drift_poll_failed", "error": str(e)}
                _emit_log(log_fh, rec)
                print(f"[{rec['ts']}] poll failed: {e}", file=sys.stderr)
            now = time.time()
            if now - last_summary >= args.summary_interval:
                _print_summary(_rolling_rates(history), thresholds)
                last_summary = now
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[{_now_iso()}] monitor stopped.", file=sys.stderr)
    finally:
        log_fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
