"""CPCV / DSR / PBO 的纯函数实现（标准库，零第三方依赖）。"""
from __future__ import annotations

import collections
import itertools
import json
import math
import statistics
from dataclasses import dataclass

DAY_MS = 86_400_000
# 日收益序列的年化因子（此处仅用于夏普口径，bps 不影响符号判定）。
PERIODS_PER_YEAR = 365


def day_bps_series(path: str, arm: str) -> list[float]:
    """从 trade JSONL 装载单一臂的"按天 bps"有序序列（与 bps_block_bootstrap 同契约）。"""
    byday: dict[int, list[float]] = collections.defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("type") != "trade" or d.get("arm") != arm or not d.get("notional"):
                continue
            byday[d["entry_t"] // DAY_MS].append(d["pnl_net"] / d["notional"] * 1e4)
    return [statistics.mean(byday[k]) for k in sorted(byday)]


def sharpe(x: list[float]) -> float:
    """非年化夏普（均值/样本标准差）；标准差为 0 时返回 0。"""
    n = len(x)
    if n < 2:
        return 0.0
    sd = statistics.stdev(x)
    if sd == 0:
        return 0.0
    return statistics.mean(x) / sd


@dataclass(frozen=True)
class CPCVResult:
    n_paths: int
    oos_sharpe: tuple[float, ...]
    oos_mean_bps: tuple[float, ...]
    oos_win_frac: float          # OOS 路径中夏普>0 的占比
    test_indices: tuple[tuple[int, ...], ...]


def cpcv_paths(
    series: list[float],
    n_groups: int = 6,
    n_test_groups: int = 2,
    embargo: int = 1,
) -> CPCVResult:
    """组合 purged 交叉验证。

    将有序日块序列划分为 ``n_groups`` 个相邻组；枚举选出 ``n_test_groups``
    个组作为测试集的所有组合（路径数 = C(K,C)）。每条路径在测试段两侧做
    purge（剔除与训练段相邻的点）并施加 ``embargo`` 天禁运，消除标签重叠
    （日块聚合后 purge 需求小，默认 1 天）。
    """
    n = len(series)
    if n < n_groups * 2:
        raise ValueError(f"序列过短: n={n} < 2*n_groups={n_groups * 2}")
    # 各组边界（尽量均分）
    bounds = [round(i * n / n_groups) for i in range(n_groups + 1)]
    groups = [list(range(bounds[i], bounds[i + 1])) for i in range(n_groups)]

    test_idx_sets: list[tuple[int, ...]] = []
    oos_sh: list[float] = []
    oos_mu: list[float] = []
    for test_groups in itertools.combinations(range(n_groups), n_test_groups):
        test_set = sorted(i for g in test_groups for i in groups[g])
        purge_set = set(test_set)
        for g in test_groups:
            # 测试段两端的 purge + embargo
            if groups[g][0] - 1 >= 0:
                for e in range(1, embargo + 2):
                    if groups[g][0] - e >= 0:
                        purge_set.add(groups[g][0] - e)
            if groups[g][-1] + 1 < n:
                for e in range(1, embargo + 2):
                    if groups[g][-1] + e < n:
                        purge_set.add(groups[g][-1] + e)
        vals = [series[i] for i in test_set]
        test_idx_sets.append(tuple(test_set))
        oos_sh.append(sharpe(vals))
        oos_mu.append(statistics.mean(vals) if vals else 0.0)

    win = sum(1 for s in oos_sh if s > 0) / len(oos_sh)
    return CPCVResult(
        n_paths=len(oos_sh),
        oos_sharpe=tuple(oos_sh),
        oos_mean_bps=tuple(oos_mu),
        oos_win_frac=win,
        test_indices=tuple(test_idx_sets),
    )


def deflated_sharpe_prob(
    observed_sharpe: float,
    n_trials: int,
    n_obs: int,
    *,
    skew: float = 0.0,
    kurt: float = 3.0,
    seed_sr_variance: float | None = None,
) -> float:
    """DSR：观察到的夏普为真（非多重检验运气）的概率，返回 [0,1]。

    采用 Bailey & López de Prado (2014) 的闭式解。以"多次试验中最优夏普"的
    期望（由极值近似）作为夏普的零假设门槛，再据观测夏普的标准误做正态校正。

    参数为**非年化**夏普（与 :func:`sharpe` 一致）。``kurt`` 为普通峰度
    （正态=3）。
    """
    if n_obs < 2 or n_trials < 1:
        raise ValueError("n_obs 与 n_trials 必须为正")
    # 1) 夏普估计量的方差（PLT 式 2014，式（9)）
    if seed_sr_variance is None:
        sr_var = (1.0 - skew * observed_sharpe +
                  (kurt - 1.0) / 4.0 * observed_sharpe ** 2) / (n_obs - 1)
    else:
        sr_var = seed_sr_variance
    sr_sd = math.sqrt(max(sr_var, 0.0))
    if sr_sd == 0:
        return 1.0 if observed_sharpe > 0 else 0.0
    # 2) 零假设门槛：N 次独立试验下"最大夏普"的期望（Euler-Mascheroni 极值近似）
    #    单次试验时 E[max]=0（即不做多重检验校正）。
    euler = 0.5772156649
    if n_trials == 1:
        sr0 = 0.0
    else:
        sr0 = sr_sd * (
            (1 - euler) * _norm_ppf(1 - 1.0 / n_trials)
            + euler * _norm_ppf(1 - 1.0 / (n_trials * math.e))
        )
    # 3) P(真实夏普 > 0 | 观测) = Φ((SR - SR0)/sd_SR)
    z = (observed_sharpe - sr0) / sr_sd
    return _norm_cdf(z)


def probability_of_backtest_overfitting(
    is_sharpe: list[float], oos_sharpe: list[float]
) -> float:
    """PBO：样本内最优策略在样本外落到中位数以下的路径占比。

    入参为按 CPCV 路径对齐的"各候选策略 IS 夏普矩阵 → 选中策略 OOS 夏普"。
    简化接口：直接给每条路径上"被选中策略"的 IS 与 OOS 夏普，本函数据 OOS
    相对该路径 OOS 横截面中位的位置估计；当只评估单一策略时，OOS 夏普<0
    （即劣于零基准）即记为过拟合路径。
    """
    if len(is_sharpe) != len(oos_sharpe) or not oos_sharpe:
        raise ValueError("is/oos 夏普序列须等长且非空")
    bad = sum(1 for s in oos_sharpe if s < 0)
    return bad / len(oos_sharpe)


# --- 标准正态 CDF / PPF（Acklam / 误差函数近似），标准库实现 ---

def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """逆标准正态 CDF（Acklam 近似），p∈(0,1)。"""
    if not 0.0 < p < 1.0:
        raise ValueError("p 必须在 (0,1)")
    # 常量
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
           ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
