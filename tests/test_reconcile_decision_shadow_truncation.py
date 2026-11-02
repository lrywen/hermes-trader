"""ADR-0012 / v2 §4.4: decision-shadow reconcile atomic write + truncation guard.

Regression for the 2026-11-01 incident: reconcile_decision_shadow.main rewrites
the whole derived outcomes file and races log rotation; a partial source read
silently shrank the output by 75/37 rows. Fix = atomic os.replace publish plus
a pre-write src>=98% guard that returns exit code 2 without touching a prior
good output.
"""
from __future__ import annotations

import json

import pytest

from scripts import reconcile_decision_shadow as rds


def _write_source(d, rows, name="runner_gate_shadow.jsonl"):
    p = d / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def _src_row(i):
    return {"ts": 1_700_000_000_000 + i * 300_000, "coin": "BTC",
            "side": "long", "rule": "ok", "admitted": False,
            "mid_price": None}


def test_main_atomic_writes_full_file_no_tmp_left(tmp_path, monkeypatch):
    rows = [_src_row(i) for i in range(20)]
    pat = str(_write_source(tmp_path, rows))
    out = tmp_path / "decision_shadow_outcomes.jsonl"
    # no-network + empty cache => every row is uncovered but still preserved 1:1.
    monkeypatch.setattr("sys.argv", [
        "reconcile", "--pattern", pat, "--out", str(out),
        "--cache", str(tmp_path / "nocache.json"), "--no-network",
    ])
    rc = rds.main()
    assert rc == 0
    written = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    assert len(written) == 20
    # atomic publish leaves no temp file behind
    assert not (tmp_path / "decision_shadow_outcomes.jsonl.tmp").exists()


def test_truncation_guard_returns_2_and_preserves_prior_output(tmp_path, monkeypatch):
    # A prior good output already exists on disk.
    out = tmp_path / "decision_shadow_outcomes.jsonl"
    out.write_text("PRIOR_GOOD_OUTPUT\n")

    rows = [_src_row(i) for i in range(100)]
    pat = str(_write_source(tmp_path, rows))

    # Real 2026-11-01 race: the physical source file is complete (100 lines),
    # but load_rows read a short mid-rotation snapshot and enriched only 1 row.
    # The guard must compare against PHYSICAL source lines, not len(load_rows).
    monkeypatch.setattr(rds, "load_rows", lambda pattern: [dict(rows[0])])
    monkeypatch.setattr("sys.argv", [
        "reconcile", "--pattern", pat, "--out", str(out),
        "--cache", str(tmp_path / "nocache.json"), "--no-network",
    ])
    rc = rds.main()
    assert rc == 2
    # the prior good file must be untouched
    assert out.read_text() == "PRIOR_GOOD_OUTPUT\n"
    assert not (tmp_path / "decision_shadow_outcomes.jsonl.tmp").exists()


def test_count_source_lines_counts_parseable_only(tmp_path):
    good = [_src_row(i) for i in range(10)]
    p = _write_source(tmp_path, good)
    # append a corrupt line (as observed: 1 bad json line in production)
    with open(p, "a") as f:
        f.write("{not valid json\n")
    assert rds.count_source_lines(str(p)) == 10


def test_max_rows_bypasses_truncation_guard(tmp_path, monkeypatch):
    # --max-rows intentionally shrinks output; guard must not block it.
    rows = [_src_row(i) for i in range(100)]
    pat = str(_write_source(tmp_path, rows))
    out = tmp_path / "decision_shadow_outcomes.jsonl"
    monkeypatch.setattr("sys.argv", [
        "reconcile", "--pattern", pat, "--out", str(out),
        "--cache", str(tmp_path / "nocache.json"), "--no-network",
        "--max-rows", "10",
    ])
    rc = rds.main()
    assert rc == 0
    written = [l for l in out.read_text().splitlines() if l.strip()]
    assert len(written) == 10
