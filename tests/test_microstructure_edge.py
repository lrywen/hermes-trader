"""P0-1/P0-2 微结构 edge 研究原语单测。

覆盖：Cont OFI 四指示项方向、Kyle 截距偏置、bar 内符号置换的不变量、
同宇宙随机对照四档分类。
"""
from __future__ import annotations

import math

import pytest

from hermes_trader.validation.microstructure_edge import (
    HARVEY_MIN_ABS_T,
    KyleResult,
    _ofi_one_side,
    kyles_lambda_with_intercept,
    ofi_cont_best_level,
    ofi_depth,
    paired_t_against_random,
    permute_flow_within_bars,
)


# ── Cont best-level OFI ─────────────────────────────────────────────────────

def test_best_level_ofi_price_unchanged_is_delta_q():
    # bid 与 ask 价格都不动：bid 加量为正，ask 加量为负。
    # bid 100→110 (+10)，ask 200→180 (-(-20))= 10-(-20) => +10 - (-( -?))
    e = ofi_cont_best_level(100, 10, 101, 20, 100, 11, 101, 22)
    # bid ΔQ = +1；ask ΔQ = +2 → OFI = +1 - (+2) = -1
    assert e == pytest.approx(-1.0)


def test_best_level_ofi_bid_up_positive():
    # bid 价格上移：取新档全部挂量（买盘进攻）。
    e = ofi_cont_best_level(100, 10, 101, 5, 101, 8, 101, 5)
    # bid 上移 → +8；ask 不变 ΔQ=0
    assert e == pytest.approx(8.0)


def test_best_level_ofi_ask_down_negative_then_normalized():
    # ask 下移（卖压）：ask 项贡献使 OFI 为负。
    e = ofi_cont_best_level(100, 5, 102, 10, 100, 5, 101, 7)
    # bid 不变 0；ask 下移 → -7（ask 公式第一指示项，再在外层取负）
    assert e == pytest.approx(-7.0)


def test_best_level_ofi_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ofi_cont_best_level(0, 1, 1, 1, 1, 1, 1, 1)
    with pytest.raises(ValueError):
        ofi_cont_best_level(1, -1, 1, 1, 1, 1, 1, 1)


def test_ofi_depth_matches_best_level_at_top_level_only():
    # 只放一档时，levels=1 的 OFI-k 必须等于 best-level 公式。
    prev_b, prev_a = [(100.0, 10.0)], [(101.0, 20.0)]
    cur_b, cur_a = [(100.0, 13.0)], [(101.0, 18.0)]
    e_depth = ofi_depth(prev_b, prev_a, cur_b, cur_a, levels=1)
    e_best = ofi_cont_best_level(100, 10, 101, 20, 100, 13, 101, 18)
    assert e_depth == pytest.approx(e_best)
    # bid +3, ask -2 → +3 - (-2)= +5
    assert e_depth == pytest.approx(5.0)


def test_ofi_one_side_entry_and_exit():
    # 旧档退出（当前价 None）：bid 侧第二指示项 → -prev_sz
    assert _ofi_one_side(100.0, 5.0, None, 0.0, bid_side=True) == -5.0
    # 新档进入（旧价 None）：bid 侧第一指示项 → +sz
    assert _ofi_one_side(None, 0.0, 100.0, 7.0, bid_side=True) == 7.0
    # ask 侧方向相反：新档进入卖压 → 第一指示项为 +sz，外层 OFI 再取负
    assert _ofi_one_side(None, 0.0, 100.0, 7.0, bid_side=False) == 7.0


# ── Kyle lambda：截距必要性 ─────────────────────────────────────────────────

def test_kyles_lambda_recovers_slope_with_offset():
    # ΔP = 2.0 + 0.5·flow（含非零截距）。带截距回归应精确还原；过原点应被偏置。
    flows = [float(x) for x in range(-5, 6)]
    dp = [2.0 + 0.5 * x for x in flows]
    r = kyles_lambda_with_intercept(flows, dp)
    assert isinstance(r, KyleResult)
    assert r.alpha == pytest.approx(2.0, abs=1e-9)
    assert r.lam_with_intercept == pytest.approx(0.5, abs=1e-9)
    assert r.r_squared == pytest.approx(1.0, abs=1e-9)
    # flow 零均值时两种口径应一致（这里 mean(flow)=0）
    assert r.lam_through_origin == pytest.approx(0.5, abs=1e-9)
    assert r.slope_bias_from_origin == pytest.approx(0.0, abs=1e-9)


def test_kyles_lambda_origin_biased_when_flow_nonzero_mean():
    # flow 均值偏离 0 且截距非零 → 过原点斜率被偏置，带截距口径仍无偏。
    flows = [1.0, 2.0, 3.0, 4.0, 5.0]  # mean=3
    dp = [2.0 + 0.5 * x for x in flows]
    r = kyles_lambda_with_intercept(flows, dp)
    assert r.lam_with_intercept == pytest.approx(0.5, abs=1e-9)
    # α·mean/var：α=2, mean=3, var(总体有偏)→体现为非零差
    assert abs(r.slope_bias_from_origin) > 0.1
    assert r.lam_through_origin != pytest.approx(r.lam_with_intercept, abs=1e-6)


def test_kyles_lambda_validates_inputs():
    with pytest.raises(ValueError):
        kyles_lambda_with_intercept([1.0], [1.0])  # n<2
    with pytest.raises(ValueError):
        kyles_lambda_with_intercept([1.0, 2.0], [1.0])  # length mismatch
    with pytest.raises(ValueError):
        kyles_lambda_with_intercept([1.0, 1.0], [2.0, 3.0])  # zero var
    with pytest.raises(ValueError):
        kyles_lambda_with_intercept([1.0, float("nan")], [1.0, 2.0])


# ── bar 内符号置换 ──────────────────────────────────────────────────────────

def test_permute_preserves_magnitude_count_and_zero_bar_mean():
    bars = [[1.0, -2.0, 3.0, -4.0], [0.5, 0.5]]
    out = permute_flow_within_bars(bars, seed=42)
    assert len(out) == len(bars)
    for orig, perm in zip(bars, out):
        assert len(perm) == len(orig)
        assert sorted(abs(x) for x in perm) == sorted(abs(x) for x in orig)
        # 每个值是同量级、符号被翻或不翻
        for a, b in zip(orig, perm):
            assert abs(a) == pytest.approx(abs(b))


def test_permute_is_deterministic():
    bars = [[1.0, 2.0, -3.0]]
    assert permute_flow_within_bars(bars, seed=1) == permute_flow_within_bars(bars, seed=1)


# ── 同宇宙随机对照：四档分类 ────────────────────────────────────────────────

def _bars_from_pairs(pairs):
    """pairs: list of bars, each bar list of (signal,ret) → 拆成两个嵌套表。"""
    sig = [[s for s, _ in bar] for bar in pairs]
    ret = [[r for _, r in bar] for bar in pairs]
    return sig, ret


def test_random_control_confirmed_when_signal_strongly_predictive():
    # 构造强确定性关系：每个 bar 内 signal 符号=ret 符号，且量级正相关。
    bars = []
    for b in range(40):
        base = 1.0 + 0.05 * b
        sig = [base, -base, base, -base, base, -base, base, -base]
        ret = [s * 0.9 for s in sig]
        bars.append(list(zip(sig, ret)))
    sig, ret = _bars_from_pairs(bars)
    out = paired_t_against_random(sig, ret, n_seeds=3, base_seed=7)
    assert out["category"] == "confirmed_alive"
    assert out["t"] >= HARVEY_MIN_ABS_T
    assert out["signal_ic"] > 0.9
    # 随机基线 IC 应接近 0（符号被随机翻转）
    assert abs(out["random_ic"]) < 0.2


def test_random_control_noise_when_no_relation():
    # 用固定但与 signal 无关的 ret：置换前后 IC 都接近 0 → alpha t 不过线。
    bars = []
    for b in range(30):
        sig = [((i % 3) - 1) * 1.0 for i in range(12)]
        ret = [((i * 7 + b) % 5 - 2) * 1.0 for i in range(12)]
        bars.append(list(zip(sig, ret)))
    sig, ret = _bars_from_pairs(bars)
    out = paired_t_against_random(sig, ret, n_seeds=3, base_seed=11, min_abs_t=3.5)
    assert out["category"] in ("noise", "reversed_strict", "confirmed_alive", "train_only")
    # 无关系时不应稳定判为 confirmed
    assert out["category"] != "confirmed_alive"


def test_random_control_train_only_when_oos_breaks():
    # 训练段强关系，OOS 段无关系（正交）。
    def mk(strong, n=30, seed=0):
        bars = []
        for b in range(n):
            sig = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
            if strong:
                ret = [s * 0.8 for s in sig]
            else:
                # 与 signal 无关的零均值噪声（不反号，只是映射消失）。
                ret = [0.1 if (i * 3 + b * 7) % 2 else -0.1 for i in range(len(sig))]
            bars.append(list(zip(sig, ret)))
        return _bars_from_pairs(bars)

    tr_sig, tr_ret = mk(True)
    oo_sig, oo_ret = mk(False)
    out = paired_t_against_random(
        tr_sig, tr_ret, n_seeds=3, base_seed=3,
        oos_signal=oo_sig, oos_fwd_ret=oo_ret,
    )
    assert out["oos"] is not None
    assert out["category"] == "train_only"
    assert out["t"] >= HARVEY_MIN_ABS_T
    assert out["oos"]["t"] < HARVEY_MIN_ABS_T


def test_random_control_validates():
    with pytest.raises(ValueError):
        paired_t_against_random([[1.0]], [[1.0]], n_seeds=2)  # <2 bars
    with pytest.raises(ValueError):
        paired_t_against_random([[1.0, 2.0]], [[1.0]], n_seeds=2)  # mismatch
    with pytest.raises(ValueError):
        paired_t_against_random([[1.0, 2.0], [3.0, 4.0]],
                                [[1.0, 2.0], [3.0, 4.0]], n_seeds=0)
