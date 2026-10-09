"""Active unstucking of underwater positions (strategy-paradigm P1-1).

Hermes historically treats every losing position the same way: wait for the
DSL hard stop or the time-based ``stale_flat_timeout``. Passivbot's *Unstucking*
instead actively realises small losses on stuck positions and — when several
are stuck — handles the one **closest to the market** first, freeing margin for
higher-ranked new signals and bounding drawdown against the equity peak.

This module is a pure decision layer over plain dicts: no network, no clock
reads (the caller passes ``now``), no order placement and no input mutation.
The caller gates via the ``unstucking`` config block (off/shadow/enforce) and
executes any accepted close through the existing reduce-only channel.

Severity model (why "closest to market first"): a position that is only mildly
underwater and liquid can be closed cheaply and reliably; a deeply underwater
one is left to its hard stop. We rank stuck positions by an urgency score that
blends relative loss, age, and opportunity cost, but only NUDGE when an
account-level trigger fires (drawdown pressure or too many stuck slots), so
this never becomes a tight stop that churns normal trades.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def from_hl_position(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Adapt one Hyperliquid nested asset-position row to an unstick dict.

    Accepts the ``{"type": ..., "position": {"coin", "szi", "entryPx",
    "markPx", "positionValue", ...}}`` shape. Returns ``None`` for a closed
    (zero szi) or malformed row so the caller can filter with a comprehension.
    """
    pos = raw.get("position") if isinstance(raw, Mapping) else None
    if not isinstance(pos, Mapping):
        return None
    try:
        szi = _f(pos.get("szi"))
    except Exception:
        return None
    if szi == 0.0:
        return None
    entry = _f(pos.get("entryPx") or pos.get("markPx"))
    if entry <= 0:
        return None
    notional = _f(pos.get("positionValue"))
    if notional <= 0:
        notional = abs(szi) * entry
    return {
        "coin": pos.get("coin"),
        "side": "long" if szi > 0 else "short",
        "entry_px": entry,
        # Hyperliquid does not report an open timestamp; age contribution is
        # therefore unavailable from this source (entry_time defaults to 0).
        "entry_time": 0.0,
        "mark_px": _f(pos.get("markPx")) or None,
        "notional_usd": notional,
    }


def from_shadow_position(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Adapt one shadow_book (flat) position row to an unstick dict.

    Shadow positions use the internal flat shape (``coin``, ``side``,
    ``entry_px``, ``mark_px``, ``size_usd``, ``opened_at``) rather than the
    nested Hyperliquid ``{"position": {...}}`` shape. Lets the unstucking arm
    run in SHADOW against virtual positions even when the real account holds
    nothing (otherwise the arm can never produce evidence). Returns None for a
    closed/malformed row.
    """
    coin = raw.get("coin")
    if not coin:
        return None
    entry = _f(raw.get("entry_px"))
    if entry <= 0:
        return None
    notional = _f(raw.get("size_usd"))
    if notional <= 0:
        notional = entry
    opened = raw.get("opened_at")
    # shadow_book stores opened_at in ms (>1e12); urgency wants seconds.
    opened_f = _f(opened)
    entry_time = opened_f / 1000.0 if opened_f > 1e12 else opened_f
    return {
        "coin": coin,
        "side": str(raw.get("side", "long")).lower(),
        "entry_px": entry,
        "entry_time": entry_time,
        "mark_px": _f(raw.get("mark_px")) or None,
        "notional_usd": notional,
    }


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if v != v else v  # NaN -> default


def unrealized_pct(pos: Mapping[str, Any], mark_px: float | None = None) -> float:
    """Signed spot (unleveraged) move vs entry, from the position's perspective.

    Long: positive when mark > entry; short: inverted. Uses an explicit
    ``mark_px`` when given, else the position's ``mark_px`` field, else its
    cached ``unrealized_pct``. Never raises.
    """
    if mark_px is None:
        mark_px = pos.get("mark_px")
    entry = _f(pos.get("entry_px"), 0.0)
    if mark_px is None or entry <= 0:
        return _f(pos.get("unrealized_pct"), 0.0)
    raw = (_f(mark_px) - entry) / entry
    return -raw if str(pos.get("side", "long")).lower() == "short" else raw


def _loss_severity(u_pct: float) -> float:
    """Desirability-to-close-now as a 0–1 weight; gains contribute 0.

    Peaks for a *mild, cheap-to-close* loss (~3–4% underwater) and falls back
    toward 0 for deep losses, which are hard-stop territory rather than cheap
    unstucks. This encodes Passivbot's "handle the position closest to the
    market first": a position only a few percent down is cheap and reliable to
    flatten and frees margin, while a deeply underwater one is not forced into
    a realised catastrophic loss here.
    """
    if u_pct >= 0:
        return 0.0
    x = (-u_pct) / 0.04  # 1.0 at -4% (the preferred close-to-market zone)
    # Triangular peak at x=1: ramp up 0->1 by x=1, decay back toward 0 by x=5
    # (~-20%). Below the peak it's just x; above, (5-x)/4 clipped to [0,1].
    if x <= 1.0:
        return max(0.0, min(1.0, x))
    return max(0.0, min(1.0, (5.0 - x) / 4.0))


def _age_minutes(pos: Mapping[str, Any], now: float) -> float:
    et = _f(pos.get("entry_time"), 0.0)
    if et <= 0:
        return 0.0
    # Real epoch seconds stay below ~1e11 for centuries; epoch millis are
    # >1e12, so the boundary cleanly disambiguates the two conventions.
    if et > 1e11:
        et /= 1000.0
    return max(0.0, (now - et) / 60.0)


def urgency_score(pos: Mapping[str, Any], now: float,
                  *, mark_px: float | None = None) -> float:
    """Return a 0–100 unstuck urgency score for one position.

    Higher = more desirable to close *cheaply soon*. Blends:
      - loss severity (mild-but-real losses score high enough to act)
      - age (stuck capital incurs opportunity cost over time)
      - notional at risk (bigger stuck slots free more margin when released)

    Deeply catastrophic positions are deliberately NOT ranked highest: they are
    hard-stop territory, not cheap unstucks.
    """
    u = unrealized_pct(pos, mark_px)
    sev = _loss_severity(u)
    if sev <= 0:
        return 0.0
    age = _age_minutes(pos, now)
    age_term = min(1.0, age / 480.0)  # saturates at 8h
    notional = max(0.0, _f(pos.get("notional_usd") or pos.get("notional")))
    # Normalise notional by a reference the caller may pass; default treats the
    # present notional as a moderate contribution rather than zero.
    ref = max(0.0, _f(pos.get("notional_ref_usd"), 0.0))
    notional_term = min(1.0, notional / ref) if ref > 0 else 0.5
    # Mild losses (severity ~0.3) that are liquid/aged get prioritised; weight
    # loss 0.5, age 0.3, notional 0.2.
    return 100.0 * (0.5 * sev + 0.3 * age_term + 0.2 * notional_term)


def drawdown_pressure(equity_usd: float, peak_equity_usd: float,
                      max_peak_drawdown_pct: float) -> bool:
    """Account-level trigger: equity within the configured peak-drawdown band.

    Mirrors Passivbot's "don't let equity fall more than X% below the historical
    peak" guard. Returns False when inputs are invalid or no pressure exists.
    """
    equity_usd = _f(equity_usd, -1.0)
    peak = _f(peak_equity_usd, 0.0)
    band = _f(max_peak_drawdown_pct, 0.0)
    if equity_usd < 0 or peak <= 0 or band <= 0:
        return False
    dd_pct = (peak - equity_usd) / peak * 100.0
    return dd_pct >= band


def rank_stuck(positions: Sequence[Mapping[str, Any]], now: float,
               *, marks: Mapping[str, float] | None = None
               ) -> list[tuple[dict[str, Any], float]]:
    """Return underwater positions as ``(position, score)`` best-to-unstick-first.

    Deterministic: descending score, then ascending coin name. Only losing
    positions are included.
    """
    out = []
    for pos in positions:
        mp = None
        if marks is not None:
            mp = marks.get(pos.get("coin"))
        s = urgency_score(pos, now, mark_px=mp)
        if s > 0:
            out.append((dict(pos), s))
    out.sort(key=lambda item: (-item[1], str(item[0].get("coin", ""))))
    return out


def select_unstucks(
    positions: Sequence[Mapping[str, Any]],
    now: float,
    *,
    equity_usd: float,
    peak_equity_usd: float,
    max_peak_drawdown_pct: float,
    max_active_slots: int | None = None,
    currently_stuck: int | None = None,
    marks: Mapping[str, float] | None = None,
    min_urgency: float = 20.0,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], float]], str | None]:
    """Decide which stuck positions to actively close.

    Unstucking only activates under an account-level trigger:
      - drawdown pressure, OR
      - too many slots occupied by stuck positions (``currently_stuck >=
        max_active_slots``).
    When inactive, nothing is selected (caller keeps normal DSL exits).
    When active, the highest-urgency positions above ``min_urgency`` are
    returned, up to the number needed to relieve the pressure.

    Returns ``(selected, ranked_remaining, trigger_reason)``.
    """
    ranked = rank_stuck(positions, now, marks=marks)
    pressured = drawdown_pressure(
        equity_usd, peak_equity_usd, max_peak_drawdown_pct)
    slots_full = (
        max_active_slots is not None and currently_stuck is not None
        and int(currently_stuck) >= int(max_active_slots) > 0)

    if not (pressured or slots_full):
        return [], ranked, None

    reason = "drawdown_pressure" if pressured else "stuck_slots_full"
    eligible = [(p, s) for p, s in ranked if s >= min_urgency]
    if not eligible:
        return [], ranked, reason

    # Under drawdown, relieve one slot at a time (conservative; the caller
    # re-evaluates next tick). Under slot pressure, free enough to drop below
    # the cap (at least one).
    if slots_full and max_active_slots is not None and currently_stuck is not None:
        need = max(1, int(currently_stuck) - int(max_active_slots) + 1)
    else:
        need = 1
    chosen = eligible[:need]
    selected = [p for p, _ in chosen]
    selected_ids = {id(p) for p in selected}
    remaining = [(p, s) for p, s in ranked if id(p) not in selected_ids]
    return selected, remaining, reason
