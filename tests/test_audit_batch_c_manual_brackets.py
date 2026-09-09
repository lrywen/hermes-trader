"""Batch C — manual post-fill bracket hardening (server._place_manual_post_fill_brackets).

Guards the money-safety parity between a MANUAL web order and the autonomous
executor path:

  * stop width resolves through _resolve_sl_width_config and is clamped to the
    15% sl_ceiling_hard_max_pct (no HYPE-class 40% stop, no inverted stop);
  * slippage widening is bounded; direction is asserted (long sl<entry<tp);
  * tracker registration is preceded by load_state(force=True) so the web
    process never clobbers the trading loop's on-disk registry;
  * SL arms with ONE cloid, retries once after a generic failure, and claims a
    lost response by cloid (three-state lookup) before retrying;
  * a missing SL is LOUD: logger.error + Feishu alert + sl_missing/warnings;
  * atr<=0 cannot silently return an empty bracket list.
"""
from __future__ import annotations

import pytest

from hermes_trader import server


@pytest.fixture
def bracket_env(monkeypatch):
    """Patch every side effect the helper touches; record place calls."""
    calls: list[dict] = []
    sl_script: list[dict] = []
    tp_script: list[dict] = []
    claim = {"value": None}
    registered: list[dict] = []
    bracket_sets: list[dict] = []
    force_loads: list[bool] = []
    alerts: list[str] = []
    sleeps: list[float] = []

    def _fake_place(is_long_position, size, trigger_px, kind,
                    coin="BTC", limit_band_pct=None, cloid=None):
        calls.append({
            "is_long": is_long_position, "size": size, "px": trigger_px,
            "kind": kind, "coin": coin, "band": limit_band_pct,
            "cloid": cloid,
        })
        script = sl_script if kind == "sl" else tp_script
        if script:
            return script.pop(0)
        return {"ok": True, "order_id": 9000 + len(calls)}

    def _fake_find(coin, cloid, user=None):
        return claim["value"]

    import hermes_trader.client.exchange as exchange_mod
    monkeypatch.setattr(exchange_mod, "place_hl_trigger_order", _fake_place)
    monkeypatch.setattr(exchange_mod, "find_open_order_by_cloid", _fake_find)

    import hermes_trader.agents.dsl_exit as dsl_exit
    monkeypatch.setattr(
        dsl_exit, "load_state",
        lambda force=False: force_loads.append(force),
    )
    monkeypatch.setattr(
        dsl_exit, "register_position",
        lambda coin, side, entry_px, **kw: registered.append(
            {"coin": coin, "side": side, "entry_px": entry_px, **kw}),
    )
    monkeypatch.setattr(
        dsl_exit, "set_bracket",
        lambda coin, side, **fields: bracket_sets.append(fields) or True,
    )

    monkeypatch.setattr(
        server.memory, "avg_exit_slip_bps", lambda coin, days=None: 0.0)
    monkeypatch.setattr(server, "send_text",
                        lambda text, category="risk": alerts.append(text) or True,
                        raising=False)
    monkeypatch.setattr(server.time, "sleep", lambda s: sleeps.append(s))

    return {
        "calls": calls, "sl_script": sl_script, "tp_script": tp_script,
        "claim": claim, "registered": registered, "bracket_sets": bracket_sets,
        "force_loads": force_loads, "alerts": alerts, "sleeps": sleeps,
    }


def _run(**over):
    kw = dict(coin="BTC", is_buy=True, atr=1.0, entry_px=100.0,
              size_in_coin=0.5, leverage=5, cfg={})
    kw.update(over)
    return server._place_manual_post_fill_brackets(**kw)


def _sl_calls(env):
    return [c for c in env["calls"] if c["kind"] == "sl"]


def _tp_calls(env):
    return [c for c in env["calls"] if c["kind"] == "tp"]


def test_success_long_clamps_registers_and_persists_brackets(bracket_env):
    out = _run()
    assert out["sl_missing"] is False
    assert out["warnings"] == []
    # default mult 1.5, floor 1%, ceiling 3% → width 1.5% → sl=98.5, tp=101
    sl = _sl_calls(bracket_env)
    tp = _tp_calls(bracket_env)
    assert len(sl) == 1 and len(tp) == 1
    assert sl[0]["px"] == pytest.approx(98.5)
    assert sl[0]["px"] < 100.0 < tp[0]["px"]
    assert tp[0]["px"] == pytest.approx(101.0)
    # force-load BEFORE register, correct side/leverage/atr
    assert bracket_env["force_loads"] == [True]
    reg = bracket_env["registered"]
    assert len(reg) == 1
    assert reg[0]["side"] == "long" and reg[0]["leverage"] == 5
    assert reg[0]["entry_atr_pct"] == pytest.approx(1.0)
    # both bracket oids persisted on the tracker
    fields = {}
    for b in bracket_env["bracket_sets"]:
        fields.update(b)
    assert fields["sl_oid"] and fields["tp_oid"]
    assert fields["sl_px"] == pytest.approx(98.5)
    assert bracket_env["alerts"] == []


def test_ceiling_hard_max_15pct(bracket_env):
    # ATR=50 on px=100 → raw 75% stop; configured ceiling 50% must still be
    # pinned to the 15% hard max regardless of operator config.
    out = _run(atr=50.0, cfg={"sl_ceiling_pct": 50.0, "tp_atr_mult": 0.1})
    sl = _sl_calls(bracket_env)
    assert len(sl) == 1
    assert sl[0]["px"] == pytest.approx(85.0)  # 100 - 15%
    assert out["sl_missing"] is False


def test_short_side_triggers_are_directionally_correct(bracket_env):
    out = _run(is_buy=False)
    assert out["sl_missing"] is False
    sl = _sl_calls(bracket_env)
    tp = _tp_calls(bracket_env)
    assert sl[0]["px"] == pytest.approx(101.5)  # short stop ABOVE entry
    assert tp[0]["px"] == pytest.approx(99.0)   # short target BELOW entry
    assert bracket_env["registered"][0]["side"] == "short"


def test_sl_generic_failure_retries_once_then_succeeds(bracket_env):
    bracket_env["sl_script"][:] = [
        {"ok": False, "error": "429 rate limited"},
        {"ok": True, "order_id": 777},
    ]
    out = _run()
    sl = _sl_calls(bracket_env)
    assert len(sl) == 2
    assert sl[0]["cloid"] is sl[1]["cloid"]  # same cloid on plain retry
    assert bracket_env["sleeps"] == [2]
    assert out["sl_missing"] is False
    assert any(f.get("sl_oid") == 777 for f in bracket_env["bracket_sets"])


def test_sl_double_failure_is_loud(bracket_env):
    bracket_env["sl_script"][:] = [
        {"ok": False, "error": "boom1"},
        {"ok": False, "error": "boom2"},
    ]
    out = _run()
    assert out["sl_missing"] is True
    assert any("SL placement failed" in w for w in out["warnings"])
    assert len(bracket_env["alerts"]) == 1
    assert "止损" in bracket_env["alerts"][0]
    # TP still armed independently
    assert len(_tp_calls(bracket_env)) == 1
    sl_fields = [f for f in bracket_env["bracket_sets"] if "sl_oid" in f]
    assert sl_fields == []


def test_response_unknown_claimed_resting_order_no_retry(bracket_env):
    bracket_env["sl_script"][:] = [
        {"ok": False, "error": "timeout", "error_code": "response_unknown"},
    ]
    bracket_env["claim"]["value"] = {
        "found": True, "lookup_ok": True, "oid": 4242,
        "trigger_px": 98.4, "size": 0.5, "side": "S",
    }
    out = _run()
    sl = _sl_calls(bracket_env)
    assert len(sl) == 1  # claimed — never resubmitted
    assert out["sl_missing"] is False
    sl_fields = [f for f in bracket_env["bracket_sets"] if "sl_oid" in f][0]
    assert sl_fields["sl_oid"] == 4242
    assert sl_fields["sl_px"] == pytest.approx(98.4)
    assert bracket_env["sleeps"] == []


def test_response_unknown_confirmed_absent_retries_with_new_cloid(bracket_env):
    bracket_env["sl_script"][:] = [
        {"ok": False, "error": "timeout", "error_code": "response_unknown"},
        {"ok": True, "order_id": 888},
    ]
    bracket_env["claim"]["value"] = {"found": False, "lookup_ok": True}
    out = _run()
    sl = _sl_calls(bracket_env)
    assert len(sl) == 2
    assert sl[0]["cloid"] is not sl[1]["cloid"]  # fresh cloid after confirmed absence
    assert bracket_env["sleeps"] == [2]
    assert out["sl_missing"] is False


def test_response_unknown_lookup_failed_preserves_cloid_no_blind_retry(bracket_env):
    bracket_env["sl_script"][:] = [
        {"ok": False, "error": "timeout", "error_code": "response_unknown"},
    ]
    bracket_env["claim"]["value"] = {"found": False, "lookup_ok": False}
    out = _run()
    sl = _sl_calls(bracket_env)
    assert len(sl) == 1  # ambiguous — do not mint a new cloid / resubmit
    assert out["sl_missing"] is True
    assert len(bracket_env["alerts"]) == 1


def test_atr_missing_is_visible_not_silent_empty(bracket_env):
    out = _run(atr=0.0)
    assert out["sl_missing"] is True
    assert bracket_env["calls"] == []
    assert bracket_env["registered"] == []
    assert any("no bracket armed" in w for w in out["warnings"])
    assert len(bracket_env["alerts"]) == 1


def test_force_load_precedes_registration_no_clobber(bracket_env):
    order: list[str] = []
    import hermes_trader.agents.dsl_exit as dsl_exit
    bracket_env  # fixture sets patched attrs; wrap to record order
    orig_load = dsl_exit.load_state
    orig_reg = dsl_exit.register_position

    def _load(force=False):
        order.append("load")
        return orig_load(force=force)

    def _reg(coin, side, entry_px, **kw):
        order.append("register")
        return orig_reg(coin, side, entry_px, **kw)

    # re-patch on the live module (helper imports the module, not the names)
    import hermes_trader.client.exchange as _ex  # noqa: F401
    from unittest import mock
    with mock.patch.object(dsl_exit, "load_state", _load), \
         mock.patch.object(dsl_exit, "register_position", _reg):
        out = _run()
    assert out["sl_missing"] is False
    assert order[:2] == ["load", "register"]
