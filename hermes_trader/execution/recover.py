"""P3 崩溃恢复：启动时扫描意图 WAL 中的未决订单并对账。

进程崩溃可能发生在下单的任意窗口：
  * INTENT 已写但未 place —— 交易所无此单，标记 FAILED（可安全重试）。
  * SUBMITTED/UNKNOWN —— 订单可能已成交但回执/登记丢失。用 cloid 调
    reconcile_order_fill 查 userFills：
      - filled     → 补登（由调用方走 rehydrate 建 tracker），标 FILLED；
      - not_filled → 标 FAILED，可安全重试；
      - unknown    → 保持 UNKNOWN，记日志 + 告警，留给下一轮恢复或人工。

本模块只读交易所、只写 WAL，不直接建 DSL tracker（那是 rehydrate_from_exchange
的职责，它在恢复之后跑，会把确认成交的仓位补登）。恢复本身 best-effort：
任何单条失败只记日志，不阻断启动。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import intent_wal
from .order_fsm import OrderIntent, OrderState

logger = logging.getLogger("execution.recover")


def _reconcile(intent: OrderIntent) -> dict[str, Any]:
        from hermes_trader.client.exchange import reconcile_order_fill
        return reconcile_order_fill(
            coin=intent.coin, cloid=intent.cloid,
            is_buy=intent.is_buy,
            expect_size=float(intent.size_in_coin or 0.0) or None,
        )


def recover_pending_orders(*, wal_path: Optional[str] = None,
                           notify_fn=None) -> dict[str, Any]:
    """启动时调用：恢复 WAL 中所有未决订单意图。返回恢复摘要。

    notify_fn: 可选 callable(text, category=...)，用于风险告警（filled 补登
    或 unresolvable）。默认尝试 hermes_trader.notify.send_text。
    """
    if notify_fn is None:
        try:
            from hermes_trader import notify as _n
            notify_fn = _n.send_text
        except Exception:
            notify_fn = None

    pending = intent_wal.load_pending(wal_path)
    summary = {"scanned": len(pending), "filled": 0, "failed": 0,
               "still_unknown": 0, "errors": 0, "cloids_filled": []}
    if not pending:
        return summary

    logger.warning("[recover] %d 未决订单意图待对账", len(pending))
    for intent in pending:
        try:
            # INTENT 未 place：交易所无单，直接标 FAILED。
            if intent.state == OrderState.INTENT:
                rec = intent.transition(OrderState.FAILED,
                                        reason="crash_before_place")
                intent_wal.record_transition(intent, rec)
                summary["failed"] += 1
                continue
            rc = _reconcile(intent)
        except Exception as e:
            logger.error("[recover] reconcile raised for %s: %r", intent.coin, e)
            summary["errors"] += 1
            continue

        status = rc.get("status")
        try:
            if status == "filled":
                rec = intent.transition(
                    OrderState.FILLED, reason="crash_recovered_fill",
                    order_id=rc.get("oid"), avg_px=rc.get("avg_px"),
                    total_sz=rc.get("total_sz"))
                intent_wal.record_transition(intent, rec)
                summary["filled"] += 1
                summary["cloids_filled"].append(intent.cloid)
                logger.warning(
                    "[recover] %s %s 崩溃后确认成交 avg_px=%s sz=%s — 待 rehydrate 补登",
                    intent.coin, intent.cloid, rc.get("avg_px"), rc.get("total_sz"))
                if notify_fn:
                    try:
                        notify_fn(
                            f"⚠️ 崩溃恢复：{intent.coin} 订单已成交，已补登对账 "
                            f"px={rc.get('avg_px')} sz={rc.get('total_sz')} "
                            f"cloid={intent.cloid}", category="risk")
                    except Exception:
                        pass
            elif status == "not_filled":
                rec = intent.transition(OrderState.FAILED,
                                        reason="crash_recovered_not_filled")
                intent_wal.record_transition(intent, rec)
                summary["failed"] += 1
            else:
                # 对账本身未决：保持 UNKNOWN，下轮恢复或人工。
                summary["still_unknown"] += 1
                logger.error("[recover] %s %s 对账未决: %s",
                             intent.coin, intent.cloid, rc.get("reason"))
        except Exception as e:
            logger.error("[recover] transition failed for %s: %r", intent.cloid, e)
            summary["errors"] += 1
    return summary
