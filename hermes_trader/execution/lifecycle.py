"""P3 executor ↔ FSM/WAL 桥：下单生命周期的持久化接线。

把 executor 下单主流程的三个关键点接到显式状态机 + 意图 WAL，而不让
executor 直接散落 FSM/WAL 调用。所有函数都是 best-effort：WAL 写失败只记
日志，绝不阻断下单主路径（资金安全靠 exchange cloid 幂等 + reconcile 兜底，
WAL 是崩溃恢复的增强，不是下单的前置条件）。

用法（executor）：
    intent = begin_order(cloid=..., coin=..., ...)   # place 之前
    mark_submitted(intent)                            # place 之后（无论回执）
    mark_unknown(intent, reason=...)                  # response_unknown
    mark_filled(intent, order_res=...)                # 确认成交
    mark_failed(intent, reason=...)                   # 明确失败/拒单
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import intent_wal
from .order_fsm import OrderIntent, OrderState

logger = logging.getLogger("execution.lifecycle")

# 进程内活跃 intent 注册表：cloid -> OrderIntent。崩溃后由 intent_wal 重建。
_ACTIVE: dict[str, OrderIntent] = {}


def begin_order(*, cloid: Any, coin: str, side: str, is_buy: bool,
                size_in_coin: float, mid_px: float, mode: str,
                analysis_id: str, limit_px: Optional[float] = None
                ) -> Optional[OrderIntent]:
    """place_hl_order 之前调用：建 intent 并写 WAL。cloid 为 None 时跳过
    （无幂等键的订单不做 WAL，与 executor 的 fail-open cloid 策略一致）。"""
    if cloid is None:
        return None
    try:
        intent = OrderIntent(
            cloid=str(cloid), coin=coin, side=side, is_buy=is_buy,
            size_in_coin=float(size_in_coin or 0.0), limit_px=limit_px,
            mid_px=float(mid_px or 0.0), mode=mode, analysis_id=analysis_id,
        )
        intent_wal.record_intent(intent)
        _ACTIVE[intent.cloid] = intent
        return intent
    except Exception as e:  # WAL 失败不阻断下单
        logger.error("[lifecycle] begin_order WAL failed for %s: %r", coin, e)
        return None


def _transition(intent: Optional[OrderIntent], to: OrderState,
                reason: str = "", **fields: Any) -> None:
    if intent is None:
        return
    try:
        rec = intent.transition(to, reason=reason, **fields)
        intent_wal.record_transition(intent, rec)
        if intent.is_terminal:
            _ACTIVE.pop(intent.cloid, None)
    except Exception as e:
        logger.error("[lifecycle] transition %s->%s failed for %s: %r",
                     intent.state.value, to.value, intent.coin, e)


def mark_submitted(intent: Optional[OrderIntent]) -> None:
    """place_hl_order 之后调用（无论回执如何，订单已离手）。"""
    _transition(intent, OrderState.SUBMITTED)


def mark_unknown(intent: Optional[OrderIntent], reason: str = "") -> None:
    """response_unknown：回执丢失，进入待对账状态。"""
    _transition(intent, OrderState.UNKNOWN, reason=reason or "response_unknown")


def mark_filled(intent: Optional[OrderIntent], order_res: dict[str, Any]) -> None:
    """确认成交（正常回执或 reconcile 补登）。"""
    _transition(intent, OrderState.FILLED,
                order_id=order_res.get("order_id"),
                avg_px=order_res.get("avg_px"),
                total_sz=order_res.get("total_sz"))


def mark_failed(intent: Optional[OrderIntent], reason: str = "") -> None:
    """明确失败/拒单/确认未成交。"""
    _transition(intent, OrderState.FAILED, reason=reason)


def active_intents() -> dict[str, OrderIntent]:
    return dict(_ACTIVE)
