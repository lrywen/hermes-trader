"""Offline tests for reconcile_short_only_shadow (no network).

Covers: short candle walk (win as price falls, stop as it rises), real-close
join with sign flip, the Tier-A admittable predicate (confidence + structure),
Tier A/B split, and pending on fetch error. fetch_hl_candles / memory stubbed.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "reconcile_short",
    _REPO / "scripts" / "reconcile_short_only_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_short"] = mod
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


_CFG = {
    "runner_entry_gate": {"min_short_confidence": 0.68,
                          "min_short_composite": 40},
    "dsl_exit": {"max_loss_pct": 0.8, "protect_pct": 1.5,
                 "retrace_threshold": 0.5},
}
_DSL = _CFG["dsl_exit"]


def _detail(conf=0.71, score=48, downtrend=True, burst=True, volume=False,
            breakout=False, slow=1, entry=100.0, macro="down", own="down"):
    return {"confidence": conf, "composite_score": score, "entry_px": entry,
            "downtrend": downtrend, "burst": burst, "volume_spike": volume,
            "breakout": breakout, "slow_burn_count": slow,
            "macro_regime": macro, "own_1h_regime": own}


def _rec(detail, ts=None):
    return {"timestamp": ts or _iso(48), "rule": "short_only",
            "coin": "AAA", "side": "short", "detail": detail, "outcome": None}


# ── admittable predicate ──────────────────────────────────────────────────

def test_admittable_strong_downtrend():
    ok, info = mod._admittable(_detail(), _CFG)
    assert ok and info["conf_ok"] and info["structured"]


def test_not_admittable_low_confidence():
    ok, info = mod._admittable(_detail(conf=0.60, downtrend=True), _CFG)
    assert not ok and not info["conf_ok"] and info["structured"]


def test_not_admittable_no_structure():
    # conf passes but no downtrend and no fresh impulse + low score
    ok, info = mod._admittable(
        _detail(conf=0.71, score=20, downtrend=False, burst=False, slow=0),
        _CFG)
    assert not ok and info["conf_ok"] and not info["structured"]


def test_admittable_via_fresh_burst_and_score():
    # no downtrend flag, but burst + score>=40 -> structured
    ok, _ = mod._admittable(
        _detail(downtrend=False, burst=True, score=42, slow=0), _CFG)
    assert ok


# ── grading: sim + real close ──────────────────────────────────────────────

def test_short_sim_winner_when_price_falls(monkeypatch):
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: _candles(_iso(48), [100, 99, 98, 97]))
    r = _rec(_detail())
    assert mod.grade_record(r, _DSL, _CFG, {}) is True
    assert r["admittable_if_enabled"] is True
    assert r["outcome"] == "sim_winner" and r["pnl_pct"] > 0


def test_short_sim_loser_when_price_rises(monkeypatch):
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: _candles(_iso(48),
                                                 [100, 100.5, 100.9, 101.5, 102]))
    r = _rec(_detail())
    assert mod.grade_record(r, _DSL, _CFG, {}) is True
    assert r["outcome"] == "sim_loser" and r["pnl_pct"] < 0


def test_real_close_short_flips_sign():
    # a real close with price ROSE +1.2% is a loss for a short
    sig_ms = datetime.now(timezone.utc).timestamp() * 1000 - 48 * 3600_000
    closes = {"AAA": [{"closed_at": sig_ms, "side": "short",
                       "spot_pct": 1.2}]}
    r = _rec(_detail())
    assert mod.grade_record(r, _DSL, _CFG, closes) is True
    assert r["outcome"] == "real_loser" and r["pnl_pct"] < 0


def test_fetch_error_pending(monkeypatch):
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = _rec(_detail())
    assert mod.grade_record(r, _DSL, _CFG, {}) is False
    assert r.get("outcome") is None and "sim_error" in r


def test_tier_split_and_stats(monkeypatch, capsys):
    win = _candles(_iso(48), [100, 98, 97, 96, 95])
    lose = _candles(_iso(48), [100, 100.5, 101, 102, 103])
    def fetch(coin, interval, n):
        return win if coin == "AAA" else lose
    monkeypatch.setattr(mod, "fetch_hl_candles", fetch)
    rows = [_rec(_detail(conf=0.71, downtrend=True)),                    # A win
            {**_rec(_detail(conf=0.60, downtrend=True)), "coin": "BBB"}]  # B
    rows[1]["detail"]["entry_px"] = 100.0
    for r in rows:
        assert mod.grade_record(r, _DSL, _CFG, {}) is True
    useful = [r for r in rows if "pnl_pct" in r]
    a = [r for r in useful if r["admittable_if_enabled"]]
    b = [r for r in useful if not r["admittable_if_enabled"]]
    assert len(a) == 1 and a[0]["pnl_pct"] > 0
    assert len(b) == 1 and b[0]["pnl_pct"] < 0
    mod._summary(rows)
    out = capsys.readouterr().out
    assert "Tier A admittable" in out and "Tier B still blocked" in out
