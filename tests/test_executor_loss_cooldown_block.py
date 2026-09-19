"""Characterization tests for the S5 loss-cooldown decision leaf.

The helper is a pre-lock, read-only gate; these tests pin its block/bypass
behaviour after the verbatim extraction from maybe_execute.
"""

from types import SimpleNamespace

from hermes_trader.agents import executor
from hermes_trader.agents.executor import _loss_cooldown_block

ON = {"momentum_reentry": {"enabled": True, "reclaim_pct": 1.0, "min_composite": 30}}
OFF = {"momentum_reentry": {"enabled": False}}


def _analysis(mid=101.5, composite=40):
    return {"coin": "BTC", "id": "aid-1", "mid": mid, "composite_score": composite}


_UNQUERIED = object()


def _memory(remaining, last_close=_UNQUERIED):
    def _last_close_for(coin):
        if last_close is _UNQUERIED:
            raise AssertionError("last_close_for must not be queried without an active cooldown")
        return last_close

    return SimpleNamespace(
        loss_cooldown_remaining_min=lambda coin: remaining,
        last_close_for=_last_close_for,
    )


def test_no_cooldown_returns_none_without_touching_last_close(monkeypatch):
    monkeypatch.setattr(executor, "memory", _memory(0))
    assert _loss_cooldown_block(analysis=_analysis(), mode="live", config=ON) is None


def test_negative_cooldown_returns_none(monkeypatch):
    monkeypatch.setattr(executor, "memory", _memory(-5))
    assert _loss_cooldown_block(analysis=_analysis(), mode="live", config=ON) is None


def test_momentum_bypass_returns_none_and_logs(monkeypatch, caplog):
    # stopped long at 100, price reclaimed to 101.5 (+1.5% > 1%), comp 40 >= 30
    monkeypatch.setattr(
        executor, "memory",
        _memory(42, {"exit_px": 100.0, "side": "long"}),
    )
    with caplog.at_level("INFO"):
        result = _loss_cooldown_block(analysis=_analysis(), mode="live", config=ON)
    assert result is None
    assert "momentum re-entry on BTC" in caplog.text
    assert "42min loss cooldown" in caplog.text


def test_active_cooldown_without_reclaim_blocks(monkeypatch):
    # price still below the stop (falling knife) -> block
    monkeypatch.setattr(
        executor, "memory",
        _memory(37, {"exit_px": 100.0, "side": "long"}),
    )
    result = _loss_cooldown_block(analysis=_analysis(mid=98.0, composite=50),
                                  mode="live", config=ON)
    assert result == {
        "executed": False,
        "mode": "live",
        "analysis_id": "aid-1",
        "reason": "loss_cooldown (BTC closed at a loss recently — 37min remaining)",
    }


def test_missing_last_close_record_blocks(monkeypatch):
    # last_close_for -> None is normalised to {}; predicate then sees None inputs
    monkeypatch.setattr(executor, "memory", _memory(10, None))
    result = _loss_cooldown_block(analysis=_analysis(), mode="shadow", config=ON)
    assert result is not None
    assert result["executed"] is False
    assert result["mode"] == "shadow"
    assert result["analysis_id"] == "aid-1"
    assert "BTC" in result["reason"] and "10min remaining" in result["reason"]


def test_disabled_bypass_config_blocks(monkeypatch):
    monkeypatch.setattr(
        executor, "memory",
        _memory(12, {"exit_px": 100.0, "side": "long"}),
    )
    result = _loss_cooldown_block(analysis=_analysis(mid=105.0, composite=99),
                                  mode="live", config=OFF)
    assert result is not None
    assert result["reason"] == "loss_cooldown (BTC closed at a loss recently — 12min remaining)"


def test_short_side_never_bypasses(monkeypatch):
    monkeypatch.setattr(
        executor, "memory",
        _memory(30, {"exit_px": 100.0, "side": "short"}),
    )
    result = _loss_cooldown_block(analysis=_analysis(mid=95.0, composite=80),
                                  mode="live", config=ON)
    assert result is not None
    assert result["executed"] is False


def test_requires_keyword_arguments():
    import pytest

    with pytest.raises(TypeError):
        _loss_cooldown_block(_analysis(), "live", ON)
