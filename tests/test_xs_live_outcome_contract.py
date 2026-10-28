"""Contract tests for the live-shadow xs outcome -> S2 evidence feed.

Locks the unit/schema agreement after the outcome-backfill wiring fix:
  * reconcile_xs_reversal_shadow writes NET percent on rec["net_outcome"]
    (outcome stays a win/loss string for the grader);
  * xs_s2_daily_dashboard.live_fwd72_net_pct and
    xs_top_pct_dual_count._live_fwd72_net_pct read that net PERCENT from three
    evolving schemas (net_outcome -> legacy outcome dict -> gross forward grid
    minus cost);
  * the value is fed in PERCENT (not divided by 100), so downstream *100 gives
    true bp on both the historical and live panels.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _REPO / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


dash = _load("xs_dash_contract", "scripts/xs_s2_daily_dashboard.py")
dual = _load("xs_dual_contract", "scripts/xs_top_pct_dual_count.py")
COST = 9.26


def test_readers_prefer_net_outcome_dict():
    rec = {"net_outcome": {"fwd72_net_pct": 2.968}}
    assert dash.live_fwd72_net_pct(rec) == 2.968
    assert dual._live_fwd72_net_pct(rec) == 2.968


def test_readers_fallback_legacy_outcome_dict():
    rec = {"outcome": {"fwd72_net_pct": 1.5}}
    assert dash.live_fwd72_net_pct(rec) == 1.5
    assert dual._live_fwd72_net_pct(rec) == 1.5


def test_readers_derive_from_gross_forward_minus_cost():
    # gross +5% at 72h on the forward grid -> net percent = 5 - 0.0926
    rec = {"outcome": "win", "forward": {"fwd72h_pct": 5.0}}
    exp = 5.0 - COST / 100.0
    assert dash.live_fwd72_net_pct(rec) == exp
    assert dual._live_fwd72_net_pct(rec) == exp


def test_string_outcome_without_grid_returns_none():
    assert dash.live_fwd72_net_pct({"outcome": "win"}) is None
    assert dual._live_fwd72_net_pct({"outcome": None}) is None
    assert dash.live_fwd72_net_pct({}) is None


def test_net_outcome_wins_over_legacy_outcome_dict():
    rec = {"net_outcome": {"fwd72_net_pct": 3.0},
           "outcome": {"fwd72_net_pct": 9.0}}
    assert dash.live_fwd72_net_pct(rec) == 3.0
    assert dual._live_fwd72_net_pct(rec) == 3.0


def test_live_panel_unit_yields_true_bp(tmp_path):
    # One mature candidate row: net +2.968% must read as 296.8bp in the live panel.
    f = tmp_path / "xs.jsonl"
    import json, time as _t
    row = {"timestamp": int(_t.time() * 1000) - 200 * 3600 * 1000,
           "coin": "AAA", "is_candidate": True,
           "ext_percentile": 0.05, "ext_pct": -12.0, "rsi14": 25.0,
           "atr24_pct": 2.0, "atr_pctile": 0.9,
           "net_outcome": {"fwd72_net_pct": 2.968}}
    f.write_text(json.dumps(row) + chr(10))
    res = dash.run_jsonl(str(f), boot=500, seed=1)
    leg = res["jsonl_live"]["none"]
    assert leg["n_episodes"] == 1
    # percent 2.968 *100 = 296.8 bp (NOT 2.968bp which the old /100 produced)
    assert leg["mean_net_bp"] == 296.8


def test_dual_count_live_unit_true_bp(tmp_path):
    f = tmp_path / "xs.jsonl"
    import json, time as _t
    # pctile in the 85-only band (0.10 < p <= 0.15)
    row = {"timestamp": int(_t.time() * 1000) - 200 * 3600 * 1000,
           "coin": "AAA", "is_candidate": True, "ext_percentile": 0.12,
           "net_outcome": {"fwd72_net_pct": 2.968}}
    f.write_text(json.dumps(row) + chr(10))
    res = dual.jsonl_dual(str(f), boot=500, seed=1, days=None)
    assert res["band_85_only"]["n"] == 1
    assert res["band_85_only"]["mean_net_bp"] == 296.8
