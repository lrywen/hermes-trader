"""Veto-path tests for the M2 promotion gate (kind="veto")."""
import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "promotion_gate",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "promotion_gate.py"))
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)


def _veto_pass():
    return {
        "arm": "R1_remove_daily_mover", "kind": "veto",
        "blocked": {"n": 325, "days": 88, "mean_pct": -1.14, "ci95_pct": [-2.642, -0.65]},
        "blocked_gross_pct": -0.95,
        "kept_minus_blocked_pct": 1.277,
        "max_coin_share_pct": 12.0, "max_day_share_pct": 8.0,
        "real_verdict": {"n": 60, "agree": True, "same_direction": True},
    }


def test_veto_full_pass_promotes():
    r = pg.evaluate(_veto_pass())
    assert r["verdict"] == "PROMOTE_VETO_CANDIDATE"
    assert r["passed"] == 6


def test_veto_missing_real_fails_closed():
    e = _veto_pass()
    e["real_verdict"] = {"n": 0, "agree": False, "same_direction": False}
    r = pg.evaluate(e)
    assert r["verdict"] == "HOLD_SHADOW"
    assert r["checks"]["V6_real_confirm"]["pass"] is False
    assert r["passed"] == 5


def test_veto_blocked_not_negative_fails_v2():
    e = _veto_pass()
    e["blocked"] = {"n": 325, "days": 88, "mean_pct": -0.2, "ci95_pct": [-0.6, 0.1]}
    r = pg.evaluate(e)
    assert r["checks"]["V2_blocked_negative"]["pass"] is False
    assert r["verdict"] == "HOLD_SHADOW"


def test_veto_gross_positive_fails_v4():
    e = _veto_pass()
    e["blocked_gross_pct"] = 0.1  # only loses because of fees -> not a bad-selection veto
    r = pg.evaluate(e)
    assert r["checks"]["V4_bad_even_gross"]["pass"] is False


def test_veto_small_sample_fails_v1():
    e = _veto_pass()
    e["blocked"] = {"n": 40, "days": 88, "mean_pct": -1.14, "ci95_pct": [-2.6, -0.6]}
    r = pg.evaluate(e)
    assert r["checks"]["V1_sample"]["pass"] is False


def test_veto_no_improvement_fails_v3():
    e = _veto_pass()
    e["kept_minus_blocked_pct"] = 0.01
    r = pg.evaluate(e)
    assert r["checks"]["V3_removal_improves"]["pass"] is False


def test_edge_path_unchanged_default():
    # default kind (no "kind" key) uses the edge evaluator
    e = {"arm": "x", "kept": {"n": 100, "days": 50, "mean_pct": 0.5, "ci95_pct": [0.1, 0.9]},
         "kept_minus_blocked_pct": 0.1, "cost9_kept_pct": 0.4, "cost19_kept_pct": 0.3,
         "max_coin_share_pct": 5, "max_day_share_pct": 5,
         "real_verdict": {"n": 60, "agree": True, "same_direction": True}}
    r = pg.evaluate(e)
    assert r["kind"] == "edge"
    assert r["verdict"] == "PROMOTE_CANDIDATE"
