"""Deep-analysis pipeline: perception -> multi-timeframe indicators -> AI verdict -> persist."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import asyncio
import threading

import httpx

from hermes_trader.agents.config_store import read_agent_config, cfg_get
from hermes_trader.agents.market_regime import _obv_slope_sign
from hermes_trader.agents.research_schema import (
    ResearchVerdict,
    parse_structured,
    structured_to_analysis_fields,
)
from hermes_trader.agents.memory import memory
from hermes_trader.agents.perception import extract_fired_triggers
from hermes_trader.agents.system_prompt import build_system_prompt
from hermes_trader.client.hl_client import (
    fetch_account_state,
    fetch_funding_history,
    fetch_hl_candles,
    resolve_user_address,
)
from hermes_trader.indicators.math import adx, atr, candle_val, ema, rsi
from hermes_trader.models.types import Candle
from hermes_trader.shared_config import load_shared_config

# ── P1-1: one shared thread pool for all research fan-outs ──────────────
# Previously the 6-worker data-fetch pool created a nested 3-worker pool
# inside _signals_block (6 coins × 4 tasks = up to 24 concurrent HTTP calls),
# and every debate/synth block spun up its own short-lived pool — thread
# churn + burst concurrency that tripped OpenRouter/Binance/CBOE rate limits.
# All sites now submit here; the pool bounds total research concurrency.
_POOL: Optional[ThreadPoolExecutor] = None
_POOL_WORKERS = int(os.environ.get("HERMES_RESEARCH_POOL_WORKERS", "16"))


def _get_pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        # R13-B10: pool width resolves through research_fetch_params (legacy
        # HERMES_RESEARCH_POOL_WORKERS env → canonical research_fetch block →
        # the _POOL_WORKERS import-time literal). Read once at construction.
        workers = int(research_fetch_params()["pool_workers"])
        _POOL = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="research",
        )
    return _POOL


# ── P1-9: one reused HTTP client for all outbound LLM/news calls ────────
# httpx.get/httpx.post build a fresh Client (new TCP+TLS handshake, no
# connection reuse, no HTTP/2) on every call. A module-level Client keeps a
# warm keepalive pool and negotiates HTTP/2. Lazy: httpx is imported at
# module top already, but constructing the client lazily avoids opening
# sockets at import time (tests / offline tooling).
_HTTP: Optional[httpx.Client] = None
_HTTP_LOCK = threading.Lock()


def _http() -> httpx.Client:
    global _HTTP
    if _HTTP is None or _HTTP.is_closed:
        with _HTTP_LOCK:
            if _HTTP is None or _HTTP.is_closed:
                # R13-B10: connection-pool limits resolve through
                # research_fetch_params (canonical research_fetch block → the
                # former 8/16 literals). Read once at client construction.
                fp = research_fetch_params()
                _HTTP = httpx.Client(
                    http2=True,
                    limits=httpx.Limits(
                        max_keepalive_connections=int(fp["max_keepalive_connections"]),
                        max_connections=int(fp["max_connections"]),
                    ),
                )
    return _HTTP


# ── Shared infrastructure (cross-component single source of truth) ──────
_SHARED_DIR = os.path.expanduser("~/.hermes-trading")
if _SHARED_DIR not in sys.path and os.path.isdir(_SHARED_DIR):
    sys.path.insert(0, _SHARED_DIR)
try:
    from timeutil import today_utc_str, utcnow, to_iso_z  # type: ignore
    from signal_bus import get_bus  # type: ignore
    from signal_schema import Signal, Verdict  # type: ignore
    from event_log import new_trace_id, record_risk, record_system  # type: ignore
    _SHARED_OK = True
except Exception:  # pragma: no cover - shared dir optional
    today_utc_str = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d")  # noqa: E731
    utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731
    to_iso_z = lambda v: ""  # noqa: E731
    get_bus = None  # type: ignore
    Signal = None  # type: ignore
    Verdict = None  # type: ignore
    new_trace_id = lambda prefix="trc": f"{prefix}-{uuid.uuid4().hex[:12]}"  # noqa: E731
    record_risk = None  # type: ignore
    record_system = None  # type: ignore
    _SHARED_OK = False

# ── Shared config (跨组件单一配置源) ────────────────────────────────
# Canonical loader lives in hermes_trader.shared_config; kept as a thin
# private alias so the module's existing call sites don't all change.
_load_shared_config = load_shared_config

logger = logging.getLogger(__name__)


# ── R13-B10: canonical research LLM / fetch knobs ───────────────────────
# The LLM-call parameters (gateway model/base URL, temperature, token
# budgets, read/connect timeouts, 429 retry budget with exponential backoff,
# length-continuation turns) and the concurrency/prefetch knobs (shared pool
# width, httpx connection-pool limits, signals-block future timeout, the
# per-source prefetch ceilings) used to live as inline literals or
# os.environ.get(HERMES_*/OPENROUTER_*, <literal>) reads scattered across
# _call_openrouter / _get_pool / _http / _signals_block / _parallel_prefetch.
# They never appeared in CANONICAL_DEFAULTS, so the dashboard dump /
# validate_config_updates could neither observe nor tune them. Both blocks
# are now registered canonically (research_llm / research_fetch); these
# helpers resolve every leaf as: legacy env var (highest priority, operator
# / test compat) → cfg_get("research_llm|research_fetch.<leaf>") which covers
# HERMES_CFG_* env and the agent-config dict → the inline literal. The
# helpers never raise on the hot path — any coercion failure or out-of-range
# value falls back to the literal copy.
_RESEARCH_LLM_DEFAULTS: dict[str, Any] = {
    "model": "deepseek-v4-flash",
    "base_url": "https://openrouter.ai/api/v1",
    "temperature": 0.1,
    "max_tokens": 500,
    "debate_max_tokens": 350,
    "timeout_sec": 60.0,
    "connect_timeout_sec": 5.0,
    "retries": 2,
    "backoff_base_sec": 1.0,
    "backoff_cap_sec": 15.0,
    "continuations": 2,
}
# leaf -> (legacy env var or None, kind "i"/"f"/"s", minimum guard).
_RESEARCH_LLM_SPEC: dict[str, tuple[Optional[str], str, float]] = {
    "model": ("OPENROUTER_MODEL", "s", 0.0),
    "base_url": ("OPENROUTER_BASE_URL", "s", 0.0),
    "temperature": (None, "f", 0.0),
    "max_tokens": (None, "i", 1),
    "debate_max_tokens": (None, "i", 1),
    "timeout_sec": (None, "f", 0.1),
    "connect_timeout_sec": (None, "f", 0.1),
    "retries": (None, "i", 0),
    "backoff_base_sec": (None, "f", 0.0),
    "backoff_cap_sec": (None, "f", 0.0),
    "continuations": (None, "i", 0),
}

_RESEARCH_FETCH_DEFAULTS: dict[str, Any] = {
    "pool_workers": _POOL_WORKERS,
    "max_connections": 16,
    "max_keepalive_connections": 8,
    "signals_timeout_sec": 40.0,
    "fetch_timeout_default_sec": 45.0,
    "fetch_timeout_candles_sec": 15.0,
    "fetch_timeout_funding_sec": 8.0,
    "fetch_timeout_news_sec": 10.0,
    "fetch_timeout_signals_sec": 12.0,
}
_RESEARCH_FETCH_SPEC: dict[str, tuple[Optional[str], str, float]] = {
    "pool_workers": ("HERMES_RESEARCH_POOL_WORKERS", "i", 1),
    "max_connections": (None, "i", 1),
    "max_keepalive_connections": (None, "i", 0),
    "signals_timeout_sec": ("HERMES_RESEARCH_SIGNALS_TIMEOUT_S", "f", 0.1),
    "fetch_timeout_default_sec": ("HERMES_RESEARCH_FETCH_TIMEOUT_S", "f", 0.1),
    "fetch_timeout_candles_sec": ("HERMES_RESEARCH_FETCH_TIMEOUT_CANDLES", "f", 0.1),
    "fetch_timeout_funding_sec": ("HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING", "f", 0.1),
    "fetch_timeout_news_sec": ("HERMES_RESEARCH_FETCH_TIMEOUT_NEWS", "f", 0.1),
    "fetch_timeout_signals_sec": ("HERMES_RESEARCH_FETCH_TIMEOUT_SIGNALS", "f", 0.1),
}


def _research_params(block: str, defaults: dict[str, Any],
                     spec: dict[str, tuple[Optional[str], str, float]],
                     *, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve one canonical research block (shared by llm/fetch).

    Per leaf: a non-empty legacy env var wins (operator/test compat), then
    cfg_get covers HERMES_CFG_<BLOCK>__<LEAF> env and the agent-config dict,
    then the inline literal. Strings pass through; ints/floats are coerced
    and must clear the minimum guard. Any failure returns a fresh literal
    copy so the research hot path never raises.
    """
    p = dict(defaults)
    try:
        for leaf, (legacy_env, kind, min_v) in spec.items():
            raw: Any = None
            if legacy_env is not None:
                raw = os.environ.get(legacy_env)
            if raw is None or raw == "":
                raw = cfg_get(f"{block}.{leaf}", config=config)
            if raw is None:
                continue
            if kind == "s":
                v: Any = str(raw)
                if v == "":
                    continue
            elif kind == "i":
                v = int(raw)
            else:
                v = float(raw)
            if kind == "s" or v >= min_v:
                p[leaf] = v
    except Exception as e:
        logger.debug(f"[research] {block} params read failed, using literals: {e}")
        return dict(defaults)
    return p


def research_llm_params(*, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve the eleven research LLM-call knobs (independent dict copy)."""
    return _research_params("research_llm", _RESEARCH_LLM_DEFAULTS,
                            _RESEARCH_LLM_SPEC, config=config)


def research_fetch_params(*, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve the nine research concurrency/prefetch knobs (independent copy)."""
    return _research_params("research_fetch", _RESEARCH_FETCH_DEFAULTS,
                            _RESEARCH_FETCH_SPEC, config=config)


# P3-2: research-path LLM circuit breaker. Without it, a dead OpenRouter
# upstream means every coin on every scan tick pays a full 60s timeout across
# every research/debate/HTA call — an avalanche that stalls the loop. After N
# consecutive hard failures the breaker opens for M seconds and
# _call_openrouter short-circuits to "" (every caller already degrades
# gracefully on empty). Mirrors the dashboard chat breaker (F15) but reads its
# thresholds from canonical config (llm_circuit_breaker.fail_threshold /
# cooldown_s) with the same env-style fallback the rest of research.py uses.
_LLM_CB_FAILURES = 0
_LLM_CB_OPEN_UNTIL = 0.0
_LLM_CB_LOCK = threading.Lock()
_LLM_CB_FAIL_THRESHOLD_FALLBACK = 3
_LLM_CB_COOLDOWN_S_FALLBACK = 300.0


def _llm_cb_settings() -> tuple[int, float]:
    try:
        threshold = int(cfg_get("llm_circuit_breaker.fail_threshold",
                                _LLM_CB_FAIL_THRESHOLD_FALLBACK))
    except (TypeError, ValueError):
        threshold = _LLM_CB_FAIL_THRESHOLD_FALLBACK
    try:
        cooldown = float(cfg_get("llm_circuit_breaker.cooldown_s",
                                 _LLM_CB_COOLDOWN_S_FALLBACK))
    except (TypeError, ValueError):
        cooldown = _LLM_CB_COOLDOWN_S_FALLBACK
    if threshold <= 0:
        threshold = _LLM_CB_FAIL_THRESHOLD_FALLBACK
    if cooldown <= 0:
        cooldown = _LLM_CB_COOLDOWN_S_FALLBACK
    return threshold, cooldown


def _llm_circuit_open() -> bool:
    return time.time() < _LLM_CB_OPEN_UNTIL


def _llm_record_success() -> None:
    global _LLM_CB_FAILURES
    with _LLM_CB_LOCK:
        _LLM_CB_FAILURES = 0
    try:
        from hermes_trader import metrics

        if not _llm_circuit_open():
            metrics.LLM_CIRCUIT_STATE.set(0.0)
    except Exception:  # noqa: BLE001
        pass


def _llm_record_failure() -> None:
    global _LLM_CB_FAILURES, _LLM_CB_OPEN_UNTIL
    threshold, cooldown = _llm_cb_settings()
    tripped = False
    with _LLM_CB_LOCK:
        _LLM_CB_FAILURES += 1
        if _LLM_CB_FAILURES >= threshold:
            _LLM_CB_OPEN_UNTIL = time.time() + cooldown
            _LLM_CB_FAILURES = 0
            tripped = True
            logger.warning(
                "[research] LLM circuit OPEN for %.0fs after %d consecutive failures",
                cooldown, threshold,
            )
    if tripped:
        # P3-1: count trips and flip the state gauge outside the lock.
        try:
            from hermes_trader import metrics

            metrics.LLM_CIRCUIT_TRIPS.inc()
            metrics.LLM_CIRCUIT_STATE.set(1.0)
        except Exception:  # noqa: BLE001
            pass


def _compute_indicators(candles: list[Candle]) -> dict[str, Any]:
    """Compute EMA8/21, RSI, ATR, ADX for a set of candles."""
    if not candles:
        return {
            "ema8": None, "ema21": None, "slope_up": None,
            "rsi14": None, "atr14": None, "adx14": None,
            "last_close": 0, "last_time": 0,
        }

    closes = [candle_val(c, "c") for c in candles]

    if len(closes) < 21:
        return {
            "ema8": None, "ema21": None, "slope_up": None,
            "rsi14": None, "atr14": None, "adx14": None,
            "last_close": closes[-1],
            "last_time": candles[-1].t,
        }

    ema8_arr = ema(closes, 8)
    ema21_arr = ema(closes, 21)

    last_ema8 = ema8_arr[-1] if ema8_arr else None
    last_ema21 = ema21_arr[-1] if ema21_arr else None

    slope_up = None
    if last_ema8 is not None and len(ema8_arr) >= 3:
        slope_up = last_ema8 > ema8_arr[-3]

    rsi_arr = rsi(candles, 14)
    atr_arr = atr(candles, 14)
    adx_arr = adx(candles, 14)

    return {
        "ema8": last_ema8 if last_ema8 is not None and math.isfinite(last_ema8) else None,
        "ema21": last_ema21 if last_ema21 is not None and math.isfinite(last_ema21) else None,
        "slope_up": slope_up,
        "rsi14": rsi_arr[-1] if rsi_arr and math.isfinite(rsi_arr[-1]) else None,
        "atr14": atr_arr[-1] if atr_arr and math.isfinite(atr_arr[-1]) else None,
        "adx14": adx_arr[-1] if adx_arr and math.isfinite(adx_arr[-1]) else None,
        "last_close": closes[-1],
        "last_time": candles[-1].t,
    }


def _fetch_funding_rate(coin: str) -> str:
    """Latest hourly funding rate for a coin, or 'N/A' if unavailable."""
    # P2-3: lookback window is config-driven (funding_lookback_hours,
    # default 24h); fetch_funding_history walks back from start_time.
    try:
        lookback_h = int(cfg_get("funding_lookback_hours", 24))
        if lookback_h <= 0:
            lookback_h = 24
    except (TypeError, ValueError):
        lookback_h = 24
    start_time = int(time.time() * 1000) - lookback_h * 3_600_000
    history = fetch_funding_history(coin, start_time)
    if history:
        rate = float(history[-1].get("fundingRate", "0"))
        if math.isfinite(rate):
            return f"{rate * 100:.4f}%/hr"
    return "N/A"


# Only surface news from the last N days. Without this, Brave returned
# year-old articles (e.g. AIXBT's 2025 hack) that then tripped the binary-news
# gate on a fresh 2026 trade. The gate reasons about *imminent* event risk, so
# stale headlines are noise — both to the gate and to the LLM prompt.
# R9/P2-3: the live value comes from config (news_freshness_days); this is
# only the fallback used when config is missing/invalid.
_NEWS_FRESHNESS_DAYS_DEFAULT = 2

# Short-TTL news cache: Brave headlines don't change second-to-second, and a
# 2-min cache avoids duplicate API calls when research fires for multiple coins
# in quick succession (e.g. a 3-trigger scan cycle). TTL is config-driven
# (news_cache_ttl_s); this is the fallback default.
_NEWS_CACHE: dict[str, tuple] = {}
_NEWS_CACHE_TTL_S_DEFAULT = 120
_NEWS_CACHE_LOCK = threading.Lock()


def _fetch_news(coin: str) -> str:
    """Recent (last `news_freshness_days` config days) news headlines for a
    coin via the Brave Search API.

    Returns a compact ' | '-joined headline string, or 'no news' when no
    BRAVE_API_KEY is set or the request fails — news is a supplementary
    signal, so a fetch failure degrades gracefully and never blocks research.
    """
    # Cache check
    try:
        cache_ttl_s = float(cfg_get("news_cache_ttl_s",
                                    _NEWS_CACHE_TTL_S_DEFAULT))
        if cache_ttl_s < 0:
            cache_ttl_s = _NEWS_CACHE_TTL_S_DEFAULT
    except (TypeError, ValueError):
        cache_ttl_s = _NEWS_CACHE_TTL_S_DEFAULT
    with _NEWS_CACHE_LOCK:
        cached = _NEWS_CACHE.get(coin)
        if cached is not None:
            _ts, _val = cached
            if time.monotonic() - _ts < cache_ttl_s:
                return _val

    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        return "no news"
    # Brave `freshness` takes a YYYY-MM-DDtoYYYY-MM-DD range; a 2-day window
    # approximates "within 48h" (the closest the API offers to an hour-precise
    # bound without per-result age filtering).
    try:
        freshness_days = int(cfg_get("news_freshness_days",
                                     _NEWS_FRESHNESS_DAYS_DEFAULT))
        if freshness_days < 1:
            freshness_days = _NEWS_FRESHNESS_DAYS_DEFAULT
    except (TypeError, ValueError):
        freshness_days = _NEWS_FRESHNESS_DAYS_DEFAULT
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=freshness_days)
    freshness = f"{start.isoformat()}to{today.isoformat()}"
    try:
        resp = _http().get(
            "https://api.search.brave.com/res/v1/news/search",
            params={"q": f"{coin} crypto", "count": 5, "freshness": freshness},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=10.0,
        )
        if not resp.is_success:
            return "no news"
        results = resp.json().get("results", []) or []
        headlines = [str(r.get("title", "")).strip() for r in results if r.get("title")]
        _news = " | ".join(headlines[:5]) if headlines else "no news"
        # Cache the result (including "no news") so repeated coins in a scan
        # cycle don't each hit the Brave API.
        with _NEWS_CACHE_LOCK:
            _NEWS_CACHE[coin] = (time.monotonic(), _news)
        return _news
    except Exception:
        return "no news"


def _signals_block(coin: str) -> str:
    """Free positioning signals (GEX / aggTrades whale / FINRA short-vol / news
    catalyst) formatted for the AI prompt. Fetches all independent sources in
    parallel so cold-cache latency is max(T) instead of sum(T)."""
    is_hip3 = ":" in (coin or "")
    lines: list[str] = []
    try:
        skip_news = _should_skip_news()

        def _fetch_gex() -> Optional[Any]:
            if not is_hip3:
                return None
            from hermes_trader.agents.options_gex import gex_signal_cached
            return gex_signal_cached(coin)

        def _fetch_short_vol() -> Optional[Any]:
            if not is_hip3:
                return None
            from hermes_trader.agents.short_volume import short_volume_signal
            return short_volume_signal(coin)

        def _fetch_whale() -> Optional[Any]:
            if is_hip3:
                return None
            from hermes_trader.agents.crypto_whale import crypto_whale_signal
            return crypto_whale_signal(coin, window_minutes=15)

        def _fetch_catalyst() -> Optional[Any]:
            if skip_news:
                return None
            from hermes_trader.agents.news_catalyst import catalyst_scan
            base = coin.split(":", 1)[1] if ":" in coin else coin
            return catalyst_scan(base, timespan="1h")

        # All sources are independent (different APIs, separate caches).
        # P1-1: submit to the shared pool (no nested-pool burst). Each source
        # has its own internal timeout; give the futures a bound too so a hung
        # source can't wedge this block forever.
        # R13-B10: legacy HERMES_RESEARCH_SIGNALS_TIMEOUT_S env still wins;
        # canonical research_fetch.signals_timeout_sec resolves underneath.
        _sig_timeout = float(research_fetch_params()["signals_timeout_sec"])
        pool = _get_pool()
        f_gex = pool.submit(_fetch_gex)
        f_sv = pool.submit(_fetch_short_vol)
        f_whale = pool.submit(_fetch_whale)
        f_cat = pool.submit(_fetch_catalyst)
        g = f_gex.result(timeout=_sig_timeout)
        sv = f_sv.result(timeout=_sig_timeout)
        w = f_whale.result(timeout=_sig_timeout)
        n = f_cat.result(timeout=_sig_timeout)

        if g:
            lines.append(
                f"  - Dealer gamma (GEX): {g.regime}; call wall {g.call_wall} (overhead "
                f"resistance / ride target), put wall {g.put_wall} (support), spot {g.spot:g}. "
                + ("Negative gamma = squeeze-prone, lets moves RUN."
                   if g.regime == "trend_short_gamma"
                   else "Long gamma = pins/mean-reverts near the walls.")
            )
        if sv:
            lines.append(
                f"  - Short volume (FINRA): {sv.ratio * 100:.0f}% ({sv.regime}, {sv.trend})."
                + (" Crowded short = SQUEEZE FUEL for a long."
                   if sv.regime == "crowded_short_squeeze_fuel" else "")
            )
        if w and w.whale_n:
            lines.append(
                f"  - Whale order-flow (Binance aggTrades, 15m): {w.bias}, net "
                f"${w.net_usd:+,.0f} across {w.whale_n} large prints."
                + (" Large buyers stepping in (bullish)." if w.bias == "whale_buying"
                   else " Large sellers hitting bids (bearish)." if w.bias == "whale_selling"
                   else "")
            )
        if n and (n.breaking or n.surge_x >= 1.5):
            top = n.headlines[0].title[:90] if n.headlines else ""
            lines.append(
                f"  - News catalyst: {'BREAKING' if n.breaking else f'elevated ({n.surge_x}x coverage)'}"
                f" — {top!r}"
            )
    except Exception as e:
        logger.debug(f"[research] signals block failed for {coin}: {e}")
    if not lines:
        return "Positioning signals (GEX/whale/short-vol/news): none flagged"
    return ("Positioning signals (free data — weigh these in your verdict):\n"
            + "\n".join(lines))


def _build_user_message(
    coin: str,
    perception: dict[str, Any],
    tf1h: dict[str, Any],
    tf4h: dict[str, Any],
    tf1d: dict[str, Any],
    funding_rate: str,
    news: str,
    equity: float,
    open_positions: list[dict[str, Any]],
    mode: str,
    dex_equity: dict[str, float] | None = None,
    recent_candles: list[Candle] | None = None,
    signals_block: str | None = None,
) -> str:
    """Build the user message passed to the LLM."""
    trigger_summary = (
        ", ".join(
            f"{t['name']}: {t['reason']}"
            for t in perception.get("triggers", [])
            if t.get("fired")
        )
        or "no triggers fired"
    )

    # 1h-structure block — accumulation/exhaustion patterns the multi-tf
    # indicator blocks miss. Surfaced as an ENTRY-TIMING signal to be combined
    # WITH the 4h/1d trend, not as a reason to trade against it: in an uptrend a
    # 1h accumulation times a long pullback-entry; in a downtrend a 1h bounce is
    # a short entry (sell the rip), NOT a counter-trend dip-buy.
    _slow_burn_names = {"volumeBuildup1h", "trendFlip1h", "higherLows1h"}
    slow_burn_hits = [
        t for t in perception.get("triggers", [])
        if t.get("name") in _slow_burn_names and t.get("fired")
    ]
    if slow_burn_hits:
        structure_lines = ["1h structure signals (entry-timing — apply IN the 4h/1d trend direction):"]
        for t in slow_burn_hits:
            structure_lines.append(f"  - {t['name']}: {t['reason']}")
        structure_lines.append(
            "Use these to time the entry, not to pick the side. If 4h/1d are bullish, this is a "
            "long pullback-entry; if 4h/1d are bearish, a 1h pop is a SHORT entry (sell the rip) — "
            "do not buy the dip into a downtrend."
        )
        structure_block = "\n".join(structure_lines)
    else:
        structure_block = "1h structure signals: none fired (no accumulation / breakout setup detected)"

    # Whale-accumulation block: oi_funding_anomaly flag (deep-negative funding +
    # flat price + high OI = whales loading while retail shorts). When present
    # this is a strong LONG-bias signal — don't fight it.
    whale = perception.get("whale_signal")
    if whale:
        whale_block = (
            "Whale accumulation flag (oi_funding_anomaly):\n"
            f"  - funding rate: {whale.get('funding_rate', 0):.6f} (deeply negative = retail shorting)\n"
            f"  - 24h price change: {whale.get('price_24h_change_pct', 0):+.2f}% (relatively flat)\n"
            f"  - open interest: ${whale.get('oi', 0):,.0f}\n"
            f"  - confidence: {whale.get('confidence', 0):.2f}\n"
            "Interpretation: smart money is building long positions while retail pays them "
            "to short. When the shorts cover, price tends to squeeze UP. Bias LONG unless "
            "structure is overwhelmingly bearish."
        )
    else:
        whale_block = "Whale accumulation flag: not flagged for this coin"

    def _fmt_px(p: float) -> str:
        """Adaptive precision so sub-cent coins (HMSTR at $0.000173 etc.) don't
        all read as '0.0002' to the LLM. Without this the AI returned identical
        entry/sl/tp on cheap coins because the prompt rounded them to the same
        4-decimal value."""
        if p == 0:
            return "0"
        ap = abs(p)
        if ap >= 1:
            return f"{p:.4f}"
        if ap >= 0.01:
            return f"{p:.5f}"
        if ap >= 0.0001:
            return f"{p:.6f}"
        return f"{p:.8f}"

    def _indicator_block(label: str, snap: dict[str, Any]) -> str:
        parts = []
        if snap.get("ema8") is not None and snap.get("ema21") is not None:
            direction = "bullish" if snap["ema8"] > snap["ema21"] else "bearish"
            parts.append(
                f"EMA8={_fmt_px(snap['ema8'])}, EMA21={_fmt_px(snap['ema21'])}, {direction}"
            )
        if snap.get("slope_up") is not None:
            parts.append(f"EMA8 slope: {'rising' if snap['slope_up'] else 'falling'}")
        if snap.get("rsi14") is not None:
            parts.append(f"RSI(14)={snap['rsi14']:.1f}")
        if snap.get("atr14") is not None:
            parts.append(f"ATR(14)={_fmt_px(snap['atr14'])}")
        if snap.get("adx14") is not None:
            parts.append(f"ADX(14)={snap['adx14']:.1f}")
        parts.append(f"last close={_fmt_px(snap.get('last_close', 0))}")
        return f"{label}: {' | '.join(parts)}"

    # Only the coins/sides we already hold — purely so the LLM doesn't
    # double-trade a coin or can CLOSE one. Deliberately NO dollar sizes:
    # account notional/leverage must not influence the verdict (sizing and
    # every risk cap live in the execution gates, not here).
    position_block = (
        "Open positions (do not re-enter these; CLOSE only if structure flipped): "
        + ", ".join(f"{p['coin']} {p['side']}" for p in open_positions)
        if open_positions
        else "Open positions: none"
    )

    # Raw recent price action so the LLM can read candlestick/chart patterns
    # directly (shooting star, hammer, engulfing, flags) — the indicator blocks
    # above summarize away the candle bodies/wicks that patterns live in.
    def _ohlc_block(candles: list[Candle] | None, n: int = 12) -> str:
        if not candles:
            return ""
        rows = []
        for i, c in enumerate(candles[-n:]):
            idx = -(len(candles[-n:]) - i)  # ... -2, -1 (newest = last closed)
            o, h, l, cl = (candle_val(c, k) for k in ("o", "h", "l", "c"))
            rows.append(f"  [{idx:>3}] O={_fmt_px(o)} H={_fmt_px(h)} L={_fmt_px(l)} C={_fmt_px(cl)}")
        return ("Recent 1h candles (oldest→newest, last row = most recent closed bar):\n"
                + "\n".join(rows))

    ohlc_block = _ohlc_block(recent_candles)

    return "\n".join([
        f"Candidate: {coin} (HL {perception.get('type', 'perp')}-PERP)",
        f"Current mid: ${_fmt_px(perception.get('mid', 0))}",
        f"Perception score: {perception.get('composite_score', 0)}/100",
        f"Fired triggers: {trigger_summary}",
        "",
        "Market context (multi-timeframe):",
        _indicator_block("1h", tf1h),
        _indicator_block("4h", tf4h),
        _indicator_block("1d", tf1d),
        "",
        ohlc_block,
        "" if ohlc_block else "",
        structure_block,
        "",
        whale_block,
        "",
        _signals_block(coin) if signals_block is None else signals_block,
        "",
        f"Funding rate (latest): {funding_rate}",
        f"Recent news: {news}",
        position_block,
        "",
        f"Mode: {mode} — {'your verdict will execute against real funds' if mode == 'LIVE' else 'analysis only, no execution'}",
        "",
        'Respond with 2-3 bullet points of reasoning, then output your decision as VALID JSON on the very last line:',
        '{"verdict":"PASS"|"LONG"|"SHORT"|"CLOSE","confidence":0.0-1.0,"side":"long"|"short"|"null","entryPx":number,"stopPx":number,"tpPx":number,"reasoning":"brief"}',
        # stopPx is not decoration: equal-risk sizing divides by the stop
        # distance and the SL bracket order needs a price. A zero stop silently
        # degrades sizing and leaves the position unprotected, so state the
        # requirement explicitly rather than relying on the schema line above.
        'For LONG/SHORT you MUST give non-zero stopPx and tpPx as absolute prices '
        '(stopPx below entry for LONG, above entry for SHORT; never 0, never a percentage). '
        'For PASS/CLOSE set stopPx and tpPx to 0.',
        "Nothing after the JSON.",
    ])


def _call_ai(system_prompt: str, user_message: str, *, trace_id: str = "") -> str:
    """Call the AI for analysis via the in-process OpenRouter path.

    Uses the native in-process multi-perspective debate (``_debate_research``,
    enabled via ``debate_research.enabled``) when available; otherwise goes
    straight to the OpenAI-compatible gateway.
    """
    t0 = time.time()
    result = _call_openrouter(system_prompt, user_message)
    elapsed_ms = int((time.time() - t0) * 1000)
    if result:
        logger.info(
            f"[research] OpenRouter-OK | elapsed_ms={elapsed_ms} chars={len(result)} "
            f"trace_id={trace_id}"
        )
    else:
        logger.error(
            f"[research] OpenRouter-EMPTY | elapsed_ms={elapsed_ms} trace_id={trace_id}"
        )
    return result


def _llm_metric_outcome(path: str, outcome: str, started: float) -> None:
    """P3-1: record one LLM request outcome + duration. Best-effort only —
    a metrics failure must never affect the LLM hot path."""
    try:
        from hermes_trader import metrics

        metrics.LLM_REQUESTS.labels(path=path, outcome=outcome).inc()
        metrics.LLM_REQUEST_DURATION.labels(path=path, outcome=outcome).observe(
            max(0.0, time.time() - started)
        )
    except Exception:  # noqa: BLE001
        pass


def _call_openrouter(
    system_prompt: str,
    user_message: str,
    *,
    response_format: Optional[dict] = None,
    timeout: Optional[float] = None,
    max_tokens: Optional[int] = None,
    path: str = "call_ai",
) -> str:
    """Call the LLM API via OpenAI-compatible endpoint (synchronous httpx).

    Supports OpenRouter, Volcengine Ark, or any OpenAI-compatible gateway via:
      OPENROUTER_API_KEY  — API key (stays a bare secret env var; NOT canonical)
      OPENROUTER_MODEL    — model name (default: deepseek-v4-flash)
      OPENROUTER_BASE_URL — base URL without /chat/completions
                            (default: https://openrouter.ai/api/v1)

    R13-B10: every other knob (model/base URL fallbacks, temperature, token
    budgets, timeouts, retries/backoff, continuations) resolves through
    research_llm_params() — legacy OPENROUTER_* env wins, then the canonical
    research_llm block, then the inline literals. Caller-passed ``timeout`` /
    ``max_tokens`` (e.g. the debate path's shorter values) still override.

    ``path`` is a bounded P3-1 metrics label identifying the caller
    (call_ai/debate_direct); it never carries a coin or free text.
    """
    _t_started = time.time()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    lp = research_llm_params()
    model = str(lp["model"])
    base_url = str(lp["base_url"])
    if timeout is None:
        timeout = float(lp["timeout_sec"])
    if max_tokens is None:
        max_tokens = int(lp["max_tokens"])
    temperature = float(lp["temperature"])
    connect_timeout = float(lp["connect_timeout_sec"])
    max_429_retries = int(lp["retries"])
    backoff_base_s = float(lp["backoff_base_sec"])
    backoff_cap_s = float(lp["backoff_cap_sec"])
    max_length_continuations = int(lp["continuations"])

    if not openrouter_key:
        logger.warning("[research] OPENROUTER_API_KEY not set — returning empty response")
        _llm_metric_outcome(path, "no_key", _t_started)
        return ""

    # P3-2: breaker open — don't even attempt the call; return "" and let the
    # caller degrade (the whole research path already handles empty).
    if _llm_circuit_open():
        logger.warning("[research] LLM circuit OPEN — short-circuiting call to openrouter")
        _llm_metric_outcome(path, "circuit_open", _t_started)
        return ""

    # P3-1: reflect the breaker state on every real attempt.
    try:
        from hermes_trader import metrics

        metrics.LLM_CIRCUIT_STATE.set(0.0)
    except Exception:  # noqa: BLE001
        pass

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {openrouter_key}"}

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        # Only attached when the caller opts in (debate synthesis). Models that
        # don't support it may 400; the callers catch/return "" and fall back.
        payload["response_format"] = response_format

    # P2-5: rate-limit retry budget. 429s (and the occasional 5xx) are retried
    # with exponential backoff, honouring the provider Retry-After header when
    # it is present; cap the sleep so a hostile header can't wedge the loop.
    # R13-B10: the budget / backoff / continuation values above now resolve
    # through research_llm_params() (canonical research_llm block) instead of
    # local literals; the closure names below are kept for readability.

    def _post(msgs: list[dict[str, str]], max_toks: int) -> httpx.Response:
        body = dict(payload)
        body["messages"] = msgs
        body["max_tokens"] = max_toks
        # Give the read (response body) phase the full timeout; connect/pool
        # phases are fast but LLM inference can take several seconds, so a
        # blanket timeout kills otherwise-successful slow reads.
        return _http().post(
            url,
            json=body,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
        )

    def _retry_after_s(resp: httpx.Response, attempt: int) -> float:
        """Provider Retry-After (seconds), else exponential backoff, capped."""
        try:
            ra = (resp.headers.get("retry-after") or "").strip()
            if ra:
                return max(0.0, min(backoff_cap_s, float(ra)))
        except (TypeError, ValueError):
            pass
        return min(backoff_cap_s, backoff_base_s * (2 ** attempt))

    def _send(msgs: list[dict[str, str]], max_toks: int) -> httpx.Response:
        """POST with 429/5xx backoff. Returns the final response (any status)."""
        resp = None
        for attempt in range(max_429_retries + 1):
            try:
                resp = _post(msgs, max_toks)
            except Exception as e:
                # Network/timeout error: back off and retry like a 429 while
                # the budget lasts, else surface as a failure response.
                if attempt >= max_429_retries:
                    raise
                wait = min(backoff_cap_s, backoff_base_s * (2 ** attempt))
                logger.warning(
                    f"[research] LLM call EXCEPTION ({type(e).__name__}) — "
                    f"retry {attempt + 1}/{max_429_retries} in {wait:.1f}s"
                )
                try:
                    from hermes_trader import metrics

                    metrics.LLM_RETRIES.labels(cause="network").inc()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(wait)
                continue
            if resp.status_code in (429, 502, 503) and attempt < max_429_retries:
                wait = _retry_after_s(resp, attempt)
                logger.warning(
                    f"[research] HTTP {resp.status_code} rate-limit/unavailable — "
                    f"retry {attempt + 1}/{max_429_retries} in {wait:.1f}s"
                )
                try:
                    from hermes_trader import metrics

                    metrics.LLM_RETRIES.labels(cause="rate_limit").inc()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(wait)
                continue
            return resp
        return resp  # pragma: no cover - loop always returns

    messages: list[dict[str, str]] = list(payload["messages"])
    try:
        resp = _send(messages, max_tokens)
        if resp.status_code == 402:
            m = re.search(r"can only afford (\d+)", resp.text or "")
            if m and int(m.group(1)) >= 300:
                budget = max(300, int(m.group(1)) - 50)
                logger.warning(
                    f"[research] 402 with affordability hint — retrying DEGRADED "
                    f"at max_tokens={budget}"
                )
                resp = _send(messages, budget)

        if not resp.is_success:
            body = resp.text[:200] if resp.text else ""
            logger.error(
                f"[research] LLM call FAILED: HTTP {resp.status_code} — {body}"
            )
            _llm_record_failure()  # P3-2
            _llm_metric_outcome(path, "error", _t_started)
            return ""

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            logger.error("[research] LLM returned 200 but no choices — empty response")
            _llm_record_failure()  # P3-2
            _llm_metric_outcome(path, "empty", _t_started)
            return ""
        choice = choices[0]
        content = choice.get("message", {}).get("content", "") or ""
        finish_reason = choice.get("finish_reason") or ""

        # P2-5: truncated output (finish_reason=length). Ask the model to keep
        # going from where it stopped and concatenate the raw chunks, so a JSON
        # object cut mid-stream is reassembled into something parse_structured
        # can recover. Chat turn order: assistant chunk, then "continue".
        if finish_reason == "length" and content.strip():
            for cont_i in range(max_length_continuations):
                logger.warning(
                    f"[research] finish_reason=length — continuation "
                    f"{cont_i + 1}/{max_length_continuations} "
                    f"(partial_chars={len(content)})"
                )
                try:
                    from hermes_trader import metrics

                    metrics.LLM_RETRIES.labels(cause="continuation").inc()
                except Exception:  # noqa: BLE001
                    pass
                cont_messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": (
                        "Your previous response was cut off at the token limit. "
                        "Continue EXACTLY where it stopped, without repeating "
                        "anything, without rewrites or preamble. Output only the "
                        "remaining part."
                    )},
                ]
                cont_resp = _send(cont_messages, max_tokens)
                if not cont_resp.is_success:
                    logger.error(
                        f"[research] continuation FAILED: HTTP "
                        f"{cont_resp.status_code} — returning partial content"
                    )
                    break
                cont_data = cont_resp.json()
                cont_choices = cont_data.get("choices", [])
                if not cont_choices:
                    break
                cont_choice = cont_choices[0]
                chunk = cont_choice.get("message", {}).get("content", "") or ""
                content += chunk
                if (cont_choice.get("finish_reason") or "") != "length":
                    break
        _llm_record_success()  # P3-2: usable content closes any failure streak
        _llm_metric_outcome(path, "ok", _t_started)
        return content
    except Exception as e:
        logger.error(f"[research] LLM call EXCEPTION: {e}")
        _llm_record_failure()  # P3-2
        _llm_metric_outcome(path, "error", _t_started)
        return ""


_NLP_VERDICT_MAP = {
    "bullish": ("LONG", 0.60),
    "bearish": ("SHORT", 0.60),
    "neutral": ("PASS", 0.25),
}

_NLP_CONF_HINTS = [
    ("very high", 0.85), ("very-high", 0.85), ("high confidence", 0.75),
    ("high", 0.75), ("medium-to-high", 0.65), ("med-high", 0.65),
    ("medium", 0.55), ("moderate", 0.55), ("low-to-medium", 0.45),
    ("low-to-med", 0.45), ("low", 0.35), ("very low", 0.20),
]


def _parse_nlp_verdict(text: str) -> Optional[dict[str, Any]]:
    """Extract a structured verdict from natural-language HTA prose.

    HTA /research/short returns sentences like:
      "Recommendation: bullish, medium confidence."
      "neutral with low confidence"
      "bearish, high conviction"
    Returns {"verdict", "confidence"} or None if nothing can be inferred.
    """
    if not text:
        return None
    low = text.lower()

    # 1. Detect direction — explicit "recommendation:" wins, else keyword scan.
    direction = None
    rec_match = re.search(r"recommendation\s*:?\s*(bullish|bearish|neutral)", low)
    if rec_match:
        direction = rec_match.group(1)
    else:
        for word in ("bullish", "bearish", "neutral"):
            if word in low:
                direction = word
                break
    if not direction:
        return None

    base_verdict, base_conf = _NLP_VERDICT_MAP[direction]

    # 2. Refine confidence from qualitative hints.
    conf = base_conf
    for hint, val in _NLP_CONF_HINTS:
        if hint in low:
            conf = val
            break

    # Strong conviction words boost slightly.
    if re.search(r"strong(?:ly)?\s+(?:bullish|bearish|buy|sell)", low):
        conf = max(conf, 0.70)
    if "conviction" in low and ("high" in low or "strong" in low):
        conf = max(conf, 0.70)

    return {"verdict": base_verdict, "confidence": round(conf, 2)}


def _coerce_px(value: Any) -> float:
    """Best-effort float for a price field; 0.0 for junk/None/negative."""
    try:
        px = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(px) or px <= 0:
        return 0.0
    return px


def _atr_bracket(entry_ref: float, atr_ref: float, is_long: bool,
                 sl_mult: float, tp_mult: float) -> tuple[float, float]:
    """Derive (stop_px, tp_px) from entry ± ATR multiples, clamped at 0.

    Single source of truth for the ATR bracket math shared by the legacy
    `parse_verdict` repair path (research.py) and the structured-verdict
    mapping (research_schema.py) — the two used identical formulas.
    """
    stop_px = (entry_ref - atr_ref * sl_mult) if is_long else (entry_ref + atr_ref * sl_mult)
    tp_px = (entry_ref + atr_ref * tp_mult) if is_long else (entry_ref - atr_ref * tp_mult)
    return max(0.0, stop_px), max(0.0, tp_px)


def _extract_verdict_json(ai_text: str, perception: dict[str, Any]
                          ) -> tuple[str, Any, Optional[str], Any, float, float,
                                     str, Any, bool, list[str]]:
    """Decode the structured JSON verdict block from an AI response.

    Looks for JSON on the last line first, then a regex fallback. Returns
    (verdict, confidence, side, entry_px, stop_px, tp_px, news_risk,
    reasoning, json_parsed, lines). On an unparseable/missing block the
    verdict defaults to PASS and the first line is keyword-scanned for a
    LONG/SHORT/CLOSE opinion.
    """
    lines = ai_text.strip().split("\n")
    verdict = "PASS"
    confidence: Any = 0.0
    side: Optional[str] = None
    entry_px: Any = perception.get("mid", 0)
    stop_px = 0.0
    tp_px = 0.0
    news_risk = "none"
    reasoning: Any = ai_text.strip()
    json_parsed = False

    # Find JSON on the last line
    json_str = ""
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{") and "verdict" in line and line.endswith("}"):
            json_str = line
            break

    # Fallback: regex match
    if not json_str:
        match = re.search(r'\{[^{}]*"verdict"[^{}]*\}', ai_text)
        if match:
            json_str = match.group(0)

    if json_str:
        try:
            cleaned = re.sub(r'```json\s*|```', '', json_str).strip()
            parsed = json.loads(cleaned)
            json_parsed = True

            raw = str(parsed.get("verdict", "")).upper()
            if raw == "LONG":
                verdict = "LONG"
            elif raw == "SHORT":
                verdict = "SHORT"
            elif raw == "CLOSE":
                verdict = "CLOSE"

            confidence = parsed.get("confidence", 0)
            side = parsed.get("side") if parsed.get("side") in ("long", "short") else None
            entry_px = parsed.get("entry_px") or parsed.get("entryPx", perception.get("mid", 0))
            stop_px = _coerce_px(parsed.get("stop_px") or parsed.get("stopPx", 0))
            tp_px = _coerce_px(parsed.get("tp_px") or parsed.get("tpPx", 0))
            nr = str(parsed.get("news_risk") or parsed.get("newsRisk") or "none").lower()
            news_risk = nr if nr in ("none", "positive", "negative") else "none"
            reasoning = parsed.get("reasoning", ai_text[:500])
        except json.JSONDecodeError:
            first_line = lines[0] if lines else ""
            if re.search(r"LONG", first_line, re.IGNORECASE):
                verdict = "LONG"
            elif re.search(r"SHORT", first_line, re.IGNORECASE):
                verdict = "SHORT"
            elif re.search(r"CLOSE", first_line, re.IGNORECASE):
                verdict = "CLOSE"

    return (verdict, confidence, side, entry_px, stop_px, tp_px,
            news_risk, reasoning, json_parsed, lines)


def _repair_stop_target(coin: str, verdict: str, entry_px: Any,
                        stop_px: float, tp_px: float,
                        perception: dict[str, Any],
                        atr_abs: Optional[float],
                        sl_atr_mult: float, tp_atr_mult: float) -> tuple[float, float]:
    """Validate/repair stop & target for a LONG/SHORT verdict.

    Two failure modes seen in production: the LLM omits stopPx/tpPx entirely
    (73/73 analyses on 2026-08-19 carried stop_px=0.0), or it returns a
    stop on the wrong side of entry. Both leave the record with no auditable
    risk plan and remove the executor's only bracket fallback for the atr<=0
    path. Drop an inverted stop/target; derive missing ones from ATR.
    """
    entry_ref = _coerce_px(entry_px) or _coerce_px(perception.get("mid", 0))
    atr_ref = _coerce_px(atr_abs)
    is_long = verdict == "LONG"

    if stop_px > 0 and entry_ref > 0:
        if (is_long and stop_px >= entry_ref) or (not is_long and stop_px <= entry_ref):
            logger.warning(
                f"[research] {coin} {verdict}: AI stop {stop_px} on wrong side of "
                f"entry {entry_ref} — discarding"
            )
            stop_px = 0.0
    if tp_px > 0 and entry_ref > 0:
        if (is_long and tp_px <= entry_ref) or (not is_long and tp_px >= entry_ref):
            logger.warning(
                f"[research] {coin} {verdict}: AI target {tp_px} on wrong side of "
                f"entry {entry_ref} — discarding"
            )
            tp_px = 0.0

    if entry_ref > 0 and atr_ref > 0:
        if stop_px <= 0:
            stop_px, _tp_derived = _atr_bracket(entry_ref, atr_ref, is_long,
                                                sl_atr_mult, tp_atr_mult)
            logger.info(
                f"[research] {coin} {verdict}: stop_px missing from AI — "
                f"ATR fallback {stop_px:.6g} (entry={entry_ref:.6g} atr={atr_ref:.6g} x{sl_atr_mult})"
            )
        if tp_px <= 0:
            _stop_derived, tp_px = _atr_bracket(entry_ref, atr_ref, is_long,
                                                sl_atr_mult, tp_atr_mult)
            logger.info(
                f"[research] {coin} {verdict}: tp_px missing from AI — "
                f"ATR fallback {tp_px:.6g} (entry={entry_ref:.6g} atr={atr_ref:.6g} x{tp_atr_mult})"
            )
    elif stop_px <= 0:
        logger.warning(
            f"[research] {coin} {verdict}: no AI stop and no ATR "
            f"(entry={entry_ref} atr={atr_abs}) — risk plan left empty"
        )
    return stop_px, tp_px


def parse_verdict(
    ai_text: str,
    coin: str,
    perception: dict[str, Any],
    atr_abs: Optional[float] = None,
    sl_atr_mult: Optional[float] = None,
    tp_atr_mult: float = 1.0,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Parse the AI response: JSON on the last line, with a regex fallback.

    `atr_abs` (absolute ATR in price units, 4h) enables the stop/target
    fallback: the LLM routinely omits stopPx/tpPx or returns 0, which leaves
    the analysis record with no auditable risk plan. When that happens on a
    LONG/SHORT we derive them from ATR rather than propagating zeros.

    P2-1: the JSON decode and the stop/target repair live in the
    `_extract_verdict_json` / `_repair_stop_target` helpers.
    """
    if sl_atr_mult is None:
        # P1-2: thread the runtime config through so an operator-tuned
        # sl_atr_mult is honoured instead of falling back to the default.
        sl_atr_mult = float(cfg_get("sl_atr_mult", config=config))
    if not ai_text:
        ai_text = ""

    (verdict, confidence, side, entry_px, stop_px, tp_px,
     news_risk, reasoning, json_parsed, _lines) = _extract_verdict_json(ai_text, perception)

    # Coerce confidence to a clamped float — the LLM occasionally returns it
    # as a string ("0.8") or out of range; a string would TypeError at the
    # gate comparison (`ctx.confidence >= 0.85`) on a live trade.
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    # If JSON parsing failed (confidence still 0), attempt NLP extraction from
    # natural-language HTA responses. HTA /research/short returns prose like:
    #   "Recommendation: bullish, medium confidence."
    #   "neutral with low confidence"
    # We map direction + qualitative confidence to a numeric value so the
    # executor's structural override can make an informed upgrade decision.
    nlp_parsed = False
    if confidence <= 0 and ai_text.strip():
        nlp_result = _parse_nlp_verdict(ai_text)
        if nlp_result:
            verdict = nlp_result["verdict"]
            confidence = nlp_result["confidence"]
            nlp_parsed = True
            reasoning = (reasoning or "")[:500]

    # Derive side from verdict when the LLM omitted/nulled the side field.
    # Without this a SHORT verdict with side=None falls through to the
    # executor's `or "long"` default and executes in the WRONG direction.
    if verdict == "LONG":
        side = "long"
    elif verdict == "SHORT":
        side = "short"
    # CLOSE/PASS keep whatever side was parsed (unused downstream).

    # Stop/target repair for directional verdicts (extracted to helper).
    if verdict in ("LONG", "SHORT"):
        stop_px, tp_px = _repair_stop_target(
            coin, verdict, entry_px, stop_px, tp_px,
            perception, atr_abs, sl_atr_mult, tp_atr_mult)

    return {
        "verdict": verdict,
        "confidence": confidence,
        "side": side,
        "entry_px": entry_px,
        "stop_px": stop_px,
        "tp_px": tp_px,
        "news_risk": news_risk,
        "reasoning": reasoning,
        # Empty ai_text = the LLM call failed (402/429/timeout) and this PASS is
        # an ERROR CODE, not an opinion. Tagged so the executor's structural/whale
        # override won't upgrade a failure-PASS into a blind LONG — on 2026-06-11
        # a 402 window let the override shotgun 8 PASS→LONG upgrades in one
        # minute, filling the book with unvetted longs that then blocked real
        # AI SHORT signals on the movers.
        "ai_down": not ai_text.strip(),
        # True when the verdict/confidence came from NLP prose extraction rather
        # than structured JSON. Lets the executor distinguish "AI returned a
        # parseable-but-low-conviction opinion" from "AI is broken".
        "nlp_parsed": nlp_parsed,
        # True when a structured JSON verdict block decoded cleanly, regardless
        # of what confidence it carried. Together with nlp_parsed this tells the
        # executor "the AI answered" vs "the response was garbage".
        "json_parsed": json_parsed,
    }


def _compute_as_of_date() -> Optional[str]:
    """Compute as_of_date from shared config (A3).

    Returns an ISO date string (YYYY-MM-DD) or None if the filter is disabled.
    Prevents look-ahead bias by limiting data to what was available on that
    date. Uses the unified UTC clock so hermes-trader and HTA agree on the
    analysis date.
    """
    cfg = _load_shared_config()
    aflt = cfg.get("as_of_date_filter", {})
    if not aflt.get("enabled", True):
        return None
    offset = int(aflt.get("default_offset_days", 0))
    return (utcnow() - timedelta(days=offset)).strftime("%Y-%m-%d")


def _should_skip_news() -> bool:
    """Check if news fetching should be skipped (B9 data boundary).

    When data_boundary.news is set to 'hta', news is handled by the
    HTA pipeline and should not be fetched by hermes-trader's research.py.
    """
    cfg = _load_shared_config()
    boundary = cfg.get("data_boundary", {})
    return boundary.get("news", "both") == "hta"


# ── Native multi-perspective debate (replaces external HTA :8766) ────────
# Three in-process LLM calls (bull / bear in parallel, then arbiter synthesis)
# using the SAME Hyperliquid perpetual context as the single-call path. This
# retires the cross-service HTA call (≈52s, spot-domain mismatch) while keeping
# the "second opinion" value. Off by default (debate_research.enabled=false).
_BULL_SYS = (
    "You are a Hyperliquid PERP futures LONG specialist. Given the perp data, "
    "argue ONLY the bull case using concrete perp signals (funding, OI change, "
    "orderbook/trend/whale flow). Never reference equities/spot. Output JSON: "
    '{"stance":"bullish","confidence":0-1,"arguments":[2-3 short bullets]}.'
)
_BEAR_SYS = (
    "You are a Hyperliquid PERP futures SHORT/sidestep specialist. Given the "
    "perp data, argue ONLY the bear case or why to stand aside, using concrete "
    "perp signals (funding, OI, trend/whale flow). Never reference "
    "equities/spot. Output JSON: "
    '{"stance":"bearish"|"neutral","confidence":0-1,"arguments":[2-3 short bullets]}.'
)
_SYNTH_SYS = (
    "You are the arbiter for a Hyperliquid PERP trade. Given BULL, BEAR and the "
    "perp data, return ONE compact JSON object only (no markdown, no prose):\n"
    '{"verdict":"LONG|SHORT|PASS","confidence":0-1,"conviction":"low|med|high",'
    '"thesis":"one short sentence","bull_case":"one short sentence",'
    '"bear_case":"one short sentence","suggested_stop_pct":0.01-0.1 or null,'
    '"key_risks":["short risk"]}. No equities/spot, no price targets.'
)

# Module-level TTL verdict cache. P2-2: key is (coin, score-bucket,
# trigger-hash) so the same coin firing DIFFERENT signals (or crossing a
# score bucket) no longer reuses a stale verdict; entries also carry a
# capacity cap with an opportunistic expiry sweep.
_debate_cache: dict[str, tuple] = {}
_debate_cache_lock = threading.Lock()
_DEBATE_CACHE_MAX_ENTRIES = 128  # fallback bound; debate_research.cache_max_entries overrides


def _debate_cache_key(coin: str, perception: dict[str, Any]) -> str:
    """Composite cache key: coin + composite-score bucket + fired-trigger hash.

    P2-2: a coin-only key meant a second perception within the TTL with
    different fired triggers (or a materially different composite score)
    silently reused the old verdict. Score is bucketed to 0.05 so normal
    float jitter still hits; the trigger set is hashed as a sorted tuple so
    order does not matter.
    """
    try:
        score = float(perception.get("composite_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    bucket = round(score / 0.05)
    triggers = ",".join(sorted(extract_fired_triggers(perception)))
    digest = hashlib.sha1(triggers.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{coin}|s{bucket}|t{digest}"


def _debate_cache_sweep_locked(now: float) -> None:
    """Drop expired entries, then evict oldest-expiry if still over capacity.

    Called with ``_debate_cache_lock`` held. Eviction order = nearest expiry
    first (those entries would die soonest anyway).
    """
    expired = [k for k, v in _debate_cache.items() if v[0] <= now]
    for k in expired:
        _debate_cache.pop(k, None)
    try:
        cap = int(cfg_get("debate_research.cache_max_entries", _DEBATE_CACHE_MAX_ENTRIES))
    except (TypeError, ValueError):
        cap = _DEBATE_CACHE_MAX_ENTRIES
    if cap <= 0:
        cap = _DEBATE_CACHE_MAX_ENTRIES
    evicted = 0
    if len(_debate_cache) > cap:
        over = sorted(_debate_cache.items(), key=lambda kv: kv[1][0])[: len(_debate_cache) - cap]
        for k, _ in over:
            _debate_cache.pop(k, None)
        evicted = len(over)
    # P3-1: eviction accounting (best-effort; called under the cache lock).
    if expired or evicted:
        try:
            from hermes_trader import metrics

            if expired:
                metrics.DEBATE_CACHE_EVICTIONS.labels(reason="expired").inc(len(expired))
            if evicted:
                metrics.DEBATE_CACHE_EVICTIONS.labels(reason="capacity").inc(evicted)
        except Exception:  # noqa: BLE001
            pass


def _debate_cfg() -> dict[str, Any]:
    cfg = read_agent_config()
    d = cfg.get("debate_research") or {}
    return {
        "enabled": bool(d.get("enabled", False)),
        "max_latency_s": float(d.get("max_latency_s", 15)),
        "cache_ttl_s": float(d.get("cache_ttl_s", 300)),
        "parallel": bool(d.get("parallel", True)),
        "use_structured_output": bool(d.get("use_structured_output", True)),
    }


def _debate_role(system_prompt: str) -> str:
    """Fingerprint a debate system prompt into a short role tag for logs."""
    low = system_prompt.lower()
    if "arbiter" in low:
        return "synth"
    if "long specialist" in low:
        return "bull"
    if "sidestep specialist" in low:
        return "bear"
    return "unknown"


def _debate_per_call_timeout() -> float:
    """Per-LLM-call timeout (seconds) for bull/bear (parallel, ~1900 char prompts).

    Bull/bear run in parallel (~5-10s each on Ark, but two simultaneous
    requests can push P95 to ~15s). 18s gives enough headroom for P95.
    """
    dcfg = _debate_cfg()
    return max(8.0, min(18.0, dcfg["max_latency_s"] * 0.7))


def _debate_synth_timeout() -> float:
    """Longer timeout for the synth/arbiter call.

    Synth runs serially after bull/bear and its prompt is ~3300 chars (bull +
    bear + market data), so P95 latency is higher than the individual bull/bear
    calls. Give it up to 24s while staying under the overall max_latency cap.
    """
    dcfg = _debate_cfg()
    return max(12.0, min(24.0, dcfg["max_latency_s"] * 0.92))


def _debate_direct(
    system_prompt: str,
    user_message: str,
    *,
    structured: bool,
    timeout: Optional[float] = None,
) -> str:
    """LLM call for the debate path — goes STRAIGHT to OpenRouter.

    Deliberately bypasses ``_call_ai`` (which would re-enter the external HTA
    service and its 60s timeout). A shorter timeout keeps the whole debate
    within the latency cap.

    NOTE: The arbiter (synth) now always calls this with ``structured=False``
    because direct measurement showed Ark's ``response_format=json_schema``
    adds ~11s (16s vs 5s for the same prompt). The ``structured`` parameter
    is retained so future callers can opt in, but the arbiter path relies on
    :func:`parse_structured` to extract JSON from prose/code-fences.
    """
    dcfg = _debate_cfg()
    # R13-B10: the debate path's tighter token budget resolves through
    # research_llm_params (canonical research_llm.debate_max_tokens → the
    # former 350 literal).
    debate_tokens = int(research_llm_params()["debate_max_tokens"])
    rf = ResearchVerdict.openrouter_response_format() if structured else None
    per_call_timeout = timeout if timeout is not None else _debate_per_call_timeout()
    role = _debate_role(system_prompt)
    t0 = time.time()
    logger.info(
        f"[debate] call START | role={role} structured={structured} "
        f"timeout_s={per_call_timeout:.1f} prompt_chars={len(user_message)}"
    )
    try:
        out = _call_openrouter(
            system_prompt,
            user_message,
            response_format=rf,
            timeout=per_call_timeout,
            max_tokens=debate_tokens,
            path="debate_direct",
        )
    except Exception as e:
        logger.warning(
            f"[debate] call ERROR | role={role} elapsed_ms={int((time.time()-t0)*1000)} "
            f"err={type(e).__name__}: {e}"
        )
        try:
            from hermes_trader import metrics

            metrics.DEBATE_STAGE_DURATION.labels(stage=role, outcome="failed").observe(
                max(0.0, time.time() - t0)
            )
        except Exception:  # noqa: BLE001
            pass
        raise
    # _call_openrouter swallows HTTP/timeout errors and returns "" — treat that
    # as a failure so the debate path falls back instead of feeding an empty
    # string into the arbiter (which would always parse-fail).
    if not out or not out.strip():
        elapsed = int((time.time() - t0) * 1000)
        logger.warning(
            f"[debate] call EMPTY | role={role} elapsed_ms={elapsed} "
            f"(timeout/HTTP error from LLM endpoint)"
        )
        try:
            from hermes_trader import metrics

            metrics.DEBATE_STAGE_DURATION.labels(stage=role, outcome="empty").observe(
                max(0.0, time.time() - t0)
            )
        except Exception:  # noqa: BLE001
            pass
        raise TimeoutError(f"empty LLM response after {elapsed}ms (role={role})")
    logger.info(
        f"[debate] call OK | role={role} elapsed_ms={int((time.time()-t0)*1000)} "
        f"resp_chars={len(out)}"
    )
    try:
        from hermes_trader import metrics

        metrics.DEBATE_STAGE_DURATION.labels(stage=role, outcome="ok").observe(
            max(0.0, time.time() - t0)
        )
    except Exception:  # noqa: BLE001
        pass
    return out


def _bull_analysis(ctx_msg: str) -> str:
    logger.debug("[debate] bull analysis start")
    return _debate_direct(_BULL_SYS, ctx_msg, structured=False)


def _bear_analysis(ctx_msg: str) -> str:
    logger.debug("[debate] bear analysis start")
    return _debate_direct(_BEAR_SYS, ctx_msg, structured=False)


def _synthesize(bull: str, bear: str, ctx_msg: str) -> Optional[dict[str, Any]]:
    """Arbiter step. Returns a validated ResearchVerdict dict or None.

    Always uses unstructured (prose-JSON) mode: direct testing against the Ark
    endpoint showed ``response_format=json_schema`` adds ~11s of latency (16s
    vs 5s for the same prompt), while ``parse_structured`` reliably extracts
    the JSON object from code fences or surrounding prose.
    """
    msg = (
        f"=== PERP MARKET DATA ===\n{ctx_msg}\n\n"
        f"=== BULL ARGUMENT ===\n{bull}\n\n"
        f"=== BEAR ARGUMENT ===\n{bear}\n"
    )
    logger.info(
        f"[debate] synth START | bull_chars={len(bull or '')} "
        f"bear_chars={len(bear or '')} msg_chars={len(msg)}"
    )
    t0 = time.time()
    raw = _debate_direct(_SYNTH_SYS, msg, structured=False, timeout=_debate_synth_timeout())
    parsed = parse_structured(raw)
    if parsed is None:
        logger.warning(
            f"[debate] synth PARSE-FAIL | elapsed_ms={int((time.time()-t0)*1000)} "
            f"raw_chars={len(raw or '')} raw_head={(raw or '')[:200]!r}"
        )
    else:
        logger.info(
            f"[debate] synth OK | elapsed_ms={int((time.time()-t0)*1000)} "
            f"verdict={parsed.get('verdict')} conf={parsed.get('confidence')} "
            f"conviction={parsed.get('conviction')}"
        )
    return parsed


def _debate_metric(
    kind: str, name: str, labels: dict[str, str], value: Optional[float] = None
) -> None:
    """Best-effort Prometheus emit for the debate path. Replaces the repeated
    inline `try: from hermes_trader import metrics ... except Exception: pass`
    blocks: metrics are optional, so any failure is swallowed silently.

    kind is one of "inc" (counter), "observe" (histogram), "set" (gauge).
    """
    try:
        from hermes_trader import metrics

        metric = getattr(metrics, name)
        if labels:
            metric = metric.labels(**labels)
        if kind == "inc":
            metric.inc()
        elif kind == "observe":
            metric.observe(value)
        elif kind == "set":
            metric.set(value)
    except Exception:  # noqa: BLE001
        pass


def _debate_research(
    coin: str, ctx_msg: str, perception: dict[str, Any], *, atr_abs: Optional[float],
    config: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Run bull/bear in parallel, then synthesize. Returns mapped fields or
    None on any failure/timeout so the caller falls back to the single path."""
    dcfg = _debate_cfg()
    ttl = dcfg["cache_ttl_s"]
    now = time.time()
    logger.info(
        f"[debate] RESEARCH-START | coin={coin} parallel={dcfg['parallel']} "
        f"max_latency_s={dcfg['max_latency_s']} cache_ttl_s={ttl} "
        f"structured={dcfg['use_structured_output']} ctx_chars={len(ctx_msg)} "
        f"atr_abs={atr_abs}"
    )
    # P2-2: composite key (coin + score bucket + fired-trigger hash) so a
    # different signal mix on the same coin never reuses the old verdict.
    cache_key = _debate_cache_key(coin, perception)
    with _debate_cache_lock:
        hit = _debate_cache.get(cache_key)
        if hit and hit[0] > now:
            logger.info(
                f"[debate] cache HIT | key={cache_key} ttl_remaining_s="
                f"{int(hit[0] - now)} verdict={hit[1].get('verdict')}"
            )
            _debate_metric("inc", "DEBATE_CACHE_LOOKUPS", {"result": "hit"})
            return dict(hit[1])
        if hit:
            logger.info(f"[debate] cache STALE | key={cache_key} → refetch")
            _cache_lookup = "stale"
        else:
            _cache_lookup = "miss"
    _debate_metric("inc", "DEBATE_CACHE_LOOKUPS", {"result": _cache_lookup})

    bb_start = time.time()
    bull = ""
    bear = ""
    per_call = _debate_per_call_timeout()
    try:
        if dcfg["parallel"]:
            logger.info(
                f"[debate] bull/bear PARALLEL start | coin={coin} per_call_s={per_call:.1f}"
            )
            # P1-1: shared pool (no per-call pool churn). Each call has its own
            # per-call httpx timeout; give the future a few seconds of slack
            # beyond that so the inner timeout (not the future) is what
            # enforces the cap and raises a clear error.
            pool = _get_pool()
            f_bull = pool.submit(_bull_analysis, ctx_msg)
            f_bear = pool.submit(_bear_analysis, ctx_msg)
            bull = f_bull.result(timeout=per_call + 4.0)
            bear = f_bear.result(timeout=per_call + 4.0)
        else:
            logger.info(f"[debate] bull/bear SERIAL start | coin={coin}")
            bull = _bull_analysis(ctx_msg)
            bear = _bear_analysis(ctx_msg)
    except Exception as e:
        logger.warning(
            f"[debate] bull/bear FAILED → single fallback | coin={coin} "
            f"elapsed_ms={int((time.time()-bb_start)*1000)} "
            f"err={type(e).__name__}: {e}"
        )
        _debate_metric(
            "observe", "DEBATE_STAGE_DURATION",
            {"stage": "bull_bear", "outcome": "failed"}, max(0.0, time.time() - bb_start),
        )
        _debate_metric("inc", "DEBATE_FALLBACKS", {"reason": "bull_bear_failed"})
        return None

    bb_elapsed = int((time.time() - bb_start) * 1000)
    logger.info(
        f"[debate] bull/bear DONE | coin={coin} elapsed_ms={bb_elapsed} "
        f"bull_chars={len(bull or '')} bear_chars={len(bear or '')}"
    )
    _debate_metric(
        "observe", "DEBATE_STAGE_DURATION",
        {"stage": "bull_bear", "outcome": "ok"}, max(0.0, time.time() - bb_start),
    )

    # No emptiness check needed: _debate_direct raises on empty responses, so
    # bull/bear are always non-empty strings here (any empty call already
    # aborted above via the bull/bear FAILED fallback).

    synth_start = time.time()
    try:
        # Give synth its own (longer) per-call timeout; the wall-clock cap is
        # enforced by the inner httpx timeout, not by starving the future.
        synth_timeout = max(8.0, _debate_synth_timeout() + 4.0)
        logger.info(
            f"[debate] synth dispatch | coin={coin} timeout_s={synth_timeout:.1f} "
            f"(elapsed_s={(synth_start-now):.1f})"
        )
        # P1-1: shared pool. Timeout enforced by the future (inner httpx call
        # has its own longer cap for LLM inference).
        f_synth = _get_pool().submit(_synthesize, bull, bear, ctx_msg)
        sv = f_synth.result(timeout=synth_timeout)
    except Exception as e:
        logger.warning(
            f"[debate] synth FAILED → single fallback | coin={coin} "
            f"elapsed_ms={int((time.time()-synth_start)*1000)} "
            f"err={type(e).__name__}: {e}"
        )
        _debate_metric(
            "observe", "DEBATE_STAGE_DURATION",
            {"stage": "synth", "outcome": "failed"}, max(0.0, time.time() - synth_start),
        )
        _debate_metric("inc", "DEBATE_FALLBACKS", {"reason": "synth_failed"})
        return None

    if not sv:
        logger.warning(
            f"[debate] synth unparseable → single fallback | coin={coin} "
            f"elapsed_ms={int((time.time()-synth_start)*1000)}"
        )
        _debate_metric(
            "observe", "DEBATE_STAGE_DURATION",
            {"stage": "synth", "outcome": "empty"}, max(0.0, time.time() - synth_start),
        )
        _debate_metric("inc", "DEBATE_FALLBACKS", {"reason": "synth_empty"})
        return None

    _debate_metric(
        "observe", "DEBATE_STAGE_DURATION",
        {"stage": "synth", "outcome": "ok"}, max(0.0, time.time() - synth_start),
    )

    fields = structured_to_analysis_fields(
        sv, coin, perception,
        atr_abs=atr_abs,
        sl_atr_mult=float(cfg_get("sl_atr_mult", config=config)),
    )
    if ttl > 0:
        with _debate_cache_lock:
            _debate_cache[cache_key] = (time.time() + ttl, dict(fields))
            # P2-2: opportunistic sweep — drop expired entries and enforce the
            # capacity cap (nearest-expiry eviction) so the cache cannot grow
            # unbounded across many (coin, signal-mix) combinations.
            _debate_cache_sweep_locked(time.time())
            _entries = len(_debate_cache)
        logger.info(
            f"[debate] cache WRITE | key={cache_key} ttl_s={ttl} "
            f"entries={_entries}"
        )
        _debate_metric("set", "DEBATE_CACHE_ENTRIES", {}, float(_entries))
    logger.info(
        f"[debate] DEBATE-OK | coin={coin} verdict={fields['verdict']} "
        f"side={fields['side']} conf={fields['confidence']:.2f} "
        f"stop_px={fields['stop_px']} tp_px={fields['tp_px']} "
        f"elapsed_ms={int((time.time()-now)*1000)}"
    )
    _debate_metric(
        "observe", "DEBATE_STAGE_DURATION",
        {"stage": "total", "outcome": "ok"}, max(0.0, time.time() - now),
    )
    return fields


def _timed_fetch(coin: str, label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Wrap a fetch call with start/end timing logs so the slowest source
    is immediately visible in trader logs."""
    _t0 = time.monotonic()
    logger.info(f"[research] {coin}:   → {label} START")
    try:
        result = fn(*args, **kwargs)
        _elapsed = time.monotonic() - _t0
        logger.info(f"[research] {coin}:   ← {label} OK in {_elapsed:.2f}s")
        return result
    except Exception as _e:
        _elapsed = time.monotonic() - _t0
        logger.error(
            f"[research] {coin}:   ← {label} FAIL after {_elapsed:.2f}s: "
            f"{type(_e).__name__}: {_e}"
        )
        raise


def _parallel_prefetch(coin: str, skip_news_flag: bool) -> dict[str, Any]:
    """Fetch all independent pre-LLM data in parallel: 3 candle timeframes +
    funding rate + news + positioning signals. None depend on each other,
    so issue them together to collapse serial latency into max(T).

    Returns a dict with keys c1h/c4h/c1d/funding_raw/news/signals_block.
    Raises RuntimeError if any future fails or exceeds the per-fetch timeout.
    """
    # Per-fetch timeout for the parallel pre-LLM data gather. Each individual
    # HTTP call already has its own sub-timeout (hl_client: 5s+3 retries;
    # funding/news/signals: their own), but if ANY of them hangs without
    # raising, f.result() with no timeout blocks the whole research pipeline
    # indefinitely — which is enough to trip the 600s watchdog during a
    # 3-trigger cycle. Bound the gather and bail fast on a stuck future.
    #
    # P1-7: the outer ceiling is PER-SOURCE, not one flat 45s for every
    # future. Funding/news are light calls that should return in seconds, so a
    # hung funding future must not be allowed to burn 45s; candles carry
    # hl_client retries and deserve more headroom. Each future's own HTTP
    # timeout still applies underneath; these bounds only cap a HUNG future.
    # HERMES_RESEARCH_FETCH_TIMEOUT_S remains honored as a fallback ceiling for
    # any source without an explicit override. R13-B10: the ceilings resolve
    # through research_fetch_params — each legacy HERMES_RESEARCH_FETCH_* env
    # var still wins (test/operator compat), then the canonical
    # research_fetch block, then these literals.
    fp = research_fetch_params()
    _default_timeout = float(fp["fetch_timeout_default_sec"])
    _src_leaf = {
        "candles": "fetch_timeout_candles_sec",
        "funding": "fetch_timeout_funding_sec",
        "news": "fetch_timeout_news_sec",
        "signals": "fetch_timeout_signals_sec",
    }

    def _src_timeout(name: str) -> float:
        v = float(fp[_src_leaf[name]])
        return v if v > 0 else _default_timeout

    t_candles = _src_timeout("candles")
    t_funding = _src_timeout("funding")
    t_news = _src_timeout("news")
    t_signals = _src_timeout("signals")
    _fetch_t0 = time.monotonic()
    logger.info(
        f"[research] {coin}: parallel data-fetch START "
        f"(timeouts candles={t_candles:.0f}s funding={t_funding:.0f}s "
        f"news={t_news:.0f}s signals={t_signals:.0f}s)"
    )

    # P1-1: submit to the shared pool instead of a per-research 6-worker
    # pool (which nested a further 3-worker pool — up to 24 concurrent HTTP
    # calls per coin). The shared pool bounds total research concurrency.
    pool = _get_pool()
    f_1h = pool.submit(_timed_fetch, coin, "candles-1h", fetch_hl_candles, coin, "1h", 100)
    f_4h = pool.submit(_timed_fetch, coin, "candles-4h", fetch_hl_candles, coin, "4h", 100)
    f_1d = pool.submit(_timed_fetch, coin, "candles-1d", fetch_hl_candles, coin, "1d", 60)
    f_funding = pool.submit(_timed_fetch, coin, "funding", _fetch_funding_rate, coin)
    f_news = pool.submit(
        _timed_fetch, coin, "news",
        lambda: "news handled by HTA (B9 boundary)" if skip_news_flag else _fetch_news(coin),
    )
    f_signals = pool.submit(_timed_fetch, coin, "signals", _signals_block, coin)

    # (future, result-key, source-label, per-source timeout). P1-7: wait each
    # future with its OWN ceiling and report the specific source that stalled,
    # instead of one flat timeout that lets a light call hang for 45s.
    _specs = [
        (f_1h, "c1h", "candles-1h", t_candles),
        (f_4h, "c4h", "candles-4h", t_candles),
        (f_1d, "c1d", "candles-1d", t_candles),
        (f_funding, "funding_raw", "funding", t_funding),
        (f_news, "news", "news", t_news),
        (f_signals, "signals_block", "signals", t_signals),
    ]
    out: dict[str, Any] = {}
    timed_src = ""
    timed_after = 0.0
    try:
        for fut, key, label, t in _specs:
            try:
                out[key] = fut.result(timeout=t)
            except FuturesTimeoutError:
                # Re-raise with the source label so the failure message (and
                # the caller's log) names exactly which fetch stalled.
                timed_src, timed_after = label, t
                raise TimeoutError(f"{label} timed out after {t:.0f}s")
    except Exception as _e:
        # Cancel anything still running so a stuck HL/LLM call can't leak a
        # thread; raise so the caller's per-coin try/except logs the failure
        # and the trading loop moves on to the next trigger.
        for fut, _key, _label, _t in _specs:
            if not fut.done():
                fut.cancel()
        _total = time.monotonic() - _fetch_t0
        logger.error(
            f"[research] {coin}: parallel data-fetch FAILED after {_total:.2f}s: "
            f"{type(_e).__name__}: {_e}"
        )
        _detail = (f"{timed_src} timed out after {timed_after:.0f}s"
                   if timed_src else f"{type(_e).__name__}: {_e}")
        raise RuntimeError(
            f"parallel data-fetch for {coin} failed/timed out ({_detail})"
        ) from _e

    _fetch_total = time.monotonic() - _fetch_t0
    logger.info(f"[research] {coin}: parallel data-fetch DONE in {_fetch_total:.2f}s")
    return out


def _build_analysis(coin: str, perception: dict[str, Any], *,
                    news: Any, parsed: dict[str, Any], ai_text: str,
                    debate_used: bool, trace_id: str,
                    as_of_date: Optional[str],
                    fired_names: set,
                    tf1h: dict[str, Any], tf4h: dict[str, Any],
                    c1h: list[Any]) -> dict[str, Any]:
    """Assemble the persisted analysis record from the researched verdict.

    Pure assembly — every field is carried verbatim from the parsed verdict,
    the perception, or the indicator frames; the trigger flags all derive
    from the single extracted fired-trigger set. P2-1 extraction keeps
    research() as the orchestration layer.
    """
    analysis = {
        "id": str(uuid.uuid4()),
        "trace_id": trace_id,
        "perception_id": perception.get("id", "unknown"),
        "coin": coin,
        "verdict": parsed["verdict"],
        "confidence": parsed["confidence"],
        "side": parsed["side"],
        "entry_px": parsed["entry_px"],
        "stop_px": parsed["stop_px"],
        "tp_px": parsed["tp_px"],
        "reasoning": parsed["reasoning"],
        "news_context": news,
        # AI's good/bad judgment of the recent news — drives the news gate
        # (only "negative" stands the trade down; an earnings beat is fine).
        "news_risk": parsed["news_risk"],
        # Failure-PASS marker — must survive this whitelist or the executor's
        # override guard never sees it (it didn't, on first deploy).
        "ai_down": bool(parsed.get("ai_down")),
        # Same deal: the executor's zero-confidence guard uses nlp_parsed to
        # tell "AI gave a real low-conviction opinion" apart from "AI response
        # was unparseable". Dropping it here would block every conf=0 PASS.
        "nlp_parsed": bool(parsed.get("nlp_parsed")),
        "json_parsed": bool(parsed.get("json_parsed")),
        "degraded": "[DEGRADED" in (ai_text or ""),
        # Native multi-perspective debate provenance (replaces external HTA).
        "debate_used": debate_used,
        "structured": bool(parsed.get("structured")),
        "conviction": parsed.get("conviction"),
        "bull_case": parsed.get("bull_case", ""),
        "bear_case": parsed.get("bear_case", ""),
        "suggested_stop_pct": parsed.get("suggested_stop_pct"),
        "key_risks": list(parsed.get("key_risks") or []),
        "as_of_date": as_of_date,
        "created_at": int(time.time() * 1000),
        # Carry forward so risk gates can read own-coin signal strength.
        "composite_score": float(perception.get("composite_score", 0) or 0),
        # P2-6: all fired-trigger flags derive from the single extracted set.
        "momentum_burst_fired": "momentumBurst" in fired_names,
        "slow_burn_fired": bool(fired_names & {"volumeBuildup1h", "trendFlip1h", "higherLows1h"}),
        "slow_burn_count": len(fired_names & {"volumeBuildup1h", "trendFlip1h", "higherLows1h"}),
        # O'Neil breakout pair — feeds the breakout force-execute (a hedged AI
        # PASS on a 20-period-high break WITH a volume surge gets upgraded;
        # XPL +32% 2026-06-12 was researched 38x, PASSed 21x, never traded
        # while both of these were fired hours before the move).
        "breakout_fired": "breakout" in fired_names,
        "volume_spike_fired": "volumeSpike" in fired_names,
        "uptrend_momentum_fired": "uptrendMomentum" in fired_names,
        "downtrend_momentum_fired": "downtrendMomentum" in fired_names,
        "daily_mover_fired": "dailyMover" in fired_names,
        # OI+funding accumulation signal (oi_funding_anomaly). When present,
        # the coin shows whale-loading patterns (high OI, negative funding,
        # flat price). Used as a counter-regime bypass for LONGs.
        "whale_signal": perception.get("whale_signal"),
        # Fired indicator names — used by the executor's structural-override
        # diagnostics log so a post-mortem can see exactly which TA/slow-burn
        # triggers upgraded a PASS to LONG.
        "fired_triggers": extract_fired_triggers(perception),
        # 4h indicator snapshot — feeds the executor's late-entry veto
        # (RSI extremes + over-extension from EMA21). Already computed above
        # for the AI prompt; carried forward so the executor doesn't refetch.
        "rsi4h": tf4h.get("rsi14"),
        "adx4h": tf4h.get("adx14"),
        "atr4h": tf4h.get("atr14"),
        "ema21_4h": tf4h.get("ema21"),
        "close4h": tf4h.get("last_close"),
        # 1h indicator snapshot — feeds the executor's Plan-B regime-strength
        # score (5-component weighted, byte-aligned with backtest_ab_compare).
        # Used to distinguish mid-strength TREND (size x0.5) from STRONG_TREND
        # (full size); the 4-state production detect_regime() cannot make that
        # split because it has no strength score.
        "ema8_1h": tf1h.get("ema8"),
        "ema21_1h": tf1h.get("ema21"),
        "atr1h": tf1h.get("atr14"),
        "adx1h": tf1h.get("adx14"),
        "close1h": tf1h.get("last_close"),
        "obv_slope_1h": _obv_slope_sign(c1h),
    }
    return analysis


def _thin_history_pass(coin: str, perception: dict[str, Any], c4h: list[Any], news: Any) -> dict[str, Any]:
    """Thin-history guard: multi-timeframe TA is meaningless without enough 4h
    bars (EMA21/ADX need history). Builds the ai_down PASS analysis, records it
    and returns it so research() can short-circuit — no LLM call, no entry.
    Extracted from research() (P2-1)."""
    logger.warning(f"[research] thin 4h history for {coin}: only {len(c4h)} candles — PASS (skip)")
    analysis = {
        "id": str(uuid.uuid4()), "perception_id": perception.get("id", "unknown"),
        "coin": coin, "verdict": "PASS", "confidence": 0.0, "side": None,
        "entry_px": perception.get("mid", 0), "stop_px": 0.0, "tp_px": 0.0,
        "reasoning": f"insufficient 4h history ({len(c4h)} candles) for reliable multi-TF TA",
        "news_context": news, "news_risk": "none",
        "created_at": int(time.time() * 1000),
        "composite_score": float(perception.get("composite_score", 0) or 0),
        # P1-16: thin-history PASS skips the LLM entirely, so flag
        # ai_down so downstream record/notify sees "no AI decision made"
        # rather than implying a live model produced a PASS.
        "ai_down": True,
        "momentum_burst_fired": False, "slow_burn_fired": False,
        "slow_burn_count": 0,
        "daily_mover_fired": "dailyMover" in set(extract_fired_triggers(perception)),
        "whale_signal": perception.get("whale_signal"),
    }
    memory.record_analysis(analysis)
    return analysis


def _account_context(
    account_snapshot: Optional[dict[str, Any]],
) -> tuple[float, dict[str, float], list[dict[str, Any]]]:
    """Resolve equity, per-DEX equity and open positions for the user prompt.
    Uses the cycle-level snapshot when provided (avoids N duplicate account
    POSTs per cycle); fetches via fetch_account_state() otherwise and pushes
    equity to memory in that case. Extracted from research() (P2-1)."""
    equity = 0.0
    dex_equity: dict[str, float] = {}
    open_positions: list[dict[str, Any]] = []
    user = resolve_user_address()

    if user:
        state = account_snapshot if account_snapshot is not None else fetch_account_state(user, include_hip3=True)
        equity = float(state.get("equity", "0"))
        dex_equity = state.get("dex_equity") or {}
        if account_snapshot is None:
            memory.update_equity(equity)

        open_positions = [
            {
                "coin": p.get("position", {}).get("coin", ""),
                "side": "long" if float(p.get("position", {}).get("szi", "0")) > 0 else "short",
                "size_usd": float(p.get("position", {}).get("positionValue", "0")) or (
                    abs(float(p.get("position", {}).get("szi", "0"))) *
                    float(p.get("position", {}).get("entryPx", "0"))
                ),
            }
            for p in (state.get("asset_positions") or [])
            if float(p.get("position", {}).get("szi", "0")) != 0
        ]

    return equity, dex_equity, open_positions


def research(coin: str, perception: dict[str, Any], *, account_snapshot: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Full AI research pipeline for a perception — returns an analysis dict.

    Args:
        account_snapshot: Pre-fetched account state from the current scan cycle.
            When provided, skips the per-coin fetch_account_state() call to avoid
            N duplicate HTTP POSTs per cycle. When None, fetches its own.
    """
    as_of_date = _compute_as_of_date()
    # Parallel pre-LLM data gather (3 candle TFs + funding + news + signals),
    # extracted to _parallel_prefetch (P2-1).
    _prefetched = _parallel_prefetch(coin, _should_skip_news())
    c1h = _prefetched["c1h"]
    c4h = _prefetched["c4h"]
    c1d = _prefetched["c1d"]
    funding_raw = _prefetched["funding_raw"]
    news = _prefetched["news"]
    signals_block = _prefetched["signals_block"]

    # Thin-history guard extracted to _thin_history_pass (P2-1): a near-empty
    # 4h series produced confident-looking but baseless entries — decline with
    # an ai_down PASS (no LLM call, no entry).
    if len(c4h) < 30:
        return _thin_history_pass(coin, perception, c4h, news)

    tf1h = _compute_indicators(c1h)
    tf4h = _compute_indicators(c4h)
    tf1d = _compute_indicators(c1d)

    # P2-6: canonical fired-trigger names once, reused by every signal flag.
    fired_names = set(extract_fired_triggers(perception))

    config = read_agent_config()
    mode = str(config.get("mode", "OFF"))

    # Account/positions extraction moved to _account_context (P2-1). Uses the
    # cycle-level snapshot when provided, else fetches (and updates equity).
    equity, dex_equity, open_positions = _account_context(account_snapshot)

    wr = memory.get_win_rate()
    system_prompt = build_system_prompt(mode, wr.get("rate", 0), int(wr.get("total", 0)))
    user_message = _build_user_message(
        coin, perception, tf1h, tf4h, tf1d,
        funding_raw, news, equity, open_positions, mode,
        dex_equity=dex_equity, recent_candles=c1h,
        signals_block=signals_block,
    )

    # Trace this decision cycle end-to-end. The trace id flows into HTA calls
    # and events.jsonl so a signal/order/close can be correlated later.
    trace_id = str(perception.get("trace_id") or new_trace_id("sig"))

    # Pass the 4h ATR so parse_verdict can synthesise a stop/target when the
    # LLM omits them (it usually does). 4h matches the timeframe the executor
    # uses for its own bracket, so the two agree.
    _atr_4h = tf4h.get("atr14")

    # Native multi-perspective debate (in-process replacement for external HTA
    # :8766). When enabled, run bull/bear in parallel + arbiter synthesis with
    # a hard latency cap; on any failure/timeout fall back to the single-LLM
    # path so behavior never degrades. Default off (C2).
    dcfg = _debate_cfg()
    parsed: Optional[dict[str, Any]] = None
    debate_used = False
    if dcfg["enabled"]:
        logger.info(
            f"[debate] research() entry ENABLED | coin={coin} trace_id={trace_id} "
            f"parallel={dcfg['parallel']} max_latency_s={dcfg['max_latency_s']} "
            f"structured={dcfg['use_structured_output']}"
        )
        debate_fields = _debate_research(
            coin, user_message, perception, atr_abs=_atr_4h, config=config
        )
        if debate_fields is not None:
            parsed = debate_fields
            debate_used = True
            logger.info(
                f"[debate] research() debate SUCCEEDED | coin={coin} "
                f"verdict={parsed.get('verdict')} side={parsed.get('side')}"
            )
        else:
            logger.warning(
                f"[debate] research() debate returned None → single-LLM fallback | "
                f"coin={coin}"
            )
    else:
        logger.debug(
            f"[debate] research() entry DISABLED | coin={coin} trace_id={trace_id}"
        )

    if parsed is None:
        ai_text = _call_ai(system_prompt, user_message, trace_id=trace_id)
        parsed = parse_verdict(
            ai_text, coin, perception,
            atr_abs=_atr_4h,
            sl_atr_mult=float(cfg_get("sl_atr_mult", config=config)),
        )
    else:
        # Debate path does not touch _call_ai; synthesize a marker for telemetry.
        ai_text = ""

    # Analysis-record assembly extracted to _build_analysis (P2-1).
    analysis = _build_analysis(
        coin, perception,
        news=news, parsed=parsed, ai_text=ai_text,
        debate_used=debate_used, trace_id=trace_id,
        as_of_date=as_of_date,
        fired_names=fired_names,
        tf1h=tf1h, tf4h=tf4h, c1h=c1h,
    )

    memory.record_analysis(analysis)
    return analysis
