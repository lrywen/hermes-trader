"""Tests for the record-only Candidate A shadow arm (merge-plan §5.3).

Candidate A = higherLows1h AND pctMoveSpike AND volumeSpike under the coin's
own 1h regime=="up", fixed 4h hold. It must be a strict no-op when disabled,
must never imply a short/route/order, must require all three legs + up regime,
and must dedup the same closed bar.
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.agents import candidate_a_shadow as ca


_LEGS = ["higherLows1h", "pctMoveSpike", "volumeSpike"]


def _cfg(enabled=True, require_regime_up=True, log_path=""):
    return {"launch_capture": {"candidate_a_shadow": {
        "enabled": enabled, "require_regime_up": require_regime_up,
        "log_path": log_path}}}


def _perc(fired=_LEGS, bar_close=1_000, mid=100.0, score=60.0):
    return {"bar_close_ms": bar_close, "mid": mid,
            "composite_score": score, "fired_triggers": list(fired)}


@pytest.fixture(autouse=True)
def _reset():
    ca.reset_seen_for_tests()
    yield
    ca.reset_seen_for_tests()


def test_disabled_is_strict_noop(tmp_path):
    log = tmp_path / "ca.jsonl"
    out = ca.maybe_record(
        coin="ETH", perception=_perc(), regime="up",
        config=_cfg(enabled=False, log_path=str(log)),
        data_dir=str(tmp_path))
    assert out is None and not log.exists()


def test_requires_all_three_legs():
    cfg = _cfg()
    assert ca.in_candidate_a(fired_names=_LEGS, regime="up", config=cfg)
    for leg in _LEGS:
        missing = [n for n in _LEGS if n != leg]
        assert ca.in_candidate_a(
            fired_names=missing, regime="up", config=cfg) is False


def test_requires_up_regime_when_enabled():
    cfg = _cfg(require_regime_up=True)
    assert ca.in_candidate_a(fired_names=_LEGS, regime="chop", config=cfg) is False
    assert ca.in_candidate_a(fired_names=_LEGS, regime="down", config=cfg) is False
    assert ca.in_candidate_a(fired_names=_LEGS, regime=None, config=cfg) is False
    # filter can be relaxed (regime is still recorded)
    cfg_relaxed = _cfg(require_regime_up=False)
    assert ca.in_candidate_a(
        fired_names=_LEGS, regime="chop", config=cfg_relaxed) is True


def test_record_written_and_dedup(tmp_path):
    log = tmp_path / "ca.jsonl"
    cfg = _cfg(log_path=str(log))
    rec1 = ca.maybe_record(
        coin="ETH", perception=_perc(), regime="up",
        config=cfg, data_dir=str(tmp_path))
    rec2 = ca.maybe_record(
        coin="ETH", perception=_perc(), regime="up",
        config=cfg, data_dir=str(tmp_path))
    assert rec1 is not None and rec2 is None  # same closed bar deduped
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["coin"] == "ETH" and r["regime"] == "up"
    assert r["combo"] == _LEGS and r["shadow"] is True
    assert r["hold_bars_1h"] == 4 and r["round_trip_cost_bps"] == 9.47
    assert set(r["fired_triggers"]) == set(_LEGS)


def test_different_closed_bars_both_recorded(tmp_path):
    cfg = _cfg()
    a = ca.maybe_record(coin="ETH", perception=_perc(bar_close=1000),
                        regime="up", config=cfg, data_dir=str(tmp_path))
    b = ca.maybe_record(coin="ETH", perception=_perc(bar_close=2000),
                        regime="up", config=cfg, data_dir=str(tmp_path))
    assert a is not None and b is not None


def test_default_log_path_under_data_dir(tmp_path):
    rec = ca.maybe_record(
        coin="ETH", perception=_perc(), regime="up",
        config=_cfg(log_path=""), data_dir=str(tmp_path))
    assert rec is not None
    assert (tmp_path / "candidate_a_shadow.jsonl").exists()


def test_never_raises_on_bad_input():
    assert ca.maybe_record(
        coin="ETH", perception=None, regime="up",
        config=_cfg(), data_dir="/nonexistent-dir") is None
