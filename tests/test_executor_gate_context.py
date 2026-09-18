"""P1-1 step ③ — characterization for _build_gate_context (S10).

Pins the pure assembly of the 23-input GateContext extracted from
maybe_execute: scalar passthrough, the 坑1 side-adjusted own-gap demote input,
REALIZED-PnL give-back fields sourced from memory, whale_signal gated by the
whale_regime_bypass config (missing config fails closed), H4 entry_px zeroing
when there is no live mid, and read-only 24h volume lookup.
"""
from __future__ import annotations

from hermes_trader.agents import executor

build = executor._build_gate_context


def _analysis(**over):
    base = {
        "id": "a1", "coin": "ETH", "confidence": 0.8,
        "composite_score": 62, "close4h": 2000.0, "ema21_4h": 1990.0,
        "debate_used": True, "momentum_burst_fired": True,
        "slow_burn_fired": False, "whale_signal": True,
    }
    base.update(over)
    return base


def _patch_reads(monkeypatch):
    monkeypatch.setattr(executor, "_get_market_volume_24h", lambda coin: 12345.0)
    monkeypatch.setattr(
        executor, "side_adjusted_own_gap",
        lambda side, close, ema: 0.42)
    monkeypatch.setattr(executor.memory, "peak_daily_pnl", lambda: 11.0)
    monkeypatch.setattr(executor.memory, "daily_realized_pnl", lambda: 7.5)
    monkeypatch.setattr(
        executor.memory, "peak_daily_realized_pnl", lambda: 9.0)


def _build(monkeypatch, **over):
    _patch_reads(monkeypatch)
    kw = dict(
        analysis=_analysis(), config={}, positions=[{"coin": "BTC"}],
        trade_notional=300.0, daily_pnl=5.0, trade_side="long",
        has_binary_news=False, binary_news_match=False, mid_price=1800.0,
        leverage=10.0, h4_stop_distance_pct=1.2, agg_equity=100.0,
        total_open_notional=400.0)
    kw.update(over)
    return build(**kw)


def test_scalar_passthrough_and_read_only_lookups(monkeypatch):
    ctx = _build(monkeypatch)
    assert ctx.confidence == 0.8
    assert ctx.current_positions == [{"coin": "BTC"}]
    assert ctx.trade_notional_usd == 300.0
    assert ctx.daily_pnl == 5.0
    assert ctx.market_volume_24h_usd == 12345.0
    assert ctx.coin == "ETH"
    assert ctx.trade_side == "long"
    assert ctx.equity == 100.0
    assert ctx.total_open_notional == 400.0
    assert ctx.composite_score == 62.0
    assert ctx.leverage == 10.0
    assert ctx.stop_distance_pct == 1.2
    assert ctx.entry_px == 1800.0


def test_own_gap_and_realized_pnl_fields(monkeypatch):
    ctx = _build(monkeypatch)
    assert ctx.own_gap_pct == 0.42
    assert ctx.peak_daily_pnl == 11.0
    assert ctx.daily_realized_pnl == 7.5
    assert ctx.peak_daily_realized_pnl == 9.0
    assert ctx.debate_used is True
    assert ctx.momentum_burst_fired is True
    assert ctx.slow_burn_fired is False


def test_whale_signal_fails_closed_without_config(monkeypatch):
    # whale_signal present but whale_regime_bypass not armed → must be False.
    ctx = _build(monkeypatch, config={})
    assert ctx.whale_signal_fired is False
    ctx2 = _build(monkeypatch, config={"whale_regime_bypass": True})
    assert ctx2.whale_signal_fired is True


def test_entry_px_zeroed_without_live_mid(monkeypatch):
    ctx = _build(monkeypatch, mid_price=0.0)
    assert ctx.entry_px == 0.0


def test_binary_news_fields_passthrough(monkeypatch):
    # has_binary_news_risk is bool; binary_news_match is a str label (the
    # GateContext dataclass coerces via str(value or "")).
    ctx = _build(monkeypatch, has_binary_news=True, binary_news_match="fed_cpi",
                 trade_side="short")
    assert ctx.has_binary_news_risk is True
    assert ctx.binary_news_match == "fed_cpi"
    assert ctx.trade_side == "short"
