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

v2（``simulate_shadow_order_v2``，2026-10-27 S1）修正 v1 的双重乐观：
触及(touch)只代表"可能成交"，v2 按队列穿透概率
``p = base * decay**(resting-1)`` **条件接受**（确定性保守口径下 p<1 一律不
成交；离线回放可传带种子 rng 做蒙特卡洛）；价格擦到一次就跑掉的盈利单在
TTL 末撤单，不再被默认成交。同时把成交当根的 adverse drift 按 ``kappa``
计入成交价（买单更贵/卖单更便宜），消除 v1"固定白拿 offset、drift 仅记账"
的系统性高估。v2 仅用于影子账本/离线回放，**永不触碰实盘**。
"""
from __future__ import annotations

import enum
import hashlib
import random
from dataclasses import dataclass
from typing import Optional

from hermes_trader.models.types import Candle


class FillTimePolicy(str, enum.Enum):
    BAR_START = "bar_start"  # 乐观：成交 bar 开盘即成交
    BAR_END = "bar_end"      # 保守：成交 bar 收盘才成交


class FillModel(str, enum.Enum):
    """成交模型版本。

    V1（maker_shadow）：触及即成交 + 按 limit_px 成交（等于白拿 offset，
        post-fill drift 只记账不计价）。已知双重乐观，保留仅用于对照。
    V2（maker_shadow_v2）：条件成交（队列穿透概率）+ 把逆选择 drift 计入
        成交价，消除 V1 的固定 edge 奖励。仅影子/回放，永不碰实盘。
    """

    V1 = "maker_shadow"
    V2 = "maker_shadow_v2"


# v2 默认参数（用真实 Binance K线经验标定的保守先验；详见
# scripts/maker_v2_calibrate_real.py 与 analysis/maker_v2_calib_*_2026-10-27.json）。
# - BASE_FILL_PROB：第一次 touch 即被队列"良性"接受的条件概率。诊断账本
#   touch 口径上界 94.9% 含严重乐观偏差。经验标定（offset=5bp、resting=30m）：
#   * 5m 全样本 2023-05→2026-10（5.2 万窗口）：touch 后 runaway（逆选择接刀）
#     68%(BTC)/76%(ETH)/84%(SOL)，良性(touch&收回)条件占比 0.15–0.26；
#   * 1m 近 30 天 BTC（1393 窗口，已消除 5m 影线偏差）：touch 71.1%、runaway
#     60.5%（block-bootstrap95 [0.554,0.651]）、良性条件占比 0.298
#     （Wilson95 [0.270,0.327]）。与同口径 5m（0.315）一致，方向稳健。
#   取 0.30 作为 majors 保守先验（≈1m 良性占比的 Wilson 下界附近），使
#   "擦边即弹/快速跑掉的盈利单"按概率不成交。1m 真实回放成交率仅 ~9%，
#   拒掉 v1 约 87% 的虚高成交。队列穿透本身在 OHLC 上不可观测，须在拿到
#   真实成交回报后重标。
# - FILL_PROB_DECAY：每多 resting 一根 bar，穿透概率的指数衰减底数。
# - ADV_DRIFT_KAPPA：把已实现 post-fill adverse drift 计入成交价的系数。
#   1m 近30天 adverse drift 中位正值 +2.5bp（p90 +8.2bp；5m 因影线高估到
#   +6.5/+16.4bp），中位整体为正，故取 1.0（全额计价），不允许"未被 drift
#   解释的固定 +5bp 奖励"。
V2_DEFAULT_BASE_FILL_PROB = 0.30
V2_DEFAULT_FILL_PROB_DECAY = 0.5
V2_DEFAULT_ADV_DRIFT_KAPPA = 1.0


def _deterministic_uniform(order: "ShadowMakerOrder", bar_idx: int) -> float:
    """把 (订单身份, 触及的 bar) 确定性地映射到 [0,1) 上的均匀数。

    实时账本不持有跨调用的 RNG（重放/重启必须得到同一结论），又要让 v2 的
    概率成交长期收敛到 base，因此用稳定 hash 做"等概率确定性判定"：同一订单
    在同一根 bar 上永远得到同一个 u，不同订单/bar 近似 iid 均匀。
    """
    key = f"{order.coin}|{int(order.is_buy)}|{order.limit_px:.10g}|" \
          f"{order.posted_bar_idx}|{order.size:.10g}|{bar_idx}"
    h = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") / float(1 << 64)


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
    # --- v2 审计字段（v1 路径保持为默认值，不改变既有结论）---
    fill_model: str = FillModel.V1.value
    touch_bar_idx: Optional[int] = None     # 首次触及但未被队列接受的 bar
    fill_prob: Optional[float] = None       # 成交当次的穿透概率
    fill_draw: Optional[float] = None       # 当次 [0,1) 均匀判定值（审计用）
    raw_drift_bps: Optional[float] = None   # 计入成交价之前的原始 adverse drift
    adv_drift_charge_bps: Optional[float] = None  # 实际计入价格的逆选择折扣
    residual_drift_bps: Optional[float] = None  # raw−charge（v2 kappa=1 时≈0）
    reject_count: int = 0                   # touch 但被概率拒绝的次数（逆选择证据）

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


def locate_posted_bar_idx(bars: list[Candle], posted_at_ms: float,
                          bar_period_ms: int = 60_000) -> Optional[int]:
    """Index of the last bar that had CLOSED when the order was posted.

    The touch test may only run on bars strictly after posting, so the
    returned index is the most recent bar whose close-time (``t +
    bar_period_ms``) is <= ``posted_at_ms``. A bar still forming at post
    time is excluded (its high/low did not yet exist and using it would be
    look-ahead). Returns None when no closed bar precedes the post time
    (the order must rest until at least one later bar is available).
    """
    idx: Optional[int] = None
    for i, bar in enumerate(bars):
        if bar.t + bar_period_ms <= posted_at_ms:
            idx = i
        else:
            break
    return idx


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


def _bar_raw_adverse_drift_bps(order: ShadowMakerOrder, bars: list[Candle],
                               touch_idx: int) -> Optional[float]:
    """成交 bar 之后一根 bar 的 mid 相对 limit 的逆选择漂移（正＝不利，bps）。

    与 ``ShadowMakerFill.post_fill_mid_drift_bps`` 同一定义，但在"是否计入
    价格"之前单独算出来供 v2 定价；无 forward bar 时返回 None（不可臆造）。
    """
    if touch_idx + 1 >= len(bars):
        return None
    post_mid = (bars[touch_idx + 1].h + bars[touch_idx + 1].l) / 2.0
    if order.limit_px <= 0:
        return None
    drift = (order.limit_px - post_mid) if order.is_buy \
        else (post_mid - order.limit_px)
    return drift / order.limit_px * 1e4


def _apply_adv_drift_to_price(limit_px: float, is_buy: bool,
                              charge_bps: float) -> float:
    """把逆选择折扣计入成交价：买单付得更贵、卖单收得更便宜（更悲观）。"""
    f = max(0.0, charge_bps) / 1e4
    return limit_px * (1.0 + f) if is_buy else limit_px * (1.0 - f)


def simulate_shadow_order_v2(
    order: ShadowMakerOrder,
    bars: list[Candle],
    *,
    base_fill_prob: float = V2_DEFAULT_BASE_FILL_PROB,
    fill_prob_decay: float = V2_DEFAULT_FILL_PROB_DECAY,
    adv_drift_kappa: float = V2_DEFAULT_ADV_DRIFT_KAPPA,
    rng: Optional[random.Random] = None,
    deterministic: bool = False,
) -> ShadowMakerFill:
    """条件成交模型 v2（零资金，仅影子/回放）。

    与 v1 的两处本质差别：

    1. **条件成交而非触及即成交**：第 k 次 touch（k 从 0 起，按 resting 根
       数衰减）的队列穿透概率 ``p_k = base * decay**(resting-1)``。一次 touch
       被拒绝后订单继续挂着等后续 bar；价格一路跑掉（不再 touch）的盈利单
       自然在 TTL 末撤单——修正 v1"漏掉未成交盈利单"的成交率虚高。
       ``deterministic=True``（实时账本默认）做**保守判定**：穿透概率 p<1 的
       touch 一律不成交（不抽随机数，结论可复现）；回放蒙特卡洛请传
       ``deterministic=False`` 并给一个带种子的 ``random.Random``。
    2. **逆选择计入成交价**：成交当根的已实现 adverse drift 按
       ``kappa`` 折扣进 fill price（买单更贵/卖单更便宜），消除 v1 的固定
       +offset 白拿。drift 不可计算（无 forward bar）时不臆造、不收费，但
       在 raw_drift_bps=None 上保留可过滤标记，统计时应排除该笔。

    PIT 约束与 v1 相同：只在 ``posted_bar_idx+1`` 之后判定，不回溯挂单当根。
    """
    base = min(1.0, max(0.0, float(base_fill_prob)))
    decay = min(1.0, max(0.0, float(fill_prob_decay)))
    kappa = min(1.0, max(0.0, float(adv_drift_kappa)))
    if not deterministic and rng is None:
        rng = random.Random()

    start = order.posted_bar_idx + 1
    deadline = min(order.posted_bar_idx + order.ttl_bars, len(bars) - 1)

    first_touch_idx: Optional[int] = None
    rejects = 0
    for i in range(start, deadline + 1):
        bar = bars[i]
        if not would_fill_on_bar(order, bar):
            continue
        if first_touch_idx is None:
            first_touch_idx = i
        resting = i - order.posted_bar_idx
        p = base * (decay ** max(0, resting - 1))
        if deterministic:
            # 等概率确定性判定：用 (订单, bar) 派生稳定均匀数 u，u<p 即成交。
            # 同一输入永远同结论（可复现/可重放），长期成交率收敛到期望 p，
            # 不会出现"p 恒<1 因而永不成交"的静默空账本。
            u = _deterministic_uniform(order, i)
            accept = u < p
        else:
            u = rng.random()
            accept = u < p
        if not accept:
            # 触到但没排到 → 继续挂，等待后续 bar（可能成交也可能跑掉）。
            rejects += 1
            continue

        # 被队列接受 → 成交。逆选择 drift 计入成交价。
        post_mid: Optional[float] = None
        if i + 1 < len(bars):
            post_mid = (bars[i + 1].h + bars[i + 1].l) / 2.0
        raw_drift = _bar_raw_adverse_drift_bps(order, bars, i)
        # 只对不利（正）漂移计价；有利漂移不发奖励（clip 到 0）。
        if raw_drift is not None and raw_drift > 0:
            charge = round(kappa * raw_drift, 6)
        else:
            charge = 0.0
        fill_px = _apply_adv_drift_to_price(order.limit_px, order.is_buy, charge)
        # 残留逆选择（相对 limit 口径）＝ 原始 drift 减去已计入价格的部分；
        # kappa=1 时归零，供 S1 验收直接监控"drift 残差是否回到零附近"。
        residual_drift_bps = (
            round(raw_drift - charge, 6) if raw_drift is not None else None)
        return ShadowMakerFill(
            order=order, filled=True, fill_bar_idx=i, fill_ms=bar.t,
            fill_px=fill_px, post_fill_mid_px=post_mid, canceled=False,
            fill_model=FillModel.V2.value, touch_bar_idx=first_touch_idx,
            fill_prob=round(p, 6), fill_draw=round(u, 6), raw_drift_bps=(
                round(raw_drift, 6) if raw_drift is not None else None),
            adv_drift_charge_bps=charge, reject_count=rejects,
            residual_drift_bps=residual_drift_bps)

    # TTL 末仍未被接受（含触及后跑掉）→ 撤单。
    return ShadowMakerFill(
        order=order, filled=False, fill_bar_idx=None, fill_ms=None,
        fill_px=None, post_fill_mid_px=None, canceled=True,
        fill_model=FillModel.V2.value, touch_bar_idx=first_touch_idx,
        reject_count=rejects)


def deterministic_shadow_order_v2(order: ShadowMakerOrder,
                                  bars: list[Candle], **kwargs) -> ShadowMakerFill:
    """v2 的确定性封装：用稳定 hash 派生均匀数做等概率判定（不抽系统随机数）。

    用于实时影子账本：同一订单/同一根 bar 重放永远得到同一结论（可复现、重启
    不抖），而长期成交率收敛到期望穿透概率 base*decay^k。注意早期版本这里是
    "p>=1 才成交"的保守口径，但 base=0.30、decay=0.5 时 p 恒<1，会导致实时
    v2 账本成交率恒为 0（静默空账本、无法取证），已于 2026-10-27 改为等概率
    确定性判定。需要期望口径回放时也可直接用带种子 ``random.Random`` 的
    ``simulate_shadow_order_v2(deterministic=False, ...)``。
    """
    kwargs.pop("rng", None)
    return simulate_shadow_order_v2(order, bars, deterministic=True, **kwargs)
