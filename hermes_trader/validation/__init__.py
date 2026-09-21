"""独立显著性/过拟合检验方法（与 block-bootstrap 并列，不替换它）。

数据契约与 scripts/bps_block_bootstrap.py 完全一致：逐笔
``pnl_net / notional * 1e4``（bps），按 ``entry_t // 86_400_000`` 聚合为
"每日 bps"有序序列。本模块在该序列上提供：

- CPCV（Combinatorial Purged Cross-Validation，López de Prado）：
  把 N 个日块分 K 组、选其中 C 组为测试集，并在测试段两侧做 purge + embargo，
  得到多路"样本外"路径，统计 OOS 收益/夏普；
- DSR（Deflated Sharpe Ratio，Bailey & López de Prado）：
  在已做 N 次试验、观察到的最优夏普基础上，扣除多重检验的"选择膨胀"，
  给出该夏普为真（非运气）的概率；
- PBO（Probability of Backtest Overfitting）：由 CPCV 的 OOS 路径估计
  "样本内最优策略在样本外落到中位数以下"的概率。

全部纯标准库自实现，不引入外部框架（满足方案 C-05）。
"""
from __future__ import annotations

from .significance import (
    CPCVResult,
    cpcv_paths,
    day_bps_series,
    deflated_sharpe_prob,
    probability_of_backtest_overfitting,
    sharpe,
)

__all__ = [
    "CPCVResult",
    "day_bps_series",
    "cpcv_paths",
    "deflated_sharpe_prob",
    "probability_of_backtest_overfitting",
    "sharpe",
]
