"""Tests for regime-aware exit selection (pure fn)."""

from hermes_trader.agents.executor import select_exit_params

SCALP_BASE = {
    "protect_pct": 1.5, "retrace_threshold": 0.30,
    "max_loss_pct": 0.4, "max_loss_roe_pct": 5.0,
    "phase2_tiers": [{"pct_above_entry": 1.5, "retrace_threshold": 0.30}],
    "regime_aware": {
        "enabled": True,
        "trend_ride": {"protect_pct": 3.0, "retrace_threshold": 0.55,
                       "phase2_tiers": [{"pct_above_entry": 3.0, "retrace_threshold": 0.55}]},
        "max_loss": {
            "trend": {"max_loss_pct": 0.8, "max_loss_roe_pct": 10.0},
            "non_trend": {"max_loss_pct": 0.4, "max_loss_roe_pct": 5.0},
        },
    },
}


def test_chop_uses_scalp():
    pp, rt, tiers, ml, mlr, label = select_exit_params(SCALP_BASE, "neutral")
    assert pp == 1.5 and rt == 0.30 and label == "scalp"
    # Plan C: non-trend keeps the tight stop.
    assert ml == 0.4 and mlr == 5.0


def test_down_uses_trend_ride_wide_stop():
    # Plan C: 'down' is also a directional regime → ride + widen stop (was scalp
    # before Plan C, but a downtrend that stays a downtrend deserves the same
    # room as an uptrend; the gate blocks counter-trend longs anyway).
    pp, rt, tiers, ml, mlr, label = select_exit_params(SCALP_BASE, "down")
    assert pp == 3.0 and rt == 0.55 and "trend_ride" in label
    assert ml == 0.8 and mlr == 10.0


def test_up_uses_trend_ride():
    pp, rt, tiers, ml, mlr, label = select_exit_params(SCALP_BASE, "up")
    assert pp == 3.0 and rt == 0.55 and "trend_ride" in label
    assert tiers[0]["retrace_threshold"] == 0.55
    assert ml == 0.8 and mlr == 10.0


def test_chop_state_keeps_tight_stop():
    pp, rt, tiers, ml, mlr, label = select_exit_params(SCALP_BASE, "chop")
    assert label == "scalp" and ml == 0.4 and mlr == 5.0


def test_disabled_stays_scalp_even_in_up():
    cfg = {**SCALP_BASE, "regime_aware": {"enabled": False,
           "trend_ride": {"protect_pct": 3.0, "retrace_threshold": 0.55}}}
    pp, rt, tiers, ml, mlr, label = select_exit_params(cfg, "up")
    assert pp == 1.5 and rt == 0.30 and label == "scalp"
    # When disabled, max_loss falls through to the top-level dsl_exit defaults.
    assert ml == 0.4 and mlr == 5.0


def test_missing_regime_aware_safe():
    pp, rt, tiers, ml, mlr, label = select_exit_params(
        {"protect_pct": 1.5, "retrace_threshold": 0.30}, "up")
    assert pp == 1.5 and label == "scalp"
    # No top-level max_loss either → falls back to CANONICAL_DEFAULTS
    # (dsl_exit.max_loss_pct=0.4, dsl_exit.max_loss_roe_pct=5.0).
    assert ml == 0.4 and mlr == 5.0
