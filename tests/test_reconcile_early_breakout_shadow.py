"""Offline tests for reconcile_early_breakout_shadow (no network).

Pins the long two-phase walk (tight ATR stop / trailing), half-size scaling,
real-close join, and no-atr / fetch-error handling.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_early",
    _REPO / "scripts" / "reconcile_early_breakout_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_early"] = mod
_spec.loader.exec_module(mod)


class _C:
    def __init__(self, t, o, h, l, c):
        self.t, self.o, self.h, self.l, self.c = t, o, h, l, c


def _iso(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _candles(iso_ts, prices):
    t0 = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    base = int(t0.timestamp() * 1000) - 3600_000
    out = []
    px = prices[0]
    for i, p in enumerate(prices):
        out.append(_C(base + i * 3600_000, px, max(px, p), min(px, p), p))
        px = p
    return out


_DSL = {"dsl_exit": {"protect_pct": 1.5, "retrace_threshold": 0.5}}


def _detail(entry=100.0, atr_pct=1.0, mult=1.2, frac=0.5):
    return {"entry_px": entry, "atr4h_pct": atr_pct, "early_stop_atr_mult": mult,
            "early_size_fraction": frac, "fresh_impulse": True,
            "composite_score": 12.6, "confidence": 0.6}


def _rec(detail=None, ts=None):
    return {"timestamp": ts or _iso(48), "rule": "early_breakout_entry",
            "coin": "AAA", "side": "long", "detail": detail or _detail(),
            "outcome": None}


def test_tight_stop_when_price_drops(monkeypatch):
    # stop = 1.2 * 1% = 1.2%; a bar dipping -1.5% stops out (losing, half size)
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: _candles(_iso(48),
                                                 [100, 99.5, 98.4, 98.0]))
    r = _rec()
    assert mod.grade_record(r, _DSL, {}) is True
    assert r["exit_reason"] == "tight_stop"
    assert r["early_pnl_pct"] < 0
    # half-size magnitude is roughly half of full size
    assert abs(r["early_pnl_pct"] - r["full_size_pnl_pct"] * 0.5) < 0.01


def test_winner_half_size(monkeypatch):
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: _candles(_iso(48),
                                                 [100, 102, 103, 104]))
    r = _rec()
    assert mod.grade_record(r, _DSL, {}) is True
    assert r["early_pnl_pct"] > 0 and r["mfe_pct"] > 3


def test_real_close_join_scaled():
    sig = datetime.now(timezone.utc).timestamp() * 1000 - 48 * 3600_000
    closes = {"AAA": [{"closed_at": sig, "side": "long", "spot_pct": 4.0,
                       "mfe_pct": 6.0}]}
    r = _rec()
    assert mod.grade_record(r, _DSL, closes) is True
    assert r["outcome"] == "real_winner"
    # (4.0 - 0.05 fee) * 0.5
    assert abs(r["early_pnl_pct"] - (3.95 * 0.5)) < 0.01


def test_no_atr_pending_field():
    r = _rec(_detail(atr_pct=None))
    assert mod.grade_record(r, _DSL, {}) is True
    assert r["outcome"] == "no_atr"


def test_fetch_error_returns_false(monkeypatch):
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = _rec()
    assert mod.grade_record(r, _DSL, {}) is False
    assert r.get("outcome") is None and "sim_error" in r


def test_signal_bar_open_fallback(monkeypatch):
    d = _detail(entry=None)
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: _candles(_iso(48),
                                                 [100, 102, 103, 104]))
    r = _rec(d)
    assert mod.grade_record(r, _DSL, {}) is True
    assert r.get("entry_px_source") == "signal_bar_open"
