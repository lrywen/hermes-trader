"""Unit tests for launch_capture.low_position_relax (LIT-review feature).

A sub-threshold LONG is admitted ONLY when it is still LOW (prior-1h move not
extended) AND backed by real buyer flow. The helper reaches three boundaries
via lazy imports — 5m candles, the forming-bar drop, microstructure aggression
— all stubbed here. Fail-closed: missing data must never admit.
"""

import pytest

from hermes_trader.agents import executor
from hermes_trader.models.types import Candle


def _candle(c):
    return Candle(t=0, o=c, h=c, l=c, c=c, v=1.0)


def _config(**relax):
    base = {"enabled": True, "confidence_min": 0.58,
            "pre1h_max_pct": 1.5, "aggression_min": 0.7}
    base.update(relax)
    return {"launch_capture": {"low_position_relax": base}}


@pytest.fixture
def fed(monkeypatch):
    """Provide flat-low candles (pre1h = 0%) and strong buyer flow (0.8)."""
    import hermes_trader.agents.microstructure as micro
    import hermes_trader.agents.perception as perc
    import hermes_trader.client.hl_client as hl

    candles = [_candle(5.0) for _ in range(20)]
    monkeypatch.setattr(hl, "fetch_hl_candles", lambda *a, **k: list(candles))
    monkeypatch.setattr(perc, "_drop_forming_bar",
                        lambda cs, iv: (list(cs), False))

    class _MS:
        def aggression(self, coin):
            return 0.8

    monkeypatch.setattr(micro, "get_microstructure", lambda: _MS())
    return monkeypatch


def _allows(analysis, config):
    return executor._low_position_relax_allows(analysis, config)


def test_disabled_by_default_blocks(fed):
    cfg = {"launch_capture": {"low_position_relax": {"enabled": False}}}
    ok, _ = _allows({"coin": "TST", "side": "long",
                     "confidence": 0.60}, cfg)
    assert ok is False


def test_low_position_with_flow_admitted(fed):
    ok, detail = _allows(
        {"coin": "TST", "side": "long", "confidence": 0.60}, _config())
    assert ok is True
    assert "flow 0.80" in detail


def test_short_never_relaxed(fed):
    ok, _ = _allows({"coin": "TST", "side": "short",
                     "confidence": 0.60}, _config())
    assert ok is False


def test_confidence_below_relax_floor_blocks(fed):
    ok, detail = _allows(
        {"coin": "TST", "side": "long", "confidence": 0.55}, _config())
    assert ok is False
    assert "0.55 < 0.58" in detail


def test_extended_position_blocks(fed):
    import hermes_trader.client.hl_client as hl
    # current 5.09 vs 12-bars-ago 5.00 => +1.8% > 1.5 cap
    candles = [_candle(5.0) for _ in range(19)] + [_candle(5.09)]
    fed.setattr(hl, "fetch_hl_candles", lambda *a, **k: list(candles))
    ok, detail = _allows(
        {"coin": "TST", "side": "long", "confidence": 0.60}, _config())
    assert ok is False
    assert "pre1h" in detail


def test_weak_flow_blocks(fed):
    import hermes_trader.agents.microstructure as micro

    class _MS:
        def aggression(self, coin):
            return 0.4

    fed.setattr(micro, "get_microstructure", lambda: _MS())
    ok, detail = _allows(
        {"coin": "TST", "side": "long", "confidence": 0.60}, _config())
    assert ok is False
    assert "0.40 < 0.70" in detail


def test_missing_flow_fails_closed(fed):
    import hermes_trader.agents.microstructure as micro

    class _MS:
        def aggression(self, coin):
            return None

    fed.setattr(micro, "get_microstructure", lambda: _MS())
    ok, _ = _allows(
        {"coin": "TST", "side": "long", "confidence": 0.60}, _config())
    assert ok is False


def test_missing_candles_fails_closed(fed):
    import hermes_trader.client.hl_client as hl
    fed.setattr(hl, "fetch_hl_candles", lambda *a, **k: [])
    ok, _ = _allows(
        {"coin": "TST", "side": "long", "confidence": 0.60}, _config())
    assert ok is False
