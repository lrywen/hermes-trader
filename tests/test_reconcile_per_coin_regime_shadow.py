"""Offline tests for reconcile_per_coin_regime_shadow (no network).

Covers:
  * long + short candle-walk grading into sim_winner / sim_loser,
  * real-close join preferred over candles (spot in the trade direction),
  * quadrant tier + soft-demotion aggregation stats,
  * terminal buckets (tier_na / no_side / no_future_bars) and pending when
    candles are still missing.
fetch_hl_candles and the memory file are stubbed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_per_coin",
    _REPO / "scripts" / "reconcile_per_coin_regime_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_per_coin"] = mod
_spec.loader.exec_module(mod)


class _C:
    def __init__(self, t, o, h, l, c):
        self.t, self.o, self.h, self.l, self.c = t, o, h, l, c


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _candles(entry_ts_iso: str, prices):
    t0 = datetime.fromisoformat(entry_ts_iso.replace("Z", "+00:00"))
    base = int(t0.timestamp() * 1000) - 3600_000
    out = []
    px = prices[0]
    for i, p in enumerate(prices):
        out.append(_C(base + i * 3600_000, px, max(px, p), min(px, p), p))
        px = p
    return out


_DSL = {"max_loss_pct": 0.8, "protect_pct": 1.5, "retrace_threshold": 0.5}


def _rec(side="long", tier="weak_review", would="pass", ts=None):
    return {"timestamp": ts or _iso(48), "rule": "per_coin_regime",
            "coin": "AAA", "side": side, "would": would,
            "detail": {"quadrant_tier": tier, "macro_regime": "up",
                       "own_1h_regime": "down", "own_1h_score": 0.4},
            "outcome": None}


# ── candle walk grading (long + short) ─────────────────────────────────────

def test_long_weak_tier_graded_loser(monkeypatch):
    cands = _candles(_iso(48), [100, 99.4, 98.5, 97])
    monkeypatch.setattr(mod, "fetch_hl_candles", lambda *a, **k: cands)
    r = _rec("long", "weak_review")
    assert mod.grade_record(r, _DSL, {}) is True
    assert r["outcome"] == "sim_loser"
    assert r["pnl_pct"] < 0


def test_short_side_grades_winner_when_price_falls(monkeypatch):
    # short profits as price drops below entry
    cands = _candles(_iso(48), [100, 99, 98, 97, 96.5])
    monkeypatch.setattr(mod, "fetch_hl_candles", lambda *a, **k: cands)
    r = _rec("short", "strong")
    assert mod.grade_record(r, _DSL, {}) is True
    assert r["outcome"] == "sim_winner"
    assert r["pnl_pct"] > 0


def test_short_side_stops_when_price_rises(monkeypatch):
    cands = _candles(_iso(48), [100, 100.9, 101.5, 102])
    monkeypatch.setattr(mod, "fetch_hl_candles", lambda *a, **k: cands)
    r = _rec("short", "weak_review")
    assert mod.grade_record(r, _DSL, {}) is True
    assert r["outcome"] == "sim_loser"


def test_pending_when_no_future_bars(monkeypatch):
    # entry lands at/after the last candle -> terminal no_future_bars
    cands = _candles(_iso(0), [100, 101])
    monkeypatch.setattr(mod, "fetch_hl_candles", lambda *a, **k: cands)
    r = _rec("long", "mid", ts=_iso(0))
    assert mod.grade_record(r, _DSL, {}) is True
    assert r["outcome"] == "no_future_bars"


def test_fetch_error_leaves_pending(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(mod, "fetch_hl_candles", _boom)
    r = _rec("long", "mid")
    assert mod.grade_record(r, _DSL, {}) is False
    assert r.get("outcome") is None and "sim_error" in r


# ── real-close join preferred ──────────────────────────────────────────────

def test_real_close_long_preferred(monkeypatch):
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: pytest.fail("should not fetch"))
    sig_ms = datetime.now(timezone.utc).timestamp() * 1000 - 48 * 3600_000
    closes = {"AAA": [{"closed_at": sig_ms + 1000, "side": "long",
                       "spot_pct": -0.83, "realized_pnl_usd": -5.0,
                       "leverage": 10}]}
    r = _rec("long", "weak_review")
    assert mod.grade_record(r, _DSL, closes) is True
    assert r["outcome"] == "real_loser"
    assert r["real_close"]["spot_pct"] == -0.83


def test_real_close_short_flips_sign():
    sig_ms = datetime.now(timezone.utc).timestamp() * 1000 - 48 * 3600_000
    closes = {"AAA": [{"closed_at": sig_ms, "side": "short",
                       "spot_pct": 1.2, "realized_pnl_usd": -3.0}]}
    r = _rec("short", "strong")
    # price ROSE +1.2% against a short -> signed pnl negative
    assert mod.grade_record(r, _DSL, closes) is True
    assert r["outcome"] == "real_loser"
    assert r["pnl_pct"] < 0


def test_real_close_side_mismatch_skipped(monkeypatch):
    # a long close must not join a short candidate; falls through to sim
    cands = _candles(_iso(48), [100, 99, 98, 97])
    monkeypatch.setattr(mod, "fetch_hl_candles", lambda *a, **k: cands)
    sig_ms = datetime.now(timezone.utc).timestamp() * 1000 - 48 * 3600_000
    closes = {"AAA": [{"closed_at": sig_ms, "side": "long", "spot_pct": 3.0}]}
    r = _rec("short", "strong")
    assert mod.grade_record(r, _DSL, closes) is True
    assert r["outcome"] == "sim_winner"
    assert "real_close" not in r


# ── terminal buckets ───────────────────────────────────────────────────────

def test_tier_na_and_no_side():
    r = _rec("long"); r["detail"]["quadrant_tier"] = "n/a"
    assert mod.grade_record(r, _DSL, {}) is True and r["outcome"] == "tier_na"
    r2 = _rec("long"); r2["side"] = ""
    assert mod.grade_record(r2, _DSL, {}) is True and r2["outcome"] == "no_side"


# ── aggregation stats ──────────────────────────────────────────────────────

def test_stats_and_summary_grouping(capsys):
    rows = [
        {"outcome": "sim_loser", "would": "demote_to_weak_aligned",
         "pnl_pct": -0.9, "detail": {"quadrant_tier": "weak_review"}},
        {"outcome": "sim_loser", "would": "demote_to_weak_aligned",
         "pnl_pct": -0.5, "detail": {"quadrant_tier": "weak_review"}},
        {"outcome": "sim_winner", "would": "pass", "pnl_pct": 2.0,
         "detail": {"quadrant_tier": "strong"}},
        {"outcome": "sim_winner", "would": "pass", "pnl_pct": 1.0,
         "detail": {"quadrant_tier": "mid"}},
    ]
    s_weak = mod._stats([r for r in rows
                         if r["detail"]["quadrant_tier"] == "weak_review"])
    assert s_weak["graded"] == 2 and s_weak["win_rate"] == 0.0
    assert s_weak["mean_pct"] == pytest.approx(-0.7)
    mod._summary(rows)
    out = capsys.readouterr().out
    assert "weak_review" in out and "strong" in out
    assert "would demote" in out
