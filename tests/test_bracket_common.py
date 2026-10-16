"""P1 (audit 2026-10-15): shared post-fill bracket primitives.

Covers bracket_common.validate_clamped_sl_widths (post-HYPE clamp rules) and
place_backup_stop_once (one-cloid / claim-before-retry / one-retry state
machine) plus the manual-path emergency-flatten parity.
"""
from __future__ import annotations

import logging

from hermes_trader.agents.bracket_common import (
    place_backup_stop_once,
    validate_clamped_sl_widths,
)
from hermes_trader.agents.executor import _should_emergency_flatten

_DEFAULTS = dict(
    default_atr_mult=1.5, default_ceiling_pct=3.0, default_floor_pct=1.0,
    default_hard_max_pct=15.0,
)


def _clamp(**kw):
    base = dict(sl_atr_mult=1.2, sl_ceiling_pct=3.0, sl_floor_pct=1.2,
                sl_limit_band_pct=0.0, hard_max_pct=15.0)
    base.update(kw)
    base.update(_DEFAULTS)
    return validate_clamped_sl_widths(**base)


# ── width validation / clamping ────────────────────────────────────────────

def test_valid_widths_pass_through():
    out = _clamp()
    assert out == {"sl_atr_mult": 1.2, "sl_ceiling_pct": 3.0,
                   "sl_floor_pct": 1.2, "sl_limit_band_pct": 0.0}


def test_invalid_atr_mult_falls_back():
    out = _clamp(sl_atr_mult=0.0)
    assert out["sl_atr_mult"] == 1.5


def test_invalid_ceiling_falls_back():
    assert _clamp(sl_ceiling_pct=float("nan"))["sl_ceiling_pct"] == 3.0


def test_ceiling_capped_at_hard_max():
    out = _clamp(sl_ceiling_pct=40.0, hard_max_pct=15.0)
    assert out["sl_ceiling_pct"] == 15.0


def test_invalid_hard_max_uses_default():
    out = _clamp(sl_ceiling_pct=40.0, hard_max_pct=-1.0)
    assert out["sl_ceiling_pct"] == 15.0  # default hard max


def test_floor_pinned_to_ceiling():
    out = _clamp(sl_floor_pct=5.0, sl_ceiling_pct=3.0)
    assert out["sl_floor_pct"] == 3.0


def test_band_pinned_to_ceiling():
    out = _clamp(sl_limit_band_pct=8.0, sl_ceiling_pct=3.0)
    assert out["sl_limit_band_pct"] == 3.0


def test_logger_receives_warnings():
    msgs = []
    out = validate_clamped_sl_widths(
        sl_atr_mult=1.2, sl_ceiling_pct=99.0, sl_floor_pct=1.0,
        sl_limit_band_pct=0.0, hard_max_pct=15.0, log=msgs.append, **_DEFAULTS)
    assert out["sl_ceiling_pct"] == 15.0
    assert any("hard max" in m for m in msgs)


# ── idempotent stop placement state machine ─────────────────────────────────

def _place_seq(seq, claims=None):
    """seq: list of result dicts returned by successive place_order calls.
    claims: list of claim lookup dicts returned by find_by_cloid."""
    calls = {"place": [], "find": [], "sleeps": []}
    claims = list(claims or [])

    def place_order(is_buy, size, px, kind, coin, **kw):
        calls["place"].append({"px": px, "cloid": kw.get("cloid"),
                               "band": kw.get("limit_band_pct")})
        return seq[min(len(calls["place"]) - 1, len(seq) - 1)]

    def find_by_cloid(coin, cloid):
        calls["find"].append(cloid)
        return claims[min(len(calls["find"]) - 1, len(claims) - 1)]

    out = place_backup_stop_once(
        is_buy=True, size_in_coin=1.0, sl_px=100.0, coin="BTC",
        limit_band_pct=0.5, place_order=place_order,
        find_by_cloid=find_by_cloid, sleep=calls["sleeps"].append,
        log=lambda lvl, m: None)
    return out, calls


def test_first_try_success_places_once():
    out, calls = _place_seq([{"ok": True, "order_id": "O1"}])
    assert out["ok"] is True and out["order_id"] == "O1" and not out["missing"]
    assert len(calls["place"]) == 1
    assert calls["place"][0]["band"] == 0.5


def test_transient_failure_retries_same_cloid_once():
    out, calls = _place_seq([{"ok": False, "error": "429"},
                             {"ok": True, "order_id": "O2"}])
    assert out["ok"] is True
    assert len(calls["place"]) == 2
    assert calls["sleeps"] == [2]
    # same cloid reused on the ordinary retry
    assert calls["place"][0]["cloid"] is calls["place"][1]["cloid"]


def test_lost_response_claimed_resting_adopts_oid():
    seq = [{"ok": False, "error_code": "response_unknown"},
           {"ok": False, "error": "should not reach"}]
    claim = {"found": True, "oid": "REST", "trigger_px": 99.5, "lookup_ok": True}
    out, calls = _place_seq(seq, claims=[claim])
    assert out["ok"] is True and out["order_id"] == "REST"
    assert out["sl_px"] == 99.5
    assert len(calls["place"]) == 1  # adopted, no re-submit


def test_lost_response_confirmed_absent_retries_new_cloid():
    seq = [{"ok": False, "error_code": "response_unknown"},
           {"ok": True, "order_id": "O3"}]
    claim = {"found": False, "lookup_ok": True}
    out, calls = _place_seq(seq, claims=[claim])
    assert out["ok"] is True and out["order_id"] == "O3"
    assert len(calls["place"]) == 2
    # a NEW cloid must be minted for the confirmed-absent retry
    assert calls["place"][0]["cloid"] is not calls["place"][1]["cloid"]


def test_lost_response_lookup_unreadable_no_blind_retry():
    seq = [{"ok": False, "error_code": "response_unknown"}]
    claim = {"found": False, "lookup_ok": False}
    out, calls = _place_seq(seq, claims=[claim])
    assert out["missing"] is True
    assert len(calls["place"]) == 1  # never blind-retry; cloid preserved
    assert out["cloid"] is calls["place"][0]["cloid"]


def test_double_failure_marks_missing():
    out, calls = _place_seq([{"ok": False, "error": "500"},
                             {"ok": False, "error": "500"}])
    assert out["missing"] is True
    assert len(calls["place"]) == 2


# ── emergency-flatten predicate parity (auto == manual) ─────────────────────

def test_flatten_only_live_and_armed():
    assert _should_emergency_flatten(
        sl_missing=True, mode="LIVE",
        config={"flatten_on_sl_missing": True}) is True
    assert _should_emergency_flatten(
        sl_missing=True, mode="SHADOW",
        config={"flatten_on_sl_missing": True}) is False
    assert _should_emergency_flatten(
        sl_missing=True, mode="LIVE",
        config={"flatten_on_sl_missing": False}) is False
    assert _should_emergency_flatten(
        sl_missing=False, mode="LIVE",
        config={"flatten_on_sl_missing": True}) is False


def test_manual_module_imports_clean(caplog):
    # importing server must not raise and must retain the bracket helper
    import hermes_trader.server as srv
    assert hasattr(srv, "_place_manual_post_fill_brackets")
    # no warnings during import
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
