"""Post-only（被动 maker）下单 —— 方案 A 取样专用。

与既有 ``client.exchange.place_hl_order``（硬编码 IOC taker）**并列**，不改其
任何行为。HL 的被动单时间在效指令为 ``Alo``（add-liquidity-only）：订单只在
能挂入簿内、为市场增加流动性时才被接受；一旦会立即吃单（post-only 违约）即
被拒绝，**绝不作为 taker 成交**。

本模块默认服务于"$100 实盘取样"，带硬性名义上限：即使调用方传入更大规模，
也在提交前截断/拒绝，确保取样敞口不超出授权。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from hyperliquid.utils.signing import OrderType
from hyperliquid.utils.types import Cloid

from hermes_trader.client.exchange import (
    _make_exchange,
    _min_order_size,
    _round_price_for_hl,
    get_coin_index,
)

logger = logging.getLogger(__name__)

# 取样授权的单笔名义硬上限（美元）。
MAKER_SAMPLE_MAX_NOTIONAL_USD = 100.0
# HL 单订单最小名义。
HL_MIN_NOTIONAL_USD = 10.0


class MakerNotionalError(ValueError):
    """提交规模超出取样授权或低于交易所下限。"""


def validate_maker_notional(limit_price: float, size: float,
                            max_notional: float = MAKER_SAMPLE_MAX_NOTIONAL_USD
                            ) -> float:
    """校验并返回名义金额；越界即抛错（不静默截断，防止误敞口）。"""
    if limit_price <= 0 or size <= 0:
        raise MakerNotionalError("price/size 必须为正")
    notional = limit_price * size
    if notional < HL_MIN_NOTIONAL_USD:
        raise MakerNotionalError(
            f"名义 ${notional:.2f} < HL 最小 ${HL_MIN_NOTIONAL_USD}")
    if notional > max_notional:
        raise MakerNotionalError(
            f"名义 ${notional:.2f} > 取样授权上限 ${max_notional:.2f}")
    return notional


def place_hl_maker_order(
    is_buy: bool,
    size: float,
    limit_price: float,
    coin: str = "BTC",
    reduce_only: bool = False,
    cloid: Optional[Cloid] = None,
    max_notional: float = MAKER_SAMPLE_MAX_NOTIONAL_USD,
) -> dict[str, Any]:
    """提交一笔 post-only（Alo）限价单；不吃单、不越权。

    返回在标准 ``_parse_order_result`` 结构上补充 ``limit``/``notional``/
    ``tif="Alo"``，便于取样记录挂单起点。
    """
    # 延迟导入以避免与 client.exchange 的模块级单例产生循环。
    from hermes_trader.client.exchange import _parse_order_result

    try:
        notional = validate_maker_notional(limit_price, size, max_notional)
    except MakerNotionalError as e:
        return {"ok": False, "error": str(e), "error_code": "notional_rejected"}

    _, sz_dec, _ = get_coin_index(coin)
    # Post-only：价格必须落在簿内。买入不高于对手价、卖出不低于对手价由调用方
    # 保证；这里仅按 tick/有效数字规则取整（不向外越过）。
    price_str = _round_price_for_hl(limit_price, sz_dec, is_perp=True,
                                    is_buy=is_buy)
    size = max(size, _min_order_size(limit_price, sz_dec))
    size_str = f"{size:.{sz_dec}f}"

    exchange = _make_exchange()
    order_type = OrderType(limit={"tif": "Alo"})

    order_kwargs: dict[str, Any] = {"reduce_only": reduce_only}
    if cloid is not None:
        order_kwargs["cloid"] = cloid

    logger.info(
        f"[place_hl_maker_order] {coin} {'BUY' if is_buy else 'SELL'} "
        f"size={size_str} px={price_str} tif=Alo notional=${notional:.2f}")

    result = exchange.order(
        coin,
        is_buy,
        float(size_str),
        float(price_str),
        order_type,
        **order_kwargs,
    )
    parsed = _parse_order_result(result)
    parsed["tif"] = "Alo"
    parsed["limit"] = float(price_str)
    parsed["notional"] = round(notional, 4)
    return parsed
