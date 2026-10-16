"""P3 (audit 2026-10-15): regime-exit overlay single source + orphan cleanup.

Pin that (a) the shared regime_exit_common.overlay_regime_exit produces the
same six-field overlay and RetraceTier conversion the three hand-copied
callers used to, (b) the three callers (dsl_exit / shadow_book /
backtest.regime) now all delegate to it, and (c) the three launch_capture
orphan keys are gone from defaults/schema.

The dsl_config shape mirrors production: the ``dsl_exit`` block with
``regime_aware.trend_ride`` / ``regime_aware.max_loss.{trend,non_trend}`` /
``regime_aware.clocks.{trend,non_trend}`` (see executor.select_exit_params /
resolve_regime_clocks). Regime values used by production are "up"/"down"
(trend-ride) and "neutral"/"chop" (scalp).
"""
from __future__ import annotations

from hermes_trader.agents.dsl_exit import ExitPolicy, RetraceTier
from hermes_trader.agents.regime_exit_common import overlay_regime_exit


def _dsl_exit_block():
    return {
        "max_loss_pct": 1.0,
        "max_loss_roe_pct": 15.0,
        "protect_pct": 1.5,
        "retrace_threshold": 0.15,
        "hard_timeout_minutes": 240.0,
        "stale_flat_timeout_minutes": 90.0,
        "phase2_tiers": [
            {"pct_above_entry": 2.0, "retrace_threshold": 0.35},
            {"pct_above_entry": 6.0, "retrace_threshold": 0.3},
        ],
        "regime_aware": {
            "enabled": True,
            "trend_ride": {
                "protect_pct": 2.5,
                "retrace_threshold": 0.4,
                "phase2_tiers": [
                    {"pct_above_entry": 2.5, "retrace_threshold": 0.4},
                    {"pct_above_entry": 8, "retrace_threshold": 0.38},
                ],
            },
            "max_loss": {
                "trend": {"max_loss_pct": 4.0, "max_loss_roe_pct": 20.0},
                "non_trend": {"max_loss_pct": 1.5, "max_loss_roe_pct": 15.0},
            },
            "clocks": {
                "enabled": True,
                "trend": {"hard_timeout_minutes": 240.0,
                          "stale_flat_timeout_minutes": 120.0},
                "non_trend": {"hard_timeout_minutes": 120.0,
                              "stale_flat_timeout_minutes": 60.0},
            },
        },
    }


def test_overlay_trend_ride_applies_six_fields():
    base = ExitPolicy()
    dsl = _dsl_exit_block()
    policy, label = overlay_regime_exit(base, dsl, "up")
    assert policy.max_loss_pct == 4.0
    assert policy.max_loss_roe_pct == 20.0
    assert policy.protect_pct == 2.5
    assert policy.retrace_threshold == 0.4
    assert policy.hard_timeout_minutes == 240.0
    assert policy.stale_flat_timeout_minutes == 120.0
    # tiers are RetraceTier objects, never raw dicts (P3 invariant)
    assert policy.phase2_tiers and all(
        isinstance(t, RetraceTier) for t in policy.phase2_tiers)
    assert "trend_ride" in label


def test_overlay_neutral_uses_scalp_bucket():
    base = ExitPolicy()
    dsl = _dsl_exit_block()
    policy, label = overlay_regime_exit(base, dsl, "neutral")
    assert policy.max_loss_pct == 1.5
    assert policy.max_loss_roe_pct == 15.0
    assert policy.protect_pct == 1.5       # base scalp protect (unchanged)
    assert policy.hard_timeout_minutes == 120.0
    assert policy.stale_flat_timeout_minutes == 60.0
    assert label == "scalp"


def test_overlay_disabled_regime_aware_keeps_scalp_base():
    base = ExitPolicy()
    dsl = _dsl_exit_block()
    dsl["regime_aware"]["enabled"] = False
    policy, label = overlay_regime_exit(base, dsl, "up")
    # regime_aware disabled -> scalp path. select_exit_params still applies the
    # non_trend max-loss override (executor L1064-1067 reads it unconditionally),
    # so the scalp 1.5%/15% cap binds — the trend_ride 4.0%/20% does NOT apply.
    assert policy.max_loss_pct == 1.5
    assert policy.max_loss_roe_pct == 15.0
    assert label == "scalp"


def test_overlay_empty_tiers_falls_back_to_base():
    base = ExitPolicy()
    dsl = _dsl_exit_block()
    # no phase2_tiers in the trend_ride block -> select returns base_tiers
    del dsl["regime_aware"]["trend_ride"]["phase2_tiers"]
    policy, _ = overlay_regime_exit(base, dsl, "up")
    # base_tiers = dsl_exit top-level phase2_tiers, converted to RetraceTier
    assert policy.phase2_tiers and all(
        isinstance(t, RetraceTier) for t in policy.phase2_tiers)
    assert len(policy.phase2_tiers) == 2


def test_overlay_clocks_disabled_uses_global_clocks():
    base = ExitPolicy()
    dsl = _dsl_exit_block()
    dsl["regime_aware"]["clocks"]["enabled"] = False
    policy, _ = overlay_regime_exit(base, dsl, "up")
    # clocks disabled -> global dsl_exit hard/stale timeouts
    assert policy.hard_timeout_minutes == float(ExitPolicy().hard_timeout_minutes)


def test_three_callers_all_delegate_to_common():
    # dsl_exit and shadow_book build from live config; backtest takes a base.
    # At minimum they must import and route through the shared function.
    import inspect

    import hermes_trader.agents.dsl_exit as dsl
    import hermes_trader.agents.shadow_book as sb
    import hermes_trader.backtest.regime as br

    dsl_src = inspect.getsource(dsl._regime_aware_policy_for)
    sb_src = inspect.getsource(sb._build_policy)
    br_src = inspect.getsource(br.regime_aware_policy)
    assert "overlay_regime_exit" in dsl_src
    assert "overlay_regime_exit" in sb_src
    assert "overlay_regime_exit" in br_src


# ── orphan key cleanup ──────────────────────────────────────────────────────

def test_launch_capture_orphan_keys_removed():
    from hermes_trader.agents import config_schema as cs
    from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS

    lc = CANONICAL_DEFAULTS["launch_capture"]
    for k in ("imbalance_min", "compression_pct_max", "require_key_level"):
        assert k not in lc, f"{k} should be removed from defaults"
    assert "presubscribe_compress_pct_max" in lc  # the real key stays
    sch = cs._NESTED_BLOCK_SPECS["launch_capture"]
    for k in ("imbalance_min", "compression_pct_max", "require_key_level"):
        assert k not in sch, f"{k} should be removed from schema"
