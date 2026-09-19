"""Characterization tests for the idempotency fast-path decision leaf.

The helper is the pre-lock, read-only duplicate filter; these tests pin its
matching rules after the verbatim extraction from maybe_execute.
"""

from types import SimpleNamespace

from hermes_trader.agents import executor
from hermes_trader.agents.executor import _already_executed_block


def _memory(trades):
    calls = []

    def _get_recent_trades(n):
        calls.append(n)
        return trades

    return SimpleNamespace(get_recent_trades=_get_recent_trades), calls


def test_no_history_returns_none(monkeypatch):
    mem, calls = _memory([])
    monkeypatch.setattr(executor, "memory", mem)
    assert _already_executed_block(aid="aid-1", mode="live") is None
    assert calls == [100]


def test_unrelated_analysis_returns_none(monkeypatch):
    mem, _ = _memory([{"analysis_id": "other", "size_usd": 500, "order_id": "X"}])
    monkeypatch.setattr(executor, "memory", mem)
    assert _already_executed_block(aid="aid-1", mode="live") is None


def test_matching_filled_trade_blocks_with_order_id(monkeypatch):
    mem, _ = _memory([
        {"analysis_id": "other", "size_usd": 500, "order_id": "X"},
        {"analysis_id": "aid-1", "size_usd": 499.9, "order_id": "oid-7"},
    ])
    monkeypatch.setattr(executor, "memory", mem)
    result = _already_executed_block(aid="aid-1", mode="live")
    assert result == {
        "executed": False,
        "mode": "live",
        "analysis_id": "aid-1",
        "reason": "already_executed",
        "order_id": "oid-7",
    }


def test_zero_size_record_does_not_block(monkeypatch):
    # A rejected/failed record carries size_usd == 0 and must NOT poison retries.
    mem, _ = _memory([{"analysis_id": "aid-1", "size_usd": 0, "order_id": "oid-9"}])
    monkeypatch.setattr(executor, "memory", mem)
    assert _already_executed_block(aid="aid-1", mode="live") is None


def test_missing_size_key_does_not_block(monkeypatch):
    mem, _ = _memory([{"analysis_id": "aid-1", "order_id": "oid-9"}])
    monkeypatch.setattr(executor, "memory", mem)
    assert _already_executed_block(aid="aid-1", mode="live") is None


def test_missing_order_id_passes_none(monkeypatch):
    mem, _ = _memory([{"analysis_id": "aid-1", "size_usd": 10}])
    monkeypatch.setattr(executor, "memory", mem)
    result = _already_executed_block(aid="aid-1", mode="shadow")
    assert result is not None
    assert result["reason"] == "already_executed"
    assert result["mode"] == "shadow"
    assert result["order_id"] is None


def test_first_positive_match_wins(monkeypatch):
    mem, _ = _memory([
        {"analysis_id": "aid-1", "size_usd": 25, "order_id": "first"},
        {"analysis_id": "aid-1", "size_usd": 75, "order_id": "second"},
    ])
    monkeypatch.setattr(executor, "memory", mem)
    result = _already_executed_block(aid="aid-1", mode="live")
    assert result["order_id"] == "first"


def test_requires_keyword_arguments():
    import pytest

    with pytest.raises(TypeError):
        _already_executed_block("aid-1", "live")
