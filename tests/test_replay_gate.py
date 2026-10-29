"""M1.5 replay: hermetic checks for candle-derived reconstruction."""
import importlib.util, os
import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..",
                      "scripts", "replay_gate.py")


def _mod():
    spec = importlib.util.spec_from_file_location("replay_gate", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _synthetic_candles(n=800, seed=7):
    from hermes_trader.models.types import Candle
    import math
    cs = []
    base = 1_700_000_000_000
    px = 100.0
    for i in range(n):
        # gentle uptrend with mild cycles -> uptrend/slow structure may appear
        drift = 0.02 + (0.05 if i % 50 < 25 else -0.01)
        o = px
        c = px + drift
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        v = 1000.0 * (3.0 if i % 90 in (0, 1, 2) else 1.0)
        cs.append(Candle(t=base + i * 300_000, o=o, h=h, l=l, c=c, v=v))
        px = c
    return cs


def test_resample_4h_is_epoch_aligned_and_ohlcv():
    m = _mod()
    c5 = _synthetic_candles(800)
    c4 = m._resample_4h(c5)
    assert len(c4) > 10
    for b in c4:
        assert b.t % (4 * 3_600_000) == 0
        assert b.h >= b.l and b.c > 0 and b.v > 0


def test_build_analysis_keys_and_replay_flags():
    m = _mod()
    from hermes_trader.agents.config import trigger_thresholds_params, trigger_weights_params
    cfg = {}
    th = trigger_thresholds_params(config=cfg)
    w = trigger_weights_params(config=cfg)
    c5 = _synthetic_candles(800)
    # no 15m/1h -> slow triggers absent, but core fields must populate safely
    data = m.Data("", use_network=False)
    an = m.build_analysis(data, "TEST", 700, c5, [], [], th, w,
                          {"enabled": True, "min_crypto_24h_pct": 10.0,
                           "min_volume_usd": 5e6})
    assert an["coin"] == "TEST" and an["side"] == "long"
    assert 0 <= an["composite_score"] <= 1000
    assert an["slow_burn_count"] == 0  # no 1h candles supplied
    assert an["mid"] == c5[700].c
    assert set(("volume_spike_fired", "breakout_fired", "momentum_burst_fired",
                "uptrend_momentum_fired", "daily_mover_fired")).issubset(an)


def test_resample_needs_enough_history_returns_none_short():
    m = _mod()
    data = m.Data("", use_network=False)
    from hermes_trader.agents.config import trigger_thresholds_params, trigger_weights_params
    th = trigger_thresholds_params(config={})
    w = trigger_weights_params(config={})
    c5 = _synthetic_candles(100)
    assert m.build_analysis(data, "X", 90, c5, [], [], th, w, {}) is None
