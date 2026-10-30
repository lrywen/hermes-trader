"""M2 R1 ENFORCE (gray) tests: the kill-switch actually blocks mover-only longs."""
from __future__ import annotations

import pytest
from hermes_trader.agents import executor
from tests.test_m2_mover_bypass_veto import (
    _gate, _mover_analysis, _fresh_analysis, isolated, _spy)


def test_r1_enforce_blocks_mover_only(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(mover_bypass_veto={"shadow_mode": True, "enforce": True})
    res = executor._runner_entry_block_reason_impl(_mover_analysis(), cfg)
    assert res != ""
    assert "mover_bypass_veto ENFORCE" in res
    assert len(calls) == 1
    assert calls[0]["rule"] == "mover_bypass_veto"
    assert calls[0]["would"] == "block_enforced"
    assert calls[0]["detail"]["enforced"] is True


def test_r1_enforce_skips_fresh_impulse(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(mover_bypass_veto={"shadow_mode": True, "enforce": True})
    res = executor._runner_entry_block_reason_impl(_fresh_analysis(), cfg)
    assert res == ""  # fresh impulse still admits; veto only targets mover-only
    assert [c for c in calls if c.get("rule") == "mover_bypass_veto"] == []


def test_r1_enforce_false_still_record_only(isolated, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = _gate(mover_bypass_veto={"shadow_mode": True, "enforce": False})
    res = executor._runner_entry_block_reason_impl(_mover_analysis(), cfg)
    assert res == ""
    assert calls[0]["would"] == "block"
    assert calls[0]["detail"]["enforced"] is False


def test_r1_enforce_without_shadow_still_records(isolated, monkeypatch):
    # enforce implies recording even if shadow_mode is false
    calls = _spy(monkeypatch)
    cfg = _gate(mover_bypass_veto={"shadow_mode": False, "enforce": True})
    res = executor._runner_entry_block_reason_impl(_mover_analysis(), cfg)
    assert res != ""
    assert len(calls) == 1
    assert calls[0]["would"] == "block_enforced"
