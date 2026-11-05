"""Tests for hermes_trader/agents/manual_control.py (S5 manual intervention).

Covers:
  * mode switch (auto / manual_confirm / manual_only) with audit
  * per-coin pause / resume / is_paused
  * manual SL/TP set / clear / get with validation
  * should_skip_auto combined gate
  * snapshot shape
  * fail-safe defaults when state file is absent
  * case-insensitive coin normalisation
"""
from __future__ import annotations

import json
import os

import pytest

from hermes_trader.agents import manual_control as mc


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Redirect state and audit files to tmp_path for every test."""
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    yield


class TestMode:
    def test_default_mode_is_auto(self):
        assert mc.get_mode() == "auto"
        assert mc.auto_entries_blocked() is False
        assert mc.confirmation_required() is False

    def test_set_and_read_mode(self):
        r = mc.set_mode("manual_only", actor="alice", reason="incident")
        assert r == {"mode": "manual_only", "previous": "auto"}
        assert mc.get_mode() == "manual_only"
        assert mc.auto_entries_blocked() is True
        assert mc.confirmation_required() is False

    def test_manual_confirm_mode(self):
        mc.set_mode("manual_confirm", actor="bob")
        assert mc.confirmation_required() is True
        assert mc.auto_entries_blocked() is False

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="invalid mode"):
            mc.set_mode("turbo")

    def test_mode_change_audited(self):
        mc.set_mode("manual_only", actor="alice")
        audit_path = os.path.join(os.environ["HERMES_DATA_DIR"],
                                  "manual_control_audit.jsonl")
        with open(audit_path) as f:
            records = [json.loads(line) for line in f if line.strip()]
        mode_records = [r for r in records if r["action"] == "set_mode"]
        assert len(mode_records) == 1
        assert mode_records[0]["to"] == "manual_only"
        assert mode_records[0]["actor"] == "alice"


class TestCoinPause:
    def test_pause_and_check(self):
        mc.pause_coin("btc", actor="alice", reason="volatility")
        assert mc.is_coin_paused("BTC") is True
        assert mc.is_coin_paused("ETH") is False

    def test_case_insensitive(self):
        mc.pause_coin("eth")
        assert mc.is_coin_paused("ETH") is True

    def test_resume(self):
        mc.pause_coin("SOL")
        r = mc.resume_coin("sol", actor="bob")
        assert r == {"coin": "SOL", "paused": False, "was_paused": True}
        assert mc.is_coin_paused("SOL") is False

    def test_resume_never_paused_is_noop(self):
        r = mc.resume_coin("XRP")
        assert r["was_paused"] is False

    def test_paused_snapshot(self):
        mc.pause_coin("BTC")
        mc.pause_coin("ETH")
        snap = mc.paused_coins_snapshot()
        assert set(snap.keys()) == {"BTC", "ETH"}

    def test_pause_audited(self):
        mc.pause_coin("DOGE", actor="alice")
        audit_path = os.path.join(os.environ["HERMES_DATA_DIR"],
                                  "manual_control_audit.jsonl")
        with open(audit_path) as f:
            records = [json.loads(line) for line in f if line.strip()]
        assert any(r["action"] == "pause_coin" and r["coin"] == "DOGE"
                   for r in records)


class TestManualExit:
    def test_set_sl_and_tp(self):
        r = mc.set_manual_exit("BTC", sl_pct=2.0, tp_pct=5.0, actor="alice")
        assert r["sl_pct"] == 2.0
        assert r["tp_pct"] == 5.0
        got = mc.get_manual_exit("btc")
        assert got["sl_pct"] == 2.0

    def test_partial_update_preserves_other_side(self):
        mc.set_manual_exit("ETH", sl_pct=1.5, tp_pct=4.0)
        mc.set_manual_exit("ETH", sl_pct=2.0)  # only SL
        got = mc.get_manual_exit("ETH")
        assert got["sl_pct"] == 2.0
        assert got["tp_pct"] == 4.0

    def test_invalid_sl_raises(self):
        with pytest.raises(ValueError):
            mc.set_manual_exit("BTC", sl_pct=-1.0)
        with pytest.raises(ValueError):
            mc.set_manual_exit("BTC", sl_pct=0)

    def test_invalid_tp_raises(self):
        with pytest.raises(ValueError):
            mc.set_manual_exit("BTC", tp_pct=-2.0)

    def test_clear(self):
        mc.set_manual_exit("SOL", sl_pct=1.0)
        r = mc.clear_manual_exit("sol")
        assert r["cleared"] is True
        assert mc.get_manual_exit("SOL") is None

    def test_clear_never_set_is_noop(self):
        r = mc.clear_manual_exit("XRP")
        assert r["cleared"] is False

    def test_snapshot(self):
        mc.set_manual_exit("BTC", sl_pct=2.0)
        snap = mc.manual_exits_snapshot()
        assert "BTC" in snap


class TestShouldSkipAuto:
    def test_auto_mode_no_pause(self):
        skip, reason = mc.should_skip_auto("BTC")
        assert skip is False
        assert reason == ""

    def test_manual_only_blocks_all(self):
        mc.set_mode("manual_only")
        skip, reason = mc.should_skip_auto("BTC")
        assert skip is True
        assert reason == "manual_only_mode"

    def test_paused_coin_blocked(self):
        mc.pause_coin("ETH")
        skip, reason = mc.should_skip_auto("ETH")
        assert skip is True
        assert "coin_paused" in reason

    def test_unpaused_coin_not_blocked_in_auto(self):
        mc.pause_coin("ETH")
        skip, _ = mc.should_skip_auto("BTC")
        assert skip is False


class TestSnapshot:
    def test_full_snapshot_shape(self):
        mc.set_mode("manual_confirm", actor="alice")
        mc.pause_coin("BTC")
        mc.set_manual_exit("ETH", sl_pct=1.5, tp_pct=3.0)
        snap = mc.snapshot()
        assert snap["mode"] == "manual_confirm"
        assert "BTC" in snap["paused_coins"]
        assert "ETH" in snap["manual_exits"]
        assert snap["auto_entries_blocked"] is False
        assert snap["confirmation_required"] is True

    def test_snapshot_is_copy(self):
        mc.pause_coin("BTC")
        snap = mc.snapshot()
        snap["paused_coins"]["INJECTED"] = {}
        assert "INJECTED" not in mc.paused_coins_snapshot()


class TestPersistence:
    def test_state_survives_reload(self, tmp_path):
        mc.set_mode("manual_only")
        mc.pause_coin("BTC", reason="test")
        mc.set_manual_exit("ETH", sl_pct=2.0)
        # Simulate a fresh process by creating a new logical load (same file).
        state = mc._load()
        assert state["mode"] == "manual_only"
        assert "BTC" in state["paused_coins"]
        assert state["manual_exits"]["ETH"]["sl_pct"] == 2.0

    def test_corrupt_state_returns_fresh(self, tmp_path):
        state_file = tmp_path / "manual_control.json"
        state_file.write_text("{not valid json")
        assert mc.get_mode() == "auto"
        assert mc.is_coin_paused("BTC") is False
