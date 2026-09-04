"""Offline tests for the 2026-09-04 config audit P2 (cleanup) disposition.

The P2 list flagged eight items as dead/latent/inconsistent. Code verification
showed most of them already behave correctly ("只处理有问题的项，合理项不动");
these tests LOCK the correct behavior so a future cleanup cannot silently break
it, and document each disposition (no network, no live container):

  * P2-17 equity_fraction_per_trade is a LIVE legacy fallback (executor reads it
           whenever atr_risk_sizing is disabled) and is consumed by backtests —
           not a dead key; keep.
  * P2-18 conviction_sizing / force-execute / bypass knobs are an intentional
           fail-safe switch group (schema + executor bypass list + armed
           FORBIDDEN_OVERRIDE checks); they ship OFF by design, not as dead
           code.
  * P2-19 two news caches are DISTINCT (Brave→LLM prompt vs GDELT/RSS→signal);
           independent TTLs (120s vs 300s) resolve from different config keys.
  * P2-20 shadow_book.max_positions is intentionally ABSENT from canonical and
           _max_positions() tracks the global max_concurrent (+DRIFT warning),
           so shadow/live position caps can't silently diverge.
  * P2-21 debate_gate ANDs min_agreement and min_agree_count → the stricter
           binds. Production 0.4/2 coincide under the 5-role vote.
  * P2-22 "_comment" is the operator free-form note, deliberately accepted by
           config validation (can't be a Pydantic field due to the underscore)
           — not a stray dead key.
  * P2-23 the six thresholds named in the audit ARE in CANONICAL_DEFAULTS and
           the pydantic schema (observable/tunable) — not hardcoded.
  * P2-24 hl_client_io.default_leverage (cross-margin fallback) is a DISTINCT
           knob from top-level `leverage` (trading/sizing) and is documented.
"""

from __future__ import annotations

import pytest

from hermes_trader.agents import executor
from hermes_trader.agents import news_catalyst
from hermes_trader.agents import research
from hermes_trader.agents import risk_gates as RG
from hermes_trader.agents import shadow_book
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get


# ── P2-17: equity_fraction_per_trade is a live legacy fallback ────────────

def test_p2_17_equity_fraction_is_live_legacy_fallback():
    # Canonical key present and consumed directly by the executor's non-ATR
    # sizing branch (config.get("equity_fraction_per_trade", 0.2)).
    assert "equity_fraction_per_trade" in CANONICAL_DEFAULTS
    src = __import__("inspect").getsource(executor)
    assert 'config.get("equity_fraction_per_trade", 0.2)' in src


def test_p2_17_default_matches_executor_fallback_literal():
    # The canonical value must equal the executor's own call-site fallback so
    # a missing config and a missing canonical agree.
    assert CANONICAL_DEFAULTS["equity_fraction_per_trade"] == 0.2


# ── P2-18: conviction / force-execute group ships OFF but is wired ─────────

def test_p2_18_conviction_group_present_and_defaults_off():
    # The knobs exist in canonical and default to the safe OFF state; the
    # executor bypass list reads the force-execute flags at run time.
    assert CANONICAL_DEFAULTS["conviction_sizing"] is False
    for flag in (
        "composite_force_execute",
        "ta_sidestep_force_execute",
        "whale_force_execute",
        "whale_regime_bypass",
        "breakout_force_execute",
        "whale_scan_bypass",
    ):
        assert CANONICAL_DEFAULTS[flag] is False, f"{flag} expected OFF default"


def test_p2_18_executor_reads_force_execute_flags():
    # Guard against accidental removal: the bypass list references the flags.
    src = __import__("inspect").getsource(executor)
    for flag in (
        "composite_force_execute",
        "breakout_force_execute",
        "whale_force_execute",
        "ta_sidestep_force_execute",
        "whale_regime_bypass",
    ):
        assert flag in src


# ── P2-19: two news caches are distinct and independently configurable ─────

def test_p2_19_news_ttls_are_independent_and_differ():
    brave_ttl = cfg_get("news_cache_ttl_s", config={})
    catalyst_ttl = cfg_get("news_catalyst.ttl_sec", config={})
    assert float(brave_ttl) == 120
    assert float(catalyst_ttl) == 300
    assert float(brave_ttl) != float(catalyst_ttl)


def test_p2_19_caches_use_different_modules_and_defaults():
    # Brave→LLM prompt path vs GDELT/RSS→signal path keep separate module-level
    # fallback constants so one TTL can never clobber the other.
    assert research._NEWS_CACHE_TTL_S_DEFAULT == 120
    assert news_catalyst._NEWS_CATALYST_DEFAULTS["ttl_sec"] == 300.0
    assert research._NEWS_CACHE is not news_catalyst._cache


def test_p2_19_catalyst_ttl_override_does_not_touch_brave_ttl():
    # Overriding the catalyst TTL leaves the Brave cache TTL untouched.
    cfg = {"news_catalyst": {"ttl_sec": 60.0}}
    assert float(cfg_get("news_catalyst.ttl_sec", config=cfg)) == 60.0
    assert float(cfg_get("news_cache_ttl_s", config=cfg)) == 120


# ── P2-20: shadow_book.max_positions tracks global max_concurrent ──────────

def test_p2_20_shadow_max_positions_absent_from_canonical_by_design():
    # The canonical shadow_book block deliberately omits max_positions; the
    # paper book derives the cap from the live max_concurrent to preserve 1:1
    # SHADOW/LIVE parity.
    assert "max_positions" not in CANONICAL_DEFAULTS["shadow_book"]


def test_p2_20_max_positions_falls_back_to_global_max_concurrent():
    live_cap = int(cfg_get("max_concurrent", 2))
    # _shadow_cfg has no max_positions key in canonical → falls back to live.
    assert shadow_book._max_positions() == live_cap


# ── P2-21: debate_gate passes only when BOTH knobs clear (stricter wins) ────

def _ctx(**kw):
    base = dict(
        confidence=0.9,
        current_positions=[],
        trade_notional_usd=10,
        daily_pnl=0,
        market_volume_24h_usd=1e8,
        coin="BTC",
        trade_side="long",
        has_binary_news_risk=False,
        equity=1000,
        total_open_notional=0,
        composite_score=50.0,
        # No trigger types fire → analyst 1 votes no; analyst 3 only yes via
        # analyst3_default=True in the config. This yields a deterministic 3/5
        # vote (analyst 3 regime + analyst 4 clean-news + analyst 5 conf floor).
        momentum_burst_fired=False,
        slow_burn_fired=False,
        whale_signal_fired=False,
    )
    base.update(kw)
    return RG.GateContext(**base)


# A ctx casting exactly 3/5 yes-votes when analyst3_default=True:
#   analyst1 trigger diversity    = NO  (no momentum/slow/whale trigger fired)
#   analyst2 conf/score consensus = NO  (conf 0.65 & score 50 clear none of the
#                                         0.7/40, 0.5/60, 0.8/20 vote bands)
#   analyst3 regime               = YES (only via analyst3_default=True)
#   analyst4 clean news           = YES (no binary-news risk)
#   analyst5 whale-or-conf        = YES (conf 0.65 >= the 0.62 whale-or floor)
# → 3 votes, ratio 0.60.
_CTX3 = dict(confidence=0.65, composite_score=50.0)

# 3/5 vote config: ratio 0.60, count 3.
_VOTE3 = {"debate_gate": {"enabled": True, "analyst3_default": True}}


def test_p2_21_canonical_knobs_coincide_under_five_role_vote():
    dg = CANONICAL_DEFAULTS["debate_gate"]
    assert dg["min_agreement"] == 0.4
    assert dg["min_agree_count"] == 2
    # 2/5 == 0.4 → the two knobs are numerically equivalent in production.
    assert dg["min_agree_count"] / 5 == pytest.approx(dg["min_agreement"])


def test_p2_21_ratio_knob_can_be_stricter_and_blocks():
    # 3 votes clear count>=2, but ratio 0.60 < a raised 0.7 → the ratio knob
    # blocks when it is the stricter of the two.
    cfg = {"debate_gate": {"enabled": True, "analyst3_default": True,
                           "min_agreement": 0.7, "min_agree_count": 2}}
    r = RG.debate_gate(_ctx(**_CTX3), cfg)
    assert r["pass"] is False
    assert r["agree_count"] == 3


def test_p2_21_count_knob_can_be_stricter_and_blocks():
    # 3 votes clear ratio>=0.4 (0.60 >= 0.4), but count 3 < a raised 4 → the
    # count knob blocks when it is the stricter of the two.
    cfg = {"debate_gate": {"enabled": True, "analyst3_default": True,
                           "min_agreement": 0.4, "min_agree_count": 4}}
    r = RG.debate_gate(_ctx(**_CTX3), cfg)
    assert r["pass"] is False
    assert r["agree_count"] == 3


def test_p2_21_production_knobs_pass_a_three_vote_trade():
    # Same 3/5 trade clears the production 0.4 / 2 knobs on BOTH conditions
    # (0.60 >= 0.4 AND 3 >= 2).
    r = RG.debate_gate(_ctx(**_CTX3), _VOTE3)
    assert r["pass"] is True
    assert r["agree_count"] == 3


# ── P2-22: _comment is the accepted operator note ─────────────────────────

def test_p2_22_comment_key_is_accepted_freeform_note():
    from hermes_trader.agents.config_store import validate_config_dict

    assert validate_config_dict({"_comment": "operator note"}) == []
    # And it survives a canonical read as the documented note slot.
    assert "_comment" in CANONICAL_DEFAULTS


# ── P2-23: the six thresholds are canonical + schema, not hardcoded ────────

@pytest.mark.parametrize("key", [
    "sl_ceiling_hard_max_pct",
    "max_atr_pct",
    "max_spread_pct",
    "liq_buffer_usd",
    "liquidation_maint_margin_pct",
    "neutral_threshold",
])
def test_p2_23_six_thresholds_are_in_canonical(key):
    assert key in CANONICAL_DEFAULTS, f"{key} missing from canonical"
    assert cfg_get(key, config={}) == CANONICAL_DEFAULTS[key]


def test_p2_23_six_thresholds_present_in_schema():
    from hermes_trader.agents.config_schema import _ConfigPatch

    fields = _ConfigPatch.model_fields
    for key in (
        "sl_ceiling_hard_max_pct",
        "max_atr_pct",
        "max_spread_pct",
        "liq_buffer_usd",
        "liquidation_maint_margin_pct",
        "neutral_threshold",
    ):
        assert key in fields, f"{key} not a pydantic schema field"


# ── P2-24: two leverage knobs are distinct and documented ─────────────────

def test_p2_24_two_leverage_knobs_are_distinct():
    # hl_client_io.default_leverage = cross-margin FALLBACK; top-level
    # `leverage` = trading/sizing leverage. They differ on purpose.
    assert CANONICAL_DEFAULTS["hl_client_io"]["default_leverage"] == 5
    assert CANONICAL_DEFAULTS["leverage"] == 10
    assert (
        CANONICAL_DEFAULTS["hl_client_io"]["default_leverage"]
        != CANONICAL_DEFAULTS["leverage"]
    )


def test_p2_24_executor_prefers_top_level_leverage():
    # Sizing reads config["leverage"]; HL_LEVERAGE (from hl_client_io) is only
    # the fallback when the top-level key is absent.
    src = __import__("inspect").getsource(executor)
    assert 'config.get("leverage", HL_LEVERAGE)' in src
