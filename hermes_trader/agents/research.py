"""Deep-analysis pipeline: perception -> multi-timeframe indicators -> AI verdict -> persist."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import yaml

from hermes_trader.agents.config_store import read_agent_config
from hermes_trader.agents.memory import memory
from hermes_trader.agents.system_prompt import build_system_prompt
from hermes_trader.client.hl_client import (
    fetch_account_state,
    fetch_funding_history,
    fetch_hl_candles,
    resolve_user_address,
)
from hermes_trader.indicators.math import adx, atr, candle_val, ema, rsi
from hermes_trader.models.types import Candle

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
_SHARED_CONFIG_PATH = os.path.expanduser("~/.hermes-trading/config.yaml")

# Shared httpx client with connection pooling for LLM calls.
_LLM_CLIENT_LOCK = threading.Lock()
_LLM_CLIENT: Optional[httpx.AsyncClient] = None
_LLM_CLIENT_REFCNT = 0


def _get_llm_client() -> httpx.AsyncClient:
    """Get or create the shared LLM HTTP client with connection pooling."""
    global _LLM_CLIENT, _LLM_CLIENT_REFCNT
    with _LLM_CLIENT_LOCK:
        if _LLM_CLIENT is None:
            _LLM_CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(120.0),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        _LLM_CLIENT_REFCNT += 1
        return _LLM_CLIENT


def _release_llm_client() -> None:
    """Release a reference to the shared LLM client; close when last ref drops."""
    global _LLM_CLIENT, _LLM_CLIENT_REFCNT
    with _LLM_CLIENT_LOCK:
        _LLM_CLIENT_REFCNT -= 1
        if _LLM_CLIENT_REFCNT <= 0 and _LLM_CLIENT is not None:
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                loop.run_until_complete(_LLM_CLIENT.aclose())
                loop.close()
            except Exception:
                pass
            _LLM_CLIENT = None
            _LLM_CLIENT_REFCNT = 0


def _load_shared_config() -> dict:
    """Load the cross-component shared config from ~/.hermes-trading/config.yaml.
    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    try:
        with open(_SHARED_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _call_hta_service(
    ticker: str,
    analysis_date: str,
    system_prompt: str,
    user_message: str,
    *,
    trace_id: str = "",
) -> Optional[str]:
    """Call the HTA FastAPI resident service for multi-agent analysis (B1/B5).

    Reads the HTA service URL from the shared config, with fallback to 8766.
    Returns the HTA analysis text, or None if the service is unavailable.
    Successful responses are routed through the Signal Bus for validation,
    audit logging (events.jsonl), and circuit-breaker bookkeeping.
    """
    cfg = _load_shared_config()
    hta_cfg = cfg.get("hta_service", {})
    if not hta_cfg.get("enabled", True):
        return None

    url = hta_cfg.get("url") or os.environ.get("HTA_SERVICE_URL", "http://localhost:8766")
    try:
        resp = httpx.post(
            f"{url}/research/short",
            json={
                "ticker": ticker,
                "date": analysis_date,
                "asset_type": "crypto",
                # Ship the prompt pair: without it HTA falls back to its own
                # 2-sentence prose prompt and the verdict/confidence JSON we
                # parse downstream never appears, collapsing every analysis
                # onto the NLP keyword fallback.
                "system_prompt": system_prompt,
                "user_message": user_message,
            },
            timeout=float(os.environ.get("HTA_TIMEOUT", "60")),
        )
        if resp.is_success:
            data = resp.json()
            decision = data.get("decision", "")
            if decision:
                logger.info(f"[research] HTA analysis OK for {ticker} ({len(decision)} chars)")
                # Feed the signal bus for schema validation + audit log.
                # The bus only records a signal when the decision parses cleanly
                # into BUY/SELL/HOLD; raw text flows through regardless.
                if get_bus is not None and Signal is not None:
                    try:
                        parsed_verdict = _infer_verdict_from_text(decision)
                        raw_conf = _infer_confidence_from_text(decision)
                        bus = get_bus()
                        bus.ingest(
                            {
                                "ticker": ticker,
                                "as_of_date": analysis_date,
                                "asset_type": "crypto",
                                "verdict": parsed_verdict,
                                "confidence": raw_conf,
                                "reasoning": decision[:500],
                                "trace_id": trace_id,
                                "source": "hta",
                            },
                            source="hta",
                            trace_id=trace_id,
                        )
                    except Exception as e:  # pragma: no cover - bus is advisory
                        logger.debug(f"[research] signal_bus ingest skipped: {e}")
                return decision
        logger.warning(f"[research] HTA service returned {resp.status_code} for {ticker}")
        if get_bus is not None:
            try:
                get_bus().report_failure(f"http {resp.status_code}")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[research] HTA service unavailable for {ticker}: {e}")
        if get_bus is not None:
            try:
                get_bus().report_failure(str(e))
            except Exception:
                pass

    return None


def _infer_verdict_from_text(text: str) -> str:
    """Best-effort verdict extraction used for audit logging only."""
    if not text:
        return "HOLD"
    upper = text.upper()
    # Prefer an explicit JSON verdict if present.
    m = re.search(r'"verdict"\s*:\s*"(LONG|SHORT|CLOSE|PASS|BUY|SELL|HOLD)"', upper)
    if m:
        v = m.group(1)
        return {"LONG": "BUY", "SHORT": "SELL", "PASS": "HOLD"}.get(v, v)
    if "BUY" in upper or "LONG" in upper:
        return "BUY"
    if "SELL" in upper or "SHORT" in upper:
        return "SELL"
    return "HOLD"


def _infer_confidence_from_text(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', text)
    if m:
        try:
            return max(0.0, min(1.0, float(m.group(1))))
        except ValueError:
            return 0.0
    return 0.0

logger = logging.getLogger(__name__)


def _compute_indicators(candles: List[Candle]) -> Dict[str, Any]:
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
    start_time = int(time.time() * 1000) - 86_400_000
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
NEWS_FRESHNESS_DAYS = 2


def _fetch_news(coin: str) -> str:
    """Recent (last NEWS_FRESHNESS_DAYS) news headlines for a coin via the
    Brave Search API.

    Returns a compact ' | '-joined headline string, or 'no news' when no
    BRAVE_API_KEY is set or the request fails — news is a supplementary
    signal, so a fetch failure degrades gracefully and never blocks research.
    """
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        return "no news"
    # Brave `freshness` takes a YYYY-MM-DDtoYYYY-MM-DD range; a 2-day window
    # approximates "within 48h" (the closest the API offers to an hour-precise
    # bound without per-result age filtering).
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=NEWS_FRESHNESS_DAYS)
    freshness = f"{start.isoformat()}to{today.isoformat()}"
    try:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/news/search",
            params={"q": f"{coin} crypto", "count": 5, "freshness": freshness},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=10.0,
        )
        if not resp.is_success:
            return "no news"
        results = resp.json().get("results", []) or []
        headlines = [str(r.get("title", "")).strip() for r in results if r.get("title")]
        return " | ".join(headlines[:5]) if headlines else "no news"
    except Exception:
        return "no news"


def _signals_block(coin: str) -> str:
    """Free positioning signals (GEX / aggTrades whale / FINRA short-vol / news
    catalyst) formatted for the AI prompt. Fetches all independent sources in
    parallel so cold-cache latency is max(T) instead of sum(T)."""
    is_hip3 = ":" in (coin or "")
    lines: List[str] = []
    try:
        skip_news = _should_skip_news()

        def _fetch_gex():
            if not is_hip3:
                return None
            from hermes_trader.agents.options_gex import gex_signal_cached
            return gex_signal_cached(coin)

        def _fetch_short_vol():
            if not is_hip3:
                return None
            from hermes_trader.agents.short_volume import short_volume_signal
            return short_volume_signal(coin)

        def _fetch_whale():
            if is_hip3:
                return None
            from hermes_trader.agents.crypto_whale import crypto_whale_signal
            return crypto_whale_signal(coin, window_minutes=15)

        def _fetch_catalyst():
            if skip_news:
                return None
            from hermes_trader.agents.news_catalyst import catalyst_scan
            base = coin.split(":", 1)[1] if ":" in coin else coin
            return catalyst_scan(base, timespan="1h")

        # All sources are independent (different APIs, separate caches).
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_gex = pool.submit(_fetch_gex)
            f_sv = pool.submit(_fetch_short_vol)
            f_whale = pool.submit(_fetch_whale)
            f_cat = pool.submit(_fetch_catalyst)
            g = f_gex.result()
            sv = f_sv.result()
            w = f_whale.result()
            n = f_cat.result()

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
    perception: Dict[str, Any],
    tf1h: Dict[str, Any],
    tf4h: Dict[str, Any],
    tf1d: Dict[str, Any],
    funding_rate: str,
    news: str,
    equity: float,
    open_positions: List[Dict[str, Any]],
    mode: str,
    dex_equity: Dict[str, float] | None = None,
    recent_candles: List[Candle] | None = None,
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

    def _indicator_block(label: str, snap: Dict[str, Any]) -> str:
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
    def _ohlc_block(candles: List[Candle] | None, n: int = 12) -> str:
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
        'Respond with 3-5 bullet points of reasoning, then output your decision as VALID JSON on the very last line:',
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
    """Call the AI for analysis — tries HTA resident service first (B1/B5),
    falls back to direct OpenRouter call.

    When HTA is enabled in the shared config, the request is routed to the
    HTA FastAPI multi-agent pipeline for a deeper multi-analyst debate.
    If HTA is unavailable or disabled, the existing OpenRouter path is used.
    A ``DEGRADED`` marker is appended when falling back so downstream
    telemetry can distinguish the two paths.
    """
    # Try HTA resident service first (B1/B5)
    cfg = _load_shared_config()
    hta_cfg = cfg.get("hta_service", {})
    if hta_cfg.get("enabled", True):
        # Extract ticker from user_message (first line after "Candidate:")
        ticker = ""
        for line in user_message.split("\n"):
            if line.startswith("Candidate:"):
                ticker = line.split("Candidate:")[-1].strip().split(" ")[0]
                break
        if ticker:
            analysis_date = today_utc_str()
            hta_result = _call_hta_service(
                ticker, analysis_date, system_prompt, user_message, trace_id=trace_id
            )
            if hta_result:
                return hta_result
            logger.info(
                f"[research] HTA unavailable for {ticker}, falling back to OpenRouter "
                f"(trace_id={trace_id})"
            )
            degraded_marker = "\n[DEGRADED: legacy_openrouter]\n"
        else:
            degraded_marker = ""
    else:
        degraded_marker = "\n[DEGRADED: hta_disabled]\n"

    # Fallback: direct OpenRouter call
    result = _call_openrouter(system_prompt, user_message)
    return result + degraded_marker if (degraded_marker and result) else result


def _call_openrouter(system_prompt: str, user_message: str) -> str:
    """Call the LLM API via OpenAI-compatible endpoint (synchronous httpx).

    Supports OpenRouter, Volcengine Ark, or any OpenAI-compatible gateway via:
      OPENROUTER_API_KEY  — API key
      OPENROUTER_MODEL    — model name (default: deepseek-v4-flash)
      OPENROUTER_BASE_URL — base URL without /chat/completions
                            (default: https://openrouter.ai/api/v1)
    """
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("OPENROUTER_MODEL", "deepseek-v4-flash")
    base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    if not openrouter_key:
        logger.warning("[research] OPENROUTER_API_KEY not set — returning empty response")
        return ""

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {openrouter_key}"}

    def _post(max_toks: int):
        return httpx.post(
            url,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "stream": False,
                "max_tokens": max_toks,
                "temperature": 0.1,
            },
            headers=headers,
            timeout=120.0,
        )

    try:
        resp = _post(2048)
        if resp.status_code == 402:
            m = re.search(r"can only afford (\d+)", resp.text or "")
            if m and int(m.group(1)) >= 500:
                budget = int(m.group(1)) - 50
                logger.warning(
                    f"[research] 402 with affordability hint — retrying DEGRADED "
                    f"at max_tokens={budget}"
                )
                resp = _post(budget)

        if resp.is_success:
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            logger.error("[research] LLM returned 200 but no choices — empty response")
            return ""
        body = resp.text[:200] if resp.text else ""
        logger.error(
            f"[research] LLM call FAILED: HTTP {resp.status_code} — {body}"
        )
        return ""
    except Exception as e:
        logger.error(f"[research] LLM call EXCEPTION: {e}")
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


def _parse_nlp_verdict(text: str) -> Optional[Dict[str, Any]]:
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


def parse_verdict(
    ai_text: str,
    coin: str,
    perception: Dict[str, Any],
    atr_abs: Optional[float] = None,
    sl_atr_mult: float = 1.2,
    tp_atr_mult: float = 1.0,
) -> Dict[str, Any]:
    """Parse the AI response: JSON on the last line, with a regex fallback.

    `atr_abs` (absolute ATR in price units, 4h) enables the stop/target
    fallback: the LLM routinely omits stopPx/tpPx or returns 0, which leaves
    the analysis record with no auditable risk plan. When that happens on a
    LONG/SHORT we derive them from ATR rather than propagating zeros.
    """
    if not ai_text:
        ai_text = ""

    verdict = "PASS"
    confidence = 0.0
    side = None
    entry_px = perception.get("mid", 0)
    stop_px = 0.0
    tp_px = 0.0
    news_risk = "none"
    reasoning = ai_text.strip()
    # True once a structured JSON verdict block was successfully decoded. A
    # decoded {"verdict":"PASS","confidence":0} is a real AI opinion, whereas
    # unparseable text is an error — the executor's zero-confidence guard needs
    # to tell those apart and confidence alone cannot.
    json_parsed = False

    lines = ai_text.strip().split("\n")

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

    # Stop/target repair for directional verdicts. Two failure modes seen in
    # production: the LLM omits stopPx/tpPx entirely (73/73 analyses on
    # 2026-08-19 carried stop_px=0.0), or it returns a stop on the wrong side
    # of entry. Both leave the record with no auditable risk plan and remove
    # the executor's only bracket fallback for the atr<=0 path. Derive from
    # ATR when we have it; drop an inverted stop either way.
    if verdict in ("LONG", "SHORT"):
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
                stop_px = (entry_ref - atr_ref * sl_atr_mult) if is_long else (entry_ref + atr_ref * sl_atr_mult)
                stop_px = max(0.0, stop_px)
                logger.info(
                    f"[research] {coin} {verdict}: stop_px missing from AI — "
                    f"ATR fallback {stop_px:.6g} (entry={entry_ref:.6g} atr={atr_ref:.6g} x{sl_atr_mult})"
                )
            if tp_px <= 0:
                tp_px = (entry_ref + atr_ref * tp_atr_mult) if is_long else (entry_ref - atr_ref * tp_atr_mult)
                tp_px = max(0.0, tp_px)
                logger.info(
                    f"[research] {coin} {verdict}: tp_px missing from AI — "
                    f"ATR fallback {tp_px:.6g} (entry={entry_ref:.6g} atr={atr_ref:.6g} x{tp_atr_mult})"
                )
        elif stop_px <= 0:
            logger.warning(
                f"[research] {coin} {verdict}: no AI stop and no ATR "
                f"(entry={entry_ref} atr={atr_abs}) — risk plan left empty"
            )

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


def research(coin: str, perception: Dict[str, Any], *, account_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Full AI research pipeline for a perception — returns an analysis dict.

    Args:
        account_snapshot: Pre-fetched account state from the current scan cycle.
            When provided, skips the per-coin fetch_account_state() call to avoid
            N duplicate HTTP POSTs per cycle. When None, fetches its own.
    """
    as_of_date = _compute_as_of_date()
    # Parallel fetch for all independent pre-LLM data: 3 candle timeframes +
    # funding rate + news + positioning signals. None depend on each other,
    # so issue them together to collapse serial latency into max(T).
    skip_news_flag = _should_skip_news()
    # Per-fetch timeout for the parallel pre-LLM data gather. Each individual
    # HTTP call already has its own sub-timeout (hl_client: 5s+3 retries;
    # funding/news/signals: their own), but if ANY of them hangs without
    # raising, f.result() with no timeout blocks the whole research pipeline
    # indefinitely — which is enough to trip the 600s watchdog during a
    # 3-trigger cycle. Bound the gather and bail fast on a stuck future.
    _fetch_timeout = float(os.environ.get("HERMES_RESEARCH_FETCH_TIMEOUT_S", "45"))
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_1h = pool.submit(fetch_hl_candles, coin, "1h", 100)
        f_4h = pool.submit(fetch_hl_candles, coin, "4h", 100)
        f_1d = pool.submit(fetch_hl_candles, coin, "1d", 60)
        f_funding = pool.submit(_fetch_funding_rate, coin)
        f_news = pool.submit(
            lambda: "news handled by HTA (B9 boundary)" if skip_news_flag else _fetch_news(coin)
        )
        f_signals = pool.submit(_signals_block, coin)
        try:
            c1h = f_1h.result(timeout=_fetch_timeout)
            c4h = f_4h.result(timeout=_fetch_timeout)
            c1d = f_1d.result(timeout=_fetch_timeout)
            funding_raw = f_funding.result(timeout=_fetch_timeout)
            news = f_news.result(timeout=_fetch_timeout)
            signals_block = f_signals.result(timeout=_fetch_timeout)
        except Exception as _e:
            # Cancel anything still running so a stuck HL/LLM call can't leak
            # a thread; raise so the caller's per-coin try/except logs the
            # failure and the trading loop moves on to the next trigger.
            for _f in (f_1h, f_4h, f_1d, f_funding, f_news, f_signals):
                if not _f.done():
                    _f.cancel()
            raise RuntimeError(
                f"parallel data-fetch for {coin} failed/timed out after {_fetch_timeout:.0f}s: {type(_e).__name__}: {_e}"
            ) from _e

    # Thin-history guard: multi-timeframe TA is meaningless without enough 4h
    # bars (EMA21/ADX need history). A near-empty series produced confident-
    # looking but baseless entries (e.g. WLD entered at 0.68 conf on "0 candles"
    # then ran straight to the stop). Decline outright — PASS, no LLM call, no entry.
    if len(c4h) < 30:
        logger.warning(f"[research] thin 4h history for {coin}: only {len(c4h)} candles — PASS (skip)")
        analysis = {
            "id": str(uuid.uuid4()), "perception_id": perception.get("id", "unknown"),
            "coin": coin, "verdict": "PASS", "confidence": 0.0, "side": None,
            "entry_px": perception.get("mid", 0), "stop_px": 0.0, "tp_px": 0.0,
            "reasoning": f"insufficient 4h history ({len(c4h)} candles) for reliable multi-TF TA",
            "news_context": news, "news_risk": "none",
            "created_at": int(time.time() * 1000),
            "composite_score": float(perception.get("composite_score", 0) or 0),
            "momentum_burst_fired": False, "slow_burn_fired": False,
            "slow_burn_count": 0,
            "daily_mover_fired": any(
                t.get("name") == "dailyMover" and t.get("fired")
                for t in (perception.get("triggers") or [])
            ),
            "whale_signal": perception.get("whale_signal"),
        }
        memory.record_analysis(analysis)
        return analysis

    tf1h = _compute_indicators(c1h)
    tf4h = _compute_indicators(c4h)
    tf1d = _compute_indicators(c1d)

    config = read_agent_config()
    mode = str(config.get("mode", "OFF"))

    equity = 0.0
    dex_equity: Dict[str, float] = {}
    open_positions: List[Dict[str, Any]] = []
    user = resolve_user_address()

    if user:
        # Use cycle-level snapshot when provided by trading_loop to avoid
        # re-fetching account state for every coin (N × (2+M) POSTs/cycle).
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

    ai_text = _call_ai(system_prompt, user_message, trace_id=trace_id)
    # Pass the 4h ATR so parse_verdict can synthesise a stop/target when the
    # LLM omits them (it usually does). 4h matches the timeframe the executor
    # uses for its own bracket, so the two agree.
    _atr_4h = tf4h.get("atr14")
    parsed = parse_verdict(
        ai_text, coin, perception,
        atr_abs=_atr_4h,
        sl_atr_mult=float(config.get("sl_atr_mult", 1.2) or 1.2),
    )

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
        "as_of_date": as_of_date,
        "created_at": int(time.time() * 1000),
        # Carry forward so risk gates can read own-coin signal strength.
        "composite_score": float(perception.get("composite_score", 0) or 0),
        "momentum_burst_fired": any(
            t.get("name") == "momentumBurst" and t.get("fired")
            for t in (perception.get("triggers") or [])
        ),
        "slow_burn_fired": any(
            t.get("name") in ("volumeBuildup1h", "trendFlip1h", "higherLows1h")
            and t.get("fired")
            for t in (perception.get("triggers") or [])
        ),
        "slow_burn_count": sum(
            1 for t in (perception.get("triggers") or [])
            if t.get("name") in ("volumeBuildup1h", "trendFlip1h", "higherLows1h")
            and t.get("fired")
        ),
        # O'Neil breakout pair — feeds the breakout force-execute (a hedged AI
        # PASS on a 20-period-high break WITH a volume surge gets upgraded;
        # XPL +32% 2026-06-12 was researched 38x, PASSed 21x, never traded
        # while both of these were fired hours before the move).
        "breakout_fired": any(
            t.get("name") == "breakout" and t.get("fired")
            for t in (perception.get("triggers") or [])
        ),
        "volume_spike_fired": any(
            t.get("name") == "volumeSpike" and t.get("fired")
            for t in (perception.get("triggers") or [])
        ),
        "uptrend_momentum_fired": any(
            t.get("name") == "uptrendMomentum" and t.get("fired")
            for t in (perception.get("triggers") or [])
        ),
        "downtrend_momentum_fired": any(
            t.get("name") == "downtrendMomentum" and t.get("fired")
            for t in (perception.get("triggers") or [])
        ),
        "daily_mover_fired": any(
            t.get("name") == "dailyMover" and t.get("fired")
            for t in (perception.get("triggers") or [])
        ),
        # OI+funding accumulation signal (oi_funding_anomaly). When present,
        # the coin shows whale-loading patterns (high OI, negative funding,
        # flat price). Used as a counter-regime bypass for LONGs.
        "whale_signal": perception.get("whale_signal"),
        # Fired indicator names — used by the executor's structural-override
        # diagnostics log so a post-mortem can see exactly which TA/slow-burn
        # triggers upgraded a PASS to LONG.
        "fired_triggers": [
            t.get("name") for t in (perception.get("triggers") or [])
            if t.get("fired") and t.get("name")
        ],
    }

    memory.record_analysis(analysis)
    return analysis
