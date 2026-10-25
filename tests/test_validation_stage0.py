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

import pytest

from hermes_trader.validation import (
    Trial,
    TrialLedger,
    bonferroni,
    fixed_effect_ic,
    ic_by_volatility_group,
    partial_rank_ic,
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
    rows = [{"v": float(i), "f": float(i), "r": float(i)}
            for i in range(120)]
    g = ic_by_volatility_group(rows, "f", "r", "v", n_groups=3, min_per_group=20)
    assert set(g.groups) == {"low", "mid", "high"}
    assert g.consistent is True  # all strongly positive


# ── iron-law 3: cross-sectional portfolio tradability ─────────────────────

def _res(n, gross, net, conc=0.2):
    return PortfolioResult(N=n, n_entries=10, n_days=10, gross_bps=gross,
                           cost_bps=9.47, net_bps=net, net_ci_low=net - 5,
                           net_ci_high=net + 5, top1_concentration=conc)


def test_tradability_rejects_opposite_sign_n1_vs_n5():
    ok, why = tradability_verdict({1: _res(1, 2.0, -7.47), 5: _res(5, 11.2, 1.73)})
    assert ok is False and "opposite" in why


def test_tradability_requires_positive_top1_after_cost():
    ok, _ = tradability_verdict({1: _res(1, 2.0, -7.47), 5: _res(5, 2.0, -7.47)})
    assert ok is False


def test_tradability_passes_agreeing_positive():
    ok, _ = tradability_verdict({1: _res(1, 20.0, 10.5), 5: _res(5, 15.0, 5.5)})
    assert ok is True


def test_simulate_sizes_one_signal_per_coin_per_day_and_cost():
    rows = []
    for d in range(20):
        for c in range(6):
            rows.append({"day": d, "coin": f"C{c}",
                         "score": float(c), "ret": 0.002})
        # duplicate row for same (day, coin) must be ignored
        rows.append({"day": d, "coin": "C0", "score": 99.0, "ret": 0.9})
    res = simulate_sizes(rows, "score", "ret", sizes=(1, 5),
                         cost_bps=9.47, B=200)
    # N=1 -> 20 entries (one per day); the spiky C0 duplicate never wins.
    assert res[1].n_entries == 20
    assert res[5].n_entries == 100
    assert res[1].net_bps == pytest.approx(0.002 * 1e4 - 9.47)


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
