# -*- coding: utf-8 -*-
"""统一验证 CLI（validate_outcome.py）单元测试。"""
from __future__ import annotations

import json

from scripts.validate_outcome import evaluate


def _write(path, days):
    with open(path, "w", encoding="utf-8") as fh:
        for day, pnl in days:
            fh.write(json.dumps({
                "type": "trade", "arm": "filt",
                "entry_t": day * 86_400_000, "notional": 10000.0,
                "pnl_net": pnl,
            }) + "\n")


def _ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    rec = {
        "ledger_version": 2, "trial_id": "filt-prospective",
        "family": "filt::daily_outcome",
        "arm": "filt", "paradigm": "daily_outcome", "iteration": 1,
        "test_at": "2025-01-02T00:00:00Z", "recorded_at": "2025-01-02T00:00:00Z",
        "preregistration": {
            "status": "preregistered", "registered_at": "2025-01-01T00:00:00Z",
            "prospective": True,
        },
        "source": "preregistered", "status": "active",
        "provenance": {"adr": "docs/adr/test.md"},
    }
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return str(path)


def test_evaluate_negative_series_gives_negative_verdict(tmp_path):
    p = tmp_path / "t.jsonl"
    # 60 天，收益围绕负值波动（有方差，使夏普<0）→ 三方法均为负
    _write(p, [(d, -5.0 - (d % 5) * 0.5) for d in range(60)])
    r = evaluate(str(p), ("filt",), boot=500, seed=1, n_trials=999,
                 as_of="2026-01-01T00:00:00Z", ledger_path=_ledger(tmp_path),
                 output_path=None)
    assert r["verdict"] == "NEGATIVE_EXPECTANCY"
    row = r["arms"][0]
    assert row["bb_positive"] is False
    assert row["sharpe"] < 0
    assert row["n_trials"] == 1


def test_evaluate_positive_series_possible_edge(tmp_path):
    p = tmp_path / "t.jsonl"
    # 强正收益序列：CI 下界为正、DSR 高
    _write(p, [(d, 5.0 + (d % 7) * 0.1) for d in range(120)])
    r = evaluate(str(p), ("filt",), boot=500, seed=1,
                 as_of="2026-01-01T00:00:00Z", ledger_path=_ledger(tmp_path),
                 output_path=None)
    assert r["verdict"] == "POSITIVE_EDGE_POSSIBLE"
    assert r["arms"][0]["bb_positive"] is True
    assert r["promotion_eligible"] is True


def test_evaluate_skips_short_series(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [(d, 1.0) for d in range(5)])  # < 12 天
    r = evaluate(str(p), ("filt",), boot=100, seed=1, n_trials=1)
    assert "skip" in r["arms"][0]
    assert r["verdict"] == "NEGATIVE_EXPECTANCY"