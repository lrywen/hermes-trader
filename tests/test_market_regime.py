"""Audit 2026-09-06 (F3, engineering hygiene): table-driven behaviour tests for
hermes_trader.agents.market_regime.

The module's canonical-config registration and strength-score calibration are
already covered by test_r13_b5_regime_score_registration.py, and the basic
asset mapping / cache-hit happy paths live in test_cleanup.py. This file fills
the remaining *behavioural branch* gaps that previously had no test:

  * trend_from_closes — the STRICT slope threshold boundary (slope just above
    vs just below ±slope_up), explicit-vs-config parameter selection, the
    too-short-series guard and the divide-by-zero (f_prev == 0) guard.
  * classify_candles — empty input, trend-wins-over-chop ordering, the STRICT
    ADX(14) < adx_max chop boundary (ADX == adx_max is NOT chop), and the
    ADX-computation-raises → 'neutral' degradation path.
  * _classifier_params — live regime_classifier config overlay, the
    fast_ema >= slow_ema guard, and the read-failure → module-defaults path.
  * detect_regime_with_score — TTL expiry triggers a re-fetch, force=True
    bypasses a fresh cache, an equity coin whose OWN trend is neutral/chop
    falls back to the EQUITY_PROXY (xyz:SP500), a missing score-cache entry
    reads back as 0.0, and a candle-fetch exception degrades to
    ("neutral", 0.0).
  * classify_asset — foreign / non-US indices (KR200, DAX, NIKKEI, ...) map to
    the own-trend "commodity" path, allowlist priority over the namespaced
    equity default, and empty/None input.
  * regime_snapshot — cached proxies render with rounded score + age and a
    missing score entry defaults to 0.0.

Everything is network-free: candle fetches are stubbed and caches are seeded
or cleared explicitly. Test comments / assertions are English.
"""
import pytest

from hermes_trader.agents import market_regime as mr
from hermes_trader.indicators.math import ema
from hermes_trader.models.types import Candle


# ── helpers ─────────────────────────────────────────────────────────────

def _mk(t, o, h, l, c, v=1000.0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _flat_candles(n=100, price=100.0, rng=0.3):
    """Choppy/flat candles oscillating around `price` (low directional slope)."""
    out = []
    for i in range(n):
        s = 1.0 if i % 2 == 0 else -1.0
        c = price + s * rng
        out.append(_mk(i, c, c + rng, c - rng, c))
    return out


def _clear_caches():
    mr._regime_cache.clear()
    mr._score_cache.clear()


def _rising_closes(n=60, start=100.0, g=0.3):
    return [start + i * g for i in range(n)]


def _falling_closes(n=60, start=200.0, g=0.3):
    return [start - i * g for i in range(n)]


def _fast_slope(closes, fast_p, lookback=8):
    """Realised fast-EMA slope over `lookback` bars, mirroring trend_from_closes."""
    f = ema(closes, fast_p)
    f_now, f_prev = f[-1], f[-(lookback + 1)]
    return (f_now - f_prev) / abs(f_prev)


# ── 1. trend_from_closes: strict slope threshold boundary ───────────────

@pytest.mark.parametrize("fast_p,slow_p", [(20, 30), (10, 20)])
def test_trend_from_closes_up_threshold_boundary(fast_p, slow_p):
    """On a rising series f_now > s_now; 'up' fires only when slope > slope_up.
    A threshold just under the realised slope → 'up'; just over → 'neutral'."""
    closes = _rising_closes(n=slow_p + 30, g=0.3)
    slope = _fast_slope(closes, fast_p)
    assert slope > 0  # sanity: the series is genuinely rising
    # threshold BELOW the realised slope → up
    assert mr.trend_from_closes(closes, fast_p=fast_p, slow_p=slow_p,
                                slope_up=slope * 0.5) == "up"
    # threshold ABOVE the realised slope → neutral (strict > comparison)
    assert mr.trend_from_closes(closes, fast_p=fast_p, slow_p=slow_p,
                                slope_up=slope * 2.0) == "neutral"


@pytest.mark.parametrize("fast_p,slow_p", [(20, 30), (10, 20)])
def test_trend_from_closes_down_threshold_boundary(fast_p, slow_p):
    """On a falling series f_now < s_now; 'down' fires only when slope <
    -slope_up. A magnitude threshold just under |slope| → 'down'; just over →
    'neutral'."""
    closes = _falling_closes(n=slow_p + 30, g=0.3)
    slope = _fast_slope(closes, fast_p)
    assert slope < 0  # sanity: genuinely falling
    mag = abs(slope)
    # |threshold| below the realised magnitude → down
    assert mr.trend_from_closes(closes, fast_p=fast_p, slow_p=slow_p,
                                slope_up=mag * 0.5) == "down"
    # |threshold| above the realised magnitude → neutral (strict < comparison)
    assert mr.trend_from_closes(closes, fast_p=fast_p, slow_p=slow_p,
                                slope_up=mag * 2.0) == "neutral"


def test_trend_from_closes_flat_is_neutral_with_explicit_params():
    """A flat tape never crosses the slope threshold regardless of params."""
    closes = [100.0] * 60
    assert mr.trend_from_closes(closes, fast_p=5, slow_p=10,
                                slope_up=0.0001) == "neutral"


def test_trend_from_closes_too_short_returns_neutral():
    """Fewer closes than the slow EMA period → 'neutral' (no look-ahead)."""
    assert mr.trend_from_closes([100.0] * 29, fast_p=20, slow_p=30,
                                slope_up=0.002) == "neutral"
    assert mr.trend_from_closes([], fast_p=5, slow_p=10) == "neutral"


def test_trend_from_closes_zero_fprev_guard(monkeypatch):
    """A leading all-zero region makes the lookback-ago fast EMA 0; the divide
    must be guarded → 'neutral' instead of ZeroDivisionError."""
    # Force a short lookback so f_prev (fast EMA `lookback` bars ago) lands on
    # the last all-zero bar, while only the tail rises into nonzero territory.
    monkeypatch.setattr(mr, "_slope_lookback", lambda *, config=None: 3)
    closes = [0.0] * 50 + [1.0, 2.0, 3.0]  # f_prev = fast[-4] is still 0.0
    assert mr.trend_from_closes(closes, fast_p=3, slow_p=5,
                                slope_up=0.001) == "neutral"


# ── 2. classify_candles: chop boundary, ordering, degradation ───────────

def test_classify_candles_empty_returns_neutral():
    assert mr.classify_candles([]) == "neutral"


def test_classify_candles_trend_wins_over_chop(monkeypatch):
    """A clear trend must short-circuit before the ADX/chop overlay — even when
    ADX is low (which would otherwise read 'chop')."""
    monkeypatch.setattr(mr, "adx", lambda candles, period=14: [5.0] * len(candles))
    up = [_mk(i, 100 + i * 0.5, 100 + i * 0.5 + 0.8, 100 + i * 0.5 - 0.2,
              100 + i * 0.5, 1000 + i) for i in range(100)]
    assert mr.classify_candles(up, fast_p=5, slow_p=10,
                               slope_up=0.0001) == "up"


@pytest.mark.parametrize("adx_value,expected", [
    (19.9, "chop"),    # strictly below the 20.0 threshold → chop
    (20.0, "neutral"), # exactly AT the threshold: '<' is False → NOT chop
    (25.0, "neutral"), # above the threshold → neutral
])
def test_classify_candles_adx_chop_boundary(monkeypatch, adx_value, expected):
    """Trend is forced neutral via a huge slope threshold; the verdict then
    hinges on the STRICT last_adx < adx_max comparison."""
    candles = _flat_candles(100)
    n = len(candles)
    monkeypatch.setattr(mr, "adx",
                        lambda candles, period=14: [float("nan")] * (n - 1) + [adx_value])
    assert mr.classify_candles(candles, fast_p=5, slow_p=10,
                               slope_up=999.0, adx_max=20.0) == expected


def test_classify_candles_adx_exception_degrades_to_neutral(monkeypatch):
    """An ADX computation failure must be swallowed → 'neutral', never raise."""
    def _boom(candles, period=14):
        raise RuntimeError("adx exploded")
    monkeypatch.setattr(mr, "adx", _boom)
    assert mr.classify_candles(_flat_candles(100), fast_p=5, slow_p=10,
                               slope_up=999.0, adx_max=20.0) == "neutral"


# ── 3. _classifier_params: live config overlay + guards ─────────────────

def test_classifier_params_defaults():
    fast, slow, slope, adx = mr._classifier_params()
    assert (fast, slow) == (mr._FAST_EMA, mr._SLOW_EMA) == (20, 30)
    assert slope == mr._SLOPE_UP == 0.002
    assert adx == mr._CHOP_ADX_MAX == 20.0


def test_classifier_params_config_overlay(monkeypatch):
    from hermes_trader.agents import config_store
    monkeypatch.setattr(config_store, "read_agent_config", lambda: {
        "regime_classifier": {
            "fast_ema": 10, "slow_ema": 40,
            "slope_threshold": 0.005, "chop_adx_max": 25.0,
        }})
    assert mr._classifier_params() == (10, 40, 0.005, 25.0)


def test_classifier_params_fast_ge_slow_guard(monkeypatch):
    """A malformed fast_ema >= slow_ema (which would disable the EMA cross)
    resets the periods to the module defaults; slope/adx still come from cfg."""
    from hermes_trader.agents import config_store
    monkeypatch.setattr(config_store, "read_agent_config", lambda: {
        "regime_classifier": {
            "fast_ema": 50, "slow_ema": 20,
            "slope_threshold": 0.007, "chop_adx_max": 22.0,
        }})
    fast, slow, slope, adx = mr._classifier_params()
    assert (fast, slow) == (20, 30)
    assert slope == 0.007
    assert adx == 22.0


def test_classifier_params_read_failure_falls_back(monkeypatch):
    """Any config-read exception must be swallowed and return module defaults."""
    from hermes_trader.agents import config_store

    def _boom():
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(config_store, "read_agent_config", _boom)
    assert mr._classifier_params() == (20, 30, 0.002, 20.0)


# ── 4. detect_regime_with_score: cache TTL / force / proxy fallback ─────

def test_detect_crypto_uses_btc_proxy_and_caches(monkeypatch):
    """An alt coin classifies to crypto → proxy BTC; a second alt within TTL is
    a cache hit (no second fetch)."""
    _clear_caches()
    calls = []

    def _fake(proxy):
        calls.append(proxy)
        return "up", 0.9
    monkeypatch.setattr(mr, "_detect_for_proxy_with_score", _fake)

    assert mr.detect_regime("PEPE") == "up"
    assert mr.detect_regime("WIF") == "up"
    assert calls == ["BTC"]


def test_detect_cache_expiry_triggers_refetch(monkeypatch):
    """A cache entry older than the TTL is treated as a miss and re-fetched."""
    _clear_caches()
    calls = []

    def _fake(proxy):
        calls.append(proxy)
        return "down", 0.4
    monkeypatch.setattr(mr, "_detect_for_proxy_with_score", _fake)

    # Seed a STALE entry (far in the past, well past REGIME_TTL_S).
    mr._regime_cache["BTC"] = ("up", 1.0)
    mr._score_cache["BTC"] = (0.9, 1.0)
    assert mr.detect_regime("PEPE") == "down"
    assert calls == ["BTC"]


def test_detect_force_bypasses_fresh_cache(monkeypatch):
    """force=True re-fetches even when a fresh cached value is available."""
    _clear_caches()
    calls = []

    def _fake(proxy):
        calls.append(proxy)
        return ("down", 0.3) if len(calls) > 1 else ("up", 0.9)
    monkeypatch.setattr(mr, "_detect_for_proxy_with_score", _fake)

    assert mr.detect_regime("PEPE") == "up"       # miss → fetch #1
    assert mr.detect_regime("PEPE") == "up"       # fresh cache hit, no fetch
    assert mr.detect_regime("PEPE", force=True) == "down"  # bypass → fetch #2
    assert len(calls) == 2


def test_detect_equity_neutral_own_falls_back_to_equity_proxy(monkeypatch):
    """An equity coin whose OWN trend reads neutral/chop must fall through to
    the EQUITY_PROXY (xyz:SP500) instead of being gated by its own flat tape."""
    _clear_caches()
    calls = []

    def _fake(proxy):
        calls.append(proxy)
        # The equity's own trend is neutral; the broad-market proxy is 'up'.
        if proxy == mr.EQUITY_PROXY:
            return "up", 0.8
        return "neutral", 0.0
    monkeypatch.setattr(mr, "_detect_for_proxy_with_score", _fake)

    regime, score = mr.detect_regime_with_score("TSLA")
    assert regime == "up"
    assert score == 0.8
    # own-ticker fetch first, then the SP500 proxy fallback
    assert calls == ["TSLA", mr.EQUITY_PROXY]


def test_detect_equity_own_trend_used_directly(monkeypatch):
    """When the equity's OWN trend is up/down it is used directly — no SP500
    fallback fetch."""
    _clear_caches()
    calls = []

    def _fake(proxy):
        calls.append(proxy)
        return "up", 0.7
    monkeypatch.setattr(mr, "_detect_for_proxy_with_score", _fake)

    regime, score = mr.detect_regime_with_score("NVDA")
    assert regime == "up" and score == 0.7
    assert calls == ["NVDA"]


def test_detect_missing_score_cache_reads_zero(monkeypatch):
    """A regime-cache entry without a matching score entry reads score 0.0."""
    _clear_caches()
    now = mr.time.time()
    mr._regime_cache["BTC"] = ("up", now)  # fresh, but NO _score_cache entry

    def _fake(proxy):
        raise AssertionError("fresh cache must not trigger a fetch")
    monkeypatch.setattr(mr, "_detect_for_proxy_with_score", _fake)

    regime, score = mr.detect_regime_with_score("PEPE")
    assert regime == "up"
    assert score == 0.0


def test_detect_fetch_failure_degrades_to_neutral(monkeypatch):
    """A candle-fetch exception must be swallowed → ("neutral", 0.0)."""
    _clear_caches()

    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(mr, "fetch_hl_candles", _boom)

    regime, score = mr.detect_regime_with_score("PEPE")
    assert regime == "neutral"
    assert score == 0.0


def test_detect_fetch_empty_candles_degrades_to_neutral(monkeypatch):
    """An empty candle response (after the forming-bar drop) → neutral/0.0."""
    _clear_caches()
    monkeypatch.setattr(mr, "fetch_hl_candles", lambda *a, **k: [])
    regime, score = mr.detect_regime_with_score("PEPE")
    assert regime == "neutral"
    assert score == 0.0


# ── 5. classify_asset: foreign indices / priority / empty input ─────────

@pytest.mark.parametrize("coin", [
    "KR200", "KOSPI", "KOSPI200",
    "JP225", "NIKKEI", "N225",
    "HSI", "HANGSENG", "HK50",
    "DAX", "DAX40", "FTSE", "FTSE100", "CAC", "STOXX50", "ESTX50",
    "ASX200", "SENSEX", "NIFTY50",
    "xyz:KR200", "km:DAX", "xyz:NIKKEI",  # namespaced variants resolve by bare ticker
])
def test_classify_asset_foreign_indices_use_own_trend(coin):
    """Foreign / non-US indices do NOT track the US SP500 proxy — they use the
    own-trend ("commodity") path."""
    assert mr.classify_asset(coin) == "commodity"


@pytest.mark.parametrize("coin,expected", [
    ("xyz:GOLD", "commodity"),   # commodity allowlist beats namespaced default
    ("km:USOIL", "commodity"),
    ("xyz:NVDA", "equity"),      # equity allowlist
    ("xyz:SNDK", "equity"),      # unknown tokenized stock → equity default
    ("", "crypto"),              # empty → main-dex crypto default
    (None, "crypto"),            # None tolerated
])
def test_classify_asset_priority_and_empty(coin, expected):
    assert mr.classify_asset(coin) == expected


# ── 6. regime_snapshot: operator summary ────────────────────────────────

def test_regime_snapshot_reports_cached_proxies():
    """snapshot renders every cached proxy with rounded score + age; a proxy
    present in the regime cache but absent from the score cache reads 0.0."""
    _clear_caches()
    now = mr.time.time()
    mr._regime_cache["BTC"] = ("up", now - 10.0)
    mr._score_cache["BTC"] = (0.77777, now - 10.0)
    mr._regime_cache["NATGAS"] = ("chop", now - 50.0)
    # NATGAS deliberately NOT added to _score_cache → score defaults to 0.0

    snap = mr.regime_snapshot()
    assert set(snap.keys()) == {"BTC", "NATGAS"}

    assert snap["BTC"]["regime"] == "up"
    assert snap["BTC"]["score"] == 0.778          # rounded to 3 decimals
    assert snap["BTC"]["age_s"] == pytest.approx(10.0, abs=0.2)

    assert snap["NATGAS"]["regime"] == "chop"
    assert snap["NATGAS"]["score"] == 0.0         # missing score entry
    assert snap["NATGAS"]["age_s"] == pytest.approx(50.0, abs=0.2)


def test_regime_snapshot_empty_when_cold():
    _clear_caches()
    assert mr.regime_snapshot() == {}


# ── 7. regime_strength_score: output clamp on a trending tape ───────────

def test_strength_score_clamped_to_unit_interval():
    """The continuous score is always within [0, 1] and a strong trend scores
    materially higher than a flat tape (sanity that calibration is intact)."""
    up = [_mk(i, 100 + i * 0.6, 100 + i * 0.6 + 0.8, 100 + i * 0.6 - 0.2,
              100 + i * 0.6, 1000 + i) for i in range(80)]
    flat = _flat_candles(80)
    s_up = mr.regime_strength_score(up)
    s_flat = mr.regime_strength_score(flat)
    assert 0.0 <= s_flat <= s_up <= 1.0
    assert s_up > 0.5
