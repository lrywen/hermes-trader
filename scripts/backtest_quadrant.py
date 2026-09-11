#!/usr/bin/env python3
"""Macro×own regime quadrant backtest for the per-coin direction overlay.

Question: the live crypto regime gate keys EVERY crypto off the BTC proxy, so a
BTC-"aligned" alt gets a free pass even when that alt's OWN trend has rolled
over (the ZEC / "坑1" failure mode). This script measures, on enough
mechanical backtest trades, the counterfactual EV of demoting/blocking an
entry when BTC macro is aligned but the coin's OWN 4h direction disagrees.

It reuses scripts/backtest.py's signal simulation UNCHANGED (same triggers +
TA confirm + ta_late_entry gate + DSL exit + H-7 cost model). The only
addition is an entry-time *label*: for each simulated entry we record
  * macro  : BTC 1h regime via the live classify_candles() (same params)
  * own    : the coin's OWN 4h close-vs-EMA21 direction + ADX14 confirmation
              (exactly per_coin_regime_shadow.own_4h_divergence's rule)
and bucket each CLOSED trade into the macro×own quadrant. Labels never feed
the entry/exit logic, so the PnL stream is identical to scripts/backtest.py.

Mechanical edge only (the AI research step is the same deterministic
heuristic scripts/backtest.py uses). This measures whether the per-coin
overlay has an EDGE, not the AI's judgment.

Usage:
    python3 scripts/backtest_quadrant.py --days 90 --coins 40
    python3 scripts/backtest_quadrant.py --days 120 --coins 50 --no-late-entry
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

os.environ["HERMES_BACKTEST"] = "1"
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

from hermes_trader.agents.config import get_config
from hermes_trader.indicators.math import ema, adx
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.models.types import Candle
from hermes_trader.agents.market_regime import classify_candles, _classifier_params

import backtest as bt

_MS_1H = 3_600_000
_MS_4H = 4 * 3_600_000


def _closed(bars: List[Candle], cutoff_ms: int, bar_ms: int) -> List[Candle]:
    return [b for b in bars if b.t + bar_ms <= cutoff_ms]


class QuadrantTagger:
    """Holds the BTC 1h series and a per-coin 4h cache; returns a label dict."""

    def __init__(self, require_own_adx: float = 20.0):
        self.require_own_adx = require_own_adx
        self.fast, self.slow, self.slope_up, self.adx_max = _classifier_params()
        self.btc1: List[Candle] = fetch_hl_candles("BTC", "1h", 2000)
        self._own_cache: Dict[str, List[Candle]] = {}

    def _own4(self, coin: str) -> List[Candle]:
        if coin not in self._own_cache:
            if coin == "BTC":
                # own 4h for BTC itself
                self._own_cache[coin] = fetch_hl_candles("BTC", "4h", 1200)
            else:
                try:
                    self._own_cache[coin] = fetch_hl_candles(coin, "4h", 1200)
                except Exception:
                    self._own_cache[coin] = []
        return self._own_cache[coin]

    def __call__(self, coin: str, side: str, decision_ms: int) -> Optional[dict]:
        macro = "unknown"
        bs = _closed(self.btc1, decision_ms, _MS_1H)
        if len(bs) > 35:
            macro = classify_candles(bs, self.fast, self.slow,
                                     self.slope_up, self.adx_max)
        own_dir = "unknown"
        own_adx: Optional[float] = None
        own_gap: Optional[float] = None
        confirms = False
        cb = _closed(self._own4(coin), decision_ms, _MS_4H)
        if len(cb) >= 35:
            closes = [c.c for c in cb]
            e21 = ema(closes, 21)[-1]
            if e21 > 0:
                own_dir = "down" if closes[-1] < e21 else "up"
                own_gap = (closes[-1] - e21) / e21 * 100.0
                try:
                    own_adx = adx(cb, 14)[-1]
                    confirms = own_adx is not None and own_adx >= self.require_own_adx
                except Exception:
                    own_adx = None
        aligned = (macro == "up" and side == "long") or \
                  (macro == "down" and side == "short")
        against = ((side == "long" and own_dir == "down") or
                   (side == "short" and own_dir == "up"))
        # Same demote rule as per_coin_regime_shadow.own_4h_divergence:
        # only a macro-ALIGNED trade, own 4h against the side, ADX confirms.
        would_demote = bool(aligned and against and confirms)
        return {"macro": macro, "own_dir": own_dir, "own_adx": own_adx,
                "own_gap_pct": own_gap, "aligned": aligned,
                "own_against": against, "adx_confirms": confirms,
                "would_demote": would_demote}


def _bucket(tag: Optional[dict]) -> str:
    if not tag:
        return "untagged"
    macro, own = tag["macro"], tag["own_dir"]
    if macro in ("neutral", "chop", "unknown"):
        return f"macro_{macro}"
    # directional macro
    if own == "unknown":
        return f"{macro}__own_unknown"
    macro_up = macro == "up"
    own_up = own == "up"
    if macro_up == own_up:
        return "aligned_macro_AND_own"
    return "aligned_macro_OWN_AGAINST"


def _stats(rows: List[Any]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = [r for r in rows if r.pnl_usd > 0]
    losses = [r for r in rows if r.pnl_usd <= 0]
    pnl = sum(r.pnl_usd for r in rows)
    avg_w = sum(r.pnl_usd for r in wins) / len(wins) if wins else 0.0
    avg_l = sum(r.pnl_usd for r in rows if r.pnl_usd <= 0) / len(losses) if losses else 0.0
    # Normalised return-on-margin (%) — notional/leverage-invariant, so buckets
    # with different leverage mixes are directly comparable.
    ret_pct = [r.pnl_usd / r.margin * 100.0 for r in rows if r.margin]
    ev_ret = sum(ret_pct) / len(ret_pct) if ret_pct else 0.0
    return {"n": n, "win%": 100 * len(wins) / n, "ev": pnl / n,
            "sum": pnl, "avg_win": avg_w, "avg_loss": avg_l, "ev_ret%": ev_ret}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--coins", type=int, default=40)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--equity", type=float, default=200.0)
    ap.add_argument("--require-own-adx", type=float, default=20.0)
    ap.add_argument("--no-late-entry", action="store_true")
    args = ap.parse_args()

    bars_per_day = {"5m": 288, "15m": 96, "1h": 24, "4h": 6, "1d": 1}[args.interval]
    sim_ms = bt._MS_PER[args.interval]
    total_bars = args.days * bars_per_day + 100

    cfg = get_config()
    cfg["_interval"] = args.interval
    universe = get_universe()
    perps = [m for m in universe if m["type"] == "perp" and not m["coin"].startswith("@")]
    coins = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[: args.coins]

    from hermes_trader.agents.config_store import cfg_get, read_agent_config
    live = {}
    try:
        live = read_agent_config()
    except Exception:
        live = {}
    le_params = {} if args.no_late_entry else dict(live.get("ta_late_entry") or {})

    # Mirror scripts/backtest.py main()'s exact sizing/DSL parameters.
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.10))
    leverage_ceiling = int(cfg_get("leverage", config=live))
    live_dsl = live.get("dsl_exit") or {}
    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=live_dsl) or 2.5)
    protect = float(cfg_get("dsl_exit.protect_pct", config=live_dsl) or 1.5)
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=live_dsl) or 0.30)
    live_atr = live.get("atr_stop") or {}
    atr_mult = float(live_atr.get("atr_mult", 0.0)) if bool(live_atr.get("enabled", False)) else 0.0

    print("=== macro×own quadrant backtest ===")
    print(f"period={args.days}d interval={args.interval} coins=top-{args.coins} "
          f"require_own_adx={args.require_own_adx} late_entry={'off' if args.no_late_entry else 'on'}")

    tagger = QuadrantTagger(require_own_adx=args.require_own_adx)
    need_4h = math.ceil(total_bars * sim_ms / bt._MS_PER["4h"]) + 40
    need_15m = math.ceil(total_bars * sim_ms / bt._MS_PER["15m"]) + 30

    all_trades: List[bt.Trade] = []
    for m in coins:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        try:
            candles = fetch_hl_candles(coin, args.interval, total_bars)
            if len(candles) < 110:
                continue
            c4 = c15 = None
            if le_params:
                try:
                    c4 = candles if args.interval == "4h" else fetch_hl_candles(coin, "4h", need_4h)
                    c15 = candles if args.interval == "15m" else fetch_hl_candles(coin, "15m", need_15m)
                except Exception:
                    c4 = c15 = None
            trades = bt._simulate(
                coin, candles, max_lev,
                equity=args.equity,
                equity_fraction=equity_fraction,
                lev_ceiling=leverage_ceiling,
                cfg=cfg,
                max_loss_pct=max_loss, protect_pct=protect,
                retrace_threshold=retrace, atr_mult=atr_mult,
                candles_4h=c4, candles_15m=c15,
                late_entry_params=le_params,
                regime_tag_fn=tagger,
            )
            all_trades.extend(trades)
        except Exception as e:
            print(f"  {coin:8} error: {e}")

    if not all_trades:
        print("no trades produced")
        return 1

    # ---- quadrant report ----
    groups: Dict[str, List[bt.Trade]] = defaultdict(list)
    for t in all_trades:
        groups[_bucket(t.regime_tag)].append(t)

    print(f"\n{'quadrant':32} {'n':>5} {'win%':>6} {'EV$':>9} {'EVret%':>8} {'sum$':>10}")
    order = ["aligned_macro_AND_own", "aligned_macro_OWN_AGAINST",
             "macro_neutral", "macro_chop", "macro_unknown",
             "up__own_unknown", "down__own_unknown", "untagged"]
    for k in order + [k for k in groups if k not in order]:
        if k not in groups:
            continue
        s = _stats(groups[k])
        if not s["n"]:
            continue
        print(f"{k:32} {s['n']:5d} {s['win%']:6.1f} {s['ev']:+9.4f} {s['ev_ret%']:+8.3f} "
              f"{s['sum']:+10.2f}")

    # ---- the specific overlay decision: among macro-aligned, demote vs keep ----
    aligned = [t for t in all_trades if t.regime_tag and t.regime_tag["aligned"]]
    demote = [t for t in aligned if t.regime_tag["would_demote"]]
    keep = [t for t in aligned if not t.regime_tag["would_demote"]]
    demote_anyadx = [t for t in aligned if t.regime_tag["own_against"]]

    print("\n=== overlay counterfactual (macro-aligned mechanical entries) ===")
    for label, g in [("aligned total", aligned),
                     ("would DEMOTE (own4h against + ADX confirm)", demote),
                     ("would KEEP", keep),
                     ("own-against regardless of ADX (sensitivity)", demote_anyadx)]:
        s = _stats(g)
        if not s["n"]:
            print(f"  {label:48} n=0")
            continue
        print(f"  {label:48} n={s['n']:4d} win={s['win%']:5.1f}% EV$={s['ev']:+.4f} "
              f"EVret%={s['ev_ret%']:+.3f}")

    # per-side demote vs keep — the BTC proxy leak is direction-specific and the
    # production book is long-only, so pooling long/short hides the real edge.
    print("\n=== per-side: would-DEMOTE vs would-KEEP (macro-aligned) ===")
    print(f"  {'side/bucket':28} {'n':>5} {'win%':>6} {'EV$':>9} {'EVret%':>8}")
    for side in ("long", "short"):
        sd = [t for t in demote if t.side == side]
        sk = [t for t in keep if t.side == side]
        sa = [t for t in demote_anyadx if t.side == side]
        for label, g in [(f"{side} demote(+ADX)", sd),
                         (f"{side} demote(any ADX)", sa),
                         (f"{side} keep", sk)]:
            s = _stats(g)
            if not s["n"]:
                print(f"  {label:28} {0:5d}")
                continue
            print(f"  {label:28} {s['n']:5d} {s['win%']:6.1f} {s['ev']:+9.4f} {s['ev_ret%']:+8.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
