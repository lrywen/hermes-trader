"""SHADOW maker 成交模拟器（零资金）。

在不向交易所提交任何订单的前提下，用真实历史/实时 K线判定一张"假想被动单"
是否会成交、何时成交、成交后价格如何漂移，用于在花钱做实盘取样前先回答
maker 策略的**生死线问题：逆向选择是否吃掉 maker edge**。

口径（务必在引用结论时一并声明）：

1. **成交判定＝保守触及规则（touch）**，基于 K线 high/low：
   - 买单：bar.low <= limit_px 判为成交；
   - 卖单：bar.high >= limit_px 判为成交。
   该规则**不知道你在队列中的位置**，因此会**系统性高估真实成交率**——
   价格刚碰到挂单价就弹回时，真实世界可能根本轮不到你。SHADOW 给的是
   "乐观成交上界"，真实成交率需后续小额实盘校准。
2. **成交时点**默认取成交 bar 的开盘（``FillTimePolicy.BAR_START``），即在
   bar 内尽早成交；同样偏乐观。可切换为 BAR_END（保守，bar 收盘才成交）。
3. **成交后 mid**用"成交后下一根 bar 的 (h+l)/2"代理，计算逆向选择漂移；
   若在最后一根成交、无后续 bar，则漂移为 None（不可计算，不臆造）。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from hermes_trader.models.types import Candle


class FillTimePolicy(str, enum.Enum):
    BAR_START = "bar_start"  # 乐观：成交 bar 开盘即成交
    BAR_END = "bar_end"      # 保守：成交 bar 收盘才成交


@dataclass
class ShadowMakerOrder:
    coin: str
    is_buy: bool
    size: float
    posted_bar_idx: int
    limit_px: float
    post_mid_px: float
    ttl_bars: int = 24          # 挂单最长存活 bar 数，超时判撤销
    reduce_only: bool = False


@dataclass
class ShadowMakerFill:
    order: ShadowMakerOrder
    filled: bool
    fill_bar_idx: Optional[int]
    fill_ms: Optional[int]
    fill_px: Optional[float]
    post_fill_mid_px: Optional[float]
    canceled: bool

    @property
    def resting_bars(self) -> Optional[int]:
        if not self.filled:
            return None
        return self.fill_bar_idx - self.order.posted_bar_idx

    @property
    def maker_edge_bps(self) -> Optional[float]:
        """成交价相对挂单时 mid 的改善（正＝更好的价）。"""
        if not self.filled or self.order.post_mid_px <= 0:
            return None
        diff = (self.order.post_mid_px - self.fill_px) if self.order.is_buy \
            else (self.fill_px - self.order.post_mid_px)
        return diff / self.order.post_mid_px * 1e4

    @property
    def post_fill_mid_drift_bps(self) -> Optional[float]:
        """成交后 mid 的不利方向漂移（正＝逆向选择）。

        买单不利＝mid 下跌；卖单不利＝mid 上涨。无后续 bar 时返回 None。
        """
        if not self.filled or self.post_fill_mid_px is None \
                or self.fill_px <= 0:
            return None
        drift = (self.fill_px - self.post_fill_mid_px) if self.order.is_buy \
            else (self.post_fill_mid_px - self.fill_px)
        return drift / self.fill_px * 1e4


def would_fill_on_bar(order: ShadowMakerOrder, bar: Candle) -> bool:
    """保守触及规则：该 bar 是否"碰到"挂单价。"""
    if order.is_buy:
        return bar.l <= order.limit_px
    return bar.h >= order.limit_px


def simulate_shadow_order(
    order: ShadowMakerOrder,
    bars: list[Candle],
    fill_time_policy: FillTimePolicy = FillTimePolicy.BAR_START,
) -> ShadowMakerFill:
    """在挂单之后的 bar 上判定成交/撤销。

    ``bars`` 为**整段**K线（含挂单 bar）；判定从 ``posted_bar_idx+1`` 开始
    （挂单当根不回溯成交，避免用已发生的 high/low 形成前视）。
    """
    start = order.posted_bar_idx + 1
    deadline = min(order.posted_bar_idx + order.ttl_bars, len(bars) - 1)

    for i in range(start, deadline + 1):
        bar = bars[i]
        if not would_fill_on_bar(order, bar):
            continue
        # 成交。成交后 mid 用"下一根 bar 的 (h+l)/2"代理。
        post_mid: Optional[float] = None
        if i + 1 < len(bars):
            nb = bars[i + 1]
            post_mid = (nb.h + nb.l) / 2
        fill_ms = bar.t
        if fill_time_policy == FillTimePolicy.BAR_END:
            # BAR_END 口径：在 bar 收盘时点成交（时间戳仍取该 bar.t，
            # 成交价用 close 更贴近"末段才成交"的保守假设）。
            fill_px = bar.c
        else:
            fill_px = order.limit_px
        return ShadowMakerFill(
            order=order, filled=True, fill_bar_idx=i, fill_ms=fill_ms,
            fill_px=fill_px, post_fill_mid_px=post_mid, canceled=False)

    # TTL 内未成交 → 撤销
    return ShadowMakerFill(
        order=order, filled=False, fill_bar_idx=None, fill_ms=None,
        fill_px=None, post_fill_mid_px=None, canceled=True)
