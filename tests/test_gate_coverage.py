"""P1.5 gate_coverage: live/replay coverage aggregation against the 500-row gate."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gate_coverage as gc  # noqa: E402

DAY_MS = 86_400_000


def _row(day, passes40=50.0, admitted=False, side="long", rule="runner_gate_blocked",
         replay=False):
    r = {"ts": day * DAY_MS + 1_000_000, "passes40": passes40,
         "passes45": False, "composite_score": passes40,
         "admitted": admitted, "side": side, "rule": rule}
    if replay:
        r["replay"] = True
    return r


def test_summarize_counts_gradable_rows_and_days():
    rows = [
        _row(20000), _row(20000, admitted=True, rule=""),
        _row(20001), _row(20002, replay=True),
        {"ts": 20002 * DAY_MS, "passes40": None},   # pre-dual-counter row
        {"passes40": 50.0, "ts": None},             # undated verdict
    ]
    out = gc.summarize(rows, "x.jsonl")
    assert out["rows"] == 6
    assert out["gradable_verdicts"] == 5
    assert out["rows_without_dual_counter"] == 1
    assert out["rows_missing_ts"] == 1
    assert out["utc_days"] == 3
    assert out["first_day"] == gc._utc_day(20000 * DAY_MS)
    assert out["last_day"] == gc._utc_day(20002 * DAY_MS)
    assert out["admitted"] == 1
    assert out["replay_tagged_rows"] == 1
    assert out["top_rules"]["runner_gate_blocked"] == 3
    # empty rule on a gradable row is bucketed as "(admitted)"
    assert out["top_rules"]["(admitted)"] == 2


def test_combine_reports_deficit_and_presence():
    live = {"file": "live", "exists": True, "gradable_verdicts": 29, "utc_days": 1}
    replay = {"file": "replay", "exists": True, "gradable_verdicts": 120, "utc_days": 20}
    out = gc.combine(live, replay, min_rows=500)
    assert out["gradable_verdicts"] == 149
    assert out["deficit"] == 351
    assert out["meets_min_rows"] is False
    assert out["utc_days_min"] == 20
    assert out["sources_present"] == ["live", "replay"]


def test_evaluate_handles_missing_files(tmp_path):
    rep = gc.evaluate(str(tmp_path / "nope.jsonl"), str(tmp_path / "nope2.jsonl"))
    assert rep["live"]["exists"] is False
    assert rep["replay"]["exists"] is False
    assert rep["combined"]["gradable_verdicts"] == 0
    assert rep["combined"]["deficit"] == 500
    assert rep["combined"]["sources_present"] == []


def test_evaluate_meets_gate_end_to_end(tmp_path):
    live = tmp_path / "live.jsonl"
    with open(live, "w", encoding="utf-8") as fh:
        for i in range(600):
            fh.write(json.dumps(_row(20000 + i % 30)) + "\n")
    rep = gc.evaluate(str(live), str(tmp_path / "missing.jsonl"), min_rows=500)
    assert rep["live"]["gradable_verdicts"] == 600
    assert rep["combined"]["meets_min_rows"] is True
    assert rep["combined"]["deficit"] == 0
