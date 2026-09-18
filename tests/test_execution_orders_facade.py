"""P1-1 step ② — execution/orders facade guard.

The new execution layer must be a thin, behaviour-neutral facade over
client.exchange: every order-mechanics name re-exported by execution.orders
is the SAME object client.exchange defines (no copy / no rewrite), and the
agents.executor module binds those same objects so existing
``monkeypatch.setattr(executor, "place_hl_order", ...)`` tests keep working.

Market-data reads (get_hl_price / get_hl_atr / get_max_leverage /
get_orderbook_spread) intentionally stay OUT of this facade.
"""
from __future__ import annotations

from hermes_trader.agents import executor
from hermes_trader.client import exchange
from hermes_trader.execution import orders

_ORDER_SURFACE = (
    # venue writes
    "set_leverage",
    "place_hl_order",
    "place_hl_trigger_order",
    "modify_sl_trigger",
    "cancel_orders",
    "cancel_open_orders_for_coin",
    # order sizing
    "entry_size_for_notional",
    "min_entry_notional_usd",
    # post-order read-only reconciliation
    "verify_order_exists",
    "find_open_order_by_cloid",
    "find_sl_trigger_in_open_orders",
    "reconcile_order_fill",
)


def test_orders_facade_reexports_same_objects():
    for name in _ORDER_SURFACE:
        assert getattr(orders, name) is getattr(exchange, name), name


def test_executor_binds_facade_order_objects_for_monkeypatch_seam():
    # Names the executor calls at module scope must resolve to the SAME
    # underlying object, regardless of which module it imported them from.
    for name in (
        "set_leverage",
        "place_hl_order",
        "place_hl_trigger_order",
        "modify_sl_trigger",
        "cancel_open_orders_for_coin",
        "entry_size_for_notional",
        "min_entry_notional_usd",
        "find_open_order_by_cloid",
        "find_sl_trigger_in_open_orders",
    ):
        assert getattr(executor, name) is getattr(exchange, name), name


def test_market_data_reads_kept_out_of_orders_facade():
    # The order-mechanics facade must not become a grab-bag of exchange fns.
    for name in ("get_hl_price", "get_hl_atr", "get_max_leverage",
                 "get_orderbook_spread", "get_all_hl_mids"):
        assert not hasattr(orders, name), name
