"""Whale Index — smart-money signal heuristics over the Hyperliquid universe.

Provides OI/funding-based concentration and accumulation signals. The HL
PUBLIC api has NO leaderboard endpoint (verified: vaults/leaderBoard/
vaultDetails all return None), so production uses self-sourced, verifiable
signals (OI surge + negative funding) rather than a static wallet registry.
The older per-wallet leaderboard/get_trader_state helpers were removed
because the WHALE_WALLETS registry was always empty — they only ever
returned [] / None and gave a false impression of coverage. MCP callers use
hyperfeed.leaderboard_get_top (private backend) instead.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from hermes_trader.agents import atomic_io
from hermes_trader.agents.config_store import cfg_get
from hermes_trader.client.universe import get_universe

logger = logging.getLogger(__name__)

# ── Config wiring (R13-B7) ──────────────────────────────────────────────────
# Literal fallbacks (== pre-R13-B7 behavior); the CANONICAL `whale_index` block
# carries the same values and is the single source of truth at runtime (env
# HERMES_CFG_WHALE_INDEX__* overrides included).
_WHALE_INDEX_DEFAULTS: dict[str, Any] = {
    "min_volume_usd": 1_000_000.0,
    "funding_confidence_scale": 0.0001,
    "oi_vol_ratio_min": 10,
    "oi_vol_confidence_norm": 50,
    "min_oi_usd": 5_000_000.0,
    "max_funding_threshold": -0.00001,
    "funding_norm": 0.00008,
    "flat_price_pct": 10,
    "min_oi_growth_pct": 8.0,
    "max_price_move_pct": 4.0,
    "surge_norm_pct": 25.0,
    "min_confidence": 0.05,
    "mcp_min_confidence": 0.1,
    "mcp_top_n": 10,
}


def whale_index_params(*, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve the whale-index heuristic knobs (canonical ``whale_index`` block)
    with the module literals as fallback. Per-leaf lookups keep every key
    independently env-overridable; magnitudes/thresholds must be positive (the
    negative-funding threshold is kept as-is), int knobs must be positive ints.
    Any read/coerce failure returns a fresh copy of the literals — the signal
    path must never break on a bad config."""
    p = dict(_WHALE_INDEX_DEFAULTS)
    try:
        positive_float = ("min_volume_usd", "funding_confidence_scale", "min_oi_usd",
                          "funding_norm", "min_oi_growth_pct", "max_price_move_pct",
                          "surge_norm_pct", "min_confidence", "mcp_min_confidence")
        for key in positive_float:
            v = cfg_get(f"whale_index.{key}", config=config)
            if v is not None:
                fv = float(v)
                p[key] = fv if fv > 0 else p[key]
        v = cfg_get("whale_index.max_funding_threshold", config=config)
        if v is not None:
            p["max_funding_threshold"] = float(v)
        for key in ("oi_vol_ratio_min", "oi_vol_confidence_norm", "flat_price_pct",
                    "mcp_top_n"):
            v = cfg_get(f"whale_index.{key}", config=config)
            if v is not None:
                iv = int(v)
                p[key] = iv if iv > 0 else p[key]
    except Exception as e:  # never let config break the signal path
        logger.debug(f"[whale] whale_index params config read failed: {e}")
        return dict(_WHALE_INDEX_DEFAULTS)
    return p

# Persisted OI snapshots for the self-sourced OI-surge whale detector. The HL
# PUBLIC api has NO leaderboard endpoint (verified: vaults/leaderBoard/vaultDetails
# all return None) — Senpi uses a private backend. So instead of a static wallet
# list that goes stale, we build a VERIFIABLE whale signal from data we pull
# ourselves: snapshot open-interest each scan, and flag coins where OI surges
# (positions being built) while price stays flat = smart money loading quietly.
_OI_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".oi-history.json",
)

def smart_money_concentration(
    lookback_days: Optional[int] = None,
    min_volume_usd: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Identify assets with growing smart money concentration.

    Analyzes OI + volume distribution to find assets where large traders
    are accumulating positions. Flags:
    - OI growth outpaces volume growth
    - Top whales increasing positions in same asset
    - OI concentration in top 10 wallets

    Args:
        lookback_days: how far back to scan for concentration changes
        min_volume_usd: minimum 24h volume threshold
    """
    p = whale_index_params()
    if min_volume_usd is None:
        min_volume_usd = p["min_volume_usd"]
    funding_confidence_scale = p["funding_confidence_scale"]
    oi_vol_ratio_min = p["oi_vol_ratio_min"]
    oi_vol_confidence_norm = p["oi_vol_confidence_norm"]
    universe = get_universe()
    results = []
    
    for m in universe:
        day_oi = m.get("openInterest", 0)
        day_vol = m.get("dayNtlVlm", 0)
        funding = m.get("funding", 0)
        mid_px = m.get("midPx", 0)
        
        if day_vol < min_volume_usd:
            continue
        
        # Concentration signal: OI growing + negative funding = accumulation
        # (whales buying while retail sells into dips)
        if day_oi > 0 and funding < 0:
            results.append({
                "coin": m["coin"],
                "type": m["type"],
                "signal": "accumulation",
                "confidence": min(1.0, abs(funding) / funding_confidence_scale),  # scale funding magnitude
                "oi": day_oi,
                "volume_24h": day_vol,
                "funding_rate": funding,
                "mid_price": mid_px,
            })
        
        # High OI relative to volume = whale accumulation
        oi_vol_ratio = day_oi / (day_vol / 1e6) if day_vol > 0 else 0
        if oi_vol_ratio > oi_vol_ratio_min:
            results.append({
                "coin": m["coin"],
                "type": m["type"],
                "signal": "high_oi_concentration",
                "confidence": min(1.0, oi_vol_ratio / oi_vol_confidence_norm),  # scale ratio
                "oi": day_oi,
                "volume_24h": day_vol,
                "oi_volume_ratio": oi_vol_ratio,
                "mid_price": mid_px,
            })
    
    return sorted(results, key=lambda x: x["confidence"], reverse=True)


def oi_funding_anomaly(
    min_oi_usd: Optional[float] = None,
    max_funding_threshold: Optional[float] = None,
    funding_norm: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Detect assets where OI is high but price is flat while funding is
    negative — classic smart money accumulation pattern.

    Signal: whales are building long positions (high OI) while retail is
    shorting (negative funding). When the crowd finally covers, price squeezes up.

    RECALIBRATED 2026-06-02 (audit): the prior thresholds (OI>=$10M, funding<
    -0.00005) flagged only 1 coin across the whole universe — the funding cut sat
    at the ~p10 extreme of negative funding, so the signal was effectively dead and
    the downstream whale force-execute + 1.3x size + regime-bypass machinery never
    fired. Loosened to OI>=$5M and funding<-0.00001 (catches the real negative-
    funding cohort, ~7-9 coins) AND fixed the confidence normalization: it was
    dividing by 0.0005 (so a -0.00001 coin scored 0.02 and got filtered by the
    0.05 min_confidence gate downstream — a second silent kill). Now normalizes
    against `funding_norm` (0.00008 ≈ the deeply-negative end) so a real anomaly
    clears the gate. All three tunable.

    Args:
        min_oi_usd: minimum OI notional in USD (OI_coins * price)
        max_funding_threshold: funding rate must be below this (more negative = stronger)
        funding_norm: funding magnitude that maps to ~full confidence
    """
    p = whale_index_params()
    if min_oi_usd is None:
        min_oi_usd = p["min_oi_usd"]
    if max_funding_threshold is None:
        max_funding_threshold = p["max_funding_threshold"]
    if funding_norm is None:
        funding_norm = p["funding_norm"]
    flat_price_pct = p["flat_price_pct"]
    universe = get_universe(include_hip3=True)
    results = []

    for m in universe:
        oi_coins = m.get("openInterest", 0)
        funding = m.get("funding", 0)
        mid_px = m.get("midPx", 0)
        prev_px = m.get("prevDayPx", 0)

        # BUGFIX 2026-06-02 audit: openInterest is in COIN UNITS, not USD. The old
        # `oi < min_oi_usd` compared coins to dollars — nonsensical (BTC's 29,885-coin
        # / $2.2B OI failed a "$5M" filter while a 10M-token meme passed). Convert to
        # true USD notional = OI * price.
        oi = oi_coins * mid_px if mid_px > 0 else 0

        if oi < min_oi_usd or funding > max_funding_threshold:
            continue

        price_change_24h = (mid_px - prev_px) / prev_px * 100 if prev_px > 0 else 0

        # Signal: OI high + funding negative + price relatively flat (quiet accumulation)
        if abs(price_change_24h) < flat_price_pct:
            results.append({
                "coin": m["coin"],
                "type": m["type"],
                "signal": "smart_money_accumulation",
                "confidence": (
                    min(1.0, abs(funding) / funding_norm)   # funding magnitude (calibrated)
                    * (1 - abs(price_change_24h) / flat_price_pct)      # flatter price = stronger
                ),
                "oi": oi,
                "funding_rate": funding,
                "price_24h_change_pct": price_change_24h,
                "mid_price": mid_px,
                "prev_day_px": prev_px,
            })

    return sorted(results, key=lambda x: x["confidence"], reverse=True)


# ── OI-surge whale detector (self-sourced, verifiable) ───────────────

def oi_surge_accumulation(
    min_oi_usd: Optional[float] = None,
    min_oi_growth_pct: Optional[float] = None,
    max_price_move_pct: Optional[float] = None,
    surge_norm_pct: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Flag coins whose OPEN INTEREST surged since the last scan while price stayed
    flat — positions being built quietly = smart-money accumulation, about to move.

    Fully self-sourced & verifiable: we snapshot OI from get_universe() (HL public
    data) to `.oi-history.json` each call and compare to the prior snapshot. No
    external leaderboard / wallet list needed (HL has no public leaderboard API).

    A coin qualifies if: OI >= min_oi_usd, OI grew >= min_oi_growth_pct vs last
    snapshot, and |price move since last snapshot| <= max_price_move_pct (the
    "loading while flat" tell — if price already ran, the move's not ahead of us).
    """
    p = whale_index_params()
    if min_oi_usd is None:
        min_oi_usd = p["min_oi_usd"]
    if min_oi_growth_pct is None:
        min_oi_growth_pct = p["min_oi_growth_pct"]
    if max_price_move_pct is None:
        max_price_move_pct = p["max_price_move_pct"]
    if surge_norm_pct is None:
        surge_norm_pct = p["surge_norm_pct"]
    universe = get_universe()
    now = time.time()
    # load prior snapshot
    prev = {}
    try:
        with open(_OI_HISTORY_FILE) as f:
            blob = json.load(f)
            prev = blob.get("oi", {})
            prev_ts = blob.get("ts", 0)
    except (OSError, json.JSONDecodeError):
        prev_ts = 0  # noqa: F841  (P1-2 baseline: legacy unused; trading logic untouched)

    cur = {}
    results = []
    for m in universe:
        coin = m.get("coin")
        oi_coins = float(m.get("openInterest", 0) or 0)
        mid = float(m.get("midPx", 0) or 0)
        if not coin or oi_coins <= 0 or mid <= 0:
            continue
        # openInterest is in COIN UNITS. Store BOTH: coin-units (for true position
        # growth, price-independent) and USD notional (for the size gate). OI-growth
        # MUST be on coin units — computing it on USD notional would let a price rise
        # masquerade as position-building (a false surge).
        oi_usd = oi_coins * mid
        cur[coin] = {"oi_coins": oi_coins, "oi": oi_usd, "px": mid}
        p = prev.get(coin)
        if oi_usd < min_oi_usd or not p:
            continue
        # back-compat: older snapshots only stored "oi" (was raw coins pre-fix);
        # prefer oi_coins, fall back to oi.
        p_oi_coins = p.get("oi_coins", p.get("oi", 0)); p_px = p.get("px", 0)
        if p_oi_coins <= 0 or p_px <= 0:
            continue
        oi_growth = (oi_coins - p_oi_coins) / p_oi_coins * 100   # COIN-unit growth (price-independent)
        px_move = abs(mid - p_px) / p_px * 100
        if oi_growth >= min_oi_growth_pct and px_move <= max_price_move_pct:
            results.append({
                "coin": coin,
                "type": m.get("type"),
                "signal": "oi_surge_accumulation",
                "confidence": min(1.0, oi_growth / surge_norm_pct) * (1 - px_move / max(max_price_move_pct, 1e-9)),
                "oi": oi_usd,
                "oi_growth_pct": round(oi_growth, 1),
                "price_move_pct": round(px_move, 2),
                "mid_price": mid,
            })

    # persist current snapshot (best-effort, atomic rename; regenerable cache
    # so fsync=False — agents.atomic_io owns the tmp+replace machinery).
    try:
        atomic_io.write_json_atomic(
            _OI_HISTORY_FILE, {"ts": now, "oi": cur}, indent=None, fsync=False
        )
    except OSError as e:
        logger.warning(f"[whale] OI history persist failed: {e}")

    return sorted(results, key=lambda x: x["confidence"], reverse=True)


# ── Whale Index MCP Integration ─────────────────────────────────────
# These functions can be registered as MCP tools for autonomous agents
# to query whale data as part of their scanning pipeline.

def whale_accumulation_map(min_confidence: Optional[float] = None) -> dict[str, dict[str, Any]]:
    """Return {coin: signal_dict} for coins flagged as smart-money accumulation.

    MERGES two self-sourced, verifiable signals (no external leaderboard needed):
      1. oi_funding_anomaly — high OI + deeply negative funding + flat price
         (whales long while retail shorts -> squeeze setup).
      2. oi_surge_accumulation — OI surging vs last scan + price flat
         (positions being built quietly, move not yet happened).
    A coin flagged by EITHER (or both, taking the higher confidence) is returned.
    These feed perception.whale_signal -> executor force-execute + 1.3x size + regime bypass.
    """
    if min_confidence is None:
        min_confidence = whale_index_params()["min_confidence"]
    merged: dict[str, dict[str, Any]] = {}
    for s in oi_funding_anomaly() + oi_surge_accumulation():
        if s.get("confidence", 0) < min_confidence:
            continue
        c = s["coin"]
        if c not in merged or s["confidence"] > merged[c]["confidence"]:
            merged[c] = s
    return merged


def get_whale_signals(
    min_confidence: Optional[float] = None,
    top_n: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Aggregate concentration + anomaly signals for MCP-tool callers.

    Kept for the MCP exposure (`whale_index` tool). Production perception
    uses `whale_accumulation_map()` instead — the high-OI-concentration
    branch this combines in is too noisy for direction calls.
    """
    p = whale_index_params()
    if min_confidence is None:
        min_confidence = p["mcp_min_confidence"]
    if top_n is None:
        top_n = int(p["mcp_top_n"])
    concentration = smart_money_concentration()
    anomalies = oi_funding_anomaly()
    
    # Merge signals by coin
    merged: dict[str, dict[str, Any]] = {}
    for sig in concentration + anomalies:
        coin = sig["coin"]
        if coin not in merged:
            merged[coin] = {"coin": coin, "signals": [], "max_confidence": 0}
        merged[coin]["signals"].append(sig)
        merged[coin]["max_confidence"] = max(
            merged[coin]["max_confidence"],
            sig.get("confidence", 0),
        )
    
    # Filter and sort
    results = [
        s for s in merged.values()
        if s["max_confidence"] >= min_confidence
    ]
    return sorted(results, key=lambda x: x["max_confidence"], reverse=True)[:top_n]
