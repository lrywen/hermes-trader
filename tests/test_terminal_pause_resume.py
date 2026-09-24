"""T-08 (DEF-07): terminal pause/resume mode semantics.

* pause  → records the current mode and flips OFF
* resume → restores the pre-pause mode (SHADOW stays SHADOW, no jump to LIVE)
* resume live → explicit LIVE arm (still B-13 gated; a raised guard is surfaced)
* resume with no recorded mode → error, never silently LIVE
"""
from __future__ import annotations

import asyncio

import pytest

from hermes_trader import dashboard


@pytest.fixture
def pause_state(tmp_path, monkeypatch):
    p = tmp_path / ".pre-pause-mode"
    monkeypatch.setattr(dashboard, "_PRE_PAUSE_STATE_PATH", str(p))
    return p


def _run(coro):
    # asyncio.run creates a fresh loop per call; get_event_loop() raises when
    # no loop exists yet (as happens when this file runs inside the full suite
    # rather than in isolation).
    return asyncio.run(coro)


class _FakeConfig:
    """In-memory stand-in for the config behind read/_apply."""
    def __init__(self, mode):
        self.mode = mode

    def apply(self, updates):
        old = {"mode": self.mode}
        # Simulate T-01 guard: arming LIVE is rejected.
        if updates.get("mode") == "LIVE":
            raise RuntimeError("no valid B-13 acceptance record")
        self.mode = updates["mode"]
        return {"old": old, "new": {"mode": self.mode}}


@pytest.fixture
def fake_cfg(monkeypatch):
    cfg = _FakeConfig("SHADOW")
    monkeypatch.setattr(dashboard, "read_agent_config", lambda: {"mode": cfg.mode})
    monkeypatch.setattr(
        dashboard, "_config_apply",
        lambda updates: cfg.apply(updates))
    return cfg


def _resp(parts):
    r = _run(dashboard._h_pause_resume(list(parts), parts[0]))
    return r.body.decode() if hasattr(r, "body") else str(r)


def test_pause_records_mode_and_goes_off(pause_state, fake_cfg):
    resp = _resp(("pause",))
    assert "OFF" in resp
    assert fake_cfg.mode == "OFF"
    assert dashboard._read_pre_pause_mode() == "SHADOW"


def test_resume_restores_shadow(pause_state, fake_cfg):
    _resp(("pause",))
    resp = _resp(("resume",))
    assert fake_cfg.mode == "SHADOW"
    assert "SHADOW" in resp
    # stored mode consumed
    assert dashboard._read_pre_pause_mode() is None


def test_resume_does_not_jump_to_live(pause_state, fake_cfg):
    _resp(("pause",))  # was SHADOW
    _resp(("resume",))
    assert fake_cfg.mode == "SHADOW"


def test_resume_without_record_refuses_live(pause_state, fake_cfg):
    # No pause first → no stored mode.
    resp = _resp(("resume",))
    assert "error" in resp.lower()
    assert fake_cfg.mode == "SHADOW"  # unchanged


def test_explicit_resume_live_is_gated(pause_state, fake_cfg):
    # FakeConfig.apply raises for LIVE (B-13) → handler surfaces an error and
    # the current mode is left untouched.
    resp = _resp(("resume", "live"))
    assert "error" in resp.lower()
    assert "B-13" in resp
    assert fake_cfg.mode == "SHADOW"


def test_live_roundtrip_when_armed(pause_state, monkeypatch):
    # If a LIVE arm is allowed (record valid), resume live flips to LIVE.
    cfg = _FakeConfig("SHADOW")

    def allow(updates):
        old = {"mode": cfg.mode}
        cfg.mode = updates["mode"]
        return {"old": old, "new": {"mode": cfg.mode}}

    monkeypatch.setattr(dashboard, "_config_apply", allow)
    monkeypatch.setattr(dashboard, "read_agent_config",
                        lambda: {"mode": cfg.mode})
    resp = _resp(("resume", "live"))
    assert cfg.mode == "LIVE"
    assert "LIVE" in resp
