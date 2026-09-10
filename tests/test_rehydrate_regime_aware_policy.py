"""Regression: LIVE-side tracker synthesis (rehydrate_from_exchange) must build
the SAME regime-aware ExitPolicy a fresh live entry would get.

Root cause being pinned: after a blackout/orphan recovery, rehydrate
synthesizes a tracker for an exchange position that has no in-memory tracker.
It used the regime-BLIND dsl_exit._policy_from_config() (top-level
max_loss_pct/max_loss_roe_pct) for every synth tracker, so a position re-adopted
after a state wipe silently inherited LOOSER/regime-blind stops instead of the
regime-specific caps the live entry path selected (trend up/down → 0.8%/10%ROE
trend-ride; neutral/chop → 0.4%/5%ROE scalp).

The fix resolves the coin's CURRENT (TTL-cached) regime as the best-available
proxy for its (now-lost) entry regime, builds the policy via
_regime_aware_policy_for(), and stamps entry_regime on the tracker. An explicit
caller policy still wins; detect_regime and the policy builder both fail OPEN so
regime I/O can never block rehydrate.
"""
from __future__ import annotations

import pytest

from hermes_trader.agents import dsl_exit
from hermes_trader.agents import executor
from hermes_trader.agents import market_regime

# Mirrors production dsl_exit: loose top-level caps that regime_aware overrides.
DSL_CFG = {
    "protect_pct": 1.5,
    "retrace_threshold": 0.35,
    "max_loss_pct": 1.0,
    "max_loss_roe_pct": 15.0,
    "regime_aware": {
        "enabled": True,
        "trend_ride": {"protect_pct": 2.5, "retrace_threshold": 0.40},
        "max_loss": {
            "trend": {"max_loss_pct": 0.8, "max_loss_roe_pct": 10.0},
            "non_trend": {"max_loss_pct": 0.4, "max_loss_roe_pct": 5.0},
        },
    },
}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"dsl_exit": DSL_CFG})
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE", None)
    monkeypatch.setattr(dsl_exit, "_POLICY_CACHE_TS", 0.0)
    # These tests synth trackers into the module-level live registry; clear on
    # teardown too so BTC/ETH/SOL trackers and suspect-SL state do not leak into
    # later test modules run in the same process (cross-file isolation).
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()
    yield
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()
    dsl_exit._loaded_from_disk = False


def _isolate(monkeypatch, tmp_path):
    state_file = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state_file))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    return dsl_exit


def _positions(*coins_szi):
    out = []
    for coin, szi, entry in coins_szi:
        out.append({"position": {"coin": coin, "szi": str(szi), "entryPx": str(entry)}})
    return out


def test_regime_aware_policy_for_trend_gets_10():
    for regime in ("up", "down"):
        pol = dsl_exit._regime_aware_policy_for(regime)
        assert pol.max_loss_roe_pct == pytest.approx(10.0), regime
        assert pol.max_loss_pct == pytest.approx(0.8), regime
        assert pol.protect_pct == pytest.approx(2.5), regime


def test_regime_aware_policy_for_non_trend_gets_5():
    for regime in ("neutral", "chop", ""):
        pol = dsl_exit._regime_aware_policy_for(regime)
        assert pol.max_loss_roe_pct == pytest.approx(5.0), repr(regime)
        assert pol.max_loss_pct == pytest.approx(0.4), repr(regime)


def test_rehydrate_synth_tracker_uses_current_regime(monkeypatch, tmp_path):
    d = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        market_regime, "detect_regime",
        lambda coin, *, force=False: "up" if coin == "BTC" else "neutral")
    d.rehydrate_from_exchange(_positions(("BTC", 1, 100.0), ("ETH", -1, 200.0)))

    t_trend = d._active_positions["BTC_long"]
    t_scalp = d._active_positions["ETH_short"]
    assert t_trend.entry_regime == "up"
    assert t_trend.policy.max_loss_roe_pct == pytest.approx(10.0)
    assert t_trend.policy.max_loss_pct == pytest.approx(0.8)
    assert t_scalp.entry_regime == "neutral"
    assert t_scalp.policy.max_loss_roe_pct == pytest.approx(5.0)
    assert t_scalp.policy.max_loss_pct == pytest.approx(0.4)


def test_rehydrate_down_regime_gets_trend_cap(monkeypatch, tmp_path):
    d = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(market_regime, "detect_regime",
                        lambda coin, *, force=False: "down")
    d.rehydrate_from_exchange(_positions(("SOL", 1, 150.0)))
    t = d._active_positions["SOL_long"]
    assert t.entry_regime == "down"
    assert t.policy.max_loss_roe_pct == pytest.approx(10.0)


def test_rehydrate_explicit_policy_skips_regime(monkeypatch, tmp_path):
    d = _isolate(monkeypatch, tmp_path)

    def _boom(*_a, **_k):
        raise AssertionError("detect_regime must not be consulted when policy given")

    monkeypatch.setattr(market_regime, "detect_regime", _boom)
    custom = dsl_exit.ExitPolicy(max_loss_roe_pct=7.0, max_loss_pct=0.6)
    d.rehydrate_from_exchange(_positions(("BTC", 1, 100.0)), policy=custom)
    t = d._active_positions["BTC_long"]
    assert t.policy is custom
    assert t.policy.max_loss_roe_pct == pytest.approx(7.0)
    assert t.entry_regime == ""   # not stamped when caller supplied the policy


def test_rehydrate_detect_regime_failure_fails_open(monkeypatch, tmp_path):
    d = _isolate(monkeypatch, tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("regime service down")

    monkeypatch.setattr(market_regime, "detect_regime", _boom)
    # Must not raise: falls back to neutral/scalp policy.
    d.rehydrate_from_exchange(_positions(("BTC", 1, 100.0)))
    t = d._active_positions["BTC_long"]
    assert t.entry_regime == "neutral"
    assert t.policy.max_loss_roe_pct == pytest.approx(5.0)


def test_regime_aware_policy_builder_fails_open(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("select failed")

    monkeypatch.setattr(executor, "select_exit_params", _boom)
    pol = dsl_exit._regime_aware_policy_for("up")  # must not raise
    assert pol is not None
    base = dsl_exit._policy_from_config()
    assert pol.max_loss_roe_pct == pytest.approx(base.max_loss_roe_pct)
