#!/usr/bin/env python3
"""Reconcile risk-tuning SHADOW verdicts with would-be outcomes.

Reads ~/.hermes-trading/risk_tuning_shadow.jsonl (path overridable via
HERMES_RISK_TUNING_SHADOW_FILE) and grades each counter-factual rule:

  * breakout_score_floor / per_coin_cooldown (would="block"):
      The signal was a live ADMIT. Fetch 1h candles after the signal and run
      the same simplified two-phase DSL simulation used by the pullback
      reconcile, so a blocked-by-the-proposal admit is graded on what it would
      have made. Aggregate: of the trades the proposal WOULD have blocked, how
      many were actually winners/losers and the counter-factual expectancy —
      i.e. the false-block cost (a good proposal blocks mostly losers).

  * leverage_tier (would="deleverage"):
      The signal was live at the high leverage. Prefer the REAL close when the
      same coin has a memory close shortly after the signal; otherwise fall
      back to candle simulation. Compare PnL at live_leverage vs
      proposed_leverage to size the risk reduction (and the upside given up).

  * stop_tuning (would="survive_wider_cap" / "breakeven_would_arm"):
      Recorded at a real max_loss exit. Simulate the same bars with the
      candidate wider cap and the live cap: report whether the wider stop
      survives to a profit / times out at a profit, or just stops out later at
      a bigger loss — the central "noise stop vs real stop" question.

  * exit_outcome (one row per REAL close, L5-A3):
      Self-contained — carries the realized spot/ROE, observed MFE, live vs
      candidate protect_pct, and the pre-computed record-only counterfactual
      tags (protect_arms_earlier / protect_would_unlock_phase2) keyed by entry
      regime. No candle fetch or grading is needed; this report aggregates the
      real outcomes directly. NOTE: protect_would_unlock_phase2 only says the
      trade's real peak would have crossed a LOWER arming threshold; it is not
      a price-path replay and does NOT by itself prove a better exit. It sizes
      the affected population; the realized-ROE split across the tagged groups
      is the directional evidence (does the would-unlock cluster actually have
      positive MFE / recoverable ROE?).

  * ignite_grace_cf (D2, one snapshot per STILL-UNIGNITED non_trend position at
      the candidate earlier 45-min grace, paired to its real exit):
      Self-contained join on (coin, side, entry_ts) — no candles. cf net ROE at
      the snapshot vs realized net ROE at the live exit gives the paired delta
      of cutting the drifter before the 60-min stale-flat. Pre-registered gate:
      n>=30 joined non_trend pairs, 15-min entry-block bootstrap (B=10000, fixed
      seed) 95% CI lower bound strictly > 0, one-sided Wilcoxon p<0.05, shorter
      cf median hold, majority improved, and overall net delta positive despite
      the floor_breach give-up cluster. No verdict is printed below n=30.

Pure paper reconciliation: never places orders. Default is a dry-run report;
pass --write to stamp outcome fields back into the JSONL.

Usage:
    python3 scripts/reconcile_risk_tuning_shadow.py
    python3 scripts/reconcile_risk_tuning_shadow.py --window-hours 24 --write
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

os.environ["HERMES_BACKTEST"] = "1"
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config_store import cfg_get, read_agent_config
from hermes_trader.client.hl_client import _http_post
from hermes_trader.models.types import Candle

SHADOW_FILE = os.environ.get(
    "HERMES_RISK_TUNING_SHADOW_FILE",
    # D2-fix (2026-10-23): default to /data/ (the container volume, single
    # source of truth).  The old default (~/.hermes-trading/) pointed to the
    # host bind-mount which the container cannot see, causing a silent split
    # where the container wrote /data/ and the reconciler read ~/.hermes-trading/.
    # When running outside the container, set HERMES_RISK_TUNING_SHADOW_FILE
    # explicitly or mount /data/ to the same path.
    "/data/risk_tuning_shadow.jsonl",
)
if not os.path.isfile(SHADOW_FILE):
    # Fallback for host-side runs where /data/ is not mounted.
    _host_fallback = os.path.expanduser("~/.hermes-trading/risk_tuning_shadow.jsonl")
    if os.path.isfile(_host_fallback):
        print(f"[reconcile] WARNING: {SHADOW_FILE} not found, "
              f"falling back to host file {_host_fallback}")
        SHADOW_FILE = _host_fallback
    else:
        print(f"[reconcile] WARNING: shadow file not found at {SHADOW_FILE} "
              f"or {_host_fallback}")
MEMORY_FILE = os.environ.get(
    "HERMES_MEMORY_FILE",
    # Mirror agents/memory.py: env override (the live container sets
    # HERMES_AGENT_MEMORY_FILE=/data/.agent-memory.json), else repo-root file.
    os.environ.get(
        "HERMES_AGENT_MEMORY_FILE",
        str(_REPO / ".agent-memory.json")),
)
ROUND_TRIP_FEE_BPS = 5.0
SIM_BARS = 180          # max 1h bars walked per signal (7.5 days)
JOIN_WINDOW_MS = 6 * 3600_000   # match a real close within 6h of the signal

# D2 (ignite_grace_cf) decision constants — fixed BEFORE seeing outcomes so the
# n>=30 / CI gate cannot be tuned on the accumulating sample.
D2_MIN_PAIRS = 30            # first promotion read only after >=30 joined pairs
D2_BOOT_BOOTSTRAPS = 10000
D2_BOOT_SEED = 20261022
D2_BLOCK_SECONDS = 15 * 60   # resample whole 15-min entry-time blocks
D2_ENTRY_TS_TOL_MS = 2_000   # exact entry_ts join tolerance (same open)
D2_FALLBACK_WINDOW_MS = 12 * 3600_000  # nearest-exit fallback for legacy rows
D2_MATURING_MS = 8 * 3600_000          # cf younger than 8h may still be open


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


BAR_MS = 3600_000  # 1h


def _fetch_window(coin: str, t0_ms: int) -> List[Candle]:
    """Explicit time-anchored candle window around the signal.

    Audit 2026-09-15: replaces ``fetch_hl_candles(coin, "1h", 300)``. The
    "latest N bars" call silently mis-anchored any signal older than the
    300-bar window: ``_find_entry_bar`` only checks ``t >= signal``, so the
    window's FIRST bar matched and the whole DSL walk ran on the wrong tape
    (29/100 live outcomes disagreed with the PIT-anchored replay). We request
    exactly [grid0-1h, grid0+(SIM_BARS+4)h] so the signal bar is always at
    the head of the returned tape.
    """
    grid0 = t0_ms - (t0_ms % BAR_MS)
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": grid0 - BAR_MS,
        "endTime": grid0 + (SIM_BARS + 4) * BAR_MS,
    }}
    raw = _http_post("/info", payload)
    if not isinstance(raw, list) or not raw:
        return []
    rows = sorted(raw, key=lambda c: int(c["t"]))
    return [Candle(t=int(c["t"]), o=float(c["o"]), h=float(c["h"]),
                   l=float(c["l"]), c=float(c["c"]), v=float(c["v"]))
            for c in rows]


def _find_entry_bar(candles, after_ts: datetime) -> int:
    for i, c in enumerate(candles):
        ct = getattr(c, "t", None)
        if ct is None:
            continue
        if datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc) >= after_ts:
            return i
    return -1


def _simulate(entry_px: float, entry_idx: int, candles,
              max_loss_pct: float, protect_pct: float,
              retrace_pct: float,
              max_bars: int = SIM_BARS) -> Tuple[float, str, int]:
    """Long-only two-phase walk; mirror of reconcile_pullback_shadow.

    Returns (exit_px, reason, exit_idx). Shorts are marked unsupported by the
    caller (the audited 2026-09-10 set is long-only).
    """
    peak = entry_px
    end = min(entry_idx + 1 + max_bars, len(candles))
    for j in range(entry_idx + 1, end):
        bar = candles[j]
        stop_px = entry_px * (1 - max_loss_pct / 100)
        if bar.l <= stop_px:
            return min(stop_px, bar.o), f"max_loss {max_loss_pct:g}%", j
        if peak > entry_px:
            profit_pct = (peak - entry_px) / entry_px * 100
            if profit_pct >= protect_pct:
                floor = entry_px + (peak - entry_px) * (1 - retrace_pct)
                if bar.l <= floor:
                    return min(floor, bar.o), "trailing_stop", j
        if bar.h > peak:
            peak = bar.h
    last = candles[end - 1]
    return last.c, "window_end", end - 1


def _load_real_closes() -> Dict[str, list]:
    """coin -> list of {closed_at, spot_pct, realized_pnl_usd, leverage}."""
    try:
        mem = json.load(open(MEMORY_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, list] = {}
    for c in mem.get("closes", []) or []:
        coin = str(c.get("coin") or "")
        ts = c.get("closed_at")
        if not coin or not isinstance(ts, (int, float)):
            continue
        out.setdefault(coin, []).append(c)
    return out


def _join_real_close(coin: str, signal_ms: float,
                     closes_by_coin: Dict[str, list]) -> Optional[dict]:
    best = None
    for c in closes_by_coin.get(coin, []):
        dt = abs(float(c["closed_at"]) - signal_ms)
        if dt <= JOIN_WINDOW_MS and (best is None or dt < best[0]):
            best = (dt, c)
    return best[1] if best else None


def _pnl_pct_at_leverage(spot_pct: float, leverage: float) -> float:
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0 * 100.0  # round-trip fee in %
    return spot_pct * leverage - fee_pct


def grade_record(r: dict, dsl_cfg: dict, closes_by_coin: Dict[str, list]) -> bool:
    """Fill outcome fields on one record. Returns False if not gradeable yet."""
    rule = r.get("rule")
    detail = r.get("detail") or {}
    after_ts = _parse_iso(r.get("timestamp", ""))
    if after_ts is None:
        r["outcome"] = "bad_timestamp"
        return True
    coin = r.get("coin") or ""
    signal_ms = after_ts.timestamp() * 1000.0

    if rule == "stop_tuning":
        # No candles needed for the headline counter-factual flags, but a
        # candle walk with the WIDER cap tells us if it actually turns green.
        r.setdefault("graded_at", datetime.now(timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"))
        entry_px = float(detail.get("entry_px") or 0)
        cap = detail.get("candidate_max_loss_pct")
        if entry_px > 0 and cap:
            try:
                candles = _fetch_window(coin, int(signal_ms))
                idx = _find_entry_bar(candles, after_ts)
                # Anchoring guard folded into the condition: a first match
                # beyond the signal bar means the tape does not cover the
                # signal (head gap) — skip the sim fields, never walk the
                # wrong tape. The outcome below is still written.
                grid0 = int(signal_ms) - (int(signal_ms) % BAR_MS)
                anchored = 0 <= idx and candles[idx].t <= grid0 + BAR_MS
                if anchored and idx < len(candles) - 2:
                    live_cap = float(detail.get("live_spot_cap_pct") or
                                     cfg_get("dsl_exit.max_loss_pct",
                                             config=dsl_cfg))
                    protect = float(cfg_get("dsl_exit.protect_pct",
                                            config=dsl_cfg))
                    retrace = float(cfg_get("dsl_exit.retrace_threshold",
                                            config=dsl_cfg))
                    wx, wr, _ = _simulate(entry_px, idx, candles, float(cap),
                                          protect, retrace)
                    lx, lr, _ = _simulate(entry_px, idx, candles, live_cap,
                                          protect, retrace)
                    fee = ROUND_TRIP_FEE_BPS / 10000.0
                    r["wider_cap_sim"] = {
                        "exit_px": round(wx, 6), "reason": wr,
                        "pnl_pct": round((wx - entry_px) / entry_px * 100
                                         - fee * 100, 3),
                    }
                    r["live_cap_sim"] = {
                        "exit_px": round(lx, 6), "reason": lr,
                        "pnl_pct": round((lx - entry_px) / entry_px * 100
                                         - fee * 100, 3),
                    }
            except Exception as e:  # network/parse: leave candle fields empty
                r["sim_error"] = str(e)
        r["outcome"] = (
            "wider_cap_would_green"
            if (r.get("wider_cap_sim") or {}).get("pnl_pct", 0) > 0
            else "wider_cap_no_gain")
        return True

    if str(r.get("side") or "").lower() not in ("", "long"):
        r["outcome"] = "short_unsupported"
        return True

    # leverage_tier: prefer the real close (these were live trades).
    if rule == "leverage_tier":
        real = _join_real_close(coin, signal_ms, closes_by_coin)
        if real and isinstance(real.get("spot_pct"), (int, float)):
            spot = float(real["spot_pct"])
            live_lev = float(detail.get("live_leverage") or
                             real.get("leverage") or 10)
            prop_lev = float(detail.get("proposed_leverage") or 5)
            r["real_close"] = {
                "closed_at": real.get("closed_at"),
                "spot_pct": round(spot, 4),
                "realized_pnl_usd": real.get("realized_pnl_usd"),
            }
            r["live_pnl_pct"] = round(_pnl_pct_at_leverage(spot, live_lev), 3)
            r["proposed_pnl_pct"] = round(
                _pnl_pct_at_leverage(spot, prop_lev), 3)
            r["outcome"] = ("deleverage_avoids_loss"
                            if r["live_pnl_pct"] < 0 else "deleverage_gives_up_gain")
            return True
        # fall through to candle simulation if no real close join
        entry_px = float(detail.get("entry_px") or 0)
        if entry_px <= 0:
            return False
    elif rule in ("breakout_score_floor", "per_coin_cooldown"):
        entry_px = float(detail.get("entry_px") or 0)
        if entry_px <= 0:
            r["outcome"] = "no_entry_px"
            return True
    else:
        return False

    # Candle simulation for block proposals and unmatched leverage signals.
    try:
        candles = _fetch_window(coin, int(signal_ms))
    except Exception as e:
        r["sim_error"] = f"fetch: {e}"
        return False
    idx = _find_entry_bar(candles, after_ts)
    if idx < 0 or idx >= len(candles) - 2:
        return False  # not enough future bars yet; leave pending
    # Anchoring guard: a first match beyond the signal bar means the tape
    # does not cover the signal (head gap) — leave pending, never walk the
    # wrong tape.
    grid0 = int(signal_ms) - (int(signal_ms) % BAR_MS)
    if candles[idx].t > grid0 + BAR_MS:
        return False

    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=dsl_cfg))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl_cfg))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl_cfg))
    exit_px, reason, _ = _simulate(entry_px, idx, candles, max_loss,
                                   protect, retrace)
    fee = ROUND_TRIP_FEE_BPS / 10000.0
    spot_pct = (exit_px - entry_px) / entry_px
    pnl_pct = (spot_pct - fee) * 100
    r["exit_px"] = round(exit_px, 6)
    r["exit_reason"] = reason
    if rule == "leverage_tier":
        live_lev = float(detail.get("live_leverage") or 10)
        prop_lev = float(detail.get("proposed_leverage") or 5)
        r["live_pnl_pct"] = round(pnl_pct * live_lev, 3)
        r["proposed_pnl_pct"] = round(pnl_pct * prop_lev, 3)
        r["outcome"] = ("deleverage_avoids_loss" if pnl_pct < 0
                        else "deleverage_gives_up_gain")
    else:
        r["pnl_pct"] = round(pnl_pct, 4)
        # A block proposal is "good" when the admit it would have blocked lost.
        r["outcome"] = "blocked_a_loser" if pnl_pct <= 0 else "blocked_a_winner"
    return True


def _regime_bucket(entry_regime: str) -> str:
    """Mirror dsl_exit._candidate_protect_for_regime bucketing.

    trend-family prefix -> trend; everything else (neutral/chop/"") ->
    non_trend. "non_trend" contains the substring "trend", so do NOT use a
    naive substring test.
    """
    r = str(entry_regime or "").lower()
    return "trend" if r.startswith(("uptrend", "downtrend", "trend")) \
        else "non_trend"


def _avg(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _summarize_exit_outcomes(records: List[dict]) -> None:
    """Aggregate the per-real-close exit_outcome rows (record-only protect A/B).

    No grading / candles: each row already carries realized ROE, observed MFE
    and the counterfactual tags. We bucket by trend vs non_trend (the map's
    keys) and, within each, split by whether the lower candidate protect would
    have newly armed / unlocked phase-2. Printed numbers are REALIZED live ROE
    and observed MFE only — a precondition check, not a counterfactual PnL.
    """
    rows = [r for r in records if r.get("rule") == "exit_outcome"
            and isinstance(r.get("detail"), dict)]
    if not rows:
        print("\n[exit_outcome]  (no real-close rows recorded yet)")
        return

    print(f"\n[exit_outcome]  real closes recorded = {len(rows)}")
    by_bucket: Dict[str, List[dict]] = {}
    for r in rows:
        d = r["detail"]
        by_bucket.setdefault(
            _regime_bucket(d.get("entry_regime", "")), []).append(r)

    def _stat(sub: List[dict]) -> str:
        roe = [float(s["detail"].get("realized_roe_pct") or 0) for s in sub]
        spot = [float(s["detail"].get("realized_spot_pct") or 0) for s in sub]
        mfe = [float(s["detail"].get("mfe_spot_pct") or 0) for s in sub]
        wins = sum(1 for p in spot if p > 0)
        return (f"n={len(sub):3}  realized ROE mean={_avg(roe):+6.2f}%  "
                f"spot mean={_avg(spot):+6.2f}%  win={wins/len(sub)*100:4.1f}%  "
                f"MFE mean={_avg(mfe):5.2f}%")

    for bucket in ("trend", "non_trend"):
        sub = by_bucket.get(bucket)
        if not sub:
            print(f"\n  [{bucket}] no rows")
            continue
        # Live protect in effect for this bucket (take the first row's value;
        # it is regime-constant within a deployment).
        live_p = sub[0]["detail"].get("live_protect_pct")
        cand_p = sub[0]["detail"].get("candidate_protect_pct")
        print(f"\n  [{bucket}] live protect={live_p}  "
              f"candidate protect={cand_p}  "
              f"(inert = candidate None means no rows are tagged)")
        print(f"    ALL            {_stat(sub)}")
        if cand_p is None:
            continue
        earlier = [s for s in sub
                   if s["detail"].get("protect_arms_earlier")]
        unlock = [s for s in sub
                  if s["detail"].get("protect_would_unlock_phase2")]
        already = [s for s in sub
                   if s["detail"].get("protect_arms_earlier")
                   and not s["detail"].get("protect_would_unlock_phase2")]
        never = [s for s in sub
                 if not s["detail"].get("protect_arms_earlier")]
        print(f"    arms earlier   {_stat(earlier) if earlier else '(none)'}")
        print(f"      -> unlock*   {_stat(unlock) if unlock else '(none)'}")
        print(f"      -> already   {_stat(already) if already else '(none)'}")
        print(f"    never arms     {_stat(never) if never else '(none)'}")
        if unlock:
            mfe_u = [float(s['detail'].get('mfe_spot_pct') or 0) for s in unlock]
            roe_u = [float(s['detail'].get('realized_roe_pct') or 0)
                     for s in unlock]
            print(f"    *unlock = real peak crossed candidate {cand_p}% but "
                  f"never reached live {live_p}%: the population a lower protect")
            print(f"     would newly convert into phase-2. Their MFE mean "
                  f"{_avg(mfe_u):.2f}% vs realized ROE mean {_avg(roe_u):+.2f}% "
                  f"sizes the give-back that an earlier floor MIGHT retain.")

    # Exit-reason composition, tagged vs not (the historical replay predicted a
    # stale_flat_timeout -> floor_breach migration under lower protect).
    print("\n  exit_reason composition (all real closes):")
    reason_tot: Dict[str, int] = {}
    reason_unlock: Dict[str, int] = {}
    for r in rows:
        d = r["detail"]
        token = str(r.get("exit_reason") or "?")
        reason_tot[token] = reason_tot.get(token, 0) + 1
        if d.get("protect_would_unlock_phase2"):
            reason_unlock[token] = reason_unlock.get(token, 0) + 1
    for token, n in sorted(reason_tot.items(), key=lambda kv: -kv[1]):
        u = reason_unlock.get(token, 0)
        print(f"    {token:22} {n:4}   of which would-unlock phase2: {u}")


def _net_roe_pct(gross_roe_pct: float, leverage: float) -> float:
    """Net a gross ROE % for one round trip at ROUND_TRIP_FEE_BPS taker fees.

    Fee is charged on notional per side (5 bps round trip = 0.05% of notional),
    i.e. 0.05 * leverage ROE points. Applied identically to both arms of a
    pair, so the paired delta is fee-invariant; netting only makes each arm's
    headline ROE comparable to the live book.
    """
    return float(gross_roe_pct) - (ROUND_TRIP_FEE_BPS / 10000.0 * 100.0) \
        * max(1.0, float(leverage or 1))


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _block_bootstrap_mean_ci(pairs: List[dict], boot: int,
                             seed: int) -> Tuple[float, float]:
    """95% CI for the mean paired net delta via 15-min entry-block resampling.

    Units that share an entry-time block (bursty same-tape entries) are
    resampled together, preventing within-block correlation from shrinking
    the interval. Deterministic seed -> reproducible verdict.
    """
    blocks: Dict[int, List[float]] = {}
    for p in pairs:
        b = int(float(p["entry_ts"]) // D2_BLOCK_SECONDS)
        blocks.setdefault(b, []).append(float(p["delta_net_roe"]))
    keys = list(blocks.keys())
    rng = random.Random(seed)
    means: List[float] = []
    for _ in range(boot):
        acc = 0.0
        cnt = 0
        for _ in range(len(keys)):
            vals = blocks[rng.choice(keys)]
            acc += sum(vals)
            cnt += len(vals)
        means.append(acc / cnt if cnt else 0.0)
    means.sort()
    lo = means[int(0.025 * boot)]
    hi = means[min(boot - 1, int(0.975 * boot))]
    return lo, hi


def _wilcoxon_one_sided(deltas: List[float]) -> Optional[float]:
    """One-sided Wilcoxon signed-rank p (H1: median delta > 0).

    Self-contained normal approximation (drops zero deltas, tie-corrected
    variance incl. zero ranks). Returns None when there is no information
    (all zeros or fewer than one non-zero pair).
    """
    d = [x for x in deltas if x != 0.0]
    n = len(d)
    if n == 0:
        return None
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        a = abs(d[order[i]])
        while j + 1 < n and abs(d[order[j + 1]]) == a:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    tie_groups: Dict[float, int] = {}
    for r in ranks:
        tie_groups[r] = tie_groups.get(r, 0) + 1
    tie_adj = sum(t ** 3 - t for t in tie_groups.values() if t > 1)
    var = (n * (n + 1) * (2 * n + 1) - tie_adj / 2.0) / 24.0
    if var <= 0:
        return None
    mean = n * (n + 1) / 4.0
    z = (w_plus - mean - 0.5) / math.sqrt(var)  # continuity correction
    # H1: median delta > 0 -> upper-tail p = 1 - Phi(z).
    return 0.5 * (1.0 - math.erf(z / math.sqrt(2.0)))


def _summarize_ignite_grace_cf(records: List[dict]) -> None:
    """D2: pair each 45-min unignited snapshot with its REAL exit and size the
    counterfactual of an earlier non_trend ignite cut.

    Both arms are the SAME position (join on coin+side+entry_ts):
      cf   = mark at the candidate grace (detail.cf_unrealized_roe_pct)
      real = realized ROE at the live exit (exit_outcome row)
    delta_net = net(cf) - net(real). Positive means cutting the drifter at the
    candidate grace beats holding to the real exit. No candles / simulation:
    the snapshot freezes the unrealized PnL at the cutoff, and the real close
    is already recorded. Guard rails (n>=30, block-bootstrap CI lower bound
    strictly > 0, one-sided Wilcoxon, shorter hold, floor_breach give-up) are
    pre-registered; with fewer than 30 joined pairs NO verdict is rendered.
    """
    cf_rows = [r for r in records if r.get("rule") == "ignite_grace_cf"
               and isinstance(r.get("detail"), dict)]
    ex_rows = [r for r in records if r.get("rule") == "exit_outcome"
               and isinstance(r.get("detail"), dict)]
    print("\n[ignite_grace_cf]  D2 record-only earlier non_trend ignite cut")
    if not cf_rows:
        print("  (no ignite_grace_cf snapshots recorded yet; arm is DEFAULT OFF)")
        return

    now_ms = datetime.now(timezone.utc).timestamp() * 1000.0
    ex_index: Dict[Tuple[str, str], List[dict]] = {}
    for r in ex_rows:
        ex_index.setdefault((r.get("coin"), str(r.get("side") or "").lower()),
                            []).append(r)

    pairs: List[dict] = []
    used_exit_ids = set()
    maturing = gap = 0
    for r in cf_rows:
        d = r["detail"]
        coin = r.get("coin")
        side = str(r.get("side") or "").lower()
        entry_ts = d.get("entry_ts")
        cf_ts = (_parse_iso(r.get("timestamp", "")) or
                 datetime.fromtimestamp(1, tz=timezone.utc))
        cf_ms = cf_ts.timestamp() * 1000.0
        match = None
        how = ""
        cands = ex_index.get((coin, side), [])
        if isinstance(entry_ts, (int, float)):
            best = None
            for e in cands:
                ets = (e.get("detail") or {}).get("entry_ts")
                if not isinstance(ets, (int, float)):
                    continue
                dt = abs(ets - float(entry_ts)) * 1000.0
                if dt <= D2_ENTRY_TS_TOL_MS and (best is None or dt < best[0]):
                    best = (dt, e)
            if best:
                match, how = best[1], "entry_ts"
        if match is None:
            # Legacy exit rows (pre entry_ts) only: nearest same coin/side
            # close strictly after the snapshot within the fallback window.
            best = None
            for e in cands:
                ex_ts = _parse_iso(e.get("timestamp", ""))
                if ex_ts is None:
                    continue
                ex_ms = ex_ts.timestamp() * 1000.0
                dt = ex_ms - cf_ms
                if 0 <= dt <= D2_FALLBACK_WINDOW_MS and (
                        best is None or dt < best[0]):
                    best = (dt, e)
            if best:
                match, how = best[1], "nearest"
        if match is None:
            if now_ms - cf_ms < D2_MATURING_MS:
                maturing += 1
            else:
                gap += 1
            continue
        if id(match) in used_exit_ids:
            gap += 1
            continue
        used_exit_ids.add(id(match))

        ed = match.get("detail") or {}
        lev = float(d.get("leverage") or ed.get("leverage") or 1)
        cf_gross = float(d.get("cf_unrealized_roe_pct") or 0.0)
        real_gross = float(ed.get("realized_roe_pct") or 0.0)
        cf_net = _net_roe_pct(cf_gross, lev)
        real_net = _net_roe_pct(real_gross, lev)
        ex_ts = _parse_iso(match.get("timestamp", ""))
        real_hold_min = (
            (ex_ts.timestamp() - float(entry_ts)) / 60.0
            if isinstance(entry_ts, (int, float)) and ex_ts is not None
            else float("nan"))
        pairs.append({
            "entry_ts": float(entry_ts) if isinstance(
                entry_ts, (int, float)) else cf_ts.timestamp(),
            "coin": coin,
            "regime_bucket": d.get("regime_bucket")
            or _regime_bucket(d.get("entry_regime", "")),
            "candidate_min": float(d.get("candidate_grace_minutes") or 0.0),
            "cf_close_min": float(d.get("cf_close_min") or 0.0),
            "real_hold_min": real_hold_min,
            "cf_net_roe": cf_net,
            "real_net_roe": real_net,
            "delta_net_roe": cf_net - real_net,
            "exit_reason": str(match.get("exit_reason") or "?"),
            "join": how,
        })

    print(f"  snapshots={len(cf_rows)}  joined_pairs={len(pairs)}  "
          f"maturing(open,<8h)={maturing}  unmatched_gap={gap}")
    if not pairs:
        print("  (no paired real exits yet — every arm needs its close)")
        return

    non_trend = [p for p in pairs if p["regime_bucket"] == "non_trend"]
    trend = [p for p in pairs if p["regime_bucket"] != "non_trend"]
    if trend:
        print(f"  NOTE: {len(trend)} pairs bucketed trend (candidate targets "
              f"non_trend only; excluded from the verdict)")
    if not non_trend:
        print("\n  [non_trend] no paired non_trend exits yet "
              "(candidate targets non_trend only); no verdict.")
        return
    n = len(non_trend)
    deltas = [p["delta_net_roe"] for p in non_trend]
    cf_hold = [p["cf_close_min"] for p in non_trend]
    real_hold = [p["real_hold_min"] for p in non_trend
                 if not math.isnan(p["real_hold_min"])]
    improved = sum(1 for x in deltas if x > 0)
    worse = sum(1 for x in deltas if x < 0)
    floor_give = [p for p in non_trend
                  if p["exit_reason"] == "floor_breach"
                  and p["real_net_roe"] > p["cf_net_roe"]]
    floor_give_sum = sum(p["real_net_roe"] - p["cf_net_roe"]
                         for p in floor_give)

    print(f"\n  [non_trend] n={n}  (promote read requires n>={D2_MIN_PAIRS})")
    print(f"    paired net dROE  mean={_avg(deltas):+6.2f} pp  "
          f"median={_median(deltas):+6.2f} pp  sum={sum(deltas):+7.2f} pp")
    print(f"    cf arm  net ROE mean={_avg([p['cf_net_roe'] for p in non_trend]):+6.2f}%  "
          f"median={_median([p['cf_net_roe'] for p in non_trend]):+6.2f}%")
    print(f"    real    net ROE mean={_avg([p['real_net_roe'] for p in non_trend]):+6.2f}%  "
          f"median={_median([p['real_net_roe'] for p in non_trend]):+6.2f}%")
    print(f"    pairs improved={improved}  worse={worse}  "
          f"flat={n - improved - worse}  "
          f"improve rate={improved/n*100:4.1f}%")
    if real_hold:
        print(f"    hold minutes  cf median={_median(cf_hold):5.1f}  "
              f"real median={_median(real_hold):5.1f}  "
              f"(shorter under cf: "
              f"{'YES' if _median(cf_hold) < _median(real_hold) else 'NO'})")
    print(f"    floor_breach give-up cluster: {len(floor_give)} pairs, "
          f"foregone sum={floor_give_sum:+.2f} pp")

    lo = hi = None
    pval = _wilcoxon_one_sided(deltas)
    if n >= D2_MIN_PAIRS:
        lo, hi = _block_bootstrap_mean_ci(
            non_trend, D2_BOOT_BOOTSTRAPS, D2_BOOT_SEED)
        print(f"\n    block bootstrap (15-min entry blocks, B={D2_BOOT_BOOTSTRAPS}, "
              f"seed={D2_BOOT_SEED})")
        print(f"      mean dROE 95% CI = [{lo:+.2f}, {hi:+.2f}] pp  "
              f"(lower bound strictly > 0 required)")
        if pval is not None:
            print(f"    one-sided paired Wilcoxon p = {pval:.4f}")
        else:
            print("    Wilcoxon: undefined (all-zero deltas)")

    print("\n    PROMOTE GATE (all must hold; pre-registered):")
    checks = [
        (f"n={n} >= {D2_MIN_PAIRS} paired non_trend pairs", n >= D2_MIN_PAIRS),
        ("block-bootstrap 95% CI lower bound > 0",
         lo is not None and lo > 0.0),
        ("one-sided Wilcoxon p < 0.05",
         pval is not None and pval < 0.05),
        ("cf median hold shorter than real",
         bool(real_hold) and _median(cf_hold) < _median(real_hold)),
        ("majority of pairs improved", improved > n / 2),
        ("overall net dROE positive incl. floor_breach give-up",
         sum(deltas) > 0.0),
    ]
    for label, ok in checks:
        print(f"      [{'PASS' if ok else 'WAIT'}] {label}")
    if n < D2_MIN_PAIRS:
        print(f"\n    NO VERDICT: only {n}/{D2_MIN_PAIRS} pairs. Do not tune "
              f"the candidate or read interim significance until the natural "
              f"sample reaches {D2_MIN_PAIRS} (other cutoffs, e.g. 30 min, "
              f"stay post-hoc hypotheses and do not share this alpha).")
    elif all(ok for _, ok in checks):
        print("\n    ALL GATES PASS — eligible for a SEPARATE regime-aware "
              "change that sets the non_trend binding ignite grace < 60 min "
              "(trend untouched). Code/config promotion still needs its own "
              "review; this report is evidence only.")
    else:
        print("\n    GATE NOT MET — keep the binding ignite grace unchanged; "
              "continue record-only collection.")


def _summary(records: List[dict]) -> None:
    by_rule: Dict[str, List[dict]] = {}
    for r in records:
        if r.get("outcome") and r["outcome"] not in (
                "bad_timestamp", "no_entry_px", "short_unsupported",
                "no_future_bars"):
            by_rule.setdefault(r["rule"], []).append(r)

    print("\n=== risk-tuning shadow reconciliation ===")
    _summarize_exit_outcomes(records)
    _summarize_ignite_grace_cf(records)
    for rule, rows in sorted(by_rule.items()):
        graded = [r for r in rows if r.get("outcome") not in (None, "")]
        print(f"\n[{rule}]  graded={len(graded)} / total={len(rows)}")
        counts: Dict[str, int] = {}
        for r in graded:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        for k, v in sorted(counts.items()):
            print(f"    {k:30} {v}")

        if rule in ("breakout_score_floor", "per_coin_cooldown"):
            pnls = [float(r.get("pnl_pct", 0)) for r in graded
                    if "pnl_pct" in r]
            if pnls:
                wins = [p for p in pnls if p > 0]
                print(f"    blocked-admit counter-factual: "
                      f"winners={len(wins)} losers={len(pnls)-len(wins)} "
                      f"mean={sum(pnls)/len(pnls):+.2f}%")
        if rule == "leverage_tier":
            for key in ("live_pnl_pct", "proposed_pnl_pct"):
                vals = [float(r[key]) for r in graded if key in r]
                if vals:
                    print(f"    {key:18} mean={sum(vals)/len(vals):+.2f}% "
                          f"sum={sum(vals):+.2f}%")
        if rule == "stop_tuning":
            green = sum(1 for r in graded
                        if r["outcome"] == "wider_cap_would_green")
            nogain = sum(1 for r in graded
                         if r["outcome"] == "wider_cap_no_gain")
            print(f"    wider cap would have turned GREEN: {green} ; "
                  f"still no gain: {nogain}")

    print("\n  per-record detail:")
    for r in sorted(records, key=lambda x: x.get("timestamp", "")):
        if r.get("rule") in ("exit_outcome", "ignite_grace_cf"):
            continue  # self-contained; summarized in the sections above
        if r.get("outcome"):
            d = r.get("detail") or {}
            extra = ""
            if "pnl_pct" in r:
                extra = f"cf_pnl={r['pnl_pct']:+.2f}%"
            elif r["rule"] == "leverage_tier" and "live_pnl_pct" in r:
                extra = (f"live={r['live_pnl_pct']:+.1f}% "
                         f"prop={r['proposed_pnl_pct']:+.1f}%")
            elif r["rule"] == "stop_tuning":
                w = r.get("wider_cap_sim") or {}
                extra = f"wider_sim={w.get('pnl_pct')}%/{w.get('reason')}"
            print(f"    {r.get('timestamp')}  {r.get('rule'):22} "
                  f"{r.get('coin'):9} -> {r['outcome']:28} {extra}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=SHADOW_FILE)
    ap.add_argument("--window-hours", type=float, default=24.0,
                    help="Only grade records older than this many hours")
    ap.add_argument("--write", action="store_true",
                    help="Write outcomes back to the JSONL (default: report)")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"shadow file not found (no candidate recorded yet): {args.file}")
        return 1

    records = [json.loads(line) for line in open(args.file, encoding="utf-8")
               if line.strip()]
    cutoff = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600
    dsl_cfg = read_agent_config() or {}
    closes_by_coin = _load_real_closes()

    graded = pending = skipped = 0
    for r in records:
        # exit_outcome / ignite_grace_cf rows are self-contained observations;
        # they need no candle grading and are aggregated directly by _summary.
        if r.get("rule") in ("exit_outcome", "ignite_grace_cf"):
            skipped += 1
            continue
        if r.get("outcome") is not None:
            continue
        ts = _parse_iso(r.get("timestamp", ""))
        if ts is None or ts.timestamp() > cutoff:
            pending += 1
            continue
        if grade_record(r, dsl_cfg, closes_by_coin):
            graded += 1
        else:
            pending += 1

    print(f"total={len(records)}  newly_graded={graded}  "
          f"pending(immature/unmatched)={pending}  "
          f"observed(no grading)={skipped}")
    _summary(records)

    if args.write:
        with open(args.file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nOutcomes written back to {args.file}")
    else:
        print("\n(dry-run; pass --write to persist outcomes into the JSONL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
