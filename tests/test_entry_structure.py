"""entry_structure 模块测试：组合波动率目标、regime 突破 veto、横截面动量。"""
from __future__ import annotations

import math

from hermes_trader.agents import entry_structure as es


# ── vol target multiplier ───────────────────────────────────────────────────

def test_vol_target_at_target_is_one() -> None:
    assert es.vol_target_multiplier(sigma=0.04, target_sigma=0.04) == 1.0


def test_vol_target_scales_down_above_target() -> None:
    assert es.vol_target_multiplier(sigma=0.08, target_sigma=0.04) == 0.5


def test_vol_target_cap_prevents_leverage_up() -> None:
    # cap=1 → low vol never levers the book up
    assert es.vol_target_multiplier(sigma=0.02, target_sigma=0.04, cap=1.0) == 1.0
    # explicit cap>1 allows re-lever
    assert es.vol_target_multiplier(sigma=0.02, target_sigma=0.04, cap=2.0) == 2.0


def test_vol_target_fails_safe_on_bad_input() -> None:
    assert es.vol_target_multiplier(sigma=0.0, target_sigma=0.04) == 1.0
    assert es.vol_target_multiplier(sigma=math.nan, target_sigma=0.04) == 1.0


def test_realised_sigma() -> None:
    assert es.realised_sigma([0.01, -0.01, 0.01]) > 0
    assert es.realised_sigma([0.01]) is None


# ── regime breakout veto ───────────────────────────────────────────────────

def test_breakout_veto_in_chop() -> None:
    r = es.breakout_regime_veto(regime="chop", trigger="breakout")
    assert r is not None and "chop" in r


def test_breakout_veto_neutral_blocks() -> None:
    assert es.breakout_regime_veto(regime="neutral", trigger="breakout") is not None


def test_breakout_veto_allows_in_trend() -> None:
    assert es.breakout_regime_veto(regime="up", trigger="breakout") is None


def test_breakout_veto_ignores_pullback() -> None:
    # 回撤/非突破触发不受 veto 约束
    assert es.breakout_regime_veto(regime="chop", trigger="pullback") is None
    assert es.breakout_regime_veto(regime="chop", trigger="burst") is None


# ── cross-sectional momentum ───────────────────────────────────────────────

def test_directional_momentum_score() -> None:
    assert es.directional_momentum_score(px_now=110, px_past=100) == 0.1
    assert es.directional_momentum_score(px_now=0, px_past=100) == -1.0
    assert es.directional_momentum_score(px_now=100, px_past=0) is None


def test_rank_and_select_top_n() -> None:
    ranked = es.rank_by_momentum([("A", 0.1), ("B", 0.3), ("C", -0.2)])
    assert [c for c, _ in ranked] == ["B", "A", "C"]
    assert es.select_top_n(ranked, n=2) == ["B", "A"]


def test_select_top_n_requires_positive_for_long() -> None:
    ranked = es.rank_by_momentum([("A", 0.1), ("B", -0.1), ("C", -0.3)])
    assert es.select_top_n(ranked, n=3, side="long") == ["A"]


def test_select_top_n_short_takes_weakest() -> None:
    ranked = es.rank_by_momentum([("A", 0.3), ("B", -0.1), ("C", -0.3)])
    assert es.select_top_n(ranked, n=2, side="short") == ["C", "B"]
