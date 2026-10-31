"""P3 订单显式状态机（order lifecycle FSM）。

融合文档偏离⑤：订单生命周期无状态机。此前 executor 的状态迁移散落在
in-flight set / flock / order_res dict / rehydrate 各处，崩溃后无法回答
"这个 cloid 现在到底处于什么状态"。本模块把生命周期收拢为显式状态机：

    INTENT ──submit──▶ SUBMITTED ──fill──▶ FILLED
       │                 │
       │                 ├─definite reject─▶ FAILED
       │                 ├─cancel──────────▶ CANCELLED
       │                 └─response_unknown─▶ UNKNOWN（待对账）
       │                                        │
       │                                        ├─reconcile filled──▶ FILLED
       │                                        ├─reconcile not_fill─▶ FAILED
       │                                        └─unresolvable──────▶ UNKNOWN(stay)
       └─abort（gate 拦截/未下单）─────────────▶ FAILED

终态（FILLED/FAILED/CANCELLED）不可再迁移；UNKNOWN 是唯一的"挂起待对账"
状态，崩溃恢复扫描的就是 SUBMITTED + UNKNOWN 两类未决记录。

状态机本身纯内存、无副作用；持久化由 intent_wal 在每次迁移时追加。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class OrderState(str, Enum):
    INTENT = "intent"          # 意图已记录，尚未向交易所提交
    SUBMITTED = "submitted"    # 已提交，等待成交回执
    UNKNOWN = "unknown"        # response_unknown：提交后回执丢失，待对账
    FILLED = "filled"          # 已成交（终态）
    FAILED = "failed"          # 明确失败/拒单/未成交（终态）
    CANCELLED = "cancelled"    # 已撤销（终态）


TERMINAL = {OrderState.FILLED, OrderState.FAILED, OrderState.CANCELLED}
# 崩溃恢复需要 reconcile 的未决状态。
PENDING = {OrderState.INTENT, OrderState.SUBMITTED, OrderState.UNKNOWN}

# 合法迁移表：from -> 允许的 to 集合。
_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.INTENT: {OrderState.SUBMITTED, OrderState.FAILED,
                        OrderState.CANCELLED},
    OrderState.SUBMITTED: {OrderState.FILLED, OrderState.FAILED,
                           OrderState.UNKNOWN, OrderState.CANCELLED},
    OrderState.UNKNOWN: {OrderState.FILLED, OrderState.FAILED,
                         OrderState.CANCELLED},
    # 终态无出边
    OrderState.FILLED: set(),
    OrderState.FAILED: set(),
    OrderState.CANCELLED: set(),
}


class IllegalTransition(Exception):
    """非法状态迁移（如终态再迁移、跨级跳转）。"""


@dataclass
class OrderIntent:
    """一条订单意图及其生命周期状态。cloid 是唯一幂等键。"""
    cloid: str
    coin: str
    side: str                 # "long" / "short"
    is_buy: bool
    size_in_coin: float
    limit_px: Optional[float] = None
    mid_px: float = 0.0
    mode: str = "live"
    analysis_id: str = ""
    state: OrderState = OrderState.INTENT
    created_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    order_id: Optional[str] = None
    avg_px: Optional[float] = None
    total_sz: Optional[float] = None
    reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def can_transition(self, to: OrderState) -> bool:
        return to in _TRANSITIONS[self.state]

    def transition(self, to: OrderState, *, reason: str = "",
                   **fields: Any) -> dict[str, Any]:
        """执行一次状态迁移，返回迁移记录（供 WAL 追加）。

        非法迁移抛 IllegalTransition。fields 可附带 order_id/avg_px/total_sz。
        """
        if not self.can_transition(to):
            raise IllegalTransition(
                f"{self.cloid}: {self.state.value} -> {to.value} 非法")
        frm = self.state
        self.state = to
        self.updated_ms = int(time.time() * 1000)
        if reason:
            self.reason = reason
        for k in ("order_id", "avg_px", "total_sz"):
            if k in fields and fields[k] is not None:
                setattr(self, k, fields[k])
        rec = {
            "cloid": self.cloid, "coin": self.coin,
            "from": frm.value, "to": to.value,
            "ts": self.updated_ms, "reason": reason,
            "analysis_id": self.analysis_id,
        }
        for k in ("order_id", "avg_px", "total_sz"):
            v = getattr(self, k)
            if v is not None:
                rec[k] = v
        self.history.append(rec)
        return rec

    @property
    def is_pending(self) -> bool:
        return self.state in PENDING

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "cloid": self.cloid, "coin": self.coin, "side": self.side,
            "is_buy": self.is_buy, "size_in_coin": self.size_in_coin,
            "limit_px": self.limit_px, "mid_px": self.mid_px,
            "mode": self.mode, "analysis_id": self.analysis_id,
            "state": self.state.value, "created_ms": self.created_ms,
            "updated_ms": self.updated_ms, "order_id": self.order_id,
            "avg_px": self.avg_px, "total_sz": self.total_sz,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OrderIntent":
        return cls(
            cloid=d["cloid"], coin=d["coin"], side=d.get("side", "long"),
            is_buy=bool(d.get("is_buy", True)),
            size_in_coin=float(d.get("size_in_coin", 0.0)),
            limit_px=d.get("limit_px"), mid_px=float(d.get("mid_px", 0.0)),
            mode=d.get("mode", "live"), analysis_id=d.get("analysis_id", ""),
            state=OrderState(d.get("state", "intent")),
            created_ms=int(d.get("created_ms", 0)),
            updated_ms=int(d.get("updated_ms", 0)),
            order_id=d.get("order_id"), avg_px=d.get("avg_px"),
            total_sz=d.get("total_sz"), reason=d.get("reason", ""),
        )
