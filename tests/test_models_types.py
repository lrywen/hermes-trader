"""P1-3 — central boundary type layer guard.

Pins that the boundary DTOs (Candle / TriggerHit / GateContext) live in
hermes_trader.models.types and that risk_gates re-exports the SAME GateContext
object (existing ``from ...risk_gates import GateContext`` call sites must see
the moved class, not a duplicate definition). Also locks GateContext's
fail-safe __post_init__ coercion so the move is behaviour-preserving.
"""
from __future__ import annotations

import inspect

from hermes_trader.agents import risk_gates
from hermes_trader.models import types as types_mod
from hermes_trader.models.types import GateContext


def test_gatecontext_defined_in_models_types():
    assert GateContext is types_mod.GateContext
    # risk_gates must re-export the very same class, not redefine it.
    assert risk_gates.GateContext is GateContext


def test_gatecontext_has_no_duplicate_definition_in_risk_gates_source():
    src = inspect.getsource(risk_gates)
    assert "class GateContext" not in src, (
        "GateContext moved to models.types; risk_gates must only re-import it"
    )


def _minimal_kwargs(**overrides):
    base = dict(
        confidence="0.8",  # str -> float coercion
        current_positions="not-a-list",  # -> []
        trade_notional_usd=1000,
        daily_pnl=None,  # -> 0.0
        market_volume_24h_usd="5000000",
        coin=None,  # -> ""
        trade_side=None,  # -> "long"
        has_binary_news_risk=0,
        equity=20000,
        total_open_notional=3000,
    )
    base.update(overrides)
    return base


def test_gatecontext_coercion_preserved():
    ctx = GateContext(**_minimal_kwargs())
    assert ctx.confidence == 0.8
    assert ctx.current_positions == []
    assert ctx.daily_pnl == 0.0
    assert ctx.market_volume_24h_usd == 5_000_000.0
    assert ctx.coin == ""
    assert ctx.trade_side == "long"
    assert ctx.has_binary_news_risk is False
    assert ctx.composite_score == 0.0  # default


def test_gatecontext_nan_and_bool_coercion_preserved():
    nan = float("nan")
    ctx = GateContext(**_minimal_kwargs(
        confidence=nan,                 # NaN -> 0.0 (fail closed)
        debate_used=1,
        momentum_burst_fired=0,
    ))
    assert ctx.confidence == 0.0
    assert ctx.debate_used is True
    assert ctx.momentum_burst_fired is False
