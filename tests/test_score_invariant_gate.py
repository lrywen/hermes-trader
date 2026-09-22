"""Score-invariant gate tests (2026-09-22, AVAX postmortem follow-up).

The runner gate admits on the composite_score carried in the analysis
snapshot. The score_invariant gate re-scans the coin and blocks when the
live score has since fallen below the runner floor — i.e. the trade would be
booked on a score the market no longer supports (AVAX persisted score=22
after an admission that required >=30).
"""

from hermes_trader.agents.risk_gates import score_invariant_gate
from hermes_trader.models.types import GateContext


def _ctx(score: float) -> GateContext:
    return GateContext(
        confidence=0.8, current_positions=[], trade_notional_usd=10, daily_pnl=0,
        market_volume_24h_usd=1e8, coin="AVAX", trade_side="long",
        has_binary_news_risk=False, equity=20, total_open_notional=0,
        entry_px=11.0, composite_score=score,
    )


def test_blocks_when_fresh_score_crosses_below_floor():
    r = score_invariant_gate(_ctx(45), True, 30, rescorer=lambda c: 22.0)
    assert r["pass"] is False
    assert "invariant violated" in r["reason"]
    assert "22.0" in r["reason"]


def test_passes_when_fresh_score_still_above_floor():
    r = score_invariant_gate(_ctx(45), True, 30, rescorer=lambda c: 40.0)
    assert r["pass"] is True
    assert r["fresh_score"] == 40.0


def test_inconclusive_rescore_passes_open():
    r = score_invariant_gate(_ctx(45), True, 30, rescorer=lambda c: None)
    assert r["pass"] is True


def test_snapshot_below_floor_is_not_this_gate():
    # Other gates own a sub-floor snapshot; the invariant gate stays inert so
    # it can't double-block on a stale-snapshot path that bypassed the runner.
    r = score_invariant_gate(_ctx(22), True, 30, rescorer=lambda c: 10.0)
    assert r["pass"] is True


def test_disabled_passes_without_rescoring():
    called = []
    r = score_invariant_gate(
        _ctx(45), False, 30,
        rescorer=lambda c: called.append(c) or 10.0)
    assert r["pass"] is True
    assert called == []
