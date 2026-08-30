"""C3 (HYPE RCA 2026-08-21 item 5): blow-up-level self-halt.

A single closing trade whose leveraged ROE loss breaches
``roe_halt_threshold_pct`` (default -50%) flips the bot to mode=OFF and
fires a risk alert + audit event. These tests pin the opt-in / fail-safe
behaviour of `maybe_roe_blowup_halt`:

  * default OFF -> never acts;
  * only acts on a loss AS BAD AS / WORSE than the (negative) threshold;
  * switches mode via update_agent_config exactly once (idempotent when OFF);
  * ignores non-finite / missing ROE (no-fill path);
  * never raises even when every side effect blows up;
  * event_log sink is called with the audit payload when injected.
"""

import contextlib

import pytest

from hermes_trader.agents import executor


@pytest.fixture
def halt_env(monkeypatch):
    """Wire the halt helper with in-memory fakes (no real config file/IO)."""
    state = {
        "enabled": True,
        "threshold": -50.0,
        "mode": "LIVE",
        "writes": 0,          # number of update_agent_config RMW blocks entered
        "cards": [],          # notify.send_card calls captured as kwargs
        "events": [],         # injected event_log calls
    }

    def _fake_read_config():
        return {"mode": state["mode"],
                "roe_halt_enabled": state["enabled"],
                "roe_halt_threshold_pct": state["threshold"]}

    def _fake_cfg_get(key, config=None, default=None):
        src = config if config is not None else _fake_read_config()
        # support dotted keys if ever used; here keys are top-level
        node = src
        for part in str(key).split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    @contextlib.contextmanager
    def _fake_update(backup=True):
        cfg = _fake_read_config()
        yield cfg
        # persist the mode switch back into state, mirroring the RMW write
        state["mode"] = cfg.get("mode", state["mode"])
        state["writes"] += 1

    def _fake_send_card(title, **kwargs):
        state["cards"].append({"title": title, **kwargs})
        return True

    monkeypatch.setattr(executor, "read_agent_config", _fake_read_config)
    monkeypatch.setattr(executor, "cfg_get", _fake_cfg_get)
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.update_agent_config",
        _fake_update, raising=False)
    monkeypatch.setattr(
        "hermes_trader.notify.send_card", _fake_send_card, raising=False)

    def _event_log(rec):
        state["events"].append(rec)

    return state, _event_log


def test_disabled_by_default_does_nothing(monkeypatch):
    """With the switch absent/False the helper must never act."""
    monkeypatch.setattr(executor, "read_agent_config",
                        lambda: {"mode": "LIVE"})
    monkeypatch.setattr(executor, "cfg_get",
                        lambda k, config=None, default=None: default)
    called = {"n": 0}

    @contextlib.contextmanager
    def _boom(backup=True):
        called["n"] += 1
        yield {}

    monkeypatch.setattr(
        "hermes_trader.agents.config_store.update_agent_config", _boom,
        raising=False)
    fired = executor.maybe_roe_blowup_halt("HYPE", -252.0)
    assert fired is False
    assert called["n"] == 0


def test_hypothetical_hype_loss_hhalts_and_switches_off(halt_env):
    """-252% ROE (the HYPE incident) must trip at the -50% default."""
    state, event_log = halt_env
    fired = executor.maybe_roe_blowup_halt("HYPE", -252.37, source="close",
                                           event_log=event_log)
    assert fired is True
    assert state["mode"] == "OFF"
    assert state["writes"] == 1
    assert len(state["cards"]) == 1
    assert state["cards"][0]["level"] == "danger"
    assert state["cards"][0]["category"] == "risk"
    assert state["cards"][0]["dedup_key"] == "roe_halt:HYPE"
    assert state["events"] and state["events"][0]["event"] == "roe_halt"
    assert state["events"][0]["mode_switched"] is True
    assert state["events"][0]["source"] == "close"


def test_loss_better_than_threshold_does_not_halt(halt_env):
    """A -20% ROE loss is bad but below the -50% blow-up line -> no halt."""
    state, event_log = halt_env
    fired = executor.maybe_roe_blowup_halt("DOGE", -20.0, event_log=event_log)
    assert fired is False
    assert state["mode"] == "LIVE"
    assert state["writes"] == 0
    assert state["cards"] == []


def test_boundary_value_at_threshold_hhalts(halt_env):
    """Exactly at the threshold (-50%) trips (<= semantics)."""
    state, event_log = halt_env
    fired = executor.maybe_roe_blowup_halt("X", -50.0, event_log=event_log)
    assert fired is True
    assert state["mode"] == "OFF"


def test_profit_or_small_loss_never_hhalts(halt_env):
    state, event_log = halt_env
    for roe in (5.0, 0.0, -5.0):
        assert executor.maybe_roe_blowup_halt("X", roe) is False
    assert state["writes"] == 0


def test_non_finite_and_missing_roe_ignored(halt_env):
    """No-fill / degraded paths must not trip the halt."""
    state, event_log = halt_env
    for roe in (None, float("nan"), float("inf"), "not-a-number"):
        assert executor.maybe_roe_blowup_halt("X", roe, event_log=event_log) is False
    assert state["writes"] == 0
    assert state["cards"] == []


def test_already_off_skips_config_write_but_alerts(halt_env):
    """Idempotent: if already OFF, don't RMW the config again."""
    state, event_log = halt_env
    state["mode"] = "OFF"
    fired = executor.maybe_roe_blowup_halt("HYPE", -99.0,
                                           source="exchange_trigger",
                                           event_log=event_log)
    assert fired is True
    assert state["writes"] == 0              # no redundant config write
    assert len(state["cards"]) == 1          # but the alert still fires
    assert state["events"][0]["mode_switched"] is False
    assert state["events"][0]["source"] == "exchange_trigger"


def test_never_raises_when_side_effects_fail(halt_env, monkeypatch):
    """A failing config write / notify must not propagate out of the close path."""
    state, event_log = halt_env

    @contextlib.contextmanager
    def _boom(backup=True):
        raise RuntimeError("config file corrupt")
        yield  # pragma: no cover

    monkeypatch.setattr(
        "hermes_trader.agents.config_store.update_agent_config", _boom,
        raising=False)
    # Must return False (halt not confirmed) and swallow the error.
    fired = executor.maybe_roe_blowup_halt("HYPE", -252.0, event_log=event_log)
    assert fired is False
