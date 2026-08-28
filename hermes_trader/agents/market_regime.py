"""Per-asset-class market regime detection — feeds the market_regime risk gate.

Classifies each coin into crypto / equity / commodity, then picks the right
trend proxy:

  crypto    → BTC 4h trend (everything in crypto correlates to BTC)
  equity    → NVDA 4h trend (most-liquid HL single-stock perp; proxy for
              risk-on/off in the tradfi single-stock basket; switch via
              EQUITY_PROXY)
  commodity → the coin's own 4h trend (commodities aren't correlated to each
              other — gas, silver, copper, oil all move on their own drivers)

Trend itself is EMA20 vs EMA30 on 1h closes, with a short slope check on the
fast EMA so we don't whipsaw at the cross. Four states: 'up', 'down',
'neutral' (no strong direction → gate stays out of the way), and 'chop'
(ADX(14) < 20 on the same proxy — directionless whipsaw; the gate raises the
conviction bar rather than free-passing like 'neutral').

A note on 'neutral' vs 'chop': both mean "no clear trend", but 'neutral' is
the EMA/slope verdict (could be an early cross or quiet tape) while 'chop' is
confirmed low-ADX noise. The market_regime_gate historically free-passed
'neutral', which let fakeout entries through in chop; 'chop' is the
explicit "be picky here" signal.

Regimes are cached per-proxy for `REGIME_TTL_S` (default 10 min) so the gate
doesn't re-fetch candles for every trade attempt in a scan cycle.
"""
from __future__ import annotations

import logging
import time
from typing import Literal, Optional

from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.indicators.math import adx, atr as _atr_ind, ema, obv as _obv_ind

logger = logging.getLogger(__name__)

AssetClass = Literal["crypto", "equity", "commodity"]
Regime = Literal["up", "down", "neutral", "chop"]

# ---------------------------------------------------------------------------
# 5-component continuous trend-strength score (byte-aligned with
# scripts/backtest_ab_compare._regime_score). Production's 4-state classifier
# (EMA20/50 + ADX chop overlay) has no strength component, so the audit
# (scripts/audit_neutral_chop.py) measured a 36% false-trend rate: the EMA
# cross fires on weak/noise bars that the backtest's score correctly rates
# NEUTRAL/CHOP. We compute the same continuous score here so the risk gate can
# require score >= 0.55 to clear an "aligned" entry.
# 5-component continuous trend-strength score weights (byte-aligned with
# scripts/backtest_ab_compare._regime_score). Shared by executor.py's Plan B
# sizing — do not duplicate; import REGIME_WEIGHTS from this module.
REGIME_WEIGHTS = {
    "adx": 0.25,
    "atr": 0.225,
    "ema_align": 0.175,
    "price_ext": 0.175,
    "obv": 0.175,
}


def _obv_slope_sign(candles: list, period: int = 10) -> int:
    """+1 rising OBV, -1 falling, 0 flat/insufficient — mirrors backtest."""
    if len(candles) < period + 1:
        return 0
    obv_arr = _obv_ind(candles)
    recent = obv_arr[-period:]
    xbar = (len(recent) - 1) / 2
    ybar = sum(recent) / len(recent)
    num = sum((i - xbar) * (y - ybar) for i, y in enumerate(recent))
    if num > 0:
        return 1
    if num < 0:
        return -1
    return 0


def regime_strength_score(candles: list) -> float:
    """Continuous [0, 1] trend-strength score from 1h candles.

    Direction-agnostic — measures how strongly price is trending whichever way
    the EMA8/21 cross points. Components and weights are byte-for-byte the
    backtest reference (see _REGIME_WEIGHTS). Returns 0.0 on insufficient
    data; callers should treat low/0 as "not a confirmed trend"."""
    if not candles or len(candles) < 50:
        return 0.0
    closes = [float(c.c) for c in candles]
    try:
        e8_arr = ema(closes, 8)
        e21_arr = ema(closes, 21)
        if len(e8_arr) < 1 or len(e21_arr) < 1:
            return 0.0
        e8 = e8_arr[-1]
        e21 = e21_arr[-1]
        close = closes[-1]
        atr_arr = _atr_ind(candles, 14)
        atr_v = next((v for v in reversed(atr_arr)
                      if v == v and v != float("inf")), None)
        adx_arr = adx(candles, 14)
        adx_v = next((v for v in reversed(adx_arr)
                      if v == v and v != float("inf")), None)
    except Exception as e:
        logger.debug(f"[regime] score indicator calc failed: {e}")
        return 0.0
    if atr_v is None or adx_v is None or not e21 or not close:
        return 0.0

    bullish = e8 > e21
    # ADX 15 -> 0, 45 -> 1
    adx_c = max(0.0, min(1.0, (adx_v - 15.0) / 30.0))
    # ATR% 0.2% -> 0, 1.0% -> 1
    atr_pct = atr_v / close * 100
    atr_c = max(0.0, min(1.0, (atr_pct - 0.2) / 0.8)) if atr_v else 0.0
    # |EMA8-EMA21| gap%: 0% -> 0, 0.5% -> 1
    ema_c = 0.0
    if e21 > 0:
        gap_pct = abs(e8 - e21) / e21 * 100
        ema_c = max(0.0, min(1.0, gap_pct / 0.5))
    # Price vs EMA21 in ATR units: 0 -> 0, 2.0 ATR -> 1
    ext_c = 0.0
    if atr_v > 0 and e21:
        ext = abs((close - e21) / atr_v)
        ext_c = max(0.0, min(1.0, ext / 2.0))
    # OBV: aligned = 1.0, flat = 0.3, opposing = 0.0
    obv_dir = _obv_slope_sign(candles)
    if (bullish and obv_dir > 0) or (not bullish and obv_dir < 0):
        obv_c = 1.0
    elif obv_dir == 0:
        obv_c = 0.3
    else:
        obv_c = 0.0

    w = REGIME_WEIGHTS
    score = (w["adx"] * adx_c + w["atr"] * atr_c + w["ema_align"] * ema_c
             + w["price_ext"] * ext_c + w["obv"] * obv_c)
    return max(0.0, min(1.0, score))

# HL single-stock perps. Curated rather than auto-discovered because the
# universe shifts and we want a stable classifier — adds latency to no new
# coin, just a one-line update when HL lists more.
_EQUITY_COINS = frozenset([
    # Single-name equities
    "TSLA", "NVDA", "AAPL", "MSFT", "GOOGL", "GOOG", "META", "AMZN",
    "NFLX", "AMD", "INTC", "SPY", "QQQ", "COIN", "MSTR", "HOOD", "PLTR",
    "DIS", "JPM", "BA", "WMT", "XOM", "MU", "ARM", "BABA", "SKHX",
    # Broad-market indices (HIP-3: xyz:SP500, xyz:XYZ100, km:US500, km:USTECH, km:SMALL2000)
    "SP500", "US500", "XYZ100", "USTECH", "QQQ", "DJI", "NDX", "SMALL2000", "USENERGY",
])

# HL commodity perps — names vary across the API; cover the obvious aliases
# including HIP-3 namespaced equivalents (xyz:CL = crude, xyz:BRENTOIL, km:USOIL, etc.).
_COMMODITY_COINS = frozenset([
    "NATGAS", "GAS", "NGAS",
    "OIL", "USOIL", "BRENT", "BRENTOIL", "WTI", "CL",
    "GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM", "ALUMINIUM",
])

# Foreign / non-US-correlated stock indices. These trade on their own session
# and drivers (Korea, Japan, HK, Europe, India, Australia) and do NOT track the
# US SP500 proxy — gating them by US equity regime made them perennial losers
# (e.g. xyz:KR200 = KOSPI 200, repeatedly "trend-aligned with US-up" yet falling).
# Classified to use their OWN 4h trend (the commodity/own-trend path) instead.
_FOREIGN_INDICES = frozenset([
    "KR200", "KOSPI", "KOSPI200",
    "JP225", "NIKKEI", "N225",
    "HSI", "HANGSENG", "HK50",
    "DAX", "DAX40", "FTSE", "FTSE100", "CAC", "CAC40", "STOXX", "STOXX50", "ESTX50",
    "ASX", "ASX200", "SENSEX", "NIFTY", "NIFTY50",
])

CRYPTO_PROXY = "BTC"
# HIP-3 tokenized equity perp — xyz:SP500 is the highest-volume broad-market
# proxy ($194M 24h vol). Only resolves when enable_hip3 is on; falls back to
# crypto proxy when the candle fetch returns nothing.
EQUITY_PROXY = "xyz:SP500"

# Trend thresholds on 1h closes — 8 bars = 8h lookback so intraday
# rotations are caught (a slower 4h × 5-bar = 20h window was missing
# every relief rally and pinning regime to BTC's macro drift).
#
# Sensitivity raised 0.2%→0.1% (2026-05): a soft/chop tape was reading
# 'neutral' all day, and a neutral regime is a FREE PASS in market_regime_gate
# — so the gate gave zero trend discipline exactly when the book was filling
# with counter-trend dip-buy longs. A bearish EMA20<EMA50 cross that is only
# gently sloped is still a real downtrend; we want it to register as 'down' so
# counter-trend longs face the gate. Flat (slope≈0) still reads neutral.
#
# Re-calibrated 2026-08 (scripts/calibrate_regime_thresholds.py, 30d/20 coins,
# 15k windows): slope 0.1%→0.2% and EMA50→EMA30 cut the false-trend rate
# (prod=trend | bt=NEUTRAL/CHOP) from 36.3%→27.7% and missed-trend from
# 26.1%→19.4%, while keeping a real fast/slow cross (unlike the degenerate
# 20/21 the grid optimizer also found). On 1h candles EMA30 ≈ 1.25d, better
# matched to the 1h trend horizon than the old EMA50 (≈2d) carried over from
# the 4h design.
_SLOPE_LOOKBACK = 8
_SLOPE_UP = 0.002       # +0.2% over 8 bars → 'up'  (DEFAULT; overridable via config)
_SLOPE_DOWN = -0.002    # -0.2% over 8 bars → 'down' (DEFAULT; overridable via config)

# ADX(14) below this on the proxy 1h series → 'chop' (directionless whipsaw).
# ADX<20 is the classic Wilder "no trend" threshold; the gate then raises the
# conviction bar instead of free-passing like 'neutral'.
_CHOP_ADX_MAX = 20.0

# EMA periods (DEFAULTS; overridable via the regime_classifier config block).
_FAST_EMA = 20
_SLOW_EMA = 30

REGIME_TTL_S = 300  # 5min cache — 1h trends can flip faster than 4h

_regime_cache: dict[str, tuple[Regime, float]] = {}
# Continuous 5-component trend-strength score (0..1) keyed by the same proxy as
# _regime_cache. Populated alongside the regime on the same candle fetch so the
# risk gate can read score>=0.55 without a second network call.
_score_cache: dict[str, tuple[float, float]] = {}

# Bare tickers that trade as native (main-dex) HL perps — authoritatively
# crypto. Built once from the universe and cached; lets `hyna:BTC`, `cash:ETH`,
# `flx:XMR` etc. resolve to crypto by their bare ticker rather than being
# swept into the HIP-3 equity default below.
_crypto_tickers_cache: frozenset[str] | None = None


def _native_crypto_tickers() -> frozenset[str]:
    global _crypto_tickers_cache
    if _crypto_tickers_cache is None:
        try:
            from hermes_trader.client.universe import get_universe
            uni = get_universe()  # main dex only — every perp here is crypto
            tickers = frozenset(
                m["coin"].upper() for m in uni
                if m.get("type") == "perp" and ":" not in m.get("coin", "")
            )
            if tickers:  # only cache a real result; retry next call on failure
                _crypto_tickers_cache = tickers
            return tickers
        except Exception:
            return frozenset()
    return _crypto_tickers_cache


def classify_asset(coin: str) -> AssetClass:
    """Map a coin to its asset class, picking the trend proxy + funding-regime
    bucket. Resolution is by BARE ticker (the dex prefix is stripped), because
    HIP-3 venues are mixed: `xyz:`/`km:` are tokenized stocks/commodities, but
    `hyna:`/`cash:`/`flx:` also list crypto (`hyna:BTC`, `cash:ETH`).

    Order:
      1. commodity allowlist  → commodity
      2. equity allowlist     → equity
      3. native HL perp ticker (e.g. BTC, LINK, FARTCOIN) → crypto, so
         `hyna:LINK` is gated by BTC, not SP500
      4. any OTHER HIP-3 namespaced coin → equity — a tokenized stock the
         allowlist doesn't enumerate (xyz:SNDK, xyz:CBRS). The old code
         defaulted these to crypto and gated SanDisk by BTC's trend.
      5. bare unknown (no dex prefix) → crypto (main dex is all crypto)
    """
    raw = (coin or "")
    namespaced = ":" in raw
    bare = raw.split(":", 1)[-1].upper() if namespaced else raw.upper()
    if bare in _COMMODITY_COINS:
        return "commodity"
    if bare in _FOREIGN_INDICES:
        # Own-trend (commodity path): a foreign index follows its own market, not
        # the US SP500 proxy used for the `equity` class.
        return "commodity"
    if bare in _EQUITY_COINS:
        return "equity"
    if bare in _native_crypto_tickers():
        return "crypto"
    if namespaced:
        return "equity"
    return "crypto"


def _classifier_params() -> tuple[int, int, float, float]:
    """Return (fast_ema, slow_ema, slope_up, chop_adx_max) from the live
    `regime_classifier` config block, falling back to the calibrated module
    defaults when the block/keys are absent. Lets the weekly calibration job
    retune thresholds via .agent-config.json without a code deploy. The config
    is read on each (cache-missed) classification, but the regime itself is
    cached for REGIME_TTL_S so this never gets hot."""
    fast, slow, slope_up, adx_max = _FAST_EMA, _SLOW_EMA, _SLOPE_UP, _CHOP_ADX_MAX
    try:
        from hermes_trader.agents.config_store import read_agent_config
        cfg = (read_agent_config().get("regime_classifier") or {})
        fast = int(cfg.get("fast_ema", fast))
        slow = int(cfg.get("slow_ema", slow))
        slope_up = float(cfg.get("slope_threshold", slope_up))
        adx_max = float(cfg.get("chop_adx_max", adx_max))
        if fast >= slow:  # guard against a malformed config disabling the cross
            fast, slow = _FAST_EMA, _SLOW_EMA
    except Exception as e:  # config must never break regime detection
        logger.debug(f"[regime] classifier params config read failed: {e}")
    return fast, slow, slope_up, adx_max


def trend_from_closes(closes: list[float],
                      fast_p: Optional[int] = None,
                      slow_p: Optional[int] = None,
                      slope_up: Optional[float] = None) -> Regime:
    """EMA fast/slow + slope on raw closes — canonical trend classifier.

    Public API shared by production (`_trend_from_closes`) and the backtest
    scripts (previously byte-level copies in `backtest_ab_compare.py` and
    `backtest_full.py`). Parameters fall back to the live `regime_classifier`
    config block (and ultimately to the calibrated module defaults EMA20/30,
    0.2% over 8 bars). Pass explicit params for historical replay.

    Returns 'neutral' if the series is too short or the move isn't clear
    enough either way (the deliberate sit-out case). Does NOT detect 'chop'
    — use `classify_candles` for the ADX overlay.
    """
    if fast_p is None or slow_p is None or slope_up is None:
        d_fast, d_slow, d_slope, _ = _classifier_params()
        fast_p = fast_p if fast_p is not None else d_fast
        slow_p = slow_p if slow_p is not None else d_slow
        slope_up = slope_up if slope_up is not None else d_slope
    if len(closes) < slow_p:
        return "neutral"
    fast = ema(closes, fast_p)
    slow = ema(closes, slow_p)
    if len(fast) < _SLOPE_LOOKBACK + 1 or len(slow) < 1:
        return "neutral"
    f_now, s_now = fast[-1], slow[-1]
    f_prev = fast[-(_SLOPE_LOOKBACK + 1)]
    if f_prev == 0:
        return "neutral"
    slope = (f_now - f_prev) / abs(f_prev)
    if f_now > s_now and slope > slope_up:
        return "up"
    if f_now < s_now and slope < -slope_up:
        return "down"
    return "neutral"


def classify_candles(candles: list,
                     fast_p: Optional[int] = None,
                     slow_p: Optional[int] = None,
                     slope_up: Optional[float] = None,
                     adx_max: Optional[float] = None) -> Regime:
    """Full regime classification from candles with explicit params.

    Same logic as `_classify_candles` but with overridable parameters so
    backtests can replay historical data without touching the live config.
    When params are None, falls back to the `regime_classifier` config block.
    """
    if not candles:
        return "neutral"
    closes = [float(c.c) for c in candles]
    trend = trend_from_closes(closes, fast_p=fast_p, slow_p=slow_p,
                              slope_up=slope_up)
    if trend != "neutral":
        return trend
    _f, _s, _slope, d_adx = _classifier_params()
    if adx_max is None:
        adx_max = d_adx
    try:
        adx_arr = adx(candles, 14)
        last_adx = next(
            (v for v in reversed(adx_arr) if v == v and v != float("inf")),
            None,
        )
        if last_adx is not None and last_adx < adx_max:
            return "chop"
    except Exception as e:
        logger.debug(f"[regime] ADX chop check failed (non-fatal): {e}")
    return "neutral"


def _classify_candles(candles: list) -> Regime:
    """Internal wrapper — uses live config params (see classify_candles)."""
    return classify_candles(candles)


def _detect_for_proxy(proxy: str) -> Regime:
    """Network path — fetch candles for `proxy`, compute regime.
    Wrapped by `detect_regime` for caching."""
    try:
        candles = fetch_hl_candles(proxy, interval="1h", count=100)
        if not candles:
            return "neutral"
        return _classify_candles(candles)
    except Exception as e:
        logger.warning(f"[regime] candle fetch failed for {proxy}: {e}")
        return "neutral"


def _detect_for_proxy_with_score(proxy: str) -> tuple[Regime, float]:
    """Same candle fetch as _detect_for_proxy but returns (regime, score).
    Populates _score_cache so a subsequent detect_regime() reuses it."""
    try:
        candles = fetch_hl_candles(proxy, interval="1h", count=100)
        if not candles:
            return "neutral", 0.0
        return _classify_candles(candles), regime_strength_score(candles)
    except Exception as e:
        logger.warning(f"[regime] candle fetch failed for {proxy}: {e}")
        return "neutral", 0.0


def detect_regime_with_score(coin: str, *, force: bool = False) -> tuple[Regime, float]:
    """Like detect_regime but also returns the continuous 5-component strength
    score (0..1) for the same proxy candles. The risk gate uses score>=0.55 as
    a strictness overlay on "aligned" entries to cut the 36% false-trend rate
    measured by scripts/audit_neutral_chop.py. Cached for REGIME_TTL_S."""
    klass = classify_asset(coin)
    if klass == "commodity":
        proxy = coin if ":" in coin else coin.upper()
    elif klass == "equity":
        now = time.time()
        cached_own = _regime_cache.get(coin)
        if not force and cached_own and (now - cached_own[1]) < REGIME_TTL_S:
            if cached_own[0] not in ("neutral", "chop"):
                sc = _score_cache.get(coin, (0.0, 0.0))[0]
                return cached_own[0], sc
        else:
            own, own_score = _detect_for_proxy_with_score(coin)
            ts = time.time()
            _regime_cache[coin] = (own, ts)
            _score_cache[coin] = (own_score, ts)
            if own not in ("neutral", "chop"):
                return own, own_score
        proxy = EQUITY_PROXY
    else:
        proxy = CRYPTO_PROXY

    now = time.time()
    cached = _regime_cache.get(proxy)
    if not force and cached and (now - cached[1]) < REGIME_TTL_S:
        sc = _score_cache.get(proxy, (0.0, 0.0))[0]
        return cached[0], sc
    regime, score = _detect_for_proxy_with_score(proxy)
    ts = time.time()
    _regime_cache[proxy] = (regime, ts)
    _score_cache[proxy] = (score, ts)
    return regime, score


def detect_regime(coin: str, *, force: bool = False) -> Regime:
    """Return the regime applicable to a trade in `coin`. Cached for TTL.

    Thin wrapper over detect_regime_with_score so a single candle fetch
    populates both the regime and strength-score caches.
    `force=True` bypasses the cache (used by tests + the operator console)."""
    regime, _score = detect_regime_with_score(coin, force=force)
    return regime


def regime_snapshot() -> dict[str, dict[str, object]]:
    """Operator-facing summary: every cached proxy + regime + score + cache age."""
    now = time.time()
    out: dict[str, dict[str, object]] = {}
    for proxy, (regime, ts) in _regime_cache.items():
        score = _score_cache.get(proxy, (0.0, 0.0))[0]
        out[proxy] = {"regime": regime, "score": round(score, 3),
                      "age_s": round(now - ts, 1)}
    return out
