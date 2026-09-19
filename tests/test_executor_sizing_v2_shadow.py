"""Characterization tests for the S9 sizing-v2 SHADOW observation leaf.

``_sizing_v2_shadow_observe`` was extracted verbatim from ``maybe_execute`` in
the P1-1 step ③ phase split. It must stay read-only: it records the v1-vs-v2
comparison and the CS-G cost-cap model but never mutates ``analysis`` nor the
applied stop, and every failure is swallowed best-effort (observation only).
The enforce path is intentionally NOT exercised here — it stays inline because
it drives real notional.
"""

import pytest

from hermes_trader.agents import executor

observe = executor._sizing_v2_shadow_observe


class _FakeMemory:
    """Just the four memory primitives the shadow leaf reads."""

    def __init__(self, side_slip=1.0, fee_bps=0.0, hold=8.0):
        self._side_slip = side_slip
        self._fee_bps = fee_bps
        self._hold = hold

    def avg_exit_slip_bps_side(self, coin, side, days=30.0):
        return self._side_slip, "measured"

    def avg_round_trip_fee_bps(self, coin, days=30.0):
        return self._fee_bps

    def avg_hold_hours_side(self, coin, side, days=30.0, default_hours=8.0):
        return self._hold, "measured"


def _eff():
    return {
        "effective_stop_pct": 1.0,
        "core_stop": 1.0,
        "regime_label": "trend",
        "atr_spike": False,
        "slip_adj_pct": 0.05,
        "spot_cap": 5.0,
        "roe_cap": 50.0,
    }


def _calib():
    return {"mode": "off", "regime": "neutral", "factor": 1.0,
            "effective_stop_pct": 1.0}


def _kwargs(**over):
    base = dict(
        coin="ETH",
        analysis={"id": "a1", "coin": "ETH", "side": "long"},
        leverage=10,
        agg_equity=1000.0,
        memory=_FakeMemory(),
        _risk_pct=0.02,
        _v1_stop_frac=0.025,
        _v2_stop_pct=1.0,
        _atr_pct=1.2,
        _atr_mean_pct=1.0,
        _slip_bps=0.5,
        _regime="trend",
        _eff=_eff(),
        _calib=_calib(),
        _cap=0.0,
        _atr_sizing={},
        _sizing_v2_mode="shadow",
        _sv2={"block": {"path": "/tmp/x"}},
    )
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # Deterministic fee/fills and no network funding + config lookups.
    monkeypatch.setattr(executor, "_resolve_hl_taker_fee_pct", lambda: 0.025)
    monkeypatch.setattr(executor, "_resolve_hl_round_trip_fills", lambda: 2)
    monkeypatch.setattr(executor, "cfg_get", lambda *a, **k: 24)

    import hermes_trader.client.hl_client as hl
    monkeypatch.setattr(hl, "fetch_funding_history", lambda *a, **k: [])


def test_records_shadow_comparison_with_cost_cap(monkeypatch):
    emitted = {}

    def _record(rec, path):
        emitted["rec"] = rec
        emitted["path"] = path

    monkeypatch.setattr(executor, "_sizing_v2_record_shadow", _record)
    monkeypatch.setattr(executor, "_sizing_v2_shadow_path",
                        lambda blk: "/tmp/shadow.jsonl")

    assert observe(**_kwargs()) is None

    rec = emitted["rec"]
    assert emitted["path"] == "/tmp/shadow.jsonl"
    assert rec["mode"] == "shadow"
    assert rec["coin"] == "ETH"
    # risk 0.02 * 1000 = 20; v2 stop 1% -> 2000; v1 stop 2.5% -> 800.
    assert rec["v2_notional_usd"] == 2000.0
    assert rec["v1_notional_usd"] == 800.0
    assert rec["notional_ratio"] == 2.5
    # CS-G cost-cap block merged in.
    assert rec["side"] == "long"
    assert "v2_cost_denom_pct" in rec
    assert "v2_cost_notional_usd" in rec


def test_does_not_mutate_analysis(monkeypatch):
    monkeypatch.setattr(executor, "_sizing_v2_record_shadow",
                        lambda rec, path: None)
    an = {"id": "a1", "coin": "ETH", "side": "long"}
    observe(**_kwargs(analysis=an))
    assert an == {"id": "a1", "coin": "ETH", "side": "long"}
    assert "_sizing_v2_stop_pct" not in an
    assert "_sizing_v2_breakdown" not in an


def test_short_side_flips_sign_and_normalizes(monkeypatch):
    emitted = {}
    monkeypatch.setattr(executor, "_sizing_v2_record_shadow",
                        lambda rec, path: emitted.update(rec))
    monkeypatch.setattr(executor, "_sizing_v2_shadow_path",
                        lambda blk: "/tmp/s.jsonl")

    # Funding income for a short is a negative rate; the memory side-slip on
    # shorts is higher so the incremental adverse slip widens the denominator.
    import hermes_trader.client.hl_client as hl
    monkeypatch.setattr(hl, "fetch_funding_history",
                        lambda *a, **k: [{"fundingRate": -0.0001}])
    kw = _kwargs(analysis={"id": "a2", "coin": "ETH", "side": "SHORT"},
                 memory=_FakeMemory(side_slip=2.0))
    observe(**kw)
    assert emitted["side"] == "short"


def test_invalid_side_normalizes_to_long(monkeypatch):
    emitted = {}
    monkeypatch.setattr(executor, "_sizing_v2_record_shadow",
                        lambda rec, path: emitted.update(rec))
    monkeypatch.setattr(executor, "_sizing_v2_shadow_path",
                        lambda blk: "/tmp/s.jsonl")
    observe(**_kwargs(analysis={"id": "a3", "coin": "ETH", "side": "weird"}))
    assert emitted["side"] == "long"


def test_inner_cost_cap_failure_still_emits_base_record(monkeypatch):
    """An exception inside the CS-G cost block is swallowed; the base
    v1-vs-v2 record is still emitted without cost keys."""
    emitted = {}

    def _boom(*a, **k):
        raise RuntimeError("fee resolution down")

    monkeypatch.setattr(executor, "_resolve_hl_taker_fee_pct", _boom)
    monkeypatch.setattr(executor, "_sizing_v2_record_shadow",
                        lambda rec, path: emitted.update(rec))
    monkeypatch.setattr(executor, "_sizing_v2_shadow_path",
                        lambda blk: "/tmp/s.jsonl")

    assert observe(**_kwargs()) is None
    assert emitted["v2_notional_usd"] == 2000.0
    assert "v2_cost_denom_pct" not in emitted  # cost block failed


def test_outer_record_failure_is_swallowed(monkeypatch):
    def _boom(rec, path):
        raise RuntimeError("disk full")

    monkeypatch.setattr(executor, "_sizing_v2_record_shadow", _boom)
    # Must not propagate: observation only.
    assert observe(**_kwargs()) is None
