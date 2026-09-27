"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def _ctx(**kw):
    from hermes_trader.agents.risk_gates import GateContext
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="BTC",
                trade_side="long", has_binary_news_risk=False, equity=1000,
                total_open_notional=0)
    base.update(kw)
    return GateContext(**base)


def _mk_candle(t, o, h, l, c, v):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _flat_candles(n, price=100.0, vol=1000.0, rng=0.5):
    """Choppy/flat candles oscillating around `price` (produces low ADX)."""
    out = []
    for i in range(n):
        s = 1.0 if (i % 2 == 0) else -1.0
        c = price + s * (i % 3) * 0.1
        out.append(_mk_candle(i, c, c + rng, c - rng, c, vol))
    return out


def _patch_fetch(monkeypatch, candles_5m, candles_1h):
    from hermes_trader.agents import perception
    monkeypatch.setattr(
        perception, "_fetch_candles_sync",
        lambda coin, interval, count, ttl, **kwargs:
        candles_5m if interval == "5m" else candles_1h)


def _trend_candles(n, start=100.0, step=0.5, vol=1000.0):
    """Steadily rising candles (produces high ADX)."""
    out = []
    for i in range(n):
        c = start + i * step
        out.append(_mk_candle(i, c, c + 0.8, c - 0.2, c, vol + i))
    return out


def _trend_surface_market_and_cfg():
    """A coin in a steady 5m downtrend (~9% over the 72-bar momentum window)
    that fires ONLY downtrendMomentum (weight 0) — the composite stays well
    below the gate, so surfacing depends entirely on the trend bypass."""
    from hermes_trader.agents.config import get_config
    cfg = get_config()
    market = {"coin": "TRX", "type": "perp", "dex": None}
    return market, cfg, cfg["scan"]["minCompositeScore"]


def test_detect_regime_caches_and_uses_proxy(monkeypatch):
    """detect_regime should call the proxy (BTC/NVDA/own) and cache the result."""
    from hermes_trader.agents import market_regime
    market_regime._regime_cache.clear()
    market_regime._score_cache.clear()
    calls: list[str] = []
    monkeypatch.setattr(market_regime, "_detect_for_proxy_with_score",
                        lambda proxy: (calls.append(proxy) or "up", 1.0))
    # First call for an alt coin → fetches BTC proxy
    assert market_regime.detect_regime("PEPE") == "up"
    assert calls == ["BTC"]
    # Second call for another alt → cache hit, no new fetch
    assert market_regime.detect_regime("WIF") == "up"
    assert calls == ["BTC"]
    # Equity coin uses its OWN trend now (audit fix #3, 2026-06-02): each equity is
    # gated by its own chart, not the single xyz:SP500 proxy. SP500 is only the
    # fallback when the name's own trend reads neutral/thin.
    # _detect_for_proxy_with_score is stubbed to "up", so the own-trend
    # ("TSLA") resolves and is used directly.
    assert market_regime.detect_regime("TSLA") == "up"
    assert calls == ["BTC", "TSLA"]
    # Commodity uses its own ticker
    assert market_regime.detect_regime("NATGAS") == "up"
    assert calls == ["BTC", "TSLA", "NATGAS"]


def test_market_regime_gate_aligned_passes(monkeypatch):
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("up", 1.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "assets": []})
    # Long when up → pass, regardless of confidence
    r = market_regime_gate(_ctx(confidence=0.1, trade_side="long"))
    assert r["pass"] is True
    assert r["via"] == "aligned"


def test_market_regime_gate_via_reports_trigger_bypass(monkeypatch):
    """A counter-regime trade that clears only via a slow-burn trigger reports
    via='trigger:slow_burn' and counter context — this is the LINK/FARTCOIN case."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "SHORT_CROWDED",
                                 "regimes_by_class": {"crypto": "SHORT_CROWDED"}})
    monkeypatch.setattr(market_regime, "classify_asset", lambda c: "crypto")
    # conf 0.52, low composite, against SHORT_CROWDED long → only slow_burn clears.
    ctx = _ctx(confidence=0.52, trade_side="long", coin="FARTCOIN",
               composite_score=21, slow_burn_fired=True)
    r = market_regime_gate(ctx)
    assert r["pass"] is True
    assert r["via"] == "trigger:slow_burn"
    assert r["against_funding"] is True
    assert r["funding"] == "SHORT_CROWDED"


def test_market_regime_gate_via_confidence_and_blocked(monkeypatch):
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "SHORT_CROWDED",
                                 "regimes_by_class": {"crypto": "SHORT_CROWDED"}})
    monkeypatch.setattr(market_regime, "classify_asset", lambda c: "crypto")
    # High enough conf clears the elevated 0.85 bar → via confidence.
    hi = market_regime_gate(_ctx(confidence=0.9, trade_side="long", composite_score=0))
    assert hi["pass"] is True and hi["via"] == "confidence"
    # Nothing clears → blocked, with via marker for the log.
    lo = market_regime_gate(_ctx(confidence=0.5, trade_side="long",
                                 composite_score=10, slow_burn_fired=False))
    assert lo["pass"] is False and lo["via"] == "blocked"


def test_market_regime_gate_neutral_passes(monkeypatch):
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "assets": []})
    r = market_regime_gate(_ctx(confidence=0.1, trade_side="short"))
    assert r["pass"] is True


def test_market_regime_gate_counter_low_conf_blocks(monkeypatch):
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("up", 1.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "assets": []})
    r = market_regime_gate(_ctx(confidence=0.5, trade_side="short"))
    assert r["pass"] is False
    assert "counter-regime" in r["reason"]


def test_market_regime_gate_counter_high_conf_passes(monkeypatch):
    """A 0.85-confidence counter-trend trade should sneak through the gate —
    high-conviction contrarian trades are the whole point of the bypass."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("up", 1.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "assets": []})
    r = market_regime_gate(_ctx(confidence=0.85, trade_side="short"))
    assert r["pass"] is True


def test_market_regime_gate_against_funding_empty_env_no_crash(monkeypatch):
    """P3 defense: an empty-string HERMES_CFG_AGAINST_FUNDING_MIN_* override makes
    cfg_get return '', and float('') raises ValueError. The gate must coerce to
    the elevated-bar defaults instead of crashing (and still block a low-conv
    counter-funding long)."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "SHORT_CROWDED",
                                 "regimes_by_class": {"crypto": "SHORT_CROWDED"}})
    monkeypatch.setattr(market_regime, "classify_asset", lambda c: "crypto")
    monkeypatch.setenv("HERMES_CFG_AGAINST_FUNDING_MIN_CONF", "")
    monkeypatch.setenv("HERMES_CFG_AGAINST_FUNDING_MIN_SCORE", "")
    r = market_regime_gate(_ctx(confidence=0.1, trade_side="long",
                                composite_score=0, coin="FARTCOIN"))
    assert r["pass"] is False
    assert r["against_funding"] is True


def test_market_regime_gate_wired_into_eval_all(monkeypatch):
    """The new gate is part of the 12-gate evaluation now and blocks at the
    right time — regression guard against forgetting to wire it in."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import eval_all_gates
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("up", 1.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "assets": []})
    cfg = {"min_ai_confidence": 0.3, "max_concurrent": 10,
           "max_trade_notional_usd": 1000, "max_daily_loss_usd": -100,
           "min_market_volume_usd": 5e6, "max_total_notional_pct": 10.0,
           "cooldown_min": 0, "counter_regime_min_conf": 0.7}
    # Low-conf short in an up regime → blocked, with the new reason surfaced
    out = eval_all_gates(_ctx(confidence=0.4, trade_side="short"), cfg)
    assert out["blocked"] is True
    assert any("counter-regime" in r for r in out["block_reasons"])
    # Aligned long → not blocked by the regime gate
    out_ok = eval_all_gates(_ctx(confidence=0.4, trade_side="long"), cfg)
    assert out_ok["results"]["market_regime"]["pass"] is True


def test_regime_gate_bypasses_on_whale_signal():
    """A counter-regime LONG should pass when whale_signal_fired is True,
    even at low confidence and zero composite — the oi_funding_anomaly
    signal (whale accumulation, negative funding, flat price) is its own
    bypass path, parallel to slow_burn_fired."""
    import hermes_trader.agents.market_regime as mr
    from hermes_trader.agents.risk_gates import GateContext, market_regime_gate
    mr._regime_cache.clear()
    mr._regime_cache["BTC"] = ("down", 99999999999)

    ctx = GateContext(
        confidence=0.45,
        current_positions=[], trade_notional_usd=100, daily_pnl=0,
        market_volume_24h_usd=5_000_000, coin="ALT", trade_side="long",
        has_binary_news_risk=False, equity=200, total_open_notional=0,
        composite_score=10, momentum_burst_fired=False,
        slow_burn_fired=False, whale_signal_fired=True,
    )
    res = market_regime_gate(ctx, counter_regime_min_conf=0.65)
    assert res["pass"] is True, res

    # Without whale signal, same setup blocks.
    ctx.whale_signal_fired = False
    res = market_regime_gate(ctx, counter_regime_min_conf=0.65)
    assert res["pass"] is False


def test_regime_gate_bypasses_on_slow_burn():
    """A counter-regime LONG with neither high conviction nor momentumBurst
    should still pass if slow_burn_fired is True — the empirical fix for
    WLFI/ICP-style accumulation breakouts."""
    import hermes_trader.agents.market_regime as mr
    from hermes_trader.agents.risk_gates import GateContext, market_regime_gate
    # Force regime = "down" so the gate engages on a LONG.
    mr._regime_cache.clear()
    mr._regime_cache["BTC"] = ("down", 99999999999)

    ctx = GateContext(
        confidence=0.55,  # below 0.65 bar
        current_positions=[],
        trade_notional_usd=100,
        daily_pnl=0,
        market_volume_24h_usd=5_000_000,
        coin="ALT",
        trade_side="long",
        has_binary_news_risk=False,
        equity=200,
        total_open_notional=0,
        composite_score=15,  # below 50 bypass
        momentum_burst_fired=False,
        slow_burn_fired=True,  # ← the new bypass
    )
    res = market_regime_gate(ctx, counter_regime_min_conf=0.65)
    assert res["pass"] is True, res

    # Without slow_burn_fired, same setup should block.
    ctx.slow_burn_fired = False
    res = market_regime_gate(ctx, counter_regime_min_conf=0.65)
    assert res["pass"] is False


def test_chop_regime_gate_raises_conviction_bar(monkeypatch):
    """In chop, a weak long must be blocked; high conviction or momentum burst
    must pass. slow_burn/whale alone must NOT bypass chop."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("chop", 0.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "assets": []})

    # weak conviction, no momentum -> blocked
    weak = _ctx(confidence=0.5, composite_score=30, trade_side="long",
                momentum_burst_fired=False, slow_burn_fired=True)
    r = market_regime_gate(weak)
    assert r["pass"] is False
    assert r["via"] == "chop_blocked"
    assert r.get("chop") is True

    # high confidence -> passes (threshold now max(0.82 counter, 0.85 chop))
    strong = _ctx(confidence=0.9, composite_score=30, trade_side="long")
    r = market_regime_gate(strong)
    assert r["pass"] is True and r["via"] == "chop_conviction"

    # momentum burst -> passes (genuine impulse out of the range)
    burst = _ctx(confidence=0.4, composite_score=20, trade_side="long",
                 momentum_burst_fired=True)
    r = market_regime_gate(burst)
    assert r["pass"] is True and r["via"] == "trigger:momentum_burst"

    # slow_burn alone must NOT bypass chop (those fire constantly in ranges)
    slow_only = _ctx(confidence=0.4, composite_score=20, trade_side="long",
                     slow_burn_fired=True)
    r = market_regime_gate(slow_only)
    assert r["pass"] is False and r["via"] == "chop_blocked"


def test_trend_surface_suppressed_in_chop_regime(monkeypatch):
    """downtrendMomentum fires but the coin's own 1h regime is chop (EMA-neutral
    + ADX<20): trend surfacing must be SILENCED (not surfaced)."""
    from hermes_trader.agents import perception
    from hermes_trader.agents.market_regime import classify_candles
    down_5m = _trend_candles(120, start=100.0, step=-0.12)
    chop_1h = _flat_candles(48, price=100.0)
    assert classify_candles(chop_1h) == "chop"  # fixture sanity
    _patch_fetch(monkeypatch, down_5m, chop_1h)
    market, cfg, gate = _trend_surface_market_and_cfg()

    ok, res = perception._scan_single_market(market, 100.0, cfg, gate, None,
                                             False, trend_surface_enabled=True)
    assert ok and res is None, f"chop regime must silence trend surfacing, got {res}"


def test_trend_surface_fires_in_trend_regime(monkeypatch):
    """Same 5m downtrend, but the 1h regime is a directional trend: the coin is
    surfaced even though its composite score is below the gate (the whole point
    of the trend bypass — unblocking shorts)."""
    from hermes_trader.agents import perception
    from hermes_trader.agents.market_regime import classify_candles
    down_5m = _trend_candles(120, start=100.0, step=-0.12)
    trend_1h = _trend_candles(48, start=100.0, step=-0.5)
    assert classify_candles(trend_1h) == "down"  # fixture sanity
    _patch_fetch(monkeypatch, down_5m, trend_1h)
    market, cfg, gate = _trend_surface_market_and_cfg()

    ok, res = perception._scan_single_market(market, 100.0, cfg, gate, None,
                                             False, trend_surface_enabled=True)
    assert ok and isinstance(res, dict), f"trend regime must surface, got {res}"
    assert res["coin"] == "TRX"
    assert res["composite_score"] < gate  # genuinely sub-gate → bypass did the work
    assert any(h["name"] == "downtrendMomentum" and h["fired"]
               for h in res["triggers"])


def test_trend_surface_fail_open_without_1h_candles(monkeypatch):
    """If the 1h candle fetch fails/returns nothing, the regime can't be
    computed — surfacing must fail OPEN (surface) so a data hiccup never
    silences a real trend."""
    from hermes_trader.agents import perception
    down_5m = _trend_candles(120, start=100.0, step=-0.12)
    _patch_fetch(monkeypatch, down_5m, None)
    market, cfg, gate = _trend_surface_market_and_cfg()

    ok, res = perception._scan_single_market(market, 100.0, cfg, gate, None,
                                             False, trend_surface_enabled=True)
    assert ok and isinstance(res, dict), f"missing 1h must fail open (surface), got {res}"


def test_trend_surface_disabled_drops_subgate_trend(monkeypatch):
    """Master switch OFF: even a clean directional trend must not surface a
    sub-gate coin (trend_surface_enabled is the hot kill-switch)."""
    from hermes_trader.agents import perception
    down_5m = _trend_candles(120, start=100.0, step=-0.12)
    trend_1h = _trend_candles(48, start=100.0, step=-0.5)
    _patch_fetch(monkeypatch, down_5m, trend_1h)
    market, cfg, gate = _trend_surface_market_and_cfg()

    ok, res = perception._scan_single_market(market, 100.0, cfg, gate, None,
                                             False, trend_surface_enabled=False)
    assert ok and res is None, f"switch OFF must not surface, got {res}"
