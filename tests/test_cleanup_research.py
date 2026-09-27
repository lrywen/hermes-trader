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


def test_research_thin_history_pass_flags_ai_down(monkeypatch):
    """P1-16: a thin-history (<30 4h candles) PASS skips the LLM entirely, so it
    must flag ai_down=True — otherwise record/notify implies a live model made
    the decision."""
    from hermes_trader.agents import research

    def _fake_prefetch(coin, skip_news_flag):
        # 5 bars across all TFs → well under the 30-bar 4h threshold.
        short = [{"t": i, "o": 100, "h": 101, "l": 99, "c": 100, "v": 10}
                 for i in range(5)]
        return {"c1h": short, "c4h": short, "c1d": short,
                "funding_raw": "N/A", "news": "", "signals_block": ""}

    monkeypatch.setattr(research, "_parallel_prefetch", _fake_prefetch)
    monkeypatch.setattr(research, "_should_skip_news", lambda: True)
    recorded = []
    monkeypatch.setattr(research.memory, "record_analysis",
                        lambda a: recorded.append(a))

    out = research.research("TESTCOIN", {"id": "p1", "mid": 100,
                                         "composite_score": 0})
    assert out["verdict"] == "PASS"
    assert out["ai_down"] is True
    assert recorded and recorded[0]["ai_down"] is True


def test_build_user_message_includes_whale_and_structure_blocks():
    from hermes_trader.agents.research import _build_user_message
    perception = {
        "type": "perp", "mid": 0.000173, "composite_score": 62,
        "triggers": [
            {"name": "higherLows1h", "reason": "3 HL", "fired": True},
        ],
        "whale_signal": {"funding_rate": -0.0006, "price_24h_change_pct": 1.2,
                         "oi": 5e7, "confidence": 0.8},
    }
    snap = {"ema8": None, "ema21": None, "last_close": 0.000173}
    msg = _build_user_message(
        "HMSTR", perception, snap, snap, snap, "0.01%/hr", "no news",
        250.0, [], "LIVE",
    )
    assert "Whale accumulation flag (oi_funding_anomaly)" in msg
    assert "1h structure signals (entry-timing" in msg
    # sub-cent mid must NOT collapse to 0.0002 — adaptive precision keeps it.
    assert "0.000173" in msg and "0.0002" not in msg


def test_build_user_message_omits_account_equity_and_notional():
    """Account equity / notional must NOT reach the LLM — leverage/exposure is
    the gates' job and was causing the model to PASS good setups on 'over-leverage'
    grounds. Only the held coins/sides are surfaced (for dup/CLOSE detection)."""
    from hermes_trader.agents.research import _build_user_message
    perception = {"type": "perp", "mid": 100, "composite_score": 10, "triggers": []}
    snap = {"last_close": 100}
    msg = _build_user_message(
        "xyz:MU", perception, snap, snap, snap, "N/A", "no news",
        300.0, [{"coin": "ETH", "side": "long", "size_usd": 779.0}], "LIVE",
        dex_equity={"": 96.0, "xyz": 114.0},
    )
    # no equity figure, no dex-equity framing, no per-position dollar size
    assert "Equity" not in msg
    assert "$300" not in msg and "114.00" not in msg
    assert "$779" not in msg
    # held coin/side still surfaced so the model won't double-trade / can CLOSE
    assert "ETH long" in msg


def test_build_user_message_indicator_block_full_snap():
    """A full indicator snapshot renders the bullish/bearish + RSI/ATR/ADX line."""
    from hermes_trader.agents.research import _build_user_message
    perception = {"type": "perp", "mid": 100, "composite_score": 50, "triggers": []}
    full = {"ema8": 105.0, "ema21": 100.0, "slope_up": True,
            "rsi14": 62.5, "atr14": 3.2, "adx14": 28.0, "last_close": 104.0}
    msg = _build_user_message(
        "BTC", perception, full, full, full, "0.01%/hr", "no news",
        500.0, [{"coin": "ETH", "side": "long", "size_usd": 120}], "OFF",
    )
    assert "bullish" in msg
    assert "RSI(14)=62.5" in msg
    assert "ADX(14)=28.0" in msg
    assert "EMA8 slope: rising" in msg
    # held coin/side surfaced for dup/CLOSE detection, but NO dollar size
    # (account notional must not influence the verdict).
    assert "ETH long" in msg and "$120" not in msg
    assert "analysis only" in msg  # OFF mode message
