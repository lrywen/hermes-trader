"""P4-3: unit tests for the backtest signal entry points.

Two independent layers over synthetic data:

* heuristic mode — the deterministic verdict stand-in gates and the PIT bar
  scan (signal at bar close, fill at next open, never on the last bar);
* replay mode — the ai/lowconf/force/sidestep admission matrix plus the
  ``created_at`` -> bar alignment and same-coin dedup.
"""
from __future__ import annotations

import pytest

from hermes_trader.backtest.signals import (
    ReplayConfig,
    admit_analysis,
    default_heuristic_config,
    heuristic_signals,
    heuristic_verdict,
    replay_signals,
    ta_confirmed,
    trend_and_atr_pct,
)
from hermes_trader.models.types import Candle

BAR_MS = 300_000
T0 = 1_700_000_000_000


def _trend_bars(n: int, step: float = 0.007) -> list[Candle]:
    """Steadily rising/falling bars with intraday wicks (strong trend, ATR>0)."""
    bars: list[Candle] = []
    prev = 100.0
    for i in range(n):
        c = 100.0 * ((1.0 + step) ** i)
        o = prev
        hi = max(o, c) * 1.001
        lo = min(o, c) * 0.999
        bars.append(Candle(t=T0 + i * BAR_MS, o=o, h=hi, l=lo, c=c, v=1.0))
        prev = c
    return bars


# ── heuristic stand-in verdict ──────────────────────────────────────────────

def test_heuristic_verdict_needs_direction() -> None:
    assert heuristic_verdict(90, [], None, 1.0) is None


def test_heuristic_verdict_score_gate() -> None:
    # No trend ATR gate, but composite >= 25 -> long.
    assert heuristic_verdict(25, [], True, 0.1) == "long"
    assert heuristic_verdict(24.9, [], True, 0.1) is None


def test_heuristic_verdict_trend_atr_gate() -> None:
    # Low score, no burst: a directional trend with ATR% >= 0.4 still fires.
    assert heuristic_verdict(0, [], True, 0.4) == "long"
    assert heuristic_verdict(0, [], False, 0.5) == "short"
    assert heuristic_verdict(0, [], True, 0.39) is None


def test_heuristic_verdict_burst_gate() -> None:
    hits = [{"name": "momentumBurst", "fired": True}]
    assert heuristic_verdict(0, hits, False, 0.0) == "short"
    assert heuristic_verdict(0, [{"name": "momentumBurst", "fired": False}],
                             True, 0.0) is None


def test_trend_and_atr_pct_thin_data() -> None:
    bars = _trend_bars(20)
    assert trend_and_atr_pct(bars) == (None, None, None)


def test_ta_confirmed_proxy() -> None:
    # Trend present but weak everything else -> below 45.
    assert ta_confirmed(True, 0.1, None, 0.0) is False
    # Trend + strong ADX + ATR + high composite -> confirmed.
    assert ta_confirmed(True, 0.6, 30.0, 80.0) is True
    assert ta_confirmed(None, 0.6, 30.0, 80.0) is False


# ── heuristic PIT scan ──────────────────────────────────────────────────────

def test_heuristic_signals_long_trend_pit() -> None:
    bars = _trend_bars(120)
    cfg = default_heuristic_config(warmup=30)
    sigs = heuristic_signals(bars, cfg)
    assert sigs, "a steady uptrend should produce long signals"
    assert all(s.side == "long" for s in sigs)
    # Every signal is decided on a closed bar and has a later bar to fill on.
    assert all(0 <= s.bar_index < len(bars) - 1 for s in sigs)
    # No two signals on the same bar; chronological.
    idxs = [s.bar_index for s in sigs]
    assert idxs == sorted(set(idxs))
    assert sigs[0].bar_index >= 30


def test_heuristic_signals_no_signal_on_last_bar() -> None:
    bars = _trend_bars(120)
    sigs = heuristic_signals(bars, default_heuristic_config(warmup=30))
    assert all(s.bar_index != len(bars) - 1 for s in sigs)


def test_heuristic_signals_short_trend() -> None:
    bars = _trend_bars(120, step=-0.007)
    sigs = heuristic_signals(bars, default_heuristic_config(warmup=30))
    assert sigs and all(s.side == "short" for s in sigs)


def test_heuristic_signals_context_enrichment() -> None:
    bars = _trend_bars(120)

    def ctx(decision_close_ms: int) -> tuple[float, str]:
        return 1.23, "up"

    sigs = heuristic_signals(bars, default_heuristic_config(warmup=30),
                             context_fn=ctx)
    assert sigs[0].entry_atr_pct == pytest.approx(1.23)
    assert sigs[0].entry_regime == "up"
    # Context receives the signal bar's CLOSE ms (open + 5m).
    assert sigs[0].entry_atr_pct == 1.23
    target = bars[sigs[0].bar_index].t + BAR_MS
    seen: list[int] = []
    heuristic_signals(bars, default_heuristic_config(warmup=30),
                      context_fn=lambda ms: (seen.append(ms) or 0.0, ""))
    assert target in seen


# ── replay admission matrix ─────────────────────────────────────────────────

def _analysis(verdict, conf, coin="BTC", created_at=T0, pid="p1", **kw):
    d = {"verdict": verdict, "confidence": conf, "coin": coin,
         "created_at": created_at, "perception_id": pid}
    d.update(kw)
    return d


def _perc(pid="p1", composite=0.0, triggers=None):
    return {"id": pid, "composite_score": composite, "triggers": triggers or []}


def test_admit_ai_mode() -> None:
    cfg = ReplayConfig(mode="ai", min_ai_conf=0.6)
    assert admit_analysis(_analysis("LONG", 0.7), None, cfg) is not None
    assert admit_analysis(_analysis("LONG", 0.5), None, cfg) is None
    assert admit_analysis(_analysis("PASS", 0.9), None, cfg) is None
    d = admit_analysis(_analysis("SHORT", 0.8), None, cfg)
    assert d and d[0] == "short"


def test_admit_lowconf_mode() -> None:
    cfg = ReplayConfig(mode="lowconf", min_conf=0.2)
    assert admit_analysis(_analysis("LONG", 0.2), None, cfg) is not None
    assert admit_analysis(_analysis("LONG", 0.1), None, cfg) is None


def test_admit_force_mode_promotes_high_composite_pass() -> None:
    cfg = ReplayConfig(mode="force", min_ai_conf=0.6, force_bar=45.0)
    # High-conf AI verdict flows through.
    assert admit_analysis(_analysis("SHORT", 0.7), None, cfg)[0] == "short"
    # A PASS with composite >= force_bar is forced LONG.
    out = admit_analysis(_analysis("PASS", 0.1), _perc(composite=50.0), cfg)
    assert out is not None
    side, eff_conf, forced, sidestep = out
    assert (side, forced, sidestep) == ("long", True, False)
    assert eff_conf == 0.6
    # Below the bar, rejected.
    assert admit_analysis(_analysis("PASS", 0.1), _perc(composite=30.0), cfg) is None


def test_admit_sidestep_mode_takes_ta_confirmed_long() -> None:
    cfg = ReplayConfig(mode="sidestep", min_ai_conf=0.6, force_bar=45.0)
    # Composite-confirmed even on a PASS -> forced long sidestep override.
    burst = [{"name": "momentumBurst", "fired": True}]
    out = admit_analysis(_analysis("PASS", 0.1), _perc(triggers=burst), cfg)
    assert out is not None
    side, _conf, forced, sidestep = out
    assert (side, forced, sidestep) == ("long", True, True)
    # Unconfirmed PASS is rejected; high-conf SHORT still flows through.
    assert admit_analysis(_analysis("PASS", 0.1), _perc(composite=0), cfg) is None
    assert admit_analysis(_analysis("SHORT", 0.8), _perc(composite=0), cfg) is not None


def test_admit_long_only() -> None:
    cfg = ReplayConfig(mode="ai", min_ai_conf=0.0, long_only=True)
    assert admit_analysis(_analysis("SHORT", 0.9), None, cfg) is None
    assert admit_analysis(_analysis("LONG", 0.9), None, cfg) is not None


def test_admit_unknown_mode_raises() -> None:
    with pytest.raises(ValueError):
        admit_analysis(_analysis("LONG", 0.9), None, ReplayConfig(mode="nope"))


# ── replay bar alignment ────────────────────────────────────────────────────

def _flat_bars(n: int) -> list[Candle]:
    return [Candle(t=T0 + i * BAR_MS, o=100, h=100, l=100, c=100, v=1.0)
            for i in range(n)]


def test_replay_aligns_decision_to_next_open() -> None:
    bars = _flat_bars(10)
    # Decision exactly at bar 5 open -> signal decided on bar 4 close.
    analyses = [_analysis("LONG", 0.9, created_at=bars[5].t)]
    sigs = replay_signals(bars, analyses, None,
                          ReplayConfig(mode="ai", min_ai_conf=0.0))
    assert [s.bar_index for s in sigs] == [4]
    # Decision just after bar 5 open still fills at bar 6 -> signal bar 5.
    analyses = [_analysis("LONG", 0.9, created_at=bars[5].t + 1)]
    sigs = replay_signals(bars, analyses, None,
                          ReplayConfig(mode="ai", min_ai_conf=0.0))
    assert [s.bar_index for s in sigs] == [5]


def test_replay_filters_coin_and_drops_unaligned() -> None:
    bars = _flat_bars(10)
    analyses = [
        _analysis("LONG", 0.9, coin="ETH", created_at=bars[2].t),  # wrong coin
        _analysis("LONG", 0.9, coin="BTC", created_at=bars[-1].t + 1),  # no fwd bar
        _analysis("LONG", 0.9, coin="BTC", created_at=0),  # missing ts
        _analysis("LONG", 0.9, coin="BTC", created_at=bars[3].t),
    ]
    sigs = replay_signals(bars, analyses, None,
                          ReplayConfig(mode="ai", min_ai_conf=0.0), coin="BTC")
    assert [s.bar_index for s in sigs] == [2]


def test_replay_uses_perception_for_force() -> None:
    bars = _flat_bars(10)
    analyses = [_analysis("PASS", 0.1, pid="p9", created_at=bars[4].t)]
    percs = {"p9": _perc("p9", composite=60.0)}
    sigs = replay_signals(bars, analyses, percs,
                          ReplayConfig(mode="force", force_bar=45.0))
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].bar_index == 3


def test_replay_dedup_within_window() -> None:
    bars = _flat_bars(10)
    analyses = [
        _analysis("LONG", 0.9, created_at=bars[2].t),
        _analysis("LONG", 0.9, created_at=bars[3].t),  # 5 min later, < dedup
    ]
    cfg = ReplayConfig(mode="ai", min_ai_conf=0.0, dedup_ms=10 * 60_000)
    assert len(replay_signals(bars, analyses, None, cfg)) == 1
    cfg2 = ReplayConfig(mode="ai", min_ai_conf=0.0, dedup_ms=0)
    assert len(replay_signals(bars, analyses, None, cfg2)) == 2


def test_replay_collapses_two_verdicts_in_same_bar() -> None:
    bars = _flat_bars(10)
    # Both verdicts land inside bar 3 (next open is bar 4 → decision bar 3).
    # Only the earliest may act on that next open; its side wins.
    analyses = [
        _analysis("SHORT", 0.9, created_at=bars[3].t + 100),
        _analysis("LONG", 0.9, created_at=bars[3].t + 200),
    ]
    cfg = ReplayConfig(mode="ai", min_ai_conf=0.0, dedup_ms=0)
    sigs = replay_signals(bars, analyses, None, cfg)
    assert len(sigs) == 1
    assert sigs[0].bar_index == 3 and sigs[0].side == "short"


def test_replay_context_enrichment() -> None:
    bars = _flat_bars(10)
    analyses = [_analysis("LONG", 0.9, created_at=bars[5].t)]
    sigs = replay_signals(
        bars, analyses, None, ReplayConfig(mode="ai", min_ai_conf=0.0),
        context_fn=lambda ms: (2.5, "chop"),
    )
    assert sigs[0].entry_atr_pct == pytest.approx(2.5)
    assert sigs[0].entry_regime == "chop"


def test_bar_ms_drives_context_close_timestamp() -> None:
    """P4-5: non-5m feeds stamp the context lookup at t + real interval."""
    one_hour = 3_600_000
    bars = [Candle(t=T0 + i * one_hour, o=100, h=100, l=100, c=100, v=1.0)
            for i in range(10)]

    seen: list[int] = []
    replay_signals(
        bars, [_analysis("LONG", 0.9, created_at=bars[5].t)], None,
        ReplayConfig(mode="ai", min_ai_conf=0.0),
        context_fn=lambda ms: (seen.append(ms) or 0.0, ""),
        bar_ms=one_hour,
    )
    # Decision bar is 4; its close = open 4 + 1h.
    assert seen == [bars[4].t + one_hour]

    seen.clear()
    uptrend = _trend_bars(120)
    heuristic_signals(
        uptrend, default_heuristic_config(warmup=30),
        context_fn=lambda ms: (seen.append(ms) or 0.0, ""),
    )
    first_bar = next(s.bar_index for s in heuristic_signals(
        uptrend, default_heuristic_config(warmup=30)))
    assert seen[0] == uptrend[first_bar].t + BAR_MS


def test_capped_window_matches_full_prefix() -> None:
    """O(n) tail-capping must produce identical triggers to a growing prefix."""
    from hermes_trader.backtest import signals as S

    bars = _trend_bars(800)
    th = default_heuristic_config().thresholds
    weights = default_heuristic_config().weights
    for i in (300, 550, 799):
        full = bars[: i + 1]
        lo = max(0, i + 1 - S._WINDOW_LOOKBACK)
        tail = bars[lo: i + 1]
        _sf, hits_full = S.evaluate_window(full, th, weights)
        _st, hits_tail = S.evaluate_window(tail, th, weights)
        for a, b in zip(hits_full, hits_tail):
            assert a["fired"] == b["fired"]
            assert a["score"] == b["score"]
