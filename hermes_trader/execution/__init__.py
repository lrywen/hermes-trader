"""Execution layer: order placement / mutation / reconciliation.

See orders.py for the order-mechanics facade over the Hyperliquid venue.
P1-1 step ②; market-data reads remain in hermes_trader.client.exchange.
"""
from __future__ import annotations
