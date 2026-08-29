"""H3 (deep audit 2026-08-28): runtime arming of a FORBIDDEN_OVERRIDE
force-execute switch must leave a durable ``force_override_armed`` audit
line in events.jsonl whenever the armed state actually changes a trading
decision (PASS→LONG structural override, spread gate fail-open, whale
regime bypass). The schema side (arming requires override_requires_ai=true)
is covered in test_forbidden_override.py; this file covers the executor
runtime chokepoint and the sixth switch (ta_sidestep_force_execute).
"""

from __future__ import annotations

from hermes_trader.agents import executor
from hermes_trader.agents import config_schema


# ── schema / executor key lists stay in sync (six switches) ──────────────


def test_force_override_key_lists_include_ta_sidestep():
    """ta_sidestep_force_execute is consumed by _evaluate_force_override as
    a force-execute switch; it must be in both the schema whole-view gate
    and the executor audit list, or it can arm silently."""
    assert "ta_sidestep_force_execute" in config_schema._FORCE_OVERRIDE_KEYS_FOR_GATE
    assert "ta_sidestep_force_execute" in executor._FORCE_OVERRIDE_CONFIG_KEYS
    # The two runtime lists must cover exactly the same six switches.
    assert set(executor._FORCE_OVERRIDE_CONFIG_KEYS) == set(
        config_schema._FORCE_OVERRIDE_KEYS_FOR_GATE
    )


# ── _record_force_override_armed: durable audit line ─────────────────────


def test_record_force_override_armed_writes_event(monkeypatch):
    captured: list[dict] = []

    def fake_append(event, payload=None, trace_id="", **kw):
        captured.append({"event": event, "payload": payload, "trace_id": trace_id})
        return True

    # The helper imports event_log locally inside the call; patch the
    # real module's attribute so the `from hermes_trader import event_log`
    # binding picks up the fake.
    import hermes_trader.event_log as event_log_mod
    monkeypatch.setattr(event_log_mod, "append", fake_append)

    executor._record_force_override_armed(
        coin="ETH",
        trigger="structural_override:TA sidestep",
        config={
            "ta_sidestep_force_execute": True,
            "override_requires_ai": True,
            "whale_force_execute": False,
        },
        details={"subtests": {"ta_sidestep": True}},
        trace_id="trace-123",
    )

    assert len(captured) == 1
    rec = captured[0]
    assert rec["event"] == "force_override_armed"
    assert rec["trace_id"] == "trace-123"
    p = rec["payload"]
    assert p["coin"] == "ETH"
    assert p["trigger"] == "structural_override:TA sidestep"
    # Only the ARMED switch is listed; disarmed ones must not appear.
    assert p["armed_switches"] == {"ta_sidestep_force_execute": True}
    assert p["override_requires_ai"] is True
    assert p["details"]["subtests"] == {"ta_sidestep": True}


def test_record_force_override_armed_no_armed_switches(monkeypatch):
    """With every switch off, armed_switches is an empty dict (the line is
    still written — a call site only fires when a switch was consulted)."""
    captured: list[dict] = []

    def fake_append(event, payload=None, **kw):
        captured.append(payload)
        return True

    import hermes_trader.event_log as event_log_mod
    monkeypatch.setattr(event_log_mod, "append", fake_append)

    executor._record_force_override_armed(
        coin="BTC", trigger="spread_gate_fail_open", config={},
    )
    assert captured[0]["armed_switches"] == {}
    # override_requires_ai defaults to True (fail-safe) when absent.
    assert captured[0]["override_requires_ai"] is True


def test_record_force_override_armed_swallows_event_log_failure(monkeypatch):
    """Best-effort contract: an audit-feed failure must never block trading.
    The helper returns None instead of raising."""
    import hermes_trader.event_log as event_log_mod

    def boom(*a, **kw):
        raise OSError("audit feed down")

    monkeypatch.setattr(event_log_mod, "append", boom)
    # Must not raise.
    assert executor._record_force_override_armed(
        coin="BTC", trigger="whale_regime_bypass",
        config={"whale_regime_bypass": True},
    ) is None


# ── ta_sidestep sub-test inside _evaluate_force_override ─────────────────


def _ta_sidestep_analysis():
    return {
        "coin": "DOGE",
        "composite_score": 0.0,
        "slow_burn_count": 99,
        "momentum_burst_fired": True,
        "whale_signal": False,
        "volume_spike_fired": False,
        "breakout_fired": False,
        "uptrend_momentum_fired": False,
    }


def _ta_sidestep_config(**over):
    cfg = {
        # Bar unreachable by composite=0; the burst leg must still fire.
        "force_execute_composite": 999,
        "force_execute_slow_burn_count": 2,
        "ta_sidestep_min_slow_burn_count": 99,
    }
    cfg.update(over)
    return cfg


def test_ta_sidestep_override_requires_switch(monkeypatch):
    """The sixth force-execute switch: without ta_sidestep_force_execute the
    sidestep sub-test must not fire even when the signal conditions hold."""
    # Signal enforcement may touch caches; neutralize the local import.
    import sys
    import types
    fake_mod = types.SimpleNamespace(enforce_signals=lambda *a, **kw: None)
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.shadow_signals", fake_mod)

    off, det_off = executor._evaluate_force_override(
        _ta_sidestep_analysis(), _ta_sidestep_config()
    )
    assert det_off["ta_sidestep"] is False
    assert off is False

    on, det_on = executor._evaluate_force_override(
        _ta_sidestep_analysis(),
        _ta_sidestep_config(ta_sidestep_force_execute=True),
    )
    assert det_on["ta_sidestep"] is True
    assert on is True


def test_ta_sidestep_override_needs_enough_slow_burn(monkeypatch):
    """The slow-burn count floor still applies with the switch armed — the
    switch only arms the path, it doesn't waive its signal conditions."""
    import sys
    import types
    fake_mod = types.SimpleNamespace(enforce_signals=lambda *a, **kw: None)
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.shadow_signals", fake_mod)

    analysis = _ta_sidestep_analysis()
    analysis["slow_burn_count"] = 50  # below the 99 floor
    on, det = executor._evaluate_force_override(
        analysis, _ta_sidestep_config(ta_sidestep_force_execute=True)
    )
    assert det["ta_sidestep"] is False
    assert on is False
