"""Execution layer — order mechanics for the Hyperliquid venue.

P1-1 step ② (low-risk half): this package is the new home for *order
mechanics* as distinct from market-data reads. Today it is a thin facade
over ``hermes_trader.client.exchange`` (the single place that actually talks
to the Hyperliquid SDK). No logic is moved or copied: every name below is
the SAME object re-exported from client.exchange, so behaviour is identical
and both import paths keep working.

Scope of this facade — only order placement / mutation / reconciliation:
  * venue writes:  set_leverage, place_hl_order, place_hl_trigger_order,
                   modify_sl_trigger, cancel_orders, cancel_open_orders_for_coin
  * order sizing:  entry_size_for_notional, min_entry_notional_usd
  * post-order read-only reconciliation: verify_order_exists,
                   find_open_order_by_cloid, find_sl_trigger_in_open_orders,
                   reconcile_order_fill

Deliberately NOT here (they are market-data / venue reads and stay imported
directly from client.exchange): get_hl_price / get_hl_atr / get_max_leverage
/ get_orderbook_spread / mids / meta / Info access. Keeping that line keeps
the order-mechanics surface small and avoids a circular or grab-bag module.

The bracket/lifecycle helpers (_place_backup_sl, _register_filled_position,
_reconcile_unknown_order_result, ...) remain in agents.executor for now: they
own pending-SL persistence, the cross-process entry lock and DSL registry
writes, so moving them is the higher-risk half of ② that is intentionally
out of scope here.
"""
from __future__ import annotations

from hermes_trader.client.exchange import (
    cancel_open_orders_for_coin,
    cancel_orders,
    entry_size_for_notional,
    find_open_order_by_cloid,
    find_sl_trigger_in_open_orders,
    min_entry_notional_usd,
    modify_sl_trigger,
    place_hl_order,
    place_hl_trigger_order,
    reconcile_order_fill,
    set_leverage,
    verify_order_exists,
)

__all__ = (
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
