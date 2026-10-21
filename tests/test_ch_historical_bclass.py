"""Tests for the historical B-class analysis helpers (2026-10-21).

Network-free; cover regime construction (production BTC-proxy semantics),
signal loading (LONG only), and policy-variant construction (protect/BE knobs
override the neutral baseline without mutating it).
"""
from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(_HERE, "..", "scripts")


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _grid():
    return _load("ch_exit_param_grid", "ch_exit_param_grid.py")


def _feat():
    return _load("ch_feature_table", "ch_feature_table.py")


def test_load_signals_long_only(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"side":"LONG","coin":"A","ts":1}\n'
        '{"side":"SHORT","coin":"A","ts":2}\n'
        '{"side":"LONG","coin":"B","ts":3}\n')
    g = _grid()
    sigs = g._load_signals(str(p))
    assert len(sigs) == 2 and all(s["side"] == "LONG" for s in sigs)


def test_variants_override_knobs_without_mutating_base():
    import dataclasses
    from hermes_trader.agents.dsl_exit import ExitPolicy
    g = _grid()
    base = ExitPolicy(protect_pct=1.5, breakeven_trigger_pct=0.0)
    variants = dict((n, p) for n, p in g._variants(base))
    assert variants["baseline"].protect_pct == 1.5
    assert variants["protect_1.0"].protect_pct == 1.0
    assert variants["protect_0.75"].protect_pct == 0.75
    assert variants["breakeven_1.0"].breakeven_trigger_pct == 1.0
    assert variants["breakeven_1.0"].breakeven_lock_pct == 0.2
    # protect untouched on BE-only, combined carries both
    assert variants["breakeven_1.0"].protect_pct == 1.5
    assert variants["protect1.0+BE1.0"].protect_pct == 1.0
    assert variants["protect1.0+BE1.0"].breakeven_trigger_pct == 1.0
    # base never mutated (variants are replace() copies)
    assert base.protect_pct == 1.5 and base.breakeven_trigger_pct == 0.0
    assert dataclasses.is_dataclass(base)


def test_regime_at_lookup():
    f = _feat()
    series = [(1000, "up"), (2000, "chop"), (3000, "down")]
    assert f.regime_at(series, 1500) == "up"      # latest close <= ts
    assert f.regime_at(series, 2000) == "chop"
    assert f.regime_at(series, 2999) == "chop"
    assert f.regime_at(series, 999) == ""         # before first
    assert f.regime_at([], 1500) == ""


def test_btc_regime_series_aligned(tmp_path):
    """Aggregate BTC 15m -> 1h and produce one regime per completed window."""
    f = _feat()
    # 130 flat-ish 15m bars (~32h) so the trailing-100 window is satisfiable;
    # every 4 bars share one aligned 1h bucket. Use a mild random walk.
    # ~105 aligned 1h buckets = 420 15m bars (W=100 trailing 1h warmup).
    bars = []
    t0 = 1_700_000_000_000
    step = 15 * 60_000
    px = 100.0
    for i in range(420):
        px += 0.01
        t = t0 + i * step
        bars.append((t, px, px + 0.05, px - 0.05, px, 10.0))
    out = f.btc_regime_series({"BTC": bars})
    # one classified point per completed 1h bucket after the 100-bucket warmup;
    # result is (close_ms, regime) pairs, sorted, regime in known set.
    assert len(out) > 0
    assert all(r in ("up", "down", "chop", "neutral") for _, r in out)
    ts = [a for a, _ in out]
    assert ts == sorted(ts)
