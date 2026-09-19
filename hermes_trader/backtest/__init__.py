"""Unified point-in-time backtest kernel.

Single-position, event-driven replay over closed bars that drives the SAME
production exit engine (``agents.dsl_exit.DSLTracker``) used by live trading.
The legacy research scripts each carried their own DSL re-implementation and
drifted (see tests/test_p4_dsl_parity.py D1-D6); this package is the single
replacement they wrap as thin CLIs (P4-5).
"""
from __future__ import annotations
