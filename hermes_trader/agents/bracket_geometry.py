"""Bracket geometry primitives ported from upstream pathiel (P0, 2026-11-06).

Pure, zero-dependency, zero-state functions that validate the *geometry* of a
bracket (entry / stop / take-profit) BEFORE any order is placed. These complement
— not replace — the existing ``liquidation_buffer_gate`` in ``risk_gates.py``:

  * ``liquidation_buffer_gate`` (H4, 2026-08-29) enforces the coarse contract
    ``liq_distance_pct > stop_distance_pct + buffer`` using the simplified
    ``(1/lev - mmr)`` estimate.
  * ``check_leverage`` here uses the venue's exact isolated-margin closed form
    (``liq_estimate``, with the ``1 - mmr*side`` denominator) and additionally
    returns ``max_leverage_ok`` — the highest whole leverage the given stop
    would still pass at — so an operator can be told "this stop allows at most
    Nx" instead of just "refused".
  * ``check_levels`` adds the side + reward:risk (R multiple) validation the
    local gate set never had.
  * ``offset_target`` nudges the take-profit slightly inside the structure
    target to raise fill probability.

Nothing here reads config, the exchange, or memory; callers supply the prices
and leverage. Ported from ``pathiel/agents/bracket.py`` (upstream commit
7e4bce85 and the 2026-10-23/29/30 revisions). Only the deterministic,
transport-agnostic functions are ported — the order-placement state machine
stays with the local ``execution/`` and ``bracket_common.py``.
"""
from __future__ import annotations

from typing import Any

# The reward:risk a new trade must pay. Upstream moved to 2R on 2026-10-30
# (adopting its strategy sweep winner); the local default matches that but is
# overridable per call.
DEFAULT_MIN_R = 2.0

# The stop may use at most this share of the distance to liquidation, so a stop
# fires long before the venue force-closes. Half is the upstream rule (20x Brent
# record, 2026-10-23): at 20x liquidation sits ~2.5% away and one average hour
# moves ~2.7%, so only stops far inside that distance survived.
DEFAULT_MAX_STOP_FRAC_OF_LIQ = 0.5

# The take-profit sits this fraction of price inside the structure target:
# closer to the entry, so a move that only approaches the target still pays.
TARGET_OFFSET_FRAC = 0.005


def liq_estimate(entry_px: float, leverage: float, max_leverage: float, is_long: bool) -> float:
    """Hyperliquid isolated-margin liquidation price (exact closed form).

    liq = entry - side * (entry/leverage - mmr*entry) / (1 - mmr*side),
    mmr = 1/(2*max_leverage). Checked by upstream against the venue's own number
    (104.41 computed vs 104.43 shown for a 20x long; the gap is fees + funding).
    """
    side = 1.0 if is_long else -1.0
    mmr = 1.0 / (2.0 * max_leverage)
    return entry_px - side * (entry_px / leverage - mmr * entry_px) / (1.0 - mmr * side)


def max_leverage_for_stop(
    entry_px: float,
    stop_px: float,
    max_leverage: float,
    max_stop_frac: float = DEFAULT_MAX_STOP_FRAC_OF_LIQ,
) -> int:
    """Highest whole leverage at which this stop still uses no more than
    ``max_stop_frac`` of the liquidation distance.

    Closed form of ``liq_estimate``: the liquidation distance as a fraction of
    entry is ``(1/L - mmr)/(1 - mmr*side)``, so
    ``L <= 1 / (stop_frac/max_stop_frac * (1 - mmr*side) + mmr)``.
    """
    is_long = stop_px < entry_px
    side = 1.0 if is_long else -1.0
    mmr = 1.0 / (2.0 * max_leverage)
    s = abs(entry_px - stop_px) / entry_px
    if s <= 0:
        return 1
    lev = 1.0 / ((s / max_stop_frac) * (1.0 - mmr * side) + mmr)
    return max(1, min(int(max_leverage), int(lev + 1e-9)))


def check_leverage(
    entry_px: float,
    stop_px: float,
    leverage: float,
    coin_max_leverage: float,
    max_stop_frac: float = DEFAULT_MAX_STOP_FRAC_OF_LIQ,
) -> dict[str, Any]:
    """The "20x rule": the stop must sit inside ``max_stop_frac`` of the way to
    liquidation, so a stop fires long before the venue does.

    Returns the liquidation price, both distances, and — when it fails — the
    highest leverage that would pass with this stop (``max_leverage_ok``).
    """
    if min(entry_px, stop_px, leverage, coin_max_leverage) <= 0 or entry_px == stop_px:
        return {"ok": False, "reason": "entry, stop, leverage and coin max leverage must be positive"}
    is_long = stop_px < entry_px
    liq = liq_estimate(entry_px, leverage, coin_max_leverage, is_long)
    liq_dist = abs(entry_px - liq)
    stop_dist = abs(entry_px - stop_px)
    frac = stop_dist / liq_dist if liq_dist > 0 else float("inf")
    out: dict[str, Any] = {
        "ok": frac <= max_stop_frac + 1e-9,
        "leverage": leverage,
        "liq_px": round(liq, 6),
        "liq_dist_pct": round(liq_dist / entry_px * 100, 3),
        "stop_dist_pct": round(stop_dist / entry_px * 100, 3),
        "stop_frac_of_liq": round(frac, 3),
        "max_stop_frac": max_stop_frac,
        "max_leverage_ok": max_leverage_for_stop(entry_px, stop_px, coin_max_leverage, max_stop_frac),
    }
    if not out["ok"]:
        out["reason"] = (
            f"stop uses {frac:.0%} of the liquidation distance at {leverage:g}x "
            f"(max {max_stop_frac:.0%}); this stop allows at most {out['max_leverage_ok']}x"
        )
    return out


def check_levels(
    entry_px: float,
    stop_px: float,
    tp_px: float,
    min_r: float = DEFAULT_MIN_R,
) -> dict[str, Any]:
    """Side and reward:risk from the three prices.

    ``ok`` is False with a plain reason when they don't form a valid bracket
    (entry not between stop and target) or pay under ``min_r``.
    """
    if min(entry_px, stop_px, tp_px) <= 0:
        return {"ok": False, "reason": "prices must be positive"}
    if stop_px < entry_px < tp_px:
        side = "long"
    elif tp_px < entry_px < stop_px:
        side = "short"
    else:
        return {"ok": False, "reason": "entry must sit between stop and target"}
    r = abs(tp_px - entry_px) / abs(entry_px - stop_px)
    out = {"ok": r + 1e-9 >= min_r, "side": side, "r_multiple": round(r, 2), "min_r": min_r}
    if not out["ok"]:
        out["reason"] = f"pays {r:.2f}R, under the {min_r}R minimum"
    return out


def offset_target(target_px: float, side: str, frac: float = TARGET_OFFSET_FRAC) -> float:
    """The take-profit ``frac`` of price inside the structure target (the same
    formula the upstream backtest used): long x (1 - frac), short x (1 + frac).
    """
    if frac <= 0:
        return target_px
    return target_px * (1.0 - frac) if side == "long" else target_px * (1.0 + frac)
