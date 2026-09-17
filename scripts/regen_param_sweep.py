#!/usr/bin/env python3
"""Regenerating-signal replayer + walk-forward parameter sweep for the
K-line-reproducible shadow arms (ta_late_entry / trend_filter_200ma /
daily_extension_cap, plus the relax_tier probe trio).

Why this exists: the live shadow logs only cover ~2 weeks and only the
candidates the live agent happened to evaluate (recording selection bias).
Every rule these arms enforce is a pure function of closed candles, so the
full candidate space can be REGENERATED over a much longer window: every
4h bar close x every coin x both sides is an entry-intent candidate, scored
by the SAME decision functions the live gate calls, then graded forward.

Design contract:
  * decision math is never re-implemented for measurement: each candidate
    calls hermes_trader.agents.ta_filter.late_entry_check directly (the
    single source of truth). Parameter SWEEPS then replay the block verdict
    arithmetically from the measured (rsi4h, adx4h, extension, trend_dir)
    via replay_late_entry_block / replay_relax_tier, which are locked to
    late_entry_check / relax_tier_check by property tests
    (tests/test_regen_param_sweep.py). Production runs with
    mtf_enabled=false, so the 15m override never participates and no 15m
    series is needed.
  * point-in-time: a candidate at 4h bar i is decided on bars[:i+1] only
    (that bar had just closed); forward grading uses bars[i+1..] — the same
    B1/hold-bars geometry and 5bps round-trip fee as
    backfill_ta_late_entry_historical (B1 = first bar opening at/after the
    signal, entry = B0.close chase approximation, exit = close of bar
    b1_idx-2+hold_bars, mae = worst side-aware close over [B0, exit]).
  * 24h change (trend_filter mover bypass / daily_extension_cap) and the
    200SMA are recomputed from closed 1h/1d bars. APPROXIMATION: live reads
    the 24h % from the universe snapshot's prevDayPx (not reproducible);
    we use close(t)/close(t-24h)-1 from closed 1h bars instead.
  * walk-forward: the window is split 60/20/20 by time into
    train/validate/test; the test segment covers the live 2 weeks so the
    overlap check (regenerated vs live shadow rows) validates the replay
    on the exact segment the production gate saw.
  * overlap check: each live row (coin, ts) maps to the candidate whose
    deciding bar is the newest bar closed at ts; measured
    rsi4h/adx4h/extension and the base-parameter block verdict are compared
    (fetch-window tail differences: live fetches 100 bars incl. the forming
    one -> 99 closed, we slice up to 100 closed — Wilder-smoothed tails
    agree to ~1e-6).
  * safety: dry-run by default; --write emits ONE isolated JSON report
    (never any live JSONL). Sets HERMES_BACKTEST=1, never orders.

Usage:
    python3 scripts/regen_param_sweep.py --days 120            # dry-run
    python3 scripts/regen_param_sweep.py --days 120 --write
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.ta_filter import late_entry_check  # noqa: E402
from hermes_trader.data import historical_candles as hc  # noqa: E402
from hermes_trader.indicators.math import sma  # noqa: E402

BAR_4H = hc.INTERVAL_MS["4h"]
BAR_1H = hc.INTERVAL_MS["1h"]
BAR_1D = hc.INTERVAL_MS["1d"]

ROUND_TRIP_FEE_BPS = 5.0
HOLD_BARS = 2                # ta_late_entry geometry (8h); 72h arms use 18
HOLD_BARS_72H = 18
FETCH_BARS_4H = 100          # mirrors ta_late_entry.fetch_bars in production
MIN_BARS_4H = 30
SMA_PERIOD = 200
SMA_WARMUP_DAYS = 215        # extra daily history pulled for the 200SMA
CONTEXT_4H_DAYS = 18         # extra 4h history for indicator warm-up (~108 bars)

DEFAULT_UNIVERSE_FILE = "/data/ta_late_entry_shadow.jsonl"
DEFAULT_OUT = "/data/regen_param_sweep_report.json"

# Production baseline (audited 2026-09-15 from /data/.agent-config.json).
BASE_PARAMS: dict[str, Any] = {
    "mode": "enforce",
    "rsi_ob": 75, "rsi_os": 25,
    "ext_ob": 2.5, "ext_os": -2.5,
    "trend_relax_enabled": True,
    "adx_trend_threshold": 35,
    "rsi_ob_relaxed": 82, "rsi_os_relaxed": 18,
    "ext_ob_relaxed": 3.5, "ext_os_relaxed": -3.5,
    "relax_tier_probe_enabled": True,
    "rt_relax_adx": 45, "rt_weak_adx": 35,
    "rt_weak_rsi_long": 70, "rt_weak_rsi_short": 30,
    "rt_no_trend_adx": 20,
    "mtf_enabled": False,
    "rsi15m_ob": 72, "rsi15m_os": 28,
    "min_bars_4h": 30, "min_bars_15m": 20,
    "fetch_bars": 100,
}
BASE_TREND: dict[str, Any] = {
    "period": 200, "block_unknown": False,
    "allow_daily_mover_long_bypass": True,
    "daily_mover_min_ext_pct": 10.0, "daily_mover_max_ext_pct": 30.0,
}
BASE_EXT_CAP = 30.0          # override_max_daily_extension_pct

# Sweep axes (symmetric long/short mirroring, as in production).
GRID_RSI_OB = [70.0, 72.5, 75.0, 77.5, 80.0]
GRID_EXT_OB = [2.0, 2.25, 2.5, 2.75, 3.0]
GRID_ADX_FLOOR = [35.0, 40.0, 45.0]
GRID_RELAX_ENABLED = [True, False]
GRID_CAP = [15.0, 20.0, 25.0, 30.0, 35.0, 40.0]
GRID_MOVER = [
    (10.0, 30.0), (15.0, 35.0), (20.0, 40.0),
    (0.0, 1e9),   # bypass any mover
    None,         # bypass disabled
]

WF_RATIOS = (0.6, 0.2, 0.2)  # train / validate / test
MAX_HARMFUL_RATE = 0.5       # shadow_grade REVIEW red line


# ---------------------------------------------------------------------------
# Live universe / records
# ---------------------------------------------------------------------------

def _iter_input_files(primary: str) -> list[str]:
    files = sorted(glob.glob(primary + ".*"), reverse=True)
    if os.path.exists(primary):
        files.append(primary)
    seen, out = set(), []
    for f in files:
        if os.path.abspath(f) not in seen:
            seen.add(os.path.abspath(f))
            out.append(f)
    return out


def _signal_ms(rec: dict[str, Any]) -> Optional[int]:
    ts = rec.get("timestamp", rec.get("ts"))
    if isinstance(ts, (int, float)):
        v = int(ts)
        return v if v > 10_000_000_000 else v * 1000
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def load_live_records(primary: str) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    seen_ids: set[tuple[Any, ...]] = set()
    for path in _iter_input_files(primary):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = (r.get("coin"), r.get("timestamp"), r.get("entry_px"))
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                recs.append(r)
    return recs


def load_live_coins(primary: str) -> list[str]:
    coins = {str(r.get("coin")) for r in load_live_records(primary)
             if r.get("coin")}
    return sorted(coins)


# ---------------------------------------------------------------------------
# Candidate generation (measurement via the single source of truth)
# ---------------------------------------------------------------------------

def forward_grade(bars: list, i: int, side: str,
                  hold_bars: int = HOLD_BARS) -> Optional[dict[str, float]]:
    """Grade the regenerated candidate decided at the close of bars[i].

    Mirrors backfill_ta_late_entry_historical geometry: B1 = first bar whose
    open >= signal ts (here: bars[i+1], since the signal ts is the close of
    bars[i]); entry = B0.close = bars[i].c; exit = close of bar
    b1_idx - 2 + hold_bars; net of 5bps round-trip; MAE = worst side-aware
    close over [B0, exit]. Returns None when forward bars are missing."""
    b1_idx = i + 1
    exit_idx = b1_idx - 2 + hold_bars
    if b1_idx >= len(bars) or exit_idx >= len(bars) or exit_idx < 0:
        return None
    entry = float(bars[i].c)
    if entry <= 0:
        return None
    sign = 1.0 if side == "long" else -1.0
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0
    exit_px = float(bars[exit_idx].c)
    mae_pct = 0.0
    for j in range(i, exit_idx + 1):
        move_pct = sign * (float(bars[j].c) - entry) / entry * 100.0
        if move_pct < mae_pct:
            mae_pct = move_pct
    gross_pct = sign * (exit_px - entry) / entry * 100.0
    net_pct = gross_pct - fee_pct * 100.0
    return {"net_pct": round(net_pct, 4), "mae_pct": round(mae_pct, 4),
            "exit_px": exit_px}


def _chg24_from_1h(closes_1h: dict[int, float], t_close: int) -> Optional[float]:
    """close(t)/close(t-24h)-1 in %, from closed 1h bars keyed by open time.

    t_close is 4h-aligned, so the 1h bar opening at t_close-1h has exactly
    closed. Approximates the live universe snapshot's prevDayPx (audited
    non-reproducible)."""
    t_now = t_close - BAR_1H
    t_prev = t_now - 24 * BAR_1H
    c_now = closes_1h.get(t_now)
    c_prev = closes_1h.get(t_prev)
    if c_now is None or c_prev is None or c_prev <= 0:
        return None
    return (c_now - c_prev) / c_prev * 100.0


def _sma200_as_of(daily_ts: list[int], daily_closes: list[float],
                  t_close: int, period: int = SMA_PERIOD) -> Optional[float]:
    """200SMA of daily closes whose bar had closed at t_close."""
    # Daily bar opening at t closes at t+1d; keep those closed by t_close.
    k = bisect.bisect_right(daily_ts, t_close - BAR_1D)
    if k < period:
        return None
    series = sma(daily_closes[:k], period)
    last = series[-1] if series else float("nan")
    return float(last) if last == last else None


def compute_candidates(coin: str, bars_4h: list,
                       closes_1h: dict[int, float],
                       daily_ts: list[int], daily_closes: list[float], *,
                       side: str, params: Optional[dict[str, Any]] = None,
                       hold_bars: int = HOLD_BARS,
                       fetch_bars: int = FETCH_BARS_4H) -> list[dict[str, Any]]:
    """One candidate per closed 4h bar (decided at its close), measured by
    late_entry_check on the PIT slice and forward-graded. Bars are the full
    ascending series for the window (with warm-up context included)."""
    p = dict(BASE_PARAMS if params is None else params)
    out: list[dict[str, Any]] = []
    min_bars = int(p.get("min_bars_4h", MIN_BARS_4H))
    for i in range(len(bars_4h)):
        if i + 1 < min_bars:
            continue
        window = bars_4h[max(0, i + 1 - fetch_bars): i + 1]
        verdict = late_entry_check(window, None, side, p)
        if not verdict.get("data_ok"):
            continue
        grade = forward_grade(bars_4h, i, side, hold_bars=hold_bars)
        if grade is None:
            continue
        t_close = bars_4h[i].t + BAR_4H
        price = float(bars_4h[i].c)
        sma200 = _sma200_as_of(daily_ts, daily_closes, t_close)
        out.append({
            "coin": coin, "i": i, "t_close": t_close, "side": side,
            "close": price,
            "rsi4h": verdict.get("rsi4h"),
            "adx4h": verdict.get("adx4h"),
            "extension": verdict.get("extension"),
            "trend_dir": verdict.get("trend_direction", "flat"),
            "chg24": _chg24_from_1h(closes_1h, t_close),
            "sma200": sma200,
            "above_sma": (None if sma200 is None else price >= sma200),
            **grade,
        })
    return out


# ---------------------------------------------------------------------------
# Arithmetic verdict replays (locked to the source functions by tests)
# ---------------------------------------------------------------------------

def _p(p: dict[str, Any], key: str, default: Any) -> Any:
    v = p.get(key, default)
    return default if v is None else v


def _relaxed(m: dict[str, Any], side: str, p: dict[str, Any]) -> bool:
    if not bool(_p(p, "trend_relax_enabled", True)):
        return False
    adx4h = m.get("adx4h")
    if adx4h is None:
        return False
    floor = float(_p(p, "adx_trend_threshold", 35))
    aligned = ((m.get("trend_dir") == "bullish") if side == "long"
               else (m.get("trend_dir") == "bearish"))
    return bool(adx4h >= floor and aligned)


def replay_late_entry_block(m: dict[str, Any], side: str,
                            p: dict[str, Any]) -> bool:
    """late_entry_check's block with mtf_enabled=False (production): block
    iff the (possibly trend-relaxed) RSI or extension limit is hit."""
    rsi4h, ext = m.get("rsi4h"), m.get("extension")
    relaxed = _relaxed(m, side, p)
    if side == "long":
        rsi_limit = float(_p(p, "rsi_ob_relaxed", 82)) if relaxed \
            else float(_p(p, "rsi_ob", 75))
        ext_limit = float(_p(p, "ext_ob_relaxed", 3.5)) if relaxed \
            else float(_p(p, "ext_ob", 2.5))
        rsi_hit = rsi4h is not None and rsi4h > rsi_limit
        ext_hit = ext is not None and ext > ext_limit
    else:
        rsi_limit = float(_p(p, "rsi_os_relaxed", 18)) if relaxed \
            else float(_p(p, "rsi_os", 25))
        ext_limit = float(_p(p, "ext_os_relaxed", -3.5)) if relaxed \
            else float(_p(p, "ext_os", -2.5))
        rsi_hit = rsi4h is not None and rsi4h < rsi_limit
        ext_hit = ext is not None and ext < ext_limit
    return bool(rsi_hit or ext_hit)


def replay_relax_tier(m: dict[str, Any], side: str,
                      p: dict[str, Any]) -> dict[str, Optional[bool]]:
    """relax_tier_check's three probes, replayed from measurements."""
    out: dict[str, Optional[bool]] = {"rt_relax45": None,
                                      "rt_weak_rsi70": None,
                                      "rt_no_adx20": None}
    rsi4h, adx4h, ext = m.get("rsi4h"), m.get("adx4h"), m.get("extension")
    if adx4h is None or rsi4h is None:
        return out
    is_long = side == "long"
    relax_today = _relaxed(m, side, p)
    rt_relax_adx = float(_p(p, "rt_relax_adx", 45))
    if relax_today and adx4h < rt_relax_adx:
        if is_long:
            rsi_hit = rsi4h > float(_p(p, "rsi_ob", 75))
            ext_hit = ext is not None and ext > float(_p(p, "ext_ob", 2.5))
        else:
            rsi_hit = rsi4h < float(_p(p, "rsi_os", 25))
            ext_hit = ext is not None and ext < float(_p(p, "ext_os", -2.5))
        out["rt_relax45"] = bool(rsi_hit or ext_hit)
    else:
        out["rt_relax45"] = False

    weak_adx = float(_p(p, "rt_weak_adx", 35))
    if adx4h < weak_adx:
        floor = (float(_p(p, "rt_weak_rsi_long", 70)) if is_long
                 else float(_p(p, "rt_weak_rsi_short", 30)))
        out["rt_weak_rsi70"] = bool(rsi4h >= floor) if is_long \
            else bool(rsi4h <= floor)
    else:
        out["rt_weak_rsi70"] = False

    out["rt_no_adx20"] = bool(adx4h < float(_p(p, "rt_no_trend_adx", 20)))
    return out


def replay_trend_filter_block(m: dict[str, Any], p: dict[str, Any]) -> bool:
    """trend_filter_200ma (longs only): block when price is below the daily
    200SMA unless the qualified daily-mover bypass applies. SMA unavailable
    fails OPEN (production block_unknown=false)."""
    above = m.get("above_sma")
    if above is None:
        return False
    if above:
        return False
    if not bool(p.get("allow_daily_mover_long_bypass", True)):
        return True
    chg = m.get("chg24")
    if chg is None:
        return True
    lo = float(p.get("daily_mover_min_ext_pct", 10.0))
    hi = float(p.get("daily_mover_max_ext_pct", 30.0))
    return not (lo <= chg <= hi)


def replay_daily_ext_cap_block(m: dict[str, Any], cap: float) -> bool:
    """daily_extension_cap (longs only): block when the 24h change exceeds
    the cap; unknown change fails open."""
    chg = m.get("chg24")
    return bool(chg is not None and chg > cap)


# ---------------------------------------------------------------------------
# Walk-forward segmentation + stats
# ---------------------------------------------------------------------------

def wf_bounds(t0: int, t1: int,
              ratios: tuple[float, float, float] = WF_RATIOS) -> tuple[int, int]:
    """(train_end, val_end) epoch-ms boundaries for the 60/20/20 split."""
    span = t1 - t0
    return (int(t0 + span * ratios[0]),
            int(t0 + span * (ratios[0] + ratios[1])))


def segment_of(t_close: int, bounds: tuple[int, int]) -> str:
    if t_close < bounds[0]:
        return "train"
    if t_close < bounds[1]:
        return "val"
    return "test"


def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"n": 0}
    nets = [r["net_pct"] for r in rows]
    wins = sum(1 for x in nets if x > 0)
    return {
        "n": n,
        "wr": round(wins / n, 4),
        "ev": round(sum(nets) / n, 4),
        "total": round(sum(nets), 2),
    }


def gate_stats(cands: list[dict[str, Any]],
               block_fn: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Value of a gate over one segment: what it blocked (worse = better)
    vs what it passed. harmful = blocked candidates that would have WON."""
    blocked = [c for c in cands if block_fn(c)]
    passed = [c for c in cands if not block_fn(c)]
    out = {"all": _agg(cands), "blocked": _agg(blocked),
           "passed": _agg(passed)}
    if blocked:
        harm = sum(1 for c in blocked if c["net_pct"] > 0)
        out["harmful_rate"] = round(harm / len(blocked), 4)
        out["avoided_loss_per_block"] = round(
            -out["blocked"].get("ev", 0.0), 4)
    else:
        out["harmful_rate"] = None
        out["avoided_loss_per_block"] = None
    return out


def _by_segment(cands: list[dict[str, Any]],
                bounds: tuple[int, int]) -> dict[str, list[dict[str, Any]]]:
    segs: dict[str, list[dict[str, Any]]] = {"train": [], "val": [],
                                             "test": []}
    for c in cands:
        segs[segment_of(c["t_close"], bounds)].append(c)
    return segs


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

def sweep_ta_late_entry(cands: list[dict[str, Any]],
                        bounds: tuple[int, int]) -> list[dict[str, Any]]:
    """One row per (rsi_ob, ext_ob, adx_floor, relax_enabled) combo, with
    per-segment gate stats. Symmetric mirroring: rsi_os=100-rsi_ob,
    ext_os=-ext_ob (production convention)."""
    segs = _by_segment(cands, bounds)
    rows: list[dict[str, Any]] = []
    for rsi_ob in GRID_RSI_OB:
        for ext_ob in GRID_EXT_OB:
            for adx_floor in GRID_ADX_FLOOR:
                for relax_on in GRID_RELAX_ENABLED:
                    p = dict(BASE_PARAMS)
                    p.update({
                        "rsi_ob": rsi_ob, "rsi_os": 100.0 - rsi_ob,
                        "ext_ob": ext_ob, "ext_os": -ext_ob,
                        "adx_trend_threshold": adx_floor,
                        "trend_relax_enabled": relax_on,
                    })
                    row: dict[str, Any] = {
                        "params": {"rsi_ob": rsi_ob, "ext_ob": ext_ob,
                                   "adx_floor": adx_floor,
                                   "relax": relax_on},
                    }
                    for seg, seg_cands in segs.items():
                        row[seg] = gate_stats(
                            seg_cands,
                            lambda c, p=p: replay_late_entry_block(
                                c, c["side"], p))
                    rows.append(row)
    return rows


def sweep_relax_tier(cands: list[dict[str, Any]],
                     bounds: tuple[int, int]) -> dict[str, Any]:
    """The three relax_tier probes as named counterfactual gates."""
    segs = _by_segment(cands, bounds)
    out: dict[str, Any] = {}
    for probe in ("rt_relax45", "rt_weak_rsi70", "rt_no_adx20"):
        out[probe] = {
            seg: gate_stats(
                seg_cands,
                lambda c, probe=probe: bool(
                    replay_relax_tier(c, c["side"], BASE_PARAMS)[probe]))
            for seg, seg_cands in segs.items()
        }
    return out


def sweep_trend_filter(cands_long: list[dict[str, Any]],
                       bounds: tuple[int, int]) -> list[dict[str, Any]]:
    segs = _by_segment(cands_long, bounds)
    rows: list[dict[str, Any]] = []
    for mover in GRID_MOVER:
        p = dict(BASE_TREND)
        if mover is None:
            p["allow_daily_mover_long_bypass"] = False
            label = "bypass_off"
        else:
            p["daily_mover_min_ext_pct"], p["daily_mover_max_ext_pct"] = mover
            label = (f"[{mover[0]:g},{mover[1]:g}]" if mover[1] < 1e8
                     else "bypass_all")
        row: dict[str, Any] = {"mover_window": label}
        for seg, seg_cands in segs.items():
            row[seg] = gate_stats(
                seg_cands, lambda c, p=p: replay_trend_filter_block(c, p))
        rows.append(row)
    return rows


def sweep_daily_ext_cap(cands_long: list[dict[str, Any]],
                        bounds: tuple[int, int]) -> list[dict[str, Any]]:
    segs = _by_segment(cands_long, bounds)
    rows: list[dict[str, Any]] = []
    for cap in GRID_CAP:
        row: dict[str, Any] = {"cap": cap}
        for seg, seg_cands in segs.items():
            row[seg] = gate_stats(
                seg_cands, lambda c, cap=cap:
                replay_daily_ext_cap_block(c, cap))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Robust-plateau selection (anti-overfit): prefer the middle of a flat,
# consistently-negative blocked-EV region over a single sharp optimum.
# ---------------------------------------------------------------------------

def plateau_pick(curve: list[dict[str, Any]], *, value_key: str = "ev",
                 min_n: int = 30,
                 tol: float = 0.15) -> Optional[dict[str, Any]]:
    """curve: axis points sorted by axis value, each with
    {axis, ev, n, sign_consistent}. A plateau = the longest run of adjacent
    points whose ev agrees within tol AND whose sign is consistent across
    segments. Returns the plateau's middle point, else None."""
    best: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []
    for pt in curve:
        ok = (pt.get("n", 0) >= min_n and pt.get("sign_consistent") and
              pt.get(value_key) is not None)
        if ok and run and abs(pt[value_key] - run[-1][value_key]) <= tol:
            run.append(pt)
        elif ok:
            run = [pt]
        else:
            run = []
        if len(run) > len(best):
            best = run
    if not best:
        return None
    return best[len(best) // 2]


def axis_curve(rows: list[dict[str, Any]], axis: str,
               values: list[float]) -> list[dict[str, Any]]:
    """Single-axis curve (other axes fixed at baseline): blocked-set EV per
    segment, sign consistency, and the validate+test agreement the plateau
    picker needs."""
    base = BASE_PARAMS
    out: list[dict[str, Any]] = []
    for v in values:
        match = None
        for r in rows:
            prm = r["params"]
            on_axis = prm[axis] == v or (axis == "relax" and
                                         prm["relax"] == v)
            others_base = all(
                prm[k] == base[b] for k, b in
                (("rsi_ob", "rsi_ob"), ("ext_ob", "ext_ob"),
                 ("adx_floor", "adx_trend_threshold"),
                 ("relax", "trend_relax_enabled")) if k != axis)
            if on_axis and others_base:
                match = r
                break
        if match is None:
            continue
        evs = {seg: match[seg]["blocked"].get("ev")
               for seg in ("train", "val", "test")}
        have = [e for e in evs.values() if e is not None]
        out.append({
            "axis": v,
            "ev": evs.get("val"),
            "evs": evs,
            "n": sum(match[seg]["blocked"].get("n", 0)
                     for seg in ("train", "val", "test")),
            "sign_consistent": bool(have) and
                (all(e < 0 for e in have) or all(e > 0 for e in have)),
        })
    return out


# ---------------------------------------------------------------------------
# Overlap check: regenerated candidates vs the live shadow rows
# ---------------------------------------------------------------------------

def overlap_check(live_recs: list[dict[str, Any]],
                  cand_index: dict[tuple[str, int], dict[str, Any]],
                  bars_by_coin: dict[str, list]) -> dict[str, Any]:
    """For each live row, find the regenerated candidate whose deciding bar
    is the newest bar closed at the live timestamp (same bar the live gate
    scored), then compare measured indicators and the base-parameter block
    verdict."""
    diffs: dict[str, list[float]] = {"rsi4h": [], "adx4h": [],
                                     "extension": []}
    n_matched = 0
    n_block_cmp = 0
    n_block_agree = 0
    for rec in live_recs:
        coin = rec.get("coin")
        t0 = _signal_ms(rec)
        side = rec.get("side") if rec.get("side") in ("long", "short") \
            else "long"
        if not coin or not t0 or coin not in bars_by_coin:
            continue
        ts_list = bars_by_coin[coin]  # sorted 4h bar open-times
        # Newest bar CLOSED at t0: bar.t + BAR <= t0.
        i = bisect.bisect_right(ts_list, t0 - BAR_4H) - 1
        if i < 0:
            continue
        cand = cand_index.get((str(coin), i, side))
        if cand is None:
            continue
        n_matched += 1
        for k in ("rsi4h", "adx4h", "extension"):
            lv, rv = rec.get(k), cand.get(k)
            if isinstance(lv, (int, float)) and isinstance(rv, (int, float)):
                diffs[k].append(abs(float(lv) - float(rv)))
        lb = rec.get("blocked")
        if lb is not None:
            n_block_cmp += 1
            if bool(lb) == replay_late_entry_block(cand, side, BASE_PARAMS):
                n_block_agree += 1

    def _summ(d: list[float]) -> dict[str, Any]:
        if not d:
            return {"n": 0}
        s = sorted(d)
        return {"n": len(d), "max": round(s[-1], 6),
                "p95": round(s[int(0.95 * (len(s) - 1))], 6),
                "mae": round(sum(d) / len(d), 6)}

    return {
        "live_rows": len(live_recs),
        "matched": n_matched,
        "indicator_diff": {k: _summ(v) for k, v in diffs.items()},
        "block_compared": n_block_cmp,
        "block_agree_rate": (round(n_block_agree / n_block_cmp, 4)
                             if n_block_cmp else None),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _fetch_window(coin: str, days: int, as_of_ms: int,
                  sleep_s: float = 0.0) -> dict[str, Any]:
    """Pull the full 4h/1h/1d window (with warm-up context) for one coin."""
    end = as_of_ms
    start_4h = end - (days + CONTEXT_4H_DAYS) * BAR_1D
    start_1h = end - (days + 2) * BAR_1D
    start_1d = end - (days + SMA_WARMUP_DAYS) * BAR_1D

    def _load(interval: str, t_fetch: int) -> list:
        interval_ms = {"4h": BAR_4H, "1h": BAR_1H, "1d": BAR_1D}[interval]
        cached = hc._BAR_CACHE.get((coin, interval))
        if cached:
            win = sorted(
                (b for b in cached.values()
                 if t_fetch - interval_ms <= b.t <= as_of_ms),
                key=lambda b: b.t,
            )
            # Use cache only when it covers the window up to ~the last bar.
            if win and win[-1].t >= as_of_ms - interval_ms:
                return win
        return hc.fetch_candle_range(coin, interval, t_fetch, end)

    bars_4h = _load("4h", start_4h)
    if sleep_s:
        time.sleep(sleep_s)
    bars_1h = _load("1h", start_1h)
    if sleep_s:
        time.sleep(sleep_s)
    bars_1d = _load("1d", start_1d)
    if sleep_s:
        time.sleep(sleep_s)
    return {"bars_4h": bars_4h, "bars_1h": bars_1h, "bars_1d": bars_1d}


def run(*, days: int = 120, coins: Optional[list[str]] = None,
        universe_file: str = DEFAULT_UNIVERSE_FILE,
        as_of_ms: Optional[int] = None, hold_bars: int = HOLD_BARS,
        write: bool = False, out: str = DEFAULT_OUT,
        cache_file: Optional[str] = None, sleep_s: float = 0.0,
        skip_trend: bool = False,
        evict_cache: bool = False) -> dict[str, Any]:
    if cache_file:
        hc.set_cache_file(cache_file)
    as_of = as_of_ms if as_of_ms is not None else int(time.time() * 1000)
    t_start = as_of - days * BAR_1D

    if coins is None:
        coins = load_live_coins(universe_file)
    if not coins:
        raise SystemExit("empty universe: no coins to replay")

    all_cands: list[dict[str, Any]] = []
    all_cands_long72: list[dict[str, Any]] = []
    cand_index: dict[tuple[str, int], dict[str, Any]] = {}
    bars_by_coin: dict[str, list] = {}
    t0 = time.time()
    for ci, coin in enumerate(coins):
        w = _fetch_window(coin, days, as_of, sleep_s=sleep_s)
        bars_4h = w["bars_4h"]
        if len(bars_4h) < MIN_BARS_4H + 2:
            print(f"[{ci+1}/{len(coins)}] {coin}: only {len(bars_4h)} 4h "
                  f"bars — skipped")
            continue
        closes_1h = {b.t: float(b.c) for b in w["bars_1h"]}
        daily_ts = [b.t for b in w["bars_1d"]]
        daily_closes = [float(b.c) for b in w["bars_1d"]]
        bars_by_coin[coin] = [b.t for b in bars_4h]
        for side in ("long", "short"):
            cands = compute_candidates(
                coin, bars_4h, closes_1h, daily_ts, daily_closes,
                side=side, hold_bars=hold_bars)
            for c in cands:
                cand_index[(coin, c["i"], side)] = c
            all_cands.extend(cands)
        if not skip_trend:
            all_cands_long72.extend(compute_candidates(
                coin, bars_4h, closes_1h, daily_ts, daily_closes,
                side="long", hold_bars=HOLD_BARS_72H))
        if evict_cache:
            # Bounded-memory mode for long windows inside the 1GB container:
            # drop this coin's raw bars; only aggregates are retained.
            for _iv in ("4h", "1h", "1d"):
                hc._BAR_CACHE.pop((coin, _iv), None)
        print(f"[{ci+1}/{len(coins)}] {coin}: {len(bars_4h)} 4h bars "
              f"({time.time()-t0:.0f}s elapsed)")

    # Restrict to the requested analysis window (warm-up context excluded).
    all_cands = [c for c in all_cands if c["t_close"] >= t_start]
    all_cands_long72 = [c for c in all_cands_long72
                        if c["t_close"] >= t_start]
    if not all_cands:
        raise SystemExit("no candidates generated")
    t_min = min(c["t_close"] for c in all_cands)
    t_max = max(c["t_close"] for c in all_cands)
    bounds = wf_bounds(t_min, t_max)

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params_baseline": BASE_PARAMS,
        "days": days, "coins": coins, "hold_bars": hold_bars,
        "window": {"t_min": t_min, "t_max": t_max,
                   "train_end": bounds[0], "val_end": bounds[1]},
        "n_candidates": len(all_cands),
        "n_candidates_long72": len(all_cands_long72),
    }

    print("sweeping ta_late_entry grid ...")
    report["ta_late_entry_sweep"] = sweep_ta_late_entry(all_cands, bounds)
    report["relax_tier"] = sweep_relax_tier(all_cands, bounds)
    report["axis_curves"] = {
        "rsi_ob": axis_curve(report["ta_late_entry_sweep"], "rsi_ob",
                             GRID_RSI_OB),
        "ext_ob": axis_curve(report["ta_late_entry_sweep"], "ext_ob",
                             GRID_EXT_OB),
        "adx_floor": axis_curve(report["ta_late_entry_sweep"], "adx_floor",
                                GRID_ADX_FLOOR),
    }
    report["plateau_picks"] = {
        k: plateau_pick(curve) for k, curve in report["axis_curves"].items()
    }

    if not skip_trend and all_cands_long72:
        print("sweeping trend_filter / daily_extension_cap ...")
        report["trend_filter_sweep"] = sweep_trend_filter(
            all_cands_long72, bounds)
        report["daily_ext_cap_sweep"] = sweep_daily_ext_cap(
            all_cands_long72, bounds)

    print("overlap check vs live shadow rows ...")
    live_recs = load_live_records(universe_file)
    report["overlap"] = overlap_check(live_recs, cand_index, bars_by_coin)

    if write:
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)
        os.replace(tmp, out)
        if cache_file:
            # Flush only into an explicitly pinned cache file; never rewrite
            # the shared live cache (it would shrink under cache eviction).
            hc.flush_disk_cache()
    report["_written"] = write
    return report


def _fmt_gate(s: dict[str, Any]) -> str:
    b, p = s.get("blocked", {}), s.get("passed", {})
    return (f"n={b.get('n', 0):>5} ev={b.get('ev')} wr={b.get('wr')} | "
            f"pass_ev={p.get('ev')} harm={s.get('harmful_rate')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--coins", default=None,
                    help="comma list; default = coins seen in the live "
                    "ta_late_entry shadow log")
    ap.add_argument("--universe-file", default=DEFAULT_UNIVERSE_FILE)
    ap.add_argument("--as-of", type=int, default=None)
    ap.add_argument("--hold-bars", type=int, default=HOLD_BARS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--cache-file", default=None,
                    help="pin an isolated disk-cache file (also enables the "
                    "post-write cache flush; default: no flush, shared live "
                    "cache file is never written)")
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--skip-trend", action="store_true")
    ap.add_argument("--evict-cache", action="store_true",
                    help="drop each coin's raw bars after use (bounded memory "
                    "for long windows in the 1GB container)")
    args = ap.parse_args()

    coins = ([c.strip() for c in args.coins.split(",") if c.strip()]
             if args.coins else None)
    rep = run(days=args.days, coins=coins, universe_file=args.universe_file,
              as_of_ms=args.as_of, hold_bars=args.hold_bars,
              write=args.write, out=args.out, cache_file=args.cache_file,
              sleep_s=args.sleep, skip_trend=args.skip_trend,
              evict_cache=args.evict_cache)

    w = rep["window"]
    print(f"\n=== regen replay: {rep['days']}d over {len(rep['coins'])} "
          f"coins ===")
    print(f"candidates: {rep['n_candidates']} (long72h set: "
          f"{rep['n_candidates_long72']})")
    print(f"window: {datetime.fromtimestamp(w['t_min']/1000, timezone.utc):%Y-%m-%d} → "
          f"{datetime.fromtimestamp(w['t_max']/1000, timezone.utc):%Y-%m-%d}  "
          f"(train< {datetime.fromtimestamp(w['train_end']/1000, timezone.utc):%m-%d}, "
          f"val< {datetime.fromtimestamp(w['val_end']/1000, timezone.utc):%m-%d})")

    ov = rep["overlap"]
    print(f"\n-- overlap vs live: matched {ov['matched']}/{ov['live_rows']} "
          f"rows, block agree {ov['block_agree_rate']}")
    for k, d in ov["indicator_diff"].items():
        if d.get("n"):
            print(f"   Δ{k}: max={d['max']} p95={d['p95']} mae={d['mae']}")

    print("\n-- relax_tier probes (baseline params, test segment):")
    for probe, segs in rep["relax_tier"].items():
        print(f"   {probe:<18} test: {_fmt_gate(segs['test'])}")

    print("\n-- plateau picks (robust mid-platform, blocked-EV on val):")
    for axis, pick in rep["plateau_picks"].items():
        print(f"   {axis:<10} -> {pick}")

    if "trend_filter_sweep" in rep:
        print("\n-- trend_filter mover windows (test segment):")
        for r in rep["trend_filter_sweep"]:
            print(f"   {r['mover_window']:<12} {_fmt_gate(r['test'])}")
        print("\n-- daily_extension_cap (test segment):")
        for r in rep["daily_ext_cap_sweep"]:
            print(f"   cap={r['cap']:<6g} {_fmt_gate(r['test'])}")

    print(f"\nfull report {'written to ' + args.out if args.write else '(dry-run; --write to save)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
