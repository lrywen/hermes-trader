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



def test_corrupt_line_does_not_drop_following_rows(tmp_path):
    """2026-11-02 incident: ONE corrupt line cost 75 rows.

    load_rows used a per-file try, so a JSONDecodeError on line 84 of
    runner_gate_shadow.jsonl.1 aborted the rest of that file. load_rows returned
    2,386 against 2,461 physical lines, the 98% guard refused to publish, and
    decision_shadow_outcomes.jsonl stayed frozen at 2,319 rows since 11-02
    00:18. A corrupt line must cost exactly itself.
    """
    good_head = [_src_row(i) for i in range(5)]
    good_tail = [_src_row(i) for i in range(5, 80)]
    p = tmp_path / "runner_gate_shadow.jsonl"
    with open(p, "w") as f:
        for r in good_head:
            f.write(json.dumps(r) + "\n")
        f.write('{"ts": 1, "coin": "BTC", "side": "long", "rule": "x"\n')  # torn
        for r in good_tail:
            f.write(json.dumps(r) + "\n")

    rows = rds.load_rows(str(p))
    assert len(rows) == 80                      # 5 + 75, the torn row alone lost
    assert len(rows) == rds.count_source_lines(str(p))


def test_main_survives_corrupt_line_and_writes_all_good_rows(tmp_path, monkeypatch):
    good = [_src_row(i) for i in range(60)]
    p = tmp_path / "runner_gate_shadow.jsonl"
    with open(p, "w") as f:
        for i, r in enumerate(good):
            if i == 10:
                f.write("{torn\n")
            f.write(json.dumps(r) + "\n")
    out = tmp_path / "decision_shadow_outcomes.jsonl"
    monkeypatch.setattr("sys.argv", [
        "reconcile", "--pattern", str(p), "--out", str(out),
        "--cache", str(tmp_path / "nocache.json"), "--no-network",
    ])
    assert rds.main() == 0
    written = [l for l in out.read_text().splitlines() if l.strip()]
    assert len(written) == 60

class _FakeSrc:
    """Minimal CandleSource: constant 5m-bar grid with a programmable price path."""

    def __init__(self, path):
        # path: coin -> list[float] of closes. Grid starts one bar before t0 so
        # a row at t0 aligns to index 1 (bisect_right(ts, t0-1)-1 == 0 would
        # otherwise reject it), leaving enough forward bars for every horizon.
        self.path = path
        self.t0 = 1_700_000_000_000

    def series(self, coin):
        if coin not in self.path:
            return None
        closes = self.path[coin]
        # start TWO bars before t0: index0 is the benchmark lead bar, index1
        # (t0-BAR) is the entry bar a t0 row aligns to.
        ts = [self.t0 + (i - 2) * rds.BAR_MS for i in range(len(closes))]
        return ts, closes


def _row(side, ts_idx=0, coin="BTC", mid=None):
    return {"ts": 1_700_000_000_000 + ts_idx * rds.BAR_MS, "coin": coin,
            "side": side, "rule": "ok", "admitted": False,
            "mid_price": mid}


def _path(up, n=902):
    # index0 benchmark lead, index1 entry close 100, index 1+12 forward = up.
    return [100.0, 100.0, 100.0, up] + [up] * (n - 4)


def test_p03_short_gross_sign_is_direction_aware():
    # Price rises 10% over the horizon: a LONG gains, a SHORT loses.
    src = _FakeSrc({"BTC": _path(110.0)})
    rows = [_row("long"), _row("short")]
    out = rds.enrich(rows, src, bench_coins=[])
    long_g, short_g = out[0]["gross_fwd"]["1h"], out[1]["gross_fwd"]["1h"]
    assert abs(long_g - 0.10) < 1e-9
    assert abs(short_g - (-0.10)) < 1e-9          # flipped, not zero/positive
    assert abs(long_g + short_g) < 1e-9            # long/short symmetric


def test_p03_short_profits_when_price_falls():
    src = _FakeSrc({"BTC": _path(90.0)})           # -10%
    out = rds.enrich([_row("short")], src, bench_coins=[])
    assert abs(out[0]["gross_fwd"]["1h"] - 0.10) < 1e-9


def test_p03_cost_charged_once_not_doubled_for_short():
    src = _FakeSrc({"BTC": _path(110.0)})
    out = rds.enrich([_row("short")], src, bench_coins=[])
    g = out[0]["gross_fwd"]["1h"]
    net = out[0]["net_bp9.26"]["1h"]
    # net = signed gross - one round-trip cost, never gross - 2*cost.
    assert abs(net - (g - rds.COSTS["bp9.26"])) < 1e-12


def test_p03_short_excess_flips_both_leg_and_benchmark():
    # Both coin and benchmark rise 10% -> market-neutral gross excess is 0 for
    # either side (sgn applied to both leg and bench, so they cancel); only the
    # one round-trip cost remains.
    src = _FakeSrc({"BTC": _path(110.0), "ETH": _path(110.0)})
    out = rds.enrich([_row("short")], src, bench_coins=["ETH"])
    assert abs(out[0]["excess_bp9.26"]["1h"] - (-rds.COSTS["bp9.26"])) < 1e-12


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
