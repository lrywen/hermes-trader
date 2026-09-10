"""Offline tests for reconcile_risk_tuning_shadow (no network).

Covers: candle simulation grading of block proposals (winner vs loser),
real-close join + leverage comparison, and stop_tuning wider-cap sim, with
fetch_hl_candles and the memory file stubbed.
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
    "reconcile_risk_tuning",
    _REPO / "scripts" / "reconcile_risk_tuning_shadow.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["reconcile_risk_tuning"] = mod
_spec.loader.exec_module(mod)


class _C:
    def __init__(self, t, o, h, l, c):
        self.t, self.o, self.h, self.l, self.c = t, o, h, l, c


def _iso(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _candles(entry_ts_iso: str, prices):
    """Build 1h candles; entry falls on the first bar at/after entry_ts."""
    t0 = datetime.fromisoformat(entry_ts_iso.replace("Z", "+00:00"))
    base = int(t0.timestamp() * 1000) - 3600_000
    out = []
    px = prices[0]
    for i, p in enumerate(prices):
        out.append(_C(base + i * 3600_000, px, max(px, p), min(px, p), p))
        px = p
    return out


_DSL = {"max_loss_pct": 1.0, "protect_pct": 2.5, "retrace_threshold": 0.5}


def test_block_proposal_graded_as_loser(monkeypatch):
    entry = 100.0
    # price immediately drops through the 1% stop → counter-factual loss
    cands = _candles(_iso(48), [100, 99.5, 98.5, 97])
    monkeypatch.setattr(mod, "fetch_hl_candles",
                        lambda *a, **k: cands)
    r = {"timestamp": _iso(48), "rule": "breakout_score_floor",
         "coin": "AAA", "side": "long",
         "detail": {"entry_px": entry}, "outcome": None}
    ok = mod.grade_record(r, _DSL, {})
    assert ok is True
    assert r["outcome"] == "blocked_a_loser"
    assert r["pnl_pct"] < 0


def test_block_proposal_graded_as_winner(monkeypatch):
    entry = 100.0
    # climbs to +4% then holds above trailing → counter-factual gain
    cands = _candles(_iso(48), [100, 101, 102, 103, 104, 103.9, 103.8])
    monkeypatch.setattr(mod, "fetch_hl_candles", lambda *a, **k: cands)
    r = {"timestamp": _iso(48), "rule": "per_coin_cooldown",
         "coin": "BBB", "side": "long",
         "detail": {"entry_px": entry}, "outcome": None}
    ok = mod.grade_record(r, _DSL, {})
    assert ok is True
    assert r["outcome"] == "blocked_a_winner"
    assert r["pnl_pct"] > 0


def test_leverage_tier_real_close_comparison(tmp_path, monkeypatch):
    sig_iso = _iso(48)
    sig_ms = int(datetime.fromisoformat(
        sig_iso.replace("Z", "+00:00")).timestamp() * 1000)
    # real close -0.83% spot within join window
    mem = {"closes": [{"coin": "ZEC", "closed_at": sig_ms + 3600_000,
                       "spot_pct": -0.83, "realized_pnl_usd": -0.22,
                       "leverage": 10}]}
    mf = tmp_path / "mem.json"
    mf.write_text(json.dumps(mem))
    monkeypatch.setattr(mod, "MEMORY_FILE", str(mf))
    closes = mod._load_real_closes()

    r = {"timestamp": sig_iso, "rule": "leverage_tier", "coin": "ZEC",
         "side": "long",
         "detail": {"live_leverage": 10, "proposed_leverage": 5,
                    "entry_px": 1252.4}, "outcome": None}
    ok = mod.grade_record(r, _DSL, closes)
    assert ok is True
    assert r["outcome"] == "deleverage_avoids_loss"
    # 5x must show a smaller absolute loss than 10x
    assert abs(r["proposed_pnl_pct"]) < abs(r["live_pnl_pct"])
    assert r["live_pnl_pct"] < 0


def test_stop_tuning_wider_cap_no_network_flags(tmp_path, monkeypatch):
    # With no adverse bars before a recovery, wider cap turns green vs the
    # tight live cap that stops out.
    sig = _iso(48)
    # live cap 0.8%: low of bar1 = 99.0 => -1.0% stops the live sim;
    # wider 1.5% cap survives; price then rallies to +3%.
    cands = []
    t0 = int(datetime.fromisoformat(sig.replace("Z", "+00:00")).timestamp()*1000)
    series = [(100, 100), (99.0, 100), (101, 101), (102.5, 102.5), (103, 103)]
    for i, (lo, c) in enumerate(series):
        cands.append(_C(t0 + i*3600_000, c, c, lo, c))
    monkeypatch.setattr(mod, "fetch_hl_candles", lambda *a, **k: cands)

    cfg = {"max_loss_pct": 0.8, "protect_pct": 2.5, "retrace_threshold": 0.5}
    r = {"timestamp": sig, "rule": "stop_tuning", "coin": "ADA",
         "side": "long", "would": "survive_wider_cap",
         "detail": {"entry_px": 100.0, "live_spot_cap_pct": 0.8,
                    "candidate_max_loss_pct": 1.5}, "outcome": None}
    ok = mod.grade_record(r, cfg, {})
    assert ok is True
    assert "wider_cap_sim" in r
    assert r["wider_cap_sim"]["pnl_pct"] > r["live_cap_sim"]["pnl_pct"]
