"""C-M2 (deep audit 2026-08-28): candle data quality gate.

HL's candleSnapshot has silently returned truncated / gappy / stale series
during 429 storms. An ATR computed over distorted bars is too small → oversized
position + stop tighter than the real bar range. The gate
(``assess_candle_quality``) flags gaps / staleness / low coverage; sizing
consumers (``get_hl_atr``, ``get_atr_hist_mean_pct``) treat a failed gate as
0.0 so the executor's existing ``atr <= 0 → no_atr_no_stop`` rule fails CLOSED.
"""

import time

from hermes_trader.client.hl_client import assess_candle_quality
from hermes_trader.models.types import Candle


def _candle(t_ms: int, price: float = 100.0) -> Candle:
    return Candle(t=t_ms, o=price, h=price + 1, l=price - 1, c=price, v=10.0)


def _series(interval_ms: int, n: int, now_ms: float, *,
            gap_at: int | None = None, price: float = 100.0) -> list[Candle]:
    """n bars ending on the grid before now; bar index ``gap_at`` (if given)
    is skipped, producing one missing-bar gap."""
    # last bar is still forming: its open time is the current grid slot
    last_open = int(now_ms // interval_ms) * interval_ms
    ts = [last_open - (n - 1 - i) * interval_ms for i in range(n)]
    if gap_at is not None:
        ts = [t for j, t in enumerate(ts) if j != gap_at]
    return [_candle(t, price) for t in ts]


_MS_4H = 14_400_000


# ── assess_candle_quality: pure-function unit tests ──────────────────────────

def test_quality_ok_on_contiguous_fresh_series():
    now = time.time() * 1000
    candles = _series(_MS_4H, 30, now)
    q = assess_candle_quality(candles, "4h", 30, now_ms=now)
    assert q["ok"] is True
    assert q["issues"] == []
    assert q["gaps"] == 0


def test_quality_flags_single_gap():
    now = time.time() * 1000
    candles = _series(_MS_4H, 30, now, gap_at=10)
    q = assess_candle_quality(candles, "4h", 30, now_ms=now)
    assert q["ok"] is False
    assert "gaps" in q["issues"]
    assert q["gaps"] == 1


def test_quality_flags_multiple_gaps():
    now = time.time() * 1000
    candles = _series(_MS_4H, 40, now, gap_at=5)
    # remove a second bar too
    candles = [c for i, c in enumerate(candles) if i != 20]
    q = assess_candle_quality(candles, "4h", 40, now_ms=now)
    assert q["ok"] is False
    assert q["gaps"] == 2


def test_quality_flags_stale_series():
    now = time.time() * 1000
    # contiguous series whose NEWEST bar is 5 intervals old (no forming bar —
    # the feed stopped updating). 30 bars so coverage alone doesn't flag it.
    candles = [_candle(int(now) - (34 - i) * _MS_4H) for i in range(30)]
    q = assess_candle_quality(candles, "4h", 24, now_ms=now)
    assert q["ok"] is False
    assert "stale" in q["issues"]
    assert q["age_ms"] > 2 * _MS_4H


def test_quality_fresh_forming_bar_not_stale():
    now = time.time() * 1000
    candles = _series(_MS_4H, 24, now)  # last bar forming, opened this slot
    q = assess_candle_quality(candles, "4h", 24, now_ms=now)
    assert "stale" not in q["issues"]


def test_quality_flags_low_coverage():
    now = time.time() * 1000
    candles = _series(_MS_4H, 5, now)  # asked for 24, got 5 closed
    q = assess_candle_quality(candles, "4h", 24, now_ms=now)
    assert q["ok"] is False
    assert "low_coverage" in q["issues"]


def test_quality_just_above_coverage_threshold_passes():
    now = time.time() * 1000
    # expected 24 → threshold int(23*0.8)=18 closed; provide 20 total (19 closed)
    candles = _series(_MS_4H, 20, now)
    q = assess_candle_quality(candles, "4h", 24, now_ms=now)
    assert "low_coverage" not in q["issues"]


def test_quality_flags_thin_series():
    now = time.time() * 1000
    q = assess_candle_quality([_candle(int(now))], "4h", 24, now_ms=now)
    assert q["ok"] is False
    assert "thin" in q["issues"]


def test_quality_empty_series_is_thin():
    q = assess_candle_quality([], "4h", 24)
    assert q["ok"] is False
    assert "thin" in q["issues"]


def test_quality_interval_agnostic_grid():
    now = time.time() * 1000
    candles = _series(3_600_000, 30, now)
    q = assess_candle_quality(candles, "1h", 30, now_ms=now)
    assert q["ok"] is True
    # same series checked against 4h grid → every delta looks like a gap
    q4 = assess_candle_quality(candles, "4h", 30, now_ms=now)
    assert q4["ok"] is False
    assert "gaps" in q4["issues"]


# ── get_hl_atr: fail-closed on distorted candles ─────────────────────────────

def _patch_candles(monkeypatch, candles):
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "fetch_hl_candles",
                        lambda *a, **k: list(candles))
    # isolate the ATR memo cache so tests don't read each other's results
    monkeypatch.setattr(exchange, "_ATR_CACHE", {})


def test_get_hl_atr_returns_positive_on_healthy_series(monkeypatch):
    from hermes_trader.client import exchange
    now = time.time() * 1000
    candles = _series(_MS_4H, 30, now, price=100.0)
    _patch_candles(monkeypatch, candles)
    atr = exchange.get_hl_atr("4h", 14, "BTC")
    assert atr > 0.0


def test_get_hl_atr_zero_on_gappy_series(monkeypatch):
    from hermes_trader.client import exchange
    now = time.time() * 1000
    candles = _series(_MS_4H, 30, now, gap_at=12)
    _patch_candles(monkeypatch, candles)
    assert exchange.get_hl_atr("4h", 14, "BTC") == 0.0


def test_get_hl_atr_zero_on_stale_series(monkeypatch):
    from hermes_trader.client import exchange
    now = time.time() * 1000
    # newest bar ~5 intervals stale, no forming bar (feed outage)
    candles = [_candle(int(now) - (35 - i) * _MS_4H, 100.0) for i in range(30)]
    _patch_candles(monkeypatch, candles)
    assert exchange.get_hl_atr("4h", 14, "BTC") == 0.0


def test_get_hl_atr_zero_on_truncated_series(monkeypatch):
    from hermes_trader.client import exchange
    now = time.time() * 1000
    candles = _series(_MS_4H, 6, now)  # far below period+10=24 expected
    _patch_candles(monkeypatch, candles)
    assert exchange.get_hl_atr("4h", 14, "BTC") == 0.0


def test_get_hl_atr_zero_on_empty_series(monkeypatch):
    from hermes_trader.client import exchange
    _patch_candles(monkeypatch, [])
    assert exchange.get_hl_atr("4h", 14, "BTC") == 0.0


# ── get_atr_hist_mean_pct: spike-breaker baseline also gated ─────────────────

def test_atr_hist_mean_zero_on_gappy_history(monkeypatch):
    from hermes_trader.agents import executor
    now = time.time() * 1000
    candles = _series(_MS_4H, 180, now, gap_at=50)
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: list(candles))
    assert executor.get_atr_hist_mean_pct("BTC", "4h", 180) == 0.0


def test_atr_hist_mean_positive_on_healthy_history(monkeypatch):
    from hermes_trader.agents import executor
    now = time.time() * 1000
    candles = _series(_MS_4H, 180, now, price=100.0)
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: list(candles))
    assert executor.get_atr_hist_mean_pct("BTC", "4h", 180) > 0.0


# ── end-to-end: failed gate flows through no_atr_no_stop rejection ───────────

def test_executor_rejects_trade_when_quality_gate_fails(monkeypatch):
    """Distorted candles → ATR 0.0 → executor refuses with no_atr_no_stop
    (fail-closed), never places an order."""
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "mode": "LIVE", "enable_crypto": True, "enable_hip3": False,
        "min_available_margin_pct": 0.0,
        "atr_risk_sizing": {"enabled": True},
    })
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {
        "equity": 1000.0, "available": 1000.0,
        "dex_equity": {"": 1000.0}, "dex_available": {"": 1000.0},
        "total_ntl": 0.0, "asset_positions": [],
    })
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 100.0)
    monkeypatch.setattr(executor, "get_max_leverage", lambda c: 10)
    monkeypatch.setattr(executor, "eval_all_gates",
                        lambda ctx, cfg, lt, **kw: {"blocked": False, "results": {}})
    # quality gate fails: gappy 4h series → get_hl_atr returns 0.0.
    # executor.get_hl_atr runs inside the exchange namespace, so patch there
    # (and clear exchange's ATR memo so no cross-test cached positive value).
    from hermes_trader.client import exchange
    now = time.time() * 1000
    gappy = _series(_MS_4H, 30, now, gap_at=10)
    monkeypatch.setattr(exchange, "fetch_hl_candles",
                        lambda *a, **k: list(gappy))
    monkeypatch.setattr(exchange, "_ATR_CACHE", {})

    placed = {"n": 0}
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *a, **k: placed.update(n=placed["n"] + 1) or {"ok": True})

    res = executor.maybe_execute({
        "id": "cm2-gappy", "coin": "BTC", "verdict": "LONG",
        "side": "long", "confidence": 0.8, "composite_score": 60.0,
    })
    assert res["executed"] is False
    assert "no_atr_no_stop" in res["reason"]
    assert placed["n"] == 0
