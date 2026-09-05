"""Regression: the SHADOW paper book must build the SAME regime-aware DSL
ExitPolicy a fresh live entry would get.

Root cause being pinned: shadow_book._build_policy() used to call the
regime-BLIND dsl_exit._policy_from_config() (top-level max_loss_pct=1.0 /
max_loss_roe_pct=15) for EVERY paper position, so shadow stops were
systematically LOOSER than live (57/57 historical shadow closes exited beyond
the live cap; 53 would have been stopped earlier — 95.9 excess ROE points).

The fix overlays select_exit_params(dsl, entry_regime) on top of the base
config policy: trend regimes (up/down) get the trend-ride 0.8% / 10%-ROE cap,
non-trend (neutral/chop/unknown) get the scalp 0.4% / 5%-ROE cap — for both
fresh shadow_open() and rehydrated trackers. Any resolution error fails OPEN
to the base config policy (never a crash, never bare ExitPolicy()).
"""
from __future__ import annotations

import pytest

from hermes_trader.agents import shadow_book as sb
from hermes_trader.agents import dsl_exit
from hermes_trader.agents import executor

# Mirrors the production .agent-config.json dsl_exit block: loose top-level
# caps (1.0% / 15 ROE) that regime_aware must override per entry regime.
DSL_CFG = {
    "protect_pct": 1.5,
    "retrace_threshold": 0.35,
    "max_loss_pct": 1.0,
    "max_loss_roe_pct": 15.0,
    "phase2_tiers": [
        {"pct_above_entry": 1.5, "retrace_threshold": 0.30},
    ],
    "regime_aware": {
        "enabled": True,
        "trend_ride": {
            "protect_pct": 2.5,
            "retrace_threshold": 0.40,
            "phase2_tiers": [
                {"pct_above_entry": 2.5, "retrace_threshold": 0.40},
                {"pct_above_entry": 8.0, "retrace_threshold": 0.40},
                {"pct_above_entry": 15.0, "retrace_threshold": 0.40},
                {"pct_above_entry": 25.0, "retrace_threshold": 0.40},
            ],
        },
        "max_loss": {
            "trend": {"max_loss_pct": 0.8, "max_loss_roe_pct": 10.0},
            "non_trend": {"max_loss_pct": 0.4, "max_loss_roe_pct": 5.0},
        },
    },
}


@pytest.fixture(autouse=True)
def _hermetic_dsl_config(monkeypatch):
    """Pin read_agent_config in BOTH modules and reset the policy TTL cache so
    every test builds a policy from the hermetic DSL_CFG above."""
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": DSL_CFG})
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE", None)
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE_TS", 0.0)
    yield


def _open(book, coin, side, regime, *, entry=100.0):
    fill = book.shadow_open(
        coin=coin, side=side, entry_px=entry, size_usd=1000.0,
        leverage=1, entry_atr_pct=0.0, entry_regime=regime,
        analysis_id="regime-policy-test",
    )
    assert fill is not None
    return book._trackers[book._key(coin, side)]


def test_build_policy_trend_regimes_get_10_roe():
    for regime in ("up", "down"):
        pol = sb._build_policy(regime)
        assert pol is not None
        assert pol.max_loss_roe_pct == pytest.approx(10.0), regime
        assert pol.max_loss_pct == pytest.approx(0.8), regime
        # trend-ride overrides ride the overlay too (parity with live)
        assert pol.protect_pct == pytest.approx(2.5), regime
        assert pol.retrace_threshold == pytest.approx(0.40), regime
        assert len(pol.phase2_tiers) == 4


def test_build_policy_non_trend_regimes_get_5_roe():
    for regime in ("neutral", "chop", ""):
        pol = sb._build_policy(regime)
        assert pol is not None
        assert pol.max_loss_roe_pct == pytest.approx(5.0), repr(regime)
        assert pol.max_loss_pct == pytest.approx(0.4), repr(regime)
        assert pol.protect_pct == pytest.approx(1.5), repr(regime)
        assert pol.retrace_threshold == pytest.approx(0.35), repr(regime)


def test_build_policy_not_regime_blind_15():
    # The old bug returned the top-level 15-ROE cap for every regime.
    for regime in ("up", "down", "neutral", "chop", ""):
        assert sb._build_policy(regime).max_loss_roe_pct != pytest.approx(15.0)


def test_shadow_open_tracker_uses_entry_regime_policy(tmp_path):
    book = sb.ShadowBook(path=str(tmp_path / "shadow.json"))
    t_trend = _open(book, "BTC", "long", "down")
    assert t_trend.policy.max_loss_roe_pct == pytest.approx(10.0)
    assert t_trend.policy.max_loss_pct == pytest.approx(0.8)

    t_scalp = _open(book, "ETH", "short", "neutral")
    assert t_scalp.policy.max_loss_roe_pct == pytest.approx(5.0)
    assert t_scalp.policy.max_loss_pct == pytest.approx(0.4)


def test_rehydrate_rebuilds_per_regime_policies(tmp_path):
    path = str(tmp_path / "shadow.json")
    book = sb.ShadowBook(path=path)
    _open(book, "BTC", "long", "up")
    _open(book, "ETH", "short", "neutral")

    # Restart: trackers are rebuilt from the persisted book.
    book2 = sb.ShadowBook(path=path)
    t_trend = book2._trackers[book2._key("BTC", "long")]
    t_scalp = book2._trackers[book2._key("ETH", "short")]
    assert t_trend.policy.max_loss_roe_pct == pytest.approx(10.0)
    assert t_scalp.policy.max_loss_roe_pct == pytest.approx(5.0)
    # The two positions must NOT share one regime-blind policy.
    assert t_trend.policy.max_loss_roe_pct != t_scalp.policy.max_loss_roe_pct


def test_build_policy_fails_open_to_base_policy(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("regime resolution down")

    # Snapshot what the base path produces WITHOUT the regime overlay (its exact
    # caps depend on the surrounding suite's config/cache state; the fail-open
    # contract is "never crash, never None", and that we fall back to the base
    # policy instead of applying the (exploding) regime overlay).
    from hermes_trader.agents.dsl_exit import _policy_from_config
    base = _policy_from_config()

    monkeypatch.setattr(executor, "select_exit_params", _boom)
    # Must not raise even though regime resolution explodes...
    pol = sb._build_policy("up")
    # ...and returns a usable policy equal to the base config policy, NOT the
    # trend-ride 10-ROE overlay that select_exit_params would have produced.
    assert pol is not None
    assert pol.max_loss_roe_pct == pytest.approx(base.max_loss_roe_pct)
    assert pol.max_loss_pct == pytest.approx(base.max_loss_pct)
    assert pol.protect_pct == pytest.approx(base.protect_pct)
