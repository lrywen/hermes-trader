#!/usr/bin/env python3
"""Read-only per-exit analysis: do floor_breach exits give back large winners?

Reuses replay_exit_fixes harness plumbing. For the post-fix shadow policy
(F1fix1.0 + F2 fill-at-floor) and the pre-fix control, buckets each trade by
exit reason and reports, per reason: count, total $, win%, mean MFE (peak spot
%), mean realized spot %, and mean GIVEBACK (MFE - realized). A large positive
giveback on floor_breach winners means the trailing floor lets winners round-
trip before exiting -> the remaining optimisation target (F3-style). Small
giveback means exits fire near the peak and there is nothing to reclaim.

Read-only: candleSnapshot only, no orders/state writes.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import replay_exit_fixes as R


def spot_pct(t: dict, exit_px: float) -> float:
    if t["side"] == "long":
        return (exit_px - t["entry_px"]) / t["entry_px"] * 100.0
    return (t["entry_px"] - exit_px) / t["entry_px"] * 100.0


# Live exchange BACKUP-SL width params (executor._resolve_sl_width_config,
# .agent-config.json top-level). The disaster net fires only on a gap that
# trades THROUGH the wider backup trigger; normal exits fill as software IOC at
# the confirming mark. C4-2: mult/floor resolve from the SHARED dsl_exit.atr_stop
# block (== the DSL policy), backup keeps its own narrower ceiling.
_BACKUP_SL_ATR_MULT = 1.2   # config sl_atr_mult (mirrors dsl_exit.atr_stop.atr_mult)
_BACKUP_SL_FLOOR_PCT = 1.2  # config sl_floor_pct
_BACKUP_SL_CEILING_PCT = 3.0  # config sl_ceiling_pct
_BACKUP_COIN_FLOOR = {"HYPE": 1.5, "PURR": 1.2, "BOME": 1.2}


def _backup_trigger(t: dict) -> float:
    """Worst-case price the live backup SL allows before the exchange stop fires.

    width_pct = min(max(entry_atr_pct*mult, floor), ceiling); trigger sits that
    far on the adverse side of entry. (Slip-widening ignored — needs live
    avg_exit_slip_bps the shadow book has no access to; its effect is sub-floor.)
    """
    atr = float(t.get("_atr", 0.0) or 0.0)
    floor = _BACKUP_COIN_FLOOR.get(t["coin"], _BACKUP_SL_FLOOR_PCT)
    width = min(max(atr * _BACKUP_SL_ATR_MULT, floor), _BACKUP_SL_CEILING_PCT)
    if t["side"] == "long":
        return float(t["entry_px"]) * (1.0 - width / 100.0)
    return float(t["entry_px"]) * (1.0 + width / 100.0)


def _gapped_through(t: dict, mark: float, trig: float) -> bool:
    """True when the confirming mark has traded PAST the backup trigger (gap)."""
    if t["side"] == "long":
        return mark < trig
    return mark > trig


def run_tag(have, variant_cache, name: str, fill_mode: str):
    """fill_mode:
      'mark'      — shadow fills EVERY exit at the confirming mark.
      'hard_floor'— F2 (rejected): only max_loss fills at its DSL stop floor.
      'all_floor' — F2-ext (rejected): max_loss AND floor_breach fill at floor.
      'gap_cap'   — F6 parity: every exit fills at the mark (= live software IOC),
                    EXCEPT a max_loss whose confirming mark has gapped THROUGH the
                    wider live backup-SL trigger — then the fill is capped at the
                    backup trigger (the price the live disaster net guarantees).
    """
    rows = []
    for t, vc in zip(have, variant_cache):
        pol = vc[name]
        tracker = R.DSLTracker(
            coin=t["coin"], side=t["side"], entry_px=t["entry_px"],
            entry_time=t["open_ts"] / 1000.0, policy=pol,
            leverage=t["lev"], entry_atr_pct=t["_atr"],
        )
        exit_px = reason = None
        capped = False
        trig = _backup_trigger(t)
        entry_ms = t["open_ts"]
        for c in t["_c1"]:
            cts = int(c["t"])
            if cts < entry_ms:
                continue
            if cts > t["close_ts"]:
                break
            cl = float(c["c"])
            with R._bar_clock(cts / 1000.0):
                v = tracker.check(cl, index_px=cl)
            if getattr(v, "exit", False):
                reason = str(getattr(v, "reason", "") or "")
                mfe = float(getattr(v, "mfe_pct", 0.0) or 0.0)
                rkey0 = reason.split(" (")[0].split("@")[0]
                use_floor = (
                    (fill_mode == "hard_floor" and rkey0 == "max_loss")
                    or (fill_mode == "all_floor"
                        and rkey0 in ("max_loss", "floor_breach"))
                )
                if use_floor:
                    fl = getattr(v, "floor_price", None)
                    exit_px = float(fl) if fl else cl
                elif (fill_mode == "gap_cap" and rkey0 == "max_loss"
                      and _gapped_through(t, cl, trig)):
                    # Gap punched through the live disaster net: live fills at the
                    # exchange trigger (capped), never at the post-gap mark.
                    exit_px = trig
                    capped = True
                else:
                    exit_px = cl
                break
        if exit_px is None:
            last = t["_c1"][-1] if t["_c1"] else None
            exit_px = float(last["c"]) if last else t["actual_px"]
            reason = "eod"
            mfe = 0.0
        rkey = reason.split(" (")[0].split("@")[0]
        realized = spot_pct(t, exit_px)
        usd = R.pnl_usd(t, exit_px)
        rows.append({"reason": rkey, "mfe": mfe, "realized": realized,
                     "usd": usd, "coin": t["coin"], "side": t["side"],
                     "capped": capped})
    return rows


def summarize(rows):
    by = {}
    for r in rows:
        by.setdefault(r["reason"], []).append(r)
    print(f"{'reason':<16} {'n':>3} {'tot$':>8} {'win%':>5} "
          f"{'mfe%':>6} {'real%':>6} {'givebk%':>8}")
    for reason, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        tot = sum(x["usd"] for x in rs)
        wr = sum(1 for x in rs if x["usd"] > 0) / n * 100
        mfe = sum(x["mfe"] for x in rs) / n
        real = sum(x["realized"] for x in rs) / n
        # giveback only meaningful where a favorable peak existed
        gb = [x["mfe"] - x["realized"] for x in rs if x["mfe"] > 0.05]
        gbm = sum(gb) / len(gb) if gb else 0.0
        print(f"{reason:<16} {n:>3} {tot:>8.2f} {wr:>5.0f} "
              f"{mfe:>6.2f} {real:>6.2f} {gbm:>8.2f}")


def main():
    trades = R.load_trades()
    paths, atrs = {}, {}
    for t in trades:
        key = (t["coin"], t["open_ts"] // 3_600_000)
        if key not in paths:
            c1 = R.fetch_candles(t["coin"], "1m", t["open_ts"] - 60_000,
                                 t["close_ts"] + 60_000)
            c4 = R.fetch_candles(t["coin"], "4h",
                                 t["open_ts"] - 40 * 4 * 3_600_000,
                                 t["close_ts"])
            paths[key] = c1
            atrs[key] = R.atr14_pct(c4, t["open_ts"])
        t["_c1"], t["_atr"] = paths[key], atrs[key]
    have = [t for t in trades if t["_c1"]]
    print(f"replayable: {len(have)}/{len(trades)}")
    variant_cache = [R.make_variants(t["_atr"]) for t in have]

    def agg(rows):
        n = len(rows)
        tot = sum(x["usd"] for x in rows)
        wins = [x["usd"] for x in rows if x["usd"] > 0]
        los = [x["usd"] for x in rows if x["usd"] <= 0]
        pf = (sum(wins) / abs(sum(los))) if los and sum(los) else float("inf")
        wr = len(wins) / n * 100 if n else 0
        return tot, pf, wr

    print("\n=== FILL-MODE comparison on F1fix1.0 (post-fix shadow stop) ===")
    modes = [("mark (all at confirm mark)", "mark"),
             ("hard_floor (F2: max_loss@floor)", "hard_floor"),
             ("gap_cap (F6: mark unless gap>backup SL)", "gap_cap"),
             ("all_floor (F2-ext: max_loss+breach@floor)", "all_floor")]
    cached = {m: run_tag(have, variant_cache, "F1fix1.0", fill_mode=m)
              for _, m in modes}
    print(f"{'mode':<44} {'tot$':>8} {'PF':>5} {'win%':>5}")
    for label, m in modes:
        tot, pf, wr = agg(cached[m])
        print(f"{label:<44} {tot:>8.2f} {pf:>5.2f} {wr:>5.1f}")

    # F6: how often the backup net actually binds in the replay, and the $ it
    # reclaims versus pure-mark (which overstates gap losses on 1m-bar granularity).
    print("\n=== F6 gap_cap: max_loss exits capped at the live backup-SL trigger ===")
    print(f"{'coin':<9} {'side':<5} {'real%mark':>10} {'real%cap':>9} "
          f"{'d$':>7}")
    reclaim = 0.0
    ncap = 0
    for i, rc in enumerate(cached["gap_cap"]):
        if not rc.get("capped"):
            continue
        ncap += 1
        rm = cached["mark"][i]
        d = rc["usd"] - rm["usd"]
        reclaim += d
        print(f"{rc['coin']:<9} {rc['side']:<5} {rm['realized']:>10.2f} "
              f"{rc['realized']:>9.2f} {d:>7.3f}")
    print(f"gap-capped max_loss exits: {ncap}; F6 reclaim vs pure-mark: ${reclaim:.2f}")

    print("\n=== all_floor: per-reason breakdown ===")
    summarize(cached["all_floor"])

    # per-trade delta of F2-ext over F2 on the floor_breach winners
    print("\n=== floor_breach trades: mark vs floor fill (F2-ext reclaim) ===")
    mk = { (r["coin"], r["side"], i): r for i, r in enumerate(cached["mark"]) }
    print(f"{'coin':<9} {'side':<5} {'real%mark':>10} {'real%flr':>9} {'d$':>7}")
    gain = 0.0
    for i, rf in enumerate(cached["all_floor"]):
        if rf["reason"] != "floor_breach":
            continue
        rm = cached["mark"][i]
        d = rf["usd"] - rm["usd"]
        gain += d
        if abs(d) > 0.005:
            print(f"{rf['coin']:<9} {rf['side']:<5} {rm['realized']:>10.2f} "
                  f"{rf['realized']:>9.2f} {d:>7.3f}")
    print(f"total F2-ext reclaim on floor_breach: ${gain:.2f}")


if __name__ == "__main__":
    main()
