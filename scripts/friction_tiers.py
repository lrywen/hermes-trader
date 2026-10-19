#!/usr/bin/env python3
"""P1-6 (2026-10-19): realized-friction table across three notional tiers.

Reads the production shadow book (``/data/.shadow-book.json``) and replays the
SAME recorded entry/exit prints at three intended notionals — $30 / $100 /
$1000 — to show how the round-trip cost stack (fee + modeled slippage + maker
edge) and the net result translate at each size.

Why friction is (mostly) size-invariant here
--------------------------------------------
The shadow fill model prices everything as a *fraction of mid*:
  * taker fee 0.045%/side (9 bp round trip), maker fee 0.015%/side (3 bp);
  * taker slippage a flat 0.05%/side (10 bp round trip) once the slip-era field
    exists (post 2026-10-10); pre-era fills carry no modeled slippage;
  * maker posts ~5 bp through mid (maker_edge_bps) and exits at mid.
So every bp component is independent of notional; dollar cost scales linearly.
The two genuinely size-sensitive effects are modeled explicitly:
  1. Hyperliquid's $10.5 minimum + per-coin szDecimals rounding. A $30 target on
     an integer-size / high-priced coin can only be sent as a discrete size, so
     the *real* fill notional is ceil-to-tick. Supply ``--meta`` (a JSON map of
     coin -> szDecimals, e.g. exported from the exchange meta) to enable it;
     without meta the run is the pure-linear model and the rounding column is
     flagged unavailable.
  2. TP leg mechanics: DSL scale-out legs whose slice is below the $10.5 minimum
     are skipped/upsized on a micro account. The recorded ``reason`` already
     reflects the exit that actually happened at ~$30; the report buckets
     reasons so a reader can see how often a small account exits via a
     stop/time exit instead of a profitable TP scale-out.

This is an offline, read-only analyzer: it never places orders and never writes
the shadow book.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import Counter
from typing import Any

DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
DEFAULT_BOOK = os.path.join(DATA_DIR, ".shadow-book.json")

# Epoch-ms cutover when taker close fills started carrying slippage_pct
# (commit 2a8ba70; pre-era modeled slippage is unknown, not zero).
SLIP_ERA_MS = 1_791_639_388_000

DEFAULT_TIERS = (30.0, 100.0, 1000.0)
MIN_ORDER_USD = 10.5  # Hyperliquid hard floor ($10 + buffer); mirrors exchange.MIN_ORDER_USD

ACCOUNTS = ("taker", "maker_shadow")


# --------------------------------------------------------------------------- helpers

def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * q
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _reason_group(reason: str) -> str:
    return str(reason).split(" (")[0].split(";")[0].strip() or "unknown"


def load_meta(path: str | None) -> dict[str, int]:
    """Optional coin -> szDecimals map for minimum-order/tick rounding."""
    if not path or not os.path.isfile(path):
        return {}
    with open(path) as fh:
        raw = json.load(fh)
    # Accept either {coin: int} or the exchange meta shape
    # {"universe":[{"name":..,"szDecimals":..}]} / list of {name,szDecimals}.
    if isinstance(raw, dict) and "universe" in raw:
        raw = raw["universe"]
    if isinstance(raw, list):
        return {str(r.get("name")): int(r.get("szDecimals", 5)) for r in raw if r.get("name")}
    return {str(k): int(v) for k, v in raw.items()}


# --------------------------------------------------------------------------- per-fill model

def _direction(side: str) -> int:
    return -1 if side == "short" else 1


def round_trip_notional(coin: str, target: float, entry_px: float,
                        meta: dict[str, int]) -> tuple[float, float, bool]:
    """Map an intended entry notional to the real sendable notional after
    HL's minimum-order + szDecimals ceil-to-tick rule.

    Returns (real_notional, uplift_ratio, floor_bound). ``floor_bound`` is True
    only when the $10.5 MINIMUM (not mere tick rounding) forced the size above
    the target — i.e. the size-sensitive effect that actually bites a micro
    account. Pure ceil-to-tick residual at a large target is noise, not a
    minimum-order effect. Without szDecimals for the coin it returns the
    continuous target.
    """
    if target <= 0 or entry_px <= 0 or coin not in meta:
        return target, 1.0, False
    sz_dec = max(0, int(meta[coin]))
    tick = 10.0 ** (-sz_dec)
    min_size = math.ceil((MIN_ORDER_USD / entry_px) / tick) * tick
    want_size = target / entry_px
    floor_bound = min_size > want_size
    size = max(want_size, min_size)
    size = float(f"{size:.{sz_dec}f}")
    real = size * entry_px
    return real, (real / target if target else 1.0), floor_bound


def decompose_fill(close: dict[str, Any], open_fill: dict[str, Any] | None,
                   acct: str) -> dict[str, float]:
    """Return the size-invariant bp components of one round trip, recomputed
    from the recorded prices rather than trusted from summary fields.

    Conventions (shadow book):
      gross_bps  = signed spot move * 1e4, measured on ACTUAL fill prices
                   (taker: includes the modeled per-side slippage post-era;
                    maker: entry is the passive limit ~+5bp edge, exit at mid);
      fee_bps    = fee_usd / notional * 1e4 (9 taker / 3 maker by construction);
      net_bps    = realized_pnl_usd / notional * 1e4.
    """
    notional = _f(close.get("notional_usd"))
    entry_px = _f(close.get("entry_px"))
    exit_px = _f(close.get("price"))
    direction = _direction(str(close.get("side")))
    if notional > 0:
        net_bps = _f(close.get("realized_pnl_usd")) / notional * 1e4
        fee_bps = _f(close.get("fee_usd")) / notional * 1e4
        gross_bps = _f(close.get("gross_pnl_usd")) / notional * 1e4
    else:
        net_bps = fee_bps = gross_bps = 0.0

    # Decision-mid gross (stripped of modeled slippage / maker edge) lets the
    # report separate "raw directional result" from "execution friction".
    # taker close carries mid_px at exit; entry mid is on the open fill.
    mid_gross_bps: float | None = None
    if acct == "taker" and open_fill is not None:
        entry_mid = _f(open_fill.get("mid_px"))
        exit_mid = _f(close.get("mid_px"))
        if entry_mid > 0 and exit_mid > 0:
            mid_gross_bps = direction * (exit_mid - entry_mid) / entry_mid * 1e4

    # Pure execution friction = actual-fill gross minus decision-mid gross.
    # Positive number = cost in bp the fill model gave up vs untouched mid.
    friction_bps: float | None = None
    if mid_gross_bps is not None:
        friction_bps = gross_bps - mid_gross_bps  # negative => fills worse than mid

    return {
        "gross_bps": gross_bps,
        "mid_gross_bps": mid_gross_bps if mid_gross_bps is not None else float("nan"),
        "fill_friction_bps": friction_bps if friction_bps is not None else float("nan"),
        "fee_bps": fee_bps,
        "net_bps": net_bps,
        "entry_px": entry_px,
        "exit_px": exit_px,
        "direction": float(direction),
    }


# --------------------------------------------------------------------------- aggregation

def summarize_bps(values: list[float]) -> dict[str, Any]:
    vals = [v for v in values if v is not None and not math.isnan(v)]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 2),
        "median": round(statistics.median(vals), 2),
        "p10": round(_pct(vals, 0.10), 2),
        "p90": round(_pct(vals, 0.90), 2),
    }


def analyze_account(book: dict[str, Any], acct: str, tiers: tuple[float, ...],
                    meta: dict[str, int]) -> dict[str, Any]:
    fills = book["accounts"][acct]["fills"]
    closes = [f for f in fills if f.get("type") == "close"]
    opens_by_id = {f.get("position_id"): f for f in fills if f.get("type") == "open"}
    opens_by_aid = {f.get("analysis_id"): f for f in fills if f.get("type") == "open"}

    rows: list[dict[str, Any]] = []
    pre_era = 0
    uplift_buckets: dict[float, list[float]] = {t: [] for t in tiers}
    floor_buckets: dict[float, list[bool]] = {t: [] for t in tiers}

    for c in closes:
        open_fill = opens_by_id.get(c.get("position_id")) or opens_by_aid.get(c.get("analysis_id"))
        comp = decompose_fill(c, open_fill, acct)
        if _f(c.get("ts")) < SLIP_ERA_MS:
            pre_era += 1
        coin = str(c.get("coin"))
        entry_px = comp["entry_px"]
        tier_view = {}
        for t in tiers:
            real_n, uplift, floor_bound = round_trip_notional(coin, t, entry_px, meta)
            uplift_buckets[t].append(uplift)
            floor_buckets[t].append(floor_bound)
            # bps are size-invariant; dollars scale with the REAL sendable notional.
            tier_view[t] = {
                "real_notional": round(real_n, 4),
                "floor_bound": floor_bound,
                "gross_usd": round(real_n * comp["gross_bps"] / 1e4, 4),
                "fee_usd": round(real_n * comp["fee_bps"] / 1e4, 4),
                "net_usd": round(real_n * comp["net_bps"] / 1e4, 4),
            }
        rows.append({
            "coin": coin,
            "side": c.get("side"),
            "reason": _reason_group(str(c.get("reason"))),
            "reason_raw": c.get("reason"),
            "book_notional": round(_f(c.get("notional_usd")), 4),
            "mfe_pct": _f(c.get("mfe_pct")),
            "pre_slip_era": _f(c.get("ts")) < SLIP_ERA_MS,
            **comp,
            "tiers": tier_view,
        })

    bp_table = {
        "gross_bps": summarize_bps([r["gross_bps"] for r in rows]),
        "fee_bps": summarize_bps([r["fee_bps"] for r in rows]),
        "net_bps": summarize_bps([r["net_bps"] for r in rows]),
        "decision_mid_gross_bps": summarize_bps([r["mid_gross_bps"] for r in rows]),
        "fill_friction_vs_mid_bps": summarize_bps([r["fill_friction_bps"] for r in rows]),
    }

    # TP scale-out leg feasibility (size-sensitive exit MECHANISM, not cost).
    # DSL first TP leg is tp_scale_fraction=0.40 of the position; HL rejects a
    # reducing leg below ~$10.5. At $30 the 40% leg is $12 (just legal); at
    # $100/$1k it is comfortably legal. We cannot replay the K-line path
    # offline, so we report feasibility + how many fills saw MFE touch the
    # first phase2 tier (+2% spot) without recording a TP exit. NOTE: MFE touch
    # != a confirmed scale-out trigger (needs the close/retrace rule), and at
    # $30 the leg is already legal, so a zero recorded-TP count is NOT shown as
    # a size effect — it is reported for transparency only.
    tp_scale_fraction = 0.40
    tp_first_tier_spot_pct = 2.0
    reached_tier = [r for r in rows if r["mfe_pct"] >= tp_first_tier_spot_pct]
    tp_feasibility = {}
    for t in tiers:
        leg_notional = t * tp_scale_fraction
        tp_feasibility[t] = {
            "first_tp_leg_notional_usd": round(leg_notional, 2),
            "leg_meets_min_order": leg_notional >= MIN_ORDER_USD,
        }
    tp_scale = {
        "first_tp_leg_fraction": tp_scale_fraction,
        "first_tier_reached_mfe_spot_pct": tp_first_tier_spot_pct,
        "fills_reaching_first_tier": len(reached_tier),
        "fills_reaching_first_tier_pct": round(100.0 * len(reached_tier) / len(rows), 1) if rows else None,
        "recorded_tp_scaleout_exits": sum(
            1 for r in rows if "phase2" in str(r["reason_raw"]).lower()
            or "take_profit" in str(r["reason_raw"]).lower() or "tp " in str(r["reason_raw"]).lower()),
        "tiers": tp_feasibility,
    }

    tier_summary = {}
    for t in tiers:
        nets = [r["tiers"][t]["net_usd"] for r in rows]
        fees = [r["tiers"][t]["fee_usd"] for r in rows]
        gross = [r["tiers"][t]["gross_usd"] for r in rows]
        wins = sum(1 for v in nets if v > 0)
        uplifts = uplift_buckets[t]
        floors = floor_buckets[t]
        floor_n = sum(1 for b in floors if b)
        floor_uplifts = [u for u, b in zip(uplifts, floors) if b]
        tier_summary[t] = {
            "fills": len(rows),
            "win_rate_pct": round(100.0 * wins / len(rows), 1) if rows else None,
            "sum_gross_usd": round(sum(gross), 3),
            "sum_fee_usd": round(sum(fees), 3),
            "sum_net_usd": round(sum(nets), 3),
            "mean_net_usd": round(statistics.fmean(nets), 4) if nets else None,
            "median_net_usd": round(statistics.median(nets), 4) if nets else None,
            "mean_fee_round_trip_usd": round(statistics.fmean(fees), 4) if fees else None,
            "sz_rounding": {
                "meta_available": bool(meta),
                "min_floor_bound_fills": floor_n,
                "min_floor_bound_pct": round(100.0 * floor_n / len(floors), 1) if floors else None,
                "floor_uplift_ratio_median": round(statistics.median(floor_uplifts), 3) if floor_uplifts else None,
                "floor_uplift_ratio_max": round(max(floor_uplifts), 3) if floor_uplifts else None,
            },
        }

    return {
        "account": acct,
        "fills": len(rows),
        "pre_slip_era_fills": pre_era,
        "bps": bp_table,
        "reasons": dict(Counter(r["reason"] for r in rows).most_common()),
        "tp_scale_out": tp_scale,
        "tiers": tier_summary,
    }


# --------------------------------------------------------------------------- report

def print_text(report: dict[str, Any], tiers: tuple[float, ...], meta_used: bool) -> None:
    print("=" * 78)
    print("P1-6  REALIZED FRICTION ACROSS NOTIONAL TIERS  (shadow book, read-only)")
    print("=" * 78)
    print(f"book         : {report['book_path']}")
    print(f"tiers (USD)  : {', '.join(f'${t:g}' for t in tiers)}")
    print(f"sz rounding  : {'enabled (--meta coin->szDecimals)' if meta_used else 'OFF (pure-linear; pass --meta for $10.5/tick uplift)'}")
    for acct in report["accounts"]:
        a = report["accounts"][acct]
        print("\n" + "-" * 78)
        print(f"ACCOUNT {acct}   fills={a['fills']}   pre-slip-era fills={a['pre_slip_era_fills']}")
        print("-" * 78)
        print("size-invariant bp components (per round trip):")
        for name, label in (
            ("decision_mid_gross_bps", "  decision-mid gross "),
            ("fill_friction_vs_mid_bps", "  fill friction vs mid (taker only, <=0 = cost)"),
            ("gross_bps", "  actual-fill gross "),
            ("fee_bps", "  fee               "),
            ("net_bps", "  NET               "),
        ):
            s = a["bps"][name]
            if s.get("n"):
                print(f"{label}: n={s['n']:3d}  median={s['median']:8.2f}bp  "
                      f"mean={s['mean']:8.2f}  p10={s['p10']:8.2f}  p90={s['p90']:8.2f}")
            else:
                print(f"{label}: n=0 (n/a for this account)")
        print("\nexit-reason mix at recorded size (TP scale-out visibility):")
        for reason, cnt in a["reasons"].items():
            print(f"  {cnt:4d}  {reason}")
        tps = a["tp_scale_out"]
        print("\nTP scale-out leg feasibility (exit MECHANISM vs size; diagnostic):")
        print(f"  fills whose MFE reached first tier (+"
              f"{tps['first_tier_reached_mfe_spot_pct']:.0f}% spot): "
              f"{tps['fills_reaching_first_tier']}/{a['fills']} "
              f"({tps['fills_reaching_first_tier_pct']}%); "
              f"recorded TP scale-out exits: {tps['recorded_tp_scaleout_exits']}")
        for t in tiers:
            z = tps["tiers"][t]
            ok = "legal" if z["leg_meets_min_order"] else "BELOW $10.5 — leg skipped/upsized"
            print(f"  ${t:>5g}: 40% first-TP leg = ${z['first_tp_leg_notional_usd']:>6.2f}  → {ok}")
        print("\nper-tier dollars (same fills replayed at each intended notional):")
        hdr = f"  {'tier':>8} {'win%':>6} {'sumGross':>10} {'sumFee':>9} {'sumNet':>10} {'medNet':>9} {'fee/RT':>8}"
        if meta_used:
            hdr += f" {'$10.5floor%':>11} {'flrUpMed':>9} {'flrUpMax':>9}"
        print(hdr)
        for t in tiers:
            x = a["tiers"][t]
            line = (f"  ${t:>7g} {str(x['win_rate_pct']):>6} {x['sum_gross_usd']:>10.3f} "
                    f"{x['sum_fee_usd']:>9.3f} {x['sum_net_usd']:>10.3f} "
                    f"{x['median_net_usd']:>9.4f} {x['mean_fee_round_trip_usd']:>8.4f}")
            if meta_used:
                sr = x["sz_rounding"]
                line += (f" {str(sr['min_floor_bound_pct']):>11} "
                         f"{str(sr['floor_uplift_ratio_median']):>9} {str(sr['floor_uplift_ratio_max']):>9}")
            print(line)
    print("\nnotes:")
    print("  * bp components are size-invariant in the shadow model; dollar PnL/fee")
    print("    scale with the real (post-rounding) notional.")
    print("  * FEE ERA: older fills carry 5bp taker / 2bp maker round trip; newer")
    print("    (post slip-era) fills carry 9bp / 3bp. The median fee bp reflects this mix.")
    print("  * pre-slip-era taker fills have NO modeled slippage (unknown, not 0).")
    print("  * fill friction vs mid is only separable for taker (mid stored on fills).")
    print("  * TP leg reach is MFE-based and diagnostic; a scale-out PnL counterfactual")
    print("    needs a K-line replay and is intentionally NOT claimed here.")
    print("  * this is a realized-friction replay, not a new edge estimate.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", default=DEFAULT_BOOK, help="path to .shadow-book.json")
    ap.add_argument("--tier", type=float, action="append",
                    help="notional tier USD (repeatable; default 30/100/1000)")
    ap.add_argument("--meta", default=None,
                    help="optional JSON: coin->szDecimals (or exchange meta universe)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    with open(args.book) as fh:
        book = json.load(fh)
    tiers = tuple(args.tier) if args.tier else DEFAULT_TIERS
    meta = load_meta(args.meta)

    accounts = {}
    for acct in ACCOUNTS:
        if acct in book.get("accounts", {}):
            accounts[acct] = analyze_account(book, acct, tiers, meta)

    report = {"book_path": args.book, "tiers": list(tiers),
              "sz_meta_available": bool(meta), "accounts": accounts}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_text(report, tiers, bool(meta))


if __name__ == "__main__":
    main()
