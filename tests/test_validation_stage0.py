"""Stage 0 research-validation framework tests (full-review P0, 2026-10-25).

Pins the three frozen iron-laws and the append-only trial ledger:
  1. fixed_effect_ic rejects a coin-membership/scale factor (within/all < 0.3
     or sign flip) — the total_depth_log failure mode.
  2. partial_rank_ic flags a feature absorbed by known controls (beta).
  3. tradability_verdict rejects opposite-sign N=1 vs N=5 portfolios — the
     spread_bps "significant IC but not tradable" failure mode.

Pure standard library, no network / no production data.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hermes_trader.validation import (
    Trial,
    TrialLedger,
    bonferroni,
    fixed_effect_ic,
    ic_by_volatility_group,
    partial_rank_ic,
    simulate_size,
    simulate_sizes,
    spearman_ic,
    tradability_verdict,
)
from hermes_trader.validation.portfolio_sim import PortfolioResult


# ── iron-law 1: fixed-effect / scale-factor rejection ──────────────────────

def test_perfect_monotone_ic_is_one():
    assert spearman_ic([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman_ic([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_scale_factor_rejected_when_within_flips_sign():
    # Feature is essentially a per-coin depth tier (tiny within-coin wobble so
    # the within IC is estimable); pooled IC is strong only because big coins
    # outperformed over the window. Within-coin it has no predictive power.
    rows = []
    coins = ["ETH", "BTC", "SOL", "DOGE"]
    tier = {"ETH": 8.0, "BTC": 7.0, "SOL": 3.0, "DOGE": 1.0}
    beta = {"ETH": 0.004, "BTC": 0.003, "SOL": 0.0, "DOGE": -0.001}
    for d in range(40):
        for c in coins:
            rows.append({
                "day": d, "coin": c,
                "depth": tier[c] + (((d * 7 + len(c)) % 3) - 1) * 1e-6,
                "ret": beta[c] + (((d * 7 + len(c)) % 5) - 2) * 0.001,
            })
    r = fixed_effect_ic(rows, "depth", "ret")
    assert r.ic_all > 0.3                       # looks great pooled
    assert r.is_scale_factor is True           # but rejected
    assert abs(r.ic_within) < 0.2              # no within-coin power


def test_genuine_within_coin_edge_passes_fixed_effect():
    # Feature predicts THIS coin's next return relative to its own mean.
    rows = []
    for ci, c in enumerate(["A", "B", "C", "D"]):
        base = ci * 5  # arbitrary coin-level offset that must be removed
        for d in range(60):
            sig = math.sin(d / 3.0 + ci)
            rows.append({"day": d, "coin": c,
                         "f": base + sig, "ret": sig * 0.002})
    r = fixed_effect_ic(rows, "f", "ret")
    assert r.is_scale_factor is False
    assert r.ic_within > 0.5


# ── iron-law 2: partial / incremental IC ───────────────────────────────────

def test_partial_ic_absorbed_by_control():
    # ret is driven entirely by `beta`; f is just a noisy copy of beta.
    rows = []
    for i in range(200):
        b = (i % 7) * 0.01
        f = b + ((i % 3) - 1) * 1e-6
        rows.append({"coin": "X", "f": f, "beta": b,
                     "ret": b + ((i % 5) - 2) * 1e-5})
    r = partial_rank_ic(rows, "f", "ret", control_keys=["beta"])
    assert r.estimable is True
    assert r.absorbed is True
    assert abs(r.ic_partial) < abs(r.ic_marginal)


def test_partial_ic_survives_independent_information():
    rows = []
    for i in range(300):
        ctrl = (i % 11) * 0.005
        independent = math.sin(i / 5.0)
        rows.append({"coin": "X", "f": independent, "ctrl": ctrl,
                     "ret": independent * 0.01 + ctrl})
    r = partial_rank_ic(rows, "f", "ret", control_keys=["ctrl"])
    assert r.estimable is True
    assert r.absorbed is False
    assert r.ic_partial > 0.4


def test_ic_by_volatility_group_buckets():
    rows = [{"coin": "A", "v": float(i), "f": float(i), "r": float(i)}
            for i in range(120)]
    g = ic_by_volatility_group(rows, "f", "r", "v", n_groups=3, min_per_group=20)
    assert set(g.groups) == {"low", "mid", "high"}
    assert g.consistent is True  # all strongly positive


def test_ic_by_volatility_group_raises_without_coin_identity():
    # Regression guard for the residual pooling leak: rows with no coin key
    # (or all coin=None) must fail fast instead of collapsing to one group and
    # returning pooled IC=1.0 / consistent=True for a scale factor.
    no_coin = [{"v": float(i), "f": float(i), "r": float(i)}
               for i in range(30)]
    with pytest.raises(ValueError, match="coin"):
        ic_by_volatility_group(no_coin, "f", "r", "v", min_per_group=5)
    all_none = [{"coin": None, "v": float(i), "f": float(i), "r": float(i)}
                for i in range(30)]
    with pytest.raises(ValueError, match="coin"):
        ic_by_volatility_group(all_none, "f", "r", "v", min_per_group=5)
    # PARTIAL missing (not all-None) must also raise: those rows pool into a
    # synthetic coin=None group whose mean is just the pooled mean, re-opening
    # the scale-factor leak. The guard is per-row, not "all missing".
    partial = (
        [{"coin": "A", "v": float(i), "f": float(i), "r": float(i)}
         for i in range(30)]
        + [{"coin": None, "v": float(i), "f": float(i), "r": float(i)}
           for i in range(30)]
    )
    with pytest.raises(ValueError, match="EVERY"):
        ic_by_volatility_group(partial, "f", "r", "v", min_per_group=5)
    # A single explicitly-named coin is allowed (degenerate but intentional).
    one_coin = [{"coin": "A", "v": float(i), "f": float(i), "r": float(i)}
                for i in range(30)]
    g = ic_by_volatility_group(one_coin, "f", "r", "v", min_per_group=5)
    assert g.n == 30


def test_ic_by_volatility_group_rejects_pure_coin_scale_factor():
    # Iron-law #2 regression guard: a pure per-coin level/membership factor
    # (feature and return only differ BETWEEN coins, ~zero within-coin) must
    # NOT be judged "consistent" after within-coin demeaning. The old code
    # pooled raw values and returned IC ~= +0.7 in every bucket.
    import random
    rnd = random.Random(0)
    levels = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 5.0}
    vols = {"A": 0.5, "B": 1.0, "C": 1.5, "D": 2.0, "E": 2.5}
    rows = []
    for coin, lvl in levels.items():
        for _ in range(90):  # enough per bucket (>=20)
            rows.append({"coin": coin,
                         "f": lvl + rnd.gauss(0.0, 1e-9),
                         "r": lvl + rnd.gauss(0.0, 1e-9),
                         "v": vols[coin]})
    g = ic_by_volatility_group(rows, "f", "r", "v", n_groups=3, min_per_group=20)
    # Within-coin variation is pure noise: no bucket keeps a meaningful IC.
    assert all(abs(ic) < 0.2 for ic in g.groups.values())
    assert g.consistent is False


def test_ic_by_volatility_group_keeps_genuine_within_coin_edge():
    # A real within-coin edge (feature up -> forward return up, inside each
    # coin, regardless of the coin's level) must still pass through.
    import random
    rnd = random.Random(1)
    levels = {"A": 1.0, "B": 4.0, "C": 2.0, "D": 5.0, "E": 3.0}
    vols = {"A": 0.5, "B": 1.0, "C": 1.5, "D": 2.0, "E": 2.5}
    rows = []
    for coin, lvl in levels.items():
        for _ in range(90):
            z = rnd.gauss(0.0, 1.0)
            rows.append({"coin": coin,
                         "f": lvl + z,
                         "r": lvl + 0.8 * z + rnd.gauss(0.0, 0.2),
                         "v": vols[coin]})
    g = ic_by_volatility_group(rows, "f", "r", "v", n_groups=3, min_per_group=20)
    assert all(ic > 0.3 for ic in g.groups.values())
    assert g.consistent is True


def test_ic_by_volatility_group_requires_magnitude_not_just_sign(monkeypatch):
    # Sign agreement alone is uninformative — three buckets share a sign by
    # chance ~25% under the null, often with |IC|~1e-3. Pin the per-bucket
    # Spearman to tiny same-sign values and assert the magnitude floor rejects
    # them at the default 0.10 but accepts them when BOTH floors are loosened
    # (significance_z=0 disables the sample-size-aware term too).
    from hermes_trader.validation import incremental as inc

    base_rows = [{"coin": "A", "v": float(i), "f": float(i), "r": float(i)}
                 for i in range(60)]

    def run(floor, z=1.96):
        vals = iter([0.0008, 0.0011, 0.0006])
        monkeypatch.setattr(inc, "spearman_ic", lambda f, y: next(vals))
        return ic_by_volatility_group(
            base_rows, "f", "r", "v", n_groups=3, min_per_group=5,
            min_abs_ic=floor, significance_z=z)

    assert run(0.10).consistent is False   # same sign but |IC| ~ 1e-3
    assert run(0.0, z=0.0).consistent is True  # sign-only gate would pass them


def test_vol_group_floor_scales_with_bucket_size(monkeypatch):
    # Regression guard for the fixed-0.10 small-bucket leak: a flat absolute
    # floor passes the null ~8% at n=20/bucket but ~0.5% at n=130 because null
    # |rs| noise scales as 1/sqrt(n). The per-bucket floor must therefore be
    # stricter for small buckets and must be reported for audit.
    from hermes_trader.validation import incremental as inc

    def thresholds_for(n):
        rows = [{"coin": "A", "v": float(i), "f": float(i), "r": float(i)}
                for i in range(3 * n)]
        g = ic_by_volatility_group(rows, "f", "r", "v", n_groups=3,
                                   min_per_group=5, min_abs_ic=0.0,
                                   significance_z=1.96)
        return g.thresholds

    small = thresholds_for(20)
    large = thresholds_for(133)
    assert set(small) == {"low", "mid", "high"}
    assert small["low"] > large["low"]          # stricter when n is small
    assert small["low"] == pytest.approx(0.4665, abs=0.01)
    assert large["low"] == pytest.approx(0.1813, abs=0.01)
    # min_abs_ic stays a lower bound at large n (large-sample trivial IC).
    g_large = ic_by_volatility_group(
        [{"coin": "A", "v": float(i), "f": float(i), "r": float(i)}
         for i in range(3 * 133)],
        "f", "r", "v", n_groups=3, min_per_group=5,
        min_abs_ic=0.25, significance_z=1.96)
    assert g_large.thresholds["low"] == pytest.approx(0.25, abs=1e-9)


def test_vol_group_adaptive_floor_rejects_small_bucket_null(monkeypatch):
    # A same-sign |IC|~0.30 across buckets would clear a flat 0.10 but is well
    # inside the null noise at n=20/bucket (~SE 0.25); the adaptive floor must
    # reject it there. The SAME |IC| at n=133 (SE~0.09) clears that bucket's
    # floor and is accepted.
    from hermes_trader.validation import incremental as inc

    def group_with(n, ic):
        rows = [{"coin": "A", "v": float(i), "f": 0.0, "r": 0.0}
                for i in range(3 * n)]
        vals = iter([ic, ic, ic])
        monkeypatch.setattr(inc, "spearman_ic", lambda f, y: next(vals))
        return ic_by_volatility_group(rows, "f", "r", "v", n_groups=3,
                                      min_per_group=5)

    # |IC|=0.30: below the small-bucket adaptive floor (~0.47) -> not
    # consistent; above the large-bucket floor (~0.18) -> consistent.
    assert group_with(20, 0.30).consistent is False
    assert group_with(133, 0.30).consistent is True


def test_fixed_effect_ic_raises_on_missing_coin():
    # 3.1: the fixed-effect gate must fail fast with ValueError (CLI maps this
    # to rc=3), not a bare KeyError from r[coin_key].
    good = [{"coin": "A", "f": 1.0, "r": 0.1},
            {"coin": "B", "f": 2.0, "r": 0.2},
            {"coin": "A", "f": 1.5, "r": 0.15},
            {"coin": "B", "f": 2.5, "r": 0.25}]
    fixed_effect_ic(good, "f", "r")  # does not raise
    bad = [dict(r, coin=None) if i == 1 else r for i, r in enumerate(good)]
    with pytest.raises(ValueError, match="EVERY"):
        fixed_effect_ic(bad, "f", "r")
    missing_key = [{"f": 1.0, "r": 0.1}, {"f": 2.0, "r": 0.2},
                   {"f": 1.5, "r": 0.15}, {"f": 2.5, "r": 0.25}]
    with pytest.raises(ValueError, match="EVERY"):
        fixed_effect_ic(missing_key, "f", "r")


# ── iron-law 3: cross-sectional portfolio tradability ─────────────────────

def _res(n, gross, net, conc=0.2):
    return PortfolioResult(N=n, n_entries=10, n_days=10, gross_bps=gross,
                           cost_bps=9.47, net_bps=net, net_ci_low=net - 5,
                           net_ci_high=net + 5, p_value=0.01,
                           top1_concentration=conc)


def test_tradability_rejects_opposite_sign_n1_vs_n5():
    ok, why = tradability_verdict({1: _res(1, 2.0, -7.47), 5: _res(5, 11.2, 1.73)})
    assert ok is False and "opposite" in why


def test_tradability_requires_positive_top1_after_cost():
    ok, _ = tradability_verdict({1: _res(1, 2.0, -7.47), 5: _res(5, 2.0, -7.47)})
    assert ok is False


def test_tradability_passes_agreeing_positive():
    ok, _ = tradability_verdict({1: _res(1, 20.0, 10.5), 5: _res(5, 15.0, 5.5)})
    assert ok is True


def test_simulate_size_p_value_discriminates_signal_from_null():
    # N1: p_value is a one-sided day-block bootstrap p for mean net > 0, from
    # the SAME resamples as the CI. Strongly positive daily nets -> tiny p
    # (>= detection floor 1/(B+1)); symmetric zero-mean nets -> large p.
    from hermes_trader.validation.portfolio_sim import simulate_size

    pos = [{"day": f"d{d:02d}", "coin": f"C{c}", "score": 1.0,
            "ret": 0.005} for d in range(30) for c in "ABC"]
    rp = simulate_size(pos, 1, "score", "ret", cost_bps=0.0, B=1000)
    assert 0.0 < rp.p_value <= 1.0 / 1001 + 1e-12
    assert rp.p_value < 0.05

    # Deterministically symmetric zero-mean daily nets around 0 -> resampled
    # mean is 0 -> every replicate counts as <= 0 -> p at/near 1.
    zero = []
    for d in range(20):
        zero.append({"day": f"d{d:02d}", "coin": "A", "score": 1.0,
                     "ret": 0.001 if d % 2 == 0 else -0.001})
    rz = simulate_size(zero, 1, "score", "ret", cost_bps=0.0, B=500)
    assert rz.p_value > 0.5


def test_simulate_sizes_one_signal_per_coin_per_day_and_cost():
    # Exactly one row per (day, coin); cost is charged round-trip.
    rows = []
    for d in range(20):
        for c in range(6):
            rows.append({"day": d, "coin": f"C{c}",
                         "score": float(c), "ret": 0.002})
    res = simulate_sizes(rows, "score", "ret", sizes=(1, 5),
                         cost_bps=9.47, B=200)
    # N=1 -> 20 entries (one per day); N=5 -> 100.
    assert res[1].n_entries == 20
    assert res[5].n_entries == 100
    assert res[1].net_bps == pytest.approx(0.002 * 1e4 - 9.47)


def test_simulate_size_fails_fast_on_duplicate_day_coin():
    # Iron-law #3 regression guard: a duplicate (day, coin) must raise rather
    # than silently keep whichever row happened to be first in input order.
    rows = [
        {"day": 0, "coin": "C0", "score": 1.0, "ret": 0.001},
        {"day": 0, "coin": "C1", "score": 0.5, "ret": 0.001},
    ]
    # Unique rows are fine.
    simulate_size(rows, N=2, feature_key="score", ret_key="ret", B=50)
    # Adding a conflicting row for an existing pair must fail fast.
    rows.append({"day": 0, "coin": "C0", "score": 9.0, "ret": -0.9})
    with pytest.raises(ValueError, match="duplicate observation"):
        simulate_size(rows, N=2, feature_key="score", ret_key="ret", B=50)


def test_simulate_size_is_order_independent():
    # Same data, different row order must give identical results. Build one
    # unique row per (day, coin) with varied scores/returns, then shuffle.
    import random
    rows = []
    rnd = random.Random(7)
    for d in range(20):
        for c in range(6):
            rows.append({"day": d, "coin": f"C{c}",
                         "score": rnd.random(), "ret": rnd.gauss(0.0, 0.002)})
    base = simulate_size(rows, N=3, feature_key="score", ret_key="ret",
                         cost_bps=9.47, B=500, seed=42)
    for s in range(5):
        shuffled = rows[:]
        random.Random(s).shuffle(shuffled)
        r = simulate_size(shuffled, N=3, feature_key="score", ret_key="ret",
                          cost_bps=9.47, B=500, seed=42)
        assert r.gross_bps == pytest.approx(base.gross_bps)
        assert r.net_bps == pytest.approx(base.net_bps)
        assert r.n_entries == base.n_entries


# ── trial ledger ───────────────────────────────────────────────────────────

def test_bonferroni():
    assert bonferroni(0.01, 20) == pytest.approx(0.2)
    assert bonferroni(0.1, 20) == 1.0
    assert math.isnan(bonferroni(float("nan"), 5))


def test_ledger_append_only_and_family_multiplicity(tmp_path):
    path = str(tmp_path / "trials.jsonl")
    led = TrialLedger(path)
    for i in range(3):
        led.append(Trial(hypothesis=f"H{i}", caliber="c", n=100,
                         net_bps=1.0, p_raw=0.01, family="depth"))
    recs = [json.loads(l) for l in open(path)]
    assert [r["trial_id"] for r in recs] == ["T0001", "T0002", "T0003"]
    # family size captured at append time -> Bonferroni tightens over time
    assert [r["family_size_at_test"] for r in recs] == [1, 2, 3]
    assert recs[-1]["p_bonferroni"] == pytest.approx(0.03)
    # rewrite protection: appending again adds a line, never mutates prior
    led.append(Trial(hypothesis="H3", caliber="c", n=100, net_bps=1.0,
                     p_raw=0.01, family="depth"))
    assert sum(1 for _ in open(path)) == 4


# ── Stage 0 process glue: scripts/stage0_audit.py end-to-end ──────────────

def _write_dataset(path, *, edge: bool, days: int = 30, seed: int = 5):
    import random
    rnd = random.Random(seed)
    with open(path, "w", encoding="utf-8") as fh:
        for d in range(days):
            for ci, coin in enumerate("ABCDEFGH"):
                z = rnd.gauss(0, 1)
                ret = (0.004 * z if edge else 0.0) + rnd.gauss(0, 0.004)
                fh.write(json.dumps({
                    "day": f"2026-09-{d % 28 + 1:02d}", "coin": coin,
                    "feat": z + ci, "ret": ret + ci * 0.0002,
                    "vol": 1 + ci * 0.3 + rnd.gauss(0, 0.1)}) + "\n")


def test_stage0_audit_cli_edge_clears_null_rejects_and_logs(tmp_path):
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "stage0_audit.py"
    edge = tmp_path / "edge.jsonl"
    null = tmp_path / "null.jsonl"
    ledger = tmp_path / "trials.jsonl"
    _write_dataset(edge, edge=True)
    _write_dataset(null, edge=False)

    def run(src, hyp):
        return subprocess.run(
            [sys.executable, str(script), "--input", str(src),
             "--hypothesis", hyp, "--feature", "feat", "--ret", "ret",
             "--vol", "vol", "--ledger", str(ledger)],
            capture_output=True, text=True, cwd=str(repo))

    r1 = run(edge, "edge H1")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert json.loads(r1.stdout)["verdict"] == "reject_h0"
    r2 = run(null, "null H2")
    assert r2.returncode == 1, r2.stdout + r2.stderr
    assert json.loads(r2.stdout)["verdict"] == "fail_reject"

    # The process glue is the first real ledger writer: family m increments.
    recs = [json.loads(l) for l in open(ledger)]
    assert [r["trial_id"] for r in recs] == ["T0001", "T0002"]
    assert [r["family_size_at_test"] for r in recs] == [1, 2]

    # N1: the Bonferroni p is wired (no longer the always-NaN placeholder).
    # The edge trial clears; its raw p and corrected p are finite and in (0,1].
    edge_rec = recs[0]
    assert edge_rec["p_raw"] == edge_rec["p_raw"]  # not NaN
    assert 0.0 < edge_rec["p_raw"] <= 1.0
    assert edge_rec["p_bonferroni"] == pytest.approx(
        min(1.0, edge_rec["family_size_at_test"] * edge_rec["p_raw"]))
    # N2: the data window is recorded (not the empty-string placeholder).
    assert edge_rec["day_split"].count("..") == 1
    assert edge_rec["day_split"].split("..")[0] == "2026-09-01"
    # N3: per-bucket vol-group IC + the thresholds actually applied survive in
    # the ledger even if stdout is discarded.
    vg = edge_rec["gates_summary"]["vol_group"]
    assert set(vg["ic"]) == {"low", "mid", "high"}
    assert set(vg["thresholds"]) == {"low", "mid", "high"}
    assert vg["consistent"] is True
    assert edge_rec["gates_summary"]["fixed_effect"]["is_scale_factor"] is False


def test_stage0_audit_cli_dry_run_does_not_write(tmp_path):
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "stage0_audit.py"
    src = tmp_path / "edge.jsonl"
    ledger = tmp_path / "trials.jsonl"
    _write_dataset(src, edge=True)
    r = subprocess.run(
        [sys.executable, str(script), "--input", str(src), "--hypothesis", "h",
         "--feature", "feat", "--ret", "ret", "--vol", "vol",
         "--ledger", str(ledger), "--dry-run"],
        capture_output=True, text=True, cwd=str(repo))
    assert r.returncode in (0, 1)
    assert not ledger.exists()


def test_stage0_audit_cli_missing_coin_is_rc3_and_writes_nothing(tmp_path):
    # 3.1: a dataset lacking coin identity must surface as a distinct
    # precondition error (rc=3), NOT a bare traceback rc=1, and must never
    # append a Trial (an un-runnable audit records no verdict).
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "stage0_audit.py"
    src = tmp_path / "nocoin.jsonl"
    ledger = tmp_path / "trials.jsonl"
    with open(src, "w", encoding="utf-8") as fh:
        for d in range(30):
            for ci in range(8):
                # Two named coins on most rows, but SOME rows omit the coin.
                # The >=2-coins prefilter passes, so the per-row gate guard —
                # not a generic "insufficient data" rc=2 — must fire rc=3.
                rec = {"day": f"2026-09-{d % 28 + 1:02d}",
                       "feat": 0.5 + ci * 0.01, "ret": 0.001, "vol": 1.0}
                if not (d == 0 and ci == 0):
                    rec["coin"] = "A" if ci % 2 == 0 else "B"
                fh.write(json.dumps(rec) + "\n")
    r = subprocess.run(
        [sys.executable, str(script), "--input", str(src), "--hypothesis", "h",
         "--feature", "feat", "--ret", "ret", "--vol", "vol",
         "--ledger", str(ledger)],
        capture_output=True, text=True, cwd=str(repo))
    assert r.returncode == 3, r.stdout + r.stderr
    payload = json.loads(r.stderr.strip().splitlines()[-1])
    assert payload["verdict"] == "invalid"
    assert "coin" in payload["error"]
    assert not ledger.exists()
