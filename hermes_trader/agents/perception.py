"""Perception scan engine — sweeps Hyperliquid markets for trigger signals.

Fetches candles, runs trigger detection, and returns candidates that meet the
composite-score threshold. Scans fan out across threads; volume pre-filtering
limits the sweep to the top-N markets by 24h volume to stay within HL's
1200 weight/minute rate limit (candle fetch = 20 weight each).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from hermes_trader.agents.config import get_config, trigger_thresholds_params, trigger_weights_params
from hermes_trader.agents.config_store import cfg_get
from hermes_trader.client.cache import _Cache
from hermes_trader.client.hl_client import fetch_all_mids, fetch_hl_candles
from hermes_trader.client.universe import get_universe
from hermes_trader.indicators import triggers as trigger_mod
from hermes_trader.models.types import Candle

logger = logging.getLogger(__name__)

# ── Feed freshness: data-gap accounting (Phase-0 hardening) ──────────────────
# A coin whose candle fetch comes back empty/short is treated as "no trigger"
# (perception returns (True, None)) — which is INDISTINGUISHABLE from a real
# no-signal. So a big mover we simply failed to read looks identical to one we
# evaluated and skipped. We now count those per scan (thread-safe; scan fans out
# over a ThreadPoolExecutor) and surface the count in the scan summary so a
# silent data gap is visible, not invisible. Observability only — no behavior
# change to what gets traded.
_data_gap_lock = threading.Lock()
_data_gap_count = 0


def _reset_data_gaps() -> None:
    global _data_gap_count
    with _data_gap_lock:
        _data_gap_count = 0


def _note_data_gap() -> None:
    global _data_gap_count
    with _data_gap_lock:
        _data_gap_count += 1


def _get_data_gaps() -> int:
    with _data_gap_lock:
        return _data_gap_count

# ── Candle cache (module-level, shared across ticks) ──────────────────────────
# Per-coin TTL cache backed by the shared _Cache abstraction (LRU + TTL). The
# 5m interval uses the short scan TTL; the 1h interval uses a longer TTL so
# slow-burn/accumulation triggers don't refetch a static 1h candle every tick.
# The hl_client layer also caches raw candles at 90s; this per-interval TTL
# layer keeps the 1h data around for longer (1h bars don't change intra-hour).
_CANDLE_CACHE_MAX = int(os.environ.get("HERMES_PERCEPTION_CACHE_MAX", "512"))
_candle_cache: _Cache = _Cache(max_size=_CANDLE_CACHE_MAX, default_ttl=50.0)


# P2-6: single canonical way to read fired trigger names off a perception.
# Previously the same comprehension was inlined in research / ta_filter /
# perception / __main__ (and a *string* lookup in the executor); any change to
# the trigger shape had to be made in every site.
def extract_fired_triggers(perception: Optional[dict[str, Any]]) -> list[str]:
    """Names of the triggers that fired for a perception (de-duplicated,
    order-preserving). Accepts a perception dict; also tolerates a plain
    ``{"fired_triggers": [...]}`` analysis dict."""
    if not isinstance(perception, dict):
        return []
    if "fired_triggers" in perception and "triggers" not in perception:
        # Already-normalized analysis payload (executor side).
        return [str(n) for n in (perception.get("fired_triggers") or []) if n]
    seen: dict[str, None] = {}
    for t in perception.get("triggers") or []:
        if isinstance(t, dict) and t.get("fired") and t.get("name"):
            seen.setdefault(str(t["name"]), None)
    return list(seen.keys())


def signal_fingerprint(perception: dict[str, Any] | None) -> tuple | None:
    """O-2: content-level identity of the signal a perception represents.

    Returns ``(coin, bar_close_ms, fired_triggers)`` — the closed bar the
    triggers were scored on plus the exact set of triggers that fired on it.
    Two perceptions produced from the SAME closed bar with the SAME fired
    triggers are the same setup even if they were scanned on different cycles
    and got different random perception ids; the paid LLM research (and any
    downstream action) should happen once for that setup.

    Returns ``None`` when the payload carries no ``bar_close_ms`` (older
    payloads) or is not a perception dict — callers treat None as "no dedup
    key", so the gate is inert rather than crashing the loop.
    """
    if not isinstance(perception, dict):
        return None
    bar_close_ms = perception.get("bar_close_ms")
    if bar_close_ms is None:
        return None
    coin = perception.get("coin")
    fired = tuple(sorted(extract_fired_triggers(perception)))
    return (coin, bar_close_ms, fired)


def _make_cache_key(coin: str, interval: str, count: int) -> str:
    return f"{coin}:{interval}:{count}"


def _fetch_candles_sync(
    coin: str,
    interval: str,
    count: int,
    cache_ttl_ms: int,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    force_refresh: bool = False,
) -> Optional[list[Candle]]:
    """Fetch candles from the SDK with in-memory caching and retry on 429.

    When ``force_refresh`` is True the cache is bypassed and the result is
    written back to the cache — used right after a bar closes to make sure we
    evaluate the just-closed bar rather than a snapshot captured while it was
    still forming.
    """
    key = _make_cache_key(coin, interval, count)
    ttl_s = cache_ttl_ms / 1000.0
    if not force_refresh:
        # _Cache returns None for both misses and cached-None; candles are
        # never stored as None here (empty result is not cached), so this is
        # unambiguous.
        cached = _candle_cache.get(key)
        if cached is not None:
            return cached

    for attempt in range(max_retries):
        try:
            candles = fetch_hl_candles(coin, interval, count)
            if not candles:
                return None
            _candle_cache.set(key, candles, ttl=ttl_s)
            return candles
        except Exception as e:
            err_str = str(e).lower()
            if attempt < max_retries - 1 and ("429" in err_str or "rate" in err_str or "connection" in err_str or "timeout" in err_str):
                wait = backoff_base ** attempt
                logger.warning(f"[candles] rate-limited/connection error for {coin} {interval}, retry {attempt+1}/{max_retries} in {wait:.1f}s")
                time.sleep(wait)
            else:
                logger.error(f"[candles] failed for {coin} {interval}: {e}")
                return None

    return None


# ── Bar-close alignment (fix for in-flight bar scoring) ──────────────────────
# Triggers only score candles[-1]. If that candle is still forming, its close
# and volume are intra-bar snapshots (e.g. at 40% of the bar the breakout/RVOL
# that will land at the close isn't there yet), so a strong close can be scored
# as a weak near-miss and then slide to [-2] once the next bar opens — never
# evaluated. We therefore (a) drop the still-forming last bar, scoring the last
# CLOSED bar instead, and (b) force a cache-bypass refresh for a short window
# after a bar closes so we don't read a stale snapshot that predates the close.

_INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400,
}


def _interval_seconds(interval: str) -> int:
    return _INTERVAL_SECONDS.get(interval, 300)


def _last_bar_closed(candles: list[Candle], interval: str, now_ms: Optional[float] = None) -> bool:
    """True if the final candle's close time has already passed."""
    if not candles:
        return False
    last = candles[-1]
    bar_open_ms = float(getattr(last, "t"))
    bar_dur_ms = _interval_seconds(interval) * 1000
    cur_ms = now_ms if now_ms is not None else time.time() * 1000
    return cur_ms >= bar_open_ms + bar_dur_ms


def _drop_forming_bar(candles: list[Candle], interval: str) -> tuple[list[Candle], bool]:
    """Return (candles_to_score, dropped). If the last bar is still forming,
    drop it so triggers evaluate the last CLOSED bar. ``dropped`` reports
    whether a forming bar was removed (for logging)."""
    if _last_bar_closed(candles, interval):
        return candles, False
    return candles[:-1], True


def _apply_squeeze_breakout_coupling(hits: list[dict[str, Any]]) -> None:
    """Mutate `hits` in place: when a Bollinger squeeze (rangeCompression)
    fired alongside a fired breakout, BOOST the breakout hit's score by +2
    (cap 10) and tag the reason. A breakout resolving out of a squeeze is a
    higher-conviction setup than one from already-wide bands (which is more
    often a late chase / stop run). rangeCompression itself carries weight 0,
    so the boost is expressed on the breakout hit instead."""
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


# ── Scan single market (returns result or (False, error)) ────────────────────

def _scan_single_market(
    market: dict[str, Any],
    mid: float,
    config: dict[str, Any],
    min_score: float,
    whale_signals: Optional[dict[str, dict[str, Any]]] = None,
    whale_scan_bypass: bool = False,
    trend_surface_enabled: bool = True,
) -> tuple[bool, dict[str, Any] | str | None]:
    """Run all triggers on a single market's candles.

    `whale_signals` is the per-scan whale_accumulation_map() result, keyed by
    coin. When present and the coin matches, the perception's `whale_signal`
    field carries the signal dict for downstream gating.

    Returns (success, perception_dict | None) on success, or (False, error_string).
    Designed to run inside a ThreadPoolExecutor worker.
    """
    try:
        scan_cfg = config["scan"]
        interval = scan_cfg["candleInterval"]
        count = scan_cfg["candleCount"]
        now_ms = time.time() * 1000
        bar_dur_ms = _interval_seconds(interval) * 1000

        # Right after a bar closes, the in-memory snapshot may still predate the
        # close (the cache TTL is 50s). Bypass the cache for a short window so
        # we reliably read the just-completed bar.
        post_close_ms = scan_cfg.get("postCloseForceRefreshMs", 15_000)
        since_bar_open_ms = now_ms % bar_dur_ms
        force_refresh = since_bar_open_ms < post_close_ms

        candles = _fetch_candles_sync(
            market["coin"],
            interval,
            count,
            scan_cfg["cacheTtlMs"],
            force_refresh=force_refresh,
        )

        if not candles or len(candles) < 50:
            # Empty/short can mean genuine thin history OR a fetch failure that
            # survived retries (429/timeout). Either way we evaluated nothing
            # here — count it as a data gap so the scan summary distinguishes
            # "read it, no signal" from "couldn't read it". (Still returns the
            # same (True, None) — no behavior change.)
            _note_data_gap()
            return (True, None)  # Not an error, just no triggers

        # Evaluate CLOSED bars only. If the last bar is still forming, drop it
        # so triggers score the last completed bar instead of an intra-bar
        # snapshot (whose partial close/volume would understate a breakout and
        # then vanish to [-2] on the next bar — the 12:40 BTC missed-surge bug).
        if scan_cfg.get("evaluateClosedBarsOnly", True):
            candles, dropped_forming = _drop_forming_bar(candles, interval)
            if dropped_forming and force_refresh:
                logger.debug(
                    f"[bar-align] {market['coin']} {interval}: force-refreshed at "
                    f"{since_bar_open_ms/1000:.1f}s after close, evaluating last "
                    f"closed bar t={getattr(candles[-1], 't')}"
                )
            if len(candles) < 50:
                _note_data_gap()
                return (True, None)

        # 1h candles for slow-burn / accumulation triggers. Cached far longer
        # than 5m (1h bars don't change intra-hour). Failure here doesn't
        # block the scan — slow-burn triggers just won't fire.
        candles_1h = _fetch_candles_sync(
            market["coin"], "1h", 48,
            scan_cfg.get("cacheTtlMs1h", 600_000),
        ) or []
        if scan_cfg.get("evaluateClosedBarsOnly", True) and candles_1h:
            candles_1h, _ = _drop_forming_bar(candles_1h, "1h")

        thresholds = config["thresholds"]
        hits = [
            trigger_mod.pct_move_spike(candles, thresholds["sigmaThreshold"]),
            trigger_mod.volume_spike(candles, thresholds["sigmaThreshold"]),
            trigger_mod.breakout(
                candles,
                thresholds["breakoutLookback"],
                min_rvol=thresholds.get("breakoutMinRvol", 1.5),
                rvol_window=thresholds.get("breakoutRvolWindow", 20),
                atr_score_mult=thresholds.get("breakoutAtrScoreMult", 3.0),
                confirm_bars=thresholds.get("breakoutConfirmBars", 2),
            ),
            trigger_mod.range_compression(candles, thresholds["bbLength"], thresholds["bbStdDev"]),
            trigger_mod.trend_strength(candles, thresholds["adxPeriod"]),
            trigger_mod.momentum_burst(candles, thresholds["momentumLookback"], thresholds["momentumPct"]),
            trigger_mod.volume_buildup_1h(candles_1h, thresholds.get("volBuildupRatio", 2.5)),
            trigger_mod.trend_flip_1h(candles_1h, thresholds.get("trendFlipBars", 3)),
            trigger_mod.higher_lows_1h(candles_1h, thresholds.get("higherLowsRequired", 4)),
            # Symmetric directional surfacing (weight 0 → no composite-denominator
            # impact). uptrend/downtrend momentum surface a coin in a sustained
            # intraday trend for research REGARDLESS of the bullish-biased composite
            # gate — the down side is what lets us short selloffs (the weighted
            # triggers are all long-structured, so down-movers scored ~0 and never
            # reached the AI). Acts as a bypass below; the AI + aligned-conf bar +
            # short floor + counter-regime gate adjudicate direction/execution.
            trigger_mod.uptrend_momentum(candles, thresholds.get("trendMomentumLookback", 72),
                                         thresholds.get("trendMomentumPct", 5.0)),
            trigger_mod.downtrend_momentum(candles, thresholds.get("trendMomentumLookback", 72),
                                           thresholds.get("trendMomentumPct", 5.0)),
        ]

        # Squeeze-breakout coupling: a breakout that resolves out of a Bollinger
        # squeeze is a higher-conviction setup (boost breakout +2, cap 10).
        # rangeCompression carries weight 0 standalone, so the boost is applied
        # to the breakout hit. Factored into _apply_squeeze_breakout_coupling.
        _apply_squeeze_breakout_coupling(hits)

        # Momentum-continuation trigger (LEAK #2) — OFF by default. Catches a coin
        # in a sustained multi-hour uptrend that is now consolidating (already-
        # extended movers that print no fresh 5m spike, so the other triggers miss
        # them). Gated so it has ZERO scoring effect when off: only when enabled is
        # the hit appended AND its weight added to the denominator. LONG-biased —
        # enable only when the macro regime is up/neutral (counter-trend gate backs it up).
        _mc = config.get("momentum_continuation", {}) or {}
        _score_weights = config["weights"]
        if _mc.get("enabled"):
            hits.append(trigger_mod.momentum_continuation_1h(
                candles_1h,
                _mc.get("min_trend_pct", 8.0),
                _mc.get("max_pullback_pct", 6.0),
            ))
            _score_weights = {**config["weights"],
                              "momentumContinuation1h": _mc.get("weight", 0.4)}

        # Candlestick reversal patterns — OFF by default. Shooting-star / bearish-
        # engulfing (top of an advance → SHORT) and hammer / bullish-engulfing
        # (bottom of a decline → LONG). The momentum/breakout triggers are weak at
        # calling tops & bottoms; these catch exhaustion/reversal. Surfacing bypass
        # (weight 0, like uptrend/downtrend) — the AI (which now also sees raw OHLC)
        # adjudicates direction/execution. Gated so it's reversible without code.
        _cp = config.get("candlestick_patterns", {}) or {}
        if _cp.get("enabled"):
            _wbr = _cp.get("wick_body_ratio", 2.0)
            _ctx_lb = int(_cp.get("context_lookback", 6))
            _ctx_pct = _cp.get("context_pct", 1.5)
            hits.append(trigger_mod.bearish_reversal_candle(candles, _wbr, _ctx_lb, _ctx_pct))
            hits.append(trigger_mod.bullish_reversal_candle(candles, _wbr, _ctx_lb, _ctx_pct))

        # Daily mover surfacing: the scan already reserves slots for top 24h
        # movers, but the trigger gate can still drop an orderly runner once the
        # fresh spike/breakout bar has passed. Surface large liquid movers to AI
        # as a weight-0 trigger so SHADOW can tell us whether they are late-chase
        # junk or real continuation setups. Execution is still governed by the
        # runner gate, liquidity gate, AI confidence, and SHADOW/LIVE mode.
        _rms = config.get("runner_mover_surface") or {}
        daily_mover_fired = False
        daily_mover_reason = ""
        if bool(_rms.get("enabled", False)):
            prev = float(market.get("prevDayPx") or 0)
            cur = float(mid or market.get("midPx") or market.get("markPx") or 0)
            vol = float(market.get("dayNtlVlm") or 0)
            if prev > 0 and cur > 0:
                move_pct = (cur - prev) / prev * 100
                is_hip3 = bool(market.get("dex"))
                min_move = float(_rms.get(
                    "min_hip3_24h_pct" if is_hip3 else "min_crypto_24h_pct",
                    8.0 if is_hip3 else 10.0,
                ))
                min_vol = float(_rms.get("min_volume_usd", 5_000_000))
                daily_mover_fired = move_pct >= min_move and vol >= min_vol
                daily_mover_reason = (
                    f"{move_pct:+.1f}% 24h mover, vol ${vol/1e6:.2f}M"
                    if daily_mover_fired
                    else f"{move_pct:+.1f}% 24h / vol ${vol/1e6:.2f}M"
                )
        hits.append({
            "name": "dailyMover",
            "score": 10 if daily_mover_fired else 0,
            "reason": daily_mover_reason or "not a configured 24h mover",
            "fired": daily_mover_fired,
        })

        # At least one trigger must fire.
        fired_count = sum(1 for h in hits if h.get("fired"))
        if fired_count < 1:
            return (True, None)

        score = trigger_mod.composite_score(hits, _score_weights)
        # A confirmed momentum burst is always surfaced — a large, fast move is
        # exactly the signal the composite gate must never filter out.
        burst_fired = any(h["name"] == "momentumBurst" and h["fired"] for h in hits)
        # Whale-accumulation bypass (gated by whale_scan_bypass, default OFF).
        # oi_funding_anomaly / oi_surge_accumulation fire on FLAT price (smart
        # money loading vs crowded shorts), which by definition scores low on the
        # momentum/breakout triggers — so without this the coin is dropped here
        # and the executor's whale override (whale_force_execute / regime bypass)
        # never sees it. When enabled, surface the coin so the downstream whale
        # gates can decide; they still apply min_ai_confidence + all risk gates.
        whale_bypass = whale_scan_bypass and bool((whale_signals or {}).get(market["coin"]))
        # Directional-trend bypass: a sustained intraday up/down trend surfaces the
        # coin for research even below the composite gate (the gate is calibrated
        # for bullish multi-trigger setups; a lone trend signal can't clear it).
        # This is what unblocks shorting downtrends. Gated by trend_surface_enabled
        # (default ON) so it's reversible without a code change.
        trend_fired = trend_surface_enabled and any(
            h["name"] in ("uptrendMomentum", "downtrendMomentum") and h["fired"] for h in hits)
        # Regime gate: the momentum triggers measure only the window's NET % move
        # (no monotonicity/ADX quality), so in a choppy/sideways market a round-trip
        # that nets +/-3% would surface noise. Suppress trend surfacing when the
        # coin's own 1h regime is "chop" (ADX<20 AND EMA20/30 show no direction).
        # up/down/neutral still surface — neutral implies ADX>=20, i.e. it is not a
        # sideways whipsaw. Reuses the already-fetched candles_1h (zero extra
        # requests); any failure falls back to surfacing so a regime-compute hiccup
        # never silences a real signal.
        trend_chop = False
        if trend_fired and candles_1h:
            try:
                from hermes_trader.agents.market_regime import classify_candles
                trend_chop = classify_candles(candles_1h) == "chop"
            except Exception as _e:
                # R12-D1: regime-classifier hiccup must not silence a real
                # trend signal. Surface as DEBUG so the fallback is visible
                # without spamming the operator on every choppy market.
                logger.debug(f"[regime] classify_candles failed for {market['coin']}: {_e}")
        if trend_fired and trend_chop:
            logger.debug(f"[regime] {market['coin']} trend-momentum surfacing suppressed (chop)")
        trend_bypass = trend_fired and not trend_chop
        # Candlestick reversal bypass: a fired shooting-star/hammer/engulfing surfaces
        # the coin for AI research even below the composite gate (the gate is tuned for
        # momentum, not reversals). Gated by candlestick_patterns.enabled.
        pattern_bypass = bool(_cp.get("enabled")) and any(
            h["name"] in ("bearishReversalCandle", "bullishReversalCandle") and h["fired"] for h in hits)
        daily_mover_bypass = any(h["name"] == "dailyMover" and h["fired"] for h in hits)
        if (score < min_score and not burst_fired and not whale_bypass
                and not trend_bypass and not pattern_bypass and not daily_mover_bypass):
            # Near-miss observability: always persist coins that scored at 70%+
            # of the gate so surge postmortems and daily reports can reconstruct
            # the score trajectory of coins that almost made it. Independent of
            # the momentum_continuation.log_near_miss switch (which only controls
            # the verbose logger.info line below).
            if score >= min_score * 0.7:
                try:
                    from hermes_trader.session_log import append as _log_event
                    _log_event({
                        "ts": int(time.time() * 1000),
                        "event": "near_miss",
                        "coin": market["coin"],
                        "score": round(float(score), 2),
                        "gate": float(min_score),
                        "fired_triggers": [h["name"] for h in hits if h.get("fired")],
                        "hits": [
                            {"name": h.get("name"), "score": h.get("score"),
                             "fired": bool(h.get("fired")),
                             "reason": str(h.get("reason", ""))[:120]}
                            for h in hits
                        ],
                    })
                except Exception as _e:
                    # R12-D1: near_miss observability row — losing it silently
                    # would hide the score trajectory of coins that almost made
                    # it. Surface as WARNING (rare path, this won't spam).
                    logger.warning(
                        f"[near-miss] session_log.append failed for {market['coin']}: {_e}"
                    )
            # Verbose near-miss log line (gated by the momentum switch).
            # R12-D1: the previous `try/except Exception: pass` was a defensive
            # shell around a logger.info call that cannot throw; removed to
            # avoid the silent-except anti-pattern.
            if _mc.get("enabled") and _mc.get("log_near_miss") and score >= min_score * 0.5:
                logger.info(
                    f"[near-miss] {market['coin']} composite {score:.1f} "
                    f"(gate {min_score}) fired={[h['name'] for h in hits if h.get('fired')]}"
                )
            return (True, None)

        whale = (whale_signals or {}).get(market["coin"])
        # O-2: expose the CLOSE TIME (ms) of the bar the triggers were scored
        # on (= its open time + bar duration). `candles` here is the closed
        # set the trigger eval used (the forming bar was dropped above), so
        # this is the stable bar identity downstream content dedup keys on —
        # unlike fired_at (scan wall-clock, different every cycle) or id
        # (random per scan). No extra network fetch: the data is in hand.
        scored_bar_close_ms = int(candles[-1].t) + int(bar_dur_ms)
        return (True, {
            "id": f"{market['coin']}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
            "coin": market["coin"],
            "type": market["type"],
            "fired_at": int(time.time() * 1000),
            "bar_close_ms": scored_bar_close_ms,
            "mid": mid,
            "triggers": hits,
            "composite_score": score,
            "whale_signal": whale,  # None unless coin is in oi_funding_anomaly hits
        })
    except Exception as e:
        # R12-D1: per-market eval failure used to vanish behind a False return.
        # Caller (scan_once) drops the result, so the operator needs the cause
        # in the logs to reconstruct which coins were silently skipped. DEBUG
        # level — scan_once already counts and logs the first 5 at WARNING+,
        # and per-coin eval is high-volume.
        logger.debug(f"[scan] per-market eval failed for {market.get('coin','?')}: {e}")
        return (False, str(e))


# ── Main scan entry point ───────────────────────────────────────────────────

# Rotating universe-sweep cursor — persists across scan_once calls in the running
# process so successive cycles walk the FULL universe (see HERMES_UNIVERSE_SWEEP).
_sweep_offset = 0


# R13-B9: scan budget / pacing knobs. These twelve literals were previously
# read inline via os.environ.get(HERMES_*, <literal>) inside scan_once and had
# no canonical registration (invisible to dashboard dump / validate_config).
# Each leaf maps to the legacy HERMES_* env var that operators / the MCP server
# (scripts/hermes-mcp-server.py writes HERMES_MAX_MARKETS) / existing tests set;
# that legacy channel stays the TOP-priority override for backward compat, then
# cfg_get reads the canonical scan_budget block (HERMES_CFG_SCAN_BUDGET__* env +
# agent-config). Defaults mirror the old literals verbatim — zero behaviour
# change. spec: (legacy env or None, kind "i"/"f", min value); a value failing
# coercion or the guard falls back to the literal. Zero is a legal
# "reserved/disabled" value for budget slots / sweep / sleep, hence 0 for those.
_SCAN_BUDGET_DEFAULTS: dict[str, Any] = {
    "cache_max": 512,
    "max_markets": 60,
    "max_markets_hip3": 25,
    "max_markets_movers": 10,
    "movers_vol_floor_usd": 300_000.0,
    "hip3_movers_floor_usd": 50_000.0,
    "universe_sweep": 0,
    "batch_size": 20,
    "batch_sleep_sec": 0.3,
    "parallel_workers": 32,
    "movers_min_pct": 1.0,
    "future_timeout_sec": 60,
}
_SCAN_BUDGET_SPEC: dict[str, tuple[Optional[str], str, float]] = {
    "cache_max": ("HERMES_PERCEPTION_CACHE_MAX", "i", 1),
    "max_markets": ("HERMES_MAX_MARKETS", "i", 0),
    "max_markets_hip3": ("HERMES_MAX_MARKETS_HIP3", "i", 0),
    "max_markets_movers": ("HERMES_MAX_MARKETS_MOVERS", "i", 0),
    "movers_vol_floor_usd": ("HERMES_MOVERS_VOL_FLOOR_USD", "f", 0),
    "hip3_movers_floor_usd": ("HERMES_HIP3_MOVERS_FLOOR_USD", "f", 0),
    "universe_sweep": ("HERMES_UNIVERSE_SWEEP", "i", 0),
    "batch_size": ("HERMES_BATCH_SIZE", "i", 1),
    "batch_sleep_sec": ("HERMES_BATCH_SLEEP", "f", 0),
    "parallel_workers": (None, "i", 1),
    "movers_min_pct": (None, "f", 0.0001),
    "future_timeout_sec": (None, "i", 1),
}


def scan_budget_params(*, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve the twelve scan budget/pacing knobs for one scan_once call.

    Returns an independent dict of snake_case leaves. Resolution per leaf:
    legacy HERMES_* env var (highest priority, operator / MCP compat) →
    cfg_get("scan_budget.<leaf>") which covers HERMES_CFG_SCAN_BUDGET__* env,
    the agent-config dict and CANONICAL_DEFAULTS → the inline literal. Any
    coercion failure or out-of-range value falls back to the literal so the
    scan hot path never raises.
    """
    p = dict(_SCAN_BUDGET_DEFAULTS)
    try:
        for leaf, (legacy_env, kind, min_v) in _SCAN_BUDGET_SPEC.items():
            raw = None
            if legacy_env is not None:
                raw = os.environ.get(legacy_env)
            if raw is None or raw == "":
                raw = cfg_get(f"scan_budget.{leaf}", config=config)
            if raw is None:
                continue
            if kind == "i":
                v: Any = int(raw)
            else:
                v = float(raw)
            if v >= min_v:
                p[leaf] = v
    except Exception as e:
        # Bad env / config must not kill the scan — return the literal budget.
        logger.debug(f"[scan] budget params read failed, using literals: {e}")
        return dict(_SCAN_BUDGET_DEFAULTS)
    return p


def scan_once(
    universe: Optional[list[dict[str, Any]]] = None,
    min_score: float = 20,
    config: Optional[dict[str, Any]] = None,
    parallel_workers: Optional[int] = None,
    coin: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Scan Hyperliquid markets for trigger signals.

    Returns perception dicts sorted by composite score descending. Markets are
    scanned in parallel; the only shared state is the candle cache.

    Args:
        universe: pre-fetched market list. Defaults to get_universe().
        min_score: minimum composite score to include a result.
        config: config dict. Defaults to get_config().
        parallel_workers: max concurrent market scans. Defaults to 32.
        coin: when set, scan ONLY this coin (case-insensitive), bypassing the
            volume/budget/sweep selection. Used by the operator console's
            single-coin scan so a typed symbol is always evaluated even if it
            ranks outside the top-N budget.
    """
    started = time.time()
    _reset_data_gaps()
    cfg = config or get_config()
    min_score = cfg["scan"]["minCompositeScore"] if min_score == 20 else min_score
    # Defensive default only; the R13-B9 canonical scan_budget.parallel_workers
    # resolves the real value (32) below once the agent-config read completes.
    workers = parallel_workers or 32

    # Asset-class toggles read fresh per scan so operator flips take effect
    # without restart. `enable_hip3` adds per-dex POSTs (cost) so it's opt-in.
    try:
        from hermes_trader.agents.config_store import read_agent_config
        _cfg = read_agent_config()
        include_crypto = bool(_cfg.get("enable_crypto", True))
        include_hip3 = bool(_cfg.get("enable_hip3", False))
        whale_scan_bypass = bool(_cfg.get("whale_scan_bypass", False))
        trend_surface_enabled = bool(_cfg.get("trend_surface_enabled", False))
    except Exception as e:
        # R12-D1: agent-config read failure used to silently fall back to
        # crypto-only / no-bypass — a real degraded-config state that the
        # operator must see. WARNING once per scan; this gates whale and
        # trend surfacing for the whole cycle.
        logger.warning(f"[scan] read_agent_config failed, falling back to defaults: {e}")
        _cfg = {}
        include_crypto = True
        include_hip3 = False
        whale_scan_bypass = False
        trend_surface_enabled = False

    # `get_config()` owns static trigger weights/thresholds; `.agent-config.json`
    # owns hot strategy toggles. Merge root-level live keys so optional scan
    # features such as momentum_continuation / candlestick_patterns actually
    # follow the live config instead of being dead knobs.
    scan_cfg = {**cfg, **_cfg}

    # R13-B9: resolve the twelve scan budget / pacing knobs from the canonical
    # scan_budget block (legacy HERMES_* env still wins for backward compat —
    # the MCP server drives HERMES_MAX_MARKETS and operator/test knobs remain
    # live). Helper falls back to the inline literals on any failure.
    budget = scan_budget_params(config=_cfg)
    # Thread-pool width: explicit call arg > canonical scan_budget (default 32).
    workers = parallel_workers or int(budget["parallel_workers"])
    # The candle cache is created at module import with the legacy env value;
    # live-sync its cap so canonical/agent-config overrides take effect without
    # rebuilding the cache (reads _max_size on every set).
    _candle_cache._max_size = int(budget["cache_max"])

    # R13-B8: resolve weights/thresholds from the canonical trigger_weights /
    # trigger_thresholds blocks (env + agent-config + CANONICAL_DEFAULTS) and
    # override the TRIGGER_CONFIG copies so per-leaf env overrides take effect
    # on the scan hot path. Helpers fall back to the TRIGGER_CONFIG literals
    # on any failure and return the camelCase runtime keys _scan_single_market
    # / composite_score expect — runtime values are unchanged by default.
    scan_cfg["weights"] = trigger_weights_params(config=_cfg)
    scan_cfg["thresholds"] = trigger_thresholds_params(config=_cfg)

    if not include_crypto and not include_hip3:
        logger.warning("[scan] both enable_crypto and enable_hip3 are False — nothing to scan")
        return []

    # ── Step 1: Fetch mids (HTTP POST, ~150ms; +~8 per-dex POSTs if HIP-3 on) ─
    raw_mids = fetch_all_mids(include_hip3=include_hip3)
    mids: dict[str, float] = {}
    # 注意：循环变量不能用 `coin`，否则会遮蔽函数参数 `coin`（单币扫描目标），
    # Python 循环变量在循环结束后仍保留，导致下方 `if coin:` 恒为真。
    for sym, val in raw_mids.items():
        if isinstance(val, str):
            try:
                mids[sym] = float(val)
            except ValueError as _e:
                # R12-D1: previously a silent `pass`. A non-numeric mid
                # string is a data-feed anomaly — log at DEBUG (high
                # volume per scan) and skip the entry.
                logger.debug(f"[scan] mid {sym!r} not float-coercible: {_e}")
        elif isinstance(val, (int, float)):
            mids[sym] = val

    # ── Step 2: Get universe & pre-filter by volume ─────────────────────
    # HL rate limit: 1200 weight/minute. Candle fetch = 20 weight each.
    # Fetching all 500+ markets would need 10,000+ weight → instant 429.
    # Pre-filter to top-N markets by 24h notional volume to stay under limit.
    if universe is None:
        universe = get_universe(include_hip3=include_hip3)

    # Filter: must have valid mid, exclude spot (@ or type=spot), then apply
    # asset-class gates + budget split.
    # Eligibility falls back to the cached midPx/markPx when the live WS mid is
    # missing — the live feed only covers a subset of HIP-3 coins, and requiring
    # it silently shrank the HIP-3 scan pool to ~3 names against a 25-slot
    # budget (xyz:QNT +9.0% / xyz:NBIS +8.2% / xyz:PURRDAT +10.3% / xyz:ARM
    # +9.3% all absent from perceptions on 2026-06-12 while only CBRS/SKHX/SMSN
    # scanned). _abs_pct_24h already uses this exact fallback for ranking.
    eligible = [m for m in universe
                if (mids.get(m["coin"], 0)
                    or float(m.get("midPx") or m.get("markPx") or 0)) > 0
                and not m["coin"].startswith("@")
                and m.get("type") != "spot"]
    if not include_crypto:
        eligible = [m for m in eligible if m.get("dex")]
    if not include_hip3:
        eligible = [m for m in eligible if not m.get("dex")]
    # HIP-3 dex mute: focus scanning on specific HIP-3 venues without disabling
    # HIP-3 entirely. `hip3_dex_allowlist` (e.g. ["xyz"]) = scan ONLY those dexes;
    # `hip3_dex_blocklist` = scan all but those. Crypto/main-dex markets (no
    # `dex`) are never affected. Stops wasted research on unfunded/uninteresting
    # dexes (km, hyna, cash, ...). Both read fresh each scan (hot-reload).
    if include_hip3:
        allow = {d for d in (_cfg.get("hip3_dex_allowlist") or []) if d}
        block = {d for d in (_cfg.get("hip3_dex_blocklist") or []) if d}
        if allow:
            eligible = [m for m in eligible if not m.get("dex") or m.get("dex") in allow]
        if block:
            eligible = [m for m in eligible if not m.get("dex") or m.get("dex") not in block]
    # Single-coin scan from the operator console: bypass all volume/budget/
    # sweep selection and evaluate exactly the requested symbol. Match is
    # case-insensitive on the base coin (ignoring any HIP-3 "dex:" prefix only
    # when the user typed a bare symbol).
    markets: list[dict[str, Any]] = []
    if coin:
        target = str(coin).strip().upper()
        def _coin_match(name: str) -> bool:
            base = name.split(":", 1)[1] if ":" in name else name
            return base.upper() == target or name.upper() == target
        single = [m for m in eligible if _coin_match(m.get("coin", ""))]
        if not single:
            logger.warning(f"[scan] coin={target!r} not found in eligible universe "
                           f"({len(eligible)} markets)")
            return []
        # Ensure a live (or cached) mid exists before scanning.
        markets = [m for m in single
                   if (mids.get(m["coin"], 0)
                       or float(m.get("midPx") or m.get("markPx") or 0)) > 0]
        logger.info(f"[scan] single-coin mode: {markets[0]['coin'] if markets else target}")
    if not coin:
        # Bucketed budget so HIP-3 markets and low-volume big-movers each get
        # candle fetches instead of being crowded out by crypto majors. Crypto
        # gets `max_markets - max_markets_hip3` slots, further split between
        # top-by-volume and top-by-|24h%| (movers); HIP-3 gets a flat
        # top-by-volume slice. Single-class runs hand the entire budget to
        # that class. Total candle fetches stay at `max_markets` to keep
        # the scanner inside HL's 1200 weight/minute rate budget.
        # R13-B9: resolved once above via scan_budget_params() (legacy HERMES_*
        # env still wins, then canonical scan_budget.*, then these literals).
        max_markets = int(budget["max_markets"])
        max_markets_hip3 = int(budget["max_markets_hip3"])
        max_markets_movers = int(budget["max_markets_movers"])
        movers_vol_floor = float(budget["movers_vol_floor_usd"])
        # Half the HIP-3 budget goes to top-by-volume (clean liquid markets),
        # half to top-by-|24h%| above a tiny floor (catches xyz:DKNG-style
        # low-volume HIP-3 pumpers that would never make a vol cut). The HIP-3
        # universe is bounded so this doesn't expose us to crypto-microcap noise.
        hip3_movers_floor = float(budget["hip3_movers_floor_usd"])
        crypto_sweep_floor = float(_cfg.get("min_market_volume_usd", movers_vol_floor) or movers_vol_floor)
        hip3_sweep_floor = float(_cfg.get("min_hip3_volume_usd", hip3_movers_floor) or hip3_movers_floor)

        def _abs_pct_24h(m: dict[str, Any]) -> float:
            prev = float(m.get("prevDayPx") or 0)
            # Current price MUST come from this cycle's fresh mids — the universe
            # dict's midPx is from the (up-to-24h-cached) metaAndAssetCtxs snapshot
            # and freezes at loop-start, so ranking off it selects YESTERDAY's
            # movers and misses a coin ripping right now. Fall back to the cached
            # mid/mark only if the live mid is missing.
            cur = float(mids.get(m["coin"]) or m.get("midPx") or m.get("markPx") or 0)
            if prev <= 0 or cur <= 0:
                return 0.0
            return abs((cur - prev) / prev * 100)

        def _pick_with_movers(
            pool: list[dict[str, Any]],
            vol_budget: int,
            movers_budget: int,
            mv_floor: float,
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            """Top-N by 24h volume + top-M by |24h%|, deduped, in that priority.

            Movers slot guarantees a budget for sub-top-volume big movers
            regardless of their volume rank; the floor filters out pico-cap
            noise where a $200 trade can print a 50% move.
            """
            by_vol = sorted(pool, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)
            vol_pick = by_vol[:vol_budget]
            chosen = {m["coin"] for m in vol_pick}
            candidates = [m for m in pool
                          if m["coin"] not in chosen
                          and m.get("dayNtlVlm", 0) >= mv_floor]
            by_pct = sorted(candidates, key=_abs_pct_24h, reverse=True)
            movers_pick = [m for m in by_pct if _abs_pct_24h(m) >= budget["movers_min_pct"]][:movers_budget]
            return vol_pick, movers_pick

        if include_crypto and include_hip3:
            crypto_budget = max(0, max_markets - max_markets_hip3)
            crypto_vol_budget = max(0, crypto_budget - max_markets_movers)
            crypto = [m for m in eligible if not m.get("dex")]
            hip3 = [m for m in eligible if m.get("dex")]
            crypto_top, crypto_movers = _pick_with_movers(crypto, crypto_vol_budget,
                                                         max_markets_movers, movers_vol_floor)
            # Split HIP-3: half by volume, half by |24h%| above the tiny floor.
            hip3_vol_budget = max_markets_hip3 // 2
            hip3_mover_budget = max_markets_hip3 - hip3_vol_budget
            hip3_top, hip3_movers = _pick_with_movers(hip3, hip3_vol_budget,
                                                      hip3_mover_budget, hip3_movers_floor)
            markets = crypto_top + crypto_movers + hip3_top + hip3_movers
            logger.info(
                f"[scan] budget split: {len(crypto_top)} crypto-vol + {len(crypto_movers)} crypto-movers "
                f"+ {len(hip3_top)} HIP-3-vol + {len(hip3_movers)} HIP-3-movers "
                f"(of {len(crypto)} crypto + {len(hip3)} HIP-3 eligible)"
            )
            if crypto_movers:
                sample = ", ".join(f"{m['coin']} {_abs_pct_24h(m):+.1f}%" for m in crypto_movers[:5])
                logger.info(f"[scan] crypto-movers: {sample}")
            if hip3_movers:
                sample = ", ".join(f"{m['coin']} {_abs_pct_24h(m):+.1f}%" for m in hip3_movers[:5])
                logger.info(f"[scan] HIP-3-movers: {sample}")
        else:
            pool = eligible
            vol_budget = max(0, max_markets - max_markets_movers)
            # Use the appropriate floor for the single-class mode.
            floor = hip3_movers_floor if include_hip3 else movers_vol_floor
            chosen, movers = _pick_with_movers(pool, vol_budget, max_markets_movers, floor)
            markets = chosen + movers
            cls = "crypto-only" if include_crypto else "HIP-3-only"
            logger.info(
                f"[scan] {cls} mode: {len(chosen)} by-volume + {len(movers)} by-momentum "
                f"(of {len(eligible)} eligible)"
            )
        # ── Rotating universe sweep ─────────────────────────────────────────
        # Cover the FULL universe over successive cycles, not just top-vol+movers.
        # Each scan adds the next `sweep_n` eligible markets, advancing a persistent
        # offset that wraps around — so every market is seen within
        # ceil(len(eligible)/sweep_n) cycles, while top-vol+movers are ALWAYS scanned
        # (never miss a live ripper). Pacing (batch_sleep_sec) keeps us under HL's
        # ~1200 weight/min budget. universe_sweep=0 disables (default).
        # R13-B9: resolved via scan_budget_params() above.
        sweep_n = int(budget["universe_sweep"])
        if sweep_n > 0 and eligible:
            global _sweep_offset
            sweep_pool = [
                m for m in eligible
                if float(m.get("dayNtlVlm") or 0) >= (
                    hip3_sweep_floor if m.get("dex") else crypto_sweep_floor
                )
            ]
            ordered = sorted(sweep_pool, key=lambda m: m.get("coin", ""))
            if not ordered:
                logger.info("[scan] universe sweep: no markets above liquidity floor")
                ordered = []
        if sweep_n > 0 and eligible and ordered:
            n = len(ordered)
            off = _sweep_offset % n
            window = ordered[off:off + sweep_n]
            if len(window) < sweep_n:                      # wrap-around
                window += ordered[: sweep_n - len(window)]
            have = {m["coin"] for m in markets}
            added = [m for m in window if m.get("coin") not in have]
            markets = markets + added
            _sweep_offset = (off + sweep_n) % n
            logger.info(f"[scan] universe sweep: +{len(added)} new (offset {off}/{n}, "
                        f"full coverage ~{(n + sweep_n - 1) // sweep_n} cycles)")

    if not markets:
        return []

    # ── Step 3: Parallel scan with rate-limiting ───────────────────────
    # Batch markets into groups of `batch_size` and sleep between batches
    # to stay under the HL rate limit. Within each batch, fan out with
    # `workers` threads.
    # R13-B9: batch pacing resolved via scan_budget_params() above.
    batch_size = int(budget["batch_size"])
    batch_sleep = float(budget["batch_sleep_sec"])

    # Build per-market scan callables
    callables = []
    for m in markets:
        mid = mids.get(m["coin"], 0)
        if mid <= 0:
            continue
        callables.append((m, mid))

    total = len(callables)
    logger.info(f"[scan] scanning {total} markets in batches of {batch_size} ({workers} workers/batch)...")

    whale_enabled = any([
        whale_scan_bypass,
        bool(_cfg.get("whale_force_execute", False)),
        bool(_cfg.get("whale_regime_bypass", False)),
        float(_cfg.get("whale_size_multiplier", 1.0) or 1.0) != 1.0,
    ])
    # Fetch the whale-accumulation map only when a downstream whale feature can
    # actually use it. Keeping disabled signals out of the research prompt avoids
    # stale "shadow" context nudging verdicts while force/regime/size paths are off.
    if whale_enabled:
        try:
            from hermes_trader.agents.whale_index import whale_accumulation_map
            whale_signals = whale_accumulation_map()
            if whale_signals:
                logger.info(
                    f"[scan] whale accumulation: {len(whale_signals)} coins flagged "
                    f"({', '.join(list(whale_signals.keys())[:5])})"
                )
        except Exception as e:
            logger.warning(f"[scan] whale_accumulation_map failed: {e}")
            whale_signals = {}
    else:
        whale_signals = {}

    results: list[dict[str, Any]] = []
    errors = 0
    completed = 0

    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch = callables[batch_start:batch_end]

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hermes-scan") as pool:
            futures = [pool.submit(_scan_single_market, m, md, scan_cfg, min_score, whale_signals, whale_scan_bypass, trend_surface_enabled) for m, md in batch]
            for i, future in enumerate(futures):
                idx = batch_start + i
                try:
                    success, result = future.result(timeout=int(budget["future_timeout_sec"]))
                    if success and isinstance(result, dict):
                        results.append(result)
                    elif not success:
                        errors += 1
                        if errors <= 5:
                            logger.warning(f"[scan] market scan #{idx} failed: {result}")
                except Exception as e:
                    errors += 1
                    if errors <= 5:
                        logger.error(f"[scan] market scan #{idx} exception: {e}")

        completed += len(batch)
        if completed % 100 == 0 or completed == total:
            logger.info(f"[scan] progress: {completed}/{total} ({completed/total*100:.0f}%), {len(results)} triggers so far")

        if batch_end < total:
            time.sleep(batch_sleep)

    # ── Step 4: Sort by composite score descending ──────────────────────
    elapsed = (time.time() - started) * 1000
    data_gaps = _get_data_gaps()
    logger.info(f"[scan] scanned {len(markets)} markets, {len(results)} triggers in {elapsed:.0f}ms ({errors} errors, {data_gaps} data-gaps)")
    if data_gaps > 0 and len(markets) > 0 and data_gaps / len(markets) > 0.25:
        # >25% of the universe unreadable this scan = a degraded data feed, not a
        # quiet market. Surface loudly so a silent miss-the-move window is visible.
        logger.warning(
            f"[scan] FEED-FRESHNESS: {data_gaps}/{len(markets)} markets had empty/short "
            f"candles ({data_gaps/len(markets)*100:.0f}%) — possible degraded candle feed; "
            f"signals may be silently missed this scan")
    return sorted(results, key=lambda r: r["composite_score"], reverse=True)
