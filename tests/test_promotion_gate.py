"""Unit tests for the M2 six-rule promotion gate (deterministic, fail-closed)."""
from __future__ import annotations

import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "promotion_gate", os.path.join(os.path.dirname(__file__), "..",
                                   "scripts", "promotion_gate.py"))
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


def _full_pass(**kw):
    e = {
        "arm": "X",
        "kept": {"n": 500, "days": 60, "mean_pct": 0.30,
                 "ci95_pct": [0.10, 0.50]},
        "kept_minus_blocked_pct": 0.20,
        "cost9_kept_pct": 0.40, "cost19_kept_pct": 0.30,
        "max_coin_share_pct": 10.0, "max_day_share_pct": 5.0,
        "real_verdict": {"n": 80, "agree": True, "same_direction": True},
    }
    e.update(kw)
    return e


def test_all_six_pass_promotes():
    r = pg.evaluate(_full_pass())
    assert r["verdict"] == "PROMOTE_CANDIDATE"
    assert r["passed"] == r["of"] == 6


def test_missing_real_verdict_fails_closed():
    # R1 today: strong offline structural case but insufficient REAL shadow.
    e = _full_pass(real_verdict={"n": 0, "agree": False})
    r = pg.evaluate(e)
    assert r["verdict"] == "HOLD_SHADOW"
    assert r["checks"]["C6_real_confirm"]["pass"] is False


def test_ci_straddling_zero_fails_c2():
    e = _full_pass(kept={"n": 500, "days": 60, "mean_pct": 0.20,
                         "ci95_pct": [-0.05, 0.45]})
    r = pg.evaluate(e)
    assert r["checks"]["C2_positive_edge"]["pass"] is False
    assert r["verdict"] == "HOLD_SHADOW"


def test_small_sample_fails_c1():
    e = _full_pass(kept={"n": 50, "days": 20, "mean_pct": 0.5,
                         "ci95_pct": [0.1, 0.9]})
    assert pg.evaluate(e)["checks"]["C1_sample"]["pass"] is False


def test_not_beating_baseline_fails_c3():
    e = _full_pass(kept_minus_blocked_pct=0.0)
    assert pg.evaluate(e)["checks"]["C3_beats_blocked"]["pass"] is False


def test_negative_under_19bp_fails_c4():
    e = _full_pass(cost19_kept_pct=-0.05)
    assert pg.evaluate(e)["checks"]["C4_cost_robust"]["pass"] is False


def test_concentration_fails_c5():
    e = _full_pass(max_coin_share_pct=40.0)
    assert pg.evaluate(e)["checks"]["C5_not_concentrated"]["pass"] is False
