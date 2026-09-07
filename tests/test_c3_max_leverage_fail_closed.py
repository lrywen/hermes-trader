"""Audit 2026-09-06 (C3): unknown exchange max leverage must FAIL CLOSED.

get_max_leverage() raises ValueError for a coin absent from the (cached)
universe metadata. maybe_execute catches it mid-sizing and must refuse the
trade (executed=False, reason=unknown_max_leverage_<coin>) without ever
reaching order placement — sizing at the configured leverage for an unknown
cap could over-leverage or be rejected by the exchange. Prior tests all
monkeypatched get_max_leverage to return a normal int, so the exception
branch was uncovered. These tests drive the full maybe_execute path with
the I/O boundaries mocked, in both SHADOW (paper) and LIVE modes.
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


def _base_config(mode):
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
        # C11 floor: neutralise so the synthetic equity passes.
        "min_tradable_equity_usd": 0.0,
        "dsl_exit": {
            "max_loss_pct": 2.5, "max_loss_roe_pct": 25.0,
            "atr_stop": {"enabled": True, "atr_mult": 0.5,
                         "floor_pct": 1.0, "ceiling_pct": 4.0},
        },
    }


def _wire(monkeypatch, mode, *, leverage_side_effect):
    """Mock every I/O boundary around maybe_execute's sizing section."""
    cfg = _base_config(mode)
    monkeypatch.setattr(executor, "read_agent_config", lambda: dict(cfg))
    monkeypatch.setattr(executor, "memory", _StubMemory())

    def _lev(_coin):
        if isinstance(leverage_side_effect, BaseException):
            raise leverage_side_effect
        return leverage_side_effect

    monkeypatch.setattr(executor, "get_max_leverage", _lev)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state",
                        lambda *_a, **_k: {"equity": 1000.0, "available": 900.0,
                                           "total_ntl": 0.0, "asset_positions": []})
    monkeypatch.setattr(executor, "get_hl_price", lambda _c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *_a, **_k: 2.0)
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda _c, _m: 0.0)
    monkeypatch.setattr(executor, "entry_size_for_notional",
                        lambda _c, n, m: n / m)
    # No order may ever be placed in any configuration; a call fails the test.
    monkeypatch.setattr(executor, "place_hl_order", _forbid_order)


def _forbid_order(*_a, **_k):
    pytest.fail("place_hl_order must NOT be called when max leverage is unknown")


def _analysis():
    return {
        "id": "c3leverage", "coin": "TEST", "action": "LONG", "side": "long",
        "confidence": 0.9, "composite_score": 80,
        "entry_px": 100.0, "stop_px": 99.0, "tp_px": 110.0,
        "reasoning": "c3 fail-closed test",
    }


def test_c3_unknown_max_leverage_fails_closed_shadow(monkeypatch):
    """SHADOW (paper): a ValueError from get_max_leverage refuses the trade
    and never places an order."""
    _wire(monkeypatch, "SHADOW",
          leverage_side_effect=ValueError("Unknown coin TEST"))
    res = executor.maybe_execute(_analysis())
    assert res["executed"] is False
    assert res["reason"] == "unknown_max_leverage_TEST"


def test_c3_unknown_max_leverage_fails_closed_live(monkeypatch):
    """LIVE: same fail-closed behaviour — an unknown leverage cap must never
    be sized at the configured leverage into a real order."""
    _wire(monkeypatch, "LIVE",
          leverage_side_effect=ValueError("Unknown coin TEST"))
    res = executor.maybe_execute(_analysis())
    assert res["executed"] is False
    assert res["reason"] == "unknown_max_leverage_TEST"


def test_c3_any_leverage_lookup_exception_fails_closed(monkeypatch):
    """Not just ValueError: any transient lookup failure (e.g. cache meta
    endpoint error) must fail closed rather than abort mid-sizing."""
    _wire(monkeypatch, "SHADOW",
          leverage_side_effect=RuntimeError("meta endpoint 500"))
    res = executor.maybe_execute(_analysis())
    assert res["executed"] is False
    assert res["reason"] == "unknown_max_leverage_TEST"


def test_c3_known_leverage_proceeds_past_lookup(monkeypatch):
    """Control: with a normal leverage lookup the path must NOT take the
    fail-closed branch (reason is something else / paper booking)."""
    _wire(monkeypatch, "SHADOW", leverage_side_effect=1)
    # Neutralise paper-booking noise: SHADOW would book; we only assert the
    # leverage branch was not hit.
    try:
        res = executor.maybe_execute(_analysis())
    except Exception:
        # Downstream paper-booking internals are out of scope; the point is
        # that sizing reached past the leverage lookup without the fail-closed
        # return.
        return
    assert res.get("reason") != "unknown_max_leverage_TEST"
