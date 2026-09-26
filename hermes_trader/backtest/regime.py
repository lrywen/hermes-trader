"""RFT-01：回测内核的 regime 层同源回放。

生产入场路径（``executor`` 注册）在每次开仓时：
1. 用 :func:`hermes_trader.agents.market_regime.detect_regime` 取 regime（实盘
   读缓存、底层是 ``classify_candles``）；
2. 用 :func:`hermes_trader.agents.executor.select_exit_params` 按 regime 选
   trend_ride / scalp 出场档；
3. 用 :func:`hermes_trader.agents.executor.resolve_regime_clocks` 取 regime 分
   层持仓时钟。

回测不能调用会发起网络请求、读全局缓存的 ``detect_regime``；但也**禁止**在
内核里重写一套 regime/选档逻辑（会形成第二套实现并漂移）。因此这里：

* regime 判定直接调用生产**纯函数**
  :func:`hermes_trader.agents.market_regime.classify_candles`，喂入"入场决策
  bar 收盘为止"的 PIT 尾部窗口（无前视）；
* 选档与时钟直接调用生产的 ``select_exit_params`` /
  ``resolve_regime_clocks``，并把结果套到调用方给定的基础 policy 上。

这样回测与实盘的"判 regime → 选档 → 选时钟"三步物理同源。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Optional

from hermes_trader.agents.dsl_exit import ExitPolicy

#: PIT 尾部窗口长度。classify_candles 需要慢 EMA（默认30）+ ADX(14) 预热，
#: 100 根与生产 detect_regime 拉取的 1h×100 口径一致。
REGIME_WINDOW = 100


def classify_pit_regime(bars: Sequence, decision_bar: int,
                        window: int = REGIME_WINDOW) -> str:
    """返回决策 bar（含其收盘）为止、仅用历史 bar 的 regime。

    PIT 硬约束：只用 ``bars[0 .. decision_bar]``，绝不触碰决策之后的 bar。
    导入放在函数内，避免 backtest 包在模块导入期强依赖 agents 层。
    """
    from hermes_trader.agents.market_regime import classify_candles

    start = max(0, decision_bar - window + 1)
    tail = list(bars[start:decision_bar + 1])
    return classify_candles(tail)


def regime_aware_policy(base: ExitPolicy, dsl_config: dict,
                        regime: str) -> tuple[ExitPolicy, str]:
    """按 regime 把 ``base`` policy 套成生产入场会得到的出场 policy。

    选档 / 时钟全部调用生产同源函数；``base`` 提供其余所有旋钮（noise_band /
    smooth / scratch / 确认门等）。返回 (新 policy, 选档标签)。
    """
    from hermes_trader.agents.executor import (
        resolve_regime_clocks,
        select_exit_params,
    )

    protect, retrace, tiers_raw, ml_pct, ml_roe, label = \
        select_exit_params(dsl_config, regime)
    # 与生产 executor（:2373）同源：select_exit_params 返回 raw dict tiers，
    # 必须转成 RetraceTier，不能把 dict 直接塞进 phase2_tiers（否则
    # dsl_exit 读 tier.pct_above_entry 时报 'dict' has no attribute）。
    if tiers_raw:
        from hermes_trader.agents.dsl_exit import RetraceTier
        tiers = [RetraceTier(**t) for t in tiers_raw]
    else:
        tiers = list(base.phase2_tiers)
    clocks = resolve_regime_clocks(dsl_config, regime)
    policy = replace(
        base,
        max_loss_pct=float(ml_pct),
        max_loss_roe_pct=float(ml_roe),
        protect_pct=float(protect),
        retrace_threshold=float(retrace),
        hard_timeout_minutes=float(clocks["hard_timeout_minutes"]),
        stale_flat_timeout_minutes=float(clocks["stale_flat_timeout_minutes"]),
        phase2_tiers=tiers,
    )
    return policy, label


def resolve_regime(
    bars: Sequence,
    decision_bar: int,
    *,
    enabled: bool,
    dsl_config: Optional[dict],
    fallback: str,
) -> str:
    """开仓时决定 regime。``enabled=False`` ⇒ 维持旧的常量/信号口径。

    信号自带的 regime（回放真实记录）优先；否则在内核里按 PIT 窗口现判。
    任何判定异常都退回 ``fallback``，不使回测崩溃。
    """
    if not enabled:
        return fallback
    if fallback:
        # 信号已携带决策时刻的真实 regime（replay）——直接用，不再重判。
        return fallback
    try:
        return classify_pit_regime(bars, decision_bar)
    except Exception:  # pragma: no cover - 纯防御
        return "neutral"
