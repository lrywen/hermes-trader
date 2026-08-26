"""DSL (Dynamic Stop-Loss) exit engine.

Manages exit logic for open positions with a two-phase design:

  Phase 1 — Loss protection from entry until price moves up `protect_pct`.
  Phase 2 — Profit locking with tiered retrace thresholds once PnL is positive.

Unlike a plain SL order, DSL trails upward as price rises and only exits
when the mark price breaches the computed floor.

Phase 1 (Loss protection):
  - max_loss_pct below entry → hard stop
  - protect_pct above entry → transition to Phase 2
  - min(profit_floor, entry - max_loss)

Phase 2 (Profit locking):
  - trailing floor at entry + (peak - entry) * (1 - retrace_pct)
  - retrace_pct increases with unrealized profit (tiers)
  - hard_timeout after entry → emergency exit

Usage:
    dsl = ExitPolicy(max_loss_pct=3.0, protect_pct=1.5, ...)
    verdict = dsl.check(position_entry_price, current_mark_price, entry_time)
    if verdict.exit:
        close_position(reason=verdict.reason)
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from hermes_trader.client.hl_client import _http_post

logger = logging.getLogger(__name__)

# Persist tracker state so a daemon restart doesn't lose peak/floor ratchets.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DSL_STATE_FILE = os.environ.get(
    "HERMES_DSL_STATE_FILE",
    os.path.join(_REPO_ROOT, ".dsl-state.json"),
)
_STATE_VERSION = 2

# Cross-process lock file so concurrent writers (trading loop, rehydrate,
# dashboard) can't interleave tmp+replace and clobber each other's peak/floor.
DSL_STATE_LOCK_FILE = DSL_STATE_FILE + ".lock"


def _resolve_fill_time_ms(user: str, coin: str, side: str) -> Optional[float]:
    """Query the exchange's userFills for the most recent fill matching coin+side.

    Returns the fill time in seconds (epoch) or None on any failure.
    """
    try:
        fills = _http_post(
            "/info", {"type": "userFills", "user": user, "limit": 5}, timeout=5
        )
        if not isinstance(fills, list):
            return None
        # userFills returns newest-first; match the first fill for this coin+side.
        for f in fills:
            f_coin = f.get("coin", "")
            f_side = "long" if f.get("side") == "B" else "short" if f.get("side") == "A" else None
            if f_coin == coin and f_side == side:
                return int(f["time"]) / 1000.0
    except Exception:
        logger.debug(f"[dsl] fill-time lookup failed for {coin} {side} (non-fatal)")
    return None


def resolve_close_fill(user: str, coin: str, side: str,
                       since_ts: float) -> Optional[Dict[str, Any]]:
    """Find the most recent REDUCING fill for this position after ``since_ts``.

    Used to attribute an externally-closed position (an exchange-side SL/TP
    trigger, manual close, or liquidation that the DSL monitor never saw in
    real time) so the close/outcome record can be backfilled. Returns the fill
    dict (px, sz, time, closedPnl, fee, oid, …) or None when no reducing fill
    is found / the lookup fails.

    ``since_ts`` is epoch seconds; only fills newer than it are considered so a
    prior round-trip on the same coin isn't mis-attributed. A reducing fill is
    one whose side is opposite the position (A for a long, B for a short) OR
    that carries a non-zero ``closedPnl`` (HL tags the closing leg of a round
    trip with realized PnL). Newest-first ordering means the first match is the
    close.
    """
    try:
        fills = _http_post(
            "/info", {"type": "userFills", "user": user, "limit": 50}, timeout=8
        )
        if not isinstance(fills, list):
            return None
        want = "A" if side == "long" else "B"
        for f in fills:
            if f.get("coin") != coin:
                continue
            try:
                f_ts = int(f.get("time", 0)) / 1000.0
            except (TypeError, ValueError):
                continue
            if f_ts < since_ts:
                break  # fills are newest-first; nothing newer remains
            try:
                closed_pnl = float(f.get("closedPnl", 0) or 0)
            except (TypeError, ValueError):
                closed_pnl = 0.0
            if f.get("side") == want or closed_pnl != 0.0:
                return f
    except Exception as e:
        logger.debug(f"[dsl] close-fill lookup failed for {coin} {side}: {e}")
    return None


def _fetch_open_orders(user: str) -> List[Dict[str, Any]]:
    """Fetch the user's resting orders from the HL REST endpoint. [] on failure."""
    try:
        data = _http_post("/info", {"type": "openOrders", "user": user}, timeout=8)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug(f"[dsl] openOrders lookup failed (non-fatal): {e}")
        return []


def _order_trigger_px(o: Dict[str, Any]) -> Optional[float]:
    """Extract the trigger price from a resting trigger order.

    HL's openOrders exposes it as `triggerPx` for some order shapes and as
    `limitPx` for market tpsl orders; accept either.
    """
    for key in ("triggerPx", "limitPx"):
        v = o.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def backfill_brackets_from_exchange(user: Optional[str]) -> int:
    """Fill in missing sl_oid/tp_oid on trackers by scanning the user's open orders.

    Used on restart: a v1 state file (or a tracker synthesized before bracket
    persistence shipped) has no oids, so query the exchange once and match
    resting reduce-only trigger orders to trackers by coin + direction relative
    to entry. Only fills fields that are currently None — never overwrites an
    oid the trading path already set. Returns the number of trackers updated.
    Best-effort; never raises.
    """
    if not user or not _active_positions:
        return 0
    # Only query if at least one tracker is missing bracket info.
    if all(t.sl_oid is not None for t in _active_positions.values()):
        return 0
    orders = _fetch_open_orders(user)
    if not orders:
        logger.debug("[dsl] backfill: openOrders empty or unavailable")
        return 0
    updated = 0
    for tracker in _active_positions.values():
        # Consider every reduce-only resting order for this coin. HL's
        # openOrders does NOT reliably expose a trigger marker: live mainnet
        # market-tpsl orders come back with only `limitPx` (the trigger price)
        # and NO `triggerPx` / `orderType` fields. Requiring either would wrongly
        # drop the real backup SL/TP. Because these orders are reduceOnly on a
        # coin we hold, the trigger price's position relative to entry_px is
        # enough to classify SL (adverse side) vs TP (favourable side) below.
        coin_orders = [
            o for o in orders
            if o.get("coin") == tracker.coin
            and o.get("reduceOnly") is True
        ]
        if not coin_orders:
            continue
        for o in coin_orders:
            oid = _opt_int(o.get("oid"))
            tpx = _order_trigger_px(o)
            if oid is None or tpx is None:
                logger.debug(
                    f"[dsl] backfill: skipping {tracker.coin} order "
                    f"oid={oid} (no parseable trigger px; raw={o})"
                )
                continue
            # Classify by direction relative to entry: a backup SL for a long is
            # BELOW entry (sell trigger); a TP scale-out for a long is ABOVE entry.
            # Shorts are the mirror. This is robust even when the exchange payload
            # omits an explicit tpsl tag.
            is_sl_side = (tracker.is_long() and tpx < tracker.entry_px) or \
                         (not tracker.is_long() and tpx > tracker.entry_px)
            try:
                sz = abs(float(o.get("sz", 0) or 0))
            except (TypeError, ValueError):
                sz = None
            if is_sl_side and tracker.sl_oid is None:
                tracker.sl_oid = oid
                tracker.sl_px = tpx
                if sz:
                    tracker.sl_size = sz
                updated += 1
            elif not is_sl_side and tracker.tp_oid is None:
                tracker.tp_oid = oid
                tracker.tp_px = tpx
                updated += 1
    if updated:
        _save_state()
        logger.info(f"[dsl] backfilled {updated} bracket oid(s) from exchange openOrders")
    return updated


@dataclass
class RetraceTier:
    """A profit tier with its own retrace threshold.

    Example: when price is 10% above entry, retrace threshold is 30% —
    so the floor trails at entry + (peak - entry) * (1 - 0.30).
    """
    pct_above_entry: float  # Min profit % above entry to activate this tier
    retrace_threshold: float  # Fraction of peak profit to give back (0-1)


@dataclass
class ExitVerdict:
    """Result of a DSL floor check."""
    exit: bool = False
    reason: str = ""
    floor_price: Optional[float] = None
    peak_price: Optional[float] = None
    phase: str = ""  # "phase1" or "phase2"
    unrealized_pct: float = 0.0  # spot price-move %, not leveraged
    coin: str = ""
    position_side: str = ""  # "long" or "short" (distinct from `phase`)
    leverage: int = 1
    # Exit telemetry (populated by DSLTracker.check on exit verdicts):
    entry_regime: str = ""     # market regime captured at entry (up/down/neutral/chop)
    hold_min: float = 0.0      # minutes held from entry to exit
    mfe_pct: float = 0.0       # max favorable excursion (spot %), peak profit reached


@dataclass
class ExitPolicy:
    """DSL exit policy configuration.

    Tuning profiles:
      Conservative: max_loss_pct=5, retrace=10, protect=3, hard_timeout=360min
      Moderate:     max_loss_pct=2.5, retrace=7, protect=1.5, hard_timeout=180min
      Aggressive:   max_loss_pct=1.5, retrace=5, protect=0.8, hard_timeout=90min
    """
    max_loss_pct: float = 0.4  # Max loss SPOT % below entry (hard stop)
    # Max loss ROE (margin) % — leverage-aware safety net. At 12x leverage a
    # 0.4% spot move ≈ 5% ROE, so the spot threshold alone is meaningless on
    # high-lev trades. The effective floor used at check time is
    # min(max_loss_pct, max_loss_roe_pct / leverage). Set to a high value
    # (e.g. 100) to disable the leveraged cap and use spot only.
    max_loss_roe_pct: float = 5.0
    protect_pct: float = 1.25  # Price must rise this % above entry before Phase 2
    retrace_threshold: float = 0.20  # Give back 20% of peak profit (Phase 2 default)
    hard_timeout_minutes: float = 1800.0  # Emergency exit after this long
    # ── Breakeven ratchet (guaranteed-profit lock) ──────────────────────
    # Once a position's PEAK profit clears `breakeven_trigger_pct` (spot %),
    # the trailing floor may never fall below `breakeven_lock_pct` (spot %)
    # above entry. This is strictly risk-REDUCING on the upside: it only ever
    # raises the floor (for longs), never touches max_loss, and never affects
    # a position that hasn't yet reached the arm threshold. It targets the
    # documented leak where medium winners (peak +2–4%) round-trip back to
    # ~flat before the retrace floor catches them. Disabled when trigger<=0.
    breakeven_trigger_pct: float = 0.0  # Peak spot % that arms the lock (0 = off)
    breakeven_lock_pct: float = 0.0     # Floor spot % above entry once armed
    # ── ATR-scaled primary stop (volatility-aware max_loss) ─────────────
    # A fixed max_loss_pct is wrong across a universe whose 4h ATR spans
    # 0.8%–5%: it's noise-tight on volatile coins and slack on quiet ones.
    # When enabled, the Phase-1 stop becomes `atr_stop_mult × entry_atr_pct`
    # (the coin's ATR as a % of entry price, captured once at registration),
    # clamped to [atr_stop_floor_pct, atr_stop_ceiling_pct]. The leverage-aware
    # max_loss_roe_pct cap still applies on top. Positions registered without
    # an ATR (entry_atr_pct<=0) fall back to the fixed max_loss_pct.
    atr_stop_enabled: bool = False
    atr_stop_mult: float = 1.5
    atr_stop_floor_pct: float = 1.0
    atr_stop_ceiling_pct: float = 4.0
    # ── Stale-flat timeout (slot opportunity cost) ──────────────────────
    # Cut a position that has NEVER armed phase-2 (peak profit < protect_pct)
    # after this many minutes — it is statistically a drifter occupying a
    # scarce slot. Evidence 2026-06-12: ETH held 20h below protect at -6% ROE
    # while SIX breakout-override candidates (incl. xyz:RKLB) died at
    # max_concurrent. Positions that ever reached protect are EXEMPT (the
    # hard_timeout bucket's +3.41% avg is driven by agers that peaked).
    # 0 = off.
    stale_flat_timeout_minutes: float = 480.0
    phase2_tiers: List[RetraceTier] = field(default_factory=lambda: [
        RetraceTier(8.0, 0.35),   # 8% profit → give back 35%
        RetraceTier(15.0, 0.40),  # 15% profit → give back 40% (let winners run)
    ])
    consecutive_breaches_required: int = 1  # Number of consecutive floor breaches before exit
    # ── Patch A: don't exit inside the noise band (sub-first-tier) ──────────
    # Phase-3 finding: on strong movers we trailing-exited at +0.6–1.2% (the 0.30
    # give-back applied to a barely-green position) while the trend kept running,
    # then re-bought higher and stopped at the top. Below the FIRST phase-2 tier,
    # if a floor breach would fire but the pull-back from peak is still INSIDE the
    # name's volatility noise band, HOLD — let it clear the tier (real trend) or
    # fall to the hard max_loss stop (which is checked earlier and NOT suppressed).
    # The band is ATR-RELATIVE (noise_band_atr_mult × entry_atr_pct), not a
    # hardcoded %, so it generalizes across vol regimes. Same class of fix as the
    # old too-tight 1.2% stop: exiting inside the noise band is -EV churn.
    noise_band_enabled: bool = False
    noise_band_atr_mult: float = 1.0  # pull-back tolerated = this × entry ATR% (only sub-first-tier)


class DSLTracker:
    """Tracks DSL state for a single open position.

    Must be called on every price tick (e.g., from the scan loop's WS mids).
    """
    def __init__(self, coin: str, side: str, entry_px: float,
                 entry_time: float, policy: Optional[ExitPolicy] = None,
                 leverage: int = 1, entry_atr_pct: float = 0.0,
                 entry_regime: str = "") -> None:
        self.coin = coin
        self.side = side  # "long" | "short"
        self.entry_px = entry_px
        self.entry_time = entry_time
        self.policy = policy or ExitPolicy()
        self.leverage = int(leverage) if leverage else 1
        # Market regime at entry (up/down/neutral/chop); used for exit telemetry
        # so we can audit per-regime stop behavior (e.g. trailing-stop conservatism
        # in TREND vs STRONG_TREND). Empty when not provided (back-compat).
        self.entry_regime = entry_regime or ""
        # ATR as % of entry price, captured ONCE at registration so the stop
        # width is stable for the life of the trade (never recomputed per tick).
        self.entry_atr_pct = float(entry_atr_pct or 0.0)

        # State
        self.peak_px = entry_px
        self.consecutive_breaches = 0
        self._last_floor: Optional[float] = None

        # Exchange-side bracket order IDs (for the static backup SL / TP scale-out).
        # Persisted across restarts so the dynamic SL mover (batchModify) can target
        # the correct resting order and reconcile after an oid change (HL implements
        # modify as cancel+replace, so the oid changes on every move).
        self.sl_oid: Optional[int] = None
        self.sl_px: Optional[float] = None     # last-known trigger price of the backup SL
        self.sl_size: Optional[float] = None   # size covered by the backup SL
        self.tp_oid: Optional[int] = None
        self.tp_px: Optional[float] = None     # trigger price of the TP scale-out

    def is_long(self) -> bool:
        return self.side == "long"

    def _unrealized_pct(self, mark_px: float) -> float:
        if self.is_long():
            return (mark_px - self.entry_px) / self.entry_px * 100
        return (self.entry_px - mark_px) / self.entry_px * 100

    def _verdict(self, **kwargs: Any) -> ExitVerdict:
        """ExitVerdict pre-filled with coin/side/leverage so callers don't repeat."""
        # Max favorable excursion (spot %): peak_px is maintained as the
        # favorable extreme, so this is the largest open profit the trade saw.
        if self.is_long():
            mfe = (self.peak_px - self.entry_px) / self.entry_px * 100
        else:
            mfe = (self.entry_px - self.peak_px) / self.entry_px * 100
        return ExitVerdict(coin=self.coin, position_side=self.side,
                           leverage=self.leverage,
                           entry_regime=self.entry_regime,
                           hold_min=(time.time() - self.entry_time) / 60.0,
                           mfe_pct=mfe, **kwargs)

    def _active_tier(self, mark_px: float) -> RetraceTier:
        """Find the highest active retrace tier based on current profit."""
        upct = self._unrealized_pct(mark_px)
        active = RetraceTier(0.0, self.policy.retrace_threshold)  # default
        for tier in self.policy.phase2_tiers:
            if upct >= tier.pct_above_entry:
                active = tier
        return active

    def check(self, mark_px: float) -> ExitVerdict:
        """Evaluate DSL floor against current mark price. Call on every tick."""
        elapsed_min = (time.time() - self.entry_time) / 60
        upct = self._unrealized_pct(mark_px)
        is_long = self.is_long()
        pol = self.policy

        # Update peak (for longs: highest price seen; for shorts: lowest)
        peak_changed = False
        if is_long and mark_px > self.peak_px:
            self.peak_px = mark_px
            peak_changed = True
        elif not is_long and mark_px < self.peak_px:
            self.peak_px = mark_px
            peak_changed = True

        # ── Effective max-loss in SPOT % terms ───────────────────────
        # Two thresholds combine into one effective floor:
        #   * `max_loss_pct`        — direct spot-% cap (e.g. 2.5%)
        #   * `max_loss_roe_pct`    — ROE/margin cap, divided by leverage
        # At 40x leverage, max_loss_roe_pct=50 → 1.25% spot cap, which is
        # MUCH tighter than max_loss_pct=2.5. The min() takes whichever
        # fires first. Without this leverage-aware check, a 40x BTC long
        # would happily lose 100% ROE before the 2.5% spot trigger.
        lev = max(1, self.leverage)
        # ATR-scaled stop: replaces the fixed spot cap when enabled AND this
        # tracker captured an ATR at registration; clamped so an ATR spike at
        # entry can't set an unbounded stop. ROE cap still applies after.
        spot_cap = pol.max_loss_pct
        if pol.atr_stop_enabled and self.entry_atr_pct > 0:
            spot_cap = min(max(self.entry_atr_pct * pol.atr_stop_mult,
                               pol.atr_stop_floor_pct),
                           pol.atr_stop_ceiling_pct)
        # Guard against a misconfigured (zero/negative) cap, which would
        # otherwise make effective_max_loss == 0 and stop out the position on
        # the first tick. A non-positive cap means "disabled" → infinity.
        roe_cap = (pol.max_loss_roe_pct / lev) if pol.max_loss_roe_pct > 0 else float("inf")
        spot_cap = spot_cap if spot_cap > 0 else float("inf")
        effective_max_loss = min(spot_cap, roe_cap)
        # Reason string surfaces both inputs so it's obvious post-hoc
        # which cap was binding for a given exit.

        # ── Stale-flat timeout ────────────────────────────────────────
        # Only for positions that never armed phase-2: peak profit < protect.
        if pol.stale_flat_timeout_minutes > 0 and elapsed_min >= pol.stale_flat_timeout_minutes:
            if is_long:
                peak_profit = (self.peak_px - self.entry_px) / self.entry_px * 100
            else:
                peak_profit = (self.entry_px - self.peak_px) / self.entry_px * 100
            if peak_profit < pol.protect_pct:
                return self._verdict(
                    exit=True,
                    reason=(f"stale_flat_timeout ({elapsed_min:.0f}min below protect; "
                            f"peak {peak_profit:.2f}% < {pol.protect_pct}%)"),
                    floor_price=None, peak_price=self.peak_px, phase="timeout",
                    unrealized_pct=upct,
                )

        # ── Hard timeout ──────────────────────────────────────────────
        if elapsed_min >= pol.hard_timeout_minutes:
            return self._verdict(
                exit=True, reason=f"hard_timeout ({elapsed_min:.0f}min)",
                floor_price=None, peak_price=self.peak_px, phase="timeout",
                unrealized_pct=upct,
            )

        # ── Compute floor ───────────────────────────────────────────
        # Floor only moves UP (for longs) — once it rises above entry,
        # it never falls back. This prevents giving back locked profit.
        # retrace_used is logged on every floor change so the dynamic
        # trail can be verified against peak/tier in the logs.
        retrace_used = 0.0
        if is_long:
            profit_pct = (mark_px - self.entry_px) / self.entry_px * 100
            loss_pct = (self.entry_px - mark_px) / self.entry_px * 100

            # Max loss check (uses leverage-aware effective floor)
            # Use isclose on the boundary so a mark sitting exactly at the hard
            # stop (within floating-point noise) reports max_loss rather than
            # falling through to the phase-1 floor_breach — the hard stop must
            # win on priority even when the two floors coincide.
            if loss_pct >= effective_max_loss or math.isclose(
                loss_pct, effective_max_loss, rel_tol=1e-9, abs_tol=1e-9
            ):
                roe_loss = loss_pct * lev
                return self._verdict(
                    exit=True,
                    reason=(f"max_loss ({loss_pct:.2f}% spot / {roe_loss:.1f}% ROE "
                            f">= {effective_max_loss:.2f}% spot cap; "
                            f"spot_cap={spot_cap:.2f}{'[atr]' if (pol.atr_stop_enabled and self.entry_atr_pct > 0) else ''}, "
                            f"roe_cap={pol.max_loss_roe_pct}/{lev}x)"),
                    floor_price=self.entry_px * (1 - effective_max_loss / 100),
                    peak_price=self.peak_px, phase="phase1", unrealized_pct=upct,
                )

            if profit_pct >= pol.protect_pct:
                # Phase 2: floor = entry + profit_range * (1 - retrace)
                tier = self._active_tier(self.peak_px)  # Use PEAK for tier, not current
                retrace_used = tier.retrace_threshold
                profit_range = self.peak_px - self.entry_px
                floor = self.entry_px + profit_range * (1 - tier.retrace_threshold)
            else:
                # Phase 1: floor at effective max loss
                floor = self.entry_px * (1 - effective_max_loss / 100)
        else:
            # Short side
            profit_pct = (self.entry_px - mark_px) / self.entry_px * 100
            loss_pct = (mark_px - self.entry_px) / self.entry_px * 100

            if loss_pct >= effective_max_loss or math.isclose(
                loss_pct, effective_max_loss, rel_tol=1e-9, abs_tol=1e-9
            ):
                roe_loss = loss_pct * lev
                return self._verdict(
                    exit=True,
                    reason=(f"max_loss ({loss_pct:.2f}% spot / {roe_loss:.1f}% ROE "
                            f">= {effective_max_loss:.2f}% spot cap; "
                            f"spot_cap={spot_cap:.2f}{'[atr]' if (pol.atr_stop_enabled and self.entry_atr_pct > 0) else ''}, "
                            f"roe_cap={pol.max_loss_roe_pct}/{lev}x)"),
                    floor_price=self.entry_px * (1 + effective_max_loss / 100),
                    peak_price=self.peak_px, phase="phase1", unrealized_pct=upct,
                )

            if profit_pct >= pol.protect_pct:
                tier = self._active_tier(self.peak_px)
                retrace_used = tier.retrace_threshold
                profit_range = self.entry_px - self.peak_px
                floor = self.entry_px - profit_range * (1 - tier.retrace_threshold)
            else:
                floor = self.entry_px * (1 + effective_max_loss / 100)

        # ── Breakeven ratchet ─────────────────────────────────────────
        # Once PEAK profit has cleared the arm threshold, clamp the floor to a
        # locked-in gain so the position can't round-trip back to flat. Uses
        # PEAK (not current) so a dip after a high doesn't disarm it. Long-only
        # raises the floor; short-only lowers it — never loosens either side.
        if pol.breakeven_trigger_pct > 0:
            if is_long:
                peak_profit_pct = (self.peak_px - self.entry_px) / self.entry_px * 100
                if peak_profit_pct >= pol.breakeven_trigger_pct:
                    floor = max(floor, self.entry_px * (1 + pol.breakeven_lock_pct / 100))
            else:
                peak_profit_pct = (self.entry_px - self.peak_px) / self.entry_px * 100
                if peak_profit_pct >= pol.breakeven_trigger_pct:
                    floor = min(floor, self.entry_px * (1 - pol.breakeven_lock_pct / 100))

        # Floor should never decrease for longs (or increase for shorts)
        prev_floor = self._last_floor
        if prev_floor is not None:
            if is_long:
                floor = max(floor, prev_floor)
            else:
                floor = min(floor, prev_floor)

        self._last_floor = floor
        # Use a relative tolerance so floating-point noise between two
        # essentially-equal floors doesn't trigger a spurious save/log (which
        # also amplifies lock contention with other processes).
        floor_moved = prev_floor is None or not math.isclose(
            prev_floor, floor, rel_tol=1e-9, abs_tol=1e-12
        )
        if peak_changed or floor_moved:
            _save_state()
            # Log every floor update so the dynamic trail can be verified:
            # peak, active retrace %, new floor, and what moved it.
            logger.info(
                f"[dsl:floor] {self.coin} {self.side} "
                f"phase={'phase2' if retrace_used > 0 else 'phase1'} "
                f"entry={self.entry_px:.6g} mark={mark_px:.6g} "
                f"peak={self.peak_px:.6g} retrace={retrace_used*100:.0f}% "
                f"floor={floor:.6g} "
                f"(peak_changed={peak_changed}, prev_floor={prev_floor})"
            )

        # ── Floor breach check ────────────────────────────────────────
        # Use <=/>= (not strict inequality) so a mark sitting exactly on the
        # floor counts as a breach — matches exchange trigger-order semantics
        # and avoids the local/remote boundary disagreeing at the tick.
        breached = (is_long and mark_px <= floor) or (not is_long and mark_px >= floor)
        # Patch A — noise-band suppression (sub-first-tier only). The hard
        # max_loss stop already returned above; this only governs the trailing
        # give-back of a barely-green position. If peak profit hasn't yet cleared
        # the first phase-2 tier AND the current pull-back from peak is inside the
        # ATR noise band, treat it as NOT breached (hold) so we don't concede
        # inside the noise. Requires an ATR captured at entry; degrades to current
        # behavior when absent.
        if breached and pol.noise_band_enabled and self.entry_atr_pct > 0:
            first_tier_pct = min((t.pct_above_entry for t in pol.phase2_tiers), default=3.0)
            peak_profit_pct = (abs(self.peak_px - self.entry_px) / self.entry_px) * 100
            pullback_pct = (abs(self.peak_px - mark_px) / self.entry_px) * 100
            band = pol.noise_band_atr_mult * self.entry_atr_pct
            if peak_profit_pct < first_tier_pct and pullback_pct <= band:
                self.consecutive_breaches = 0
                self._last_floor = floor
                return self._verdict(
                    exit=False, reason="noise_band_hold", floor_price=floor,
                    peak_price=self.peak_px,
                    phase="phase1", unrealized_pct=upct,
                )
        if breached:
            self.consecutive_breaches += 1
            if self.consecutive_breaches >= pol.consecutive_breaches_required:
                return self._verdict(
                    exit=True,
                    reason=f"floor_breach ({self.consecutive_breaches}x consec, floor={floor:.2f})",
                    floor_price=floor, peak_price=self.peak_px,
                    phase="phase2" if self._unrealized_pct(mark_px) >= pol.protect_pct else "phase1",
                    unrealized_pct=upct,
                )
        else:
            self.consecutive_breaches = 0

        return self._verdict(
            exit=False, reason="", floor_price=self._last_floor,
            peak_price=self.peak_px,
            phase="phase2" if self._unrealized_pct(mark_px) >= pol.protect_pct else "phase1",
            unrealized_pct=upct,
        )

    def status(self, mark_px: float) -> Dict[str, Any]:
        """Return current DSL status dict (for logging/MCP)."""
        verdict = self.check(mark_px)
        return {
            "coin": self.coin,
            "side": self.side,
            "entry_px": self.entry_px,
            "mark_px": mark_px,
            "peak_px": verdict.peak_price,
            "floor_px": verdict.floor_price,
            "unrealized_pct": round(verdict.unrealized_pct, 2),
            "phase": verdict.phase,
            "consecutive_breaches": self.consecutive_breaches,
            "exit": verdict.exit,
            "exit_reason": verdict.reason,
        }


# ── Global tracker registry ──────────────────────────────────────────

_active_positions: Dict[str, DSLTracker] = {}
_loaded_from_disk = False


def _tracker_to_dict(t: DSLTracker) -> Dict[str, Any]:
    return {
        "coin": t.coin,
        "side": t.side,
        "leverage": t.leverage,
        "entry_px": t.entry_px,
        "entry_time": t.entry_time,
        "entry_atr_pct": t.entry_atr_pct,
        "entry_regime": t.entry_regime,
        "peak_px": t.peak_px,
        "consecutive_breaches": t.consecutive_breaches,
        "last_floor": t._last_floor,
        "policy": asdict(t.policy),
        # v2: exchange bracket order IDs / prices (None when no resting order).
        "sl_oid": t.sl_oid,
        "sl_px": t.sl_px,
        "sl_size": t.sl_size,
        "tp_oid": t.tp_oid,
        "tp_px": t.tp_px,
    }


def _tracker_from_dict(d: Dict[str, Any]) -> DSLTracker:
    pol_raw = d.get("policy") or {}
    tiers = [RetraceTier(**rt) for rt in pol_raw.get("phase2_tiers", [])]
    policy = ExitPolicy(
        max_loss_pct=pol_raw.get("max_loss_pct", ExitPolicy.max_loss_pct),
        max_loss_roe_pct=pol_raw.get("max_loss_roe_pct", ExitPolicy.max_loss_roe_pct),
        protect_pct=pol_raw.get("protect_pct", ExitPolicy.protect_pct),
        retrace_threshold=pol_raw.get("retrace_threshold", ExitPolicy.retrace_threshold),
        hard_timeout_minutes=pol_raw.get("hard_timeout_minutes", ExitPolicy.hard_timeout_minutes),
        phase2_tiers=tiers if tiers else ExitPolicy().phase2_tiers,
        consecutive_breaches_required=pol_raw.get("consecutive_breaches_required", 1),
        breakeven_trigger_pct=pol_raw.get("breakeven_trigger_pct", ExitPolicy.breakeven_trigger_pct),
        breakeven_lock_pct=pol_raw.get("breakeven_lock_pct", ExitPolicy.breakeven_lock_pct),
        atr_stop_enabled=pol_raw.get("atr_stop_enabled", ExitPolicy.atr_stop_enabled),
        stale_flat_timeout_minutes=pol_raw.get("stale_flat_timeout_minutes", 0.0),
        atr_stop_mult=pol_raw.get("atr_stop_mult", ExitPolicy.atr_stop_mult),
        atr_stop_floor_pct=pol_raw.get("atr_stop_floor_pct", ExitPolicy.atr_stop_floor_pct),
        atr_stop_ceiling_pct=pol_raw.get("atr_stop_ceiling_pct", ExitPolicy.atr_stop_ceiling_pct),
        noise_band_enabled=pol_raw.get("noise_band_enabled", ExitPolicy.noise_band_enabled),
        noise_band_atr_mult=pol_raw.get("noise_band_atr_mult", ExitPolicy.noise_band_atr_mult),
    )
    t = DSLTracker(d["coin"], d["side"], float(d["entry_px"]),
                   float(d.get("entry_time") or time.time()), policy,
                   leverage=int(d.get("leverage", 1) or 1),
                   entry_atr_pct=float(d.get("entry_atr_pct", 0.0) or 0.0),
                   entry_regime=str(d.get("entry_regime") or ""))
    t.peak_px = float(d.get("peak_px", d["entry_px"]))
    t.consecutive_breaches = int(d.get("consecutive_breaches", 0))
    lf = d.get("last_floor")
    t._last_floor = float(lf) if lf is not None else None
    # v2 fields (absent in v1 state files -> None, backfilled on next rehydrate).
    t.sl_oid = _opt_int(d.get("sl_oid"))
    t.sl_px = _opt_float(d.get("sl_px"))
    t.sl_size = _opt_float(d.get("sl_size"))
    t.tp_oid = _opt_int(d.get("tp_oid"))
    t.tp_px = _opt_float(d.get("tp_px"))
    return t


def _opt_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _save_state() -> None:
    """Atomically write the tracker registry to disk. Best-effort — never raises."""
    lock_fd = None
    try:
        payload = {
            "version": _STATE_VERSION,
            "saved_at": int(time.time() * 1000),
            "positions": [_tracker_to_dict(t) for t in _active_positions.values()],
        }
        # Cross-process exclusive lock prevents lost updates when the trading
        # loop races a rehydrate/dashboard write through the same file.
        lock_fd = os.open(DSL_STATE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        tmp = DSL_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, DSL_STATE_FILE)
    except OSError as e:
        logger.warning(f"[dsl] failed to persist state: {e}")
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def load_state(force: bool = False) -> None:
    """Load persisted trackers into `_active_positions`.

    Idempotent by default (skips after the first call in a process). Pass
    `force=True` from read-only consumers like the web dashboard that need to
    pick up the latest disk state on every request — the trading loop is in a
    different process and writes through the same file.
    """
    global _loaded_from_disk
    if _loaded_from_disk and not force:
        return
    _loaded_from_disk = True
    lock_fd = None
    try:
        # Shared lock pairs with the exclusive lock in _save_state so a
        # force-reload never observes a torn write.
        lock_fd = os.open(DSL_STATE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        with open(DSL_STATE_FILE) as f:
            payload = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[dsl] state file unreadable, ignoring: {e}")
        return
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass
    if force:
        _active_positions.clear()
    for d in payload.get("positions", []):
        try:
            t = _tracker_from_dict(d)
            _active_positions[f"{t.coin}_{t.side}"] = t
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[dsl] skipping malformed tracker entry: {e}")
    if not force:
        logger.info(f"[dsl] rehydrated {len(_active_positions)} tracker(s) from disk")


def register_position(coin: str, side: str, entry_px: float,
                      entry_time: Optional[float] = None,
                      policy: Optional[ExitPolicy] = None,
                      leverage: int = 1,
                      entry_atr_pct: float = 0.0,
                      entry_regime: str = "") -> DSLTracker:
    """Register a new position for DSL tracking."""
    key = f"{coin}_{side}"
    tracker = DSLTracker(coin, side, entry_px, entry_time or time.time(), policy,
                         leverage=leverage, entry_atr_pct=entry_atr_pct,
                         entry_regime=entry_regime)
    _active_positions[key] = tracker
    _save_state()
    atr_note = ""
    pol = tracker.policy
    if pol.atr_stop_enabled and entry_atr_pct > 0:
        width = min(max(entry_atr_pct * pol.atr_stop_mult, pol.atr_stop_floor_pct),
                    pol.atr_stop_ceiling_pct)
        atr_note = f" atr_stop={width:.2f}% ({pol.atr_stop_mult}x ATR {entry_atr_pct:.2f}%)"
    logger.info(f"[dsl] Registered {key} @ {entry_px} ({leverage}x){atr_note}")
    return tracker


def active_position_coins() -> Dict[str, str]:
    """coin -> side for every coin with an active DSL tracker.

    Restart-safe backstop against re-entry stacking: the DSL registry rehydrates
    from disk on startup, so it knows a position is held even in the window where
    a live account read flakes/returns empty (which would otherwise let the
    re-entry guard fail open and pyramid the position).
    """
    return {t.coin: t.side for t in _active_positions.values()}


def deregister_position(coin: str, side: str) -> bool:
    """Remove a tracker (after a successful close). Returns True if removed."""
    key = f"{coin}_{side}"
    if key in _active_positions:
        del _active_positions[key]
        _save_state()
        logger.info(f"[dsl] Deregistered {key}")
        return True
    return False


def get_tracker(coin: str, side: str) -> Optional[DSLTracker]:
    """Return the active tracker for a coin+side, or None."""
    return _active_positions.get(f"{coin}_{side}")


def set_bracket(coin: str, side: str, **fields: Any) -> bool:
    """Atomically update one or more exchange-bracket fields on a tracker and persist.

    Supported keyword fields: sl_oid, sl_px, sl_size, tp_oid, tp_px.
    Returns True if the tracker existed and was updated. Used after SL/TP
    placement and after every batchModify (which returns a NEW oid) so the
    persisted oid stays valid across restarts.
    """
    tracker = _active_positions.get(f"{coin}_{side}")
    if tracker is None:
        return False
    allowed = {"sl_oid", "sl_px", "sl_size", "tp_oid", "tp_px"}
    changed = False
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k.endswith("_oid"):
            v = _opt_int(v)
        elif k.endswith("_px") or k == "sl_size":
            v = _opt_float(v)
        if getattr(tracker, k, None) != v:
            setattr(tracker, k, v)
            changed = True
    if changed:
        _save_state()
        logger.debug(f"[dsl] bracket updated for {coin}_{side}: {fields}")
    return True


def _policy_from_config() -> ExitPolicy:
    """Build an ExitPolicy from the live .agent-config.json dsl_exit block.

    Mirrors the executor's entry-time policy construction so a SYNTHESIZED
    tracker (post-blackout reconcile) gets the SAME stops a fresh entry would,
    instead of the looser ExitPolicy() class defaults. Lazy import avoids a
    config_store <-> dsl_exit import cycle. Falls back to class defaults if the
    config can't be read."""
    try:
        from hermes_trader.agents.config_store import read_agent_config
        dsl = read_agent_config().get("dsl_exit", {}) or {}
        tiers_raw = dsl.get("phase2_tiers")
        tiers = [RetraceTier(**t) for t in tiers_raw] if tiers_raw else None
        atr_cfg = dsl.get("atr_stop", {}) or {}
        noise_cfg = dsl.get("noise_band", {}) or {}
        return ExitPolicy(
            max_loss_pct=dsl.get("max_loss_pct", ExitPolicy.max_loss_pct),
            max_loss_roe_pct=dsl.get("max_loss_roe_pct", ExitPolicy.max_loss_roe_pct),
            protect_pct=dsl.get("protect_pct", ExitPolicy.protect_pct),
            retrace_threshold=dsl.get("retrace_threshold", ExitPolicy.retrace_threshold),
            hard_timeout_minutes=dsl.get("hard_timeout_minutes", ExitPolicy.hard_timeout_minutes),
            breakeven_trigger_pct=dsl.get("breakeven_trigger_pct", ExitPolicy.breakeven_trigger_pct),
            breakeven_lock_pct=dsl.get("breakeven_lock_pct", ExitPolicy.breakeven_lock_pct),
            atr_stop_enabled=bool(atr_cfg.get("enabled", False)),
            atr_stop_mult=float(atr_cfg.get("atr_mult", ExitPolicy.atr_stop_mult)),
            atr_stop_floor_pct=float(atr_cfg.get("floor_pct", ExitPolicy.atr_stop_floor_pct)),
            atr_stop_ceiling_pct=float(atr_cfg.get("ceiling_pct", ExitPolicy.atr_stop_ceiling_pct)),
            stale_flat_timeout_minutes=float(dsl.get("stale_flat_timeout_minutes", 0.0) or 0.0),
            consecutive_breaches_required=int(dsl.get("consecutive_breaches_required", 1) or 1),
            noise_band_enabled=bool(noise_cfg.get("enabled", False)),
            noise_band_atr_mult=float(noise_cfg.get("atr_mult", ExitPolicy.noise_band_atr_mult)),
            phase2_tiers=tiers if tiers else ExitPolicy().phase2_tiers,
        )
    except Exception:
        return ExitPolicy()


def rehydrate_from_exchange(asset_positions: Iterable[Dict[str, Any]],
                            policy: Optional[ExitPolicy] = None,
                            default_leverage: int = 1,
                            queried_dexes: Optional[set] = None,
                            user: Optional[str] = None) -> List["DSLTracker"]:
    """Reconcile the tracker registry with the exchange's live position list.

    Synthesizes a tracker for any open position without one (entry_time =
    resolved from userFills when `user` is supplied, else now), and drops
    trackers for coins no longer open. When `queried_dexes` is given, a
    tracker is only dropped if its dex *successfully responded* this cycle
    — protecting trackers on timed-out dexes from being reset to fresh
    state next tick.

    Returns the list of trackers that were dropped because the exchange no
    longer reports the position (i.e. externally-closed fills: exchange-side
    SL/TP trigger, manual close, or liquidation). The caller uses this to
    backfill the close/outcome record so PnL accounting is not silently lost.
    """
    load_state()
    live_keys = set()
    added = 0
    dropped: List["DSLTracker"] = []
    for p in asset_positions or []:
        pos = p.get("position", {}) if isinstance(p, dict) else {}
        coin = pos.get("coin")
        if not coin:
            continue
        try:
            szi = float(pos.get("szi", "0") or 0)
            entry = float(pos.get("entryPx") or 0)
        except (TypeError, ValueError):
            continue
        if abs(szi) < 1e-12 or entry <= 0:
            continue
        side = "long" if szi > 0 else "short"
        key = f"{coin}_{side}"
        live_keys.add(key)
        pos_leverage = pos.get("leverage", {})
        lev = int(pos_leverage.get("value", 0) or 0) if isinstance(pos_leverage, dict) else int(pos_leverage or 0)
        if not lev:
            lev = default_leverage
        if key not in _active_positions:
            # Inherit the CURRENT config exit policy, never the bare ExitPolicy()
            # default. A synthesize happens after a blackout-induced drop (the
            # exchange momentarily reported the position gone), and the default
            # is LOOSER (2.5%/50% ROE vs config 2.0%/30%) — re-synthesizing with
            # the default silently widened live stops ("policy drift"). Pull
            # config when the caller didn't pass an explicit policy.
            synth_policy = policy if policy is not None else _policy_from_config()
            # Try to resolve the actual fill time so the hard_timeout is accurate.
            _entry_time = _resolve_fill_time_ms(user, coin, side) if user else None
            if _entry_time is None:
                # Resolution failed (API timeout/rate-limit) OR no user was
                # supplied. Falling back to now() is safe for genuinely new
                # positions but would RESET the timeout clock for an old
                # position being re-synthesized after a state wipe. Log loudly
                # so an operator can verify the position isn't being held past
                # its hard_timeout; do NOT silently pretend we know the age.
                if user:
                    logger.error(
                        f"[dsl] fill-time resolution FAILED for {key} @ {entry}; "
                        f"synthesizing with entry_time=now. hard_timeout clock is "
                        f"reset — verify this position is not stale."
                    )
                _entry_time = time.time()
            _active_positions[key] = DSLTracker(coin, side, entry, _entry_time, synth_policy,
                                                leverage=lev)
            added += 1
            logger.info(f"[dsl] Synthesized tracker for existing {key} @ {entry} ({lev}x)")
            # Record the open in the outcome store so rehydrated positions
            # aren't orphaned in memory.trades[] (previously only a tracker
            # was synthesized; the close would later arrive with no matching
            # open row — breaking the trades↔closes join and win-rate stats).
            # Idempotency check: skip if a trade for this coin/side already
            # exists within a 2% price band of the exchange entry, so a
            # blackout-induced re-synthesize does not double-count.
            try:
                from hermes_trader.agents.memory import memory as _mem
                import uuid as _uuid
                _already = any(
                    t.get("coin") == coin and t.get("side") == side
                    and abs(float(t.get("entry_px") or 0) - entry) / entry < 0.02
                    for t in _mem._trades
                )
                if not _already:
                    _mem.record_trade({
                        "id": str(_uuid.uuid4()),
                        "analysis_id": "rehydrate_synth",
                        "coin": coin,
                        "side": side,
                        "entry_px": entry,
                        "size_usd": None,
                        "order_id": None,
                        "executed_at": int(_entry_time * 1000),
                        "close_source": "rehydrate_synth",
                        "backfill_note": "tracker synthesized from live exchange position",
                    })
                    logger.info(
                        f"[dsl] rehydrated open recorded in outcome store: "
                        f"{key} @ {entry} ({lev}x)")
            except Exception as _rt_e:
                logger.warning(
                    f"[dsl] rehydrate record_trade failed for {key} "
                    f"(non-fatal): {_rt_e}")

    def _key_in_queried_scope(k: str) -> bool:
        """True iff the dex behind this tracker key was queried this cycle.
        Key format `<coin>_<side>`; coin format `BTC` or `xyz:MU`."""
        if queried_dexes is None:
            return True
        coin, _, _ = k.rpartition("_")
        dex = coin.split(":", 1)[0] if ":" in coin else ""
        return dex in queried_dexes

    stale = [k for k in _active_positions
             if k not in live_keys and _key_in_queried_scope(k)]
    for k in stale:
        dropped.append(_active_positions.pop(k))
        logger.info(f"[dsl] Dropped stale tracker {k} (no live exchange position)")
    skipped = [k for k in _active_positions
               if k not in live_keys and not _key_in_queried_scope(k)]
    if skipped:
        logger.warning(
            f"[dsl] Preserving {len(skipped)} tracker(s) whose dex query failed "
            f"this cycle (will retry next tick): {', '.join(skipped[:5])}"
            + (f" +{len(skipped)-5} more" if len(skipped) > 5 else "")
        )

    if added or stale:
        _save_state()

    # Best-effort: fill in any missing exchange bracket oids (e.g. after a restart
    # from a v1 state file, or a just-synthesized tracker). Skips the network call
    # when every tracker already has an sl_oid.
    if user:
        try:
            backfill_brackets_from_exchange(user)
        except Exception as e:
            logger.debug(f"[dsl] bracket backfill failed (non-fatal): {e}")

    return dropped


def check_all_positions(mids: Dict[str, float]) -> List[ExitVerdict]:
    """Check all active positions against current mids. Call each scan tick.

    Returns list of ExitVerdict for positions that should be closed.
    """
    exits = []
    for tracker in list(_active_positions.values()):
        mark_px = mids.get(tracker.coin)
        # Handle both str and float values from different sources
        if mark_px is not None:
            try:
                mark_px = float(mark_px)
            except (ValueError, TypeError):
                continue
            if mark_px > 0:
                verdict = tracker.check(mark_px)
                if verdict.exit:
                    exits.append(verdict)
    return exits
