#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reconcile the "change" SHADOW arms with their counterfactual would-be PnL.

Audit 2026-09-08 (change-arm counterfactual backfill):
The nightly grader (scripts/shadow_grade.py) only upgrades a shadow arm once it
has >= MIN_MATURE_OUTCOMES (20) backfilled counterfactual outcomes AND a
backfilled pnl_usd series to judge against. The block arms already had
reconcile scripts (reconcile_ta_late_entry_shadow.py,
reconcile_pullback_shadow.py), but the three *change* arms — sizing_v2,
atr_regime_calib, confidence_decay — had none, so their records showed
"mature_outcomes=0 / has_pnl=false" forever and could never be graded on
evidence. This script closes that gap. It is pure paper reconciliation: it
never places orders, never writes config, never touches a gate.

A "change" arm does not veto entries the way a block arm does; instead it
*modifies* a parameter that would have applied to the same trade. The
counterfactual question therefore differs per arm:

  * confidence_decay (gate-like change): when `would_block_gate` is true the
    decayed confidence would have dropped the AI conviction below
    min_ai_confidence and the entry would have been skipped. We simulate the
    would-be trade over a fixed 1h-candle hold (same shape as
    reconcile_ta_late_entry_shadow.py) and record its net pnl. A veto is "good"
    when these would-be trades lose. pnl_usd is left unset (these records carry
    no notional); pnl_pct/outcome feed the mature-outcome count.

  * sizing_v2 (notional/stop-width change): v1 (live) and v2 (shadow) widen or
    tighten the stop and, via equal-risk sizing, change the notional. We
    simulate BOTH exits from the same entry using each arm's own stop width.
    To reuse the grader's block-arm convention UNCHANGED ("backfilled
    counterfactual pnl_usd > 0 => the arm's intervention foregoes profit /
    hurts"), we write pnl_usd = (v1 net usd) - (v2 net usd) — the PnL the LIVE
    sizing would make minus what the v2 change would make, i.e. the profit we
    would GIVE UP by adopting the arm. Positive => switching to v2 costs money
    on this opportunity (arm hurts; grader flags REVIEW); negative => the
    change adds value (grader lets it PROMOTE). outcome "win" mirrors the block
    arms: it marks an opportunity where the arm's intervention is associated
    with forgone profit (pnl_usd > 0). cf_v1/cf_v2_pnl_usd keep both absolute
    legs for audit.

  * atr_regime_calib (stop-width factor): same v1-vs-v2 simulation using
    raw_stop_pct (v1) vs calibrated_stop_pct (v2); only `would_change=true`
    records are material. It logs no notional, so we write the forgone pct edge
    pnl_pct = (v1 net pct) - (v2 net pct) (positive => calibration costs
    return) and NO pnl_usd; the grader still counts its mature outcomes but
    cannot apply the pnl_usd REVIEW heuristic to this sub-step (the
    notional-bearing sizing_v2 grade carries the promote decision). atr_calib
    is a sub-step of sizing_v2, so its totals are reported separately and must
    not be summed with sizing_v2.

Maturity / fill convention (kept simple and explicit, mirrors the block-arm
scripts): entry fills at the open of the first 1h bar to open after the signal;
the trade exits at the close of the HOLD_BARS-th later bar, OR earlier if a
stop at that arm's stop width is touched. sizing_v2 / atr_calib records carry
no side and no entry price (they are logged at sizing time before direction is
finalised), so direction defaults to long and entry defaults to that post-signal
bar open. This is an approximation, documented per record via `cf_entry="bar_open"`
and `cf_side_default=true`; it is good enough to accrue a directional-bias-free
expectancy sample, which is what the promote/review gate needs.

Reads each arm's shadow JSONL via the same path resolution the grader uses
(shadow_progress._arm_path), so container env (/data paths) and host defaults
both work. Writes outcome/pnl back in place only with --write; default is a
dry-run report.

Usage (inside the container, or on host with HERMES_*_SHADOW_FILE set):
    python3 scripts/reconcile_change_arms_shadow.py
    python3 scripts/reconcile_change_arms_shadow.py --arms confidence_decay --write
    python3 scripts/reconcile_change_arms_shadow.py --hold-bars 24 --window-hours 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.models.types import Candle  # noqa: E402
import shadow_progress as sp  # noqa: E402

# Round-trip taker fees, bps (matches reconcile_ta_late_entry / reconcile_pullback).
ROUND_TRIP_FEE_BPS = 5.0
HOLD_BARS = 24          # 1h bars to hold the counterfactual trade (~24h)
FETCH_COUNT = 300       # 1h candles fetched per coin (enough headroom)

# arm -> (grader label used in ARMS, env file var, default jsonl name)
CHANGE_ARMS = {
    "sizing_v2": ("sizing_v2", "HERMES_SIZING_V2_SHADOW_FILE", "sizing_v2_shadow.jsonl"),
    "atr_regime_calib": ("atr_regime_calib", "HERMES_ATR_REGIME_CALIB_SHADOW_FILE",
                         "atr_regime_calib_shadow.jsonl"),
    "confidence_decay": ("confidence_decay", "HERMES_CONFIDENCE_DECAY_SHADOW_FILE",
                         "confidence_decay_shadow.jsonl"),
}


def _parse_ts_ms(rec: Dict[str, Any]) -> Optional[float]:
    """Epoch millis from a record; handles `ts` numeric/ms and ISO `timestamp`."""
    for key in ("ts", "timestamp", "bar_close_ms"):
        v = rec.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v) if v > 1e12 else float(v) * 1000.0
        if isinstance(v, str):
            s = v.strip().replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp() * 1000.0
            except ValueError:
                continue
    return None


def _arm_shadow_path(arm: str) -> str:
    """Resolve a shadow JSONL path the same way the grader does.

    Reuses shadow_progress._arm_path with the arm's ARMS registry row so the
    config/env/default precedence matches scripts/shadow_grade.py exactly.
    """
    for label, blk_name, env_file, default_name, mode_key, path_key in sp.ARMS:
        if label == arm:
            try:
                from hermes_trader.agents.config_store import read_agent_config
                cfg = read_agent_config() or {}
            except Exception:
                cfg = {}
            return sp._arm_path(cfg, blk_name, env_file, default_name, path_key)
    # Fallback: env then home default.
    _, env_file, default_name = CHANGE_ARMS[arm]
    env_p = os.environ.get(env_file, "").strip()
    if env_p:
        return os.path.expanduser(env_p)
    return os.path.join(sp.READONLY_HOME, default_name)


def _find_entry_bar(candles: List[Candle], after_ms: float) -> int:
    """Index of the first 1h candle whose OPEN time is strictly after the signal.

    Mirrors reconcile_ta_late_entry_shadow._find_entry_bar (the signal fires
    while the prior bar is still forming; the first bar to open after it is the
    fill bar). Returns -1 if no such bar exists.
    """
    after_dt = datetime.fromtimestamp(after_ms / 1000.0, tz=timezone.utc)
    for i, c in enumerate(candles):
        ct = getattr(c, "t", None)
        if ct is None:
            continue
        bar_dt = datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc)
        if bar_dt > after_dt:
            return i
    return -1


def _simulate(side: str, entry_px: float, stop_pct: float,
              entry_idx: int, candles: List[Candle], hold_bars: int,
              fee_pct: float) -> Tuple[float, str, float]:
    """Walk forward from entry_idx; exit at stop (arm width) or at the hold
    window close. Returns (exit_px, reason, net_pct_pct where pct in percent).

    Long-only stop logic (sizing/atr arms default to long; confidence_decay
    carries its own side). A short stops above entry; symmetric.
    """
    sign = 1.0 if side == "long" else -1.0
    exit_idx = min(entry_idx + hold_bars, len(candles) - 1)
    stop_frac = max(0.0, stop_pct) / 100.0
    for j in range(entry_idx, exit_idx + 1):
        bar = candles[j]
        # Stop touch: adverse excursion beyond the arm's stop width.
        if side == "long":
            adverse = (entry_px - bar.l) / entry_px
        else:
            adverse = (bar.h - entry_px) / entry_px
        if stop_frac > 0 and adverse >= stop_frac:
            exit_px = entry_px * (1.0 - sign * stop_frac)
            gross = sign * (exit_px - entry_px) / entry_px
            return exit_px, "stop", (gross - fee_pct) * 100.0
    exit_px = candles[exit_idx].c
    gross = sign * (exit_px - entry_px) / entry_px
    return exit_px, "window_close", (gross - fee_pct) * 100.0


def _arm_files(path: str) -> List[str]:
    """Active shadow file plus its rotated siblings, oldest->newest.

    Audit 2026-09-08 (rotation-aware backfill): shadow_log.append_jsonl rotates
    daily (and on 10 MiB) through ``<path>.1``..``<path>.BACKUP_COUNT`` (see
    hermes_trader/shadow_log.py). The active ``<path>`` therefore holds only the
    current local day; a window covering mature (~30h) records spans at least
    yesterday's ``.1`` sibling. We merge the active file and the numeric rotated
    siblings, skipping ``.bak-*`` manual snapshots. Order is oldest-first so the
    active (newest) file's records win if the same record id ever appears twice.
    """
    import glob as _glob
    files = []
    # Numeric rotated siblings (.1 .. .5), oldest last; reverse to oldest-first.
    nums = []
    for p in _glob.glob(f"{path}.*"):
        suf = p[len(path) + 1:]
        if suf.isdigit():
            nums.append((int(suf), p))
    for _, p in sorted(nums, key=lambda x: -x[0]):
        files.append(p)
    if os.path.exists(path):
        files.append(path)
    return files


def _load_records(path: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Load records from the active file and all rotated siblings.

    Returns (records, any_file_existed). The returned dicts carry a private
    ``_cf_file`` marker naming the physical file each record belongs to, so
    --write can persist outcomes back to the same file (including read-only-by-
    convention rotated siblings, which are written only with --write).
    """
    files = _arm_files(path)
    if not files:
        return [], False
    recs: List[Dict[str, Any]] = []
    seen = set()
    for fpath in files:
        try:
            fh = open(fpath, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # De-duplicate across siblings by raw record identity. A shadow
                # log line is immutable once written (backfill fields get added
                # in place), so the raw JSON (minus our private marker) is a
                # stable cross-file key; the active/newest copy wins (loaded
                # last). Keying on the full line avoids merging distinct records
                # that merely share (ts, coin).
                rec["_cf_file"] = fpath
                key = json.dumps(
                    {k: v for k, v in rec.items() if k != "_cf_file"},
                    sort_keys=True, ensure_ascii=False)
                if key in seen:
                    continue
                seen.add(key)
                recs.append(rec)
    return recs, True


def _write_back(path: str, records: List[Dict[str, Any]]) -> List[str]:
    """Persist records back to each physical file they were loaded from.

    Groups records by their ``_cf_file`` marker and rewrites each touched file.
    The private marker is stripped on write. Returns the list of files written.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        fpath = rec.get("_cf_file") or path
        groups.setdefault(fpath, []).append(rec)
    written = []
    for fpath, recs in groups.items():
        try:
            with open(fpath, "w", encoding="utf-8") as fh:
                for r in recs:
                    out = {k: v for k, v in r.items() if k != "_cf_file"}
                    fh.write(json.dumps(out, ensure_ascii=False) + "\n")
            written.append(fpath)
        except OSError as e:
            print(f"  [warn] write-back failed for {fpath}: {e}")
    return written


def _settle_confidence_decay(rec: Dict[str, Any], fee_pct: float,
                             hold_bars: int) -> Optional[Dict[str, Any]]:
    """Block-like counterfactual: only would_block_gate=True records are
    skipped trades; score what the skipped trade would have made."""
    if rec.get("would_block_gate") is not True:
        return None
    side = str(rec.get("side") or "").lower()
    if side not in ("long", "short"):
        side = "long" if str(rec.get("verdict", "")).upper().startswith("LONG") else "short"
    return {"side": side, "stop_pct": 0.0, "cf_side_default": rec.get("side") not in ("long", "short")}


def _settle_sizing_v2(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Material only when v2 notional differs >1% from v1 (grader's own rule)."""
    v1 = rec.get("v1_notional_usd")
    v2 = rec.get("v2_notional_usd")
    if not isinstance(v1, (int, float)) or not isinstance(v2, (int, float)) or v1 <= 0:
        return None
    if abs(v2 - v1) / v1 <= 0.01:
        return None
    return {"v1_stop": float(rec.get("v1_stop_pct") or 0.0),
            "v2_stop": float(rec.get("v2_stop_pct") or 0.0),
            "v1_notional": float(v1), "v2_notional": float(v2),
            "side": "long", "cf_side_default": True}


def _settle_atr_calib(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Material only when the calibrated width actually changes (would_change)."""
    if rec.get("would_change") is not True:
        return None
    return {"v1_stop": float(rec.get("raw_stop_pct") or rec.get("core_stop_pct") or 0.0),
            "v2_stop": float(rec.get("calibrated_stop_pct") or 0.0),
            # atr_calib logs no notional; the stop width change feeds position
            # size via equal-risk, so approximate notional inversely to width.
            "v1_notional": 0.0, "v2_notional": 0.0,
            "factor": float(rec.get("factor") or 1.0),
            "side": "long", "cf_side_default": True}


def reconcile_arm(arm: str, records: List[Dict[str, Any]], fee_pct: float,
                  hold_bars: int, window_hours: int) -> List[Dict[str, Any]]:
    """Settle mature, not-yet-outcome records for one arm. Mutates records in
    place (sets outcome / pnl fields); returns the list of settled records."""
    cutoff_ms = time.time() * 1000.0 - window_hours * 3600.0 * 1000.0
    settled: List[Dict[str, Any]] = []
    cache: Dict[str, List[Candle]] = {}

    for rec in records:
        if rec.get("outcome") in ("win", "loss"):
            continue
        ts_ms = _parse_ts_ms(rec)
        if ts_ms is None or ts_ms > cutoff_ms:
            continue
        coin = rec.get("coin")
        if not coin:
            rec["outcome"] = "no_coin"
            continue

        if arm == "confidence_decay":
            plan = _settle_confidence_decay(rec, fee_pct, hold_bars)
        elif arm == "sizing_v2":
            plan = _settle_sizing_v2(rec)
        else:
            plan = _settle_atr_calib(rec)
        if plan is None:
            # Not a material record for this arm (wouldn't have changed
            # anything); mark so we don't re-scan it every night.
            rec["outcome"] = "not_material"
            continue

        if coin not in cache:
            try:
                cache[coin] = fetch_hl_candles(coin, "1h", FETCH_COUNT)
            except Exception as e:  # network / rate-limit: leave for next run
                print(f"  {coin}: fetch error: {e}")
                cache[coin] = []
        candles = cache[coin]
        if not candles:
            continue
        idx = _find_entry_bar(candles, ts_ms)
        if idx < 0 or idx + hold_bars >= len(candles):
            rec["outcome"] = "no_future_bars"
            continue

        entry_px = float(candles[idx].o)
        side = plan["side"]
        rec["cf_entry_px"] = round(entry_px, 6)
        rec["cf_side"] = side
        rec["cf_entry"] = "bar_open"
        if plan.get("cf_side_default"):
            rec["cf_side_default"] = True
        rec["cf_hold_bars"] = hold_bars

        if arm == "confidence_decay":
            # Fixed-hold counterfactual for the skipped trade (stop_pct=0 ->
            # always exits at the hold window, matching ta_late's hold model).
            exit_px, reason, net_pct = _simulate(
                side, entry_px, 0.0, idx, candles, hold_bars, fee_pct)
            rec["cf_exit_px"] = round(exit_px, 6)
            rec["pnl_pct"] = round(net_pct, 4)
            rec["cf_exit_reason"] = reason
            rec["outcome"] = "win" if net_pct > 0 else "loss"
        else:
            # Simulate v1 (live width) and v2 (shadow width) exits, then the
            # incremental PnL of switching to v2.
            e1, r1, net1_pct = _simulate(
                side, entry_px, plan["v1_stop"], idx, candles, hold_bars, fee_pct)
            e2, r2, net2_pct = _simulate(
                side, entry_px, plan["v2_stop"], idx, candles, hold_bars, fee_pct)
            rec["cf_v1_exit_px"] = round(e1, 6)
            rec["cf_v2_exit_px"] = round(e2, 6)
            rec["cf_v1_pnl_pct"] = round(net1_pct, 4)
            rec["cf_v2_pnl_pct"] = round(net2_pct, 4)
            rec["cf_v1_exit_reason"] = r1
            rec["cf_v2_exit_reason"] = r2
            if plan.get("v1_notional", 0.0) > 0:
                usd1 = net1_pct / 100.0 * plan["v1_notional"]
                usd2 = net2_pct / 100.0 * plan["v2_notional"]
                # Counterfactual "profit foregone by adopting the arm", signed
                # like the block arms (positive => the arm hurts / loses us
                # money) so the grader's pnl_usd REVIEW heuristic applies
                # unchanged. cf_v1/cf_v2 legs keep the absolute PnL each sizing
                # would book.
                rec["pnl_usd"] = round(usd1 - usd2, 2)
                rec["cf_v1_pnl_usd"] = round(usd1, 2)
                rec["cf_v2_pnl_usd"] = round(usd2, 2)
            else:
                # atr_calib: no notional logged; fall back to the width-driven
                # pct edge, signed the same way (v1 net pct minus v2 net pct:
                # positive => calibration costs return). pnl_usd omitted so the
                # grader treats it as pct-only / mature outcome count.
                rec["pnl_pct"] = round(net1_pct - net2_pct, 4)
            # outcome mirrors the block-arm convention: "win" marks an
            # opportunity where the arm's intervention is associated with
            # forgone profit (counterfactual pnl > 0 => arm hurts).
            edge = rec["pnl_usd"] if "pnl_usd" in rec else rec.get("pnl_pct", 0.0)
            rec["outcome"] = "win" if edge > 0 else "loss"
        settled.append(rec)
    return settled


def _report(arm: str, settled: List[Dict[str, Any]]) -> None:
    if not settled:
        print(f"[{arm}] no new mature material records to settle")
        return
    wins = [r for r in settled if r["outcome"] == "win"]
    losses = [r for r in settled if r["outcome"] == "loss"]
    print(f"\n=== [{arm}] counterfactual: {len(settled)} mature material records ===")
    print(f"  win rate : {len(wins)}/{len(settled)} = {len(wins)/len(settled)*100:.1f}%")
    pnl_usd = [r["pnl_usd"] for r in settled if isinstance(r.get("pnl_usd"), (int, float))]
    if pnl_usd:
        print(f"  sum pnl_usd (v1-v2 profit foregone by adopting the arm; "
              f">0 = arm hurts -> grader REVIEW): "
              f"{sum(pnl_usd):+.2f} over {len(pnl_usd)} sized records")
    pnl_pct = [r["pnl_pct"] for r in settled if isinstance(r.get("pnl_pct"), (int, float))]
    if pnl_pct:
        print(f"  avg pnl_pct : {sum(pnl_pct)/len(pnl_pct):+.3f}% "
              f"({'block skipped-trade pnl' if arm=='confidence_decay' else 'v1-v2 forgone edge'})")
    reasons: Dict[str, int] = {}
    for r in settled:
        key = r.get("cf_v2_exit_reason") or r.get("cf_exit_reason") or "?"
        reasons[key] = reasons.get(key, 0) + 1
    print(f"  exits     : {reasons}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arms", default="all",
                    help="Comma list of sizing_v2,atr_regime_calib,confidence_decay (default all)")
    ap.add_argument("--hold-bars", type=int, default=HOLD_BARS,
                    help="1h bars to hold each counterfactual trade (default 24)")
    ap.add_argument("--window-hours", type=int, default=30,
                    help="Only settle records older than this many hours (default 30)")
    ap.add_argument("--write", action="store_true",
                    help="Write outcomes back into each JSONL (default: dry-run)")
    args = ap.parse_args()

    arms = list(CHANGE_ARMS) if args.arms == "all" else [
        a.strip() for a in args.arms.split(",") if a.strip() in CHANGE_ARMS]
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0

    for arm in arms:
        path = _arm_shadow_path(arm)
        records, exists = _load_records(path)
        if not exists:
            print(f"[{arm}] shadow file not found: {path} (skipping)")
            continue
        n_files = len(_arm_files(path))
        before = sum(1 for r in records if r.get("outcome") in ("win", "loss"))
        settled = reconcile_arm(arm, records, fee_pct, args.hold_bars, args.window_hours)
        after = sum(1 for r in records if r.get("outcome") in ("win", "loss"))
        print(f"[{arm}] path={path}  files_merged={n_files}  total={len(records)}  "
              f"mature outcomes {before} -> {after}  (settled {len(settled)} this run)")
        _report(arm, settled)

        if args.write and settled:
            written = _write_back(path, records)
            for wp in written:
                print(f"  -> outcomes written back to {wp}")
        elif settled:
            print("  (dry-run; pass --write to persist outcomes into the JSONL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
