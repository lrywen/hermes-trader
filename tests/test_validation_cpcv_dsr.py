# -*- coding: utf-8 -*-
"""T-02 独立显著性检验（CPCV / DSR / PBO）单元测试。"""
from __future__ import annotations

import json
import math

import pytest

from hermes_trader.validation import (
    CPCVResult,
    cpcv_paths,
    day_bps_series,
    deflated_sharpe_prob,
    probability_of_backtest_overfitting,
    sharpe,
)
from hermes_trader.validation.significance import _norm_cdf, _norm_ppf


def _write_trades(path, rows, arm="filt", notional=10000.0):
    with open(path, "w", encoding="utf-8") as fh:
        for day, pnl in rows:
            t = day * 86_400_000
            fh.write(json.dumps({"type": "trade", "arm": arm,
                                 "entry_t": t, "notional": notional,
                                 "pnl_net": pnl}) + "\n")


def test_day_bps_series_aggregates_by_day(tmp_path):
    p = tmp_path / "t.jsonl"
    # 两天，每天两笔：bps = pnl/notional*1e4
    _write_trades(p, [(0, 10.0), (0, 20.0), (1, -10.0), (1, 30.0)])
    s = day_bps_series(str(p), "filt")
    assert s == pytest.approx([15.0, 10.0])


def test_day_bps_series_filters_arm_and_type(tmp_path):
    p = tmp_path / "t.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "trade", "arm": "other", "entry_t": 0,
                             "notional": 1, "pnl_net": 999}) + "\n")
        fh.write(json.dumps({"type": "signal", "arm": "filt", "entry_t": 0,
                             "notional": 1, "pnl_net": 999}) + "\n")
    assert day_bps_series(str(p), "filt") == []


def test_sharpe_sign_and_zero_variance():
    assert sharpe([1, 1, 1]) == 0.0
    assert sharpe([1, 2, 3]) > 0
    assert sharpe([-1, -2, -3]) < 0
    assert sharpe([5]) == 0.0


def test_cpcv_path_count_and_index_coverage():
    series = [float(i) for i in range(60)]
    r = cpcv_paths(series, n_groups=6, n_test_groups=2, embargo=1)
    # 路径数 = C(6,2) = 15
    assert r.n_paths == 15
    assert len(r.oos_sharpe) == 15
    assert len(r.test_indices) == 15
    # 每条路径测试点数 = 2 组 ≈ 20 个（60/6*2）
    assert all(len(idx) == 20 for idx in r.test_indices)


def test_cpcv_purge_embargo_does_not_drop_test_points():
    series = [0.0] * 48
    r = cpcv_paths(series, n_groups=6, n_test_groups=2, embargo=2)
    # 测试点本身保留（purge 作用于训练侧），每条路径仍 16 点
    assert all(len(idx) == 16 for idx in r.test_indices)


def test_cpcv_positive_series_wins_most_paths():
    # 明确单调向上的序列，OOS 各段均值为正、夏普应普遍 >=0
    series = [float(i % 7) for i in range(120)]
    r = cpcv_paths(series, n_groups=6, n_test_groups=2, embargo=1)
    assert r.oos_win_frac > 0.8


def test_cpcv_short_series_raises():
    with pytest.raises(ValueError):
        cpcv_paths([1.0, 2.0], n_groups=6)


def test_dsr_strong_positive_sharpe_is_likely_true():
    # 大样本、夏普高（非年化 ~0.3），即便有 10 次试验，概率应很高
    p = deflated_sharpe_prob(0.3, n_trials=10, n_obs=400)
    assert p > 0.95


def test_dsr_marginal_sharpe_after_many_trials_deflates():
    # 大量试验 + 小正夏普 + 中等样本 → 概率被压低
    p = deflated_sharpe_prob(0.05, n_trials=200, n_obs=180)
    assert p < 0.5


def test_dsr_negative_sharpe_low_prob():
    p = deflated_sharpe_prob(-0.2, n_trials=1, n_obs=300)
    assert p < 0.05


def test_dsr_validates_inputs():
    with pytest.raises(ValueError):
        deflated_sharpe_prob(0.1, n_trials=0, n_obs=100)
    with pytest.raises(ValueError):
        deflated_sharpe_prob(0.1, n_trials=1, n_obs=1)


def test_pbo_all_negative_is_one():
    p = probability_of_backtest_overfitting([0.1, 0.2], [-0.1, -0.3])
    assert p == 1.0


def test_pbo_all_positive_is_zero():
    p = probability_of_backtest_overfitting([0.1, 0.2], [0.1, 0.3])
    assert p == 0.0


def test_pbo_validates_lengths():
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([0.1], [])
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting([0.1, 0.2], [0.1])


def test_norm_ppf_cdf_roundtrip():
    for p in (0.01, 0.25, 0.5, 0.75, 0.99):
        assert _norm_cdf(_norm_ppf(p)) == pytest.approx(p, abs=1e-5)
    assert _norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert _norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-4)


def test_result_is_frozen_dataclass():
    r = cpcv_paths([float(i % 5) for i in range(60)])
    with pytest.raises((TypeError, AttributeError)):
        r.n_paths = 3  # type: ignore[misc]
    assert isinstance(r, CPCVResult)


def test_dsr_skew_kurt_do_not_break():
    # 带偏度/厚尾的输入应仍返回合法概率
    p = deflated_sharpe_prob(0.2, n_trials=5, n_obs=250, skew=-0.5, kurt=5.0)
    assert 0.0 <= p <= 1.0
    assert math.isfinite(p)
