# -*- coding: utf-8 -*-
"""Read-only shadow-evidence dashboard route payload tests (no funds, no HTTP).

Covers xs panic (conditional vs active), net-outcome (net=gross-cost, schema
fallback), and runner 40/45 dual counts, incl. missing-file fail-soft.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from hermes_trader.dashboard_routes import shadow_evidence as ev


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HERMES_XS_REVERSAL_SHADOW_FILE", raising=False)
    monkeypatch.delenv("HERMES_RUNNER_GATE_SHADOW_FILE", raising=False)
    return tmp_path


def _w(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def now_ms():
    return int(time.time() * 1000)


def test_missing_files_fail_soft(data_dir):
    p = ev._panic_payload(30)
    assert p["present"] is False and p["rows"] == 0
    assert ev._net_payload(30, False)["present"] is False
    assert ev._dual_payload(30)["present"] is False


def test_panic_separates_conditional_pass_from_active_switch(data_dir):
    ts = now_ms()
    _w(data_dir / ev._XS_NAME, [
        # switch off but conditional verdict True (8e18a84 behavior)
        {"timestamp": ts, "coin": "BTC", "panic_gate_active": False,
         "panic_gate_pass": True, "is_candidate": True, "rsi14": 25.0,
         "atr24_pct": 5.0, "atr_pctile": 0.8, "panic_gate_reasons": ["atr"]},
        # legacy row: only panic_gate_enabled
        {"timestamp": ts - 1000, "coin": "ETH", "panic_gate_enabled": True,
         "panic_gate_pass": False, "rsi14": 40.0},
    ])
    p = ev._panic_payload(30)
    assert p["rows"] == 2
    assert p["conditional_pass"] == 1
    assert p["active_switch_true"] == 1
    assert p["active_and_pass"] == 0
    assert p["candidates"] == 1
    assert p["reason_counts"] == {"atr": 1}
    assert p["avg_rsi14"] == 32.5


def test_net_outcome_is_gross_minus_cost_and_fallback(data_dir):
    ts = now_ms()
    rows = [
        # new contract: net_outcome present (percent)
        {"timestamp": ts, "coin": "A", "is_candidate": True,
         "pnl_pct": 3.0, "net_outcome": {"cost_bps_rt": 9.26,
                                          "fwd72_net_pct": 2.9074}},
        # legacy: only pnl_pct -> net = gross - cost
        {"timestamp": ts - 1000, "coin": "B", "is_candidate": True,
         "pnl_pct": 1.0},
    ]
    _w(data_dir / ev._LEDGER_NAME, rows)
    d = ev._net_payload(30, candidates_only=True)
    assert d["rows"] == 2
    w72 = d["windows"]["fwd72h"]
    assert w72["n_net"] == 2
    # bp: 290.74 and (1.0 - .0926)*100 = 90.74
    assert w72["net_mean_bp"] == pytest.approx((290.74 + 90.74) / 2, abs=0.01)
    assert w72["net_win_rate_pct"] == 100.0
    assert w72["gross_mean_bp"] == pytest.approx(200.0, abs=0.01)
    assert d["cost_bps_rt"] == 9.26


def test_dual_counts_pass_levels_and_low_score_admits(data_dir):
    ts = now_ms()
    _w(data_dir / ev._RUNNER_NAME, [
        {"ts": ts, "coin": "C1", "passes40": True, "passes45": False,
         "admitted": True, "composite_score": 42.0, "rule": "bypass (ai)"},
        {"ts": ts - 1000, "coin": "C2", "passes40": True, "passes45": True,
         "admitted": True, "composite_score": 47.0, "rule": "ok"},
        {"ts": ts - 2000, "coin": "C3", "passes40": False, "passes45": False,
         "admitted": False, "composite_score": 20.0, "rule": "blocked (x)"},
    ])
    d = ev._dual_payload(30)
    assert d["rows"] == 3
    assert d["passes40"] == 2
    assert d["passes45"] == 1
    assert d["passes40_not45"] == 1
    assert d["admitted"] == 2
    assert d["admitted_score_below45"] == 1


def test_window_filter_excludes_old_rows(data_dir):
    old = int((time.time() - 100 * 86400) * 1000)
    _w(data_dir / ev._RUNNER_NAME, [
        {"ts": old, "coin": "OLD", "passes40": True, "passes45": True,
         "admitted": True, "composite_score": 50.0, "rule": "ok"}])
    assert ev._dual_payload(30)["rows"] == 0
    assert ev._dual_payload(120)["rows"] == 1
