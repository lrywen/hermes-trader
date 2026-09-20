"""B-1a-改: byte-aligned reimplementation of the live effective-stop model.

The research backtest hard-coded ``lev=1`` and used
``eff_max_loss = dsl.max_loss_pct`` unconditionally (old
``scripts/bt_ra_exch.py`` L603-606). Production instead computes a
**leverage-aware, ATR-aware** spot stop (the 8 live ``reason`` strings invert to
0.40%–1.67%, never a flat 0.8/0.4):

    lev        = max(1, leverage)
    regime_cap = max_loss_pct
    if atr_stop_enabled and entry_atr_pct > 0:
        atr_cap   = clamp(entry_atr_pct * atr_mult, floor, ceiling)
        spot_cap  = min(regime_cap, atr_cap)      # ATR only widens up to regime
    else:
        spot_cap  = regime_cap
    roe_cap    = max_loss_roe_pct / lev
    effective  = min(spot_cap, roe_cap)

This is the SAME expression as
``ExitPolicyTracker._effective_max_loss`` (dsl_exit.py) and the executor's
sizing helper (executor.py ~L1058). It is duplicated here as a dependency-free
pure scalar function so the research backtest does not import the live agent
stack; ``tests/test_p4_stop_model_parity.py`` pins the two to identical output
across a covering grid (any future drift there fails CI).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EffectiveStop:
    """Effective spot-% stop plus which layer bound it (for ``[atr]`` labelling)."""

    spot_pct: float
    """Binding hard-stop width in SPOT percent (positive number)."""

    atr_active: bool
    """True when the ATR branch was taken (live renders the ``[atr]`` suffix)."""

    binding: str
    """Which cap bound: ``"regime"``, ``"atr"`` or ``"roe"``."""


def effective_stop_pct(
    *,
    max_loss_pct: float,
    leverage: float,
    max_loss_roe_pct: float = 5.0,
    atr_stop_enabled: bool = False,
    entry_atr_pct: float = 0.0,
    atr_mult: float = 1.5,
    atr_floor_pct: float = 1.0,
    atr_ceiling_pct: float = 4.0,
) -> EffectiveStop:
    """Effective SPOT-% hard stop, byte-aligned with the live DSL computation.

    ``max_loss_pct`` is the per-regime spot cap (trend 0.8 / non-trend 0.4 in
    production). ``leverage`` is the per-trade leverage (``lev = max(1, …)``);
    the ROE/margin cap in spot terms is ``max_loss_roe_pct / lev``. When
    ``atr_stop_enabled`` and a positive ``entry_atr_pct`` are supplied, an ATR
    width ``clamp(atr*mult, floor, ceiling)`` may WIDEN the stop, but only up to
    the regime cap — never override a tighter regime stop.

    B-11：``atr_stop_enabled`` 分支已 **DEPRECATED**（死代码）。生产数据下
    regime cap 恒胜出、atr_cap 从不 bind（§2.7 P4，W3 A-3）；入参仅为与
    DSLTracker 的 byte-aligned parity 而保留，禁止据此重新启用 ATR 止损。
    """
    lev = max(1.0, float(leverage))
    regime_cap = float(max_loss_pct) if float(max_loss_pct) > 0 else float("inf")

    atr_active = bool(atr_stop_enabled) and float(entry_atr_pct) > 0
    if atr_active:
        atr_cap = min(max(float(entry_atr_pct) * float(atr_mult),
                          float(atr_floor_pct)),
                      float(atr_ceiling_pct))
        spot_cap = min(regime_cap, atr_cap)
        spot_binding = "atr" if atr_cap < regime_cap else "regime"
    else:
        spot_cap = regime_cap
        spot_binding = "regime"

    roe_cap = (float(max_loss_roe_pct) / lev) if float(max_loss_roe_pct) > 0 \
        else float("inf")

    spot_cap = spot_cap if spot_cap > 0 else float("inf")
    effective = min(spot_cap, roe_cap)
    binding = "roe" if roe_cap < spot_cap else spot_binding
    return EffectiveStop(spot_pct=effective, atr_active=atr_active,
                         binding=binding)
