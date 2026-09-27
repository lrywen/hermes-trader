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


def _patch_funding(monkeypatch, regime: str):
    """Patch the cached funding-regime lookup that market_regime_gate calls."""
    from hermes_trader.agents import hyperfeed
    monkeypatch.setattr(
        hyperfeed,
        "market_get_funding_regime",
        lambda: {"regime": regime, "assets": []},
    )


def test_funding_regime_short_crowded_blocks_low_conf_long(monkeypatch):
    """SHORT_CROWDED + long at 0.70 conf should now block — the elevated bar
    is 0.85, even though the old counter_regime_min_conf would have let it
    through. This is the main reason for the patch."""
    from hermes_trader.agents import market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    _patch_funding(monkeypatch, "SHORT_CROWDED")
    r = market_regime_gate(
        _ctx(confidence=0.70, trade_side="long", composite_score=0),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is False
    assert "SHORT_CROWDED" in r["reason"]


def test_funding_regime_short_crowded_high_conf_long_passes(monkeypatch):
    """A 0.90-confidence long in a SHORT_CROWDED market still passes —
    we never want to hard-block strong individual signals."""
    from hermes_trader.agents import market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    _patch_funding(monkeypatch, "SHORT_CROWDED")
    r = market_regime_gate(
        _ctx(confidence=0.90, trade_side="long"),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is True


def test_funding_regime_long_crowded_blocks_low_conf_short(monkeypatch):
    """SYMMETRIC: LONG_CROWDED + short at 0.70 conf is blocked the same way
    SHORT_CROWDED + long is blocked. Regression guard against the gate
    becoming long-only-restrictive when the regime flips."""
    from hermes_trader.agents import market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    _patch_funding(monkeypatch, "LONG_CROWDED")
    r = market_regime_gate(
        _ctx(confidence=0.70, trade_side="short", composite_score=0),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is False
    assert "LONG_CROWDED" in r["reason"]


def test_funding_regime_aligned_no_extra_friction(monkeypatch):
    """A short in a SHORT_CROWDED market is aligned with the crowd → the
    elevated bar must NOT apply. A 0.40-conf aligned short should pass
    once we're at trend-regime neutral."""
    from hermes_trader.agents import market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    _patch_funding(monkeypatch, "SHORT_CROWDED")
    r = market_regime_gate(
        _ctx(confidence=0.40, trade_side="short"),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is True


def test_funding_regime_neutral_doesnt_change_behavior(monkeypatch):
    """When funding regime is NEUTRAL, the gate behaves exactly like the
    pre-patch version — no elevated bar, only the trend-regime check."""
    from hermes_trader.agents import market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    _patch_funding(monkeypatch, "NEUTRAL")
    # Low-conf long in a neutral trend + neutral funding → pass (no friction).
    r = market_regime_gate(
        _ctx(confidence=0.30, trade_side="long"),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is True


def test_funding_regime_overlay_respects_binary_triggers(monkeypatch):
    """momentum_burst / slow_burn / whale_signal bypasses MUST be preserved
    even against the crowded funding regime — those are explicit overrides
    for stale macro calls, and the user's spec said do not weaken them."""
    from hermes_trader.agents import market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    _patch_funding(monkeypatch, "SHORT_CROWDED")
    # Low-conf, low-score long in SHORT_CROWDED, but momentum_burst fired → pass
    r = market_regime_gate(
        _ctx(confidence=0.30, trade_side="long",
             composite_score=10, momentum_burst_fired=True),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is True
    # Same setup, whale_signal instead → still passes
    r2 = market_regime_gate(
        _ctx(confidence=0.30, trade_side="long",
             composite_score=10, whale_signal_fired=True),
        counter_regime_min_conf=0.70,
    )
    assert r2["pass"] is True


def test_funding_regime_overlay_score_threshold_elevated(monkeypatch):
    """Elevated bar: counter-funding-regime trades need composite_score >= 60
    (vs the normal 50) to clear via the score bypass."""
    from hermes_trader.agents import market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    _patch_funding(monkeypatch, "SHORT_CROWDED")
    # Score 55 was enough pre-patch (>= 50), should now BLOCK against funding regime.
    r_block = market_regime_gate(
        _ctx(confidence=0.30, trade_side="long", composite_score=55),
        counter_regime_min_conf=0.70,
    )
    assert r_block["pass"] is False
    # Score 65 clears the elevated 60 bar.
    r_pass = market_regime_gate(
        _ctx(confidence=0.30, trade_side="long", composite_score=65),
        counter_regime_min_conf=0.70,
    )
    assert r_pass["pass"] is True


def test_funding_regime_cache_short_circuits_repeated_calls(monkeypatch):
    """The 5-min cache on market_get_funding_regime must avoid refetching the
    universe on every gate call. Without this guard the risk gates would
    hammer the API once per trade attempt."""
    from hermes_trader.agents import hyperfeed

    # Reset cache so this test is order-independent.
    monkeypatch.setattr(hyperfeed, "_funding_regime_cache", None)

    calls = {"count": 0}

    def fake_compute():
        calls["count"] += 1
        return {"regime": "SHORT_CROWDED", "assets": []}

    monkeypatch.setattr(hyperfeed, "_compute_funding_regime", fake_compute)

    r1 = hyperfeed.market_get_funding_regime()
    r2 = hyperfeed.market_get_funding_regime()
    r3 = hyperfeed.market_get_funding_regime()
    assert r1["regime"] == "SHORT_CROWDED"
    assert r2["regime"] == "SHORT_CROWDED"
    assert r3["regime"] == "SHORT_CROWDED"
    # Only the first call should hit _compute_funding_regime.
    assert calls["count"] == 1


def test_funding_regime_per_class_crypto_short_crowded_does_not_gate_oil(monkeypatch):
    """xyz:CL (oil, commodity class) long must pass even when the crypto
    funding regime is SHORT_CROWDED — oil has its own funding market."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime", lambda: {
        "regime": "SHORT_CROWDED",
        "regimes_by_class": {
            "crypto":    "SHORT_CROWDED",
            "equity":    "NEUTRAL",
            "commodity": "NEUTRAL",
        },
        "assets": [],
    })
    # xyz:CL classifies as commodity → look up commodity regime → NEUTRAL → pass.
    r = market_regime_gate(
        _ctx(confidence=0.40, trade_side="long", coin="xyz:CL"),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is True


def test_funding_regime_per_class_crypto_short_crowded_does_not_gate_arm(monkeypatch):
    """xyz:ARM (semis, equity class) long passes when the crypto regime is
    SHORT_CROWDED but the equity regime is NEUTRAL — this is the actual
    bug that snuck xyz:ARM through the gate in production."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime", lambda: {
        "regime": "SHORT_CROWDED",
        "regimes_by_class": {
            "crypto":    "SHORT_CROWDED",
            "equity":    "NEUTRAL",
            "commodity": "NEUTRAL",
        },
        "assets": [],
    })
    r = market_regime_gate(
        _ctx(confidence=0.40, trade_side="long", coin="xyz:ARM"),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is True


def test_funding_regime_per_class_equity_short_crowded_gates_equity_long(monkeypatch):
    """When the EQUITY class itself is SHORT_CROWDED, an equity long is the
    one that faces the elevated bar — proving the per-class lookup applies
    correctly to the matching asset class."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime", lambda: {
        "regime": "NEUTRAL",
        "regimes_by_class": {
            "crypto":    "NEUTRAL",
            "equity":    "SHORT_CROWDED",
            "commodity": "NEUTRAL",
        },
        "assets": [],
    })
    # Low-conf long on an equity perp → blocked (equity class is short-crowded).
    r = market_regime_gate(
        _ctx(confidence=0.40, trade_side="long", coin="xyz:ARM", composite_score=0),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is False
    assert "SHORT_CROWDED" in r["reason"]


def test_funding_regime_per_class_falls_back_to_legacy_when_missing(monkeypatch):
    """Older callers / unit-test stubs may return a dict without
    `regimes_by_class`. The gate must fall back to the legacy `regime` field
    rather than silently disabling the overlay."""
    from hermes_trader.agents import hyperfeed, market_regime
    from hermes_trader.agents.risk_gates import market_regime_gate
    monkeypatch.setattr(market_regime, "detect_regime_with_score", lambda c, force=False: ("neutral", 0.0))
    # NO regimes_by_class key — legacy shape.
    monkeypatch.setattr(hyperfeed, "market_get_funding_regime",
                        lambda: {"regime": "SHORT_CROWDED", "assets": []})
    # BTC (crypto) long with mid confidence → legacy SHORT_CROWDED applies → block.
    r = market_regime_gate(
        _ctx(confidence=0.40, trade_side="long", coin="BTC", composite_score=0),
        counter_regime_min_conf=0.70,
    )
    assert r["pass"] is False


def test_compute_funding_regime_includes_hip3(monkeypatch):
    """`_compute_funding_regime` must fetch the universe WITH HIP-3 so
    equity / commodity perps are visible. Regression guard for the bug
    where xyz:CL and xyz:ARM weren't in the regime scan at all."""
    from hermes_trader.agents import hyperfeed

    captured = {}

    def fake_get_universe(*, include_hip3=False, **kw):
        captured["include_hip3"] = include_hip3
        # Mixed universe: crypto with negative funding + commodity with positive funding.
        # Funding magnitudes use ±0.0006: P1-11 raised crowded_funding_threshold
        # 0.0001 → 0.0004 (0.0001 == HL perp baseline 0.01%/8h flagged the whole
        # market), so the old ±0.0002 fixtures no longer cross the threshold.
        return [
            {"coin": "BTC",    "funding": -0.0006, "openInterest": 5e7, "dayNtlVlm": 1e9},
            {"coin": "ETH",    "funding": -0.0006, "openInterest": 5e7, "dayNtlVlm": 5e8},
            {"coin": "SOL",    "funding": -0.0006, "openInterest": 5e7, "dayNtlVlm": 3e8},
            {"coin": "DOGE",   "funding": -0.0006, "openInterest": 5e7, "dayNtlVlm": 2e8},
            {"coin": "AVAX",   "funding": -0.0006, "openInterest": 5e7, "dayNtlVlm": 1e8},
            {"coin": "XRP",    "funding": -0.0006, "openInterest": 5e7, "dayNtlVlm": 1e8},
            {"coin": "LINK",   "funding": -0.0006, "openInterest": 5e7, "dayNtlVlm": 1e8},
            {"coin": "xyz:CL", "funding":  0.0006, "openInterest": 5e6, "dayNtlVlm": 1e7},
        ]

    monkeypatch.setattr(hyperfeed, "get_universe", fake_get_universe)
    out = hyperfeed._compute_funding_regime()
    assert captured["include_hip3"] is True
    # Crypto class is short-crowded (7 short, 0 long).
    assert out["regimes_by_class"]["crypto"] == "SHORT_CROWDED"
    # Commodity class has only one signal, < margin of 5 → NEUTRAL.
    assert out["regimes_by_class"]["commodity"] == "NEUTRAL"
    # Legacy regime field tracks crypto.
    assert out["regime"] == "SHORT_CROWDED"


def test_compute_funding_regime_long_crowded_margin(monkeypatch):
    """A class needs a >5 long-over-short margin to be LONG_CROWDED."""
    from hermes_trader.agents import hyperfeed
    # 7 crypto longs (funding>0, oi high), 0 shorts → margin 7 > 5.
    # funding=0.0006 clears the P1-11 threshold (0.0004); see the HIP-3
    # fixture above for why the old 0.0002 no longer counts as crowded.
    universe = [
        {"coin": c, "funding": 0.0006, "openInterest": 5e7, "dayNtlVlm": 1e8}
        for c in ("BTC", "ETH", "SOL", "DOGE", "AVAX", "XRP", "LINK")
    ]
    monkeypatch.setattr(hyperfeed, "get_universe",
                        lambda *, include_hip3=False, **k: universe)
    out = hyperfeed._compute_funding_regime()
    assert out["regimes_by_class"]["crypto"] == "LONG_CROWDED"
    assert out["regime"] == "LONG_CROWDED"
