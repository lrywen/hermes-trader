"""C4 (HYPE RCA follow-up): shared stop-width config + trigger-limit band.

Covers two fixes from docs/HYPE_LIQUIDATION_RCA_2026-08-21.md §7:

* C4-2: the backup SL and the DSL atr_stop layer resolve their multiplier /
  floor from ONE canonical block (dsl_exit.atr_stop); top-level sl_* stay
  explicit overrides; the backup ceiling stays layer-specific (never inherits
  the wider DSL ceiling); coin_overrides floor still wins; bad values fall
  back to defaults.
* C4-3: place_hl_trigger_order / modify_sl_trigger gained an optional
  worst-case limit band. Default (no band) is unchanged market-on-trigger
  (isMarket=True, limit_px == triggerPx); a positive band arms a trigger
  LIMIT order (isMarket=False) whose limit_px sits the band on the adverse
  side of the trigger (sell → below, buy → above).
"""

from __future__ import annotations

import math

import pytest

from hermes_trader.client import exchange
from hermes_trader.agents import executor


# ── C4-2: _resolve_sl_width_config ──────────────────────────────────────────

def test_c4_2_shared_atr_stop_block_feeds_backup_sl():
    # No top-level sl_*: mult/floor come from the shared dsl_exit.atr_stop.
    cfg = {"dsl_exit": {"atr_stop": {"atr_mult": 2.0, "floor_pct": 1.4,
                                     "ceiling_pct": 4.0}}}
    w = executor._resolve_sl_width_config(cfg, "HYPE")
    assert w["sl_atr_mult"] == 2.0
    assert w["sl_floor_pct"] == 1.4
    # Ceiling is deliberately NOT shared (backup keeps its own 3% default).
    assert w["sl_ceiling_pct"] == executor._DEFAULT_SL_CEILING_PCT == 3.0


def test_c4_2_toplevel_override_wins_over_shared():
    cfg = {"sl_atr_mult": 1.8, "sl_floor_pct": 1.6, "sl_ceiling_pct": 2.5,
           "dsl_exit": {"atr_stop": {"atr_mult": 1.5, "floor_pct": 1.0,
                                     "ceiling_pct": 4.0}}}
    w = executor._resolve_sl_width_config(cfg, "HYPE")
    assert w["sl_atr_mult"] == 1.8
    assert w["sl_floor_pct"] == 1.6
    assert w["sl_ceiling_pct"] == 2.5


def test_c4_2_coin_override_floor_has_top_priority():
    cfg = {"sl_floor_pct": 1.2,
           "dsl_exit": {"atr_stop": {"floor_pct": 1.0}},
           "atr_risk_sizing": {"coin_overrides": {"BOME": {"sl_floor_pct": 2.2}}}}
    assert executor._resolve_sl_width_config(cfg, "BOME")["sl_floor_pct"] == 2.2
    # Other coins unaffected.
    assert executor._resolve_sl_width_config(cfg, "HYPE")["sl_floor_pct"] == 1.2


def test_c4_2_defaults_when_config_empty():
    w = executor._resolve_sl_width_config({}, "HYPE")
    assert w["sl_atr_mult"] == executor._DEFAULT_SL_ATR_MULT
    assert w["sl_floor_pct"] == executor._DEFAULT_SL_FLOOR_PCT == 1.0
    assert w["sl_ceiling_pct"] == executor._DEFAULT_SL_CEILING_PCT


def test_c4_2_shared_ceiling_never_loosens_backup_ceiling():
    # A wide DSL ceiling (4%) must NOT bleed into the backup ceiling even when
    # no top-level ceiling is set (HYPE P0 clamp).
    cfg = {"dsl_exit": {"atr_stop": {"ceiling_pct": 9.0}}}
    w = executor._resolve_sl_width_config(cfg, "HYPE")
    assert w["sl_ceiling_pct"] == executor._DEFAULT_SL_CEILING_PCT


# ── C4-3: trigger-limit band ────────────────────────────────────────────────

class _FakeExchange:
    """Captures the wire arguments handed to the HL SDK."""

    def __init__(self):
        self.order_calls = []
        self.modify_calls = []

    @staticmethod
    def _resting(oid):
        return {"status": "ok",
                "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False):
        self.order_calls.append({"name": name, "is_buy": is_buy, "sz": sz,
                                 "limit_px": limit_px, "order_type": order_type,
                                 "reduce_only": reduce_only})
        return self._resting(12345)

    def modify_order(self, oid, name, is_buy, sz, limit_px, order_type,
                     reduce_only=False):
        self.modify_calls.append({"oid": oid, "name": name, "is_buy": is_buy,
                                  "sz": sz, "limit_px": limit_px,
                                  "order_type": order_type,
                                  "reduce_only": reduce_only})
        return self._resting(67890)


@pytest.fixture()
def _trigger_env(monkeypatch):
    monkeypatch.setattr(exchange, "PRIVATE_KEY_HEX", "0xabc")
    monkeypatch.setattr(exchange, "get_coin_index",
                        lambda coin: (0, 2, 2))  # 2 dp size/price
    fake = _FakeExchange()
    monkeypatch.setattr(exchange, "_make_exchange", lambda: fake)
    return fake


def test_c4_3_default_market_trigger_is_unchanged(_trigger_env):
    res = exchange.place_hl_trigger_order(
        is_long_position=True, size=1.0, trigger_px=100.0, kind="sl",
        coin="HYPE")
    assert res["ok"]
    call = _trigger_env.order_calls[-1]
    trig = call["order_type"]["trigger"]
    assert trig["isMarket"] is True
    assert trig["triggerPx"] == 100.0
    assert trig["tpsl"] == "sl"
    # Market mode: limit_px is just the reference == trigger.
    assert call["limit_px"] == 100.0
    assert call["reduce_only"] is True


def test_c4_3_long_sl_band_places_limit_below_trigger(_trigger_env):
    # Long SL closes by SELLING → worst-case limit sits BELOW trigger.
    res = exchange.place_hl_trigger_order(
        is_long_position=True, size=1.0, trigger_px=100.0, kind="sl",
        coin="HYPE", limit_band_pct=2.0)
    assert res["ok"]
    call = _trigger_env.order_calls[-1]
    trig = call["order_type"]["trigger"]
    assert trig["isMarket"] is False
    assert trig["triggerPx"] == 100.0
    # limit_px = 100 * (1 - 0.02) = 98.0
    assert call["limit_px"] == pytest.approx(98.0, abs=0.01)
    assert call["limit_px"] < 100.0


def test_c4_3_short_sl_band_places_limit_above_trigger(_trigger_env):
    # Short SL closes by BUYING → worst-case limit sits ABOVE trigger.
    res = exchange.place_hl_trigger_order(
        is_long_position=False, size=1.0, trigger_px=100.0, kind="sl",
        coin="HYPE", limit_band_pct=3.0)
    assert res["ok"]
    call = _trigger_env.order_calls[-1]
    trig = call["order_type"]["trigger"]
    assert trig["isMarket"] is False
    assert call["is_buy"] is True
    # limit_px = 100 * (1 + 0.03) = 103.0
    assert call["limit_px"] == pytest.approx(103.0, abs=0.01)
    assert call["limit_px"] > 100.0


def test_c4_3_zero_band_is_market(_trigger_env):
    res = exchange.place_hl_trigger_order(
        is_long_position=True, size=1.0, trigger_px=100.0, kind="sl",
        coin="HYPE", limit_band_pct=0.0)
    assert res["ok"]
    assert _trigger_env.order_calls[-1]["order_type"]["trigger"]["isMarket"] is True


def test_c4_3_tp_kind_uses_tpsl_tp(_trigger_env):
    res = exchange.place_hl_trigger_order(
        is_long_position=True, size=1.0, trigger_px=110.0, kind="tp",
        coin="HYPE", limit_band_pct=1.0)
    assert res["ok"]
    assert _trigger_env.order_calls[-1]["order_type"]["trigger"]["tpsl"] == "tp"


def test_c4_3_modify_default_market_keeps_limit_equals_trigger(_trigger_env):
    res = exchange.modify_sl_trigger(
        is_long_position=True, size=1.0, new_trigger_px=105.0,
        coin="HYPE", oid=555)
    assert res["ok"]
    call = _trigger_env.modify_calls[-1]
    assert call["oid"] == 555
    assert call["order_type"]["trigger"]["isMarket"] is True
    assert call["limit_px"] == 105.0


def test_c4_3_modify_long_band_moves_limit_below(_trigger_env):
    res = exchange.modify_sl_trigger(
        is_long_position=True, size=1.0, new_trigger_px=105.0,
        coin="HYPE", oid=555, limit_band_pct=2.0)
    assert res["ok"]
    call = _trigger_env.modify_calls[-1]
    assert call["order_type"]["trigger"]["isMarket"] is False
    # 105 * 0.98 = 102.9
    assert call["limit_px"] == pytest.approx(102.9, abs=0.01)
    assert call["limit_px"] < 105.0


# ── C4-3: executor-side band resolution ─────────────────────────────────────

def test_c4_3_band_defaults_to_market():
    w = executor._resolve_sl_width_config({}, "HYPE")
    assert w["sl_limit_band_pct"] == 0.0


def test_c4_3_band_resolves_from_config():
    w = executor._resolve_sl_width_config({"sl_limit_band_pct": 1.5}, "HYPE")
    assert w["sl_limit_band_pct"] == 1.5


def test_c4_3_negative_band_falls_back_to_market():
    w = executor._resolve_sl_width_config({"sl_limit_band_pct": -2.0}, "HYPE")
    assert w["sl_limit_band_pct"] == 0.0


def test_c4_3_nonfinite_band_falls_back_to_market():
    w = executor._resolve_sl_width_config(
        {"sl_limit_band_pct": float("nan")}, "HYPE")
    assert w["sl_limit_band_pct"] == 0.0
    assert math.isfinite(w["sl_limit_band_pct"])
