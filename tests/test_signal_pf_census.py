"""Tests for the S2 signal-source PF census (scripts/signal_pf_census.py).

The script is a standalone CLI under scripts/ (not a package module). It imports
the S1 report as a sibling module, so both are loaded via importlib and
registered in sys.modules before exec (required for @dataclass resolution). Only
the pure core functions are unit-tested; no candle fetching or network I/O is
ever invoked.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# S1 report must be registered first (the census imports it at module load).
pf = _load("pf_dual_period_report", "pf_dual_period_report.py")
ce = _load("signal_pf_census", "signal_pf_census.py")


# ── analysis verdict -> side ─────────────────────────────────────────────────

@pytest.mark.parametrize(("verdict", "side"), [
    ("LONG", "long"),
    ("SHORT", "short"),
    ("PASS", None),
    ("CLOSE", None),
    ("", None),
])
def test_analysis_side(verdict, side):
    assert ce.analysis_side({"verdict": verdict}) == side


# ── directional analysis selection ───────────────────────────────────────────

def _a(verdict, coin="BTC", ts=1_000, **kw):
    a = {"verdict": verdict, "coin": coin, "created_at": ts}
    a.update(kw)
    return a


def test_iter_directional_analyses_filters_and_orders():
    now = int(time.time() * 1000)
    mem = {"analyses": [
        _a("PASS", ts=now - 10),
        _a("LONG", coin="", ts=now - 20),            # no coin -> dropped
        _a("LONG", ts=now - 100),
        _a("SHORT", ts=now - 50),
        _a("LONG", ts=now - 9_999_999_999),          # before cutoff -> dropped
        "not-a-dict",
    ]}
    out = ce.iter_directional_analyses(mem, now - 1_000_000)
    assert [a["verdict"] for a in out] == ["LONG", "SHORT"]
    assert all(str(a["coin"]) for a in out)


def test_iter_directional_analyses_empty():
    assert ce.iter_directional_analyses({}, 0) == []
    assert ce.iter_directional_analyses({"analyses": []}, 0) == []


# ── context bucket tags ──────────────────────────────────────────────────────

def test_logged_tags_base_and_provenance():
    # real debate consensus
    tags = ce.logged_tags(_a("LONG", debate_used=True, ai_down=False))
    assert "llm_verdict" in tags and "llm_debate" in tags
    assert "llm_single" not in tags and "ai_down" not in tags
    # single-model fallback (no debate flag, not degraded)
    tags = ce.logged_tags(_a("LONG"))
    assert "llm_single" in tags and "llm_debate" not in tags
    # degraded ai_down wins over the debate/single split
    tags = ce.logged_tags(_a("LONG", ai_down=True, debate_used=True))
    assert "ai_down" in tags and "llm_debate" not in tags and "llm_single" not in tags


def test_logged_tags_context_bands():
    tags = ce.logged_tags(_a("LONG", whale_signal={"bias": "long"},
                             news_risk="positive", composite_score=75.0,
                             confidence=0.8))
    assert "whale_present" in tags
    assert "news_positive" in tags and "news_negative" not in tags
    assert "composite_ge70" in tags
    assert "conf_ge70" in tags


def test_logged_tags_negative_news_and_low_bands():
    tags = ce.logged_tags(_a("SHORT", whale_signal=None, news_risk="negative",
                             composite_score=40.0, confidence=0.5))
    assert "whale_present" not in tags
    assert "news_negative" in tags
    assert "composite_lt50" in tags
    assert "conf_lt70" in tags


def test_logged_tags_composite_mid_band():
    tags = ce.logged_tags(_a("LONG", composite_score=60.0))
    assert "composite_50_70" in tags


def test_logged_tags_fired_triggers():
    tags = ce.logged_tags(_a("LONG", momentum_burst_fired=True,
                             breakout_fired=True, volume_spike_fired=True,
                             uptrend_momentum_fired=True,
                             downtrend_momentum_fired=True,
                             daily_mover_fired=True, slow_burn_count=2))
    for t in ("trig_momentum_burst", "trig_breakout", "trig_volume_spike",
              "trig_uptrend", "trig_downtrend", "trig_daily_mover",
              "trig_slow_burn"):
        assert t in tags
    # slow-burn flag variant (no count)
    assert "trig_slow_burn" in ce.logged_tags(
        _a("LONG", slow_burn_fired=True, slow_burn_count=0))
    # none fired
    bare = ce.logged_tags(_a("LONG"))
    assert not any(t.startswith("trig_") for t in bare)


def test_logged_tags_news_none_omitted():
    tags = ce.logged_tags(_a("LONG", news_risk="none"))
    assert "news_positive" not in tags and "news_negative" not in tags


# ── forward trade construction ───────────────────────────────────────────────

class _C:
    def __init__(self, t, o, c):
        self.t, self.o, self.c = t, o, c


def test_trade_from_forward_long():
    a = _a("LONG", coin="ETH", ts=1000)
    fwd = [_C(1000, 100.0, 101.0), _C(2000, 101.0, 102.0), _C(3000, 102.0, 110.0)]
    t = ce.trade_from_forward(a, fwd, hold_bars=2, cost_pct=0.09)
    assert t is not None
    assert t.side == "long" and t.coin == "ETH"
    assert t.entry_ts == 1000 and t.exit_ts == 3000
    # entry open 100 -> exit close 110 = +10% gross
    assert t.gross_pct == pytest.approx(10.0)
    assert t.net_pct == pytest.approx(9.91)


def test_trade_from_forward_short():
    a = _a("SHORT", ts=1000)
    fwd = [_C(1000, 100.0, 99.0), _C(2000, 99.0, 90.0)]
    t = ce.trade_from_forward(a, fwd, hold_bars=1, cost_pct=0.0)
    assert t is not None and t.side == "short"
    assert t.gross_pct == pytest.approx(10.0)  # price fell 100->90


def test_trade_from_forward_insufficient_bars():
    a = _a("LONG", ts=1000)
    assert ce.trade_from_forward(a, [_C(1, 100.0, 101.0)], hold_bars=2, cost_pct=0.0) is None
    # PASS verdict -> no direction
    assert ce.trade_from_forward(_a("PASS"),
                                 [_C(1, 100.0, 101.0)] * 5, 2, 0.0) is None
    # non-positive entry price
    assert ce.trade_from_forward(_a("LONG"),
                                 [_C(1, 0.0, 1.0)] * 5, 2, 0.0) is None


# ── single-horizon gate ──────────────────────────────────────────────────────

def _stats(n, net_pf):
    return pf.PFStats(n=n, gross_pf=net_pf, net_pf=net_pf,
                      gross_first_half=None, gross_second_half=None,
                      net_first_half=None, net_second_half=None)


@pytest.mark.parametrize(("n", "net_pf", "want"), [
    (10, 1.30, "INSUFFICIENT"),   # under sample floor
    (30, 1.04, "FAIL"),           # enough samples but under gate
    (30, 1.20, "PASS"),
    (30, None, "PASS"),           # no losses -> clears
    (5, 0.8, "INSUFFICIENT"),     # short samples dominate over fail
])
def test_logged_verdict(n, net_pf, want):
    assert ce.logged_verdict(_stats(n, net_pf), 30, 1.05) == want


# ── momentum burst side parsing (offline addition) ───────────────────────────

def test_momentum_burst_side_parsing():
    assert pf._momentum_burst_side({"reason": "+5.0% over 2 bars up"}) == "long"
    assert pf._momentum_burst_side({"reason": "-5.0% over 2 bars down"}) == "short"
    assert pf._momentum_burst_side({"reason": "flat"}) is None
    assert pf._momentum_burst_side({}) is None
