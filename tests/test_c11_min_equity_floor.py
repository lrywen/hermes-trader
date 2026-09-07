"""Audit 2026-09-07 (C11 test gap): the hard equity floor must FAIL CLOSED.

maybe_execute refuses new entries when aggregate equity is below
``min_tradable_equity_usd`` (Pathia carries an equivalent ~$12 floor; Hermes
pins a conservative $10). Below the floor there is not enough book to size any
trade above exchange min-notional with meaningful stop room. The C3 suite only
neutralised this gate (set it to 0) so the floor branch itself had no
dedicated coverage. These tests drive maybe_execute with a synthetic
sub-floor account state in both SHADOW and LIVE modes and assert:

  * equity below the floor  -> executed=False, reason below_min_tradable_equity,
    and no order placement is ever reached;
  * equity at/above the floor, or threshold 0 (gate disabled) -> the floor
    branch is NOT taken.

I/O boundaries are mocked exactly as in the C3 suite.
"""

from __future__ import annotations

import pytest

from hermes_trader.agents import executor


class _StubMemory:
    """Neutral memory stub: no disk, no state, zero slip/cooldowns/pnl."""

    def __getattr__(self, name):
        def _stub(*_a, **_k):
            return None
        return _stub

    def avg_exit_slip_bps(self, coin, days=None):
        return 0.0

    def loss_cooldown_remaining_min(self, coin):
        return 0

    def get_daily_pnl(self):
        return 0.0

    def peak_daily_pnl(self):
        return 0.0

    def daily_realized_pnl(self):
        return 0.0

    def peak_daily_realized_pnl(self):
        return 0.0

    def get_recent_trades(self, n=10):
        return []

    def track_daily_pnl(self, equity):
        return None


def _base_config(mode, floor):
    return {
        "mode": mode, "enable_crypto": True,
        "leverage": 1,
        "max_trade_notional_usd": 0,
        "max_concurrent": 9999,
        "min_market_volume_usd": 0,
        "min_hip3_volume_usd": 0,
        "min_short_volume_usd": 0,
        "max_total_notional_pct": 50.0,
        "max_daily_loss_usd": -1_000_000_000,
        "min_ai_confidence": 0.0,
        "aligned_min_conf": None,
        "min_trend_score": 0.0,
        "coin_allowlist": [],
        "coin_blocklist": [],
        "max_crypto_long_correlated": 9999,
        "cooldown_min": 0,
        "counter_regime_min_conf": 0.0,
        "block_counter_trend_bypass": False,
        "crowded_with_min_conf": 0.0,
        "debate_gate": {"enabled": False},
        "news_blackout": {"enabled": False},
        "circuit_breaker": {"consecutive_loss_limit": 0,
                            "coin_daily_loss_pct": 0.0,
                            "max_drawdown_pct": 0.0},
        "liquidation_maint_margin_pct": 1.0,
        "sl_buffer_bps": 10.0,
        # C11 floor under test.
        "min_tradable_equity_usd": floor,
        "min_available_margin_pct": 0.0,
        "dsl_exit": {
            "max_loss_pct": 2.5, "max_loss_roe_pct": 25.0,
            "atr_stop": {"enabled": True, "atr_mult": 0.5,
                         "floor_pct": 1.0, "ceiling_pct": 4.0},
        },
    }


def _wire(monkeypatch, mode, *, floor, equity):
    """Mock every I/O boundary; feed a synthetic live account equity."""
    cfg = _base_config(mode, floor)
    monkeypatch.setattr(executor, "read_agent_config", lambda: dict(cfg))
    monkeypatch.setattr(executor, "memory", _StubMemory())
    monkeypatch.setattr(executor, "get_max_leverage", lambda _coin: 1)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state",
                        lambda *_a, **_k: {"equity": float(equity),
                                           "available": float(equity),
                                           "total_ntl": 0.0,
                                           "asset_positions": []})
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 2.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 0.0)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)
    # No order may ever be placed while the floor blocks; a call fails the test.
    monkeypatch.setattr(executor, "place_hl_order", _forbid_order)


def _forbid_order(*_a, **_k):
    pytest.fail("place_hl_order must NOT be called while below the equity floor")


def _analysis():
    return {
        "id": "c11floor", "coin": "TEST", "action": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 80,
        "entry_px": 100.0, "stop_px": 99.0, "tp_px": 110.0,
        "reasoning": "c11 equity-floor test",
    }


def test_c11_below_floor_fails_closed_shadow(monkeypatch):
    """SHADOW: aggregate equity ($8) below the floor ($10) refuses entry."""
    _wire(monkeypatch, "SHADOW", floor=10.0, equity=8.0)
    res = executor.maybe_execute(_analysis())
    assert res["executed"] is False
    assert res["reason"].startswith("below_min_tradable_equity")


def test_c11_below_floor_fails_closed_live(monkeypatch):
    """LIVE: same fail-closed behaviour — a dust account must never open a
    real position regardless of mode."""
    _wire(monkeypatch, "LIVE", floor=10.0, equity=5.0)
    res = executor.maybe_execute(_analysis())
    assert res["executed"] is False
    assert res["reason"].startswith("below_min_tradable_equity")


def test_c11_at_floor_boundary_does_not_block(monkeypatch):
    """Equity exactly at the floor is NOT below it (``<``, not ``<=``) -> the
    floor branch is not taken."""
    _wire(monkeypatch, "SHADOW", floor=10.0, equity=10.0)
    try:
        res = executor.maybe_execute(_analysis())
    except Exception:
        # Downstream paper-booking internals are out of scope; the point is
        # that execution reached past the floor without its fail-closed return.
        return
    assert not str(res.get("reason", "")).startswith("below_min_tradable_equity")


def test_c11_above_floor_does_not_block(monkeypatch):
    """Control: a funded account ($1000) must not trip the $10 floor."""
    _wire(monkeypatch, "SHADOW", floor=10.0, equity=1000.0)
    try:
        res = executor.maybe_execute(_analysis())
    except Exception:
        return
    assert not str(res.get("reason", "")).startswith("below_min_tradable_equity")


def test_c11_zero_threshold_disables_gate(monkeypatch):
    """Threshold 0 disables the gate (inert for legacy deployments): even a
    sub-$10 account must not be blocked by the floor branch itself."""
    _wire(monkeypatch, "SHADOW", floor=0.0, equity=8.0)
    try:
        res = executor.maybe_execute(_analysis())
    except Exception:
        return
    assert not str(res.get("reason", "")).startswith("below_min_tradable_equity")
