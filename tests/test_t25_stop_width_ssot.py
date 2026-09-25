"""T-25：止损宽度单一事实来源。

收口前"这笔最多亏多少"有四套互不一致的答案（sizing 顶层 1.0 / non_trend 0.8 /
trend 实际值 / 备份 SL），靠注释"两处语义勿混"提醒。注释不是机制。本测试断言：

* 存在唯一纯函数 ``resolve_effective_stop_width_pct``；
* 生产出场（DSLTracker._effective_max_loss）与 sizing 层
  （compute_effective_stop_pct 的 core_stop）对同一配置/regime 返回【完全相同】
  的宽度 —— 一旦两层漂移，测试失败；
* 该纯函数本身的分层 clamp / 单向性正确。
"""
from __future__ import annotations

import math

import pytest

from hermes_trader.agents.dsl_exit import DSLTracker, ExitPolicy, resolve_effective_stop_width_pct
from hermes_trader.agents.executor import compute_effective_stop_pct

DSL = {
    "max_loss_pct": 1, "max_loss_roe_pct": 15,
    "protect_pct": 1.5, "retrace_threshold": 0.15,
    "atr_stop": {"enabled": False, "atr_mult": 1.5, "floor_pct": 1.0,
                 "ceiling_pct": 4.0},
    "regime_aware": {
        "enabled": True,
        "max_loss": {
            "trend": {"max_loss_pct": 4.0, "max_loss_roe_pct": 20.0},
            "non_trend": {"max_loss_pct": 0.8, "max_loss_roe_pct": 10.0}},
    },
}


def _sizing_core(regime: str) -> float:
    return compute_effective_stop_pct(DSL, regime, 10, 0.0)["core_stop"]


def _exit_width(regime: str, *, max_loss_pct: float, max_loss_roe: float) -> float:
    pol = ExitPolicy(max_loss_pct=max_loss_pct, max_loss_roe_pct=max_loss_roe,
                     atr_stop_enabled=False)
    tr = DSLTracker("T", "long", 100.0, 0.0, policy=pol, leverage=10)
    return tr._effective_max_loss()


@pytest.mark.parametrize("regime,ml,roe", [
    ("neutral", 0.8, 10.0),
    ("chop", 0.8, 10.0),
    ("up", 4.0, 20.0),
    ("down", 4.0, 20.0),
])
def test_sizing_width_equals_exit_width(regime, ml, roe) -> None:
    sizing = _sizing_core(regime)
    exit_w = _exit_width(regime, max_loss_pct=ml, max_loss_roe=roe)
    assert math.isclose(sizing, exit_w, rel_tol=1e-12), (
        f"{regime}: sizing={sizing} exit={exit_w}")


def test_widths_match_real_regime_values() -> None:
    assert _sizing_core("neutral") == 0.8
    # trend：ROE 20/10=2 比 spot 4 更紧 ⇒ 2.0
    assert _sizing_core("up") == 2.0


def test_pure_function_atr_only_tightens() -> None:
    # ATR 启用且算出更窄宽度 ⇒ 收紧到 ATR；算出更宽（超 regime）⇒ 不覆盖 regime。
    base_kw = dict(regime_max_loss_pct=4.0, max_loss_roe_pct=100.0, leverage=10,
                   atr_mult=1.5, atr_floor_pct=1.0, atr_ceiling_pct=4.0)
    tight = resolve_effective_stop_width_pct(
        **base_kw, atr_stop_enabled=True, entry_atr_pct=1.0)  # 1.5
    assert tight == 1.5
    wide = resolve_effective_stop_width_pct(
        **base_kw, atr_stop_enabled=True, entry_atr_pct=9.0)  # cap4 vs regime4 ->4
    assert wide == 4.0


def test_pure_function_roe_cap_binds() -> None:
    w = resolve_effective_stop_width_pct(
        regime_max_loss_pct=4.0, max_loss_roe_pct=20.0, leverage=10,
        atr_stop_enabled=False, atr_mult=1.5, atr_floor_pct=1.0,
        atr_ceiling_pct=4.0, entry_atr_pct=0.0)
    assert w == 2.0


def test_pure_function_no_bound_is_inf() -> None:
    w = resolve_effective_stop_width_pct(
        regime_max_loss_pct=0.0, max_loss_roe_pct=0.0, leverage=10,
        atr_stop_enabled=False, atr_mult=1.5, atr_floor_pct=1.0,
        atr_ceiling_pct=4.0, entry_atr_pct=0.0)
    assert math.isinf(w)
