#!/usr/bin/env python3
"""Offline replay: why-do-we-buy-high-sell-low fix validation (F1/F2/F3).

Reconstructs the 1-minute price path for every closed shadow trade from
Hyperliquid candleSnapshot, rebuilds the entry ATR(14) from 4h candles, then
drives the REAL production DSLTracker.check() over each bar under several
ExitPolicy variants:

  base      - current production dsl_exit config (ceiling 3%, protect 1.5,
              retrace 35%, BE trigger 2.5)  [control]
  F1        - atr_stop.ceiling_pct 3.0 -> 2.0  (tighter stop)
  F2        - hard stop FILLS AT THE STOP FLOOR PRICE (not the overrun mark);
              models the live exchange-side stop instead of the shadow's
              fill-at-current-mark that turns a 3% stop into -4..-7.8%
  F3        - earlier profit protection: protect 1.5->1.0, retrace 35->25,
              BE trigger 2.5->1.0 / lock 0.3
  F1+F2+F3  - all three together

Replay is entry-invariant (same fills, same price paths for every variant) so
the ONLY difference is the exit policy. We first sanity-check `base` against
the ledger's actual PnL, then compare variants.

Read-only: no orders, no state writes. Network: candleSnapshot only.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, "/app")

from hermes_trader.agents.dsl_exit import (  # noqa: E402
    DSLTracker,
    ExitPolicy,
    RetraceTier,
    _build_policy_from_config,
)

HL = "https://api.hyperliquid.xyz/info"
FEE_PCT = 0.025  # round_trip_fills (taker_fee_pct=0.025 per side)
BOOK = Path("/data/.shadow-book.json")


def _post(payload: dict, retries: int = 3):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                HL, data=data, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return []


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int):
    """Fetch candles covering [start_ms, end_ms]. HL caps 500/request, so page
    backwards from end_ms."""
    out = []
    end = end_ms
    while True:
        batch = _post({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": start_ms, "endTime": end},
        })
        if not batch:
            break
        out = batch + out
        oldest = int(batch[0]["t"])
        if len(batch) < 500 or oldest <= start_ms:
            break
        end = oldest - 1
        time.sleep(0.15)
    # de-dup + sort by t
    seen = {int(c["t"]): c for c in out}
    return [seen[t] for t in sorted(seen)]


def atr14_pct(candles: list[dict], entry_ts_ms: int) -> float:
    """Wilder ATR(14) on 4h candles, as % of close, evaluated at entry."""
    bars = [c for c in candles if int(c["t"]) <= entry_ts_ms]
    if len(bars) < 16:
        return 0.0
    hs = [float(c["h"]) for c in bars]
    ls = [float(c["l"]) for c in bars]
    cs = [float(c["c"]) for c in bars]
    trs = [hs[0] - ls[0]]
    for i in range(1, len(bars)):
        trs.append(max(hs[i] - ls[i],
                       abs(hs[i] - cs[i - 1]),
                       abs(ls[i] - cs[i - 1])))
    # Wilder smoothing up to the entry bar
    atr = sum(trs[:15]) / 14.0
    for i in range(15, len(trs)):
        atr = (atr * 13 + trs[i]) / 14.0
    return atr / cs[-1] * 100.0


def load_trades():
    d = json.load(open(BOOK))
    fills = d["fills"]
    by_pos = {}
    for f in fills:
        by_pos.setdefault(f["position_id"], []).append(f)
    trades = []
    for pid, fs in by_pos.items():
        op = next((f for f in fs if f.get("type") == "open"), None)
        cl = next((f for f in fs if f.get("type") == "close"), None)
        if not op or not cl:
            continue
        trades.append({
            "coin": op["coin"],
            "side": op["side"],
            "qty": float(op["qty"]),
            "entry_px": float(op["price"]),
            "lev": int(op.get("leverage") or 1),
            "open_ts": int(op["ts"]),
            "close_ts": int(cl["ts"]),
            "actual_px": float(cl["price"]),
            "actual_pnl": float(cl.get("realized_pnl_usd") or 0.0),
            "actual_reason": cl.get("reason", ""),
        })
    trades.sort(key=lambda t: t["open_ts"])
    return trades


# ── Policy variants (ALL derived from the real production dsl_exit block) ──
#
# Fidelity note: the 50 closed shadow trades were produced by the PRE-F1 code,
# whose _effective_max_loss() lets the ATR-derived stop OVERRIDE max_loss_pct.
# The production shadow policy has max_loss_pct=1.0, but with atr_stop on (floor
# 1.2% / ceiling 3%) the pre-fix code actually runs an ATR-WIDE stop — so the
# ledger's losses correspond to `prod_prefx`, NOT to a 1% stop. After the F1
# code fix the shadow effective stop becomes min(1.0, atr_cap); since the ATR
# floor 1.2% is always wider than 1.0%, the post-fix shadow stop binds at a
# FIXED 1.0%. We model that by disabling the atr branch + max_loss_pct=1.0
# (equivalent spot cap under either code version). Live regime stops 0.8/0.4
# are swept too, to judge whether 1.0% is too loose or too tight.


def base_policy(atr_pct: float) -> ExitPolicy:
    """Real production shadow policy from .agent-config.json dsl_exit block."""
    return _build_policy_from_config()


def _fixed_stop(base: ExitPolicy, pct: float) -> ExitPolicy:
    """Post-fix effective spot stop = fixed `pct` (ATR branch cannot widen it).

    Modelled by turning the atr branch off and pinning max_loss_pct; noise_band
    is a separate field and stays on. This is code-version independent."""
    return replace(base, atr_stop_enabled=False, max_loss_pct=pct)


def make_variants(atr_pct: float) -> dict[str, ExitPolicy]:
    base = base_policy(atr_pct)  # production pre-fix shadow policy (ATR wide)
    # F1 code fix -> shadow stop binds at fixed cap. Sweep the curve:
    f1_10 = _fixed_stop(base, 1.0)   # post-fix shadow: min(1.0, atr>=1.2) = 1.0
    f1_08 = _fixed_stop(base, 0.8)   # live trend regime cap
    f1_04 = _fixed_stop(base, 0.4)   # live non_trend regime cap
    f1_12 = _fixed_stop(base, 1.2)   # looser control
    f1_15 = _fixed_stop(base, 1.5)   # looser control
    # F3 as originally specced (aggressive profit-protection tightening),
    # applied to the POST-fix 1.0% shadow stop.
    f3 = replace(f1_10, protect_pct=1.0, retrace_threshold=0.25,
                 breakeven_trigger_pct=1.0, breakeven_lock_pct=0.3)
    # F3 mild: earlier breakeven only (2.5/0.3 -> 1.5/0.4), post-fix stop
    f3_be = replace(f1_10, breakeven_trigger_pct=1.5, breakeven_lock_pct=0.4)
    return {
        "prod_prefx": base,
        "F1fix1.0": f1_10, "F1fix0.8": f1_08, "F1fix0.4": f1_04,
        "F1fix1.2": f1_12, "F1fix1.5": f1_15,
        "F1+F3": f3, "F1+be": f3_be,
    }


def _bar_clock(bar_wall_s: float):
    """Context manager: advance BOTH wall clock (time.time) and monotonic
    clock (time.monotonic) to the bar's timestamp. check() uses time.time for
    hold-duration/timeout and time.monotonic for the hard-stop (1s) and
    floor-breach (4s) confirmation gates. On a 1m grid a close that breaches
    persists ~60s into the next bar, so both gates correctly confirm on the
    bar AFTER first breach — while a single-bar wick that closes back inside
    resets the timer and does NOT stop (mirrors the production wick guard)."""
    import contextlib

    import hermes_trader.agents.dsl_exit as mod

    @contextlib.contextmanager
    def _cm():
        orig_t, orig_m = mod.time.time, mod.time.monotonic
        mod.time.time = lambda: bar_wall_s
        mod.time.monotonic = lambda: bar_wall_s
        try:
            yield
        finally:
            mod.time.time, mod.time.monotonic = orig_t, orig_m

    return _cm()


def replay_trade(t: dict, candles1m: list[dict], atr_pct: float,
                 policy: ExitPolicy, fill_at_floor: bool):
    """Drive the REAL DSLTracker.check() over 1m bar CLOSES only.

    This mirrors production mark_to_market(): check() is polled with the mark
    (bar close; index==close in replay since a candle is an executed trade, not
    a single-book wick) and exits FILL AT THAT MARK. The wick guard / confirm
    gates inside check() decide whether a stop fires — we never pre-judge a stop
    off the intra-bar high/low (the old harness did, bypassing the guard and
    producing fake stops).

    F2 (fill_at_floor=True): when check() returns a max_loss (hard stop), fill
    at the verdict's floor_price (the stop trigger price), modelling a live
    exchange stop that executes at its trigger instead of the shadow's
    fill-at-confirming-mark overrun. All other exits (trailing/timeout) and the
    base/F1/F3 variants fill at the bar close, exactly like the shadow.

    Returns (exit_px, reason, hold_min)."""
    tracker = DSLTracker(
        coin=t["coin"], side=t["side"], entry_px=t["entry_px"],
        entry_time=t["open_ts"] / 1000.0, policy=policy,
        leverage=t["lev"], entry_atr_pct=atr_pct,
    )
    entry_ms = t["open_ts"]
    for c in candles1m:
        cts = int(c["t"])
        if cts < entry_ms:
            continue
        if cts > t["close_ts"]:
            break
        cl = float(c["c"])
        with _bar_clock(cts / 1000.0):
            v = tracker.check(cl, index_px=cl)
        if getattr(v, "exit", False):
            reason = str(getattr(v, "reason", "") or "")
            if fill_at_floor and reason.startswith("max_loss"):
                floor = getattr(v, "floor_price", None)
                exit_px = float(floor) if floor else cl
                reason = "max_loss@floor"
            else:
                exit_px = cl
            return exit_px, reason, (cts - entry_ms) / 60000.0
    # No DSL exit fired by the actual close time: close at the last available
    # bar close (≈ the real close price). Tagged so we can see model fidelity.
    last = candles1m[-1] if candles1m else None
    px = float(last["c"]) if last else t["actual_px"]
    return px, "eod", (t["close_ts"] - entry_ms) / 60000.0


def pnl_usd(t: dict, exit_px: float) -> float:
    if t["side"] == "long":
        gross = (exit_px - t["entry_px"]) * t["qty"]
    else:
        gross = (t["entry_px"] - exit_px) * t["qty"]
    notional = t["entry_px"] * t["qty"]
    fee = notional * FEE_PCT / 100.0 * 2
    return gross - fee


def stats(pnls):
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    aw = sum(wins) / len(wins) if wins else 0
    al = abs(sum(losses) / len(losses)) if losses else 0
    return {"n": len(pnls), "total": total, "winrate": wr,
            "pf": pf, "avg_win": aw, "avg_loss": al,
            "wins": len(wins), "losses": len(losses)}


def main():
    trades = load_trades()
    print(f"loaded {len(trades)} closed trades")
    # cache candles per coin/window
    paths = {}
    atrs = {}
    for i, t in enumerate(trades):
        coin = t["coin"]
        key = (coin, t["open_ts"] // 3_600_000)
        if key not in paths:
            c1 = fetch_candles(coin, "1m", t["open_ts"] - 60_000,
                               t["close_ts"] + 60_000)
            c4 = fetch_candles(coin, "4h", t["open_ts"] - 40 * 4 * 3_600_000,
                               t["close_ts"])
            paths[key] = c1
            atrs[key] = atr14_pct(c4, t["open_ts"])
            time.sleep(0.1)
        t["_c1"] = paths[key]
        t["_atr"] = atrs[key]
    have = [t for t in trades if t["_c1"]]
    print(f"replayable with 1m data: {len(have)}/{len(trades)}")

    variants = ["prod_prefx",
                "F1fix1.0", "F1fix0.8", "F1fix0.4", "F1fix1.2", "F1fix1.5",
                "F1+be", "F1+F3"]
    # F2 = fill-at-floor; run for every policy variant
    variant_cache = [make_variants(t["_atr"]) for t in have]
    results = {}
    reasons = {}
    for name in variants:
        for floor in (False, True):
            tag = name + ("+F2" if floor else "")
            pnls = []
            rcount = {}
            for t, vc in zip(have, variant_cache):
                pol = vc[name]
                px, reason, hold = replay_trade(t, t["_c1"], t["_atr"],
                                                pol, fill_at_floor=floor)
                p = pnl_usd(t, px)
                pnls.append(p)
                rkey = reason.split(" (")[0].split("@")[0]
                rcount[rkey] = rcount.get(rkey, 0) + 1
            results[tag] = stats(pnls)
            reasons[tag] = rcount
    # actual ledger on the same subset
    actual = stats([t["actual_pnl"] for t in have])
    actual_reasons = {}
    for t in have:
        rk = (t["actual_reason"] or "?").split(" (")[0]
        actual_reasons[rk] = actual_reasons.get(rk, 0) + 1

    print("\n=== RESULT (same %d trades, same price paths) ===" % len(have))
    hdr = f"{'variant':<12} {'n':>3} {'total$':>9} {'win%':>6} {'PF':>5} {'avgW':>6} {'avgL':>6}"
    print(hdr)
    print(f"{'ACTUAL':<12} {actual['n']:>3} {actual['total']:>9.2f} "
          f"{actual['winrate']:>6.1f} {actual['pf']:>5.2f} "
          f"{actual['avg_win']:>6.3f} {actual['avg_loss']:>6.3f}"
          f"   reasons={actual_reasons}")
    order = ["prod_prefx", "prod_prefx+F2",
             "F1fix0.4", "F1fix0.4+F2",
             "F1fix0.8", "F1fix0.8+F2",
             "F1fix1.0", "F1fix1.0+F2",
             "F1fix1.2", "F1fix1.2+F2",
             "F1fix1.5", "F1fix1.5+F2",
             "F1+be", "F1+be+F2",
             "F1+F3", "F1+F3+F2"]
    for tag in order:
        s = results.get(tag)
        if not s:
            continue
        print(f"{tag:<12} {s['n']:>3} {s['total']:>9.2f} "
              f"{s['winrate']:>6.1f} {s['pf']:>5.2f} "
              f"{s['avg_win']:>6.3f} {s['avg_loss']:>6.3f}"
              f"   reasons={reasons.get(tag)}")


if __name__ == "__main__":
    main()
