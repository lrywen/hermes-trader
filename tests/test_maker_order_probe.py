# -*- coding: utf-8 -*-
"""Maker 取样：post-only 下单构造 + 成交质量派生指标单元测试。"""
from __future__ import annotations

from unittest import mock

from hermes_trader.execution import maker as maker_mod
from hermes_trader.execution.maker_probe import (
    MakerSample,
    append_maker_sample,
    maker_sample_to_dict,
)

# ---------- 下单构造（mock exchange，不联网） ----------

def test_place_maker_order_uses_alo_and_returns_envelope():
    fake_exchange = mock.Mock()
    fake_exchange.order.return_value = {"status": "ok", "response": {
        "type": "order", "data": {"statuses": [{"resting": {"oid": 7}}]}}}

    with mock.patch.object(maker_mod, "_make_exchange",
                           return_value=fake_exchange), \
         mock.patch.object(maker_mod, "get_coin_index",
                           return_value=(0, 3, 0)), \
         mock.patch.object(maker_mod, "_round_price_for_hl",
                           return_value="100.000"), \
         mock.patch.object(maker_mod, "_min_order_size",
                           return_value=0.1):
        out = maker_mod.place_hl_maker_order(
            is_buy=True, size=0.5, limit_price=100.0, coin="BTC")

    # 提交给 SDK 的第 5 个位置参数是 OrderType，其 tif 必须为 Alo
    args, kwargs = fake_exchange.order.call_args
    order_type = args[4]
    assert order_type["limit"]["tif"] == "Alo"
    assert kwargs.get("reduce_only") is False
    assert out["tif"] == "Alo"
    assert out["notional"] == 50.0


def test_place_maker_order_rejects_oversize_before_submit():
    fake_exchange = mock.Mock()
    with mock.patch.object(maker_mod, "_make_exchange",
                           return_value=fake_exchange):
        out = maker_mod.place_hl_maker_order(
            is_buy=True, size=2.0, limit_price=100.0, coin="BTC")  # $200
    assert out["ok"] is False
    assert out["error_code"] == "notional_rejected"
    fake_exchange.order.assert_not_called()


# ---------- 成交质量派生 ----------

def test_probe_buy_maker_edge_positive():
    s = MakerSample(coin="BTC", is_buy=True, size=0.5,
                    posted_ms=0, post_limit_px=99.9, post_mid_px=100.0,
                    filled_ms=5000, fill_px=99.9, post_fill_mid_px=100.0)
    assert s.resting_ms == 5000
    # 买单低于 mid 0.1/100 → +10bps 改善；成交后 mid 回到 100，无不利漂移
    assert s.maker_edge_bps > 0
    assert s.post_fill_mid_drift_bps < 0


def test_probe_sell_adverse_drift_detected():
    # 卖单成交后 mid 继续上涨（对空头不利）→ 漂移为正（逆向选择）
    s = MakerSample(coin="ETH", is_buy=False, size=1.0,
                    posted_ms=0, post_limit_px=100.0, post_mid_px=100.0,
                    filled_ms=1000, fill_px=100.0, post_fill_mid_px=101.0)
    assert s.post_fill_mid_drift_bps > 0


def test_probe_dict_roundtrip_and_append(tmp_path):
    s = MakerSample(coin="BTC", is_buy=True, size=0.5,
                    posted_ms=0, post_limit_px=100.0, post_mid_px=100.0,
                    filled_ms=1000, fill_px=100.0, post_fill_mid_px=100.0)
    d = maker_sample_to_dict(s)
    assert "resting_ms" in d and "maker_edge_bps" in d
    p = tmp_path / "probe.jsonl"
    append_maker_sample(str(p), s)
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
