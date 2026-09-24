"""Tests for the coin-local strong-breakout trend override (regime fix).

A strong, high-RVOL coin breakout must upgrade the sizing/exit regime to
``up``/``down`` (trend-ride + wide stop) even when the macro market regime is
neutral — the AERO / ARB misclassification.
"""
from types import SimpleNamespace

from hermes_trader.agents import executor


def _bars(volumes):
    base_t = 1_700_000_000_000
    return [
        SimpleNamespace(t=base_t + i * 300_000, o=1.0, h=1.0, l=1.0,
                        c=1.0, v=float(v))
        for i, v in enumerate(volumes)
    ]


def _patch_candles(monkeypatch, bars):
    def _fake_fetch(coin, interval, limit):
        return list(bars)
    import hermes_trader.client.hl_client as hl
    monkeypatch.setattr(hl, "fetch_hl_candles", _fake_fetch)
    # executor imports the name lazily inside the helper from the module, so
    # patching the source module is sufficient.


def test_no_breakout_returns_empty():
    assert executor.coin_breakout_regime({"breakout_fired": False}) == ""
    assert executor.coin_breakout_regime({}) == ""


def test_strong_long_breakout_is_up(monkeypatch):
    bars = _bars([100] * 20 + [300])  # RVOL 3.0
    _patch_candles(monkeypatch, bars)
    analysis = {"coin": "ARB", "breakout_fired": True, "side": "long"}
    assert executor.coin_breakout_regime(analysis) == "up"


def test_strong_short_breakout_is_down(monkeypatch):
    bars = _bars([100] * 20 + [250])  # RVOL 2.5
    _patch_candles(monkeypatch, bars)
    analysis = {"coin": "X", "breakout_fired": True, "side": "short"}
    assert executor.coin_breakout_regime(analysis) == "down"


def test_weak_rvol_stays_empty(monkeypatch):
    bars = _bars([100] * 20 + [160])  # RVOL 1.6 < 2.0
    _patch_candles(monkeypatch, bars)
    analysis = {"coin": "AERO", "breakout_fired": True, "side": "long"}
    assert executor.coin_breakout_regime(analysis) == ""


def test_direction_falls_back_to_ema(monkeypatch):
    bars = _bars([100] * 20 + [300])
    _patch_candles(monkeypatch, bars)
    analysis = {
        "coin": "ARB", "breakout_fired": True,
        "ema8_1h": 1.1, "ema21_1h": 1.0,
    }
    assert executor.coin_breakout_regime(analysis) == "up"


def test_thin_snapshot_returns_empty(monkeypatch):
    _patch_candles(monkeypatch, _bars([100] * 5))
    analysis = {"coin": "ARB", "breakout_fired": True, "side": "long"}
    assert executor.coin_breakout_regime(analysis) == ""


def test_strong_bar_within_window_upgrades(monkeypatch):
    # XPL case: the 6x launch bar printed two bars before the decision; the
    # latest bar is post-launch low volume. A 3-bar window still qualifies.
    vols = [100] * 20 + [600] + [300] + [50]
    bars = _bars(vols)
    _patch_candles(monkeypatch, bars)
    analysis = {"coin": "XPL", "breakout_fired": True, "side": "long"}
    assert executor.coin_breakout_regime(analysis) == "up"


def test_strong_bar_outside_window_stays_empty(monkeypatch):
    # Strong bar is more than the window (3 bars) back → no upgrade.
    vols = [100] * 20 + [600] + [100] + [100] + [100]
    bars = _bars(vols)
    _patch_candles(monkeypatch, bars)
    analysis = {"coin": "XPL", "breakout_fired": True, "side": "long"}
    assert executor.coin_breakout_regime(analysis) == ""


def test_window_lookback_config(monkeypatch):
    # With a wider lookback (5), the strong bar 4 bars back now qualifies.
    monkeypatch.setattr(
        executor, "cfg_get",
        lambda key, default=None: (
            5 if key == "launch_capture.breakout_trend_rvol_lookback"
            else (2.0 if key == "launch_capture.breakout_trend_rvol_min"
                  else default))
    )
    vols = [100] * 20 + [600] + [100] + [100] + [100]
    bars = _bars(vols)
    _patch_candles(monkeypatch, bars)
    analysis = {"coin": "XPL", "breakout_fired": True, "side": "long"}
    assert executor.coin_breakout_regime(analysis) == "up"
