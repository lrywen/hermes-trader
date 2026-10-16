"""Net directional exposure (observation-only, Audit 2026-10-16 / problem #6).

Hermes tracks 74+ metrics but until this none looked at the *net* directional
exposure — i.e. whether the book's longs and shorts are netting off or silently
stacking one way. In a flat tape that stacking is invisible to the equity curve
until the market moves, at which point it is already a realized loss.

This module is a pure decision/reporting layer over plain dicts: no network, no
clock, no order placement, no input mutation. The caller (trading_loop) feeds
either Hyperliquid ``asset_positions`` rows or shadow_book position rows and
logs the resulting net fraction. Beta normalisation is deliberately NOT applied
here: the system has no per-coin beta source, so we report the raw signed
notional fraction and let a future beta layer refine it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def _norm_position(row: Mapping[str, Any]) -> dict[str, float] | None:
    """Normalise one position row to ``{notional, direction}``.

    Accepts both Hyperliquid asset_positions rows (``{"position": {"coin",
    "szi", "positionValue", ...}}``) and shadow_book rows (``{"coin", "side",
    "size_usd"}``). Returns None for a closed/malformed row.
    """
    pos = row.get("position") if isinstance(row, Mapping) else None
    if isinstance(pos, Mapping):
        # Hyperliquid row.
        try:
            szi = float(pos.get("szi") or 0)
        except (TypeError, ValueError):
            return None
        if szi == 0:
            return None
        try:
            nv = float(pos.get("positionValue") or 0)
        except (TypeError, ValueError):
            nv = 0.0
        if nv < 0:
            nv = -nv
        if nv == 0.0:
            # HL may omit positionValue on some rows; fall back to |szi| × mark.
            mark = float(pos.get("markPx") or 0) or float(pos.get("entryPx") or 0)
            if mark <= 0:
                return None
            nv = abs(szi) * mark
        direction = 1.0 if szi > 0 else -1.0
        return {"notional": nv, "direction": direction}
    # shadow_book row.
    side = row.get("side")
    if side not in ("long", "short"):
        return None
    try:
        size = float(row.get("size_usd") or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    return {"notional": size, "direction": 1.0 if side == "long" else -1.0}


def compute_net_exposure(
    positions: Iterable[Mapping[str, Any]],
    equity: float,
) -> dict[str, float]:
    """Compute signed net directional exposure over ``positions``.

    Returns:
      net_notional    : Σ notional × direction (signed; + long / - short)
      gross_notional  : Σ |notional|
      net_frac        : net_notional / equity (0 when equity<=0 or no rows)
      gross_frac      : gross_notional / equity
      long_notional   : Σ notional of longs
      short_notional  : Σ notional of shorts
      n_positions     : number of open rows counted
      max_dir_units   : max(|net_notional|/equity, 0) — the worst single-way
                        concentration as a fraction of equity
    """
    net_notional = 0.0
    gross_notional = 0.0
    long_notional = 0.0
    short_notional = 0.0
    n = 0
    for row in positions or []:
        if not isinstance(row, Mapping):
            continue
        norm = _norm_position(row)
        if norm is None:
            continue
        notional = norm["notional"]
        direction = norm["direction"]
        n += 1
        net_notional += direction * notional
        gross_notional += notional
        if direction > 0:
            long_notional += notional
        else:
            short_notional += notional

    eq = equity if equity and equity > 0 else 0.0
    net_frac = (net_notional / eq) if eq > 0 else 0.0
    gross_frac = (gross_notional / eq) if eq > 0 else 0.0
    max_dir_units = max(abs(net_notional) / eq, 0.0) if eq > 0 else 0.0
    return {
        "net_notional": net_notional,
        "gross_notional": gross_notional,
        "net_frac": net_frac,
        "gross_frac": gross_frac,
        "long_notional": long_notional,
        "short_notional": short_notional,
        "n_positions": n,
        "max_dir_units": max_dir_units,
    }
