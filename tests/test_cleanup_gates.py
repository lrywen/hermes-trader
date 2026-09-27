"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def _ctx(**kw):
    from hermes_trader.agents.risk_gates import GateContext
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="BTC",
                trade_side="long", has_binary_news_risk=False, equity=1000,
                total_open_notional=0)
    base.update(kw)
    return GateContext(**base)


def test_risk_gates_pass_and_block():
    from hermes_trader.agents.risk_gates import eval_all_gates
    cfg = {"min_ai_confidence": 0.8, "max_concurrent": 3, "max_trade_notional_usd": 200,
           "max_daily_loss_usd": -100, "min_market_volume_usd": 5e6,
           "max_total_notional_pct": 1.0, "cooldown_min": 60,
           # debate_gate is fail-closed default-on (P0-A) with analyst3 default
           # False (P0-D); this case exercises the generic gates, so opt out
           # explicitly instead of relying on implicit debate defaults.
           "debate_gate": {"enabled": False}}
    assert eval_all_gates(_ctx(), cfg)["blocked"] is False
    blocked = eval_all_gates(_ctx(confidence=0.1), cfg)
    assert blocked["blocked"] is True
    assert any("confidence" in r for r in blocked["block_reasons"])


def test_debate_gate_fail_closed_defaults():
    """P0-A/P0-D: debate_gate enables itself with fail-closed defaults when no
    debate_gate config exists, and analyst3 no longer casts a rubber-stamp
    vote. A bare high-confidence ctx (no triggers/score/whale) must therefore
    be BLOCKED (2/5 votes); analyst3_default=true restores the legacy pass."""
    from hermes_trader.agents.risk_gates import debate_gate

    bare = debate_gate(_ctx(), {})
    assert bare["pass"] is False
    assert bare["agree_count"] == 2  # news-clean + high-conf whale-boost only

    assert debate_gate(_ctx(), {"debate_gate": {"enabled": False}})["pass"] is True

    legacy = debate_gate(_ctx(), {"debate_gate": {"analyst3_default": True}})
    assert legacy["pass"] is True
    assert legacy["agree_count"] == 3


def test_notional_cap_allows_exchange_precision_dust():
    from hermes_trader.agents.risk_gates import per_trade_notional_cap_gate

    assert per_trade_notional_cap_gate(_ctx(trade_notional_usd=650.05), 650)["pass"] is True
    assert per_trade_notional_cap_gate(_ctx(trade_notional_usd=0.01), 0)["pass"] is True
    blocked = per_trade_notional_cap_gate(_ctx(trade_notional_usd=660.0), 650)
    assert blocked["pass"] is False
    assert "exceeds cap" in blocked["reason"]


def test_aligned_min_conf_lets_aligned_shorts_through(monkeypatch):
    """Regime-aware confidence floor: an ALIGNED short (down regime) clears the
    lower aligned_min_conf, while the same confidence on a non-aligned trade is
    still blocked by the default min_ai_confidence. Enables shorting selloffs
    (SOL SHORT 0.72 was being blocked by the 0.78 long-calibrated bar)."""
    import hermes_trader.agents.market_regime as mr
    from hermes_trader.agents.risk_gates import eval_all_gates
    monkeypatch.setattr(mr, "detect_regime_with_score", lambda coin, **k: ("down", 1.0))
    cfg = {"min_ai_confidence": 0.78, "aligned_min_conf": 0.70, "max_concurrent": 5,
           "max_trade_notional_usd": 500, "max_daily_loss_usd": -300,
           "min_market_volume_usd": 8e5, "max_total_notional_pct": 1.0,
           "cooldown_min": 60, "counter_regime_min_conf": 0.80,
           "min_short_volume_usd": 50_000_000}
    # ALIGNED short (down regime + short) at 0.72 → confidence gate passes (>=0.70)
    r = eval_all_gates(_ctx(trade_side="short", coin="SOL", confidence=0.72,
                            market_volume_24h_usd=4e8), cfg)
    assert r["results"]["confidence"]["pass"] is True
    # NON-aligned (long in a down regime = counter-trend) at 0.72 → still blocked by 0.78
    r2 = eval_all_gates(_ctx(trade_side="long", coin="SOL", confidence=0.72,
                             market_volume_24h_usd=4e8), cfg)
    assert r2["results"]["confidence"]["pass"] is False
    # With aligned_min_conf UNSET, the aligned short reverts to the 0.78 bar (blocked)
    cfg_off = {**cfg}; cfg_off.pop("aligned_min_conf")
    r3 = eval_all_gates(_ctx(trade_side="short", coin="SOL", confidence=0.72,
                             market_volume_24h_usd=4e8), cfg_off)
    assert r3["results"]["confidence"]["pass"] is False


def test_short_liquidity_floor_blocks_thin_shorts_only():
    """Shorts on thin markets squeeze (data: bleeders ~$13M vol, winners ~$223M).
    The floor must block a thin SHORT, allow a thin LONG, allow a liquid short,
    and be a no-op when unset."""
    from hermes_trader.agents.risk_gates import short_liquidity_floor
    FLOOR = 50_000_000
    # thin short → blocked
    r = short_liquidity_floor(_ctx(trade_side="short", coin="XPL", market_volume_24h_usd=16e6), FLOOR)
    assert r["pass"] is False and "squeeze" in r["reason"]
    # thin LONG → allowed (longs are unaffected)
    assert short_liquidity_floor(_ctx(trade_side="long", coin="XPL", market_volume_24h_usd=16e6), FLOOR)["pass"] is True
    # liquid short → allowed
    assert short_liquidity_floor(_ctx(trade_side="short", coin="BTC", market_volume_24h_usd=4e9), FLOOR)["pass"] is True
    # disabled (0) → no-op even for a thin short
    assert short_liquidity_floor(_ctx(trade_side="short", coin="XPL", market_volume_24h_usd=1e6), 0)["pass"] is True


def test_eval_all_gates_short_volume_floor_integration():
    from hermes_trader.agents.risk_gates import eval_all_gates
    cfg = {"min_ai_confidence": 0.78, "max_concurrent": 5, "max_trade_notional_usd": 500,
           "max_daily_loss_usd": -300, "min_market_volume_usd": 8e5,
           "max_total_notional_pct": 1.0, "cooldown_min": 60,
           "min_short_volume_usd": 50_000_000}
    # thin short blocked by the new floor
    blk = eval_all_gates(_ctx(trade_side="short", coin="XPL", market_volume_24h_usd=16e6), cfg)
    assert blk["blocked"] is True
    assert any("short floor" in r or "squeeze" in r for r in blk["block_reasons"])
    # same thin market as a LONG is NOT blocked by the short floor
    lng = eval_all_gates(_ctx(trade_side="long", coin="XPL", market_volume_24h_usd=16e6), cfg)
    assert lng["results"]["short_liquidity"]["pass"] is True


def test_held_coin_blocks_both_pyramid_and_flip():
    """A coin we already hold must block re-entry in BOTH directions: opposite =
    no auto-flip, same side = no uncontrolled pyramid (the held-coin close-check
    can return a fresh LONG/SHORT; only this guard stops it adding)."""
    from hermes_trader.agents.risk_gates import opposite_direction_guard
    held_long = [{"coin": "ETH", "side": "long", "size_usd": 100}]
    # same-direction re-entry → blocked (pyramid)
    r_same = opposite_direction_guard(_ctx(coin="ETH", trade_side="long", current_positions=held_long))
    assert r_same["pass"] is False and "pyramid" in r_same["reason"]
    # opposite-direction → blocked (no auto-flip)
    r_opp = opposite_direction_guard(_ctx(coin="ETH", trade_side="short", current_positions=held_long))
    assert r_opp["pass"] is False and "auto-flip" in r_opp["reason"]
    # unheld coin → passes
    assert opposite_direction_guard(_ctx(coin="SOL", trade_side="long", current_positions=held_long))["pass"] is True


def test_cfg_camelcase_tolerance():
    """Gate config keys resolve whether written snake_case or camelCase."""
    from hermes_trader.agents.risk_gates import _cfg
    assert _cfg({"max_trade_notional_usd": 30}, "max_trade_notional_usd", 200) == 30
    assert _cfg({"maxTradeNotionalUsd": 20}, "max_trade_notional_usd", 200) == 20  # camelCase
    assert _cfg({"minAiConfidence": 0.5}, "min_ai_confidence", 0.8) == 0.5
    assert _cfg({}, "max_trade_notional_usd", 200) == 200  # default


def test_news_blackout_gate_reason_includes_match():
    from hermes_trader.agents.risk_gates import news_blackout_gate
    ok = news_blackout_gate(_ctx(has_binary_news_risk=False))
    assert ok["pass"] is True
    blocked = _ctx(has_binary_news_risk=True)
    blocked.binary_news_match = "'hack' in: Coin hacked for $1M"
    r = news_blackout_gate(blocked)
    assert r["pass"] is False
    assert "Coin hacked for $1M" in r["reason"]
